# services/lorebook.py
"""
群设定 / 群梗 / 人物词条（对齐 SillyTavern 的 World Info）。

为什么要有它（用户 2026-11-18）：
  档案一直是「按人存」的，但群里还有大量**不属于某个人**的东西：
  梗、事件、群规、两个人的关系。以前只能塞进人格卡或记忆里，
  每轮都占 token，而且模型不知道该不该提。
  ST 的做法是：词条带关键词，只有聊到才注入，还支持 sticky/cooldown/预算。

这一版只做最小可用集：
  · keys（主关键词）+ secondary（副关键词）+ 四种逻辑
  · constant（永远注入）
  · order（顺序）+ 预算（默认 520 字，超了按 order 丢）
  · sticky / cooldown / delay（按「轮」计，不按消息条数）
  · probability（0~100，避免每句都提同一个梗）
  · group / group_weight（同组只取一个，防止一次注入三条同义梗）
  · position：before（历史前）/ post（当前消息前）

数据：data/lore/<gid>.json
  {"turn": 12, "entries": {id: {...}}, "state": {id: {...}}}
"""
from core import tuning

import hashlib
import json
import os
import random
import re
import time
from pathlib import Path

try:  # 允许脱离 nonebot 单独跑脚本
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

LORE_DIR = Path("data/lore")
LORE_DIR.mkdir(parents=True, exist_ok=True)

SCAN_DEPTH = tuning.i("LORE_SCAN_DEPTH")
BUDGET_BEFORE = tuning.i("LORE_BUDGET")
BUDGET_POST = tuning.i("LORE_BUDGET_POST")
MAX_ENTRIES = tuning.i("LORE_MAX_ENTRIES")

LOGIC = ("and_any", "and_all", "not_any", "not_all")

# (gid, turn) -> scan result，防止同一轮 build_packet 被调用两次时重复 roll 概率
_CACHE: dict = {}


# ---------------- 读写 ----------------
def _path(gid: str) -> Path:
    gid = str(gid or "0").strip() or "0"
    return LORE_DIR / f"{re.sub(r'[^0-9A-Za-z_-]', '_', gid)}.json"


def _load(gid: str) -> dict:
    f = _path(gid)
    if f.exists():
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                d.setdefault("turn", 0)
                d.setdefault("entries", {})
                d.setdefault("state", {})
                return d
        except Exception as e:
            logger.warning(f"[LORE] 读取失败 {f}: {e}")
    return {"turn": 0, "entries": {}, "state": {}}


def _save(gid: str, data: dict) -> None:
    f = _path(gid)
    tmp = f.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(f)
    except Exception as e:
        logger.warning(f"[LORE] 保存失败 {f}: {e}")


# ---------------- 轮次 ----------------
def begin_turn(gid: str) -> int:
    """一轮新的用户消息：轮次 +1，返回新轮次。"""
    d = _load(gid)
    d["turn"] = int(d.get("turn") or 0) + 1
    _save(gid, d)
    # 清掉很旧的缓存
    for k in [k for k in _CACHE if k[0] == str(gid) and k[1] < d["turn"] - 3]:
        _CACHE.pop(k, None)
    return d["turn"]


def current_turn(gid: str) -> int:
    return int(_load(gid).get("turn") or 0)


# ---------------- 关键词匹配 ----------------
def _norm(s: str) -> str:
    return str(s or "").strip().lower().lstrip("@＠").strip()


def _match(entry: dict, text: str) -> bool:
    keys = [_norm(k) for k in (entry.get("keys") or []) if _norm(k)]
    if not keys:
        return False
    hay = str(text or "").lower()
    hit = [k for k in keys if k in hay]
    if not hit:
        return False

    secondary = [_norm(k) for k in (entry.get("secondary") or []) if _norm(k)]
    if not secondary:
        return True
    sec_hit = [k for k in secondary if k in hay]
    logic = str(entry.get("logic") or "and_any").lower()
    if logic == "and_all":
        return len(sec_hit) == len(secondary)
    if logic == "not_any":
        return not sec_hit
    if logic == "not_all":
        return len(sec_hit) < len(secondary)
    return bool(sec_hit)  # and_any


# ---------------- 扫描 ----------------
def scan(gid: str, text: str, turn=None) -> dict:
    """
    返回 {"before": [str], "post": [str], "ids": [str]}。

    turn 不传就用当前轮次（只读，不推进）。
    """
    gid = str(gid or "").strip()
    if not gid:
        return {"before": [], "post": [], "ids": []}
    turn = int(turn if turn is not None else current_turn(gid))
    ck = (gid, turn)
    if ck in _CACHE:
        return _CACHE[ck]

    d = _load(gid)
    entries = d.get("entries") or {}
    state = d.get("state") or {}
    activated = []
    changed = False

    for eid, e in entries.items():
        if not isinstance(e, dict) or not e.get("enabled", True):
            continue
        try:
            order = int(e.get("order") or 100)
            sticky = max(0, int(e.get("sticky") or 0))
            cooldown = max(0, int(e.get("cooldown") or 0))
            delay = max(0, int(e.get("delay") or 0))
        except Exception:
            order, sticky, cooldown, delay = 100, 0, 0, 0

        st = state.get(eid)
        if not isinstance(st, dict):
            st = {}
            state[eid] = st
            changed = True

        if delay and turn < delay:
            continue

        # ⚠️ sticky 优先于 cooldown：两者同时设置时，粘性期内不该被冷却踢掉
        is_sticky = int(st.get("sticky_until") or 0) >= turn
        if not is_sticky:
            if int(st.get("cooldown_until") or 0) > turn:
                continue
            # constant = 永远注入（群规/边界），不要求关键词
            if not e.get("constant") and not _match(e, text):
                continue
            prob = e.get("probability")
            if prob is not None and not e.get("constant"):
                try:
                    p = max(0.0, min(100.0, float(prob)))
                except Exception:
                    p = 100.0
                if p <= 0:
                    continue
                if p < 100 and random.random() * 100 > p:
                    continue
            if sticky:
                st["sticky_until"] = turn + sticky
            if cooldown:
                st["cooldown_until"] = turn + cooldown
            st["first_seen"] = int(st.get("first_seen") or turn)
            st["last_seen"] = turn
            changed = True
        else:
            st["last_seen"] = turn
            changed = True

        activated.append((order, int(e.get("group_weight") or 100), eid, e))

    # 同组只留一个（权重高的优先）
    best_group = {}
    keep = []
    for order, weight, eid, e in sorted(activated, key=lambda x: (x[0], x[1]), reverse=True):
        grp = str(e.get("group") or "").strip()
        if grp:
            if grp in best_group:
                continue
            best_group[grp] = eid
        keep.append(e)

    # 预算：按 order 顺序装，constant / ignore_budget 必留
    before, post = [], []
    used_b, used_p = 0, 0
    for e in keep:
        content = str(e.get("content") or "").strip()
        if not content:
            continue
        comment = str(e.get("comment") or "").strip()
        line = (f"· 【{comment}】{content}" if comment else f"· {content}")
        where = str(e.get("position") or "before").lower()
        if where == "post":
            if e.get("ignore_budget") or used_p + len(line) <= BUDGET_POST:
                post.append(line)
                used_p += len(line)
        else:
            if e.get("ignore_budget") or used_b + len(line) <= BUDGET_BEFORE:
                before.append(line)
                used_b += len(line)

    if changed:
        d["state"] = state
        _save(gid, d)

    out = {"before": before, "post": post, "ids": [x[2] for x in activated]}
    _CACHE[ck] = out
    return out


# ---------------- 写词条 ----------------
def _new_id(keys: list, content: str) -> str:
    raw = "|".join(sorted(_norm(k) for k in keys)) + "|" + str(content)[:120]
    return "l" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]


def add_entry(gid: str, content: str, keys, *, secondary=None, logic="and_any",
              comment="", constant=None, order=None, position=None,
              sticky=None, cooldown=None, delay=None, probability=None,
              group="", group_weight=None, enabled=None,
              source="llm", confidence=0.7) -> str:
    """
    新增/更新一条词条。返回 id；内容为空返回 ""。

    去重规则：关键词集合相同 → 视为同一条，合并关键词、用更新的内容覆盖。
    """
    gid = str(gid or "").strip()
    content = str(content or "").strip()
    keys = [_norm(k) for k in (keys or []) if _norm(k)]
    if not gid or not content or not keys:
        return ""

    d = _load(gid)
    entries = d.setdefault("entries", {})
    keyset = set(keys)

    for eid, e in entries.items():
        if not isinstance(e, dict):
            continue
        old = {_norm(k) for k in (e.get("keys") or []) if _norm(k)}
        if old and old == keyset:
            merged = sorted(old | keyset)
            e.update({
                "keys": merged,
                "content": content[:600],
                "updated_at": time.time(),
                "source": source or e.get("source") or "llm",
                "confidence": max(float(e.get("confidence") or 0), float(confidence or 0)),
            })
            for k, v in (("secondary", secondary), ("comment", comment),
                         ("position", position), ("group", group)):
                if v:
                    e[k] = v
            for k, v in (("constant", constant), ("order", order), ("sticky", sticky),
                         ("cooldown", cooldown), ("delay", delay),
                         ("probability", probability), ("group_weight", group_weight),
                         ("enabled", enabled)):
                if v is not None:
                    e[k] = v
            _save(gid, d)
            _invalidate(gid)
            return eid

    eid = _new_id(keys, content)
    entries[eid] = {
        "id": eid,
        "keys": keys,
        "secondary": [_norm(k) for k in (secondary or []) if _norm(k)],
        "logic": logic if logic in LOGIC else "and_any",
        "content": content[:600],
        "comment": str(comment or "")[:40],
        "constant": bool(constant) if constant is not None else False,
        "order": int(order if order is not None else 100),
        "position": position if position in ("before", "post") else "before",
        "sticky": max(0, int(sticky if sticky is not None else 0)),
        "cooldown": max(0, int(cooldown if cooldown is not None else 0)),
        "delay": max(0, int(delay if delay is not None else 0)),
        "probability": max(0, min(100, int(probability if probability is not None else 100))),
        "group": str(group or ""),
        "group_weight": int(group_weight if group_weight is not None else 100),
        "enabled": bool(enabled) if enabled is not None else True,
        "source": str(source or "llm"),
        "confidence": max(0.0, min(1.0, float(confidence or 0.7))),
        "created_at": time.time(),
        "updated_at": time.time(),
    }

    # 超过上限：先丢最旧、order 最低的非 constant 词条
    if len(entries) > MAX_ENTRIES:
        drop = sorted(
            (k for k, v in entries.items() if isinstance(v, dict) and not v.get("constant")),
            key=lambda k: (int(entries[k].get("order") or 100),
                           float(entries[k].get("updated_at") or 0)),
        )
        for k in drop[: max(0, len(entries) - MAX_ENTRIES)]:
            entries.pop(k, None)
            d.get("state", {}).pop(k, None)

    _save(gid, d)
    _invalidate(gid)
    return eid


def _invalidate(gid: str) -> None:
    for k in [k for k in _CACHE if k[0] == str(gid)]:
        _CACHE.pop(k, None)


def seed_from_profiles(gid: str, qqs) -> int:
    """
    把已有档案批量灌成「人物词条」：keys = 称呼/群名片/别名。
    这是 lorebook 的第一批内容（用户：我们不是一直有记录档案吗）。
    """
    from . import profiles

    n = 0
    for qq in qqs or []:
        qq = str(qq or "").strip()
        if not qq:
            continue
        try:
            p = profiles.get_profile(qq)
            card = profiles.render_card(qq, gid, detail="brief", max_len=300)
        except Exception:
            continue
        if not card:
            continue
        keys = []
        for k in [profiles.call_of(qq, "", gid), p.get("nickname"), p.get("call_name")]:
            k = str(k or "").strip()
            if len(k) >= 2 and k not in keys:
                keys.append(k)
        for k in (p.get("aliases") or []):
            k = str(k or "").strip()
            if len(k) >= 2 and k.isdigit() is False and k not in keys:
                keys.append(k)
        if not keys:
            continue
        add_entry(
            gid, card, keys,
            comment=profiles.call_of(qq, "", gid) or qq,
            order=120, position="before", source="profile", confidence=0.9,
        )
        n += 1
    return n


def list_entries(gid: str, limit: int = 50) -> list:
    d = _load(gid)
    items = [v for v in (d.get("entries") or {}).values() if isinstance(v, dict)]
    items.sort(key=lambda e: (int(e.get("order") or 100), float(e.get("updated_at") or 0)),
               reverse=True)
    return items[:limit]


def delete_entry(gid: str, eid: str) -> bool:
    d = _load(gid)
    if str(eid) in (d.get("entries") or {}):
        d["entries"].pop(str(eid), None)
        d.get("state", {}).pop(str(eid), None)
        _save(gid, d)
        _invalidate(gid)
        return True
    return False


def format_block(items, title: str = "群里的设定（聊到才给，别主动复述）") -> str:
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not items:
        return ""
    return "▍" + title + "\n" + "\n".join(items)
