# services/memdb.py
"""记忆存储的 sqlite 后端（与 memory_v2 的 JSON 后端并存，可切回）。

为什么要有它：
- 现有 `data/memory/index.json` 是全量读-改-写。2383 条 ≈12MB，
  每条消息至少 2 读 2 写，记忆越多越慢。
- 「记忆全记」之后一年约 300 万条，JSON 索引直接不可用。

设计：
- 单库 `data/memory.db`，WAL 模式；表 `memories` + FTS5 `memories_fts`。
- FTS5 不可用时自动降级为 LIKE（功能不中断，只是慢一点）。
- 对外只暴露函数，不暴露连接；`memory_v2` 保留门面，内部按
  `MEMORY_BACKEND=sqlite` 委托到这里。
- 所有写入幂等：`turn_id` 唯一；`id` 主键。
"""
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DB_PATH = Path(os.getenv("MEMORY_DB", "data/memory.db"))

_CJK = re.compile(r"[\u4e00-\u9fa5]")
_WORD = re.compile(r"[a-zA-Z0-9]+")

_LOCAL = threading.local()
_LOCK = threading.Lock()
_INITED = {"done": False, "fts": False}


# ==================== 基础 ====================
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


def tokenize(text: str) -> list:
    text = (text or "").lower()
    out = list(_WORD.findall(text))
    cjk = [c for c in text if _CJK.match(c)]
    out.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    out.extend(cjk)
    return out


def init() -> bool:
    """建表。返回 FTS5 是否可用。"""
    if _INITED["done"]:
        return _INITED["fts"]
    with _LOCK:
        if _INITED["done"]:
            return _INITED["fts"]
        c = _conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
              id TEXT PRIMARY KEY,
              turn_id TEXT UNIQUE,
              ts REAL NOT NULL,
              session TEXT NOT NULL DEFAULT '',
              gid TEXT NOT NULL DEFAULT '',
              about_qq TEXT NOT NULL DEFAULT '',
              kind TEXT NOT NULL DEFAULT 'event',
              source TEXT NOT NULL DEFAULT 'auto',
              title TEXT NOT NULL DEFAULT '',
              mind TEXT NOT NULL DEFAULT '',
              detail TEXT NOT NULL DEFAULT '',
              mood_word TEXT NOT NULL DEFAULT '',
              mood_value REAL,
              delta REAL,
              salience REAL NOT NULL DEFAULT 0,
              valence REAL NOT NULL DEFAULT 0,
              importance REAL NOT NULL DEFAULT 0.5,
              status TEXT NOT NULL DEFAULT 'active',
              last_hit_at REAL NOT NULL DEFAULT 0,
              hit_count INTEGER NOT NULL DEFAULT 0,
              tokens TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_mem_ts ON memories(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_mem_qq ON memories(about_qq, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_mem_gid ON memories(gid, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_mem_sal ON memories(salience DESC, ts DESC);
            """
        )
        fts = False
        try:
            c.executescript(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                  tokens, content='memories', content_rowid='rowid',
                  tokenize='unicode61'
                );
                CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
                  INSERT INTO memories_fts(rowid, tokens) VALUES (new.rowid, new.tokens);
                END;
                CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
                  INSERT INTO memories_fts(memories_fts, rowid, tokens)
                    VALUES ('delete', old.rowid, old.tokens);
                END;
                CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
                  INSERT INTO memories_fts(memories_fts, rowid, tokens)
                    VALUES ('delete', old.rowid, old.tokens);
                  INSERT INTO memories_fts(rowid, tokens) VALUES (new.rowid, new.tokens);
                END;
                """
            )
            c.execute("SELECT count(*) FROM memories_fts").fetchone()
            fts = True
        except Exception:
            fts = False
        c.commit()
        _INITED["done"] = True
        _INITED["fts"] = fts
        return fts


def fts_available() -> bool:
    return init()


def _emo(d: dict) -> float:
    try:
        return max(0.0, min(1.0, float(d.get("salience") or 0.0)))
    except Exception:
        return 0.0


def _forget(now: float, last_hit: float, salience: float = 0.0) -> float:
    days = max(0.0, (now - float(last_hit or now)) / 86400.0)
    tau = 30.0 + 60.0 * max(0.0, min(1.0, salience))
    return math.exp(-days / tau)


# ==================== 写入 ====================
def remember(*, text: str, kind: str = "event", about_qq: str = "",
             tags=None, importance: float = 0.5, emotion_hit: bool = False,
             emotion_weight: float = 0.0, mood_word: str = "",
             mood_value=None, gid: str = "", session: str = "",
             title: str = "", mind: str = "", delta=None,
             salience=None, valence=None, turn_id: str = "",
             source: str = "auto") -> str:
    """写一条记忆。相同 turn_id 幂等更新；否则新增。"""
    init()
    now = time.time()
    text = (text or "").strip()
    _ew = max(float(emotion_weight or 0.0), 0.6 if emotion_hit else 0.0)
    _sal = float(salience) if salience is not None else max(0.0, min(1.0, _ew))
    _val = float(valence) if valence is not None else 0.0
    mid = uuid.uuid4().hex[:12]
    toks = " ".join(tokenize(text))
    row = {
        "id": mid, "turn_id": turn_id or None, "ts": now, "session": session,
        "gid": gid, "about_qq": str(about_qq), "kind": kind, "source": source,
        "title": (title or text[:30]), "mind": mind, "detail": text,
        "mood_word": str(mood_word or "")[:20],
        "mood_value": None if mood_value is None else float(mood_value),
        "delta": None if delta is None else float(delta),
        "salience": _sal, "valence": _val, "importance": float(importance),
        "status": "active", "last_hit_at": now, "hit_count": 0, "tokens": toks,
    }
    c = _conn()
    with _LOCK:
        if turn_id:
            old = c.execute(
                "SELECT id FROM memories WHERE turn_id=?", (turn_id,)
            ).fetchone()
            if old:
                row["id"] = old["id"]
                c.execute(
                    """UPDATE memories SET ts=?, session=?, gid=?, about_qq=?, kind=?,
                       source=?, title=?, mind=?, detail=?, mood_word=?, mood_value=?,
                       delta=?, salience=?, valence=?, importance=?, last_hit_at=?,
                       tokens=? WHERE id=?""",
                    (row["ts"], row["session"], row["gid"], row["about_qq"],
                     row["kind"], row["source"], row["title"], row["mind"],
                     row["detail"], row["mood_word"], row["mood_value"],
                     row["delta"], row["salience"], row["valence"],
                     row["importance"], row["last_hit_at"], row["tokens"],
                     row["id"]),
                )
                c.commit()
                return row["id"]
        c.execute(
            """INSERT INTO memories
               (id,turn_id,ts,session,gid,about_qq,kind,source,title,mind,detail,
                mood_word,mood_value,delta,salience,valence,importance,status,
                last_hit_at,hit_count,tokens)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], row["turn_id"], row["ts"], row["session"], row["gid"],
             row["about_qq"], row["kind"], row["source"], row["title"],
             row["mind"], row["detail"], row["mood_word"], row["mood_value"],
             row["delta"], row["salience"], row["valence"], row["importance"],
             row["status"], row["last_hit_at"], row["hit_count"], row["tokens"]),
        )
        c.commit()
    return mid


def upsert_turn(turn: dict) -> str:
    """消息到达先落一条 pending；回复后 patch_turn 补 title/mind/detail。"""
    return remember(
        text=str(turn.get("detail") or turn.get("text") or ""),
        kind=str(turn.get("kind") or "event"),
        about_qq=str(turn.get("about_qq") or ""),
        importance=float(turn.get("importance") or 0.5),
        gid=str(turn.get("gid") or ""),
        session=str(turn.get("session") or ""),
        title=str(turn.get("title") or ""),
        mind=str(turn.get("mind") or ""),
        turn_id=str(turn.get("turn_id") or ""),
        source=str(turn.get("source") or "auto"),
    )


def patch_turn(turn_id: str, **fields) -> bool:
    """判读回来后补 delta/salience/valence/mood_word/mind/title/detail。"""
    init()
    allowed = {"title", "mind", "detail", "mood_word", "mood_value", "delta",
               "salience", "valence", "importance", "kind", "about_qq", "gid"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed and v is not None:
            sets.append("%s=?" % k)
            vals.append(v)
    if not sets:
        return False
    if "detail" in fields and fields.get("detail") is not None:
        sets.append("tokens=?")
        vals.append(" ".join(tokenize(str(fields["detail"]))))
    vals.append(turn_id)
    with _LOCK:
        cur = _conn().execute(
            "UPDATE memories SET %s WHERE turn_id=?" % ",".join(sets), vals)
        _conn().commit()
        return cur.rowcount > 0


def deepest_event(about_qq: str, *, min_abs: float = 0.15,
                  max_age_days: float = 0.0) -> dict:
    """记忆里关于这个人 |Δ| 最大的那件事（给「印象最深的一件事」用）。

    返回 {text, mind, delta, at}；没有就 {}。
    """
    qq = str(about_qq or "").strip()
    if not qq:
        return {}
    init()
    sql = ("SELECT * FROM memories WHERE status='active' AND about_qq=? "
           "AND delta IS NOT NULL AND abs(delta)>=?")
    args = [qq, float(min_abs)]
    if max_age_days and float(max_age_days) > 0:
        sql += " AND ts>=?"
        args.append(time.time() - float(max_age_days) * 86400)
    sql += " ORDER BY abs(delta) DESC, ts DESC LIMIT 1"
    r = _conn().execute(sql, args).fetchone()
    if not r:
        return {}
    return {"text": str(r["detail"] or r["title"] or "")[:80],
            "mind": str(r["mind"] or "")[:100],
            "delta": round(float(r["delta"] or 0), 3),
            "at": float(r["ts"] or 0)}


def import_row(d: dict) -> bool:
    """迁移用：按原 id 原样写入一条旧索引记录（幂等）。"""
    init()
    mid = str(d.get("id") or uuid.uuid4().hex[:12])
    text = str(d.get("detail") or d.get("text_full") or d.get("summary") or "")
    row = {
        "id": mid, "turn_id": d.get("turn_id") or None,
        "ts": float(d.get("created_at") or time.time()),
        "session": str(d.get("session") or ""),
        "gid": str(d.get("gid") or _gid_from_text(text)),
        "about_qq": str(d.get("about_qq") or ""),
        "kind": str(d.get("kind") or "event"),
        "source": str(d.get("source") or "migrated"),
        "title": str(d.get("title") or d.get("summary") or text[:30]),
        "mind": str(d.get("mind") or ""),
        "detail": text,
        "mood_word": str(d.get("mood_word") or ""),
        "mood_value": (None if d.get("mood_value") is None else float(d.get("mood_value"))),
        "delta": (None if d.get("delta") is None else float(d.get("delta"))),
        "salience": float(d.get("salience") if d.get("salience") is not None
                          else d.get("emotion_weight") or 0.0),
        "valence": float(d.get("valence") or 0.0),
        "importance": float(d.get("importance") or 0.5),
        "status": str(d.get("status") or "active"),
        "last_hit_at": float(d.get("last_hit_at") or time.time()),
        "hit_count": int(d.get("hit_count") or 0),
        "tokens": " ".join(tokenize(text)),
    }
    with _LOCK:
        _conn().execute(
            """INSERT OR REPLACE INTO memories
               (id,turn_id,ts,session,gid,about_qq,kind,source,title,mind,detail,
                mood_word,mood_value,delta,salience,valence,importance,status,
                last_hit_at,hit_count,tokens)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(row[k] for k in (
                "id", "turn_id", "ts", "session", "gid", "about_qq", "kind",
                "source", "title", "mind", "detail", "mood_word", "mood_value",
                "delta", "salience", "valence", "importance", "status",
                "last_hit_at", "hit_count", "tokens")),
        )
        _conn().commit()
    return True


_GID_RE = re.compile(r"群号[:：]\s*(\d+)")


def _gid_from_text(text: str) -> str:
    m = _GID_RE.search(text or "")
    return m.group(1) if m else ""


def touch(mid: str) -> None:
    init()
    with _LOCK:
        _conn().execute(
            "UPDATE memories SET last_hit_at=?, hit_count=hit_count+1 WHERE id=?",
            (time.time(), str(mid)))
        _conn().commit()


# ==================== 读取 ====================
def _row_to_hit(r, score: float = 0.0) -> dict:
    now = time.time()
    return {
        "id": r["id"], "summary": (r["title"] or r["detail"] or "")[:80],
        "text": (r["detail"] or "")[:600], "text_full": r["detail"] or "",
        "kind": r["kind"], "about_qq": r["about_qq"],
        "emotion_hit": bool(r["salience"] >= 0.5),
        "emotion_weight": float(r["salience"] or 0.0),
        "emotion_mark": ("💥💥💥" if r["salience"] >= 0.8 else
                         "💥💥" if r["salience"] >= 0.5 else
                         "💥" if r["salience"] >= 0.2 else ""),
        "created_at": float(r["ts"] or now),
        "time_str": time.strftime("%m-%d %H:%M", time.localtime(float(r["ts"] or now))),
        "ago": _ago(now - float(r["ts"] or now)),
        "score": score,
        "mood_word": r["mood_word"] or "", "mind": r["mind"] or "",
        "salience": float(r["salience"] or 0.0), "delta": r["delta"],
        "valence": float(r["valence"] or 0.0), "gid": r["gid"] or "",
    }


def _ago(sec: float) -> str:
    sec = max(0.0, float(sec or 0))
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return "%d 分钟前" % int(sec // 60)
    if sec < 86400:
        return "%d 小时前" % int(sec // 3600)
    return "%d 天前" % int(sec // 86400)


def _candidates(c, qt, gid, max_age_days, limit=200):
    sql = "SELECT * FROM memories WHERE status='active'"
    args = []
    if gid:
        sql += " AND (gid='' OR gid=?)"
        args.append(str(gid))
    if max_age_days and max_age_days > 0:
        sql += " AND (ts>=? OR importance>=0.8 OR salience>=0.5)"
        args.append(time.time() - float(max_age_days) * 86400)
    if qt and _INITED["fts"]:
        match = " OR ".join(sorted(set(qt))[:24])
        try:
            rows = c.execute(
                "SELECT m.* FROM memories_fts f JOIN memories m ON m.rowid=f.rowid "
                "WHERE memories_fts MATCH ? AND m.status='active' "
                "ORDER BY rank LIMIT ?", (match, limit)).fetchall()
            if rows:
                return rows
        except Exception:
            pass
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return c.execute(sql, args).fetchall()


def recall(query: str, *, limit: int = 5, speaker_qq: str = "",
           min_score: float = 0.05, max_age_days: float = 0.0,
           gid: str = "", mark_hit: bool = True) -> list:
    init()
    c = _conn()
    qt = tokenize(query)
    qset = set(qt)
    rows = _candidates(c, qt, gid, max_age_days, limit=200)
    now = time.time()
    scored = []
    for r in rows:
        dt = set((r["tokens"] or "").split())
        base = (len(qset & dt) / max(1, len(qset))) if qset else 0.0
        sal = _emo(dict(r))
        w_speaker = 2.0 if (speaker_qq and r["about_qq"] == str(speaker_qq)) else 1.0
        w_imp = 0.5 + float(r["importance"] or 0.5)
        w_emo = 1.0 + 2.5 * sal
        w_forget = _forget(now, float(r["last_hit_at"] or now), sal)
        score = (0.03 + base) * w_speaker * w_imp * w_emo * w_forget
        if score >= min_score:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    top = scored[:max(1, int(limit))]
    if top and mark_hit:
        with _LOCK:
            for _, r in top:
                c.execute("UPDATE memories SET last_hit_at=?, hit_count=hit_count+1 "
                          "WHERE id=?", (now, r["id"]))
            c.commit()
    return [_row_to_hit(r, s) for s, r in top]


def recent_about(qq: str, *, gid: str = "", limit: int = 3) -> list:
    """这个人最近的事（不按关键词，只按时间）。

    2026-11-24 用户：「最近很少主动去找记忆了，提起相似的事情还是要回想起来」。
    关键词召回不到时，至少把「关于这个人的最近几条」给她，别让她像失忆一样。
    """
    qq = str(qq or "").strip()
    if not qq:
        return []
    init()
    c = _conn()
    sql = "SELECT * FROM memories WHERE status='active' AND about_qq=?"
    args = [qq]
    if gid:
        sql += " AND (gid='' OR gid=?)"
        args.append(str(gid))
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(max(1, int(limit)))
    try:
        rows = c.execute(sql, args).fetchall()
    except Exception:
        return []
    return [_row_to_hit(r, 0.0) for r in rows]


def recall_catalog(query: str, *, offset: int = 0, limit: int = 10,
                   speaker_qq: str = "", gid: str = "", time_range=None) -> dict:
    """目录模式：默认按时间倒序（从近到远）；有关键词才按相关度。"""
    init()
    c = _conn()
    q = str(query or "").strip()
    if q:
        hits = recall(q, limit=max(limit + offset, limit), speaker_qq=speaker_qq, gid=gid,
                      mark_hit=False)
        page = hits[offset:offset + limit]
        items = [{"id": h["id"], "time_str": h["time_str"], "ago": h["ago"],
                  "emotion_mark": h["emotion_mark"], "summary": h["summary"],
                  "mood_word": h.get("mood_word") or "", "salience": h["salience"]}
                 for h in page]
        return {"items": items, "total": len(hits), "offset": offset,
                "has_more": len(hits) > offset + limit, "mode": "relevance"}
    sql = "SELECT * FROM memories WHERE status='active'"
    args = []
    if gid:
        sql += " AND (gid='' OR gid=?)"
        args.append(str(gid))
    if speaker_qq:
        sql += " AND (about_qq=? OR about_qq='')"
        args.append(str(speaker_qq))
    if time_range:
        sql += " AND ts>=? AND ts<=?"
        args.extend([float(time_range[0]), float(time_range[1])])
    sql += " ORDER BY ts DESC LIMIT ? OFFSET ?"
    args.extend([int(limit), int(offset)])
    rows = c.execute(sql, args).fetchall()
    items = [{"id": r["id"],
              "time_str": time.strftime("%m-%d %H:%M", time.localtime(float(r["ts"] or 0))),
              "ago": _ago(time.time() - float(r["ts"] or 0)),
              "emotion_mark": ("💥💥💥" if r["salience"] >= 0.8 else
                               "💥💥" if r["salience"] >= 0.5 else
                               "💥" if r["salience"] >= 0.2 else ""),
              "summary": (r["title"] or r["detail"] or "")[:80],
              "mood_word": r["mood_word"] or "",
              "salience": float(r["salience"] or 0.0)}
             for r in rows]
    total = c.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
    return {"items": items, "total": total, "offset": offset,
            "has_more": offset + len(items) < total, "mode": "time"}


def recall_detail(mid: str) -> str:
    init()
    r = _conn().execute("SELECT detail FROM memories WHERE id=?", (str(mid),)).fetchone()
    return (r["detail"] if r else "") or ""


def revisit_candidate(about_qq: str, *, min_age_days: float = 7.0,
                      min_idle_days: float = 3.0) -> dict:
    init()
    now = time.time()
    r = _conn().execute(
        """SELECT * FROM memories
           WHERE status='active' AND about_qq=? AND ts<=?
             AND last_hit_at<=? AND salience>=0.3
           ORDER BY salience DESC, ts DESC LIMIT 1""",
        (str(about_qq), now - float(min_age_days) * 86400,
         now - float(min_idle_days) * 86400),
    ).fetchone()
    return _row_to_hit(r) if r else {}


def stats() -> dict:
    init()
    c = _conn()
    total = c.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
    gid_n = c.execute("SELECT count(DISTINCT gid) FROM memories WHERE gid!=''").fetchone()[0]
    sal = c.execute("SELECT count(*) FROM memories WHERE salience>=0.5").fetchone()[0]
    return {"total": total, "groups": gid_n, "strong": sal,
            "fts": bool(_INITED["fts"]), "db": str(DB_PATH)}
