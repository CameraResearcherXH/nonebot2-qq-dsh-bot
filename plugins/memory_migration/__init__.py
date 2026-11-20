# plugins/memory_migration/__init__.py
"""
两个独立的定时任务（**职责已拆分**）：

  ① 自我复盘 —— 每 2 天一次
     do_archive=False，只产出 journal（日记）+ impression（对某个人的核心印象）。
     这是「人格成长」，绝不能降频：一个月才更新一次印象，人设会僵化。

  ② 记忆归档 —— 每 30 天一次
     do_journal=False，只把过时记忆标掉（mark_outdated），让它们退出本地检索、
     交给算力机的「图书馆」去管。
     配合新的分工：30 天内 → 本地 memory_v2；30 天以前 → 图书馆 Agent。

真正的逻辑在 services/journal_agent.py：
  打包 data/（档案 + 相处印象 + 心情 + 记忆目录与详情 + 角色卡 + 近期日记）
  → POST {AGENT_BASE_URL}/agent/reflection（算力机 qwen2）
  → 按开关回写 journals/ + profile.long_term_impression + memory_v2 归档指令

（旧版本这里是手写的上传逻辑，路径写错到 data/memory/*.json、还漏了 import time，
  已整体替换为 journal_agent，不再维护两份。）
"""
from core import tuning
import os
import time

from nonebot import on_command, require, logger
from nonebot.adapters.onebot.v11 import MessageEvent

scheduler = require("nonebot_plugin_apscheduler").scheduler
from apscheduler.triggers.cron import CronTrigger

from services.journal_agent import run_reflection
from services.access import is_owner

# ---------- ① 自我复盘：每 2 天 20:00 ----------
REFLECT_DAY = os.getenv("REFLECT_DAY", "*/2")
REFLECT_HOUR = tuning.i("REFLECT_HOUR")
REFLECT_MINUTE = tuning.i("REFLECT_MINUTE")

# ---------- ② 记忆归档：每 30 天 03:00 ----------
# 用 CronTrigger(day="*/30") 会在每月 1/31 号跑，31 号那个月可能跳过，
# 所以归档用「距上次超 30 天」的判重更稳；这里用 day="1" 每月 1 号跑一次近似 30 天。
ARCHIVE_DAY = os.getenv("ARCHIVE_DAY", "1")
ARCHIVE_HOUR = tuning.i("ARCHIVE_HOUR")
ARCHIVE_MINUTE = tuning.i("ARCHIVE_MINUTE")
ARCHIVE_WINDOW_DAYS = tuning.f("ARCHIVE_WINDOW_DAYS")


async def do_reflect():
    """自我复盘：只要日记和印象，不动归档。"""
    logger.info("[REFLECT] 开始自我复盘（2 天一次）…")
    r = await run_reflection(do_archive=False)
    if r.get("ok"):
        logger.info(
            f"[REFLECT] 复盘完成：日记={r.get('journal') or '(无)'} "
            f"印象={r.get('impression') or '(无)'}"
        )
    else:
        logger.warning(f"[REFLECT] 复盘未完成：{r.get('error')}")


async def do_archive():
    """记忆归档：只要归档指令，不写日记/印象。"""
    since = time.time() - ARCHIVE_WINDOW_DAYS * 86400
    logger.info(f"[ARCHIVE] 开始记忆归档（近 {ARCHIVE_WINDOW_DAYS:.0f} 天窗口）…")
    r = await run_reflection(since_ts=since, do_journal=False)
    if r.get("ok"):
        logger.info(f"[ARCHIVE] 归档完成：标记过时 {r.get('archived', 0)} 条")
    else:
        logger.warning(f"[ARCHIVE] 归档未完成：{r.get('error')}")


scheduler.add_job(
    do_reflect,
    CronTrigger(day=REFLECT_DAY, hour=REFLECT_HOUR, minute=REFLECT_MINUTE),
    id="reflect_job",
    replace_existing=True,
)

scheduler.add_job(
    do_archive,
    CronTrigger(day=ARCHIVE_DAY, hour=ARCHIVE_HOUR, minute=ARCHIVE_MINUTE),
    id="archive_job",
    replace_existing=True,
)


# ================= 手动触发（调试 / 主人专属） =================
reflect_cmd = on_command("复盘", priority=5, block=True)


@reflect_cmd.handle()
async def _(event: MessageEvent):
    if not is_owner(event.user_id):
        await reflect_cmd.finish("只有主人能让我复盘哦。")

    await reflect_cmd.send("（闭上眼睛…我把这两天的记忆翻一遍）")
    r = await run_reflection(do_archive=False)
    if r.get("ok"):
        await reflect_cmd.finish(
            f"📓 日记：{r.get('journal') or '(算力机没写)'}\n"
            f"🎯 核心印象：{r.get('impression') or '(空)'}"
        )
    await reflect_cmd.finish(f"算力机没反应…{r.get('error')}")


archive_cmd = on_command("归档记忆", aliases={"记忆归档"}, priority=5, block=True)


@archive_cmd.handle()
async def _(event: MessageEvent):
    if not is_owner(event.user_id):
        await archive_cmd.finish("只有主人能归档记忆哦。")

    since = time.time() - ARCHIVE_WINDOW_DAYS * 86400
    await archive_cmd.send(f"（把近 {ARCHIVE_WINDOW_DAYS:.0f} 天的记忆翻出来归档…）")
    r = await run_reflection(since_ts=since, do_journal=False)
    if r.get("ok"):
        await archive_cmd.finish(f"🗂️ 标记过时：{r.get('archived', 0)} 条")
    await archive_cmd.finish(f"算力机没反应…{r.get('error')}")
