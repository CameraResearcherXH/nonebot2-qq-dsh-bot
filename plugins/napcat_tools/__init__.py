# plugins/napcat_tools/__init__.py
"""
把 NapCat 的全部能力接给她（DeepSeek）当工具用。

只挂 2 个 meta 工具，60 多个 API 按需查说明 —— 常驻 token 从 ~5000 降到 ~200：
  · napcat_help(category?/tool?)  → 类别总表 / 某类工具名 / 某个工具的完整参数
  · napcat_call(tool, args)       → 真去调 NapCat

另外三个命令（都是人用的）：
  · /工具表 [类别|工具名]  —— 看她手上有哪些 API（仅主人）
  · /批准 <编号>           —— 主人点头，执行她刚才想做但没权限做的事（仅主人）
  · /拒绝 <编号>           —— 主人拒绝（仅主人）

权限分级在 services/napcat_tools.py 的 TOOLS 表里，加好友/踢人/上下管理这类是 owner。
"""
from nonebot import on_command, logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg

from services import llm as llm_mod
from services import napcat_tools as NT
from services.access import is_owner

# 群聊也挂：这两个工具很轻，真正贵的是「查说明」那一步，模型不查就不知道参数。
SCOPE = ("private", "group") if NT.IN_GROUP else ("private",)


# ============================================================
# 工具 1：看菜单 / 看说明
# ============================================================
async def _tool_help(args: dict) -> str:
    tool = str(args.get("tool") or "").strip()
    cat = str(args.get("category") or "").strip()
    # 👇 把当前会话的真实 id 传进去，示例里的 group_id/message_id 就是能直接跑的
    _ev = llm_mod.TOOL_CTX.get("event")
    _bot = llm_mod.TOOL_CTX.get("bot")
    ctx = {
        "group_id": str(getattr(_ev, "group_id", "") or ""),
        "user_id": str(getattr(_ev, "user_id", "") or ""),
        "message_id": str(getattr(_ev, "message_id", "") or ""),
        "self_id": str(getattr(_bot, "self_id", "") or getattr(_ev, "self_id", "") or ""),
    }
    if tool:
        return NT.help_text(tool, ctx=ctx)
    if cat:
        return NT.menu_text(cat)
    return NT.menu_text()


# ============================================================
# 工具 2：真的去调
# ============================================================
async def _tool_call(args: dict) -> str:
    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")
    if not bot or not ev:
        return "现在没法操作 QQ（缺少会话信息）"
    speaker = str(llm_mod.TOOL_CTX.get("speaker_qq")
                  or getattr(ev, "user_id", "") or "")
    ctx = {
        "self_id": str(getattr(bot, "self_id", "") or ""),
        "group_id": getattr(ev, "group_id", None),
        "user_id": getattr(ev, "user_id", None),
        "name": str(llm_mod.TOOL_CTX.get("speaker_name") or ""),
    }
    return await NT.execute(
        bot,
        str(args.get("tool") or ""),
        args.get("args"),
        requester=speaker,
        ctx=ctx,
    )


llm_mod.register_tool(
    {
        "type": "function",
        "function": {
            "name": "napcat_help",
            "description": (
                "查 QQ 操作工具的说明。不带参数=看所有类别；"
                "给 category=看这一类下有哪些工具；给 tool=看这个工具的完整参数。"
                "**用任何 QQ 操作之前，必须先看它的说明**，别瞎猜参数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "类别名：msg/group/group_admin/friend/request/interact/file/account/history",
                    },
                    "tool": {
                        "type": "string",
                        "description": "工具名（就是 OneBot API 名），例如 set_group_ban",
                    },
                },
                "required": [],
            },
        },
    },
    _tool_help,
    scope=SCOPE,
)

llm_mod.register_tool(
    {
        "type": "function",
        "function": {
            "name": "napcat_call",
            "description": (
                "真的执行一个 QQ 操作（发消息、撤回、改群名片、禁言、踢人、戳一戳、"
                "点赞、加好友、传文件、拉历史消息……）。"
                "参数必须先通过 napcat_help(tool=...) 查清楚。"
                "危险操作（踢人/禁言/上下管理/加好友/退群/改群名）会自动去问主人，"
                "返回「等主人回复」就老实说在等，别假装做成了。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool": {
                        "type": "string",
                        "description": "要调的 API 名，例如 send_group_msg / set_group_card",
                    },
                    "args": {
                        "type": "object",
                        "description": '参数字典，例如 {"group_id":123,"user_id":456,"card":"小明"}',
                    },
                },
                "required": ["tool"],
            },
        },
    },
    _tool_call,
    scope=SCOPE,
)


# ============================================================
# /工具表 —— 看她手上有什么（仅主人）
# ============================================================
tool_table_cmd = on_command("工具表", priority=3, block=True)


@tool_table_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(str(event.user_id)):
        await tool_table_cmd.finish("仅主人可用。")
    what = args.extract_plain_text().strip()
    if not what:
        await tool_table_cmd.finish(
            "🧰 " + NT.stats() + "\n\n"
            + NT.menu_text()
            + f"\n\n待批准：{len(NT.PENDING)} 个\n"
            f"群聊是否也挂工具：{'是' if NT.IN_GROUP else '否'}"
        )
    if what in NT.CATEGORIES:
        await tool_table_cmd.finish(NT.menu_text(what))
    await tool_table_cmd.finish(NT.help_text(what))


# ============================================================
# /批准 /拒绝 —— 主人点头
# ============================================================
approve_cmd = on_command("批准", priority=3, block=True)
reject_cmd = on_command("拒绝", priority=3, block=True)


async def _deliver(p: dict, text: str) -> bool:
    """把结果发回「她原本要操作的那个会话」。"""
    bot = p.get("bot")
    if not bot:
        return False
    try:
        if p.get("group_id"):
            await bot.send_group_msg(group_id=int(p["group_id"]), message=text)
        else:
            await bot.send_private_msg(user_id=int(p["user_id"]), message=text)
        return True
    except Exception as e:
        logger.warning(f"[NAPCAT] 回发结果失败: {type(e).__name__}: {e}")
        return False


@approve_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(str(event.user_id)):
        await approve_cmd.finish("仅主人可用。")
    pid = args.extract_plain_text().strip()
    if not pid:
        await approve_cmd.finish(f"用法：/批准 编号\n待批准：{len(NT.PENDING)} 个")
    p = NT.take_pending(pid)
    if not p:
        await approve_cmd.finish(f"没有编号 {pid} 的待批准请求（可能已过期或已处理）。")

    res = await NT.execute(
        p.get("bot") or bot, p["tool"], p["args"],
        requester=p.get("requester") or "", ctx=p, skip_level=True,
    )
    logger.info(f"[NAPCAT] 主人批准 {pid}：{p['tool']} → {str(res)[:100]}")

    ok = await _deliver(p, f"✅ 主人同意了：{res[:200]}")
    await approve_cmd.finish(
        f"✅ 已执行 {p['tool']}\n{res[:300]}"
        + ("" if ok else "\n⚠️ 但结果没发出去（会话可能没了）")
    )


@reject_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(str(event.user_id)):
        await reject_cmd.finish("仅主人可用。")
    pid = args.extract_plain_text().strip()
    p = NT.take_pending(pid)
    if not p:
        await reject_cmd.finish(f"没有编号 {pid} 的待批准请求。")
    await _deliver(p, f"❌ 主人没同意这个操作（{p['tool']}）。")
    await reject_cmd.finish(f"已拒绝 {p['tool']}（编号 {pid}）。")


logger.info(f"🧰 [NapCat 工具] 已注册 {len(NT.TOOLS)} 个 API，"
            f"按需查说明；群聊挂载：{NT.IN_GROUP}")
