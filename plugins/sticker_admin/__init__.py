# plugins/sticker_admin/__init__.py
"""
表情库运维命令（不用登服务器）：

  /导入表情          把 data/memes 里的素材入库（按文件名分类，几秒）
  /导入表情 精细      入库后再让 DeepSeek 把「兜底到搞怪」的重新分类
  /清理表情          清掉 index 里「文件已不存在」的坏条目
  /表情健康          库存健康度一览

⚠️ 关键概念：`data/memes/` 里的文件只是**素材**，
   不入库（不写 index.json）就等于没有 —— 这是最容易踩的坑。
"""
from core import tuning
import asyncio
import os
import re
import time
from pathlib import Path

from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import (
    Bot, GroupMessageEvent, Message, MessageEvent,
)
from nonebot.params import CommandArg
from nonebot.rule import Rule

from services.access import is_owner
from services import sticker_importer as imp
from services.sticker_library import STICKER_MAX, count_importable, sticker_lib

import_cmd = on_command("导入表情", aliases={"导入表情包", "载入表情"}, priority=4, block=True)
clean_cmd = on_command("清理表情", aliases={"修复表情"}, priority=4, block=True)
health_cmd = on_command("表情健康", priority=4, block=True)
send_cmd = on_command("发表情", aliases={"表情测试", "试表情"}, priority=4, block=True)

# 能力开关（/技能 关 表情）
from services import skill_switch as _sk   # noqa: E402
for _c in (import_cmd, clean_cmd, health_cmd):
    _sk.gate(_c, "sticker_save", "表情")
_sk.gate(send_cmd, "sticker_send", "表情")


def _stat_line() -> str:
    h = sticker_lib.health()
    n_imp, where = count_importable()
    line = f"库存 {h['total']}/{STICKER_MAX} 张，能发 {h['usable']} 张"
    if h["broken_file"]:
        line += f"，坏数据 {h['broken_file']} 张"
    if n_imp:
        line += f"\n未入库素材 {n_imp} 张（{'、'.join(where)}）"
    return line


@import_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(event.user_id):
        await import_cmd.finish("只有主人能操作表情库哦。")

    raw_arg = str(args).strip()
    arg = raw_arg.lower()
    fine = ("精" in arg) or ("llm" in arg) or ("fine" in arg)
    copy_mode = ("复制" in raw_arg) or ("copy" in arg)

    # 支持四种写法：
    #   /导入表情                          → 自动找 data/memes
    #   /导入表情 data/memes               → 指定目录（原地索引）
    #   /导入表情 复制 data/uploads/xxx     → 复制进 data/stickers/（大图顺带缩小）
    #   /导入表情 精细                      → 自动找 + 入库后 LLM 精修
    src = None
    n = 0
    # 只去尾部斜杠，保留开头的 /（否则 /app/data/memes 会被切成相对路径）
    raw_path = raw_arg
    if copy_mode:
        raw_path = re.sub(r"(复制|copy)", "", raw_path, flags=re.I)
    raw_path = raw_path.replace("\\", "/").strip()
    if len(raw_path) > 1:
        raw_path = raw_path.rstrip("/")
    cand = Path(raw_path) if raw_path else None
    if cand and (not fine) and cand.exists() and cand.is_dir():
        src = cand
        n = sum(1 for f in src.rglob("*") if f.is_file() and f.suffix.lower() in imp.IMG_EXTS)
        if n == 0:
            src, n = None, 0

    if src is None:
        src, n = imp.find_source()
    if not src:
        await import_cmd.finish(
            "没找到素材目录。\n"
            "把表情包解压到 data/memes/ 再试（宿主机 ~/qq-grok-bot/data/memes）。\n"
            f"当前检查过：data/memes、data/sticker_src、data/表情包"
        )

    # ⚠️ data/uploads 是「/上传文件」的暂存区，可能被清理。从那儿导入必须**复制**
    #    进 data/stickers/，否则 index 指向的文件一被删，表情全变坏数据。
    _src_abs = str(Path(src).resolve()).replace("\\", "/").lower()
    in_place = (not copy_mode) and ("data/uploads" not in _src_abs)

    await import_cmd.send(
        f"📦 开始导入 {n} 张（{src}）\n"
        f"分类：先看文件夹名（疑惑→思考、问候→打招呼、生气→生气…），"
        f"再看文件名关键词\n"
        + ("入库方式：复制进 data/stickers/（大图顺带缩到 "
           f"{imp.STICKER_MAX_SIDE}px）\n" if not in_place
           else "入库方式：原地索引（文件留在原目录）\n")
        + ("入库后还会让 DeepSeek 精修一遍" if fine
           else "想更准可以发 /导入表情 精细")
    )

    # 入库是同步 IO（md5 + 可能复制文件），丢到线程里别卡事件循环
    try:
        res = await asyncio.to_thread(imp.import_dir, src, in_place=in_place,
                                      override_tag="")
    except Exception as e:
        await import_cmd.finish(f"❌ 导入失败：{type(e).__name__}: {e}")

    if not res.get("ok"):
        await import_cmd.finish(f"❌ {res.get('error')}")

    top = sorted(res["by_tag"].items(), key=lambda x: -x[1])[:8]
    lines = [
        f"✅ 入库完成：新增 {res['added']} 张，重复跳过 {res['dup']}，失败 {res['fail']}",
        f"分类：{'、'.join(f'{t}{c}' for t, c in top)}",
    ]
    if res.get("saved"):
        lines.append(f"压缩：省下 {res['saved'] / 1024 / 1024:.1f}MB（长边 ≤ "
                     f"{imp.STICKER_MAX_SIDE}px，GIF 不动）")
    lines += ["", _stat_line()]
    await import_cmd.send("\n".join(lines))

    # ---- 精细模式：让 DeepSeek 重分「兜底到搞怪」的 ----
    if fine:
        fallback = [(md5, info) for md5, info in sticker_lib.index.items()
                    if info.get("tag") == "搞怪" and info.get("path")]
        if not fallback:
            await import_cmd.finish("没有需要精修的（没有兜底到搞怪的表情）。")

        await import_cmd.send(f"🤖 让 DeepSeek 重分 {len(fallback)} 张兜底表情…（约 1 分钟）")
        try:
            from services import sticker_curator as cur
            names = [Path(i["path"]).name for _, i in fallback]
            mapping = await cur.classify_filenames(names, batch=40)
            moved = 0
            for md5, info in fallback:
                new_tag = mapping.get(Path(info["path"]).name)
                if new_tag and new_tag != "搞怪":
                    imp.retag(md5, new_tag)
                    moved += 1
            await import_cmd.finish(
                f"✅ 精修完成：{moved} 张换了标签\n\n{_stat_line()}"
            )
        except Exception as e:
            await import_cmd.finish(f"⚠️ 精修失败（已入库的部分不受影响）：{type(e).__name__}: {e}")


@clean_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    if not is_owner(event.user_id):
        await clean_cmd.finish("只有主人能操作表情库哦。")
    h = sticker_lib.health()
    n = sticker_lib.clean_broken()
    await clean_cmd.finish(
        f"🧹 清理完成：删掉 {n} 条坏数据\n"
        f"清理前 {h['total']} 条（可用 {h['usable']}，坏 {h['broken_file']}）\n\n"
        f"{_stat_line()}"
    )


@send_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    """
    手动让她发一张表情 —— 用来验证「发送链路通不通」，不依赖 DeepSeek 是否想发。
      /发表情            随机标签（走 pick_any）
      /发表情 搞怪       指定标签
    不带参数时直接发一张，然后回一条说明挑了哪张、走哪条路。
    """
    if not is_owner(event.user_id):
        await send_cmd.finish("只有主人能让我发表情哦。")

    tag = str(args).strip()
    from services.sticker_sender import send_sticker_test

    res = await send_sticker_test(bot, event, tag)
    if res["ok"]:
        await send_cmd.finish(
            f"✅ 发出去了（请求标签 {tag or '随机'} → 实际 {res['tag']}）\n"
            f"文件：{res['file'] or res['mface']}\n"
            f"挑选路径：{'、'.join(res['tried']) or '—'}\n\n"
            f"{_stat_line()}"
        )
    await send_cmd.finish(f"❌ 没发出去：{res['error']}")


@health_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    h = sticker_lib.health()
    lines = [
        "🩺 表情库健康度",
        f"· 条目总数：{h['total']}（上限 {STICKER_MAX}）",
        f"· 能发出去：{h['usable']}",
        f"· 坏文件：{h['broken_file']}",
        f"· 商城表情：{h['mface']}",
    ]
    if h["broken_ids"]:
        lines.append("")
        lines.append("坏条目（文件已不存在）：")
        for md5 in h["broken_ids"][:5]:
            info = sticker_lib.index.get(md5) or {}
            lines.append(f"  {md5[:12]}… tag={info.get('tag')} path={info.get('path')}")

    n_imp, where = count_importable()
    if n_imp:
        lines.append("")
        lines.append(f"📦 未入库素材 {n_imp} 张：{'、'.join(where)}")
        lines.append("   → 发 /导入表情 入库")

    if h["usable"] == 0:
        lines.append("")
        lines.append("⚠️ 一张都发不出去，先 /清理表情 再 /导入表情")

    await health_cmd.finish("\n".join(lines))


# ================= 异步收图/识图（先下指令，之后再发图，像音频分离那样） =================
# 用户 2026-11-17 反馈：/识表情 必须「指令和图一起发」很别扭，改成：
#   先发 /识表情 或 /收表情 或「收一下这个表情，标签打为X」→ 她回「发图来」→ 再单独发图。
_STICKER_WAIT: dict = {}          # user_id -> {"mode": "identify"|"collect", "tag": str, "ts": float}
_STICKER_WAIT_TTL = tuning.f("STICKER_WAIT_TTL")
_COLLECT_WORDS = ("收表情", "存表情", "收藏表情", "收图", "收照片", "收图片",
                   "收这张", "存这张")
_TAG_FROM_TEXT_RE = re.compile(
    r"(?:打为|打成|写成|记为|标记为|标记成|标为|标成|分成|分到|标签[:：为])"
    r"\s*([\u4e00-\u9fa5A-Za-z]{1,8})"
)


def _ext_of(data: bytes) -> str:
    """按 magic bytes 猜图片扩展名（动图 GIF 也要收得下）。"""
    if data[:6] in (b"GIF89a", b"GIF87a"):
        return ".gif"
    if data[:4] == b"\x89PNG":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    return ".png"


def _norm_tag(word: str) -> str:
    """心情词原样返回（自由标签，不再强制归一到固定词表）。"""
    return str(word or "").strip()


def _tag_from_text(text: str) -> str:
    m = _TAG_FROM_TEXT_RE.search(str(text or ""))
    return m.group(1) if m else ""


def _has_image(event) -> bool:
    return any(s.type == "image" for s in event.get_message())


async def _pick_image_bytes(bot: Bot, event: MessageEvent) -> bytes | None:
    seg = next((s for s in event.get_message() if s.type == "image"), None)
    if seg is None:
        return None
    try:
        from plugins.vision_agent import _fetch_image_bytes
        return await _fetch_image_bytes(bot, event, seg)
    except Exception:
        return None


async def _do_identify(bot: Bot, event: MessageEvent, img: bytes, tag_hint: str = ""):
    from services.sticker_receiver import classify_sticker_by_vision
    tag, desc = await classify_sticker_by_vision(img)
    if not desc:
        await bot.send(event, "识别失败（视觉模型没返回内容），看 docker logs 的 [STICKER]/[VISION] 行。")
        return
    lines = [f"标签：{tag}", f"画面：{desc}"]
    if tag_hint:
        lines.append(f"你说的心情词：{tag_hint}")
    await bot.send(event, "\n".join(lines))


async def _do_collect(bot: Bot, event: MessageEvent, img: bytes, tag: str = ""):
    from services.sticker_receiver import classify_sticker_by_vision
    auto_tag, desc = await classify_sticker_by_vision(img)
    # 自由标签：用户/她写了心情词就原样入库；没写才用视觉建议
    final_tag = _norm_tag(tag) or auto_tag or "吃瓜"
    r = sticker_lib.add(data=img, tag=final_tag, chat_id=event.get_session_id(),
                        ext=_ext_of(img), desc=desc, quality=0.5)
    if r.get("ok"):
        await bot.send(event, f"✅ 收了这张表情 → {final_tag}（{desc or '无描述'}）")
    else:
        await bot.send(event, f"没存进去：{r.get('reason')}")


# ================= /识表情（用户给图 → 让她分标签） =================
# 这条是「用户主动给图让她分类」的通路，和被动收表情走的是**同一个视觉分类函数**
# （services.sticker_receiver.classify_sticker_by_vision），所以测通了它，就等于测通了打标签链路。
tag_cmd = on_command("识表情", aliases={"分标签", "表情识别", "看图打标签"}, priority=4, block=True)
_sk.gate(tag_cmd, "sticker_save", "表情")


@tag_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    image_bytes = None

    # ① 本消息里直接带图
    seg = next((s for s in event.get_message() if s.type == "image"), None)
    if seg is not None:
        try:
            from plugins.vision_agent import _fetch_image_bytes
            image_bytes = await _fetch_image_bytes(bot, event, seg)
        except Exception:
            image_bytes = None

    # ② 本消息没图 → 看是不是引用回复了一张图
    if image_bytes is None:
        rid = ""
        for s in event.get_message():
            if s.type == "reply":
                rid = str((s.data or {}).get("id") or "").strip()
                break
        if rid:
            try:
                d = await bot.call_api("get_msg", message_id=int(rid))
                for s in (d.get("message") or []):
                    if not isinstance(s, dict) or s.get("type") != "image":
                        continue
                    url = str((s.get("data") or {}).get("url") or "").strip()
                    if not url:
                        continue
                    import httpx
                    async with httpx.AsyncClient(timeout=15) as c:
                        r = await c.get(url)
                    if r.status_code == 200 and r.content:
                        image_bytes = r.content
                    break
            except Exception:
                pass

    if not image_bytes:
        # 改成异步等待：先下指令，之后再把图发过来（不用再带命令）
        _STICKER_WAIT[str(event.user_id)] = {"mode": "identify", "tag": "", "ts": time.time()}
        await tag_cmd.finish(
            f"好，把图直接发过来（不用再带命令），"
            f"{_STICKER_WAIT_TTL / 60:.0f} 分钟内有效。"
        )

    await tag_cmd.send("👁️ 让我看看这张图…")
    try:
        from services.sticker_receiver import classify_sticker_by_vision, coerce_tag
        tag, desc = await classify_sticker_by_vision(image_bytes)
    except Exception as e:
        await tag_cmd.finish(f"❌ 识图异常：{type(e).__name__}: {e}")

    if not desc:
        await tag_cmd.finish("识别失败（视觉模型没返回内容），看 docker logs 的 [STICKER]/[VISION] 行。")

    lines = [f"标签：{tag}", f"画面：{desc}"]
    raw = str(args).strip()
    if raw:
        ctag = coerce_tag(raw)
        if ctag:
            lines.append(f"你说的「{raw}」→ 表情库标签：{ctag}")
    await tag_cmd.finish("\n".join(lines))


# ================= /收表情（收集指定/自动标签的表情） =================
collect_cmd = on_command("收表情", aliases={"存表情", "收藏表情"}, priority=4, block=True)
_sk.gate(collect_cmd, "sticker_save", "表情")


@collect_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(event.user_id):
        await collect_cmd.finish("只有主人能操作表情库哦。")
    tag = _norm_tag(str(args).strip())
    img = await _pick_image_bytes(bot, event)
    if img:
        await _do_collect(bot, event, img, tag)
        return
    _STICKER_WAIT[str(event.user_id)] = {"mode": "collect", "tag": tag, "ts": time.time()}
    await collect_cmd.finish(
        f"好，把要收的表情发过来（图片/动图都行）…\n"
        + (f"标签：{tag}" if tag else "标签我按画面自己打")
        + f"，{_STICKER_WAIT_TTL / 60:.0f} 分钟内有效。"
    )


# ================= 等待用户发图（消费 /识表情、/收表情、自然语言设置的等待） =================
def _sticker_wait_rule(event: MessageEvent) -> bool:
    info = _STICKER_WAIT.get(str(event.user_id))
    if not info:
        return False
    if time.time() - float(info.get("ts") or 0) > _STICKER_WAIT_TTL:
        _STICKER_WAIT.pop(str(event.user_id), None)
        return False
    return _has_image(event)


sticker_wait = on_message(rule=Rule(_sticker_wait_rule), priority=1, block=True)


@sticker_wait.handle()
async def _(bot: Bot, event: MessageEvent):
    info = _STICKER_WAIT.pop(str(event.user_id), None) or {}
    img = await _pick_image_bytes(bot, event)
    if not img:
        await bot.send(event, "没拿到图，重发一次试试。")
        return
    if info.get("mode") == "identify":
        await _do_identify(bot, event, img, info.get("tag") or "")
    else:
        await _do_collect(bot, event, img, info.get("tag") or "")


# ================= 自然语言「收表情，标签打为X」 =================
_TAG_WORDS = ("标签", "标记", "标为", "标成", "写成", "记为", "分成", "打为", "打成")
# 「收/存」在正常聊天里到处都是（收到、收拾、存档、收藏夹…），
# 光看这两个字会把聊天当成指令（用户 2026-11-19 实测：群里突然冒出一句
# 「好呀，把表情发给我吧」，前面 50 条根本没有相关话题）。
_NEG_RE = re.compile(r"(别|不要|不用|甭|先不|算了)[^，。！？]{0,6}(收|存|收藏)")
_OBJ_WORDS = ("表情", "贴图", "梗图", "表情包", "图片", "动图", "meme",
              "这张图", "那张图", "截图")


def _collect_intent(text: str) -> bool:
    """
    这句是不是「收表情」的指令（自然语言版）。

    比老版本严得多，因为它是 `block=True` 的：一旦误判，她会突然在群里
    冒出一句「好呀，把表情发过来吧」，而群友根本没在说这个。
    三道门：① 明确词直接算；② 必须有「表情/图」这个对象；③ 开头就是祈使动词 + 标签词。
    """
    t = str(text or "").strip()
    if not t or len(t) > 60:
        return False
    if _NEG_RE.search(t):                      # 「别收了」「先不存」
        return False
    if t.endswith(("吗", "？", "?")):           # 「你收到了吗」是提问，不是指令
        return False
    if any(w in t for w in _COLLECT_WORDS):    # 收表情 / 存表情 / 收这张…
        return True
    has_verb = any(w in t for w in ("收", "存", "收藏"))
    if not has_verb:
        return False
    if any(w in t for w in _OBJ_WORDS):        # 「这张表情收一下」
        return True
    # 「收一下，标签写成忠诚」：动词必须在**开头**（祈使），不能出现在句子中间
    if any(w in t for w in _TAG_WORDS) and re.match(r"^[把帮给麻]?(收|存|收藏)", t):
        return True
    return False


def _collect_intent_rule(event: MessageEvent) -> bool:
    if not is_owner(event.user_id):
        return False
    # 群里必须是在跟她说话（@ 她）。群里聊到「收」「存」「标签」太常见了，
    # 不要求 @ 的话她会在别人正常聊天时插一句「好呀，把表情发过来吧」。
    try:
        if isinstance(event, GroupMessageEvent) and not event.is_tome():
            return False
    except Exception:
        pass
    return _collect_intent(event.get_plaintext())


sticker_intent = on_message(rule=Rule(_collect_intent_rule), priority=2, block=True)


@sticker_intent.handle()
async def _(bot: Bot, event: MessageEvent):
    txt = (event.get_plaintext() or "").strip()
    tag = _norm_tag(_tag_from_text(txt))
    img = await _pick_image_bytes(bot, event)
    if img:
        await _do_collect(bot, event, img, tag)
        return
    _STICKER_WAIT[str(event.user_id)] = {"mode": "collect", "tag": tag, "ts": time.time()}
    await bot.send(
        event,
        f"好呀，把表情发过来吧（{'标签：' + tag if tag else '我按画面自己打标签'}），"
        f"{_STICKER_WAIT_TTL / 60:.0f} 分钟内有效。",
    )
