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
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from core import tuning

DB_PATH = Path(os.getenv("MEMORY_DB", "data/memory.db"))

# ---- 检索规划器（P4）----
W_STRUCT = tuning.f("RET_W_STRUCT")
W_BM25 = tuning.f("RET_W_BM25")
W_TAG = tuning.f("RET_W_TAG")
W_STRENGTH = tuning.f("RET_W_STRENGTH")
W_RECENCY = tuning.f("RET_W_RECENCY")
W_SALIENCE = tuning.f("RET_W_SALIENCE")
PLAN_CAND = tuning.i("RET_PLAN_CAND")
TAG_TOP = tuning.i("RET_TAG_TOP")
STRENGTH_GAIN = tuning.f("RET_STRENGTH_GAIN")
RECENCY_TAU_DAYS = tuning.f("RET_RECENCY_TAU_DAYS")
# 检索融合：0=加权求和（旧行为）；1=多路 RRF（服务器侧先做，向量路仍留算力机）
FUSION_RRF = tuning.i("RET_FUSION_RRF")
RRF_K = max(1.0, tuning.f("RET_RRF_K"))

# ---- 检索二期（emotion §6）：显著性倒 U + 按 kind 的提取半衰期 ----
SAL_LOW = tuning.f("MEM_SAL_LOW")
SAL_HIGH = tuning.f("MEM_SAL_HIGH")
SAL_SLOPE_LOW = tuning.f("MEM_SAL_SLOPE_LOW")
SAL_SLOPE_MID = tuning.f("MEM_SAL_SLOPE_MID")
SAL_FLOOR = tuning.f("MEM_SAL_FLOOR")
SAL_TAIL = tuning.f("MEM_SAL_TAIL")
BAD_BOOST = tuning.f("MEM_BAD_BOOST")
TAU_KIND = {
    "event": tuning.f("MEM_TAU_EVENT"),
    "bond": tuning.f("MEM_TAU_BOND"),
    "fact": tuning.f("MEM_TAU_FACT"),
    "preference": tuning.f("MEM_TAU_PREFERENCE"),
    "style": tuning.f("MEM_TAU_STYLE"),
}
TAU_DEFAULT = tuning.f("MEM_TAU_DEFAULT")
STYLE_MMR_ENABLED = tuning.i("STYLE_MMR_ENABLED")
STYLE_MMR_LAMBDA = tuning.f("STYLE_MMR_LAMBDA")

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


# ==================== 标签（P4） ====================
# topic 标签只用「二元词 + 过滤虚词」，不引入分词依赖。
_TAG_STOP = {
    "什么", "这个", "那个", "一个", "我们", "你们", "他们", "她们", "今天",
    "现在", "可以", "就是", "不是", "没有", "知道", "觉得", "自己", "怎么",
    "因为", "所以", "但是", "还是", "已经", "时候", "事情", "东西", "真的",
    "有点", "感觉", "其实", "然后", "如果", "这样", "那样", "一下", "一点",
    "这些", "那些", "这里", "那里", "出来", "起来", "过来", "回去",
}


def tags_for(*, text: str, about_qq: str = "", gid: str = "",
             kind: str = "event", mood_word: str = "", delta=None,
             ts: float = 0, top: int = 0) -> list:
    """一条记忆 → [(tag_type, tag_value, weight)]。

    标签只做**结构化过滤**，不做语义判断：
      person / group / event_type / mood / mood_dir / time_bucket / topic
    """
    out = []
    qq = str(about_qq or "").strip()
    if qq.isdigit():
        out.append(("person", qq, 1.0))
    if str(gid or "").strip():
        out.append(("group", str(gid).strip(), 1.0))
    if str(kind or "").strip():
        out.append(("event_type", str(kind).strip()[:16], 0.6))
    mw = str(mood_word or "").strip()
    if mw:
        out.append(("mood", mw[:12], 0.8))
    if delta is not None:
        try:
            d = float(delta)
            out.append(("mood_dir", "pos" if d > 1e-3 else
                        "neg" if d < -1e-3 else "neutral", 0.7))
        except Exception:
            pass
    if ts:
        try:
            out.append(("time_bucket",
                        time.strftime("%Y-%m-%d", time.localtime(float(ts))), 0.5))
        except Exception:
            pass
    toks = [t for t in tokenize(text)
            if len(t) == 2 and not t.isdigit() and t not in _TAG_STOP]
    cnt = {}
    for t in toks:
        cnt[t] = cnt.get(t, 0) + 1
    total = sum(cnt.values()) or 1
    k = int(top or TAG_TOP)
    for t, n in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:k]:
        out.append(("topic", t, round(0.3 + 0.7 * n / total, 3)))
    return out


def _replace_tags(c: sqlite3.Connection, mid: str, tags: list) -> None:
    c.execute("DELETE FROM memory_tags WHERE memory_id=?", (str(mid),))
    if tags:
        c.executemany(
            "INSERT OR REPLACE INTO memory_tags(memory_id,tag_type,tag_value,weight) "
            "VALUES (?,?,?,?)",
            [(str(mid), t, v, float(w)) for t, v, w in tags])


def _ensure_columns(c: sqlite3.Connection, table: str, cols: dict) -> None:
    """老库补列（sqlite 没有 ADD COLUMN IF NOT EXISTS）。"""
    try:
        have = {r[1] for r in c.execute("PRAGMA table_info(%s)" % table).fetchall()}
    except Exception:
        return
    for name, decl in cols.items():
        if name in have:
            continue
        try:
            c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
        except Exception:
            pass


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
        # P4：v2 列（老库用 ALTER 补齐；新库直接带默认值）
        _ensure_columns(c, "memories", {
            "impact": "INTEGER DEFAULT 0",
            "unexpected": "REAL DEFAULT 0",
            "relation_relevance": "REAL DEFAULT 0.5",
            "storage_strength": "REAL DEFAULT 0.5",
            "retrieval_strength": "REAL DEFAULT 0.5",
            "embedding": "BLOB",
        })
        c.executescript(
            """
            -- ② 标签（多对多）：人/群/话题/心情/时间桶/事件类型
            CREATE TABLE IF NOT EXISTS memory_tags (
              memory_id TEXT NOT NULL,
              tag_type  TEXT NOT NULL,
              tag_value TEXT NOT NULL,
              weight    REAL DEFAULT 1.0,
              PRIMARY KEY (memory_id, tag_type, tag_value)
            );
            CREATE INDEX IF NOT EXISTS idx_tag_lookup ON memory_tags(tag_type, tag_value);
            CREATE INDEX IF NOT EXISTS idx_tag_mid ON memory_tags(memory_id);

            -- ③ 实体：QQ / 称呼 / 别名 / 群名片
            CREATE TABLE IF NOT EXISTS entities (
              entity_id TEXT PRIMARY KEY,
              kind TEXT, qq TEXT, gid TEXT,
              primary_name TEXT, aliases TEXT, style_id TEXT, updated_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_entity_qq ON entities(qq);
            CREATE INDEX IF NOT EXISTS idx_entity_name ON entities(primary_name);

            -- ⑥ 互动边（DESIGN addressing §3.2 / P1）：谁对谁说过话
            CREATE TABLE IF NOT EXISTS interaction_edges (
              id TEXT PRIMARY KEY, ts REAL, session TEXT,
              from_qq TEXT, to_qq TEXT, kind TEXT, conf REAL
            );
            CREATE INDEX IF NOT EXISTS idx_edge_from ON interaction_edges(from_qq, ts);
            CREATE INDEX IF NOT EXISTS idx_edge_to ON interaction_edges(to_qq, ts);

            -- ⑦ 档案镜像（retrieval §8）：profiles/*.json → profiles
            CREATE TABLE IF NOT EXISTS profiles (
              qq TEXT PRIMARY KEY, name TEXT, aliases TEXT,
              affinity REAL, intimacy REAL, trust REAL, respect REAL,
              updated_at REAL
            );

            -- ⑧ 说话样本（style_vault → style_samples）
            CREATE TABLE IF NOT EXISTS style_samples (
              id TEXT PRIMARY KEY, qq TEXT, gid TEXT,
              text TEXT, quality REAL, ts REAL
            );
            CREATE INDEX IF NOT EXISTS idx_sample_qq ON style_samples(qq, gid, ts);

            -- ⑨ 线程（group_scene → threads）
            CREATE TABLE IF NOT EXISTS threads (
              thread_id TEXT PRIMARY KEY, gid TEXT, topic TEXT,
              participants TEXT, last_ts REAL
            );
            CREATE INDEX IF NOT EXISTS idx_thread_gid ON threads(gid, last_ts);

            -- ⑥ 心情事件（结构化）
            CREATE TABLE IF NOT EXISTS mood_events (
              id TEXT PRIMARY KEY, ts REAL, session TEXT,
              about_qq TEXT, target TEXT,
              impact INTEGER, direction TEXT,
              dv REAL, da REAL, dd REAL,
              confidence REAL, relevance REAL,
              reason TEXT, msg_ids TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_mood_person
              ON mood_events(about_qq, direction, ts);
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


def salience_weight(u: float) -> float:
    """记忆显著性 w：|ΔVAD| 的**倒 U**（emotion §6）。

    小冲击几乎不记（u<0.15 线性很小）；中等冲击最显著（0.15~0.6 上升）；
    极端冲击回落到 MEM_SAL_TAIL（防止一次大事把记忆钉死）。
    """
    try:
        u = max(0.0, min(1.0, float(u or 0.0)))
    except Exception:
        u = 0.0
    low = max(1e-6, float(SAL_LOW or 0.15))
    high = max(low + 1e-6, float(SAL_HIGH or 0.60))
    if u < low:
        return max(0.0, float(SAL_SLOPE_LOW or 0.3) * u)
    if u <= high:
        return float(SAL_FLOOR or 0.045) + float(SAL_SLOPE_MID or 2.1) * (u - low)
    # 1.0 → TAIL；high → FLOOR + SLOPE_MID×(high−low)
    peak = float(SAL_FLOOR or 0.045) + float(SAL_SLOPE_MID or 2.1) * (high - low)
    tail = float(SAL_TAIL or 0.6)
    return peak + (tail - peak) * ((u - high) / max(1e-6, 1.0 - high))


def storage_strength_of(salience: float, *, unexpected: float = 0.0,
                        relation_relevance: float = 0.5,
                        valence: float = 0.0) -> float:
    """编码强度：0.15 + 0.45w + 0.20×unexpected + 0.20×relation_relevance。"""
    w = salience_weight(salience)
    s = (0.15 + 0.45 * w + 0.20 * max(0.0, min(1.0, float(unexpected or 0.0)))
         + 0.20 * max(0.0, min(1.0, float(relation_relevance or 0.0))))
    if float(valence or 0.0) < 0:
        s *= float(BAD_BOOST or 1.15)
    return max(0.0, min(1.0, s))


def tau_days(kind: str) -> float:
    """按记忆 kind 的提取半衰期（天）。"""
    return float(TAU_KIND.get(str(kind or "").lower(), TAU_DEFAULT or 30.0))


def _forget(now: float, last_hit: float, salience: float = 0.0,
            kind: str = "event") -> float:
    """提取强度衰减：exp(−Δt / τ_kind)。

    `salience` 参数保留兼容（旧调用点传了），但不再参与 τ ——
    显著性改由 `salience_weight()` 在打分里体现（emotion §6）。
    """
    days = max(0.0, (now - float(last_hit or now)) / 86400.0)
    return math.exp(-days / max(1.0, tau_days(kind)))


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
    # 调用方显式给的 tags（如 weekly_apply 的 "weekly_check"）以前被丢掉了：
    # 这里只用了 tags_for() 自动派生，json 后端却会保留 → 两个后端行为不一致。
    _extra_tags = [("custom", str(t).strip()[:32], 1.0)
                   for t in (tags or []) if str(t).strip()]
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
                _store = storage_strength_of(_sal, valence=_val)
                c.execute(
                    "UPDATE memories SET storage_strength=max(storage_strength, ?), "
                    "retrieval_strength=max(retrieval_strength, ?) WHERE id=?",
                    (_store, _store, row["id"]))
                _replace_tags(c, row["id"], tags_for(
                    text=text, about_qq=row["about_qq"], gid=row["gid"],
                    kind=row["kind"], mood_word=row["mood_word"],
                    delta=row["delta"], ts=row["ts"]) + _extra_tags)
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
        _store = storage_strength_of(_sal, valence=_val)
        c.execute("UPDATE memories SET storage_strength=?, retrieval_strength=? "
                  "WHERE id=?", (_store, _store, mid))
        _replace_tags(c, mid, tags_for(
            text=text, about_qq=row["about_qq"], gid=row["gid"],
            kind=row["kind"], mood_word=row["mood_word"],
            delta=row["delta"], ts=row["ts"]) + _extra_tags)
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
        c = _conn()
        cur = c.execute(
            "UPDATE memories SET %s WHERE turn_id=?" % ",".join(sets), vals)
        if cur.rowcount and any(k in fields for k in
                                ("detail", "mood_word", "delta", "kind")):
            r = c.execute("SELECT * FROM memories WHERE turn_id=?",
                          (turn_id,)).fetchone()
            if r:
                _replace_tags(c, r["id"], tags_for(
                    text=r["detail"] or r["title"] or "",
                    about_qq=r["about_qq"], gid=r["gid"], kind=r["kind"],
                    mood_word=r["mood_word"], delta=r["delta"], ts=r["ts"]))
                _store = storage_strength_of(
                    float(r["salience"] or 0),
                    valence=float(r["valence"] or 0))
                c.execute("UPDATE memories SET storage_strength=max(storage_strength, ?), "
                          "retrieval_strength=max(retrieval_strength, ?) WHERE id=?",
                          (_store, _store, r["id"]))
        c.commit()
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
        _store = storage_strength_of(float(row["salience"] or 0),
                                     valence=float(row["valence"] or 0))
        _ret = _store * _forget(time.time(), float(row["last_hit_at"] or 0),
                                kind=str(row["kind"] or "event"))
        _conn().execute(
            "UPDATE memories SET storage_strength=?, retrieval_strength=?, "
            "impact=COALESCE(?, impact), unexpected=COALESCE(?, unexpected), "
            "relation_relevance=COALESCE(?, relation_relevance) WHERE id=?",
            (_store, _ret,
             d.get("impact"), d.get("unexpected"), d.get("relation_relevance"),
             row["id"]))
        _replace_tags(_conn(), row["id"], tags_for(
            text=text, about_qq=row["about_qq"], gid=row["gid"],
            kind=row["kind"], mood_word=row["mood_word"],
            delta=row["delta"], ts=row["ts"]))
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


def note_hits(ids, *, now: float = 0) -> int:
    """把「最终注入 prompt」的几条记为命中。

    为什么单独一个函数：检索时为了做 MMR/配额会多取候选（mark_hit=False），
    只有最后真正选中的几条才该 `retrieval_strength += gain×(1−r)`。
    """
    ids = [str(x) for x in (ids or []) if str(x).strip()]
    if not ids:
        return 0
    init()
    now = float(now or time.time())
    n = 0
    with _LOCK:
        c = _conn()
        for mid in ids:
            r = c.execute(
                "SELECT retrieval_strength,last_hit_at,kind FROM memories WHERE id=?",
                (mid,)).fetchone()
            if not r:
                continue
            cur = float(r["retrieval_strength"] or 0.5) * _forget(
                now, float(r["last_hit_at"] or now), kind=str(r["kind"] or "event"))
            new = min(1.0, cur + STRENGTH_GAIN * (1.0 - cur))
            c.execute(
                "UPDATE memories SET last_hit_at=?, hit_count=hit_count+1, "
                "retrieval_strength=? WHERE id=?", (now, new, mid))
            n += 1
        c.commit()
    return n


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
        # MMR 去重要用：检索命中后 memory_v2._diversify 读这两个字段
        "tokens": r["tokens"] or "",
        "storage_strength": float(r["storage_strength"] or 0.0),
        "retrieval_strength": float(r["retrieval_strength"] or 0.0),
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
        # 倒 U：中等冲击最显著，极端冲击回落（emotion §6）
        w_emo = 1.0 + 2.0 * salience_weight(sal)
        w_forget = _forget(now, float(r["last_hit_at"] or now),
                           kind=str(r["kind"] or "event"))
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
        # `no` 是目录编号（翻页时连续）：json 后端一直有，sqlite 以前漏了，
        # 导致「目录条目带编号」这条契约在 sqlite 下不成立（2026-11-25 修）。
        items = [{"no": offset + i + 1, "id": h["id"], "time_str": h["time_str"],
                  "ago": h["ago"], "emotion_mark": h["emotion_mark"],
                  "summary": h["summary"], "mood_word": h.get("mood_word") or "",
                  "salience": h["salience"]}
                 for i, h in enumerate(page)]
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
    items = [{"no": offset + i + 1,
              "id": r["id"],
              "time_str": time.strftime("%m-%d %H:%M", time.localtime(float(r["ts"] or 0))),
              "ago": _ago(time.time() - float(r["ts"] or 0)),
              "emotion_mark": ("💥💥💥" if r["salience"] >= 0.8 else
                               "💥💥" if r["salience"] >= 0.5 else
                               "💥" if r["salience"] >= 0.2 else ""),
              "summary": (r["title"] or r["detail"] or "")[:80],
              "mood_word": r["mood_word"] or "",
              "salience": float(r["salience"] or 0.0)}
             for i, r in enumerate(rows)]
    total = c.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
    return {"items": items, "total": total, "offset": offset,
            "has_more": offset + len(items) < total, "mode": "time"}


def list_memories(*, limit: int = 300, since_ts: float = 0.0) -> list:
    """按时间倒序列出 active 记忆（统一结构，给日志/周报/复盘用）。

    为什么要它：`journal_agent` / `weekly_apply` 以前直接读 `index.json`，
    切到 sqlite 后读到的永远是空列表（2026-11-25 修）。
    """
    init()
    sql = "SELECT * FROM memories WHERE status='active'"
    args = []
    if since_ts:
        sql += " AND ts>=?"
        args.append(float(since_ts))
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(int(limit))
    c = _conn()
    rows = c.execute(sql, args).fetchall()
    _ids = [r["id"] for r in rows]
    _tags = _tag_map(c, _ids) if _ids else {}
    out = []
    for r in rows:
        _sal = float(r["salience"] or 0.0)
        out.append({
            "id": r["id"],
            "kind": r["kind"] or "event",
            "tags": sorted(_tags.get(r["id"], set())),
            "about_qq": r["about_qq"] or "",
            "gid": r["gid"] or "",
            "summary": (r["title"] or r["detail"] or "")[:120],
            "emotion_hit": _sal >= 0.2,
            "importance": float(r["importance"] or 0.5),
            "salience": _sal,
            "created_at": float(r["ts"] or 0.0),
            "last_hit_at": float(r["last_hit_at"] or r["ts"] or 0.0),
            "status": "active",
        })
    return out


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


# ==================== 检索规划器（P4） ====================
def _tag_map(c, ids) -> dict:
    out = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for r in c.execute("SELECT memory_id, tag_value FROM memory_tags "
                           "WHERE memory_id IN (%s)" % ph, chunk):
            out.setdefault(r["memory_id"], set()).add(r["tag_value"])
    return out


def recall_planned(query: str = "", *, limit: int = 5, speaker_qq: str = "",
                   gid: str = "", max_age_days: float = 0,
                   direction: str = "", tag_values=None,
                   person_hard: bool = False, min_score: float = 0.0,
                   mark_hit: bool = True) -> list:
    """**人 → 时间 → 情绪 → 标签 → 关键词**：结构化过滤在前，打分在后。

    和旧 `recall()` 的区别：时间/情绪/标签是 **WHERE**，不是加分项
    （旧版「上周的事」会混进去年，就是因为它们只是权重）。
    """
    init()
    c = _conn()
    now = time.time()
    q = str(query or "").strip()
    qset = set(tokenize(q))
    qtag = {t for t in qset if len(t) >= 2}

    where = ["m.status='active'"]
    args = []
    if gid:
        where.append("(m.gid='' OR m.gid=?)")
        args.append(str(gid))
    if max_age_days and float(max_age_days) > 0:
        where.append("(m.ts>=? OR m.importance>=0.8 OR m.salience>=0.5)")
        args.append(now - float(max_age_days) * 86400)
    if person_hard and str(speaker_qq).isdigit():
        where.append("(m.about_qq=? OR m.about_qq='')")
        args.append(str(speaker_qq))
    if direction in ("pos", "neg", "neutral"):
        if direction == "pos":
            where.append("m.delta>0.001")
        elif direction == "neg":
            where.append("m.delta<-0.001")
        else:
            where.append("(m.delta IS NULL OR abs(m.delta)<=0.001)")
    _tags = [str(v) for v in (tag_values or []) if str(v).strip()]
    if _tags:
        where.append("EXISTS (SELECT 1 FROM memory_tags t WHERE t.memory_id=m.id "
                     "AND t.tag_value IN (%s))" % ",".join("?" * len(_tags)))
        args += _tags

    rows = []
    # 只为最终 limit 条打分，不必每次扫满 PLAN_CAND（400 条 × Python 打分是
    # 角色脑 50ms 目标的主要开销）。MMR 会多取候选，所以给 12×limit 的余量。
    _cand = max(48, min(int(PLAN_CAND or 400), max(1, int(limit)) * 8))
    if qset and _INITED["fts"]:
        match = " OR ".join(sorted(qset)[:24])
        try:
            rows = c.execute(
                "SELECT m.* FROM memories_fts f JOIN memories m ON m.rowid=f.rowid "
                "WHERE memories_fts MATCH ? AND " + " AND ".join(where) +
                " ORDER BY rank LIMIT ?", [match] + args + [_cand]).fetchall()
        except Exception:
            rows = []
    if not rows:
        rows = c.execute(
            "SELECT m.* FROM memories m WHERE " + " AND ".join(where) +
            " ORDER BY m.ts DESC LIMIT ?", args + [_cand]).fetchall()

    ids = [r["id"] for r in rows]
    tagmap = _tag_map(c, ids) if ids else {}
    struct_total = 0
    if str(speaker_qq).isdigit():
        struct_total += 1
    if direction:
        struct_total += 1
    if _tags:
        struct_total += 1

    # 先把每条候选的各路分数算出来（RRF 需要按路排名，不再直接加权求和）
    metrics = []
    for r in rows:
        toks = set((r["tokens"] or "").split())
        bm = (len(qset & toks) / max(1, len(qset))) if qset else 0.0
        hits = 0
        if str(speaker_qq).isdigit() and str(r["about_qq"] or "") == str(speaker_qq):
            hits += 1
        if direction:
            hits += 1
        if _tags:
            hits += 1
        struct = (hits / struct_total) if struct_total else 0.0
        mtag = tagmap.get(r["id"], set())
        tagov = (len(qtag & mtag) / max(1, len(qtag))) if qtag else 0.0
        # 提取强度要按 kind 衰减：event 30 天 / fact 365 天（emotion §6）
        _last_hit = float(r["last_hit_at"] or r["ts"] or now)
        strength = float(r["retrieval_strength"] or 0.5) * _forget(
            now, _last_hit, kind=str(r["kind"] or "event"))
        days = max(0.0, (now - float(r["ts"] or now)) / 86400.0)
        recency = math.pow(0.5, days / max(1.0, RECENCY_TAU_DAYS))
        # 显著性 w（倒 U）：不是「情绪越强越加分」，极端冲击会回落
        w_sal = salience_weight(float(r["salience"] or 0.0))
        metrics.append((r, struct, bm, tagov, strength, recency, w_sal))

    scored = []
    if FUSION_RRF and metrics:
        # 多路 RRF：每路按名次给 weight/(k+rank)，再 ×100 保持和 min_score 同量级。
        # 这样「BM25 很强但 recency 很差」不会因为量纲差异被一个权重吃掉。
        # 六路融合：200 条自检索实测 R@5 0.405→0.430，但 R@1 0.300→0.240。
        # 所以默认不开（RET_FUSION_RRF=0）；等向量路接上、或线上 A/B 确认
        # 「多给一条证据」比「第一条更准」更重要时再开。
        branches = (
            (W_STRUCT, 1), (W_BM25, 2), (W_TAG, 3),
            (W_STRENGTH, 4), (W_RECENCY, 5), (W_SALIENCE, 6),
        )
        fused = [0.0] * len(metrics)
        for weight, idx in branches:
            vals = [m[idx] for m in metrics]
            if not any(v > 0 for v in vals):
                continue
            order = sorted(range(len(metrics)), key=lambda i: -vals[i])
            for rank, i in enumerate(order, 1):
                fused[i] += float(weight) / (RRF_K + rank)
        for i, m in enumerate(metrics):
            r, struct, bm, tagov, _, _, _ = m
            # 关键词/标签/结构化全没命中 → 只放行「高重要 / 强情绪」
            if qset and bm <= 0 and tagov <= 0 and struct <= 0:
                if (float(r["importance"] or 0) < 0.8
                        and float(r["salience"] or 0) < 0.5):
                    continue
            score = 100.0 * fused[i]
            if score >= float(min_score):
                scored.append((score, r))
    else:
        for r, struct, bm, tagov, strength, recency, w_sal in metrics:
            score = (W_STRUCT * struct + W_BM25 * bm + W_TAG * tagov
                     + W_STRENGTH * strength + W_RECENCY * recency
                     + W_SALIENCE * w_sal)
            # 关键词/标签/结构化全没命中 → 只放行「高重要 / 强情绪」
            if qset and bm <= 0 and tagov <= 0 and struct <= 0:
                if (float(r["importance"] or 0) < 0.8
                        and float(r["salience"] or 0) < 0.5):
                    continue
            if score >= float(min_score):
                scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    top = scored[:max(1, int(limit))]
    if top and mark_hit:
        with _LOCK:
            for _, r in top:
                c.execute(
                    "UPDATE memories SET last_hit_at=?, hit_count=hit_count+1, "
                    "retrieval_strength=min(1.0, retrieval_strength + ? * "
                    "(1-retrieval_strength)) WHERE id=?",
                    (now, STRENGTH_GAIN, r["id"]))
            c.commit()
    return [_row_to_hit(r, s) for s, r in top]


def backfill_tags(*, force: bool = False, limit: int = 0) -> dict:
    """给还没有标签的记忆补标签（P4 回填）。幂等：已有标签默认跳过。"""
    init()
    c = _conn()
    sql = "SELECT * FROM memories WHERE status='active'"
    if not force:
        sql += " AND id NOT IN (SELECT DISTINCT memory_id FROM memory_tags)"
    if limit:
        sql += " LIMIT %d" % int(limit)
    rows = c.execute(sql).fetchall()
    n = 0
    with _LOCK:
        for r in rows:
            _replace_tags(c, r["id"], tags_for(
                text=r["detail"] or r["title"] or "",
                about_qq=r["about_qq"], gid=r["gid"], kind=r["kind"],
                mood_word=r["mood_word"], delta=r["delta"], ts=r["ts"]))
            n += 1
        c.commit()
    total = c.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
    tagged = c.execute("SELECT count(DISTINCT memory_id) FROM memory_tags").fetchone()[0]
    return {"tagged_now": n, "total": total, "tagged": tagged,
            "coverage": round(tagged / max(1, total), 4)}


# ==================== 心情事件 / 实体 ====================
def log_mood_event(*, ts=None, session: str = "", about_qq: str = "",
                   target: str = "", impact=None, dv: float = 0.0,
                   da: float = 0.0, dd: float = 0.0, confidence: float = 0.0,
                   relevance: float = 0.5, reason: str = "",
                   msg_ids: str = "") -> str:
    init()
    eid = uuid.uuid4().hex[:12]
    _dv = float(dv or 0.0)
    direction = "+" if _dv > 1e-3 else "-" if _dv < -1e-3 else "0"
    with _LOCK:
        _conn().execute(
            "INSERT OR REPLACE INTO mood_events (id,ts,session,about_qq,target,"
            "impact,direction,dv,da,dd,confidence,relevance,reason,msg_ids) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, float(ts or time.time()), str(session), str(about_qq),
             str(target), impact, direction, _dv, float(da or 0.0),
             float(dd or 0.0), float(confidence or 0.0), float(relevance or 0.0),
             str(reason or "")[:120], str(msg_ids or "")))
        _conn().commit()
    return eid


def mood_events_of(about_qq: str, *, direction: str = "", limit: int = 5) -> list:
    init()
    sql = "SELECT * FROM mood_events WHERE about_qq=?"
    args = [str(about_qq)]
    if direction in ("+", "-", "0"):
        sql += " AND direction=?"
        args.append(direction)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(max(1, int(limit)))
    return [dict(r) for r in _conn().execute(sql, args).fetchall()]


def upsert_entity(*, qq: str = "", gid: str = "", name: str = "",
                  aliases=None, kind: str = "person") -> str:
    init()
    qq = str(qq or "").strip()
    gid = str(gid or "").strip()
    eid = ("qq:" + qq) if qq else ("group:" + gid if gid else "")
    if not eid:
        return ""
    _al = [str(a) for a in (aliases or []) if str(a).strip()]
    with _LOCK:
        _conn().execute(
            "INSERT INTO entities (entity_id,kind,qq,gid,primary_name,aliases,"
            "style_id,updated_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(entity_id) DO UPDATE SET kind=excluded.kind, "
            "qq=excluded.qq, gid=excluded.gid, primary_name=excluded.primary_name, "
            "aliases=excluded.aliases, updated_at=excluded.updated_at",
            (eid, kind, qq, gid, str(name or "")[:40],
             json.dumps(_al, ensure_ascii=False), "", time.time()))
        _conn().commit()
    return eid


def note_edge(*, from_qq: str, to_qq: str, session: str = "",
              kind: str = "address", conf: float = 0.0, ts: float = 0) -> str:
    """记一条「谁 → 谁」的互动边（只在收发件人高置信时调用）。

    和 memories 分开：这是社交图，用来回答「他最近都在跟谁说话 / 她跟谁最常互动」。
    """
    from_qq = str(from_qq or "").strip()
    to_qq = str(to_qq or "").strip()
    if not (from_qq.isdigit() and to_qq.isdigit()) or from_qq == to_qq:
        return ""
    init()
    eid = uuid.uuid4().hex
    with _LOCK:
        _conn().execute(
            "INSERT INTO interaction_edges (id,ts,session,from_qq,to_qq,kind,conf) "
            "VALUES (?,?,?,?,?,?,?)",
            (eid, float(ts or time.time()), str(session or ""),
             from_qq, to_qq, str(kind or "address")[:16], float(conf or 0.0)))
        _conn().commit()
    return eid


def upsert_profile(*, qq: str, name: str = "", aliases=None,
                   affinity=None, intimacy=None, trust=None,
                   respect=None) -> bool:
    """档案镜像（retrieval §8）：profiles/*.json 的关键字段同步到 sqlite。"""
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return False
    init()
    now = time.time()
    with _LOCK:
        c = _conn()
        row = c.execute("SELECT * FROM profiles WHERE qq=?", (qq,)).fetchone()
        cur = dict(row) if row else {}
        vals = {
            "name": str(name or cur.get("name") or "")[:40],
            "aliases": json.dumps([str(a) for a in (aliases or []) if str(a).strip()],
                                  ensure_ascii=False) if aliases is not None
            else str(cur.get("aliases") or "[]"),
            "affinity": float(cur.get("affinity") or 0.0) if affinity is None else float(affinity),
            "intimacy": float(cur.get("intimacy") or 0.0) if intimacy is None else float(intimacy),
            "trust": float(cur.get("trust") or 0.2) if trust is None else float(trust),
            "respect": float(cur.get("respect") or 0.3) if respect is None else float(respect),
        }
        c.execute(
            "INSERT INTO profiles (qq,name,aliases,affinity,intimacy,trust,respect,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(qq) DO UPDATE SET "
            "name=excluded.name, aliases=excluded.aliases, affinity=excluded.affinity, "
            "intimacy=excluded.intimacy, trust=excluded.trust, respect=excluded.respect, "
            "updated_at=excluded.updated_at",
            (qq, vals["name"], vals["aliases"], vals["affinity"], vals["intimacy"],
             vals["trust"], vals["respect"], now))
        c.commit()
    return True


def profile_of(qq: str) -> dict:
    init()
    row = _conn().execute("SELECT * FROM profiles WHERE qq=?",
                          (str(qq or ""),)).fetchone()
    return dict(row) if row else {}


def add_style_sample(*, qq: str, gid: str = "", text: str,
                     quality: float = 0.5, ts: float = 0) -> str:
    qq = str(qq or "").strip()
    text = str(text or "").strip()
    if not qq.isdigit() or not text:
        return ""
    init()
    sid = uuid.uuid4().hex
    with _LOCK:
        _conn().execute(
            "INSERT INTO style_samples (id,qq,gid,text,quality,ts) VALUES (?,?,?,?,?,?)",
            (sid, qq, str(gid or ""), text[:120], float(quality or 0.5),
             float(ts or time.time())))
        _conn().commit()
    return sid


def _style_sim(a: dict, b: dict) -> float:
    """风格样本相似度：字符 bigram Jaccard + 长度桶/语气桶同质惩罚。"""
    ta = set(tokenize(str((a or {}).get("text") or "")))
    tb = set(tokenize(str((b or {}).get("text") or "")))
    jac = (len(ta & tb) / max(1, len(ta | tb))) if (ta or tb) else 0.0
    la = len(str((a or {}).get("text") or ""))
    lb = len(str((b or {}).get("text") or ""))
    same_len = 0.2 if (la <= 8) == (lb <= 8) else 0.0
    pa = any(ch in str((a or {}).get("text") or "") for ch in "？?！!…")
    pb = any(ch in str((b or {}).get("text") or "") for ch in "？?！!…")
    same_punct = 0.1 if pa == pb else 0.0
    return min(1.0, jac + same_len + same_punct)


def style_samples_of(qq: str, *, gid: str = "", limit: int = 3) -> list:
    """取这个人最像「说话方式」的 N 句：quality + recency，再做 MMR 去重。

    style-white-moon-design §5.3：λ=0.7，避免 3 句都是同一长度/同一语气。
    """
    init()
    lim = max(1, int(limit or 3))
    cand = max(lim * 4, lim + 8)
    if gid:
        rows = _conn().execute(
            "SELECT * FROM style_samples WHERE qq=? AND gid=? "
            "ORDER BY quality DESC, ts DESC LIMIT ?",
            (str(qq or ""), str(gid), cand)).fetchall()
    else:
        rows = _conn().execute(
            "SELECT * FROM style_samples WHERE qq=? "
            "ORDER BY quality DESC, ts DESC LIMIT ?",
            (str(qq or ""), cand)).fetchall()
    items = [dict(r) for r in rows]
    if not int(STYLE_MMR_ENABLED or 0) or len(items) <= lim:
        return items[:lim]
    ts = [float(x.get("ts") or 0) for x in items]
    t0, t1 = min(ts or [0]), max(ts or [0])
    span = max(1.0, t1 - t0)

    def _rel(x):
        rec = (float(x.get("ts") or 0) - t0) / span
        return 0.7 * float(x.get("quality") or 0.5) + 0.3 * rec

    from . import mmr
    return mmr.pick(items, lim, relevance=_rel, similarity=_style_sim,
                    lam=float(STYLE_MMR_LAMBDA or 0.7))


def upsert_thread(*, thread_id: str, gid: str, topic: str = "",
                  participants=None, last_ts: float = 0) -> bool:
    tid = str(thread_id or "").strip()
    if not tid:
        return False
    init()
    with _LOCK:
        _conn().execute(
            "INSERT INTO threads (thread_id,gid,topic,participants,last_ts) "
            "VALUES (?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET "
            "topic=excluded.topic, participants=excluded.participants, "
            "last_ts=excluded.last_ts",
            (tid, str(gid or ""), str(topic or "")[:60],
             json.dumps([str(x) for x in (participants or [])], ensure_ascii=False),
             float(last_ts or time.time())))
        _conn().commit()
    return True


def threads_of(gid: str, *, limit: int = 10) -> list:
    init()
    rows = _conn().execute(
        "SELECT * FROM threads WHERE gid=? ORDER BY last_ts DESC LIMIT ?",
        (str(gid or ""), int(limit or 10))).fetchall()
    return [dict(r) for r in rows]


def resolve_person(name: str) -> str:
    """称呼/别名 → QQ。找不到返回空串。"""
    n = str(name or "").strip()
    if not n:
        return ""
    if n.isdigit():
        return n
    init()
    c = _conn()
    r = c.execute("SELECT qq FROM entities WHERE primary_name=? AND qq!='' "
                  "LIMIT 1", (n,)).fetchone()
    if r:
        return str(r["qq"] or "")
    for r in c.execute("SELECT qq, aliases FROM entities WHERE qq!=''"):
        try:
            if n in (json.loads(r["aliases"] or "[]") or []):
                return str(r["qq"] or "")
        except Exception:
            continue
    return ""


def stats() -> dict:
    init()
    c = _conn()
    total = c.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
    gid_n = c.execute("SELECT count(DISTINCT gid) FROM memories WHERE gid!=''").fetchone()[0]
    sal = c.execute("SELECT count(*) FROM memories WHERE salience>=0.5").fetchone()[0]
    try:
        tagged = c.execute("SELECT count(DISTINCT memory_id) FROM memory_tags").fetchone()[0]
        ents = c.execute("SELECT count(*) FROM entities").fetchone()[0]
        mev = c.execute("SELECT count(*) FROM mood_events").fetchone()[0]
    except Exception:
        tagged = ents = mev = 0
    return {"total": total, "groups": gid_n, "strong": sal,
            "tagged": tagged, "tag_coverage": round(tagged / max(1, total), 4),
            "entities": ents, "mood_events": mev,
            "fts": bool(_INITED["fts"]), "db": str(DB_PATH)}
