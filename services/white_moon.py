# services/white_moon.py
"""白月光模式（2026-11-23 用户提出，18:20 按新理解重做）。

用户的设计（两层统计）：
  · **每个人**：统计「这个人最喜欢 @ 谁 / 引用回复谁」→ 那个被 @ 最多的人，
    就是这个人的「白月光」。
  · **每个群**：统计「群里被 @ / 被引用 / 被接话最多的人」→ 群白月光。
  · 她回某个人 A 时（引用回复 A / 被 A @ / 接 A 的话）→ 用 **A 的白月光** 的说话方式。
  · 她吐槽 / 回大家时 → 用 **群白月光** 的说话方式。

所以统计的是**群友之间**的 @/引用/接话，不是她自己 @ 了谁。
数据：`data/white_moon.json`

    groups.<gid>.persons.<A>.targets.<B> = {at, quote, addressed, name}
    groups.<gid>.targets.<B>            = 群级汇总（at + quote + addressed）
    groups.<gid>.person_moons.<A>       = A 的白月光缓存（qq/name/score/style）
    groups.<gid>.moon                   = 群白月光缓存

配置：WHITE_MOON_MODE、WHITE_MOON_MIN_SCORE、WHITE_MOON_STYLE_ITEMS、
      WHITE_MOON_STYLE_TTL。
"""
from core import tuning

import json
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path

from services import identity as _identity

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

ENABLED = os.getenv("WHITE_MOON_MODE", "true").lower() in ("1", "true", "yes", "on")
FILE = Path(os.getenv("WHITE_MOON_FILE", "data/white_moon.json"))
MIN_SCORE = tuning.i("WHITE_MOON_MIN_SCORE")
STYLE_ITEMS = tuning.i("WHITE_MOON_STYLE_ITEMS")
STYLE_TTL = tuning.f("WHITE_MOON_STYLE_TTL")

_LOCK = threading.RLock()
_CACHE = {"mtime": 0.0, "data": {}}

_STOP_PHRASES = {
    "就是", "这个", "那个", "什么", "不是", "没有", "可以", "然后", "因为",
    "所以", "但是", "他们", "你们", "我们", "一个", "现在", "真的", "已经",
    "还是", "自己", "知道", "觉得", "可能", "应该", "这么", "那么",
}


def _empty() -> dict:
    return {"groups": {}}


def _load() -> dict:
    try:
        st = FILE.stat().st_mtime
    except Exception:
        return _empty()
    if st == _CACHE.get("mtime"):
        return _CACHE.get("data") or _empty()
    try:
        data = json.loads(FILE.read_text(encoding="utf-8") or "{}")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("groups", {})
    _CACHE["mtime"] = st
    _CACHE["data"] = data
    return data


def _save(data: dict) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        try:
            _CACHE["mtime"] = FILE.stat().st_mtime
        except Exception:
            _CACHE["mtime"] = time.time()
        _CACHE["data"] = data
    except Exception as e:
        logger.debug(f"[MOON] 写盘失败: {type(e).__name__}: {e}")


def _group(data: dict, gid: str) -> dict:
    groups = data.setdefault("groups", {})
    g = groups.setdefault(str(gid), {})
    g.setdefault("persons", {})
    g.setdefault("targets", {})
    return g


# ============================ 统计（群友之间） ============================
def note_incoming(gid: str, from_qq: str, from_name: str,
                  to_qq: str, to_name: str, kind: str = "at",
                  self_qq: str = "") -> None:
    """记一笔「A → B」：A @ 了 B / 引用回复了 B / 被判定在接 B 的话。

    kind:
      · at        —— A 在消息里 @ 了 B
      · quote     —— A 引用回复了 B
      · addressed —— 没 @/引用，但收发件人判定「这句是对 B 说的」（接话）
    """
    gid = str(gid or "").strip()
    a = str(from_qq or "").strip()
    b = str(to_qq or "").strip()
    me = str(self_qq or "").strip() or _identity.canonical()
    if not ENABLED or not gid or not a or not b:
        return
    if b in ("all", "everyone") or a == b:
        return                      # 不统计 @全体 / 自己
    if ((me and b == me) or _identity.is_self(a) or _identity.is_self(b)):
        return                      # 不统计她本人（当前号 / 历史号都算）
    kind = kind if kind in ("at", "quote", "addressed") else "at"
    try:
        with _LOCK:
            data = _load()
            g = _group(data, gid)
            p = g["persons"].setdefault(a, {"name": "", "targets": {}})
            if from_name:
                p["name"] = str(from_name)[:40]
            t = p["targets"].setdefault(
                b, {"name": "", "at": 0, "quote": 0, "addressed": 0, "last": 0.0})
            t[kind] = int(t.get(kind) or 0) + 1
            if to_name:
                t["name"] = str(to_name)[:40]
            t["last"] = time.time()

            gt = g["targets"].setdefault(
                b, {"name": "", "at": 0, "quote": 0, "addressed": 0, "last": 0.0})
            gt[kind] = int(gt.get(kind) or 0) + 1
            if to_name:
                gt["name"] = str(to_name)[:40]
            gt["last"] = time.time()
            _save(data)
    except Exception as e:
        logger.debug(f"[MOON] note_incoming 失败: {type(e).__name__}: {e}")


def _score(rec: dict, *, with_addressed: bool) -> int:
    s = int(rec.get("at") or 0) + 2 * int(rec.get("quote") or 0)
    if with_addressed:
        s += int(rec.get("addressed") or 0)
    return s


def _top(targets: dict, *, with_addressed: bool) -> dict:
    rows = []
    for qq, rec in (targets or {}).items():
        if not isinstance(rec, dict):
            continue
        rows.append({
            "qq": str(qq),
            "name": str(rec.get("name") or qq),
            "at": int(rec.get("at") or 0),
            "quote": int(rec.get("quote") or 0),
            "addressed": int(rec.get("addressed") or 0),
            "score": _score(rec, with_addressed=with_addressed),
            "last": float(rec.get("last") or 0),
        })
    rows.sort(key=lambda x: (-x["score"], -x["last"]))
    return rows[0] if rows else {}


# ============================ 风格学习 ============================
def _texts_of(gid: str, qq: str) -> list:
    """取这个人最近说的话（优先处理过的现场，退回落盘日志）。"""
    out = []
    try:
        from . import group_scene
        for m in group_scene.recent_messages(gid, 200):
            if str(m.get("qq") or "") != str(qq) or m.get("is_bot"):
                continue
            t = str(m.get("text") or "").strip()
            if t and not t.startswith(("【", "[图片", "[表情", "[视频")):
                out.append(t)
    except Exception:
        pass
    if len(out) < 5:
        try:
            from . import dialogue_log
            for r in dialogue_log.load_recent(gid, 400):
                if str(r.get("qq") or "") != str(qq) or r.get("kind") == "bot":
                    continue
                t = str(r.get("text") or "").strip()
                if t:
                    out.append(t)
        except Exception:
            pass
    return out[-80:]


def _style_from_texts(texts: list) -> str:
    """不调模型的轻量风格画像：长短 / 标点 / 表情 / 口头禅。"""
    texts = [str(t or "").strip() for t in (texts or []) if str(t or "").strip()]
    if len(texts) < 5:
        return ""
    n = len(texts)
    lens = [len(t) for t in texts]
    avg = sum(lens) / n
    short = sum(1 for x in lens if x <= 8) / n
    q = sum(1 for t in texts if ("？" in t or "?" in t)) / n
    ex = sum(1 for t in texts if ("！" in t or "!" in t)) / n
    no_punct = sum(1 for t in texts if not re.search(r"[。！？!?…，,]", t)) / n
    emoji = sum(1 for t in texts
                if re.search(r"[😂🤣😅😭🥲😊😄👍🐟🌙]|\[表情|\[CQ:face", t)) / n

    cnt = Counter()
    for t in texts:
        seen = set()
        for size in (2, 3, 4):
            for i in range(max(0, len(t) - size + 1)):
                s = t[i:i + size]
                if (s.strip() and s not in seen and s not in _STOP_PHRASES
                        and not re.search(r"[0-9A-Za-z]", s)):
                    seen.add(s)
                    cnt[s] += 1
    phrases = [s for s, c in cnt.most_common(10)
               if c >= max(2, int(n * 0.25))][:4]

    parts = []
    if short >= 0.6:
        parts.append("句子很短")
    elif avg >= 35:
        parts.append("喜欢一次说一长段")
    if q >= 0.3:
        parts.append("爱用问号")
    if ex >= 0.3:
        parts.append("语气带感叹")
    if no_punct >= 0.5:
        parts.append("经常不打标点")
    if emoji >= 0.2:
        parts.append("爱带表情")
    if phrases:
        parts.append("常挂嘴边：" + "、".join(f"「{x}」" for x in phrases))
    return "、".join(parts) or "说话比较随意"


def learn_style(gid: str, qq: str) -> str:
    return _style_from_texts(_texts_of(gid, qq))


def _style_or_empty(gid: str, qq: str) -> str:
    st = learn_style(gid, qq)
    if st:
        return st
    # 退回 expression / profiles 的已有样本
    try:
        from . import expression
        items = expression.load(f"person_{qq}")
        items = sorted(
            items,
            key=lambda x: (-(int(x.get("count") or 1)), -float(x.get("at") or 0)),
        )[:max(1, STYLE_ITEMS)]
        txt = "；".join(
            f"{x.get('text')}（{x.get('scene') or '随口'}·{x.get('tone') or '自然'}）"
            for x in items if str(x.get("text") or "").strip())
        if txt:
            return txt
    except Exception:
        pass
    try:
        from . import profiles
        return profiles.style_summary(qq)
    except Exception:
        return ""


# ============================ 白月光查询 ============================
def person_moon(gid: str, qq: str) -> dict:
    """这个人的白月光：他 @/引用最多的人。"""
    data = _load()
    g = ((data.get("groups") or {}).get(str(gid)) or {})
    p = (g.get("persons") or {}).get(str(qq)) or {}
    top = _top(p.get("targets") or {}, with_addressed=False)
    if not top or int(top.get("score") or 0) < MIN_SCORE:
        return {}
    cached = (g.get("person_moons") or {}).get(str(qq)) or {}
    if (str(cached.get("qq") or "") == str(top["qq"])
            and int(cached.get("score") or 0) == int(top["score"])
            and cached.get("style")
            and time.time() - float(cached.get("style_at") or 0) < STYLE_TTL):
        return {**top, "style": str(cached.get("style") or "")}
    style = _style_or_empty(gid, top["qq"])
    try:
        with _LOCK:
            data = _load()
            g = _group(data, str(gid))
            g.setdefault("person_moons", {})[str(qq)] = {
                "qq": top["qq"], "name": top["name"],
                "score": int(top["score"]), "style": style,
                "style_at": time.time(),
            }
            _save(data)
    except Exception as e:
        logger.debug(f"[MOON] 保存个人白月光失败: {type(e).__name__}: {e}")
    return {**top, "style": style}


def group_moon(gid: str) -> dict:
    """群白月光：群里被 @ / 引用 / 接话最多的人。"""
    data = _load()
    g = ((data.get("groups") or {}).get(str(gid)) or {})
    top = _top(g.get("targets") or {}, with_addressed=True)
    if not top or int(top.get("score") or 0) < MIN_SCORE:
        return {}
    cached = g.get("moon") or {}
    if (str(cached.get("qq") or "") == str(top["qq"])
            and int(cached.get("score") or 0) == int(top["score"])
            and cached.get("style")
            and time.time() - float(cached.get("style_at") or 0) < STYLE_TTL):
        return {**top, "style": str(cached.get("style") or "")}
    style = _style_or_empty(gid, top["qq"])
    try:
        with _LOCK:
            data = _load()
            g = _group(data, str(gid))
            g["moon"] = {
                "qq": top["qq"], "name": top["name"],
                "score": int(top["score"]), "style": style,
                "style_at": time.time(),
            }
            _save(data)
    except Exception as e:
        logger.debug(f"[MOON] 保存群白月光失败: {type(e).__name__}: {e}")
    return {**top, "style": style}


def ranking_persons(gid: str, limit: int = 12) -> list:
    data = _load()
    persons = (((data.get("groups") or {}).get(str(gid)) or {}).get("persons") or {})
    rows = []
    for qq, p in persons.items():
        m = person_moon(gid, qq)
        rows.append({
            "qq": str(qq),
            "name": str((p or {}).get("name") or qq),
            "moon_qq": str(m.get("qq") or ""),
            "moon_name": str(m.get("name") or ""),
            "moon_score": int(m.get("score") or 0),
        })
    rows.sort(key=lambda x: (-x["moon_score"], x["name"]))
    return rows[:max(1, int(limit or 12))]


# ============================ 注入 ============================
def render(gid: str, target_qq: str = "", target_name: str = "",
           speaker_name: str = "") -> str:
    """回复前注入的白月光块。

    target_qq 非空：她这次回的是这个人 → 用「这个人的白月光」的说话方式。
    target_qq 为空：她回大家 / 吐槽 → 用「群白月光」的说话方式。
    """
    if not ENABLED or not gid:
        return ""
    target_qq = str(target_qq or "").strip()
    if target_qq:
        m = person_moon(gid, target_qq)
        if not m:
            return ""
        style = str(m.get("style") or "") or _style_or_empty(gid, m["qq"])
        if not style:
            return ""
        who = target_name or str(target_qq)
        return (
            f"▍白月光模式（{who} 的白月光是 {m['name']}）\n"
            f"· {who} 在这个群最常 @ / 引用 {m['name']}\n"
            f"· {m['name']} 说话的样子：{style}\n"
            f"这次是回 {who}：**多用 {m['name']} 习惯的句式 / 口头禅 / 标点**，"
            "但内容是你自己的；别照抄原句、别每句都用，也别把自己变成他。"
        )
    m = group_moon(gid)
    if not m:
        return ""
    style = str(m.get("style") or "") or _style_or_empty(gid, m["qq"])
    if not style:
        return ""
    return (
        f"▍白月光模式（这个群的白月光是 {m['name']}）\n"
        f"· 群里被 @ / 引用 / 接话最多的是 {m['name']}\n"
        f"· 他说话的样子：{style}\n"
        "这次是回大家 / 吐槽：**可以带一点他的说话方式**，"
        "但别丢自己的性格、别照抄原句、别每句都用。"
    )


def profile_note(qq: str, gid: str = "") -> str:
    """给每个人档案的一行：这个人自己的白月光是谁。"""
    m = person_moon(gid, qq)
    if not m:
        return "· 白月光：还没统计出来（他 @/引用谁最多就会记）"
    return (f"· 白月光：{qq} 在这个群最常 @ / 引用 {m['name']}；"
            f"回他的时候会不自觉用 {m['name']} 的说话方式。")


def status_text(gid: str = "") -> str:
    if not ENABLED:
        return "白月光模式：关闭（WHITE_MOON_MODE=false）"
    if not gid:
        data = _load()
        gids = sorted((data.get("groups") or {}).keys())
        lines = [f"白月光模式：开启｜已有 {len(gids)} 个群的数据"]
        for g in gids[:12]:
            m = group_moon(g)
            lines.append(
                f"· 群 {g}：群白月光 {m.get('name') or '还没有'}"
                f"（score {m.get('score') or 0}）")
        return "\n".join(lines)

    gm = group_moon(gid)
    lines = [f"🌙 群 {gid} 白月光"]
    if gm:
        lines.append(
            f"群白月光：{gm['name']}（{gm['qq']}）score={gm['score']}"
            f"｜@ {gm.get('at', 0)}｜引用 {gm.get('quote', 0)}"
            f"｜接话 {gm.get('addressed', 0)}")
        lines.append(f"他说话的样子：{gm.get('style') or '（样本还不够）'}")
    else:
        lines.append(f"群白月光：还没有（需要 score ≥ {MIN_SCORE}）")

    rows = ranking_persons(gid, 12)
    if rows:
        lines.append("\n每个人的白月光（他 @/引用最多的人）：")
        for r in rows:
            if r["moon_qq"]:
                lines.append(
                    f"· {r['name']} → {r['moon_name']}"
                    f"（score {r['moon_score']}）")
            else:
                lines.append(f"· {r['name']} → 还没统计出来")
    else:
        lines.append("\n每个人的白月光：还没有数据")
    return "\n".join(lines)
