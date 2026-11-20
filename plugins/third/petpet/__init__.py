import math
import random
import time
from io import BytesIO
from pathlib import Path
from typing import Any, List, Literal, Union

from nonebot import on_command, on_message, require
from nonebot.log import logger
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.permission import (
    GROUP_ADMIN,
    GROUP_OWNER,
    PRIVATE_FRIEND,
)
from nonebot.dependencies import Dependent
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot.params import CommandArg, Depends
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_Handler
from nonebot.utils import run_sync
from PIL.Image import Image as IMG

require("nonebot_plugin_imageutils")
from nonebot_plugin_imageutils import BuildImage, Text2Image

from .config import Config
from .data_source import memes
from .depends import regex, split_msg
from .manager import ActionResult, MemeMode, meme_manager
from .utils import Meme

__plugin_meta__ = PluginMetadata(
    name="头像表情包",
    description="摸头等头像相关表情制作",
    usage="触发方式：指令 + @user/qq/自己/图片\n发送“头像表情包”查看支持的指令",
    config=Config,
    extra={
        "unique_name": "petpet",
        "example": "摸 @小Q\n摸 114514\n摸 自己\n摸 [图片]",
        "author": "meetwq <meetwq@gmail.com>",
        "version": "0.3.21",
    },
)

PERM_EDIT = GROUP_ADMIN | GROUP_OWNER | PRIVATE_FRIEND | SUPERUSER
PERM_GLOBAL = SUPERUSER

help_cmd = on_command("头像表情包", aliases={"头像相关表情包", "头像相关表情制作"}, block=True, priority=12)
block_cmd = on_command("禁用表情", block=True, priority=12, permission=PERM_EDIT)
unblock_cmd = on_command("启用表情", block=True, priority=12, permission=PERM_EDIT)
block_cmd_gl = on_command("全局禁用表情", block=True, priority=12, permission=PERM_GLOBAL)
unblock_cmd_gl = on_command("全局启用表情", block=True, priority=12, permission=PERM_GLOBAL)


@run_sync
def help_image(user_id: str, memes: List[Meme]) -> BytesIO:
    def cmd_text(memes: List[Meme], start: int = 1) -> str:
        texts = []
        for i, meme in enumerate(memes):
            text = f"{i + start}. " + "/".join(meme.keywords)
            if not meme_manager.check(user_id, meme):
                text = f"[color=lightgrey]{text}[/color]"
            texts.append(text)
        return "\n".join(texts)

    head_text = "摸头等头像相关表情制作\n触发方式：指令 + @某人 / qq号 / 自己 / [图片]\n支持的指令："
    head = Text2Image.from_text(head_text, 30, weight="bold").to_image(padding=(20, 10))
    imgs: List[IMG] = []
    col_num = 3
    num_per_col = math.ceil(len(memes) / col_num)
    for idx in range(0, len(memes), num_per_col):
        text = cmd_text(memes[idx : idx + num_per_col], start=idx + 1)
        imgs.append(Text2Image.from_bbcode_text(text, 30).to_image(padding=(20, 10)))
    w = max(sum((img.width for img in imgs)), head.width)
    h = head.height + max((img.height for img in imgs))
    frame = BuildImage.new("RGBA", (w, h), "white")
    frame.paste(head, alpha=True)
    current_w = 0
    for img in imgs:
        frame.paste(img, (current_w, head.height), alpha=True)
        current_w += img.width
    return frame.save_jpg()


def get_user_id():
    def dependency(event: MessageEvent) -> str:
        return (
            f"group_{event.group_id}"
            if isinstance(event, GroupMessageEvent)
            else f"private_{event.user_id}"
        )

    return Depends(dependency)


def check_flag(meme: Meme):
    def dependency(user_id: str = get_user_id()) -> bool:
        # meme_manager.check 是 __meme_list[meme.name] 直接取值，键缺失会 KeyError。
        # 它在依赖解析的 TaskGroup 里跑，抛出去只会变成
        # ExceptionGroup: 1 sub-exception，什么都看不出来，所以自己兜住。
        try:
            return meme_manager.check(user_id, meme)
        except Exception as e:  # noqa: BLE001
            logger.exception(
                f"[PETPET] check_flag 失败 meme={meme.name} user={user_id}"
                f" → {type(e).__name__}: {e}（放行）"
            )
            return True

    return Depends(dependency)


@help_cmd.handle()
async def _(user_id: str = get_user_id()):
    img = await help_image(user_id, memes)
    if img:
        await help_cmd.finish(MessageSegment.image(img))


@block_cmd.handle()
async def _(
    matcher: Matcher, msg: Message = CommandArg(), user_id: str = get_user_id()
):
    meme_names = msg.extract_plain_text().strip().split()
    if not meme_names:
        matcher.block = False
        await matcher.finish()
    results = meme_manager.block(user_id, meme_names)
    messages = []
    for name, result in results.items():
        if result == ActionResult.SUCCESS:
            message = f"表情 {name} 禁用成功"
        elif result == ActionResult.NOTFOUND:
            message = f"表情 {name} 不存在！"
        else:
            message = f"表情 {name} 禁用失败"
        messages.append(message)
    await matcher.finish("\n".join(messages))


@unblock_cmd.handle()
async def _(
    matcher: Matcher, msg: Message = CommandArg(), user_id: str = get_user_id()
):
    meme_names = msg.extract_plain_text().strip().split()
    if not meme_names:
        matcher.block = False
        await matcher.finish()
    results = meme_manager.unblock(user_id, meme_names)
    messages = []
    for name, result in results.items():
        if result == ActionResult.SUCCESS:
            message = f"表情 {name} 启用成功"
        elif result == ActionResult.NOTFOUND:
            message = f"表情 {name} 不存在！"
        else:
            message = f"表情 {name} 启用失败"
        messages.append(message)
    await matcher.finish("\n".join(messages))


@block_cmd_gl.handle()
async def _(matcher: Matcher, msg: Message = CommandArg()):
    meme_names = msg.extract_plain_text().strip().split()
    if not meme_names:
        matcher.block = False
        await matcher.finish()
    results = meme_manager.change_mode(MemeMode.WHITE, meme_names)
    messages = []
    for name, result in results.items():
        if result == ActionResult.SUCCESS:
            message = f"表情 {name} 已设为白名单模式"
        elif result == ActionResult.NOTFOUND:
            message = f"表情 {name} 不存在！"
        else:
            message = f"表情 {name} 设置失败"
        messages.append(message)
    await matcher.finish("\n".join(messages))


@unblock_cmd_gl.handle()
async def _(matcher: Matcher, msg: Message = CommandArg()):
    meme_names = msg.extract_plain_text().strip().split()
    if not meme_names:
        matcher.block = False
        await matcher.finish()
    results = meme_manager.change_mode(MemeMode.BLACK, meme_names)
    messages = []
    for name, result in results.items():
        if result == ActionResult.SUCCESS:
            message = f"表情 {name} 已设为黑名单模式"
        elif result == ActionResult.NOTFOUND:
            message = f"表情 {name} 不存在！"
        else:
            message = f"表情 {name} 设置失败"
        messages.append(message)
    await matcher.finish("\n".join(messages))


_tmp_cleaned = False


def _cleanup_temp(max_age_hours: int = 6) -> None:
    """
    清掉 data/temp/petpet 里的旧临时图。

    每调用一次表情包就往这里写一个文件（/复读 那种一张 5 MB），
    以前从来不删，跑久了能把盘吃满。只删超过 max_age_hours 的，
    刚发出去的还在 NapCat 手里，不能动。
    """
    global _tmp_cleaned
    if _tmp_cleaned:
        return
    _tmp_cleaned = True
    try:
        tmp_dir = Path() / "data" / "temp" / "petpet"
        if not tmp_dir.exists():
            return
        deadline = time.time() - max_age_hours * 3600
        removed = 0
        for f in tmp_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < deadline:
                f.unlink(missing_ok=True)
                removed += 1
        if removed:
            logger.info(f"[PETPET] 🧹 清理临时图 {removed} 个")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[PETPET] 清理临时图失败（忽略）：{type(e).__name__}: {e}")


def create_matchers():
    def handler(meme: Meme) -> T_Handler:
        async def handle(
            matcher: Matcher,
            flag: Literal[True] = check_flag(meme),
            res: Union[str, BytesIO] = Depends(meme.func),
        ):
            if not flag:
                # check_flag 挂了 = 被 petpet_disabled_list 禁用 / 冷却中 / 权限不够。
                # 表现就是「指令有反应但什么都不出」，必须打日志，否则完全静默。
                logger.info(f"[PETPET] ⛔ 模板 {meme.name} 被 check_flag 拦下（禁用/冷却/权限）")
                return
            matcher.stop_propagation()
            if isinstance(res, str):
                logger.info(f"[PETPET] ⚠️ 模板 {meme.name} 返回文本而非图片：{res[:80]}")
                await matcher.finish(res)
            # 👇 写临时文件用 file:// 发，别直接塞 BytesIO（2026-11-13 实测踩过）：
            #    MessageSegment.image(BytesIO) → nonebot 转 base64 data URI →
            #    NapCat 收下再解码再上传给 NTQQ，实测 retcode=1200
            #    「Timeout: NTEvent ... NodeIKernelMsgService/sendMsg」，卡满 10 秒失败。
            #    本项目 jinriyunshi / steam_judger / song_cover 发图全是 file://，
            #    NapCat 直接读本地文件，少一次 base64 编解码，稳得多。
            data = res.getvalue() if hasattr(res, "getvalue") else bytes(res)
            # 按文件头判格式：模板有的出 jpg 有的出 gif，写错后缀 NapCat 会当坏图
            ext = ".gif" if data[:6] in (b"GIF87a", b"GIF89a") else ".jpg"
            tmp_dir = Path() / "data" / "temp" / "petpet"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            _cleanup_temp()
            tmp = tmp_dir / f"{meme.name}_{int(time.time() * 1000)}{ext}"
            try:
                tmp.write_bytes(data)
                logger.info(f"[PETPET] 📤 发送 {tmp.name}（{len(data) // 1024} KB）")
                await matcher.finish(MessageSegment.image(f"file://{tmp.resolve()}"))
            except FinishedException:
                # ⚠️ matcher.finish() 是**正常结束**，它内部靠抛 FinishedException 做控制流。
                #    这里必须原样往上抛，绝对不能当异常接住 ——
                #    否则会掉进下面的 except Exception，走「回退 base64」再 finish 一次，
                #    用户就收到两张一样的图（2026-11-13 实测：/这像画吗 每次发两张）。
                raise
            except Exception as e:
                # ⚠️ 大文件超时**不代表没发出去**（2026-11-13 实测 /复读）：
                #    /复读 固定生成 50 帧 × 1079×1192 的 GIF，实测 4578~5032 KB。
                #    NapCat 的 sendMsg 调用 30 秒超时会返回 retcode=1200
                #      "Timeout: NTEvent ... NodeIKernelMsgService/sendMsg"，
                #    但 NTQQ 那边其实还在传，**图最后是能到的**，只是慢。
                #    所以这里不能当成失败去补救：
                #      · 回退 base64 = 把 5 MB 变成 6.7 MB 的 JSON 再超时一次，纯浪费
                #      · 回一句"图片太大已跳过" = 用户明明收到了图，还多收一句废话
                #    正确做法：记一条日志，静默收工，把控制权交回 nonebot。
                if len(data) > 1_000_000:
                    logger.warning(
                        f"[PETPET] {meme.name} 发送超时（{len(data) // 1024} KB）："
                        f"{type(e).__name__}。大图上传慢属正常，NTQQ 通常仍会送达，不再重试"
                    )
                    return

                # 小图失败多半是路径/权限问题（NapCat 读不到 file://），base64 值得一试
                logger.warning(
                    f"[PETPET] file:// 发送失败，回退 base64：{type(e).__name__}: {e}"
                )
                try:
                    await matcher.finish(MessageSegment.image(res))
                except FinishedException:
                    raise
                except Exception as e2:  # noqa: BLE001
                    # base64 也失败：只记日志，不让 ActionFailed 冒到 matcher 外层变红栈
                    logger.error(
                        f"[PETPET] ❌ {meme.name} base64 回退也失败："
                        f"{type(e2).__name__}: {e2}"
                    )

        return handle

    for meme in memes:
        on_message(
            regex(meme.pattern),
            block=False,
            priority=12,
        ).append_handler(handler(meme), parameterless=[split_msg()])

    def random_handler() -> T_Handler:
        def handle(matcher: Matcher, user_id: str = get_user_id()):
            random_meme = random.choice(
                [meme for meme in memes if meme_manager.check(user_id, meme)]
            )
            handler_ = Dependent[Any].parse(
                call=handler(random_meme),
                parameterless=[split_msg()],
                allow_types=matcher.HANDLER_PARAM_TYPES,
            )
            matcher.handlers.append(handler_)

        return handle

    on_message(regex("随机表情"), block=False, priority=12).append_handler(random_handler())


create_matchers()
