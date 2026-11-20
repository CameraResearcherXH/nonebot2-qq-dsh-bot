# services/group_mood.py
"""群心情：**独立于整体心情**的「这个群让我舒不舒服」。

为什么单独做一层（用户 2026-11-20）：
  · 整体心情是 -1~1 的持续变量，一个群、一个私聊、一件事都会影响它；
  · 但「我愿不愿意在这个群多说话 / 要不要在这个群安静」看的不是整体心情，
    而是**这个群最近 5 小时里，大家让我涨了多少、掉了多少**；
  · 某一个人让我掉了 0.4 → 单独不理他（他的 @ 和图片也不接）；
  · 三个人及以上、整群净掉 0.7 → 判定「群聊导致的不愉快」，可以考虑整群安静；
  · 正负值互相抵消（不是只统计一个方向），5 小时没抵消的事件自动过期。

数据只存事件流水，快照在读取时现算：
  {"groups": {"<gid>": {"events": [
      {"at": 123.0, "delta": -0.2, "about_qq": "10001",
       "cause": "speaker", "why": "他当众说我笨", "mind": "有点下不来台",
       "word": "被戳到"}
  ]}}}

和 mood.py 的分工：
  · mood         —— 她此刻的整体状态（决定这一句怎么说）；
  · group_mood   —— 这个群的环境（决定要不要在这个群多说话 / 对谁安静）。
"""
from core import tuning
import json
import os
import threading
import time
from pathlib import Path

GROUP_MOOD_FILE = Path(os.getenv("GROUP_MOOD_FILE", "data/group_mood.json"))
GROUP_MOOD_FILE.parent.mkdir(parents=True, exist_ok=True)

WINDOW = tuning.f("GROUP_MOOD_WINDOW_SEC")
PERSON_DROP = tuning.f("GROUP_MOOD_PERSON_DROP")
GROUP_DROP = tuning.f("GROUP_MOOD_GROUP_DROP")
MIN_PEOPLE = tuning.i("GROUP_MOOD_MIN_PEOPLE")
MIN_DELTA = tuning.f("GROUP_MOOD_MIN_DELTA")
LIKE_MIN = tuning.f("GROUP_MOOD_LIKE_MIN")
MAX_EVENTS = tuning.i("GROUP_MOOD_MAX_EVENTS")
CHAT_MIN = tuning.f("GROUP_MOOD_CHAT_MIN")
CHAT_MAX = tuning.f("GROUP_MOOD_CHAT_MAX")

_LOCK = threading.Lock()
# 每轮 prompt 会多次读群心情（chat_factor + quiet_advisor），
# 按 mtime 缓存整份文件，避免反复读盘；record 写完会刷新缓存。
_CACHE = {"mtime": -1.0, "st": {"groups": {}}}


def _gid(session_key: str) -> str:
    """group_123 → 123；私聊/空 → ""。"""
    k = str(session_key or "").strip()
    if k.startswith("group_"):
        return k[len("group_"):]
    return k if k.isdigit() else ""


def _load() -> dict:
    try:
        mtime = GROUP_MOOD_FILE.stat().st_mtime
    except Exception:
        mtime = 0.0
    if mtime == _CACHE.get("mtime"):
        return _CACHE.get("st") or {"groups": {}}
    raw = {}
    if GROUP_MOOD_FILE.exists():
        try:
            raw = json.loads(GROUP_MOOD_FILE.read_text(encoding="utf-8")) or {}
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    groups = raw.get("groups")
    if not isinstance(groups, dict):
        # 兼容：万一有人把整份文件写成了 {gid: [...]}
        groups = {k: v for k, v in raw.items() if isinstance(v, list)}
    st = {"groups": groups}
    _CACHE["mtime"], _CACHE["st"] = mtime, st
    return st


def _save(st: dict) -> None:
    try:
        tmp = GROUP_MOOD_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, GROUP_MOOD_FILE)
        try:
            _CACHE["mtime"] = GROUP_MOOD_FILE.stat().st_mtime
            _CACHE["st"] = st
        except Exception:
            _CACHE["mtime"] = -1.0
    except Exception:
        _CACHE["mtime"] = -1.0


def _prune(events: list, now: float) -> list:
    out = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        try:
            at = float(e.get("at") or 0)
            d = float(e.get("delta") or 0)
        except Exception:
            continue
        if at and now - at <= WINDOW and abs(d) >= 1e-9:
            out.append(e)
    return out[-max(1, int(MAX_EVENTS)):]


def record(session_key: str, delta: float, *, about_qq: str = "",
           cause: str = "", why: str = "", mind: str = "",
           word: str = "", at: float = 0) -> dict:
    """记一次「这个群让我涨/跌了多少」。

    about_qq 只在 cause == speaker 时填（这次是谁引起的）。
    cause == self（她自己的事）不算群环境，直接丢弃。
    """
    gid = _gid(session_key)
    try:
        d = float(delta)
    except Exception:
        return {}
    if not gid or abs(d) < MIN_DELTA:
        return {}
    cause = str(cause or "").strip().lower()
    if cause == "self":
        return {}
    now = time.time()
    ev = {
        "at": float(at or now),
        "delta": round(d, 4),
        "about_qq": (str(about_qq or "").strip()
                     if cause == "speaker" and str(about_qq or "").isdigit()
                     else ""),
        "cause": cause,
        "why": str(why or "").strip()[:80],
        "mind": str(mind or "").strip()[:80],
        "word": str(word or "").strip()[:20],
    }
    with _LOCK:
        st = _load()
        groups = st.setdefault("groups", {})
        rec = groups.get(gid)
        if not isinstance(rec, dict):
            rec = {"events": []}
        events = _prune(rec.get("events") or [], now)
        events.append(ev)
        rec["events"] = events[-max(1, int(MAX_EVENTS)):]
        rec["updated_at"] = now
        groups[gid] = rec
        # 群数量上限：只留最近活跃的 200 个
        if len(groups) > 200:
            keep = sorted(groups.items(),
                          key=lambda kv: float((kv[1] or {}).get("updated_at") or 0),
                          reverse=True)[:200]
            st["groups"] = dict(keep)
        _save(st)
    return ev


def _name_of(qq: str) -> str:
    try:
        from . import profiles
        return profiles.call_of(str(qq), "", "") or str(qq)
    except Exception:
        return str(qq)


def snapshot(session_key: str, *, window_sec: float = 0) -> dict:
    """这个群最近窗口内的群心情快照（读时现算，正负互相抵消）。"""
    gid = _gid(session_key)
    out = {
        "gid": gid, "net": 0.0, "pos": 0.0, "neg": 0.0,
        "drop": 0.0, "up": 0.0, "n": 0,
        "people": {}, "likes": 0, "dislikes": 0, "ratio": 0.5,
        "neg_people": 0, "group_quiet": False, "person_quiet": [],
        "top_hater": {}, "top_liker": {}, "window": float(window_sec or WINDOW),
        "why": "",
    }
    if not gid:
        return out
    win = float(window_sec or WINDOW)
    now = time.time()
    st = _load()
    rec = (st.get("groups") or {}).get(gid) or {}
    events = []
    for e in rec.get("events") or []:
        try:
            at = float(e.get("at") or 0)
            d = float(e.get("delta") or 0)
        except Exception:
            continue
        if now - at <= win and abs(d) >= 1e-9:
            events.append((at, d, e))
    events.sort(key=lambda x: x[0])

    net = pos = neg = 0.0
    people: dict = {}
    for _at, d, e in events:
        net += d
        if d > 0:
            pos += d
        else:
            neg += d
        qq = str(e.get("about_qq") or "").strip()
        if not qq.isdigit():
            continue
        p = people.setdefault(qq, {"qq": qq, "net": 0.0, "pos": 0.0,
                                   "neg": 0.0, "n": 0, "drop": 0.0})
        p["net"] += d
        p["n"] += 1
        if d > 0:
            p["pos"] += d
        else:
            p["neg"] += d

    for p in people.values():
        p["net"] = round(p["net"], 3)
        p["pos"] = round(p["pos"], 3)
        p["neg"] = round(p["neg"], 3)
        p["drop"] = round(max(0.0, -p["net"]), 3)
        p["name"] = _name_of(p["qq"])

    likes = [p for p in people.values() if p["net"] >= LIKE_MIN]
    dislikes = [p for p in people.values() if p["net"] <= -LIKE_MIN]
    total = len(likes) + len(dislikes)
    ratio = (len(likes) / total) if total else 0.5

    person_quiet = sorted([p for p in people.values()
                           if p["drop"] >= PERSON_DROP],
                          key=lambda p: -p["drop"])
    group_drop = max(0.0, -net)
    group_quiet = bool(group_drop >= GROUP_DROP and len(dislikes) >= MIN_PEOPLE)

    out.update({
        "net": round(net, 3), "pos": round(pos, 3), "neg": round(neg, 3),
        "drop": round(group_drop, 3), "up": round(max(0.0, net), 3),
        "n": len(events), "people": people,
        "likes": len(likes), "dislikes": len(dislikes),
        "ratio": round(ratio, 3), "neg_people": len(dislikes),
        "group_quiet": group_quiet,
        "person_quiet": person_quiet,
        "top_hater": (min(dislikes, key=lambda p: p["net"]) if dislikes else {}),
        "top_liker": (max(likes, key=lambda p: p["net"]) if likes else {}),
    })
    bits = []
    if events:
        bits.append(f"5 小时净 {net:+.2f}（+{pos:.2f}/-{abs(neg):.2f}）")
    if total:
        bits.append(f"{len(likes)} 人让你舒服 / {len(dislikes)} 人让你不舒服"
                    f"（喜欢比例 {ratio:.0%}）")
    if group_quiet:
        bits.append(f"整群掉了 {group_drop:.2f}、涉及 {len(dislikes)} 人 → 可以考虑整群安静")
    if person_quiet:
        names = "、".join(p["name"] for p in person_quiet[:3])
        bits.append(f"单独让你难受的人：{names}")
    out["why"] = "；".join(bits)
    return out


def person_of(session_key: str, qq: str) -> dict:
    """某个人在这个群窗口内的净变化（没有记录就空 dict）。"""
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return {}
    return dict((snapshot(session_key).get("people") or {}).get(qq) or {})


def chat_factor(session_key: str) -> float:
    """群心情 → 「愿不愿意在这个群多说话」的乘数（0.55~1.35）。

    整体心情决定这一句怎么说；这个乘数决定**在这个群想不想多说**：
      · 5 小时净掉 0.7 → 接近下限（话明显变少）；
      · 净涨 0.4 且喜欢比例高 → 接近上限（更愿意接话）。
    """
    snap = snapshot(session_key)
    if not snap.get("n"):
        return 1.0
    net = max(-0.7, min(0.4, float(snap.get("net") or 0.0)))
    ratio = float(snap.get("ratio") or 0.5)
    # net: -0.7 → 0.55 倍；0 → 1.0；+0.4 → 1.25 倍
    base = 1.0 + (net / 0.7) * 0.45
    base *= (0.88 + 0.24 * ratio)      # ratio 0 → 0.88；1 → 1.12
    return round(max(CHAT_MIN, min(CHAT_MAX, base)), 3)


def status_text(session_key: str) -> str:
    """/测函数 群心情 / 安静 用：把群心情摊开。"""
    s = snapshot(session_key)
    if not s.get("gid"):
        return "· 不是群会话，没有群心情"
    lines = [f"· 群 {s['gid']}｜窗口 {s['window'] / 3600:.1f} 小时"
             f"｜事件 {s['n']} 条"]
    if not s.get("n"):
        return "\n".join(lines + ["· 还没有群心情记录"])
    lines.append(f"· 净变化 {s['net']:+.2f}（涨 {s['pos']:.2f} / 跌 {abs(s['neg']):.2f}）"
                 f"｜跌 {s['drop']:.2f}｜回暖 {s['up']:.2f}")
    lines.append(f"· 喜欢/讨厌：{s['likes']} 人 / {s['dislikes']} 人"
                 f"（喜欢比例 {s['ratio']:.0%}）")
    if s.get("top_liker"):
        p = s["top_liker"]
        lines.append(f"· 最让你舒服：{p.get('name')}（{p['net']:+.2f}）")
    if s.get("top_hater"):
        p = s["top_hater"]
        lines.append(f"· 最让你难受：{p.get('name')}（{p['net']:+.2f}，跌 {p['drop']:.2f}）")
    for p in (s.get("person_quiet") or [])[:5]:
        lines.append(f"· 建议单独不理：{p.get('name')}"
                     f"（净 {p['net']:+.2f}，跌 {p['drop']:.2f} ≥ {PERSON_DROP}）")
    lines.append(f"· 整群判定：{'群聊导致的不愉快' if s.get('group_quiet') else '还没到'}"
                 f"（群跌 {s['drop']:.2f} / 阈值 {GROUP_DROP}，"
                 f"{s['neg_people']} 人 / 至少 {MIN_PEOPLE} 人）")
    lines.append(f"· 愿意在这个群多说话：×{chat_factor(session_key):.2f}")
    if s.get("why"):
        lines.append(f"· 依据：{s['why']}")
    return "\n".join(lines)
