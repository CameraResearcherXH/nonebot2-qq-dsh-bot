# services/vision_verify.py
"""
识图结果核验：把「视觉模型猜出来的专名」和「联网搜索结果」对一遍，
不一致就以搜索为准，并把结论写进缓存（下次同一张图不再搜）+ 长期记忆。

为什么要这么做：
  实测 VL 模型对**画面**的描述是可靠的（发色、服装、构图、道具基本不会错），
  但对**专名**（动漫角色 / 名人 / 作品）的幻觉率极高，实测约 60%。
  所以策略拆成两半：
      画面  → 以视觉为准（它真的看到了）
      专名  → 以搜索为准（它只是猜的）

三道成本闸门：
  ① 图片指纹缓存：同一张图只认一次、只搜一次（转发/复读/存表情都不再花钱）
  ② 没有专名就不搜：画面里没出现作品/角色 → 直接返回
  ③ 每小时核验次数上限：防止有人刷图把 tavily 额度打光
"""
from core import tuning
from . import ocr_rapid as ocr_mod
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

CACHE_FILE = Path("data/vision_cache.json")
CACHE_MAX = tuning.i("VISION_CACHE_MAX")          # 最多存多少张图
VERIFY_ENABLED = os.getenv("VISION_VERIFY", "true").lower() == "true"
VERIFY_MAX_PER_HOUR = tuning.i("VISION_VERIFY_MAX_PER_HOUR")
VERIFY_SEARCH_CHARS = 1500                                     # 喂给裁决的搜索文本上限

# ---- trace.moe：动漫截图「以图搜番」，免费、免 key ----
# 这是「直接对上人物」的唯一可靠办法：VLM 只能猜，它是拿画面去比对帧库。
TRACE_MOE = os.getenv("TRACE_MOE", "true").lower() == "true"
# 👇 2026-11-22：识图除了 trace.moe，还要给角色扮演模型一份「搜图源」证据
#    （SauceNAO / Ascii2D / IQDB）。用户的设计：
#    「看到图 → 同时给搜图源和识图 LLM → 两边的结果（画面描述 + 出处/角色）一起总结回复」。
#    和视觉模型并行跑，不额外等；失败/超时静默跳过。
EXTRA_SEARCH = os.getenv("VISION_EXTRA_SEARCH", "true").lower() == "true"


# 默认只跑快的两个：SauceNAO（插画/网图，最好配 key）+ Ascii2D。
# ⚠️ IQDB 实测一次要 40s+，会拖住图片回复 —— 想用就自己加：
#      VISION_EXTRA_ENGINES=saucenao,ascii2d,iqdb
EXTRA_ENGINES = tuple(
    x.strip().lower() for x in
    (os.getenv("VISION_EXTRA_ENGINES", "saucenao,ascii2d") or "").split(",")
    if x.strip())


async def _extra_sources(image_bytes: bytes, timeout: float = 8.0) -> list:
    """SauceNAO / Ascii2D 并行搜（trace.moe 已经单独跑过，不重复）。"""
    if not EXTRA_ENGINES:
        return []
    try:
        from . import image_search
        return await image_search.search(
            image_bytes, engines=EXTRA_ENGINES, timeout=timeout) or []
    except Exception as e:
        logger.debug(f"[VISION] 多源搜图失败（忽略）: {type(e).__name__}: {e}")
        return []
# ⚠️ 2026-11-19 放宽（用户：「用了一个月没有一次认出一张图」）：
#    旧规则是「相似度 ≥0.85 + 前 8 条里同一部 ≥3 票且占比 ≥60%」，
#    两条同时满足才算命中。实测一张 B 站截图（带字幕/弹幕/边框）：
#      · similarity 通常 0.72~0.88 —— 永远过不了 0.85
#      · 命中结果被拆成 3~4 部番、每部 1~2 票 —— 永远「太分散」
#    结果是这条链路形同虚设。现在分三档：
#      高置信  ≥ TRACE_STRONG_SIM 且 ≥2 票  → 当事实用（可写记忆）
#      疑似    ≥ TRACE_MIN_SIM              → 采信但标注「疑似」，交给 DeepSeek 自己判断
#      线索    <  TRACE_MIN_SIM             → 不采信，但候选仍列进 report 当线索
#    另加一条兜底：单帧相似度 ≥ TRACE_SINGLE_SIM 直接放行（几乎是同一帧）。
TRACE_MIN_SIM = tuning.f("TRACE_MIN_SIM")        # 采信下限（疑似）
TRACE_STRONG_SIM = tuning.f("TRACE_STRONG_SIM")  # 高置信
TRACE_SINGLE_SIM = tuning.f("TRACE_SINGLE_SIM")  # 单条即可采信
TRACE_MIN_VOTES = tuning.i("TRACE_MIN_VOTES")         # 至少几票
TRACE_MIN_SHARE = tuning.f("TRACE_MIN_SHARE")    # 最高票至少占几成
# 纯色/极简图的灰度标准差阈值（挡掉「纯色帧 similarity 0.98」的假命中）
TRACE_FLAT_STD = tuning.f("TRACE_FLAT_STD")
# trace.moe 每天只有 100 次配额（返回里的 quota 字段），别按小时算
TRACE_MAX_PER_DAY = tuning.i("TRACE_MAX_PER_DAY")
TRACE_MOE_URL = "https://api.trace.moe/search"
ANILIST_GQL = "https://graphql.anilist.co"

# 第二视觉证据源（2026-11-27 从「写死 GLM」改成「可配 provider」）：
#   用户拍板：GLM 额度用完，先靠 qwen；但保留「可选第二家」的能力。
#   配置方法：VISION_SECOND_PROVIDER=glm / openai / 自定义名（+ _MODEL/_API_KEY/_BASE_URL）。
#   **不配 = 关闭**，不会因为主模型有 key 就误调第二家。
# 模式：
#   on_miss  = trace 没命中且 qwen 没给出专名时才问（默认，省一半调用）
#   always   = 每张图都问（两家对拍，最贵）
#   off      = 关
_SECOND_MODE = (os.getenv("VISION_SECOND_MODE")
                or os.getenv("VISION_GLM_MODE")
                or tuning.raw("VISION_SECOND_MODE")
                or "on_miss").strip().lower()


def _second_target() -> dict:
    """第二视觉源配置；没配齐 / 和主视觉完全同一家 → {}。"""
    try:
        from . import providers
        p = providers.resolve("vision_second")
        if not all(str(p.get(k) or "").strip()
                   for k in ("provider", "base_url", "api_key", "model")):
            return {}
        main = providers.resolve("vision")
        if (p["base_url"] == main["base_url"] and p["model"] == main["model"]):
            return {}       # 同一家同一个模型，问两遍纯浪费
        return p
    except Exception as e:
        logger.debug(f"[VISION] 第二视觉源解析失败（忽略）: {type(e).__name__}: {e}")
        return {}

# 第四层：定向追问。前三层都没给出专名时，换一个**明确的问法**再问一次视觉模型。
# 为什么有效：问「这是什么」模型倾向描述画面，问「这是谁」它才被迫给名字；
# cosplay / 痛车 / 海报这种场景，不点破它就不会主动说角色名。
TARGETED_ASK = os.getenv("VISION_TARGETED_ASK", "true").lower() == "true"

TARGETED_PROMPT = (
    "这张图里出现了哪些**能叫出名字的角色 / 人物 / IP**？\n"
    "注意这几种情况都要看：\n"
    "- 如果是真人 cosplay，指出被 cos 的角色名和作品名\n"
    "- 如果车身上、衣服上、海报上、屏幕里有角色图案，也把那个角色说出来\n"
    "- 如果是原创形象、品牌吉祥物、网红本人，就说是谁\n"
    "- 真的认不出就写「认不出」，绝对不要编一个名字\n\n"
    "严格按两行输出，不要多余内容：\n"
    "角色：名字（多个用、分隔）；认不出就写「认不出」\n"
    "作品：出自哪部作品；认不出就写「认不出」"
)

# 结构化识图 prompt：逼模型把「看到的」和「猜的」分开写，
# 这样我们才知道哪部分需要核验、哪部分可以直接信。
#
# ⚠️ 2026-11-11 改：要求**必须给猜测**（标注「猜」），而不是认不出就写"不确定"。
#    原因：DeepSeek 拿到只有画面的描述，只能自己瞎猜角色（用户：「猜角色为什么
#    一定要 deepseek 猜，还浪费时间」）。识图模型是 LLM，它有知识，让它顺手给猜测，
#    标好「猜」让 DeepSeek 参考即可 —— 比让 DeepSeek 从零猜准得多，也快得多。
# 文字行（2026-11-27 P1）：装了 RapidOCR 之后，全文交给 OCR（确定性、不改写），
# 视觉模型只负责「有没有字 + 最关键一句」，省 output token、也不再产出改写版原文。
# OCR 关闭时退回旧行为（逐字照抄），保证没装依赖的部署不掉能力。
_TEXT_LINE_FULL = (
    "文字：图里若有文字就**逐字照抄原话**（最多 150 字，不要总结、不要改写、不要翻译）；"
    "没有文字写「无」\n"
)
_TEXT_LINE_OCR = (
    "文字：图里若有文字，只抄最关键的 1 句（≤30 字，不要逐字抄全文、不要翻译；"
    "另有 OCR 负责全文）；没有文字写「无」\n"
)
_TEXT_LINE = _TEXT_LINE_OCR if ocr_mod.ENABLED else _TEXT_LINE_FULL

VISION_PROMPT_STRUCT = (
    "看这张图，严格按下面六行输出，不要任何多余内容、不要分点解释：\n"
    "标记：如果画面是成人/擦边/血腥/恶心内容写「NSFW」；否则写「正常」\n"
    "类型：如果是截图/聊天记录/文档/表格/网页这类「以文字为主」的图写「文字图」；否则写「图片」\n"
    "画面：只写你真正看到的（发色、发型、服装、动作、构图、道具），"
    "不超过 60 字，不要写作品名或人名\n"
    + _TEXT_LINE +
    "作品：认得出就写作品名；认不出但能联想到最像的，写「X（猜）」；完全没头绪写「不确定」\n"
    "角色：同上。哪怕只有三成把握也写出来，格式「名字（猜）」；完全没头绪写「不确定」"
)
# 动图（GIF 抽帧网格）专用 prompt。
#
# 用户 2026-11-20：「动图的时候会把所有帧的解析结果一块给 deepseek，让他判断，
#   我觉得这样不对，应该视觉模型直接总结这张动图的内容，然后把总结给 deepseek。」
# 所以这里**明确禁止逐格罗列**，只要一句「动作过程」的总结；DeepSeek 只该看到结论。
GIF_PROMPT_STRUCT = (
    "这是一张**动图**抽帧拼成的网格图（按时间顺序：左→右、上→下）。\n"
    "严格按下面五行输出，不要任何多余内容、不要分点解释：\n"
    "标记：如果画面是成人/擦边/血腥/恶心内容写「NSFW」；否则写「正常」\n"
    "类型：表情包 / 动漫截图 / 实拍 / 截图 / 其他（选一个）\n"
    "画面：用**一句话总结整段动画在演什么**（谁/什么 + 做了什么 + 从什么变成什么 + 笑点），"
    "不超过 60 字。**不要逐格描述**（不要写「第一帧…第二帧…」「左上…右下…」）\n"
    "文字：" + ("只抄最关键的 1 句（≤20 字，不要逐字抄，另有 OCR）；没有写「无」\n"
               if ocr_mod.ENABLED else
               "图里若有文字就**逐字照抄原话**（最多 60 字，不要总结）；没有写「无」\n") +
    "角色：认得出就写「作品·角色」；认不出但能联想到最像的，写「X（猜）」；"
    "完全没头绪写「不确定」\n"
)

# ⚠️ 为什么加「类型」「文字」两行（2026-11-13）：
#   原来只有四行（标记/画面/作品/角色），是**动漫识图**的格式。
#   「画面文字」被塞在「画面」的括号里，还整行限 50 字 →
#   贴吧截图只能被描述成「社交媒体评论区截图，含中文段子文本」，一个字都拿不到，
#   于是她只能瞎猜这张图跟哪个话题有关，回错了话题。
#   现在：类型让模型自己声明这是不是文字图；文字单独成行、单独配额、原样照抄。

_KEYS = {
    "画面": "scene", "场景": "scene", "描述": "scene", "content": "scene",
    "作品": "work", "出处": "work", "番剧": "work", "来源": "work",
    "角色": "char", "人物": "char", "name": "char",
    "标记": "nsfw", "内容分级": "nsfw", "分级": "nsfw", "nsfw": "nsfw",
    # 👇 OCR：截图/聊天记录/文档的全部信息都在这里，必须能解析进来
    "文字": "text", "文本": "text", "内容": "text", "ocr": "text", "text": "text",
    "类型": "kind", "类别": "kind", "kind": "kind",
}
# 「（猜）」这种标注：判名字前先剥掉，但要把"这是猜的"这个信息保留下来
_GUESS_MARK = re.compile(r"[（(]\s*猜(?:测)?\s*[)）]")
_BAD_NAMES = {"", "不确定", "不知道", "无法确定", "未知", "无", "none", "unknown",
              "n/a", "na", "null", "-", "—", "?", "？",
              "认不出", "认不出来", "看不出", "没有", "无角色", "无法识别"}
_WRAP = "《》「」『』\"'“”‘’()（）[]【】 "


# ================== 指纹 & 缓存 ==================
def fingerprint(image_bytes: bytes) -> str:
    """图片指纹：同一张图（哪怕被转发、被重新压缩前的原图）算同一个 key。"""
    if not image_bytes:
        return ""
    return hashlib.sha1(image_bytes).hexdigest()[:16]


def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 超上限就按最久没用过的淘汰（近似 LRU）
        if len(cache) > CACHE_MAX:
            keep = sorted(cache.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
            cache = dict(keep[:CACHE_MAX])
        CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as e:
        logger.debug(f"[VISION] 缓存写入失败: {type(e).__name__}: {e}")


# 缓存格式版本：改了「报告怎么写」就 +1，老条目自动失效。
# 2026-11-20：动图从「逐帧描述」改成「一句话总结」→ v2。
CACHE_VER = 2


def get_cached(fp: str) -> dict | None:
    if not fp:
        return None
    rec = _load_cache().get(fp)
    if not isinstance(rec, dict):
        return None
    if int(rec.get("v") or 1) != CACHE_VER:
        return None          # 旧格式（逐帧描述）→ 当没缓存，重新识别
    # 命中即续命，别让常发的图被淘汰
    rec["ts"] = time.time()
    cache = _load_cache()
    cache[fp] = rec
    _save_cache(cache)
    return rec


def put_cache(fp: str, rec: dict) -> None:
    if not fp:
        return
    cache = _load_cache()
    rec = dict(rec)
    rec["ts"] = time.time()
    rec["v"] = CACHE_VER
    cache[fp] = rec
    _save_cache(cache)


# ================== 结构化输出解析 ==================
def _clean_name(s: str) -> str:
    v = str(s or "").strip()
    v = _GUESS_MARK.sub("", v)            # 先剥掉「（猜）」—— 必须在 strip(_WRAP) 之前，
    v = v.strip().strip(_WRAP).strip()    # 否则右括号会先被剥掉，标记就残缺了
    if v.lower() in _BAD_NAMES:
        return ""
    if len(v) > 16:          # 太长的不是名字，是句子（真实角色名最长也就「阿尼亚·福杰」这种）
        return ""
    return v


def parse_structured(text: str) -> dict:
    """
    把 VL 的多行输出拆成 {scene, work, char, nsfw, text, kind}。
    模型经常不老实（多写一行、用冒号/顿号/换行混着来），这里尽量宽松地捡。
    """
    out = {"scene": "", "work": "", "char": "", "nsfw": "", "guess": False,
           "text": "", "kind": ""}
    raw = str(text or "")
    if not raw.strip():
        return out

    for line in raw.splitlines():
        line = line.strip().lstrip("-*·• ").strip()
        if not line:
            continue
        m = re.match(r"^([\u4e00-\u9fa5A-Za-z]{2,4})\s*[:：]\s*(.+)$", line)
        if not m:
            continue
        key = _KEYS.get(m.group(1).strip())
        if key and not out[key]:
            out[key] = m.group(2).strip()

    # 兜底：模型完全没按格式写 → 整段当画面（此时不核验，至少描述没错）
    if not out["scene"]:
        first = raw.strip().splitlines()[0].strip() if raw.strip() else ""
        out["scene"] = first[:80]

    # 「（猜）」标记要在清洗名字之前判，清洗会把括号剥掉
    out["guess"] = bool(_GUESS_MARK.search(raw))
    out["nsfw"] = "NSFW" if "nsfw" in str(out.get("nsfw") or "").lower() else ""
    out["work"] = _clean_name(out["work"])
    out["char"] = _clean_name(out["char"])
    # 👇 文字**绝不能**走 _clean_name（那会把超过 16 字的整段判成「不是名字」丢掉）。
    #    OCR 结果是原文照抄，长度本来就该长。
    out["text"] = str(out.get("text") or "").strip().strip("「」『』\"'“”")
    if out["text"] in ("无", "没有", "无文字", "没有文字", "none", "null", "-", "—", "N/A"):
        out["text"] = ""
    return out


def compose(scene: str, work: str, char: str, guess: bool = False,
            nsfw: str = "", text: str = "", kind: str = "") -> str:
    """
    拼成最终给 bot 的一句话描述。专名放括号里，没专名就只留画面。

    guess=True → 专名后面标「仅猜测」，提醒 DeepSeek 别当成事实去接话。
    nsfw 非空 → 前面加 [NSFW]，让她知道这是擦边图，别认真分析画面。
    text 非空 → 图里的文字原样带上；这是文字类图片（截图/聊天记录/文档）的全部信息。

    ⚠️ 为什么 text 一定要进 desc（2026-11-13 实测踩过）：
       贴吧截图原来只被描述成「社交媒体评论区截图，含中文段子文本」，
       desc 里一个字都没有 → 她只能瞎猜这图跟哪个话题有关 → 回错话题。
       文字图的信息 100% 在文字里，画面描述只是补充。
    """
    scene = str(scene or "").strip()
    text = str(text or "").strip()
    is_text_img = "文字" in str(kind or "")
    tag = "·".join(x for x in (work, char) if x)
    if guess and tag:
        tag += "，仅猜测"
    head = "[NSFW] " if nsfw else ""

    parts = []
    if text:
        if is_text_img or not scene:
            # 文字图：文字在前，画面退成补充说明
            parts.append(f"图里文字：{text}")
            if scene:
                parts.append(f"（画面：{scene}）")
        else:
            # 普通图：画面在前，文字作为附加线索（图里的小字/水印/台词）
            parts.append(scene)
            parts.append(f"图里文字：{text}")
    else:
        parts.append(scene)

    body = "。".join(p for p in parts if p)
    if tag:
        body += f"（{tag}）" if body else tag
    return head + body


# ================== 搜索 + 裁决 ==================
_hour_window: list = []          # 最近一小时内的核验时间戳
_trace_window: list = []         # trace.moe 的调用时间戳（它有限速，别打爆）


def _allow_verify() -> bool:
    now = time.time()
    while _hour_window and now - _hour_window[0] > 3600:
        _hour_window.pop(0)
    if len(_hour_window) >= VERIFY_MAX_PER_HOUR:
        logger.info(f"[VISION] 本小时核验已达上限 {VERIFY_MAX_PER_HOUR} 次，本次跳过")
        return False
    _hour_window.append(now)
    return True


def _allow_trace() -> bool:
    now = time.time()
    while _trace_window and now - _trace_window[0] > 86400:
        _trace_window.pop(0)
    if len(_trace_window) >= TRACE_MAX_PER_DAY:
        logger.info(f"[VISION] 今天 trace.moe 已达上限 {TRACE_MAX_PER_DAY} 次（它一天只给 100 次）")
        return False
    _trace_window.append(now)
    return True


def _looks_flat(image_bytes: bytes) -> bool:
    """
    纯色 / 极简图（截图、色块、黑屏）直接跳过 trace.moe。

    为什么必须挡：实测一张纯色图能拿到 similarity=0.981，
    因为动漫里有大量纯色转场帧。不挡的话每张图都会"命中"一部不相干的番。
    """
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(image_bytes)).convert("L")
        im.thumbnail((64, 64))
        px = list(im.getdata())
        n = len(px) or 1
        mean = sum(px) / n
        var = sum((p - mean) ** 2 for p in px) / n
        return (var ** 0.5) < TRACE_FLAT_STD   # 灰度标准差太小 = 没内容
    except Exception:
        return False                       # 没装 Pillow 就不挡


# ================== GIF：抽帧 ==================
def gif_frames(image_bytes: bytes, limit: int = 0) -> list:
    """
    GIF → 单帧 JPEG bytes 列表。limit=0 表示全抽。

    为什么要抽：GLM 视觉直接喂 gif 会报 1210，qwen 也只认第一帧。
    用户 2026-11-19 定的流程是「全帧给视觉模型 / 均分三帧给 trace.moe」。
    """
    try:
        import io as _io
        from PIL import Image
    except Exception:
        return []
    out = []
    try:
        im = Image.open(_io.BytesIO(image_bytes))
        total = int(getattr(im, "n_frames", 1) or 1)
        idxs = list(range(total))
        if limit and total > limit:
            step = total / float(limit)
            idxs = [min(total - 1, int(i * step)) for i in range(limit)]
        for i in idxs:
            try:
                im.seek(i)
                fr = im.convert("RGB")
                fr.thumbnail((640, 640))
                buf = _io.BytesIO()
                fr.save(buf, format="JPEG", quality=85)
                out.append(buf.getvalue())
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"[VISION] GIF 抽帧失败: {type(e).__name__}: {e}")
        return []
    return out


def gif_grid(image_bytes: bytes, cols: int = 3, rows: int = 2) -> bytes:
    """
    GIF 抽帧拼成一张静态网格图（PNG），给视觉模型一次看完。

    为什么拼图而不是只给第一帧：动图的表情/动作变化才是笑点，
    只看第一帧会把「笑着笑着哭了」看成「一个笑脸」。

    ⚠️ 2026-11-20 从 4x4=16 格改成 3x2=6 格：
       格子一多，视觉模型就开始**逐格报菜名**（「第一格…第二格…」），
       这一大段逐帧描述又被原样塞给 DeepSeek，等于让它自己再总结一遍。
       6 帧（开头/过程/结尾）足够看出动作，模型也更愿意给一句话总结。
    """
    try:
        import io as _io
        from PIL import Image
    except Exception:
        return b""
    try:
        im = Image.open(_io.BytesIO(image_bytes))
        total = int(getattr(im, "n_frames", 1) or 1)
        cap = cols * rows
        if total <= cap:
            idxs = list(range(total))
        else:
            # 均分取样（开头/中间/结尾），别只取前 6 帧 —— 很多 gif 前几帧是静止的
            idxs = sorted({int(round(i * (total - 1) / max(1, cap - 1)))
                           for i in range(cap)})
        if not idxs:
            return b""
        cell_w, cell_h = 220, 220
        grid = Image.new("RGB", (cols * cell_w, rows * cell_h), (24, 24, 24))
        for n, i in enumerate(idxs):
            try:
                im.seek(i)
                fr = im.convert("RGB")
                fr.thumbnail((cell_w - 6, cell_h - 6))
                x = (n % cols) * cell_w + (cell_w - fr.width) // 2
                y = (n // cols) * cell_h + (cell_h - fr.height) // 2
                grid.paste(fr, (x, y))
            except Exception:
                continue
        buf = _io.BytesIO()
        grid.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.debug(f"[VISION] GIF 拼图失败: {type(e).__name__}: {e}")
        return b""


async def _trace_gif(image_bytes: bytes) -> dict:
    """
    GIF → 均分 3 帧分别搜，取最可信的那个结果。

    为什么是 3 帧：trace.moe 每天 100 次额度，一个 gif 打 3 次已经是上限；
    均分取样能覆盖「开头/中间/结尾」的动作变化。
    命中高置信就提前停，省额度。
    """
    frames = gif_frames(image_bytes, limit=3)
    if not frames:
        return {}
    best, best_rank = {}, -1
    weak_pool = []
    for i, fr in enumerate(frames):
        r = await trace_moe(fr) or {}
        if not r:
            continue
        if r.get("work"):
            rank = (1, 1 if r.get("confidence") == "high" else 0,
                    float(r.get("similarity") or 0))
            if rank > best_rank:
                best, best_rank = r, rank
            if r.get("confidence") == "high":
                logger.info(f"[VISION] GIF 第 {i + 1}/3 帧就高置信命中，提前停")
                break
        else:
            weak_pool.extend(r.get("candidates") or [])
    if best:
        return best
    if weak_pool:
        weak_pool.sort(key=lambda c: float(c.get("similarity") or 0), reverse=True)
        return {"work": "", "similarity": float(weak_pool[0].get("similarity") or 0),
                "weak": True, "candidates": weak_pool[:3]}
    return {}


# ================== trace.moe：以图搜番 ==================
async def trace_moe(image_bytes: bytes, timeout: float = 25) -> dict:
    """
    动漫截图专用检索。免费、免 key，直接返回「哪部番、第几集、第几秒」。

    为什么它比 VLM 强：
      它是拿你的图去比对**真实帧库**，不是让模型「猜像谁」。
      五条悟、久留美这种，VLM 只会说「白发蓝眼少年 / 粉发小女孩」，
      trace.moe 会直接告诉你《呪術廻戦》第 X 集。

    返回（采信）：
      {"work","work_romaji","similarity","votes","episode","at",
       "characters":[...],"confidence":"high"|"mid","candidates":[...]}
    返回（不采信但有候选，给 DeepSeek 当线索）：
      {"work":"", "similarity":x, "weak":True, "candidates":[...]}
    没搜到 / 失败返回 {}。
    """
    if not TRACE_MOE or not image_bytes:
        return {}
    if _looks_flat(image_bytes):
        logger.info("[VISION] 图像内容太少（纯色/极简），跳过 trace.moe")
        return {}
    if not _allow_trace():
        return {}
    try:
        # 2026-11-22：统一走 PicImageSearch 的 TraceMoe，不再自己拼 HTTP。
        # 以后要加 SauceNAO / ascii2d 综合搜，也只需要改 services/image_search.py。
        from . import image_search
        data = await image_search.trace_moe_raw(image_bytes, timeout=timeout)
    except Exception as e:
        logger.warning(f"[VISION] trace.moe 异常: {type(e).__name__}: {e}")
        return {}
    if not data:
        return {}

    if data.get("error"):
        logger.info(f"[VISION] trace.moe 报错：{str(data.get('error'))[:80]}")
        return {}
    results = data.get("result") or []
    if not results:
        logger.info("[VISION] trace.moe 没搜到（不是动漫截图，或截得太碎）")
        return {}
    # 留一行原始概况：下次「为什么没认出来」直接看这行就知道是相似度低还是结果分散
    _top = max((float(x.get("similarity") or 0) for x in results[:10]), default=0)
    logger.info(f"[VISION] trace.moe 返回 {len(results)} 条，最高相似度 {_top:.2f}")
    # 原始前 5 条（诊断用：/测函数 搜图 会原样打出来）
    _debug = [{
        "work": _work_of(x.get("anilist") or {}),
        "sim": round(float(x.get("similarity") or 0), 3),
        "ep": x.get("episode"),
    } for x in results[:5]]

    picked = _pick_cluster(results)
    if not picked.get("accepted"):
        logger.info(
            f"[VISION] trace.moe 不采信（最高票 {picked.get('votes', 0)}/{picked.get('total', 0)}，"
            f"占比 {picked.get('share', 0):.0%}，相似度 {picked.get('similarity', 0):.2f}，"
            f"门槛 {TRACE_MIN_SIM:.2f}）；候选："
            + ("、".join(f"{c['work']}({c['similarity']:.2f})"
                         for c in picked.get("candidates") or []) or "无")
        )
        return {
            "work": "",
            "similarity": picked.get("similarity", 0.0),
            "weak": True,
            "candidates": picked.get("candidates") or [],
            "raw": len(results),
            "debug": _debug,
        }

    # trace.moe 的 anilist 里**没有 characters 字段**，得再问一次 AniList。
    chars = await _anilist_characters(picked["media_id"])
    logger.info(
        f"[VISION] trace.moe 命中（{picked['confidence']}）：{picked['work']}"
        f"（id={picked['media_id']} 票数 {picked['votes']}/{picked['total']}，"
        f"占比 {picked['share']:.0%}，第{picked.get('episode')}集，"
        f"相似度 {picked['similarity']:.2f}），候选角色 {len(chars)} 个"
    )
    picked["characters"] = chars
    picked["raw"] = len(results)
    picked["debug"] = _debug
    return picked


def _pick_cluster(results: list) -> dict:
    """
    按 anilist id 聚类投票 → 判定采不采信。**纯函数**，不联网，方便自检和调参。

    为什么不能只信 results[0]：
      实测纯色图第一条 similarity=0.981，指向一部完全不相干的番，
      而后面 5 条其实指向另一部 —— 「单条相似度」根本不能当判据。

    放行条件（任一条）：
      ① 最高票 ≥ TRACE_MIN_VOTES 且占比 ≥ TRACE_MIN_SHARE（不太分散）
      ② 单帧相似度 ≥ TRACE_SINGLE_SIM（几乎是同一帧）
      并且相似度 ≥ TRACE_MIN_SIM（否则连「疑似」都不算，只给候选当线索）。
    """
    votes: dict = {}
    for r in results[:10]:
        aid = (r.get("anilist") or {}).get("id")
        if not aid:
            continue
        votes.setdefault(aid, []).append(r)
    if not votes:
        return {"accepted": False, "similarity": 0.0, "candidates": []}

    def _sim_of(rows: list) -> float:
        return max(float(x.get("similarity") or 0) for x in rows)

    # 先按票数、再按最高相似度排序 —— 同票时取更像的那部
    ranked = sorted(votes.items(), key=lambda kv: (len(kv[1]), _sim_of(kv[1])), reverse=True)
    best_id, best_rows = ranked[0]
    total = max(1, len(results[:10]))
    share = len(best_rows) / total
    sim = _sim_of(best_rows)

    # 候选清单（最多 3 部）：不采信时也要交给 DeepSeek 当线索，别直接扔掉
    candidates = []
    for _aid, _rows in ranked[:3]:
        candidates.append({
            "work": _work_of(_rows[0].get("anilist") or {}),
            "similarity": round(_sim_of(_rows), 3),
            "votes": len(_rows),
            "episode": _rows[0].get("episode"),
        })

    by_vote = len(best_rows) >= TRACE_MIN_VOTES and share >= TRACE_MIN_SHARE
    by_single = sim >= TRACE_SINGLE_SIM
    if not (by_vote or by_single) or sim < TRACE_MIN_SIM:
        return {
            "accepted": False, "work": "", "similarity": sim, "votes": len(best_rows),
            "total": total, "share": share, "candidates": candidates,
        }

    top = best_rows[0]
    an = top.get("anilist") or {}
    title = an.get("title") or {}
    # 高置信：相似度够高 + 至少两票（单帧 0.95 以上也算）
    confidence = ("high" if (sim >= TRACE_STRONG_SIM and (len(best_rows) >= 2 or by_single))
                  else "mid")
    at = top.get("from")
    return {
        "accepted": True,
        "work": _work_of(an),
        "work_romaji": str(title.get("romaji") or ""),
        "similarity": sim,
        "votes": len(best_rows),
        "total": total,
        "share": share,
        "episode": top.get("episode"),
        "at": float(at) if isinstance(at, (int, float)) else None,
        "media_id": best_id,
        "confidence": confidence,
        "candidates": candidates,
    }


def _work_of(an: dict) -> str:
    """从 trace.moe 的 anilistInfo 里取作品名：优先中文名，其次日文原名，最后罗马字。"""
    title = an.get("title") or {}
    zh = [str(x).strip() for x in (an.get("synonyms_chinese") or []) if str(x).strip()]
    return (zh[0] if zh else "") or str(title.get("native") or title.get("romaji") or "").strip()


async def _anilist_characters(media_id: int, limit: int = 12) -> list:
    """
    trace.moe 的 anilistInfo 不带角色表，这里补一次 AniList GraphQL（免费、免 key）。
    拿 MAIN + SUPPORTING 的主要角色，给 VL 做选择题用。
    """
    if not media_id:
        return []
    query = (
        "query ($id: Int) { Media(id: $id, type: ANIME) { "
        "characters(sort: [ROLE, RELEVANCE], perPage: 20) { "
        "nodes { name { full native } } } } }"
    )
    try:
        import httpx
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                ANILIST_GQL,
                json={"query": query, "variables": {"id": int(media_id)}},
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        if r.status_code != 200:
            logger.warning(f"[VISION] AniList 查角色 HTTP {r.status_code}")
            return []
        nodes = (((r.json().get("data") or {}).get("Media") or {})
                 .get("characters") or {}).get("nodes") or []
    except Exception as e:
        logger.warning(f"[VISION] AniList 查角色异常: {type(e).__name__}: {e}")
        return []

    out = []
    for n in nodes:
        nm = n.get("name") or {}
        name = str(nm.get("native") or nm.get("full") or "").strip()
        if name and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


async def _judge(scene: str, work: str, char: str, query: str, search_text: str) -> dict:
    """
    让 DeepSeek 拿「画面描述」去比对搜索结果：外观对不上就是猜错了。
    返回 {"verdict": 一致/不一致/无法判断, "work":…, "char":…, "why":…}
    """
    try:
        from .llm import XAI_API_KEY, XAI_BASE_URL, XAI_MODEL
    except Exception:
        return {}
    if not XAI_API_KEY:
        return {}

    prompt = (
        "视觉模型看了一张图，它的判断是：\n"
        f"画面：{scene}\n"
        f"它猜的作品：《{work or '没猜'}》\n"
        f"它猜的角色：{char or '没猜'}\n\n"
        f"下面是联网搜索「{query}」的结果：\n{search_text[:VERIFY_SEARCH_CHARS]}\n\n"
        "请你核对：搜索结果里的作品/角色，外观特征是否和「画面」吻合"
        "（发色、发型、服装、性别、年龄都要看）。\n"
        "注意：动漫角色经常被认错，不要因为名字出现过就认为对了。\n"
        "严格按四行输出，不要多余内容：\n"
        "结论：一致 / 不一致 / 无法判断\n"
        "作品：正确的作品名，或「不确定」\n"
        "角色：正确的角色名，或「不确定」\n"
        "理由：不超过 25 字"
    )
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{XAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {XAI_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": XAI_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.1, "max_tokens": 160},
            )
        if r.status_code != 200:
            logger.warning(f"[VISION] 裁决失败 HTTP {r.status_code}")
            return {}
        content = str(r.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception as e:
        logger.warning(f"[VISION] 裁决异常: {type(e).__name__}: {e}")
        return {}

    out = {"verdict": "", "work": "", "char": "", "why": ""}
    for line in content.splitlines():
        m = re.match(r"^(结论|作品|角色|理由)\s*[:：]\s*(.+)$", line.strip())
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "结论":
            out["verdict"] = v
        elif k == "作品":
            out["work"] = _clean_name(v)
        elif k == "角色":
            out["char"] = _clean_name(v)
        else:
            out["why"] = v[:30]
    return out


async def verify(scene: str, work: str, char: str) -> dict:
    """
    有专名就去搜一遍核对。返回
      {"work","char","corrected":bool,"verdict":str,"why":str,"query":str}
    永远不抛异常；任何一步失败都退回「原样保留、标记未核验」。
    """
    base = {"work": work, "char": char, "corrected": False,
            "verdict": "未核验", "why": "", "query": ""}

    if not VERIFY_ENABLED:
        base["verdict"] = "已关闭"
        return base
    if not (work or char):
        return base                      # 没猜专名 → 不用搜
    if not _allow_verify():
        base["verdict"] = "超限"
        return base

    # 搜索词：有作品就「作品 角色」，只有角色就加「角色」二字避免搜到同名词
    if work and char:
        query = f"{work} {char}"
    elif char:
        query = f"{char} 动漫角色"
    else:
        query = f"{work} 角色"

    try:
        from .web_search import fetch_web_search
        search_text = await fetch_web_search(query)
    except Exception as e:
        logger.warning(f"[VISION] 核验搜索异常: {type(e).__name__}: {e}")
        search_text = ""
    if not str(search_text or "").strip():
        base["verdict"] = "搜索无结果"
        return base

    base["query"] = query
    judged = await _judge(scene, work, char, query, search_text)
    if not judged or not judged.get("verdict"):
        base["verdict"] = "裁决失败"
        return base

    verdict = judged["verdict"]
    base["verdict"] = verdict
    base["why"] = judged.get("why", "")

    if "不一致" in verdict:
        # 以搜索为准：换成裁决给出的名字（它可能给「不确定」→ 那就退回无专名）
        base["work"] = judged.get("work", "")
        base["char"] = judged.get("char", "")
        base["corrected"] = True
    elif "一致" in verdict:
        # 一致：但如果裁决给了更规范的写法（比如补全了作品名），也采纳
        if judged.get("work"):
            base["work"] = judged["work"]
        if judged.get("char"):
            base["char"] = judged["char"]
    return base


# ================== 动漫截图：候选角色里做选择题 ==================
def _anime_prompt(work: str, chars: list, confidence: str = "high") -> str:
    """
    trace.moe 已经把作品锁死了，剩下的「是哪个角色」改成**选择题**。
    开放题 VL 会瞎猜，选择题基本不会错 —— 这是准确率差 60% 和 95% 的区别。

    confidence="mid"：相似度一般，措辞改成「可能」，别让 VL 被一个错答案带跑。
    """
    _lead = (f"检索说这张图来自动画《{work}》" if confidence == "high"
             else f"检索**可能**是动画《{work}》（相似度一般，别全信）")
    p = (
        f"{_lead}，但你只信自己的眼睛。严格按三行输出：\n"
        "是否本番：如果画风和角色确实像这部番就写「是」；"
        "如果明显不是（画风/人物完全不像，或者压根不是动画截图）就写「不是」\n"
    )
    if chars:
        p += f"角色：从这些候选里选一个最像的（{'、'.join(chars)}）；都对不上就写「不确定」\n"
    else:
        p += "角色：认得出就写角色名，认不出写「不确定」\n"
    p += "画面：不超过 60 字，只写你看到的（发色、发型、服装、动作），不要写作品名\n"
    # 👇 动漫截图也常带字幕/台词/水印，顺手一起抄（同一次调用，成本几乎为零）
    p += "文字：图里若有文字就逐字照抄（最多 100 字，不要总结改写）；没有写「无」"
    return p


def _pick_majority(names: list, allowed: list | None = None) -> str:
    """
    多个来源给出的专名，取「多数一致」的那个。
      · 两个都说同一个 → 用
      · 只有一个来源给出 → 用（report 里会标注「仅一方」）
      · 两个不一样 → 返回空（但 report 里两个都列出来，让 DeepSeek 自己看）
    """
    vals = [str(x or "").strip() for x in names]
    if allowed:
        vals = [v if v in allowed else "" for v in vals]
    vals = [v for v in vals if v]
    if not vals:
        return ""
    if len(vals) == 1:
        return vals[0]
    from collections import Counter
    top, n = Counter(vals).most_common(1)[0]
    return top if n >= 2 else ""


# 模型描述网格图时爱用的逐格前缀：「第3帧：」「左上：」「第一格，」
_FRAME_ENUM_RE = re.compile(
    r"(?:第\s*\d+\s*[格帧幅张]|第\s*[一二三四五六七八九十]+\s*[格帧幅张]"
    r"|左上|右上|左下|右下|中上|中下|中间)\s*[:：，,]?")


def _gif_condense(text: str, limit: int = 90) -> str:
    """
    把动图描述压成「一句话」：去掉逐格前缀、合并重复短句、截断。

    用户 2026-11-20：视觉模型逐帧描述的结果整段塞给 DeepSeek 等于让它再总结一遍。
    这里在**进报告之前**就压掉，DeepSeek 只看到结论。
    """
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    if not t:
        return ""
    t = _FRAME_ENUM_RE.sub(" ", t)
    parts = [p.strip(" ，,；;。、") for p in re.split(r"[；;。]", t)]
    seen, out = set(), []
    for p in parts:
        if not p:
            continue
        k = p[:10]
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    t = "；".join(out) if out else t
    return re.sub(r"\s+", " ", t).strip()[:limit]


GIF_SUMMARY_PROMPT = (
    "这是一张动图抽帧拼成的网格图（按时间顺序：左→右、上→下）。\n"
    "只用**一句话**说清这段动画在演什么：谁/什么 + 做了什么 + 结果或笑点。\n"
    "不超过 40 字。不要分点、不要逐格、不要解释、不要写「这张图」。"
)


def _gif_need_summary(text: str) -> bool:
    """判断模型是不是还在逐格罗列（≥3 个分句基本就是报菜名了）。"""
    t = _FRAME_ENUM_RE.sub(" ", str(text or ""))
    parts = [p for p in re.split(r"[；;。，,]", t) if p.strip()]
    return len(parts) >= 3


async def _gif_summary(vl_call) -> str:
    """
    模型第一次没按要求总结（还在逐帧报菜名）→ 拿同一张网格图**再问一次**，
    这次只要一句话。用户 2026-11-20：DeepSeek 该拿到总结，不是帧列表。
    """
    try:
        raw = await vl_call(GIF_SUMMARY_PROMPT, 80)
    except Exception as e:
        logger.warning(f"[VISION] GIF 二次总结失败: {type(e).__name__}: {e}")
        return ""
    t = re.sub(r"\s+", " ", str(raw or "")).strip()
    t = re.sub(r"^(画面|描述|总结)\s*[:：]\s*", "", t)
    return t.strip("「」\"'。")[:80]


def _fmt_one(tag: str, parsed: dict, *, with_text: bool = True) -> str:
    """一行「谁看到了什么」。"""
    if not parsed:
        return f"{tag}：（没有结果）"
    scene = str(parsed.get("scene") or "").strip()
    work = str(parsed.get("work") or "").strip()
    char = str(parsed.get("char") or "").strip()
    text = str(parsed.get("text") or "").strip()
    name = "·".join(x for x in (work, char) if x)
    body = scene or "（没描述）"
    # 👇 文字必须原样带进多源报告：截图/聊天记录的信息全在文字里，
    #    只给「一张截图」等于没给（2026-11-13 实测：她拿不到文字只能瞎猜话题）
    #    2026-11-27：OCR 有结果时这里不再重复视觉模型的改写版（下面单独一行给原文）
    if text and with_text:
        body += f"｜图里文字：{text}"
    return f"{tag}：{body}" + (f"（{name}）" if name else "")


async def _targeted_ask(vl_call, scene: str, hint: str = "") -> dict:
    """
    第四层：图片 + 明确问题再问一遍「这是谁」。

    和前面「结构化描述」的区别只在问法：
      前面问「这是什么」→ 模型给你描述画面（蓝发双马尾少女…）
      这里问「这是谁」→ 模型被迫给名字（小鲸鱼 / 星井美希）
    实测 cosplay、痛车、海报这类场景，不点破它就不会主动说角色名。
    """
    p = TARGETED_PROMPT
    if scene:
        p += f"\n\n（另一个模型只看到这些：{scene[:80]}，可以参考，但你自己判断）"
    if hint:
        p += f"\n（发图的人说：{hint[:100]}）"
    try:
        raw = await vl_call(p, 160)
    except Exception as e:
        logger.warning(f"[VISION] 定向追问失败: {type(e).__name__}: {e}")
        return {}
    if not str(raw or "").strip():
        return {}
    parsed = parse_structured(raw)
    if not (parsed.get("work") or parsed.get("char")):
        logger.info("[VISION] 定向追问也没问出名字")
        return {}
    logger.info(
        f"[VISION] 🎯 定向追问出名字：{parsed.get('char') or '?'}"
        f"（{parsed.get('work') or '?'}）"
    )
    return parsed


# ================== 对外唯一入口 ==================
async def recognize(image_bytes: bytes, vl_call, *, url: str = "", hint: str = "",
                    is_gif: bool = False, vl_bytes: bytes | None = None) -> dict:
    """
    识图 → 收集**所有来源**的证据 → 拼成一份多源报告交给 DeepSeek 判断。

    为什么不再「两票制否决」：
      两个模型不一致就直接丢掉专名太浪费 —— DeepSeek 有常识，
      看到「qwen 说粉发 / GLM 说黄发 / trace.moe 说是偶像大师」它自己就能判断。
      我们的活是**把证据摆齐**，不是替它下结论。

    返回：
      desc    一句话结论（给上下文/记忆用，保守）
      report  多源证据全文（给 DeepSeek 看）
      sources 各来源原始结果
    """
    fp = fingerprint(image_bytes)

    # ① 缓存命中：不调视觉、不搜索
    cached = get_cached(fp)
    if cached and cached.get("desc"):
        logger.info(f"[VISION] ♻️ 命中缓存（{fp}）：{cached['desc'][:40]}")
        cached["source"] = "cache"
        return cached

    sources = {}

    # ②' OCR 与视觉模型并行（2026-11-27 P1）：
    #     OpenCV 先判「有没有字」（毫秒级），有字才把 OCR 丢线程池，
    #     和 VL 调用同时跑 —— 基本不额外增加回复延迟。
    _ocr_task = None
    if ocr_mod.ENABLED:
        try:
            from . import vision_report as _vr_q
            _img_q = vl_bytes or image_bytes
            if _vr_q.quality_of(_img_q).get("has_text"):
                _ocr_task = asyncio.create_task(ocr_mod.read_async(_img_q))
        except Exception as e:
            logger.debug(f"[VISION] OCR 启动失败（忽略）: {type(e).__name__}: {e}")
            _ocr_task = None

    # ② trace.moe（只对动漫截图有效）
    #    GIF 走「均分三帧分别搜」—— 直接把动图丢给它，它只会拿第一帧乱猜。
    anime = {}
    if TRACE_MOE:
        anime = (await _trace_gif(image_bytes) if is_gif
                 else await trace_moe(image_bytes)) or {}
    trace_ok = bool(anime.get("work"))
    # 不采信时的候选（相似度偏低）—— 不写记忆、不当事实，但作为线索塞进 report
    weak = [] if trace_ok else list(anime.get("candidates") or [])
    if trace_ok:
        sources["trace"] = anime
    elif weak:
        # 不采信也留档：排查「为什么没认出来」时能直接看到最接近的是哪部
        sources["trace_weak"] = anime

    # ③ 两个视觉模型各答一遍（prompt 随 trace 结果变：有番名就做选择题）
    chars = (anime.get("characters") or []) if trace_ok else []
    if trace_ok:
        prompt = _anime_prompt(anime["work"], chars, anime.get("confidence") or "high")
    elif is_gif:
        # 动图专用：只要一句「整段动画在演什么」，**禁止逐格罗列**
        prompt = GIF_PROMPT_STRUCT
    else:
        prompt = VISION_PROMPT_STRUCT
    # 👇 发送者附言（图配文字时）：一句话就能把「蓝发女仆装少女」点成「我cos的小鲸鱼」，
    #    比任何检索都便宜有效。没有附言就不加。
    hint = str(hint or "").strip()
    if hint:
        prompt += f"\n\n（发送者发这张图时说的话，可能相关也可能无关，仅供线索：{hint[:120]}）"

    # 👇 多源搜图和视觉模型**并行**：不额外等，失败也不影响识图
    _extra_task = None
    if EXTRA_SEARCH:
        try:
            import asyncio as _aio
            _extra_task = _aio.create_task(_extra_sources(image_bytes))
        except Exception:
            _extra_task = None

    qwen_raw = ""
    try:
        # max_tokens 220 → 400：多了「类型」「文字」两行，文字行最多 150 字，
        # 220 会把「角色」挤掉（文字图尤其明显）
        qwen_raw = await vl_call(prompt, 400) or ""
    except Exception as e:
        logger.warning(f"[VISION] qwen 视觉调用异常: {type(e).__name__}: {e}")
    qwen = parse_structured(qwen_raw) if qwen_raw.strip() else {}

    # ②'' OCR 收尾：VL 调用这十几秒里它早就跑完了，这里只是取结果
    ocr_blocks = []
    if _ocr_task is not None:
        try:
            ocr_blocks = await _ocr_task or []
        except Exception as e:
            logger.debug(f"[VISION] OCR 取结果失败（忽略）: {type(e).__name__}: {e}")
            ocr_blocks = []
        if ocr_blocks:
            logger.info(f"[VISION] 🔤 OCR {len(ocr_blocks)} 块："
                        f"{ocr_mod.text_of(ocr_blocks, 60)}")
        else:
            logger.info("[VISION] OCR 没读出字（文字退回视觉模型）")

    alt_parsed = {}
    alt_raw = ""
    # 2026-11-27：第二视觉源改成**可配 provider**（默认关闭）。
    # 之前写死 GLM，GLM 额度用完后这里每张图都白等一次失败。
    _second = _second_target()
    _second_name = str((_second or {}).get("provider") or "第二家")
    _need_alt = bool(_second) and _SECOND_MODE != "off" and (
        _SECOND_MODE == "always"
        or (_SECOND_MODE == "on_miss" and not trace_ok
            and not (qwen.get("work") or qwen.get("char")))
    )
    if _need_alt:
        try:
            from . import vlm
            # GIF 已经抽帧拼成静态图（vl_bytes），别把动图原样喂过去
            alt_raw = await vlm.describe(
                "vision_second", vl_bytes or image_bytes, prompt, max_tokens=400) or ""
            if alt_raw.strip():
                alt_parsed = parse_structured(alt_raw)
        except Exception as e:
            logger.debug(f"[VISION] 第二视觉源失败（忽略）: {type(e).__name__}: {e}")
    # 👇 GIF：把「逐格描述」压成一句话。模型经常无视「不要逐格」的指令，
    #    这里在**进报告之前**兜一层，DeepSeek 只看到总结（用户 2026-11-20）。
    if is_gif:
        for _p in (qwen, alt_parsed):
            if not isinstance(_p, dict):
                continue
            _raw_scene = str(_p.get("scene") or "")
            if not _raw_scene:
                continue
            _p["scene"] = _gif_condense(_raw_scene)
            if len(_raw_scene) > len(_p["scene"]) + 20:
                logger.info(f"[VISION] GIF 逐帧描述 {len(_raw_scene)} 字 "
                            f"→ 总结 {len(_p['scene'])} 字")
        # 清洗完还在逐格罗列 → 再问一次「一句话总结」（同一张网格图，很便宜）
        _cur_scene = (str((qwen or {}).get("scene") or "").strip()
                      or str((alt_parsed or {}).get("scene") or "").strip())
        if _cur_scene and _gif_need_summary(_cur_scene):
            _sum = await _gif_summary(vl_call)
            if _sum:
                logger.info(f"[VISION] GIF 二次总结：{_sum}")
                if isinstance(qwen, dict):
                    qwen["scene"] = _sum
                if isinstance(alt_parsed, dict) and alt_parsed:
                    alt_parsed["scene"] = _sum

    extra = []
    if _extra_task is not None:
        try:
            import asyncio as _aio
            extra = await _aio.wait_for(_extra_task, timeout=10)
        except Exception as e:
            logger.debug(f"[VISION] 多源搜图超时/失败（忽略）: {type(e).__name__}: {e}")
            extra = []
    if extra:
        sources["picsearch"] = extra

    if qwen:
        sources["qwen"] = qwen
    if alt_parsed:
        sources["second"] = alt_parsed
    if ocr_blocks:
        sources["ocr"] = {"blocks": len(ocr_blocks)}

    if not qwen and not alt_parsed and not trace_ok:
        return {}

    # ④ trace 番名被否决？（纯色帧/撞脸会误命中，两个视觉模型都能投反对票）
    if trace_ok and re.search(r"是否本番\s*[:：]\s*不是", qwen_raw + alt_raw):
        logger.info(f"[VISION] 视觉模型否决了 trace.moe 给的《{anime.get('work')}》")
        trace_ok = False
        chars = []

    # ⑤ 汇总：画面用 qwen 优先，专名取多数一致
    scene = (qwen.get("scene") or alt_parsed.get("scene") or "").strip()
    # 👇 文字：**OCR 优先**（确定性、原文照抄）；OCR 没跑/没读到才退回视觉模型，
    #    视觉模型两家都给了就取**较长**的那份（短的多半被 max_tokens 截断）。
    _t_q = str((qwen or {}).get("text") or "").strip()
    _t_g = str((alt_parsed or {}).get("text") or "").strip()
    _t_ocr = ocr_mod.text_of(ocr_blocks)
    _t_vlm = _t_q if len(_t_q) >= len(_t_g) else _t_g
    ocr_text = _t_ocr or _t_vlm
    text_source = "ocr" if _t_ocr else ("vlm" if _t_vlm else "")
    kind = str((qwen or {}).get("kind") or (alt_parsed or {}).get("kind") or "").strip()
    if ocr_text:
        logger.info(f"[VISION] 🔤 文字({text_source}) {len(ocr_text)} 字：{ocr_text[:60]}")
    if trace_ok:
        work = anime["work"]
        char = _pick_majority([qwen.get("char"), alt_parsed.get("char")], allowed=chars)
        if not char and len(chars) == 1:
            char = chars[0]
    else:
        work = _pick_majority([qwen.get("work"), alt_parsed.get("work")])
        char = _pick_majority([qwen.get("char"), alt_parsed.get("char")])

    # ⑥ 文本核验：trace 已经用图比对过就不必再搜文本了
    v = {}
    if not trace_ok and (work or char):
        v = await verify(scene, work, char)
        if v.get("corrected") or v.get("work") or v.get("char"):
            work = v.get("work") or work
            char = v.get("char") or char

    # ⑥-b 第四层：前三层都没给专名 → 换个问法「这是谁」再问一次
    asked = {}
    if TARGETED_ASK and not (work or char):
        asked = await _targeted_ask(vl_call, scene, hint)
        if asked:
            work = asked.get("work") or ""
            char = asked.get("char") or ""

    # ⑦ 多源报告（给 DeepSeek 的证据清单）
    lines = []
    if is_gif:
        # 动图只给一句总结 + 类型/文字，不把两个模型的逐格描述整段倒给 DeepSeek
        lines.append("（这是一张**动图**：已抽 6 帧拼成网格给视觉模型看过，"
                     "下面是**总结**；评论时可以提「动图里…」）")
        _gsc = (str((qwen or {}).get("scene") or "").strip()
                or str((alt_parsed or {}).get("scene") or "").strip())
        _alt = str((alt_parsed or {}).get("scene") or "").strip()
        if _gsc and _alt and _alt[:12] != _gsc[:12]:
            _gsc = f"{_gsc}（另一模型：{_alt[:40]}）"
        lines.append(f"动图内容：{_gsc or '（没描述出来）'}")
        _kt = str((qwen or {}).get("kind") or (alt_parsed or {}).get("kind") or "").strip()
        if _kt:
            lines.append(f"类型：{_kt}")
        if ocr_text:
            _src = "OCR 原文" if _t_ocr else "识图模型，可能不准"
            lines.append(f"图里文字（{_src}）：{ocr_text[:120]}")
        _nm = "·".join(x for x in (work, char) if x)
        if _nm:
            lines.append(f"识别：{_nm}")
    else:
        if qwen:
            lines.append(_fmt_one("qwen识图", qwen, with_text=not _t_ocr))
        else:
            lines.append("qwen识图：（没结果）")
        if alt_parsed:
            lines.append(_fmt_one(f"{_second_name}识图", alt_parsed, with_text=not _t_ocr))
        elif _need_alt:
            lines.append(f"{_second_name}识图：（不可用或没结果）")
        # 👇 最终文字单独一行：OCR 原文优先（视觉模型的「逐字抄」会改写，只当线索）
        if ocr_text:
            _src = "OCR 原文" if _t_ocr else "识图模型，可能不准"
            lines.append(f"图里文字（{_src}）：{ocr_text[:200]}")
    if trace_ok:
        ep = anime.get("episode")
        _at = anime.get("at")
        _ts = (f"，约 {int(_at) // 60:02d}:{int(_at) % 60:02d} 处"
               if isinstance(_at, (int, float)) else "")
        _hi = (anime.get("confidence") or "high") == "high"
        lines.append(
            ("trace.moe：命中" if _hi else "trace.moe：疑似命中（相似度一般，仅供参考）")
            + f"《{anime['work']}》"
            + (f"第{ep}集" if ep else "") + _ts
            + f"，相似度 {anime.get('similarity', 0):.2f}，票数 {anime.get('votes', '?')}"
            + (f"，角色候选：{'、'.join(chars[:8])}" if chars else "")
            + ("" if _hi else "。**别当事实讲**，可以顺着说「这是《X》吧」")
        )
        # 角色名不确定（相似度一般 / 候选一大串）→ 明确给她「可以不提名字」的许可。
        # 用户 2026-11-19：「她觉得 trace.moe 给的角色名不太确定，可以不回复角色名相关信息，正常回复。」
        if not _hi:
            lines.append(
                "（角色名**不确定**：这一条你完全可以不提名字 —— 直接聊画面 / 吐槽 / 接梗都行，"
                "不用硬猜、也不用为了显得懂而编人名。）")
        elif len(chars) > 2:
            lines.append(
                f"（角色有 {len(chars)} 个候选，拿不准是谁就别点名，正常说画面就行。）")
    elif weak:
        # 相似度没到采信线，但候选仍有信息量 —— 给 DeepSeek 当线索，比「啥都没有」强
        lines.append(
            "trace.moe：没有可靠命中；最接近的候选："
            + "、".join(f"《{c['work']}》{c['similarity']:.2f}" for c in weak[:3])
            + "（相似度偏低，只能当线索：如果你自己看也觉得像，就用「这是X吧」的推测语气，"
              "**不要当事实**）"
        )
    elif TRACE_MOE:
        lines.append("trace.moe：没有可靠命中（不是动漫截图，或结果太分散）")
    if extra:
        _es = []
        for _r in extra[:4]:
            _bits = [f"[{_r.get('engine')}]", str(_r.get("title") or "（无标题）")]
            _sim = _r.get("similarity") or 0
            if _sim:
                _bits.append(f"相似度 {float(_sim):.0f}%")
            if _r.get("author"):
                _bits.append(f"作者 {_r['author']}")
            if (_r.get("extra") or {}).get("episode"):
                _bits.append(f"第 {_r['extra']['episode']} 集")
            _es.append("- " + "｜".join(_bits))
        lines.append("搜图源（SauceNAO / Ascii2D / IQDB）：\n" + "\n".join(_es)
                     + "\n（和画面描述冲突时**以画面为准**；相似度低就只当线索，"
                       "别当事实讲。）")
    if v:
        lines.append(
            f"联网核验：{v.get('verdict') or '未核验'}"
            + (f"（{v.get('why')}）" if v.get("why") else "")
        )
    if asked:
        sources["ask"] = asked
        lines.append(_fmt_one("定向追问", asked))
    elif TARGETED_ASK and not trace_ok:
        lines.append("定向追问：（也没问出名字）")
    if not trace_ok and not v and not (work or char):
        # ⚠️ 别写「只能描述画面」——那等于叫她放弃。
        #    她结合群聊上下文/常识往往能认出来（比如群里刚聊过某个角色）。
        lines.append(
            "（以上来源都没给出专名。你如果结合上下文认得出这是谁，就直接说；"
            "拿不准就用「这是XX吧」这种推测语气，别硬编）"
        )

    # 专名是识图模型猜的（没有 trace.moe/联网佐证）→ 明确告诉 DeepSeek 别当真
    if not trace_ok and not (v.get("verdict") == "一致"):
        if ((qwen or {}).get("guess") or (alt_parsed or {}).get("guess")):
            lines.append(
                "（带「猜」的专名是识图模型凭印象给的，**把握不大**："
                "可以顺着说「这是XX吧」，但别当成事实讲，也别为它编细节）"
            )

    report = "\n".join(lines)

    # ⑧ 缓存 + 记忆
    # guess：专名是识图模型猜的（不是 trace.moe 比对出来的）→ 描述里标「仅猜测」
    _guess = bool((qwen or {}).get("guess") or (alt_parsed or {}).get("guess")) and not trace_ok
    # trace 只是「疑似命中」→ 角色名不写进 desc：desc 会进群聊上下文和长期记忆，
    # 认错一次就会被当成事实翻出来很久（用户 2026-11-19：不确定就别回角色名）。
    _uncertain = bool(trace_ok and (anime.get("confidence") or "high") != "high")
    _nsfw = str((qwen or {}).get("nsfw") or (alt_parsed or {}).get("nsfw") or "")
    if _nsfw:
        logger.info("[VISION] ⚠️ 识图模型标记为 NSFW，描述里加提示，别让她认真分析画面")
    desc = compose(scene, work, ("" if _uncertain else char),
                   guess=(_guess or _uncertain), nsfw=_nsfw,
                   text=ocr_text, kind=kind)
    if not desc:
        return {}

    # 记忆只写「高置信」的：疑似命中（相似度 0.75~0.90）写进记忆，以后会被当事实翻出来，
    # 错一次就污染很久。低置信的留在本次上下文里就够。
    _trace_hi = trace_ok and (anime.get("confidence") or "high") == "high"
    if (work or char) and (_trace_hi or v.get("verdict") == "一致"):
        try:
            from . import memory_v2
            memory_v2.remember(
                text=f"图里是：{scene}（{('·'.join(x for x in (work, char) if x))}）",
                kind="vision",
                importance=0.78,
            )
        except Exception as e:
            logger.debug(f"[VISION] 写记忆失败: {e}")

    rec = {
        "scene": scene,
        "work": work,
        "char": char,
        "guess": _guess,
        "nsfw": _nsfw,
        "text": ocr_text,
        # 文字来源：ocr = 原文照抄（可信）；vlm = 识图模型改写版（只当线索）
        "text_source": text_source,
        # OCR 分块（含置信度/坐标）→ vision_report 直接复用，不再二次识字
        "ocr_blocks": ocr_blocks,
        "kind": kind,
        "desc": desc,
        "report": report,
        "sources": sources,
        "verified": bool(_trace_hi or v.get("verdict") == "一致"),
        "verdict": (
            (f"trace.moe {anime.get('similarity', 0):.2f}"
             + ("" if _trace_hi else "（疑似）"))
            if trace_ok else (v.get("verdict") or "未核验")
        ),
        "corrected": bool(v.get("corrected")),
        "source": "trace.moe" if trace_ok else (
            f"{_second_name}+qwen" if alt_parsed else "qwen"),
        "episode": anime.get("episode") if trace_ok else None,
        "confidence": anime.get("confidence") if trace_ok else "",
    }
    logger.info(f"[VISION] 📋 多源报告：\n{report}")
    put_cache(fp, rec)
    return rec
