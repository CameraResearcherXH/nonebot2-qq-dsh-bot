# services/sticker_receiver.py
from core import tuning
import asyncio
import base64
import importlib
import os
import random
import re
import time

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message

from services.sticker_library import sticker_lib, TAG_LIBRARY

# 视觉侧「强度」阈值：|强度| 低于它 = 不上心，不收（2026-11-20）
STICKER_COLLECT_MIN = tuning.f("STICKER_COLLECT_MIN")


# ================= 收发表情计数器（重启清零，够用） =================
STATS = {
    "received_today": 0,
    "sent_today": 0,
    "day": "",
}

def _tick(kind: str):
    today = time.strftime("%Y%m%d")
    if STATS["day"] != today:
        STATS.update({"received_today": 0, "sent_today": 0, "day": today})
    STATS[f"{kind}_today"] += 1

def bump_received():
    _tick("received")

def bump_sent():
    _tick("sent")


# ================= pending 暂存（"她挑"模式） =================
# chat_id -> list[{md5, data(base64), ext, chat_id, desc, tag_hint}]
PENDING: dict[str, list[dict]] = {}

def list_pending(chat_id: str) -> list:
    return PENDING.get(chat_id, [])

def describe_pending(chat_id: str) -> str:
    """给模型'看'的 pending 清单（带编号）"""
    pending = PENDING.get(chat_id, [])
    if not pending:
        return ""
    lines = []
    for i, p in enumerate(pending, start=1):
        desc = p.get("desc") or "待识别的表情"
        lines.append(f"第{i}张：{desc}")
    return "\n".join(lines)

def commit_pending_by_index(chat_id: str, indexes: list[int]) -> int:
    """收下指定编号（1-based），其余保留"""
    pending = PENDING.get(chat_id, [])
    if not pending:
        return 0
    taken = 0
    new_pending = []
    for i, item in enumerate(pending, start=1):
        if i in indexes:
            data = base64.b64decode(item["data"])
            tag = item.get("tag_hint") or "吃瓜"
            r = sticker_lib.add(
                data=data, tag=tag, chat_id=chat_id, ext=item["ext"],
                desc=item.get("desc") or "",
                quality=float(item.get("quality", 0.5) or 0.5),
            )
            if r["ok"]:
                taken += 1
                bump_received()
        else:
            new_pending.append(item)
    PENDING[chat_id] = new_pending
    return taken

def commit_pending(chat_id: str, indexes: list[int] | None = None) -> int:
    """不带编号时，全部收下"""
    pending = PENDING.get(chat_id, [])
    if not pending:
        return 0
    if indexes is None:
        indexes = list(range(1, len(pending) + 1))
    return commit_pending_by_index(chat_id, indexes)


def commit_pending_with_tag(chat_id: str, indexes: list[int], tag: str = "") -> int:
    """收下指定编号（1-based），可覆盖 tag 为她写的心情标签（用户 2026-11-16）。"""
    pending = PENDING.get(chat_id, [])
    if not pending:
        return 0
    taken = 0
    new_pending = []
    for i, item in enumerate(pending, start=1):
        if i in indexes:
            data = base64.b64decode(item["data"])
            use_tag = (tag or item.get("tag_hint") or "吃瓜")
            r = sticker_lib.add(
                data=data, tag=use_tag, chat_id=chat_id, ext=item["ext"],
                desc=item.get("desc") or "",
                quality=float(item.get("quality", 0.5) or 0.5),
                reaction=float(item.get("reaction") or 0.0),
            )
            if r["ok"]:
                taken += 1
                bump_received()
        else:
            new_pending.append(item)
    PENDING[chat_id] = new_pending
    return taken

def drop_pending(chat_id: str) -> int:
    n = len(PENDING.get(chat_id, []))
    PENDING[chat_id] = []
    return n


# ================= 视觉识别（兼容多种函数名） =================
async def _call_vision_any(image_bytes: bytes) -> str:
    """
    调用视觉模块，返回图片描述文本。

    ⚠️ 原来这里写的是 importlib.import_module("services.vision_agent")，
       但视觉代码实际在 plugins/vision_agent/__init__.py —— 这个 import 永远失败，
       于是 classify_sticker_by_vision 只能 random.choice(TAG_LIBRARY)，
       每张表情的标签都是随机的。这就是「表情分类是坏的」的真正原因。
    """
    va = None
    for mod_name in ("plugins.vision_agent", "services.vision_agent"):
        try:
            va = importlib.import_module(mod_name)
            break
        except Exception as e:
            logger.debug(f"[STICKER] import {mod_name} 失败: {e}")
    if va is None:
        logger.warning("[STICKER] ⚠️ 找不到视觉模块，表情只能随机打标签")
        return ""

    # 1) 传字节的函数名候选
    byte_fns = (
        "describe_image_bytes", "describe_image", "caption_image",
        "analyze_image", "describe_bytes",
        "describe_image_base64", "image_to_text",
    )
    for fname in byte_fns:
        fn = getattr(va, fname, None)
        if not fn:
            continue
        # 依次尝试传 bytes / base64 字符串
        for arg in (image_bytes, base64.b64encode(image_bytes).decode()):
            try:
                r = fn(arg)
                if asyncio.iscoroutine(r):
                    r = await r
                if isinstance(r, str) and r.strip():
                    return r
                if isinstance(r, dict):
                    for key in ("text", "description", "desc", "caption", "content"):
                        v = r.get(key)
                        if isinstance(v, str) and v.strip():
                            return v
            except Exception as e:
                logger.debug(f"[STICKER] vision_agent.{fname} 调用失败: {e}")

    # 2) 传 URL / data URI 的函数名候选
    url_fns = ("describe_url", "caption_url", "analyze_image_url")
    b64 = base64.b64encode(image_bytes).decode()
    data_uri = f"data:image/png;base64,{b64}"
    for fname in url_fns:
        fn = getattr(va, fname, None)
        if not fn:
            continue
        try:
            r = fn(data_uri)
            if asyncio.iscoroutine(r):
                r = await r
            if isinstance(r, str) and r.strip():
                return r
        except Exception as e:
            logger.debug(f"[STICKER] vision_agent.{fname} 失败: {e}")

    return ""


# 关键词 → 标签 的映射表（先匹配更具体的）
_TAG_HINTS = [
    # 困 / 睡
    ("晕", "困"), ("睡着", "困"), ("闭眼", "困"), ("瞌睡", "困"), ("zzz", "困"), ("zZ", "困"),
    # 哭
    ("哭", "哭"), ("流泪", "哭"), ("流泪", "哭"), ("眼泪", "哭"), ("泪", "哭"),
    # 委屈
    ("委屈", "委屈"), ("难过", "委屈"), ("悲伤", "委屈"), ("伤心", "委屈"),
    # 生气
    ("生气", "生气"), ("怒", "生气"), ("骂", "生气"), ("火", "生气"),
    # 嘲笑
    ("嘲笑", "嘲笑"), ("不屑", "嘲笑"), ("滑稽", "嘲笑"), ("鬼畜", "嘲笑"), ("奸笑", "嘲笑"),
    # 无语
    ("无语", "无语"), ("沉默", "无语"), ("翻白眼", "无语"), ("无奈", "无语"), ("摊手", "无语"),
    # 吃瓜
    ("吃瓜", "吃瓜"), ("围观", "吃瓜"), ("看戏", "吃瓜"), ("西瓜", "吃瓜"),
    # 惊讶
    ("震惊", "惊讶"), ("惊", "惊讶"), ("瞪大", "惊讶"), ("张嘴", "惊讶"), ("哇", "惊讶"),
    # 害羞
    ("害羞", "害羞"), ("脸红", "害羞"), ("羞", "害羞"),
    # 点赞
    ("点赞", "点赞"), ("大拇指", "点赞"), ("称赞", "点赞"), ("竖拇指", "点赞"),
    # 狗头
    ("狗头", "狗头"), ("doge", "狗头"),
    # 开心
    ("哈哈", "开心"), ("哈", "开心"),
    ("笑", "开心"), ("开心", "开心"), ("欢乐", "开心"), ("高兴", "开心"), ("喜悦", "开心"),
    # 搞怪 / 捣蛋（TAG_LIBRARY 后补的标签，给自由词兜底）
    ("捣蛋", "搞怪"), ("调皮", "搞怪"), ("整蛊", "搞怪"), ("恶搞", "搞怪"), ("鬼马", "搞怪"),
    # 得意 / 嚣张
    ("得意", "得意"), ("嚣张", "搞怪"), ("最强", "得意"), ("叉腰", "得意"),
]


# 表情识图 prompt：只描述画面，不判情绪标签 —— 情绪标签由「她」按心情决定。
# 用户 2026-11-16：「返回 60 个字说明图内容，然后 llm 来选择是否打标签收藏」。
STICKER_DESC_PROMPT = (
    "这是一张聊天表情包。请只用一句话描述画面内容"
    "（谁/什么在做什么、什么表情或动作、有什么梗），"
    "不超过 60 个字，只输出这一句话，不要解释、不要分点、不要情绪词标签。"
)


async def _gif_to_still(gif_bytes: bytes, grid: str = "4x4") -> bytes | None:
    """
    GIF → 抽全帧拼成一张静态网格 PNG（GLM 视觉不支持 GIF，报 1210）。
    GIF 短所以全抽；grid 4x4 最多 16 帧，超出部分截断（表情 GIF 基本够）。
    """
    import shutil
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp(prefix="sticker_gif_"))
    gif_p = tmp / "in.gif"
    out_p = tmp / "montage.png"
    try:
        gif_p.write_bytes(gif_bytes)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(gif_p),
            "-vf", f"tile={grid}",
            "-frames:v", "1", str(out_p),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        if out_p.exists() and out_p.stat().st_size > 0:
            return out_p.read_bytes()
    except Exception as e:
        logger.warning(f"[STICKER] GIF 抽帧失败: {type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return None


async def describe_sticker_glm_first(image_bytes: bytes, prompt: str = "") -> str:
    """
    表情识图：优先 GLM（本月免费额度多），GLM 没额度/失败再退回 qwen。
    返回模型原文（保留换行，方便解析「标签/描述」两行格式），失败返回空串。
    """
    prompt = prompt or STICKER_DESC_PROMPT
    # GIF → GLM 视觉 1210，先抽全帧拼成一张静态图再识别（2026-11-17）
    if image_bytes[:6] in (b"GIF87a", b"GIF89a"):
        still = await _gif_to_still(image_bytes)
        if still:
            image_bytes = still
            logger.info("[STICKER] GIF 已抽帧拼成静态图再识别")
    # 1) GLM 优先
    try:
        from . import glm
        if glm.available():
            desc = await glm.describe_image(image_bytes, prompt, max_tokens=160)
            desc = (desc or "").strip()
            if desc:
                logger.info(f"[STICKER] GLM 识图：{desc[:60]}")
                return desc
    except Exception as e:
        logger.debug(f"[STICKER] GLM 识图失败（转 qwen）: {type(e).__name__}: {e}")

    # 2) qwen 兜底
    try:
        from plugins import vision_agent
        desc = await vision_agent._vl_describe(image_bytes, prompt, max_tokens=160)
        desc = (desc or "").strip()
        if desc:
            logger.info(f"[STICKER] qwen 识图（GLM 不可用）：{desc[:60]}")
            return desc
    except Exception as e:
        logger.debug(f"[STICKER] qwen 识图失败: {type(e).__name__}: {e}")
    return ""


def coerce_tag(word: str) -> str:
    """把她的自由心情词（笑死/可爱/哈哈哈）映射到 TAG_LIBRARY 里能发的标签。"""
    w = str(word or "").strip()
    if not w:
        return ""
    if w in TAG_LIBRARY:
        return w
    low = w.lower()
    for kw, tag in _TAG_HINTS:
        if kw.lower() in low:
            return tag
    return ""


def _sticker_tag_prompt() -> str:
    """视觉侧一次调用出三行：心情（自由词）/ 强度 / 描述。

    2026-11-20：不再从固定 18 词里挑。标签 = 她看到这张图时的心情名词，
    强度决定「收不收」（|强度| >= STICKER_COLLECT_MIN 才收）。
    """
    return (
        "这是一张聊天表情包。请严格按三行输出，不要解释：\n"
        "反应：4~10 字，**这张图给你的第一反应**（例：这张好贱、被戳到、笑得不行、"
        "无语到翻白眼）。写「对这张图的反应」，不是「我此刻心情如何」\n"
        "强度：-1.00 ~ +1.00，**这张图让你变化多大**（和你现在心情好不好无关）："
        "好笑/被戳到=正，恶心/无语=负；平淡无感的图给 0.0x\n"
        "描述：不超过40字，说画面里是谁/什么在做什么、表情动作、画面上的文字"
    )


def _parse_tag_desc(raw: str) -> tuple:
    """解析「心情 / 强度 / 描述」三行；兼容旧的「标签」写法。

    返回 (mood, desc, strength)。解析不出时整段当描述，strength=0。
    """
    raw = str(raw or "").strip()
    mood = desc = ""
    strength = 0.0
    for line in raw.splitlines():
        line = line.strip().strip("。，, ")
        if not line:
            continue
        m = re.search(r"(?:反应|心情|标签|情绪)\s*[:：]?\s*(.+)", line)
        if m and not mood:
            mood = m.group(1).strip()
            continue
        m = re.search(r"(?:强度|幅度)\s*[:：]?\s*([-+]?\d+(?:\.\d+)?)", line)
        if m:
            try:
                strength = max(-1.0, min(1.0, float(m.group(1))))
            except Exception:
                strength = 0.0
            continue
        m = re.search(r"(?:描述|画面)\s*[:：]?\s*(.+)", line)
        if m:
            desc = m.group(1).strip()
    if not desc:
        desc = raw
    return mood, desc, strength


def _tag_from_desc(desc: str) -> str:
    """从画面描述里按关键词兜一个标签（模型没给标签时的降级路径）。"""
    low = str(desc or "").lower()
    for kw, tag in _TAG_HINTS:
        if kw.lower() in low:
            return tag
    return ""


async def classify_sticker_detail(image_bytes: bytes) -> dict:
    """视觉侧完整结果：{mood, desc, strength}。

    mood = 自由心情词（标签）；strength = 这张图让她心情变化多大（收不收的依据）。
    """
    raw = await describe_sticker_glm_first(image_bytes, _sticker_tag_prompt())
    mood, desc, strength = _parse_tag_desc(raw)
    # 模型没给描述 → 老纯描述 prompt 再试一次
    if not desc:
        desc = await describe_sticker_glm_first(image_bytes, STICKER_DESC_PROMPT)
    # 模型没给心情 → 关键词兜底
    if not mood:
        mood = _tag_from_desc(desc)
    if not mood:
        mood = "吃瓜"
    return {"mood": str(mood)[:12], "desc": str(desc or "")[:60],
            "strength": float(strength or 0.0)}


async def classify_sticker_by_vision(image_bytes: bytes) -> tuple:
    """兼容旧调用方：返回 (tag, desc)。"""
    d = await classify_sticker_detail(image_bytes)
    return d["mood"], d["desc"]


# ================= 从消息段提取表情数据 =================
async def extract_sticker_from_segment(bot, seg) -> tuple[bytes, str] | None:
    """
    普通图片段（sub_type==1 视为自定义表情），返回 (image_bytes, ext) 或 None。
    mface 不在这个函数里处理。
    """
    if seg.type != "image":
        return None
    # sub_type==1 是 OneBot 的自定义表情标记；某些 NapCat 版本不带这个字段，
    # 这时用 summary 里有没有「表情」兜一下，否则真实表情全被丢掉
    sub_type = seg.data.get("sub_type")
    is_sticker = (sub_type == 1) or (sub_type is None and "表情" in str(seg.data.get("summary") or ""))
    if not is_sticker:
        return None
    url = seg.data.get("url")
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return None
            data = r.content
    except Exception as e:
        logger.debug(f"[STICKER] 下载表情失败: {e}")
        return None

    # 体积上限 2MB
    if len(data) > 2 * 1024 * 1024:
        return None
    # 粗判扩展名
    if data[:6] == b"GIF89a" or data[:6] == b"GIF87a":
        ext = ".gif"
    elif data[:4] == b"\x89PNG":
        ext = ".png"
    elif data[:3] == b"\xff\xd8\xff":
        ext = ".jpg"
    else:
        ext = ".png"
    return data, ext


def _parse_mface(seg) -> tuple[str, str] | None:
    """
    解析 mface 段，返回 (emoji_id, summary)，拿不到 emoji_id 就返回 None。
    兼容不同 OneBot / NapCat 版本的字段命名。
    """
    d = seg.data or {}
    logger.debug(f"[STICKER] mface 原始段: {d}")

    emoji_id = (
        d.get("emoji_id")
        or d.get("emojiId")
        or d.get("key")
        or d.get("id")
        or ""
    )
    if not emoji_id and isinstance(d.get("emoji"), dict):
        emoji_id = d["emoji"].get("emojiId") or d["emoji"].get("emoji_id") or ""

    summary = (
        d.get("summary")
        or d.get("text")
        or d.get("desc")
        or d.get("emoji_name")
        or ""
    )
    if not emoji_id:
        return None
    return str(emoji_id), str(summary)


# ================= 收图主流程 =================
async def try_collect_sticker(bot: Bot, event: MessageEvent, msg: Message,
                              force: bool = False) -> int:
    """
    遍历消息段，有真表情就尝试收集。
    收集模式由 .env 的 STICKER_COLLECT_MODE 控制：
      auto  = 立刻收（视觉模型自动打标签）
      ask   = 存 pending，等她说"收"/"不收"
      off   = 关闭

    force=True：她**自己决定**要收（sticker_save 工具），忽略 STICKER_COLLECT_MODE。
    用户 2026-11-19：收不收该由她看了图之后决定，不该在识图之前就自动收掉。
    """
    import os
    mode = os.getenv("STICKER_COLLECT_MODE", "auto").lower()
    if mode == "off" and not force:
        return 0

    chat_id = event.get_session_id()
    collected = 0

    for seg in msg:
        # ---------- 商城表情 mface ----------
        if seg.type == "mface":
            parsed = _parse_mface(seg)
            if not parsed:
                logger.info("[STICKER] mface 段里找不到 emoji_id，跳过")
                continue
            emoji_id, summary = parsed

            key = f"mface_{emoji_id}"
            if key in sticker_lib.index:
                continue

            # 商城表情不参与"她挑"模式，直接收
            tag = "吃瓜"
            sticker_lib.index[key] = {
                "tag": tag,
                "mface_id": emoji_id,
                "summary": summary,
                "used_count": 0,
                "created_at": time.time(),
            }
            sticker_lib._save()
            collected += 1
            bump_received()
            logger.info(f"[STICKER] 收了商城表情: {emoji_id} ({summary})")
            continue

        # ---------- 普通自定义表情（图片） ----------
        if seg.type == "image":
            got = await extract_sticker_from_segment(bot, seg)
            if not got:
                continue
            data, ext = got
            md5 = sticker_lib.md5_of(data)

            if md5 in sticker_lib.index:
                continue

            if mode == "ask" and not force:
                pending = PENDING.setdefault(chat_id, [])
                if len(pending) >= 4:
                    continue
                _d = await classify_sticker_detail(data)
                tag, desc = _d["mood"], _d["desc"]
                pending.append({
                    "md5": md5,
                    "data": base64.b64encode(data).decode(),
                    "ext": ext,
                    "chat_id": chat_id,
                    "desc": desc,
                    "tag_hint": tag,
                    "reaction": float(_d.get("strength") or 0.0),
                })
                collected += 1
                bump_received()
                logger.info(f"[STICKER] 暂存一张表情待她挑: {desc or '(无描述)'} (tag={tag})")
            else:  # auto
                _detail = await classify_sticker_detail(data)
                tag, desc = _detail["mood"], _detail["desc"]
                _st = float(_detail.get("strength") or 0.0)
                # 阈值收集：变化太小 = 不上心，不收（对应「人对不在意的事不上心」）
                if abs(_st) < STICKER_COLLECT_MIN:
                    logger.info(
                        f"[STICKER] 强度 {_st:+.2f} < {STICKER_COLLECT_MIN:.2f} "
                        f"→ 不上心，不收（{tag} / {desc[:30]}）")
                    continue
                # 标签合并：精确 → 别名 → 包含 → 才算新词
                try:
                    tag = sticker_lib.resolve_tag(tag)
                except Exception:
                    pass

                # 👇 策展人审查：库容宽松就直接收；拥挤/疑似重复才交 DeepSeek 判精品
                verdict = {"accept": True, "quality": 0.5, "reason": "跳过审查"}
                try:
                    from . import sticker_curator
                    verdict = await sticker_curator.review_before_collect(desc, tag)
                except Exception as e:
                    logger.debug(f"[STICKER] 策展审查失败，直接收: {e}")

                if not verdict.get("accept", True):
                    logger.info(
                        f"[STICKER] 🚫 不收这张：{verdict.get('reason')}"
                        f"（quality={verdict.get('quality')}，tag={tag}）"
                    )
                    continue

                result = sticker_lib.add(
                    data=data, tag=tag, chat_id=chat_id, ext=ext,
                    desc=desc, quality=float(verdict.get("quality", 0.5)),
                    # 记下「这张图让她反应多大」（收集时判定的强度 Δ），
                    # 不是她当时的心情水平 —— 以后挑图/排序看这个。
                    reaction=_st,
                )
                if result.get("ok"):
                    collected += 1
                    bump_received()
                    logger.info(
                        f"[STICKER] 收了一张表情 → {tag}"
                        f"（精品分 {verdict.get('quality')}，{desc or '无描述'}）"
                    )
                else:
                    logger.debug(f"[STICKER] 收图失败: {result.get('reason')}")

    # 👇 收完之后控容量：超上限就让 DeepSeek 判该删谁
    if collected:
        try:
            from . import sticker_curator
            if sticker_lib.over_limit():
                await sticker_curator.maintain_capacity()
        except Exception as e:
            logger.warning(f"[STICKER] 容量维护失败: {e}")

    return collected
