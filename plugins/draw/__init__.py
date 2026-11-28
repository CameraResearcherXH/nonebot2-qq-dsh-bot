# plugins/draw/__init__.py
"""
画图插件（服务器端）：/画图 + /以图绘图

链路：
  群友发指令 → 服务器把中文描述翻成 danbooru tags（调 XAI/Grok）
            → 塞任务进 tool_dispatcher 的任务队列（type="draw"）
            → 算力机 node.py 轮询拉走 → 本地调 ComfyUI 出图 → 回传 png
            → 服务器把 png 发回 QQ

命令：
  /画图 <描述>                 文生图
  /以图绘图 <模式> <描述>       图生图，模式：
     线稿/上色   → 草图线稿上色（ControlNet Lineart）
     姿势/动作   → 参考图锁姿势（ControlNet OpenPose）
     景深/透视   → 参考图锁景深（ControlNet Depth）
     画风/风格   → 参考图画风迁移（IP-Adapter）
     重绘/图生图 → 直接重绘（img2img）
  图可以跟着指令一起发，也可以先发指令再补图（60 秒内）；/取消 退出。

配置（服务器 .env，都有默认值）：
  DRAW_MAX_CONCURRENT=2   同时进流程的画图数（算力机 node.py 串行，实际 1）
  DRAW_WAIT_SEC=360       等成品的最长秒数
  DRAW_ENABLED=true       总开关
"""
from core import tuning
import json
import os
import re
import time
import asyncio
from pathlib import Path

import httpx
from nonebot import on_command, on_message, logger
from nonebot.rule import Rule
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg

from services.config import XAI_API_KEY, XAI_BASE_URL, XAI_MODEL, PROXY
from services.llm import register_tool, TOOL_CTX
from services import skill_switch

TEMP_DRAW = Path("data/temp_draw")
TEMP_DRAW.mkdir(parents=True, exist_ok=True)

DRAW_ENABLED = os.getenv("DRAW_ENABLED", "true").strip().lower() != "false"
DRAW_WAIT_SEC = tuning.f("DRAW_WAIT_SEC")
DRAW_MAX_CONCURRENT = max(1, tuning.i("DRAW_MAX_CONCURRENT"))
_draw_semaphore = asyncio.Semaphore(DRAW_MAX_CONCURRENT)

# ================= 画图总开关（/画图 on|off，仿翻唱开关） =================
# 状态落盘 data/draw_switch.json：bot 重启不丢，算力机忙时主人手动暂停接单。
_DRAW_SWITCH_FILE = Path("data") / "draw_switch.json"
_DRAW_SWITCH = {"on": True, "by": "", "ts": 0.0}
_DRAW_SWITCH_ON = ("on", "开", "开启", "恢复", "启用", "1", "true")
_DRAW_SWITCH_OFF = ("off", "关", "关闭", "停", "暂停", "0", "false")


def _load_draw_switch():
    try:
        if _DRAW_SWITCH_FILE.exists():
            _d = json.loads(_DRAW_SWITCH_FILE.read_text(encoding="utf-8"))
            if isinstance(_d, dict):
                _DRAW_SWITCH["on"] = bool(_d.get("on", True))
                _DRAW_SWITCH["by"] = str(_d.get("by") or "")
                _DRAW_SWITCH["ts"] = float(_d.get("ts") or 0)
                if not _DRAW_SWITCH["on"]:
                    logger.warning(f"[DRAW] 画图开关是关闭状态（{_DRAW_SWITCH['by'] or '?'} 关的），"
                                   f"发 /画图 on 恢复")
    except Exception as e:
        logger.warning(f"[DRAW] 读画图开关失败: {type(e).__name__}: {e}")


def draw_on() -> bool:
    return bool(_DRAW_SWITCH.get("on", True))


def set_draw(on: bool, by: str = "") -> None:
    _DRAW_SWITCH["on"] = bool(on)
    _DRAW_SWITCH["by"] = str(by or "")
    _DRAW_SWITCH["ts"] = time.time()
    try:
        _DRAW_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DRAW_SWITCH_FILE.write_text(json.dumps(_DRAW_SWITCH, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[DRAW] 写画图开关失败: {type(e).__name__}: {e}")
    logger.info(f"[DRAW] 画图开关 → {'on' if on else 'off'}（by {by}）")


def _parse_draw_switch(text: str):
    """`off` / `关` / `暂停` → False；`on` / `开` → True；其它 → None。"""
    t = str(text or "").strip().lower()
    if not t:
        return None
    head = re.split(r"[\s,，]+", t)[0]
    if head in _DRAW_SWITCH_OFF:
        return False
    if head in _DRAW_SWITCH_ON:
        return True
    return None


def _draw_off_hint() -> str:
    _by = _DRAW_SWITCH.get("by") or "主人"
    return (f"🛑 画图现在是关着的（{_by} 关的）\n"
            f"   要用了发 /画图 on")


_load_draw_switch()

# 每个用户同时只能有一张在画
_ACTIVE_USERS: set = set()
# 等待补图：user_id -> {"mode":..., "desc":...}
_WAIT_IMG: dict = {}

draw_cmd = on_command("画图", aliases={"画画", "生成图", "AI画图"}, priority=5, block=True)
imgdraw_cmd = on_command("以图绘图", aliases={"图生图", "以图生图"}, priority=5, block=True)

# 能力开关（/技能 关 画图）：关掉后命令直接提示，不进生成链路
skill_switch.gate(draw_cmd, "draw_image", "画图")
skill_switch.gate(imgdraw_cmd, "draw_image", "画图")

_MODE_ALIASES = {
    "线稿": "lineart", "线稿上色": "lineart", "上色": "lineart", "草图": "lineart", "lineart": "lineart",
    "姿势": "pose", "姿态": "pose", "动作": "pose", "pose": "pose",
    "景深": "depth", "透视": "depth", "深度": "depth", "depth": "depth",
    "画风": "style", "风格": "style", "参考画风": "style", "style": "style",
    "重绘": "img2img", "图生图": "img2img", "img2img": "img2img",
}


def _node_online() -> bool:
    """算力机心跳在线 + caps 里有 draw。"""
    try:
        from plugins.tool_dispatcher import STATUS as S
        caps = S.get("caps") or set()
        if "draw" not in caps:
            return False
        hb = float(S.get("last_heartbeat") or 0)
        return hb and (time.time() - hb) < 90
    except Exception:
        return False


def _looks_like_tags(text: str) -> bool:
    """已经是英文 tags（带逗号且基本无中文）就不翻，原样用。"""
    t = str(text or "").strip()
    if not t:
        return False
    has_cjk = re.search(r"[\u4e00-\u9fff]", t)
    return ("," in t or " " in t) and not has_cjk


async def _translate_to_tags(text: str) -> str:
    """中文 → danbooru tags。失败就原样返回（英文 tags 或中文描述直接下发给模型）。"""
    t = str(text or "").strip()
    if not t or _looks_like_tags(t):
        return t
    if not XAI_API_KEY:
        logger.warning("[DRAW] 没配 XAI_API_KEY，中文描述原样下发给模型（质量会打折）")
        return t
    system = (
        "你是二次元绘画提示词翻译器。把用户的中文描述翻译成英文 danbooru 标签，"
        "用英文逗号分隔，只输出标签本身，不要解释、不要完整句子、不要引号。"
        "保留角色名/作品名的常见英文写法（如 hatsune miku）。"
    )
    payload = {
        "model": XAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": t},
        ],
        "temperature": 0.4,
        "max_tokens": 200,
    }
    headers = {"Authorization": f"Bearer {XAI_API_KEY}"}
    try:
        proxy = PROXY or None
        async with httpx.AsyncClient(timeout=30, proxy=proxy) as c:
            r = await c.post(f"{XAI_BASE_URL}/chat/completions",
                             headers=headers, json=payload)
            r.raise_for_status()
            out = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"[DRAW] 翻译失败，原样下发: {type(e).__name__}: {e}")
        return t
    out = re.sub(r"[\r\n]+", ", ", out).strip("`\"'")
    return out or t


def _sniff_ext(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".png"


async def _fetch_image(seg: MessageSegment, path: Path) -> bool:
    """从 QQ 图片 segment 下载到 path，返回是否成功。"""
    url = seg.data.get("url") or ""
    if not url:
        return False
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
    }
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(url, headers=headers)
            if r.status_code != 200:
                return False
            data = r.content
        if not data:
            return False
        path.write_bytes(data)
        # 按真实内容补后缀，避免 gif 当 png 存
        ext = _sniff_ext(data)
        if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            path = path.with_suffix(ext)
            path.write_bytes(data)
        return True
    except Exception as e:
        logger.error(f"[DRAW] 下载图片失败: {e}")
        return False


def _extract_image_seg(msg: Message):
    for seg in msg:
        if seg.type == "image":
            return seg
    return None


async def _resolve_image(bot: Bot, event: MessageEvent):
    """从指令消息（含引用）里找参考图，返回 MessageSegment 或 None。"""
    seg = _extract_image_seg(event.get_message())
    if seg:
        return seg
    # 引用了别人的图片消息
    for s in event.get_message():
        if s.type == "reply":
            try:
                mid = int(s.data.get("id") or 0)
                if mid:
                    rep = await bot.get_msg(message_id=mid)
                    return _extract_image_seg(rep.get("message") or Message())
            except Exception as e:
                logger.warning(f"[DRAW] 取引用消息失败: {e}")
    return None


def _negative_default() -> str:
    # NoobAI 官方推荐负向 + 我们自己的 NSFW 锁
    return os.getenv(
        "DRAW_NEGATIVE",
        "nsfw, nsfw content, explicit, worst quality, old, early, low quality, lowres, "
        "normal quality, jpeg artifacts, signature, username, watermark, logo, text, "
        "bad anatomy, bad hands, mutated hands, extra fingers, missing fingers, "
        "extra digit, fewer digits, cropped, blurry, out of focus, rough sketch, "
        "messy lines, unfinished, mammal, anthro, furry, ambiguous form, feral, semi-anthro",
    )


def _is_send_timeout(exc: BaseException) -> bool:
    """NapCat 发大图时的 sendMsg 超时（retcode 1200）≠ 真失败：图几分钟后照样到。"""
    info = getattr(exc, "info", None)
    if isinstance(info, dict) and info.get("retcode") == 1200:
        return True
    msg = str(exc).lower()
    return "timeout" in msg or "retcode=1200" in msg


def _compress_image(src: str) -> str:
    """发 QQ 前压缩：大 PNG → 长边≤1440 的 JPEG。

    NapCat 发大图（1536² PNG ≈ 2.9MB → base64 ≈ 4MB）会 sendMsg 超时
    （retcode 1200），压成 JPEG 后几百 KB，稳定很多。失败就返回原路径。
    """
    try:
        from PIL import Image
        img = Image.open(src).convert("RGB")
        long_side = max(img.size)
        if long_side > 1440:
            r = 1440 / long_side
            img = img.resize(
                (max(1, int(img.width * r)), max(1, int(img.height * r))),
                Image.LANCZOS,
            )
        dst = os.path.join(os.path.dirname(src), "send_" + os.path.basename(src) + ".jpg")
        img.save(dst, "JPEG", quality=90)
        if os.path.getsize(dst) > 0:
            return dst
    except Exception as e:
        logger.warning(f"[DRAW] 压缩失败，改用原图发送: {e}")
    return src


async def _run_draw(bot: Bot, event: MessageEvent, mode: str, desc: str,
                    image_seg=None, image_path=None, notify: bool = True):
    """公共流程：翻 tags → 建任务 → 轮询 → 发图。

    image_seg：QQ 图片消息段（下载到本地再下发）。
    image_path：已有本地文件路径（比如 pixiv 搜图下载好的图），直接用。
    notify：是否发「正在出图」的进度提示；LLM 工具调用时关掉，由模型自己说话。
    """
    if not DRAW_ENABLED:
        await bot.send(event, "🖼️ 画图功能暂时关闭。")
        return
    if not draw_on():
        await bot.send(event, _draw_off_hint())
        return
    if not _node_online():
        await bot.send(event, "😢 算力机画图服务离线，暂时画不了。")
        return
    if event.user_id in _ACTIVE_USERS:
        await bot.send(event, "⏳ 你还有一张在画，等它出来再发下一张吧。")
        return

    desc = str(desc or "").strip()
    if not desc and mode == "txt2img":
        await bot.send(event, "🎨 用法：/画图 <描述>，例如：/画图 樱花树下的白裙女孩")
        return
    if mode != "txt2img" and image_seg is None and image_path is None:
        await bot.send(event, "🖼️ 这个模式需要一张参考图：直接把图跟着指令一起发，或先发指令再补图。")
        return

    tags = await _translate_to_tags(desc)
    task_id = f"draw_{int(time.time() * 1000)}"
    input_path = None

    if image_seg is not None:
        input_path = TEMP_DRAW / f"in_{task_id}.png"
        if not await _fetch_image(image_seg, input_path):
            await bot.send(event, "❌ 参考图下载失败，请重试。")
            return
    elif image_path is not None:
        input_path = Path(image_path)
        if not input_path.exists():
            await bot.send(event, "❌ 参考图文件丢了。")
            return

    payload = {
        "mode": mode,
        "prompt": tags,
        "negative": _negative_default(),
    }
    if input_path is not None:
        payload["input"] = str(input_path)

    async with _draw_semaphore:
        _ACTIVE_USERS.add(event.user_id)
        try:
            from plugins.tool_dispatcher import STATUS as S
            # deliver：重启后靠它把成品补发回原会话（2026-11-24）
            S["tasks"][task_id] = dict(
                payload, type="draw", status="pending", output="",
                deliver={
                    "gid": str(getattr(event, "group_id", "") or ""),
                    "uid": str(getattr(event, "user_id", "") or ""),
                    "label": str(desc or "")[:60],
                })
        except Exception as e:
            logger.error(f"[DRAW] 建任务失败: {e}")
            await bot.send(event, "❌ 任务下发失败，请稍后再试。")
            _ACTIVE_USERS.discard(event.user_id)
            return

        if notify:
            hint = "正在绘图，稍等哦" if mode == "txt2img" \
                else "正在以图绘图，稍等哦"
            await bot.send(event, hint)

        try:
            out_path = None
            abs_out = None
            send_path = None
            rounds = max(30, int(DRAW_WAIT_SEC / 2))
            for _ in range(rounds):
                await asyncio.sleep(2)
                t = S["tasks"].get(task_id, {})
                st = t.get("status")
                if st == "completed":
                    out_path = t.get("output")
                    break
                if st == "failed" or (isinstance(out_path, dict)):
                    err = t.get("output")
                    if isinstance(err, dict):
                        err = err.get("error") or err.get("journal") or str(err)
                    logger.warning(f"[DRAW] 任务失败: {err}")
                    await bot.send(event, f"画失败了：{err}")
                    return
                # 算力机一直没拉走 → 离线
                if st == "pending" and _ >= 30:
                    await bot.send(event, "❌ 算力机没接任务（可能离线了），请稍后再试。")
                    S["tasks"].pop(task_id, None)
                    return

            if not out_path or not isinstance(out_path, str) or not os.path.exists(out_path):
                await bot.send(event, "图是画了，但成品没回来（超时）。")
                S["tasks"].pop(task_id, None)
                return

            abs_out = os.path.abspath(out_path)
            send_path = _compress_image(abs_out)
            try:
                await bot.send(event, MessageSegment.image(file=f"file://{send_path}"))
            except Exception as e:
                if _is_send_timeout(e):
                    # retcode 1200 / send_msg timeout：图几分钟后照样到，别报失败
                    logger.warning(f"[DRAW] 发图超时（图稍后到达），按成功处理: {type(e).__name__}")
                else:
                    logger.error(f"[DRAW] 发图失败: {e}")
                    try:
                        await bot.send(event, "😢 图画好了，但发图失败，稍后再试。")
                    except Exception:
                        pass
            # 算力机如实回报的降级/备注（如 pose 模板缺失→文生图）要让用户看到，
            # 不能只发图、假装按请求模式成功（2026-11-28 审计）。
            _note = str((S["tasks"].get(task_id, {}).get("meta") or {}).get("note") or "")
            if _note:
                try:
                    await bot.send(event, f"⚠️ {_note}")
                except Exception:
                    pass
        finally:
            _ACTIVE_USERS.discard(event.user_id)
            S["tasks"].pop(task_id, None)
            for p in (input_path,):
                # 只删我们自己在 TEMP_DRAW 里下载的参考图；pixiv 搜图落盘的文件不归这里管
                if p and str(p).startswith(str(TEMP_DRAW)) and p.exists():
                    try:
                        p.unlink()
                    except Exception:
                        pass
            if isinstance(out_path, str) and out_path and os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except Exception:
                    pass
            # 压缩出来的临时 jpg 也要删
            if send_path and send_path != abs_out and os.path.exists(send_path):
                try:
                    os.remove(send_path)
                except Exception:
                    pass


@draw_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    desc = arg.extract_plain_text().strip()

    # ---- /画图 on | off：暂停 / 恢复接单（主人/管理员专属，仿翻唱开关）----
    _sw = _parse_draw_switch(desc)
    if _sw is not None:
        from services import access
        if access.level_of(event.user_id) == access.LEVEL_MEMBER:
            await bot.send(event, "这个开关只有主人或管理员能动。")
            return
        set_draw(_sw, by=str(event.user_id))
        if _sw:
            await bot.send(event, "✅ 画图已恢复 —— /画图、/以图绘图 都能用了")
        else:
            await bot.send(event, "🛑 画图已暂停。\n"
                                  "   期间 /画图、/以图绘图 都不接单，她也不会主动画。\n"
                                  "   要用了发 /画图 on 恢复（重启 bot 也不会丢）")
        return

    # ---- 检测到具体角色名 → 先 P 站搜图再重绘（保留角色面部），用户 2026-11-16 ----
    _kw = await _extract_character_keyword(desc)
    if _kw:
        logger.info(f"[DRAW] 检测到角色名「{_kw}」，改走 P 站搜图 → 重绘")
        _msg = await _pixiv_redraw(bot, event, desc, _kw, "img2img")
        await bot.send(event, _msg)
        return

    await _run_draw(bot, event, "txt2img", desc)


@imgdraw_cmd.handle()
async def _(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    text = arg.extract_plain_text().strip()
    if not draw_on():
        await bot.send(event, _draw_off_hint())
        return
    parts = re.split(r"\s+", text, maxsplit=1)
    first = parts[0] if parts else ""
    if first in _MODE_ALIASES:
        mode = _MODE_ALIASES[first]
        desc = parts[1] if len(parts) > 1 else ""
    else:
        # 没写模式：默认重绘（img2img），整段当描述
        mode = "img2img"
        desc = text

    image_seg = await _resolve_image(bot, event)
    if image_seg is None:
        # 让用户补图
        _WAIT_IMG[event.user_id] = {"mode": mode, "desc": desc}
        await imgdraw_cmd.finish(
            f"🖼️ 收到（模式：{mode}）。请把参考图发过来（60 秒内有效），或回复 /取消 退出。"
        )
    await _run_draw(bot, event, mode, desc, image_seg)


# ================= 补图 / 取消 =================
async def _wait_rule(event: MessageEvent) -> bool:
    return event.user_id in _WAIT_IMG


wait_img = on_message(rule=Rule(_wait_rule), priority=1, block=True)


@wait_img.handle()
async def _(bot: Bot, event: MessageEvent):
    text = event.get_plaintext().strip()
    if text == "/取消":
        _WAIT_IMG.pop(event.user_id, None)
        await wait_img.finish("✅ 已取消。")
    state = _WAIT_IMG.pop(event.user_id)
    seg = _extract_image_seg(event.get_message())
    if seg is None:
        # 不是图，塞回去继续等
        _WAIT_IMG[event.user_id] = state
        await wait_img.finish("⏳ 请发一张图片，或回复 /取消 退出。")
    await _run_draw(bot, event, state["mode"], state["desc"], seg)


# ================= LLM 工具（Grok 可调用） =================
# 把「画图 / 搜图 + 以图绘图 / 纯搜图」挂成 function calling 工具，
# 让她自己判断用户是想直接画、还是先搜一张参考图再以图绘图。
# 工具 handler 用 create_task 后台跑，不阻塞本轮回复；图稍后自己发出来。

def _tool_ctx():
    bot = TOOL_CTX.get("bot")
    ev = TOOL_CTX.get("event")
    return bot, ev


def _tool_ready() -> str:
    """返回空串=可以画；否则返回一句给模型的拒绝话术。"""
    if not DRAW_ENABLED:
        return "画图功能现在被关了，别答应对方，直接说现在画不了。"
    if not draw_on():
        return "画图现在被关了（主人关的），别答应对方，直接说现在画不了。"
    if not _node_online():
        return "算力机画图服务离线，暂时画不了，直接跟对方说现在画不了。"
    return ""


async def _tool_draw_image(args: dict) -> str:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "没说画什么，问清楚再画。"
    bot, ev = _tool_ctx()
    if not bot or not ev:
        return "现在画不了（缺会话上下文）。"
    deny = _tool_ready()
    if deny:
        return deny
    asyncio.create_task(_run_draw(bot, ev, "txt2img", prompt, notify=False))
    return "已经开始画了，几分钟后把图发出来"


async def _extract_character_keyword(desc: str) -> str:
    """判断画图描述里有没有「具体角色名」，有则返回该角色的 P 站搜索关键词，没有返回空串。

    用户 2026-11-16：「通过一个角色词，并没有去 pixiv 搜索后以图绘图」——
    /画图 带具体角色名时应该自动先搜图再重绘，而不是纯文生图瞎画。
    """
    t = str(desc or "").strip()
    if not t or not XAI_API_KEY:
        return ""
    system = (
        "你判断一段画图描述里有没有「具体的角色名」（动漫/游戏/小说/虚拟主播里叫得出名字的角色，"
        "比如 芙宁娜、纳西妲、初音ミク、五条悟、星野爱、雷电将军）。\n"
        "只输出 JSON，不要任何解释：{\"has\": true/false, \"keyword\": \"角色名\"}\n"
        "规则：\n"
        "· 描述里只是「一个女孩/白毛少女/女仆/穿白裙的少女」这种泛称，没有具体角色 → has=false\n"
        "· 有具体角色名 → has=true，keyword 写这个角色的通用写法（日文原名优先，用于 P 站搜索）\n"
        "· 有多个角色名 → keyword 写最主要的那一个"
    )
    payload = {
        "model": XAI_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": t},
        ],
        "temperature": 0.1,
        "max_tokens": 80,
    }
    headers = {"Authorization": f"Bearer {XAI_API_KEY}"}
    try:
        proxy = PROXY or None
        async with httpx.AsyncClient(timeout=20, proxy=proxy) as c:
            r = await c.post(f"{XAI_BASE_URL}/chat/completions",
                             headers=headers, json=payload)
            r.raise_for_status()
            out = r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.warning(f"[DRAW] 角色名检测失败（走纯文生图）: {e}")
        return ""
    try:
        import json as _json
        _s = str(out or "").strip()
        if _s.startswith("```"):
            _s = _s.strip("`").strip()
            if _s.lower().startswith("json"):
                _s = _s[4:].strip()
        _s = _s[_s.index("{"): _s.rindex("}") + 1]
        _d = _json.loads(_s)
        if _d.get("has") and str(_d.get("keyword") or "").strip():
            return str(_d["keyword"]).strip()
    except Exception:
        logger.debug(f"[DRAW] 角色名检测 JSON 解析失败: {str(out or '')[:80]}")
    return ""


async def _pixiv_redraw(bot, ev, prompt: str, keyword: str, mode: str = "img2img") -> str:
    """搜 P 站参考图 → 以图绘图（重绘保留角色面部/特征）。返回一句给人/模型的话。

    供两个入口共用：LLM 工具 draw_from_pixiv、以及 /画图 检测到角色名时自动改走这条。
    """
    if not keyword:
        return "没说搜什么参考图。"
    deny = _tool_ready()
    if deny:
        return deny
    try:
        from plugins.third.pixivrank_search.data_source import (
            search_pixiv_urls, download_pixiv_imgs,
        )
        _texts, _urls, _code = await search_pixiv_urls(keyword, 1, "popular", 1)
        if _code != 200 or not _urls:
            return "P 站没搜到合适的参考图，直接告诉对方搜不到。"
        # ⚠️ search_pixiv_urls 返回的 urls 是 list[list[str]]（每个作品一个 list，多页），
        #    download_pixiv_imgs 要的是扁平的 list[str]。插件自己用 urls[0]，这里同样取
        #    第一个非空作品的图，否则传进去的是 [[url]]，下载时对 list 调 rpartition 报错。
        _ref_urls = next((u for u in _urls if u), [])
        paths = await download_pixiv_imgs(_ref_urls[:1], str(ev.get_user_id()))
        if not paths:
            return "参考图下载失败，直接告诉对方没成功。"
        ref_path = os.path.abspath(paths[0])
    except Exception as e:
        logger.warning(f"[DRAW] 搜参考图失败: {e}")
        return "搜参考图失败，直接告诉对方没成功。"
    asyncio.create_task(_run_draw(bot, ev, mode, prompt, image_path=ref_path, notify=False))
    return f"开始绘图，稍等哦，几分钟后发图"


async def _tool_draw_from_pixiv(args: dict) -> str:
    prompt = str(args.get("prompt") or "").strip()
    keyword = str(args.get("keyword") or "").strip()
    # 默认「重绘」：用户要的是「保留角色面部+特征」的以图绘图，img2img 低 denoise 最合适
    mode = _MODE_ALIASES.get(str(args.get("mode") or "").strip(), "img2img")
    if mode not in ("lineart", "pose", "depth", "style", "img2img"):
        mode = "img2img"
    if not keyword:
        return "没说搜什么参考图，问清楚再画。"
    bot, ev = _tool_ctx()
    if not bot or not ev:
        return "现在画不了（缺会话上下文）。"
    return await _pixiv_redraw(bot, ev, prompt, keyword, mode)


async def _tool_pixiv_search(args: dict) -> str:
    keyword = str(args.get("keyword") or "").strip()
    if not keyword:
        return "没说要搜什么。"
    bot, ev = _tool_ctx()
    if not bot or not ev:
        return "现在搜不了（缺会话上下文）。"
    try:
        from plugins.third.pixivrank_search.data_source import (
            search_pixiv_urls, download_pixiv_imgs,
        )
        _texts, _urls, _code = await search_pixiv_urls(keyword, 1, "popular", 1)
        if _code != 200 or not _urls:
            return "P 站没搜到，直接告诉对方搜不到。"
        # ⚠️ _urls 是 list[list[str]]（每个作品一个 list，多页），
        #    download_pixiv_imgs 要扁平 list[str]，直接 _urls[:1] 会传 [[url]]，
        #    下载时对 list 调 rpartition 报错（2026-11-17 搜图失败根因）。
        _ref_urls = next((u for u in _urls if u), [])
        paths = await download_pixiv_imgs(_ref_urls[:1], str(ev.get_user_id()))
        if not paths:
            return "图片下载失败，直接告诉对方没成功。"
        try:
            await bot.send(ev, MessageSegment.image(file=f"file://{os.path.abspath(paths[0])}"))
        except Exception as e:
            if _is_send_timeout(e):
                logger.warning(f"[DRAW-TOOL] 搜图发送超时（图稍后到）: {type(e).__name__}")
            else:
                logger.error(f"[DRAW-TOOL] 搜图发送失败: {e}")
        return "已经搜到并发过去了"
    except Exception as e:
        logger.warning(f"[DRAW-TOOL] 搜图失败: {e}")
        return "搜图失败，直接告诉对方没成功。"


register_tool(
    {
        "type": "function",
        "function": {
            "name": "draw_image",
            "description": (
                "用算力机 SDXL 画一张图（文生图）。用户说「画/画图/画一张/画个/生成一张 X」"
                "时**直接调用**，不要去搜索 X 是什么。泛称描述（白裙女孩、樱花树下的少女）走这个；"
                "具体角色名走 draw_from_pixiv。prompt 写清楚主体/场景/画风，中文即可。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "要画的内容描述，例如：樱花树下的白裙女孩，长发，仰望天空"},
                },
                "required": ["prompt"],
            },
        },
    },
    _tool_draw_image,
    scope=("private", "group"),
)

register_tool(
    {
        "type": "function",
        "function": {
            "name": "draw_from_pixiv",
            "description": (
                "先去 P 站搜一张参考图，再以图绘图。用户说「画/画图/画一张 X」且 X 是**具体角色名**"
                "（如 芙宁娜、纳西妲、初音ミク、某画师）时用。**别先 [[SEARCH]] 查 X 是谁**，"
                "直接把 X 当 keyword 去 P 站搜参考图，再用「重绘」尽量保留角色面部和特征。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "希望画出的内容/风格描述，中文即可"},
                    "keyword": {"type": "string", "description": "P 站搜索关键词（角色名/画师/标签），例如 纳西妲、初音ミク"},
                    "mode": {
                        "type": "string",
                        "enum": ["重绘", "画风", "姿势", "线稿", "景深"],
                        "description": "以图绘图方式：重绘=重绘参考图、保留角色面部和特征(默认，最适合画角色)，画风=风格迁移，姿势=锁姿势，线稿=线稿上色，景深=锁景深",
                    },
                },
                "required": ["prompt", "keyword"],
            },
        },
    },
    _tool_draw_from_pixiv,
    scope=("private", "group"),
)

register_tool(
    {
        "type": "function",
        "function": {
            "name": "pixiv_search",
            "description": (
                "在 P 站搜图并发给用户看。用户想找某角色/某画风的图时用，搜到直接发一张。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "P 站搜索关键词（角色名/画师/标签）"},
                },
                "required": ["keyword"],
            },
        },
    },
    _tool_pixiv_search,
    scope=("private", "group"),
)
