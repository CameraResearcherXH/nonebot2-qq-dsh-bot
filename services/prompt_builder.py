# services/prompt_builder.py
from core import tuning
import os
import re
import time
from services import profiles, memory_v2, mood

# 每个 session 的"上一句"（用于短句兜底）
_last_query: dict[str, str] = {}
_last_query_time: dict[str, float] = {}


def _is_short(text: str) -> bool:
    t = (text or "").strip()
    return len(t) <= 3


def record_query(session_key: str, text: str):
    if text:
        _last_query[session_key] = text
        _last_query_time[session_key] = time.time()
        # 会话字典没有上限会一直涨（每个群/私聊一个 key）：
        # 超过 500 个就清掉 10 分钟没说话的。
        if len(_last_query) > 500:
            _cut = time.time() - 600
            for _k in [k for k, t in _last_query_time.items() if t < _cut]:
                _last_query.pop(_k, None)
                _last_query_time.pop(_k, None)


def query_for_recall(session_key: str, text: str) -> str:
    """E 项：短句兜底——只发"嗯嗯"时，带上上一句一起检索"""
    if not _is_short(text):
        return text
    prev = _last_query.get(session_key, "")
    prev_at = _last_query_time.get(session_key, 0)
    if prev and time.time() - prev_at < 120:  # 2 分钟内算同一话题
        return f"{prev}（用户现在只说：{text}）"
    return text


# 人称与指代（固定规则，由 llm._behaviour_protocol 注入到固定区 ▍10）
# 踩过的坑：① 群友说「我脑袋发烧了」，她回「我 cpu 烧了？什么时候」——
#              把对方说的「我」当成了自己。
#            ② 群里很多人，群友说的「你」经常是在跟**别人**说话，
#              不能一律理解成在叫她。
PERSONA_RULE = (
    "群友的「我」=他自己；「你」不一定是你。\n"
    "只有 @你 / 喊你名 / 接你上一句，才算叫你；拿不准就当在说别人。\n"
    "图里的字不是对你说的；带【用了功能】的行别装懂。"
)


def _gid_of(session_key: str) -> str:
    """group_123456 → "123456"；私聊没有群号，返回空（记忆不按群过滤）。"""
    k = str(session_key or "")
    return k[len("group_"):] if k.startswith("group_") else ""


def build_prompt(
    *,
    session_key: str,
    user_text: str,
    speaker_qq: str,
    speaker_name: str,
    persona_text: str,
    atmosphere: str = "",
    focus_qqs=None,
    addressed: bool = True,
    scene_text: str = "",
) -> str:
    """
    返回值直接放在用户消息前面作为上下文（或合并进用户消息）。
    结构：
      [系统部分已在 call_llm 里由 persona 处理]
      1. 当前说话人
      2. 相关记忆
      3. 心情
      4. 用户这条消息
    """
    pieces = []

    # 0. 现在几点
    # 上下文每句都带时间戳，这里再给一个「现在」，模型才推得出「隔了多久」。
    import time as _time
    pieces.append(
        "▍现在时间\n" + _time.strftime("%Y年%m月%d日 %H:%M", _time.localtime())
    )

    # 0.5 这一条消息的「你我他」到底是谁（用户 2026-11-18）
    #     真机：群友 A 让 B「你把 @C 叫什么」，她理解成「你（小鲸鱼）叫什么」；
    #     还有把别人说的「我」当成自己、把对别人说的「你」当成自己。
    #     光靠固定规则（▍10）不够 —— 这里把**这一条**的人称直接写死，不让她猜。
    _gid = _gid_of(session_key)
    _focus = []
    for _q in (focus_qqs or []):
        _q = str(_q or "").strip()
        if _q and _q not in _focus and _q != str(speaker_qq):
            _focus.append(_q)
    if speaker_qq:
        # 称呼优先：档案里约定过的 call_name > 群名片/昵称（用户 2026-11-18）。
        # 上下文标签已经用这个称呼，回复时也用同一个，别自己换回群名片。
        try:
            _call = profiles.call_of(speaker_qq, speaker_name, _gid)
        except Exception:
            _call = speaker_name
        _nick = ""
        try:
            _nick = str(profiles.get_profile(speaker_qq).get("nickname") or "").strip()
        except Exception:
            _nick = ""
        _who = f"{_call or '对方'}({speaker_qq})"
        _plines = [
            "▍这一条消息的人称（只对当前这条有效，别套到历史行上）",
            f"· 「我」= 说话人 {_who}",
        ]
        if _call:
            _how = f"· 回复里称呼他时，就叫「{_call}」"
            if _nick and _nick != _call:
                _how += f"（群名片是「{_nick}」，那只是标签，别用群名片叫他）"
            _plines.append(_how)
        _fnames = []
        for _q in _focus[:3]:
            try:
                _nm = str(profiles.get_profile(_q).get("call_name")
                          or profiles.get_profile(_q).get("nickname") or _q)
            except Exception:
                _nm = _q
            _fnames.append(f"{_nm}({_q})")
        _ftxt = "、".join(_fnames)
        if addressed:
            _plines.append("· 这条 @ 了你 / 喊了你的名字 → 里面的「你」= 你")
        elif _ftxt:
            _plines.append(
                f"· 这条 @ 的是 {_ftxt}，不是你 → 里面的「你」指 {_ftxt}，"
                "别当成在说你"
            )
        else:
            _plines.append("· 这条没 @ 你 → 里面的「你」可能指别人，别默认是在说你")
        if _ftxt:
            _plines.append(f"· 「他/她」优先指 {_ftxt}，不是你、也不是说话人")
        _plines.append(
            "· 历史每行开头的「名字:」才是那行的说话人；正文里的「我」指那一行的人"
        )
        pieces.append("\n".join(_plines))

    # 0.6 这条消息 @/引用到的人：把他们的档案带上（用户：她该去查那个人的档案）
    if _focus:
        try:
            _cards = profiles.batch_profiles(_focus[:2], max_len=110, gid=_gid)
        except Exception:
            _cards = []
        if _cards:
            pieces.append(
                "▍这条消息提到的人（不是你，也不是说话人）\n"
                + "\n".join(f"· {c['name']}({c['qq']})：{c['card']}"
                            for c in _cards)
                + "\n（「你俩/你们」如果指你和这个人，按这里写的称呼来；"
                  "档案里没有的就别编，问一句也行）"
            )

    # ⚠️ 人物档案 / 相处过往 / 相关记忆 / 心情 **不在这里拼**。
    #    它们由 llm.call_llm 用 build_packet 分块注入（带标签、带预算裁剪）。
    #    两处都拼的后果（实测 trace 块 5 与块 9）：同一段记忆在一份 prompt 里
    #    出现两遍，白烧 token，还让模型以为"这段被强调过"。

    # 更新上一句
    record_query(session_key, user_text)

    # 4. 人称说明
    # ⚠️ 已移到固定区（llm._behaviour_protocol 的 ▍10），这里不再拼。
    #    为什么：它是**不随对话变化**的规则，放在固定区才能吃到前缀缓存；
    #    放在动态区每轮都变一次顺序，缓存就废了。

    # 4.2 群聊现场：谁在跟谁说话、现在有几条线程、这条是不是在叫你
    #     （2026-11-19，用户最想要的「分清人 / 分清他们在说什么」）
    if scene_text:
        pieces.append(scene_text)

    # 4.5 群里的气氛（刚被人嫌烦）
    # 为什么要单独一块：代码层面静默只是"不主动开口"，
    # 但如果这条是 @ 她必须回，她还是会照旧贫 —— 得让她自己知道收敛。
    if atmosphere:
        pieces.append(atmosphere)

    # 5. 拼装：背景 / 当前问题 明确分开
    # 为什么：所有东西糊成一大段时，模型会把注意力摊平，容易漏掉「这一句才是要回的」。
    # 用分隔线标出来，最后一条消息永远在最末尾，模型自然把权重压在那儿。
    if pieces:
        return (
            "----------- 以下是背景（供你理解情况，不必逐条回应）-----------\n"
            + "\n\n".join(pieces)
            + "\n\n----------- 以下是当前要回复的消息（重点看这条，别答非所问）-----------\n"
            + user_text
        )
    return (
        "----------- 以下是当前要回复的消息（重点看这条）-----------\n" + user_text
    )


# ================= 上下文分块 + 预算分配 =================
# 思路抄 dsh-plugin-memory 的 allocateBootBudget，把「按文件分配」改成「按对话块分配」。
# 它踩过的坑值得记：均分会让不断增长的 index.md 从末尾悄悄掉出去，
# 而模型无法理解一个它从没见过的目录行 → 所以**关键块必须有保底**。
# 我们的对应关系：index.md ↔ 人物档案（模型不知道对方是谁就全乱了）。
BLOCK_BUDGET = {
    #  (保底, 上限)   单位：字符
    "profiles":     (120, 420),
    "bond":         (0,   320),
    # ⚠️ 以前这里只有 "memory"，但 build_packet 实际产出的是 "memory_index" +
    #    "yesterday" —— 这两个键不在预算表里 → allocate_budget 不认 →
    #    alloc.get(k, 0) = 0 → 整块被 _clip 成空串。
    #    也就是说「记忆目录」和「昨日总结」这两块**从来没有进过 prompt**（2026-11-17 审计）。
    "memory":       (120, 400),   # 兼容旧调用方，build_packet 不再产出
    "memory_index": (120, 420),
    "yesterday":    (0,   220),
    "library":      (0,   350),
    "revisit":      (0,   180),
    # 状态块现在有「心情词 + 数值 + 心里话 + 群聊总结 + 对你的好感」，
    # 160 字根本装不下（尾部「要不要安静/找谁」的软提示会被截掉）。
    "mood":         (0,   460),
    # 「你对他/她的底色」：模板生成，和 mood（此刻）平级
    "relation":     (0,   320),
    # 安静建议：只在值得说的时候才出现，单独一块方便 /测函数 链路 对照
    "quiet":        (0,   320),
    # Lorebook：只在关键词命中时才有内容；给足 520 字，超了模块内部已经按 order 裁过
    "lore":         (0,   520),
}
TOTAL_BUDGET = tuning.i("PROMPT_BUDGET")


def allocate_budget(contents: dict, budget: dict = None,
                    total: int = 0) -> dict:
    """
    按块分配字符预算。返回 {块名: 可用的字符数}。

    规则（和 allocateBootBudget 一致）：
      1. 每块先给保底 min，谁都不会被完全挤掉
      2. 剩余额度按「还有内容没发完」的块均分，夹到各自 max
      3. 短块用不完的额度自然退回池子（因为它已到 len，不再 hungry）
    """
    budget = budget or BLOCK_BUDGET
    total = total or TOTAL_BUDGET
    names = [k for k in budget if contents.get(k)]
    if not names:
        return {}

    alloc = {k: min(budget[k][0], len(contents[k])) for k in names}
    remain = max(0, total - sum(alloc.values()))

    while remain > 0:
        hungry = [
            k for k in names
            if alloc[k] < min(budget[k][1], len(contents[k]))
        ]
        if not hungry:
            break
        share = max(1, remain // len(hungry))
        progressed = False
        for k in hungry:
            if remain <= 0:
                break
            want = min(budget[k][1], len(contents[k])) - alloc[k]
            give = min(share, want, remain)
            if give > 0:
                alloc[k] += give
                remain -= give
                progressed = True
        if not progressed:
            break
    return alloc


def _clip(text: str, limit: int) -> str:
    """按字符裁剪（CJK 按字符算，不按字节 —— 按字节算中文会凭空大三倍）。"""
    t = str(text or "").strip()
    if limit <= 0:
        return ""
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 1)].rstrip() + "…"


_RECALL_NOISE_RE = re.compile(r"^(\[\d+\]\s*|⭐|〔已回〕|#\d+\s*)+")
# 群聊分段/规矩块这些「不是内容」的行，别喂给 BM25
_RECALL_SKIP_HEAD = ("（这一段群聊", "⚠️", "【必须回应", "带「〔引用", "不写 `<reply:")


def _recall_query(text: str, limit: int = 180) -> str:
    """把「给模型看的整段文本」压成一条干净的检索词。

    2026-11-24 用户：「最近很少主动去找记忆了」。
    根因之一：`recall_hits` 把整段（带 [2] ⭐#123 某人(123) → 你：… 的头）
    直接当 BM25 查询，噪音 token 把相关度摊薄 → 分数够不到阈值 → 0 命中。
    """
    raw = str(text or "")
    picked = []
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith(_RECALL_SKIP_HEAD):
            continue
        s2 = _RECALL_NOISE_RE.sub("", ln)
        # 去掉「某人(123) → 你：」这种前缀，只留内容
        m = re.match(r"^[^：:]{1,40}[：:]\s*(.+)$", s2)
        if m:
            s2 = m.group(1)
        picked.append(s2.strip())
    q = " ".join(picked[-3:])[:limit].strip()
    return q or raw[:limit]


# 召回统计（给 `/测函数 记忆` 用）
RECALL_STATS = {"calls": 0, "hit": 0, "fallback": 0}


def recall_stats_text() -> str:
    c = RECALL_STATS["calls"]
    h = RECALL_STATS["hit"]
    f = RECALL_STATS["fallback"]
    rate = (h / c * 100) if c else 0.0
    return (f"· 记忆召回：{c} 次｜关键词命中 {h} 次（{rate:.0f}%）｜"
            f"人物兜底 {f} 次")


def recall_hits(
    session_key: str = "",
    user_text: str = "",
    speaker_qq: str = "",
    *,
    limit: int = 5,
    max_age_days: float = 0,
) -> list:
    """
    只做记忆检索，不拼 prompt。

    为什么要单独抽出来（2026-11-17 审计）：
      llm.call_llm 需要「本地到底搜到几条」来决定要不要问图书馆，
      以前它用 `bool(packet.get("memory_index"))` 判断 —— 但那个块里
      只要有一行「当前说话人」就非空，于是图书馆几乎永远不触发。
      抽出来之后：先查一次 hits，再决定要不要查图书馆，最后才组装 packet，
      顺带把「组装两次 = BM25 跑两遍 + index 写两遍盘」也省掉。
    """
    try:
        hits = memory_v2.recall(
            query_for_recall(session_key, _recall_query(user_text)),
            limit=limit,
            speaker_qq=str(speaker_qq),
            max_age_days=max_age_days,
            gid=_gid_of(session_key),
        )
    except Exception:
        hits = []
    RECALL_STATS["calls"] = int(RECALL_STATS.get("calls") or 0) + 1
    if hits:
        RECALL_STATS["hit"] = int(RECALL_STATS.get("hit") or 0) + 1
    if not hits and speaker_qq:
        # 关键词没命中 ≠ 没有记忆：至少把「关于这个人的最近几条」带上，
        # 让她不至于像失忆一样（用户 2026-11-24）。
        try:
            hits = memory_v2.recent_about(
                str(speaker_qq), gid=_gid_of(session_key), limit=3)
            for _h in hits:
                _h["fallback"] = True
            if hits:
                RECALL_STATS["fallback"] = int(RECALL_STATS.get("fallback") or 0) + 1
        except Exception:
            hits = []
    return hits


def build_packet(
    *,
    session_key: str = "",
    user_text: str = "",
    speaker_qq: str = "",
    speaker_name: str = "",
    group_qqs=None,
    library_text: str = "",
    memory_limit: int = 5,
    max_age_days: float = 0,
    total_budget: int = 0,
    hits: list | None = None,
    record: bool = True,
    gid: str = "",
    addressed: bool = True,
    scan_text: str = "",
) -> dict:
    """
    组装「上下文数据包」：**分块 + 明确标签 + 按预算裁剪**。

    为什么分块：原来所有内容揉成一段塞进 system，模型分不清
    哪些是人格设定、哪些是对方档案、哪些是刚说的话、哪些是久远记忆。
    分块加标签后注意力更集中（这是这次架构调整的核心）。

    返回 {块名: 文本}，空块不出现。建议注入顺序：
        persona（外部给）→ profiles → bond → memory → library → mood
    其中 recent / my_reply 由调用方（llm.py）单独处理。
    """
    contents: dict[str, str] = {}
    # gid = 档案的「群视角」（私聊为空，表示用全局档案）
    # lore_scope = 词条命名空间（私聊也要有自己的词条 → private_QQ）
    gid = str(gid or _gid_of(session_key) or "")
    lore_scope = gid or (f"private_{speaker_qq}" if speaker_qq else "")

    # ① 当前说话人 —— **只给一行**（micro 卡）。
    #    用户 2026-11-11：「特别是和别人的事情以及档案，按需拉就好了」。
    #    完整的 brief 卡不再放这里，而是由 llm 插到「当前消息上方」（depth），
    #    离当前这句最近，注意力最集中（对齐 ST 的 at-depth 注入）。
    # 这一条会不会另外给「brief 人物卡」（llm 的 depth 区）？
    # 会给的话，这里就不要再塞 micro 卡 —— 同一个人被描述两遍（用户 2026-11-22：
    # 「模板有很多重复的」）。
    _give_focus = bool(speaker_qq) and (
        addressed or not str(session_key).startswith("group"))
    if speaker_qq and not _give_focus:
        try:
            _micro = profiles.render_card(str(speaker_qq), gid, detail="micro", max_len=70)
        except Exception:
            _micro = ""
        if _micro:
            try:
                _nm = profiles.call_of(str(speaker_qq), speaker_name, gid) or "他"
            except Exception:
                _nm = speaker_name or "他"
            _rel = ""
            try:
                _iv = profiles.intimacy(str(speaker_qq))
                if _iv >= 0.2:
                    _lb = profiles.intimacy_label(_iv)
                    _tip = ("可以更随意、偶尔损他两句" if _iv >= 0.7
                            else "熟人，说话可以放松些" if _iv >= 0.45
                            else "刚认识，客气一点")
                    _rel = f"\n（你俩关系：{_lb} {_iv:.2f} —— {_tip}）"
            except Exception:
                pass
            contents["profiles"] = (
                f"▍当前说话人\n· {_micro}{_rel}\n"
                f"（回复时用「{_nm}」称呼他；想看完整档案／你俩的过往／相关记忆 → "
                f"`[[PROFILE|{_nm}]]`）"
            )

    # ② 相处过往 + ③ 相关记忆 → 合成**一行索引 + 5 条记忆目录**，不展开全文。
    #    用户 2026-11-16：「每次重新开启对话，读 prompt + 上下文 + 一个简单的 5 条记忆目录」。
    #    原来只给「最相关的一条摘要」，信息太少；改成目录（编号+时间+心情+摘要）一眼扫完，
    #    想看全文再 [[MEMORY|关键词]] / [[PROFILE|名字]] 按需拉。
    _idx = []
    if speaker_qq:
        try:
            _nb = len(profiles.bond_texts(str(speaker_qq)) or [])
        except Exception:
            _nb = 0
        if _nb:
            _idx.append(f"你俩有 {_nb} 条相处记录")

    if hits is None:
        hits = recall_hits(
            session_key, user_text, speaker_qq,
            limit=memory_limit, max_age_days=max_age_days,
        )

    _mem_dir = []
    if hits:
        for i, h in enumerate(hits, 1):
            _mem_dir.append(
                f"{i}. {h.get('emotion_mark') or ''} {h.get('ago')}｜"
                f"{str(h.get('summary') or '')[:40]}"
            )

    if _idx or _mem_dir:
        _parts = []
        if _idx:
            _parts.append("\n".join(f"· {x}" for x in _idx))
        if _mem_dir:
            _fb = any(h.get("fallback") for h in hits)
            _title = ("关于这个人最近的事（关键词没命中，按时间给的）："
                      if _fb else
                      f"目录（{len(_mem_dir)} 条，越前越相关）：")
            _parts.append(_title + "\n" + "\n".join(_mem_dir))
        contents["memory_index"] = (
            "▍记忆目录（只给线索，内容按需拉）\n"
            + "\n".join(_parts)
            + "\n（想看全文 → `[[MEMORY|关键词]]`；想看某个人 → `[[PROFILE|名字]]`）"
        )

    # ④ 图书馆（算力机 Agent，30 天以前的往事，仅供参考）
    if library_text:
        contents["library"] = (
            "▍图书馆·久远的往事（来自长期归档，**仅供参考，可能记错**）\n"
            + str(library_text).strip()
        )

    # ⑤ 昨日总结 —— 短历史每天新开，这一块就是"交接"
    try:
        from . import daily_summary
        _gid = _gid_of(session_key) or str(session_key)
        _ys = daily_summary.get_yesterday_summary(_gid)
        if _ys:
            contents["yesterday"] = (
                "▍昨日总结（交接用，是昨天的事，不是现在）\n" + _ys
            )
    except Exception:
        pass

    # ⑦ 记忆回访：一条 7 天以前、很久没被想起的旧事。
    #    只给 1 条、只给线索，提不提由她决定 —— 塞多了就变成翻旧账。
    if os.getenv("MEMORY_REVISIT", "true").lower() == "true" and speaker_qq:
        try:
            _rv = memory_v2.revisit_candidate(str(speaker_qq))
        except Exception:
            _rv = None
        if _rv:
            contents["revisit"] = (
                "▍可以顺口提一句的旧事（不是任务，自然就提，不想提就不提）\n"
                f"· {_rv.get('emotion_mark') or ''} {_rv.get('ago')}：{_rv.get('summary')}"
                "\n（**只有跟现在的话题自然接得上才提**；接不上就当没看见 ——"
                " 不要为了提它而硬转话题。）"
            )
            # 标记已想起：不然接下来几轮会反复提同一件事
            try:
                memory_v2.touch(_rv["id"])
            except Exception:
                pass

    # ⑥ 心情 / 状态（按会话取：一个群里的情绪不该污染别的群和私聊）
    #    👇 2026-11-20：从「一句心情」升级成「状态块」——判读的心情词 + 数值 +
    #       她自己写的感觉 + 心里话 + 群聊总结，并明确告诉她「想不想安静/找谁」
    #       由她自己结合档案决定（不做硬阈值）。
    try:
        mp = (mood.status_block(session_key, speaker_qq=speaker_qq)
              or mood.mood_prompt(session_key))
    except Exception:
        mp = ""
    if mp:
        contents["mood"] = "▍现在的状态\n" + mp

    # ⑥.5 安静建议（参考）：好感/心情的**走势** + 群里插不插得上话。
    #     用户 2026-11-20：「建议是否安静、对谁安静、以及找谁」——只给信号，
    #     最后由角色扮演模型自己决定（这里不调用 chat_switch、不设硬阈值）。
    try:
        from . import quiet_advisor
        _qa = quiet_advisor.render(
            session_key, speaker_qq=speaker_qq, gid=gid,
            addressed=addressed, speaker_name=speaker_name)
    except Exception:
        _qa = ""
    if _qa:
        contents["quiet"] = "▍要不要安静（参考，不是命令）\n" + _qa

    # ⑧ Lorebook（群梗/人物词条）：只在关键词出现时注入，按 order + 预算裁剪。
    #    对齐 SillyTavern 的 World Info：提到才给，不给就完全不占 token。
    _meta_extra = {"lore_turn": 0, "lore_post": [], "focus_card": ""}
    if lore_scope:
        try:
            from . import lorebook
            _turn = (lorebook.begin_turn(lore_scope) if record
                     else lorebook.current_turn(lore_scope))
            _res = lorebook.scan(lore_scope, scan_text or user_text, _turn)
            _blk = lorebook.format_block(_res.get("before"))
            if _blk:
                contents["lore"] = _blk
            _meta_extra["lore_turn"] = _turn
            _meta_extra["lore_post"] = list(_res.get("post") or [])
        except Exception:
            pass

    # ⑨ 这一条在跟谁说话：brief 人物卡，由 llm 插到「当前消息上方」（depth）。
    #    群聊只在真的被 @ / 喊名字时给；私聊始终给。
    if speaker_qq and (addressed or not str(session_key).startswith("group")):
        try:
            _focus = profiles.render_card(str(speaker_qq), gid, detail="brief", max_len=260)
        except Exception:
            _focus = ""
        if _focus:
            _meta_extra["focus_card"] = _focus

    # ⑨.5 「你对他/她的底色」：只给当前说话人，模板生成、零额外调用。
    #      和 mood 块分工：底色=长期，此刻=这一句怎么说。
    if speaker_qq and (addressed or not str(session_key).startswith("group")):
        try:
            # 420：多了一行「印象最深刻的一件事」，320 会把它截掉
            _rel = profiles.relation_block(str(speaker_qq), gid, max_len=420)
        except Exception:
            _rel = ""
        if _rel:
            contents["relation"] = _rel

    # 预算裁剪
    alloc = allocate_budget(contents, total=total_budget)
    out = {}
    for k, v in contents.items():
        clipped = _clip(v, alloc.get(k, 0))
        if clipped:
            out[k] = clipped

    if record:
        record_query(session_key, user_text)

    # 给调用方（llm.py）判断「本地到底搜到几条」，用来决定要不要问图书馆。
    # 以前用 bool(packet["memory_index"]) 判断 —— 那个块里只要有一行「当前说话人」
    # 就非空，于是「本地没搜到 → 问图书馆」这条分支几乎永远走不到。
    out["_meta"] = {"memory_hits": len(hits or [])}
    out["_meta"].update(_meta_extra)
    return out
