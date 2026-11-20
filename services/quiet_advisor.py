# services/quiet_advisor.py
"""
「要不要安静、对谁安静、找谁说话」—— 给角色扮演 LLM 的**参考**，不是命令。

用户 2026-11-20 的原话（这套设计的依据）：
  「如果某人让她不开心她可以选择不接话那个人，以及那个人的艾特和图片；
    如果群聊让她感觉难插入或者心情不好可以整个群都安静。
    私聊：如果谁让她觉得不愉快可以对那个人安静。
    我觉得这个要结合看，并不是说好感低和心情不好就单独双向的对人偏见不理。
    而是说要多方面的。比如说忽然连续降低的好感度或者心情，或者大幅度的好感度
    或者心情。当然如果这个人的好感度本身比较高可以有忍耐。但是跌到一定程度
    再加上忽然的连续降低或者大幅度降低事件同时发生就可以选择安静了。
    当然还是那句，道歉可以破冰，可以回暖好感以及回升一些心情。
    当然这些都是外部心情那个 llm 决策的。我们只是加了这套逻辑辅助。
    最终给到她角色扮演 llm 这个结果，建议是否安静对谁安静，以及找谁。」

所以这里**只做信号汇总 + 软建议**：
  · 不给硬阈值开关，不直接调用 chat_switch；
  · 把「好感现在多少、刚才掉了几次、跌得多狠、她忍了多久」「心情现在多少、
    是不是突然掉了」「群里是不是插不上话」摊开给模型；
  · 模型自己决定：要不要安静、对谁、多久、还是照常聊。

三个信号（都归一化到 0~1，越大越建议）：
  1. 对人：好感「低」+ 最近「连续/大幅下降」+ 忍耐（本来好感高/亲近 → 更容忍）
  2. 对群：心情「低」+ 最近「突然下滑」+ 难插入（她说过话没人接 / 群里没人点她）
  3. 找谁：最近在群里说话、好感为正、亲近度不错的人（排第一的给个名字）

⚠️ 关键设计（用户反复强调的）：**好感低本身不构成安静理由**。
    必须同时有「最近连续下降或单次大幅下降」这个事件；只有长期低好感没有事件时，
    分数会被压到阈值以下 —— 她可以不喜欢一个人，但不会因为「就是不喜欢」就静音。
"""
from core import tuning
import os
import time

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

from . import chat_switch, group_mood, group_scene, mood, profiles

ENABLED = os.getenv("QUIET_ADVISOR_ENABLED", "true").lower() in (
    "1", "true", "yes", "on")

# 看「最近多久」的走势（秒）
WINDOW = tuning.f("QUIET_ADVISOR_WINDOW_SEC")
# 建议阈值：低于这个分数就不往 prompt 里塞（避免每轮都在说「你要不要安静」）
PERSON_MIN = tuning.f("QUIET_PERSON_MIN")
GROUP_MIN = tuning.f("QUIET_GROUP_MIN")
# 同一条建议多久内不重复（秒）：她没照做也别每轮念一遍
COOLDOWN = tuning.f("QUIET_ADVISOR_COOLDOWN_SEC")
# 破冰后多久内不重新建议对他安静（秒）
THAW_GRACE = tuning.f("QUIET_THAW_GRACE_SEC")

# 群心情（独立于整体心情）的两个硬阈值：
#   单人 5 小时净跌 0.4 → 单独不理他；整群净跌 0.7 + 3 人 → 群聊不愉快
GM_PERSON_DROP = tuning.f("GROUP_MOOD_PERSON_DROP")
GM_GROUP_DROP = tuning.f("GROUP_MOOD_GROUP_DROP")
GM_MIN_PEOPLE = tuning.i("GROUP_MOOD_MIN_PEOPLE")

_LAST: dict = {}     # session_key -> {"at", "sig"}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(x)))
    except Exception:
        return lo


def _level_cn(score: float, *, person: bool = True) -> str:
    if person:
        if score >= 0.75:
            return "很想避开他"
        if score >= 0.55:
            return "有点不想理他"
        return "稍微想避开他"
    if score >= 0.75:
        return "很想安静一会儿"
    if score >= 0.55:
        return "有点不想说话"
    return "稍微想安静"


# ==================== 群聊「难插入」 ====================
def _hard_to_join(gid: str, addressed: bool, window_sec: float = 600) -> float:
    """
    群里她插不上话的程度 0~1：
      · 她说过话，但之后连着 5 条没人接她、也没人点她 → 1.0（被无视了）
      · 之后 3 条没人接 → 0.6
      · 她压根没说话，群里已经刷了 10 条以上 → 0.5（人多、没她的位置）
    有人在叫她（addressed）→ 0（这时不是插不上话，是该不该应的问题）。
    """
    if addressed:
        return 0.0
    gid = str(gid or "").strip()
    if not gid:
        return 0.0
    try:
        rows = group_scene.recent_messages(gid, 30)
    except Exception:
        return 0.0
    if not rows:
        return 0.0
    now = time.time()
    recent = [r for r in rows if now - float(r.get("ts") or 0) <= window_sec]
    rows = recent or rows[-10:]
    bot_idx = -1
    for i, r in enumerate(rows):
        if r.get("is_bot"):
            bot_idx = i
    if bot_idx >= 0:
        after = rows[bot_idx + 1:]
        called = any(str(r.get("to_kind") or "") == "bot" for r in after)
        if called:
            return 0.1
        if len(after) >= 5:
            return 1.0
        if len(after) >= 3:
            return 0.6
        return 0.2
    if len(rows) >= 10:
        return 0.5
    if len(rows) >= 6:
        return 0.3
    return 0.0


# ==================== 找谁说话 ====================
def _talk_to(gid: str, exclude=()) -> dict:
    """从最近在群里说话的人里，挑一个「好感正、亲近度不错」的。"""
    gid = str(gid or "").strip()
    if not gid:
        return {}
    try:
        rows = group_scene.recent_messages(gid, 40)
    except Exception:
        return {}
    cnt: dict = {}
    for r in rows:
        qq = str(r.get("qq") or "").strip()
        if not qq or qq in exclude or r.get("is_bot"):
            continue
        cnt[qq] = int(cnt.get(qq) or 0) + 1
    # 只算最近说话最多的 8 个人：每人都要读档案/相处记录，人多了白花 IO
    best = None
    for qq, n in sorted(cnt.items(), key=lambda kv: -kv[1])[:8]:
        try:
            aff = profiles.get_affinity(qq)
            inti = profiles.intimacy(qq)
        except Exception:
            continue
        # 好感是主因，亲近度是辅；最近说过话只加一点点（避免只按话多选人）
        score = aff * 0.7 + inti * 0.3 + min(0.10, n * 0.01)
        if aff <= 0.10 or score < 0.25:
            continue
        if best is None or score > best[0]:
            best = (score, qq, aff, inti, n)
    if not best:
        return {}
    _, qq, aff, inti, n = best
    name = ""
    try:
        name = profiles.call_of(qq, "", gid) or profiles.get_profile(qq).get("nickname") or qq
    except Exception:
        name = qq
    why = f"最近在群里说过 {n} 句"
    if inti >= 0.45:
        why += "，你们算熟人"
    return {"qq": qq, "name": name, "aff": round(aff, 2),
            "intimacy": round(inti, 2), "why": why}


# ==================== 对人 ====================
def _person_signal(session_key: str, qq: str, gid: str) -> dict:
    tr = profiles.affinity_trend(qq, WINDOW)
    aff = float(tr.get("cur") or 0.0)
    base = float(tr.get("base") or 0.0)
    try:
        inti = profiles.intimacy(qq)
    except Exception:
        inti = 0.0

    # 忍耐：本来好感越高 / 越亲近，越不会因为一次小事就翻脸。
    tol = _clamp(0.18 + 0.50 * max(0.0, base) + 0.22 * inti, 0.0, 0.90)

    # 「跌到一定程度」：现在的绝对低 + 从窗口起点掉下来的幅度
    low = _clamp((-aff - 0.10) / 0.40)
    fell = _clamp((base - aff) / 0.22)
    level = (low * 0.6 + fell * 0.4) * (1.0 - 0.35 * tol)

    # 「忽然连续降低 / 大幅度降低」——没有这个事件，好感再低也不单独成立
    cont = _clamp((int(tr.get("neg_n") or 0) - 1) / 1.5)
    a_event = max(cont * _clamp(float(tr.get("drop") or 0) / 0.16),
                  _clamp(float(tr.get("spike") or 0) / 0.08))
    mt = mood.trend(session_key, about_qq=qq, window_sec=WINDOW)
    m_event = max(_clamp(float(mt.get("drop") or 0) / 0.40),
                  _clamp(float(mt.get("spike") or 0) / 0.35))
    event = max(a_event, m_event * 0.9)

    score = level * (0.30 + 0.70 * event)
    if event < 0.20:
        score *= 0.25          # 只有「好感低」没有事件 → 基本不建议

    # 👇 群心情里「这个人 5 小时净跌 ≥ 0.4」是独立证据（用户 2026-11-20）：
    #    即使好感本身还高，只要他最近连续让她掉，也该单独不理他（含 @）。
    gpm = {}
    try:
        gpm = group_mood.person_of(session_key, qq) or {}
    except Exception:
        gpm = {}
    gm_drop = float(gpm.get("drop") or 0.0)
    if gm_drop >= GM_PERSON_DROP:
        score = max(score, 0.75 + min(0.20, (gm_drop - GM_PERSON_DROP) * 0.5))

    # 刚道歉/哄她 → 给台阶，别马上又建议安静
    thaw = {}
    try:
        thaw = chat_switch.recent_thaw(session_key, within=THAW_GRACE)
    except Exception:
        thaw = {}
    thawed = bool(thaw) and str(thaw.get("qq") or "") == str(qq)
    if thawed:
        score *= 0.35

    if score < PERSON_MIN:
        return {}

    # ---- 把依据写成一句人话（她看到的是「发生了什么」，不是「算法给了 0.62」）----
    bits = [f"好感 {aff:+.2f}"]
    if abs(base - aff) >= 0.01:
        bits.append(f"本来 {base:+.2f}")
    if int(tr.get("neg_n") or 0) >= 2:
        bits.append(f"{int(tr.get('neg_n'))} 次往下掉、一共 {float(tr.get('drop') or 0):.2f}")
    if float(tr.get("spike") or 0) >= 0.05:
        bits.append(f"最狠一次 -{float(tr.get('spike')):.2f}")
    if float(mt.get("drop") or 0) >= 0.10:
        bits.append(f"心情也跟着掉了 {float(mt.get('drop')):.2f}")
    if gm_drop >= GM_PERSON_DROP:
        bits.append(f"这个群 5 小时里他让你净掉了 {gm_drop:.2f}")
    if tol >= 0.45:
        bits.append("你本来挺容忍他的")
    why = "，".join(bits)
    _w = str(tr.get("why") or "").strip()
    if _w:
        why += f"（最近一次：{_w}）"
    if thawed:
        why += "；不过他刚道歉了，已经给了台阶"

    return {"qq": str(qq), "name": _name_of(qq, gid),
            "score": round(score, 2), "level": _level_cn(score, person=True),
            "why": why, "minutes": 60 if score >= 0.75 else 30,
            "aff": round(aff, 2), "event": round(event, 2), "tol": round(tol, 2)}


def _name_of(qq: str, gid: str = "") -> str:
    try:
        return profiles.call_of(str(qq), "", gid) or str(qq)
    except Exception:
        return str(qq)


# ==================== 对群 ====================
def _group_signal(session_key: str, gid: str, addressed: bool,
                  has_person: bool = False) -> dict:
    mt = mood.trend(session_key, window_sec=WINDOW)
    val = float(mt.get("value") or 0.0)
    low = _clamp((-val - 0.15) / 0.40)
    shock = max(_clamp(float(mt.get("drop") or 0) / 0.45),
                _clamp(float(mt.get("spike") or 0) / 0.35))
    join = _hard_to_join(gid, addressed)
    score = low * (0.35 + 0.65 * max(shock, join * 0.8))
    # 有人在叫她：整群安静会显得「叫不动」，降一点权（但不是不能安静）
    if addressed:
        score *= 0.75
    # 已经明确是「某个人惹的」、群里也没有插不上话的证据 →
    # 只建议对那个人安静就够了，别再让她整群安静（避免一人生气、全群遭殃）。
    if has_person and join < 0.2:
        score *= 0.50

    # 👇 群心情（独立于整体心情，5 小时窗口）：这是「整群环境」的硬证据。
    #    · 整群净跌 0.7 + 至少 3 个人让她难受 → 群聊导致的不愉快（0.85）
    #    · 还没到阈值但已经在往下走 → 给个弱提醒（0.55）
    #    ⚠️ 放在上面那些降权**之后**再取 max：一两个人惹她时不该压掉整群判定。
    gm = {}
    try:
        gm = group_mood.snapshot(session_key) or {}
    except Exception:
        gm = {}
    gm_drop = float(gm.get("drop") or 0.0)
    gm_neg = int(gm.get("neg_people") or 0)
    gm_quiet = bool(gm.get("group_quiet"))
    if gm_quiet:
        score = max(score, 0.85)
    elif gm_drop >= GM_GROUP_DROP * 0.7 and gm_neg >= GM_MIN_PEOPLE:
        score = max(score, 0.55)

    if score < GROUP_MIN:
        return {}

    bits = [f"心情 {val:+.2f}"]
    if float(mt.get("drop") or 0) >= 0.10:
        bits.append(f"刚才掉了 {float(mt.get('drop')):.2f}")
    if float(mt.get("spike") or 0) >= 0.15:
        bits.append(f"一下掉了 {float(mt.get('spike')):.2f}")
    if join >= 0.6:
        bits.append("群里聊得热闹，你插不上话" if join < 0.9 else "你说了话没人接")
    elif join > 0:
        bits.append("群里人多，没你的位置")
    if gm.get("n"):
        bits.append(f"这个群 5 小时净 {float(gm.get('net') or 0):+.2f}")
        bits.append(f"{int(gm.get('likes') or 0)} 人让你舒服 / "
                    f"{int(gm.get('dislikes') or 0)} 人让你不舒服")
    if gm_quiet:
        bits.append(f"整群掉了 {gm_drop:.2f}、涉及 {gm_neg} 人")
    return {"score": round(score, 2), "level": _level_cn(score, person=False),
            "why": "，".join(bits), "minutes": 90 if score >= 0.75 else 45,
            "join": round(join, 2), "event": round(shock, 2),
            "gm_net": round(float(gm.get("net") or 0), 2),
            "gm_drop": round(gm_drop, 2),
            "likes": int(gm.get("likes") or 0),
            "dislikes": int(gm.get("dislikes") or 0),
            "ratio": float(gm.get("ratio") or 0.5),
            "group_quiet": gm_quiet}


# ==================== 汇总 ====================
def _compute(session_key: str, *, speaker_qq: str = "", gid: str = "",
             addressed: bool = True) -> dict:
    key = str(session_key or "")
    gid = str(gid or (key[len("group_"):] if key.startswith("group_") else ""))
    is_group = bool(gid)
    qq = str(speaker_qq or "").strip()
    out = {"ok": False, "person": {}, "group": {}, "talk_to": {},
           "gid": gid, "key": key}
    if not ENABLED:
        return out

    # 已经在安静中 → 不再重复建议（她自己会 on 回来）
    try:
        if chat_switch.is_off(key, qq)[0]:
            out["already_off"] = True
            return out
    except Exception:
        pass

    if qq.isdigit():
        try:
            out["person"] = _person_signal(key, qq, gid) or {}
        except Exception as e:
            logger.debug(f"[QUIET] 对人信号失败: {type(e).__name__}: {e}")
    if is_group:
        try:
            out["group"] = _group_signal(
                key, gid, addressed, has_person=bool(out.get("person"))) or {}
        except Exception as e:
            logger.debug(f"[QUIET] 对群信号失败: {type(e).__name__}: {e}")
        # 心情偏低、或已经建议安静时，才提「可以找谁说话」——平时不打扰
        try:
            _mv = float(mood.trend(key, window_sec=WINDOW).get("value") or 0.0)
            if out["person"] or out["group"] or _mv <= 0.05:
                out["talk_to"] = _talk_to(gid, exclude=(qq,)) or {}
        except Exception:
            pass
    out["ok"] = bool(out["person"] or out["group"] or out["talk_to"])
    return out


def advise(session_key: str = "", *, speaker_qq: str = "", gid: str = "",
           addressed: bool = True) -> dict:
    """算一次建议（带冷却：同一条建议 COOLDOWN 内不重复给）。"""
    out = _compute(session_key, speaker_qq=speaker_qq, gid=gid,
                   addressed=addressed)
    if not out.get("ok"):
        return out
    key = out.get("key") or ""
    p = out.get("person") or {}
    g = out.get("group") or {}
    t = out.get("talk_to") or {}
    sig = "|".join([str(p.get("qq") or ""), "g" if g else "",
                    str(t.get("qq") or "")])
    score = max(float(p.get("score") or 0), float(g.get("score") or 0))
    now = time.time()
    prev = _LAST.get(key) or {}
    # 同一轮里 build_packet 可能组装两次（第二次带上图书馆结果）：
    # 5 秒内再算一次不算「重复」，照给 —— 否则第二次组装会把建议吞掉。
    if prev and now - float(prev.get("at") or 0) < 5:
        return out
    # 同一条建议 15 分钟内不重复；但如果情况**明显变严重**（分数又涨 0.15），
    # 就允许再说一次 —— 否则「越忍越难受」时她反而看不到提醒。
    if (prev.get("sig") == sig
            and now - float(prev.get("at") or 0) < COOLDOWN
            and score < float(prev.get("score") or 0) + 0.15):
        return {"ok": False, "person": {}, "group": {}, "talk_to": {},
                "gid": out.get("gid") or "", "key": key, "cooled": True}
    _LAST[key] = {"at": now, "sig": sig, "score": round(score, 2)}
    # 会话数不会无限涨：只留最近 200 个（超了丢最久没用的）
    if len(_LAST) > 200:
        for _k in sorted(_LAST, key=lambda k: float((_LAST.get(k) or {}).get("at") or 0))[:50]:
            _LAST.pop(_k, None)
    out["sig"] = sig
    return out


def render(session_key: str = "", *, speaker_qq: str = "", gid: str = "",
           addressed: bool = True, speaker_name: str = "",
           dry_run: bool = False) -> str:
    """
    给角色扮演 LLM 的一段「参考建议」。没有值得说的就返回 ""（不占 prompt）。

    dry_run=True：只算不记冷却（/测函数 用）—— 否则看一眼自检就把
    真正的 prompt 建议压掉 15 分钟。
    """
    out = (_compute(session_key, speaker_qq=speaker_qq, gid=gid,
                    addressed=addressed)
           if dry_run else
           advise(session_key, speaker_qq=speaker_qq, gid=gid,
                  addressed=addressed))
    if not out.get("ok"):
        return ""
    lines = []
    p = out.get("person") or {}
    if p:
        lines.append(f"· 对「{p['name']}」：{p['level']}（倾向 {p['score']:.2f}）"
                     f"—— {p['why']}。想安静的话可以 {p['minutes']} 分钟")
    g = out.get("group") or {}
    if g:
        lines.append(f"· 这个群：{g['level']}（倾向 {g['score']:.2f}）"
                     f"—— {g['why']}。想安静的话可以 {g['minutes']} 分钟")
    t = out.get("talk_to") or {}
    if t:
        lines.append(f"· 想找人说说话：「{t['name']}」比较合适"
                     f"（好感 {float(t['aff']):+.2f}，{t['why']}）")
    if not lines:
        return ""
    lines.append(
        "→ 想安静就用 chat_switch(action=\"off\")：target 写某个人 = **只屏蔽他**"
        "（他的 @ 和图片也不接，群里别人照常）；target=\"here\" = 整个群安静。"
        "他道歉/哄你了就 action=\"on\" 给台阶。"
        "不想安静就照常聊 —— 这只是参考，不是命令。")
    return "\n".join(lines)


def status_text(session_key: str = "", *, speaker_qq: str = "",
                gid: str = "", addressed: bool = True) -> str:
    """/测函数 安静 用：把信号原样摊开（不触发冷却、不改任何状态）。"""
    out = _compute(session_key, speaker_qq=speaker_qq, gid=gid,
                   addressed=addressed)
    key = out.get("key") or ""
    lines = [f"· 开关：{'开' if ENABLED else '关（QUIET_ADVISOR_ENABLED=false）'}"
             f"｜窗口 {WINDOW / 60:.0f} 分钟"
             f"｜阈值 人 {PERSON_MIN:.2f} / 群 {GROUP_MIN:.2f}"]
    if out.get("already_off"):
        lines.append("· 当前会话/这个人已经在安静中 → 不再重复建议")
    if speaker_qq:
        try:
            tr = profiles.affinity_trend(str(speaker_qq), WINDOW)
            mt = mood.trend(key, about_qq=str(speaker_qq), window_sec=WINDOW)
            lines.append(
                f"· 好感：现在 {float(tr['cur']):+.2f}（窗口前 {float(tr['base']):+.2f}）"
                f"｜下降 {float(tr['drop']):.2f}×{int(tr['neg_n'])} 次"
                f"｜最大 -{float(tr['spike']):.2f}"
                f"｜亲近度 {profiles.intimacy(str(speaker_qq)):.2f}")
            lines.append(
                f"· 因为他掉的心情：{float(mt['drop']):.2f}"
                f"｜单次最大 -{float(mt['spike']):.2f}")
            _th = chat_switch.recent_thaw(key, within=THAW_GRACE)
            if _th:
                lines.append(f"· 最近破冰：{int((time.time() - float(_th['at'])) / 60)} 分钟前"
                             f"（{_th.get('why') or '没写原因'}）→ 建议降权")
        except Exception as e:
            lines.append(f"· 好感/心情走势读取失败：{type(e).__name__}: {e}")
    if out.get("gid"):
        mt = mood.trend(key, window_sec=WINDOW)
        lines.append(f"· 本群心情：{float(mt['value']):+.2f}"
                     f"｜下降 {float(mt['drop']):.2f}"
                     f"｜单次最大 -{float(mt['spike']):.2f}"
                     f"｜难插入 {_hard_to_join(out['gid'], addressed):.2f}")
        # 群心情（独立层）：5 小时净变化 / 喜欢讨厌人数 / 整群判定
        try:
            lines.append(group_mood.status_text(key))
        except Exception as e:
            lines.append(f"· 群心情读取失败：{type(e).__name__}: {e}")
    if speaker_qq:
        try:
            _gpm = group_mood.person_of(key, str(speaker_qq))
            if _gpm:
                lines.append(f"· 这个人在本群 5 小时：净 {_gpm['net']:+.2f}"
                             f"｜跌 {_gpm['drop']:.2f}"
                             f"（单独不理阈值 {GM_PERSON_DROP}）")
        except Exception:
            pass
    p = out.get("person") or {}
    g = out.get("group") or {}
    lines.append("· 结论："
                 + ("对人 " + p.get("level", "") + f"（{p.get('score')}）"
                    if p else "对人 不建议")
                 + "｜"
                 + ("对群 " + g.get("level", "") + f"（{g.get('score')}）"
                    if g else "对群 不建议"))
    if p:
        lines.append(f"   · 对人依据：{p.get('why')}")
    if g:
        lines.append(f"   · 对群依据：{g.get('why')}")
    t = out.get("talk_to") or {}
    if t:
        lines.append(f"· 可以找：{t.get('name')}（好感 {float(t.get('aff') or 0):+.2f}）")
    return "\n".join(lines)
