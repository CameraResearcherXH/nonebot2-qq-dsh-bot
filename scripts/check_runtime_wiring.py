#!/usr/bin/env python3
"""运行时接线校验（证明「设计键真的改变行为」，不只是文档↔常量自证）。

为什么需要它（2026-11-27 外部审计「校验是自证」）：
  `check_design_docs` 只比 DESIGN.md ↔ `core/design_constants.py`，
  而后者是前者的只读镜像 → 常量写了 6000、运行时没接，它照样绿。
  这个脚本直接 import 运行时模块、构造最小输入、断言输出变化：
    ① long_doc 证据/图书馆块真的被放开到 6000/3000 字符；
    ② `±3` 每日计数真的生效（第一次 3、第二次降到 2、换会话重新计）；
    ③ 锚点软死区连续（0.149 / 0.151 不再跳变），小抖动位移可忽略；
    ④ P 回补只补不降；
    ⑤ relevance gate 不再把 `mood.observe` 整条挡掉（AST 证明）；
    ⑥ 整份 prompt 长度有 `prompt_total` 记账。

用法：`python3 scripts/check_runtime_wiring.py`（容器内/本地都行）
退出码：0 = 全过；1 = 有失败。
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
PASSES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASSES if ok else FAILS).append(name + (f"（{detail}）" if detail else ""))


def _fresh_mood(mod):
    """让每个 case 从干净状态开始（同进程内直接重置内存文件）。"""
    try:
        mod.MOOD_FILE.write_text('{"global": {"value": 0.0}, "sessions": {}}',
                                 encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="wiring-"))
    os.chdir(tmp)
    # 必须在 import services 之前设，mood 在模块级读 MOOD_FILE
    os.environ["MOOD_FILE"] = str(tmp / "mood.json")
    os.environ["MOOD_HISTORY"] = str(tmp / "mood_history.jsonl")
    os.environ["MOOD_JUDGE_TRACE"] = "0"

    from services import ctx_budget, mood, prompt_builder, long_doc

    # ---------- ① long_doc 预算（字符口径 + 真的进 prompt） ----------
    check("long_doc 证据预算 = 6000 字符",
          ctx_budget.limit_chars("long_doc_evidence") == 6000,
          str(ctx_budget.limit_chars("long_doc_evidence")))
    check("long_doc 图书馆预算 = 3000 字符",
          ctx_budget.limit_chars("long_doc_library") == 3000,
          str(ctx_budget.limit_chars("long_doc_library")))
    check("long_doc 篇数 × 单篇 ≤ 证据预算",
          long_doc.PIECES * long_doc.PIECE_CHARS <= 6000 + 300,
          f"{long_doc.PIECES}×{long_doc.PIECE_CHARS}")

    _hits = [{"summary": "短摘要" + str(i), "ago": "3天前",
              "text_full": ("证据正文%d-" % i) * 900} for i in range(1, 5)]
    _long = prompt_builder.build_packet(
        session_key="group_wire", user_text="详细讲讲这本书",
        hits=_hits, memory_limit=8, total_budget=12000,
        long_doc=True, record=False)
    _short = prompt_builder.build_packet(
        session_key="group_wire", user_text="在吗",
        hits=_hits, memory_limit=8, total_budget=0,
        long_doc=False, record=False)
    _lmem = len(str(_long.get("memory_index") or ""))
    _smem = len(str(_short.get("memory_index") or ""))
    check("long_doc 记忆块 > 常态 500 字上限", _lmem > 500, f"long={_lmem} short={_smem}")
    check("long_doc 记忆块 ≤ 6000 字符", _lmem <= 6000, str(_lmem))
    check("long_doc 真的带正文（不是 40 字目录）",
          "证据正文" in str(_long.get("memory_index") or ""),
          str(_long.get("memory_index") or "")[:40])
    check("常态记忆块仍是目录（≤500）", _smem <= 520, str(_smem))
    _lib = prompt_builder.build_packet(
        session_key="group_wire", user_text="详细讲讲这本书",
        hits=[], library_text="图书馆事实" * 1200, total_budget=12000,
        long_doc=True, record=False)
    _llib = len(str(_lib.get("library") or ""))
    check("long_doc 图书馆块 > 350 字且 ≤3000", 350 < _llib <= 3000, str(_llib))

    # ---------- ② ±3 每日计数 ----------
    _fresh_mood(mood)
    check("±3 第一次生效", mood.cap_daily_impact("group_cap", 3) == 3)
    check("±3 同日第二次降为 ±2", mood.cap_daily_impact("group_cap", 3) == 2)
    check("±3 负向同样降为 -2", mood.cap_daily_impact("group_cap", -3) == -2)
    check("±3 计数按会话隔离", mood.cap_daily_impact("group_other", 3) == 3)
    check("±2 不受日限", mood.cap_daily_impact("group_cap", 2) == 2)

    # ---------- ③ 锚点软死区：连续 + 小抖动可忽略 ----------
    def _move(v_pct: float) -> float:
        return mood._anchor_rec({"value": v_pct}, 1, 1.0, 0.0,
                                gain=mood.ANCHOR_WEIGHT)

    _inside = _move(25.0)       # diff=0.10 < 0.15
    _edge_in = _move(20.01)     # diff≈0.1499
    _edge_out = _move(20.0)     # diff=0.15（边界）
    _tiny = _move(34.0)         # diff=0.01
    check("锚点软死区内仍会挪（不是死分支）", _inside > 0, "%+.4f" % _inside)
    check("锚点边界连续（0.149 vs 0.151 差 <0.005）",
          abs(abs(_edge_in) - abs(_edge_out)) < 0.005,
          "%.4f vs %.4f" % (_edge_in, _edge_out))
    check("锚点小抖动可忽略（diff=0.01 位移 <0.002）", abs(_tiny) < 0.002,
          "%+.4f" % _tiny)

    # ---------- ④ P 回补只补不降 ----------
    check("P 回补只补不降（P0 > CAP）",
          mood.event_p_boost(0.25, 2) == 0.25, str(mood.event_p_boost(0.25, 2)))
    check("P 回补能从地板抬起",
          mood.event_p_boost(0.005, 2) > 0.005, str(mood.event_p_boost(0.005, 2)))

    # ---------- ⑤ relevance gate 不挡 mood.observe（AST） ----------
    src = (ROOT / "services" / "mood_judge.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    _observe_calls = 0
    _guarded = 0

    class _V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def generic_visit(self, node):
            self.stack.append(node)
            super().generic_visit(node)
            self.stack.pop()

        def visit_Call(self, node):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "observe":
                nonlocal _observe_calls, _guarded
                _observe_calls += 1
                for anc in self.stack:
                    if isinstance(anc, ast.If):
                        seg = ast.get_source_segment(src, anc.test) or ""
                        if "_skip_update" in seg:
                            _guarded += 1
                            break
            self.generic_visit(node)

    _V().visit(tree)
    check("mood.observe 有真实调用点", _observe_calls >= 1, str(_observe_calls))
    check("mood.observe 不被 _skip_update 整条挡住", _guarded == 0, str(_guarded))

    # ---------- ⑥ 整份 prompt 长度记账 ----------
    ctx_budget.STATS.pop("prompt_total", None)
    ctx_budget.observe_prompt_total(99999, source="wiring-test")
    _st = ctx_budget.STATS.get("prompt_total") or {}
    check("整份 prompt 有 prompt_total 记账", int(_st.get("n") or 0) >= 1, str(_st))
    check("整份 prompt 超 soft_cap 会记超限", int(_st.get("over") or 0) >= 1, str(_st))

    # ---------- ⑦ 日预算刹车（2026-11-27） ----------
    from services import budget_guard as bg
    _o_limit, _o_used, _o_eff = bg.limit, bg.used, bg.effective
    _o_file = bg.STATE_FILE
    import tempfile as _tf
    bg.STATE_FILE = _tf.NamedTemporaryFile(suffix=".json", delete=False).name
    bg.effective = lambda: True
    try:
        bg.limit = lambda: 100.0
        bg.used = lambda: 10
        check("预算 10% → ok", bg.state() == "ok", bg.state())
        bg.used = lambda: 95
        check("预算 95% → soft（先提醒，不硬停）", bg.state() == "soft", bg.state())
        bg.used = lambda: 100
        check("预算 100% → rest（不再调模型）", bg.state() == "rest", bg.state())
        check("rest 通知每会话每天只一次",
              bg.claim("rest", "group_x") is True
              and bg.claim("rest", "group_x") is False, "")
        check("另一个会话仍会收到通知", bg.claim("rest", "group_y") is True, "")
        check("rest 话术带用量、不空", "100" in bg.notice_line(), bg.notice_line()[:40])
        bg.limit = lambda: 0.0
        check("预算 0 → off（不刹车）", bg.state() == "off", bg.state())
    finally:
        bg.limit, bg.used, bg.effective = _o_limit, _o_used, _o_eff
        bg.STATE_FILE = _o_file
        try:
            import os as _os
            _os.unlink(bg.STATE_FILE)
        except Exception:
            pass

    # ---------- ⑧ 昵称零宽字符清洗（人称不串） ----------
    from services import group_scene as _gs
    _raw = "下个路口见\u2067\u2067喵\u2067\u2067"
    check("昵称零宽字符被清掉",
          _gs.clean_name(_raw) == "下个路口见喵", _gs.clean_name(_raw))

    # ---------- ⑨ 插话段裁短 + 昵称渲染（2026-11-27 复盘） ----------
    from services import segments as _seg, text as _text
    _recs = [{"name": "群友%d" % i, "uid": str(1000 + i), "text": "第%d条" % i,
              "mid": str(100000 + i), "to_kind": "group"} for i in range(12)]
    _n = sum(1 for l in _seg.format(_recs, limit=20).splitlines()
             if l.startswith("["))
    check("插话段只给最后 5 条（防接旧话题）", _n == 5, str(_n))
    _recs[2]["to_kind"] = "bot"
    _t2 = _seg.format(_recs, limit=20)
    check("有 ⭐ 的段不被裁（12 条且带 ⭐）",
          sum(1 for l in _t2.splitlines() if l.startswith("[")) == 12
          and "⭐" in _t2, "")
    check("昵称含人称代词会加「」",
          _text.render_name("我的床会长猫") == "「我的床会长猫」",
          _text.render_name("我的床会长猫"))
    check("普通昵称不加引号", _text.render_name("小明") == "小明",
          _text.render_name("小明"))

    # ---------- ⑩ 意图层（2026-11-27）：只加不减 + 闸门默认关 ----------
    from services import atmosphere as _atmo
    _old = _atmo._parse('{"mood":"低落","who":"月下橘","feel":"在哭",'
                        '"others":"在斗图","tip":"先安慰"}')
    check("意图层向后兼容：老 JSON 仍解析",
          _old.get("mood") == "低落" and not _atmo.need_reply_of(_old), str(_old))
    _new = _atmo._parse(json.dumps({
        "mood": "低落", "who": "月下橘", "feel": "在哭", "others": "在斗图",
        "tip": "先安慰", "topic": "锁帧数", "asks": "帧数重要吗",
        "need_reply": "YES", "why": "在问她", "replied": "no",
        "keywords": ["帧数", "90", "4060", "多余"],
        "queries": {"memory": "帧数设置", "library": "", "web": "RTX 4060 帧数"},
    }, ensure_ascii=False))
    check("need_reply 归一化（YES→yes）",
          _atmo.need_reply_of(_new) == "yes", _atmo.need_reply_of(_new))
    check("queries 只留非空三键",
          _atmo.queries_of(_new) == {"memory": "帧数设置", "web": "RTX 4060 帧数"},
          str(_atmo.queries_of(_new)))
    check("keywords 最多 3 个", len(_new.get("keywords") or []) == 3,
          str(_new.get("keywords")))
    _blk = _atmo.render(_new, 8)
    check("意图行真的进 prompt（在聊/该不该接）",
          "在聊" in _blk and "该不该接" in _blk, _blk[:40])
    check("气氛四行没被删（只加不减）",
          all(x in _blk for x in ("整体", "最需要被照顾", "其他人", "接话注意")), "")
    check("插话闸门默认关（ATMO_GATE=0）", _atmo.GATE is False, str(_atmo.GATE))
    # 8 行历史真的带〔已回〕：断言 read 把 replied_mids 传给了 history_lines
    _seen: dict = {}
    _orig_hl = _gs.history_lines

    def _fake_hl(*a, **k):
        _seen.update(k)
        return ["甲 → 大家: hi"]

    _gs.history_lines = _fake_hl
    try:
        _atmo._rows("1", replied_mids={"m1"})
    finally:
        _gs.history_lines = _orig_hl
    check("读气氛把 replied_mids 传给 history_lines（补〔已回〕）",
          _seen.get("replied_mids") == {"m1"}, str(_seen.get("replied_mids")))
    _big = "\n".join(f"[{i}] 群友{i}(10{i}) → 大家：闲聊第{i}条"
                     for i in range(20)).replace(
                         "群友3(103) → 大家", "群友3(103) → 你")
    _cmp = _atmo.compact_segment(_big)
    check("20 条段里的 ⭐ 不会被截掉（压缩后再给模型）",
          "群友3" in _cmp and len(_cmp) <= 800, f"{len(_cmp)} 字")
    _nick = ("[1] A(1) → 你已被管理员禁言6小时：给你一拳\n"
             "[2] B(2) → 大家：在吗\n[3] C(3) → 大家：嗯")
    check("昵称以「你」开头不算在叫她（→ 你 必须是完整收件人）",
          "其中 0 条在叫你" in _atmo.compact_segment(_nick),
          _atmo.compact_segment(_nick)[:30])
    # 闸门只能拿「同一窗口」的结论，窗口变了就不挡
    _atmo._CACHE["gate_test"] = {"at": _atmo.time.time(), "data": _new,
                                 "tail": ["甲 → 大家: a", "乙 → 大家: b"]}
    check("闸门只在窗口一致时生效",
          bool(_atmo.fresh("gate_test", tail=["甲 → 大家: a", "乙 → 大家: b"]))
          and not _atmo.fresh("gate_test", tail=["甲 → 大家: a", "丙 → 大家: c"]), "")
    _atmo._CACHE.pop("gate_test", None)

    # ---------- ⑫ send 工具不许把 <sticker:…> / ||| 当话发出去 ----------
    # 2026-11-27 18:03 真机（群 100000016）：群里原样出现
    #   `这群已经变成图片展览馆了 |||<sticker:吃瓜>`
    from services import aura_logic as _al
    _leak = "这群已经变成图片展览馆了 |||<sticker:吃瓜>"
    check("send 文本清洗：<sticker:> 与 ||| 不外发",
          _al.clean_outgoing(_leak) == "这群已经变成图片展览馆了",
          repr(_al.clean_outgoing(_leak)))
    check("表情标签能被摘出来（交给表情库真发）",
          _al.extract_stickers(_leak) == ["吃瓜"],
          str(_al.extract_stickers(_leak)))
    check("只有表情标签时不会被当成「协议标记」拦掉",
          _al.clean_outgoing("<sticker:吃瓜>") == ""
          and _al.extract_stickers("<sticker:吃瓜>") == ["吃瓜"], "")
    check("<voice> 内容保留、标签不外发",
          _al.clean_outgoing("<voice>你听我说</voice>") == "你听我说",
          repr(_al.clean_outgoing("<voice>你听我说</voice>")))

    # ---------- ⑪ 离线评测脚本本身能跑（不花钱） ----------
    sys.path.insert(0, str(ROOT / "scripts"))
    import eval_intent as _ei
    _rec = {
        "key": "group_123", "time": "2026-11-27 11:02:00",
        "reply": "帧数优先吧|||还费电",
        "msgs": [
            {"role": "system", "content":
                "【历史对话】共 4 条（由旧到新，**全是已经发生过的**）：\n"
                "1. aima → 大家: 锁90太难受了\n"
                "2. 格林 → 像是在回aima: 放月假了\n"
                "3. 格林 → 大家: （图片/表情）\n"
                "4. 青柠 → 大家: 必须狠狠撅！"},
            {"role": "user", "content":
                "（这一段群聊共 2 条，按时间顺序，最后一条最新。\n"
                "[1] 青柠(1001) → 大家：2点\n"
                "[2] ⭐#150203654 青柠(1001) → 你：你会不会唱歌"},
        ],
    }
    _ss = _ei.build_samples(_rec, neg_per_trace=2)
    check("离线评测：1 正例 + 2 负例",
          [s["label"] for s in _ss] == ["yes", "no", "no"],
          str([s["label"] for s in _ss]))
    check("离线评测：正例用整段（和线上 text 一致，含 ⭐ 行）",
          "2点" in _ss[0]["text"] and "你会不会唱歌" in _ss[0]["text"],
          _ss[0]["text"][:40])
    _sc = _ei.score([{**s, "pred": ("yes" if s["label"] == "yes" else "no")}
                     for s in _ss])
    check("离线评测：打分准确率 100%、正例召回 100%",
          _sc["acc"] == 1.0 and _sc["recall"] == 1.0, str(_sc))
    _sc2 = _ei.score([{**s, "pred": "no"} for s in _ss])
    check("离线评测：全判 no 时正例召回=0（闸门会闭嘴）",
          _sc2["recall"] == 0.0, str(_sc2["recall"]))

    print("=" * 66)
    print("运行时接线校验：%d PASS / %d FAIL" % (len(PASSES), len(FAILS)))
    for x in PASSES:
        print("  ✅ " + x)
    for x in FAILS:
        print("  ❌ " + x)
    print("=" * 66)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
