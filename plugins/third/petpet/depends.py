import re
import shlex
from io import BytesIO
from typing import List, Optional

from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    unescape,
)
from nonebot.log import logger
from nonebot.params import Depends
from nonebot.rule import Rule
from nonebot.typing import T_State
from nonebot_plugin_imageutils import BuildImage

from services import identity as _identity

from .config import petpet_config
from .download import download_avatar, download_url
from .utils import UserInfo

USERS_KEY = "USERS"
SENDER_KEY = "SENDER"
ARGS_KEY = "ARGS"
REGEX_DICT = "REGEX_DICT"
REGEX_ARG = "REGEX_ARG"

command_start = "|".join(petpet_config.petpet_command_start)
logger.info(
    f"[PETPET] 前缀配置 petpet_command_start={petpet_config.petpet_command_start!r}"
    f" → 正则前缀={command_start!r}"
)
# ⚠️ 这段日志是排查「指令没反应」的第一现场：
#    petpet 的触发是 re.match(rf"(?:{command_start})(?:{pattern})", seg_text)，
#    默认 petpet_command_start=[""] 只认不带前缀的「手枪 xx」，
#    发 /手枪 xx 时开头的 "/" 会让 match 从第一个字符就失败，121 个模板全部不命中。
#    只要这里打出 前缀=''，就说明 PETPET_COMMAND_START 没配上。


def regex(pattern: str) -> Rule:
    def checker(event: MessageEvent, state: T_State) -> bool:
        msg = event.get_message()
        if not msg:
            return False
        msg_seg: MessageSegment = msg[0]
        if not msg_seg.is_text():
            return False

        seg_text = str(msg_seg).lstrip()
        matched = re.match(
            rf"(?:{command_start})(?:{pattern})", seg_text, re.IGNORECASE
        )
        if not matched:
            return False

        new_msg = msg.copy()
        rest = seg_text[matched.end() :].lstrip()
        # 只在**命中时**打日志：121 个模板每个都会跑一遍 checker，全打会刷屏。
        # 看不到这行 = 一个模板都没命中（前缀不对 / 首段不是文本 / 关键词写错）。
        logger.info(
            f"[PETPET] ✅ 命中 pattern={pattern!r} 原文={seg_text!r} 剩余参数={rest!r}"
        )
        if rest:
            new_msg[0].data["text"] = rest
        else:
            new_msg.pop(0)
        state[REGEX_DICT] = matched.groupdict()
        state[REGEX_ARG] = new_msg
        return True

    return Rule(checker)


def is_qq(msg: str):
    return msg.isdigit() and 11 >= len(msg) >= 5


def split_msg():
    def dependency(event: MessageEvent, state: T_State):
        def _is_at_me_seg(segment: MessageSegment):
            return segment.type == "at" and _identity.is_self(
                segment.data.get("qq", ""), str(event.self_id)
            )

        msg: Message = state["REGEX_ARG"]

        if event.to_me:
            raw_msg = event.original_message
            i = -1
            last_msg_seg = raw_msg[i]
            if (
                last_msg_seg.type == "text"
                and not last_msg_seg.data["text"].strip()
                and len(raw_msg) >= 2
            ):
                i -= 1
                last_msg_seg = raw_msg[i]
            if _is_at_me_seg(last_msg_seg):
                msg.append(last_msg_seg)

        users: List[UserInfo] = []
        args: List[str] = []

        if event.reply:
            for img in event.reply.message["image"]:
                users.append(UserInfo(img_url=str(img.data.get("url", ""))))

        for msg_seg in msg:
            if msg_seg.type == "at":
                users.append(
                    UserInfo(
                        qq=str(msg_seg.data.get("qq", "")),
                        group=str(event.group_id)
                        if isinstance(event, GroupMessageEvent)
                        else "",
                    )
                )
            elif msg_seg.type == "image":
                users.append(UserInfo(img_url=str(msg_seg.data.get("url", ""))))
            elif msg_seg.type == "text":
                raw_text = str(msg_seg)
                try:
                    texts = shlex.split(raw_text)
                except:
                    texts = raw_text.split()
                for text in texts:
                    if is_qq(text):
                        users.append(UserInfo(qq=text))
                    elif text == "自己":
                        users.append(
                            UserInfo(
                                qq=str(event.user_id),
                                group=str(event.group_id)
                                if isinstance(event, GroupMessageEvent)
                                else "",
                            )
                        )
                    else:
                        text = unescape(text).strip()
                        if text:
                            args.append(text)

        sender = UserInfo(qq=str(event.user_id))
        state[SENDER_KEY] = sender
        state[USERS_KEY] = users
        state[ARGS_KEY] = args
        # 命中模板后才会走到这里。users 为空说明「@某人 / qq号 / 自己 / 图片」一个都没解析出来，
        # 模板的 func 会因为 User() 依赖拿不到人而直接跳过（表现为「命令有反应但不出图」）。
        logger.info(
            f"[PETPET] 参数解析 users={len(users)} 个 args={args!r} sender={sender.qq}"
        )

    return Depends(dependency)


# 昵称里的 emoji 渲染不了（2026-11-13 实测）：
#   🥔 这类字符在 **BMP 之外**（U+1F954），WenQuanYi / DejaVu 这些中英文字体一个都没有，
#   imageutils 会打
#     [WARNING] 在当前字体列表中找不到可以显示字符"🥔"的字体
#   然后把那个位置画成空白/豆腐块，于是 /复读 /关注 出来的名字是缺一块的。
#   装 Noto Color Emoji 也没用：那是 CBDT 彩色位图字体，Pillow 不开 embedded_color
#   只会画出一团黑或者空白。
#   所以直接把非 BMP 的 emoji、变体选择符、ZWJ 去掉，让名字干净可读。
#   只清 U+1F000 以上 + VS + ZWJ：★ ✓ 这类 BMP 符号 WenQuanYi 是有的，保留。
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u200d\ufe0e\ufe0f]")


def sanitize_name(name: str) -> str:
    if not name:
        return ""
    cleaned = _EMOJI_RE.sub("", name).strip()
    if cleaned != name:
        logger.debug(f"[PETPET] 昵称去掉 emoji：{name!r} → {cleaned!r}")
    return cleaned


async def get_user_info(bot: Bot, user: UserInfo):
    if not user.qq:
        return

    if user.group:
        info = await bot.get_group_member_info(
            group_id=int(user.group), user_id=int(user.qq)
        )
        raw_name = info.get("card", "") or info.get("nickname", "")
        user.gender = info.get("sex", "")
    else:
        info = await bot.get_stranger_info(user_id=int(user.qq))
        raw_name = info.get("nickname", "")
        user.gender = info.get("sex", "")

    user.name = sanitize_name(raw_name) or f"用户{user.qq}"


async def download_image(user: UserInfo):
    img = None
    try:
        if user.qq:
            img = await download_avatar(user.qq)
        elif user.img_url:
            img = await download_url(user.img_url)
    except Exception as e:  # noqa: BLE001
        # ⚠️ 必须在这里自己兜住（2026-11-13 实测 /兑换券 挂在这）：
        #    download_avatar 拿不到头像时是**抛异常**，不是返回 None。
        #    异常冒到 Users() 的 catch 后，user.img 仍然是 None，
        #    而模板里普遍写 user.img.convert("RGBA") → AttributeError
        #    → 依赖解析阶段炸 → 日志只剩
        #      ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
        #    用户只看到「指令有反应但不出图」。
        #    为什么 /这像画吗 能出图、/兑换券 不行：前者走 UserImgs() 直接调本函数，
        #    后者走 User()→Users()，两条路径对 None 的容忍度不同。
        logger.warning(
            f"[PETPET] 头像下载失败 qq={user.qq} has_url={bool(user.img_url)}"
            f" → {type(e).__name__}: {e}（改用灰色占位图继续出图）"
        )

    try:
        if img:
            # ⚠️ 解码也必须放在 try 里（2026-11-13 二次修正）：
            #    qlogo 有时返回 200 + 非图片内容（风控页/空 body），
            #    BuildImage.open 抛 UnidentifiedImageError。
            #    上一版把这一行写在 try 外面，异常直接冒到 TaskGroup，
            #    日志里只剩 ExceptionGroup，连是哪一步都看不出来。
            user.img = BuildImage.open(BytesIO(img))
            return
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[PETPET] 头像解码失败 qq={user.qq} bytes={len(img) if img else 0}"
            f" → {type(e).__name__}: {e}（改用灰色占位图继续出图）"
        )

    # 兜底占位图：保证 user.img 永远不是 None，模板里 .convert() 不会炸
    logger.debug(f"[PETPET] qq={user.qq} 无头像可用，使用占位图")
    user.img = BuildImage.new("RGBA", (640, 640), "lightgray")


def Users(min_num: int = 1, max_num: int = 1):
    async def dependency(bot: Bot, state: T_State):
        # 整体再包一层：nonebot 的依赖解析跑在 anyio TaskGroup 里，
        # 这里漏出去的任何异常都会变成「ExceptionGroup: 1 sub-exception」，
        # 日志里连类型都看不到。宁可返回空列表让模板自己跳过，也要把原因记下来。
        try:
            users: List[UserInfo] = state[USERS_KEY]
            if len(users) > max_num or len(users) < min_num:
                logger.debug(
                    f"[PETPET] Users 数量不匹配 users={len(users)} "
                    f"需 {min_num}~{max_num}，跳过"
                )
                return

            for user in users:
                try:
                    await get_user_info(bot, user)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[PETPET] 取用户资料失败 qq={user.qq} group={user.group}"
                        f" → {type(e).__name__}: {e}（名字退化成默认值，继续出图）"
                    )
                try:
                    await download_image(user)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"[PETPET] 下载头像失败 qq={user.qq}"
                        f" → {type(e).__name__}: {e}（用灰色占位图继续出图）"
                    )
            return users
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[PETPET] Users 依赖整体失败 {type(e).__name__}: {e}")
            return []

    return Depends(dependency)


def User():
    def dependency(users: Optional[List[UserInfo]] = Users()):
        try:
            if users:
                return users[0]
            # 兜底：拿不到人也得给个可用对象。返回 None 的话，
            # 模板里 `user.name` / `user.img.convert()` 直接 AttributeError，
            # 而且是在 anyio TaskGroup 里炸，日志只剩 ExceptionGroup。
            logger.warning("[PETPET] User() 没拿到用户，返回占位 UserInfo")
            u = UserInfo()
            u.img = BuildImage.new("RGBA", (640, 640), "lightgray")
            return u
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[PETPET] User 依赖失败 {type(e).__name__}: {e}")
            u = UserInfo()
            u.img = BuildImage.new("RGBA", (640, 640), "lightgray")
            return u

    return Depends(dependency)


def UserImgs(min_num: int = 1, max_num: int = 1):
    async def dependency(state: T_State):
        try:
            users: List[UserInfo] = state[USERS_KEY]
            if len(users) > max_num or len(users) < min_num:
                return

            for user in users:
                await download_image(user)
            return [user.img for user in users]
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[PETPET] UserImgs 依赖失败 {type(e).__name__}: {e}")
            return []

    return Depends(dependency)


def UserImg():
    def dependency(imgs: List[BuildImage] = UserImgs()):
        if imgs:
            return imgs[0]

    return Depends(dependency)


def Sender():
    async def dependency(bot: Bot, state: T_State):
        try:
            sender: UserInfo = state[SENDER_KEY]
            await get_user_info(bot, sender)
            await download_image(sender)
            return sender
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[PETPET] Sender 依赖失败 {type(e).__name__}: {e}")
            u = UserInfo()
            u.img = BuildImage.new("RGBA", (640, 640), "lightgray")
            return u

    return Depends(dependency)


def SenderImg():
    async def dependency(state: T_State):
        try:
            sender: UserInfo = state[SENDER_KEY]
            await download_image(sender)
            return sender.img
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[PETPET] SenderImg 依赖失败 {type(e).__name__}: {e}")
            return BuildImage.new("RGBA", (640, 640), "lightgray")

    return Depends(dependency)


def Args(min_num: int = 1, max_num: int = 1):
    def dependency(state: T_State):
        try:
            args: List[str] = state[ARGS_KEY]
            if len(args) > max_num or len(args) < min_num:
                return
            return args
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[PETPET] Args 依赖失败 {type(e).__name__}: {e}")
            return []

    return Depends(dependency)


def RegexArg(key: str):
    def dependency(state: T_State):
        try:
            arg: dict = state[REGEX_DICT]
            # 消费方注解是 str（如 my_friend name: str = RegexArg("name")），
            # 返回 None 会在 nonebot 依赖解析阶段 TypeMisMatch，且 name.strip() 会炸。
            # 缺组/异常一律给 ""，让 `x.strip() or 默认` 正常兜底（2026-11-28 审计）。
            return arg.get(key) or ""
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[PETPET] RegexArg 依赖失败 {type(e).__name__}: {e}")
            return ""

    return Depends(dependency)


def Arg(possible_values=None):
    if possible_values is None:
        possible_values = []

    def dependency(args: List[str] = Args(0, 1)):
        if args:
            arg = args[0]
            if possible_values and arg not in possible_values:
                # 非法受限值：消费方注解 str，返回 None → TypeMisMatch。
                # 给 ""，meme 体的 `if arg == "右"` / `arg or 默认` 自然走默认分支。
                return ""
            return arg
        else:
            return ""

    return Depends(dependency)


def NoArg():
    def dependency(args: List[str] = Args(0, 0)):
        return

    return Depends(dependency)
