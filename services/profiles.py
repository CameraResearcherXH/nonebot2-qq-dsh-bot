# services/profiles.py
"""
每个人的三份独立存储：
  profile.json  - 身份信息（谁是谁，主人，常聊话题）
  bond.json     - 相处经验（"这个人有点闷，别逗太狠"）
  style.json    - 说话方式（"他喜欢发短句子"、"爱用表情"）
"""
# 2026-11-26：本地 Python 3.8 也要能 import（`list[str]` 在 3.8 会炸）——
# 和 services/mood.py 同一条约定，方便脱离容器跑 selftest / 数据修复。
from __future__ import annotations

from core import tuning
import json
import math
import os
import re
import time
from pathlib import Path

try:  # 允许脱离 nonebot 单独跑脚本（selftest / 数据修复）
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

PROFILE_DIR = Path("data/profiles")
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
# 档案「事情」只收大波动：|Δ| 低于它只进记忆，不进档案（否则档案 = 记忆目录的副本）
PROFILE_MIN_DELTA = tuning.f("PROFILE_NOTE_MIN_DELTA")


def _load(kind: str, qq: str) -> dict:
    f = PROFILE_DIR / f"{qq}_{kind}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(kind: str, qq: str, data: dict):
    f = PROFILE_DIR / f"{qq}_{kind}.json"
    f.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 档案 ----------
def get_profile(qq: str) -> dict:
    p = _load("profile", qq)
    p.setdefault("qq", str(qq))
    p.setdefault("aliases", [])
    p.setdefault("is_owner", False)
    p.setdefault("level", "member")
    p.setdefault("nickname", "")
    # 👇 「她怎么叫这个人」——和 nickname（群名片/昵称）分开存。
    #    因为 nickname 每轮都会被群名片覆盖写回，存不住；
    #    而 call_name 是聊出来的约定（「你叫我阿明吧」），不能被覆盖。
    p.setdefault("call_name", "")
    p.setdefault("topics", [])
    p.setdefault("last_seen_msg", "")
    p.setdefault("last_seen_at", 0)
    p.setdefault("long_term_impression", "")  # 两天一复盘提炼出的「核心印象」
    p.setdefault("impression_at", 0)

    # ---------- 人物卡 v2（2026-11-18，对齐 SillyTavern 角色卡字段） ----------
    # 为什么要加：以前只有「昵称 + 印象」，她回复时只知道这人叫什么，
    # 不知道这人是谁、在群里什么状态、平时怎么说话。ST 的角色卡把
    # description / personality / scenario / mes_example / depth_prompt 分开存，
    # 每块只在需要时注入，我们照这个结构补，但保持一个文件、按需裁剪。
    p.setdefault("identity", "")      # 简介：这个人是谁（职业/身份/背景）
    p.setdefault("personality", "")   # 性格：说话风格、脾气
    p.setdefault("scenario", "")      # 场景：在群里/私聊里通常什么状态
    p.setdefault("mes_example", [])   # 语气样例：["他：…\n你：…", …]
    p.setdefault("depth_note", "")    # 30 字内的相处备注（雷区/注意）
    p.setdefault("talkativeness", 50) # 0~100，群里存在感/话痨度
    p.setdefault("muted", False)      # True = 只记录、不接这个人的话
    p.setdefault("groups", {})        # {gid: {call_name/role/identity/note/muted}}
    return p


def upsert_profile(qq: str, *, nickname=None, aliases=None, owner=None, level=None, meta=None):
    p = get_profile(qq)
    if nickname:
        p["nickname"] = nickname
    if aliases:
        merged = set(p.get("aliases", [])) | set(aliases)
        p["aliases"] = [a for a in merged if a][:30]
    if owner is not None:
        p["is_owner"] = bool(owner)
    if level is not None:
        p["level"] = level
    if isinstance(meta, dict):
        # 复盘 Agent 回传的 long_term_impression 等走这里
        for k, v in meta.items():
            if v is not None:
                p[k] = v
    _save("profile", qq, p)
    return p


def set_call_names(qq: str, *, call_name: str = "", bot_alias: str = "") -> dict:
    """
    设置「互相怎么称呼」：
      call_name  = 她怎么叫这个人（「你叫我阿明吧」）
      bot_alias  = 这个人怎么叫她（「我可以叫你小鲸鱼吗」→ 以后喊这个名字 = @ 她）

    传空字符串表示这次不改；换 call_name 时会把**旧 call_name 从 aliases 摘掉**
    （否则「阿明 → 老大」之后阿明会一直留在别名里，见用户 2026-11-24 的反馈）。
    """
    p = get_profile(qq)
    if call_name:
        new_name = str(call_name).strip()[:12]
        old_name = str(p.get("call_name") or "").strip()
        # ⚠️ 换称呼时先把旧称呼从 aliases 里摘掉：
        #    以前只加不删，于是「阿明 → 老大」之后，阿明永远留在别名里，
        #    /档案 卡片和 group_roster 都还能把死人名翻出来（用户 2026-11-24）。
        aliases = [a for a in (p.get("aliases") or [])
                   if str(a).strip() and str(a).strip() != old_name]
        p["call_name"] = new_name
        aliases.append(new_name)
        # 去重但保留顺序
        seen = set()
        p["aliases"] = [a for a in aliases
                        if not (a in seen or seen.add(a))][:30]
    if bot_alias:
        p["bot_alias"] = str(bot_alias).strip()[:12]
    _save("profile", qq, p)
    return p


def remove_alias(qq: str, name: str) -> dict:
    """从 aliases 里精确删掉一个名字（/删称呼 QQ 阿明 用）。"""
    qq = str(qq or "").strip()
    name = str(name or "").strip()
    if not qq or not name:
        return {}
    p = get_profile(qq)
    p["aliases"] = [a for a in (p.get("aliases") or [])
                    if str(a).strip() and str(a).strip() != name]
    _save("profile", qq, p)
    return p


# ---------- 别名证据（design §1.1） ----------
# 别名不再只靠群名片：自述（「我叫阿明」）是 direct 证据；他述（「他叫阿明」）
# 只能算 reported。同一名字出现不同来源 → conflicted（保留，不删）。
_ALIAS_SRC_NAME = {"direct": "自述", "reported": "他述", "uncertain": "转述",
                   "conflicted": "来源冲突"}


def add_alias_evidence(qq: str, name: str, *, src: str = "direct",
                       msg_id: str = "", gid: str = "") -> dict:
    qq, name = str(qq or "").strip(), str(name or "").strip()[:12]
    if not qq or not name:
        return {}
    p = get_profile(qq)
    ev = p.setdefault("alias_src", {})
    cur = ev.get(name) or {}
    old = str(cur.get("src") or "")
    if src == "direct" or old == "direct":
        merged = "direct"
    elif old and old != src:
        merged = "conflicted"
    else:
        merged = src
    ev[name] = {"src": merged, "at": time.time(),
                "msg": str(msg_id or "")[:40], "gid": str(gid or "")}
    p["alias_src"] = dict(list(ev.items())[-40:])
    if name not in (p.get("aliases") or []):
        p["aliases"] = [a for a in (p.get("aliases") or []) if a][:29] + [name]
    _save("profile", qq, p)
    try:
        from . import diag
        diag.emit("alias", qq=qq, name=name, src=merged, old=old, gid=gid)
    except Exception:
        pass
    return p


_SELF_INTRO = (
    re.compile(r"(?:可以|就)?(?:叫我|喊我|管我叫)\s*([^\s，。,.!?！？、：:]{1,10})"),
    re.compile(r"^(?:其实)?我(?:的名字)?(?:叫|叫做|是)\s*([^\s，。,.!?！？、：:]{1,8})"),
)
_NAME_STOP = ("什么", "谁", "多少", "哪", "吗", "呢", "吧", "哦", "啊", "呀",
              "的", "了", "你", "我", "他", "她", "不", "没")


def learn_self_intro(qq: str, text: str, *, msg_id: str = "", gid: str = "") -> str:
    """从「我叫X / 叫我X」里学一个自述别名（direct 证据）。返回学到的名字。"""
    t = str(text or "").strip()
    if not qq or not t or len(t) > 60:
        return ""
    for rgx in _SELF_INTRO:
        m = rgx.search(t)
        if not m:
            continue
        name = m.group(1).strip("「」『』“”\"'：:")
        if not name or any(s in name for s in _NAME_STOP):
            continue
        add_alias_evidence(qq, name, src="direct", msg_id=msg_id, gid=gid)
        return name
    return ""


def clear_call_names(qq: str, *, call_name: bool = True, bot_alias: bool = True,
                     gid: str = "") -> dict:
    """删掉称呼（call_name / bot_alias），并把旧 call_name 从 aliases 里移除。

    为什么 aliases 也要删：`group_roster` 找人是按 aliases 匹配的；
    如果错误的「阿明」留在 aliases 里，下次 `[[WHO|阿明]]` 还会解析回同一个人，
    看起来就像「删了又自己长回来」。
    """
    qq = str(qq or "").strip()
    if not qq:
        return {}
    p = get_profile(qq)
    old = str(p.get("call_name") or "").strip()
    if call_name:
        p.pop("call_name", None)
    if bot_alias:
        p.pop("bot_alias", None)
    if old and call_name:
        p["aliases"] = [a for a in (p.get("aliases") or [])
                        if str(a).strip() and str(a).strip() != old]
    if gid:
        groups = p.get("groups") or {}
        g = groups.get(str(gid))
        if isinstance(g, dict):
            if call_name:
                g.pop("call_name", None)
            if bot_alias:
                g.pop("bot_alias", None)
            if not g:
                groups.pop(str(gid), None)
    _save("profile", qq, p)
    return p


def group_of(qq: str, gid: str = "") -> dict:
    """
    取「这个人在这个群里」的覆盖设置。

    用户 2026-11-18：同一个 QQ 在不同群可能是不同身份（A 群是管理、B 群是普通群友），
    称呼也可能不一样。ST 用 persona connections 解决，我们放在 profile.groups[gid]。
    """
    if not gid:
        return {}
    try:
        p = get_profile(str(qq))
        g = (p.get("groups") or {}).get(str(gid)) or {}
        return g if isinstance(g, dict) else {}
    except Exception:
        return {}


def set_group_fields(qq: str, gid: str, **kw) -> dict:
    """写「某人在某群」的覆盖字段（只收白名单，空字符串表示清除）。"""
    qq, gid = str(qq or "").strip(), str(gid or "").strip()
    if not qq or not gid:
        return {}
    p = get_profile(qq)
    groups = p.setdefault("groups", {})
    if not isinstance(groups, dict):
        groups = {}
        p["groups"] = groups
    cur = groups.get(gid)
    if not isinstance(cur, dict):
        cur = {}
    for k in ("call_name", "role", "identity", "note", "scenario"):
        if k in kw:
            v = str(kw.get(k) or "").strip()
            if v:
                cur[k] = v[:200]
            else:
                cur.pop(k, None)
    if "muted" in kw:
        cur["muted"] = bool(kw.get("muted"))
    if cur:
        groups[gid] = cur
    else:
        groups.pop(gid, None)
    _save("profile", qq, p)
    return cur


def update_fields(qq: str, **kw) -> dict:
    """
    给 LLM 用的档案更新入口：只允许写「稳定的人物信息」，不接受随便覆盖称呼。

    白名单是故意的：她可以在聊天里学到「他是后端、胃不好」，
    但不能靠这个接口改 is_owner / call_name（那两个有专门命令和协议）。
    """
    qq = str(qq or "").strip()
    if not qq:
        return {}
    p = get_profile(qq)
    for k in ("identity", "personality", "scenario", "depth_note"):
        if k in kw:
            p[k] = str(kw.get(k) or "").strip()[:400]
    if "talkativeness" in kw:
        try:
            p["talkativeness"] = max(0, min(100, int(float(kw.get("talkativeness")))))
        except Exception:
            pass
    if "mes_example" in kw:
        raw = kw.get("mes_example")
        items = raw if isinstance(raw, list) else [raw]
        cleaned = [str(x or "").strip()[:200] for x in items if str(x or "").strip()]
        if cleaned:
            # 最多留 3 组，新的追加在后面，重复的去掉
            merged = [x for x in (p.get("mes_example") or []) if x not in cleaned]
            p["mes_example"] = (merged + cleaned)[-3:]
    _save("profile", qq, p)
    return p


def call_of(qq: str, fallback: str = "", gid: str = "") -> str:
    """
    「她该怎么称呼这个人」：本群约定的 call_name > 全局 call_name >
    群名片/昵称 > 调用方兜底。

    为什么要单独抽出来（用户 2026-11-18）：
      上下文里用群名片标「谁说的」解决了人称混淆，但她回复时也跟着叫群名片；
      用户要的是「优先叫称呼，没有称呼才叫名字」。所有拼 prompt 的地方
      都走这一个函数，规则就不会一处改一处漏。
    """
    qq = str(qq or "").strip()
    if qq:
        # 称呼索引是唯一权威（2026-11-24）：先问 callname，再退回档案字段。
        # 这样 history_lines / 审核 / 工具 / 档案卡不会各读一份、各叫一个名。
        try:
            from . import callname
            v = callname.user_call_of(qq, gid, fallback="")
            if v:
                return v
        except Exception:
            pass
        try:
            g = group_of(qq, gid)
            name = (str(g.get("call_name") or "").strip()
                    or str(get_profile(qq).get("call_name") or "").strip()
                    or str(get_profile(qq).get("nickname") or "").strip())
            if name:
                return name
        except Exception:
            pass
    return str(fallback or "").strip()


def set_long_term_impression(qq: str, impression: str):
    """复盘 Agent 提炼的「对主人的核心印象」写回档案。"""
    text = str(impression or "").strip()
    if not text:
        return get_profile(qq)
    return upsert_profile(qq, meta={
        "long_term_impression": text[:200],
        "impression_at": time.time(),
    })


def touch_profile(qq: str, msg: str):
    p = get_profile(qq)
    if msg:
        p["last_seen_msg"] = msg[:120]
        p["last_seen_at"] = time.time()
    _save("profile", qq, p)


# ---------- 相处经验 ----------
BOND_MAX = 20

# 情绪 → 高亮标记（愤怒/开心的刻痕要显眼）
EMOTION_MARK = {
    "happy": "💛开心",
    "excited": "💛兴奋",
    "love": "💗喜欢",
    "touched": "🌸感动",
    "sad": "💧难过",
    "lonely": "💧寂寞",
    "angry": "💢生气",
    "annoyed": "💢烦躁",
    "neutral": "",
}
# 这两类算「刻痕」，要单独拎出来高亮
SCAR_EMOTIONS = ("angry", "annoyed", "happy", "excited", "love", "touched")


def _norm_note(raw) -> dict:
    """把老格式 list[str] / 新格式 dict 统一成 dict。"""
    if isinstance(raw, dict):
        out = {
            "text": str(raw.get("text") or raw.get("note") or "").strip(),
            "emotion": str(raw.get("emotion") or "").strip(),
            "at": float(raw.get("at") or 0),
            "weight": float(raw.get("weight") or 1.0),
        }
        if raw.get("mind"):
            out["mind"] = str(raw.get("mind") or "").strip()[:100]
        if raw.get("delta") is not None:
            try:
                out["delta"] = round(float(raw.get("delta")), 4)
            except Exception:
                pass
        if raw.get("mood_word"):
            out["mood_word"] = str(raw.get("mood_word") or "")[:20]
        return out
    return {"text": str(raw or "").strip(), "emotion": "", "at": 0.0, "weight": 1.0}


BOND_TTL_DAYS = tuning.f("BOND_TTL_DAYS")
# 同一个人每天好感净变化上限（防「持续跌」一天到底 / 「持续涨」一天涨满）
AFFINITY_DAILY_CAP = tuning.f("AFFINITY_DAILY_CAP")


def _prune_notes(notes: list) -> list:
    """
    淘汰：超过 BOND_TTL_DAYS 天没被重新强化的「一次性印象」自动丢掉。
    被反复提起的（weight >= 1.5）会留下 —— 那才是真正的刻痕。
    """
    now = time.time()
    keep = []
    for n in notes:
        age_days = (now - (n.get("at") or now)) / 86400.0
        if age_days > BOND_TTL_DAYS and float(n.get("weight") or 1.0) < 1.5:
            continue
        keep.append(n)
    return keep


def set_bond_word(qq: str, word: str) -> str:
    """存「她现在怎么看这个人」的描述性名词（由判读模型参考档案给出）。

    例：又爱又嫌的老熟人 / 不想搭理的陌生人 / 可以损两句的熟人。
    和 affinity（数值）分工：数值给趋势，名词给语气。
    """
    qq = str(qq or "").strip()
    w = str(word or "").strip()[:16]
    if not qq.isdigit() or not w:
        return ""
    p = get_profile(qq)
    if p.get("bond_word") == w:
        return w
    p["bond_word"] = w
    p["bond_word_at"] = time.time()
    _save("profile", qq, p)
    return w


def get_bond_word(qq: str) -> str:
    try:
        return str(get_profile(str(qq or "").strip()).get("bond_word") or "")
    except Exception:
        return ""


def get_bond(qq: str) -> dict:
    b = _load("bond", qq)
    raw_notes = b.get("notes") or []
    notes = [n for n in (_norm_note(x) for x in raw_notes) if n["text"]]
    before = len(notes)
    notes = _prune_notes(notes)
    # 兼容升级：老 bond.json 是 list[str]，读到时自动迁移成 list[dict] 并落盘
    if notes != raw_notes:
        b["notes"] = notes
        b["_schema"] = 2
        try:
            _save("bond", qq, b)
        except Exception:
            pass
        if len(notes) < before:
            logger.info(f"[BOND] {qq} 淘汰 {before - len(notes)} 条过期印象")
    b.setdefault("notes", [])
    return b


def add_bond_note(qq: str, note, *, emotion: str = "", weight: float = 1.0,
                  mood_word: str = "", mind: str = "", delta=None):
    """
    升级到 list[dict]，带 emotion 字段。
    - note 可以是 str（老调用方式，完全兼容）
    - 也可以是 dict（DeepSeek 的 MEMO 直接塞进来）
    - mood_word：第三方判读给的心情词（2026-11-20）。emotion 是她自报的粗分类，
      mood_word 才是「她当时具体什么感觉」，档案卡上可以对照着看。
    - mind：判读模型推断的「她当时的心里话」；delta：这件事的涨幅。
      这两个一起支撑档案里的「你与他印象最深刻的一件事」。
    """
    if isinstance(note, dict):
        emotion = emotion or str(note.get("emotion") or "")
        weight = float(note.get("weight") or weight or 1.0)
        text = str(note.get("text") or "").strip()
        mood_word = mood_word or str(note.get("mood_word") or "")
        mind = mind or str(note.get("mind") or "")
        if delta is None and note.get("delta") is not None:
            delta = note.get("delta")
    else:
        text = str(note or "").strip()
    if not text:
        return None

    # 2026-11-25：档案只收「心情明显动过」的事。带 Δ 的调用一律先过门槛，
    # 低于门槛只进记忆（记忆每 turn 都记，档案是稀疏的「印象」）。
    if delta is not None:
        try:
            if abs(float(delta)) < PROFILE_MIN_DELTA:
                logger.debug(f"[BOND] {qq} |Δ|={abs(float(delta)):.2f} < "
                             f"{PROFILE_MIN_DELTA} → 只进记忆，不进档案")
                return None
        except Exception:
            pass

    if emotion:
        from .aura_logic import normalize_emotion  # 局部导入，避免循环依赖
        emotion = normalize_emotion(emotion)

    b = get_bond(qq)
    notes = b["notes"]
    for n in notes:
        if n["text"] == text:
            # 同一条印象重复出现 → 强化权重、更新时间，不重复写
            n["weight"] = max(float(n.get("weight") or 1.0), float(weight)) + 0.5
            n["at"] = time.time()
            if emotion:
                n["emotion"] = emotion
            if mood_word:
                n["mood_word"] = str(mood_word)[:20]
            if mind:
                n["mind"] = str(mind)[:100]
            if delta is not None:
                try:
                    n["delta"] = round(float(delta), 4)
                except Exception:
                    pass
            _save("bond", qq, b)
            return n
    rec = {"text": text, "emotion": emotion, "at": time.time(),
           "weight": float(weight)}
    if mood_word:
        rec["mood_word"] = str(mood_word)[:20]
    if mind:
        rec["mind"] = str(mind)[:100]
    if delta is not None:
        try:
            rec["delta"] = round(float(delta), 4)
        except Exception:
            pass
    notes.append(rec)
    b["notes"] = notes[-BOND_MAX:]  # 只留最近 20 条
    _save("bond", qq, b)
    return rec


def bond_texts(qq: str) -> list[str]:
    """只要文本（给 /档案、自测用）。"""
    return [n["text"] for n in get_bond(qq)["notes"]]


def get_affinity(qq: str) -> float:
    """好感度 -1.00 ~ +1.00（2026-11-20）。和 intimacy 不同：这是**显式**涨落。

    2026-11-26：`AFF_MODEL=v2` 时改为「bond + 痕迹求和」的读时计算（不落盘）。
    """
    if AFF_MODEL == "v2":
        try:
            return affinity_now(qq)
        except Exception:
            pass
    try:
        return max(-1.0, min(1.0, float(get_profile(str(qq)).get("affinity") or 0.0)))
    except Exception:
        return 0.0


# ---- 好感 v1 双吃（2026-11-26 用户拍板）----
# 旧 v1 只吃 `_delta`（0.10/0.22/0.40 名义步长）：模型说「这事很重」，好感就按满格走，
# 哪怕她的心情其实只动了 0.02 —— 涨得太快，也和「好感只吃心情涨落」对不上。
# 现在两项相加（`affinity_v1_step`）：
#   ① 事件重量项 = AFF_V1_DELTA_K × |delta| × 人设/置信/新鲜度/饱和
#   ② 实际位移项 = AFF_V1_DV_K × 三段式(dv)      ← 含 |dv|≥0.30 的尾部跳变
# ①压到 0.25 → 日常不再那么快；②让「她真的被推动了很多」才算数；
# 尾部（一见钟情 / 心碎）仍能一次跳到 AFF_V1_CAP。
AFF_V1_DELTA_K = tuning.f("AFF_V1_DELTA_K")
AFF_V1_DV_K = tuning.f("AFF_V1_DV_K")
AFF_V1_CAP = tuning.f("AFF_V1_CAP")


def affinity_v1_step(delta: float, dv: float, a: float, *,
                     gain: float = 0.10, trust_speed: float = 1.0,
                     k_eff: float = 1.0, novelty: float = 1.0) -> float:
    """v1 单次好感增量（`-AFF_V1_CAP ~ +AFF_V1_CAP`）。

    `delta` —— 事件名义步长（impact 档位查表），代表「这件事有多重」；
    `dv`    —— `mood.observe` 走出来的实际位移，代表「她真的动了多少」。
    `dv=0`（`MOOD_UPDATE=delta` 回滚、或本窗没有观测）时只剩事件项。
    """
    try:
        d = float(delta or 0.0)
    except Exception:
        d = 0.0
    try:
        m = float(dv or 0.0)
    except Exception:
        m = 0.0
    try:
        cur = max(-1.0, min(1.0, float(a)))
    except Exception:
        cur = 0.0
    sat = max(0.0, 1.0 - abs(cur))
    # ① 事件重量项：方向跟 delta；负向再乘「越喜欢越宽容」（v1 特有，v2 用 AFF_NEG_BIAS）
    w_event = (AFF_V1_DELTA_K * abs(d) * max(0.0, float(gain))
               * max(0.0, float(trust_speed)) * max(0.0, float(novelty))
               * max(0.0, min(1.0, float(k_eff))) * sat)
    if d < 0:
        try:
            from . import relationship as _rln
            w_event *= _rln.neg_weight(cur)
        except Exception:
            pass
        w_event = -w_event
    # ② 实际位移项：三段式（已含负向偏置 / 饱和 / 尾部跳变）
    w_move = AFF_V1_DV_K * _aff_w_from_dv(m, cur, novelty=novelty)
    return max(-AFF_V1_CAP, min(AFF_V1_CAP, w_event + w_move))


def bump_affinity(qq: str, delta: float, reason: str = "") -> float:
    """
    好感度涨落（单次 `±AFF_V1_CAP`，由心情判读给）。

    用户 2026-11-20：「心情是全局的，但是对谁安静对谁倾诉……要利用一下我们的
    档案好感值系统。当然那个好感值系统也没有特别详细的涨落。」

    和 intimacy 的分工：
      · affinity  —— 她对**这个人**的显式好恶（-1~1），被具体事件推动；
      · intimacy  —— 关系温度（0~1），由相处记录的**数量+情绪+时间**算出来。
    两者一起用：intimacy 决定「熟不熟」，affinity 决定「喜不喜欢」。
    """
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return 0.0
    try:
        d = max(-AFF_V1_CAP, min(AFF_V1_CAP, float(delta)))
    except Exception:
        return get_affinity(qq)
    if abs(d) < 1e-9:
        return get_affinity(qq)
    p = get_profile(qq)
    # 每日净变化上限：防「持续跌」一天跌到底 / 「持续涨」一天涨满。
    # 同方向受剩余额度限制；反方向（回头）不限制，并且会把当日净额往回带。
    _day = time.strftime("%Y-%m-%d")
    if str(p.get("affinity_day") or "") != _day:
        p["affinity_day"] = _day
        p["affinity_day_sum"] = 0.0
    _used = float(p.get("affinity_day_sum") or 0.0)
    # 2026-11-26（外部审计抓到的真 bug）：这里原来是 `_used * d > 0`，
    # 当天第一件事 `_used=0` → `0*d==0` 不成立 → **完全不受日上限约束**，
    # 可以一次写满单次上限；写完之后 `_room = 0.15-0.36 = -0.21`，当天同向全部 return。
    # 真实语义变成「当天第一句话决定全天额度」，和「每天最多 0.15」完全相反。
    # 改成 `>= 0`：同向（含 _used==0 的第一件事）都走额度；只有反向（回头）不受限。
    if _used * d >= 0:
        _room = max(0.0, AFFINITY_DAILY_CAP - abs(_used))
        if _room <= 0:
            return get_affinity(qq)
        if abs(d) > _room:
            d = _room if d > 0 else -_room
    p["affinity_day_sum"] = round(_used + d, 4)

    cur = float(p.get("affinity") or 0.0)
    new = max(-1.0, min(1.0, cur + d))
    p["affinity"] = round(new, 3)
    p["affinity_at"] = time.time()
    hist = p.get("affinity_log") or []
    hist.append({"at": time.time(), "delta": round(d, 3), "to": round(new, 3),
                 "why": str(reason or "")[:40]})
    p["affinity_log"] = hist[-50:]
    _save("profile", qq, p)
    # 档案镜像（retrieval §8）：好感变了就同步 sqlite profiles
    try:
        from . import memdb
        memdb.upsert_profile(qq=qq,
                             name=str(p.get("name") or p.get("call_name") or ""),
                             affinity=new)
        logger.info(f"[MEM] profile sync {qq} aff={new:+.2f}")
    except Exception:
        pass
    return new


def decay_affinity(qq: str, tau_days: float, now: float = 0) -> float:
    """按半衰期把 affinity 往基线 0 拉（τ_eff 由 relationship 按深度系数给）。

    和 bump_affinity 的分工：这是**时间**造成的回落，不占每日额度、不写
    affinity_log（否则一天不聊天也能把 log 刷满）；变化 <0.001 不落盘。
    """
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return 0.0
    now = float(now or time.time())
    if AFF_MODEL == "v2":
        # v2 的衰减由痕迹自己完成，不需要半衰期，也不落盘。
        return affinity_now(qq, now)
    p = get_profile(qq)
    cur = float(p.get("affinity") or 0.0)
    if abs(cur) < 1e-9:
        return 0.0
    at = float(p.get("affinity_at") or p.get("updated_at") or now)
    days = max(0.0, (now - at) / 86400.0)
    if days <= 0 or tau_days <= 0:
        return cur
    new = cur * math.pow(0.5, days / max(0.01, float(tau_days)))
    if abs(new - cur) < 0.001:
        return cur
    p["affinity"] = round(new, 3)
    p["affinity_at"] = now
    _save("profile", qq, p)
    return p["affinity"]


# ============================================================
# 好感 v2（2026-11-26）：痕迹求和 + 三段式 dv + 巩固 + 分层门槛
#   设计见 docs/affinity-quantification-design.md（v2.5）
#   `AFF_MODEL=v1`（默认）时下面这些不生效，完全走上面的旧逻辑。
#   开关方式：.env 写 AFF_MODEL=v2 后重启。
# ============================================================
AFF_MODEL = str(tuning.raw("AFF_MODEL") or "v1").strip().lower()
AFF_MAX = tuning.f("AFF_MAX")
AFF_TRACE_T = max(1.0, tuning.f("AFF_TRACE_T"))
AFF_TRACE_P = tuning.f("AFF_TRACE_P")
AFF_TRACE_MAX = max(20, tuning.i("AFF_TRACE_MAX"))
AFF_BOND_MAX = tuning.f("AFF_BOND_MAX")
AFF_BOND_EXP = tuning.f("AFF_BOND_EXP")
AFF_BOND_DEEP_MAX = tuning.f("AFF_BOND_DEEP_MAX")
AFF_BOND_DEEP_EXP = tuning.f("AFF_BOND_DEEP_EXP")
AFF_DV_K = tuning.f("AFF_DV_K")
AFF_JUMP_FROM = tuning.f("AFF_JUMP_FROM")
AFF_JUMP_K = tuning.f("AFF_JUMP_K")
AFF_JUMP_GAMMA = tuning.f("AFF_JUMP_GAMMA")
AFF_JUMP_SAT = tuning.f("AFF_JUMP_SAT")
AFF_TAIL_FROM = tuning.f("AFF_TAIL_FROM")
AFF_TAIL_BASE = tuning.f("AFF_TAIL_BASE")
AFF_TAIL_K = tuning.f("AFF_TAIL_K")
AFF_TAIL_GAMMA = tuning.f("AFF_TAIL_GAMMA")
AFF_TAIL_SAT = tuning.f("AFF_TAIL_SAT")
AFF_W_MAX = tuning.f("AFF_W_MAX")
AFF_GUARD_POS = tuning.f("AFF_GUARD_POS")
AFF_NEG_BIAS = tuning.f("AFF_NEG_BIAS")
AFF_SAT_POS = tuning.f("AFF_SAT_POS")
AFF_SAT_NEG = tuning.f("AFF_SAT_NEG")
AFF_PEAK_DV = tuning.f("AFF_PEAK_DV")
AFF_LOVE_DV = tuning.f("AFF_LOVE_DV")
AFF_WARM_FULL = max(0.01, tuning.f("AFF_WARM_FULL"))
AFF_PEAK_BONUS = tuning.f("AFF_PEAK_BONUS")
AFF_HURT_GRUDGE = tuning.f("AFF_HURT_GRUDGE")
AFF_CONS_FROM = tuning.f("AFF_CONS_FROM")
AFF_CONS_TO = max(AFF_CONS_FROM + 0.01, tuning.f("AFF_CONS_TO"))
AFF_CONS_K = tuning.f("AFF_CONS_K")
AFF_TIER_SOFT = tuning.f("AFF_TIER_SOFT")
AFF_COMP_W = tuning.f("AFF_COMP_W")
AFF_COMP_CAP = max(0.01, tuning.f("AFF_COMP_CAP"))

# 分层门槛：(门槛, 名字, 条件)；条件全部来自 dv 统计量（无名词判断）
AFF_TIERS = (
    (0.15, "眼熟", {"days": 7}),
    (0.30, "熟人", {"days": 14}),
    (0.45, "朋友", {"days": 60, "notes": 10}),
    (0.60, "很好", {"days": 120, "notes": 20}),
    (0.75, "暧昧", {"days": 180, "peaks": 3}),
    (0.85, "爱", {"days": 240, "peaks": 5}),
    (0.95, "唯一的爱", {"days": 365, "peaks": 8, "repairs": 1}),
)


def _aff_clamp(v: float) -> float:
    return max(-1.0, min(AFF_MAX, float(v)))


def _aff_traces(p: dict) -> list:
    out = []
    for x in (p.get("aff_traces") or []):
        try:
            out.append((float(x[0]), float(x[1])))
        except Exception:
            continue
    return out


def _aff_value(p: dict, now: float) -> float:
    """a = bond + Σ w·(1+Δt/T)^-p。"""
    s = 0.0
    for t, w in _aff_traces(p):
        dt = max(0.0, now - t) / 86400.0
        s += w * (1.0 + dt / AFF_TRACE_T) ** (-AFF_TRACE_P)
    return _aff_clamp(float(p.get("aff_bond") or 0.0) + s)


def _aff_investment(qq: str, p: dict, now: float) -> tuple:
    """浅层投入 I / 认识天数 / 记录条数。"""
    notes = (get_bond(qq).get("notes") or [])
    n = len(notes)
    cands = [float(x.get("at") or 0) for x in notes]
    for x in (p.get("affinity_log") or []):
        try:
            cands.append(float(x.get("at") or 0))
        except Exception:
            continue
    for k in ("affinity_at", "first_at", "updated_at"):
        try:
            cands.append(float(p.get(k) or 0))
        except Exception:
            continue
    ts = [x for x in cands if x > 0]
    first = min(ts) if ts else now
    days = max(0.0, (now - first) / 86400.0)
    log = [x for x in (p.get("affinity_log") or []) if isinstance(x, dict)]
    n7 = sum(1 for x in log if now - float(x.get("at") or 0) <= 7 * 86400)
    inv = (0.50 * min(1.0, n / 20.0)
           + 0.30 * min(1.0, days / 120.0)
           + 0.20 * min(1.0, n7 / 150.0))
    return inv, days, n


def _aff_deep(p: dict) -> float:
    peaks = int(p.get("aff_peaks") or 0)
    repairs = int(p.get("aff_repairs") or 0)
    warm = int(p.get("aff_warm_days") or 0)
    return (0.50 * min(1.0, peaks / 10.0)
            + 0.30 * min(1.0, repairs / 3.0)
            + 0.20 * min(1.0, warm / 60.0))


def _aff_bond_max(inv: float, inv_deep: float) -> float:
    return (AFF_BOND_MAX * (max(0.0, inv) ** AFF_BOND_EXP)
            + AFF_BOND_DEEP_MAX * (max(0.0, inv_deep) ** AFF_BOND_DEEP_EXP))


def _aff_warmth7(p: dict, now: float) -> float:
    s = 0.0
    for x in (p.get("aff_dv_log") or []):
        try:
            t, dv = float(x[0]), float(x[1])
        except Exception:
            continue
        if dv > 0 and now - t <= 7 * 86400:
            s += dv
    return s


def _aff_w_from_dv(dv: float, a: float, *, novelty: float = 1.0) -> float:
    """三段式：日常线性 / 显著凸性 / 尾部跳变（见文档 §2.4）。

    **不再乘 confidence**：`dv` 是 `mood.observe` 走出来的实际位移，
    里面已经含了 K（K 含 confidence）。再乘一遍就是同一个因素算两次
    （2026-11-26 审计自己踩到的坑）。
    """
    x = abs(float(dv))
    if x < 1e-9:
        return 0.0
    if x >= AFF_TAIL_FROM:
        gain = AFF_TAIL_BASE + AFF_TAIL_K * (x ** AFF_TAIL_GAMMA)
        sat = AFF_TAIL_SAT
    elif x >= AFF_JUMP_FROM:
        gain = AFF_JUMP_K * (x ** AFF_JUMP_GAMMA)
        sat = AFF_JUMP_SAT
    else:
        gain = AFF_DV_K * x
        sat = (AFF_SAT_NEG if (dv > 0 and a < 0)
               else (AFF_SAT_POS if dv > 0 else AFF_SAT_NEG))
    gain *= max(0.0, float(novelty))
    if dv < 0:
        w = -gain * AFF_NEG_BIAS * (1.0 - abs(a)) ** sat
    else:
        if a < 0 and x >= AFF_JUMP_FROM:
            gain *= AFF_GUARD_POS
        w = gain * (1.0 - abs(a)) ** sat
    return max(-AFF_W_MAX, min(AFF_W_MAX, w))


def _aff_kappa(a: float, warmth7: float) -> float:
    if a <= AFF_CONS_FROM:
        return 0.0
    ka = AFF_CONS_K * max(0.0, min(1.0, (a - AFF_CONS_FROM)
                                 / (AFF_CONS_TO - AFF_CONS_FROM)))
    kw = max(0.0, min(1.0, warmth7 / AFF_WARM_FULL))
    return ka * kw


def _aff_add_trace(traces: list, now: float, w: float) -> list:
    """按天聚合；超上限按权重裁最弱（不能按最近 N 条，长尾会被砍）。"""
    if abs(w) < 1e-9:
        return traces
    day = int(now // 86400)
    if traces and int(traces[-1][0] // 86400) == day:
        t0, w0 = traces[-1]
        traces[-1] = (t0, w0 + w)
    else:
        traces.append((now, w))
    if len(traces) > AFF_TRACE_MAX:
        traces.sort(key=lambda x: -abs(x[1]))
        traces = sorted(traces[:AFF_TRACE_MAX], key=lambda x: x[0])
    return traces


def _aff_tier_cap(days: float, notes: float, peaks: float,
                  repairs: float) -> float:
    have = {"days": days, "notes": notes, "peaks": peaks, "repairs": repairs}
    prev = 0.10
    for th, _name, need in AFF_TIERS:
        for k, v in need.items():
            if have.get(k, 0) < v:
                return prev + AFF_TIER_SOFT
        prev = th
    return AFF_MAX


def _aff_ensure(p: dict, now: float) -> dict:
    """旧档案迁移：没有 aff_traces 时，用当前 affinity 初始化一条痕迹。

    之后只前进、不回算 —— 用户看不到跳变（设计文档 §5.1）。
    """
    if "aff_traces" in p:
        return p
    cur = float(p.get("affinity") or 0.0)
    if abs(cur) < 1e-9:
        return p
    # 2026-11-26 修：痕迹时间必须用 **now**，不能用 affinity_at。
    # 用 affinity_at（上次事件时间）会让旧档案一迁移就按「已经过了 30 天」衰减：
    # 0.90 → 0.45、0.60 → 0.19，直接腰斩，违反「迁移不跳变」（文档 §5.1）。
    p["aff_traces"] = [[now, round(cur, 5)]]
    p["aff_bond"] = 0.0
    # 2026-11-26：旧档案没有 peaks 记录，按当前好感**反推**（0.9 → 9 次心动）。
    # 否则门槛按 peaks=0 算，老用户会被卡在 0.32，涨不上去（好感「冻住」）。
    p["aff_peaks"] = min(10, max(0, int(round(abs(cur) * 10))))
    p["aff_hurts"] = max(0, int(round(max(0.0, -cur) * 10)))
    p.setdefault("aff_repairs", 0)
    p.setdefault("aff_warm_days",
                 min(60, max(0, int(round(abs(cur) * 60)))))
    try:
        _save("profile", str(p.get("qq") or ""), p)
    except Exception:
        pass
    return p


def affinity_now(qq: str, now: float = 0) -> float:
    """v2 读：bond + 痕迹衰减（不落盘，迁移除外）。"""
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return 0.0
    now = float(now or time.time())
    return _aff_value(_aff_ensure(get_profile(qq), now), now)


def add_affinity_event(qq: str, dv: float, reason: str = "", *,
                       conf: float = 1.0, novelty: float = 1.0) -> float:
    """v2 写：吃一条「归因到这个人的心情位移 dv」。

    dv 是 mood.observe 的实际位移（-0.35~+0.35），不是 impact 档位。
    `conf` 参数保留只为调用方兼容，**不参与计算**（dv 已含 K/置信）。
    """
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return 0.0
    try:
        dv = float(dv)
    except Exception:
        return get_affinity(qq)
    if abs(dv) < 1e-9:
        return get_affinity(qq)
    now = time.time()
    p = _aff_ensure(get_profile(qq), now)
    a_old = _aff_value(p, now)
    w = _aff_w_from_dv(dv, a_old, novelty=novelty)
    if abs(w) < 1e-9:
        return a_old
    # ---- 巩固：只对正向事件；高区 + 最近暖 → 一部分沉淀进 bond ----
    # 负向全部留在 traces（会衰减），不能沉淀成底子——否则「伤害」变成永久记忆。
    k = 0.0
    if dv > 0:
        k = _aff_kappa(a_old, _aff_warmth7(p, now))
        if abs(dv) >= AFF_PEAK_DV:
            k = min(0.85, k + AFF_PEAK_BONUS)
        if dv >= AFF_LOVE_DV:
            k = max(k, 0.5)
    traces = _aff_add_trace(_aff_traces(p), now, w * (1.0 - k))
    # ---- 统计量（全部来自 dv，无名词）----
    peaks = int(p.get("aff_peaks") or 0)
    hurts = int(p.get("aff_hurts") or 0)
    repairs = int(p.get("aff_repairs") or 0)
    warm_days = int(p.get("aff_warm_days") or 0)
    if abs(dv) >= AFF_PEAK_DV:
        peaks += 1
        if dv >= AFF_LOVE_DV:
            peaks += 1
        if dv < 0:
            hurts += 1
            p["aff_hurt_at"] = now
            p["aff_grudge"] = round(min(1.0, float(p.get("aff_grudge") or 0.0)
                                       + AFF_HURT_GRUDGE), 4)
        p["aff_peak_last"] = now
    if dv > 0:
        if int(p.get("aff_warm_day") or 0) != int(now // 86400):
            warm_days += 1
            p["aff_warm_day"] = int(now // 86400)
        hurt_at = float(p.get("aff_hurt_at") or 0.0)
        if hurt_at > 0 and now - hurt_at <= 7 * 86400:
            repairs += 1
            p["aff_hurt_at"] = 0.0
    inv, days, n = _aff_investment(qq, p, now)
    bm = _aff_bond_max(inv, _aff_deep({
        "aff_peaks": peaks, "aff_repairs": repairs,
        "aff_warm_days": warm_days}))
    old_bond = float(p.get("aff_bond") or 0.0)
    bond = old_bond + w * k
    if bond > bm:
        # bond 只增不减：上限变低（比如投入统计口径变了）也不能把存量压掉
        bond = max(old_bond, bm)
    # ---- 门槛：只封上涨、不回溯 ----
    a_new = _aff_clamp(bond + sum(
        wv * (1.0 + max(0.0, now - t) / 86400.0 / AFF_TRACE_T) ** (-AFF_TRACE_P)
        for t, wv in traces))
    cap = _aff_tier_cap(days, n, peaks, repairs)
    if a_new > cap and a_new > a_old and traces:
        excess = a_new - max(a_old, cap)
        t0, w0 = traces[-1]
        traces[-1] = (t0, w0 - excess)
        a_new = _aff_clamp(a_new - excess)
    # ---- 落盘 ----
    dvl = [x for x in (p.get("aff_dv_log") or [])
           if isinstance(x, list) and now - float(x[0] or 0) <= 14 * 86400]
    dvl.append([now, round(dv, 4)])
    p["aff_dv_log"] = dvl[-400:]
    p["aff_traces"] = [[round(t, 1), round(wv, 5)] for t, wv in traces]
    p["aff_bond"] = round(max(0.0, bond), 4)
    p["aff_peaks"] = peaks
    p["aff_hurts"] = hurts
    p["aff_repairs"] = repairs
    p["aff_warm_days"] = warm_days
    p["affinity"] = round(a_new, 3)
    p["affinity_at"] = now
    hist = p.get("affinity_log") or []
    hist.append({"at": now, "delta": round(w, 4), "to": round(a_new, 3),
                 "why": str(reason or "")[:40], "dv": round(dv, 4)})
    p["affinity_log"] = hist[-50:]
    _save("profile", qq, p)
    try:
        from . import memdb
        memdb.upsert_profile(qq=qq,
                             name=str(p.get("name") or p.get("call_name") or ""),
                             affinity=a_new)
    except Exception:
        pass
    logger.info(f"[AFFv2] {qq} dv={dv:+.3f} w={w:+.3f} κ={k:.2f} "
                f"bond={bond:.2f} a={a_new:+.3f} peaks={peaks} repairs={repairs}")
    return a_new


def add_affinity_comp(qq: str, reason: str = "陪伴") -> float:
    """v2：中性陪伴（这一轮没走出 dv）的小额痕迹，天花板 AFF_COMP_CAP。

    和 add_affinity_event 的区别：没有心情涨落，只是「在场」。
    由调用方控制频次（每人每天 ≤ 6 次），这里只负责记一条。
    """
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return 0.0
    now = time.time()
    p = _aff_ensure(get_profile(qq), now)
    a_old = _aff_value(p, now)
    room = max(0.0, (AFF_COMP_CAP - max(0.0, a_old)) / AFF_COMP_CAP)
    w = AFF_COMP_W * (1.0 - abs(a_old)) ** 0.5 * room
    if w < 1e-9:
        return a_old
    traces = _aff_add_trace(_aff_traces(p), now, w)
    inv, days, n = _aff_investment(qq, p, now)
    bond = min(float(p.get("aff_bond") or 0.0), _aff_bond_max(inv, _aff_deep(p)))
    a_new = _aff_clamp(bond + sum(
        wv * (1.0 + max(0.0, now - t) / 86400.0 / AFF_TRACE_T) ** (-AFF_TRACE_P)
        for t, wv in traces))
    # 2026-11-26 修：陪伴通道也要过门槛，否则新号靠每天闲聊就能刷到 0.8，
    # 分层门槛对「只陪伴」形同虚设（事件通道本来就有这道 cap）。
    cap = _aff_tier_cap(days, n, int(p.get("aff_peaks") or 0),
                        int(p.get("aff_repairs") or 0))
    if a_new > cap and a_new > a_old and traces:
        excess = a_new - max(a_old, cap)
        t0, w0 = traces[-1]
        traces[-1] = (t0, w0 - excess)
        a_new = _aff_clamp(a_new - excess)
    p["aff_traces"] = [[round(t, 1), round(wv, 5)] for t, wv in traces]
    p["affinity"] = round(a_new, 3)
    p["affinity_at"] = now
    hist = p.get("affinity_log") or []
    hist.append({"at": now, "delta": round(w, 4), "to": round(a_new, 3),
                 "why": str(reason or "")[:40], "dv": 0.0})
    p["affinity_log"] = hist[-50:]
    _save("profile", qq, p)
    return a_new


# 好感「走势」窗口（秒）：默认 30 分钟。
# 安静建议看的是「刚刚发生了什么」，不是三天前的总账 —— 所以单独算一个窗口。
AFFINITY_TREND_SEC = tuning.f("AFFINITY_TREND_WINDOW_SEC")


def affinity_trend(qq: str, window_sec: float = 0) -> dict:
    """
    最近一段时间的好感走势（给「安静建议」用，也方便 /测函数 安静 对照）。

    为什么要有走势：用户 2026-11-20 ——
      「并不是好感低和心情不好就单独对人偏见不理，而是要多方面看：
        忽然连续降低的好感度/心情，或者大幅度降低。好感本身高可以有忍耐，
        但跌到一定程度 + 忽然连续/大幅降低同时发生，就可以选择安静了。」

    返回：
      cur    现在的好感（-1~1）
      base   窗口开始前的好感（cur - 窗口内所有 delta）
      drop   窗口内累计下降（正数）
      spike  窗口内单次最大跌幅（正数）
      neg_n  窗口内下降次数
      up     窗口内累计上升（正数）
      n      窗口内变动次数
      why    最近一次变动的原因
    """
    qq = str(qq or "").strip()
    now = time.time()
    win = float(window_sec or AFFINITY_TREND_SEC)
    out = {"cur": 0.0, "base": 0.0, "drop": 0.0, "spike": 0.0,
           "neg_n": 0, "up": 0.0, "n": 0, "why": "", "window": win}
    if not qq.isdigit():
        return out
    try:
        p = get_profile(qq)
        cur = float(p.get("affinity") or 0.0)
        log = [x for x in (p.get("affinity_log") or []) if isinstance(x, dict)]
        recent = [x for x in log if now - float(x.get("at") or 0) <= win]
        deltas = [float(x.get("delta") or 0.0) for x in recent]
        neg = [d for d in deltas if d < 0]
        out.update({
            "cur": round(cur, 3),
            "base": round(cur - sum(deltas), 3),
            "drop": round(-sum(neg), 3),
            "spike": round(max(0.0, -min(deltas)), 3) if deltas else 0.0,
            "neg_n": len(neg),
            "up": round(sum(d for d in deltas if d > 0), 3),
            "n": len(deltas),
            "why": str((recent[-1] if recent else {}).get("why") or ""),
        })
    except Exception:
        pass
    return out


# 亲近半衰期（天）：DESIGN §3.2 的 30 天。以前写死在 intimacy() 里，
# 调参表里的 REL_INTIMACY_HALF_LIFE_DAYS 读了却没人用（2026-11-25 接线）。
INTIMACY_HL_D = tuning.f("REL_INTIMACY_HALF_LIFE_DAYS")


def intimacy_from_score(score: float) -> float:
    """相处量 → 亲近 0~1（线性 + 软上限 0.98）。

    2026-11-26：11-25 试过的饱和曲线 score/(score+3) 会让 15 个群友的亲近
    从 1.00 一夜掉到 0.50（用户反馈「好感掉了 0.5」），已回退。
    现在只把「恰好 1.00」压到 0.98，避免 τ_aff 被顶满；其余和旧口径一致。
    """
    try:
        s = float(score or 0.0)
    except Exception:
        s = 0.0
    return min(0.98, s / 3.0) if s > 0 else 0.0


def intimacy(qq: str) -> float:
    """
    关系温度 0~1：由相处记录条数、情绪权重、时间衰减算出来。

    为什么不只看条数：天天吵架也会攒记录，但那是负关系。
    所以情绪权重参与：love/touched 加分多，普通记录加分少；
    30 天没互动自然降温（半衰期 30 天）。

    👇 2026-11-20：显式好感度（affinity）也参与 —— 她被人哄了/被恶心到，
       会立刻影响「亲不亲近」，不用等攒够相处记录。
    """
    try:
        notes = get_bond(str(qq)).get("notes") or []
    except Exception:
        return 0.0
    now = time.time()
    score = 0.0
    for n in notes:
        try:
            age = max(0.0, (now - float(n.get("at") or 0)) / 86400.0)
            recency = 0.5 ** (age / max(1.0, float(INTIMACY_HL_D or 30.0)))
            w = min(3.0, float(n.get("weight") or 1.0))
            emo = str(n.get("emotion") or "")
            bonus = (0.35 if emo in ("love", "touched")
                     else 0.18 if emo in ("happy", "excited")
                     else 0.05)
            score += (0.10 * w + bonus) * recency
        except Exception:
            continue
    # 2026-11-26：改回线性 + 软上限 0.98（公式与理由见 intimacy_from_score）。
    base = intimacy_from_score(score)
    # 显式好感度最多贡献 ±0.25：吵一架会掉一截，但不会把老朋友直接清零
    try:
        base += get_affinity(qq) * 0.25
    except Exception:
        pass
    return max(0.0, min(1.0, base))


def intimacy_label(v: float) -> str:
    if v >= 0.7:
        return "很亲近"
    if v >= 0.45:
        return "熟人"
    if v >= 0.2:
        return "认识"
    return "不熟"


# ---------- 说话方式 ----------
def get_style(qq: str) -> dict:
    s = _load("style", qq)
    s.setdefault("avg_len", 0)
    s.setdefault("uses_emoji", 0)  # 0/1
    s.setdefault("tone", "")       # casual / formal / ...
    return s


def update_style(qq: str, *, msg_len: int, has_emoji: bool):
    s = get_style(qq)
    n = s.get("_n", 0) + 1
    s["avg_len"] = (s["avg_len"] * (n - 1) + msg_len) / n
    if has_emoji:
        s["uses_emoji"] = 1
    s["_n"] = n
    _save("style", qq, s)


def style_summary(qq: str) -> str:
    s = get_style(qq)
    if s.get("_n", 0) < 3:
        return ""
    parts = []
    if s["avg_len"] < 8:
        parts.append("说话很简短")
    elif s["avg_len"] > 40:
        parts.append("喜欢长篇大论")
    if s["uses_emoji"]:
        parts.append("爱用表情")
    return "、".join(parts)


# ---------- 人物卡渲染（2026-11-18，对齐 SillyTavern 角色卡） ----------
# 三个粒度，按 token 预算选：
#   micro  一行，<= 60 字：上下文标签 / WHO 查人
#   brief  <= 180 字：被 @ 时注入到当前消息上方（depth）
#   full   <= 420 字：/档案、[[PROFILE]]、她主动查某人
#
# 模板固定成「人 / 简介 / 事情 / 关系 / 语气 / 注意」六段，
# 不按字段散着写 —— 用户 2026-11-18：模板一定要整理好，别分散 API 注意力。

_CARD_LABELS = ("人", "简介", "事情", "关系", "语气", "注意")


def _clip_text(text: str, limit: int) -> str:
    t = str(text or "").strip()
    if limit <= 0 or len(t) <= limit:
        return t
    return t[: max(0, limit - 1)].rstrip() + "…"


def _card_head(qq: str, gid: str = "") -> str:
    p = get_profile(qq)
    g = group_of(qq, gid)
    try:
        from . import callname
        name = (callname.user_call_of(qq, gid, fallback="")
                or call_of(qq, "", gid) or str(qq))
    except Exception:
        name = call_of(qq, "", gid) or str(qq)
    nick = str(p.get("nickname") or "").strip()
    tags = []
    if p.get("is_owner"):
        tags.append("主人")
    role = str(g.get("role") or "").strip()
    if role and role not in ("member", "owner"):
        tags.append(role)
    if g.get("muted") or p.get("muted"):
        tags.append("静音")
    head = f"{name}（QQ {qq}"
    if nick and nick != name:
        head += f"｜群名片 {nick}"
    if tags:
        head += "｜" + "·".join(tags)
    return head + "）"


def _card_notes(qq: str, limit: int = 3, *, ranked: bool = False) -> list[str]:
    """
    取相处记录。
      ranked=True  → 按「权重 + 时间」挑最值得提的（brief 卡只放一条时用）；
      ranked=False → 取最近 N 条（full 卡按时间线看）。
    """
    try:
        notes = get_bond(qq).get("notes") or []
    except Exception:
        return []
    if ranked:
        notes = sorted(
            notes,
            key=lambda n: (float(n.get("weight") or 1.0), float(n.get("at") or 0)),
            reverse=True,
        )[:limit]
    else:
        notes = notes[-limit:]
    out = []
    for n in notes:
        mark = EMOTION_MARK.get(str(n.get("emotion") or ""), "")
        txt = str(n.get("text") or "").strip()
        if txt:
            out.append(f"{mark} {txt}".strip())
    return out


_POS_EMO = {"happy", "excited", "love", "touched", "proud", "grateful"}
_NEG_EMO = {"angry", "annoyed", "sad", "disgust", "hurt", "disappointed"}


def relation_block(qq: str, gid: str = "", max_len: int = 320) -> str:
    """「你对他/她的底色」——模板生成，零额外 LLM 调用。

    和 mood（此刻心情）平级：底色决定愿不愿意理他，此刻决定这一句怎么说。
    只对当前说话人 / 本条 @ 的人注入，不给全群。
    """
    qq = str(qq or "").strip()
    if not qq:
        return ""
    try:
        aff = float(get_affinity(qq))
        iv = float(intimacy(qq))
        tr = affinity_trend(qq, 7 * 86400)
    except Exception:
        return ""
    notes = []
    try:
        notes = get_bond(qq).get("notes") or []
    except Exception:
        notes = []

    # 印象最深的一件事（用户 2026-11-20）：优先记忆库里 |Δ| 最大的那件事，
    # 连判读模型当时的心理活动一起给；记忆里没有才退回档案里权重最高的刻痕。
    _deep = {}
    try:
        from . import memory_v2 as _mv
        _deep = _mv.deepest_event(
            qq, min_abs=tuning.f("BOND_EVENT_MIN_DELTA")) or {}
    except Exception:
        _deep = {}
    if not str(_deep.get("text") or "").strip():
        _cand = sorted(
            notes,
            key=lambda n: (abs(float(n.get("delta") or 0)),
                           float(n.get("weight") or 1.0),
                           float(n.get("at") or 0)),
            reverse=True)
        if _cand:
            _n = _cand[0]
            _deep = {"text": str(_n.get("text") or ""),
                     "mind": str(_n.get("mind") or "")}

    if abs(aff) < 0.05 and not notes and not str(_deep.get("text") or "").strip():
        return ""

    def _score(n):
        return float(n.get("weight") or 1.0) * (1.0 + float(n.get("at") or 0) / 1e10)

    pos = sorted([n for n in notes if str(n.get("emotion") or "") in _POS_EMO],
                 key=_score, reverse=True)[:2]
    neg = sorted([n for n in notes if str(n.get("emotion") or "") in _NEG_EMO],
                 key=_score, reverse=True)[:1]
    try:
        label = intimacy_label(iv)
    except Exception:
        label = "刚认识"
    try:
        _d = float(tr.get("cur", aff)) - float(tr.get("base", aff))
    except Exception:
        _d = 0.0
    move = "在回暖" if _d > 0.03 else ("在掉" if _d < -0.03 else "平稳")
    try:
        name = call_of(qq, "他", gid) or "他"
    except Exception:
        name = "他"

    if aff >= 0.5:
        tip = "放松、愿意多接两句；被戳到会先怼回去，但不会真翻脸"
    elif aff >= 0.15:
        tip = "熟人，可以损两句，也会主动接他的话"
    elif aff > -0.15:
        tip = "客气但不热络，他找你你会回，不会主动凑"
    elif aff > -0.5:
        tip = "有点防着他，回得短，能不接就不接"
    else:
        tip = "很冷，只想躲开；他说话你尽量不接"

    lines = [
        "▍你对%s的底色（长期，不是此刻心情）" % name,
    ]
    try:
        _bw = get_bond_word(qq)
    except Exception:
        _bw = ""
    if _bw:
        lines.append("· 你眼里%s：%s" % (name, _bw))

    if str(_deep.get("text") or "").strip():
        _dt = re.sub(r"^（群号:\d+）", "",
                     str(_deep.get("text") or "").strip())[:40]
        _dm = str(_deep.get("mind") or "").strip()[:50]
        lines.append("· 你与%s印象最深刻的一件事：%s%s" % (
            name, _dt, ("（当时你心里：%s）" % _dm) if _dm else ""))

    lines.append("· 好感 %+.2f｜亲近 %.2f（%s）｜最近%s" % (aff, iv, label, move))
    if pos:
        lines.append("· 想起%s，先想到：%s" % (
            name, "  ".join(str(n.get("text") or "")[:28] for n in pos)))
    if neg:
        lines.append("· 也记着：%s" % str(neg[0].get("text") or "")[:28])
    lines.append("· 跟他说话时：%s" % tip)
    # 🌙 白月光：每个人的档案里都标一句「你在这个群最吃谁的说话方式」。
    try:
        from . import white_moon
        _wm_note = white_moon.profile_note(qq, gid)
        if _wm_note:
            lines.append(_wm_note)
    except Exception:
        pass
    lines.append("（底色决定你愿不愿意理他；此刻心情决定这一句怎么说。"
                 "冲突时以此刻语气为主，但不要因为此刻心情否定底色。）")
    out = "\n".join(lines)
    return out[:max_len] if max_len else out


def render_card(qq: str, gid: str = "", *, detail: str = "brief",
                max_len: int = 0) -> str:
    """
    把一个人的档案渲染成固定模板。空档案返回 ""（调用方自己决定要不要问一句）。

    detail: micro / brief / full
    """
    qq = str(qq or "").strip()
    if not qq:
        return ""
    try:
        p = get_profile(qq)
        g = group_of(qq, gid)
    except Exception:
        return ""

    try:
        from . import callname
        name = (callname.user_call_of(qq, gid, fallback="")
                or call_of(qq, "", gid))
        _bot_call = callname.bot_call_of(qq, gid)
    except Exception:
        name = call_of(qq, "", gid)
        _bot_call = ""
    if not name:
        return ""
    head = _card_head(qq, gid)
    identity = _clip_text(
        g.get("identity") or p.get("identity") or p.get("long_term_impression") or "",
        120,
    )
    personality = _clip_text(
        g.get("scenario") or p.get("personality") or style_summary(qq) or "", 80)
    scenario = _clip_text(p.get("scenario") or "", 80)
    note = _clip_text(g.get("note") or p.get("depth_note") or "", 60)
    # 「事情」条数：brief 只给 1 条（省 token），full 给 10 条 ——
    # 用户 2026-11-19：「档案内容也是显示最近十条事情，不要太少了」。
    _full_n = tuning.i("PROFILE_CARD_EVENTS")
    notes = _card_notes(qq, 1 if detail != "full" else _full_n,
                        ranked=(detail != "full"))
    # 「我在他面前是什么样」—— 用户 2026-11-19：她得知道自己在每个人面前的样子
    # （爱撒娇 / 高冷 / 可爱 / 中二…）。这是**她的姿态**，不是对方的性格。
    stance = _clip_text(p.get("stance") or "", 24)
    try:
        _total_notes = len((get_bond(qq) or {}).get("notes") or [])
    except Exception:
        _total_notes = len(notes)
    examples = [str(x or "").strip() for x in (p.get("mes_example") or []) if str(x or "").strip()]
    bot_alias = str(_bot_call or p.get("bot_alias") or "").strip()

    # ---- micro：一行，塞上下文标签 ----
    if detail == "micro":
        parts = [head]
        if identity:
            parts.append(identity[:28])
        if notes:
            parts.append("最近：" + notes[0][:28])
        line = "｜".join(parts)
        return _clip_text(line, max_len or 90)

    # ---- brief / full：固定六段模板 ----
    lines = ["▍" + head]
    if identity:
        lines.append(f"人：{identity}")
    if personality and detail == "full":
        lines.append(f"性格：{personality}")
    if scenario and detail == "full":
        lines.append(f"在群里：{scenario}")
    if notes:
        if detail == "full" and len(notes) > 1:
            lines.append("事情：")
            lines.extend(f"  · {x[:60]}" for x in notes)
        else:
            lines.append(f"事情：{notes[0][:60]}")

    rel = ""
    try:
        v = intimacy(qq)
        if v >= 0.05:
            rel = f"{intimacy_label(v)}（{v:.2f}）"
    except Exception:
        pass
    if stance:
        lines.append(f"你在他面前：{stance}")
    if rel or bot_alias:
        tail = f"关系：{rel}" if rel else "关系："
        tail += f"；你叫他「{name}」"
        if bot_alias:
            tail += f"，他叫你「{bot_alias}」"
        lines.append(tail)

    if examples and detail == "full":
        lines.append("语气：")
        for ex in examples[:2]:
            for ln in str(ex).splitlines()[:2]:
                if ln.strip():
                    lines.append("  " + ln.strip()[:80])
    if note:
        lines.append(f"注意：{note}")

    # 👇 「还有更多」提示（用户 2026-11-19）：
    #    她总以为档案/记忆只有给她的这几条，于是不去翻。这里明确写清总数 + 怎么翻。
    _hint = ""
    if detail != "full" and _total_notes > len(notes):
        _hint = (f"（相处印象一共 {_total_notes} 条，这里只放最近 {len(notes)} 条；"
                 f"想细看：[[PROFILE|{name}]]，想翻更早：[[PROFILE|{name}|翻页2]]）")
        lines.append(_hint)

    text = "\n".join(lines)
    if not max_len:
        return text
    # ⚠️ 截断时必须**保住这句提示**：截掉它，她就又不知道还能翻页了
    if _hint and len(text) > max_len:
        _body = _clip_text(text[: -len(_hint)], max(40, max_len - len(_hint) - 1))
        return _body + "\n" + _hint
    return _clip_text(text, max_len)


def set_stance(qq: str, stance: str) -> str:
    """
    记「我在他面前是什么样」—— 撒娇 / 高冷 / 可爱 / 中二 / 认真…
    只增不删（传空字符串表示这次不改）。返回写后的值。
    """
    qq = str(qq or "").strip()
    stance = str(stance or "").strip()[:24]
    if not qq or not stance:
        return ""
    try:
        p = get_profile(qq)
        p["stance"] = stance
        p["stance_at"] = time.time()
        _save("profile", qq, p)
        return stance
    except Exception:
        return ""


def brief_card(qq: str, max_len: int = 90) -> str:
    """一行式档案摘要（兼容老调用）。真正的渲染在 render_card。"""
    return render_card(qq, detail="micro", max_len=max_len)


def batch_profiles(qqs, max_len: int = 90, gid: str = "") -> list[dict]:
    """
    批量取档案卡，按传入顺序去重。

    返回 [{"qq","name","card","call_name","nickname"}]。
    `name` 是**她该用来称呼这个人的名字**（本群 call_name 优先），
    不是群名片 —— 这样 prompt 里看到的、她回复时用的就是同一个称呼。
    """
    out, seen = [], set()
    for q in qqs or []:
        qq = str(q or "").strip()
        if not qq or qq in seen:
            continue
        seen.add(qq)
        card = render_card(qq, gid, detail="micro", max_len=max_len)
        if not card:
            continue
        nickname = call_name = ""
        try:
            p = get_profile(qq)
            nickname = str(p.get("nickname") or "")
            call_name = str(p.get("call_name") or "")
        except Exception:
            pass
        out.append({
            "qq": qq,
            "name": call_of(qq, nickname, gid) or qq,
            "card": card,
            "call_name": call_name,
            "nickname": nickname,
        })
    return out


# ---------- 通用：给 Prompt 用的一段话 ----------
def _note_line(n: dict) -> str:
    mark = EMOTION_MARK.get(n.get("emotion") or "", "")
    if mark:
        return f"{mark}｜{n['text']}"
    return n["text"]


def profile_prompt(qq: str, name: str) -> str:
    p = get_profile(qq)
    b = get_bond(qq)
    style = style_summary(qq)
    notes = b.get("notes") or []

    pieces = []
    pieces.append(f"当前说话人：{name}（QQ {qq}）")
    if p.get("call_name"):
        pieces.append(f"你平时叫他「{p['call_name']}」。")
    if p.get("bot_alias"):
        pieces.append(f"他叫你「{p['bot_alias']}」，喊这个名字就是在叫你。")
    if p.get("is_owner"):
        pieces.append("这是你的主人。")
    if p.get("topics"):
        pieces.append(f"你们之前聊过：{'、'.join(p['topics'][-5:])}")

    # ① 情绪刻痕高亮：生气/开心这类带强烈情绪的印象单独拎出来
    scars = [n for n in notes if (n.get("emotion") or "") in SCAR_EMOTIONS]
    scars.sort(key=lambda n: (n.get("weight") or 1.0, n.get("at") or 0), reverse=True)
    if scars:
        pieces.append("你对他印象最深（带情绪刻痕，别忘）：" + "；".join(_note_line(n) for n in scars[:3]))

    # ② 普通相处印象
    plain = [n for n in notes if (n.get("emotion") or "") not in SCAR_EMOTIONS]
    if plain:
        pieces.append("你对他印象：" + "；".join(n["text"] for n in plain[-3:]))

    # ③ 复盘 Agent 提炼的长期锚点
    if p.get("long_term_impression"):
        pieces.append(f"你最近对他的总体印象：{p['long_term_impression']}")

    if style:
        pieces.append(f"他{style}。")
    return "（" + " ".join(pieces) + "）"
