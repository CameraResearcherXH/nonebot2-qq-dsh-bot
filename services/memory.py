# services/memory.py
import os
import sqlite3
import time
from pathlib import Path

# 数据库存放到 data 目录下
DB_PATH = Path("data/memory.db")
DB_PATH.parent.mkdir(exist_ok=True)

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS histories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_key TEXT,
                role TEXT,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # 建立索引加速查询
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON histories(session_key)")

init_db()

def add_memory(session_key: str, role: str, content: str):
    """保存对话记忆"""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO histories (session_key, role, content) VALUES (?, ?, ?)", 
                     (session_key, role, content))

# 每天自动「新开对话」：只取当天的短历史。
# 用户 2026-11-11：「把每次新开对话变成每日新开一次」。
# 为什么不在 session_key 上加日期：session_key 还用于消息队列、focus、任务路由，
# 改它会牵一发动全身。在查询层按天过滤，效果一样而且安全。
# 旧记录**不删**——长期记忆检索（memory_v2）还要用。
CHAT_HISTORY_DAILY = os.getenv("CHAT_HISTORY_DAILY", "true").lower() == "true"


def get_memory(session_key: str, limit: int = 10) -> list:
    """
    获取最近的 N 条对话记忆（按时间正序）。

    CHAT_HISTORY_DAILY=true（默认）→ 只取**今天**的，等于每天开一个新对话。
    """
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        # 倒序查出最新的 limit 条，然后反转成正序
        if CHAT_HISTORY_DAILY:
            cur.execute(
                "SELECT role, content FROM histories "
                "WHERE session_key = ? "
                "  AND date(timestamp, 'localtime') = date('now', 'localtime') "
                "ORDER BY id DESC LIMIT ?",
                (session_key, limit),
            )
        else:
            cur.execute(
                "SELECT role, content FROM histories WHERE session_key = ? "
                "ORDER BY id DESC LIMIT ?",
                (session_key, limit),
            )
        rows = cur.fetchall()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def _ts_local(s) -> float:
    """SQLite 的 CURRENT_TIMESTAMP 是 **UTC**，转成本地时间戳。"""
    try:
        return time.mktime(time.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S")) - time.timezone
    except Exception:
        return 0.0


def get_recent_rows(session_key: str, limit: int = 12, days: int = 2) -> list:
    """
    最近 N 条对话，**带时间戳**。给「短期上下文」用。

    为什么单独一个函数：
      `get_memory` 只返回 role/content，没有时间 → 拼出来的上下文没有时间标记，
      她分不清「这是刚才说的」还是「昨天说的」。
      用户 2026-11-11 特别强调过：上下文的时间标记、人标记不能漏。

    days 默认 2 天（不是 1 天）：凌晨刚过 0 点时，「今天」只有几分钟记录，
    上一句还在「昨天」里 —— 那时候断片最明显。
    """
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT role, content, timestamp FROM histories "
            "WHERE session_key = ? "
            "  AND datetime(timestamp) >= datetime('now', ?) "
            "ORDER BY id DESC LIMIT ?",
            (session_key, f"-{max(1, int(days))} day", max(1, int(limit))),
        )
        rows = cur.fetchall()
    return [
        {"role": r[0], "content": r[1], "ts": _ts_local(r[2])}
        for r in reversed(rows)
    ]

# （原 add_system_event 已删：空壳实现、0 引用，接线审计确认为死代码。
#   系统事件现在走 memory_v2.remember，不需要这个占位。）
