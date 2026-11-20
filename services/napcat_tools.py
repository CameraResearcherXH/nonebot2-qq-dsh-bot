# services/napcat_tools.py
"""
把 NapCat / OneBot v11 的 API 全量接成「她能自己调用的工具」，并且**按需给说明**。

设计（用户 2026-11-11 的要求）：
  1. 工具分类 —— 每个 API 归到一个类别（消息 / 群信息 / 群管理 / 好友 / 请求 / 互动 / 文件 / 账号 / 历史）
  2. 类别里有工具名 —— 菜单只给「类别 → 工具名」，不给参数
  3. 用之前必须先给一份说明 —— napcat_help(tool=...) 才吐参数表
  4. 按需调用 —— 只挂 2 个 meta 工具（help / call），60 多个 API 不占常驻 token
  5. 分级权限 —— safe 谁都能用；admin 要管理员/主人；owner 要主人点头（会私聊问主人）

为什么不把 60 个 API 直接挂成 60 个 function：
  · 每个 spec 约 60~100 token，全挂等于每轮白烧 5000 token，而且模型容易乱挑
  · 分组 + 按需看说明，token 从 5000 降到 ~200，还更不容易调错

⚠️ 权限分级的红线（动了会出事，别改成 safe）：
  · 加好友 / 删好友 / 通过入群申请 —— owner
  · 踢人 / 上下管理 / 全员禁言 / 改群名 / 解散群 —— owner
  · 改 QQ 昵称头像 / 在线状态 / 重启 —— owner
  · 禁言单人 / 群公告 / 头衔 / 精华 / 传文件 —— admin
"""
from core import tuning
import json
import os
import random
import re
import time

try:  # 允许脱离 nonebot 单独跑脚本/自检
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

from . import access

SAFE = "safe"       # 谁都能用（查询、发消息、戳一戳、改自己名片）
ADMIN = "admin"     # 要管理员或主人
OWNER = "owner"     # 要主人点头（会私聊问主人）

LEVEL_LABEL = {
    SAFE: "随便用",
    ADMIN: "要管理员/主人",
    OWNER: "要主人点头",
}

ENABLED = os.getenv("NAPCAT_TOOLS", "true").lower() in ("1", "true", "yes", "on")
IN_GROUP = os.getenv("NAPCAT_TOOLS_IN_GROUP", "true").lower() in ("1", "true", "yes", "on")
# 常驻提示要不要带「类别速查」。默认不带（折叠成目录，按需 napcat_help 捞）
HINT_FULL = os.getenv("NAPCAT_HINT_FULL", "false").lower() in ("1", "true", "yes", "on")
MAX_CALLS_PER_MIN = tuning.i("NAPCAT_TOOL_MAX_PER_MIN")
APPROVE_TTL = tuning.i("NAPCAT_APPROVE_TTL")     # 待批准请求有效期（秒）
RESULT_MAX = tuning.i("NAPCAT_TOOL_RESULT_MAX")  # 回给模型的结果上限
# 拉历史消息的硬上限（条）。她写 100 也只给 20，免得一次把上下文拉爆。
HISTORY_MAX = tuning.i("HISTORY_MAX")

# ============================================================
# 群 / 人白名单（安全边界）
# ------------------------------------------------------------
# ⚠️ 2026-11-20 审计：以前 SAFE 级直接 return True，execute() 也不校验 group_id，
#    群里任何人说一句「去 123456 群发 xx」「私聊 999 骂他」，
#    她就会用**自己的号**去执行 —— 骚扰、封号都是这么来的。
#    现在：任何带 group_id 的调用都必须落在 ALLOWED_GROUPS；
#    任何「发给某人」的调用都必须落在 ALLOWED_PRIVATE 或主人。
#    这一层在权限分级之前，SAFE 也不例外。
# ============================================================
ALLOWED_GROUPS = set(
    g.strip() for g in os.getenv("ALLOWED_GROUPS", "").split(",") if g.strip()
)
ALLOWED_PRIVATE = set(
    g.strip() for g in os.getenv("ALLOWED_PRIVATE", "").split(",") if g.strip()
)

# 这些工具的 user_id 是「发给谁 / 查谁」，必须是人白名单
_PRIVATE_TARGET_TOOLS = {
    "send_private_msg", "send_private_forward_msg", "upload_private_file",
    "get_friend_msg_history", "friend_poke", "set_friend_add_request",
    "delete_friend", "get_private_file_url", "set_friend_remark",
}


def _whitelist_check(tool: str, clean: dict) -> str:
    """返回空串 = 通过；非空 = 拒绝原因（给模型看）。"""
    gid = str(clean.get("group_id") or "").strip()
    if gid and gid not in ALLOWED_GROUPS:
        logger.warning(f"[NAPCAT] 拒绝 {tool}：群 {gid} 不在 ALLOWED_GROUPS")
        return f"群 {gid} 不在允许列表里，这个操作我不能做。"

    uid = str(clean.get("user_id") or "").strip()
    if tool in _PRIVATE_TARGET_TOOLS and uid:
        if uid not in ALLOWED_PRIVATE and uid != _owner_qq():
            logger.warning(f"[NAPCAT] 拒绝 {tool}：{uid} 不在 ALLOWED_PRIVATE")
            return f"{uid} 不在允许私聊的名单里，这个操作我不能做。"

    # 兜底：raw 工具把 group_id / user_id 藏在 params 里
    if tool == "call_any_api":
        params = clean.get("params") or {}
        if isinstance(params, dict):
            _g = str(params.get("group_id") or "").strip()
            if _g and _g not in ALLOWED_GROUPS:
                logger.warning(f"[NAPCAT] 拒绝 call_any_api：群 {_g} 不在白名单")
                return f"群 {_g} 不在允许列表里，这个操作我不能做。"
    return ""


# ============================================================
# 类别
# ============================================================
CATEGORIES: dict = {
    "msg": ("💬 消息", "发消息 / 撤回 / 查消息 / 合并转发 / 表情回应 / 取图取语音"),
    "group": ("👥 群信息", "群列表 / 群资料 / 成员列表 / 成员资料 / 荣誉 / @全体剩余次数"),
    "group_admin": ("🛡️ 群管理", "禁言 / 踢人 / 上下管理 / 改群名 / 改名片 / 头衔 / 全员禁言 / 退群 / 群公告 / 精华"),
    "friend": ("🤝 好友", "好友列表 / 加好友审批 / 删好友 / 备注 / 陌生人资料"),
    "request": ("📨 请求", "入群申请与邀请的审批 / 群系统消息"),
    "interact": ("👉 互动", "戳一戳 / 名片赞 / 群签到"),
    "file": ("📁 文件", "群文件与私聊文件：上传 / 下载链接 / 删除 / 文件夹"),
    "account": ("⚙️ 账号", "登录信息 / 运行状态 / 昵称头像 / 在线状态 / cookie"),
    "history": ("🕘 历史", "拉群或好友的历史消息 / 最近联系人"),
    "raw": ("🧪 原始", "直接调任意 OneBot API（主人专用，兜底）"),
}


def _t(cat, desc, params=None, level=SAFE, note="", ret=""):
    return {"cat": cat, "desc": desc, "params": params or {},
            "level": level, "note": note, "ret": ret}


# ============================================================
# 工具表（key = OneBot action，调用时原样发给 NapCat）
# params: 名字 -> (类型, 是否必填, 说明)
#   类型：int / str / bool / array / any
# ============================================================
TOOLS: dict = {
    # ---------------- 消息 ----------------
    "send_group_msg": _t(
        "msg", "往群里发一条消息（可以带 @ 和图片段）",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "message": ("any", True, "消息内容。纯文本直接写；要 @ 人写 [{type:at,data:{qq:123}}]；图文混排用数组"),
         "auto_escape": ("bool", False, "true 时 message 按纯文本处理，不当 CQ 码解析")},
        note="别把你在正文里已经说过的话再发一遍，会重复。分条发才用这个。",
        ret="message_id"),
    "send_private_msg": _t(
        "msg", "给某人发私聊消息",
        {"user_id": ("int", True, "对方 QQ"),
         "message": ("any", True, "消息内容"),
         "auto_escape": ("bool", False, "按纯文本处理")},
        note="给当前会话外的人发消息要谨慎，别主动骚扰别人。",
        ret="message_id"),
    "delete_msg": _t(
        "msg", "撤回一条消息",
        {"message_id": ("int", True, "消息 ID")},
        note="只能撤回 2 分钟内的（管理员/群主可撤更早的）。她自己的消息撤回是安全的。"),
    "get_msg": _t(
        "msg", "按 ID 查一条消息的完整内容（含发送者、时间）",
        {"message_id": ("int", True, "消息 ID")}),
    "get_forward_msg": _t(
        "msg", "展开一条合并转发消息，看里面每一层的内容",
        {"message_id": ("str", True, "合并转发消息的 ID（或 id）")}),
    "send_group_forward_msg": _t(
        "msg", "发一条合并转发（聊天记录卡片）到群里",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "messages": ("array", True,
                      '节点数组，例：[{"type":"node","data":{"name":"小明","uin":"123","content":"你好"}}]，最多 20 条')},
        note="节点里的 uin 必须是真实 QQ 号，content 可以是字符串。"),
    "send_private_forward_msg": _t(
        "msg", "发一条合并转发给某人",
        {"user_id": ("int", True, "对方 QQ"),
         "messages": ("array", True, "节点数组，同 send_group_forward_msg")}),
    "get_image": _t(
        "msg", "拿图片文件（返回本地路径或 URL）",
        {"file": ("str", True, "图片文件名/ID")}),
    "get_record": _t(
        "msg", "拿语音文件（返回本地路径）",
        {"file": ("str", True, "语音文件名/ID"),
         "out_format": ("str", False, "转成什么格式，默认 mp3")}),
    "get_file": _t(
        "msg", "拿群/私聊文件的下载地址和本地路径",
        {"file_id": ("str", True, "文件 ID")}),
    "set_msg_emoji_like": _t(
        "msg", "给某条消息贴一个表情回应（就是 QQ 的小黄脸回应）",
        {"message_id": ("int", True, "消息 ID"),
         "emoji_id": ("str", True, "表情 ID，常用：128077 赞 / 128514 笑 / 128064 看 / 128068 惊"),
         "set": ("bool", False, "true=贴上，false=取消，默认 true")}),

    # ---------------- 群信息 ----------------
    "get_group_list": _t("group", "她加了哪些群（群号+群名）"),
    "get_group_info": _t(
        "group", "看一个群的资料（群名/人数/群主/公告）",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "no_cache": ("bool", False, "true=不吃缓存，强制拉最新的")}),
    "get_group_member_info": _t(
        "group", "看某个群成员的资料（群名片/昵称/角色/入群时间）",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "user_id": ("any", True, "QQ 号，也可以写名字（会去名册里查）"),
         "no_cache": ("bool", False, "不吃缓存")}),
    "get_group_member_list": _t(
        "group", "拉整个群的成员名单",
        {"group_id": ("int", False, "群号，不填=当前群")},
        note="人多时很长，返回会自动压成「名字(QQ)」列表。想找人用 [[WHO|名字]] 更快。"),
    "get_group_honor_info": _t(
        "group", "看群荣誉（龙王/群聊之火/活跃榜）",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "type": ("str", False, "all / talkative / performer / legend / strong_newbie / emotion")}),
    "get_group_at_all_remain": _t(
        "group", "查 @全体成员 还剩几次",
        {"group_id": ("int", False, "群号，不填=当前群")}),

    # ---------------- 群管理 ----------------
    "set_group_card": _t(
        "group_admin", "改群名片（她自己的，或者别人的）",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "user_id": ("any", False, "改谁，QQ 或名字；不填=改她自己的名片"),
         "card": ("str", True, "新名片，留空字符串 = 清空名片")},
        note="改别人的名片容易招人烦，除非对方明确要求。"),
    "set_group_ban": _t(
        "group_admin", "禁言某个群成员",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "user_id": ("any", True, "禁谁，QQ 或名字"),
         "duration": ("int", False, "禁言秒数，默认 600；0 = 解除禁言")},
        level=ADMIN,
        note="她得自己是管理员才禁得动，否则 NapCat 会报权限不足。别拿这个玩。"),
    "set_group_whole_ban": _t(
        "group_admin", "全员禁言 开/关",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "enable": ("bool", True, "true=开启全员禁言，false=解除")},
        level=OWNER, note="影响整个群，必须主人同意。"),
    "set_group_kick": _t(
        "group_admin", "把某人踢出群",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "user_id": ("any", True, "踢谁，QQ 或名字"),
         "reject_add_request": ("bool", False, "true=同时拒绝他再加群")},
        level=OWNER, note="不可撤销，必须主人同意。"),
    "set_group_admin": _t(
        "group_admin", "设置/取消群管理员",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "user_id": ("any", True, "谁，QQ 或名字"),
         "enable": ("bool", True, "true=设为管理员，false=取消")},
        level=OWNER, note="她得是群主才动得了。必须主人同意。"),
    "set_group_name": _t(
        "group_admin", "改群名",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "group_name": ("str", True, "新群名")},
        level=OWNER),
    "set_group_leave": _t(
        "group_admin", "退群；她是群主时等于解散群",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "is_dismiss": ("bool", False, "群主退群时 true=解散该群")},
        level=OWNER, note="退了就回不去了，必须主人同意。"),
    "set_group_special_title": _t(
        "group_admin", "给群成员设专属头衔",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "user_id": ("any", True, "谁，QQ 或名字"),
         "special_title": ("str", True, "头衔文字，留空=取消")},
        level=ADMIN),
    "set_group_anonymous": _t(
        "group_admin", "开/关群匿名聊天",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "enable": ("bool", True, "true=开匿名")},
        level=OWNER),
    "set_group_remark": _t(
        "group_admin", "给她自己的群备注改名（只有她自己看得到）",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "remark": ("str", True, "备注名")}),
    "send_group_notice": _t(
        "group_admin", "发群公告",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "content": ("str", True, "公告正文"),
         "image": ("str", False, "配图（本地路径或 URL）"),
         "pinned": ("bool", False, "是否置顶"),
         "type": ("int", False, "1=普通 2=确认（默认 1）")},
        level=ADMIN, note="公告全群可见，别乱发。"),
    "get_group_notice": _t(
        "group_admin", "看群公告列表",
        {"group_id": ("int", False, "群号，不填=当前群")}),
    "get_essence_msg_list": _t(
        "group_admin", "看群精华消息列表",
        {"group_id": ("int", False, "群号，不填=当前群")}),
    "set_essence_msg": _t(
        "group_admin", "把一条消息设为群精华",
        {"message_id": ("int", True, "消息 ID")},
        level=ADMIN),
    "delete_essence_msg": _t(
        "group_admin", "把一条消息从群精华里移除",
        {"message_id": ("int", True, "消息 ID")},
        level=ADMIN),

    # ---------------- 好友 ----------------
    "get_friend_list": _t("friend", "她的好友列表"),
    "get_stranger_info": _t(
        "friend", "看一个陌生人的资料（昵称/性别/年龄）",
        {"user_id": ("int", True, "QQ 号"),
         "no_cache": ("bool", False, "不吃缓存")}),
    "set_friend_remark": _t(
        "friend", "改好友备注",
        {"user_id": ("int", True, "QQ 号"),
         "remark": ("str", True, "备注名")}),
    "set_friend_add_request": _t(
        "friend", "同意/拒绝别人的加好友申请",
        {"flag": ("str", True, "申请标记（从好友申请列表里拿）"),
         "approve": ("bool", True, "true=同意，false=拒绝"),
         "remark": ("str", False, "同意时给的备注名")},
        level=OWNER, note="加陌生人进好友 = 隐私风险，必须主人同意。"),
    "delete_friend": _t(
        "friend", "删好友",
        {"user_id": ("int", True, "QQ 号")},
        level=OWNER, note="删了就找不回来了，必须主人同意。"),

    # ---------------- 请求 ----------------
    "set_group_add_request": _t(
        "request", "同意/拒绝入群申请或群邀请",
        {"flag": ("str", True, "请求标记"),
         "sub_type": ("str", True, "add=别人申请进群，invite=别人邀请她进群"),
         "approve": ("bool", True, "true=同意"),
         "reason": ("str", False, "拒绝理由")},
        level=OWNER, note="放人进群 = 安全边界，必须主人同意。"),
    "get_group_system_msg": _t("request", "看未处理的群申请/邀请列表"),
    "get_group_ignore_add_request": _t(
        "request", "看被忽略的入群申请",
        {"group_id": ("int", False, "群号，不填=当前群")}),

    # ---------------- 互动 ----------------
    "friend_poke": _t(
        "interact", "戳一戳某人（私聊）",
        {"user_id": ("int", True, "戳谁")},
        note="别连戳，一次就够。"),
    "group_poke": _t(
        "interact", "戳一戳群里的某人",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "user_id": ("any", True, "戳谁，QQ 或名字")},
        note="别连戳，一次就够。"),
    "send_like": _t(
        "interact", "给某人名片点赞",
        {"user_id": ("int", True, "给谁点赞"),
         "times": ("int", False, "点几个，默认 1（上限 20）")}),
    "send_group_sign": _t(
        "interact", "群签到",
        {"group_id": ("int", False, "群号，不填=当前群")}),

    # ---------------- 文件 ----------------
    "upload_group_file": _t(
        "file", "往群里传文件",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "file": ("str", True, "文件路径（必须是机器人所在机器/容器里能读到的路径）"),
         "name": ("str", False, "显示的文件名"),
         "folder": ("str", False, "传进哪个群文件夹（文件夹 ID）")},
        level=ADMIN),
    "upload_private_file": _t(
        "file", "给某人发文件",
        {"user_id": ("int", True, "对方 QQ"),
         "file": ("str", True, "文件路径"),
         "name": ("str", False, "显示的文件名")},
        level=ADMIN),
    "delete_group_file": _t(
        "file", "删群文件",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "file_id": ("str", True, "文件 ID"),
         "busid": ("int", False, "文件 busid，默认 102")},
        level=ADMIN),
    "get_group_file_url": _t(
        "file", "拿群文件的下载链接",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "file_id": ("str", True, "文件 ID"),
         "busid": ("int", False, "文件 busid，默认 102")}),
    "get_group_root_files": _t(
        "file", "看群文件根目录",
        {"group_id": ("int", False, "群号，不填=当前群")}),
    "get_group_files_by_folder": _t(
        "file", "看群某个文件夹里的文件",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "folder_id": ("str", True, "文件夹 ID")}),
    "create_group_file_folder": _t(
        "file", "在群文件里建文件夹",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "name": ("str", True, "文件夹名"),
         "parent_id": ("str", False, "父文件夹 ID，不填=根目录")},
        level=ADMIN),
    "get_private_file_url": _t(
        "file", "拿私聊文件的下载链接",
        {"user_id": ("int", True, "对方 QQ"),
         "file_id": ("str", True, "文件 ID")}),

    # ---------------- 账号 ----------------
    "get_login_info": _t("account", "她自己的 QQ 号和昵称"),
    "get_version_info": _t("account", "NapCat 版本信息"),
    "get_status": _t("account", "NapCat 运行状态（在线/正常）"),
    "set_qq_profile": _t(
        "account", "改她自己的昵称/签名等资料",
        {"nickname": ("str", False, "昵称"),
         "personal_note": ("str", False, "个性签名"),
         "sex": ("str", False, "male/female/unknown")},
        level=OWNER),
    "set_qq_avatar": _t(
        "account", "换她自己的头像",
        {"file": ("str", True, "图片路径或 URL")},
        level=OWNER),
    "set_online_status": _t(
        "account", "改她的在线状态（在线/离开/隐身…）",
        {"status": ("int", True, "11=我在线上 31=离开 41=隐身 50=忙碌 60=Q我吧 70=请勿打扰"),
         "ext_status": ("int", False, "扩展状态，默认 0"),
         "battery_status": ("int", False, "电量状态，默认 0")},
        level=OWNER),
    "get_cookies": _t(
        "account", "拿 QQ 的 cookie（敏感）",
        {"domain": ("str", False, "域名，默认 qun.qq.com")},
        level=ADMIN, note="这是登录凭据，别往聊天里贴。"),
    "get_csrf_token": _t("account", "拿 csrf token（敏感）", level=ADMIN),
    "get_credentials": _t(
        "account", "拿登录凭据（最敏感）",
        {"domain": ("str", False, "域名")},
        level=OWNER, note="绝对不要把结果发到聊天里。"),
    "can_send_image": _t("account", "现在能不能发图"),
    "can_send_record": _t("account", "现在能不能发语音"),

    # ---------------- 历史 ----------------
    "get_group_msg_history": _t(
        "history", "拉群的历史消息（比 [[HISTORY]] 更可控，能指定条数）",
        {"group_id": ("int", False, "群号，不填=当前群"),
         "message_seq": ("int", False, "从哪条开始往前拉，不填=最新"),
         "count": ("int", False, "拉多少条，默认 10，上限 20（写多了也只给 20）")},
        note="往回翻前情用它；只看最近前情用 [[HISTORY|20]] 更快（走本地记录）。"),
    "get_friend_msg_history": _t(
        "history", "拉和某人的私聊历史",
        {"user_id": ("int", True, "对方 QQ"),
         "message_seq": ("int", False, "从哪条开始"),
         "count": ("int", False, "拉多少条，默认 10，上限 20")}),
    "get_recent_contact": _t("history", "最近联系人列表"),

    # ---------------- 原始 ----------------
    "call_any_api": _t(
        "raw", "直接调任意 OneBot/NapCat API（兜底，主人专用）",
        {"action": ("str", True, "API 名，例如 get_group_info"),
         "params": ("any", False, "参数字典")},
        level=OWNER,
        note="表里没有的 API 用它。别用它绕过别的工具的权限检查。"),
}


# ============================================================
# 菜单 / 说明
# ============================================================
def _by_cat(cat: str) -> list:
    return [n for n, t in TOOLS.items() if t["cat"] == cat]


def menu_text(category: str = "") -> str:
    """无参 → 类别总表；给类别 → 该类别下每个工具名 + 一句话。"""
    cat = str(category or "").strip().lower()
    if cat:
        if cat not in CATEGORIES:
            keys = "、".join(CATEGORIES)
            return f"没有「{cat}」这个类别。有的是：{keys}"
        title, _ = CATEGORIES[cat]
        names = _by_cat(cat)
        lines = [f"{title}（{cat}）共 {len(names)} 个："]
        for n in names:
            t = TOOLS[n]
            lines.append(f"· {n} — {t['desc']}［{LEVEL_LABEL[t['level']]}］")
        lines.append('看某一个的完整参数：napcat_help(tool="工具名")')
        return "\n".join(lines)

    lines = [f"🧰 NapCat 工具总表（{len(CATEGORIES)} 类 / {len(TOOLS)} 个）"]
    for c, (title, desc) in CATEGORIES.items():
        names = _by_cat(c)
        if not names:
            continue
        shown = "、".join(names[:8]) + ("…" if len(names) > 8 else "")
        lines.append(f"· {title}（{c}）{len(names)} 个：{shown}")
    lines.append('')
    lines.append('下一步：napcat_help(category="group_admin") 看这一类，'
                 'napcat_help(tool="set_group_ban") 看某一个的完整参数。')
    return "\n".join(lines)


def help_text(tool: str, ctx: dict | None = None) -> str:
    """单个工具的完整说明 + 参数表 + 调用示例。

    ctx 有值时，示例里的 group_id / message_id 用**真实**的当前群号/消息号 ——
    以前一律写 123456，她照抄过来就是一次注定失败的调用。
    """
    name = str(tool or "").strip()
    if not name:
        return menu_text()
    if name not in TOOLS:
        hits = [n for n in TOOLS if name.lower() in n.lower()][:5]
        if hits:
            return f"没有「{name}」。你是不是想找：{'、'.join(hits)}"
        return f"没有「{name}」这个工具。先 napcat_help() 看类别。"

    t = TOOLS[name]
    lines = [f"🔧 {name} — {t['desc']}",
             f"【类别】{CATEGORIES[t['cat']][0]}（{t['cat']}）",
             f"【权限】{LEVEL_LABEL[t['level']]}"]
    if t["params"]:
        lines.append("【参数】")
        for p, (kind, req, desc) in t["params"].items():
            lines.append(f"· {p}（{kind}{'，必填' if req else '，可选'}）{desc}")
    else:
        lines.append("【参数】无")
    if t["ret"]:
        lines.append(f"【返回】{t['ret']}")
    if t["note"]:
        lines.append(f"【注意】{t['note']}")

    sample = {}
    for p, (kind, req, _d) in t["params"].items():
        if not req:
            continue
        sample[p] = _sample(p, kind, ctx)
    lines.append(
        f'【调用】napcat_call(tool="{name}", args={json.dumps(sample, ensure_ascii=False)})'
    )
    return "\n".join(lines)


def _sample(param: str, kind: str, ctx: dict | None = None):
    """给「调用示例」造一个像样的值，别全是「…」——那样她照抄就废了。

    ctx 里有真实 id 就用真实的（照抄也能跑通），没有才退回占位数字。
    """
    if param in ("group_id", "user_id", "message_id"):
        _real = str((ctx or {}).get(param) or "").strip()
        if _real.isdigit() and _real != "0":
            return int(_real)
        return 123456
    if param in ("message", "content"):
        return "你好呀"
    if param == "card":
        return "小明"
    if param == "duration":
        return 60
    if param == "action":
        return "get_group_info"
    if param == "params":
        return {"group_id": 123456}
    if kind == "int":
        return 123456
    if kind == "bool":
        return True
    if kind == "array":
        return []
    return "…"


# 高频动作：参数极简、群里天天用。
# 用户 2026-11-12：「让她给群友点赞却没点」—— 根因是折叠太狠：
# 她得 napcat_help() → napcat_help(category) → napcat_help(tool) 查三次才知道
# send_like 怎么调，干脆不做了。这几个**直接给全参数**，其余才走目录。
# 加新条目请只挑「参数 ≤2 个 + 群聊常用」的，别把这里又变成目录。
COMMON = [
    # user_id 一律用**名字**举例：她看到 789 会以为必须给 QQ 号，
    # 于是卡在「我不知道 rera 的 QQ」这一步不动手（2026-11-12 真机踩过）。
    ("send_like", '{"user_id":"小明"}', "给谁名片点赞"),
    ("group_poke", '{"user_id":"小明"}', "戳一戳谁"),
    ("set_group_card", '{"user_id":"小明","card":"新名片"}', "改群名片"),
    ("set_group_ban", '{"user_id":"小明","duration":60}', "禁言某人（要管理员）"),
    ("delete_msg", '{"message_id":123}', "撤回一条"),
    ("get_group_member_list", '{}', "看群成员名单"),
]


def menu_hint(is_group: bool) -> str:
    """
    塞进 system 的那段「你有这些能力」提示。

    用户 2026-11-11：「工具集我觉得可以像目录一样折叠，按需使用。按需捞说明和工具使用。」
    → 常驻这里**只留一个目录入口**，不列类别、更不列参数；想看什么自己 napcat_help 去捞。
      常驻字数从 ~700 降到 ~200，代价是多一次工具调用（只在真要用的时候才付）。
      想切回「类别速查版」：NAPCAT_HINT_FULL=true。
    """
    head = (
        "【你能真的操作 QQ · 写标记就行】\n"
        "不用调函数。在回复里写标记，系统替你去执行，再把结果给你（和 [[HISTORY]] 一个用法）：\n"
        "· `[[[LIKE|老大]]]` → 点 1 次名片赞；**`[[[LIKE|老大|10]]]` → 点 10 次**"
        "（群友说「赞10次」就用后面那个数字）\n"
        "· `[[[POKE|老大]]]` → 戳「老大」一下\n"
        '· `[[[NAPCAT|set_group_card|{"user_id":"老大","card":"新卡"}]]]` → 改群名片\n'
        '· `[[[NAPCAT|set_group_ban|{"user_id":"老大","duration":60}]]]` → 禁言（要管理员）\n'
        '· `[[[NAPCAT|delete_msg|{"message_id":123}]]]` → 撤回一条\n'
        "（名字直接写，系统去名册查 QQ；**正文里 `@某人(123456)` 括号里的号可以直接填**；"
        "群号不用写，自动填当前群）\n"
        # ⚠️ 2026-11-12 真机踩过两次：
        #    ① 「给他点赞」→ 她发了个 <sticker:点赞> 表情就完事，群友根本没收到名片赞
        #    ② 「戳一戳 rera」→ 她没去戳，回了句「戳我干嘛」（把"你去戳XX"当成"你被戳"）
        #    「点赞/戳一戳」中文里既可以是"动作"也可以是"表情/被动"，必须点明。
        "⚠️ 「点赞/戳一戳」是**真去操作**：发 <sticker:点赞> 只是发图，对方收不到名片赞；\n"
        "   「戳一戳 XX」= **你去戳 XX**，不是被戳，别回「戳我干嘛」。\n"
        "   不知道 XX 的 QQ？名字直接填 —— **别因为不知道号就不动手**。\n"
        "   写完标记**别自己宣布「已经赞了」**，等系统把结果给你，再照着结果说。\n"
        f"【其他 {len(TOOLS)} 个 API · 按需捞】napcat_help() 看目录 → "
        "napcat_help(tool=\"工具名\") 看参数 → 用 [[[NAPCAT|工具名|{json}]]] 调\n"
    )
    if not HINT_FULL:
        return head + (
            "规矩：踢人/禁言/上下管理/退群/加好友要主人点头（系统替你问）；"
            "一条回复最多调 3 个工具；别用 send_group_msg 重复发正文里说过的话。"
            + ("群聊里少动，普通接话就正常说话。" if is_group else "")
        )
    cats = " / ".join(f"{c}={CATEGORIES[c][0].split(' ', 1)[-1]}"
                      for c in CATEGORIES if c != "raw")
    return head + (
        f"类别速查：{cats}\n"
        "规矩：\n"
        "· 群号可以不写（自动填当前群）；**QQ 号必须写准**，不知道就把 user_id 写成名字，系统会去名册里查。\n"
        "· 踢人/禁言/上下管理/退群/加好友/改群名/全员禁言这类**要主人点头**："
        "你直接调，系统会替你去问主人，被拒了就老实说「主人没同意」，别自己假装做成了。\n"
        "· 一条回复最多调 3 个工具，别刷。\n"
        + ("· 群聊里**少动**：只有真的要改东西（禁言/踢人/改名片/发文件）才调，普通接话就正常说话。\n"
           if is_group else "")
        + "· 你已经能正常发消息了，**不要**用 send_group_msg 重复发你正文里已经说过的话。"
    )


# ============================================================
# 权限
# ============================================================
def level_ok(level: str, requester: str) -> bool:
    if not ENABLED:
        return False
    if level == SAFE:
        return True
    lv = access.level_of(requester or "")
    if level == ADMIN:
        return lv in (access.LEVEL_OWNER, access.LEVEL_ADMIN)
    return lv == access.LEVEL_OWNER


def _owner_qq() -> str:
    return str(access.OWNER_QQ or "")


# ============================================================
# 待主人批准的请求
# ============================================================
PENDING: dict = {}


def _purge_pending():
    now = time.time()
    for k in [k for k, v in PENDING.items() if now - v["ts"] > APPROVE_TTL]:
        PENDING.pop(k, None)


def make_pending(tool: str, args: dict, ctx: dict, bot) -> str:
    """登记一个待批准请求，返回 4 位编号。"""
    _purge_pending()
    for _ in range(20):
        pid = f"{random.randint(1000, 9999)}"
        if pid not in PENDING:
            break
    else:
        pid = f"{int(time.time()) % 10000:04d}"
    PENDING[pid] = {
        "tool": tool, "args": dict(args or {}), "ts": time.time(),
        "bot": bot,
        "self_id": str(ctx.get("self_id") or ""),
        "group_id": ctx.get("group_id"),
        "user_id": ctx.get("user_id"),
        "requester": str(ctx.get("requester") or ""),
        "name": str(ctx.get("name") or ""),
    }
    return pid


def take_pending(pid: str) -> dict:
    _purge_pending()
    return PENDING.pop(str(pid or "").strip(), None)


def pending_text(pid: str) -> str:
    """给主人看的批准提示。"""
    p = PENDING.get(str(pid)) or {}
    if not p:
        return ""
    where = f"群 {p.get('group_id')}" if p.get("group_id") else "私聊"
    who = f"{p.get('name') or '?'}({p.get('requester') or '?'})"
    return (
        f"🛡️ 需要你点头（编号 {pid}）\n"
        f"{where} 的 {who} 让她做：{TOOLS.get(p['tool'], {}).get('desc') or p['tool']}\n"
        f"工具：{p['tool']}\n"
        f"参数：{json.dumps(p['args'], ensure_ascii=False)[:200]}\n"
        f"· /批准 {pid} — 执行\n"
        f"· /拒绝 {pid} — 拒绝\n"
        f"{APPROVE_TTL // 60} 分钟内有效"
    )


# ============================================================
# 调用
# ============================================================
_CALLS: dict = {}


# 今天给谁点过名片赞。QQ 侧同一人**每天只算一次**，重复调只是空转 ——
# 但每次都会「成功返回」，她会一直以为自己点上了（2026-11-12 真机：
# 群友反复催「赞10次」，她反复调 send_like，对方一个都没收到）。
_LIKED: dict = {}


def _like_key(uid) -> tuple:
    return (str(uid), time.strftime("%Y-%m-%d"))


def _rate_ok(key: str) -> bool:
    now = time.time()
    rec = [t for t in _CALLS.get(key, []) if now - t < 60]
    if len(rec) >= MAX_CALLS_PER_MIN:
        _CALLS[key] = rec
        return False
    rec.append(now)
    _CALLS[key] = rec
    return True


def _coerce(kind: str, v):
    if kind == "int":
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (int, float)):
            return int(v)
        m = re.search(r"-?\d+", str(v or ""))
        return int(m.group()) if m else v
    if kind == "bool":
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on", "是", "开", "对")
    if kind == "str":
        return "" if v is None else str(v)
    return v


def _fmt_msg(m) -> str:
    """把 OneBot 消息段压成一行可读文字。"""
    if isinstance(m, str):
        return m
    if isinstance(m, dict):
        t = m.get("type")
        d = m.get("data") or {}
        if t == "text":
            return str(d.get("text") or "")
        if t == "at":
            return "@" + str(d.get("qq") or "")
        return {"image": "[图]", "face": "[表情]", "record": "[语音]",
                "video": "[视频]", "reply": "[回复]", "file": "[文件]"}.get(t, f"[{t}]")
    if isinstance(m, list):
        return "".join(_fmt_msg(x) for x in m)
    return str(m)


def _compact(tool: str, data) -> str:
    """把 API 返回值压成给模型看的一小段（_run_tool_calls 只回填 600 字）。"""
    try:
        if tool == "get_group_member_list" and isinstance(data, list):
            parts = []
            for m in data[:60]:
                nm = str(m.get("card") or m.get("nickname") or "").strip()
                role = {"owner": "群主", "admin": "管理"}.get(m.get("role"), "")
                parts.append(f"{nm}({m.get('user_id')}{'/' + role if role else ''})")
            more = f" …还有 {len(data) - 60} 人" if len(data) > 60 else ""
            return f"共 {len(data)} 人：" + "、".join(parts) + more
        if tool in ("get_friend_list", "get_group_list") and isinstance(data, list):
            parts = []
            for m in data[:50]:
                nm = str(m.get("remark") or m.get("nickname") or m.get("group_name") or "").strip()
                mid = m.get("user_id") or m.get("group_id")
                parts.append(f"{nm}({mid})")
            more = f" …还有 {len(data) - 50} 个" if len(data) > 50 else ""
            return f"共 {len(data)} 个：" + "、".join(parts) + more
        if tool in ("get_group_msg_history", "get_friend_msg_history"):
            msgs = (data or {}).get("messages") if isinstance(data, dict) else data
            lines = []
            for m in (msgs or [])[-HISTORY_MAX:]:
                sender = (m.get("sender") or {}).get("card") or \
                         (m.get("sender") or {}).get("nickname") or m.get("user_id") or "?"
                ts = m.get("time")
                when = time.strftime("%H:%M", time.localtime(ts)) if ts else ""
                lines.append(f"{when} {sender}：{_fmt_msg(m.get('message') or m.get('raw_message'))[:60]}")
            return "\n".join(lines) or "没有消息"
        if tool == "get_msg" and isinstance(data, dict):
            sender = (data.get("sender") or {}).get("card") or \
                     (data.get("sender") or {}).get("nickname") or data.get("user_id")
            return f"{sender}（{data.get('message_id')}）：{_fmt_msg(data.get('message'))[:200]}"
        if tool == "get_forward_msg":
            msgs = (data or {}).get("messages") if isinstance(data, dict) else data
            lines = []
            for m in (msgs or [])[:20]:
                sender = (m.get("sender") or {}).get("nickname") or "?"
                lines.append(f"{sender}：{_fmt_msg(m.get('message') or m.get('content'))[:80]}")
            return "\n".join(lines) or "空的合并转发"
        if tool == "get_essence_msg_list":
            msgs = (data or {}).get("messages") if isinstance(data, dict) else data
            lines = []
            for m in (msgs or [])[:15]:
                lines.append(f"{m.get('sender_nick') or m.get('sender_id')}："
                             f"{_fmt_msg(m.get('content') or m.get('message'))[:60]}")
            return "\n".join(lines) or "没有精华消息"
        if tool in ("get_group_root_files", "get_group_files_by_folder"):
            d = data if isinstance(data, dict) else {}
            files = [f"{f.get('file_name')}({f.get('file_id')})" for f in (d.get("files") or [])[:30]]
            folders = [f"📁{f.get('folder_name')}({f.get('folder_id')})" for f in (d.get("folders") or [])[:10]]
            return "、".join(folders + files) or "空目录"
    except Exception as e:
        logger.debug(f"[NAPCAT] 压缩 {tool} 结果失败: {type(e).__name__}: {e}")

    if isinstance(data, (dict, list)):
        s = json.dumps(data, ensure_ascii=False)
    else:
        s = str(data)
    return s if len(s) <= RESULT_MAX else s[:RESULT_MAX] + "…"


async def _resolve_user(bot, gid, val):
    """user_id 允许写名字：去群名册里查，唯一命中才用。返回 (uid, err)。"""
    # 已经是数字（int 或者纯数字串）→ 直接当 QQ 号，别去名册里瞎查
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return int(val), ""
    s = str(val or "").strip()
    if s.isdigit():
        return int(s), ""
    if not s:
        return None, "user_id 是空的"
    if not gid:
        return None, f"「{s}」不是 QQ 号，而且这是私聊，没法按名字查人。请直接给 QQ 号。"
    try:
        from . import group_roster
        members = await group_roster.members_of(bot, gid)
        hits = group_roster.find(members, s, limit=5)
    except Exception as e:
        return None, f"查名册失败：{type(e).__name__}"
    if not hits:
        return None, f"群里没找到叫「{s}」的人，别编 QQ 号。先 [[WHO|{s}]] 或看成员名单。"
    if len(hits) > 1:
        who = "、".join(group_roster.label(m) for m in hits)
        return None, f"「{s}」对上好几个人：{who}。请用 QQ 号指定。"
    return int(hits[0]["user_id"]), ""


async def execute(bot, tool: str, args, *, requester: str = "",
                  ctx: dict | None = None, skip_level: bool = False) -> str:
    """
    真正调 NapCat。返回**给模型看的**一段话（≤550 字），绝不抛异常。
    skip_level：主人已经批准过了，跳过权限检查。
    """
    ctx = ctx or {}
    name = str(tool or "").strip()
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except Exception:
            return f'args 得是 JSON 对象，比如 {{"group_id":123}}。你给的是：{args[:80]}'
    if not isinstance(args, dict):
        args = {}

    # 👇 模型常把「不填」写成 0。2026-11-19 真机日志：她调 get_group_msg_history 时
    #    给了 `group_id: 0`，接口直接失败，白花一轮 tool call。
    #    0 / "0" / none 一律当「没填」，让下面的「自动补当前群」生效。
    for _k in ("group_id", "user_id", "message_id"):
        if _k in args and str(args.get(_k)).strip().lower() in ("0", "", "none", "null"):
            args.pop(_k, None)
            logger.info(f"[NAPCAT] {name} 的 {_k} 填了 0/空 → 当成没填，自动补当前会话")

    if name not in TOOLS:
        hits = [n for n in TOOLS if name.lower() in n.lower()][:4]
        hint = f"，你是不是想找：{'、'.join(hits)}" if hits else ""
        return f"没有「{name}」这个工具{hint}。先 napcat_help() 看类别，再看具体工具的说明。"

    spec = TOOLS[name]
    if not ENABLED:
        return "NapCat 工具被关掉了（NAPCAT_TOOLS=false）。"

    # ---- 参数（先校验再问主人：别把一个「参数都不全」的请求丢给主人） ----
    if not _rate_ok(f"{requester}:{ctx.get('group_id') or ctx.get('user_id')}"):
        return f"调太勤了（1 分钟上限 {MAX_CALLS_PER_MIN} 次），歇一下再试。"

    gid = args.get("group_id") or ctx.get("group_id")

    # 👇 私聊里「对方」只有一个 —— user_id 不填就自动补当前对话的人。
    #    用户 2026-11-12：「napcat 大部分功能都在群聊，私聊没有」：
    #    以前私聊调 get_friend_msg_history 必须自己填 QQ 号，不填就报参数不全，
    #    等于把私聊能用的那几个 API 全堵死了。
    #    群聊里只对 safe 级补（否则 set_group_kick 不写 user_id 会默认踢说话的人）。
    _pre = dict(args)
    # set_group_card 例外：它不填 user_id 的含义是「改她自己的名片」，不是「改说话人」
    if (name != "set_group_card" and "user_id" in spec["params"]
            and _pre.get("user_id") in (None, "")):
        _uid = str(ctx.get("user_id") or "")
        if _uid.isdigit() and (not gid or spec["level"] == SAFE):
            _pre["user_id"] = int(_uid)

    clean = {}
    missing = []
    for p, (kind, req, desc) in spec["params"].items():
        v = _pre.get(p)
        if v in (None, ""):
            if p == "group_id" and gid:
                clean[p] = int(gid)
                continue
            if req:
                missing.append(f"{p}（{kind}）{desc}")
            continue
        clean[p] = _coerce(kind, v)

    if missing:
        return ("参数不全：" + "；".join(missing)
                + f"\n用法：napcat_help(tool=\"{name}\")")

    # 拉历史硬上限 20 条（用户 2026-11-11：别让她一次把上下文拉爆）。
    # 光在说明里写「上限 20」不够 —— 她照抄 100 进来就直接把上下文挤掉了。
    # ---- 白名单（先于权限分级；SAFE 也不例外）----
    if not skip_level:
        _deny = _whitelist_check(name, clean)
        if _deny:
            return _deny

    if "count" in clean and isinstance(clean["count"], int):
        clean["count"] = max(1, min(HISTORY_MAX, clean["count"]))

    # 点赞：NapCat 的 send_like 少了 times 有的版本会直接拒，
    # 她一般只说「给 XX 点赞」不会想到要给次数 → 这里兜个 1。
    if name == "send_like" and "times" not in clean:
        clean["times"] = 1
    elif name == "send_like" and isinstance(clean.get("times"), int):
        clean["times"] = max(1, min(20, clean["times"]))

    # 改群名片不写 user_id = 改她自己的
    if name == "set_group_card" and "user_id" not in clean:
        sid = str(ctx.get("self_id") or "")
        if sid.isdigit():
            clean["user_id"] = int(sid)

    if "user_id" in clean and clean["user_id"] is not None:
        uid, err = await _resolve_user(bot, clean.get("group_id") or gid, clean["user_id"])
        if err:
            return err
        clean["user_id"] = uid

    # ---- 权限 ----
    if not skip_level and not level_ok(spec["level"], requester):
        owner = _owner_qq()
        if not owner:
            return "这个操作要主人点头，但没配 SUPERUSERS，做不了。"
        pid = make_pending(name, clean, {**ctx, "requester": requester}, bot)
        try:
            if bot:
                await bot.send_private_msg(user_id=int(owner), message=pending_text(pid))
        except Exception as e:
            logger.warning(f"[NAPCAT] 通知主人失败: {type(e).__name__}: {e}")
            return ("这个操作要主人点头，但我联系不上主人（私聊发不出去）。"
                    "跟他说一声，让他先加我好友。")
        logger.info(f"[NAPCAT] {name} 需要主人批准，编号 {pid}（来自 {requester}）")
        return (f"这个要主人点头才行（编号 {pid}），我已经私聊问过主人了。"
                f"跟他说一声在等主人回复，**别假装已经做成了**。")

    # 名片赞：同一人今天已经点过就别再空转（QQ 每天只算一次，重复调也是「成功」）
    if name == "send_like":
        _k = _like_key(clean.get("user_id"))
        if _LIKED.get(_k):
            return (f"今天已经给 TA 点过 {_LIKED[_k]} 次了。"
                    "QQ 名片赞同一人**每天只算一次**，再点也不会增加 —— "
                    "老实告诉对方「今天已经赞过了，明天再补」，别硬说又赞了。")

    # ---- 真正调用 ----
    try:
        if name == "call_any_api":
            action = str(clean.get("action") or "").strip()
            params = clean.get("params") or {}
            if isinstance(params, str):
                try:
                    params = json.loads(params)
                except Exception:
                    params = {}
            if not action:
                return "action 是空的"
            data = await bot.call_api(action, **(params or {}))
            logger.info(f"[NAPCAT] raw {action} → {str(data)[:80]}")
            return _compact(action, data)

        data = await bot.call_api(name, **clean)
    except Exception as e:
        msg = str(e)
        logger.warning(f"[NAPCAT] {name} 失败: {type(e).__name__}: {msg[:200]}")
        if "retcode" in msg or "权限" in msg or "admin" in msg.lower():
            return f"{name} 被拒了：{msg[:200]}（多半是她不是管理员/群主，或者 QQ 那边风控）"
        return f"{name} 调用失败：{type(e).__name__}: {msg[:200]}"

    logger.info(f"[NAPCAT] {name}({json.dumps(clean, ensure_ascii=False)[:120]}) → {str(data)[:80]}")
    if name == "send_like":
        _k = _like_key(clean.get("user_id"))
        _LIKED[_k] = _LIKED.get(_k, 0) + int(clean.get("times") or 1)

    if data in (None, {}, []):
        # 👇 名片赞是「静默接口」：OneBot 标准里成功就是 data=null，
        #    但 QQ 那边还有**每日上限**、**同一人每天只算一次**，
        #    而且**对非好友可能直接丢掉**。别让她以为返回 None 就等于对方收到了。
        #    （2026-11-12 真机：群友一直说「没赞上」，API 每次都「成功」。）
        if name == "send_like":
            return (f"send_like 已发出（times={clean.get('times', 1)}），接口返回成功。"
                    "⚠️ 但名片赞有每日上限、同一人每天只算一次，"
                    "**对非好友可能被 QQ 静默丢掉** —— 别跟对方打包票说「一定收到了」。"
                    "如果对方说没收到，让他先加好友、或者明天再试。")
        return f"{name} 执行成功（没有返回值）"
    return _compact(name, data)


def stats() -> str:
    """给 /工具表 用的一行统计。"""
    per = "，".join(f"{c}:{len(_by_cat(c))}" for c in CATEGORIES if _by_cat(c))
    return f"共 {len(TOOLS)} 个 API / {len(CATEGORIES)} 类（{per}）"
