# services/group_scene.py
"""
群聊现场（2026-11-19，用户最想抄酒馆的那部分）。

要解决的问题
------------
群里同时有好几条对话线：
  阿明在说加班、老王在说抽卡、小柚子刚 @ 了她。
以前我们只给模型一串 `时间 名字: 内容`，它要自己猜：
  · 这句是谁对谁说的？
  · 现在有几条线程？
  · 我该接哪条？接了会不会串台？

这个模块把每条消息补上 **「谁 → 谁」**，并按回复链/提及/参与者聚成线程，
最后给模型一段「群聊现场」：线程 + 参与者 + 最近两句 + 谁在叫她。

不是 SillyTavern 的 World Info，也不是角色卡；是它没有、但真群聊必须有的东西。
"""
from core import tuning

import os
import re
import time
from collections import OrderedDict

# 收发件人信号置信度（DESIGN §3.5）：规则只做**候选召回**，不单独判决。
ADDR_AT_OR_QUOTE = tuning.f("ADDR_AT_OR_QUOTE")
ADDR_QUOTE = tuning.f("ADDR_QUOTE")
ADDR_VOCATIVE = tuning.f("ADDR_VOCATIVE")
ADDR_NAME = tuning.f("ADDR_NAME")
ADDR_QA_PAIR = tuning.f("ADDR_QA_PAIR")
ADDR_YOU = tuning.f("ADDR_YOU")
ADDR_BACKCHANNEL = tuning.f("ADDR_BACKCHANNEL")
ADDR_RECENT_MENTION = tuning.f("ADDR_RECENT_MENTION")
ADDR_ASSERT_MIN = tuning.f("ADDR_ASSERT_MIN")
ADDR_MAYBE_MIN = tuning.f("ADDR_MAYBE_MIN")

from services import identity as _identity

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

# ============================ 保留策略：按条数，不按时间 ============================
# 用户 2026-11-19：
#   「A：昨晚那部新番看了吗」……10 分钟后……「B：对啊，都是原作的锅」
#   按时间段拉会把这两句拆开，审核 LLM 看到的就是两句莫名其妙的话，必然判不出来。
#   ⚠️ 这只是**判读的上下文**（让判读知道她在接什么话）；
#      心情/好感仍只来自她的 [[THINK]]（ADR-038），对话本身不直接改数值。
#      例：同一句「原作的锅」，她心里可能是「这俩人真会聊」（impact 0），
#          也可能是「又在我面前聊我看不懂的」（impact −1）——只有 THINK 知道。
# 所以：**每个群保留最近 KEEP_TOTAL 条消息**，时间只作为标签显示（「12分钟前」），
# 不参与「要不要保留 / 算不算同一条线」的判断。
KEEP_TOTAL = 120          # 每个群总共留多少条消息（超出从最旧的线程开始丢）
THREAD_TTL = 0.0          # ⚠️ 已废弃：0 = 不按时间淘汰（保留常量兼容老调用）
MAX_THREADS = 8
# 每条线程留几句。⚠️ 必须 >= 审核窗口（20）：一条线程要是只留 8 句，
# 「拉最近 20 条」根本凑不出来，完整性又没了。
KEEP_MSGS = 24
# 新消息没 @ 时，并进「最近 MERGE_GAP 条消息内还有人说话」的线程
MERGE_GAP = 6

# ==================== 重启后从对话日志补现场 ====================
# 用户 2026-11-19 实测：
#   群里 5 句（小强→土豆→鱼），中间重启过一次，之后 `/测函数 现场` 只有
#   「窗口 1 条」，审核也只看到最后一句 —— 「打印还是打不到那条，明明 10 条间隔」。
# 原因：现场（线程/收件人）只活在内存里，一重启就空了；而**原文在对话日志里**。
# 所以：现场为空时，按**条数**（不是按天）把最近 N 条从 data/dialogue 读回来，
# 时间戳保留原值（卡片上的「5分钟前」才是真的），重启不再等于失忆。
SEED_FROM_LOG = os.getenv("SCENE_SEED_FROM_LOG", "true").lower() in ("1", "true", "yes", "on")
SEED_LIMIT = int(os.getenv("SCENE_SEED_LIMIT", str(KEEP_MSGS)))
SEED_RETRY = tuning.f("SCENE_SEED_RETRY")

_THREADS: dict = {}       # gid -> list[thread]
_LAST: dict = {}          # gid -> 最新一条
_PREV: dict = {}          # gid -> 上一条（推断「没 @ 的接话」用）
_THREAD_SYNC: dict = {}   # gid -> 上次把线程镜像到 sqlite 的时间
_NAMES: dict = {}         # gid -> (ts, {name: qq})
_SUMMARY: dict = {}       # gid -> {text, at, until_mid}（审核 LLM 给的滚动总结）
_SEEDED: dict = {}        # gid -> 上次尝试补现场的时间（失败也限流，别每次都读盘）
_SEED_N: dict = {}        # gid -> 上一次补回来几条（卡片/自检要显示）
_BOT_QQ: dict = {}        # gid -> 她当前登录的 QQ（历史号见 services.identity.accounts）
_SEQ = 0


def _is_bot_qq(qq, bot_qq: str = "") -> bool:
    """这个 QQ 是不是她自己。

    2026-11-23 换号后：只认「当前登录号」不够 —— 旧号 100000018 的发言
    还在现场/日志里，会被当成陌生群友。所以任何登录过的号都算
    （services.identity 维护 accounts 别名表）。
    """
    q = str(qq or "").strip()
    if not q:
        return False
    if bot_qq and q == str(bot_qq):
        return True
    try:
        return _identity.is_self(q)
    except Exception:
        return False


# 昵称清洗/渲染统一在 services/text.py（2026-11-27）：
#   · clean_name —— 去零宽/格式字符（`下个路口见⁧⁧喵⁧⁧` 这种人看着一样、模型会串）
#   · render_name —— 含人称代词的昵称加「」（`我的床会长猫` 会被当成一句话）
# 这里保留同名导出，老调用点不用改。
from services import text as _text

clean_name = _text.clean_name
render_name = _text.render_name


def _self_id() -> str:
    try:
        return _identity.canonical()
    except Exception:
        return ""


def _to_is_bot(m: dict, bot_qq: str = "") -> bool:
    """这条消息的收件人是不是她（当前号 + 历史号都算）。"""
    if str((m or {}).get("to_kind") or "") == "bot":
        return True
    return _is_bot_qq((m or {}).get("to_qq"), bot_qq)


def _now() -> float:
    return time.time()


def _new_thread(gid: str, ts: float) -> dict:
    global _SEQ
    _SEQ += 1
    t = {
        "id": f"t{_SEQ}",
        "created": ts,
        "last": ts,
        "participants": {},   # qq -> name
        "topic": "",
        "msgs": [],
        "bot_spoke": False,
    }
    lst = _THREADS.setdefault(gid, [])
    lst.append(t)
    # 按「最后说话时间」排序后再截断：丢的是最久没动静的线程，不是最早创建的
    lst.sort(key=lambda x: x.get("last") or 0)
    _THREADS[gid] = lst[-MAX_THREADS:]
    return t


def _active(gid: str, window: float = 0.0) -> list:
    """
    取「还在场上」的线程。

    window<=0（默认）：**按条数保留**，不按时间淘汰（用户 2026-11-19 的要求）。
      一个群最多留 KEEP_TOTAL 条消息 / MAX_THREADS 条线程，超出从最旧的丢。
    window>0：老行为，按时间过滤（现在只有显式传参才会走这条）。
    """
    ts = list(_THREADS.get(gid) or [])
    if window and window > 0:
        now = _now()
        ts = [t for t in ts if now - float(t.get("last") or 0) <= window]
    ts.sort(key=lambda x: x.get("last") or 0)
    ts = ts[-MAX_THREADS:]
    total = sum(len(t.get("msgs") or []) for t in ts)
    while len(ts) > 1 and total > KEEP_TOTAL:
        total -= len(ts[0].get("msgs") or [])
        ts = ts[1:]
    _THREADS[gid] = ts
    return ts


def _gap_since(gid: str, t: dict) -> int:
    """
    这条线程最后一句之后，群里又多了几条消息（0 = 它就是最后一条）。
    用它代替「几分钟内」，判断「还算不算同一条线」。
    """
    ms = t.get("msgs") or []
    if not ms:
        return 9999
    last_ts = float(ms[-1].get("ts") or 0)
    n = 0
    for x in _active(gid):
        for m in (x.get("msgs") or []):
            if float(m.get("ts") or 0) > last_ts:
                n += 1
    return n


def _find_by_mid(gid: str, mid: str):
    if not mid:
        return None
    for t in _active(gid):
        for m in t.get("msgs") or []:
            if str(m.get("mid") or "") == str(mid):
                return t
    return None


def _pick_thread(gid: str, qq: str, to_qq: str, reply_mid: str) -> dict:
    active = _active(gid)
    # ① 引用回复 → 直接找被引用那条所在的线程
    t = _find_by_mid(gid, reply_mid)
    if t:
        return t
    # ② 自己刚在某个线程说过 → 先接着那条
    #    （@ 她 / 在群里点别人，通常还是同一条对话线，不该另开一条）
    mine = [x for x in active if qq in (x.get("participants") or {})]
    if mine:
        latest = max(mine, key=lambda x: x.get("last") or 0)
        # 按「隔了几条消息」判断，不按分钟：A 说完没人接，10 分钟后 A 又说一句，
        # 中间一条都没有 → gap 0 → 还是同一条线
        if _gap_since(gid, latest) <= MERGE_GAP:
            return latest
    # ③ @ 了某人 → 这个人的线程；找不到就**新开**一条，
    #    不要并进最近线程（否则「老王→阿强」会被塞进「阿明→小柚子」那条线）
    if to_qq:
        cand = [x for x in active if to_qq in (x.get("participants") or {})]
        if cand:
            return max(cand, key=lambda x: x.get("last") or 0)
        return _new_thread(gid, _now())
    # ④ 没线索 → 并进最近 3 分钟的线程；但那条线已经 3 人以上时，
    #    新人更可能是在开新话题，别硬塞
    if active:
        latest = max(active, key=lambda x: x.get("last") or 0)
        if (_gap_since(gid, latest) <= MERGE_GAP
                and len(latest.get("participants") or {}) <= 2):
            return latest
    return _new_thread(gid, _now())


_STOP_CHARS = set("的了是在我你他她它这那有和就都也还很吧吗呢啊哦嗯个上下来去说对不没")


def _bigrams(text: str) -> set:
    cs = [c for c in str(text or "")
          if (("\u4e00" <= c <= "\u9fa5") or c.isalnum()) and c not in _STOP_CHARS]
    if len(cs) <= 1:
        return set(cs)
    return set(zip(cs, cs[1:]))


def _charset(text: str) -> set:
    return {c for c in str(text or "")
            if (("\u4e00" <= c <= "\u9fa5") or c.isalnum()) and c not in _STOP_CHARS}


def _overlap(a: str, b: str) -> float:
    """
    中文短句用「字 + 二元组」混合重合度：
      只看二元组太严（「保底都歪」vs「抽卡歪了」一个字都对不上），
      只看单字又太松（「你」这种会误判）。0.6 单字 + 0.4 二元组。
    """
    ca, cb = _charset(a), _charset(b)
    ba, bb = _bigrams(a), _bigrams(b)
    cs = len(ca & cb) / max(1, min(len(ca), len(cb))) if ca and cb else 0.0
    bs = len(ba & bb) / max(1, min(len(ba), len(bb))) if ba and bb else 0.0
    return 0.6 * cs + 0.4 * bs


def _best_thread_by_topic(gid: str, text: str):
    """按字面重合度找最像的线程；都低于阈值就返回 None（开新线程）。"""
    best, score = None, 0.0
    for t in _active(gid):
        hay = str(t.get("topic") or "")
        # 看最近 SCENE_WINDOW 条（不是 3 条）：话题是慢慢聊出来的，
        # 只看最后三句，「保底都歪」对不上「抽卡」这种就判不出来。
        for m in (t.get("msgs") or [])[-SCENE_WINDOW:]:
            hay += " " + str(m.get("text") or "")
        s = _overlap(text, hay)
        if s > score:
            best, score = t, s
    return best if score >= 0.12 else None


def names_of(gid: str, mapping: dict, ttl: float = 90.0) -> dict:
    """缓存一份「名字 → QQ」表；调用方每次把最新名册传进来即可。"""
    ts, old = _NAMES.get(gid, (0.0, {}))
    if mapping:
        merged = dict(old or {})
        for k, v in mapping.items():
            k = str(k or "").strip()
            if k and v:
                merged[k] = str(v)
        _NAMES[gid] = (_now(), merged)
        return merged
    if _now() - ts <= ttl:
        return old or {}
    return old or {}


# 硬逻辑 / 线程视图看多少条：和审核窗口对齐（用户 2026-11-19：
# 「线程1 和我们的硬逻辑应该同时看 20 条来判断，而不是现在的五六条」）
SCENE_WINDOW = tuning.i("SCENE_WINDOW_MSGS")

# 冲人短句（「找打了」「欠揍」「你完了」）：在群里通常不是回上一句，
# 而是冲**最近被提到 / 被吐槽的那个人**。用户 2026-11-19 真机：
# 老王「找打了」其实是对「肥鱼」说的，硬逻辑和审核都判成了紧挨着的上一句。
_SHORT_JAB_MAX = 12


def _recent_mentioned(gid: str, qq: str, nmap: dict, bot_qq: str = "",
                      window: int = 0) -> tuple:
    """最近 window 条消息里，最后一个「正文点到名字」的人（不是自己、不是她）。"""
    if not nmap:
        return "", ""
    window = int(window or SCENE_WINDOW)
    try:
        rows = _rows(gid, window)      # 按时间升序，最后一条最新
    except Exception:
        return "", ""
    for m in reversed(rows):
        if str(m.get("qq") or "") == str(qq):
            continue
        q, n = _guess_addressee(str(m.get("text") or ""),
                                str(m.get("qq") or ""), nmap)
        if q and not _is_bot_qq(q, bot_qq) and q != str(qq):
            return q, n
    return "", ""


def _jab_target(gid: str, qq: str, text: str, nmap: dict, bot_qq: str = "") -> tuple:
    """短句才做「冲谁」推断；长句正常按上下文判。"""
    t = str(text or "").strip()
    if not t or len(t) > _SHORT_JAB_MAX:
        return "", ""
    return _recent_mentioned(gid, qq, nmap, bot_qq)


def _guess_addressee(text: str, qq: str, names: dict) -> tuple:
    """没有 @ / 引用时，用「正文里点了谁的名字」猜收件人。"""
    t = str(text or "")
    if not t or not names:
        return "", ""
    # 长名字优先，避免「小明」把「小明哥」抢了
    for nm, q in sorted(names.items(), key=lambda x: -len(x[0])):
        if len(nm) < 2 or str(q) == str(qq):
            continue
        if nm in t:
            return str(q), nm
    return "", ""


# ---------------- 没 @ 时的收件人推断（用户 2026-11-19） ----------------
# 真实群里大量接话不带 @/引用，不能一律算「大家」。这里分四层：
#   ① 点名/称呼（强）  ② 问答相邻（中）  ③ 反应/附和相邻（中）  ④ 无信号 → 大家/不确定
_Q_END = re.compile(r"[?？]\s*$")
_Q_WORDS = (
    "吗", "呢", "吧", "怎么", "为啥", "为什么", "哪", "谁", "多少",
    "是不是", "能不能", "要不要", "有没有", "咋", "几点", "多久",
)
_REACTIONS = {
    "哈哈", "哈哈哈", "哈哈哈哈", "草", "笑死", "6", "666", "确实", "同意",
    "我也是", "对啊", "是的", "嗯", "哦", "啊这", "离谱", "难绷", "好家伙",
    "?", "??", "???", "？", "？？", "...", "。。", "牛的", "可以", "行",
}
_ANSWER_START = (
    "对", "是", "不", "没", "有", "我", "他", "她", "它", "这", "那",
    "嗯", "好", "可以", "行", "因为", "所以", "就是", "刚", "才",
)


def _is_question(text: str) -> bool:
    t = str(text or "").strip()
    if not t:
        return False
    if _Q_END.search(t):
        return True
    return any(w in t for w in _Q_WORDS)


def _is_reaction(text: str) -> bool:
    t = str(text or "").strip().lower()
    if not t or len(t) > 10:
        return False
    if t in _REACTIONS:
        return True
    # 纯标点 / 纯表情占位
    if all(not ch.isalnum() and ch not in "？?！!，,。" for ch in t):
        return True
    return False


def _looks_like_answer(text: str) -> bool:
    t = str(text or "").strip()
    if not t or len(t) > 60:
        return False
    if _is_question(t):
        return False
    return t.startswith(_ANSWER_START)


def _vocative(text: str, names: dict) -> tuple:
    """开头就是「名字，…」「名字你…」这种称呼 → 收件人比较确定。"""
    t = str(text or "").strip().lstrip("@＠")
    if not t or not names:
        return "", ""
    for nm, q in sorted(names.items(), key=lambda x: -len(x[0])):
        if len(nm) < 2:
            continue
        if t.startswith(nm):
            rest = t[len(nm):].lstrip()
            if not rest or rest[0] in "，,：: 、你咱":
                return str(q), nm
    return "", ""


def _infer_addressee(gid: str, qq: str, text: str, prev: dict,
                     nmap: dict, explicit_qq: str = "",
                     explicit_reason: str = "", bot_qq: str = "") -> dict:
    """
    返回 {to_qq, to_name, to_kind, to_conf, to_reason}。

    to_kind: bot / person / group / unknown
    to_conf: 0~1，低于 0.5 的不要当确定收件人展示
    """
    explicit_qq = str(explicit_qq or "")
    if explicit_qq:
        if _is_bot_qq(explicit_qq, bot_qq):
            # 不管 @ 的是当前号还是历史号，收件人统一记成人格 ID
            return {"to_qq": _self_id() or explicit_qq, "to_name": "你",
                    "to_kind": "bot", "to_conf": ADDR_AT_OR_QUOTE,
                    "to_reason": explicit_reason or "at"}
        nm = ""
        for _nm, _q in nmap.items():
            if str(_q) == explicit_qq:
                nm = _nm
                break
        return {"to_qq": explicit_qq, "to_name": nm or explicit_qq,
                "to_kind": "person", "to_conf": ADDR_QUOTE,
                "to_reason": explicit_reason or "at"}

    # ① 开头称呼（「阿明，你…」）
    vq, vn = _vocative(text, nmap)
    if vq and vq != str(qq):
        return {"to_qq": vq, "to_name": vn, "to_kind": "person",
                "to_conf": ADDR_VOCATIVE, "to_reason": "vocative"}

    # ② 正文里点了名字（不是开头也算，但置信度低一点）
    nq, nn = _guess_addressee(text, qq, nmap)
    if nq:
        return {"to_qq": nq, "to_name": nn, "to_kind": "person",
                "to_conf": ADDR_NAME, "to_reason": "name"}

    # ③ 相邻轮次：上一句是别人说的，且时间很近
    if prev and str(prev.get("qq") or "") != str(qq):
        try:
            gap = _now() - float(prev.get("ts") or 0)
        except Exception:
            gap = 999
        if gap <= 120:
            prev_text = str(prev.get("text") or "")
            # 这句直接用了「你」（不是「你们/你俩」）→ 大概率在回上一句的人
            if ("你" in text and "你们" not in text and "你俩" not in text):
                return {"to_qq": str(prev.get("qq") or ""),
                        "to_name": str(prev.get("name") or ""),
                        "to_kind": "person", "to_conf": ADDR_YOU,
                        "to_reason": "second_person"}
            # 上一句在提问 → 这句大概率是回答
            if _is_question(prev_text) and not _is_question(text):
                return {"to_qq": str(prev.get("qq") or ""),
                        "to_name": str(prev.get("name") or ""),
                        "to_kind": "person", "to_conf": ADDR_QA_PAIR,
                        "to_reason": "answer"}
            # 反应词 / 附和 / 明显在接话
            if _is_reaction(text) or _looks_like_answer(text):
                return {"to_qq": str(prev.get("qq") or ""),
                        "to_name": str(prev.get("name") or ""),
                        "to_kind": "person", "to_conf": ADDR_BACKCHANNEL,
                        "to_reason": "adjacent"}

    # ④ 没信号：不是「大家」，是「不知道对谁」
    return {"to_qq": "", "to_name": "", "to_kind": "group",
            "to_conf": 0.2, "to_reason": "none"}


def _weak_candidates(gid: str, qq: str, text: str) -> list:
    """
    对每条活跃线程打一个「这句像不像在接这条」的分，返回降序列表。
    分数来源：第二人称、反应词、上一句是问题、接话开头、话题重合、时间近。
    """
    out = []
    now = _now()
    for t in _active(gid):
        ms = t.get("msgs") or []
        if not ms:
            continue
        prev = ms[-1]
        if str(prev.get("qq") or "") == str(qq):
            continue
        s = 0.0
        reasons = []
        if "你" in text and "你们" not in text and "你俩" not in text:
            s += 0.45
            reasons.append("second_person")
        if _is_reaction(text):
            s += 0.4
            reasons.append("reaction")
        if (not _is_reaction(text)
                and _is_question(prev.get("text") or "")
                and not _is_question(text)):
            s += 0.5
            reasons.append("answer")
        if _looks_like_answer(text):
            s += 0.3
            reasons.append("adjacent")
        # 话题重合度看最近 SCENE_WINDOW 条（用户 2026-11-19：只给 3 条判不出来）
        hay = str(t.get("topic") or "") + " " + " ".join(
            str(m.get("text") or "") for m in ms[-SCENE_WINDOW:])
        ov = _overlap(text, hay)
        if ov >= 0.12:
            s += min(0.35, ov * 0.8)
            reasons.append("topic")
        # 这条线程的上一句就是群里最后一句 → 大概率在接它。
        # 交错发言时这是最可靠的信号（用户 2026-11-19：阿明连说几句，
        # 喵插一句「好噢~」，光看字面重合度判不出来）
        if _gap_since(gid, t) == 0:
            # 0.5：单独就够到「接这条线」的门槛。交错发言时它比字面重合度可靠得多
            s += 0.5
            reasons.append("last")
        try:
            gap = now - float(prev.get("ts") or 0)
        except Exception:
            gap = 999
        if gap <= 30:
            s += 0.2
        elif gap <= 120:
            s += 0.1
        out.append((round(s, 3), t, prev, reasons))
    out.sort(key=lambda x: -x[0])
    return out


def observe(gid: str, *, mid: str = "", qq: str = "", name: str = "",
            text: str = "", to_qq: str = "", to_name: str = "",
            to_reason: str = "", reply_mid: str = "", is_bot: bool = False,
            bot_qq: str = "", names=None, ts: float = 0,
            no_audit: bool = False) -> dict:
    """
    记一条群消息，返回它的元信息：
      {thread_id, topic, to_qq, to_name, participants}

    ts: 保留原始时间（回放/从日志补现场时用），卡片上的「几分钟前」才是真的。
    no_audit: 占位消息（[图片]/[视频] 这种没有字可判的），不进审核。
    """
    gid = str(gid or "").strip()
    if not gid:
        return {}
    if bot_qq:
        remember_bot(gid, bot_qq)
    ts = float(ts or 0) or _now()
    qq = str(qq or "")
    # 她自己说的话：不管来自哪个登录号，统一记成人格 ID（换号后日志能连上）
    if is_bot:
        qq = _identity.canon(qq) or qq
    elif qq and _is_bot_qq(qq, bot_qq):
        is_bot = True
        qq = _identity.canon(qq) or qq
    name = clean_name(name or qq)
    text = str(text or "").strip()
    # 有些适配器/回显事件没有 message_id；没有 id 审核结果就映射不回来。
    # 生成一个进程内唯一的合成 id（同一条消息只 observe 一次，不会重复）。
    mid = str(mid or "").strip()
    if not mid:
        mid = f"auto-{int(ts * 1000)}-{abs(hash(text)) % 100000}"

    # 同一条消息只进现场一次：NapCat 会回显、识图和 chat 也可能各记一遍、
    # 从日志补现场还可能和刚收到的活消息撞上。重复记账单会变成两条。
    _dup_t = _find_by_mid(gid, mid)
    if _dup_t is not None:
        for _m in (_dup_t.get("msgs") or []):
            if str(_m.get("mid") or "") == mid:
                return dict(_m, thread_id=_dup_t.get("id"))

    nmap = names_of(gid, names or {})
    prev_global = _LAST.get(gid) or {}
    if is_bot:
        inf = {"to_qq": "", "to_name": "", "to_kind": "group",
               "to_conf": 0.0, "to_reason": "bot"}
        # 她自己说的话：跟着被引用的那条，否则跟着最近一条线程
        t = _find_by_mid(gid, reply_mid)
        if t is None:
            active = _active(gid)
            t = max(active, key=lambda x: x.get("last") or 0) if active else None
        if t is None:
            t = _new_thread(gid, _now())
    else:
        # ① 先看强信号（@ / 引用 / 开头称呼 / 正文点名）
        strong = _infer_addressee(
            gid, qq, text, {}, nmap,
            explicit_qq=to_qq, explicit_reason=to_reason, bot_qq=bot_qq,
        )
        if str(strong.get("to_reason") or "") in ("at", "reply", "vocative", "name"):
            inf = strong
            t = _pick_thread(gid, qq, str(inf.get("to_qq") or ""), reply_mid)
        else:
            # ② 先看「是不是在接着自己上一条说」：
            #    同一个人的连续发言，不该被算成在回别人
            # ⚠️ 必须「自己那条就是群里最后一条」才算自我延续。
            #    以前用「60 秒内」判断：阿明连说 5 句、喵插一句「好噢~」，
            #    因为整段才 20 秒，喵被判定成「接着自己上一条说」，
            #    结果同一场对话被拆成两条线程（用户 2026-11-19 实测）。
            own_last = [
                x for x in _active(gid)
                if (x.get("msgs") or [])
                and str((x["msgs"][-1] or {}).get("qq") or "") == str(qq)
            ]
            own_last.sort(key=lambda x: x.get("last") or 0, reverse=True)
            # 她在里面说过话的线程（中间被别人插了一句，也还算她这条线）
            own_any = [x for x in _active(gid) if qq in (x.get("participants") or {})]
            own_any.sort(key=lambda x: x.get("last") or 0, reverse=True)
            _decided = False
            if own_last and _gap_since(gid, own_last[0]) == 0:
                t = own_last[0]
                inf = {"to_qq": "", "to_name": "", "to_kind": "group",
                       "to_conf": 0.2, "to_reason": "self_continue"}
                _decided = True
            elif own_any:
                # 被别人插过一句，但和那条线话题重合 → 还是同一条线
                t0 = own_any[0]
                hay = str(t0.get("topic") or "") + " " + " ".join(
                    str(m.get("text") or "") for m in (t0.get("msgs") or [])[-SCENE_WINDOW:])
                if _overlap(text, hay) >= 0.12:
                    t = t0
                    inf = {"to_qq": "", "to_name": "", "to_kind": "group",
                           "to_conf": 0.2, "to_reason": "self_continue"}
                    _decided = True
            if _decided:
                cands = []
            else:
                cands = _weak_candidates(gid, qq, text)
            top = cands[0] if cands else None
            if _decided:
                pass
            elif top and top[0] >= 0.5:
                score, t, prev, reasons = top
                # 两个候选分差很小 → 不硬猜，标「不确定」
                if len(cands) > 1 and (score - cands[1][0]) < 0.1:
                    alt = [
                        str(cands[0][2].get("name") or ""),
                        str(cands[1][2].get("name") or ""),
                    ]
                    inf = {
                        "to_qq": "", "to_name": "、".join([x for x in alt if x]),
                        "to_kind": "unknown", "to_conf": 0.35,
                        "to_reason": "ambiguous",
                    }
                elif reasons == ["last"]:
                    # 只是「接在上一句后面」，没有任何指向谁的字面证据：
                    # 线程归对了，收件人别硬认（交给审核 LLM）。
                    # 例外：短句冲人（「找打了」「欠揍」）→ 冲最近被提到的那个人。
                    _jq, _jn = _jab_target(gid, qq, text, nmap, bot_qq)
                    if _jq:
                        inf = {"to_qq": _jq, "to_name": _jn, "to_kind": "person",
                               "to_conf": ADDR_RECENT_MENTION,
                               "to_reason": "recent_mention"}
                    else:
                        inf = {"to_qq": "", "to_name": "", "to_kind": "group",
                               "to_conf": 0.35, "to_reason": "last"}
                else:
                    # 「接上一句」只用来归线程，不能拿来抬高「就是回他」的置信度：
                    # 否则紧挨着谁说话就变成 0.85 确定，审核也就没机会纠错了。
                    _core = [r for r in reasons if r != "last"]
                    _score = score - (0.5 if "last" in reasons else 0.0)
                    inf = {
                        "to_qq": str(prev.get("qq") or ""),
                        "to_name": str(prev.get("name") or ""),
                        "to_kind": "person",
                        "to_conf": min(0.85, 0.3 + max(0.0, _score) * 0.5),
                        "to_reason": "+".join(_core) or "last",
                    }
            else:
                # ③ 没有任何接话信号 → 按字面重合度找线程；找不到就新开
                t = _best_thread_by_topic(gid, text)
                if t is None:
                    t = _new_thread(gid, _now())
                _jq, _jn = _jab_target(gid, qq, text, nmap, bot_qq)
                if _jq:
                    inf = {"to_qq": _jq, "to_name": _jn, "to_kind": "person",
                           "to_conf": ADDR_RECENT_MENTION,
                           "to_reason": "recent_mention"}
                else:
                    inf = {"to_qq": "", "to_name": "", "to_kind": "group",
                           "to_conf": 0.2, "to_reason": "none"}
    prev = prev_global
    # 这条所在线程的上一句（比全局上一句更准：交错线程时不会串线）
    _tmsgs = t.get("msgs") or []
    _tprev = _tmsgs[-1] if _tmsgs else {}
    to_qq = str(inf.get("to_qq") or "")
    to_name = clean_name(inf.get("to_name") or to_name or "")
    to_kind = str(inf.get("to_kind") or "group")
    try:
        to_conf = float(inf.get("to_conf") or 0.0)
    except Exception:
        to_conf = 0.0
    to_reason = str(inf.get("to_reason") or "")
    if to_qq and not to_name:
        if _is_bot_qq(to_qq, bot_qq):
            to_name = "你"
        else:
            for _nm, _q in nmap.items():
                if str(_q) == to_qq:
                    to_name = _nm
                    break
    t["last"] = ts
    parts = t.setdefault("participants", {})
    if qq:
        parts[qq] = name
    if to_qq:
        parts.setdefault(to_qq, to_name or to_qq)
    if is_bot:
        t["bot_spoke"] = True
    if not t.get("topic"):
        t["topic"] = text[:24] or "（没说话）"

    t.setdefault("msgs", []).append({
        "mid": str(mid or ""),
        "ts": ts,
        "qq": qq,
        "name": name,
        "text": text[:120],
        "to_qq": to_qq,
        "to_name": to_name,
        "to_kind": to_kind,
        "to_conf": to_conf,
        "to_reason": to_reason,
        # 硬逻辑的原始判断，审核 LLM 改完后还能对比
        "hard_kind": to_kind,
        "hard_conf": to_conf,
        "hard_reason": to_reason,
        "audited": False,
        "is_bot": bool(is_bot),
        # 占位消息（[图片]/[视频]/[语音]）没有字可判收件人，审核跳过；
        # 但它必须留在现场里，否则「鱼：这猫躺得比我还会摆烂」会失去前因。
        "no_audit": bool(no_audit),
    })
    t["msgs"] = t["msgs"][-KEEP_MSGS:]

    meta = {
        "thread_id": t["id"],
        "topic": t.get("topic") or "",
        "to_qq": to_qq,
        "to_name": to_name,
        "to_kind": to_kind,
        "to_conf": to_conf,
        "to_reason": to_reason,
        "hard_kind": to_kind,
        "hard_conf": to_conf,
        "hard_reason": to_reason,
        "participants": dict(parts),
        "prev_qq": str(_tprev.get("qq") or ""),
        "prev_name": str(_tprev.get("name") or ""),
        "prev_text": str(_tprev.get("text") or ""),
        "prev_ts": float(_tprev.get("ts") or 0),
    }
    if prev:
        _PREV[gid] = dict(prev)
    _LAST[gid] = dict(meta, mid=str(mid or ""), qq=qq, name=name,
                      text=text[:120], is_bot=bool(is_bot), ts=ts)
    # 线程镜像到 sqlite（retrieval §8）：30 秒一次，避免每条消息都写
    try:
        from services import memdb
        _now2 = time.time()
        if _now2 - _THREAD_SYNC.get(str(gid), 0) > 30:
            _THREAD_SYNC[str(gid)] = _now2
            memdb.upsert_thread(
                thread_id=str(t["id"]), gid=str(gid),
                topic=str(t.get("topic") or ""),
                participants=list((parts or {}).keys()),
                last_ts=float(t.get("last") or ts or _now2))
            logger.info(f"[MEM] thread sync {gid} {str(t['id'])[-6:]}"
                        f"「{str(t.get('topic') or '')[:12]}」")
    except Exception:
        pass
    return meta


def last(gid: str) -> dict:
    return dict(_LAST.get(str(gid)) or {})


def find_by_mid(gid: str, mid: str) -> dict:
    """按消息号找现场里的那条（白月光统计：她引用的是谁）。"""
    mid = str(mid or "").strip()
    if not mid:
        return {}
    for t in _THREADS.get(str(gid), []):
        for m in (t.get("msgs") or []):
            if str(m.get("mid") or "") == mid:
                return dict(m)
    return {}


def replay(gid: str, rows, *, bot_qq: str = "", names=None,
           step: float = 3.0, keep: int = 60) -> int:
    """
    把一批历史消息灌进现场，用来**在没有真人说话时测现场/审核**。

    用户 2026-11-19：重启后群里没人说话，`/测函数 现场` 只剩「线程 0」，
    没法验证审核到底通没通。这里用对话日志回放，时间戳改写成「刚刚」，
    否则会被 THREAD_TTL 当过期线程丢掉。

    rows: [{"qq": "...", "who"/"name": "...", "text": "...", "kind": "user"/"bot"}]
    返回灌进去的条数。
    """
    gid = str(gid or "").strip()
    if not gid:
        return 0
    clear(gid)
    rows = [r for r in (rows or []) if str((r or {}).get("text") or "").strip()]
    n = len(rows)
    now = _now()
    for i, r in enumerate(rows):
        is_bot = str(r.get("kind") or "") == "bot"
        qq = str(r.get("qq") or "")
        name = str(r.get("who") or r.get("name") or qq or "某人")
        observe(
            gid, mid=f"rp-{i}", qq=qq,
            name="你" if is_bot else name,
            text=str(r.get("text") or "")[:120],
            is_bot=is_bot, bot_qq=bot_qq, names=names,
            ts=now - (n - i) * float(step or 3.0),
        )
    return len(recent_messages(gid, keep))


def set_summary(gid: str, text: str, *, until_mid: str = "") -> dict:
    """审核 LLM 给的「这段在聊什么」；until_mid 用来判断总结是不是已经过期。"""
    gid = str(gid or "").strip()
    text = str(text or "").strip()
    if not gid or not text:
        return {}
    rec = {
        "text": text[:300],
        "at": _now(),
        "until_mid": str(until_mid or ""),
    }
    _SUMMARY[gid] = rec
    return dict(rec)


def get_summary(gid: str) -> dict:
    return dict(_SUMMARY.get(str(gid)) or {})


def summary_stale(gid: str, ttl: float = 180.0) -> bool:
    gid = str(gid or "").strip()
    rec = _SUMMARY.get(gid) or {}
    if not rec.get("text"):
        return True
    if _now() - float(rec.get("at") or 0) > ttl:
        return True
    rows = recent_messages(gid, 1)
    if rows and str(rec.get("until_mid") or "") != str(rows[-1].get("mid") or ""):
        return True
    return False


def _ago(ts: float) -> str:
    d = max(0.0, _now() - float(ts or 0))
    if d < 60:
        return f"{int(d)}秒前"
    if d < 3600:
        return f"{int(d / 60)}分钟前"
    return f"{int(d / 3600)}小时前"


def snapshot(gid: str, *, bot_qq: str = "", my_names=None,
             window: float = 0.0, max_threads: int = 3, lines: int = 0) -> dict:
    """
    给 prompt 用的现场快照。

    lines: 最新那条线程渲染几句（默认 SCENE_WINDOW=20，和审核窗口对齐）；
           其它线程固定 4 句 —— 卡片/prompt 别被老线程撑爆。

    returns:
      threads: [{id, ago, participants:[names], topic, lines:[...]}]
      to_bot:  [{name, text, ago}]
      last_is_bot: bool
    """
    gid = str(gid or "").strip()
    if not gid:
        return {"threads": [], "to_bot": [], "last_is_bot": False}
    bot_qq = str(bot_qq or "")
    if bot_qq:
        remember_bot(gid, bot_qq)
    # 现场是空的（刚重启）→ 先按条数从日志补回来，别让卡片只显示最后一句
    _ensure_seeded(gid)
    names = [str(x) for x in (my_names or []) if str(x).strip()]

    threads = sorted(_active(gid, window), key=lambda x: x.get("last") or 0, reverse=True)
    out_threads = []
    to_bot = []
    _n_lines = max(3, int(lines or SCENE_WINDOW))
    for _ti, t in enumerate(threads[:max_threads]):
        parts = [str(v) for v in (t.get("participants") or {}).values() if v]
        lines = []
        # 最新那条线程给足 20 句（她要判的就是这条）；其它线程 4 句够看个上下文
        _take = _n_lines if _ti == 0 else min(4, _n_lines)
        for m in (t.get("msgs") or [])[-_take:]:
            who = render_name(m.get("name") or m.get("qq") or "?")
            if m.get("is_bot"):
                who = "你"
            to = render_name(m.get("to_name"))
            kind = str(m.get("to_kind") or "")
            try:
                conf = float(m.get("to_conf") or 0)
            except Exception:
                conf = 0.0
            if _to_is_bot(m, bot_qq):
                to = "你"
            elif kind == "person" and to:
                # 推断出来的收件人标「推断」，别让模型当成确定事实
                if conf < 0.8:
                    to = f"{to}（推断）"
            elif kind == "unknown":
                # 「→ ？：」这种双标点在卡片上很丑，直接写「不确定」
                to = f"不确定（可能回 {to}）" if to else "不确定"
            else:
                to = ""
            arrow = f" → {to}" if to else " → 大家"
            body = str(m.get("text") or "").strip()
            if body:
                lines.append(f"  {who}{arrow}：{body[:70]}")
            if _to_is_bot(m, bot_qq) or (
                not m.get("is_bot") and any(n and n in body for n in names)
            ):
                to_bot.append({
                    "name": who,
                    "text": body[:90],
                    "ago": _ago(m.get("ts") or 0),
                })
        out_threads.append({
            "id": t.get("id"),
            "ago": _ago(t.get("last") or 0),
            "participants": parts,
            "topic": t.get("topic") or "",
            "lines": lines,
        })

    last_msg = None
    for t in threads:
        ms = t.get("msgs") or []
        if ms:
            last_msg = ms[-1]
            break
    cur = _LAST.get(gid) or {}
    prev_info = {}
    if cur.get("prev_name") or cur.get("prev_qq"):
        prev_info = {
            "name": str(cur.get("prev_name") or cur.get("prev_qq") or "?"),
            "text": str(cur.get("prev_text") or "")[:90],
            "ago": _ago(cur.get("prev_ts") or 0),
            "is_question": _is_question(cur.get("prev_text") or ""),
        }
    sm = get_summary(gid)
    return {
        "threads": out_threads,
        "to_bot": to_bot[-3:],
        "last_is_bot": bool(last_msg and last_msg.get("is_bot")),
        "prev": prev_info,
        "summary": sm,
        "seeded": seeded_count(gid),
    }


def format_block(snap: dict, *, limit_threads: int = 3) -> str:
    """把快照渲染成 prompt 里的一段「群聊现场」。"""
    if not snap:
        return ""
    threads = (snap.get("threads") or [])[:limit_threads]
    to_bot = snap.get("to_bot") or []
    if not threads and not to_bot:
        return ""
    lines = ["▍群聊现场（谁在跟谁说话）"]
    for i, t in enumerate(threads, 1):
        who = "、".join(t.get("participants") or [])[:30] or "?"
        lines.append(f"【线程{i}｜{t.get('ago')}｜{who}】话题：{t.get('topic') or '（不明）'}")
        lines.extend(t.get("lines") or [])
    prev = snap.get("prev") or {}
    if prev:
        _q = "（这是个问题）" if prev.get("is_question") else ""
        lines.append(f"【上一句】{prev.get('name')}：{prev.get('text')}{_q}")
    if to_bot:
        lines.append("【在叫你】")
        for x in to_bot:
            lines.append(f"  · {x.get('ago')} {x.get('name')}：{x.get('text')}")
    lines.append(
        "（优先接「在叫你」；**如果【在叫你】里有多条，每一条都要回应**——"
        "可以合成一条回复，用 `|||` 分条、每条用 `<reply:消息号>` 引对应那句，"
        "别只挑最后一条。"
        "其次接最近有人提问/有情绪的线程；没点名的消息先看【上一句】是不是在接它 —— "
        "标了「推断」的收件人不是确定的，拿不准就当对大家说。"
        "回的时候说清你在回谁；不相干的线程别混进来、别总结。）"
    )
    sm = snap.get("summary") or {}
    if sm.get("text"):
        lines.append(f"【最近这段在聊什么】{sm['text']}")
    return "\n".join(lines)


def addressee_line(gid: str, bot_qq: str = "") -> str:
    """这一条是「谁 → 谁」，给 build_prompt 用。"""
    m = last(gid)
    if not m:
        return ""
    who = m.get("name") or m.get("qq") or "?"
    to_qq = str(m.get("to_qq") or "")
    to_name = str(m.get("to_name") or "")
    topic = str(m.get("topic") or "")
    kind = str(m.get("to_kind") or "group")
    try:
        conf = float(m.get("to_conf") or 0)
    except Exception:
        conf = 0.0
    if kind == "bot" or _is_bot_qq(to_qq, bot_qq):
        s = f"· 这条是 {who} → 你（在跟你说话）"
    elif kind == "person" and to_qq:
        if conf >= ADDR_ASSERT_MIN:
            s = f"· 这条是 {who} → {to_name or to_qq}（**不是你**）→ 里面的「你」指 {to_name or to_qq}，别当成在说你"
        elif conf >= ADDR_MAYBE_MIN:
            _why = ("最近提到过他" if "recent_mention" in str(m.get("to_reason") or "")
                    else "按上一句推断")
            s = (f"· 这条是 {who} → **像是在回 {to_name or to_qq}**（没点名，{_why}；"
                 f"也可能是在对大家说）→ 别默认是在叫你")
        else:
            s = (f"· 这条是 {who} → 不确定（信号太弱；可能是在回 {to_name or to_qq}，"
                 f"别硬认成在叫你）")
    elif kind == "unknown":
        if to_name:
            s = (f"· 这条是 {who} → 不确定（没点名；可能是在回 {to_name}，"
                 f"拿不准，别硬认成在叫你）")
        else:
            s = f"· 这条是 {who} → 不确定（没点名，也没接上一句）"
    else:
        prev_qq = str(m.get("prev_qq") or "")
        prev_name = str(m.get("prev_name") or "")
        if prev_qq and prev_qq == str(m.get("qq") or ""):
            s = f"· 这条是 {who} → 大家（接着他自己上一条继续说）"
        elif prev_name:
            s = (f"· 这条是 {who} → 大家（没点名；同一线程上一句是 {prev_name} 说的，"
                 f"可能是在接他，也可能是在自言自语）")
        else:
            s = f"· 这条是 {who} → 大家（没点名；可能是自言自语，也可能在等谁接）"
    if topic:
        s += f"\n· 他们这条线在聊：{topic}"
    return s


def clear(gid: str) -> None:
    gid = str(gid or "")
    _THREADS.pop(gid, None)
    _LAST.pop(gid, None)
    _PREV.pop(gid, None)
    _NAMES.pop(gid, None)
    _SUMMARY.pop(gid, None)
    _SEEDED.pop(gid, None)
    _SEED_N.pop(gid, None)
    _BOT_QQ.pop(gid, None)


# ============================ 重启补现场（读盘） ============================ #
def remember_bot(gid: str, bot_qq: str) -> None:
    """记住「她在这个群的 QQ」，补现场时才知道哪句是她自己说的。

    2026-11-23：顺手把这个号登记进人格别名表 —— 换号后旧号的发言
    仍然算「她说的」，不会被当成陌生群友建档案/算白月光。
    """
    gid, bot_qq = str(gid or "").strip(), str(bot_qq or "").strip()
    if gid and bot_qq:
        _BOT_QQ[gid] = bot_qq
        try:
            _identity.note_account(bot_qq)
        except Exception:
            pass


def _drop_threads(gid: str) -> None:
    """只清「线程 / 最新一条」，保留节流和计数（重建现场时用）。"""
    _THREADS.pop(gid, None)
    _LAST.pop(gid, None)
    _PREV.pop(gid, None)


def _same_msg(m: dict, qq: str, txt: str, ts: float) -> bool:
    """是不是同一句（重放时用来去重：QQ + 文本 + 5 秒内的同一个时刻）。"""
    try:
        return (str(m.get("qq") or "") == str(qq)
                and str(m.get("text") or "").strip()[:40] == str(txt).strip()[:40]
                and abs(float(m.get("ts") or 0) - float(ts or 0)) <= 5)
    except Exception:
        return False


def seed_from_log(gid: str, *, bot_qq: str = "", limit: int = 0,
                  force: bool = False, before: float = 0.0,
                  rebuild: bool = False) -> int:
    """
    把最近 N 条对话从 data/dialogue 读回现场，返回补回来几条。

    rebuild=True：现场里已经有零星几条（重启后先来了一句）→ 连同日志**按时间重放**，
      否则「这句该接哪条线」是拿未来消息推的，会莫名其妙多出一条线程。
    before>0：只补比这个时间更早的（不重建时用）。
    按条数读，不按天：一个群安静两天，最近 24 条也照样在日志里。
    """
    gid = str(gid or "").strip()
    if not gid or not SEED_FROM_LOG or "#replay" in gid:
        return 0
    now = _now()
    if not force and now - float(_SEEDED.get(gid) or 0) < SEED_RETRY:
        return 0
    # 先占位：observe 里如果又绕回来（_scene_names → recent_messages），不要递归
    _SEEDED[gid] = now
    limit = int(limit or SEED_LIMIT or KEEP_MSGS)
    try:
        from . import dialogue_log
        recs = dialogue_log.load_recent(gid, limit=limit)
    except Exception as e:
        logger.debug(f"[SCENE] {gid} 读对话日志失败: {type(e).__name__}: {e}")
        return 0
    if not recs:
        return 0

    # ⚠️ 一定要在确认「日志里有东西」之后再清：日志空还清，等于把现场删了
    mem = []
    if rebuild:
        mem = [dict(m) for m in _rows(gid, KEEP_TOTAL)]
        _drop_threads(gid)

    bot_qq = str(bot_qq or _BOT_QQ.get(gid) or "")
    nmap = {}
    for r in recs:
        nm = str(r.get("who") or "").strip()
        qq = str(r.get("qq") or "").strip()
        if nm and qq and nm != "你" and nm not in nmap:
            nmap[nm] = qq

    n = 0
    seeded = []
    for _i, r in enumerate(recs):
        txt = str(r.get("text") or "").strip()
        if not txt:
            continue
        qq = str(r.get("qq") or "")
        try:
            ts = float(r.get("ts") or 0) or now
        except Exception:
            ts = now
        if before and ts >= float(before):
            continue
        # 加序号：同一个人同一毫秒连发两条时，光靠 ts+qq 会撞 id → 被去重吃掉一条
        mid = f"log-{int(ts * 1000)}-{qq}-{_i}"
        if _find_by_mid(gid, mid) is not None:      # 同一条已经在现场里了
            continue
        is_bot = (str(r.get("kind") or "") == "bot"
                  or _is_bot_qq(qq, bot_qq))
        # 日志里的 who 是「阿明(1135)」这种带 QQ 尾巴的标签，卡片/审核都得去掉
        name = "你" if is_bot else re.sub(
            r"[（(]\d{2,}[)）]\s*$", "", str(r.get("who") or "")).strip()
        try:
            observe(gid, mid=mid, qq=qq, name=name or qq, text=txt,
                    is_bot=is_bot, bot_qq=bot_qq, names=nmap, ts=ts)
            seeded.append((qq, txt, ts))
            n += 1
        except Exception:
            continue

    # 重建：内存里那几条（日志还没写进去的）再补回去，日志里已有的跳过
    for m in mem:
        if any(_same_msg(m, _q, _t, _s) for _q, _t, _s in seeded):
            continue
        try:
            observe(gid, mid=str(m.get("mid") or ""), qq=str(m.get("qq") or ""),
                    name=str(m.get("name") or ""), text=str(m.get("text") or ""),
                    to_qq=str(m.get("to_qq") or ""),
                    to_name=str(m.get("to_name") or ""),
                    to_reason=str(m.get("to_reason") or ""),
                    is_bot=bool(m.get("is_bot")), bot_qq=bot_qq,
                    ts=float(m.get("ts") or 0), no_audit=bool(m.get("no_audit")))
        except Exception:
            continue

    if n or mem:
        _restore_order(gid)
    _SEED_N[gid] = int(_SEED_N.get(gid) or 0) + n
    if n:
        logger.info(f"[SCENE] {gid} 从对话日志补回 {n} 条（重启后现场不再只剩最后一句）")
    return n


def _restore_order(gid: str) -> None:
    """按 ts 重排每条线程，并把「最新一条」指回真正最新的那条。"""
    newest, newest_t = None, None
    for t in (_THREADS.get(gid) or []):
        ms = t.get("msgs") or []
        if not ms:
            continue
        ms.sort(key=lambda m: float(m.get("ts") or 0))
        t["last"] = float(ms[-1].get("ts") or 0)
        if newest is None or float(ms[-1].get("ts") or 0) >= float(newest.get("ts") or 0):
            newest, newest_t = ms[-1], t
    if newest is None:
        return
    prev_m = {}
    ms = (newest_t or {}).get("msgs") or []
    if len(ms) >= 2:
        prev_m = ms[-2]
    _LAST[gid] = dict(
        newest, topic=str((newest_t or {}).get("topic") or ""),
        prev_qq=str(prev_m.get("qq") or ""),
        prev_name=str(prev_m.get("name") or ""),
        prev_text=str(prev_m.get("text") or ""),
        prev_ts=float(prev_m.get("ts") or 0),
    )


def seeded_count(gid: str) -> int:
    """上一次从日志补回来几条（卡片/自检显示用）。"""
    return int(_SEED_N.get(str(gid or "")) or 0)


def ensure_seeded(gid: str) -> None:
    """
    现场太薄 → 从日志补回来（按条数）。

    为什么不是「现场为空才补」：重启后第一条消息进来时，现场就已经有 1 条了 ——
    只判空的话永远补不上，`/测函数 现场` 还是「窗口 1 条」。
    正常运行时现场有几十条，这里直接 return，零成本。
    """
    gid = str(gid or "").strip()
    if not gid or "#replay" in gid:
        return
    mem = _rows(gid, SEED_LIMIT)
    if len(mem) >= max(4, SEED_LIMIT // 2):
        return
    seed_from_log(gid, rebuild=bool(mem))


_ensure_seeded = ensure_seeded


# ================================ 审核 LLM 用 ================================ #
def _rows(gid: str, limit: int = 20, window: float = 0.0) -> list:
    """取现场里的消息（纯内存，不补日志）；recent_messages 的内部实现。"""
    gid = str(gid or "").strip()
    if not gid:
        return []
    rows = []
    for t in _active(gid, window):
        for m in (t.get("msgs") or []):
            rows.append(dict(m, thread_id=t.get("id")))
    rows.sort(key=lambda x: float(x.get("ts") or 0))
    return rows[-max(1, int(limit)):]


def image_target(gid: str, sender_qq: str, at_qqs=None, bot_qq: str = "",
                 names: dict | None = None) -> tuple:
    """
    这张图片/表情是发给谁的。返回 (kind, label, reason)，kind ∈ bot/person/group/unknown。

    用户 2026-11-20 的真实翻车：群里一对情侣，A @ 了 B 然后发了个「抱抱」表情，
    她接话「抱了上去」—— 她以为那是给她的/或她要参与。
    图片消息本身没有文字，光看描述判断不了收件人，必须结合 **@ 段 + 上文**。

    规则（从强到弱）：
      1. @ 了她 → bot
      2. @ 了别人 → person（那个人的名字）
      3. 上一条是她说的话 → bot（他在接她）
      4. 上一条是某人说的 → person（他在接那个人）
      5. 都没有 → unknown（看不出来，让她自己判断）
    """
    gid = str(gid or "")
    at = [str(x) for x in (at_qqs or []) if str(x)]
    nm = names or {}
    if any(_is_bot_qq(x, bot_qq) for x in at):
        return "bot", "你", "这条 @ 了你"
    if at:
        who = str(nm.get(at[0]) or at[0])
        return "person", who, f"这条 @ 了 {who}"
    try:
        rows = [r for r in recent_messages(gid, 4)
                if str(r.get("qq") or "") != str(sender_qq)]
    except Exception:
        rows = []
    if rows:
        last = rows[-1]
        if last.get("is_bot"):
            return "bot", "你", "你刚说完话，他在接你"
        who = str(last.get("name") or last.get("qq") or "")
        if who:
            return "person", who, f"他在接 {who} 的话"
    return "unknown", "", "看不出在跟谁说话"


def recent_messages(gid: str, limit: int = 20, window: float = 0.0) -> list:
    """
    取最近 N 条消息（按时间升序），给审核 LLM / 调试用。
    每条带 thread_id / 硬逻辑判断 / 是否已审核。

    现场为空时先按条数从对话日志补回来（重启后也能看到 20 条，不是 1 条）。
    """
    _ensure_seeded(gid)
    return _rows(gid, limit, window)


def history_lines(gid: str, n: int = 8, *, width: int = 160,
                  exclude_mids=(), exclude_texts=(),
                  replied_mids=()) -> list:
    """**处理过的历史**：带收发件人的最近 n 条（判心情 / 角色扮演 / 翻历史 共用）。

    用户 2026-11-22：
      「收发标签处理消息后给 LLM 判心情顺便给 LLM 做上下文……
        上下文不用直接从 napcat 拉了。包括拉过去未来也是标签处理过的历史。
        等于 pending + 上下文 + 拉取前后文消息合为一个块，前置链路是收发件人标签链路。」

    所以这里是唯一出口：现场里的每条消息都已经过硬逻辑 + 审核 LLM 的「谁 → 谁」，
    谁要用历史（主模型上下文、三方判心情、HISTORY 工具、/测函数 分段）都读它，
    不再各自去 recent_group_msgs / dialogue_log / napcat 各拉一份。
    """
    gid = str(gid or "").strip()
    if not gid:
        return []
    _ex = {str(x) for x in (exclude_mids or ()) if str(x or "").strip()}
    # 兜底：有些适配器事件没有 message_id（mid 为空）→ 再按 (qq, 文本前缀) 排除，
    # 否则「当前这一段」会在历史块里再出现一遍（用户 2026-11-22：模板重复）。
    _ex_txt = {str(x) for x in (exclude_texts or ()) if str(x or "").strip()}
    # 👇 2026-11-24 用户：「多次拉 8 条要标记哪些消息 id 回复过，
    #    不要因为这里 bug 重复回复。」已回过的那几条前面打〔已回〕，
    #    模型就不会再针对它们开一轮。
    _replied = {str(x) for x in (replied_mids or ()) if str(x or "").strip()}
    try:
        # 多取一些，过滤掉「当前这一段」后仍然凑够 n 条
        rows = recent_messages(gid, max(1, int(n)) + len(_ex) + len(_ex_txt))
    except Exception:
        return []
    out = []
    for m in rows:
        txt = re.sub(r"\s+", " ", str(m.get("text") or "")).strip()
        if not txt:
            continue
        if m.get("is_bot"):
            out.append(f"你 → 大家: {txt[:width]}")
            continue
        _mid = str(m.get("mid") or "")
        _key = f"{m.get('qq') or ''}|{str(m.get('text') or '')[:40]}"
        if (_ex and _mid and _mid in _ex) or (_ex_txt and _key in _ex_txt):
            # 已经在「当前这一段」里了 → 历史里不要再出现一遍
            continue
        who = render_name(m.get("name") or m.get("qq") or "?")
        kind = str(m.get("to_kind") or "").strip().lower()
        nm = re.sub(r"[(（]\s*\d{3,}\s*[)）]$", "",
                    render_name(m.get("to_name"))).strip()
        try:
            conf = float(m.get("to_conf") or 0.0)
        except Exception:
            conf = 0.0
        if kind == "bot":
            to = "你"
        elif kind == "person" and nm:
            to = nm if conf >= 0.5 else f"大概{nm}"
        elif kind == "unknown" and nm:
            to = f"{nm}?"
        else:
            to = "大家"
        # ⭐ 和 prompt 里「带 ⭐ 的是在叫你」对齐（2026-11-23）：
        # 以前只写「→ 你」不打 ⭐，而规矩让模型找 ⭐ → 她根本找不到重点。
        _star = "⭐" if to == "你" else ""
        _done = "〔已回〕" if (_mid and _mid in _replied) else ""
        out.append(f"{_star}{_done}{who} → {to}: {txt[:width]}")
    return out[-max(1, int(n)):]


def needs_audit(gid: str, limit: int = 20) -> list:
    """
    哪些消息值得让审核 LLM 看：
      · 没审核过；
      · 不是她自己的话；
      · 硬逻辑只给了「大家/不确定/推断」或置信度 < 0.75。
    强信号（@/引用/开头称呼/正文点名）不需要审核。
    """
    out = []
    for m in recent_messages(gid, limit):
        if m.get("is_bot") or m.get("audited") or m.get("no_audit"):
            continue
        reason = str(m.get("hard_reason") or m.get("to_reason") or "")
        kind = str(m.get("to_kind") or "group")
        try:
            conf = float(m.get("to_conf") or 0)
        except Exception:
            conf = 0.0
        if reason in ("at", "reply", "vocative"):
            continue
        if kind in ("group", "unknown") or conf < 0.75:
            out.append(m)
    return out


def audit_skip_reason(m: dict) -> str:
    """
    这条**为什么没进审核**（给现场卡片显示用）：
      返回 "" → 该审但还没审（真的漏了）；
      返回非空 → 本来就不该审（强信号 / 硬逻辑已经确定）。
    用户 2026-11-19：满屏「审：未审核」看不出是漏了还是不用审。
    """
    if not m:
        return ""
    if m.get("is_bot"):
        return "她自己说的"
    if m.get("no_audit"):
        return "占位（图片/视频，没有字可判）"
    reason = str(m.get("hard_reason") or m.get("to_reason") or "")
    if reason in ("at", "reply", "vocative"):
        return f"强信号·{reason_cn(reason)}"
    try:
        conf = float(m.get("to_conf") or 0)
    except Exception:
        conf = 0.0
    kind = str(m.get("to_kind") or "group")
    if kind in ("group", "unknown") or conf < 0.75:
        return ""
    return f"硬逻辑已确定·{conf:.2f}"


def apply_audit(gid: str, results, names: dict = None, bot_qq: str = "",
                bot_name: str = "", notes: list = None) -> int:
    """
    把审核 LLM 的结果写回消息。

    results: [{"mid": "123", "to": "阿明"/"大家"/"自己"/"不确定", "why": "..."}]
    返回实际改写的条数。
    notes: 可选，把「为什么没写回」记进去（名字对不上 / 不在窗口），
           给 /测函数 审核 显示用。

    规则：
      · @ / 引用 这种强证据不推翻，只记 conflict；
      · 其它（推断/大家/不确定）以审核结果为准；
      · 审核说「不确定」时，如果硬逻辑有明确 person，保留硬逻辑。
    """
    gid = str(gid or "").strip()
    if not gid or not results:
        return 0
    names = names or {}
    qmap = {}
    for nm, q in names.items():
        nm = str(nm or "").strip()
        if nm and q:
            qmap[nm] = str(q)
    if bot_name and bot_qq:
        qmap[str(bot_name)] = str(bot_qq)

    # ⚠️ 窗口要和 recent_messages 一致（900s）：以前这里用默认 600s，
    #    审核看到的是 900s 内的消息，写回时却找不到 600s 前那条 → 静默丢弃。
    idx = {}
    for t in _active(gid):
        for m in (t.get("msgs") or []):
            idx[str(m.get("mid") or "")] = m

    notes = notes if notes is not None else []
    changed = 0
    for r in results:
        if not isinstance(r, dict):
            continue
        mid = str(r.get("mid") or r.get("id") or "")
        m = idx.get(mid)
        if not m:
            notes.append(f"{mid} 不在窗口")
            continue
        to = str(r.get("to") or "").strip()
        why = str(r.get("why") or r.get("reason") or "").strip()[:60]
        hard_reason = str(m.get("hard_reason") or m.get("to_reason") or "")
        hard_kind = str(m.get("hard_kind") or m.get("to_kind") or "")
        hard_qq = str(m.get("to_qq") or "")

        if to in ("大家", "group", "所有人", "群里"):
            kind, qq, name, conf = "group", "", "", 0.8
        elif to in ("自己", "self", "自言自语"):
            kind, qq, name, conf = "group", "", "", 0.8
            why = (why + "｜自言自语").strip("｜")
        elif to in ("不确定", "unknown", "不知道", ""):
            kind, qq, name, conf = "unknown", "", "", 0.3
        else:
            qq = qmap.get(to, "")
            if not qq:
                # 审核可能写的是别名/带后缀 → 做一次包含匹配
                for _nm, _q in qmap.items():
                    if _nm and (_nm in to or to in _nm):
                        qq, to = str(_q), _nm
                        break
            if not qq:
                # 名字对不上群成员 → 不硬改
                m["audited"] = True
                m["audit_note"] = f"llm:{to}（对不上人）"
                notes.append(f"「{to}」对不上人")
                continue
            if _is_bot_qq(qq, bot_qq):
                kind, qq = "bot", _self_id() or qq
            else:
                kind = "person"
            name, conf = to, 0.85

        # 强证据（@/引用）不推翻
        if hard_reason in ("at", "reply"):
            m["audit_conflict"] = f"llm:{to}"
            m["audited"] = True
            for store in (_LAST, _PREV):
                d = store.get(gid)
                if d and str(d.get("mid") or "") == mid:
                    d.update({"audited": True, "audit_conflict": m["audit_conflict"]})
            changed += 1
            continue
        # 审核说「不确定」时，硬逻辑如果是明确 person，保留
        if kind == "unknown" and hard_kind == "person" and hard_qq:
            m["audited"] = True
            m["audit_note"] = "llm:不确定，保留硬逻辑"
            for store in (_LAST, _PREV):
                d = store.get(gid)
                if d and str(d.get("mid") or "") == mid:
                    d.update({"audited": True, "audit_note": m["audit_note"]})
            changed += 1
            continue

        upd = {
            "to_kind": kind, "to_qq": qq, "to_name": name,
            "to_conf": conf,
            "to_reason": "llm" + (f":{why}" if why else ""),
            "audited": True, "audit_at": _now(),
        }
        m.update(upd)
        # _LAST / _PREV 是 addressee_line 用的快照，也要同步，否则现场文案还是旧的
        for store in (_LAST, _PREV):
            d = store.get(gid)
            if d and str(d.get("mid") or "") == mid:
                d.update(upd)
        changed += 1
    return changed


_KIND_CN = {"bot": "你", "person": "某人", "group": "大家", "unknown": "不确定"}

# 推断原因也翻译成人话：卡片上「硬:某人/second_person+adjacent」没人看得懂
_REASON_CN = {
    "none": "无线索", "at": "@你", "reply": "引用", "vocative": "称呼",
    "name": "点名", "second_person": "第二人称", "reaction": "反应词",
    "adjacent": "相邻", "question": "问答", "answer": "回答",
    "topic": "话题", "self_continue": "自我延续", "conflict": "冲突",
    "last": "接上一句", "bot": "她说的", "ambiguous": "两个都像",
    "weak": "弱信号", "recent_mention": "最近被提到",
}


def kind_cn(kind: str) -> str:
    """bot/person/group/unknown → 你/某人/大家/不确定（卡片和文字都用这个）。"""
    k = str(kind or "")
    return _KIND_CN.get(k, k or "?")


def reason_cn(reason: str) -> str:
    """second_person+adjacent → 第二人称+相邻；llm:xxx → 审核:xxx。"""
    r = str(reason or "").strip()
    if not r:
        return "-"
    if r.startswith("llm"):
        why = r.split(":", 1)[1] if ":" in r else ""
        why = why.strip()
        return "审核" + (f"：{why[:16]}…" if len(why) > 16 else (f"：{why}" if why else ""))
    parts = [p for p in r.split("+") if p]
    return "+".join(_REASON_CN.get(p, p) for p in parts) if parts else "-"


def format_debug(gid: str, limit: int = 12) -> str:
    """硬逻辑 vs 审核 LLM 的对照，给 /测函数 现场 用。"""
    rows = recent_messages(gid, limit)
    if not rows:
        return "（还没有消息）"
    lines = ["🎬 群聊现场 · 硬逻辑 vs 审核"]
    for i, m in enumerate(rows, 1):
        who = "你" if m.get("is_bot") else str(m.get("name") or m.get("qq") or "?")
        lines.append(f"{i}. {who}: {str(m.get('text') or '')[:60]}")
        if m.get("is_bot"):
            lines.append("   硬：她自己说的（不审核）")
            continue
        hk = str(m.get("hard_kind") or m.get("to_kind") or "?")
        hr = str(m.get("hard_reason") or m.get("to_reason") or "")
        hq = str(m.get("to_qq") or "")
        lines.append(f"   硬：{kind_cn(hk)}/{reason_cn(hr)}"
                     + (f" → {hq}" if hq and hk == 'person' else ""))
        if m.get("audited"):
            ak = str(m.get("to_kind") or "?")
            ar = str(m.get("to_reason") or "")
            an = str(m.get("to_name") or "")
            lines.append(f"   审：{kind_cn(ak)}/{reason_cn(ar)}" + (f" → {an}" if an else ""))
            if m.get("audit_conflict"):
                lines.append(f"   ⚠️ 冲突：{m['audit_conflict']}")
            elif m.get("audit_note"):
                lines.append(f"   · {m['audit_note']}")
        else:
            _skip = audit_skip_reason(m)
            lines.append(f"   审：不审（{_skip}）" if _skip else "   审：未审核")
    return "\n".join(lines)
