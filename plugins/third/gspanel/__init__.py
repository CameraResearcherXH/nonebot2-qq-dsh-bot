import time
from pathlib import Path

from nonebot import get_driver
from nonebot.exception import FinishedException
from nonebot.log import logger
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.plugin import on_command
from nonebot.adapters.onebot.v11 import Bot
from nonebot.adapters.onebot.v11.event import MessageEvent
from nonebot.adapters.onebot.v11.message import MessageSegment

from services import llm as llm_mod
from services.llm import register_tool

from .data_updater import updateCache
from .data_source import getTeam, getPanel
from .__utils__ import (
    GSPANEL_ALIAS,
    LOCAL_DIR,
    aliasWho,
    uidHelper,
    uidUnbind,
    formatTeam,
    formatInput,
    fetchInitRes,
)


# ================= 发图 =================
# 2026-11-18 真机：面板已经渲染出来了，但 `MessageSegment.image(bytes)` 让 NapCat 走
# base64 上传，sendMsg 直接超时（retcode=1200 Timeout: NTEvent ...）。
# 改成先落盘再发 `file://`：NapCat 读本地文件快得多，也方便重试。
def _image_seg(data: bytes) -> MessageSegment:
    tmp_dir = Path(LOCAL_DIR) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # 顺手清掉 10 分钟前的临时图，别把磁盘塞满
    try:
        now = time.time()
        for f in tmp_dir.glob("panel_*.jpg"):
            if now - f.stat().st_mtime > 600:
                f.unlink(missing_ok=True)
    except Exception:
        pass
    out = tmp_dir / f"panel_{int(time.time() * 1000)}.jpg"
    out.write_bytes(data)
    return MessageSegment.image(f"file://{out.resolve()}")


async def _finish_image(matcher, data: bytes) -> None:
    try:
        seg = _image_seg(data)
    except Exception as e:  # noqa: BLE001
        logger.error(f"[gspanel] 图片落盘失败：{type(e).__name__}: {e}")
        await matcher.finish("面板图生成好了，但写临时文件失败，稍后再试。")
        return
    try:
        await matcher.finish(seg)
    except FinishedException:
        raise
    except Exception as e:  # noqa: BLE001
        txt = f"{type(e).__name__}: {e}"
        if "Timeout" in txt or "retcode=1200" in txt:
            # NapCat 上传超时，图通常几秒后照样到 —— 别再补一句「失败了」误导用户
            logger.info(f"[gspanel] 发图超时（图可能稍后到）：{txt[:120]}")
            return
        raise

driver = get_driver()
driver.on_startup(fetchInitRes)
driver.on_bot_connect(updateCache)

showPanel = on_command("panel", aliases=GSPANEL_ALIAS, priority=13, block=True)
showTeam = on_command("teamdmg", aliases={"队伍伤害"}, priority=13, block=True)

uidStart = ["1", "2", "5", "6", "7", "8", "9"]

# 2026-11-18：用户要一个独立的 /绑定，不用每次 /面板 绑定 xxx。
# 绑定关系存在 data/gspanel/qq-uid.json，/面板 不带 UID 时自动用它。
bindUid = on_command("绑定", aliases={"绑定uid", "bind"}, priority=13, block=True)
unbindUid = on_command("解绑", aliases={"解除绑定"}, priority=13, block=True)


@bindUid.handle()
async def bind_handle(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    qq = str(event.get_user_id())
    argsMsg = " ".join(seg.data["text"] for seg in arg["text"]).strip()
    opqq = event.message["at"][0].data["qq"] if event.message.get("at") else ""

    if not argsMsg:
        cur = await uidHelper(qq)
        await bindUid.finish(
            (f"你现在绑定的是 UID {cur}。\n" if cur else "你还没有绑定 UID。\n")
            + "用法：/绑定 你的9位UID\n"
            + "管理员：/绑定 @某人 9位UID",
            at_sender=True,
        )

    # 取 9 位数字（允许「绑定 uid 101859422」这种写法）
    uid = next(
        (s for s in reversed(argsMsg.split()) if s.isdigit() and len(s) == 9),
        "",
    )
    if not uid:
        await bindUid.finish("UID 要是 9 位数字，例如：/绑定 101859422", at_sender=True)
    opqq = opqq or qq
    if opqq != qq and qq not in bot.config.superusers:
        await bindUid.finish(f"没有权限操作 QQ{opqq} 的绑定状态！", at_sender=True)
    if uid[0] not in uidStart:
        await bindUid.finish(f"UID 是「{uid}」吗？好像不对劲呢..", at_sender=True)
    await bindUid.finish(await uidHelper(opqq, uid), at_sender=True)


@unbindUid.handle()
async def unbind_handle(event: MessageEvent):
    ok = await uidUnbind(str(event.get_user_id()))
    await unbindUid.finish(
        "已解除绑定。" if ok else "你本来就没有绑定 UID。", at_sender=True
    )


@showPanel.handle()
async def panel_handle(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    qq = str(event.get_user_id())
    argsMsg = " ".join(seg.data["text"] for seg in arg["text"])
    # 提取消息中的 at 作为操作目标 QQ
    opqq = event.message["at"][0].data["qq"] if event.message.get("at") else ""
    # 输入以「绑定」开头，识别为绑定操作
    if argsMsg.startswith("绑定"):
        args = [a for a in argsMsg[2:].split() if a.isdigit()]
        if len(args) == 1:
            uid, opqq = args[0], opqq or qq
        elif len(args) == 2:
            uid, opqq = args[0], (opqq or args[1])
        else:
            await showPanel.finish("绑定参数格式错误！", at_sender=True)
        if opqq != qq and qq not in bot.config.superusers:
            await showPanel.finish(f"没有权限操作 QQ{qq} 的绑定状态！", at_sender=True)
        elif uid[0] not in uidStart or len(uid) != 9:
            await showPanel.finish(f"UID 是「{uid}」吗？好像不对劲呢..", at_sender=True)
        await showPanel.finish(await uidHelper(opqq, uid))
    # 尝试从输入中理解 UID、角色名
    uid, char = await formatInput(argsMsg, qq, opqq)
    if not uid:
        await showPanel.finish("要查询角色面板的 UID 捏？", at_sender=True)
    elif not uid.isdigit() or uid[0] not in uidStart or len(uid) != 9:
        await showPanel.finish(f"UID 是「{uid}」吗？好像不对劲呢..", at_sender=True)
    logger.info(f"正在查找 UID{uid} 的「{char}」角色面板..")
    rt = await getPanel(uid, char)
    if isinstance(rt, str):
        await showPanel.finish(MessageSegment.text(rt))
    elif isinstance(rt, bytes):
        await _finish_image(showPanel, rt)


@showTeam.handle()
async def team_handle(bot: Bot, event: MessageEvent, arg: Message = CommandArg()):
    qq = str(event.get_user_id())
    argsMsg = " ".join(seg.data["text"] for seg in arg["text"])
    # 提取消息中的 at 作为操作目标 QQ
    opqq = event.message["at"][0].data["qq"] if event.message.get("at") else ""
    # 是否展示伤害过程，默认不显示
    showDetail, keywords = False, ["详情", "过程", "全部", "全图"]
    if any(argsMsg.startswith(word) for word in keywords):
        showDetail = True
        for word in keywords:
            argsMsg = argsMsg.lstrip(word).strip()
    # 尝试从输入中理解 UID、角色名
    uid, chars = await formatTeam(argsMsg, qq, opqq)
    if not uid:
        await showTeam.finish("要查询队伍伤害的 UID 捏？", at_sender=True)
    elif not uid.isdigit() or uid[0] not in uidStart or len(uid) != 9:
        await showTeam.finish(f"UID 是「{uid}」吗？好像不对劲呢..", at_sender=True)
    if not chars:
        logger.info(f"QQ{qq} 的输入「{argsMsg}」似乎未指定队伍角色！")
    logger.info(f"正在查找 UID{uid} 的「{'/'.join(chars) or '展柜前 4 角色'}」队伍伤害面板..")
    rt = await getTeam(uid, chars, showDetail)
    if isinstance(rt, str):
        await showTeam.finish(MessageSegment.text(rt))
    elif isinstance(rt, bytes):
        await _finish_image(showTeam, rt)


# ================= 给 LLM 的 function 工具 =================
# 用户 2026-11-18：把原神的绑定/面板/队伍伤害也做成工具，
# 用户直接说「帮我绑一下原神 UID 101859422」「查下我刻晴面板」就行，不用背 /指令。
# 出图不自己发：生成后返回文件路径，由她调 send(image=...) 决定发不发。

def _save_tool_img(data: bytes) -> str:
    tmp_dir = Path(LOCAL_DIR) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        now = time.time()
        for f in tmp_dir.glob("tool_*.jpg"):
            if now - f.stat().st_mtime > 600:
                f.unlink(missing_ok=True)
    except Exception:
        pass
    out = tmp_dir / f"tool_{int(time.time() * 1000)}.jpg"
    out.write_bytes(data)
    return str(out.resolve())


def _tool_event():
    return llm_mod.TOOL_CTX.get("event")


def _tool_qq() -> str:
    ev = _tool_event()
    return str(getattr(ev, "user_id", "") or "")


async def _tool_genshin_bind(args: dict) -> str:
    uid = str(args.get("uid") or "").strip()
    qq = _tool_qq()
    if not qq:
        return "现在没法绑定（缺会话信息）"
    if not (uid.isdigit() and len(uid) == 9 and uid[0] in uidStart):
        return "UID 要是 9 位数字，例如 101859422"
    return await uidHelper(qq, uid)


async def _tool_genshin_unbind(args: dict) -> str:
    qq = _tool_qq()
    if not qq:
        return "现在没法解绑（缺会话信息）"
    ok = await uidUnbind(qq)
    return "已解除绑定。" if ok else "本来就没有绑定 UID。"


async def _tool_genshin_panel(args: dict) -> str:
    qq = _tool_qq()
    if not qq:
        return "现在没法查面板（缺会话信息）"
    uid = str(args.get("uid") or "").strip() or await uidHelper(qq)
    if not uid:
        return "还没绑定 UID：先调 genshin_bind(uid=9位UID)，或让用户发 /绑定 UID"
    char = str(args.get("char") or "").strip() or "全部"
    char = await aliasWho(char)
    rt = await getPanel(uid, char)
    if isinstance(rt, str):
        return rt
    path = _save_tool_img(rt)
    return (
        f"面板图已生成（UID{uid}｜{char}）：{path}\n"
        f"要发就调 send(image=\"{path}\")；不想发就只用文字说，别假装发过。"
    )


async def _tool_genshin_team(args: dict) -> str:
    qq = _tool_qq()
    if not qq:
        return "现在没法查队伍伤害（缺会话信息）"
    uid = str(args.get("uid") or "").strip() or await uidHelper(qq)
    if not uid:
        return "还没绑定 UID：先调 genshin_bind(uid=9位UID)，或让用户发 /绑定 UID"
    raw = str(args.get("chars") or "").replace("，", " ").replace(",", " ")
    chars = [c.strip() for c in raw.split() if c.strip()]
    rt = await getTeam(uid, chars, bool(args.get("detail")))
    if isinstance(rt, str):
        return rt
    path = _save_tool_img(rt)
    return (
        f"队伍伤害图已生成（UID{uid}）：{path}\n"
        f"要发就调 send(image=\"{path}\")；不想发就只用文字说，别假装发过。"
    )


register_tool(
    {
        "type": "function",
        "function": {
            "name": "genshin_bind",
            "description": (
                "绑定用户的《原神》UID。用户说「绑定原神 UID xxx」「我 UID 是 xxx」"
                "「以后用这个 UID 查面板」时用。绑定后 /面板、genshin_panel 不用再带 UID。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "9 位原神 UID，例如 101859422"},
                },
                "required": ["uid"],
            },
        },
    },
    _tool_genshin_bind,
    scope=("private", "group"),
)

register_tool(
    {
        "type": "function",
        "function": {
            "name": "genshin_unbind",
            "description": "解除当前用户的《原神》UID 绑定。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    _tool_genshin_unbind,
    scope=("private", "group"),
)

register_tool(
    {
        "type": "function",
        "function": {
            "name": "genshin_panel",
            "description": (
                "查《原神》角色面板（出图）。用户说「查下我的刻晴」「我的原神面板」"
                "「看看纳西妲面板」时用。不传 uid 就用他绑定的 UID；char 不传=展柜全部角色。"
                "图不会自动发，结果里会给文件路径，要发再调 send(image=路径)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "可选，9 位 UID；省略=用绑定 UID"},
                    "char": {"type": "string", "description": "角色名，如 刻晴 / 纳西妲；省略=全部"},
                },
                "required": [],
            },
        },
    },
    _tool_genshin_panel,
    scope=("private", "group"),
)

register_tool(
    {
        "type": "function",
        "function": {
            "name": "genshin_team",
            "description": (
                "查《原神》队伍伤害（出图）。用户说「队伍伤害」「这四个角色打多少」时用。"
                "chars 可传角色名（空格分隔），省略=展柜前 4 个；detail=true 显示伤害过程。"
                "图不会自动发，结果里会给文件路径，要发再调 send(image=路径)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "可选，9 位 UID；省略=用绑定 UID"},
                    "chars": {"type": "string", "description": "角色名，空格分隔，如「纳西妲 芙宁娜」"},
                    "detail": {"type": "boolean", "description": "是否显示伤害过程，默认 false"},
                },
                "required": [],
            },
        },
    },
    _tool_genshin_team,
    scope=("private", "group"),
)
