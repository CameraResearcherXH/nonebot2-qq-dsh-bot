# services/sticker_sender.py
import os
import base64
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from services.sticker_library import sticker_lib
from services.sticker_receiver import bump_sent

REPLY_FALLBACK_TAG = {
    "开心": "嘲笑", "嘲笑": "开心", "无语": "困",
    "委屈": "哭", "生气": "嘲笑", "困": "无语",
    "撒娇": "开心", "难过": "哭", "思考": "无语",
    "搞怪": "嘲笑", "打招呼": "开心", "吃瓜": "无语",
    "惊讶": "嘲笑", "害羞": "撒娇", "点赞": "开心",
    "狗头": "嘲笑",
}


def _send_image(bot: Bot, event: MessageEvent, path: str) -> tuple:
    """读本地图片并转成 base64 URI，返回 (是否成功, uri)。"""
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        return True, f"base64://{b64}"
    except Exception as e:
        logger.warning(f"[STICKER] 读图失败 {path}: {e}")
        return False, ""


def _pick(tag: str) -> tuple:
    """
    挑一张：精确标签 → 自由词归一化 → 近义标签 → 库内标签互含。
    返回 (pick, tried)。

    指定了 tag 但全落空时**不随机兜底**（用户 2026-11-18：库里没这个标签就明说，别乱发别的）；
    只有 tag 为空（纯随机发）时才 pick_any。
    """
    pick = None
    tried = []
    if tag:
        pick = sticker_lib.pick_by_tag(tag)
        if pick:
            tried.append(tag)
        else:
            # 自由心情词（得意洋洋/最强）→ 先归一化到标准标签（得意）
            std = ""
            try:
                from services.sticker_receiver import coerce_tag
                std = coerce_tag(tag) or ""
            except Exception:
                std = ""
            if std and std != tag:
                pick = sticker_lib.pick_by_tag(std)
                if pick:
                    tried.append(f"{tag}→{std}")
            # 近义表（REPLY_FALLBACK_TAG 用标准标签做 key）
            if not pick:
                alt = REPLY_FALLBACK_TAG.get(std or tag)
                if alt:
                    pick = sticker_lib.pick_by_tag(alt)
                    if pick:
                        tried.append(f"{tag}→{alt}")
            # 库内标签和自由词互相包含（得意洋洋 ↔ 得意）
            if not pick:
                pick = sticker_lib.pick_by_fuzzy(tag)
                if pick:
                    tried.append(f"{tag}→{pick.get('tag')}")
        return pick, tried

    # tag 为空 → 随便一张能发的（不是随便一条 index，否则会挑到坏数据）
    pick = sticker_lib.pick_any()
    if pick:
        tried.append("随机兜底")
    return pick, tried


async def _deliver(bot: Bot, event: MessageEvent, pick: dict) -> bool:
    """把挑好的那一张真正发出去。mface 优先，其次 base64 图片。"""
    if pick.get("mface_id"):
        try:
            await bot.send(event, MessageSegment.mface(pick["mface_id"]))
            bump_sent()
            return True
        except Exception as e:
            logger.warning(f"[STICKER] 发 mface 失败（id={pick['mface_id']}），改发图片: {e}")

    path = pick.get("path")
    if not path or not os.path.exists(path):
        logger.warning(f"[STICKER] 挑到的表情文件不存在: {path!r}，本条跳过")
        return False

    ok, uri = _send_image(bot, event, path)
    if not ok:
        return False
    try:
        await bot.send(event, MessageSegment.image(uri))
        bump_sent()
        return True
    except Exception as e:
        logger.warning(f"[STICKER] 发图失败: {type(e).__name__}: {e}")
        return False


async def send_sticker_by_tag(bot: Bot, event: MessageEvent, tag: str) -> bool:
    """
    按标签发一张表情：精确标签 → 近义标签 → 随便一张能发的。
    每一步都只从「真的能发出去」的条目里挑（pick_by_tag / pick_any 内部已过滤）。
    """
    if not sticker_lib.index:
        logger.debug("[STICKER] 表情库空，跳过")
        return False

    pick, tried = _pick(tag)
    if not pick:
        if tag:
            logger.info(f"[STICKER] 没有「{tag}」这个标签的表情（{tried}），不随机兜底")
            try:
                await bot.send(event, f"没有「{tag}」这个标签的表情哦。")
            except Exception:
                pass
        else:
            h = sticker_lib.health()
            logger.warning(
                f"[STICKER] 没有可发的表情（库存 {h['total']} 条，"
                f"可用 {h['usable']} 条，坏文件 {h['broken_file']} 条）"
                f"—— 发 /清理表情 清掉坏数据"
            )
        return False

    logger.info(f"[STICKER] 发表情 tag={tag!r} → {pick.get('tag')!r}（{tried}）")
    return await _deliver(bot, event, pick)


async def send_sticker_test(bot: Bot, event: MessageEvent, tag: str = "") -> dict:
    """
    给 /发表情 测试命令用：发一张并回报细节（挑了哪张、走哪条路、失败原因）。
    返回 {ok, tag, file, path, mface, tried, error}
    """
    info = {"ok": False, "tag": tag, "file": "", "path": "", "mface": "",
            "tried": [], "error": ""}
    if not sticker_lib.index:
        info["error"] = "表情库是空的，先发 /导入表情"
        return info

    pick, tried = _pick(tag)
    info["tried"] = tried
    if not pick:
        if tag:
            info["error"] = f"没有「{tag}」这个标签的表情"
        else:
            h = sticker_lib.health()
            info["error"] = (
                f"挑不到能发的（库存 {h['total']}，可用 {h['usable']}，"
                f"坏文件 {h['broken_file']}）"
            )
        return info

    info["tag"] = str(pick.get("tag") or "")
    info["path"] = str(pick.get("path") or "")
    info["file"] = os.path.basename(info["path"]) if info["path"] else ""
    info["mface"] = str(pick.get("mface_id") or "")
    logger.info(f"[STICKER] 测试发表情 tag={tag!r} → {pick.get('tag')!r}（{tried}）")
    info["ok"] = await _deliver(bot, event, pick)
    if not info["ok"]:
        info["error"] = "发送接口报错（见 docker logs 的 [STICKER] 行）"
    return info
