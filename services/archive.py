# services/archive.py
"""个人档案库（C 方案，2026-11-26 用户拍板）。

边界（和图书馆分开，别混）：
  · **记忆** = 本地 sqlite `memories`（近 30 天，按人/时间/情绪/标签检索，毫秒级）；
  · **个人档案库** = 本模块（`data/archive.db`，只存 **30 天以前**、已经从近期记忆
    滑出去的旧事；只她自己能查，不对外、不进 RAG）；
  · **图书馆** = 可选的外部语料/书（RAG，另一套索引）。三者互不替代。

为什么单独一个库：
  · 用户 2026-11-26：「RAG 独立出来做图书馆，我们的记忆是我们自己的检索；
    以后提交，不一定所有人都搭 RAG。」——档案库必须**不依赖 RAG**，本地就能跑；
  · 不和 `memories` 混：近 30 天要快（BM25 + 结构化过滤），旧档案要能扫很多年，
    分开后遗忘曲线/命中回补不会互相污染；
  · 只她自己能查：没有 HTTP 端口，只在 `reply_pipeline` / `[[ARCHIVE|词]]` 里用。

存储：sqlite + FTS5（`unicode61`，写入时 bigram+单字 token，和 memdb 同一套分词）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("ARCHIVE_DB", "data/archive.db"))
ENABLED = bool(tuning.i("ARCHIVE_ENABLED"))
MIN_AGE_DAYS = max(1, tuning.i("ARCHIVE_MIN_AGE_DAYS"))
TOP = max(1, tuning.i("ARCHIVE_TOP"))
MAX_CHARS = max(120, tuning.i("ARCHIVE_MAX_CHARS"))

_LOCAL = threading.local()
_LOCK = threading.Lock()
_INITED = {"ok": False}

_WORD = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def enabled() -> bool:
    return bool(ENABLED)


def tokenize(text: str) -> list:
    """和 `memdb.tokenize` 同一套：英文/数字整词 + 中文 bigram + 单字。"""
    t = (text or "").lower()
    out = list(_WORD.findall(t))
    cjk = [c for c in t if _CJK.match(c)]
    out.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    out.extend(cjk)
    return out


def _conn() -> sqlite3.Connection:
    c = getattr(_LOCAL, "conn", None)
    if c is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        _LOCAL.conn = c
    return c


def init() -> None:
    if _INITED["ok"]:
        return
    with _LOCK:
        if _INITED["ok"]:
            return
        c = _conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS archive (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              hash TEXT UNIQUE,
              ts REAL NOT NULL,
              gid TEXT NOT NULL DEFAULT '',
              qq TEXT NOT NULL DEFAULT '',
              who TEXT NOT NULL DEFAULT '',
              kind TEXT NOT NULL DEFAULT 'chat',
              text TEXT NOT NULL,
              tokens TEXT NOT NULL DEFAULT '',
              meta TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_arc_ts ON archive(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_arc_qq ON archive(qq, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_arc_gid ON archive(gid, ts DESC);
            CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(
              tokens, content='archive', content_rowid='id', tokenize='unicode61'
            );
            CREATE TRIGGER IF NOT EXISTS archive_ai AFTER INSERT ON archive BEGIN
              INSERT INTO archive_fts(rowid, tokens) VALUES (new.id, new.tokens);
            END;
            CREATE TRIGGER IF NOT EXISTS archive_ad AFTER DELETE ON archive BEGIN
              INSERT INTO archive_fts(archive_fts, rowid, tokens)
                VALUES ('delete', old.id, old.tokens);
            END;
            CREATE TRIGGER IF NOT EXISTS archive_au AFTER UPDATE ON archive BEGIN
              INSERT INTO archive_fts(archive_fts, rowid, tokens)
                VALUES ('delete', old.id, old.tokens);
              INSERT INTO archive_fts(rowid, tokens) VALUES (new.id, new.tokens);
            END;
            """
        )
        c.commit()
        _INITED["ok"] = True


def _hash(gid: str, qq: str, ts: float, text: str) -> str:
    raw = f"{gid}|{qq}|{int(float(ts or 0))}|{text}".encode("utf-8", "ignore")
    return hashlib.sha1(raw).hexdigest()


def add(*, gid: str = "", qq: str = "", who: str = "", ts: float = 0,
        text: str = "", kind: str = "chat", meta: dict | None = None) -> bool:
    """写一条档案（幂等：同群/同人/同秒/同文本只留一条）。"""
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(body) < 2:
        return False
    init()
    h = _hash(str(gid or ""), str(qq or ""), ts, body)
    try:
        c = _conn()
        cur = c.execute(
            "INSERT OR IGNORE INTO archive(hash,ts,gid,qq,who,kind,text,tokens,meta) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (h, float(ts or time.time()), str(gid or ""), str(qq or ""),
             str(who or "")[:40], str(kind or "chat")[:16], body,
             " ".join(tokenize(body)),
             json.dumps(meta or {}, ensure_ascii=False)[:800]),
        )
        c.commit()
        return bool(cur.rowcount)
    except Exception as e:
        logger.debug(f"[ARCHIVE] 写入失败: {type(e).__name__}: {e}")
        return False


def migrate_dialogue(*, dir_path: str = "data/dialogue", min_age_days: int = 0,
                     dry_run: bool = False) -> dict:
    """把 `data/dialogue/*.jsonl` 里超过 N 天的行搬进档案库（不删源文件）。"""
    days = int(min_age_days or MIN_AGE_DAYS)
    cutoff = time.time() - days * 86400
    stats = {"files": 0, "rows": 0, "added": 0, "skipped": 0, "dry_run": dry_run}
    p = Path(dir_path)
    if not p.exists():
        return stats
    for f in sorted(p.glob("*.jsonl")):
        stats["files"] += 1
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for ln in lines:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            ts = float(r.get("ts") or 0)
            if not ts or ts > cutoff:
                stats["skipped"] += 1
                continue
            stats["rows"] += 1
            if dry_run:
                continue
            ok = add(gid=str(r.get("gid") or ""), qq=str(r.get("qq") or ""),
                     who=str(r.get("who") or ""), ts=ts,
                     text=str(r.get("text") or ""),
                     kind=str(r.get("kind") or "chat"),
                     meta={"src": f.name})
            if ok:
                stats["added"] += 1
    return stats


def migrate_memdb(*, min_age_days: int = 0, dry_run: bool = False) -> dict:
    """把 `memories` 里超过 N 天的旧记忆搬进档案库（不删原表）。"""
    days = int(min_age_days or MIN_AGE_DAYS)
    cutoff = time.time() - days * 86400
    stats = {"rows": 0, "added": 0, "dry_run": dry_run}
    try:
        from . import memdb
        memdb.init()
        c = memdb._conn()
        rows = c.execute(
            "SELECT id,ts,gid,about_qq,kind,title,detail FROM memories "
            "WHERE ts < ? AND status='active' ORDER BY ts ASC", (cutoff,)).fetchall()
    except Exception as e:
        logger.debug(f"[ARCHIVE] 读 memories 失败: {type(e).__name__}: {e}")
        return stats
    for r in rows:
        stats["rows"] += 1
        if dry_run:
            continue
        text = str(r["detail"] or r["title"] or "").strip()
        if not text:
            continue
        if add(gid=str(r["gid"] or ""), qq=str(r["about_qq"] or ""),
               who=str(r["about_qq"] or ""), ts=float(r["ts"] or 0),
               text=text, kind=str(r["kind"] or "event"),
               meta={"src": "memdb", "id": r["id"]}):
            stats["added"] += 1
    return stats


def _ago(ts: float) -> str:
    d = max(0.0, time.time() - float(ts or 0))
    if d < 86400:
        return f"{int(d // 3600)} 小时前"
    if d < 86400 * 60:
        return f"{int(d // 86400)} 天前"
    if d < 86400 * 365:
        return f"{int(d // (86400 * 30))} 个月前"
    return f"{d / (86400 * 365):.1f} 年前"


def recall(query: str, *, limit: int = 0, gid: str = "", qq: str = "",
           min_age_days: int = 0, before_ts: float = 0) -> list:
    """查档案库（默认只看 30 天以前的）。

    返回 [{summary,text,who,qq,gid,ts,ago}]，按相关度/时间排序。
    """
    q = str(query or "").strip()
    if not q:
        return []
    init()
    now = time.time()
    days = int(min_age_days or MIN_AGE_DAYS)
    cutoff = min(float(before_ts or 0) or now, now - days * 86400)
    lim = max(1, int(limit or TOP))

    qt = tokenize(q)
    qbig = {t for t in qt if len(t) >= 2}
    qset = qbig or set(qt)

    where = ["a.ts <= ?"]
    args = [cutoff]
    if gid:
        where.append("(a.gid='' OR a.gid=?)")
        args.append(str(gid))
    if qq:
        where.append("a.qq=?")
        args.append(str(qq))

    rows = []
    if qset:
        big = sorted({t for t in qset if len(t) >= 2})
        one = sorted({t for t in qset if len(t) < 2})
        terms = big[:12] if big else one[:6]
        if terms:
            try:
                rows = _conn().execute(
                    "SELECT a.* FROM archive_fts f JOIN archive a ON a.id=f.rowid "
                    "WHERE archive_fts MATCH ? AND " + " AND ".join(where) +
                    " ORDER BY rank LIMIT ?", [" OR ".join(terms)] + args + [lim * 4]
                ).fetchall()
            except Exception:
                rows = []
    if not rows:
        rows = _conn().execute(
            "SELECT a.* FROM archive a WHERE " + " AND ".join(where) +
            " ORDER BY a.ts DESC LIMIT ?", args + [lim * 4]).fetchall()

    out = []
    for r in rows:
        text = str(r["text"] or "")
        toks = set((r["tokens"] or "").split())
        cover = (len(qset & toks) / max(1, len(qset))) if qset else 0.0
        out.append({
            "summary": text[:60],
            "text": text[:MAX_CHARS],
            "who": str(r["who"] or r["qq"] or ""),
            "qq": str(r["qq"] or ""),
            "gid": str(r["gid"] or ""),
            "ts": float(r["ts"] or 0),
            "ago": _ago(float(r["ts"] or 0)),
            "cover": round(cover, 3),
        })
        if len(out) >= lim:
            break
    return out


def render(hits: list, n: int = 0) -> str:
    """档案块（给 prompt / [[ARCHIVE]] 共用）。"""
    hits = hits or []
    if not hits:
        return ""
    lines = [f"▍档案库｜{n or MIN_AGE_DAYS} 天以前的旧事（只你自己能查；"
             f"和图书馆/近期记忆不是一套）"]
    for i, h in enumerate(hits, 1):
        who = str(h.get("who") or "某人")
        lines.append(f"{i}. {h.get('ago') or ''}｜{who}：{str(h.get('summary') or '')}")
    return "\n".join(lines)[:MAX_CHARS]


def stats() -> dict:
    try:
        init()
        c = _conn()
        n = int(c.execute("SELECT count(*) FROM archive").fetchone()[0])
        rng = c.execute("SELECT min(ts), max(ts) FROM archive").fetchone()
        return {"count": n, "oldest": float(rng[0] or 0),
                "newest": float(rng[1] or 0), "db": str(DB_PATH)}
    except Exception as e:
        return {"count": 0, "error": f"{type(e).__name__}: {e}", "db": str(DB_PATH)}


def status_text() -> str:
    s = stats()
    head = (f"· 个人档案库：{'启用' if enabled() else '关闭'}"
            f"｜{s['count']} 条｜只存 {MIN_AGE_DAYS} 天以前")
    if s.get("error"):
        return head + f"\n· 错误：{s['error']}"
    if s["count"]:
        head += (f"\n· 范围：{_ago(s['oldest'])} ~ {_ago(s['newest'])}"
                 f"｜库：{s['db']}")
    else:
        head += f"\n· 还没有旧档案（先跑 scripts/archive_migrate.py）｜库：{s['db']}"
    return head
