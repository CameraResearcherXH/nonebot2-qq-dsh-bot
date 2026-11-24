# services/reply_pipeline.py
"""回复前的「动态取数」流水线（2026-11-24 用户重构要求）。

用户原话：
  「默认固定拿到的内容只有 人设卡 + tool + 标签处理后的上下文 + 档案里你叫他什么。
   拿到要接话的那句消息之后：
     · 要用工具（唱歌等）→ 直接调工具；
     · 回复类 → 先取心情值/当前状态/那件事的心理状态 →
       对方档案 + 好感词 + 印象最深的一件事 →
       心情 LLM 对这段话的总结 → 用总结去查记忆 → 用总结去查图书馆
       （图书馆是语料库，里面的你我他不是现在的你我他，仅供参考）→
       看这条消息 id 是否已经回过 → 综合回复。」

设计：
  · 每一步都**独立降级**：超时/异常只丢那一步，不影响回复；
  · 总耗时预算 `PIPELINE_BUDGET`（默认 7s），到点就带着已拿到的部分走；
  · 产出是一段紧凑文本（`render()`），由 llm.call_llm 作为最高 recency 的数据块注入，
    同时主 prompt 不再重复注入 心情/好感/记忆目录/图书馆/白月光。
  · **每一块的第一行都是「▍块名｜≤30 字说明」**，方便 /测函数 模板 一眼对照；
  · 每一步都在 `stages` 里留状态（✓ / 数字 / — / ×异常），
    `/测函数 流水线` 直接能看到「到底哪一步没拿到」。
"""
from __future__ import annotations

import asyncio
import time

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

BUDGET = tuning.f("PIPELINE_BUDGET")
STATS: dict = {"runs": 0, "stage_ms": {}, "last": {}}

# 每一步的小标题（≤30 字，用户 2026-11-24：每块要有清晰总结，方便检查）
SUMMARY = {
    "称呼": "固定区已给，这里再钉一句",
    "状态": "心情值 · 当前状态 · 那件事的心理",
    "档案": "称呼 · 身份 · 好感 · 印象最深的事",
    "总结": "判读模型：这句在接哪句、到底在说什么",
    "记忆": "按这句话检索到的旧事",
    "图书馆": "久远归档，人称仅供参考",
    "已回": "这条回过没有，别回第二遍",
    "白月光": "借他习惯的说话方式，不做他",
}
# 打印顺序（也是 /测函数 流水线 的顺序）
STAGE_ORDER = ("称呼", "状态", "档案", "总结", "记忆", "图书馆", "已回", "白月光")
# 单独一个块标题：/看模板 里一眼能认出「这是流水线取回来的数据」，
# 而不是混在人格/规矩/历史里找（用户 2026-11-24）。
PIPE_HEADER = "▍PIPELINE｜回复前动态取数（优先参考，别复述块名；某步失败只丢那步）"


def _hdr(name: str) -> str:
    return f"▍{name}｜{SUMMARY.get(name, '')}".rstrip("｜")


_LIB_QUESTION_HINTS = (
    "？", "?", "吗", "呢", "谁", "什么", "怎么", "为什么", "为啥", "哪",
    "多少", "记得", "之前", "上次", "以前", "曾经", "说过", "知道",
    "是不是", "有没有", "还记得", "那个", "那件事", "后来",
)


def _want_library(text: str) -> bool:
    """这条消息值不值得花 5~14s 查图书馆（久远归档）。

    RAG 一次要 5~14s；对「哈哈哈」「好」这种没必要。只在像问事/提旧事时查。
    """
    t = str(text or "").strip()
    if len(t) < 4:
        return False
    if any(k in t for k in _LIB_QUESTION_HINTS):
        return True
    return len(t) >= 12


def _mark(name: str, t0: float) -> None:
    ms = (time.monotonic() - t0) * 1000
    d = STATS["stage_ms"].setdefault(name, {"n": 0, "ms": 0.0})
    d["n"] += 1
    d["ms"] = (d["ms"] * (d["n"] - 1) + ms) / d["n"]


async def _step(name: str, coro, timeout: float, default=None):
    """跑一步，超时/异常返回 default（并记录耗时）。"""
    t0 = time.monotonic()
    try:
        out = await asyncio.wait_for(coro, timeout=max(0.2, timeout))
        return out if out is not None else default
    except Exception as e:
        logger.debug(f"[PIPE] {name} 跳过（{type(e).__name__}: {e}）")
        return default
    finally:
        _mark(name, t0)


async def build(
    *,
    session_key: str,
    gid: str = "",
    text: str = "",
    speaker_qq: str = "",
    speaker_name: str = "",
    seg_mids=(),
    replied_mids=(),
    moon_target_qq: str = "",
) -> dict:
    """跑完整流水线，返回 {text, stages, summary, replied, cost}。"""
    t_all = time.monotonic()
    STATS["runs"] = int(STATS.get("runs") or 0) + 1
    blocks: list = []
    stages: dict = {}
    deadline = t_all + max(1.0, BUDGET)

    def left() -> float:
        return max(0.2, deadline - time.monotonic())

    def fail(name: str, e: BaseException) -> None:
        """失败也要留痕：不然「没拿到」和「没跑」看起来一样。"""
        stages[name] = f"×{type(e).__name__}"
        logger.debug(f"[PIPE] {name} 失败：{type(e).__name__}: {e}")

    # ---------- ① 称呼（固定区已经给了，这里补一句硬约定） ----------
    try:
        from . import callname
        _c = callname.render(speaker_qq, gid, speaker_name)
        if _c:
            blocks.append(_hdr("称呼") + "\n" + _c.strip())
            stages["称呼"] = "✓"
        else:
            stages["称呼"] = "—"
    except Exception as e:
        fail("称呼", e)

    # ---------- ② 心情值 + 当前状态 + 那件事的心理状态 ----------
    # 用 mood.status_block：它一次给全「心情词 / 数值 / 刚才那下 / 心里话 /
    # 峰值事件 + 当时的心理 / 群聊总结 / 对他的好感走势」。
    # 拿不到就退到最小的一行数值 —— 保证「此刻状态」这一块永远在。
    try:
        from . import mood
        _body = str(mood.status_block(session_key, speaker_qq=speaker_qq) or "").strip()
        # 去掉结尾那段 chat_switch 操作说明（固定区 ▍02b 已经有，别重复）
        _body = _body.split("\n（这些不用说出来")[0].strip()
        # 「刚刚群里在聊」prompt 末尾还会单独给一条【最近这段在聊什么】，
        # 这里去掉，避免同一段总结在一份 prompt 里出现两遍。
        _body = "\n".join(
            ln for ln in _body.splitlines()
            if not ln.strip().startswith("· 刚刚群里在聊")).strip()
        if not _body:
            _m = mood.get_mood(session_key) or {}
            _v = float(_m.get("value") or 0.0)
            _lb = str(_m.get("label") or "").strip()
            _body = f"· 心情值 {_v:+.0f}（-100~100，只影响语气浓淡）"
            if _lb:
                _body += f"\n· 她自己写的感觉：{_lb}"
        blocks.append(_hdr("状态") + "\n" + _body[:460])
        stages["状态"] = "✓"
    except Exception as e:
        fail("状态", e)
        # 连 mood 模块都挂了，也给她一句中性状态，别整块消失
        blocks.append(_hdr("状态") + "\n· 心情值 +0（暂时读不到，按平常语气说话）")

    # ---------- ③ 对方档案 + 好感词 + 印象最深的一件事 + 他的白月光 ----------
    try:
        from . import profiles
        try:
            from . import callname
            _nm = (callname.user_call_of(
                speaker_qq, gid, fallback=speaker_name or speaker_qq)
                or profiles.call_of(speaker_qq, speaker_name or speaker_qq, gid)
                or speaker_name)
        except Exception:
            _nm = (profiles.call_of(speaker_qq, speaker_name or speaker_qq, gid)
                   or speaker_name)
        _p = profiles.get_profile(speaker_qq) or {}
        _bond = ""
        try:
            _bond = profiles.get_bond_word(speaker_qq) or ""
        except Exception:
            _bond = ""
        _lines = [f"· 你叫他：{_nm}"]
        for _k, _t in (("identity", "他是谁"), ("personality", "性格"),
                       ("depth_note", "相处备注")):
            _v = str(_p.get(_k) or "").strip()
            if _v:
                _lines.append(f"· {_t}：{_v[:60]}")
        if _bond:
            _lines.append(f"· 你对他的好感：{_bond}")
        _top = ""
        try:
            from . import memory_v2
            _hits = memory_v2.recall(_nm or "他", limit=3,
                                     speaker_qq=speaker_qq, gid=gid) or []
            if _hits:
                _best = max(_hits, key=lambda h: float(h.get("salience") or 0))
                _top = f"{str(_best.get('summary') or '')[:40]}（{_best.get('ago')}）"
                _lines.append(f"· 你俩印象最深的一件事：{_top}")
        except Exception:
            pass
        # 🌙 白月光是谁：写进「这个人」块（用户 2026-11-24：
        #    「白月光是谁以及风格并没有写到档案里」）。
        _moon_who = ""
        try:
            from . import white_moon
            _wn = str(white_moon.profile_note(speaker_qq, gid) or "").strip()
            if _wn:
                _moon_who = _wn.replace("· 白月光：", "· 他的白月光：", 1)
                if "还没统计出来" in _moon_who:
                    _moon_who = "· 他的白月光：还没统计出来（他 @/引用谁最多就会记）"
                _lines.append(_moon_who)
        except Exception:
            pass
        blocks.append(_hdr("档案") + "\n" + "\n".join(_lines))
        stages["档案"] = "✓"
    except Exception as e:
        fail("档案", e)

    # ---------- ④ 总结 + 图书馆：并行启动 ----------
    summary = ""
    # ⚠️ 2026-11-24：图片/视频的 text 里带着完整视觉报告（[图片识别结果]…），
    # 再让总结 LLM 压一遍，会把「画面描述」重新塞回 prompt —— 她就会先描述图再回答。
    # 视觉分支自己已经有「不要复述画面」的最高 recency 规则；这里直接跳过总结。
    _txt_s = str(text or "")
    _is_media = (
        "[图片识别结果]" in _txt_s
        or "[视频解析结果]" in _txt_s
        or _txt_s.strip() in ("[图片]", "[表情]", "[视频]")
        or "【对方发了一个表情包" in _txt_s
    )
    # 图书馆（RAG v3）实测 5~14s：串在总结后面就必然超时（以前写死 2.5s，
    # 结果「图书馆=0」10/10）。这里先发出去，跟总结/记忆并行，最后再收。
    _lib_task = None
    try:
        from . import agent_client as _ac0
        _lib_seed = (f"{speaker_name or '对方'} 图片" if _is_media
                     else _txt_s.strip()[:120])
        # 只对「像在问事/提旧事」的消息查图书馆；纯插科打诨不花这 5~14s。
        if _want_library(_lib_seed):
            _lib_task = asyncio.create_task(
                _ac0.recall(_lib_seed, speaker_qq=speaker_qq, recent_ctx=[]))
    except Exception:
        _lib_task = None

    if _is_media:
        stages["总结"] = "—(媒体，不总结画面)"
    else:
        try:
            from . import mood_judge
            # 只喂当前一句会把「身高 192」猜成「192 关」——必须带上最近聊天，
            # 让判读先判断「这句在接哪句」。用户 2026-11-24 真机踩过。
            _ctx4 = []
            if gid:
                try:
                    from . import group_scene
                    _ctx4 = group_scene.history_lines(
                        gid, 10, width=140, exclude_mids=seg_mids)
                    # 审核 LLM 已经有一份「这段在聊什么」的滚动总结，先当背景给它，
                    # 避免只看到最后几条时把「192」这种数字接错话题。
                    _sm4 = (group_scene.snapshot(gid).get("summary") or {}).get("text") or ""
                    if _sm4:
                        _ctx4 = [f"（之前群里在聊：{str(_sm4)[:120]}）"] + _ctx4
                except Exception:
                    _ctx4 = []
            summary = await _step(
                "总结",
                mood_judge.summarize(text, session_key, _ctx4,
                                     speaker=speaker_name),
                min(6.0, left())) or ""
            if summary:
                blocks.append(_hdr("总结") + "\n· " + summary)
                stages["总结"] = "✓"
            else:
                stages["总结"] = "—"
        except Exception as e:
            fail("总结", e)
            summary = ""

    # 总结失败就用原话当检索词；媒体消息不要拿整段视觉报告去检索，
    # 否则记忆/图书馆块里又会出现画面细节。
    if _is_media:
        query = f"{speaker_name or '对方'} 图片"
    else:
        query = summary or str(text or "")[:120]

    # ---------- ⑤⑥ 记忆 + 图书馆（并行，互不等待） ----------
    async def _mem_stage():
        try:
            from . import memory_v2
            _hits = await _step(
                "记忆",
                asyncio.to_thread(memory_v2.recall, query, limit=4,
                                  speaker_qq=speaker_qq, gid=gid),
                min(3.0, left()), default=[]) or []
            if not _hits:
                stages["记忆"] = "0"
                return ""
            _lines = [f"{i}. {str(h.get('ago') or '')}｜{str(h.get('summary') or '')[:40]}"
                      for i, h in enumerate(_hits, 1)]
            stages["记忆"] = str(len(_hits))
            return _hdr("记忆") + "\n" + "\n".join(_lines)
        except Exception as e:
            fail("记忆", e)
            return ""

    async def _lib_stage():
        try:
            if _lib_task is None:
                stages["图书馆"] = "—(不是问事，不查)"
                return ""
            # 任务已经在 ④ 之前发出去了，这里只等结果：最多 14s（RAG 实测 5~14s）。
            # 不跟 left() 抢预算：它和总结/记忆是并行的，不是串行叠加。
            _lib = await _step("图书馆", _lib_task, 15.0, default=None)
            _facts = (_lib or {}).get("facts") or []
            if not _facts:
                stages["图书馆"] = "0"
                return ""
            stages["图书馆"] = str(len(_facts))
            return (_hdr("图书馆") + "\n"
                    "（**语料库里的「我/你/他」不是现在的你我他**，只当参考线索，"
                    "别照抄、别当成现在发生的事）\n"
                    + "\n".join(f"· {str(x)[:100]}" for x in _facts[:3]))
        except Exception as e:
            fail("图书馆", e)
            return ""

    _got = await asyncio.gather(_mem_stage(), _lib_stage())
    blocks.extend([b for b in _got if b])

    # ---------- ⑦ 这条消息回过没有 ----------
    replied = False
    try:
        _mid = str(seg_mids[0] if seg_mids else "")
        # ⚠️ 2026-11-24：worker 为了防重投，在进流水线**之前**就把当前 mid
        # 登记进 `_REPLIED_MIDS` 了。这里必须把「本段 mid」排掉，否则每条新消息
        # 都会被判成「已经回过」，模型只会补一句、不敢正常回。
        _rs = ({str(x) for x in (replied_mids or [])}
               - {str(x) for x in (seg_mids or [])})
        replied = bool(_mid and _mid in _rs)
        if replied:
            blocks.append(_hdr("已回") + "\n· 这条消息你已经回过了，**别再回一遍**；"
                          "如果确实还要补充，就只补一句新的。")
            stages["已回"] = "是"
        else:
            stages["已回"] = "否"
    except Exception as e:
        fail("已回", e)

    # ---------- ⑧ 白月光说话风格 ----------
    # 「是谁」已经在 ③ 档案块里；这里只给「怎么借他的说话方式」，避免同一件事说两遍。
    try:
        from . import white_moon
        if not getattr(white_moon, "ENABLED", True):
            stages["白月光"] = "关"
        else:
            _tq = str(moon_target_qq or speaker_qq or "").strip()
            _m = white_moon.person_moon(gid, _tq) if (_tq and gid) else {}
            if not _m:
                stages["白月光"] = "—(分不够)"
            else:
                _wm = white_moon.render_style_only(gid, _tq, speaker_name)
                if _wm:
                    # 自定义风格可能是一整套样本，给足 900 字；普通风格摘要很短
                    blocks.append(_wm.strip()[:900])
                    stages["白月光"] = "✓"
                else:
                    stages["白月光"] = "—(没样本)"
    except Exception as e:
        fail("白月光", e)

    _body = "\n\n".join(b for b in blocks if b).strip()
    text_out = (PIPE_HEADER + ("\n\n" + _body if _body else "")).strip()
    cost = time.monotonic() - t_all
    STATS["last"] = {"stages": dict(stages), "cost": round(cost, 2),
                     "chars": len(text_out), "text": text_out,
                     "session": str(session_key or ""),
                     "speaker_qq": str(speaker_qq or ""),
                     "speaker_name": str(speaker_name or ""),
                     "src_text": str(text or "")[:120]}
    logger.info(
        f"[PIPE] {session_key} {cost:.1f}s｜拉了 {','.join(k for k in STAGE_ORDER if k in stages) or '无'}"
        f"｜{len(text_out)} 字｜{stages}")
    return {"text": text_out, "stages": stages, "summary": summary,
            "replied": replied, "cost": cost}


def last_text() -> str:
    """上一次流水线真正注入 prompt 的完整文本（给 /看模板 流水线 用）。"""
    return str((STATS.get("last") or {}).get("text") or "")


def dump_text() -> str:
    """把「状态 + 完整块」打在一起；单独一个标签，不和模板刷屏混在一起。"""
    last = STATS.get("last") or {}
    if not last:
        return "· 流水线：还没跑过（先让她回一句）"
    lines = [PIPE_HEADER]
    _st = last.get("stages") or {}
    _who = str(last.get("speaker_name") or "").strip()
    _qq = str(last.get("speaker_qq") or "").strip()
    _sess = str(last.get("session") or "").strip()
    if _who or _qq:
        lines.append(f"· 这次是回：{_who or '?'}（{_qq or '?'}）"
                     + (f"｜会话 {_sess}" if _sess else ""))
    _src = str(last.get("src_text") or "").strip().replace("\n", " ")
    if _src:
        lines.append(f"· 要回的原话：{_src[:80]}")
    lines.append(f"· 上次耗时 {last.get('cost')}s｜{last.get('chars')} 字")
    lines.append("· 各步：" + "｜".join(
        f"{k}={_st.get(k, '（没跑）')}" for k in STAGE_ORDER))
    body = str(last.get("text") or "").strip()
    if body.startswith(PIPE_HEADER):
        body = body[len(PIPE_HEADER):].strip()
    if body:
        lines.append("")
        lines.append(body)
    lines.append("")
    lines.append("（这是「上一条要回的消息」的取数，不是 /档案；"
                 "查某个人请用 /档案 @人 文字 或 /测函数 档案 QQ号）")
    return "\n".join(lines)


def stats_text() -> str:
    """`/测函数 流水线`：每一步的耗时 + 上次到底拿到了什么。"""
    if not STATS["runs"]:
        return "· 流水线：还没跑过"
    parts = [f"· 流水线：跑了 {STATS['runs']} 次"]
    for k, v in sorted(STATS["stage_ms"].items(), key=lambda kv: -kv[1]["ms"]):
        parts.append(f"    {k}: {v['ms']:.0f}ms（{v['n']} 次）")
    last = STATS.get("last") or {}
    if last:
        _st = last.get("stages") or {}
        parts.append(f"· 上次：{last.get('cost')}s｜{last.get('chars')} 字")
        for k in STAGE_ORDER:
            parts.append(f"    {k}: {_st.get(k, '（没跑）')}")
    return "\n".join(parts)
