# services/proactive.py
"""
主动找**私聊**的人聊天（群里不做这件事 —— 群里已经有 _random_chat_loop 插话）。

用户 2026-11-19：
  「我需要一个自动接话的功能，刚好我们有注意力漂移了，可以根据心情去选择
    是否主动找某些用户聊天。当然群里不要加这个功能。」

为什么单独一个模块：
  群里的「主动插话」看的是**当下这段对话**（有人刚说完话，接一句）。
  私聊的「主动找人」看的是**关系和时间**（他两小时没找我了，我想跟他说句话）。
  两者判据完全不同，混在一个循环里必然互相干扰。

判定顺序（任何一步不过就不打扰）：
  1. 总开关 + 不打扰时段
  2. 这个人被她静音了（chat_switch）→ 跳过
  3. 今天找他次数到顶 / 距离上次太近 → 跳过
  4. 最近有没有真人消息？太久没聊（> IDLE_MAX_H）不硬找；刚聊完（< IDLE_MIN_H）不补刀
  5. 最后一句是她自己说的 → 不连发（不刷屏）
  6. 关系够熟（intimacy ≥ MIN_INTIMACY）才主动 —— 陌生人收到私聊会很吓人
  7. 心情 × 亲密度 × 注意力漂移档位 → 分数 ≥ MIN_SCORE 才开口

⚠️ 她的回复仍然可以调 `stay_silent` —— 「想找他」不等于「必须说话」。
"""
from core import tuning
import json
import os
import random
import time
from pathlib import Path

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

from . import attention, chat_switch, conv_state, dialogue_log, mood, profiles

ENABLED = os.getenv("PROACTIVE_PRIVATE", "true").lower() in ("1", "true", "yes", "on")
# 每个人每天最多主动找几次
MAX_PER_DAY = tuning.i("PROACTIVE_MAX_PER_DAY")
# 同一个人两次之间的最小间隔（小时）
COOLDOWN_H = tuning.f("PROACTIVE_COOLDOWN_H")
# 多久没说话才值得主动找（小时）——太短像补刀，太长像诈尸
IDLE_MIN_H = tuning.f("PROACTIVE_IDLE_MIN_H")
IDLE_MAX_H = tuning.f("PROACTIVE_IDLE_MAX_H")
# 关系门槛：低于这个亲密度不主动私聊
MIN_INTIMACY = tuning.f("PROACTIVE_MIN_INTIMACY")
# 综合分门槛（心情 × 亲密度 × 漂移档位）
MIN_SCORE = tuning.f("PROACTIVE_MIN_SCORE")
# 不打扰时段（小时，左闭右开）
QUIET_START = tuning.i("PROACTIVE_QUIET_START")
QUIET_END = tuning.i("PROACTIVE_QUIET_END")
# 检查间隔（秒）
CHECK_SEC = tuning.f("PROACTIVE_CHECK_SEC")
# 演练模式：只打日志不发消息（第一次上线建议先开一天看看她会找谁）
DRY_RUN = os.getenv("PROACTIVE_DRY_RUN", "false").lower() in ("1", "true", "yes", "on")

STATE_FILE = Path(os.getenv("PROACTIVE_STATE_FILE", "data/proactive.json"))

# 注意力漂移档位 → 「想开口」的乘数
_DRIFT_FACTOR = {"off": 0.6, "subtle": 0.8, "active": 1.0,
                 "scattered": 1.15, "wild": 1.3}

_cache: dict = {"mtime": 0.0, "data": {}}


# ==================== 状态 ====================
def _load() -> dict:
    try:
        m = STATE_FILE.stat().st_mtime
    except Exception:
        _cache["mtime"], _cache["data"] = 0.0, {}
        return _cache["data"]
    if m != _cache["mtime"]:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
        except Exception:
            data = {}
        _cache["data"] = data if isinstance(data, dict) else {}
        _cache["mtime"] = m
    return _cache["data"]


def _save(data: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                              encoding="utf-8")
        _cache["data"] = data
        try:
            _cache["mtime"] = STATE_FILE.stat().st_mtime
        except Exception:
            _cache["mtime"] = 0.0
    except Exception as e:
        logger.debug(f"[PROACTIVE] 存状态失败: {e}")


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _rec(data: dict, qq: str) -> dict:
    return data.setdefault(str(qq), {})


# ==================== 素材 ====================
_REC_CACHE: dict = {}          # session_key -> (缓存时间, [records])


def _recs_for(key: str, limit: int) -> list:
    """
    按**条数**取这个会话最近的对话（60 秒缓存）。

    用户反复强调：「记录应该按条数，而不是时间」——
    以前这里是 `load_range(3)`（最近 3 天），隔了 4 天没聊就彻底找不到上次聊了什么，
    可那 20 条记录其实还在文件里。
    """
    now = time.time()
    hit = _REC_CACHE.get(key)
    if hit and now - float(hit[0] or 0) < 60 and len(hit[1]) >= limit:
        return hit[1]
    try:
        recs = dialogue_log.load_recent(key, limit=max(limit, 24))
    except Exception:
        recs = []
    _REC_CACHE[key] = (now, recs)
    if len(_REC_CACHE) > 40:
        for _k in sorted(_REC_CACHE, key=lambda x: _REC_CACHE[x][0])[:-40]:
            _REC_CACHE.pop(_k, None)
    return recs


def recent_private(qq: str, limit: int = 10) -> tuple:
    """
    这个私聊最近的对话。返回 (lines, 最后一条真人消息 ts, 最后一条是谁说的)。

    lines 的格式和群聊上下文一致（时间 + 人 + 内容），她看起来不会有违和感。
    """
    key = f"private_{qq}"
    recs = _recs_for(key, max(limit, 20))
    if not recs:
        return [], 0.0, ""
    last_human = 0.0
    for r in recs:
        if str(r.get("kind") or "user") != "bot":
            last_human = max(last_human, float(r.get("ts") or 0))
    lines = []
    for r in recs[-limit:]:
        ts = float(r.get("ts") or 0)
        hhmm = time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else ""
        who = str(r.get("who") or "?")
        txt = str(r.get("text") or "").strip()
        if not txt:
            continue
        if str(r.get("kind") or "") == "bot":
            lines.append(f"{hhmm} 这是你之前的回复：{txt[:120]}")
        else:
            lines.append(f"{hhmm} {who}：{txt[:120]}")
    last_kind = str(recs[-1].get("kind") or "user")
    return lines, last_human, last_kind


def _in_quiet(now: float) -> bool:
    h = time.localtime(now).tm_hour
    if QUIET_START <= QUIET_END:
        return QUIET_START <= h < QUIET_END
    return h >= QUIET_START or h < QUIET_END


# ==================== 选人 ====================
def score_of(qq: str) -> dict:
    """算「现在主动找他」的分。不满足硬条件时 ok=False + 原因。"""
    now = time.time()
    key = f"private_{qq}"
    info = {"qq": str(qq), "ok": False, "why": "", "score": 0.0}

    _off, _why = chat_switch.is_off(key, qq)
    if _off:
        info["why"] = f"她把这个人的私聊静音了（{_why}）"
        return info

    data = _load()
    rec = _rec(data, qq)
    if str(rec.get("day") or "") == _today():
        if int(rec.get("count") or 0) >= MAX_PER_DAY:
            info["why"] = f"今天已经找过 {rec.get('count')} 次（上限 {MAX_PER_DAY}）"
            return info
    last_at = float(rec.get("last_at") or 0)
    if last_at and now - last_at < COOLDOWN_H * 3600:
        left = (COOLDOWN_H * 3600 - (now - last_at)) / 3600
        info["why"] = f"距离上次才 {left:.1f}h（冷却 {COOLDOWN_H}h）"
        return info

    lines, last_human, last_kind = recent_private(qq)
    if not lines or not last_human:
        info["why"] = "没有历史对话（不找陌生人）"
        return info
    idle_h = (now - last_human) / 3600
    if idle_h < IDLE_MIN_H:
        info["why"] = f"刚聊完 {idle_h:.1f}h（< {IDLE_MIN_H}h）"
        return info
    if idle_h > IDLE_MAX_H:
        info["why"] = f"太久没聊了 {idle_h:.0f}h（> {IDLE_MAX_H}h）"
        return info
    if last_kind == "bot":
        # 她主动发过、对方还没回 → 12 小时内别再追一句（连发比不说话更烦人）
        sent_at = float(rec.get("sent_at") or 0)
        if sent_at and now - sent_at < 12 * 3600:
            info["why"] = "上次主动找他还没回，再等等"
        else:
            info["why"] = "最后一句是她说的一一不连发"
        return info

    try:
        inti = float(profiles.intimacy(qq))
    except Exception:
        inti = 0.0
    if inti < MIN_INTIMACY:
        info["why"] = f"还不够熟（亲密度 {inti:.2f} < {MIN_INTIMACY}）"
        return info

    # 👇 当前心情 + 对他的好感，一起决定「愿不愿意主动找他聊天 / 倾诉」
    #    （用户 2026-11-20）。亲密度决定熟不熟，好感决定想不想找。
    try:
        aff = float(profiles.get_affinity(qq))
    except Exception:
        aff = 0.0
    if aff <= -0.25:
        info["why"] = f"对他好感是负的（{aff:+.2f}），不想主动找他"
        info["affinity"] = round(aff, 2)
        return info
    try:
        mood_v = float(mood.get_mood(key).get("value") or 0.0) / 100.0
    except Exception:
        mood_v = 0.0

    try:
        chatty = float(mood.mood_factors(key).get("chatty") or 1.0)
    except Exception:
        chatty = 1.0
    drift = _DRIFT_FACTOR.get(str(attention.DRIFT_LEVEL or "active").lower(), 1.0)
    score = chatty * (0.5 + 1.2 * inti) * drift * (1.0 + 0.8 * aff)
    # 心情低 + 好感高 → 更想找他倾诉（不是硬条件，只给一点加成）
    _pour = 1.15 if (mood_v <= -0.25 and aff >= 0.35) else 1.0
    score *= _pour
    info.update({
        "ok": score >= MIN_SCORE,
        "score": round(score, 3),
        "chatty": round(chatty, 2),
        "intimacy": round(inti, 2),
        "affinity": round(aff, 2),
        "mood": round(mood_v, 2),
        "pour": round(_pour, 2),
        "drift": drift,
        "idle_h": round(idle_h, 1),
        "lines": lines,
        "name": profiles.call_of(qq, "") or qq,
        "why": "" if score >= MIN_SCORE else f"分数不够（{score:.2f} < {MIN_SCORE}）",
    })
    return info


def pick(allowed_private) -> dict:
    """从私聊白名单里挑一个最想找的人。挑不到返回 {}。"""
    if not ENABLED:
        return {}
    now = time.time()
    if _in_quiet(now):
        return {}
    best = {}
    for qq in sorted(str(x) for x in (allowed_private or [])):
        try:
            info = score_of(qq)
        except Exception as e:
            logger.debug(f"[PROACTIVE] 评估 {qq} 失败: {e}")
            continue
        if not info.get("ok"):
            logger.debug(f"[PROACTIVE] {qq} 跳过：{info.get('why')}")
            continue
        # 分高的优先；同分随机，避免每次都找同一个人
        info["_r"] = random.random()
        if (not best or (info["score"], info["_r"]) > (best["score"], best["_r"])):
            best = info
    if best:
        best.pop("_r", None)
    return best


def mark_sent(qq: str) -> None:
    data = _load()
    rec = _rec(data, qq)
    if str(rec.get("day") or "") != _today():
        rec["day"] = _today()
        rec["count"] = 0
    rec["count"] = int(rec.get("count") or 0) + 1
    rec["last_at"] = time.time()
    rec["sent_at"] = time.time()
    # 他之前主动说过话 → 上次那句不算「冷场」，清掉冷场计数
    rec["cold"] = 0
    _save(data)


def mark_skipped(qq: str, why: str = "") -> None:
    """她想找但最后没开口 —— 也记一下，避免每 10 分钟反复评估同一个人。"""
    data = _load()
    rec = _rec(data, qq)
    rec["last_at"] = time.time()
    rec["last_skip"] = str(why or "")[:40]
    _save(data)


def prompt_block(cand: dict) -> str:
    """
    给模型的「你现在为什么找他」说明。

    用户 2026-11-19：「主动接话并不是固定语句 —— 她想找谁，说话前先翻下记忆，
    然后翻下之前的聊天记录，以及在这个人面前是什么样的形式（爱撒娇？高冷？可爱？中二？），
    以及最近的关系和一起经历，综合回复。」

    所以这里不是一句「去找他聊聊」，而是把**判断依据**一起给她：
      关系/亲密度 → 我在他面前的样子 → 相处印象 → 相关记忆 → 上次聊到哪 → 我现在的心情
    """
    qq = str(cand.get("qq") or "")
    name = cand.get("name") or qq
    key = f"private_{qq}"
    lines = [
        f"【你想主动找 {name} 说句话】",
        f"你们上次聊天是 {cand.get('idle_h')} 小时前；现在是你主动找他，不是他找你。",
        "先想清楚：**我在他面前是什么样子**、你俩最近是什么关系、上次聊到哪了 ——",
        "然后用那个样子开口。可以接着上次的话题、问问他近况、分享你刚想到的事。",
        "**只说一两句**，像突然想起他那样；别客套、别解释「我为什么找你」。",
        "如果你这会儿其实没什么想说的、或者觉得不合适，就调 stay_silent ——",
        "「想找他」不等于「必须说话」。",
    ]

    # ① 关系 / 我在他面前的样子 / 相处印象（人物卡 full）
    try:
        card = profiles.render_card(qq, "", detail="full", max_len=320)
        if card:
            lines.append("\n【你俩的关系（你记得的）】\n" + card)
    except Exception:
        pass

    # ② 相关记忆：拿最近一句真人消息当 query
    try:
        from . import memory_v2
        _q = ""
        for ln in reversed(cand.get("lines") or []):
            if "：" in ln:
                _q = str(ln).split("：", 1)[-1].strip()
                if _q:
                    break
        # 主动私聊没有群上下文：显式传 gid=""，只捞私聊/全局记忆
        hits = memory_v2.recall(_q or name, limit=3, speaker_qq=qq, gid="")
        if hits:
            lines.append("\n【跟他有关的记忆】\n" + "\n".join(
                f"· {h['summary']}（{h['ago']}）{h.get('emotion_mark') or ''}"
                for h in hits))
    except Exception:
        pass

    # ③ 上次聊到哪了（滚动状态）
    try:
        state = conv_state.render(key)
        if state:
            lines.append(f"\n【上次聊到哪了】\n{state}")
    except Exception:
        pass

    # ④ 我现在的状态 —— 心情不好可以找他倾诉（这也是主动找人的正当理由）
    #    2026-11-20：换成状态块（判读词 + 数值 + 心里话 + 刚才），
    #    比一句「心情不错」更能让她判断「该不该找这个人」。
    try:
        mp = mood.status_block(key) or mood.mood_prompt(key)
        if mp:
            lines.append("\n【你现在的心情/状态】\n" + mp)
    except Exception:
        pass

    return "\n".join(lines)


def status_text() -> str:
    data = _load()
    if not data:
        return "还没有主动私聊记录。"
    out = ["主动私聊记录（每人每天上限 %d 次 / 冷却 %.0fh）：" % (MAX_PER_DAY, COOLDOWN_H)]
    for qq, rec in list(data.items())[:10]:
        when = time.strftime("%m-%d %H:%M",
                             time.localtime(float(rec.get("last_at") or 0)))
        out.append(f"· {qq}：{rec.get('count', 0)} 次，最近 {when}"
                   + (f"，冷场 {rec['cold']} 次" if rec.get("cold") else ""))
    out.append("开关：PROACTIVE_PRIVATE=%s｜演练：%s" % (ENABLED, DRY_RUN))
    return "\n".join(out)
