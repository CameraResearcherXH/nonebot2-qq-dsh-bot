# services/llm_usage.py
"""token 用量归因：按 purpose 统计，回答「哪部分消耗最大 / 最小」。

为什么单独一个模块：
- `services/api_usage.py` 只记了主对话一处（llm.py:3059），
  判读/审核/预检/压缩/表达/挖矿/识图/识曲等辅助调用全都没记，
  所以以前根本回答不了「谁最贵」。
- 这里统一 `record(purpose=...)`，报表按 purpose 聚合，给出 Top/Bottom。

存储：`data/llm_usage.db`（sqlite，WAL），保留 `LLM_USAGE_KEEP_DAYS` 天。
"""
from core import tuning
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(os.getenv("LLM_USAGE_DB", "data/llm_usage.db"))
KEEP_DAYS = tuning.i("LLM_USAGE_KEEP_DAYS")
DAILY_BUDGET = tuning.f("LLM_DAILY_BUDGET")  # 0 = 不告警

PURPOSES = (
    "chat", "tool_loop", "audit", "preflight", "judge", "conv_state",
    "expression", "profile_miner", "vision", "link_preview", "sticker",
    "song", "other",
)

_LOCK = threading.Lock()
_INITED = {"done": False}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return c


def init() -> None:
    if _INITED["done"]:
        return
    with _LOCK:
        if _INITED["done"]:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = _conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS usage (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts REAL NOT NULL,
              day TEXT NOT NULL,
              provider TEXT NOT NULL DEFAULT '',
              model TEXT NOT NULL DEFAULT '',
              purpose TEXT NOT NULL DEFAULT 'other',
              prompt INTEGER NOT NULL DEFAULT 0,
              completion INTEGER NOT NULL DEFAULT 0,
              cache_hit INTEGER NOT NULL DEFAULT 0,
              latency_ms INTEGER NOT NULL DEFAULT 0,
              session TEXT NOT NULL DEFAULT '',
              turn_id TEXT NOT NULL DEFAULT '',
              ok INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_usage_day ON usage(day, purpose);
            CREATE INDEX IF NOT EXISTS idx_usage_purpose ON usage(purpose, ts);
            """
        )
        c.commit()
        c.close()
        _INITED["done"] = True


def _price_of(model: str):
    raw = os.getenv("LLM_PRICES", "").strip()
    if not raw:
        return None
    try:
        table = json.loads(raw)
        return table.get(model) or table.get("*")
    except Exception:
        return None


def record(*, provider: str = "", model: str = "", purpose: str = "other",
           prompt: int = 0, completion: int = 0, cache_hit: int = 0,
           latency_ms: int = 0, session: str = "", turn_id: str = "",
           ok: bool = True) -> None:
    try:
        init()
        p = purpose if purpose in PURPOSES else "other"
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        with _LOCK:
            c = _conn()
            c.execute(
                """INSERT INTO usage
                   (ts,day,provider,model,purpose,prompt,completion,cache_hit,
                    latency_ms,session,turn_id,ok)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (now, day, str(provider), str(model), p, int(prompt or 0),
                 int(completion or 0), int(cache_hit or 0), int(latency_ms or 0),
                 str(session), str(turn_id), 1 if ok else 0))
            c.commit()
            c.close()
    except Exception:
        pass


def record_from_response(data, *, purpose: str = "other", provider: str = "",
                         model: str = "", latency_ms: int = 0,
                         session: str = "", turn_id: str = "") -> None:
    """从 OpenAI 兼容响应里抽 usage（deepseek/xai/glm 都兼容）。"""
    try:
        u = (data or {}).get("usage") or {}
        hit = int(u.get("prompt_cache_hit_tokens") or 0)
        prompt = int(u.get("prompt_tokens") or 0) or hit + int(
            u.get("prompt_cache_miss_tokens") or 0)
        completion = int(u.get("completion_tokens") or 0)
        if prompt or completion:
            record(provider=provider, model=model, purpose=purpose,
                   prompt=prompt, completion=completion, cache_hit=hit,
                   latency_ms=latency_ms, session=session, turn_id=turn_id)
    except Exception:
        pass


def _rows(days: int = 1):
    init()
    since = time.time() - max(1, int(days)) * 86400
    c = _conn()
    rows = c.execute(
        """SELECT purpose, model,
                  count(*) n, sum(prompt) p, sum(completion) c,
                  sum(cache_hit) h, avg(latency_ms) lat
           FROM usage WHERE ts>=? GROUP BY purpose, model""",
        (since,)).fetchall()
    c.close()
    return rows


def snapshot(days: int = 1) -> dict:
    """聚合一次，文本报表和图表共用，避免两套口径。"""
    rows = _rows(days)
    agg = {}
    models = {}
    for r in rows:
        d = agg.setdefault(r["purpose"], {"n": 0, "p": 0, "c": 0, "h": 0, "lat": []})
        d["n"] += r["n"]; d["p"] += r["p"] or 0; d["c"] += r["c"] or 0
        d["h"] += r["h"] or 0; d["lat"].append(r["lat"] or 0)
        m = str(r["model"] or "?")
        md = models.setdefault(m, {"n": 0, "p": 0, "c": 0, "purposes": {}})
        md["n"] += r["n"]; md["p"] += r["p"] or 0; md["c"] += r["c"] or 0
        md["purposes"][r["purpose"]] = md["purposes"].get(r["purpose"], 0) + (r["p"] or 0) + (r["c"] or 0)
    total = sum(d["p"] + d["c"] for d in agg.values()) or 1
    order = sorted(agg.items(), key=lambda kv: -(kv[1]["p"] + kv[1]["c"]))
    return {"rows": rows, "agg": agg, "models": models,
            "total": total, "order": order}


def report_text(days: int = 1) -> str:
    snap = snapshot(days)
    rows = snap["rows"]
    if not rows:
        return "最近 %d 天没有用量记录。" % days
    models = snap["models"]
    total = snap["total"]
    order = snap["order"]
    lines = ["▍token 用量（最近 %d 天）" % days,
             "总 token：%d" % total]
    for purpose, d in order:
        tok = d["p"] + d["c"]
        pct = tok / total * 100
        cache = (d["h"] / d["p"] * 100) if d["p"] else 0
        lines.append("· %-14s %7d（%4.1f%%）｜prompt %d / completion %d"
                     "｜缓存命中 %.0f%%｜平均 %.0fms"
                     % (purpose, tok, pct, d["p"], d["c"], cache,
                        sum(d["lat"]) / max(1, len(d["lat"]))))
    # 按模型拆开：三方判读 / 审核用的是独立模型，只看 purpose 会以为「没记到」
    if models:
        lines.append("按模型：")
        for m, d in sorted(models.items(), key=lambda kv: -(kv[1]["p"] + kv[1]["c"]))[:8]:
            tops = "、".join("%s:%d" % (k, v) for k, v in
                             sorted(d["purposes"].items(), key=lambda kv: -kv[1])[:3])
            lines.append("  · %-22s %7d（%s）" % (m[:22], d["p"] + d["c"], tops))
    top = order[:5]
    bottom = order[-5:][::-1]
    lines.append("最贵 Top5：" + "、".join(
        "%s(%d)" % (k, v["p"] + v["c"]) for k, v in top))
    lines.append("最省 Bottom5：" + "、".join(
        "%s(%d)" % (k, v["p"] + v["c"]) for k, v in bottom))
    if DAILY_BUDGET > 0:
        today = time.strftime("%Y-%m-%d")
        init()
        c = _conn()
        r = c.execute("SELECT sum(prompt+completion) FROM usage WHERE day=?",
                      (today,)).fetchone()
        c.close()
        used = int(r[0] or 0)
        lines.append("今日预算：%d / %d（%.0f%%）"
                     % (used, DAILY_BUDGET, used / DAILY_BUDGET * 100))
    return "\n".join(lines)


def budget_exceeded() -> bool:
    if DAILY_BUDGET <= 0:
        return False
    try:
        init()
        today = time.strftime("%Y-%m-%d")
        c = _conn()
        r = c.execute("SELECT sum(prompt+completion) FROM usage WHERE day=?",
                      (today,)).fetchone()
        c.close()
        return int(r[0] or 0) >= DAILY_BUDGET
    except Exception:
        return False


def status_text() -> str:
    return report_text(1)
