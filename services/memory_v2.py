# services/memory_v2.py
"""
记忆系统：
  - 目录（index.json）：清晰的结构，带标签、时间戳、重要性、情绪强度
  - 细节（blocks/{id}.json）：模糊摘要，按需再取
  - 检索：BM25（中文二元切词） + 遗忘曲线 + 说话人相关 + 情绪刻痕加权
  - 过期：标 status="outdated"，不删
"""
from core import tuning
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# recall 返回的原文长度上限。
# block 里存的是完整原文（text_full，最多 2000 字），但一次召回 4~5 条全塞进
# prompt 会直接把预算吃光，所以这里截断；复盘要全文时读 text_full 字段。
RECALL_TEXT_MAX = tuning.i("RECALL_TEXT_MAX")

MEM_DIR = Path("data/memory")
BLOCK_DIR = MEM_DIR / "blocks"
MEM_DIR.mkdir(parents=True, exist_ok=True)
BLOCK_DIR.mkdir(parents=True, exist_ok=True)
INDEX_FILE = MEM_DIR / "index.json"

# 存储后端：json（现状，默认） / sqlite（services/memdb.py）
# 切换方式：MEMORY_BACKEND=sqlite；回滚：改回 json，原 JSON 文件不删。
BACKEND = (os.getenv("MEMORY_BACKEND", "json") or "json").strip().lower()


# ================== 基础存储 ==================
def _load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"items": []}


# 索引是「全量读-改-写」的：进程被 kill / 磁盘满 / 写到一半，
# 直接 write_text 会把整个记忆库写成半截 JSON，下次 _load_index 解析失败
# 就静默返回空索引 —— 看起来像「记忆全丢了」。
# 所以统一 tmp + os.replace（原子替换）+ 进程内锁。
_INDEX_LOCK = threading.Lock()


def _save_index(idx: dict):
    with _INDEX_LOCK:
        tmp = INDEX_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, INDEX_FILE)


# ================== 中文二元切词 ==================
_CJK = re.compile(r"[\u4e00-\u9fa5]")
_WORD = re.compile(r"[a-zA-Z0-9]+")


def tokenize(text: str) -> list[str]:
    text = (text or "").lower()
    out = []
    # 英文数字整词
    out.extend(_WORD.findall(text))
    # 中文二元
    cjk_chars = [c for c in text if _CJK.match(c)]
    for i in range(len(cjk_chars) - 1):
        out.append(cjk_chars[i] + cjk_chars[i + 1])
    # 单字也留
    out.extend(cjk_chars)
    return out


# ================== 添加一条记忆 ==================
def remember(
    *,
    text: str,
    kind: str = "fact",
    about_qq: str = "",
    tags: list[str] | None = None,
    gid: str = "",
    session: str = "",
    importance: float = 0.5,
    emotion_hit: bool = False,
    emotion_weight: float = 0.0,
    mood_word: str = "",
    mood_value: float | None = None,
    # 2026-11-20：记忆的「刻痕」来自**涨幅 Δ**，不是当前心情水平。
    #   delta   —— 这件事让她的心情变了多少（-2~+2，判读原值之差）
    #   salience—— min(1, |delta|/0.5)，决定 💥 档位 / 检索加成 / 遗忘速度
    #   valence —— sign(delta)，好/坏
    delta: float | None = None,
    salience: float | None = None,
    valence: float | None = None,
    turn_id: str = "",
    # 她当时的心理活动（判读模型推断的 mind）：档案里「印象最深的一件事」
    # 要连这句话一起给她看（用户 2026-11-20）。
    mind: str = "",
) -> str:
    """
    返回这条记忆的 id。
    - 与已有记忆完全重复（同 kind + 同 about + text 相同）→ 复用旧 id，不重复写
    - 相似度很高（>=0.85）→ 也合并到旧记忆

    emotion_weight：情绪强度 0~1（维度②）。不传但 `emotion_hit=True` 时按 0.6 算，
    兼容老的布尔写法。
    """
    if BACKEND == "sqlite":
        from . import memdb
        return memdb.remember(
            text=text, kind=kind, about_qq=about_qq, tags=tags,
            importance=importance, emotion_hit=emotion_hit,
            emotion_weight=emotion_weight, mood_word=mood_word,
            mood_value=mood_value, gid=gid, session=session,
            delta=delta, salience=salience, valence=valence,
            turn_id=turn_id, mind=mind,
        )
    now = time.time()
    text = text.strip()
    # 情绪强度（= 刻痕）统一入口：**salience（涨幅换算）优先**，
    # 其次显式 weight，最后老的布尔写法。
    _ew = max(float(emotion_weight or 0), 0.6 if emotion_hit else 0.0)
    if salience is not None:
        _ew = max(_ew, float(salience))
    _ew = max(0.0, min(1.0, _ew))
    _delta = None if delta is None else round(float(delta), 4)
    _val = (float(valence) if valence is not None
            else (0.0 if _delta in (None, 0) else (1.0 if _delta > 0 else -1.0)))
    _ehit = bool(emotion_hit) or _ew >= 0.5
    tokens = tokenize(text)
    token_set = set(tokens)

    idx = _load_index()

    # 1) 完全相同 → 复用旧 id，只更新 importance / last_hit_at
    for d in idx["items"]:
        if d.get("status") != "active":
            continue
        if d.get("kind") == kind and d.get("about_qq") == about_qq and d["summary"] == text[:80]:
            # 命中完全相同
            d["last_hit_at"] = now
            d["importance"] = max(d.get("importance", 0.5), importance)
            if emotion_hit:
                d["emotion_hit"] = True
            # 同一条事被再次提起：刻痕取更强的那次（涨幅大的那次更"记忆犹新"）
            d["emotion_weight"] = max(_emo_of(d), _ew)
            if _delta is not None:
                d["delta"] = _delta
                d["salience"] = max(float(d.get("salience") or 0), _ew)
                d["valence"] = _val
            if mind:
                d["mind"] = str(mind)[:100]
            _save_index(idx)
            # 顺便把完整版也更新一下（保留 timestamp 追加）
            f = BLOCK_DIR / f"{d['id']}.json"
            try:
                full = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                full = {"id": d["id"], "text_full": ""}
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
            full["text_full"] = (full.get("text_full", "") + f"\n[{stamp}] {text}")[-2000:]
            if mind:
                full["mind"] = str(mind)[:100]
            f.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
            return d["id"]

    # 2) 高度相似（Jaccard >= 0.85）→ 合并进旧记忆
    best_id = ""
    best_sim = 0.0
    for d in idx["items"]:
        if d.get("status") != "active":
            continue
        if d.get("kind") != kind or d.get("about_qq") != about_qq:
            continue
        dt = set(d.get("tokens", []))
        if not dt or not token_set:
            continue
        inter = len(token_set & dt)
        union = len(token_set | dt)
        sim = inter / union
        if sim > best_sim:
            best_sim = sim
            best_id = d["id"]

    if best_id and best_sim >= 0.85:
        d = next(x for x in idx["items"] if x["id"] == best_id)
        d["last_hit_at"] = now
        d["importance"] = max(d.get("importance", 0.5), importance)
        if _ehit:
            d["emotion_hit"] = True
        d["emotion_weight"] = max(_emo_of(d), _ew)
        if _delta is not None:
            d["delta"] = _delta
            d["salience"] = max(float(d.get("salience") or 0), _ew)
            d["valence"] = _val
        if mind:
            d["mind"] = str(mind)[:100]
        _save_index(idx)
        f = BLOCK_DIR / f"{best_id}.json"
        try:
            full = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            full = {"id": best_id, "text_full": ""}
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
        full["text_full"] = (full.get("text_full", "") + f"\n[{stamp}] {text}")[-2000:]
        if mind:
            full["mind"] = str(mind)[:100]
        f.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
        return best_id

    # 3) 全新的记忆 → 写
    mid = uuid.uuid4().hex[:12]
    full = {
        "id": mid,
        "text_full": text,
        "kind": kind,
        "about_qq": str(about_qq),
        "tags": tags or [],
        "importance": float(importance),
        "emotion_hit": _ehit,
        "emotion_weight": _ew,      # 维度②：情绪强度 0~1
        # 👇 2026-11-20：记下「她当时的心里话 + 第三方判读的心情」。
        #    用户：「包括我们后续的记忆记录，以及档案记录，可以记录心里话加上 llm 判断的心情。」
        "mood_word": str(mood_word or "")[:20],
        "mood_value": (None if mood_value is None else float(mood_value)),
        "mind": str(mind or "")[:100],
        # 刻痕三件套：涨幅 / 强度 / 正负（都来自 Δ，不是心情水平）
        "delta": _delta,
        "salience": _ew,
        "valence": _val,
        "turn_id": str(turn_id or ""),
        "created_at": now,
        "last_hit_at": now,
        "hit_count": 0,
        "status": "active",
        "summary": text[:80],
    }
    (BLOCK_DIR / f"{mid}.json").write_text(
        json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    idx["items"].append({
        "id": mid,
        "summary": full["summary"],
        "kind": kind,
        "about_qq": full["about_qq"],
        "tags": full["tags"],
        "importance": full["importance"],
        "emotion_hit": full["emotion_hit"],
        "emotion_weight": full["emotion_weight"],
        "mood_word": full["mood_word"],
        "mind": full["mind"],
        "delta": full["delta"],
        "salience": full["salience"],
        "valence": full["valence"],
        "turn_id": full["turn_id"],
        "created_at": now,
        "last_hit_at": now,
        "hit_count": 0,
        "status": "active",
        "tokens": tokens,
    })
    _save_index(idx)
    return mid

def upsert_turn(turn: dict) -> str:
    """消息到达先落一条 pending（turn_id 幂等）。判读回来再 patch_turn。"""
    if BACKEND == "sqlite":
        from . import memdb
        return memdb.upsert_turn(turn)
    # JSON 后端也要带 turn_id：否则判读回来的 patch_turn 找不到这条，
    # 刻痕（Δ / 💥 / 好感依据）就永远写不进去。
    return remember(
        text=str(turn.get("detail") or turn.get("text") or ""),
        kind=str(turn.get("kind") or "event"),
        about_qq=str(turn.get("about_qq") or ""),
        importance=float(turn.get("importance") or 0.5),
        gid=str(turn.get("gid") or ""),
        session=str(turn.get("session") or ""),
        turn_id=str(turn.get("turn_id") or ""),
    )


_PATCHABLE = ("delta", "salience", "valence", "mood_word", "mood_value",
              "mind", "title", "importance")


def patch_turn(turn_id: str, **fields) -> bool:
    """判读回来后补 delta/salience/valence/mood_word/mind/title。

    ⚠️ json 后端以前直接 return False —— 等于**默认部署下刻痕从来没写进去过**。
    现在两个后端都支持。
    """
    if BACKEND == "sqlite":
        from . import memdb
        return memdb.patch_turn(turn_id, **fields)
    tid = str(turn_id or "").strip()
    if not tid:
        return False
    idx = _load_index()
    hit = None
    for d in idx.get("items", []):
        if str(d.get("turn_id") or "") == tid:
            hit = d
            break
    if hit is None:
        return False
    for k in _PATCHABLE:
        if k in fields and fields[k] is not None:
            hit[k] = fields[k]
    # salience 同时喂给 emotion_weight（💥 档位 / 检索加成 / 遗忘都读它）
    if fields.get("salience") is not None:
        hit["emotion_weight"] = max(float(hit.get("emotion_weight") or 0),
                                    float(fields["salience"]))
        hit["emotion_hit"] = float(hit["emotion_weight"]) >= 0.5
    _save_index(idx)
    # block 里也补一份，展开时能看到刻痕
    try:
        f = BLOCK_DIR / f"{hit['id']}.json"
        full = json.loads(f.read_text(encoding="utf-8"))
        for k in _PATCHABLE:
            if k in fields and fields[k] is not None:
                full[k] = fields[k]
        f.write_text(json.dumps(full, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    except Exception:
        pass
    return True


def deepest_event(about_qq: str, *, min_abs: float = 0.15,
                  max_age_days: float = 0.0) -> dict:
    """「你与这个人印象最深刻的一件事」：记忆里 |Δ| 最大的那件事。

    用户 2026-11-20：「好感名词后面加一件与这个人记忆最深的一件事，
    并加上这件事当时 api 的心理活动。」
    所以这里返回的 mind 是判读模型当时推断的心里话（没有就空）。

    返回 {text, mind, delta, at}；没有就 {}。
    """
    qq = str(about_qq or "").strip()
    if not qq:
        return {}
    if BACKEND == "sqlite":
        from . import memdb
        return memdb.deepest_event(qq, min_abs=min_abs,
                                   max_age_days=max_age_days)
    now = time.time()
    idx = _load_index()
    best = None
    for d in idx.get("items") or []:
        if d.get("status") != "active":
            continue
        if str(d.get("about_qq") or "") != qq:
            continue
        try:
            dv = float(d.get("delta"))
        except Exception:
            continue
        if abs(dv) < float(min_abs):
            continue
        if max_age_days and (now - float(d.get("created_at") or 0)
                             > float(max_age_days) * 86400):
            continue
        if best is None or abs(dv) > abs(float(best.get("delta") or 0)):
            best = d
    if not best:
        return {}
    text = str(best.get("summary") or "")
    mind = str(best.get("mind") or "")
    # 老索引没有 mind / summary → 从 block 里补
    if not mind or not text:
        try:
            full = json.loads(
                (BLOCK_DIR / f"{best['id']}.json").read_text(encoding="utf-8"))
            text = text or str(full.get("summary") or full.get("text_full") or "")
            mind = mind or str(full.get("mind") or "")
        except Exception:
            pass
    return {"text": text[:80], "mind": mind[:100],
            "delta": round(float(best.get("delta") or 0), 3),
            "at": float(best.get("created_at") or 0)}


# ================== 检索：BM25 + 遗忘 + 说话人 + 情绪 ==================
def _bm25_scores(query_tokens: list[str], docs: list[dict], k1=1.5, b=0.75) -> dict:
    """返回 {doc_id: score}"""
    if not query_tokens or not docs:
        return {}
    n_docs = len(docs)
    avgdl = sum(len(d["tokens"]) for d in docs) / max(n_docs, 1)
    # 文档频率
    df = {}
    for d in docs:
        for t in set(d["tokens"]):
            df[t] = df.get(t, 0) + 1

    scores = {}
    for d in docs:
        tf = {}
        for t in d["tokens"]:
            tf[t] = tf.get(t, 0) + 1
        dl = len(d["tokens"])
        s = 0.0
        for q in set(query_tokens):
            if q not in tf:
                continue
            idf = math.log(1 + (n_docs - df.get(q, 0) + 0.5) / (df.get(q, 0) + 0.5))
            tfn = tf[q] * (k1 + 1) / (tf[q] + k1 * (1 - b + b * dl / avgdl))
            s += idf * tfn
        if s > 0:
            scores[d["id"]] = s
    return scores


# ============================================================
# 二维记忆：时间 × 情绪强度
# ============================================================
# 用户 2026-11-11：「记忆是按新到旧的，越新越清晰，这没问题，但是我们的情绪刻痕
# 是另一个维度的。也可以加上，二维的记忆。」
#
# 原来只有「emotion_hit 是/否」一个布尔 + 固定 1.8 倍权重 —— 那不是维度，是开关。
# 现在两个维度都参与：
#   维度① 时间：遗忘曲线，越久没想起权重越低
#   维度② 情绪：情绪越强，衰减越慢（闪光灯记忆效应），检索权重也越高
#
# 效果举例（都是 60 天前的记忆）：
#   中性记忆   exp(-60/30)      = 0.135 → 基本搜不到
#   强情绪记忆 exp(-60/(30*3))  = 0.513 → 还能翻出来
EMOTION_WEIGHT = {
    "love": 1.0, "touched": 0.9, "感动": 0.9,
    "excited": 0.8, "angry": 0.8, "sad": 0.8, "hurt": 0.85,
    "happy": 0.5, "annoyed": 0.4, "tired": 0.2,
    "neutral": 0.0,
}
# 情绪强度的可视化标记（给 prompt 用，让她自己也能"看到"哪条更重）
_EMO_MARKS = ((0.8, "💥💥💥"), (0.5, "💥💥"), (0.2, "💥"))


def emotion_weight_of(emotion: str) -> float:
    """情绪名 → 强度 0~1。认不出的按中等处理（0.5）。"""
    e = str(emotion or "").strip().lower()
    if not e:
        return 0.0
    return float(EMOTION_WEIGHT.get(e, 0.5))


def emotion_mark(w: float) -> str:
    """强度 → 标记。0 → 空串（不标），越强越多爆炸。"""
    w = float(w or 0)
    for threshold, mark in _EMO_MARKS:
        if w >= threshold:
            return mark
    return ""


def _emo_of(d: dict) -> float:
    """
    取一条记忆的情绪强度。

    兼容老数据：只有 `emotion_hit: true` 没有 `emotion_weight` 的，
    按 0.6 算（中等偏强）—— 至少不能让它比新数据吃亏。
    """
    if "emotion_weight" in d:
        try:
            return max(0.0, min(1.0, float(d.get("emotion_weight") or 0)))
        except Exception:
            return 0.0
    return 0.6 if d.get("emotion_hit") else 0.0


def _forget_factor(now: float, last_hit: float, emotion_weight: float = 0.0) -> float:
    """
    越久没被想起 → 权重越低（但不会归零）。

    维度②：情绪越强，衰减越慢 —— `30 * (1 + 2*emo)` 天的时间常数：
      emo=0 → 30 天（30 天后 37%）
      emo=0.5 → 60 天
      emo=1.0 → 90 天（90 天后才 37%）
    """
    days = max(0.0, (now - last_hit) / 86400.0)
    tau = 30.0 * (1.0 + 2.0 * max(0.0, min(1.0, float(emotion_weight or 0))))
    return math.exp(-days / tau)


def recall(
    query: str,
    *,
    limit: int = 5,
    speaker_qq: str = "",
    min_score: float = 0.05,
    max_age_days: float = 0.0,
    gid: str = "",
    mark_hit: bool = True,
) -> list[dict]:
    """
    检索相关记忆：BM25 + 说话人加权 + 重要性 + 情绪刻痕 + 遗忘曲线。

    max_age_days：只取 N 天内的记忆（0 = 不限）。
    用于「本地管近期、算力机图书馆管久远」的分工：
      30 天以前的记忆本地已经衰减到 37% 以下，交给 Agent 语义检索更合适。
    ⚠️ 但**高重要性 / 带情绪刻痕的记忆不受时间限制** ——
      「她奶奶去世了」这种 40 天前的记忆，不能因为过期就搜不到。

    gid：当前群号。带「群号:XXX」的记忆只在同一个群里用；
         图片识别产生的临时记录（`[图片识别结果]`）一律不进召回。

    mark_hit：命中后是否更新 last_hit_at / hit_count（影响遗忘曲线）。
      ⚠️ 只有「真的要注入 prompt」时才该计命中。目录浏览（recall_catalog）
        和翻页不算 —— 否则「越常被翻目录的记忆越不遗忘」，
        遗忘曲线会被自己的检索行为污染（2026-11-17 审计）。
    """
    if BACKEND == "sqlite":
        from . import memdb
        return memdb.recall(query, limit=limit, speaker_qq=speaker_qq,
                            min_score=min_score, max_age_days=max_age_days,
                            gid=gid, mark_hit=mark_hit)
    idx = _load_index()
    docs = [d for d in idx["items"] if d.get("status") == "active"]
    if not docs:
        return []
    # 噪音先剔掉，别让它占 limit 名额（剔完再打分，省得白算）
    docs = [d for d in docs
            if not is_noise(d.get("summary") or "")
            and group_ok(d.get("summary") or "", gid)]

    qt = tokenize(query)
    bm25 = _bm25_scores(qt, docs)
    # 如果 query 太短没命中，也允许"未命中但重要"的记忆带一点分
    now = time.time()
    scored = []
    for d in docs:
        # 时间窗过滤（重要 / 带情绪的不受限）
        if max_age_days and max_age_days > 0:
            created = float(d.get("created_at") or 0)
            if created > 0 and (now - created) / 86400.0 > max_age_days:
                if not (d.get("importance", 0) >= 0.8 or d.get("emotion_hit")):
                    continue

        # 2026-11-20 审计 F1：BM25 以前是硬门槛（base<=0 且 importance<0.7 直接丢），
        # 导致「在吗」召不出昨天的事 ——「近期记得清」没实现。
        # 现在 BM25 只做**加分**；时间维度（w_forget）自然把远期压下去。
        _raw = bm25.get(d["id"], 0.0)
        # 0.03 的底分：高情绪/高重要性的近期记忆能过 min_score=0.05，
        # 普通无关键词的旧记忆仍会被时间衰减压下去（不会把 prompt 灌满）。
        base = 0.03 + min(1.0, _raw / 3.0)

        w_speaker = 1.0
        if speaker_qq and d.get("about_qq") == str(speaker_qq):
            w_speaker = 2.0  # 关于当前说话人 → 权重翻倍

        w_imp = 0.5 + d.get("importance", 0.5)  # 0.5~1.5

        # 维度②：情绪强度参与打分（0 → 1.0 倍，1 → 3.5 倍）
        _ew = _emo_of(d)
        w_emo = 1.0 + 2.5 * _ew

        # 维度①：时间衰减，但情绪越强衰减越慢
        w_forget = _forget_factor(now, d.get("last_hit_at", now), _ew)

        score = base * w_speaker * w_imp * w_emo * w_forget
        if score >= min_score:
            scored.append((score, d))

    scored.sort(key=lambda x: -x[0])
    top = scored[:limit]

    # 命中 → 更新 last_hit_at / hit_count（浏览目录不算，见 mark_hit 说明）
    if top and mark_hit:
        hit_ids = {d["id"] for _, d in top}
        for d in idx["items"]:
            if d["id"] in hit_ids:
                d["last_hit_at"] = now
                d["hit_count"] = d.get("hit_count", 0) + 1
        _save_index(idx)

    # 拼装细节
    # ⚠️ 这里必须读 block 里的 text_full：索引里的 summary 只有前 80 字，
    #    只回 summary 的话，复盘/「查一下我们之前说的」永远只能看到半句话。
    results = []
    for score, d in top:
        detail = ""
        f = BLOCK_DIR / f"{d['id']}.json"
        if f.exists():
            try:
                detail = str(json.loads(f.read_text(encoding="utf-8")).get("text_full") or "")
            except Exception:
                detail = ""
        results.append({
            "id": d["id"],
            "summary": d["summary"],
            # text = 原文（截到 RECALL_TEXT_MAX，防止把 prompt 撑爆）
            "text": (detail.strip() or d["summary"])[:RECALL_TEXT_MAX],
            "text_full": detail,
            "kind": d["kind"],
            "about_qq": d.get("about_qq", ""),
            "emotion_hit": d.get("emotion_hit", False),
            # 维度②：情绪强度 + 可视化标记（💥 / 💥💥 / 💥💥💥）
            "emotion_weight": _emo_of(d),
            "emotion_mark": emotion_mark(_emo_of(d)),
            "created_at": d["created_at"],
            "time_str": time.strftime("%m-%d %H:%M", time.localtime(d["created_at"])),
            "ago": _humanize_ago(now - d["created_at"]),
            "score": score,
        })
    return results


# ================== 目录模式 + 单条展开（[[MEMORY|...|翻页N]] / [[MEMORY|展开|编号]]） ==================
# 用户 2026-11-16：「不展开时希望有十件记录，每件事标上时间和当时的心情；
# 想看详细在某条后加「详细」展开；可以翻页直到翻完；也可以关键字直搜那段记忆。」
def recall_catalog(query: str, *, offset: int = 0, limit: int = 10,
                   speaker_qq: str = "", gid: str = "", time_range=None) -> dict:
    """目录模式：只回「编号 + 时间 + 心情 + 摘要」，不展开全文，省 token。

    time_range=(start_ts, end_ts) 时按时间召回（「前天上午」「昨天」这种），
    越新越靠前；否则走关键词 BM25 检索。
    """
    if BACKEND == "sqlite":
        from . import memdb
        return memdb.recall_catalog(query, offset=offset, limit=limit,
                                    speaker_qq=speaker_qq, gid=gid,
                                    time_range=time_range)
    # 2026-11-20：目录默认「时间倒序（从近到远）」；有关键词才按相关度。
    if not str(query or "").strip() and not time_range:
        idx = _load_index()
        rows = [d for d in idx.get("items", []) if d.get("status") == "active"]
        rows.sort(key=lambda d: -float(d.get("created_at") or 0))
        total = len(rows)
        page = rows[offset:offset + limit]
        now_ts = time.time()
        items = []
        for i, d in enumerate(page, start=offset + 1):
            _ts = float(d.get("created_at") or 0)
            items.append({
                "no": i, "id": d["id"],
                "summary": str(d.get("summary") or "")[:60],
                "ago": _humanize_ago(now_ts - _ts),
                "time_str": time.strftime("%m-%d %H:%M", time.localtime(_ts)),
                "emotion_mark": emotion_mark(_emo_of(d)) or "·",
            })
        return {"total": total, "items": items, "mode": "time"}

    if time_range:
        hits = recall_by_time(time_range[0], time_range[1],
                              speaker_qq=speaker_qq, gid=gid,
                              keyword=query)
        total = len(hits)
        page = hits[offset:offset + limit]
        items = []
        for i, h in enumerate(page, start=offset + 1):
            items.append({
                "no": i,
                "id": h["id"],
                "summary": str(h.get("summary") or "")[:60],
                "ago": h.get("ago") or "",
                "time_str": h.get("time_str") or "",
                "emotion_mark": h.get("emotion_mark") or "·",
            })
        return {"total": total, "items": items}

    # 一次多取 offset+limit 条再切片（recall 内部是全局排序后取 top N）
    # 目录浏览不计命中：翻一遍目录不该让这些记忆"变得更难忘"
    hits = recall(query, speaker_qq=speaker_qq, limit=offset + limit, gid=gid,
                  mark_hit=False)
    total = len(hits)
    items = []
    for i, h in enumerate(hits[offset:offset + limit], start=offset + 1):
        items.append({
            "no": i,
            "id": h["id"],
            "summary": str(h.get("summary") or "")[:60],
            "ago": h.get("ago") or "",
            "emotion_mark": h.get("emotion_mark") or "·",
        })
    return {"total": total, "items": items}


def revisit_candidate(about_qq: str, *, min_age_days: float | None = None,
                      min_idle_days: float | None = None, min_emotion: float = 0.5):
    """
    找一条「可以自然提一句」的旧事（记忆回访用）。

    条件：关于这个人、至少 7 天以前、至少 3 天没被想起、情绪刻痕够重（或重要性高）。
    只返回一条，让模型自己决定提不提 —— 不塞满，不然会变成翻旧账。

    天数走 env（默认 7 / 3，用户 2026-11-17 定）：
      MEMORY_REVISIT_AGE_DAYS   记忆至少多老（太新的不用「回访」，聊天里自然会提到）
      MEMORY_REVISIT_IDLE_DAYS  至少多久没被想起（防止连续几轮翻同一件旧事）
    """
    if not about_qq:
        return None
    if min_age_days is None:
        try:
            min_age_days = tuning.f("MEMORY_REVISIT_AGE_DAYS")
        except Exception:
            min_age_days = 7.0
    if min_idle_days is None:
        try:
            min_idle_days = tuning.f("MEMORY_REVISIT_IDLE_DAYS")
        except Exception:
            min_idle_days = 3.0
    try:
        idx = _load_index()
    except Exception:
        return None
    now = time.time()
    best, best_score = None, 0.0
    for d in idx.get("items", []):
        if d.get("status") != "active":
            continue
        if str(d.get("about_qq") or "") != str(about_qq):
            continue
        if is_noise(d.get("summary") or ""):
            continue
        created = float(d.get("created_at") or 0)
        last_hit = float(d.get("last_hit_at") or created)
        if created <= 0 or (now - created) / 86400.0 < min_age_days:
            continue
        if (now - last_hit) / 86400.0 < min_idle_days:
            continue
        emo = _emo_of(d)
        imp = float(d.get("importance") or 0)
        if emo < min_emotion and imp < 0.8:
            continue
        score = emo * 2.0 + imp
        if score > best_score:
            best, best_score = d, score
    if not best:
        return None
    return {
        "id": best["id"],
        "summary": str(best.get("summary") or "")[:60],
        "ago": _humanize_ago(now - float(best.get("created_at") or now)),
        "emotion_mark": emotion_mark(_emo_of(best)),
    }


def touch(mid: str) -> None:
    """把一条记忆标记为「刚想起过」（回访注入后调用，避免连续几轮都提同一件事）。"""
    if BACKEND == "sqlite":
        from . import memdb
        memdb.touch(mid)
        return
    try:
        idx = _load_index()
        for d in idx.get("items", []):
            if d.get("id") == mid:
                d["last_hit_at"] = time.time()
                d["hit_count"] = int(d.get("hit_count") or 0) + 1
                _save_index(idx)
                return
    except Exception:
        pass


def recall_detail(mid: str) -> str:
    """展开单条：返回该条完整原文（text_full，不截断）。"""
    if BACKEND == "sqlite":
        from . import memdb
        return memdb.recall_detail(mid)
    try:
        f = BLOCK_DIR / f"{mid}.json"
        if f.exists():
            return str(json.loads(f.read_text(encoding="utf-8")).get("text_full") or "").strip()
    except Exception:
        pass
    return ""


# ================== 按时间搜索（用户 2026-11-16）==================
# 「鱼宝，你记得前天上午的事情吗」→ 把「前天上午」转成绝对时间范围，
# 直接翻那段时间的记忆/档案，而不是靠关键词瞎猜。
_TIME_WORDS_RE = re.compile(
    r"\d+\s*天\s*[以之]?前"
    r"|\d+\s*(?:个)?(?:小时|钟头)\s*[以之]?前"
    r"|大前天|前天|昨天|今天|今早|今晨"
    r"|上午|中午|下午|傍晚|晚上|凌晨|深夜|早上|早晨"
    r"|上上周|上周|上个星期"
)


def parse_time_expression(text: str, now: float = None):
    """把「前天上午 / 昨天 / 大前天 / N天前 / N小时前 / 上周」等时间词转成
    (start_ts, end_ts) 绝对时间戳（左闭右开）。认不出返回 None。"""
    now = now or time.time()
    t = datetime.fromtimestamp(now)
    today_start = t.replace(hour=0, minute=0, second=0, microsecond=0)
    txt = str(text or "")

    # 1) 相对天数：N天前 / 大前天 / 前天 / 昨天 / 今天
    day_offset = None
    m = re.search(r"(\d+)\s*天\s*[以之]?前", txt)
    if m:
        day_offset = int(m.group(1))
    elif "大前天" in txt:
        day_offset = 3
    elif "前天" in txt:
        day_offset = 2
    elif "昨天" in txt:
        day_offset = 1
    elif "今天" in txt or "今早" in txt or "今晨" in txt:
        day_offset = 0

    if day_offset is not None:
        day_start = today_start - timedelta(days=day_offset)
        start, end = day_start, day_start + timedelta(days=1)
        if "上午" in txt or "早上" in txt or "早晨" in txt:
            start, end = day_start.replace(hour=6), day_start.replace(hour=12)
        elif "中午" in txt:
            start, end = day_start.replace(hour=11), day_start.replace(hour=13)
        elif "下午" in txt:
            start, end = day_start.replace(hour=12), day_start.replace(hour=18)
        elif "晚上" in txt or "傍晚" in txt:
            start = day_start.replace(hour=18)
            end = day_start.replace(hour=23, minute=59, second=59)
        elif "凌晨" in txt:
            start, end = day_start.replace(hour=0), day_start.replace(hour=6)
        elif "深夜" in txt:
            start = day_start.replace(hour=22)
            end = day_start.replace(hour=23, minute=59, second=59)
        return start.timestamp(), end.timestamp()

    # 2) N 小时前
    m = re.search(r"(\d+)\s*(?:个)?(?:小时|钟头)\s*[以之]?前", txt)
    if m:
        h = int(m.group(1))
        return (datetime.fromtimestamp(now) - timedelta(hours=h)).timestamp(), now

    # 3) 上周 / 上上周
    this_monday = today_start - timedelta(days=today_start.weekday())
    if "上上周" in txt:
        return ((this_monday - timedelta(days=14)).timestamp(),
                (this_monday - timedelta(days=7)).timestamp())
    if "上周" in txt or "上个星期" in txt:
        return ((this_monday - timedelta(days=7)).timestamp(), this_monday.timestamp())

    return None


def strip_time_words(text: str) -> str:
    """把时间词从查询里剥掉，剩下来的作为可选关键词。"""
    return _TIME_WORDS_RE.sub(" ", str(text or "")).strip()


def recall_by_time(start_ts: float, end_ts: float, *,
                   speaker_qq: str = "", gid: str = "",
                   keyword: str = "") -> list[dict]:
    """按时间范围召回记忆，越新越靠前。可再叠加 keyword 做文本过滤。"""
    idx = _load_index()
    docs = [d for d in idx["items"] if d.get("status") == "active"]
    kw_tokens = set(tokenize(keyword)) if keyword else set()
    now = time.time()
    out = []
    for d in docs:
        created = float(d.get("created_at") or 0)
        if created <= 0 or created < start_ts or created >= end_ts:
            continue
        if is_noise(d.get("summary") or ""):
            continue
        if not group_ok(d.get("summary") or "", gid):
            continue
        if kw_tokens and not (kw_tokens & set(d.get("tokens", []))):
            continue
        out.append(d)
    out.sort(key=lambda d: -float(d.get("created_at") or 0))

    results = []
    for d in out:
        detail = ""
        f = BLOCK_DIR / f"{d['id']}.json"
        if f.exists():
            try:
                detail = str(json.loads(f.read_text(encoding="utf-8")).get("text_full") or "")
            except Exception:
                detail = ""
        results.append({
            "id": d["id"],
            "summary": d["summary"],
            "text": (detail.strip() or d["summary"])[:RECALL_TEXT_MAX],
            "text_full": detail,
            "kind": d["kind"],
            "about_qq": d.get("about_qq", ""),
            "emotion_hit": d.get("emotion_hit", False),
            "emotion_weight": _emo_of(d),
            "emotion_mark": emotion_mark(_emo_of(d)),
            "created_at": d["created_at"],
            "time_str": time.strftime("%m-%d %H:%M", time.localtime(d["created_at"])),
            "ago": _humanize_ago(now - d["created_at"]),
            "score": 0.0,
        })
    return results


def _humanize_ago(seconds: float) -> str:
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds / 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds / 3600)} 小时前"
    return f"{int(seconds / 86400)} 天前"


# ================== 标记过期（不删） ==================
def mark_outdated(mid: str):
    idx = _load_index()
    for d in idx["items"]:
        if d["id"] == mid:
            d["status"] = "outdated"
            d["outdated_at"] = time.time()
            break
    _save_index(idx)


def restore(mid: str):
    idx = _load_index()
    for d in idx["items"]:
        if d["id"] == mid:
            d["status"] = "active"
            d.pop("outdated_at", None)
            break
    _save_index(idx)


# ================== 记忆过滤（别让噪音挤掉有用的）==================
_NOISE_PREFIXES = (
    "[图片识别结果]", "图片识别结果", "[图片]", "【图片识别结果】",
    # 视频解析/抽帧描述同样是「一次性的上下文」，不是记忆。
    # ⚠️ 只过滤**解析产物**；`看过的B站视频《…》` 那种总结是要长期留的，不能一起清掉。
    "[视频解析结果]", "视频解析结果", "【视频解析结果】",
)
_GROUP_RE = re.compile(r"群号[:：]\s*(\d+)")


def is_noise(text: str) -> bool:
    """
    图片识别的临时产物**不是记忆**，进 prompt 只会挤掉真正有用的东西。

    真机实测：群 100000011 的【相关记忆】里 400 字全是
    「· 蕾米埃尔 鹏：[图片识别结果] 图片内容：…（21 分钟前）」×4，
    一条有用信息都没有，还占掉上下文预算。
    """
    t = str(text or "").lstrip()
    return t.startswith(_NOISE_PREFIXES)


def group_ok(text: str, gid: str) -> bool:
    """
    带「群号:XXX」的记忆只能在同一个群里用。

    真机踩过：群 100000020 的图片识别记忆跑进了群 100000011 的 prompt。
    没标群号的（私聊记忆、通用事实）一律放行。
    """
    if not gid:
        return True
    m = _GROUP_RE.search(str(text or ""))
    if not m:
        return True
    return m.group(1) == str(gid)


# ================== 给 Prompt 用 ==================
def memory_prompt(query: str, speaker_qq: str = "", limit: int = 4,
                  gid: str = "") -> str:
    hits = recall(query, speaker_qq=speaker_qq, limit=limit, gid=gid)
    if not hits:
        return ""
    # 二维展示：时间在前（ago），情绪强度用 💥 数量标在后面
    #
    # ⚠️ 标题必须和【历史对话】区分开（2026-11-12）：
    #    这一块是 **BM25 检索出来的长期记忆**（以前攒下的印象/事实），
    #    不是「刚才聊的内容」。以前两块标题都像「你记得」，她容易把旧印象
    #    当成刚发生的事，或者反过来以为刚才的对话也算「记忆」。
    lines = ["你长期记住的事（两维排的：越新越靠前；💥 越多＝当时情绪越强、越难忘）："
             "\n（⚠️ 这些是**以前攒下来的**，不是刚才的对话；刚才聊的看【历史对话】那块）"]
    for h in hits:
        mark = h.get("emotion_mark") or "·"
        lines.append(f"{mark} {h['ago']}｜{h['text'][:100]}")
    # 👇 用户 2026-11-19：「档案和记忆只显示三四条最新，她总以为只有这些，就不翻了」
    #    明确告诉她总数 + 怎么翻页，否则她会在「只有 4 条记忆」的错觉下做判断。
    try:
        # ⚠️ 不能用 recall_catalog(limit=1)：它内部是 recall(limit=offset+limit)，
        #    拿到的 total 恒等于 1 —— 「还有 N 条」就永远不显示。
        #    这里用大 limit 且 mark_hit=False（数总数不该算命中）。
        total = len(recall(query, speaker_qq=speaker_qq, limit=200,
                           gid=gid, mark_hit=False))
    except Exception:
        total = len(hits)
    if total > len(hits):
        lines.append(
            f"（相关记忆一共 {total} 条，这里只放最强的 {len(hits)} 条 —— "
            f"想翻更早：`[[MEMORY|{query[:10]}|翻页2]]`；"
            f"想看某条全文：`[[MEMORY|展开|编号]]`；按时间翻：`[[MEMORY|前天]]`）")
    return "\n".join(lines)


# ================== 归档 & 合并（每天两次） ==================
def archive_fact(text: str, about_qq: str = "") -> str:
    """
    这是"萃取事实"的入口：
     - 先搜类似，命中相似度较高 → 合并进旧记忆
     - 如果有明显的时效相反 → 旧的标 outdated
    这层不用 LLM，纯靠 tokenize 相同度。
    """
    # 找最相似的一条 active 记忆
    qt = set(tokenize(text))
    idx = _load_index()
    best_id = ""
    best_score = 0.0
    for d in idx["items"]:
        if d.get("status") != "active":
            continue
        dt = set(d["tokens"])
        if not dt or not qt:
            continue
        inter = len(qt & dt)
        union = len(qt | dt)
        sim = inter / union
        if sim > best_score:
            best_score = sim
            best_id = d["id"]

    if best_id and best_score >= 0.6:
        # 合并：把新内容追加到旧 block 的 text_full 后面（标 timestamp）
        f = BLOCK_DIR / f"{best_id}.json"
        try:
            full = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            full = {"id": best_id, "text_full": ""}
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        merged = full.get("text_full", "") + f"\n[{stamp}] {text}"
        full["text_full"] = merged[:2000]
        f.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
        # 索引 summary 也更新
        for d in idx["items"]:
            if d["id"] == best_id:
                d["summary"] = text[:80]
                break
        _save_index(idx)
        return best_id

    return remember(text=text, kind="fact", about_qq=about_qq, importance=0.6)


def health_report(dup_threshold: float = 0.8,
                  stale_days: float = 90.0) -> dict:
    """
    记忆库体检（抄 dsh-plugin-memory 的 checkup 思路）：
      ① 疑似重复（token Jaccard ≥ 阈值）→ 建议合并
      ② 长期未命中（stale_days 天没被检索过且从没命中）→ 建议标 outdated
      ③ 主题分布 / 情绪刻痕占比
    只读不改，给 `/记忆体检` 用。
    """
    idx = _load_index()
    items = idx["items"]
    active = [d for d in items if d.get("status") == "active"]
    now = time.time()

    # ① 重复检测（O(n²)，几百条量级够用）
    sets = []
    for d in active:
        toks = set(d.get("tokens") or tokenize(d.get("summary") or ""))
        if toks:
            sets.append((d["id"], d.get("summary") or "", toks))
    dups = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            a, b = sets[i][2], sets[j][2]
            inter = len(a & b)
            if not inter:
                continue
            jac = inter / len(a | b)
            if jac >= dup_threshold:
                dups.append({
                    "a": sets[i][0], "a_text": sets[i][1][:40],
                    "b": sets[j][0], "b_text": sets[j][1][:40],
                    "sim": round(jac, 2),
                })
    dups.sort(key=lambda x: -x["sim"])

    # ② 长期未命中
    stale = []
    for d in active:
        if int(d.get("hit_count") or 0) > 0:
            continue
        last = float(d.get("last_hit_at") or d.get("created_at") or 0)
        if last <= 0:
            continue
        age = (now - last) / 86400.0
        if age >= stale_days:
            stale.append({
                "id": d["id"],
                "text": (d.get("summary") or "")[:40],
                "days": round(age, 1),
            })
    stale.sort(key=lambda x: -x["days"])

    # ③ 分布
    tag_count: dict = {}
    kind_count: dict = {}
    emo = 0
    for d in active:
        for t in (d.get("tags") or []):
            tag_count[t] = tag_count.get(t, 0) + 1
        k = d.get("kind") or "unknown"
        kind_count[k] = kind_count.get(k, 0) + 1
        if d.get("emotion_hit"):
            emo += 1

    return {
        "total": len(items),
        "active": len(active),
        "outdated": len(items) - len(active),
        "emotion_hit": emo,
        "dups": dups[:10],
        "dup_total": len(dups),
        "stale": stale[:10],
        "stale_total": len(stale),
        "tags": sorted(tag_count.items(), key=lambda x: -x[1])[:8],
        "kinds": sorted(kind_count.items(), key=lambda x: -x[1]),
    }


def stats() -> dict:
    if BACKEND == "sqlite":
        from . import memdb
        return memdb.stats()
    idx = _load_index()
    active = [d for d in idx["items"] if d.get("status") == "active"]
    outdated = [d for d in idx["items"] if d.get("status") == "outdated"]
    emotional = [d for d in active if d.get("emotion_hit")]
    return {
        "total": len(idx["items"]),
        "active": len(active),
        "outdated": len(outdated),
        "emotional": len(emotional),
    }
