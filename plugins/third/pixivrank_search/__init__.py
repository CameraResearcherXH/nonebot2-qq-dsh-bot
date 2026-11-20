import asyncio
import os
import re
from datetime import datetime
from typing import Any

from nonebot import logger, on_message, require
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import ActionFailed, Message as OB11Message, MessageSegment
from nonebot.plugin import PluginMetadata, inherit_supported_adapters
from nonebot.rule import Rule

require("nonebot_plugin_alconna")

from nepattern import SwitchPattern, combine
from nepattern.base import CustomMatchPattern
from arclet.alconna import Args, Field, Option, Alconna, CommandMeta, store_true
from nonebot_plugin_alconna import Match, Query, MsgTarget, UniMessage, on_alconna

from .config import Config
from .util import UserExistLimiter
from .data_source import (
    _random_from_ranking,
    download_pixiv_imgs,
    get_pixiv_urls,
    search_pixiv_urls,
)

__plugin_meta__ = PluginMetadata(
    name="PixivRankSearch",
    description="基于RSSHUB阅读器实现的获取P站排行和P站搜图",
    usage="""\
p站排行 [type] [count] [date]
搜图 <关键词|作品id|作品链接> [r18]
    """,
    homepage="https://github.com/HibiKier/nonebot_plugin_pixivrank_search",
    type="application",
    config=Config,
    supported_adapters=inherit_supported_adapters("nonebot_plugin_alconna"),
    extra={
        "author": "HibiKier",
        "priority": 3,
        "version": "0.2.0",
    },
)

rank_dict = {
    "1": "day",
    "2": "week",
    "3": "month",
    "4": "week_original",
    "5": "week_rookie",
    "6": "day_r18",
    "7": "week_r18",
    "8": "day_male_r18",
    "9": "week_r18g",
}

_ulmt = UserExistLimiter()

RankPat = SwitchPattern(rank_dict)  # type: ignore
# ⚠️ nepattern 新版把 SwitchPattern.match 改成了只读属性，combine() 内部会 deepcopy：
#      File "nepattern/core.py", line 786, in copy → return deepcopy(self)
#      AttributeError: 'SwitchPattern' object attribute 'match' is read-only
#    → 整个插件 import 失败，/p站排行 连 matcher 都注册不上（日志里只有 chat 在响应）。
#    alias 只是给「参数不合法」报错提示用的显示名，丢了不影响解析，所以失败就跳过。
try:
    RankPat = combine(RankPat, alias="Rank")
except Exception as _e:  # noqa: BLE001
    logger.debug(f"[pixivrank] combine(alias) 跳过（nepattern 版本不兼容）：{_e}")


def check_date(_, date):
    datetime.strptime(date, "%Y-%m-%d")
    return date


Date = CustomMatchPattern(str, check_date, "Date")


rank = Alconna(
    "p站排行",
    Args["rank#R18仅可私聊", RankPat, "day"]["count?", int]["date?", Date],
    meta=CommandMeta(
        "获得P站排行榜",
        usage="""\
p站排行榜 <类型> [数量] [日期]
其中类型为：
1. 日排行
2. 周排行
3. 月排行
4. 原创排行
5. 新人排行
6. R18日排行
7. R18周排行
8. R18受男性欢迎排行
9. R18重口排行【慎重！】
""",
        example="""\
p站排行   （无参数默认为日榜）
p站排行 1
p站排行 1 5
p站排行 1 5 2018-4-25
    """,
        hide_shortcut=True,
        fuzzy_match=True,
    ),
)

# 关键词参数类型：优先 AllParam（整体吞掉剩余文本），老版本 alconna 没有就退回 MultiVar
try:
    from arclet.alconna import AllParam as _AllParam

    _TAG_ARG = _AllParam
except ImportError:  # pragma: no cover
    from arclet.alconna import MultiVar

    _TAG_ARG = MultiVar(str)


def _join_tag(tag) -> str:
    """把 AllParam/MultiVar 解析出来的片段拼回一个关键词。"""
    if isinstance(tag, (list, tuple)):
        parts = [str(x).strip() for x in tag]
        return " ".join(p for p in parts if p)
    return str(tag or "").strip()


search = Alconna(
    "搜图",
    # ⚠️ 关键词用 `AllParam` 整体吞掉，而不是 `Args["tag", str]`（2026-11-14 修）：
    #    原来 tag 只吃**一个** token，`/搜图 二見原 莉々子` 里多出来的「莉々子」
    #    无法被消费 → alconna 解析失败 → matcher 直接跳过 → 落到 plugins.chat
    #    当普通聊天回一句。表现就是「关键词中间有空格就搜不了」。
    #    改成 AllParam 后整段剩余文本都归 tag，再用空格拼回关键词
    #    （pixiv 的搜索里空格就是多标签 AND，语义正好对）。
    Args["tag", _TAG_ARG],
    Option("r18", action=store_true, default=False, help_text="是否开启 R18，仅可私聊"),
    meta=CommandMeta(
        "搜索P站图片",
        usage="""\
搜图 <关键词> [r18]
关键词可以带空格，比如「二見原 莉々子」
也可以直接发作品 id 或作品链接，会直接定位那一张
""",
        example="""\
搜图 樱岛麻衣
搜图 二見原 莉々子
搜图 146165548
搜图 樱岛麻衣 r18
""",
        hide_shortcut=True,
        fuzzy_match=True,
    ),
)

# 一次搜图任务的**出图预算**（秒）。原图常有 3~4 MB，NapCat 上传要好几分钟，
# 给 5 分钟；5 分钟内还没发完，就直接随机推一张日榜兜底。
# 环境变量 PIXR_SEND_TIMEOUT 可覆盖（单位：秒）。
SEND_TIMEOUT = float(os.getenv("PIXR_SEND_TIMEOUT") or 300)

# 串行闸门（2026-11-14 用户要求）：
#   同一个**会话**里，前面的图还没发出去，后面的请求就排队，排队时间**不计入**自己的 5 分钟预算；
#   只有前一张真的出去了，后面这张才开始计时。
#
# ⚠️ 锁的粒度是「会话」，不是全局（用户明确要求）：
#     · 每个群一把锁（group_<群号>）—— 群里的人互相排队
#     · 每个私聊一把锁（private_<QQ>）—— 各自独立
#   为什么不能全局：一个群在传 4 MB 大图要 4 分钟，会把别的群全堵死。
_flow_locks: dict[str, asyncio.Lock] = {}


def _session_key(event) -> str:
    """会话键：群聊按群分，私聊按人分。"""
    gid = getattr(event, "group_id", None)
    if gid:
        return f"group_{gid}"
    try:
        return f"private_{event.get_user_id()}"
    except Exception:  # noqa: BLE001
        return "global"


def _session_lock(key: str) -> asyncio.Lock:
    """取（或新建）该会话的锁。顺手清掉没人在用的空锁，避免字典无限增长。"""
    lock = _flow_locks.get(key)
    if lock is None:
        if len(_flow_locks) > 200:
            for k, lk in list(_flow_locks.items()):
                if not lk.locked():
                    _flow_locks.pop(k, None)
        lock = asyncio.Lock()
        _flow_locks[key] = lock
    return lock


def _is_send_timeout(exc: BaseException) -> bool:
    """判断 ActionFailed 是不是「NapCat 回执超时」而不是真的发不出去。

    ⚠️ 2026-11-14 实测：一张 3.9 MB 的原图（PNG，透明背景）NapCat 要传 4 分钟，
    `retcode=1200 Timeout: NTEvent ... sendMsg` 只是**API 调用**超时，
    图几分钟后照样到。所以这种超时不能当失败 —— 否则用户先收到一句
    「这张图网络炸了」，几分钟后又收到图，看着像插件坏了。
    """
    info = getattr(exc, "info", None)
    if isinstance(info, dict) and info.get("retcode") == 1200:
        return True
    msg = str(exc).lower()
    return "timeout" in msg or "retcode=1200" in msg


async def _send_imgs(caption: str, img_urls: list[str], user_id: str) -> None:
    """
    发一条「标题文字 + 图片」的消息。

    为什么不用 `UniMessage.image(url="file://...")`（2026-11-13 实测）：
      alconna 的 `url=` 导出成 OneBot 段时，`file://` 这种**本地文件 URI**
      不保证落在 image 段的 `file` 字段上。表现就是：
        日志里「网页排行取到 50 条」「已保存 xxx.jpg」都有，
        send() 也不报错，但用户那边一张图都收不到 —— 完全没有错误线索。
      这里直接拼 `MessageSegment.image("file://...")`，
      跟 petpet 的发图方式完全一致（那条链路是验证过的），
      NapCat 会直接读本地文件，比 base64 少一次编解码。
    """
    paths = await download_pixiv_imgs(img_urls, user_id)
    msg = OB11Message(caption)
    for p in paths:
        msg += MessageSegment.image(f"file://{p}")
    if not paths:
        msg += "（图片下载失败了，稍后再试）"
    try:
        # 这里**不设超时**：5 分钟预算统一由调用方 `_deliver()` 用 wait_for 管，
        # 两处都设会互相打架（内层先超时，就轮不到兜底逻辑了）。
        await pixiv_rank.send(msg)
    except Exception as e:  # noqa: BLE001
        # ⚠️ 超时有两种来源，必须都接住（2026-11-14 实测踩坑）：
        #   1. ActionFailed(retcode=1200) —— NapCat 自己回的「sendMsg 超时」
        #   2. **NetworkError("WebSocket call api send_msg timeout")** —— nonebot 自己的
        #      API 超时（默认 30 秒），大图必然先撞它
        # 只捕 ActionFailed 的话，NetworkError 会一路冒到 matcher 外层，
        # 日志里刷一大段红 traceback，用户还什么都收不到。
        if _is_send_timeout(e):
            logger.warning(
                f"[pixivrank] 发送超时（{type(e).__name__}），图通常几分钟后到达，按成功处理: {e}"
            )
            return
        raise


async def _deliver(
    caption: str, img_urls: list[str], user_id: str, session: str = "global"
) -> bool:
    """
    在 5 分钟预算内把图发出去；预算内没发完就随机推一张日榜兜底。

    串行化（用户要求）：
      `async with _session_lock(session)` 这一段**排队等待不算预算**——同一会话里
      前一张图没出去，后面的请求就停在这里，拿到锁之后才开始计时。
    返回 True 表示已经交付（正常图或兜底图），False 表示连兜底都失败。
    """
    lock = _session_lock(session)
    if lock.locked():
        logger.info(f"[pixivrank] 会话 {session} 前一张还在发，本次排队（排队不计入预算）")
    async with lock:
        try:
            await asyncio.wait_for(
                _send_imgs(caption, img_urls, user_id), timeout=SEND_TIMEOUT
            )
            return True
        except asyncio.TimeoutError:
            logger.warning(
                f"[pixivrank] 超过 {SEND_TIMEOUT:.0f} 秒仍未发完，改推一张随机日榜兜底"
            )
        except Exception as e:  # noqa: BLE001
            # 兜住一切非超时异常：绝不让它冒到 matcher 外层刷红 traceback
            logger.error(f"[pixivrank] 出图失败：{type(e).__name__}: {e}")
            return False

        # ---- 兜底：随便发一张 ----
        try:
            got = await _random_from_ranking()
            if not got:
                logger.warning("[pixivrank] 兜底取日榜也失败")
                return False
            fallback_caption, fallback_url = got
            await asyncio.wait_for(
                _send_imgs(
                    fallback_caption + "（搜图超时，随机推一张日榜）\n",
                    [fallback_url],
                    user_id,
                ),
                timeout=SEND_TIMEOUT,
            )
            return True
        except asyncio.TimeoutError:
            logger.warning("[pixivrank] 兜底图也超时，放弃")
            return False
        except Exception as e:  # noqa: BLE001
            logger.error(f"[pixivrank] 兜底出图失败：{type(e).__name__}: {e}")
            return False


pixiv_rank = on_alconna(
    rank,
    aliases={"P站排行", "p站排行榜", "P站排行榜"},
    priority=5,
    block=True,
    use_cmd_start=True,
    auto_send_output=True,
    skip_for_unmatch=False,
)
pixiv_search = on_alconna(
    search,
    # 命令就是 `/搜图`，不加四字别名（2026-11-14 用户明确要求：简化成两字）。
    # 注意：Alconna 的命令名只有 `搜图`，发 `/p站搜图` 不会被这个 matcher 接住，
    # 而是落到 plugins.chat 的 on_message 当普通聊天回一句 —— 看着像功能坏了。
    priority=5,
    block=True,
    use_cmd_start=True,
    skip_for_unmatch=False,
    auto_send_output=True,
)


@pixiv_rank.handle()
async def _(target: MsgTarget, rank: str, count: Match[int], date: Match[str], event: Event):

    code = 0
    text_list = ["失败了..."]
    if not count.available:
        _count = 1
    else:
        _count = count.result
    if "r18" in rank and not target.private:
        await UniMessage.text("羞羞脸！私聊里自己看！").finish(at_sender=True)
    if _ulmt.check(event.get_user_id()):
        await pixiv_rank.finish("P站排行榜正在搜索噢，不要重复触发命令呀")
    _ulmt.set_true(event.get_user_id())
    # 同 search handler：必须 try/finally，否则异常时用户会被限流标志永久卡死
    try:
        text_list, urls, code = await get_pixiv_urls(
            rank, _count, date.result if date.available else ""
        )
        if code != 200:
            await pixiv_rank.finish(text_list[0])
        if not text_list or not urls:
            await UniMessage.text("没有找到啊，等等再试试吧~V").finish(at_sender=True)
        # 出图交给 _deliver：串行排队 + 5 分钟预算 + 超时兜底随机图
        if not await _deliver(
            text_list[0], urls[0], event.get_user_id(), _session_key(event)
        ):
            await UniMessage.text("搜图超时，稍后再试试").finish(at_sender=True)
    finally:
        _ulmt.set_false(event.get_user_id())


@pixiv_search.handle()
async def _(
    target: MsgTarget,
    event: Event,
    tag: Any,
    r18: Query[bool] = Query("r18.value"),
):
    # tag 现在是 AllParam/MultiVar 的结果（元组或字符串），先拼成完整关键词
    keyword = _join_tag(tag)
    is_r18 = bool(r18.result)
    # AllParam 会把结尾的 r18 也吃进去，这里手动剥一下（否则会去搜「xxx r18」这个标签）
    if not is_r18 and keyword.lower().endswith(" r18"):
        keyword = keyword[:-4].strip()
        is_r18 = True
    if not keyword:
        await UniMessage.text("搜图后面要跟关键词呀，比如：/搜图 初音ミク").finish(at_sender=True)
    if is_r18 and not target.private:
        await UniMessage.text("(脸红#) 你不会害羞的 八嘎！").finish(at_sender=True)
    _r18 = 2 if is_r18 else 1
    if _ulmt.check(event.get_user_id()):
        await pixiv_rank.finish("P站关键词正在搜索噢，不要重复触发命令呀")
    _ulmt.set_true(event.get_user_id())
    # ⚠️ 必须 try/finally 释放限流标志：`_deliver` 里任何异常（比如发送超时抛
    #    NetworkError）冒出去时，如果走不到 set_false，这个用户就会被永久卡在
    #    「正在搜索噢，不要重复触发命令呀」，只能重启进程才能恢复。
    try:
        # 数量/排序参数已随 AllParam 改造去掉：一次只发 1 张，排序固定按热度
        text_list, urls, code = await search_pixiv_urls(keyword, 1, "popular", _r18)
        if code != 200:
            # ⚠️ 这里原来调 `pixiv_rank.finish(...)`（另一个 matcher），
            #    真机实测：日志有「拿不到详情」，群里却一条消息都没有。
            #    改成用当前 matcher 的 UniMessage 直接发，别再跨 matcher finish。
            await UniMessage.text(text_list[0] if text_list else "没有找到啊").finish(
                at_sender=True)
        if not text_list or not urls:
            await UniMessage.text("没有找到啊，等等再试试吧~V").finish(at_sender=True)
        # 出图交给 _deliver：串行排队 + 5 分钟预算 + 超时兜底随机图
        if not await _deliver(
            text_list[0], urls[0], event.get_user_id(), _session_key(event)
        ):
            await UniMessage.text("搜图超时，稍后再试试").finish(at_sender=True)
    finally:
        _ulmt.set_false(event.get_user_id())

# ============ 兼容「命令和参数粘在一起」：/搜图151768172 ============
# Alconna 要求命令后面有空格；粘在一起就匹配不到，落到 plugins.chat 变成普通聊天
# （用户 2026-11-19 实测：群里发 /搜图151768172，谁都不回）。
# 这里补一个兜底：只认「/搜图 + 纯数字作品 id」这一种形式，直接按 id 搜。
# ⚠️ 只认**没有空格**的写法：`/搜图 147954620` 必须继续走 Alconna 那条路，
#    否则会绕开它的 r18 选项和参数校验。
_GLUED_RE = re.compile(r"^[/／]搜图(\d{5,10})$")


def _glued_rule(event: Event) -> bool:
    try:
        return bool(_GLUED_RE.match(str(event.get_plaintext()).strip()))
    except Exception:
        return False


pixiv_search_glued = on_message(rule=Rule(_glued_rule), priority=5, block=True)


@pixiv_search_glued.handle()
async def _(event: Event):
    m = _GLUED_RE.match(str(event.get_plaintext()).strip())
    if not m:
        return
    illust_id = m.group(1)
    if _ulmt.check(event.get_user_id()):
        await UniMessage.text("P站正在搜索噢，不要重复触发命令呀").finish(at_sender=True)
    _ulmt.set_true(event.get_user_id())
    try:
        text_list, urls, code = await search_pixiv_urls(illust_id, 1, "popular", 1)
        if code != 200 or not urls:
            await UniMessage.text(
                text_list[0] if text_list else "没有找到这张图").finish(at_sender=True)
        if not await _deliver(text_list[0], urls[0], event.get_user_id(),
                              _session_key(event)):
            await UniMessage.text("搜图超时，稍后再试试").finish(at_sender=True)
    finally:
        _ulmt.set_false(event.get_user_id())
