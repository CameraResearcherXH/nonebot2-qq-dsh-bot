# plugins/vision_agent/__init__.py
from core import tuning
import os
import io
import time
import base64
import aiohttp
import asyncio
import httpx
from pathlib import Path
from collections import defaultdict
from nonebot import on_message, logger
from nonebot.adapters.onebot.v11 import (
    Bot, MessageEvent, GroupMessageEvent, PrivateMessageEvent, Message, MessageSegment,
)
from nonebot.rule import Rule
from services.llm import call_llm, VISION_TAG
from services.reply import reply_with_multimodal
from services import identity as identity_mod

# ================= 核心配置 =================
# ⚠️ 原来读 os.getenv("DASHSCOPE_API_KEY", "")：.env 里没写就是空字符串，
#    于是 process_image 直接 return，看起来就像"识图没反应"。
#    这里改成和 config.py 保持一致（config 里有硬编码兜底 key）。
try:
    from services.config import DASHSCOPE_API_KEY as _CFG_DASH_KEY
except Exception:
    _CFG_DASH_KEY = ""
DASHSCOPE_API_KEY = (
    os.getenv("VISION_API_KEY", "")
    or os.getenv("DASHSCOPE_API_KEY", "")
    or _CFG_DASH_KEY
)

VISION_MODEL = os.getenv("VISION_MODEL", "Qwen3.5-Flash")
VISION_TIMEOUT = tuning.f("VISION_TIMEOUT")

# ---- 图片体积控制（7MB 的图不压缩直接 base64 = 9.4MB 请求体，必超时）----
VISION_MAX_SIDE = tuning.i("VISION_MAX_SIDE")       # 最长边缩到多少
VISION_MAX_BYTES = int(os.getenv("VISION_MAX_BYTES", str(4 * 1024 * 1024)))
# 2026-11-24：默认改成「先下载再传」。
#   以前优先把 QQ 图片链接丢给视觉模型，模型拉到的是 CDN 那份（可能被压过/被限流），
#   用户一直反馈「图看不清」。现在自己下载 → 按 VISION_MAX_SIDE 压 → base64 传，
#   我们控制画质；链接只作为「下载失败时的兜底」。
VISION_USE_URL = os.getenv("VISION_USE_URL", "false").lower() == "true"
# 视觉主通道：qwen（默认，阿里云 DashScope）｜glm（智谱，GLM_VISION_MODEL）
VISION_PROVIDER = (os.getenv("VISION_PROVIDER", "qwen") or "qwen").strip().lower()
VISION_MAX_ATTEMPTS = tuning.i("VISION_MAX_ATTEMPTS")  # 总尝试次数上限

# 👇 模型名自动降级：不同 workspace 上架的模型不一样，
#    写死一个名字会直接 404 model_not_found。
#    按顺序试，第一个能用的记进 data/vision_model.txt，下次直接用。
_env_cands = [m.strip() for m in (os.getenv("VISION_MODEL_CANDIDATES") or "").split(",") if m.strip()]
VISION_MODEL_CANDIDATES = _env_cands or [
    # 2026-11-24 实测（当前 workspace）：
    #   · qwen-vl-plus / qwen3.5-flash 名字不对或极慢（404 / 30~50s）；
    #   · qwen-vl-max-latest 403（没权限）；
    #   · qwen3-vl-plus ~10s，稳定可用 → 作 Qwen 侧主力。
    VISION_MODEL,              # 默认 qwen3-vl-plus
    "qwen3-vl-plus",
    "qwen2.5-vl-72b-instruct",
    "qwen2.5-vl-7b-instruct",
    "qwen-vl-max-latest",
    "qwen3.5-flash",           # 兜底：慢，但能读细节
]
_MODEL_CACHE_FILE = Path("data/vision_model.txt")
_working_model = None


def _load_working_model() -> str:
    global _working_model
    if _working_model:
        return _working_model
    try:
        if _MODEL_CACHE_FILE.exists():
            m = _MODEL_CACHE_FILE.read_text(encoding="utf-8").strip()
            if m:
                _working_model = m
    except Exception:
        pass
    return _working_model or ""


def _save_working_model(name: str):
    global _working_model
    _working_model = name
    try:
        _MODEL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MODEL_CACHE_FILE.write_text(name, encoding="utf-8")
    except Exception:
        pass

# ⚠️ .env 里配的是专属 workspace 端点（DASHSCOPE_BASE_URL）。
#    但 workspace 上架哪些模型是不确定的（现在 qwen-vl-max 就 404 model_not_found），
#    所以视觉单独给一个 VISION_BASE_URL：workspace 没有视觉模型时，
#    直接指到公网端点 + 公网 key，互不影响。
PUBLIC_DASH_BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"

_DASH_BASE = (
    os.getenv("VISION_BASE_URL", "").strip().rstrip("/")
    or os.getenv("DASHSCOPE_BASE_URL", "").strip().rstrip("/")
    or PUBLIC_DASH_BASE
)
VISION_ENDPOINT = f"{_DASH_BASE}/chat/completions"


def _endpoints() -> list:
    """端点也自动降级：workspace 端点找不到模型时，自动再试百炼公网端点。"""
    eps = []
    for b in (
        os.getenv("VISION_BASE_URL", "").strip().rstrip("/"),
        os.getenv("DASHSCOPE_BASE_URL", "").strip().rstrip("/"),
        PUBLIC_DASH_BASE,
    ):
        if not b:
            continue
        ep = f"{b}/chat/completions"
        if ep not in eps:
            eps.append(ep)
    return eps

TEMP_DIR = Path("data/temp_img")
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def _sniff_mime(data: bytes) -> str:
    """按 magic bytes 猜 MIME，别一律当成 jpeg。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    return "image/jpeg"


def _compress(image_bytes: bytes, max_side: int = 0) -> tuple:
    """
    把图片缩到 max_side 内并转 JPEG，返回 (bytes, mime)。
    没装 Pillow 就原样返回（此时大图很可能超时，日志会警告）。
    """
    max_side = max_side or VISION_MAX_SIDE
    try:
        from PIL import Image
    except ImportError:
        return image_bytes, _sniff_mime(image_bytes)
    try:
        im = Image.open(io.BytesIO(image_bytes))
        if getattr(im, "is_animated", False):
            try:
                im.seek(0)
            except Exception:
                pass
        im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        # quality 90：85 对截图里的细字太狠，视觉模型会回「看不清」
        im.save(buf, format="JPEG", quality=90, optimize=True)
        out = buf.getvalue()
        # 压完反而更大（小图）就用原图
        if len(out) >= len(image_bytes):
            return image_bytes, _sniff_mime(image_bytes)
        return out, "image/jpeg"
    except Exception as e:
        logger.debug(f"[VISION] 压缩失败，用原图: {type(e).__name__}: {e}")
        return image_bytes, _sniff_mime(image_bytes)


async def _download(url: str, timeout: float = 20) -> bytes:
    """下载一张图；失败返回 b""。"""
    url = str(url or "").strip()
    if not url:
        return b""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                logger.warning(f"⚠️ [VISION] 图片下载 HTTP {resp.status}: {url[:80]}")
    except Exception as e:
        logger.warning(f"⚠️ [VISION] 图片下载失败: {type(e).__name__}: {e}")
    return b""


async def _fetch_image_bytes(bot: Bot, event: MessageEvent, seg) -> bytes | None:
    """把图片段变成 bytes —— **优先原图**。

    2026-11-24 用户：「她说图片糊很可能是 QQ 压缩，没有查看原图再下载。」
    真机确实如此：QQ 消息段里的 `url` 经常是**预览/压缩版**（`term=2`），
    原图要问 NapCat `get_image`（它缓存的是原文件）。

    做法：把所有能拿到的候选都下一遍，**取字节数最大的那份**；
    如果消息段自报的 `file_size` 比我们拿到的大很多，日志里直接标出来。
    """
    cands = []          # [(来源, bytes)]

    # ① NapCat get_image：url + 本地缓存文件（缓存的是原图）
    file_id = seg.data.get("file") or seg.data.get("file_id")
    real_url = ""
    if file_id:
        try:
            res = await bot.call_api("get_image", file=file_id) or {}
            real_url = str(res.get("url") or "")
            local_path = str(res.get("file") or "")
            # ⚠️ NapCat 在**另一个容器**里，它返回的 /app/napcat/cache/xxx 在 bot 容器里
            #    默认不存在。docker-compose 里把 ./napcat/cache 同时挂给两个容器之后，
            #    这里就能读到原图（比 CDN 的预览图清楚）。
            for _p in (local_path, f"/app/napcat/cache/{Path(local_path).name}" if local_path else ""):
                if _p and os.path.exists(_p):
                    try:
                        cands.append(("napcat本地", Path(_p).read_bytes()))
                        break
                    except Exception as e:
                        logger.debug(f"[VISION] 读 NapCat 本地文件失败: {e}")
        except Exception as e:
            logger.warning(f"⚠️ [VISION] get_image 失败: {type(e).__name__}: {e}")

    # ② 两个 url 都试（NapCat 返回的通常是原图地址）
    for tag, u in (("get_image_url", real_url),
                   ("seg_url", str(seg.data.get("url") or ""))):
        if u:
            data = await _download(u)
            if data:
                cands.append((tag, data))

    if not cands:
        return None

    tag, best = max(cands, key=lambda x: len(x[1]))
    try:
        declared = int(seg.data.get("file_size") or 0)
    except Exception:
        declared = 0
    detail = "｜".join(f"{t}={len(d) // 1024}KB" for t, d in cands)
    if declared and len(best) * 1.15 < declared:
        logger.warning(
            f"[VISION] 拿到的可能还是压缩图：原图报 {declared // 1024}KB，"
            f"最大只有 {len(best) // 1024}KB（{detail}）")
    else:
        logger.info(f"[VISION] 取图 {len(best) // 1024}KB（{tag}；{detail}）")
    return best


# 被动识图冷却（识别成本，跟"要不要开口"是两回事）
VISION_COOLDOWN = tuning.f("VISION_COOLDOWN")
_last_vision_time = defaultdict(float)

# 👇 被动识图**要不要让她开口**（用户 2026-11-11 定，这是"她一直在说话"的根源）
#   llm（默认，2026-11-19 起）：识别完把「图 + 全部证据」交给 DeepSeek，
#       由她自己决定开口还是 `stay_silent`。**这才是 LLM 主导的流程**。
#   mentioned：老行为 —— 只有喊了她名字才进回复队列（LLM 决策链不跑）。
#   always：更老的行为 —— 每 VISION_COOLDOWN 秒就为一张图回一句。
#
# ⚠️ 2026-11-19 用户反馈：「这套新的 llm 决策流程根本没跑起来，反而是老流程先决策」——
#    指的就是 `_vision_should_reply` 在识图后**先拍板**要不要回，LLM 只能被动执行。
#    现在默认 llm：识图只负责「看见」，回不回、怎么回、收不收表情都由模型决定。
VISION_REPLY_MODE = (os.getenv("VISION_REPLY_MODE", "llm") or "llm").strip().lower()
# llm 模式下每小时最多把几张图交给她「想」（防刷图把 token 打爆）。
# 注意这是**频率闸门**，不是「该不该说」的判断 —— 判断在模型那边。
VISION_LLM_MAX_PER_HOUR = tuning.i("VISION_LLM_MAX_PER_HOUR")
_vision_llm_window: dict = defaultdict(list)
# 表情包要不要也走「她决定收不收」（用户 2026-11-19：收表情包不该在识图前就定）
STICKER_DECIDE = (os.getenv("STICKER_DECIDE", "llm") or "llm").strip().lower()

# 👇 同一张图 IMAGE_DEDUP_HOURS 小时内只处理一次（用户 2026-11-11 要求）
# 真机踩过：群友把同一张 `4F243681…jpg` 在 15:33 和 15:55 各发一次（复读），
# 她 15:33 回了「这熊猫头怎么还哭上了」，15:55 又回「你满意了？我还没说话呢」，
# 群友：「这肥鱼魔了」。60 秒 DEDUP 拦不住这种跨 20 分钟的复读。
IMAGE_DEDUP_HOURS = tuning.f("IMAGE_DEDUP_HOURS")
_image_seen: dict = {}          # session_key -> {图片文件名: 首次处理时间}
# 识别结果缓存：同一张图重复发时不重新识别，但要把上次的描述**再写回上下文**。
# 用户 2026-11-19：同一张图隔几分钟再发，vision 判定「24h 内已处理过」直接跳过，
# 结果她说「看不到这张」——其实几分钟前刚看过。
_image_cache: dict = {}         # session_key -> {文件名: {"desc","url","at"}}
_last_image: dict = {}          # session_key -> {"desc","url","at"}（给 vision_source 工具用）


def last_image(session_key: str) -> dict:
    """这个会话最近一张图的识别结果（没处理过就是空）。"""
    return dict(_last_image.get(str(session_key)) or {})


def _replay_cached(event, session_key: str, files) -> bool:
    """同一张图重复发：不重新识别，把上次的描述再写进上下文，让她"再看一眼"。"""
    cache = _image_cache.get(session_key) or {}
    rec = next((cache[f] for f in files if f in cache), None)
    if not rec or not rec.get("desc"):
        return False
    try:
        from plugins.chat import recent_group_msgs, recent_group_ts
        nickname = event.sender.card or event.sender.nickname or str(event.user_id)
        if isinstance(event, GroupMessageEvent):
            gid = str(event.group_id)
            recent_group_msgs[gid].append(f"{nickname}: [图片] {str(rec['desc'])[:80]}")
            recent_group_ts[gid].append(time.time())
    except Exception as e:
        logger.debug(f"[VISION] 复用描述写回上下文失败: {type(e).__name__}: {e}")
    _last_image[session_key] = rec
    return True

SLEEP_HOUR_START = tuning.i("SLEEP_HOUR_START")
SLEEP_HOUR_END = tuning.i("SLEEP_HOUR_END")

def _in_sleep_hour():
    h = time.localtime().tm_hour
    if SLEEP_HOUR_START <= SLEEP_HOUR_END:
        return SLEEP_HOUR_START <= h < SLEEP_HOUR_END
    return h >= SLEEP_HOUR_START or h < SLEEP_HOUR_END


def _sleep_blocked(event) -> bool:
    """免打扰时段只压**群聊**：私聊 24 小时都能发图（用户 2026-11-11 要求）。"""
    if isinstance(event, PrivateMessageEvent):
        return False
    return _in_sleep_hour()

# ================= 公共视觉接口（给表情库打标签用） =================
DEFAULT_VISION_PROMPT = (
    "用一句话说清这张图是什么。\n"
    "硬性要求：不超过 60 个字，只输出这一句话。\n"
    "禁止分点、禁止解释、禁止补充背景、禁止括号备注。\n"
    "如果是梗图就直接点明梗。"
)
STICKER_VISION_PROMPT = (
    "这是一张聊天表情包。请只输出一行：先说这张图表达的是什么情绪/态度，"
    "只能从这些词里挑一个：无语 / 开心 / 嘲笑 / 委屈 / 吃瓜 / 惊讶 / 困 / 生气 / 害羞 / 点赞 / 狗头 / 哭；"
    "再用不超过60字描述画面。不要任何解释。"
)


# 各 provider 的视觉兜底模型（2026-11-24 实测本账号可用性）
_VISION_FALLBACKS = {
    "glm": ["glm-4v-plus", "glm-4v-flash"],
    "qwen": ["qwen3-vl-plus", "qwen2.5-vl-72b-instruct"],
    "deepseek": ["deepseek-flash"],
    "xai": [],
    "openai": [],
    "custom": [],
}


def _vision_targets() -> list:
    """[(endpoint, api_key, model)] —— 识图用哪家由 `VISION_PROVIDER` 决定。

    2026-11-24 用户要求：「有的用户直接用 deepseek v4 flash 做视觉，有些想换 glm」。
    所以识图不再写死阿里云：providers.resolve("vision") 给出 base_url / key / model，
    这里只负责拼 `/chat/completions` 和兜底模型名。
    """
    try:
        from services import providers
        p = providers.resolve("vision")
        name = str(p.get("provider") or "").lower()
        base = str(p.get("base_url") or "").rstrip("/")
        key = str(p.get("api_key") or "")
        primary = str(p.get("model") or "")
    except Exception as e:      # pragma: no cover - 兜底：老行为
        logger.debug(f"[VISION] providers 解析失败，退回 DashScope: {type(e).__name__}: {e}")
        name, base, key, primary = "qwen", _DASH_BASE, DASHSCOPE_API_KEY, VISION_MODEL

    if not base:
        base = _DASH_BASE
    ep = base if base.endswith("/chat/completions") else base + "/chat/completions"

    models = []
    for m in [primary] + list(_VISION_FALLBACKS.get(name, [])) + [c for c in VISION_MODEL_CANDIDATES]:
        m = str(m or "").strip()
        if m and m not in models:
            models.append(m)
    cached = _load_working_model()
    if cached and cached not in models:
        models.insert(0, cached)
    return [(ep, key, m) for m in models]


async def _vl_describe(image_bytes: bytes, prompt: str = DEFAULT_VISION_PROMPT,
                       max_tokens: int = 60, url: str = "") -> str:
    """
    真正调视觉模型的地方。

    两条载体，按代价从低到高：
      1) url   —— 只把图片链接发过去（省流量，默认已关：链接可能被 CDN 压过）
      2) b64   —— 自己下载 + 压缩后再 base64（默认，画质可控）

    provider 由 `VISION_PROVIDER` 决定（glm / qwen / deepseek / 自定义）。
    """
    if not image_bytes:
        return ""

    targets = _vision_targets()
    if not targets:
        logger.error("❌ [VISION] 没有可用的视觉模型")
        return ""
    if not targets[0][1]:
        logger.error(
            "❌ [VISION] 没有视觉 API key（检查 VISION_API_KEY / "
            f"VISION_PROVIDER={VISION_PROVIDER} 对应的 key）")
        return ""

    # ---- 准备图片载体 ----
    payloads = []          # [(kind, image_item, 说明)]
    if url and VISION_USE_URL:
        payloads.append(("url", {"type": "image_url", "image_url": {"url": url}}, "url直传"))

    small, mime = _compress(image_bytes)
    ratio = f"{len(image_bytes) / 1024:.0f}KB → {len(small) / 1024:.0f}KB"
    logger.info(f"[VISION] 图片压缩 {ratio}（{mime}）")
    if len(small) > VISION_MAX_BYTES:
        logger.warning(
            f"⚠️ [VISION] 压缩后仍有 {len(small) / 1024 / 1024:.1f} MB，"
            f"超过上限 {VISION_MAX_BYTES / 1024 / 1024:.1f} MB，很可能超时"
            f"（装 Pillow 才能自动缩图：pip install Pillow）"
        )
    b64 = f"data:{mime};base64,{base64.b64encode(small).decode('utf-8')}"
    payloads.append(("b64", {"type": "image_url", "image_url": {"url": b64}}, "base64"))

    # ---- 逐个组合尝试（有次数上限，避免一路试到天荒地老）----
    attempts = 0
    last_err = ""
    for endpoint, api_key, model in targets:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        bad_key = False
        model_missing = False
        for kind, item, label in payloads:
            if attempts >= VISION_MAX_ATTEMPTS:
                logger.warning(f"⚠️ [VISION] 已达尝试上限 {VISION_MAX_ATTEMPTS} 次，停止")
                break
            attempts += 1
            t0 = time.time()
            payload = {
                "model": model,
                "messages": [{"role": "user",
                              "content": [item, {"type": "text", "text": prompt}]}],
                "max_tokens": max_tokens,
            }
            try:
                async with httpx.AsyncClient(timeout=VISION_TIMEOUT, trust_env=False) as client:
                    r = await client.post(endpoint, headers=headers, json=payload)
                cost = time.time() - t0

                if r.status_code == 200:
                    data = r.json()
                    desc = data["choices"][0]["message"]["content"]
                    if isinstance(desc, list):
                        desc = " ".join([i.get("text", "") for i in desc if isinstance(i, dict)])
                    desc = (desc or "").strip()
                    if desc:
                        if model != _load_working_model():
                            logger.info(
                                f"[VISION] ✅ 视觉可用：{model}（{label}，{cost:.1f}s）已记住")
                            _save_working_model(model)
                        return desc
                    last_err = "模型返回空描述"
                    logger.warning(f"[VISION] {model} 返回空描述（{label}，{cost:.1f}s）")
                    model_missing = True
                    break

                body = r.text[:300]
                low = body.lower()
                last_err = f"HTTP {r.status_code}: {body}"
                logger.warning(
                    f"[VISION] {model} @ {endpoint} {label} → HTTP {r.status_code} "
                    f"（{cost:.1f}s）: {body[:120]}"
                )

                if r.status_code == 404 or "model_not_found" in low or "model not exist" in low:
                    model_missing = True
                    break
                if r.status_code in (401, 403):
                    logger.error(f"❌ [VISION] {model} key 无权限（HTTP {r.status_code}）")
                    bad_key = True
                    break
                if r.status_code in (400, 413, 422) and kind == "url":
                    logger.info(f"[VISION] {label} 不可用（图片链接拉不到？），改用 base64 重试")
                    continue
                # 1210：这家模型不收图（纯文本模型）→ 换下一个模型
                if r.status_code == 400 and ("1210" in body or "content.type" in low):
                    model_missing = True
                    break
                logger.error(f"❌ [VISION] 放弃：HTTP {r.status_code}（model={model}）")
                return ""
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"[VISION] {model} @ {endpoint} {label} 请求异常"
                    f"（{time.time() - t0:.1f}s）: {last_err}"
                )
                if kind == "url":
                    continue
                model_missing = True
                break
        if bad_key:
            break
        if model_missing:
            continue
        if attempts >= VISION_MAX_ATTEMPTS:
            break

    logger.error(
        f"❌ [VISION] 所有组合都失败（试了 {attempts} 次）\n"
        f"   目标：{[(m, e) for e, _k, m in targets]}\n"
        f"   最后错误：{last_err}\n"
        f"   跑 `python scripts/probe_models.py` 看真实可用的模型名"
    )
    return ""


def _coerce_image_bytes(obj) -> bytes:
    """
    把各种形态统一成 bytes：
      bytes/bytearray → 原样
      "data:image/png;base64,xxx" → 解码
      "iVBORw0KGgo..." (纯 base64) → 解码
    sticker_receiver 会先传 bytes，失败再传纯 base64 字符串，两种都要吃得下。
    """
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj)
    if not isinstance(obj, str):
        return b""
    s = obj.strip()
    if not s:
        return b""
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    try:
        return base64.b64decode(s, validate=False)
    except Exception:
        return b""


async def describe_image_bytes(image, prompt: str = "") -> str:
    """
    公共识图接口：传图片 bytes（也吃 base64/data URI），返回一句描述。

    默认用**表情包 prompt**（sticker_receiver 打标签就吃这个）；
    但 B 站封面这类普通图片必须自己传 prompt，否则会拿到
    「无语｜画面…」这种按情绪词表硬套的结果（之前 link_preview 就踩了这个坑）。
    """
    data = _coerce_image_bytes(image)
    if not data:
        logger.debug("[VISION] describe_image_bytes 收到的数据无法解析")
        return ""
    return await _vl_describe(data, prompt or STICKER_VISION_PROMPT, max_tokens=80)


# 兼容 sticker_receiver 里可能尝试的各种函数名
describe_image = describe_image_bytes
caption_image = describe_image_bytes
analyze_image = describe_image_bytes


# ================= 视觉处理核心 =================
async def process_image(bot: Bot, event: MessageEvent, img_url: str = "",
                        reply: bool = True):
    # 从 chat 模块延迟导入，防止循环引用
    from plugins.chat import recent_group_msgs, session_queues, session_workers, _session_worker

    session_key = f"group_{event.group_id}" if isinstance(event, GroupMessageEvent) else f"private_{event.user_id}"
    logger.info(f"🚨 [VISION] 触发！开始识别图片...（{'会回复' if reply else '只看不回'}）")

    seg = next((s for s in event.get_message() if s.type == "image"), None)
    if seg is None:
        logger.warning("⚠️ [VISION] 消息里没有 image 段，放弃")
        return
    if img_url and not (seg.data.get("url") or "").strip():
        seg.data["url"] = img_url

    image_data = await _fetch_image_bytes(bot, event, seg)
    if not image_data:
        logger.error("❌ [VISION] 图片取不到（url / get_image / 本地文件都失败）")
        return

    mime = _sniff_mime(image_data)
    logger.info(f"[VISION] 图片 {len(image_data) / 1024:.0f} KB, mime={mime}")

    # 👇 把原图链接也传下去：优先走「链接直传」，请求体只有几百字节，不用上传 9MB
    img_url = (seg.data.get("url") or "").strip()

    # 👇 GIF：GLM / qwen 都读不了动图（GLM 直接报 1210），只能先抽帧。
    #    用户 2026-11-19 定的流程：gif → 全帧拼图给视觉模型总结 → 均分三帧给 trace.moe。
    _is_gif = image_data[:6] in (b"GIF87a", b"GIF89a")
    _vl_data = image_data
    if _is_gif:
        try:
            from services.vision_verify import gif_grid
            _grid = gif_grid(image_data)
            if _grid:
                _vl_data = _grid
                logger.info(
                    f"[VISION] GIF → 抽 6 帧拼成一张图，让视觉模型直接总结"
                    f"（{len(image_data) / 1024:.0f} KB → {len(_grid) / 1024:.0f} KB）")
        except Exception as e:
            logger.warning(f"[VISION] GIF 抽帧失败（退回原图）: {type(e).__name__}: {e}")

    # 👇 走「结构化识别 + 联网核验 + 指纹缓存」：
    #    VL 对画面的描述可信，但对动漫角色/名人的**专名**幻觉率极高（实测 ~60%）。
    #    所以让它把「看到的画面」和「猜的作品/角色」分开写，专名再拿搜索结果核对一遍。
    async def _vl(prompt: str, max_tokens: int = 200) -> str:
        # GIF 抽帧后是本地生成的 PNG，没有可直传的链接 → url 置空走上传
        return await _vl_describe(_vl_data, prompt, max_tokens=max_tokens,
                                  url="" if _is_gif else img_url)

    # 图配文字时，那句话往往就是答案（「这是我cos的小鲸鱼」）
    _caption = ""
    try:
        _caption = event.get_plaintext().strip()[:120]
    except Exception:
        _caption = ""

    # 表情包：让她自己决定收不收（sticker_save），所以要在 hint 里说清这是什么
    try:
        _is_sticker = any(
            s.type == "mface"
            or (s.type == "image" and (
                s.data.get("sub_type") == 1
                or (s.data.get("sub_type") is None
                    and "表情" in str(s.data.get("summary") or ""))))
            for s in event.get_message()
        )
    except Exception:
        _is_sticker = False
    if _is_sticker:
        _caption = (_caption + " " if _caption else "") + "（这是个表情包）"

    rec = {}
    try:
        from services.vision_verify import recognize
        rec = await recognize(image_data, _vl, url=img_url, hint=_caption,
                              is_gif=_is_gif, vl_bytes=_vl_data) or {}
    except Exception as e:
        logger.warning(f"[VISION] 核验模块异常，退回纯识别: {type(e).__name__}: {e}")

    desc = str(rec.get("desc") or "").strip()
    if not desc:
        # 核验模块整个挂掉时的兜底：至少给一句纯描述
        desc = await _vl_describe(_vl_data, url="" if _is_gif else img_url)
    if not desc:
        logger.error("❌ [VISION] 识别失败，本次不回复（详见上面的尝试日志）")
        return
    logger.info(f"✅ [VISION] 识别成功: {desc[:60]}")

    # 缓存描述（同图复用 + 给 vision_source 工具用）
    try:
        _rec = {"desc": desc, "url": str(seg.data.get("url") or ""), "at": time.time()}
        _files = [str(x.data.get("file") or "") for x in event.get_message()
                  if x.type == "image"]
        for _f in _files:
            if _f:
                _image_cache.setdefault(session_key, {})[_f] = _rec
        _last_image[session_key] = _rec
    except Exception:
        pass

    # 👇 重点：把群号、发信人、精炼描述打包推入 chat 队列
    nickname = event.sender.card or event.sender.nickname or str(event.user_id)
    uid = str(event.user_id)
    gid = str(event.group_id) if isinstance(event, GroupMessageEvent) else "私聊"
    
    # 👇 不再只给一句结论：把 qwen / GLM / trace.moe / 联网核验的**全部证据**摆给 DeepSeek，
    #    有冲突时它自己有常识能判断，比我们在这里硬做「两票制否决」强。
    report = str(rec.get("report") or "").strip()
    if report:
        fake_text = (
            f"{VISION_TAG} 群号:{gid} | 发送者QQ:{uid} | 发送者昵称:{nickname}\n"
            f"{report}\n"
            f"（上面是几个来源各自看到的结果。它们互相矛盾时，你自己判断哪个更可信，"
            f"不要照抄明显错的那个；都拿不准就只说画面，别编名字。）"
        )
    else:
        fake_text = f"{VISION_TAG} 群号:{gid} | 发送者QQ:{uid} | 发送者昵称:{nickname} | 图片内容:{desc}"

    # 👇 2026-11-20：这张图是**发给谁**的。
    #    真机翻车：情侣 A @ 完 B 发了个「抱抱」表情，她接话「抱了上去」——
    #    图片没有文字，光看描述判断不了收件人，必须结合 @ 段 + 上文。
    #    同时明确「不要复述画面描述」，否则她每次都先念一遍图再回答。
    try:
        if isinstance(event, GroupMessageEvent):
            from services import group_scene as _gs_t
            _at_qqs = [str(s.data.get("qq") or "") for s in event.get_message()
                       if s.type == "at"]
            _tk, _tn, _tr = _gs_t.image_target(
                gid, uid, _at_qqs,
                bot_qq=identity_mod.canonical(str(getattr(event, "self_id", "") or "")))
            if _tk == "bot":
                _tgt_line = f"这张图是发给你的（{_tr}）。"
            elif _tk == "person":
                _tgt_line = (f"这张图是 {nickname} 发给 {_tn} 的（{_tr}），"
                             f"**不是在跟你说**。")
            else:
                _tgt_line = "看不出这张图是发给谁的。"
        else:
            _tgt_line = "这是私聊发给你的图。"
    except Exception:
        _tgt_line = ""
    if _tgt_line:
        fake_text += (
            f"\n【这张图】{_tgt_line}"
            "\n（上面的画面描述只给你自己看：**绝对不要在回复里复述画面**，"
            "不要写「【图片】…」这种格式，也不要报颜色/姿势/表情/背景细节。"
            "图里是题目、截图或文字信息时，直接回答里面的问题/意思，不要念题干；"
            "其他情况就直接回应图里的内容、情绪或梗。"
            "如果这张图不是发给你的，通常不用接 —— 除非你真的想插一句。）"
        )

    # 👇 图片消息**附带的文字**必须跟图片一起走，别让它掉进「历史对话」里。
    #    真机踩过：群友发二维码 + 一句「扫扫」，她在 prompt 里看到的是
    #    user「（群友发了张图片）」，而「扫扫」只埋在 system 块的历史对话里 →
    #    她理解成「扫码抽卡」，回了「扫这个能出SSR吗」→ 群友「扫你妈」。
    #    拼进 fake_text 之后，llm.py 会把它放回 user 位置（最显眼的地方）。
    _extra_text = (event.get_plaintext() or "").strip()
    if _extra_text:
        fake_text += f"\n（他同时说：「{_extra_text}」）"
    
    # 👇 也写进群聊上下文：否则后续对话她完全不记得「刚才有人发了张图」，
    #    只靠一次性的 [图片识别结果] 推入队列，聊两句就忘了。
    if isinstance(event, GroupMessageEvent):
        try:
            recent_group_msgs[gid].append(f"{nickname}: [图片] {desc[:80]}")
            # ⚠️ msgs 和 ts 是两个 deque，必须同步 append，否则后面按时间配对全错位
            from plugins.chat import recent_group_ts as _rgts
            _rgts[gid].append(time.time())
        except Exception:
            pass
        # 👇 群聊现场也要记一条占位。用户 2026-11-19 的例子：
        #    小强发了张猫猫图 → 鱼「这猫躺得比我还会摆烂」。
        #    图不进现场，审核看到的「这猫」就是一句凭空冒出来的话，前因断链。
        #    no_audit=True：占位没有字可判收件人，不进审核（省一次调用）。
        try:
            from services import group_scene as _gs
            _gs.observe(
                gid, mid=str(getattr(event, "message_id", "") or ""),
                qq=uid, name=nickname, text=f"[图片] {desc[:60]}",
                reply_mid=str(getattr(getattr(event, "reply", None),
                                      "message_id", "") or ""),
                is_bot=False,
                bot_qq=identity_mod.canonical(str(getattr(event, "self_id", "") or "")),
                no_audit=True,
            )
        except Exception:
            pass

    # 👇 落盘：图片压缩后存 data/images/，对话里挂上路径。
    #    周报要「把图片插到对应的对话上」，靠的就是这里。
    try:
        from services import dialogue_log
        img_rel = dialogue_log.save_image(image_data)
        dialogue_log.log_msg(
            gid, nickname, uid, "[图片]",
            kind="user", img=img_rel, vision=desc,
            extra={"anime": rec.get("work", ""), "char": rec.get("char", ""),
                   "source": rec.get("source", "")},
        )
    except Exception as e:
        logger.debug(f"[VISION] 落盘失败: {e}")

    ctx = None
    if isinstance(event, GroupMessageEvent):
        # ⚠️ 纯图片消息 get_plaintext() 是空的，chat._need_reply 根本不会把它塞进 recent_group_msgs。
        #    所以这里**不能无条件 pop**，否则会误删上一条正常聊天记录。
        if recent_group_msgs[gid] and recent_group_msgs[gid][-1].startswith(f"{nickname}: "):
            last = recent_group_msgs[gid][-1]
            if last.split(": ", 1)[-1].strip() == "":
                recent_group_msgs[gid].pop()
        ctx = list(recent_group_msgs[gid])[-6:] or None

    if not reply:
        # 只看不回：识别结果已经写进 recent_group_msgs + 落盘了，
        # 后面聊天她"记得"这张图，但**不会为这张图单独开口**。
        logger.info("[VISION] 只看不回（没喊她名字）→ 已写入上下文，不进回复队列")
        return

    queue = session_queues[session_key]
    await queue.put((event, fake_text, ctx))

    if session_key not in session_workers or session_workers[session_key].done():
        session_workers[session_key] = asyncio.create_task(_session_worker(bot, session_key))


def _vision_should_reply(event: MessageEvent) -> bool:
    """
    这张图要不要**进入她的决策流程**（不是「要不要回」—— 回不回由 LLM 定）。

    llm（默认）：交给她自己想，用 stay_silent 拒绝。只受两道硬闸门限制：
                 被群友嫌烦过（mute 生效中）、每小时超过 N 张。
    mentioned：老行为，只有喊了名字才进队列。
    always：旧行为（每 VISION_COOLDOWN 秒回一次）。
    """
    try:
        from plugins.chat import _calls_me_by_name, _is_muted, _get_session_key
    except Exception as e:
        logger.debug(f"[VISION] 拿不到 chat 的判定函数: {e}")
        return False

    if VISION_REPLY_MODE == "always":
        return True

    key = _get_session_key(event)
    # 被群友嫌弃过（「真烦人」「能不能关了先」）→ 这段时间一律不往上凑
    try:
        if isinstance(event, GroupMessageEvent) and _is_muted(str(event.group_id)):
            return False
    except Exception:
        pass

    if VISION_REPLY_MODE == "mentioned":
        try:
            txt = (event.get_plaintext() or "").strip()
            if txt and _calls_me_by_name(txt, key, str(event.user_id)):
                return True
        except Exception as e:
            logger.debug(f"[VISION] 判定要不要回失败（按不回处理）: {e}")
        return False

    # ---- llm 模式 ----
    now = time.time()
    win = _vision_llm_window[key]
    while win and now - win[0] > 3600:
        win.pop(0)
    if len(win) >= VISION_LLM_MAX_PER_HOUR:
        logger.info(
            f"[VISION] {key} 本小时交给 LLM 决策的图已到上限 "
            f"{VISION_LLM_MAX_PER_HOUR} 张 → 这张只记上下文，不打扰她"
        )
        return False
    win.append(now)
    return True


# ================= 被动识图规则 =================
async def check_passive_image(event: MessageEvent) -> bool:
    if identity_mod.is_self(str(event.user_id), str(getattr(event, "self_id", "") or "")): return False
    if _sleep_blocked(event) or event.is_tome(): return False

    if isinstance(event, GroupMessageEvent):
        from plugins.chat import last_group_activity
        gid = str(event.group_id)
        if time.time() - last_group_activity.get(gid, 0) > 300:
            return False

    # 自定义表情（sub_type==1，或某些版本 sub_type 为空但 summary 带「表情」）
    # 归 chat 的 sticker_collect 管，这里别再重复识别一次 —— 同一张图两路各调一次
    # 视觉模型，浪费额度还容易描述打架（2026-11-17 实测：GLM 说「微笑」，这里说「冷淡」）。
    def _is_sticker(seg) -> bool:
        sub = seg.data.get("sub_type")
        return sub == 1 or (sub is None and "表情" in str(seg.data.get("summary") or ""))

    # STICKER_DECIDE=llm（默认）→ 表情包也交给她「看一眼再决定收不收」，
    # 所以不再在这里排除；只有明确配置成 auto 时才把表情包让给 sticker_collect。
    _segs = [seg for seg in event.get_message()
             if seg.type == "image"
             and (STICKER_DECIDE == "llm" or not _is_sticker(seg))]
    if not _segs:
        return False

    session_key = f"group_{event.group_id}" if isinstance(event, GroupMessageEvent) else f"private_{event.user_id}"
    now = time.time()

    # 👇 同一张图（按 NapCat 的文件名判）在 IMAGE_DEDUP_HOURS 内只处理一次。
    #    群友复读同一张图时，她不该再评论一遍。
    _files = [str(s.data.get("file") or s.data.get("file_id") or "") for s in _segs]
    _files = [f for f in _files if f]
    _seen = _image_seen.setdefault(session_key, {})
    for _k in [_k for _k, _t in _seen.items() if now - _t > IMAGE_DEDUP_HOURS * 3600]:
        _seen.pop(_k, None)
    if _files and all(f in _seen for f in _files):
        _reused = _replay_cached(event, session_key, _files)
        logger.info(
            f"[VISION] 同一张图 {IMAGE_DEDUP_HOURS:.0f}h 内已处理过"
            f"（{_files[0][:28]}）→ 不重复识别"
            + ("，已复用上次的描述写回上下文" if _reused else "，但没有缓存描述可用"))
        return False

    if now - _last_vision_time[session_key] < VISION_COOLDOWN:
        return False

    _last_vision_time[session_key] = now
    for _f in _files:
        _seen[_f] = now
    return True

passive_vision = on_message(rule=Rule(check_passive_image), priority=5, block=True)

@passive_vision.handle()
async def _(bot: Bot, event: MessageEvent):
    # url 可能为空（NapCat 只给了 file），交给 process_image 内部兜底，不要在这里拦掉
    asyncio.create_task(process_image(bot, event, reply=_vision_should_reply(event)))

# ================= 主动 @ 识图规则 =================
async def check_active_image(event: MessageEvent) -> bool:
    if identity_mod.is_self(str(event.user_id), str(getattr(event, "self_id", "") or "")): return False
    if _sleep_blocked(event): return False
    return event.is_tome() and any(seg.type == "image" for seg in event.get_message())

active_vision = on_message(rule=Rule(check_active_image), priority=5, block=True)

@active_vision.handle()
async def _(bot: Bot, event: MessageEvent):
    asyncio.create_task(process_image(bot, event))
