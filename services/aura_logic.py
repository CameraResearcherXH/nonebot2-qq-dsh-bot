# services/aura_logic.py
from core import tuning
import os
import re
import random
import time

# ================= 一、 真 @ 人逻辑 (from mentions.js) =================

AT_TAG = re.compile(r"<at:\s*([^>]{1,30}?)\s*>")

# 模型主动 @ 人用的隐藏标记：[[AT|小强]] / [[AT|12345678]]
# 和 `@小强` 的区别：这个不会被正文里的普通 @ 误伤，也不用担心名字里有空格。
AT_REQ_RE = re.compile(r"\[\[\s*AT\s*\|\s*([^\]|]{1,30}?)\s*\]\]", re.IGNORECASE)

def member_names(members: list) -> dict:
    """提取成员名字（群名片、昵称、去括号群名片）→ Set(QQ号)"""
    by_name = {}
    def add(name, qq):
        n = str(name or "").strip()
        if not n: return
        if n not in by_name: by_name[n] = set()
        by_name[n].add(qq)

    for m in members or []:
        qq = str(m.get("user_id", ""))
        if not qq: continue
        for raw in [m.get("card"), m.get("nickname")]:
            add(raw, qq)
            # 去掉括号备注「张小明（摸鱼仔）」→「张小明」
            add(re.sub(r"\s*[（(【\[][^）)】\]]*[）)】\]]\s*$", "", str(raw or "")), qq)
    return by_name

def resolve_member(who: str, by_name: dict, qqs: set) -> str:
    """
    名字或 QQ 号 → 唯一 QQ 号；有重名或认不出返回空。

    匹配顺序：QQ 号 → 精确名字 → 唯一子串。
    子串兜底是为了「小强」能对上「小强（摸鱼中）」这类带备注的群名片，
    但**只在候选唯一时**才认，重名一律返回空（宁可不 @，也不 @ 错人）。
    """
    w = re.sub(r"^[@＠]", "", str(who or "")).strip()
    if re.match(r"^\d{5,12}$", w): return w
    hit = by_name.get(w)
    if hit and len(hit) == 1:
        return list(hit)[0]
    if hit and len(hit) > 1:
        return ""
    if len(w) < 2:
        return ""
    cand = set()
    for name, qs in by_name.items():
        if name and w in name:
            cand |= qs
    return list(cand)[0] if len(cand) == 1 else ""

def _longest_name(rest: str, by_name: dict) -> dict:
    head_match = re.match(r"^[^\s@＠]{1,20}", rest)
    head = head_match.group(0) if head_match else ""
    for n in range(len(head), 0, -1):
        qq = resolve_member(head[:n], by_name, set())
        if qq: return {"qq": qq, "len": n}
    return None

def expand_mentions(segs: list, members: list) -> tuple:
    """把消息段里的文字 @ 换成真 @ 段。返回 (新段落, 未匹配成功列表)"""
    by_name = member_names(members)
    qqs = {str(m.get("user_id", "")) for m in members or []}
    out = []
    missed = []

    def push_text(t):
        if t: out.append({"type": "text", "data": {"text": t}})

    for seg in segs:
        if seg.get("type") != "text":
            out.append(seg)
            continue
        # 👇 模型写的是隐藏标记 [[AT|名字]]，先统一成内部写法 <at:名字>，
        #    后面的解析逻辑和「她自己写的 @名字」完全共用一套。
        s = AT_REQ_RE.sub(lambda m: f"<at:{m.group(1)}>", seg.get("data", {}).get("text", ""))
        buf = ""
        i = 0
        while i < len(s):
            tag = AT_TAG.match(s, i) if s[i] == "<" else None
            if tag:
                qq = resolve_member(tag.group(1), by_name, qqs)
                if qq:
                    push_text(buf); buf = ""
                    out.append({"type": "at", "data": {"qq": qq}})
                else:
                    missed.append(re.sub(r"^@", "", tag.group(1)))
                    buf += "@" + re.sub(r"^@", "", tag.group(1))
                i += len(tag.group(0))
                continue

            if s[i] in ("@", "＠"):
                hit = _longest_name(s[i + 1:], by_name)
                if hit:
                    push_text(buf); buf = ""
                    out.append({"type": "at", "data": {"qq": hit["qq"]}})
                    i += 1 + hit["len"]
                    if i < len(s) and not s[i].isspace():
                        buf += " "
                    continue
                name_match = re.match(r"^[^\s@，。！？,.!?、：:]{1,20}", s[i + 1:])
                if name_match: missed.append(name_match.group(0))
            buf += s[i]
            i += 1
        push_text(buf)
    return out, missed

def has_mention(text: str) -> bool:
    # [[AT|名字]] 也算 —— 否则她主动 @ 人时，代码根本不会去拉群成员列表
    return bool(re.search(r"[@＠]|<at:|\[\[\s*AT\s*\|", str(text or ""), re.IGNORECASE))


# ================= 二、 群聊策略 (from group-policy.js) =================

DEFAULT_GROUP_POLICY = {
    # 2026-11-12 已把「肥鱼/大肥鱼」从她的唤醒词里去掉（那是群里另一条鱼的名字）。
    # 但主动插话策略这里漏了 —— 之前 wakeWords 还留着，导致 @ 小肥鱼 时她也被
    # 「叫到了肥鱼」强制插话。现在清空：主动插话只靠 relevance + 概率，不再按关键词强制。
    "wakeWords": [],
    "interjectChance": 0.15,
    "cooldownSec": 90,
    "maxPerHour": 6,
    "quietHours": [1, 8],
    "minRecentMessages": 3,
    "activeWindowSec": 300,
    "interests": ["白米饭", "游戏", "猫", "狗", "睡觉", "熬夜", "天气", "表情包", "奶茶"],
    "minRelevance": 0.25,
    "burstMax": 8,
    "burstWindowSec": 120,
}

def topic_relevance(recent_texts: list, interests: list, memory_score: float = 0) -> float:
    txt = " ".join(recent_texts or [])
    hits = sum(1 for w in interests if w and w in txt)
    return min(1.0, hits * 0.3 + max(0, min(1, memory_score)) * 0.5)

def policy_for(global_policy, overrides, group_id):
    g = {**DEFAULT_GROUP_POLICY, **(global_policy or {})}
    o = (overrides or {}).get(str(group_id)) if overrides else None
    return {**g, **o} if o else g

def in_quiet_hours(hour: int, quiet: list) -> bool:
    if not isinstance(quiet, list) or len(quiet) != 2: return False
    a, b = quiet
    if a == b: return False
    return (a <= hour < b) if a < b else (hour >= a or hour < b)

def decide_group_reply(p: dict) -> dict:
    pol = p.get("policy") or DEFAULT_GROUP_POLICY
    text = str(p.get("text") or "")
    rand = p.get("rand") or random.random
    said = p.get("botReplyTimes") or []

    # 刹车
    burst = sum(1 for t in said if p["now"] - t < (pol.get("burstWindowSec", 120)) * 1000)
    if burst >= (pol.get("burstMax", 8)) and not (p.get("fromOwner") and (p.get("mentioned") or p.get("repliedToBot"))):
        return {"reply": False, "mode": "none", "reason": f"刚说了太多（{burst} 次），歇一会儿"}

    if p.get("mentioned"):
        return {"reply": True, "mode": "must", "reason": "被 @"}
    if p.get("repliedToBot"):
        return {"reply": True, "mode": "must", "reason": "引用了她的消息"}
    hit = next((w for w in pol.get("wakeWords", []) if w and w in text), None)
    if hit:
        return {"reply": True, "mode": "must", "reason": f"叫到了「{hit}」"}

    rel = p.get("relevance", 1.0)
    if not (pol.get("interjectChance", 0) > 0):
        return {"reply": False, "mode": "none", "reason": "插话关着"}
    if in_quiet_hours(p.get("hour", 0), pol.get("quietHours", [])):
        return {"reply": False, "mode": "none", "reason": "静默时段"}

    min_rel = float(pol.get("minRelevance", 0))
    engaged_until = p.get("engagedUntil", 0)
    if engaged_until and p["now"] < engaged_until and rel >= min_rel:
        return {"reply": True, "mode": "engaged", "reason": f"正聊得起劲，接着说（相关度 {rel:.2f}）"}

    times = p.get("interjectTimes") or []
    last_hour = sum(1 for t in times if p["now"] - t < 3600000)
    if last_hour >= pol.get("maxPerHour", 6):
        return {"reply": False, "mode": "none", "reason": "这小时插话够多了"}
    last = max(times, default=0)
    if p["now"] - last < pol.get("cooldownSec", 90) * 1000:
        return {"reply": False, "mode": "none", "reason": "插话冷却中"}

    recent_times = p.get("recentTimes") or []
    recent = sum(1 for t in recent_times if p["now"] - t < pol.get("activeWindowSec", 300) * 1000)
    if recent < pol.get("minRecentMessages", 3):
        return {"reply": False, "mode": "none", "reason": "群里没在聊"}

    if min_rel > 0 and rel < min_rel:
        return {"reply": False, "mode": "none", "reason": f"话题和她关系不大（相关度 {rel:.2f}）"}

    chance = min(0.8, pol["interjectChance"] * (1 + 4 * rel)) if min_rel > 0 else pol["interjectChance"]
    if rand() < chance:
        return {"reply": True, "mode": "interject", "reason": f"话题相关，插一句（相关度 {rel:.2f}）"}
    return {"reply": False, "mode": "none", "reason": "这次不插话"}


# ================= 三、 回复解析 (from reply.js) =================

SILENCE = "[沉默]"

# 「不接话权」的各种写法：模型觉得无趣/不想说话时输出这些，我们就当这条没看见。
# 注意：不回复**但记忆照写**（chat worker 里是先写记忆再判断 silent 的）。
_SILENCE_VARIANTS = (
    "[沉默]", "［沉默］", "(沉默)", "（沉默）",
    "(不想回复)", "（不想回复）", "[不想回复]", "【不想回复】",
    "不想回复", "(不想说话)", "（不想说话）",
    "(no reply)", "(skip)",
)

# ================= 三-0、 [[MEMO]] 隐藏记忆协议 =================
# DeepSeek 在回复末尾附带：
#   [[MEMO|emotion:happy|text:他夸我可爱，我很开心]]
# 这一行不会发给用户，而是被拆出来写进 memory_v2 + bond + mood。
MEMO_RE = re.compile(r"\[\[\s*MEMO\s*\|([\s\S]*?)\]\]", re.IGNORECASE)

# ================= 三-0b、 [[MOOD]] 心情自述协议 =================
# 用户 2026-11-19：「心情不能是我们指定的几种，不能在我们限制的几种心情范围内。
#   她要自己有写心情的权利。」
# 她可以在回复末尾另起一行写：
#   [[MOOD|被夸得有点飘，但还是装作没事]]
# 这一行**不会发给用户**，只写进 mood.set_label()（自由文本，不是固定枚举）。
MOOD_RE = re.compile(r"\[\[\s*MOOD\s*\|([^\]\[]{1,120})\]\]", re.IGNORECASE)


# ================= 三-0c、 [[THINK]] 心里话协议 =================
# 用户 2026-11-20：「感觉还是没有每句回复的 json 带上心理活动。」
# 以前心理活动只藏在 [[MEMO|think:…]] 里，而 MEMO 是「真的触动才写」→
# 大部分回复没有 think。现在独立成 [[THINK|…]]，**每条回复都要带**，
# 不发给用户、不当记忆，只喂心情判读。
THINK_RE = re.compile(r"\[\[\s*THINK\s*\|([^\]\[]{1,120})\]\]", re.IGNORECASE)


def parse_think(text: str) -> tuple:
    """抠出 [[THINK|…]]。返回 (清理后的正文, 心里话)。"""
    raw = str(text or "")
    think = ""
    for m in THINK_RE.finditer(raw):
        v = str(m.group(1) or "").strip()
        if v:
            think = v
    if think:
        raw = THINK_RE.sub("", raw)
    # 被截断的半截标签也清掉，别让它漏给用户
    raw = re.sub(r"\[\[\s*THINK[\s\S]*$", "", raw, flags=re.IGNORECASE)
    return raw, think[:200]


def parse_mood(text: str) -> tuple:
    """抠出 [[MOOD|…]]。返回 (清理后的正文, 心情原话)。"""
    raw = str(text or "")
    label = ""
    for m in MOOD_RE.finditer(raw):
        v = str(m.group(1) or "").strip()
        if v:
            label = v
    if label:
        raw = MOOD_RE.sub("", raw)
    return raw, label

# 一条回复最多记 2 条（协议里写了，这里再兜一层，防止模型一次吐十条把记忆库刷爆）
MEMO_MAX_PER_REPLY = 2

EMOTION_ALIAS = {
    "happy": "happy", "开心": "happy", "高兴": "happy", "joy": "happy",
    "good": "happy", "笑": "happy", "愉快": "happy",
    "excited": "excited", "兴奋": "excited", "激动": "excited",
    "love": "love", "喜欢": "love", "心动": "love", "affection": "love",
    "touched": "touched", "感动": "touched", "warm": "touched", "暖心": "touched",
    "sad": "sad", "难过": "sad", "down": "sad", "低落": "sad", "伤心": "sad",
    "lonely": "lonely", "寂寞": "lonely", "想他": "lonely", "孤单": "lonely",
    "angry": "angry", "生气": "angry", "愤怒": "angry", "mad": "angry", "怒": "angry",
    "annoyed": "annoyed", "烦躁": "annoyed", "烦": "annoyed", "不爽": "annoyed",
    "neutral": "neutral", "calm": "neutral", "普通": "neutral", "平静": "neutral",
}

EMOTION_CN = {
    "happy": "开心", "excited": "兴奋", "love": "喜欢", "touched": "感动",
    "sad": "难过", "lonely": "寂寞", "angry": "生气", "annoyed": "烦躁",
    "neutral": "平静",
}


def normalize_emotion(raw: str) -> str:
    """把 DeepSeek 写的各种情绪词归一化到固定集合。"""
    e = str(raw or "").strip().lower()
    if not e:
        return "neutral"
    if e in EMOTION_ALIAS:
        return EMOTION_ALIAS[e]
    for k, v in EMOTION_ALIAS.items():
        if k and k in e:
            return v
    return "neutral"


def emotion_cn(emotion: str) -> str:
    return EMOTION_CN.get(normalize_emotion(emotion), "平静")


def _parse_memo_fields(body: str) -> dict:
    """`emotion:happy|text:xxx|imp:0.8` → dict"""
    fields = {}
    for chunk in str(body or "").split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.split(r"[:：]", chunk, maxsplit=1)
        if len(m) == 2:
            fields[m[0].strip().lower()] = m[1].strip()
        else:
            # 没写 key 的裸文本，当作 text
            fields.setdefault("text", chunk)
    return fields


def parse_memos(text: str) -> tuple:
    """
    抠出所有 [[MEMO|...]]。
    返回 (清理后的正文, [memo dict, ...])
    memo dict: {emotion, emotion_raw, text, importance, kind, tags}
    """
    raw = str(text or "")
    memos = []
    for m in MEMO_RE.finditer(raw):
        f = _parse_memo_fields(m.group(1))
        body = (f.get("text") or "").strip()
        if not body:
            continue
        emo_raw = f.get("emotion") or f.get("mood") or f.get("feeling") or ""
        imp_raw = f.get("imp") or f.get("importance") or ""
        try:
            imp = float(imp_raw)
        except Exception:
            imp = None
        memos.append({
            "emotion": normalize_emotion(emo_raw),
            "emotion_raw": emo_raw,
            # 「心理活动」：这件事在她心里是怎么转的（给心情判读模型看，
            # 也说明白了「为什么是这个情绪」）。用户 2026-11-19 加。
            "think": str(f.get("think") or f.get("mind") or f.get("inner") or "").strip()[:200],
            "text": body,
            "importance": imp,
            "kind": (f.get("kind") or "impression").strip().lower(),
            "tags": [t.strip() for t in (f.get("tags") or "").split(",") if t.strip()],
        })
    clean = MEMO_RE.sub("", raw)
    # 顺手清掉 MEMO 被截断后残留的空标签
    clean = re.sub(r"\[\[\s*MEMO[\s\S]*$", "", clean, flags=re.IGNORECASE)
    if len(memos) > MEMO_MAX_PER_REPLY:
        memos = memos[:MEMO_MAX_PER_REPLY]
    return clean.strip(), memos


# ================= 三-0b、 [[CALL]] 称谓协议 =================
# 为什么要有这个：
#   `/改名` `/叫你` 是「硬规则指令」——用户必须背格式，只覆盖显式命令。
#   但真实聊天里全是软表达：
#       用户：我可以叫你小鲸鱼吗？
#       她：  好呀，以后你就叫我小鲸鱼吧～
#   这种句式无穷无尽，用正则去猜必翻车。所以判断权交给 DeepSeek：
#   她觉得自己和这个人之间**建立了称呼**，就在回复末尾补一行隐藏标记：
#       [[CALL|bot:小鲸鱼|user:阿明]]
#   · bot  = 这个人以后怎么叫她   → 写 profile.bot_alias（喊这个名字 = 等同 @ 她）
#   · user = 她以后怎么叫这个人   → 写 profile.nickname + aliases
#   两个字段都可以只写一个。
CALL_RE = re.compile(r"\[\[\s*CALL\s*\|([\s\S]*?)\]\]", re.IGNORECASE)
CALL_MAX_PER_REPLY = 2


def parse_calls(text: str) -> tuple:
    """
    抠出所有 [[CALL|...]]。返回 (清理后的正文, [call dict, ...])
    call dict: {bot, user, note}
    """
    raw = str(text or "")
    calls = []
    for m in CALL_RE.finditer(raw):
        f = _parse_memo_fields(m.group(1))
        bot_alias = (f.get("bot") or f.get("me") or f.get("叫我") or "").strip()
        user_name = (f.get("user") or f.get("him") or f.get("叫他") or "").strip()
        note = (f.get("note") or f.get("text") or "").strip()
        # 去掉模型可能顺手加的引号/书名号
        bot_alias = bot_alias.strip("「」『』\"'“” ")
        user_name = user_name.strip("「」『』\"'“” ")
        if not bot_alias and not user_name:
            continue
        calls.append({
            "bot": bot_alias[:12],
            "user": user_name[:12],
            "note": note[:60],
        })
    clean = CALL_RE.sub("", raw)
    # 顺手清掉被截断后残留的半截标签
    clean = re.sub(r"\[\[\s*CALL[\s\S]*$", "", clean, flags=re.IGNORECASE)
    if len(calls) > CALL_MAX_PER_REPLY:
        calls = calls[:CALL_MAX_PER_REPLY]
    return clean.strip(), calls


# ================= 括号小动作（2026-11-18 用户要求去掉）=================
# 有人投诉过「（歪头）（笑）（摊手）（小声）」这类旁白。人格卡里已经禁止，
# 这里再做一层兜底：模型手滑写了也发不出去。
# 只删「短 + 以动作词开头」的括号，正常括号（（其实我不太确定））不动。
_ACTION_PAREN_RE = re.compile(r"[（(]\s*([^（）()]{1,10}?)\s*[）)]")
_ACTION_HEADS = (
    "笑", "歪头", "偏头", "侧头", "摊手", "耸肩", "叹气", "眨眼", "点头", "摇头",
    "皱眉", "挑眉", "挠头", "抿嘴", "咬唇", "低头", "后退", "沉默", "顿了顿",
    "想了想", "小声", "轻声", "压低声音", "吐舌", "叉腰", "抱臂", "愣", "咳",
    "扶额", "捂脸", "甩尾", "翘", "拍", "戳", "摸", "转身", "缩", "抖", "伸",
)


def strip_actions(text: str) -> str:
    """去掉「（笑）（摊手）（歪头）」这类括号动作/心理，只留文字。"""
    def _rep(m):
        body = (m.group(1) or "").strip()
        if not body or len(body) > 10:
            return m.group(0)
        if re.search(r"[，。！？、,.!?；;：:…]", body):
            return m.group(0)
        if body.startswith(_ACTION_HEADS):
            return ""
        return m.group(0)

    return _ACTION_PAREN_RE.sub(_rep, str(text or ""))


def strip_markdown(text: str) -> str:
    s = str(text or "")
    s = re.sub(r"```[a-zA-Z0-9_-]*\n?([\s\S]*?)```", r"\1", s)
    s = re.sub(r"`([^`\n]+)`", r"\1", s)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", s)
    s = re.sub(r"__([^_\n]+)__", r"\1", s)
    s = re.sub(r"(^|[^*])\*([^*\n]+)\*(?!\*)", r"\1\2", s)
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*>\s?", "", s, flags=re.MULTILINE)
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", r"\1 \2", s)
    # 剥掉模型偶尔抄出来的 <img ...> HTML 标签（QQ 不会渲染，只会显示成原始代码）
    s = re.sub(r"<img[^>]*>", "", s, flags=re.IGNORECASE)
    # 剥掉裸露的 QQ 图片直链（multimedia.nt.qq.com.cn），别把图的码发给用户
    s = re.sub(r"https?://multimedia\.nt\.qq\.com\.cn/[^\s\]）】>，。！？]*", "", s)
    s = re.sub(r"[ \t]+$", "", s, flags=re.MULTILINE)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()

def is_silence(text: str) -> bool:
    t = str(text or "").strip()
    return t in ("",) + _SILENCE_VARIANTS

# ================= [[FOCUS]] 回应对象 =================
# 她回复时会标注自己在回哪一条（[[FOCUS|当前]] 或 [[FOCUS|3]]），
# 用来诊断「是不是在接历史旧话」。用户看不到这个标记。
FOCUS_RE = re.compile(r"\[\[\s*FOCUS\s*\|\s*([^\]\n]{1,10}?)\s*\]\]", re.IGNORECASE)


def parse_focus(text: str) -> tuple:
    """返回 (清理后的正文, focus 值)。focus 可能是「当前」/ 数字 / 空。"""
    raw = str(text or "")
    m = FOCUS_RE.search(raw)
    val = m.group(1).strip() if m else ""
    clean = FOCUS_RE.sub("", raw)
    return clean.strip(), val


def parse_reply(text: str) -> dict:
    # [[LONG]] 必须活到发送层（那边靠它跳过截断、改走合并转发）。
    # 但第 ③ 步会把所有未认领的 [[...]] 一起剥掉 —— 以前它就是这么被吃掉的，
    # 导致「写 [[LONG]] 也没用，照样被截成 20 字」。这里先记下来，最后补回第一个文字气泡。
    _long_flag = bool(re.search(r"\[{2,3}\s*LONG\s*\]{2,3}", str(text or ""), re.I))
    # ① 先把 [[FOCUS|..]] / [[CALL|...]] / [[MEMO|...]] 抠出来，绝不发给用户
    raw, focus = parse_focus(text)
    raw, calls = parse_calls(raw)
    raw, memos = parse_memos(raw)
    raw, mood_label = parse_mood(raw)
    raw, think = parse_think(raw)
    # ② [[AT|名字]] → 内部写法 <at:名字>，由发送层换成 NapCat 的真 @ 段
    raw = AT_REQ_RE.sub(lambda m: f"<at:{m.group(1)}>", raw)
    # ③ 兜底：剥掉任何没被上面认领的 [[...]]（模型手滑写的 [[LUCK]] 之类），
    #    绝不让「正则词」原文漏给用户（2026-11-17 实测漏过 [[LUCK]]）。
    # 支持双/三括号：她按提示常写 [[[TOOLS|猪猪]]]，
    # 只匹配双括号会把外层剩成 "[]"，用户就看到句尾挂一个 "]"（评测抓到 2 次）。
    raw = re.sub(r"\[{2,3}[^\]]*\]{2,3}", "", raw)
    # 兜底：括号小动作一律不发给用户（（歪头）（笑）（摊手）（小声）…）
    raw = strip_actions(raw)
    if is_silence(raw):
        return {"silent": True, "items": [], "memos": memos, "calls": calls,
                "focus": focus, "mood": mood_label, "think": think}

    items = []
    reply_to = ""

    def attach(from_idx):
        nonlocal reply_to
        if reply_to and len(items) > from_idx:
            items[from_idx]["replyTo"] = reply_to
            reply_to = ""

    for bubble in raw.split("|||"):
        part = bubble.strip()
        if not part: continue
        regex = re.compile(r"<poke\s*/?>|<reply:\s*(\d+)\s*>|<voice>([\s\S]*?)</voice>|<sticker:([^>]+?)>(?:\s*</sticker>)?|!\[[^\]]*\]\(\s*([^)\s]+)\s*\)")
        last = 0
        for m in regex.finditer(part):
            from_idx = len(items)
            _push_text(items, part[last:m.start()])
            attach(from_idx)
            from_idx = len(items)
            if m.group(0).startswith("<poke"):
                if not any(i["kind"] == "poke" for i in items):
                    items.append({"kind": "poke", "value": ""})
            elif m.group(1) is not None:
                reply_to = m.group(1)
            elif m.group(2) is not None:
                v = strip_markdown(m.group(2))
                if v: items.append({"kind": "voice", "value": v})
            elif m.group(3) is not None:
                v = m.group(3).strip()
                if v: items.append({"kind": "sticker", "value": v})
            elif m.group(4) is not None:
                items.append({"kind": "image", "value": m.group(4)})
            attach(from_idx)
            last = m.end()
        from_idx = len(items)
        _push_text(items, part[last:])
        attach(from_idx)

    quoted = False
    for it in items:
        if it.get("replyTo"):
            if quoted: del it["replyTo"]
            else: quoted = True

    if not items:
        return {"silent": True, "items": [], "memos": memos, "calls": calls,
                "focus": focus, "mood": mood_label, "think": think}
    # 把 [[LONG]] 补回第一个文字气泡，交给发送层决定「不截断 + 合并转发」
    if _long_flag:
        for it in items:
            if it.get("kind") == "text":
                it["value"] = "[[LONG]]" + str(it.get("value") or "")
                break
    return {"silent": False, "items": _cap_bubbles(items), "memos": memos,
            "calls": calls, "focus": focus, "mood": mood_label,
            "think": think}

# 一条回复最多拆几个气泡。用户 2026-11-12：「很多时候回复连续三句，
# 我建议改成两句或者一句就好」—— 连发三条在群里很烦，两条是上限。
MAX_BUBBLES = tuning.i("MAX_BUBBLES")


def _cap_bubbles(items: list, max_bubbles: int = MAX_BUBBLES) -> list:
    texts = [i for i in items if i["kind"] == "text"]
    if len(texts) <= max_bubbles: return items
    keep = set(id(t) for t in texts[:max_bubbles - 1])
    tail = texts[max_bubbles - 1:]
    merged = {"kind": "text", "value": "\n".join(t["value"] for t in tail)}
    out = []
    placed = False
    for it in items:
        if it["kind"] != "text" or id(it) in keep:
            out.append(it)
        elif not placed:
            out.append(merged); placed = True
    return out

def split_lines(text: str) -> list:
    t = str(text or "").strip()
    if not t: return []
    if "```" in t or len(t) > 240: return [t]
    lines = [s.strip() for s in re.split(r"\n+", t) if s.strip()]
    if len(lines) <= 1: return lines
    listy = sum(1 for l in lines if re.match(r"^(\d+[.、)]|[-*•·])", l))
    if listy >= (len(lines) + 1) // 2: return [t]
    return lines

def _push_text(items: list, s: str):
    t = strip_markdown(re.sub(r"</?reply[^>]*>", "", str(s or "")))
    if not t or is_silence(t): return
    # 只剩括号碎片的气泡不要发（真机出现过她只发一个 "]" ——
    # 模型写畸形标记 [[AT|名字] 被剥掉后留下的残渣，用户看到一脸问号）。
    # ⚠️ 只拦括号/分隔符，不拦 emoji：她只回一个 "😂" 是合理的。
    if re.fullmatch(r"[\s\[\]\(\)\{\}（【《「『】》」』|｜/／\\]+", t):
        return
    for line in split_lines(t):
        if not is_silence(line):
            items.append({"kind": "text", "value": line})

def wants_voice(text: str) -> bool:
    t = str(text or "")
    if "不要语音" in t or "别发语音" in t: return False
    return bool(re.search(r"(用|发|来|给我|录)[^，。？！\n]{0,4}语音|语音[^，。？！\n]{0,4}(说|发|讲|回|念|唱)", t))

def bubble_delay(next_text: str = "") -> int:
    length = len(str(next_text or ""))
    typing = 350 + length * 110
    return int(min(4500, max(700, typing)) * (0.8 + random.random() * 0.5))
