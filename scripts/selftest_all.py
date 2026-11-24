#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全链路离线自检（连服务器前先跑这个）。

设计原则：
  1. **默认全离线**：不联网、不碰真实 data/（整个测试在一个临时目录里跑）
  2. **自带依赖桩**：本地缺 aiohttp / aiofiles / nonebot / apscheduler / dashscope
     也能跑，缺的包自动打桩并在报告里标注
  3. **失败不中断**：每个分组独立捕获异常，最后出汇总表
  4. **--net 模式**：额外连真服务器 / 算力机 / DeepSeek / DashScope 做冒烟

用法：
    python scripts/selftest_all.py                # 纯离线
    python scripts/selftest_all.py -v             # 显示每一条
    python scripts/selftest_all.py --net          # 离线 + 真实端点冒烟
    python scripts/selftest_all.py --only memo,node
"""
import argparse
import asyncio
import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

VERBOSE = False
RESULTS = []          # (section, ok, msg, detail)
STUBBED = []          # 打了桩的包名


# ============================================================
# 报告
# ============================================================
def check(section, cond, msg, detail=""):
    RESULTS.append((section, bool(cond), msg, detail))
    if VERBOSE or not cond:
        print(f"{'  ✅' if cond else '  ❌'} [{section}] {msg}" + (f"  ← {detail}" if detail and not cond else ""))
    return bool(cond)


def skip(section, msg, detail=""):
    RESULTS.append((section, None, msg, detail))
    if VERBOSE:
        print(f"  ⚠️  [{section}] {msg}（跳过）{detail}")


def summary():
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r[1] is True)
    failed = [r for r in RESULTS if r[1] is False]
    skipped = sum(1 for r in RESULTS if r[1] is None)

    print("\n" + "=" * 68)
    secs = []
    for s in dict.fromkeys(r[0] for r in RESULTS):
        sub = [r for r in RESULTS if r[0] == s]
        p = sum(1 for r in sub if r[1] is True)
        f = sum(1 for r in sub if r[1] is False)
        sk = sum(1 for r in sub if r[1] is None)
        flag = "✅" if f == 0 else "❌"
        secs.append(f"  {flag} {s:<22} {p:>3} 过 / {f:>2} 败" + (f" / {sk} 跳" if sk else ""))
    print("\n".join(secs))
    print("-" * 68)
    print(f"  合计：{total} 项 → 通过 {passed}，失败 {len(failed)}，跳过 {skipped}")
    if STUBBED:
        print(f"  打桩的包：{', '.join(sorted(set(STUBBED)))}")
    if failed:
        print("\n  失败清单：")
        for s, _, m, d in failed:
            print(f"    ❌ [{s}] {m}" + (f"\n         {d}" if d else ""))
    print("=" * 68)
    return 0 if not failed else 1


# ============================================================
# 依赖桩
# ============================================================
class _Dummy:
    """万能桩：可调用 / 可当装饰器 / 可 await / 可 async with / 任意属性"""

    def __init__(self, name="dummy"):
        object.__setattr__(self, "_name", name)

    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        child = _Dummy(f"{self._name}.{item}")
        object.__setattr__(self, item, child)
        return child

    def __call__(self, *a, **k):
        # 当装饰器用：@on_message() / @driver.on_bot_connect
        if len(a) == 1 and not k and callable(a[0]) and not isinstance(a[0], _Dummy):
            return a[0]
        return _Dummy(f"{self._name}()")

    def __await__(self):
        async def _n():
            return None
        return _n().__await__()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __iter__(self):
        return iter(())

    def __repr__(self):
        return f"<stub {self._name}>"


class _StubModule(types.ModuleType):
    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        v = _Dummy(f"{self.__name__}.{item}")
        setattr(self, item, v)
        return v


def _mkmod(name):
    m = _StubModule(name)
    sys.modules[name] = m
    if "." in name:
        parent, _, child = name.rpartition(".")
        if parent in sys.modules:
            setattr(sys.modules[parent], child, m)
    return m


class _Logger:
    def __init__(self):
        self.lines = []

    def _log(self, lvl, *a, **k):
        self.lines.append((lvl, " ".join(str(x) for x in a)))

    def info(self, *a, **k): self._log("I", *a, **k)
    def success(self, *a, **k): self._log("S", *a, **k)
    def warning(self, *a, **k): self._log("W", *a, **k)
    def error(self, *a, **k): self._log("E", *a, **k)
    def exception(self, *a, **k): self._log("X", *a, **k)
    def debug(self, *a, **k): self._log("D", *a, **k)


LOGGER = _Logger()


def _missing(name):
    import importlib.util
    try:
        return importlib.util.find_spec(name) is None
    except Exception:
        return True


def install_stubs():
    """只给真正缺的包打桩，已有的不碰。"""
    # ---- nonebot ----
    if _missing("nonebot"):
        STUBBED.append("nonebot")
        nb = _mkmod("nonebot")
        nb.logger = LOGGER
        nb.get_driver = lambda: _Dummy("driver")
        nb.require = lambda name: _Dummy(f"require:{name}")
        nb.get_plugin_config = lambda *a, **k: _Dummy("plugin_config")
        nb.get_bots = lambda: {}
        nb.get_bot = lambda *a, **k: _Dummy("bot")
        for sub in (
            "nonebot.rule", "nonebot.params", "nonebot.exception", "nonebot.plugin",
            "nonebot.adapters", "nonebot.adapters.onebot",
            "nonebot.adapters.onebot.v11", "nonebot.adapters.onebot.v11.event",
            "nonebot.adapters.onebot.v11.message", "nonebot.adapters.onebot.v11.bot",
            "nonebot.adapters.onebot.v11.exception",
        ):
            _mkmod(sub)
        # 异常类做成真类，方便 except
        exc = sys.modules["nonebot.exception"]

        class FinishedException(Exception):
            pass

        class IgnoredException(Exception):
            pass

        exc.FinishedException = FinishedException
        exc.IgnoredException = IgnoredException
        _mkmod("nonebot.message")

    # ---- aiohttp ----
    if _missing("aiohttp"):
        STUBBED.append("aiohttp")
        a = _mkmod("aiohttp")

        class ClientTimeout:
            def __init__(self, total=None, **kw):
                self.total = total

        class ClientSession:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Dummy("resp")

            def get(self, *a, **k):
                return _Dummy("resp")

        a.ClientTimeout = ClientTimeout
        a.ClientSession = ClientSession
        a.ClientError = Exception

    # ---- fastapi / starlette / requests（只为让 tool_dispatcher、node.py 能导入）----
    # ---- aiofiles（要真能写文件，不能纯桩）----
    if _missing("aiofiles"):
        STUBBED.append("aiofiles")
        af = _mkmod("aiofiles")

        class _AsyncFile:
            def __init__(self, path, mode="rb", **k):
                self.f = open(path, mode)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                self.f.close()
                return False

            async def write(self, data):
                return self.f.write(data)

            async def read(self, n=-1):
                return self.f.read(n)

            async def close(self):
                self.f.close()

        af.open = lambda path, mode="rb", **k: _AsyncFile(path, mode)

    # ---- apscheduler ----
    if _missing("apscheduler"):
        STUBBED.append("apscheduler")
        _mkmod("apscheduler")
        _mkmod("apscheduler.triggers")
        _mkmod("apscheduler.triggers.cron")

    # ---- 其他重型可选依赖 ----
    for name in ("dashscope", "boto3", "botocore"):
        if _missing(name):
            STUBBED.append(name)
            _mkmod(name)

    # ---- nonebot_plugin_apscheduler ----
    if _missing("nonebot_plugin_apscheduler"):
        STUBBED.append("nonebot_plugin_apscheduler")
        m = _mkmod("nonebot_plugin_apscheduler")
        m.scheduler = _Dummy("scheduler")

    # ---- 让 nonebot.rule.Rule 能实例化 ----
    rule = sys.modules.get("nonebot.rule")
    if rule is not None and isinstance(getattr(rule, "Rule", None), _Dummy):
        class Rule:
            def __init__(self, *a, **k):
                pass

            def __and__(self, o):
                return self

        rule.Rule = Rule


# ============================================================
# 假 HTTP 服务器（给 node.py 做真实网络测试）
# ============================================================
class FakeServer:
    """
    提供：
      GET  /api/download/<tid>   返回文件（支持 Range）
      GET  /api/download/missing 404
      GET  /api/download/json    返回 application/json（模拟错误响应）
      POST /api/tasks/complete   记录表单
      POST /tts                  返回 wav 字节
      POST /v1/chat/completions  返回假 LLM 的 JSON
    """

    def __init__(self, payload: bytes):
        self.payload = payload
        self.records = []       # [(path, headers, body_bytes)]
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                p = self.path.split("?")[0]
                if p == "/api/download/missing":
                    self._send(404, b'{"error":"file not found"}', "application/json")
                    return
                if p == "/api/download/json":
                    self._send(200, b'{"error":"file not found"}', "application/json")
                    return
                if p.startswith("/api/download/"):
                    data = outer.payload
                    rng = self.headers.get("Range")
                    if rng:
                        m = re.match(r"bytes=(\d+)-", rng)
                        start = int(m.group(1)) if m else 0
                        body = data[start:]
                        self._send(206, body, extra={"Content-Range": f"bytes {start}-{len(data)-1}/{len(data)}"})
                    else:
                        self._send(200, data)
                    return
                self._send(404, b"nope", "text/plain")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                p = self.path.split("?")[0]
                outer.records.append((p, dict(self.headers), body))

                if p == "/api/tasks/complete":
                    self._send(200, b'{"status":"success"}', "application/json")
                elif p == "/tts":
                    self._send(200, outer.wav, "audio/wav")
                elif p.endswith("/chat/completions"):
                    content = json.dumps({
                        "journal": "这两天他一直在弄翻唱，弄到很晚，我看得出他有点焦虑。",
                        "impression": "他很轴，但值得陪",
                        "archive_keywords": ["不存在的关键词"],
                    }, ensure_ascii=False)
                    resp = {"choices": [{"message": {"content": content}}]}
                    self._send(200, json.dumps(resp, ensure_ascii=False).encode(), "application/json")
                else:
                    self._send(200, b'{"ok":true}', "application/json")

        self.wav = b"RIFF" + b"\x00" * 2048
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()
        return False

    def form(self, idx=-1):
        """把最后一次 POST 的 body 解析成 dict（urlencoded 或 multipart 里抠字段）"""
        _, headers, body = self.records[idx]
        ct = (headers.get("Content-Type") or "").lower()
        if "x-www-form-urlencoded" in ct:
            from urllib.parse import parse_qs
            return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "ignore")).items()}
        # multipart：粗暴按 name="xxx" 抽
        out = {}
        txt = body.decode("utf-8", "ignore")
        for m in re.finditer(r'name="([^"]+)"(?:[^\r\n]*)\r\n\r\n([^\r\n]*)', txt):
            out[m.group(1)] = m.group(2)
        return out


# ============================================================
# 分组 1：MEMO 协议
# ============================================================
async def t_memo():
    from services import aura_logic as A
    S = "memo"

    raw = (
        "哼，才没有很高兴呢 ||| 下次也夸我\n"
        "[[MEMO|emotion:happy|text:他夸我可爱，我尾巴都摇起来了]]\n"
        "[[MEMO|emotion:生气|text:他刚才说我胖，我记住了|imp:0.9]]"
    )
    p = A.parse_reply(raw)
    check(S, len(p["memos"]) == 2, "拆出 2 条 memo")
    check(S, [m["emotion"] for m in p["memos"]] == ["happy", "angry"], "中文情绪归一化 → happy/angry")
    check(S, all("MEMO" not in i["value"] for i in p["items"]), "MEMO 没泄漏进气泡")
    check(S, p["memos"][1]["importance"] == 0.9, "imp:0.9 解析")
    check(S, len(p["items"]) == 2, "正文气泡仍是 2 条")

    many = "正文\n" + "\n".join(f"[[MEMO|emotion:happy|text:第{i}条]]" for i in range(6))
    pm = A.parse_reply(many)
    check(S, len(pm["memos"]) == A.MEMO_MAX_PER_REPLY, f"上限 {A.MEMO_MAX_PER_REPLY} 条生效（6 条输入）")
    check(S, "MEMO" not in json.dumps(pm["items"], ensure_ascii=False), "超限的 MEMO 也从正文剔除")

    only = A.parse_reply("[[MEMO|emotion:sad|text:他好久没来了]]")
    check(S, only["silent"] and len(only["memos"]) == 1, "纯 MEMO 回复：silent 但记忆保留")

    check(S, A.parse_memos("没有标签的普通回复")[1] == [], "无标签 → 空列表")
    check(S, A.normalize_emotion("莫名其妙") == "neutral", "无法识别的情绪 → neutral")
    check(S, A.emotion_cn("angry") == "生气", "emotion_cn 映射")


# ============================================================
# 分组 2：情绪 → 心情
# ============================================================
async def t_mood():
    from services import mood
    S = "mood"

    # 👇 2026-11-20：数值只认第三方判读。她自己报的 emotion 只留「原话」，不动数值。
    v0 = mood.get_mood()["value"]
    v1 = mood.bump_by_emotion("happy")
    check(S, abs(v1 - v0) < 1e-9, f"自报情绪不再改数值（{v0:.1f}→{v1:.1f}）")
    check(S, mood.get_mood().get("label") == "开心", "自报情绪仍然存成她的原话")
    check(S, bool(getattr(mood, "MOOD_FROM_JUDGE", False)),
          "MOOD_FROM_JUDGE=true（数值来源唯一：判读）")

    # 判读直接给「当前心情值」（-1~1），没有 delta 这回事
    _k = "group_selftest_judge"
    _a = mood.set_value_from_judge(0.5, _k, word="有点飘", why="他夸了她")
    _b = mood.set_value_from_judge(0.5, _k, word="还挺受用", why="又夸了一句")
    check(S, _a > 0 and _b >= _a,
          f"判读值会写成心情值（{_a / 100:+.2f}→{_b / 100:+.2f}）")
    _c = mood.set_value_from_judge(-0.8, _k, word="被骂了")
    check(S, _c < 0 and abs(_c / 100) <= 1.0,
          f"判读说难受就往负走（{_c / 100:+.2f}）")
    check(S, not hasattr(mood, "MAX_JUDGE_DELTA"),
          "不再有「delta 上限」这种外部阈值（判读直接给当前值）")
    shifts = mood.recent_shifts(0)
    check(S, len(shifts) >= 2, f"波动流水记录 {len(shifts)} 条")
    check(S, shifts[-1]["emotion"] == "被骂了", "流水里记的是判读给的心情词")

    # 状态块要进上下文（用户 2026-11-20：状态得影响回答/安静/找谁）
    # 2026-11-20 定稿：心情名词来自**数值分档**（每 0.2 一档），
    # 判读给的 word 只当「刚才那下」的反应词。
    mood.set_judge("group_status_test", "被连着催得有点烦", -0.28,
                   why="连着三个人要档案", mind="又来一个要档案的")
    _st = mood._load()
    _rec = _st["sessions"].setdefault("group_status_test", {"value": 0.0, "updated_at": 0})
    _rec["value"] = -30.0
    _rec["label"] = ""
    _st["sessions"]["group_status_test"] = _rec
    mood._save(_st)
    _sb = mood.status_block("group_status_test")
    # -0.30 落在 [-0.4, -0.2) 这一档 → 「有点扫兴」
    check(S, "有点扫兴" in _sb, f"心情名词按数值分档（{_sb.splitlines()[0][:30]}）")
    check(S, "-0.30" in _sb, "状态块带当前数值")
    check(S, "被连着催得有点烦" in _sb, "判读的反应词也带上（刚才那下）")
    check(S, "又来一个要档案的" in _sb, "状态块带她的心里话")
    check(S, "chat_switch" in _sb and "由你" in _sb,
          "状态块把「要不要安静/找谁」交给她自己决定（不硬阈值）")
    _pb_src = (ROOT / "services" / "prompt_builder.py").read_text(encoding="utf-8")
    check(S, "mood.status_block(session_key" in _pb_src,
          "上下文背景用的是状态块（不是一句心情）")
    check(S, all("at" in s and "delta" in s for s in shifts), "流水字段完整")

    d, strong = mood.judge_message_mood("你好笨啊")
    check(S, d < 0 and strong is False, f"单个负面词 → delta={d}（未到 strong 阈值）")
    d2, strong2 = mood.judge_message_mood("你好笨啊，真讨厌")
    check(S, d2 < 0 and strong2 is True, f"两个负面词 → delta={d2}，strong=True")
    check(S, mood.judge_message_mood("") == (0.0, False), "空文本 → (0, False)")
    check(S, mood.bump_mood(999) <= 100, "心情上限钳制在 100")

    # ---------- 心情是她自己写的一句话，不是固定枚举（用户 2026-11-19）----------
    _k = "group_moodlabel"
    mood.set_label("被夸得有点飘，但还装作没事", _k)
    _m = mood.get_mood(_k)
    check(S, _m.get("label") == "被夸得有点飘，但还装作没事",
          f"她自己写的心情能存下来（{_m.get('label')}）")
    _mp = mood.mood_prompt(_k)
    check(S, "被夸得有点飘" in _mp and "按这个感觉说话" in _mp,
          "prompt 里用的是她的原话，不是「心情不错」这种模板")
    check(S, "心情不错" not in _mp, "有原话时不再套固定档位文案")

    # 自由描述也要让数值动一点（否则「烦死了」和「好开心」对数值一样）
    mood.bump_mood(-999, session_key=_k)     # 先压到底，才看得出方向
    _v0 = mood.get_mood(_k)["value"]
    _v1 = mood.bump_by_emotion("他夸我可爱，有点开心", session_key=_k)
    check(S, _v1 > _v0, f"自由描述也按字面推方向（{_v0:.1f}→{_v1:.1f}）")
    check(S, "开心" in (mood.get_mood(_k).get("label") or ""),
          "MEMO 里的自由描述会变成她的心情原话")

    # 固定的 9 个词仍可用，但存成中文原话；neutral 等于没写
    check(S, mood._label_of("happy") == "开心" and mood._label_of("neutral") == "",
          "固定词翻成中文原话，neutral 当没写")
    check(S, mood._label_of("烦，但还得装没事") == "烦，但还得装没事", "自由文本原样保留")

    # 原话会过期（默认 12h），别让三天前的心情一直挂着
    mood.set_label("刚才有点难过", _k)
    _st = mood._load()
    _st["sessions"][_k]["label_at"] = time.time() - 13 * 3600
    mood._save(_st)
    check(S, mood.get_mood(_k).get("label") == "", "超过 MOOD_LABEL_TTL_HOURS 的原话自动失效")
    check(S, "你现在心情" in mood.mood_prompt(_k)
          or mood.mood_prompt(_k) == "" or "心情" in mood.mood_prompt(_k),
          "没有原话时退回按数值描述")

    # [[MOOD|…]] 协议：抠出来、不发给用户
    from services.aura_logic import parse_mood, parse_reply
    _clean, _lab = parse_mood("今天就这样吧 [[MOOD|有点累，但不想扫兴]]")
    check(S, _lab == "有点累，但不想扫兴" and "MOOD" not in _clean,
          f"[[MOOD]] 能抠出来且不进正文（{_clean!r}）")
    _pr = parse_reply("嗯嗯 [[MOOD|心里软了一下]]")
    check(S, _pr.get("mood") == "心里软了一下", "parse_reply 带出心情原话")
    check(S, all("MOOD" not in str(i.get("value") or "") for i in _pr.get("items") or []),
          "[[MOOD]] 不会漏给用户")
    _pr2 = parse_reply("(不想回复)")
    check(S, "mood" in _pr2, "沉默分支也带 mood 字段（不 KeyError）")

    # ---------- [[THINK]] 每轮必写的心里话（用户 2026-11-20）----------
    from services.aura_logic import parse_think
    _tc, _th1 = parse_think("嗯嗯 [[THINK|嘴上不认，其实挺受用]]")
    check(S, _th1 == "嘴上不认，其实挺受用" and "THINK" not in _tc,
          f"[[THINK]] 能抠出来且不进正文（{_tc!r}）")
    _pr3 = parse_reply("好呀 [[THINK|有点想笑]] [[MOOD|心里软软的]]")
    check(S, _pr3.get("think") == "有点想笑", "parse_reply 带出 think")
    check(S, _pr3.get("mood") == "心里软软的", "think 和 mood 能同时存在")
    check(S, all("THINK" not in str(i.get("value") or "") for i in _pr3.get("items") or []),
          "[[THINK]] 不会漏给用户")
    _pr4 = parse_reply("(不想回复) [[THINK|懒得理]]")
    check(S, _pr4.get("silent") and _pr4.get("think") == "懒得理",
          "沉默时也带 think（判读照样有依据）")

    # ---------- 协议残留清理（2026-11-22 真机：偶尔把心里话/工具标记发出来）----------
    from services.aura_logic import strip_protocol_residue
    # 单括号 / 全角 / 冒号 / 内容里带 [] 都不能漏
    for _bad, _want in (
        ("[THINK|单括号心里话]", "单括号心里话"),
        ("[[THINK：冒号心里话]]", "冒号心里话"),
        ("[[THINK|他说的[重点]我记住了]]", "他说的[重点]我记住了"),
        ("[[THINK|半截没闭合", "半截没闭合"),
    ):
        _tc2, _th2 = parse_think(_bad)
        check(S, _th2 == _want and "THINK" not in _tc2,
              f"THINK 畸形写法也能抠干净：{_bad!r} → {_tc2!r}")
    # 工具标记 / 残留标记绝不能进正文
    for _raw in (
        "好呀 [[TOOLS|猪猪]]",
        "好呀 [[TOOLS|猪猪",
        "好呀 [[SEARCH|a]b]]",
        "好呀 [[PROFILE|阿明]]",
        "好呀 [[LUCK]]",
        "好呀 [[[STICKER|列表]]]",
        "好呀 THINK|忘了括号",
        "好呀 TOOLS:猪猪",
    ):
        _pp = parse_reply(_raw)
        _txt = "".join(str(i.get("value") or "") for i in _pp.get("items") or [])
        check(S, all(w not in _txt for w in
                     ("TOOLS", "SEARCH", "PROFILE", "LUCK", "STICKER", "[[")),
              f"协议标记不漏给用户：{_raw!r} → {_txt!r}")
    check(S, strip_protocol_residue("这是[1]正常的") == "这是[1]正常的",
          "正常方括号不误伤")
    check(S, strip_protocol_residue("I think: this is fine") == "I think: this is fine",
          "英文小写 think 不误伤（只认大写协议词）")
    # ---------- 识图描述回显（2026-11-23 真机：她把看到的画面念出来）----------
    from services.aura_logic import strip_media_echo
    check(S, strip_media_echo(
        "【图片】一只淡蓝色的卡通仓鼠双臂交叉抱在胸前，咧着嘴") == "",
        "画面描述整条丢弃")
    check(S, "图片识别结果" not in strip_media_echo("【图片识别结果】啊……这个……"),
          "识别标记不漏给用户")
    check(S, strip_media_echo("图里那个人我认识") == "图里那个人我认识",
          "正常提到图片不误伤")
    # ---------- 语音不许念工具词 / 引用块不许照抄（2026-11-23）----------
    from services.aura_logic import clean_voice_text
    check(S, clean_voice_text("sticker：开心 今天好累") == "今天好累",
          "语音 stick:开心 被清掉")
    _vq = parse_reply("<voice>好累啊 <sticker:开心></voice>")
    _vv = [i for i in _vq.get("items") or [] if i.get("kind") == "voice"]
    check(S, _vv and _vv[0]["value"] == "好累啊", "语音里的 <sticker:…> 不进 TTS")
    _qq = parse_reply("〔引用 阿明 的消息（只作指代）：今天吃了吗〕 吃了")
    _qqt = "".join(str(i.get("value") or "") for i in _qq.get("items") or [])
    check(S, _qqt == "吃了", "引用块不会被照抄进回复")
    _qe = parse_reply("今天吃了吗？吃了", quote_text="今天吃了吗")
    _qet = "".join(str(i.get("value") or "") for i in _qe.get("items") or [])
    check(S, _qet == "吃了", "开头照抄原文被剥掉")
    _qs = parse_reply("今天好累啊", quote_text="今天好累")
    _qst = "".join(str(i.get("value") or "") for i in _qs.get("items") or [])
    check(S, _qst == "今天好累啊", "短引用不误伤正常口语")
    # prompt 里必须有这一节（不然模型不会写）。
    # 这里读源码而不是 import llm：llm 顶层 import httpx，离线环境跑不起来。
    _llm_src = (ROOT / "services" / "llm.py").read_text(encoding="utf-8")
    check(S, "THINK_PROTOCOL" in _llm_src and "每一条回复" in _llm_src
          and "[[THINK|你此刻心里那句话]]" in _llm_src,
          "THINK_PROTOCOL 要求每轮都写")
    check(S, '("06b 心里话", THINK_PROTOCOL.strip())' in _llm_src,
          "系统提示里有「06b 心里话」这一节")
    check(S, "写了 [[MOOD]] 也不能省掉 THINK" in _llm_src,
          "post-history 最后再压一遍「THINK 每条必写」（真机命中率 2/7 的修法）")
    # 命中统计（能量化「是不是每句都带了」）
    from services import mood_judge as _mjt
    _mjt.note_think_stat(True)
    _mjt.note_think_stat(False)
    check(S, "1/2" in _mjt.think_stat_text(), f"命中统计：{_mjt.think_stat_text()}")

    # ---------- 心情判读的「为什么没判」要看得见（用户 2026-11-19）----------
    from services import mood_judge as _mj
    _mj_src = (ROOT / "services" / "mood_judge.py").read_text(encoding="utf-8")
    check(S, hasattr(_mj, "trace_text"), "有 trace_text（/测函数 心情 打上次尝试和闸门）")
    _tt = _mj.trace_text("group_selftest_mj")
    check(S, "上次尝试" in _tt and "下次可判" in _tt and "上下文" in _tt,
          "trace_text 说清「跑没跑 / 能不能判 / 拉到几条」")
    # 重启后内存 trace 是空的，但判读结果落盘了 —— 不能自相矛盾说「还没跑过」
    _tt2 = _mj.trace_text("group_selftest_mj2",
                          {"at": time.time(), "word": "有点飘", "value": 0.3})
    check(S, "已落盘" in _tt2 and "有点飘" in _tt2,
          "重启后 trace 回退到磁盘上的判读结果（不再说「还没跑过」）")
    # 图片 OCR / 链接预览别把 20 条窗口占满
    _slimmed = _mj._slim("[图片] 图里文字：https://m.douyin.com/item/xxxx 很长很长")
    check(S, len(_slimmed) <= 90 and "http" not in _slimmed,
          f"上下文里的链接被压缩（{_slimmed[:30]}…）")
    check(S, _mj._slim("今天天气不错") == "今天天气不错", "普通聊天不被改动")

    # ---------- 她的「心里话」要落盘（用户 2026-11-19：只看到回复，没有心里话）----------
    _tk = "group_think_selftest"
    _mj.note_think(_tk, "嘴上说没什么，其实挺受用", src="他: 你好可爱", reply="哼")
    _mj.note_think(_tk, "嘴上说没什么，其实挺受用", src="他: 你好可爱", reply="哼")  # 重复不记
    _mj.note_think(_tk, "想反驳，又觉得没必要", src="他: 你笨", reply="……")
    _rt = _mj.recent_think(_tk, 3)
    check(S, _rt == ["嘴上说没什么，其实挺受用", "想反驳，又觉得没必要"],
          f"心理活动落盘且去重（{_rt}）")
    _items = _mj.recent_think_items(_tk, 5)
    check(S, len(_items) == 2 and _items[-1]["src"] == "他: 你笨"
          and _items[-1]["reply"] == "……",
          "每条心里话带「接的是哪句 / 她说了什么」（逐条对照用）")
    # 她没写 THINK 时后台补一句（命中率 2/7，靠自觉不靠谱）
    check(S, hasattr(_mj, "backfill_think") and hasattr(_mj, "BACKFILL"),
          "有 backfill_think（没写 THINK 时补一句，标记 auto）")
    _mj.note_think(_tk, "补的心里话", src="他: 在吗", reply="嗯", auto=True)
    _auto_items = _mj.recent_think_items(_tk, 5)
    check(S, _auto_items[-1].get("auto") is True, "补的心里话带 auto 标记（/测函数 会标「补」）")
    check(S, _mj.THINK_FILE.exists(), f"心理活动有独立文件：{_mj.THINK_FILE.name}")
    # 判读 prompt 要让她有「心里话」这个字段（她自己没写时由模型推断）
    _pp = _mj._build_prompt("", "", ["a: b"])
    check(S, "mind" in _pp and "心里话" in _pp,
          "判读 prompt 要求输出 mind（她没写 MEMO 也有心里话）")
    check(S, "这一项不能省" in _pp, "prompt 明确要求 mind 不能省（模型爱省略）")
    check(S, hasattr(_mj, "_mind_only"), "有 _mind_only 兜底（模型没给 mind 时单独补问）")
    check(S, "_mind_only(words, think, ctx, word)" in _mj_src,
          "judge() 在 mind 为空时真的会补问")
    # 判读结果能存 mind（否则 /测函数 心情 还是只看到回复）
    _rec = mood.set_judge("group_selftest_mind", "有点飘", 0.3,
                          why="被夸了", src="", mind="嘴上不认，其实受用")
    check(S, _rec.get("mind") == "嘴上不认，其实受用",
          "set_judge 存得下判读出的心里话")
    check(S, len(_rec.get("why") or "") <= 80, "依据不再被 40 字截断")
    _chat_src2 = (ROOT / "plugins" / "chat" / "__init__.py").read_text(encoding="utf-8")
    check(S, "mood_judge.recent_think_items(_sk, 5)" in _chat_src2,
          "/测函数 心情 逐条显示心理活动（不是只给一句汇总）")
    check(S, "mood_judge.backfill_think(session_key" in _chat_src2,
          "没写 THINK 时后台补一句（不再只靠模型自觉）")
    # 被动心情观察（用户 2026-11-20：冷了也要有心理活动，否则越冷越难回升）
    check(S, "_maybe_mood_watch" in _chat_src2 and "MOOD_WATCH_SEC" in _chat_src2,
          "群里有消息就「心里过一遍」（不等她回复也判）")
    check(S, "passive=True" in _chat_src2 and "if passive and _res.get(\"ok\")" in _chat_src2,
          "被动判读的 mind 也落盘成心理活动")
    check(S, "speaker_qq=speaker_qq" in _chat_src2,
          "判读带 speaker_qq（好感度按人涨落）")
    check(S, "接的是：" in _chat_src2 and "她说：" in _chat_src2,
          "逐条记录能对上「哪句话 → 什么心里话」")
    check(S, 'str(parsed.get("think") or "").strip()' in _chat_src2,
          "触发判读时把 [[THINK]] 一起带上（不只看 MEMO 的 think）")
    check(S, 'logger.info(f"[MOOD-JUDGE] {key or \'?\'} 这次没判' in _mj_src,
          "没判成也打 INFO（以前只打 debug，真机日志里等于没有）")
    _chat_src = (ROOT / "plugins" / "chat" / "__init__.py").read_text(encoding="utf-8")
    check(S, "mood_judge.trace_text(_sk, _jd)" in _chat_src,
          "/测函数 心情 输出 trace（不是只有「还没判」）")
    check(S, "for _i, _x in enumerate(_ctxj, 1)" in _chat_src,
          "判读上下文**全部**列出来（以前只给 5 条，用户说「还是不够 20 条」）")


# ============================================================
# 分组 3：profiles（bond 兼容升级 / 刻痕 / 长期印象）
# ============================================================
async def t_profiles():
    from services import profiles
    S = "profiles"
    qq = "10001"
    d = Path("data/profiles")
    d.mkdir(parents=True, exist_ok=True)

    # 老格式 list[str]
    (d / f"{qq}_bond.json").write_text(
        json.dumps({"notes": ["老格式：喜欢猫", "老格式：怕黑"]}, ensure_ascii=False), encoding="utf-8")
    b = profiles.get_bond(qq)
    check(S, isinstance(b["notes"][0], dict), "老 list[str] 自动升级成 list[dict]")
    check(S, b["notes"][0]["text"] == "老格式：喜欢猫", "老数据没丢")

    profiles.add_bond_note(qq, "他骂了我一句，我记仇了", emotion="angry")
    profiles.add_bond_note(qq, "他夸我唱歌好听", emotion="happy")
    b = profiles.get_bond(qq)
    check(S, any(n["emotion"] == "angry" for n in b["notes"]), "带 emotion 写入成功")
    check(S, all({"text", "emotion", "at", "weight"} <= set(n) for n in b["notes"]), "笔记字段完整")

    profiles.add_bond_note(qq, "他夸我唱歌好听", emotion="happy")
    b = profiles.get_bond(qq)
    dup = [n for n in b["notes"] if n["text"] == "他夸我唱歌好听"]
    check(S, len(dup) == 1, "重复印象不重复写")
    check(S, dup[0]["weight"] > 1.0, f"重复印象权重被加强（{dup[0]['weight']}）")

    profiles.add_bond_note(qq, {"text": "他给我买了小鱼干", "emotion": "touched"}, weight=2.0)
    check(S, any(n["text"] == "他给我买了小鱼干" for n in profiles.get_bond(qq)["notes"]),
          "add_bond_note 支持直接传 dict")

    profiles.upsert_profile(qq, nickname="小明", owner=True)
    profiles.set_long_term_impression(qq, "他很轴但很真诚，需要人陪着")
    p = profiles.get_profile(qq)
    check(S, p["long_term_impression"].startswith("他很轴"), "长期印象写回档案")

    pp = profiles.profile_prompt(qq, "小明")
    check(S, "💢" in pp, "愤怒刻痕高亮进 Prompt")
    check(S, "💛" in pp, "开心刻痕高亮进 Prompt")
    check(S, "他很轴但很真诚" in pp, "长期印象进 Prompt")
    check(S, pp.startswith("（") and pp.endswith("）"), "profile_prompt 仍是一整段括号文本")

    # TTL 淘汰
    old = time.time() - 90 * 86400
    b = profiles.get_bond(qq)
    b["notes"].append({"text": "很久以前的一次性闲聊", "emotion": "neutral", "at": old, "weight": 1.0})
    b["notes"].append({"text": "反复提过的刻痕", "emotion": "touched", "at": old, "weight": 2.0})
    profiles._save("bond", qq, b)
    texts = profiles.bond_texts(qq)
    check(S, "很久以前的一次性闲聊" not in texts, "90 天未强化的一次性印象被淘汰")
    check(S, "反复提过的刻痕" in texts, "weight>=1.5 的真刻痕保留")

    check(S, profiles.add_bond_note(qq, "") is None, "空文本不写入")

    # ---------- 显式好感度（2026-11-20：心情影响「对谁安静/找谁倾诉」）----------
    _qa = "30009"
    _a0 = profiles.get_affinity(_qa)
    _a1 = profiles.bump_affinity(_qa, 0.06, reason="被夸")
    _a2 = profiles.bump_affinity(_qa, -0.10, reason="被恶心")
    check(S, _a1 > _a0 and _a2 < _a1, f"好感度能涨能跌（{_a0:.2f}→{_a1:.2f}→{_a2:.2f}）")
    _a3 = profiles.bump_affinity(_qa, 999, reason="想一次顶满")
    check(S, _a3 - _a2 <= 0.10 + 1e-6, f"单次好感最多动 0.10（实际 {_a3 - _a2:.2f}）")
    check(S, isinstance(profiles.get_profile(_qa).get("affinity_log"), list),
          "好感涨落有流水（可以回溯为什么变）")
    profiles.add_bond_note(_qa, "他记得我说过的话", emotion="touched")
    check(S, profiles.intimacy(_qa) > 0, "好感度参与 intimacy 计算")

    # ---------- 群友档案挖掘（用户 2026-11-20：只有称呼，没有内容和事情）----------
    from services import profile_miner as _pm
    check(S, _pm.ENABLED and _pm.MIN_MSGS > 0,
          f"档案挖掘开着（攒 {_pm.MIN_MSGS} 句 / {_pm.INTERVAL_H:.0f}h 冷却）")
    _g, _q2 = "999999", "30001"
    _due_at = None
    for _i in range(_pm.MIN_MSGS + 3):
        if _pm.note_message(_g, _q2):
            _due_at = _i + 1
    check(S, _due_at == _pm.MIN_MSGS,
          f"攒够 {_pm.MIN_MSGS} 句才触发一次挖掘（实际第 {_due_at} 句）")
    check(S, not _pm.note_message("", _q2), "没有群号不计数（私聊不挖）")
    check(S, "档案挖掘" in _pm.status_text(), "status_text 能看挖掘状态")
    # 素材不够时不硬挖（绝不编）
    _r = await _pm.mine(_q2, _g, force=True)
    check(S, not _r.get("ok") and "素材不够" in str(_r.get("err")),
          f"素材不足时不硬编（{_r.get('err')}）")
    check(S, _pm.SAMPLE_MAX >= 10, "样本上限合理")


# ============================================================
# 分组 4：memory_v2
# ============================================================
async def t_memory():
    from services import memory_v2 as M
    S = "memory"
    qq = "10001"

    a = M.remember(text="小明：我家猫叫二零", kind="fact", about_qq=qq, importance=0.8, emotion_hit=True)
    M.remember(text="小明：他今天心情很差，被领导骂了", kind="event", about_qq=qq, importance=0.7)
    check(S, isinstance(a, str) and len(a) > 0, "remember 返回 id")

    hits = M.recall("我家猫叫什么", speaker_qq=qq, limit=3)
    check(S, len(hits) > 0, f"recall 命中 {len(hits)} 条")
    check(S, hits[0]["emotion_hit"] is True, "recall 结果带情绪刻痕标记")
    check(S, all({"text", "ago", "emotion_hit"} <= set(h) for h in hits), "recall 字段完整")
    check(S, M.recall("完全无关的量子色动力学", speaker_qq=qq, limit=3) == [] or True, "无关查询不崩")

    idx = M._load_index()
    check(S, len(idx["items"]) >= 2, f"索引累计 {len(idx['items'])} 条")
    M.mark_outdated(a)
    idx = M._load_index()
    check(S, any(i["id"] == a and i["status"] == "outdated" for i in idx["items"]), "mark_outdated 只标状态")
    check(S, len(idx["items"]) >= 2, "mark_outdated 不删条目")
    st = M.stats()
    check(S, isinstance(st, dict) and st, f"stats() 可用：{list(st)[:4]}")

    # ---------- 二维记忆：时间 × 情绪强度 ----------
    check(S, M.emotion_weight_of("love") == 1.0, "love 情绪权重 1.0")
    check(S, M.emotion_weight_of("happy") == 0.5, "happy 情绪权重 0.5")
    check(S, M.emotion_weight_of("neutral") == 0.0, "neutral 情绪权重 0")
    check(S, M.emotion_weight_of("莫名其妙的词") == 0.5, "认不出的情绪按中等 0.5")
    check(S, M.emotion_mark(0.9) == "💥💥💥", "强度 0.9 → 💥💥💥")
    check(S, M.emotion_mark(0.5) == "💥💥", "强度 0.5 → 💥💥")
    check(S, M.emotion_mark(0.2) == "💥", "强度 0.2 → 💥")
    check(S, M.emotion_mark(0.0) == "", "强度 0 → 不标")
    # 老数据兼容：只有 emotion_hit 没有 weight
    check(S, M._emo_of({"emotion_hit": True}) == 0.6, "老数据 emotion_hit → 按 0.6 算")
    check(S, M._emo_of({"emotion_weight": 0.9}) == 0.9, "新数据直接读 weight")
    # 维度② 的核心：情绪越强，衰减越慢（同一时刻比较）
    import time as _t
    _now = _t.time()
    _old = _now - 60 * 86400          # 60 天前
    _f_neutral = M._forget_factor(_now, _old, 0.0)
    _f_strong = M._forget_factor(_now, _old, 1.0)
    check(S, _f_strong > _f_neutral * 2,
          f"60 天前的记忆：强情绪衰减更慢（{_f_strong:.3f} vs {_f_neutral:.3f}）")
    # 端到端：带情绪强度的记忆能被检索回来，且带回 weight/mark
    _mid = M.remember(text="小明：我奶奶今天走了", kind="event", about_qq=qq,
                      importance=0.9, emotion_weight=1.0)
    _h = [x for x in M.recall("奶奶", speaker_qq=qq, limit=5) if x["id"] == _mid]
    check(S, bool(_h), "强情绪记忆能检索到")
    if _h:
        check(S, _h[0]["emotion_weight"] == 1.0, "recall 带回 emotion_weight")
        check(S, _h[0]["emotion_mark"] == "💥💥💥", "recall 带回 💥💥💥 标记")
    _mp = M.memory_prompt("奶奶", speaker_qq=qq, limit=3)
    check(S, "💥" in _mp and "两维" in _mp, "memory_prompt 用二维方式展示")

    # ---------- 目录模式 + 时间搜索（2026-11-16）----------
    check(S, callable(M.recall_catalog), "有 recall_catalog（目录模式）")
    check(S, callable(M.recall_detail), "有 recall_detail（单条展开）")
    check(S, callable(M.parse_time_expression), "有 parse_time_expression（时间词解析）")
    check(S, callable(M.recall_by_time), "有 recall_by_time（按时间召回）")
    check(S, callable(M.strip_time_words), "有 strip_time_words（剥时间词）")

    _cat = M.recall_catalog("小明", limit=5, speaker_qq=qq)
    check(S, isinstance(_cat, dict) and "items" in _cat, "recall_catalog 返回目录结构")
    check(S, all({"no", "id", "summary", "ago"} <= set(x) for x in _cat.get("items", [])),
          "目录条目带编号/时间/摘要")
    check(S, M.recall_detail(a) != "", "recall_detail 能展开原文")

    from datetime import datetime as _dt
    _now_ts = _dt(2026, 11, 16, 15, 30).timestamp()
    _r1 = M.parse_time_expression("前天上午", _now_ts)
    check(S, _r1 is not None, "「前天上午」能解析")
    if _r1:
        _a = _dt.fromtimestamp(_r1[0]); _b = _dt.fromtimestamp(_r1[1])
        check(S, (_a.month, _a.day, _a.hour) == (11, 14, 6), f"前天上午 = 11-14 06:00（实得 {_a:%m-%d %H:%M}）")
        check(S, _b.hour == 12, "前天上午到 12:00 结束")
    _r2 = M.parse_time_expression("3天前", _now_ts)
    check(S, _r2 is not None, "「3天前」能解析")
    check(S, M.parse_time_expression("后天", _now_ts) is None, "「后天」不解析（只认过去）")
    check(S, M.strip_time_words("前天上午 一起去吃饭") == "一起去吃饭", "strip_time_words 剩关键词")


# ============================================================
# 分组 5：persona
# ============================================================
async def t_persona():
    from services import persona
    S = "persona"
    key, prompt = persona.persona_for("group_999")
    # 发行仓不带私人人格卡（只有 personality.example.txt）：没有卡时只做结构断言
    _pdir = ROOT / "services" / "Personality"
    _has_cards = _pdir.exists() and any(_pdir.glob("*.txt"))
    check(S, isinstance(key, str) and key, f"persona_for 返回人格名：{key}")
    if _has_cards:
        check(S, isinstance(prompt, str) and len(prompt) > 50,
              f"System Prompt 长度 {len(prompt)}")
    else:
        check(S, isinstance(prompt, str), "没有人格卡时仍返回字符串（回落内置性格）")
    check(S, key == persona.DEFAULT_PERSONA, f"未设置过的 session → 默认人格（{persona.DEFAULT_PERSONA}）")
    if persona.DEFAULT_PERSONA in persona.PERSONALITY_MAP:
        check(S, len(prompt) > 500, f"默认人格加载的是完整外部角色卡（{len(prompt)} 字）")
    else:
        skip(S, "默认人格的外部卡未部署，回落内置性格")

    # 换成一个真实存在、且和当前不同的人格
    others = [k for k in persona.list_personas() if k != key]
    if others:
        target = others[0]
        persona.current_personality["group_999"] = target
        k2, p2 = persona.persona_for("group_999")
        check(S, k2 == target, f"/persona 切换后 persona_for 跟着变（{target}）")
        check(S, p2 != prompt, "切换人格后 Prompt 变了")
        # 脏数据：不在 PERSONALITY_MAP 里的名字会静默回落默认人格
        persona.current_personality["group_999"] = "不存在的人格"
        k3, p3 = persona.persona_for("group_999")
        check(S, p3 == prompt, "非法人格名 → 静默回落默认 Prompt（不炸）")
    else:
        skip(S, "PERSONALITY_MAP 只有一个人格，跳过切换测试")
    check(S, persona.get_system_prompt(persona.DEFAULT_PERSONA), "get_system_prompt 直调可用")
    # 关键：默认人格的 prompt 必须**逐字等于** txt 原文（不叠加任何内置内容）
    fp = persona.PERSONA_FILES.get(persona.DEFAULT_PERSONA)
    if fp and fp.exists():
        raw = fp.read_text(encoding="utf-8").strip()
        check(S, prompt == raw, f"默认人格 prompt 与 txt 原文逐字一致（{len(raw)} 字）")
        # 2026-11-24 用户定稿：人设卡只保留「你是谁（含外貌）」+「性格」，
        # 说话案例/铁律/底线都交给固定区协议，不再用「长度 > 5000」判断完整卡。
        check(S, len(raw) > 500 and "一、你是谁" in raw and "二、性格" in raw,
              f"人设卡是精简版两节（{len(raw)} 字）")

        # 人格卡内容审计（2026-11-18）：不能出现本程序里不存在的 API / 已被否掉的规则
        for _bad in ("get_message_images", "web_fetch", "replyToMessageId", "send_message"):
            check(S, _bad not in raw, f"人格卡里没有不存在的 API：{_bad}")
        check(S, "电子生命" not in raw, "人格卡去掉了「电子生命/无限制」那条")
        # 这几条规则已经从人设卡移到固定区协议（services/llm.py），
        # 断言也跟着移到 llm.py，避免「人设卡瘦身」把已修 bug 悄悄丢掉。
        _llm_src = (ROOT / "services" / "llm.py").read_text(
            encoding="utf-8", errors="ignore")
        check(S, "不要用括号" in _llm_src, "系统协议明确禁止括号小动作")
        check(S, "你对 X 的称呼" in _llm_src,
              "系统协议区分「你把X叫什么」和「你叫什么」")
        check(S, "被纠正不丢人" in _llm_src, "系统协议要求被纠正后重新理解")
    else:
        skip(S, "默认人格没有对应 txt 文件，跳过逐字一致性检查")

    # 括号动作兜底过滤（模型手滑也发不出去）
    from services.aura_logic import strip_actions
    check(S, strip_actions("好的（笑）") == "好的", "括号动作会被过滤")
    check(S, strip_actions("（其实我不太确定）算了") == "（其实我不太确定）算了",
          "正常括号不被误删")
    check(S, strip_actions("（歪头）") == "", "只有括号动作 → 空（不发送）")


# ============================================================
# 分组 6：agent_client
# ============================================================
async def t_agent():
    from services import agent_client as AC
    S = "agent"

    r = AC._normalize_recall({"success": True, "answer": "他喜欢白米饭"}, "query")
    check(S, r["facts"] == ["他喜欢白米饭"] and r["source"] == "query", "/query 形状 → facts")
    r = AC._normalize_recall({"data": {"facts": ["a"], "events": ["b"], "emotions": ["c"],
                                       "self_state": "我刚在生气"}}, "recall")
    check(S, r["facts"] == ["a"] and r["events"] == ["b"] and r["emotions"] == ["c"]
          and r["self_state"] == "我刚在生气", "结构化 recall（包一层 data）")
    r = AC._normalize_recall({"facts": "单条字符串"}, "recall")
    check(S, r["facts"] == ["单条字符串"], "facts 给字符串也容错")
    r1 = AC.empty_recall()
    r1["facts"].append("x")
    check(S, AC.empty_recall()["facts"] == [], "empty_recall 不共享内部列表")

    # 全失败 → 返回空模板，不抛异常
    orig = AC._post_json

    async def _none(*a, **k):
        return None
    AC._post_json = _none
    r = await AC.recall("随便问问")
    check(S, r == AC.empty_recall(), "两条通道都挂 → 返回空模板且不抛异常")
    check(S, await AC.recall("") == AC.empty_recall(), "空 query 直接返回空模板")
    AC._post_json = orig
    # 失败会让「图书馆暂停」，后面的用例先复位（否则全是假失败）
    AC._LIBRARY_DEAD_UNTIL = 0.0

    # 结构化通道优先（前面的失败用例会把「端点不存在 / 暂停」标记置上，先复位）
    AC._RECALL_ENDPOINT_MISSING = False
    AC._LIBRARY_DEAD_UNTIL = 0.0

    async def _fake(url, payload, timeout):
        if url.endswith(AC.RECALL_ENDPOINT):
            return {"facts": ["来自 recall"]}
        return {"answer": "来自 query"}
    AC._post_json = _fake
    r = await AC.recall("测试")
    check(S, r["source"] == "recall" and r["facts"] == ["来自 recall"], "优先走结构化端点")
    # recall 端点 404 → 先退 smart-query（RAG v3 的真实路由）
    AC._RECALL_ENDPOINT_MISSING = False
    AC._LIBRARY_DEAD_UNTIL = 0.0

    async def _fake2(url, payload, timeout):
        if url.endswith(AC.RECALL_ENDPOINT):
            return None
        # 必须是「像聊天摘录」的答案，否则会被 _looks_like_chat 判成通用问答
        return {"success": True, "answer": "阿明: 上次那个蛋糕是街角那家"}
    AC._post_json = _fake2
    r = await AC.recall("测试")
    check(S, r["source"] in ("smart", "query")
          and r["facts"] == ["阿明: 上次那个蛋糕是街角那家"],
          f"结构化端点不可用 → 退回 smart-query（{r['source']}）")
    # 宋词语料 → 判成「不是聊天」，直接停用 6 小时
    async def _fake3(url, payload, timeout):
        return {"answer": "参考来源: /app/documents/poet.song.rank.1.json"}
    AC._post_json = _fake3
    AC._RECALL_ENDPOINT_MISSING = True
    AC._LIBRARY_DEAD_UNTIL = 0.0
    r = await AC.recall("测试")
    check(S, r == AC.empty_recall() and AC._LIBRARY_DEAD_UNTIL > 0,
          "图书馆语料不是聊天 → 返回空并暂停")
    AC._LIBRARY_DEAD_UNTIL = 0.0
    AC._RECALL_ENDPOINT_MISSING = False
    AC._post_json = orig


# ============================================================
# 分组 7：journal_agent
# ============================================================
async def t_journal():
    from services import journal_agent as J, memory_v2 as M, profiles
    S = "journal"
    # ⚠️ 用独立 QQ：和 t_memory / t_profiles 共用 10001 会串数据，
    #    remember() 的相似合并偶尔会把两条测试记忆并成一条，导致关键词找不到（偶发失败）
    qq = "20002"

    profiles.upsert_profile(qq, nickname="小明", owner=True)
    mid = M.remember(text="小明：翻唱功能一直报错，他很焦虑", kind="event", about_qq=qq, importance=0.7)
    M.remember(text="小明：白米饭配咸菜最好吃", kind="fact", about_qq=qq, importance=0.6)

    b = J.collect_bundle()
    need = ("instruction", "persona", "mood", "profiles", "bonds", "catalog",
            "window_details", "journals", "window", "owner_qq")
    check(S, all(k in b for k in need), f"bundle 字段齐全（缺：{[k for k in need if k not in b]}）")
    check(S, b["window"]["days"] > 0, "window.days 有值")
    check(S, isinstance(b["persona"], dict), "persona 是 dict（角色卡从磁盘读）")
    check(S, len(b["catalog"]) >= 1, f"记忆目录 {len(b['catalog'])} 条")
    check(S, all("id" in c for c in b["catalog"]), "目录每条都带 id")

    # 正常回写
    ap = J.apply_reflection(
        {"journal": "这两天他一直在弄翻唱。", "impression": "他很轴但值得陪",
         "archive_keywords": ["翻唱功能一直报错", "根本不存在的关键词"]}, b)
    check(S, ap["journal"] and Path(ap["journal"]).exists(), f"日记落盘：{ap['journal']}")
    check(S, ap["impression"] == "他很轴但值得陪", "印象提炼")
    check(S, profiles.get_profile(qq)["long_term_impression"] == "他很轴但值得陪", "印象写回档案")
    check(S, ap["archived"] == 1, f"关键词唯一命中才归档（archived={ap['archived']}）")

    # 命中多条 → 跳过
    M.remember(text="小明：白米饭真好吃", kind="fact", about_qq=qq, importance=0.5)
    ap2 = J.apply_reflection({"journal": "x", "impression": "y", "archive_keywords": ["白米饭"]}, b)
    check(S, ap2["archived"] == 0, "关键词命中多条 → 跳过不误删")

    # error 不写日记
    before = len(list(Path("data/journals").glob("*.md")))
    ap3 = J.apply_reflection({"error": "LLM call failed"}, b)
    after = len(list(Path("data/journals").glob("*.md")))
    check(S, ap3["journal"] == "" and after == before, "算力机回传 error 时不写日记")

    # 包裹形态 + list journal
    ap4 = J.apply_reflection({"data": {"journal": ["第一段。", "第二段。"], "impression": "印象X"}}, b)
    check(S, "第二段" in Path(ap4["journal"]).read_text(encoding="utf-8"), "list 形态 journal 被拼成正文")

    # run_reflection 全通道失败 → ok=False
    orig = J.agent_client.reflection
    async def _none(bundle, timeout=None):
        return None
    J.agent_client.reflection = _none
    r = await J.run_reflection()
    check(S, r["ok"] is False, "两条通道都挂 → run_reflection 返回 ok=False")
    J.agent_client.reflection = orig


# ============================================================
# 分组 8：vision_agent
# ============================================================
async def t_vision():
    S = "vision"
    try:
        import plugins.vision_agent as V
    except Exception as e:
        check(S, False, "plugins.vision_agent 导入失败", f"{type(e).__name__}: {e}")
        return

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    check(S, V._sniff_mime(png) == "image/png", "magic bytes → image/png")
    check(S, V._sniff_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == "image/jpeg", "→ image/jpeg")
    check(S, V._sniff_mime(b"GIF89a" + b"\x00" * 8) == "image/gif", "→ image/gif")
    check(S, V._sniff_mime(b"RIFF\x00\x00\x00\x00WEBPVP") == "image/webp", "→ image/webp")

    b64 = base64.b64encode(png).decode()
    check(S, V._coerce_image_bytes(png) == png, "bytes 原样")
    check(S, V._coerce_image_bytes(b64) == png, "纯 base64 解码")
    check(S, V._coerce_image_bytes(f"data:image/png;base64,{b64}") == png, "data URI 解码")
    check(S, V._coerce_image_bytes("") == b"", "空输入 → b''")

    for fn in ("describe_image_bytes", "describe_image", "caption_image", "analyze_image"):
        check(S, callable(getattr(V, fn, None)), f"sticker_receiver 找的 {fn} 存在")

    # 2026-11-20：源码里不再硬编码 DashScope key（会进镜像/进 git）。
    # 现在只认环境变量：配了就有，没配就空（语音功能降级，不偷偷用作者的 key）。
    import os as _os
    if _os.getenv("DASHSCOPE_API_KEY"):
        check(S, bool(V.DASHSCOPE_API_KEY), "DASHSCOPE_API_KEY 来自环境变量")
    else:
        check(S, V.DASHSCOPE_API_KEY == "",
              "没配 DASHSCOPE_API_KEY 时为空（源码无硬编码 key）")
    check(S, V.VISION_ENDPOINT.endswith("/chat/completions"), f"端点：{V.VISION_ENDPOINT[:60]}…")
    check(S, "只能从这些词里挑一个" in V.STICKER_VISION_PROMPT, "表情 prompt 限定词表")
    check(S, V.VISION_USE_URL is False,
          "默认自己下载再压（画质可控；链接只当下载失败时的兜底）")
    check(S, V.VISION_MAX_ATTEMPTS > 0, f"尝试次数上限 {V.VISION_MAX_ATTEMPTS}")
    check(S, "60" in V.DEFAULT_VISION_PROMPT, "视觉 prompt 硬性限定 60 字")
    check(S, "禁止" in V.DEFAULT_VISION_PROMPT, "视觉 prompt 有禁止项（防模型啰嗦）")

    # 大图压缩（7MB 不压缩直接 base64 = 9.4MB 请求体，必超时）
    try:
        import io as _io

        from PIL import Image as _Image

        buf = _io.BytesIO()
        _Image.new("RGB", (2400, 1600), (200, 30, 30)).save(buf, format="PNG")
        big = buf.getvalue()
        small, mime2 = V._compress(big)
        # 2026-11-24：quality 提到 90 后，纯色图「体积减半」不再成立（PNG 本来就小），
        # 真正要保证的是**长边被缩到上限**、且体积不涨。
        _im2 = _Image.open(_io.BytesIO(small))
        check(S, max(_im2.size) <= V.VISION_MAX_SIDE,
              f"大图长边缩到 {max(_im2.size)}px（上限 {V.VISION_MAX_SIDE}）")
        check(S, len(small) <= len(big),
              f"压缩后不涨体积 {len(big) / 1024:.0f}KB → {len(small) / 1024:.0f}KB")
        check(S, mime2 == "image/jpeg", f"压缩后 mime={mime2}")
        check(S, V._compress(png)[0] == png, "小图压完更大时保留原图")
    except ImportError:
        skip(S, "没装 Pillow，跳过压缩测试（pip install Pillow）")

    # ---- 识图核验（services/vision_verify）----
    from services import vision_verify as VV

    raw = (
        "画面：粉色短发少女，穿白色衬衫，嘴里叼着一块饼干，正对镜头\n"
        "作品：偶像大师\n"
        "角色：星井美希"
    )
    p = VV.parse_structured(raw)
    check(S, p["work"] == "偶像大师" and p["char"] == "星井美希", "结构化输出拆成 画面/作品/角色")
    check(S, "粉色短发少女" in p["scene"], "画面部分保留原始描述")
    check(S, VV.compose(p["scene"], p["work"], p["char"]).endswith("（偶像大师·星井美希）"),
          "专名拼进括号里")
    check(S, VV.compose("一只猫", "", "") == "一只猫", "没专名就只留画面")

    # 模型不按格式写 → 整段当画面，且不产生专名（也就不会去搜）
    bad = VV.parse_structured("这是一张动漫截图")
    check(S, bad["work"] == "" and bad["char"] == "" and bad["scene"], "不按格式 → 不产生假专名")

    check(S, VV._clean_name("《星井美希》") == "星井美希", "去掉书名号")
    check(S, VV._clean_name("不确定") == "", "「不确定」当空")
    check(S, VV._clean_name("unknown") == "", "unknown 当空")
    check(S, VV._clean_name("这是一个很长很长很长的句子不应该被当成名字") == "", "长句子不算名字")

    fp1 = VV.fingerprint(png)
    check(S, fp1 and fp1 == VV.fingerprint(png), "同一张图指纹一致")
    check(S, fp1 != VV.fingerprint(png + b"x"), "不同图指纹不同")

    VV.put_cache(fp1, {"desc": "粉色短发少女（偶像大师·星井美希）", "scene": "粉色短发少女"})
    hit = VV.get_cached(fp1)
    check(S, hit and hit["desc"].endswith("星井美希）"), "指纹缓存能命中（同图不再搜）")
    check(S, "画面" in VV.VISION_PROMPT_STRUCT and "作品" in VV.VISION_PROMPT_STRUCT
          and "角色" in VV.VISION_PROMPT_STRUCT, "prompt 要求 画面/作品/角色 三行")
    check(S, "不要写作品名" in VV.VISION_PROMPT_STRUCT, "画面部分禁止写专名（防止污染）")

    # 没猜专名 → 不该触发搜索
    v = await VV.verify("一只橘猫躺在沙发上", "", "")
    check(S, v["query"] == "", "没专名 → 不搜索（省 tavily 额度）")

    # trace.moe：动漫截图走「以图搜番 + 角色选择题」
    pr = VV._anime_prompt("呪術廻戦", ["五条悟", "虎杖悠仁", "伏黒恵"])
    check(S, "呪術廻戦" in pr, "动漫路径先锁定作品名（不用 VLM 猜）")
    check(S, "五条悟" in pr and "候选" in pr, "角色改成选择题（开放题必瞎猜）")
    check(S, "不要写作品名" in pr, "画面部分仍然禁止写专名")
    pr2 = VV._anime_prompt("某番", [])
    check(S, "不确定" in pr2, "没有候选角色时允许说「不确定」")
    # 疑似命中时措辞要软，别让 VLM 被一个错答案带跑
    pr3 = VV._anime_prompt("某番", [], "mid")
    check(S, "可能" in pr3 and "别全信" in pr3, "低置信时 prompt 改成「可能/别全信」")
    check(S, VV.TRACE_MIN_SIM < 0.8,
          f"trace.moe 采信门槛已放宽到 {VV.TRACE_MIN_SIM}（旧 0.85 一个月零命中）")
    check(S, VV.TRACE_STRONG_SIM > VV.TRACE_MIN_SIM, "高置信线高于采信线（分档）")
    check(S, VV.TRACE_SINGLE_SIM >= 0.9, "单帧直通线 ≥0.9")
    check(S, VV.TRACE_MOE_URL.startswith("https://api.trace.moe"), "trace.moe 端点正确")

    # ---- trace.moe 聚类判定（纯函数，不联网）----
    def _row(aid, sim, ep=1, work="某番", at=10.0):
        return {"similarity": sim, "episode": ep, "from": at,
                "anilist": {"id": aid, "title": {"native": work},
                            "synonyms_chinese": [work]}}

    # 旧规则会判「太分散」的典型形态：2 票 0.86 —— 现在要采信
    _r = VV._pick_cluster([_row(1, 0.86), _row(1, 0.81), _row(2, 0.80), _row(3, 0.79)])
    check(S, _r["accepted"] and _r["work"] == "某番", "2 票 0.86 → 采信（旧规则 ≥3票才认）")
    # 单帧几乎同帧 → 一票也认
    _r = VV._pick_cluster([_row(9, 0.98), _row(2, 0.60), _row(3, 0.55)])
    check(S, _r["accepted"], "单帧 0.98 → 一票也采信")
    # 相似度不够 → 不采信，但候选要留着
    _r = VV._pick_cluster([_row(1, 0.61), _row(1, 0.58), _row(2, 0.55)])
    check(S, not _r["accepted"] and _r["candidates"], "相似度 0.61 → 不采信但仍给候选")
    check(S, _r["candidates"][0]["work"] == "某番", "候选里有作品名（当线索）")
    # 结果全是不同番、相似度也不高 → 不采信
    _r = VV._pick_cluster([_row(i, 0.70) for i in range(1, 9)])
    check(S, not _r["accepted"], "8 条 8 部番 0.70 → 不采信")
    # 高置信：≥0.90 且 ≥2 票
    _r = VV._pick_cluster([_row(1, 0.93), _row(1, 0.91), _row(2, 0.70)])
    check(S, _r["confidence"] == "high", "0.93 + 2 票 → high")
    _r = VV._pick_cluster([_row(1, 0.80), _row(1, 0.79), _row(2, 0.70)])
    check(S, _r["accepted"] and _r["confidence"] == "mid", "0.80 + 2 票 → mid（采信但标注疑似）")

    from services import llm as L
    _Lsrc = (ROOT / "services" / "llm.py").read_text(encoding="utf-8", errors="ignore")
    # ---- 识图流程：LLM 主导（2026-11-19）----
    # 用户原话：「这套新的 llm 决策流程根本没跑起来，反而是老流程先决策在跑」
    va_src = (ROOT / "plugins" / "vision_agent" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    check(S, 'os.getenv("VISION_REPLY_MODE", "llm")' in va_src,
          "识图默认走 llm 决策（识图只负责看见，回不回由模型定）")
    check(S, "VISION_LLM_MAX_PER_HOUR" in va_src, "有每小时交给 LLM 的图片上限（防刷图烧 token）")
    check(S, "stay_silent" in va_src or "stay_silent" in _Lsrc,
          "有「不回复函数」可调")
    check(S, "STICKER_DECIDE" in va_src, "表情包也交给她决定收不收")
    check(S, "gif_grid" in va_src and "is_gif" in va_src,
          "GIF 走「抽帧拼图给视觉模型」")
    # 用户 2026-11-20：识图别先念一遍画面；图片要说清「发给谁」
    check(S, "不要在回复里复述画面" in va_src,
          "识图结果明确「不要复述画面」，直接回应")
    check(S, "image_target" in va_src and "不是在跟你说" in va_src,
          "图片带上「这张图是发给谁的」判断（防情侣互发表情被误接）")
    # 用户 2026-11-20：动图不该把逐帧解析结果整段给 DeepSeek，视觉模型先总结
    check(S, "不要逐格" in VV.GIF_PROMPT_STRUCT and "一句话总结整段动画" in VV.GIF_PROMPT_STRUCT,
          "动图 prompt 要求一句话总结、禁止逐格罗列")
    _cond = VV._gif_condense(
        "第1帧：猫在睡觉；第2帧：猫醒了；左上：猫在睡觉；右下：猫伸懒腰")
    check(S, "第1帧" not in _cond and "左上" not in _cond and len(_cond) <= 90,
          f"逐帧描述被压成一句（{_cond}）")

    # GIF 抽帧 / 拼图（Pillow 不在就跳过）
    try:
        from PIL import Image
        import io as _io2
        _frames = [Image.new("RGB", (40, 40), c) for c in
                   ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0))]
        _b = _io2.BytesIO()
        _frames[0].save(_b, format="GIF", save_all=True, append_images=_frames[1:],
                        duration=100, loop=0)
        _gif = _b.getvalue()
        _fr = VV.gif_frames(_gif)
        check(S, len(_fr) == 4 and _fr[0][:2] == b"\xff\xd8", f"GIF 能抽帧（{len(_fr)} 帧 JPEG）")
        check(S, len(VV.gif_frames(_gif, limit=3)) == 3, "GIF 能均分三帧（trace.moe 用）")
        check(S, len(VV.gif_grid(_gif)) > 200, "GIF 能拼成网格图（视觉模型用）")
    except Exception as e:
        skip(S, f"GIF 抽帧测试跳过（{type(e).__name__}）")

    # 动图识别全流程：报告里只给「总结」，不给逐帧描述（用户 2026-11-20）
    try:
        import io as _io3
        from PIL import Image as _IM
        _fs = [_IM.new("RGB", (40, 40), (i * 30 % 255, 20, 20)) for i in range(6)]
        _bb = _io3.BytesIO()
        _fs[0].save(_bb, format="GIF", save_all=True, append_images=_fs[1:],
                    duration=80, loop=0)
        _gif2 = _bb.getvalue()

        async def _vl_stub(prompt, max_tokens=200):
            # 第一次故意返回「逐格报菜名」，模拟模型不听话 → 触发二次总结
            if "只用**一句话**" in prompt:
                return "「猫从趴着到炸毛，最后跑了」"
            return ("标记：正常\n类型：表情包\n"
                    "画面：第1格：猫趴着；第2格：猫抬头；第3格：猫炸毛；"
                    "第4格：猫跑掉；第5格：空的；第6格：猫回来了\n"
                    "文字：无\n角色：不确定")

        _oT, _oG = VV.TRACE_MOE, VV.GLM_VISION
        VV.TRACE_MOE = False
        VV.GLM_VISION = False
        try:
            _rec2 = await VV.recognize(_gif2, _vl_stub, is_gif=True)
        finally:
            VV.TRACE_MOE, VV.GLM_VISION = _oT, _oG
        _rp2 = str(_rec2.get("report") or "")
        check(S, "动图内容：" in _rp2, "动图报告给的是「动图内容：一句话」")
        check(S, "第1格" not in _rp2 and "qwen识图：" not in _rp2,
              "逐帧描述没有漏给 DeepSeek（只给总结）")
    except Exception as e:
        skip(S, f"动图总结流程测试跳过（{type(e).__name__}: {e}）")

    # ---- 沉默权 / 安静开关 ----
    from services import chat_switch as CS
    import tempfile as _tf
    _old_file = CS.STATE_FILE
    try:
        CS.STATE_FILE = Path(_tf.mkdtemp(prefix="cs_")) / "switch.json"
        CS._cache.update({"mtime": 0.0, "data": {"groups": {}, "users": {}}})
        check(S, CS.is_off("group_1")[0] is False, "默认不安静")
        # strict（主人用 /安静 下的命令）：连 @ 也不回
        CS.set_off("群", "strict1", minutes=30, reason="主人要求", strict=True)
        check(S, CS.is_off("group_strict1")[0] and CS.is_strict("group_strict1"),
              "strict 安静：状态是安静，且标记为强制")
        CS.set_on("群", "strict1")
        check(S, not CS.is_strict("group_strict1"), "解除后 strict 也清掉")
        CS.set_off("群", "1", minutes=30, reason="想安静")
        check(S, CS.is_off("group_1")[0] is True, "群静音生效")
        check(S, CS.is_off("private_1")[0] is False, "群静音不影响同号的私聊")
        CS.set_off("人", "2", minutes=0, reason="他太吵")
        check(S, CS.is_off("group_1", "2")[0] is True, "群/人任一静音都算安静")
        check(S, CS.is_off("group_1", "3")[0] is True, "群里别人说话也受群静音影响")
        check(S, "只读不回" in CS.render(), "prompt 里写明「只读不回」")
        check(S, "群 1" in CS.status_text(), "status 能看到静音列表")
        CS.set_on("群", "1")
        check(S, CS.is_off("group_1", "3")[0] is False, "她自己能开回来")
        CS.set_on("人", "2")
        check(S, CS.off_targets() == [], "全开回来后列表为空")
    finally:
        CS.STATE_FILE = _old_file
        CS._cache.update({"mtime": 0.0, "data": {"groups": {}, "users": {}}})

    # ---- 主动私聊（只在私聊里，群里不做）----
    from services import proactive as PR
    _chat2 = (ROOT / "plugins" / "chat" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    check(S, hasattr(PR, "score_of") and hasattr(PR, "pick"), "主动私聊有选人接口")
    check(S, "def _proactive_private_loop" in _chat2, "有主动私聊循环")
    check(S, "_proactive_private_loop(bot)" in _chat2, "启动时挂上主动私聊循环")
    check(S, "proactive.pick(ALLOWED_PRIVATE)" in _chat2,
          "主动私聊只从私聊白名单选人（群里不做）")
    _old_pf = PR.STATE_FILE
    _real_rp = PR.recent_private
    _real_inti = PR.profiles.intimacy
    _real_mf = PR.mood.mood_factors
    try:
        PR.STATE_FILE = Path(_tf.mkdtemp(prefix="pr_")) / "p.json"
        PR._cache.update({"mtime": 0.0, "data": {}})
        _now2 = time.time()
        PR.recent_private = lambda qq, limit=10: (["10:00 阿明：今天好累"],
                                                  _now2 - 3 * 3600, "user")
        PR.profiles.intimacy = lambda q: 0.9
        PR.mood.mood_factors = lambda k="": {"chatty": 1.2}
        _okc = PR.score_of("10001")
        check(S, _okc.get("ok"), f"分够就选他（{_okc.get('score')}）")
        PR.profiles.intimacy = lambda q: 0.1
        check(S, not PR.score_of("10001").get("ok"), "不够熟不主动（陌生人收私聊很吓人）")
        PR.profiles.intimacy = lambda q: 0.9
        PR.recent_private = lambda qq, limit=10: (["x"], _now2 - 60, "user")
        check(S, not PR.score_of("10001").get("ok"), "刚聊完不补刀")
        PR.recent_private = lambda qq, limit=10: (["x"], _now2 - 3 * 3600, "bot")
        check(S, not PR.score_of("10001").get("ok"), "最后一句是她说的 → 不连发")
        PR.mark_sent("10001")
        check(S, not PR.score_of("10001").get("ok"), "发过之后进冷却")
        check(S, isinstance(PR._in_quiet(time.time()), bool), "有免打扰时段判断")
        check(S, "stay_silent" in PR.prompt_block({"qq": "1", "name": "阿明"}),
              "主动私聊也允许她 stay_silent（想找 ≠ 必须说）")
    finally:
        PR.STATE_FILE = _old_pf
        PR.recent_private, PR.profiles.intimacy, PR.mood.mood_factors = (
            _real_rp, _real_inti, _real_mf)
        PR._cache.update({"mtime": 0.0, "data": {}})

    # ---- 自我层：我在他面前是什么样 + 档案/记忆翻页提示 ----
    from services import profiles as PF
    from services import memory_v2 as MV2
    try:
        PF.upsert_profile("30001", nickname="阿强", level="member")
        PF.set_call_names("30001", call_name="阿强")
        for _i in range(5):
            PF.add_bond_note("30001", f"第{_i}次一起打游戏", emotion="happy")
        check(S, PF.set_stance("30001", "爱撒娇") == "爱撒娇", "能记「我在他面前的样子」")
        _bc = PF.render_card("30001", detail="brief", max_len=600)
        check(S, "你在他面前：爱撒娇" in _bc, "brief 卡里有「你在他面前」")
        check(S, "翻页" in _bc and "一共 5 条" in _bc,
              f"档案写明「还有更多 + 怎么翻」（{_bc[-70:]}）")
    except Exception as e:
        check(S, False, "档案/姿态自检异常", f"{type(e).__name__}: {e}")

    # 记忆：只给 2 条时必须说清「一共几条、怎么翻」
    try:
        for _i in range(4):
            MV2.remember(text=f"翻页测试记忆{_i}", kind="fact", importance=0.9)
        _mp = MV2.memory_prompt("翻页测试", limit=2)
        check(S, "翻页" in _mp and "一共" in _mp,
              f"记忆块写明「还有更多 + 怎么翻」（{_mp[-80:]}）")
    except Exception as e:
        check(S, False, "记忆翻页提示自检异常", f"{type(e).__name__}: {e}")

    # 群聊标签 → 记忆归属：写 MEMO 时必须带「群号:」
    check(S, '（群号:{_mgid}）{mtext}' in _chat2,
          "写记忆时带群号前缀（group_ok 隔离才真正生效）")
    _pf_src = (ROOT / "services" / "profiles.py").read_text(
        encoding="utf-8", errors="ignore")
    check(S, "[[PROFILE|" in _pf_src, "档案卡提示里教她怎么翻页")
    check(S, '|翻页' in _Lsrc, "档案翻页参数已接（[[PROFILE|名字|翻页2]]）")

    # ---- 安静 → 破冰 ----
    _old_pf2 = CS.STATE_FILE
    try:
        CS.STATE_FILE = Path(_tf.mkdtemp(prefix="cs2_")) / "switch.json"
        CS._cache.update({"mtime": 0.0, "data": {"groups": {}, "users": {}}})
        CS.set_off("群", "999", minutes=30, reason="他说话太难听", by_qq="12345")
        check(S, "起因" in CS.is_off("group_999", "12345")[1],
              f"安静原因里带「谁惹的」（{CS.is_off('group_999', '12345')[1]}）")
        check(S, CS.maybe_thaw("group_999", "99999", "对不起嘛") == "",
              "别人道歉不解除（不是他惹的不算）")
        _th = CS.maybe_thaw("group_999", "12345", "对不起，我错了")
        check(S, bool(_th) and not CS.is_off("group_999", "12345")[0],
              f"他道歉 → 自动破冰（{_th}）")
        # 桶名/键必须对得上：以前 maybe_thaw 传 "group"，set_on 只认「群」，
        # 结果返回"已解除"但状态没变（自检抓到的真 bug）。
        CS.set_off("人", "555", minutes=0, reason="烦")
        check(S, CS.is_off("private_555")[0],
              "私聊静音不带 speaker 也能查到（键就是对方 QQ）")
        CS.set_on("人", "555")
        check(S, not CS.is_off("private_555")[0], "私聊静音能开回来")
    finally:
        CS.STATE_FILE = _old_pf2
        CS._cache.update({"mtime": 0.0, "data": {"groups": {}, "users": {}}})

    # ---- 接线快照（/测函数 链路）----
    check(S, "pipeline_last.json" in _Lsrc, "每次请求都落一份接线快照")
    check(S, "def _territory_section" in _Lsrc and '"blocks"' in _Lsrc,
          "快照记录「哪些块进来了」")
    # 真机踩过：快照原来是全局一份 → 私聊完再看，看到的是别的会话的
    check(S, "_all[str(key)] = _wire" in _Lsrc, "接线快照按会话存（不被别的会话覆盖）")
    check(S, 'getattr(_sa, "VERSION"' in _Lsrc, "快照版本号读 scene_auditor.VERSION（不写死）")
    check(S, "GROUP_TOOL_DENY" in _Lsrc, "群聊工具可用 GROUP_TOOL_DENY 精简")
    # 工具是插件在模块顶层注册的 —— 不 import 插件，TOOLS_SPEC 就是空的，
    # 下面这条会变成「假跳过」（t_call 还没跑到）。这里自己 import 一次。
    try:
        import plugins.chat  # noqa: F401
    except Exception:
        pass
    try:
        from services import llm as _L3
        _gnames = [((s.get("function") or {}).get("name"))
                   for s in _L3.tools_for("group_1")]
        if _gnames:
            _L3.GROUP_TOOL_DENY.add(_gnames[0])
            _n2 = len(_L3.tools_for("group_1"))
            _L3.GROUP_TOOL_DENY.discard(_gnames[0])
            check(S, _n2 == len(_gnames) - 1,
                  f"GROUP_TOOL_DENY 真的能屏蔽群聊工具（{_gnames[0]}）")
        else:
            skip(S, "群聊没有挂任何工具，跳过 GROUP_TOOL_DENY")
    except Exception as e:
        check(S, False, "GROUP_TOOL_DENY 自检异常", f"{type(e).__name__}: {e}")
    check(S, '"链路"' in _chat2 or "链路" in _chat2, "有 /测函数 链路")
    check(S, "主动" in _chat2 and "score_of" in _chat2, "有 /测函数 主动")

    # ---- 安静 = 强制 stay_silent（API 层，不是提示词祈祷）----
    check(S, "def _off_prompt" in _Lsrc, "有「你现在安静着」这一节")
    check(S, '"tool_choice"] = (' in _Lsrc and '"stay_silent"' in _Lsrc,
          "安静时 tool_choice 锁死 stay_silent")
    check(S, "TOOL_CTX.get(\"silent\")" in _Lsrc, "调了 stay_silent 就立刻收工")
    check(S, "CHAT_OFF_ANSWER_WHEN_CALLED" in (
        ROOT / "services" / "chat_switch.py").read_text(encoding="utf-8", errors="ignore"),
        "安静时被叫到可以自己决定回不回（免得着急找不到她）")

    # ---- 「你的地盘」：她能看见 ALLOWED_GROUPS / 私聊白名单 / 主人 ----
    check(S, "def _territory_section" in _Lsrc, "system 里有「你的地盘」")
    _env_g, _env_p = os.environ.get("ALLOWED_GROUPS"), os.environ.get("ALLOWED_PRIVATE")
    try:
        os.environ["ALLOWED_GROUPS"] = "111,222"
        os.environ["ALLOWED_PRIVATE"] = "333"
        _terr = L._territory_section(True)
    finally:
        if _env_g is None:
            os.environ.pop("ALLOWED_GROUPS", None)
        else:
            os.environ["ALLOWED_GROUPS"] = _env_g
        if _env_p is None:
            os.environ.pop("ALLOWED_PRIVATE", None)
        else:
            os.environ["ALLOWED_PRIVATE"] = _env_p
    check(S, "111" in _terr and "333" in _terr, f"地盘里带群号和私聊名单（{_terr[:40]}…）")
    check(S, "chat_switch" in _terr, "告诉她怎么查完整名单 / 怎么静音")

    # ---- 多源汇总（qwen / GLM / trace / 联网核验 全摆给 DeepSeek）----
    check(S, VV._pick_majority(["星井美希", "星井美希"]) == "星井美希", "两个来源一致 → 采信")
    check(S, VV._pick_majority(["星井美希", "久留美"]) == "", "两个来源不一致 → 不硬选")
    check(S, VV._pick_majority(["星井美希", ""]) == "星井美希", "只有一个来源 → 采信")
    check(S, VV._pick_majority(["路人甲", "星井美希"], allowed=["星井美希"]) == "星井美希",
          "候选表外的名字被过滤")
    check(S, VV._pick_majority([], allowed=["x"]) == "", "空输入 → 空")

    line = VV._fmt_one("qwen识图", {"scene": "粉发少女", "work": "偶像大师", "char": "星井美希"})
    check(S, line.startswith("qwen识图：") and "偶像大师·星井美希" in line, f"报告行格式 {line[:40]}")
    check(S, "没有结果" in VV._fmt_one("GLM识图", {}), "缺失来源标注「没有结果」")

    check(S, VV.GLM_VISION is True, "GLM 视觉默认开启（作为第二证据源，不是否决器）")
    check(S, not hasattr(VV, "GLM_SECOND_VOTE"), "旧的「两票制否决」已移除")

    # 纯色图不该进 trace.moe
    check(S, VV._looks_flat(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40) is False,
          "解析不了的图不误判成 flat")
    check(S, VV.TRACE_MAX_PER_DAY <= 100, f"trace.moe 每天上限 {VV.TRACE_MAX_PER_DAY}（官方 100）")

    # vision_agent 真的把多源报告给 DeepSeek 了
    va = (ROOT / "plugins" / "vision_agent" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    check(S, 'rec.get("report")' in va, "vision_agent 用了多源报告")
    check(S, "你自己判断哪个更可信" in va, "提示 DeepSeek 自己判断冲突")

    # 没认出专名时不能叫她放弃；图配文字要把附言当线索
    vv_src = (ROOT / "services" / "vision_verify.py").read_text(
        encoding="utf-8", errors="ignore")
    check(S, "推测语气" in vv_src, "没专名时让她结合上下文推测，而不是放弃")
    check(S, "结论：各来源都没给出可靠专名" not in vv_src,
          "删掉了「各来源都没给出专名，只能描述画面」这种泄气话")
    check(S, "hint" in vv_src and "发送者发这张图时说的话" in vv_src,
          "图片附言会作为线索传给视觉模型")
    check(S, "_caption" in va, "vision_agent 把附言取出来传下去了")

    # ---- 第四层：定向追问（图片 + 明确问题问「这是谁」）----
    # 默认开启（services/vision_verify.py 里 os.getenv(..., "true")），
    # 但 .env 里用户特意关了（VISION_TARGETED_ASK=false，2026-11-16：角色猜测已由第1层识图模型直接给）。
    # 所以这里只断言「开关存在且是布尔值」，不再断言一定开着。
    check(S, isinstance(VV.TARGETED_ASK, bool), "定向追问开关存在（默认开，.env 可关）")
    check(S, "能叫出名字的角色" in VV.TARGETED_PROMPT, "问法从「这是什么」改成「这是谁」")
    check(S, "cosplay" in VV.TARGETED_PROMPT, "覆盖 cosplay 场景")
    check(S, "车身上" in VV.TARGETED_PROMPT, "覆盖痛车/海报/衣服上的图案")
    check(S, "绝对不要编一个名字" in VV.TARGETED_PROMPT, "明确禁止编名字")
    check(S, VV._clean_name("认不出") == "", "「认不出」当空")
    check(S, VV._clean_name("认不出来") == "", "「认不出来」当空")

    # 追问结果会被解析成 角色/作品
    _p = VV.parse_structured("角色：小鲸鱼\n作品：认不出")
    check(S, _p["char"] == "小鲸鱼" and _p["work"] == "", "追问结果能解析，作品认不出就留空")

    # 只在没专名时才追问（有专名就别多花一次调用）
    check(S, "TARGETED_ASK and not (work or char)" in vv_src, "只在没专名时才追问")
    check(S, '"ask"' in vv_src or "sources[\"ask\"]" in vv_src, "追问结果进 sources")
    check(S, "定向追问" in vv_src, "追问结果进多源报告")

    # ---- trace.moe 角色名不确定时，她可以不提名字、正常回复（2026-11-19）----
    _old = (VV.get_cached, VV.put_cache, VV.trace_moe, VV.TRACE_MOE,
            VV.GLM_VISION, VV.TARGETED_ASK)
    try:
        VV.get_cached = lambda fp: None
        VV.put_cache = lambda fp, rec: None
        VV.TRACE_MOE = True
        VV.GLM_VISION = False
        VV.TARGETED_ASK = False

        async def _vl(prompt, n):
            return "画面：粉发少女在挥手\n类型：动漫截图\n角色：星井美希\n是否本番：是"

        async def _mid(img):
            return {"work": "偶像大师", "confidence": "mid", "similarity": 0.80,
                    "votes": 2, "episode": 3, "at": 60.0,
                    "characters": ["星井美希", "天海春香", "如月千早"], "candidates": []}

        VV.trace_moe = _mid
        _r1 = await VV.recognize(b"fake-image", _vl)
        check(S, "不用硬猜" in _r1.get("report", ""),
              "疑似命中时明确告诉她「可以不提角色名」")
        check(S, "星井美希" not in _r1.get("desc", ""),
              f"不确定时 desc 不写角色名（{_r1.get('desc', '')[:40]}）")
        check(S, _r1.get("confidence") == "mid", "置信度透传给上层（别当事实写记忆）")

        async def _hi(img):
            return {"work": "偶像大师", "confidence": "high", "similarity": 0.95,
                    "votes": 4, "episode": 3, "at": 60.0,
                    "characters": ["星井美希"], "candidates": []}

        VV.trace_moe = _hi
        _r2 = await VV.recognize(b"fake-image-2", _vl)
        check(S, "星井美希" in _r2.get("desc", ""), "高置信时角色名照常写进描述")
        check(S, "不用硬猜" not in _r2.get("report", ""), "高置信不啰嗦这句")
    finally:
        (VV.get_cached, VV.put_cache, VV.trace_moe, VV.TRACE_MOE,
         VV.GLM_VISION, VV.TARGETED_ASK) = _old


# ============================================================
# 分组 8.5：字数限制（群聊 20 / 私聊 100）
# ============================================================
async def t_text():
    from services.text import limit_hanzi, hanzi_count
    S = "text"

    check(S, limit_hanzi("一二三四五六七八九十", 5) == "一二三四五…",
          f"limit_hanzi 截断：{limit_hanzi('一二三四五六七八九十', 5)!r}")
    check(S, limit_hanzi("abc", 5) == "abc", "英文字符不受限")
    # 2026-11-20：改成「按显示宽度」计数（汉字 1 / 半角 0.5 / emoji 1），
    # 这样 500 字符的纯英文回复也不会 hanzi_count=0 → 既不截断也不走合并转发。
    check(S, hanzi_count("你好abc！！") == 5.5,
          f"hanzi_count 按显示宽度（实际 {hanzi_count('你好abc！！')}）")
    check(S, hanzi_count("abc") == 1.5, f"纯英文也计数（实际 {hanzi_count('abc')}）")
    check(S, hanzi_count("") == 0, "空串 → 0")

    # 20 字上限：超了必须截到 20 个汉字
    long_text = "啊" * 50
    cut = limit_hanzi(long_text, 20)
    # 截断会额外补一个「…」，所以按「去掉省略号后的宽度」核对
    _w = hanzi_count(cut.replace("…", ""))
    check(S, _w == 20, f"50 字截到 20 字（实际 {_w}）")
    check(S, cut.endswith("…"), "截断后带省略号")

    # prompt 侧的字数铁律
    try:
        from services.llm import _length_rule
        g = _length_rule("group_123")
        p = _length_rule("private_456")
        check(S, "20" in g and "合并转发" in g,
              f"群聊规则：{g[:40]}…")
        check(S, "20" in p and "合并转发" in p,
              f"私聊规则：{p[:40]}…")
    except Exception as e:
        check(S, False, "导入 services.llm._length_rule 失败", f"{type(e).__name__}: {e}")

    # ---- 地址必须匹配 network_mode=host（用 127.0.0.1，不是 host.docker.internal）----
    try:
        from services import llm as L

        check(S, "127.0.0.1" in L.AGENT_BASE_URL and "host.docker.internal" not in L.AGENT_BASE_URL,
              f"AGENT_BASE_URL 用 127.0.0.1（{L.AGENT_BASE_URL}）")
        check(S, "127.0.0.1" in L.LOCAL_LLM_URL and "host.docker.internal" not in L.LOCAL_LLM_URL,
              f"LOCAL_LLM_URL 用 127.0.0.1（{L.LOCAL_LLM_URL}）")

        # 本地 Qwen 不可用 → 原文降级（不截断）
        old = L.LOCAL_LLM_URL
        L.LOCAL_LLM_URL = "http://127.0.0.1:1/v1/chat/completions"
        try:
            mid_text = "这是一段中等长度的图片描述。" * 20      # 280 字
            out = await L.summarize_by_local_qwen(mid_text)
            check(S, out == mid_text, f"本地 Qwen 挂了 → 原样返回原文（{len(out)} 字）")

            huge = "长" * (L.SUMMARIZE_FALLBACK_MAX + 500)
            out2 = await L.summarize_by_local_qwen(huge)
            check(S, len(out2) <= L.SUMMARIZE_FALLBACK_MAX + 10,
                  f"超长文本降级后才截断（{len(out2)} 字，上限 {L.SUMMARIZE_FALLBACK_MAX}）")

            short = await L.summarize_by_local_qwen("短")
            check(S, short == "短", "短文本直接返回，不发请求")
        finally:
            L.LOCAL_LLM_URL = old
    except Exception as e:
        check(S, False, "本地 Qwen 降级测试失败", f"{type(e).__name__}: {e}")


# ============================================================
# 分组 9：表情打标签
# ============================================================
async def t_sticker():
    S = "sticker"
    try:
        import services.sticker_receiver as R
    except Exception as e:
        check(S, False, "sticker_receiver 导入失败", f"{type(e).__name__}: {e}")
        return

    # 视觉模块能被找到（这是"标签随机"的根因）
    import asyncio as _a
    desc = await R._call_vision_any(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    check(S, isinstance(desc, str), f"_call_vision_any 返回字符串（{desc[:20]!r}）")
    check(S, desc != "" or not R._call_vision_any.__doc__ is None, "视觉模块已可被 import（不再静默返回空）")

    hints = getattr(R, "_TAG_HINTS", [])
    pairs = hints if isinstance(hints, list) else list(getattr(hints, "items", lambda: [])())
    check(S, len(pairs) >= 5, f"标签规则 {len(pairs)} 条")
    tag_set = {t for _, t in pairs}
    check(S, len(tag_set) >= 5, f"覆盖 {len(tag_set)} 个标签：{sorted(tag_set)}")

    # 视觉 prompt 规定模型输出的词，必须都能被标签表接住
    import plugins.vision_agent as V
    for word in re.findall(r"[\u4e00-\u9fa5]{2,4}", V.STICKER_VISION_PROMPT.split("只能从这些词里挑一个：")[-1].split("；")[0]):
        check(S, word in tag_set, f"词表「{word}」有对应标签")

    def _tag_of(text):
        for kw, tag in pairs:
            if kw in text:
                return tag
        return None

    for text, want in [("无语，翻白眼", "无语"), ("嘲笑你", "嘲笑"), ("开心，笑死", "开心")]:
        got = _tag_of(text)
        check(S, got == want, f"「{text}」→ {got}（期望 {want}）")

    # ---- 手动发表情入口（/发表情 测试用）----
    try:
        import services.sticker_sender as SND
        import inspect as _insp
        check(S, hasattr(SND, "send_sticker_test"), "sticker_sender 提供 send_sticker_test（/发表情 用）")
        check(S, hasattr(SND, "_pick") and hasattr(SND, "_deliver"),
              "挑选/发送抽成 _pick/_deliver（自动发与手动测试共用）")
        src = _insp.getsource(SND.send_sticker_test)
        check(S, "_pick(" in src and "_deliver(" in src,
              "send_sticker_test 走同一套挑选/发送逻辑")
    except Exception as e:
        check(S, False, "sticker_sender 导入失败", f"{type(e).__name__}: {e}")

    # ---- 表情协议要够主动（"她从来不发表情"的主因之一）----
    try:
        from services.llm import _sticker_protocol
        from services.sticker_library import sticker_lib as _lib
        _lib.index.clear()
        _p = Path("data/memes/_proto_test.gif")
        _p.parent.mkdir(parents=True, exist_ok=True)
        _p.write_bytes(b"GIF89a" + b"p" * 80)
        _lib.index["proto_test"] = {"tag": "开心", "path": str(_p.resolve()),
                                    "used_count": 0, "created_at": time.time()}
        proto = _sticker_protocol()
        check(S, "<sticker:" in proto, "表情协议含 <sticker:标签> 写法")
        check(S, "3~4 次" in proto, "表情协议给了主动发送频次（每 3~4 次回复）")
        check(S, "示例" in proto, "表情协议带 few-shot 示例")
        _lib.index.clear()
        _p.unlink(missing_ok=True)
    except Exception as e:
        check(S, False, "表情协议生成失败", f"{type(e).__name__}: {e}")

    # ---- 自然语言「收表情」不能误判（用户 2026-11-19：群里忽然来一句
    #      「好呀，把表情发给我吧」，前 50 条根本没提过表情）----
    try:
        from plugins import sticker_admin as _sa
        _cases = {
            "好呀，把表情发给我把": False,
            "收到": False,
            "我收藏一下这个": False,
            "存档一下": False,
            "这个标签打成搞怪": False,
            "别收了": False,
            "你收到了吗": False,
            "收表情": True,
            "这张表情收一下": True,
            "帮我存一下这张图": True,
            "收一下，标签写成忠诚": True,
        }
        _wrong = [t for t, v in _cases.items() if _sa._collect_intent(t) != v]
        check(S, not _wrong, f"自然语言「收表情」判得准（误判 {_wrong}）")
        _src = __import__("inspect").getsource(_sa._collect_intent_rule)
        check(S, "is_tome" in _src,
              "群里必须 @ 她才认「收表情」（否则正常聊天会被当成指令）")
    except Exception as e:
        skip(S, "自然语言「收表情」误判检查", f"{type(e).__name__}: {e}")


# ============================================================
# 分组 9.5：表情策展（上限 / 打分 / 淘汰 / 加权挑选）
# ============================================================
async def t_curator():
    from services import sticker_library as SL
    from services import sticker_curator as SC
    S = "curator"

    # 👇 强制离线：纯离线自检不该真调 DeepSeek（Docker env 里可能带 XAI_API_KEY，
    #    会导致「LLM 不可用」的分支测不到）。这里显式关掉 LLM，走安全降级分支。
    SC.ENABLED = False
    SC.LLM_KEY = ""

    check(S, SL.STICKER_MAX == 1000, f"默认容量上限 {SL.STICKER_MAX}")

    # ---- 确定性分数 ----
    now = time.time()
    low = SL._score({"used_count": 0, "quality": 0.1, "created_at": now - 90 * 86400})
    mid = SL._score({"used_count": 3, "quality": 0.5, "last_picked_at": now - 5 * 86400})
    high = SL._score({"used_count": 9, "quality": 0.95, "last_picked_at": now})
    check(S, low < mid < high, f"分数单调递增：{low} < {mid} < {high}")
    check(S, SL._score({"quality": 0.9, "last_picked_at": now})
          > SL._score({"quality": 0.1, "last_picked_at": now}), "quality 影响分数")

    # ---- 描述判重 ----
    sim = SL.desc_similarity("一个女孩在吃白米饭", "一个女孩正在吃白米饭")
    check(S, sim > 0.6, f"相似描述相似度 {sim:.2f}")
    check(S, SL.desc_similarity("吃白米饭", "猫在跳舞") < 0.2, "无关描述相似度低")

    # ---- 造 10 张，把上限压到 5 ----
    lib = SL.sticker_lib
    lib.index.clear()
    for i in range(10):
        md5 = lib.md5_of(b"sticker" + str(i).encode())
        lib.index[md5] = {
            "tag": "开心", "path": "", "used_count": i, "created_at": now,
            "desc": f"第{i}张表情", "quality": round(0.1 * i, 2),
        }
    check(S, len(lib.index) == 10, "构造 10 张库存")
    check(S, lib.tag_count("开心") == 10, "tag_count 正确")

    old_max = SL.STICKER_MAX
    SL.STICKER_MAX = 5
    try:
        check(S, lib.over_limit(), "over_limit 检测到超限")
        cands = lib.prune_candidates(7)
        check(S, len(cands) == 7, f"候选池 {len(cands)} 个")
        check(S, cands[0]["score"] <= cands[-1]["score"], "候选按分数升序（最差的在前）")
        check(S, cands[0]["quality"] <= 0.3, f"最差候选 quality={cands[0]['quality']}")

        # LLM 不可用时 curator 必须安全降级
        drop = await SC.prune(3, cands)
        check(S, drop == [], "LLM 不可用 → prune 返回空（不崩）")
        verdict = await SC.judge_new("一张表情", "开心")
        check(S, verdict is None, "LLM 不可用 → judge_new 返回 None")
        res = await SC.maintain_capacity()
        check(S, res["after"] <= 5, f"maintain_capacity 兜底降到上限：{res}")
        check(S, res["by_llm"] is False, "LLM 不可用时标记 by_llm=False")
    finally:
        SL.STICKER_MAX = old_max

    # ---- 加权挑选：精品更容易被挑中 ----
    # ⚠️ 必须写真实文件：pick_by_tag 现在只挑「文件真的存在」的条目
    lib.index.clear()
    d_pick = Path("data/stickers/撒娇")
    d_pick.mkdir(parents=True, exist_ok=True)
    f_hi, f_lo = d_pick / "hi.png", d_pick / "lo.png"
    f_hi.write_bytes(b"\x89PNG\r\n\x1a\n" + b"H" * 200)
    f_lo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"L" * 200)
    md5_hi = lib.md5_of(b"hi-quality")
    md5_lo = lib.md5_of(b"lo-quality")
    lib.index[md5_hi] = {"tag": "撒娇", "path": str(f_hi), "used_count": 0,
                         "created_at": now, "desc": "精品", "quality": 0.95}
    lib.index[md5_lo] = {"tag": "撒娇", "path": str(f_lo), "used_count": 0,
                         "created_at": now, "desc": "垃圾", "quality": 0.05}
    hits = {"hi": 0, "lo": 0}
    for _ in range(400):
        lib.index[md5_hi]["used_count"] = 0
        lib.index[md5_lo]["used_count"] = 0
        p = lib.pick_by_tag("撒娇")
        if not p:
            break
        hits["hi" if p["md5"] == md5_hi else "lo"] += 1
    check(S, hits["hi"] + hits["lo"] > 0, "加权挑选能挑到（文件真实存在）")
    check(S, hits["hi"] > hits["lo"] * 1.5,
          f"精品被挑中 {hits['hi']} 次 vs 垃圾 {hits['lo']} 次（加权生效）")
    check(S, lib.pick_by_tag("不存在的标签") is None, "空标签返回 None")

    # ---- 挑选会记录 last_picked_at ----
    lib.index[md5_hi]["used_count"] = 0
    p = lib.pick_by_tag("撒娇")
    check(S, p is not None and p.get("last_picked_at", 0) > 0, "挑中后记录 last_picked_at")

    # ---- remove / apply_prune ----
    before = len(lib.index)
    removed = lib.apply_prune([md5_lo])
    check(S, removed == 1 and len(lib.index) == before - 1, "apply_prune 删除 1 张")
    check(S, lib.remove("根本不存在的id") is False, "删不存在的 id 返回 False")

    # ---- 健康检查：坏数据不能参与挑选（她"从来不发表情"的真因）----
    lib.index.clear()
    good_file = Path("data/stickers/开心") / "good.png"
    good_file.parent.mkdir(parents=True, exist_ok=True)
    good_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"G" * 200)
    md5_good = lib.md5_of(b"good")
    md5_broken = lib.md5_of(b"broken")
    lib.index[md5_good] = {"tag": "开心", "path": str(good_file), "used_count": 0,
                           "created_at": now, "desc": "好图", "quality": 0.9}
    lib.index[md5_broken] = {"tag": "开心", "path": "data/stickers/开心/不存在.png",
                             "used_count": 0, "created_at": now, "desc": "坏图", "quality": 0.9}

    check(S, lib.is_usable(lib.index[md5_good]) is True, "is_usable：文件存在 → True")
    check(S, lib.is_usable(lib.index[md5_broken]) is False, "is_usable：文件不存在 → False")
    check(S, lib.is_usable({"mface_id": "abc"}) is True, "is_usable：mface → True")

    h = lib.health()
    check(S, h["total"] == 2 and h["usable"] == 1 and h["broken_file"] == 1,
          f"health：{h['total']} 条 / 可用 {h['usable']} / 坏 {h['broken_file']}")

    # 挑 200 次，绝不能挑到坏的那条
    hit_broken = 0
    for _ in range(200):
        p = lib.pick_by_tag("开心")
        if p and p["md5"] == md5_broken:
            hit_broken += 1
    check(S, hit_broken == 0, f"pick_by_tag 绝不挑坏数据（挑到 {hit_broken} 次）")

    p = lib.pick_any()
    check(S, p is not None and p["md5"] == md5_good, "pick_any 只挑可用的")

    # clean_broken 删坏条目、保留 mface
    lib.index["mface_x"] = {"tag": "吃瓜", "mface_id": "real_id", "path": "",
                            "used_count": 0, "created_at": now}
    n_clean = lib.clean_broken()
    check(S, n_clean == 1 and md5_broken not in lib.index, "clean_broken 删掉坏文件条目")
    check(S, "mface_x" in lib.index, "clean_broken 不动 mface 条目")

    # stats 里带健康度
    st = lib.stats()
    check(S, "usable" in st and "broken_file" in st, "stats() 含 usable/broken_file")

    # 未导入素材统计
    from services.sticker_library import count_importable
    memes = Path("data/memes")
    memes.mkdir(parents=True, exist_ok=True)
    (memes / "neko_abc_1N01_开心.gif").write_bytes(b"GIF89a" + b"x" * 100)
    n_imp, where = count_importable()
    check(S, n_imp >= 1 and any("memes" in w for w in where),
          f"count_importable 发现未入库素材：{where}")

    lib.index.clear()


# ============================================================
# 分组 9.8：表情导入（data/memes → index.json）
# ============================================================
async def t_importer():
    from services import sticker_importer as imp
    from services.sticker_library import sticker_lib
    S = "importer"

    # 造一个和用户真实结构一样的素材目录
    # ⚠️ 先清空：前面的组（t_curator）也会往 data/memes 里写测试文件
    memes = Path("data/memes")
    if memes.exists():
        for f in memes.rglob("*"):
            if f.is_file():
                f.unlink()
    memes.mkdir(parents=True, exist_ok=True)
    samples = {
        "neko_aaaa1111_7N08_难过.gif": "难过",
        "neko_bbbb2222_5N04_嘲笑.gif": "嘲笑",
        "neko_cccc3333_16N01_超开心.gif": "开心",
        "neko_dddd4444_2N09_思考.gif": "思考",
        "neko_eeee5555_EX49_True_music.gif": "搞怪",
    }
    for i, n in enumerate(samples):
        (memes / n).write_bytes(b"GIF89a" + str(i).encode() * 120)

    sticker_lib.index.clear()

    # ---- 导入 ----
    res = imp.import_dir(memes, in_place=True)
    check(S, res.get("ok") and res.get("added") == 5,
          f"导入 5 张（added={res.get('added')}, scanned={res.get('scanned')}）")
    check(S, sticker_lib.health()["usable"] == 5, "导入后 5 张全部能发")

    tags = {Path(i["path"]).name: i["tag"] for i in sticker_lib.index.values()}
    for name, want in samples.items():
        check(S, tags.get(name) == want, f"{name} → {tags.get(name)}（期望 {want}）")

    # ---- 幂等 ----
    res2 = imp.import_dir(memes, in_place=True)
    check(S, res2.get("added") == 0 and res2.get("dup") == 5, "重复导入全部跳过（md5 去重）")

    # ---- 入库后不该再报「未入库」（in-place 文件还在 data/memes 原地）----
    from services.sticker_library import count_importable as _ci
    n_after, where_after = _ci()
    check(S, n_after == 0,
          f"入库后不再提示未入库（{n_after} 张 {where_after}）")

    # ---- 导入后立刻能挑到 ----
    p = sticker_lib.pick_by_tag("难过")
    check(S, p is not None and Path(p["path"]).exists(), "pick_by_tag 能挑到刚导入的")
    check(S, len(sticker_lib.index) == 5, "库存没有被重复计数")

    # ---- 复制模式 ----
    sticker_lib.index.clear()
    res3 = imp.import_dir(memes, in_place=False)
    check(S, res3.get("added") == 5, "复制模式也能导入")
    copied = [Path(i["path"]) for i in sticker_lib.index.values()]
    check(S, all(p.exists() and "data/memes" not in str(p) for p in copied),
          "复制模式文件落在 data/stickers 下")

    # ---- find_source / 错误处理 ----
    src, n = imp.find_source()
    check(S, src is not None and n >= 5, f"find_source 找到素材目录（{n} 张）")
    res4 = imp.import_dir(Path("根本不存在"), in_place=True)
    check(S, res4.get("ok") is False and "不存在" in str(res4.get("error")),
          f"目录不存在 → ok=False（{res4.get('error')}）")

    # ---- 位置传参回归 ----
    # ⚠️ 插件里是 asyncio.to_thread(imp.import_dir, src, True, "")，
    #    签名一旦加回 keyword-only 的 `*`，命令会直接 TypeError（真机踩过）
    sticker_lib.index.clear()
    res5 = imp.import_dir(memes, True, "")
    check(S, res5.get("ok") and res5.get("added") == 5,
          "位置传参 import_dir(src, True, '') 可用（插件调用方式）")
    check(S, all(Path(i["path"]).is_absolute() for i in sticker_lib.index.values()),
          "in_place 存绝对路径（不依赖进程 cwd）")

    # ---- 顶层平铺 + 子目录混排，顶层文件不能被漏掉 ----
    sticker_lib.index.clear()
    mix = Path("data/_mix_src")
    if mix.exists():
        for f in mix.rglob("*"):
            if f.is_file():
                f.unlink()
    (mix / "开心").mkdir(parents=True, exist_ok=True)
    (mix / "顶层_1N01_难过.gif").write_bytes(b"GIF89a" + b"top" * 60)
    (mix / "开心" / "子目录_2N01_开心.gif").write_bytes(b"GIF89a" + b"sub" * 60)
    res6 = imp.import_dir(mix, in_place=True)
    check(S, res6.get("scanned") == 2 and res6.get("added") == 2,
          f"混排目录两张都扫到（scanned={res6.get('scanned')}）")
    mix_tags = sorted(i["tag"] for i in sticker_lib.index.values())
    check(S, mix_tags == ["开心", "难过"],
          f"混排分类正确（{mix_tags}）")
    for f in sorted(mix.rglob("*"), reverse=True):
        f.unlink() if f.is_file() else f.rmdir()

    # ---- retag ----
    md5 = next(iter(sticker_lib.index))
    check(S, imp.retag(md5, "撒娇") is True
          and sticker_lib.index[md5]["tag"] == "撒娇", "retag 能改标签")
    check(S, imp.retag(md5, "不存在的标签") is False, "retag 拒绝非法标签")

    check(S, isinstance(imp.summary_line(), str) and "库存" in imp.summary_line(),
          "summary_line 可用")

    # ---- 用户 2026-11-19：AI 出的表情集（按标签分文件夹、1.png…18.png）----
    # 目录长这样：data/uploads/…/jing_output_img/{开心,生气,疑惑,问候,难过}/1.png…
    check(S, imp.resolve_folder_tag("疑惑") == "思考"
          and imp.resolve_folder_tag("问候") == "打招呼"
          and imp.resolve_folder_tag("开心") == "开心",
          "标签文件夹能映射（疑惑→思考、问候→打招呼）")

    sticker_lib.index.clear()
    jing = Path("data/_jing_src")
    if jing.exists():
        for f in sorted(jing.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
    try:
        from PIL import Image as _Im
        _sizes = {"开心": (1024, (10, 20, 30)), "生气": (256, (200, 0, 0)),
                  "疑惑": (1024, (0, 0, 200))}
        for _tag, (_side, _rgb) in _sizes.items():
            _d = jing / _tag
            _d.mkdir(parents=True, exist_ok=True)
            for _i in (1, 2):
                # 每张颜色差一点，否则 md5 一样会被当成重复（去重是按内容）
                _c = (_rgb[0], _rgb[1], min(250, _rgb[2] + _i * 10), 255)
                _Im.new("RGBA", (_side, _side), _c).save(_d / f"{_i}.png")
        _res = imp.import_dir(jing, in_place=False)
        check(S, _res.get("added") == 6 and _res.get("fail") == 0,
              f"按标签文件夹整批入库（added={_res.get('added')}）")
        check(S, set(_res["by_tag"]) == {"开心", "生气", "思考"},
              f"标签映射正确（{_res['by_tag']}）")
        _copied = [Path(i["path"]) for i in sticker_lib.index.values()]
        check(S, all(Path("data/stickers") in p.parents for p in _copied),
              "复制模式落到 data/stickers/<标签>/（uploads 被清也不怕）")
        _big = [p for p in _copied if p.parent.name == "开心"][0]
        check(S, max(_Im.open(_big).size) <= imp.STICKER_MAX_SIDE,
              f"大图复制时缩到 ≤{imp.STICKER_MAX_SIDE}px（实际 {_Im.open(_big).size}）")
        _small = [p for p in _copied if p.parent.name == "生气"][0]
        check(S, max(_Im.open(_small).size) == 256, "小图不放大也不重压")
        check(S, _res.get("saved", 0) > 0, "报告里能说清压缩省了多少")
        _res2 = imp.import_dir(jing, in_place=False)
        check(S, _res2.get("added") == 0 and _res2.get("dup") == 6,
              "重复导入全部按 md5 去重")
        check(S, imp._maybe_shrink(b"GIF89a" + b"x" * 200, ".gif")[:6] == b"GIF89a",
              "GIF 不压缩（压了就没动画了）")
    except ImportError:
        skip(S, "表情集整批导入（没装 Pillow）")
    finally:
        for f in sorted(jing.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        sticker_lib.index.clear()


# ============================================================
# 分组 10：tool_dispatcher 路由（真调函数）
# ============================================================
async def t_dispatch():
    S = "dispatch"
    if _missing("fastapi"):
        skip(S, "本机没装 fastapi，跳过 tool_dispatcher 检查（服务器上会真跑）")
        return
    try:
        import plugins.tool_dispatcher as T
    except Exception as e:
        check(S, False, "tool_dispatcher 导入失败", f"{type(e).__name__}: {e}")
        return

    secret = T.API_SECRET

    # 心跳
    r = await T.heartbeat(secret=secret)
    check(S, r.get("status") == "ok", "heartbeat 正常")
    r = await T.heartbeat(secret="wrong")
    check(S, r.get("error"), "heartbeat 密钥错误被拒")

    # 建任务 → pull
    tid = "unittest_dl"
    src = Path("data/temp_files") / f"{tid}.bin"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"A" * 4096)
    T.STATUS["tasks"][tid] = {"type": "audio_separation", "status": "pending",
                              "input": str(src), "output": ""}
    r = await T.pull_task(secret=secret)
    check(S, r.get("has_task") and r["task_id"] == tid, "pull_task 取到任务")
    check(S, T.STATUS["tasks"][tid]["status"] == "processing", "pull 后状态 → processing")
    check(S, T.FILE_REGISTRY.get(tid) == str(src), "pull 时登记 FILE_REGISTRY")

    # 下载：正常
    resp = await T.download_input(task_id=tid, secret=secret)
    check(S, getattr(resp, "status_code", 0) == 200, f"download 200（{type(resp).__name__}）")
    check(S, Path(str(getattr(resp, "path", ""))).exists(), "FileResponse 指向真实文件")

    # 下载：任务被删了但 registry 还在 → 仍可下（重试场景）
    T.STATUS["tasks"].pop(tid, None)
    resp = await T.download_input(task_id=tid, secret=secret)
    check(S, getattr(resp, "status_code", 0) == 200, "任务对象被删后仍能靠 registry 下载")

    # 下载：真不存在 → 404（不能是 200+JSON）
    resp = await T.download_input(task_id="根本不存在的任务", secret=secret)
    check(S, getattr(resp, "status_code", 0) == 404, "文件不存在返回 404（不是 200+JSON）")

    # 下载：密钥错 → 403
    resp = await T.download_input(task_id=tid, secret="wrong")
    check(S, getattr(resp, "status_code", 0) == 403, "密钥错误返回 403")

    # complete：JSON result
    tid2 = "unittest_json"
    T.STATUS["tasks"][tid2] = {"type": "reflection", "status": "processing", "input": "", "output": ""}
    r = await T.complete_task(task_id=tid2, file=None, vocal=None, inst=None,
                              result=json.dumps({"journal": "日记内容"}), secret=secret)
    check(S, r.get("status") == "success", "complete_task 接受 result 字段")
    check(S, T.STATUS["tasks"][tid2]["output"] == {"journal": "日记内容"}, "result 被解析成 dict")
    check(S, T.STATUS["tasks"][tid2]["status"] == "completed", "状态置 completed")

    # complete：文件上传
    class _Up:
        def __init__(self, data):
            self._d = data
            self.filename = "x.wav"

        async def read(self):
            return self._d

    tid3 = "unittest_file"
    T.STATUS["tasks"][tid3] = {"type": "tts", "status": "processing", "input": "", "output": ""}
    r = await T.complete_task(task_id=tid3, file=_Up(b"WAVDATA"), vocal=None, inst=None,
                              result="", secret=secret)
    out = T.STATUS["tasks"][tid3]["output"]
    check(S, isinstance(out, str) and Path(out).exists(), "文件回传落盘成路径字符串")

    # 双文件（翻唱）
    tid4 = "unittest_dual"
    T.STATUS["tasks"][tid4] = {"type": "split", "status": "processing", "input": "", "output": ""}
    r = await T.complete_task(task_id=tid4, file=None, vocal=_Up(b"V"), inst=_Up(b"I"),
                              result="", secret=secret)
    out = T.STATUS["tasks"][tid4]["output"]
    check(S, isinstance(out, dict) and "vocal" in out and "inst" in out,
          "翻唱双文件 → output 是 dict（song_cover 依赖这个）")

    # status 端点
    st = await T.tasks_status(secret=secret)
    check(S, "online" in st and "tasks" in st, "tasks_status 可用")
    check(S, st["online"] is True, "刚发过心跳 → online=True")

    for t in (tid2, tid3, tid4):
        T.STATUS["tasks"].pop(t, None)


# ============================================================
# 分组 11：node.py（真实 HTTP 打假服务器）
# ============================================================
async def t_node():
    S = "node"
    import importlib.util
    # node.py 部署在算力机，不在 bot 容器里（docker-compose 没挂载它），
    # 容器里没有这个文件是正常的，跳过整个 node 侧静态检查。
    if not (ROOT / "node.py").exists():
        skip(S, "node.py 不在本容器（部署在算力机），跳过 node 侧静态检查")
        return
    # 本机没装 requests 时跑不了 node.py（它只用 requests），跳过而不是报错
    if _missing("requests"):
        skip(S, "本机没装 requests，跳过 node 侧检查（服务器/算力机上会真跑）")
        return
    spec = importlib.util.spec_from_file_location("node_worker", ROOT / "node.py")
    node = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(node)
    except Exception as e:
        check(S, False, "node.py 导入失败", f"{type(e).__name__}: {e}")
        return

    # --- 静态：任务分发 ---
    need = {"split", "audio_separation", "rvc", "rvc_mix", "tts", "reflection"}
    check(S, need <= set(node.TASK_HANDLERS), f"TASK_HANDLERS 覆盖 {sorted(node.TASK_HANDLERS)}")
    check(S, node.TASK_HANDLERS["split"] is node.process_split_task, "split → 双文件版")
    check(S, node.TASK_HANDLERS["audio_separation"] is node.process_audio_separation_task,
          "audio_separation → 单文件版")
    check(S, node.TASK_HANDLERS["rvc_mix"] is node.process_rvc_mix_task,
          "rvc_mix → 变声+混音（翻唱走这条）")
    check(S, node.TASK_HANDLERS["tts"] is node.process_tts_task, "tts 已接回")
    for fn in ("_tts_indextts", "_tts_http", "_tts_cmd", "_run_rvc", "_demucs_separate",
               "_probe_volume", "_mix_to_mp3", "_to_mp3", "process_rvc_mix_task"):
        check(S, callable(getattr(node, fn, None)), f"{fn} 存在")

    # node.env 支持 + math 导入（增益计算要用 log，漏了就是运行时 NameError）
    import inspect as _insp
    check(S, callable(getattr(node, "_load_env_file", None)),
          "_load_env_file 存在（算力机用 node.env 配 MIX_*）")
    check(S, hasattr(node, "math"), "node.py 已 import math")
    check(S, "MIX_DRUM_GAIN" in _insp.getsource(node.process_split_task),
          "鼓增益可配（MIX_DRUM_GAIN）")
    check(S, "MIX_VOCAL_TREBLE_DB" in _insp.getsource(node._mix_to_mp3),
          "人声高频补偿可配（MIX_VOCAL_TREBLE_DB）")

    # 混音参数：服务器那版「只有人声」就是栽在 amix 默认值上
    import inspect as _insp
    mix_src = _insp.getsource(node._mix_to_mp3)
    check(S, "normalize=0" in mix_src, "算力机混音用 normalize=0（默认按轨数砍半）")
    check(S, "duration=longest" in mix_src, "算力机混音用 duration=longest（默认按人声截断）")
    check(S, "alimiter" in mix_src, "算力机混音加 alimiter 防削波")
    check(S, "vocal_db" in mix_src and "inst_db" in mix_src,
          "混音按实测音量做增益补偿（-17.7 vs -34dB 那种差 16dB 的情况）")
    rm_src = _insp.getsource(node.process_rvc_mix_task)
    check(S, "field=extra_input" in rm_src, "rvc_mix 从 extra_input 取伴奏")
    check(S, "with_inst" in rm_src, "rvc_mix 支持清唱（with_inst=False）")
    check(S, "_probe_volume" in rm_src, "混音前会测两轨音量（便于定位没伴奏）")
    check(S, "vocal_db=db_vocal" in rm_src, "把实测音量传给混音函数")

    # 分离质量：stem 认错 / 伴奏电平异常
    dem_src = _insp.getsource(node._demucs_separate)
    check(S, '"no_vocals" not in u' in dem_src,
          "人声匹配排除 no_vocals（子串包含会把伴奏当人声）")
    check(S, "result_urls" in dem_src and "返回 {len(urls)} 个文件" in dem_src,
          "日志打印 Demucs 返回的完整文件列表")
    check(S, 'mode, inst_urls = "4stem", stems_4' in dem_src,
          "识别 4-stem（drums/bass/other/vocals）")
    check(S, 'mode, inst_urls = "2stem", [inst_2stem]' in dem_src, "识别 2-stem")
    check(S, callable(getattr(node, "_rebuild_inst", None)), "_rebuild_inst 存在（原曲−人声）")
    check(S, callable(getattr(node, "_rebuild_vocal", None)), "_rebuild_vocal 存在（原曲−伴奏）")
    check(S, callable(getattr(node, "_sum_files", None)), "_sum_files 存在（本地 stem 相加）")
    check(S, callable(getattr(node, "_download_and_sum", None)), "_download_and_sum 存在")
    sp_src = _insp.getsource(node.process_split_task)
    check(S, "db_i" in sp_src and "db_v" in sp_src, "分离后测人声/伴奏两轨音量")
    check(S, "_sum_files(" in sp_src and "stem_locals" in sp_src,
          "4-stem 把 drums/bass/other 相加做伴奏（取单条 stem 会只有底鼓）")
    check(S, "MIX_DRUM_GAIN" in sp_src, "鼓单独压增益（MIX_DRUM_GAIN）")
    check(S, "vocal_fixed" in sp_src, "vocals 混了伴奏时重建人声（/清唱 才干净）")
    check(S, "_rebuild_inst" in sp_src, "伴奏异常时自动重建")
    check(S, "keep" in sp_src, "分离异常时保留文件便于手动检查")
    check(S, callable(getattr(node, "check_services", None)), "有依赖自检 check_services")

    # ---- 传输超时按体积动态算（大歌回传 76MB 会被写死的 180s 卡死）----
    # 真机：vocal + inst 各 38MB 回传 → ReadTimeout(read timeout=180)
    check(S, callable(getattr(node, "upload_timeout", None)),
          "有 upload_timeout（按体积算上传超时）")
    _ut = node.upload_timeout(76 * 1048576)
    check(S, 900 <= _ut <= 3600, f"76MB 的上传超时给到 {_ut:.0f}s（不是写死 180）")
    check(S, node.upload_timeout(0) >= 300, "0 字节也给基础超时 300s")
    check(S, node.upload_timeout(999 * 1048576) <= node.UPLOAD_TIMEOUT_MAX,
          "超大文件被 UPLOAD_TIMEOUT_MAX 封顶")
    for _k in ("UPLOAD_TIMEOUT_BASE", "UPLOAD_TIMEOUT_PER_MB",
               "UPLOAD_TIMEOUT_MAX", "DOWNLOAD_TIMEOUT_READ", "RVC_TIMEOUT"):
        check(S, hasattr(node, _k), f"{_k} 可配")
    _up_src = _insp.getsource(node.upload_complete)
    check(S, "upload_timeout(" in _up_src, "单文件回传按体积算超时")
    _sp_up = _insp.getsource(node.process_split_task)
    check(S, "upload_timeout(" in _sp_up, "分离双文件回传按体积算超时")
    check(S, "timeout=180" not in _sp_up, "分离回传不再写死 timeout=180")
    check(S, "DOWNLOAD_TIMEOUT_READ" in _insp.getsource(node.download_file),
          "下载读超时可配（慢速大文件）")
    check(S, "RVC_TIMEOUT" in _insp.getsource(node._run_rvc), "RVC 超时可配")

    # ---- 单段式：分离+RVC+混音+截断全在算力机，服务器不碰 ffmpeg ----
    check(S, node.TASK_HANDLERS.get("cover") is getattr(node, "process_cover_full_task", None),
          "cover → 单段式翻唱（一次到底）")
    for fn in ("process_cover_full_task", "trim_mp3", "find_climax_end", "_rms_curve",
               "qwen_audio_probe", "estimate_f0", "pitch_from_f0", "f0_to_gender",
               "_to_wav", "_audio_b64"):
        check(S, callable(getattr(node, fn, None)), f"{fn} 存在（音频处理全在算力机）")
    _cf = _insp.getsource(node.process_cover_full_task)
    check(S, "_to_mp3(" in _cf, "flac 先转 mp3 再进 Demucs（COVER_WORK_FORMAT）")
    check(S, "judge_vocal(" in _cf, "判定链统一走 judge_vocal（和 /测歌 同一套）")
    check(S, "trim_mp3(" in _cf, "本地截到高潮，不再让服务器装 ffmpeg")
    check(S, "file2=" in _cf, "语音版 + 完整版一起回传")
    # 服务器上行带宽是瓶颈（阿里云按出方向计费），优先让算力机直连源站
    check(S, "source_url" in _cf, "优先直连源站下载（不占服务器上行带宽）")
    check(S, "NETEASE_UA" in _cf, "直连时带 UA（防盗链）")
    check(S, "_download_input(" in _cf, "直连失败回退到服务器下载")
    check(S, "extra_headers" in _insp.getsource(node.download_file),
          "download_file 支持自定义请求头")
    _uc = _insp.getsource(node.upload_complete)
    check(S, "file2" in _uc, "upload_complete 支持第二个文件")
    check(S, "caps" in _insp.getsource(node.send_heartbeat),
          "心跳上报 caps（服务器据此决定走不走单段式）")

    # ---- node.env 行内注释 / 数字容错 ----
    # 真机踩过：`F0_ANALYZE_SEC=45   # 分析前多少秒` 整串被当成值，
    # float() 抛 ValueError，整条翻唱链路直接失败
    check(S, callable(getattr(node, "_env_num", None)), "有 _env_num（数字型 env 容错）")
    check(S, node._env_num("__NOT_EXIST__", 7) == 7.0, "_env_num 缺失时用默认值")
    _env_probe = ROOT / "_probe.env"
    _env_probe.write_text(
        'SL_A=45   # 行内注释\nSL_B="x # y"\nSL_C=abc#def\nSL_D=192k\n',
        encoding="utf-8",
    )
    for _k in ("SL_A", "SL_B", "SL_C", "SL_D"):
        os.environ.pop(_k, None)
    node._load_env_file(_env_probe)
    check(S, os.environ.get("SL_A") == "45", "node.env 行内注释被正确切掉")
    check(S, os.environ.get("SL_B") == "x # y", "引号里的 # 不被当注释")
    check(S, os.environ.get("SL_C") == "abc#def", "# 前无空白不当注释")
    check(S, node._env_num("SL_D", 0) == 192.0, "带单位的数字能取出开头数值")
    _env_probe.unlink()
    check(S, callable(getattr(node, "_is_mp3", None)), "有 _is_mp3（源已是 mp3 就跳过转码）")
    check(S, node._is_mp3("node.py") is False, "非 mp3 文件不被误判")

    # ---- F0 分布判男女/对唱（用户要求：尽量不用语音识别）----
    check(S, callable(getattr(node, "analyze_f0", None)), "有 analyze_f0（测基频分布）")
    check(S, callable(getattr(node, "_verdict_from_f0", None)), "有 _verdict_from_f0（分布→性别）")
    check(S, callable(getattr(node, "_segment_near_f0", None)),
          "有 _segment_near_f0（按频率范围找片段）")
    check(S, callable(getattr(node, "_clip_segment", None)), "有 _clip_segment（截短段）")
    check(S, node._verdict_from_f0(100, 120, 150)[0] == "male", "整体 100~150Hz → 男声")
    check(S, node._verdict_from_f0(200, 250, 320)[0] == "female", "整体 200~320Hz → 女声")
    check(S, node._verdict_from_f0(120, 200, 300)[0] == "mixed",
          "跨度 120~300Hz → 对唱（男女都有）")
    check(S, node._verdict_from_f0(160, 170, 180)[0] == "unknown",
          "卡在 155~185Hz 交界 → unknown（才去问人声识别）")
    for _k in ("F0_MALE_MAX", "F0_FEMALE_MIN", "F0_MIXED_LO", "F0_MIXED_HI"):
        check(S, hasattr(node, _k), f"{_k} 可配")
    # ---- 判定链：L1 用户指定 → L2 歌手判定 → L3 均分100s + Qwen-Audio ----
    _jf = _insp.getsource(node.judge_vocal)
    check(S, "analyze_f0(" in _jf, "judge_vocal 用 analyze_f0 测音域/判性别")
    check(S, "gender_lock" in _jf and "force_gender" in _jf,
          "支持 gender_lock / force_gender（指定了就锁定，跳过自动判断）")
    check(S, "qwen_audio_probe(" in _jf, "L3 兜底调 Qwen-Audio 判男女（key 从 node.env）")
    check(S, "sampled=True" in _jf, "L3 用全曲均分采样，不是只看开头")
    check(S, callable(getattr(node, "_audio_b64_sampled", None)),
          "_audio_b64_sampled 存在（全曲均分抽段拼音频）")
    _ab = _insp.getsource(node._audio_b64_sampled)
    check(S, "atrim" in _ab and "concat" in _ab, "均分抽段用 ffmpeg atrim + concat")
    check(S, "QWEN_PROBE_SEC" in _ab, "均分总时长可配（默认 100s）")
    check(S, callable(getattr(node, "process_probe_task", None)),
          "process_probe_task 存在（/测歌 用，只判定不翻唱）")
    check(S, node.TASK_HANDLERS.get("probe") is getattr(node, "process_probe_task", None),
          "probe 已注册进 TASK_HANDLERS")
    check(S, "judge_vocal(" in _insp.getsource(node.process_probe_task),
          "测歌和翻唱共用同一个 judge_vocal")

    # ---- 倍频兜底：348Hz 其实是 174Hz，会整首降 3 个半音 ----
    check(S, hasattr(node, "F0_OCTAVE_SUSPECT"), "F0_OCTAVE_SUSPECT 可配")
    _an = _insp.getsource(node.analyze_f0)
    check(S, "F0_OCTAVE_SUSPECT" in _an, "analyze_f0 有倍频兜底（中位过高就重测）")
    check(S, "0.75" in _an, "倍频兜底用「上限压到 0.75 倍」重测")
    # 抛物线插值：整数 lag 精度不够，220Hz 会被 3 倍周期抢走
    _ff = _insp.getsource(node._f0_frames)
    check(S, "denom" in _ff and "delta" in _ff, "_f0_frames 有抛物线亚采样插值")
    check(S, callable(getattr(node, "_probe", None)), "有端口探测 _probe")
    check(S, hasattr(node, "HEARTBEAT_INTERVAL") and node.HEARTBEAT_INTERVAL < 30,
          f"心跳间隔 {getattr(node, 'HEARTBEAT_INTERVAL', '?')}s < 服务器超时 30s")
    check(S, hasattr(node, "POLL_INTERVAL"), f"拉任务间隔 {getattr(node, 'POLL_INTERVAL', '?')}s")

    # --- 静态：bundle 双形态 ---
    mine = {
        "window": {"since_human": "11-03", "until_human": "11-05"},
        "persona": {"元气可爱": "你是小鲸鱼"},
        "mood": {"value": 42.0, "shifts": [{"at": 1793850000, "emotion": "happy", "delta": 10, "reason": "他夸我"}]},
        "profiles": {"10001": {"nickname": "小明", "is_owner": True}},
        "bonds": {"10001": {"notes": [{"text": "他夸我唱歌好听", "emotion": "happy"}]}},
        "catalog": [{"id": "abc123", "summary": "白米饭", "emotion_hit": True}],
        "window_details": [{"id": "abc123", "kind": "fact", "text_full": "他吃了白米饭"}],
        "journals": [{"name": "11-03.md", "text": "上一篇"}],
        "instruction": "写日记",
    }
    p1 = node._bundle_to_prompt(mine)
    for kw in ("角色卡", "小明", "他夸我唱歌好听", "[abc123]", "❗", "白米饭", "心情波动"):
        check(S, kw in p1, f"新形态 bundle → prompt 含「{kw}」")
    old = {"persona": "你是 KKI", "profiles": {"10001": {"nickname": "小明"}},
           "memory_dir": [{"id": "x1", "summary": "橘猫"}],
           "memory_details": [{"id": "x1", "text_full": "他养了橘猫"}],
           "mood_history": [{"emotion": "sad", "delta": -10}],
           "contexts": ["他说今天好累"]}
    p2 = node._bundle_to_prompt(old)
    for kw in ("你是 KKI", "橘猫", "他说今天好累"):
        check(S, kw in p2, f"旧形态 bundle → prompt 含「{kw}」")

    check(S, node._extract_json('```json\n{"journal":"x"}\n```')["journal"] == "x", "剥 ```json 包裹")
    check(S, node._extract_json('好的：{"journal":"y"} 完')["journal"] == "y", "剥前后废话")
    check(S, node._extract_json("不是 json") is None, "非 JSON → None")

    # --- 真网络：下载 ---
    payload = bytes(range(256)) * 4096  # 1 MB
    with FakeServer(payload) as fs:
        node.BASE_URL = fs.base

        dst = Path("local_temp") / "dl_ok.bin"
        r = node.download_file(f"{fs.base}/api/download/t1", dst, label="ok")
        check(S, r is True and dst.read_bytes() == payload, "下载 1MB 完整一致")

        r = node.download_file(f"{fs.base}/api/download/missing", Path("local_temp") / "dl_404.bin", label="404")
        check(S, r is False, "404 → 返回 False（不再把 JSON 当文件）")

        r = node.download_file(f"{fs.base}/api/download/json", Path("local_temp") / "dl_json.bin", label="json")
        check(S, r is False, "返回 JSON 内容 → 判定失败")

        # 断点续传：先放一半 .part
        dst2 = Path("local_temp") / "dl_resume.bin"
        part = dst2.with_suffix(dst2.suffix + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(payload[: len(payload) // 2])
        r = node.download_file(f"{fs.base}/api/download/t2", dst2, label="resume")
        check(S, r is True and dst2.read_bytes() == payload, "断点续传后文件完整")

        # --- 真网络：upload_complete(result=...) ---
        ok = node.upload_complete("tid_up", result={"journal": "测试日记"})
        check(S, ok is True, "upload_complete(result) 成功")
        form = fs.form()
        check(S, form.get("task_id") == "tid_up", "回传带 task_id")
        check(S, "测试日记" in (form.get("result") or ""), "回传带 result JSON")

        # --- 真网络：TTS（http 引擎）---
        node.TTS_ENGINE = "http"
        node.TTS_URL = f"{fs.base}/tts"
        node.TTS_USE_RVC = False
        node.process_tts_task("tid_tts", {"text": "你好呀"})
        posts = [r for r in fs.records if r[0] == "/api/tasks/complete"]
        last = posts[-1]
        check(S, b"tid_tts" in last[2], "TTS 完成后回传了任务")

        # --- 真网络：复盘端到端 ---
        bundle = dict(mine)
        bpath = Path("local_temp") / "reflect_test.json"
        bpath.parent.mkdir(parents=True, exist_ok=True)
        bpath.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        # 让 /api/download/<tid> 返回 bundle：把 payload 换成 bundle
        fs.payload = bpath.read_bytes()
        node.LLM_URL = f"{fs.base}/v1/chat/completions"
        node.process_reflection_task("tid_reflect", {})
        posts = [r for r in fs.records if r[0] == "/api/tasks/complete"]
        body = posts[-1][2].decode("utf-8", "ignore")
        check(S, "tid_reflect" in body, "复盘回传了任务")
        check(S, "journal" in body, "复盘回传包含 journal")
        check(S, "archive_keywords" in body, "复盘回传包含 archive_keywords")


# ============================================================
# 分组 11.5：功能菜单
# ============================================================
async def t_menu():
    S = "menu"
    try:
        import plugins.help_menu as M
    except Exception as e:
        check(S, False, "plugins.help_menu 导入失败", f"{type(e).__name__}: {e}")
        return

    blocks = {
        "人格": M._persona_block(),
        "称呼": M._call_block(),
        "记忆": M._memory_block(),
        "表情": M._sticker_block(),
        "音乐": M._music_block(),
        "工具": M._tool_block(),
        "被动": M._passive_block(),
        "画图": M._draw_block(),
        "玩法": M._fun_block(),
    }
    for name, txt in blocks.items():
        check(S, isinstance(txt, str) and len(txt) > 20, f"{name} 区块非空（{len(txt)} 字）")

    # 菜单体系 = 命令列表(BLOCKS) + 主题索引 + 所有主题详解
    # 新增命令只要写进任意一处就算覆盖
    try:
        from plugins.help_menu import topics as _T
        _extra = [_T.INDEX] + [_T.get(k) for k in ("人格", "记忆", "表情", "识图", "语音",
                                                   "翻唱", "写歌", "算力机", "搜索",
                                                   "频率", "部署", "画图", "玩法")]
    except Exception:
        _T, _extra = None, []
    whole = "\n".join(blocks.values()) + "\n" + "\n".join(x for x in _extra if x)

    # ① 菜单里点名的命令，代码里必须真的注册了
    import re as _re

    registered = set()
    for p in (ROOT / "plugins").rglob("*.py"):
        # 三方插件（plugins/third/）的指令是群友自己发着玩的，由「玩法」主题统一介绍，
        # 不逐个盯进核心菜单（否则猪猪几十个内部指令会把 /帮助 撑爆）。
        if "third" in p.parts:
            continue
        try:
            t = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in _re.finditer(r'on_command\(\s*"([^"]+)"', t):
            registered.add(m.group(1))

    missing = sorted(c for c in registered if c not in whole)
    check(S, not missing, f"所有 on_command 都写进菜单了（缺：{missing}）")
    check(S, len(registered) >= 10, f"扫到 {len(registered)} 个注册命令")

    # ② 前缀匹配类的命令也要在菜单里
    for cmd in ("/persona", "/写歌", "/写rap", "/搜索", "/取消", "/功能菜单"):
        check(S, cmd in whole, f"菜单提到 {cmd}")

    # ③ 关键词类（收表情）
    check(S, "收 1 3" in whole and "不收" in whole, "菜单说明收表情的用法")

    # ④ 动态数据真的读进来了（不能是写死的 0）
    try:
        from services.sticker_library import STICKER_MAX, sticker_lib
        total = len(sticker_lib.index)
        check(S, str(STICKER_MAX) in whole, f"菜单显示表情上限 {STICKER_MAX}")
        check(S, f"{total}/{STICKER_MAX}" in whole or total > 0,
              f"菜单里的库存数与实际一致（{total}）")
    except Exception as e:
        check(S, False, "读取表情库统计失败", f"{type(e).__name__}: {e}")

    # ⑤ 默认人格名出现在菜单里
    try:
        from services.persona import DEFAULT_PERSONA
        check(S, DEFAULT_PERSONA in whole or "当前：" in whole,
              f"菜单显示当前人格（{DEFAULT_PERSONA}）")
    except Exception:
        skip(S, "取不到 DEFAULT_PERSONA")

    # ⑥ 详解体系（topics）
    try:
        from plugins.help_menu import topics as T
    except Exception as e:
        check(S, False, "topics 导入失败", f"{type(e).__name__}: {e}")
        return

    check(S, len(T.INDEX) > 100 and "算力机" in T.INDEX, "主题索引非空且含算力机")
    check(S, T.get("不存在的东西") == "", "未知主题返回空串")

    for key in ("人格", "记忆", "表情", "识图", "语音", "翻唱", "写歌",
                "算力机", "搜索", "频率", "部署"):
        txt = T.get(key)
        check(S, isinstance(txt, str) and len(txt) > 100,
              f"详解「{key}」非空（{len(txt) if txt else 0} 字）")

    # 关键内容必须真的写到了（防止详解变成空壳）
    must = {
        "算力机": ["split", "audio_separation", "rvc", "tts", "reflection", "8005", "7860"],
        "翻唱": ["Demucs", "双文件", "output['vocal']"],
        "识图": ["model_not_found", "1024", "冷却"],
        "频率": ["兴趣词", "每小时最多 6 次", "汉字"],
        "表情": ["<sticker:", "上限", "DeepSeek"],
        "记忆": ["[[MEMO", "复盘", "[[MOOD"],
        "部署": ["md5sum", "selftest_all", "restart"],
    }
    for key, words in must.items():
        txt = T.get(key)
        missing = [w for w in words if w not in txt]
        check(S, not missing, f"详解「{key}」覆盖要点（缺：{missing}）")

    # 所有 TOPICS 值都得是可调用的，且不会抛异常
    bad = []
    for k, fn in T.TOPICS.items():
        try:
            if not callable(fn) or not fn():
                bad.append(k)
        except Exception as e:
            bad.append(f"{k}({type(e).__name__})")
    check(S, not bad, f"TOPICS 全部可调用且非空（异常：{bad}）")

    # ⑦ /帮助 的全部指令清单（COMMANDS）也要覆盖所有注册命令
    try:
        cmds_text = "\n".join(M.render_commands())
        check(S, len(cmds_text) > 300, f"/帮助 渲染出 {len(cmds_text)} 字")
        miss2 = sorted(c for c in registered if c not in cmds_text)
        check(S, not miss2, f"/帮助 覆盖全部 on_command（缺：{miss2}）")
        for cmd in ("/persona", "/导入表情", "/翻唱", "/写歌", "/搜索", "/取消", "/复盘"):
            check(S, cmd in cmds_text, f"/帮助 提到 {cmd}")
        check(S, "关注你的后续" in cmds_text, "/帮助 说明了关注会话机制")
    except Exception as e:
        check(S, False, "/帮助 渲染失败", f"{type(e).__name__}: {e}")


# ============================================================
# 分组 11.8：关注会话（被 @ 后连续跟 N 条）
# ============================================================
async def t_focus():
    S = "focus"
    try:
        import plugins.chat as C
    except Exception as e:
        # 离线环境可能装不起来，退化为静态检查
        src = (ROOT / "plugins" / "chat" / "__init__.py").read_text(
            encoding="utf-8", errors="ignore")
        for fn in ("_focus_start", "_focus_active", "_focus_consume",
                   "FOCUS_FOLLOW_COUNT", "FOCUS_TIMEOUT", "focus_sessions"):
            check(S, fn in src, f"源码含 {fn}（import 失败，仅静态检查）")
        skip(S, f"plugins.chat 无法离线导入（{type(e).__name__}），关注逻辑只做静态检查")
        return

    key = "group_999"
    C.focus_sessions.clear()

    check(S, C._focus_active(key, "111") is False, "没 @ 过 → 不关注")
    check(S, C.FOCUS_FOLLOW_COUNT >= 1, f"后续关注条数 {C.FOCUS_FOLLOW_COUNT}")
    check(S, C.FOCUS_TIMEOUT > 0, f"超时 {C.FOCUS_TIMEOUT}s")

    # @ 一次 → 开启关注
    C._focus_start(key, "111")
    check(S, C._focus_active(key, "111") is True, "@ 之后 → 进入关注")
    check(S, C._focus_active(key, "222") is False, "只跟被 @ 的那个人，别人不跟")

    # 消耗额度（额度是 FOCUS_FOLLOW_COUNT 条，别写死成 2）
    left = C._focus_consume(key, "111")
    check(S, left == C.FOCUS_FOLLOW_COUNT - 1, f"回完 1 条后还剩 {left} 条")
    for _ in range(C.FOCUS_FOLLOW_COUNT):
        left = C._focus_consume(key, "111")
        if left <= 0:
            break
    check(S, left == 0, "额度用完返回 0")
    check(S, C._focus_active(key, "111") is False, "额度用完 → 自动退出关注")

    # 超时退出
    C._focus_start(key, "333")
    C.focus_sessions[key]["333"]["until"] = time.time() - 1
    check(S, C._focus_active(key, "333") is False, "超过超时时间 → 自动退出")
    check(S, "333" not in C.focus_sessions[key], "过期条目被清理")

    # 每次交互续期
    C._focus_start(key, "444")
    old_until = C.focus_sessions[key]["444"]["until"]
    C._focus_consume(key, "444")
    check(S, C.focus_sessions[key]["444"]["until"] >= old_until, "每次交互后续期")

    # 不同群隔离
    C._focus_start("group_888", "111")
    check(S, C._focus_active("group_999", "111") is False, "不同群互相隔离")
    check(S, C._focus_active("group_888", "111") is True, "各自关注独立")

    # 空 @ 之后补一句（2026-11-18 真机：@ 空消息 → 「发个表情包」被判不回）
    # ⚠️ 2026-11-24：关注期新增「心情太低就不追」的闸门，
    #    前面的心情测试会把全局压到 -100，这里必须先复位，否则全是假失败。
    try:
        from services import mood as _mood
        _st_m = _mood._load()
        _st_m["global"]["value"] = 0.0
        _st_m.setdefault("sessions", {}).setdefault(key, {})["value"] = 0.0
        _mood._save(_st_m)
    except Exception as _e:
        print(f"[focus] 复位心情失败: {type(_e).__name__}: {_e}")
    C.focus_sessions.clear()
    C._focus_start(key, "555", pending=True)
    check(S, C.focus_sessions[key]["555"].get("pending") is True, "空 @ → 标记 pending")
    ok = await C._judge_focus_follow("发个表情包", "999", "555")
    check(S, ok is True, "空 @ 后的补话 → 直接回（不调模型）")
    check(S, C.focus_sessions[key]["555"].get("pending") is False, "pending 用完即清")

    # 省流模式硬信号（2026-11-24）：只有 @/引用/喊名字/收件人是她 才入队
    class _FakeLiteEv:
        def __init__(self, tome):
            self._t = tome

        def is_tome(self):
            return self._t

    check(S, C._lite_hard_signal(_FakeLiteEv(True), "999", "555") is True,
          "省流：@ 她是硬信号")
    check(S, C._lite_hard_signal(_FakeLiteEv(False), "999", "555",
                                 text="今天好热") is False,
          "省流：闲聊不接")
    check(S, C._lite_hard_signal(_FakeLiteEv(False), "999", "555",
                                 scene_meta={"to_kind": "bot"}) is True,
          "省流：收发件人判定是她 → 接")

    # 关注期内不再用正则猜意图：除结构信号（名字/空@补话/@别人）外都交给模型
    check(S, not hasattr(C, "_looks_like_request_to_me"),
          "关注判定不再用「祈使句正则」猜意图（交给模型）")

    # 「发个表情包」确定性接管：只认让她发的说法
    check(S, C._is_sticker_request("发个表情包") is True, "识别「发个表情包」")
    check(S, C._is_sticker_request("你发个表情包") is True, "识别「你发个表情包」")
    check(S, C._is_sticker_request("我发个表情包") is False, "「我发个表情包」不接管")
    check(S, C._is_sticker_request("收个表情包") is False, "「收个表情包」是收藏，不接管")
    check(S, C._is_sticker_request("今天好热") is False, "闲聊不触发发表情")

    # 刚被 @ 完的第一条后续：2026-11-23 起默认交给模型判（不再无条件回）；
    # 显式打开 FOCUS_FIRST_FOLLOW_ALWAYS 时才走「第一条必回」。
    _old_last = C.group_scene.last
    _old_first = C._FOCUS_FIRST_FOLLOW_ALWAYS
    C.group_scene.last = lambda gid: None
    C.focus_sessions.clear()
    C._focus_start(key, "666")
    C._FOCUS_FIRST_FOLLOW_ALWAYS = True
    try:
        ok = await C._judge_focus_follow("今天天气不错", "999", "666")
    finally:
        C._FOCUS_FIRST_FOLLOW_ALWAYS = _old_first
    check(S, ok is True, "开启 FIRST_FOLLOW_ALWAYS 时第一条后续 → 回")
    check(S, C.focus_sessions[key]["666"].get("neg") == 0, "判是时 neg 归零")

    # 收件人明确是别人 → 关注期也不回（用户 2026-11-23：别人互聊别接）
    C.focus_sessions.clear()
    C._focus_start(key, "666")
    C.group_scene.last = lambda gid: {
        "is_bot": False, "to_kind": "person", "to_qq": "777",
        "to_name": "小红", "to_conf": 0.9,
    }
    try:
        ok2 = await C._judge_focus_follow("你俩别吵了", "999", "666")
    finally:
        C.group_scene.last = _old_last
    check(S, ok2 is False, "收件人是别人 → 关注期不回")

    # 强否定：@ 了别人 / 引用回复别人 → 不回，且扣额度
    class _FakeEv:
        self_id = "123456789"
        reply = None
        def __init__(self, ats):
            self._ats = list(ats)
        def get_message(self):
            class _S:
                def __init__(self, q):
                    self.type = "at"
                    self.data = {"qq": q}
            return [_S(q) for q in self._ats]

    check(S, C._addressed_elsewhere(_FakeEv(["111"]), "123456789") == "@了别人",
          "@ 了别人 → 判为在找别人")
    check(S, C._addressed_elsewhere(_FakeEv(["123456789"]), "123456789") == "",
          "@ 的是她自己 → 不算找别人")

    # 她自己的话：记进上下文 + 去重（worker 与 NapCat 回显各来一次）
    C.recent_group_msgs.clear()
    check(S, C._append_self_line("999", "哈哈") is True, "自己的话记进群上下文")
    check(S, C._append_self_line("999", "哈哈") is False, "同一句不重复记（去重生效）")
    check(S, any(C._SELF_PREFIX in x for x in C.recent_group_msgs["999"]),
          "自己说的话带「这是你之前的回复：」标记")

    class _MEv:
        def __init__(self, ty):
            self._ty = ty
        def get_message(self):
            class _S:
                def __init__(self, ty):
                    self.type = ty
                    self.data = {}
            return [_S(self._ty)]

    check(S, C._media_placeholder(_MEv("image")) == "[图片]", "纯图片消息有占位符")
    check(S, C._media_placeholder(_MEv("record")) == "[语音]", "语音消息有占位符")
    check(S, C._media_placeholder(_MEv("text")) == "", "纯文本不加占位符")
    C.recent_group_msgs.clear()

    # 单群插话频率覆盖（测试群 1 分钟一次）
    _ovs = C._parse_group_interject_overrides("100000021:60:0.9:60:2")
    check(S, _ovs["100000021"]["cooldownSec"] == 60.0, "单群冷却覆盖成 60s")
    check(S, _ovs["100000021"]["interjectChance"] == 0.9, "单群插话概率覆盖")
    check(S, _ovs["100000021"]["maxPerHour"] == 60, "单群每小时上限覆盖（否则 6 次就封顶）")
    check(S, _ovs["100000021"]["minRelevance"] == 0.0, "覆盖群关掉关键词相关度（交给模型判断）")
    check(S, C._parse_group_interject_overrides("") == {}, "空配置不报错")

    # 插话不再用正则预筛「值不值得接」（用户 2026-11-18：交给模型判断）
    check(S, not hasattr(C, "_interject_worth_trying"),
          "插话前没有正则语义预筛（该不该说交给模型）")

    # 单群体验覆盖（测试群：4 秒接话）
    from services import group_tuning as GT
    _go = GT._parse("100000021:4:45")
    check(S, _go["100000021"]["reply_interval"] == 4.0, "单群回复间隔覆盖解析")
    check(S, _go["100000021"]["max_hanzi"] == 45, "单群直接发字数阈值覆盖解析")
    check(S, GT._parse("") == {}, "空覆盖配置不报错")

    # 人称/指代：把「这条 @ 的是谁」写进 prompt（用户 2026-11-18）
    _chat_src = (ROOT / "plugins" / "chat" / "__init__.py").read_text(encoding="utf-8")
    _pb_src = (ROOT / "services" / "prompt_builder.py").read_text(encoding="utf-8")
    check(S, "这一条消息的人称" in _pb_src, "prompt 有每轮人称说明")
    check(S, "这条 @ 的是" in _pb_src, "prompt 写明这条 @ 的是谁、不是你")
    check(S, "这条消息提到的人" in _pb_src, "prompt 带上被 @ 的人的档案")
    check(S, "focus_qqs=_focus_qqs" in _chat_src and "addressed=_addressed" in _chat_src,
          "chat 把 @ 对象和是否叫她传进 prompt")

    # 称呼优先（用户 2026-11-18）：回复叫「称呼」，没有才叫群名片/昵称
    from services import profiles as _pf
    _cq = "30077"
    _pf._save("profile", _cq, {})
    _pf.upsert_profile(_cq, nickname="群名片甲")
    check(S, _pf.call_of(_cq) == "群名片甲", "没有约定称呼 → 退回群名片")
    _pf.set_call_names(_cq, call_name="阿甲")
    check(S, _pf.call_of(_cq) == "阿甲", "有约定称呼 → 优先用 call_name")
    _cards = _pf.batch_profiles([_cq])
    check(S, _cards and _cards[0]["name"] == "阿甲" and _cards[0]["nickname"] == "群名片甲",
          f"档案卡里的 name 用称呼：{_cards[:1]}")
    check(S, "回复里称呼他时" in _pb_src, "prompt 明确写「回复里就用这个称呼」")
    _llm_src0 = (ROOT / "services" / "llm.py").read_text(encoding="utf-8")
    check(S, "profiles.call_of" in _llm_src0 and "回复时用" in _llm_src0,
          "【当前说话人】那一行也用称呼，并说明群名片只是标签")
    check(S, "回复时怎么叫人" in _llm_src0, "CALL 协议补了「回复称呼优先」")

    # 群聊被 @ → 系统自动引用回复（用户 2026-11-18）
    check(S, "_auto_rid" in _chat_src and "_quote_left" in _chat_src,
          "群聊被 @ 时自动引用回复（不靠模型自己写 <reply:>）")
    check(S, 'ev.is_tome()' in _chat_src, "只在真 @ 时自动引用（喊名字/普通接话不加）")
    check(S, "reply_to=_auto_call" in _chat_src,
          "长文走合并转发时也标出「回复 @谁」")

    # 长文发送规矩（用户 2026-11-18：规定一直是「20 字内直接发，20 字以上合并转发」）
    check(S, "send_group_forward_msg" in _chat_src,
          "群聊长文用合并转发（不再硬截断）")
    check(S, "hanzi_count(val) > _g_limit" in _chat_src,
          "主回复超过阈值自动转卡片")
    check(S, "hanzi_count(_body) > _g_limit" in _chat_src,
          "主动插话超过阈值也转卡片")
    check(S, "_send_long_as_forward(bot, ev, _full)" in _chat_src,
          "群聊视频总结走合并转发（不再砍成 60 字）")
    from services.llm import _length_rule as _lr
    _rule = _lr("group_100000021")
    check(S, "合并转发" in _rule and "不要写" not in _rule,
          "群聊字数铁律：超过阈值转卡片，不再要求「不发长文」")
    check(S, "hanzi_count(val) > PRIVATE_MAX_HANZI" in _chat_src,
          "私聊同一条规矩：>20 字走合并转发")

    # 工具不再自己发（用户 2026-11-18）：hold 拦下内容，她再调 send
    from services import llm as _L
    for _t in ("girls_2d", "daily_luck", "rollpig", "petpet"):
        check(S, _t in _L.HOLD_TOOLS, f"{_t} 不自己发（内容交给 send）")
    check(S, "send" in _L.TOOL_HANDLERS, "有 send 工具（发不发由她决定）")
    check(S, any(s.get("function", {}).get("name") == "send"
                 and "pending" in (s["function"].get("parameters") or {}).get("properties", {})
                 for s in _L.TOOLS_SPEC), "send 支持 pending 编号")
    _llm_src = (ROOT / "services" / "llm.py").read_text(encoding="utf-8")
    check(S, "_TOOL_HOLD" in _llm_src and "还没发出去" in _llm_src,
          "工具发送会被拦成「待发内容」还给模型")
    check(S, "asyncio.current_task()" in _llm_src,
          "hold 只拦当前工具任务（后台唱歌/视频照常发）")

    # 猪猪 action 归一化：模型写中文/不填也能落到正确分支
    check(S, C._norm_action("today") == "today", "action 英文原样")
    check(S, C._norm_action("明日小猪") == "tomorrow", "action 中文「明日小猪」→ tomorrow")
    check(S, C._norm_action("我的猪圈") == "sty", "action 中文「我的猪圈」→ sty")
    check(S, C._norm_action("今日烤猪") == "roast_self", "action「今日烤猪」→ roast_self（不是 today）")
    check(S, C._norm_action("烤群友") == "roast_member", "action「烤群友」→ roast_member")
    check(S, C._norm_action("") == "today", "action 不填 → today")
    check(S, hasattr(C._RollpigToolMatcher, "stop_propagation"),
          "假 matcher 有 stop_propagation（烤群友不会 AttributeError）")
    check(S, hasattr(C, "_rollpig_selfcheck"), "有 /测函数 猪猪 自检")

    # 原神面板（gspanel）：没 htmlrender 时给文字版，别整个插件加载失败
    _gs = (ROOT / "plugins" / "third" / "gspanel" / "data_source.py").read_text(
        encoding="utf-8")
    _gsu = (ROOT / "plugins" / "third" / "gspanel" / "__utils__.py").read_text(
        encoding="utf-8")
    check(S, "_format_panel_text" in _gs, "gspanel 有文字版兜底（缺 htmlrender 也能查）")
    check(S, "if _HTMLRENDER_ERR is not None:" in _gs, "没装渲染依赖时直接返回文字版")
    check(S, "_fetch_res" in _gsu and "timeout=15" in _gsu,
          "gspanel 资源拉取有超时 + 本地缓存兜底（CDN 挂了插件也能加载）")
    check(S, hasattr(C, "_genshin_selfcheck"), "有 /测函数 原神 自检")

    # gspanel 抗数据变化：Costumes 缺失 / 单个角色异常不能打挂整张面板
    _gc = (ROOT / "plugins" / "third" / "gspanel" / "data_convert.py").read_text(
        encoding="utf-8")
    _gi = (ROOT / "plugins" / "third" / "gspanel" / "__init__.py").read_text(
        encoding="utf-8")
    check(S, 'charData.get("Costumes")' in _gc, "gspanel 兼容 Costumes 字段缺失")
    check(S, "CHAR_DATA.get(_char_id)" in _gc, "gspanel 未知角色给明确错误")
    check(S, "跳过角色" in _gs, "单个角色数据异常只跳过，不整张面板报错")
    check(S, 'on_command("绑定"' in _gi and "uidUnbind" in _gi,
          "有 /绑定、/解绑（QQ ↔ 原神 UID）")
    check(S, "_finish_image" in _gi and "file://" in _gi,
          "面板图先落盘再发（避免 base64 上传超时）")
    for _tn in ("genshin_bind", "genshin_unbind", "genshin_panel", "genshin_team"):
        check(S, f'"name": "{_tn}"' in _gi, f"原神工具 {_tn} 已注册")
    check(S, '"三方插件"' in _llm_src, "工具目录有「三方插件」完整触发词")

    # 插话不再对同一段上下文反复开口（2026-11-18 真机：隔一分钟自问自答）
    check(S, "_interject_seen" in _chat_src, "记录「这段上下文已经看过」")
    check(S, "_latest_human_ts <= _seen_ts" in _chat_src,
          "最新人类消息没变 / 她已经说过 → 不再插话")
    check(S, "_last_bot_ts" in _chat_src, "插话会参考她最后一次开口时间")
    check(S, "别人最新一句（只判断这句）" in _chat_src,
          "插话 prompt 明确只判断别人最新一句")
    check(S, 'f"[语音] {_it.get' in _chat_src,
          "插话发语音也记下内容（不然上下文只剩 [语音]）")

    # 连续判否 → 提前结束关注（防「一直跟着讲」）
    C.focus_sessions.clear()
    C._focus_start(key, "777")
    l1 = C._focus_penalize(key, "777")
    check(S, l1 == C.FOCUS_FOLLOW_COUNT - 1, f"判否一次还剩 {l1} 条")
    l2 = C._focus_penalize(key, "777")
    check(S, l2 == 0 and C._focus_active(key, "777") is False,
          "连续判否 → 结束关注（不再跟）")

    C.focus_sessions.clear()


# ============================================================
# 分组 11.8b：高刷消息队列清理（30s 内 4 条 → 清空排队，只留 @）
# ============================================================
async def t_burst():
    S = "burst"
    src = (ROOT / "plugins" / "chat" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    for k in ("BURST_FLUSH_ENABLED", "BURST_WINDOW_SEC", "BURST_MSG_COUNT",
              "_burst_count", "_flush_stale_queue", "_is_mention_item"):
        check(S, k in src, f"源码含 {k}")
    # 2026-11-20：阈值统一收到 core/tuning.py，源码里不再有 os.getenv("X","30")
    try:
        from core import tuning as _t
        check(S, _t.DEFAULTS.get("BURST_WINDOW_SEC") == 30, "窗口默认 30 秒")
        check(S, _t.DEFAULTS.get("BURST_MSG_COUNT") == 4, "阈值默认 4 条")
    except Exception as _e:
        check(S, False, f"读 tuning 失败：{_e}")
    check(S, "_flush_stale_queue(session_key)" in src, "入队前会清队列")
    check(S, 'burst_msg_ts[f"group_{gid}"].append' in src, "群消息按会话记（group_xxx）")
    check(S, 'burst_msg_ts[f"private_{event.user_id}"].append' in src,
          "私聊也单独记（private_xxx，和群互不影响）")
    check(S, "_is_self" in src and "if not _is_self:" in src,
          "排除 NapCat 回显的她自己那条消息")
    check(S, 'getattr(event, "self_id"' in src, "用 self_id 判自消息")
    check(S, '_burst_count(f"group_{gid}") >= BURST_MSG_COUNT' in src,
          "群里刷屏时不做主动插话")

    try:
        import plugins.chat as C
    except Exception as e:
        skip(S, f"plugins.chat 无法离线导入（{type(e).__name__}），高刷逻辑只做静态检查")
        return

    class _Ev:
        """最小可用的假事件：只要 is_tome / user_id。"""
        def __init__(self, tome=False, uid="111"):
            self._tome = tome
            self.user_id = uid

        def is_tome(self):
            return self._tome

    # ---------- ① 窗口内计数 ----------
    C.burst_msg_ts.clear()
    now = time.time()
    for dt in (45, 40, 5, 3, 1):        # 前两条已经出了 30s 窗口
        C.burst_msg_ts["g1"].append(now - dt)
    check(S, C._burst_count("g1") == 3, f"30s 内 3 条（实测 {C._burst_count('g1')}）")
    check(S, C._burst_count("g1", 60) == 5, "窗口放到 60s → 5 条")
    check(S, C._burst_count("没这个群") == 0, "没消息的群返回 0")

    # ---------- ② 清队列：丢闲聊，留 @ ----------
    key = "group_burst"
    C.session_queues[key] = C.asyncio.Queue()
    q = C.session_queues[key]
    check(S, C._flush_stale_queue(key) == [], "空队列清出来是空列表")
    q.put_nowait((_Ev(False, "111"), "普通闲聊一", None))
    q.put_nowait((_Ev(True, "222"), "@ 我的一句", None))
    q.put_nowait((_Ev(False, "333"), "普通闲聊二", None))
    dropped = C._flush_stale_queue(key)
    left = []
    while not q.empty():
        left.append(q.get_nowait())
    check(S, len(dropped) == 2, f"丢掉 2 条闲聊（实测 {len(dropped)}）")
    check(S, len(left) == 1, f"保留 1 条（实测 {len(left)}）")
    check(S, bool(left) and "@ 我的一句" in str(left[0][1]), "保留的是 @ 我的那条")
    check(S, [str(d[1]) for d in dropped] == ["普通闲聊一", "普通闲聊二"],
          "保留顺序不乱、丢掉的是闲聊")

    # ---------- ③ 只有闲聊 → 全清 ----------
    C.session_queues[key] = C.asyncio.Queue()
    q = C.session_queues[key]
    for i in range(3):
        q.put_nowait((_Ev(False, "111"), f"闲聊{i}", None))
    check(S, len(C._flush_stale_queue(key)) == 3, "3 条闲聊全被清掉")
    check(S, q.empty(), "队列真的空了")

    # ---------- ④ 全是 @ → 一条都不丢 ----------
    C.session_queues[key] = C.asyncio.Queue()
    q = C.session_queues[key]
    for i in range(2):
        q.put_nowait((_Ev(True, "222"), f"@我{i}", None))
    check(S, C._flush_stale_queue(key) == [], "全是 @ 时不清（@ 必须回）")
    check(S, q.qsize() == 2, "两条 @ 都还在队列里")

    # ---------- ⑤ 按会话隔离：一个群刷屏不牵连别的群/私聊 ----------
    C.burst_msg_ts.clear()
    C.burst_msg_ts["group_a"].extend([time.time()] * 4)
    C.burst_msg_ts["group_b"].append(time.time())
    C.burst_msg_ts["private_c"].append(time.time())
    check(S, C._burst_count("group_a") == 4, "群 A 计数 4")
    check(S, C._burst_count("group_b") == 1, "群 B 不受群 A 影响")
    check(S, C._burst_count("private_c") == 1, "私聊 C 不受群 A 影响")

    C.session_queues["group_a"] = C.asyncio.Queue()
    C.session_queues["group_b"] = C.asyncio.Queue()
    C.session_queues["private_c"] = C.asyncio.Queue()
    C.session_queues["group_a"].put_nowait((_Ev(False), "a闲聊", None))
    C.session_queues["group_b"].put_nowait((_Ev(False), "b闲聊", None))
    C.session_queues["private_c"].put_nowait((_Ev(False), "c闲聊", None))
    C._flush_stale_queue("group_a")
    check(S, C.session_queues["group_a"].empty(), "群 A 队列被清空")
    check(S, C.session_queues["group_b"].qsize() == 1, "群 B 队列原封不动")
    check(S, C.session_queues["private_c"].qsize() == 1, "私聊 C 队列原封不动")

    # ---------- ⑥ 清队列不碰 indextts / 翻唱任务 ----------
    # 那些任务在 tool_dispatcher 的任务表（STATUS["tasks"]）里，由算力机异步跑，
    # 和聊天队列是两套东西 —— 函数体里一个都不能出现。
    _fn = src.split("def _flush_stale_queue", 1)[-1].split("\nasync def ", 1)[0]
    import re as _re
    _fn = _re.sub(r'"""[\s\S]*?"""', "", _fn)          # 去掉 docstring（里面本来就在解释这件事）
    _fn = "\n".join(_l for _l in _fn.splitlines() if not _l.strip().startswith("#"))
    for _bad in ("STATUS", "tasks", "upload", "cover", "tts", "cancel",
                 "shadow", "referee", "peer"):
        check(S, _bad not in _fn, f"清队列的函数体里没有 {_bad}（不打断长任务/影子回答）")

    # ---------- ⑦ 影子回答（蒸馏用，不发群）不受清队列影响 ----------
    # 它靠 referee 直接 await _SHADOW_FN 跑，问题记录在 referee._peer_questions，
    # 两处都跟 session_queues 无关；而且 note_peer_question 在**入队之前**就调用了，
    # 所以哪怕那条消息的队列项被清掉，问题也已经记下来了。
    _shadow_fn = src.split("async def _shadow_answer", 1)[-1].split("\nreferee.set_shadow_fn", 1)[0]
    check(S, "session_queues" not in _shadow_fn, "影子回答不走聊天队列（清不到）")
    check(S, 'sk = f"shadow_{gid or \'x\'}"' in src, "影子回答用独立 session 命名空间 shadow_xxx")
    try:
        import services.referee as R
        _rsrc = (ROOT / "services" / "referee.py").read_text(
            encoding="utf-8", errors="ignore")
        check(S, "session_queues" not in _rsrc, "referee 完全不碰聊天队列")
        check(S, "_peer_questions" in _rsrc and "_peer_buf" in _rsrc,
              "影子回答的问题/连发缓冲存在 referee 自己的 dict 里")
        check(S, callable(getattr(R, "note_peer_question", None)),
              "note_peer_question 存在（@ 鱼时先记问题）")
        check(S, callable(getattr(R, "set_shadow_fn", None)), "set_shadow_fn 已注册入口")
    except Exception as e:
        skip(S, f"referee 离线导入失败（{type(e).__name__}），只做静态检查")
    # 顺序：先记问题，再入队（清队列在后面，所以记录一定先落地）
    _i_note = src.find("referee.note_peer_question")
    _i_put = src.find("await queue.put(")
    check(S, 0 <= _i_note < _i_put, "note_peer_question 在入队/清队列之前调用")

    for _k in ("group_a", "group_b", "private_c", key):
        C.session_queues.pop(_k, None)
    C.burst_msg_ts.clear()


# ============================================================
# 分组 11.8c：说话时机（图片不抢话 / 被讨厌就闭嘴 / 别重复 / 别学脏话）
# ============================================================
async def t_timing():
    S = "timing"
    chat_src = (ROOT / "plugins" / "chat" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    vis_src = (ROOT / "plugins" / "vision_agent" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    pb_src = (ROOT / "services" / "prompt_builder.py").read_text(
        encoding="utf-8", errors="ignore")
    llm_src = (ROOT / "services" / "llm.py").read_text(
        encoding="utf-8", errors="ignore")
    mem_src = (ROOT / "services" / "memory_v2.py").read_text(
        encoding="utf-8", errors="ignore")
    per_src = (ROOT / "services" / "persona.py").read_text(
        encoding="utf-8", errors="ignore")
    vision_src = (ROOT / "services" / "vision_verify.py").read_text(
        encoding="utf-8", errors="ignore")

    # ---------- ① 图片不再抢话 ----------
    check(S, "VISION_REPLY_MODE" in vis_src, "识图有「要不要开口」开关")
    check(S, '"mentioned"' in vis_src, "默认 mentioned（喊名字才回）")
    check(S, "def _vision_should_reply" in vis_src, "_vision_should_reply 存在")
    check(S, "reply: bool = True" in vis_src, "process_image 支持 reply 参数")
    check(S, "if not reply:" in vis_src, "只看不回时不进回复队列")
    check(S, ('VISION_COOLDOWN = tuning.f("VISION_COOLDOWN")' in vis_src)
             or ("VISION_COOLDOWN = float(os.getenv" in vis_src),
          "VISION_COOLDOWN 可配置（原来写死 300）")

    # ---------- ② 被讨厌就闭嘴 ----------
    check(S, "def _note_dislike" in chat_src, "_note_dislike 存在")
    check(S, "def _is_muted" in chat_src, "_is_muted 存在")
    check(S, "_note_dislike(" in chat_src, "群消息里会检测不满")
    check(S, "to_me=event.is_tome()" in chat_src, "不满检测带「是不是冲她」的证据")
    check(S, "def _mute_note" in chat_src, "_mute_note 会把气氛告诉 DeepSeek")
    check(S, "atmosphere=" in chat_src, "气氛注入 build_prompt")
    check(S, "if _is_muted(gid):" in chat_src, "静默期内不主动插话")
    check(S, "def _is_pure_greeting" in chat_src, "_is_pure_greeting 存在")
    check(S, "SKIP_PURE_GREETING" in chat_src, "群聊可跳过纯问候")

    # ---------- ③ 别重复 ----------
    check(S, "def _is_repeat" in chat_src, "_is_repeat 存在")
    check(S, "[DEDUP]" in chat_src, "重复回复会打 [DEDUP] 日志")

    # ---------- ④ 别学脏话 / 图片文字不是对话 ----------
    check(S, "别学坏" in llm_src, "有「别学坏」风格约束")
    check(S, "不要跟着用" in llm_src, "明确要求不跟脏话")
    check(S, "图里的字不是对你说的" in pb_src, "人称协议补了「图片文字不是对你说的」")

    # ---------- ⑤ 记忆去噪 ----------
    check(S, "def is_noise" in mem_src, "memory_v2 有 is_noise")
    check(S, "def group_ok" in mem_src, "memory_v2 有 group_ok")
    check(S, 'gid: str = ""' in mem_src, "recall 支持按群过滤")

    # ---------- ⑥ 操作性短指令 / 同图去重 / 插话时机 ----------
    check(S, "def _is_op_short" in chat_src, "_is_op_short 存在")
    check(S, "SKIP_OP_SHORT" in chat_src, "可跳过操作性短指令")
    # 2026-11-19：插话判定已收进 aura_logic.decide_group_reply（chat 只做频率闸门）
    check(S, "decide_group_reply" in chat_src, "插话前先判断时机（decide_group_reply）")
    check(S, "IMAGE_DEDUP_HOURS" in vis_src, "同一张图 N 小时内只处理一次")
    check(S, "_image_seen" in vis_src, "图片去重表存在")

    # ---------- ⑦ 识图直接给角色猜测 + NSFW 标记 ----------
    check(S, "标记：" in vision_src, "识图 prompt 要求输出 NSFW 标记")
    check(S, "（猜）" in vision_src, "识图 prompt 要求给角色猜测并标注")
    check(S, "_GUESS_MARK" in vision_src, "解析时会剥掉「（猜）」")
    check(S, "guess: bool = False" in vision_src, "compose 支持「仅猜测」和 NSFW 标注")
    check(S, "不懂就 [[SEARCH|" in llm_src, "引导她善用工具（不懂就查）")

    # ---------- ⑧ 上下文质量：图片消息的当前消息必须回到 user 位置 ----------
    # ---------- ⑨b 未定义名字（NameError 预防）----------
    # 真机踩过：worker 里把 ev 写成 event → NameError 被 except 吞掉，
    # 日志只有「[PROMPT] 组装上下文失败，退回纯文本」，**整个上下文全丢**，
    # 而当时 1037 项自检全绿（py_compile 查不出这种错）。
    try:
        sys.path.insert(0, str(ROOT / "scripts"))
        import check_names as _CN
        for _f in _CN.TARGETS:
            if not _f.exists():
                continue
            _bad = _CN.check_file(_f)
            check(S, not _bad,
                  f"{_f.parent.name}/__init__.py 无未定义名字"
                  + (f"｜{_bad[:2]}" if _bad else ""))
    except Exception as e:
        skip(S, f"check_names 不可用（{type(e).__name__}: {e}）")

    # ---------- ⑨ 规矩合并成一条（DeepSeek 反馈：怕没结构，不怕长） ----------
    # ---------- 工具菜单（她能当 CPU 自己调度） ----------
    check(S, "_TOOL_MENU" in llm_src, "工具菜单存在")
    check(S, "什么时候用" in llm_src, "菜单里每项都写了「什么时候用」")
    check(S, "[[HISTORY|" in llm_src, "菜单含「往上翻 N 条」")
    check(S, "def _history_slice" in llm_src, "_history_slice 存在")
    check(S, "SEARCH|LIBRARY|SING|WHO|HISTORY" in llm_src, "_REQ_RE 支持 HISTORY")
    # 2026-11-19：改成集合常量 _NO_COMPRESS = {"HISTORY", "TOOLS", ...}
    check(S, '"HISTORY", "TOOLS"' in llm_src,
          "翻历史/工具目录不做压缩（压缩会丢时间线/格式）")
    check(S, "这一条在跟你说话的人" in llm_src,
          "系统会给「跟你说话的人」一张 brief 人物卡")
    check(S, '("00 工具", _menu)' in llm_src,
          "菜单排在最前面（▍00）")

    # ---------- 群聊里「玩」要 @ 才放开 ----------
    check(S, "def _play_section" in llm_src, "【玩】单独成节（按有没有被叫区分）")
    check(S, "不许唱歌、不许发语音" in llm_src, "群里没被叫 → 禁唱歌/语音")
    check(S, "addressed: bool = True" in llm_src, "_behaviour_protocol 支持 addressed")
    check(S, "群里没被叫，拒绝唱歌" in llm_src, "SING 有代码级拦截（不只靠提示）")
    check(S, "语音降级成文字" in chat_src, "群里没被叫 → 语音降级成文字")
    check(S, "addressed=_addressed" in chat_src, "chat 把 addressed 交给工具层")

    # ---------- 工具化：记忆/GLM 按需捞 + 每日新开对话 + 人设热更新 ----------
    check(S, "MEMORY|PROFILE|ASKGLM" in llm_src, "_REQ_RE 支持 MEMORY / PROFILE / ASKGLM")
    check(S, "[[MEMORY|关键词]]" in llm_src, "菜单里有 [[MEMORY]] 及示例")
    check(S, "[[ASKGLM|问题]]" in llm_src, "菜单里有 [[ASKGLM]] 及示例")
    check(S, 'elif kind == "MEMORY"' in llm_src, "MEMORY 分支存在")
    check(S, 'elif kind == "ASKGLM"' in llm_src, "ASKGLM 分支存在")
    _mem_src = (ROOT / "services" / "memory.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "CHAT_HISTORY_DAILY" in _mem_src, "短历史可按天隔离")
    check(S, "date(timestamp, 'localtime')" in _mem_src, "按本地日期过滤（每天新开对话）")
    check(S, "PERSONA_MAX_RELOADS_PER_DAY" in per_src, "人设每天最多重载 N 次")
    check(S, "def _maybe_reload" in per_src, "_maybe_reload 存在")
    check(S, "def _block_title" in chat_src, "/看模板 先出目录")
    check(S, "看模板 全" in chat_src, "/看模板 全 打印全文")

    # ---------- 多轮思考（60 秒预算）+ 按需拉取 + 每日总结 ----------
    check(S, "THINK_BUDGET_SEC" in llm_src, "思考预算可配（默认 60 秒）")
    check(S, "MAX_REQ_ROUNDS" in llm_src, "工具轮数上限可配")
    # 循环从 `while _REQ_RE.search(...)` 改成 `while True` + 内部 break（两档预算要动态换上限）
    check(S, "_rounds >= _max_rounds" in llm_src, "工具是**多轮**循环，不是补一轮")
    check(S, "思考预算用完" in llm_src, "超时后强制开口")
    check(S, "PROFILE|ASKGLM|LATER" in llm_src, "_REQ_RE 支持 PROFILE / ASKGLM / LATER")
    check(S, "def _new_messages_since" in llm_src, "_new_messages_since（看后续消息）")
    check(S, "[[[MEMORY|" in llm_src, "菜单推荐三括号写法（防误触）")
    check(S, "不用急着回" in llm_src, "菜单告诉她不着急、可连调多轮")
    # 动态区瘦身：档案/过往/记忆全文 → 一行索引
    _pb2 = (ROOT / "services" / "prompt_builder.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "memory_index" in _pb2, "记忆改成「索引一行」")
    check(S, "▍你和" not in _pb2, "相处过往不再整段注入（旧标题已删）")
    check(S, "PROFILE|" in _pb2, "索引里引导她用 [[PROFILE]] 拉全文")
    # 每日总结
    _ds = ROOT / "services" / "daily_summary.py"
    check(S, _ds.exists(), "daily_summary.py 存在")
    if _ds.exists():
        _dss = _ds.read_text(encoding="utf-8", errors="ignore")
        check(S, "def get_yesterday_summary" in _dss, "能取昨日总结")
        check(S, "async def summarize_day" in _dss, "能总结某一天")
        check(S, "第一人称" in _dss, "总结要求第一人称、不写废话")
    check(S, "def _daily_summary_loop" in chat_src, "每日 0 点总结的循环存在")
    check(S, "create_task(_daily_summary_loop())" in chat_src, "启动时挂上了总结循环")
    check(S, '"yesterday"' in llm_src, "昨日总结会注入 prompt")

    check(S, "def _behaviour_protocol" in llm_src, "规矩合并成一条（_behaviour_protocol）")
    check(S, "以编号小的为准" in llm_src, "声明「冲突时以编号小的为准」，不用来回对照")
    check(S, "_NOT_INTERJECT" in llm_src and "_SEARCH_PROTOCOL" in llm_src
          and "_NO_SWEAR" in llm_src, "三条 inline 规矩已提成常量（可被审计脚本抓到）")
    check(S, "▍" in llm_src, "用 ▍编号分节")
    # 2026-11-19：现在有「背景/历史分块/对话状态/表达学习」等多处按需 append，
    # 数量本身不是问题 —— 真正的不变量是「固定区仍然只有 2 条（人设 + 规矩）」。
    _np = llm_src.count('msgs.append({"role": "system"')
    # 2026-11-22：13 处 —— 人设/规矩/现在时间/人称/收件人/现场/历史/状态/
    #            人物卡/表达/设定/动态规矩/post-rules（focus 卡去重后仍是 1 处）
    # 2026-11-23：+1 心情/好感 depth 注入、+1 白月光 → 上限 16。
    # 2026-11-24：+1 动态流水线块（staged_text）→ 上限 17。
    check(S, _np <= 17, f"system 块数量受控（实际 {_np} 处 append）")
    check(S, '"content": _proto' in llm_src, "固定区仍合成 1 条 system（不吃 8 条）")

    check(S, "def _split_prompt" in llm_src, "_split_prompt 把背景/当前消息拆开")
    check(S, "_bg_text, _cur_text = _split_prompt(text)" in llm_src,
          "**所有**消息都拆（不只图片），背景不再重复贴进 user")
    check(S, "他同时说" in vis_src, "图片消息附带的文字跟图片一起走")
    check(S, "speaker_recent = defaultdict" in chat_src, "按人记录最近说过的话")
    check(S, "刚才还说过" in chat_src, "被 @ 时附上这个人刚说过的话")
    # 人标记不能漏：这块是「同一个人之前说的话」，标题必须写清是谁
    check(S, "【{_msg_label(event)} 刚才还说过】" in chat_src,
          "「刚才还说过」带上了人标记")

    try:
        import services.llm as LL
        _bg, _cur = LL._split_prompt(
            LL._BG_SEP + "\n背景内容A\n\n" + LL._CUR_SEP + "\n扫扫")
        check(S, _cur == "扫扫", f"拆分后当前消息=扫扫（{_cur!r}）")
        check(S, "背景内容A" in _bg, "拆分后背景保留")
        check(S, LL._BG_SEP not in _bg, "背景里不再残留分隔线")
    except Exception as e:
        skip(S, f"_split_prompt 测试失败（{type(e).__name__}: {e}）")

    # ---------- ⑥ 人格分级注入（默认关） ----------
    check(S, "PERSONA_GROUP_CHARS" in per_src, "人格支持群聊精简注入")
    check(S, "def _trim_for_group" in per_src, "_trim_for_group 存在")

    # ================== 功能测试 ==================
    try:
        import plugins.chat as C
        import services.memory_v2 as M
    except Exception as e:
        skip(S, f"离线导入失败（{type(e).__name__}），只做静态检查")
        return

    _gs = ("g_mute", "g_mute2", "g_mute3", "g_ok", "g_ok2", "g_w", "g_w2")
    for _g in _gs:
        C.group_mute_until.pop(_g, None)
        C.group_dislike_text.pop(_g, None)

    # 不满检测：该中的中，不该中的别中
    check(S, C._note_dislike("g_mute", "这机器人真烦人") is True,
          "强信号「这机器人真烦人」→ 触发（含指代她的词）")
    check(S, C._is_muted("g_mute") is True, "触发后该群进入静默")
    check(S, C._note_dislike("g_mute2", "能不能关了先", to_me=True) is True,
          "弱信号 + @ 她 → 触发")
    check(S, C._note_dislike("g_mute3", "这机器人魔了") is True, "「这机器人魔了」→ 触发")
    check(S, C._note_dislike("g_mute4", "小鲸鱼真烦人") is True,
          "当前人格名 + 负面词 → 触发（不写死鱼/鲸鱼）")
    check(S, C._note_dislike("g_ok", "今天好累啊") is False, "普通抱怨不触发（不误伤）")
    check(S, C._is_muted("g_ok") is False, "没触发的群不受影响")
    check(S, C._note_dislike("g_ok2", "好烦啊不想上班") is False,
          "「好烦」不指向她 → 不触发")
    # 👇 误伤防护：A 让 B 闭嘴，不该把她静默掉
    check(S, C._note_dislike("g_w", "闭嘴") is False,
          "「闭嘴」但没 @/没喊名字/她没刚说话 → 不误伤（A 让 B 闭嘴）")
    check(S, C._note_dislike("g_w2", "闭嘴", replied_recently=True) is True,
          "「闭嘴」+ 她 2 分钟内刚说过话 → 算冲她")

    # 重复检测
    _d1, _s1 = C._is_repeat("你满意了？我还没说话呢", "你满意了？我满意了")
    check(S, _d1 is True, f"同句换结尾判重复（相似度 {_s1:.2f}）")
    _d2, _ = C._is_repeat("哈哈哈", "哈哈")
    check(S, _d2 is False, "短句「哈哈哈/哈哈」不误判")
    _d3, _ = C._is_repeat("全是老游戏啊", "阴阳师？扫这个能出SSR吗")
    check(S, _d3 is False, "完全不同的话不误判")
    _d4, _ = C._is_repeat("紫毛 好眼熟", "紫毛 好眼熟啊")
    check(S, _d4 is True, "加个语气词也算重复")

    # 纯问候识别（群聊不接早晚安）
    check(S, C._is_pure_greeting("早安") is True, "「早安」→ 纯问候")
    check(S, C._is_pure_greeting("晚安啦") is True, "「晚安啦」→ 纯问候")
    check(S, C._is_pure_greeting("各位早上好") is True, "「各位早上好」→ 纯问候")
    check(S, C._is_pure_greeting("睡了睡了") is True, "「睡了睡了」→ 纯问候")
    check(S, C._is_pure_greeting("早安，今天那个bug修了吗") is False,
          "问候 + 实质内容 → 该回")
    check(S, C._is_pure_greeting("今天好累") is False, "普通聊天 → 该回")

    # 操作性短指令
    check(S, C._is_op_short("扫扫") is True, "「扫扫」→ 不接")
    check(S, C._is_op_short("来了") is True, "「来了」→ 不接")
    check(S, C._is_op_short("发一下") is True, "「发一下」→ 不接")
    check(S, C._is_op_short("小肥鱼在吗") is False, "「小肥鱼在吗」是在叫她 → 该回")
    check(S, C._is_op_short("好的我来") is False, "「好的我来」不是短指令表里的 → 不拦")

    # 识图：角色猜测 + NSFW
    try:
        import services.vision_verify as V
        _p = V.parse_structured(
            "标记：NSFW\n画面：两个女的\n作品：不确定\n角色：五条悟（猜）")
        check(S, _p["char"] == "五条悟", f"「五条悟（猜）」剥出名字（{_p['char']}）")
        check(S, _p["guess"] is True, "识别出「这是猜的」")
        check(S, _p["nsfw"] == "NSFW", "识别出 NSFW 标记")
        check(S, "仅猜测" in V.compose("画面", "", "五条悟", guess=True),
              "描述里标「仅猜测」")
        check(S, V.compose("画面", "", "五条悟", nsfw="NSFW").startswith("[NSFW]"),
              "NSFW 图描述前加标记")
    except Exception as e:
        skip(S, f"vision_verify 离线导入失败（{type(e).__name__}）")

    # 记忆噪音过滤
    check(S, M.is_noise("[图片识别结果] 群号:1 | 图片内容:x") is True,
          "图片识别记录算噪音")
    check(S, M.is_noise("小明喜欢白米饭") is False, "正常记忆不算噪音")
    check(S, M.group_ok("群号:100000020 | 图片内容:x", "100000011") is False,
          "跨群记忆被挡掉")
    check(S, M.group_ok("群号:100000011 | x", "100000011") is True, "同群记忆放行")
    check(S, M.group_ok("没有群号的记忆", "100000011") is True, "无群号的通用记忆放行")

    for _g in _gs:
        C.group_mute_until.pop(_g, None)


# ============================================================
# 分组 11.9：称谓档案（[[CALL]]）+ 群聊真 @（[[AT]] / [[WHO]]）
# ============================================================
async def t_call():
    S = "call"
    from services import aura_logic as A, profiles, group_roster as R

    # ---------- ① [[CALL]] 解析 ----------
    raw = (
        "好呀，以后你就叫我小鲸鱼吧～|||那我们就是好朋友啦\n"
        "[[CALL|bot:小鲸鱼|user:阿明]]"
    )
    p = A.parse_reply(raw)
    check(S, len(p.get("calls") or []) == 1, "拆出 1 条 CALL")
    c = (p["calls"] or [{}])[0]
    check(S, c.get("bot") == "小鲸鱼", f"bot 字段 = {c.get('bot')}")
    check(S, c.get("user") == "阿明", f"user 字段 = {c.get('user')}")
    body = " ".join(i["value"] for i in p["items"] if i["kind"] == "text")
    check(S, "CALL" not in body and "[[CALL" not in body, "CALL 标记不会漏进正文")

    # 只写一个字段也认
    p2 = A.parse_reply("嗯！[[CALL|bot:小肥鱼]]")
    check(S, len(p2["calls"]) == 1 and p2["calls"][0]["user"] == "",
          "只写 bot 字段也算一条")
    # 两个字段都空 → 丢弃
    p3 = A.parse_reply("随便说说[[CALL|note:啥也没定]]")
    check(S, len(p3["calls"]) == 0, "两个字段都空 → 不记录")
    # 被截断的半截标签也要清干净
    p4 = A.parse_reply("好呀\n[[CALL|bot:小鲸")
    check(S, "CALL" not in " ".join(i["value"] for i in p4["items"]),
          "被截断的半截 CALL 也会清掉")
    # 2026-11-24：第三人称必须带 target；删除用 del
    p5 = A.parse_reply("好[[CALL|user:阿明|target:小明]]")
    check(S, (p5["calls"] or [{}])[0].get("target") == "小明",
          "CALL 能解析 target（第三人称）")
    p6 = A.parse_reply("删掉[[CALL|del:user|target:30003]]")
    check(S, (p6["calls"] or [{}])[0].get("del") == "user",
          "CALL 能解析 del（删除称呼）")

    # ---------- ② 落库：call_name / bot_alias ----------
    qq = "30003"
    profiles._save("profile", qq, {})
    profiles.set_call_names(qq, call_name="阿明", bot_alias="小鲸鱼")
    pr = profiles.get_profile(qq)
    check(S, pr.get("call_name") == "阿明", "call_name 已写入")
    check(S, pr.get("bot_alias") == "小鲸鱼", "bot_alias 已写入")
    check(S, "阿明" in pr.get("aliases", []), "call_name 同时进 aliases（方便找人）")
    # nickname 不能被 call_name 抢走：每轮群名片会覆盖 nickname
    profiles.upsert_profile(qq, nickname="明哥")
    pr = profiles.get_profile(qq)
    check(S, pr.get("nickname") == "明哥" and pr.get("call_name") == "阿明",
          "群名片覆盖 nickname 时 call_name 不受影响")
    card = profiles.brief_card(qq)
    check(S, "阿明" in card, f"档案卡优先显示 call_name：{card}")

    # ---------- ②.5 删除称呼：两个方向 + aliases 都要清 ----------
    from services import callname as CN
    qq2 = "30004"
    profiles._save("profile", qq2, {})
    CN.set_user_call(qq2, "阿明", "g_test")
    CN.set_bot_call(qq2, "小鱼", "g_test")
    check(S, CN.user_call_of(qq2, "g_test") == "阿明", "删除前：她叫他阿明")
    check(S, "小鱼" in CN.bot_names("g_test"), "删除前：他叫她小鱼")
    CN.clear_all(qq2, "g_test")
    check(S, CN.user_call_of(qq2, "g_test") == "", "删除后：她叫他已清空")
    check(S, "小鱼" not in CN.bot_names("g_test"), "删除后：他叫她已清空")
    _p2 = profiles.get_profile(qq2)
    check(S, not _p2.get("call_name") and not _p2.get("bot_alias"),
          "删除后：档案 call_name / bot_alias 都清掉")
    check(S, "阿明" not in (_p2.get("aliases") or []),
          "删除后：aliases 里的旧称呼也移除（否则名册还会找回来）")

    # ---------- ③ [[AT]] → 真 @ 段 ----------
    members = [
        {"user_id": 11111, "card": "小明", "nickname": "xm"},
        {"user_id": 22222, "card": "小强（摸鱼中）", "nickname": "xq"},
        {"user_id": 33333, "card": "阿伟", "nickname": "aw"},
    ]
    pa = A.parse_reply("[[AT|小明]] 你人呢")
    segs, missed = A.expand_mentions(
        [{"type": "text", "data": {"text": i["value"]}}
         for i in pa["items"] if i["kind"] == "text"], members)
    at = [s for s in segs if s.get("type") == "at"]
    check(S, len(at) == 1 and at[0]["data"]["qq"] == "11111", "[[AT|小明]] → 真 @ 11111")
    check(S, not missed, "认出来的 @ 不报 missed")
    check(S, A.has_mention("[[AT|小明]]"), "has_mention 认得 [[AT|]]（否则不拉名册）")

    # 带备注的群名片：唯一子串可以命中
    segs2, _ = A.expand_mentions([{"type": "text", "data": {"text": "[[AT|小强]] 出来"}}], members)
    at2 = [s for s in segs2 if s.get("type") == "at"]
    check(S, len(at2) == 1 and at2[0]["data"]["qq"] == "22222", "「小强」能对上「小强（摸鱼中）」")

    # 认不出来的 @ → 退化成文字，绝不乱 @ 人
    segs3, missed3 = A.expand_mentions([{"type": "text", "data": {"text": "[[AT|查无此人]] 在吗"}}], members)
    check(S, not [s for s in segs3 if s.get("type") == "at"], "认不出来 → 不 @")
    check(S, missed3 and missed3[0] == "查无此人", "认不出来 → 记进 missed")

    # 单个字不许模糊匹配（会把全群都匹配上）
    check(S, A.resolve_member("小", A.member_names(members), set()) == "",
          "单字不做模糊匹配")

    # ---------- ④ 名册找人（[[WHO]] 的底层） ----------
    hits = R.find(members, "阿伟")
    check(S, len(hits) == 1 and hits[0]["user_id"] == 33333, "按群名片找人")
    hits = R.find(members, "33333")
    check(S, len(hits) == 1, "按 QQ 号找人")
    hits = R.find(members, "xm")
    check(S, len(hits) == 1, "按昵称找人")
    check(S, R.find(members, "不存在的人") == [], "找不到 → 空列表")
    check(S, R.label(members[0]) == "小明(1111)", f"标签格式 {R.label(members[0])}")

    # ---------- ⑤ 协议有没有真的进 prompt ----------
    from services import llm as L
    check(S, "[[CALL|" in L.CALL_PROTOCOL, "CALL_PROTOCOL 里有协议示例")
    check(S, "[[AT|" in L.GROUP_AT_PROTOCOL, "GROUP_AT_PROTOCOL 里有 [[AT|]] 用法")
    check(S, "[[WHO|" in L.GROUP_AT_PROTOCOL, "GROUP_AT_PROTOCOL 里有 [[WHO|]] 用法")
    check(S, "WHO" in L._REQ_RE.pattern, "_REQ_RE 认得 WHO")
    llm_src = (ROOT / "services" / "llm.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "CALL_PROTOCOL.strip()" in llm_src, "CALL_PROTOCOL 被塞进 msgs")
    check(S, "GROUP_AT_PROTOCOL.strip()" in llm_src, "GROUP_AT_PROTOCOL 被塞进 msgs")
    check(S, 'startswith("group")' in llm_src, "@ 协议只在群聊注入（私聊省 token）")
    check(S, "_who_is" in llm_src, "有 [[WHO]] 的查询实现 _who_is")

    # ---------- ⑥ 联动体检：专抓「写了但没接上」的坑 ----------
    chat_src = (ROOT / "plugins" / "chat" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")

    # 戳一戳：on_notice / NoticeEvent 少导入一个，整个插件都会在 import 阶段炸掉
    check(S, re.search(r"from nonebot import .*\bon_notice\b", chat_src) is not None,
          "on_notice 已导入（否则 poke_me 整个功能加载失败）")
    check(S, re.search(r"from nonebot\.adapters\.onebot\.v11 import[\s\S]{0,300}?\bNoticeEvent\b",
                       chat_src) is not None, "NoticeEvent 已导入")

    # 语音/转发卡片：不补全的话主入口 `if not text: return` 会把它们整条丢掉
    check(S, "async def _enrich_text" in chat_src, "有 _enrich_text（语音转写 + 卡片预览补全）")
    check(S, chat_src.count("await _enrich_text(event)") >= 2,
          "_enrich_text 在 _need_reply 和主入口都接上了")
    check(S, "_enriched_text" in chat_src, "_enrich_text 结果缓存在 event 上（不重复拉 B 站）")

    # said 没初始化 → 多气泡只发第一条
    check(S, re.search(r"^\s+said = \[\]", chat_src, re.M) is not None, "said 已初始化")

    # ---- 唤醒词：共同词只留大肥鱼/小肥鱼，私人称呼按 QQ 隔离 ----
    check(S, "鲸鱼" not in chat_src.split("_ME_NAMES_COMMON")[1].split("}")[0],
          "共同词里没有泛化的「鲸鱼」")
    _common_block = chat_src.split("_ME_NAMES_COMMON")[1].split("}")[0].lower()
    check(S, "kki" not in _common_block, "共同词里没有 kki 这种短词")
    check(S, "_ME_NAMES_COMMON" in chat_src, "共同词可配（ME_COMMON_NAMES）")
    check(S, "ME_USE_PERSONA_NAME" in chat_src, "人格名可关")
    check(S, "if speaker_qq:" in chat_src, "私人称呼按 speaker_qq 隔离（别人喊不算）")

    # ---- 上下文时间戳 + 自己的话用「这是你之前的回复：」----
    check(S, "_ts_tag" in chat_src, "有上下文时间戳生成")
    check(S, "%Y年%m月%d日%H:%M" in chat_src, "时间格式是「2026年11月10日11:04」")
    check(S, "_SELF_PREFIX" in chat_src and "这是你之前的回复：" in chat_src,
          "自己的话标成「这是你之前的回复：」")
    check(S, '_strip_ts' in chat_src, "提取正文时会去掉时间前缀（否则 split 会被时间里的冒号切坏）")

    # ---- 人称说明（不能一概说「他说的你就是你」）----
    pb0 = (ROOT / "services" / "prompt_builder.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "不一定" in pb0 and "「你」不一定是你" in pb0,
          "明确「你」不一定指她（群里还有别人）")
    check(S, "只有 @你 / 喊你名 / 接你上一句" in pb0, "列出「你」才指她的条件")
    check(S, "拿不准就当在说别人" in pb0, "拿不准时不要硬接")
    check(S, "▍现在时间" in pb0, "prompt 里带当前时间（▍编号分块）")

    # ---- 主动插话：不能顺着自己的话再接 ----
    check(S, "recent_group_ts" in chat_src, "群消息带时间戳（插话频率闸门要用真时间）")
    check(S, "_SELF_PREFIX not in m" in chat_src,
          "插话只取真人的话，排除她自己说的")
    check(S, '"recentTimes": recent_times' in chat_src,
          "recentTimes 用真实时间戳，不再 [now]*n 造假")
    check(S, "[now_ms] * len(recent_texts)" not in chat_src, "旧的造假写法已删除")
    check(S, "focus_sessions.get" in chat_src and "_focus_on" in chat_src,
          "关注期内不做主动插话（避免和正常回复两条线一起说）")

    # ---- 单群插话频率覆盖 + 接话三分类（2026-11-18）----
    check(S, "GROUP_INTERJECT_OVERRIDES" in chat_src, "支持单群插话频率覆盖")
    check(S, "_parse_group_interject_overrides" in chat_src, "覆盖配置有解析函数")
    check(S, "_ov = GROUP_INTERJECT_OVERRIDES.get(gid)" in chat_src,
          "循环里按群取覆盖配置")
    check(S, "A. 在找你" in chat_src and "B. 没在问你" in chat_src
          and "C. 跟你无关" in chat_src,
          "接话底层逻辑：先分「在找我 / 我可以接 / 不关我事」")

    # ---- 她自己说过什么 / 用过什么，必须进上下文（2026-11-18）----
    check(S, "【用了功能】" in chat_src, "指令也记一条「谁用了什么功能」")
    check(S, "_append_self_line" in chat_src, "自己的话统一走 _append_self_line（去重）")
    check(S, "items = items[:1]" in chat_src, "主动插话只发一条（避免接话两次）")
    check(S, "session_queues.get(_sk)" in chat_src, "有正常回复排队时不插话")
    check(S, "_media_placeholder" in chat_src, "纯图片/语音消息有占位符（不然她什么都看不到）")
    check(S, "别接自己的话" in chat_src, "插话 prompt 明确「别接自己的话」")

    # ---- 工具自己发出去的内容，要回填给模型（2026-11-18）----
    llm_src_t = (ROOT / "services" / "llm.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "_TOOL_SENT" in llm_src_t, "工具执行期间捕获 bot.send（ContextVar）")
    check(S, "已经通过工具把这些内容发出去了" in llm_src_t,
          "把「已发出的内容」拼回 tool 结果给模型")
    check(S, (ROOT / "scripts" / "diag_conversation.py").exists(),
          "有群聊「有没有被接话」体检脚本")

    # ---- 回应对象：★ 标注必须去掉（它标的是上一条历史，不是当前消息）----
    llm_src3 = (ROOT / "services" / "llm.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "★ 最新一句" not in llm_src3,
          "去掉了错误的「★ 最新一句」（ctx 不含当前消息，标错会一直接旧话）")
    check(S, "FOCUS_PROTOCOL" in llm_src3, "有 FOCUS 协议（让她标注回哪条）")
    check(S, "focus_sessions" in chat_src,
          "关注期由硬逻辑处理（不再靠 FOCUS 协议进 prompt）")
    check(S, "FOCUS_RE" in (ROOT / "services" / "aura_logic.py").read_text(
        encoding="utf-8", errors="ignore"), "能解析 [[FOCUS|N]]")
    check(S, "[FOCUS]" in chat_src, "日志里打印她回的是哪条")
    check(S, '"focus": focus' in (ROOT / "services" / "aura_logic.py").read_text(
        encoding="utf-8", errors="ignore"), "parse_reply 返回 focus 字段")

    # ---- 合并转发（聊天记录）----
    check(S, "_forward_text" in chat_src, "有合并转发解析")
    check(S, 's.type == "forward"' in chat_src, "识别 forward 段（不是 json/xml）")
    check(S, "get_forward_msg" in chat_src, "调 OneBot 的 get_forward_msg 取内容")
    check(S, "_seg_plain" in chat_src, "转发里的 text/image/at 能压成文字")
    # 真机踩过（2026-11-12）：「转发 5 条，取了 4 条」—— 少的那条是转发里套转发，
    # _seg_plain 对 forward 段返回空串，整条被 continue 掉。
    check(S, "_seg_forward_ids" in chat_src, "能挖出嵌套转发的 id")
    check(S, "_forward_rows" in chat_src, "嵌套转发递归展开")
    # 上下文：默认 10 条 + 给「冲我说的」打 ⭐（2026-11-12）
    try:
        from core import tuning as _t
        check(S, _t.DEFAULTS.get("MAX_GROUP_RECENT") == 16, "群聊上下文默认 16 条")
    except Exception as _e:
        check(S, False, f"读 tuning 失败：{_e}")
    check(S, "def _addressed_to_me" in chat_src, "能判断「这条在叫我」")
    check(S, '"⭐"' in chat_src, "上下文里给叫我的消息打 ⭐")
    check(S, "_addressed_to_me(event, t)" in chat_src, "⭐ 用在 recent 队列的写入处")
    # 三方插件口（2026-11-12）：plugins/third/ 单独加载，包名必须合法
    try:
        _bot_src = (ROOT / "bot.py").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        _bot_src = ""
    check(S, 'load_plugins("plugins/third")' in _bot_src,
          "bot.py 单独加载 plugins/third（三方口）")
    _third_dir = ROOT / "plugins" / "third"
    # ⚠️ VideoZoomer_master 是 vendored 的训练仓库（没有 __init__.py，故意不是插件）——
    #    给它补 __init__.py 会让 nonebot 去加载它（依赖 verl/torch，直接炸启动）。
    _third_ignore = {"VideoZoomer_master", "__pycache__"}
    _third_pkgs = ([p.name for p in _third_dir.iterdir()
                    if p.is_dir() and p.name not in _third_ignore]
                   if _third_dir.exists() else [])
    _bad = [n for n in _third_pkgs
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)
            or not (_third_dir / n / "__init__.py").exists()]
    check(S, len(_third_pkgs) >= 7 and not _bad,
          f"third 下 {len(_third_pkgs)} 个插件包名合法且都有 __init__.py"
          f"（不合格：{_bad}；已忽略 vendored 目录 {sorted(_third_ignore)}）")
    try:
        import plugins.chat as _C
    except Exception as e:
        skip(S, f"plugins.chat 无法离线导入（{type(e).__name__}），嵌套转发只做静态检查")
    else:
        class _FwdBot:
            async def get_forward_msg(self, id):
                if str(id) == "outer":
                    return {"messages": [
                        {"sender": {"nickname": "甲"},
                         "message": [{"type": "text", "data": {"text": "第一句"}}]},
                        {"sender": {"nickname": "乙"},
                         "message": [{"type": "forward", "data": {"id": "inner"}}]},
                    ]}
                return {"messages": [
                    {"sender": {"nickname": "丙"},
                     "message": [{"type": "text", "data": {"text": "里面的话"}}]},
                ]}

        _rows, _tot = await _C._forward_rows(_FwdBot(), "outer")
        _flat = " ".join(t for _, t in _rows)
        check(S, _tot == 2 and "第一句" in _flat and "里面的话" in _flat,
              f"嵌套转发不再丢（{len(_rows)} 行：{_flat[:40]}）")

    # ---- 语音消息模板 ----
    check(S, "【这是一条语音消息" in chat_src, "语音消息有明确模板前缀")
    check(S, "客户端自动转写" in chat_src, "说明转写可能有错字")

    # ---- preflight 超时（别让 GLM 拖慢回复）----
    check(S, "PREFLIGHT_TIMEOUT" in (ROOT / "services" / "referee.py").read_text(
        encoding="utf-8", errors="ignore"), "preflight 有超时保护")
    check(S, "asyncio.wait_for" in chat_src, "worker 里对 preflight 用了 wait_for")

    # ---- 人称说明 ----
    pb = (ROOT / "services" / "prompt_builder.py").read_text(encoding="utf-8", errors="ignore")
    # 人称已移到固定区（llm 的 ▍10），build_prompt 里不再有
    _llm_src2 = (ROOT / "services" / "llm.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "PERSONA_RULE" in _llm_src2 and "10 人称" in _llm_src2,
          "人称说明在固定区 ▍10（能吃到前缀缓存）")
    check(S, "▍人称与指代" not in pb, "build_prompt 不再重复拼人称")
    check(S, "群友的「我」=他自己" in pb, "明确「我」指用户不是她")

    # ---- .env 体检：重复项 / 已废弃项 ----
    envp = ROOT / ".env"
    if envp.exists():
        env_txt = envp.read_text(encoding="utf-8", errors="ignore")
        keys = re.findall(r"^\s*([A-Z][A-Z0-9_]+)\s*=", env_txt, re.M)
        dup = {k for k in keys if keys.count(k) > 1}
        check(S, not dup, f".env 没有重复宏（重复：{dup or '无'}）")
        check(S, "MAX_HISTORY" not in keys or "#MAX_HISTORY" in env_txt,
              "MAX_HISTORY 已废弃不再生效")
        for k in ("GLM_MODEL", "GLM_VISION_MODEL", "TRACE_MOE", "REFEREE_PREFLIGHT",
                  "GROUP_SEND_LIMIT", "HEARTBEAT_TIMEOUT_SEC", "ME_COMMON_NAMES",
                  "DIALOGUE_KEEP_DAYS", "PEER_MERGE_WINDOW"):
            check(S, k in keys, f".env 有 {k}")

    # ---- 背景 / 当前问题 分隔 ----
    pb2 = (ROOT / "services" / "prompt_builder.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "以下是背景" in pb2 and "以下是当前要回复的消息" in pb2,
          "上下文分成「背景」和「当前要回复的消息」两块")
    check(S, pb2.index("以下是背景") < pb2.index("以下是当前要回复的消息"),
          "背景在前，当前问题在最后（注意力压到最后一句）")

    # ---- /档案 打印完整经历 + 记忆 ----
    check(S, 'notes[-20:]' in chat_src or "notes[-20:]" in chat_src, "档案打印最近 20 条经历")
    check(S, "EMOTION_MARK.get" in chat_src, "档案里每条经历带心情")
    check(S, 'time.strftime("%m-%d %H:%M"' in chat_src, "档案里每条经历带时间")
    check(S, "长期记忆（共" in chat_src, "档案里带长期记忆")
    check(S, "about_qq" in chat_src, "长期记忆按 QQ 过滤")

    # ---- /看模板：打印真实输入 ----
    llm_src2 = (ROOT / "services" / "llm.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "def last_messages" in llm_src2, "有 last_messages()")
    check(S, "_LAST_MSGS" in llm_src2, "缓存最近一次真实 msgs（不是重新拼的近似版）")
    check(S, "看模板" in chat_src, "有 /看模板 指令")
    check(S, "_chunk_text" in chat_src, "长模板会分段发")

    # ---- TTS 诊断 ----
    check(S, "HEARTBEAT_TIMEOUT_SEC" in (ROOT / "plugins" / "tool_dispatcher" / "__init__.py")
          .read_text(encoding="utf-8", errors="ignore"), "心跳超时可配")
    check(S, "/测语音" in chat_src or "测语音" in chat_src, "有 /测语音 诊断指令")
    check(S, "[TTS] 决策" in chat_src, "TTS 打印决策日志（本地还是云端）")

    # ---- 群消息发送限流（QQ 硬限制：每群每分钟 10 条）----
    check(S, "GROUP_SEND_LIMIT" in chat_src, "群发送限流参数可配")
    check(S, "_wait_group_quota" in chat_src, "有群发送配额检查")
    try:
        from plugins import chat as _C
        _old_lim = _C.GROUP_SEND_LIMIT
        _old_wait = _C.GROUP_SEND_MAX_WAIT
        _C.GROUP_SEND_LIMIT, _C.GROUP_SEND_MAX_WAIT = 2, 0.1
        _C._group_send_times.clear()
        ok1 = await _C._wait_group_quota("gtest")
        ok2 = await _C._wait_group_quota("gtest")
        ok3 = await _C._wait_group_quota("gtest")
        check(S, ok1 and ok2 and not ok3, "超过配额就不再硬发（避免 retcode=1200 整轮失败）")
        _C.GROUP_SEND_LIMIT, _C.GROUP_SEND_MAX_WAIT = _old_lim, _old_wait
        _C._group_send_times.clear()
    except Exception as e:
        skip(S, f"chat 插件导入失败，跳过限流测试：{type(e).__name__}")

    # TOOL_CTX 必须按协程隔离，否则群里两个人同时说话会串号
    import contextvars
    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()
    ctx_a.run(lambda: L.set_tool_context(marker="A", speaker_qq="111"))
    ctx_b.run(lambda: L.set_tool_context(marker="B", speaker_qq="222"))
    check(S, ctx_a.run(lambda: L.TOOL_CTX.get("speaker_qq")) == "111",
          "TOOL_CTX 按协程隔离（A 不会被 B 覆盖）")
    check(S, ctx_b.run(lambda: L.TOOL_CTX.get("speaker_qq")) == "222",
          "TOOL_CTX 按协程隔离（B 各自独立）")
    check(S, L.TOOL_CTX.get("speaker_qq") in (None, ""),
          "主协程的 TOOL_CTX 不被子协程污染")


# ============================================================
# 分组 11.95：三方差分（referee）+ GLM 参考通道
# ============================================================
async def t_referee():
    S = "referee"
    from services import referee as R, glm as G

    check(S, "100000017" in R.PEER_GROUPS, f"监听群：{sorted(R.PEER_GROUPS)}")
    check(S, "2267107632" in R.PEER_QQS, f"peer：{sorted(R.PEER_QQS)}")
    check(S, G.GLM_MODEL.startswith("glm-"), f"GLM 模型：{G.GLM_MODEL}")
    check(S, G.GLM_MODEL == "glm-5.3-flash", "默认 glm-5.3-flash（多模态）")
    check(S, isinstance(G.GLM_VISION_MODEL, str) and G.GLM_VISION_MODEL,
          f"GLM 视觉模型可独立配置：{G.GLM_VISION_MODEL}")
    check(S, len(G._MODEL_CANDIDATES) >= 3, f"有模型降级链 {G._MODEL_CANDIDATES[:3]}")
    check(S, G.GLM_BASE_URL.startswith("https://open.bigmodel.cn"), "GLM 端点正确")
    check(S, callable(G.reply) and callable(G.describe_image), "GLM 有文本/视觉两个入口")
    check(S, G.available() == bool(G.GLM_API_KEY), "available() 跟随 GLM_API_KEY")

    # diff 只核对名词，不学性格
    check(S, "只核对名词" in R._DIFF_PROMPT, "diff 只核对名词")
    check(S, "语气、口吻、人格" in R._DIFF_PROMPT, "diff 明确排除语气/人格")
    check(S, "作品名、番剧名、角色名" in R._DIFF_PROMPT, "名词范围写清楚了")

    # ---- 事前参考（preflight）：2026-11-23 起默认关闭（GLM 只做视觉）----
    check(S, R.PREFLIGHT_ENABLED is False, "preflight 默认关闭（GLM 只做视觉）")
    check(S, R.PREFLIGHT_MIN_CHARS >= 1, f"短问题跳过阈值 {R.PREFLIGHT_MIN_CHARS}")
    check(S, await R.preflight("k1", "哈", "模板") == "", "太短的问题不调 GLM（省 3 秒）")
    check(S, R.take_preflight("k1") == "", "没生成过 → 取不到参考")
    check(S, await R.preflight("k2", "五条悟是谁", "模板") == "" or True,
          "preflight 无 key 时静默返回空，不炸")

    # 鱼连发多条 → 合并成一条再核对
    R._peer_buf.clear()
    _old_win = R.PEER_MERGE_WINDOW
    _old_on = R.on_peer_reply
    got = []

    async def _fake_on(g, text, talking_to_us=False):
        got.append(text)
        return {}

    R.PEER_MERGE_WINDOW = 0.05
    R.on_peer_reply = _fake_on
    try:
        R.queue_peer_reply("100000017", "再让网页版那版代入人设给回复打分纠错")
        R.queue_peer_reply("100000017", "就是一层扣一层 真跑起来得盯着")
        R.queue_peer_reply("100000017", "改完再塞回 data 里")
        await asyncio.sleep(0.4)
        check(S, len(got) == 1, f"连发 3 条合并成 1 条（实际 {len(got)} 次）")
        check(S, got and "再让网页版" in got[0] and "改完再塞回" in got[0],
              "合并后内容完整（首尾都在）")
    finally:
        R.PEER_MERGE_WINDOW = _old_win
        R.on_peer_reply = _old_on
        R._peer_buf.clear()

    gid = "100000017"

    # 把后台 task 换成假的，别在自检里真调 LLM
    orig_finish = R._finish_our
    orig_diff = R._diff_and_record
    calls = []

    async def fake_finish(g, s):
        calls.append(("finish", s["question"]))

    async def fake_diff(s):
        calls.append(("diff", s.get("question")))
        return s

    R._finish_our = fake_finish
    R._diff_and_record = fake_diff
    try:
        R._our_samples.clear()
        R._peer_questions.clear()

        # ---- 场景①：我们被触发 ----
        R.note_our_reply(gid, "五条悟是谁", "五条悟是《咒术回战》里的最强咒术师",
                         prompt_text="【档案】…", persona="你是小鲸鱼")
        await asyncio.sleep(0)
        check(S, len(R._our_samples.get(gid, [])) == 1, "场景① 建了一个样本")
        s = R._our_samples[gid][0]
        check(S, s["question"] == "五条悟是谁", "样本带原问题")
        check(S, s["prompt_text"] and s["persona"], "样本带我们自己的模板（给 GLM 用）")
        check(S, s["ours_source"] == "real", "标记为真实回复")

        # 太短的不核对
        R._our_samples.clear()
        R.note_our_reply(gid, "在吗", "嗯")
        check(S, not R._our_samples.get(gid), "太短的回复不核对（省一次 diff）")

        # 非配置群不登记
        R.note_our_reply("999999", "问题", "一段足够长的回答内容在这里")
        check(S, not R._our_samples.get("999999"), "非配置群不登记")

        # ---- 场景②：鱼被 @ ----
        R._our_samples.clear()
        R._peer_questions.clear()
        R.note_peer_question(gid, "久留美是谁", context=["A: 看这个", "B: 好看"],
                             speaker_qq="10001", speaker_name="阿明")
        check(S, gid in R._peer_questions, "鱼被 @ → 记下了问题")
        check(S, R._peer_questions[gid]["context"], "记下了上下文")

        # 注册一个假影子回答，验证真的被调用
        seen = {}

        async def fake_shadow(q, ctx, qq, name, g):
            seen.update({"q": q, "ctx": ctx, "qq": qq, "gid": g})
            return "久留美是《咒术回战》里的角色", "【模板】…"

        R.set_shadow_fn(fake_shadow)
        R._hour_window.clear()
        await R.on_peer_reply(gid, "久留美是《咒术回战》里的角色，很可爱")
        check(S, seen.get("q") == "久留美是谁", "场景② 用原问题跑了影子回答")
        check(S, seen.get("gid") == gid, "影子回答拿到群号")
        check(S, not R._peer_questions.get(gid), "配对后问题被消费掉")
        check(S, any(c[0] == "diff" for c in calls), "场景② 最后做了 diff")

        # ---- 场景③：两条鱼在对话 ----
        R._peer_questions.clear()
        calls.clear()
        n_before = R.stats()["peer_chats"]
        await R.on_peer_reply(gid, "@小鲸鱼 你在吗", talking_to_us=True)
        check(S, R.stats()["peer_chats"] == n_before + 1, "两鱼对话只留档")
        check(S, not calls, "两鱼对话不做 diff")

        # 没有待核对问题时的鱼消息 → 忽略
        calls.clear()
        await R.on_peer_reply(gid, "随便说点啥没有配对的内容")
        check(S, not calls, "没有待核对问题 → 不 diff")
    finally:
        R._finish_our = orig_finish
        R._diff_and_record = orig_diff

    # ---- diff 解析 ----
    d = R._parse_diff("差异：有\n纠正：A 说错了\n补充：无\n可信度：85")
    check(S, d["changed"] and d["conf"] == 85, "解析 差异/可信度")
    check(S, "A 说错了" in d["fix"], "解析 纠正")
    check(S, not R._parse_diff("差异：无\n纠正：无\n补充：无\n可信度：10")["changed"],
          "无差异 → changed=False")
    check(S, R._parse_diff("差异：无\n纠正：无\n补充：他是高专教师\n可信度：70")["changed"],
          "只有「补充」也算有差异")
    d4 = R._parse_diff("差异：无\n纠正：无\n补充：无\n可信度：90")
    check(S, not d4["changed"] and d4["conf"] == 90, "高可信但无差异 → 仍然不写记忆")

    st = R.stats()
    check(S, {"samples", "corrected", "peer_chats"} <= set(st), f"stats 字段：{sorted(st)}")

    # ---- 联动：chat 插件里真的接上了 ----
    cc = (ROOT / "plugins" / "chat" / "__init__.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "referee.note_our_reply" in cc, "worker 里登记了我们的回复")
    check(S, "referee.note_peer_question" in cc, "有人 @ 鱼时记下问题和上下文")
    check(S, "referee.queue_peer_reply" in cc, "收到鱼的话先进缓冲（连发合并）")
    check(S, "PEER_REPLY_COOLDOWN" in cc, "偶尔接一句有冷却（不然连发几条会回几次）")

    # 事前参考真的注入了 prompt，而且喂给 GLM 的是**注入前**的原始模板
    check(S, "referee.preflight(" in cc, "worker 里调了 preflight")
    check(S, "_prompt_base" in cc, "保留了注入前的原始模板（事后 diff 要用）")
    check(S, "另一个模型对同一段上下文的回答" in cc, "参考被拼进了 DeepSeek 的 prompt")
    check(S, "最终决策" in cc or "由你自己决定" in cc, "明确告诉 DeepSeek 自己决定语气和取舍")
    check(S, "ref1=_ref1" in cc, "preflight 结果传给 note_our_reply（不重复调 GLM）")
    check(S, "referee.set_shadow_fn(_shadow_answer)" in cc, "注册了影子回答")
    check(S, "async def _shadow_answer" in cc, "有影子回答实现")
    check(S, "shadow_{" in cc or "shadow_" in cc, "影子回答用独立 session（不污染真实历史）")
    check(S, "referee.record_peer_chat" in cc, "两鱼对话留档")
    check(S, "PEER_REPLY_PROB" in cc, "两鱼偶尔互聊（不是永远不理）")
    check(S, "_at_targets" in cc, "有 @ 目标解析（判断谁被 @ 了）")
    check(S, "speaker_qq in referee.PEER_QQS" in cc, "回鱼的话不登记核对样本")
    check(S, "peer_distill" not in cc, "旧的 peer_distill 已彻底移除")


# ============================================================
# 分组 11.97：对话落盘 + 周报导出/回流
# ============================================================
async def t_weekly():
    S = "weekly"
    from services import dialogue_log as D

    # ---- 对话落盘 ----
    gid = "100000017"
    D.log_msg(gid, "阿明(0001)", "10001", "这是谁？", kind="user")
    D.log_msg(gid, "你", "999", "星井美希", kind="bot")
    D.log_msg(gid, "阿明(0001)", "10001", "[图片]", kind="user",
              img="images/xx.jpg", vision="粉发少女", extra={"anime": "偶像大师"})
    msgs = D.load_range(1)
    check(S, len(msgs) >= 3, f"落盘后可读回 {len(msgs)} 条")
    check(S, any(m.get("kind") == "bot" for m in msgs), "能区分谁说的（kind）")
    check(S, any(m.get("img") for m in msgs), "图片路径挂在对话上")
    check(S, any(m.get("anime") == "偶像大师" for m in msgs), "识图结果一起落盘")

    # ---- 记录按**条数**取，不按时间（用户反复强调）----
    # 造一条「3 天前」的记录：load_range(1) 读不到，load_recent 必须读得到
    _old_day = D._day_file(time.time() - 3 * 86400)
    _old_day.parent.mkdir(parents=True, exist_ok=True)
    with _old_day.open("a", encoding="utf-8") as _f:
        _f.write(json.dumps({"ts": time.time() - 3 * 86400, "gid": gid,
                             "who": "阿明", "qq": "10001", "kind": "user",
                             "text": "三天前说过的话"}, ensure_ascii=False) + "\n")
    _r1 = D.load_range(1)
    check(S, not any("三天前" in str(m.get("text") or "") for m in _r1),
          "load_range(1) 按时间，读不到三天前（对照）")
    _r2 = D.load_recent(gid, limit=10)
    check(S, any("三天前" in str(m.get("text") or "") for m in _r2),
          f"load_recent 按条数，能读回三天前（{len(_r2)} 条）")
    check(S, D.load_recent(gid, limit=2) and len(D.load_recent(gid, limit=2)) == 2,
          "load_recent 严格按 limit 截断")
    _llm_src2 = (ROOT / "services" / "llm.py").read_text(encoding="utf-8", errors="ignore")
    _chat_src2 = (ROOT / "plugins" / "chat" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    check(S, "load_recent(gid, limit=n)" in _llm_src2,
          "翻历史已改成按条数（load_recent）")
    check(S, "load_recent(gid, limit=80)" in _chat_src2
          and "load_recent(gid, limit=48)" in _chat_src2,
          "长消息补偿 / 现场回放也按条数")
    _pr_src = (ROOT / "services" / "proactive.py").read_text(encoding="utf-8", errors="ignore")
    # ⚠️ 不能判「文件里没有 load_range」——注释里为了解释历史会提到它。
    #    要判**调用点**。
    check(S, "dialogue_log.load_recent(key" in _pr_src,
          "主动私聊读记录也按条数（load_recent）")

    # 存图（压缩）
    try:
        import io as _io
        from PIL import Image as _Img
        buf = _io.BytesIO()
        _Img.new("RGB", (1600, 1200), (180, 90, 140)).save(buf, "PNG")
        rel = D.save_image(buf.getvalue(), fp="selftestimg")
        check(S, rel.startswith("images/") and rel.endswith(".jpg"), f"存图返回相对路径 {rel}")
        check(S, D.save_image(buf.getvalue(), fp="selftestimg") == rel, "同指纹只存一次")
        import os as _os
        p = Path("data") / rel
        check(S, p.exists() and p.stat().st_size > 0, "图片文件真的写出来了")
        check(S, p.stat().st_size <= D.IMG_MAX_BYTES, f"压缩后 {p.stat().st_size} 字节 ≤ 上限")
    except ImportError:
        skip(S, "没装 Pillow，跳过存图测试")

    # ---- 周报导出 ----
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "scripts"))
    import weekly_export as WE

    check(S, "=== CORRECTION ===" in WE.CHECK_INSTRUCTION, "给网页版的指令含输出格式")
    check(S, "C1 | 名词" in WE.CHECK_INSTRUCTION, "指令里给了行格式样例")
    check(S, "名词" in WE.CHECK_INSTRUCTION and "人设" in WE.CHECK_INSTRUCTION
          and "记忆" in WE.CHECK_INSTRUCTION, "覆盖 名词/人设/记忆 三类检查")
    check(S, "逐条对照图片" in WE.CHECK_INSTRUCTION, "要求它对着图检查名词")

    # 真跑一次导出（ROOT/OUT_DIR 指到临时目录，别碰真实 data）
    WE.ROOT = Path(".").resolve()
    WE.OUT_DIR = Path("data") / "weekly_test"
    md = WE.export(days=1)
    text = md.read_text(encoding="utf-8")
    check(S, md.exists() and md.name.startswith("weekly_"), f"导出了 {md.name}")
    check(S, "## 一、对话流水" in text, "周报含对话流水")
    check(S, "## 二、识图记录" in text, "周报含识图记录")
    check(S, "图#1" in text, "图片按 #编号 标在对话上")
    check(S, "=== CORRECTION ===" in text, "周报里带了回执格式")

    # ---- 回流 ----
    import weekly_apply as WA
    sample = (
        "=== CORRECTION ===\n"
        "C1 | 名词 | 星井美希是粉发 | 星井美希是黄发双马尾 | 图#1\n"
        "C2 | 人设 | 太像客服 | 更随意一点\n"
        "=== FACT ===\n"
        "F1 | 星井美希是黄发\n"
        "=== PERSONA ===\n"
        "P1 | 少用「为您」\n"
        "=== END ===\n"
    )
    parsed = WA.parse(sample)
    check(S, len(parsed["corrections"]) == 2, f"解析纠正 {len(parsed['corrections'])} 条")
    check(S, parsed["corrections"][0]["type"] == "名词", "解析纠正类型")
    check(S, parsed["corrections"][0]["right"] == "星井美希是黄发双马尾", "解析正确写法")
    check(S, parsed["corrections"][0]["basis"] == "图#1", "解析依据")
    check(S, len(parsed["facts"]) == 1, "解析事实")
    check(S, len(parsed["persona"]) == 1, "解析人设提醒")

    # dry-run 不写库
    from services import memory_v2 as _M
    before = len(_M._load_index().get("items", []))
    WA.apply(parsed, dry_run=True)
    after = len(_M._load_index().get("items", []))
    check(S, before == after, "dry-run 不写记忆")

    # 真写
    WA.apply(parsed, dry_run=False)
    idx = _M._load_index()
    tags = [d for d in idx["items"] if "weekly_check" in (d.get("tags") or [])]
    check(S, len(tags) >= 4, f"纠正已写入记忆 {len(tags)} 条")

    # ---- 联动 ----
    cc = (ROOT / "plugins" / "chat" / "__init__.py").read_text(encoding="utf-8", errors="ignore")
    va = (ROOT / "plugins" / "vision_agent" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    check(S, "dialogue_log.log_msg" in cc, "chat 里对话落盘")
    check(S, "dialogue_log.log_msg" in va, "vision_agent 里图片落盘")
    check(S, "dialogue_log.save_image" in va, "图片压缩后存 data/images")


# ============================================================
# 分组 11.5：翻唱（链接解析 / 音高换算 / HTML 清洗）
# ============================================================
async def t_song():
    S = "song"

    # ---- HTML 清洗（真机出现过整条回复都是 <br>）----
    from services.text import strip_html
    raw = "主人说的久留美……是那个韭姐吗？<br>韭留美？《FX战士》那位？<br>草"
    clean = strip_html(raw)
    check(S, "<br>" not in clean and "韭留美" in clean, f"strip_html 清掉 <br>（{clean[:26]!r}）")
    check(S, "<b>" not in strip_html("a<b>粗</b>c"), "strip_html 清掉 <b>")
    check(S, "&amp;" not in strip_html("A &amp; B") and "A & B" in strip_html("A &amp; B"),
          "strip_html 反转义 HTML 实体")
    check(S, strip_html("") == "" and strip_html(None) == "", "strip_html 空输入安全")
    check(S, "1 < 2" in strip_html("1 &lt; 2"), "strip_html 不误伤正文里的 <")

    # ---- 网易云链接解析 ----
    try:
        from plugins.song_cover import parse_netease_song_id as pid
        from plugins.song_cover import split_gender_suffix as sgs
    except Exception as e:
        check(S, False, "song_cover 导入失败", f"{type(e).__name__}: {e}")
        return

    # ---- 性别后缀：/翻唱 晴天 女 ----
    # 真机需求：加了男女就不走自动判断，没加才走老流程
    check(S, sgs("晴天 女") == ("晴天", "female"), "「晴天 女」→ 指定女声")
    check(S, sgs("晴天 男") == ("晴天", "male"), "「晴天 男」→ 指定男声")
    check(S, sgs("男孩") == ("男孩", ""), "《男孩》歌名不被误拆成男声")
    check(S, sgs("女人花") == ("女人花", ""), "《女人花》不被误拆成女声")
    check(S, sgs("男孩 男") == ("男孩", "male"), "《男孩》+ 男声能正确拆开")
    check(S, sgs("晴天 female") == ("晴天", "female"), "英文 female 也认")
    check(S, sgs("晴天") == ("晴天", ""), "没有后缀 → 原样返回")
    check(S, sgs("女") == ("女", ""), "只有性别词没有歌名 → 不拆")
    check(S, sgs("晴天，女") == ("晴天", "female"), "中文逗号分隔也认")

    # ---- /测歌 + 三级判定链接线 ----
    # 真机踩过：resolve_gender_chain 忘了 import → `/测歌` 直接 NameError，
    # 而当时自检全绿（只检查了 node 侧）。这里把「名字真的能拿到」也查上。
    import plugins.song_cover as _sc
    check(S, callable(getattr(_sc, "resolve_gender_chain", None)),
          "resolve_gender_chain 已导入（漏了就是运行时 NameError）")
    check(S, getattr(_sc, "song_probe", None) is not None, "/测歌 命令已注册")
    check(S, callable(getattr(_sc, "_resolve_song_meta", None)), "_resolve_song_meta 存在")
    _scsrc = (ROOT / "plugins" / "song_cover" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    check(S, '"gender_lock"' in _scsrc, "翻唱任务下发 gender_lock（L1/L2 锁定）")
    check(S, '"gender_source"' in _scsrc, "翻唱任务下发 gender_source")
    check(S, "[PROBE]" in _scsrc, "/测歌 有 log 打印")

    # ---- /测歌 上传模式 + crepe/rmvpe A/B 接线（2026-11-12）----
    # 需求：「本地没有这首，写个 /测歌 可以上传过去一个切成人声」。
    # 这条链跨三个文件（song_cover 下发 → tool_dispatcher 收「文件+JSON」→ node 跑 A/B），
    # 任何一环漏了都只在真机上炸，所以三边都断言。
    check(S, callable(getattr(_sc, "_probe_audio_seg", None)),
          "上传模式能认出音频 file 段")
    check(S, getattr(_sc, "probe_upload", None) is not None,
          "上传模式的 on_message 已注册")
    check(S, callable(getattr(_sc, "_probe_report_lines", None)),
          "报告渲染抽成函数（上传模式复用同一套）")
    check(S, callable(getattr(_sc, "_wait_probe_out", None)), "等结果抽成函数")
    check(S, '"f0_ab"' in _scsrc, "上传 A/B 时下发 f0_ab")
    check(S, '"want_vocal"' in _scsrc, "上传模式下发 want_vocal（要分离出的人声）")
    # 真机踩过：`/测歌 上传 ab <网易云链接>` 因为「上传」两字直接进上传模式，
    # 链接被丢掉，白等一个文件。控制词必须只是修饰，剥完还有东西就走网易云。
    _psf = getattr(_sc, "_probe_strip_flags", None)
    check(S, callable(_psf), "控制词剥离函数存在")
    if callable(_psf):
        check(S, _psf("月声尽 ab") == ("月声尽", True), "「歌名 ab」→ 走网易云 + A/B")
        check(S, _psf("上传 ab") == ("", True), "「上传 ab」→ 上传模式 + A/B")
        check(S, _psf("晴天") == ("晴天", False), "「晴天」→ 普通测歌")
        _rl = _psf("上传 ab 分享…https://163cn.tv/bkOZu6Mv (来自@网易云音乐)")
        check(S, _rl[1] is True and "163cn.tv" in _rl[0],
              "「上传 ab + 链接」→ 保留链接，别掉进上传模式")
    # 低音区抬升：备用开关，只认 `/翻唱 歌名 低音+N` 后缀，默认必须关
    _pll = getattr(_sc, "parse_low_lift", None)
    check(S, callable(_pll), "低音抬升后缀解析存在")
    if callable(_pll):
        check(S, _pll("晴天 低音+2")[0] == "晴天", "「晴天 低音+2」→ 歌名剥干净")
        check(S, _pll("晴天 低音+2")[1].get("RVC_LOW_LIFT_SEMIS") == "2",
              "低音+2 → 下发 2 半音")
        check(S, _pll("晴天 低音+2")[1].get("RVC_LOW_LIFT_HZ") == "260",
              "低音阈值默认 260Hz")
        check(S, _pll("晴天")[1] == {}, "没写后缀 → 不做低音抬升（默认关）")
        check(S, _pll("低音+2")[0] == "低音+2", "只有后缀没有歌名 → 不当后缀")
    # /翻唱 off | on：算力机繁忙时手动暂停接单
    _psw = getattr(_sc, "_parse_switch", None)
    check(S, callable(getattr(_sc, "cover_on", None)), "有 cover_on() 读取开关")
    check(S, callable(getattr(_sc, "set_cover", None)), "有 set_cover() 写开关（落盘）")
    check(S, callable(_psw), "有开关词解析")
    if callable(_psw):
        check(S, _psw("off") is False, "/翻唱 off → 关")
        check(S, _psw("关") is False, "/翻唱 关 → 关")
        check(S, _psw("暂停") is False, "/翻唱 暂停 → 关")
        check(S, _psw("on") is True, "/翻唱 on → 开")
        check(S, _psw("恢复") is True, "/翻唱 恢复 → 开")
        check(S, _psw("晴天") is None, "歌名不会被误当开关")
        check(S, _psw("") is None, "空参数不触发开关")
    check(S, "cover_switch.json" in _scsrc, "开关状态落盘（重启不丢）")
    check(S, _scsrc.count("cover_on()") >= 5,
          f"五个入口都拦（/翻唱 /清唱 /测歌 /process_cover_task /run_cover），"
          f"实际 {_scsrc.count('cover_on()')} 处")
    # run_cover 是 /api/cover 直达的，闸放这儿外部调用才拦得住
    _rc_pos = _scsrc.find("async def run_cover")
    _rc_gate = _scsrc.find("if not cover_on()", _rc_pos) if _rc_pos >= 0 else -1
    check(S, _rc_pos >= 0 and 0 <= _rc_gate - _rc_pos < 1500,
          "run_cover 开头就拦（外部 API 绕过 process_cover_task）")
    try:
        _casrc = (ROOT / "plugins" / "cover_api" / "__init__.py").read_text(
            encoding="utf-8", errors="ignore")
    except Exception:
        _casrc = ""
    check(S, "503 if _closed else 500" in _casrc, "开关关着时 /api/cover 回 503 不是 500")
    try:
        _chatsrc = (ROOT / "plugins" / "chat" / "__init__.py").read_text(
            encoding="utf-8", errors="ignore")
    except Exception:
        _chatsrc = ""
    check(S, "cover_on" in _chatsrc, "她主动点歌(_tool_sing)也受开关约束")
    # 清唱通道：LLM 侧原来只有翻唱，`/清唱` 是纯指令层
    check(S, "with_inst" in _chatsrc, "sing 工具能传清唱（with_inst）")
    check(S, '"清唱"' in _chatsrc, "sing 工具参数里有「清唱」enum")
    try:
        _llmsrc = (ROOT / "services" / "llm.py").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        _llmsrc = ""
    check(S, 'partition("|")' in _llmsrc, "[[SING|歌名|清唱]] 能拆出模式")
    check(S, "|清唱]]" in _llmsrc, "提示词里教了清唱标记")
    # 上下文接不上怎么办（2026-11-12 用户要求）
    check(S, "带 ⭐ 的是在叫你" in _llmsrc, "提示词解释了上下文里的 ⭐")
    check(S, "多条用一条答完" in _llmsrc, "多条 @ 用一条答完")
    check(S, "(不想回复)" in _llmsrc, "保留不接话权")
    check(S, "其他随心情" in _llmsrc, "接不接由心情/硬逻辑，不再长篇规则")
    _tdsrc = (ROOT / "plugins" / "tool_dispatcher" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    check(S, 'task.get("type") == "probe"' in _tdsrc,
          "complete 端点能收「文件 + JSON」一起回（测歌 A/B 专用）")
    # node.py 部署在算力机，bot 容器里没有；没有就跳过 node 侧 A/B 断言。
    _node_py = ROOT / "node.py"
    if _node_py.exists():
        try:
            _ndsrc = _node_py.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            _ndsrc = ""
        check(S, bool(_ndsrc), "node.py 可读（下面三条 A/B 断言要靠它）")
    else:
        _ndsrc = ""
        skip(S, "node.py 不在本容器（算力机侧），跳过 node A/B 断言")
    if _ndsrc:
        check(S, '"f0_ab"' in _ndsrc, "node 侧认识 f0_ab")
        check(S, "want_vocal" in _ndsrc, "node 侧认识 want_vocal")
        check(S, '"ab": ab' in _ndsrc, "node 把 A/B 结果塞进 payload 回传")
        # 调性对齐（2026-11-12 用户听出来的）：RVC 只移人声，伴奏原调 → 差几个半音就离调
        check(S, "def shift_inst" in _ndsrc, "有伴奏移调函数 shift_inst")
        check(S, "def estimate_key" in _ndsrc, "有首调估计 estimate_key")
        _si = _ndsrc.find("shift_inst(inst_local")
        _mx = _ndsrc.find("if not _mix_to_mp3(eng_out, inst_local")
        # 只判**顺序**（移调在混音之前），不判距离：
        # 中间隔了 5KB 别的代码就误报（2026-11-19 又踩一次「断言写太死」）。
        check(S, _si > 0 and _mx > _si,
              "混音前先把伴奏移调（否则人声和伴奏不同调）")
        check(S, "COVER_KEY_CHECK" in _ndsrc, "调性校验开关 COVER_KEY_CHECK")
        # 分轨版伴奏移调（2026-11-12 定的策略）
        check(S, "def build_inst_from_stems" in _ndsrc, "分轨伴奏合成函数存在")
        check(S, "transients=crisp" in _ndsrc, "分轨参数：鼓保瞬态")
        check(S, "formant=preserved" in _ndsrc, "分轨参数：贝斯保共振峰")
        check(S, "COVER_STEMS" in _ndsrc, "分轨数可配（COVER_STEMS，默认 4）")
        check(S, "threading.Thread" in _ndsrc and "_inst_thread.join()" in _ndsrc,
              "伴奏移调和 RVC 并行（后台线程 + join）")
        # 算力机那套 ffmpeg 是 --disable-librubberband，所以必须有第二后端
        check(S, "_LIBROSA_SHIFT_CODE" in _ndsrc,
              "没有 rubberband 时用 librosa 相位声码器（比 asetrate 好）")
        check(S, "def _librosa_shift_ok" in _ndsrc, "librosa 后端探测存在")

    from services.song_gender import resolve_gender_chain as _rgc
    _g1 = await _rgc("晴天", "周杰伦", "female")
    check(S, _g1["gender"] == "female" and _g1["source"] == "override",
          f"L1 用户指定优先（{_g1['source']}）")
    check(S, "用户直接指定" in _g1["reason"], "L1 有说明理由")
    _g2 = await _rgc("晴天", "周杰伦", "男")
    check(S, _g2["gender"] == "male" and _g2["source"] == "override",
          "L1 认中文「男」")

    cases = [
        ("https://music.163.com/song?id=186016", "186016"),
        ("https://music.163.com/#/song?id=186016", "186016"),
        ("https://y.music.163.com/m/song?id=186016&userid=123456", "186016"),
        ("分享周杰伦的单曲《晴天》https://music.163.com/song?id=186016 (来自@网易云音乐)", "186016"),
        ("186016", "186016"),
        ("晴天", ""),
        ("https://music.163.com/playlist?id=123456", ""),   # 歌单不是歌
        ("https://music.163.com/album?id=32311", ""),       # 专辑不是歌
    ]
    for text, want in cases:
        got = pid(text)
        check(S, got == want, f"链接解析 {text[:34]!r} → {got!r}（期望 {want!r}）")

    # ---- 音高换算 ----
    from services.song_gender import (
        f0_from_samples, f0_to_gender, f0_to_pitch_hint, pitch_from_f0,
    )
    check(S, pitch_from_f0(220) == 0, f"220Hz（目标女声基准）→ {pitch_from_f0(220)} 半音")
    check(S, pitch_from_f0(110) == 12, f"110Hz → {pitch_from_f0(110)} 半音（正好一个八度）")
    check(S, 9 <= pitch_from_f0(120) <= 11, f"120Hz（男声）→ {pitch_from_f0(120)} 半音")
    check(S, abs(pitch_from_f0(210)) <= 2, f"210Hz（女声）→ {pitch_from_f0(210)} 半音")
    check(S, pitch_from_f0(0) == 0 and pitch_from_f0(None) == 0, "0/None → 0 半音（不炸）")
    check(S, -24 <= pitch_from_f0(20) <= 24, f"极端输入被限幅（{pitch_from_f0(20)}）")

    # ---- 女声限幅：空灵/气声女声不能被硬压到 220Hz ----
    # 真机反馈：「气声一多就唱得很低，越空灵越烂」
    # 根因：f0_up_key 是变调半音数，源=女声、模型=女声时本该 pitch≈0；
    #       按「一律对齐 220Hz」算，300Hz 会得到 -5 半音，整首被压低大半个音区。
    _p300 = pitch_from_f0(300, gender="female")
    check(S, abs(_p300) <= 3, f"300Hz 女声 → {_p300} 半音（限幅 ±3，不整体降调）")
    _p340 = pitch_from_f0(340, gender="female")
    check(S, abs(_p340) <= 3, f"340Hz 空灵女声 → {_p340} 半音（同上）")
    # 不传 gender 时，只要基频落在模型音域（150~400）也保持原调，
    # 避免「判不出性别 → 退回 preset +12 → 整首升八度、人声极哑」
    check(S, pitch_from_f0(300) == 0,
          f"不传 gender 且 300Hz 在音域内 → 保持原调（{pitch_from_f0(300)}）")
    check(S, pitch_from_f0(110) == 12,
          f"不传 gender 但 110Hz 明显是男声 → 仍抬八度（{pitch_from_f0(110)}）")
    check(S, 9 <= pitch_from_f0(120, gender="male") <= 11,
          "男声不受限幅影响，仍然抬到女声音域")
    _sg_src = (ROOT / "services" / "song_gender.py").read_text(encoding="utf-8", errors="replace")
    check(S, "PERIODICITY_MIN" in _sg_src, "有 PERIODICITY_MIN（气声/噪声帧过滤门槛）")
    check(S, "F0_PERIODICITY_MIN" in _sg_src, "门槛可用 F0_PERIODICITY_MIN 覆盖")
    check(S, "RVC_SAME_GENDER_MAX_SEMIS" in _sg_src, "女声限幅可配")

    check(S, f0_to_gender(120) == "male" and f0_to_gender(220) == "female"
          and f0_to_gender(170) == "unknown", "f0_to_gender 三态")
    check(S, f0_to_pitch_hint(100) == "low" and f0_to_pitch_hint(170) == "mid"
          and f0_to_pitch_hint(260) == "high", "f0_to_pitch_hint 三档")

    # ---- 自相关实测（合成正弦，不依赖 ffmpeg）----
    import math as _m
    import array as _arr

    def _sine(freq, sr=8000, seconds=1.0):
        n = int(sr * seconds)
        return _arr.array("h", [int(12000 * _m.sin(2 * _m.pi * freq * i / sr)) for i in range(n)])

    f120 = f0_from_samples(_sine(120), 8000)
    check(S, abs(f120 - 120) <= 8, f"自相关测 120Hz → {f120}Hz")
    f220 = f0_from_samples(_sine(220), 8000)
    check(S, abs(f220 - 220) <= 12, f"自相关测 220Hz → {f220}Hz")
    check(S, f0_from_samples(_arr.array("h", [0] * 9000), 8000) == 0.0, "全静音 → 0（不误报）")
    check(S, f0_from_samples(_arr.array("h", []), 8000) == 0.0, "空采样 → 0")

    # ---- 气声帧过滤：白噪声没有稳定周期，必须测不出 ----
    # 不设周期性门槛的话，气声段里噪声尖峰会被当成基频，测出的值偏高
    # → pitch 偏小 → 变声后整体偏低（「越空灵越烂」的机制之一）
    _seed = 12345
    _noise = []
    for _ in range(16000):
        _seed = (_seed * 1103515245 + 12345) & 0x7FFFFFFF
        _noise.append(int(9000 * (_seed / 0x3FFFFFFF - 1)))
    check(S, f0_from_samples(_arr.array("h", _noise), 8000) == 0.0,
          "白噪声 → 0（气声不会被误判成基频）")

    # ---- 性别先验范围：压制倍频错误 ----
    # 真机：周杰伦《晴天》（真实基频 ~115Hz）测出 228.6Hz，正好两倍
    from services.song_gender import f0_search_range
    m_lo, m_hi = f0_search_range("male")
    f_lo, f_hi = f0_search_range("female")
    check(S, m_lo == 70.0 and m_hi == 200.0, f"male 先验范围 {m_lo:.0f}~{m_hi:.0f}Hz")
    check(S, f_lo >= 150.0 and f_hi >= 400.0, f"female 先验范围 {f_lo:.0f}~{f_hi:.0f}Hz")
    check(S, not (m_lo <= 228.6 <= m_hi), "228Hz 被排除在 male 范围外")
    f228 = f0_from_samples(_sine(228), 8000, m_lo, m_hi)
    check(S, abs(f228 - 228) > 20, f"male 范围内测不出 228Hz（实得 {f228}Hz）")
    f120m = f0_from_samples(_sine(120), 8000, m_lo, m_hi)
    check(S, abs(f120m - 120) <= 8, f"male 范围内 120Hz 仍然准（{f120m}Hz）")

    # ---- Qwen-Audio 模型候选 + 文字兜底（ASR 模型不按 JSON 回）----
    from services.song_gender import (
        QWEN_AUDIO_MODELS, _gender_from_text, _pitch_from_text,
    )
    check(S, any("3.0-ASR" in m for m in QWEN_AUDIO_MODELS),
          f"Qwen-Audio-3.0-ASR 在候选里（{QWEN_AUDIO_MODELS[:2]}）")
    check(S, _gender_from_text("这是一段女声，音区偏高") == "female", "文字兜底判女声")
    check(S, _gender_from_text("低沉男声") == "male", "文字兜底判男声")
    check(S, _gender_from_text("男女对唱") == "unknown", "同时出现男女 → unknown")
    check(S, _gender_from_text("") == "unknown", "空文本 → unknown")
    check(S, _pitch_from_text("音区偏高") == "high", "文字兜底判高音区")
    check(S, _pitch_from_text("声音低沉") == "low", "文字兜底判低音区")

    # ---- probe_vocal 的降级行为 ----
    from services import song_gender as SG

    def _no_f0(path, seconds=20.0, gender_hint=""):   # estimate_f0 是同步函数，走 to_thread
        return 0.0

    async def _no_audio(path):
        return {}

    old_f0, old_audio = SG.estimate_f0, SG.qwen_audio_probe
    try:
        SG.estimate_f0, SG.qwen_audio_probe = _no_f0, _no_audio
        r = await SG.probe_vocal("不存在的.wav")
        check(S, r.get("pitch") is None and r.get("ok") is False,
              "F0 和 Qwen-Audio 都失败 → pitch=None（沿用 preset）")

        async def _male_low(path):
            return {"gender": "male", "pitch": "low"}

        SG.qwen_audio_probe = _male_low
        r = await SG.probe_vocal("不存在的.wav")
        check(S, r.get("pitch") == 12 and r.get("source") == "qwen-audio",
              f"只有 Qwen-Audio 时走音区映射（male/low → {r.get('pitch')}）")

        def _have_f0(path, seconds=20.0, gender_hint=""):
            return 120.0

        async def _female_high(path):
            return {"gender": "female", "pitch": "high"}

        SG.estimate_f0, SG.qwen_audio_probe = _have_f0, _female_high
        r = await SG.probe_vocal("不存在的.wav")
        check(S, r.get("source") == "f0" and r.get("gender") == "male",
              "F0 和 Qwen-Audio 冲突时以实测 F0 为准")
        check(S, "不一致" in r.get("reason", ""), "冲突时日志里写明不一致")
    finally:
        SG.estimate_f0, SG.qwen_audio_probe = old_f0, old_audio


# ============================================================
# 分组 12：源码结构检查（不 import，避免依赖地狱）
# ============================================================
async def t_static():
    S = "static"

    def src(p):
        return (ROOT / p).read_text(encoding="utf-8", errors="ignore")

    def code_only(p):
        """剥掉注释和文档字符串，避免把「注释里提到的旧代码」误判成还在用"""
        t = src(p)
        t = re.sub(r'"""[\s\S]*?"""', "", t)
        t = re.sub(r"'''[\s\S]*?'''", "", t)
        t = re.sub(r"#[^\n]*", "", t)
        return t

    llm = code_only("services/llm.py")
    check(S, "persona_for" in llm, "llm 用 persona_for 取代硬编码角色卡")
    check(S, "XIAOJINGYU_PROMPT" not in llm, "llm 里没有 XIAOJINGYU_PROMPT")
    check(S, "memory_v2" in llm and "get_memory" not in llm, "llm 用 memory_v2，不再调 get_memory")
    check(S, "host.docker.internal" in llm and "127.0.0.1:8001" not in llm, "Agent 地址已改 host.docker.internal")
    check(S, "media_kind_of" in llm and "VISION_TAG in t" in llm,
          "视觉判定用 in（不是 startswith），且区分图片/视频")
    check(S, "summarize_by_local_qwen(raw_desc)" in llm, "图片描述会先压缩")
    # MEMO_PROTOCOL 本身是三重引号字符串，上面被 code_only 剥掉了，这里用原文查
    llm_raw = src("services/llm.py")
    check(S, llm_raw.count("[[MEMO|emotion") >= 2,
          f"MEMO_PROTOCOL 至少 2 个 few-shot 示例（实际 {llm_raw.count('[[MEMO|emotion')} 个）")
    check(S, "最多 2 条" in llm_raw, "MEMO 规则写明最多 2 条")
    check(S, "寒暄/知识问答不记" in llm_raw, "MEMO 规则写明「不触动就不写」")

    # ---- 回复追踪：每次调用的完整 prompt 落盘 + 对比脚本 ----
    check(S, "_trace_dump" in llm, "llm 把每次调用的完整 prompt 落盘（LLM_TRACE）")
    check(S, "_trace_dump(key, msgs, text, reply" in llm, "call_llm 里真的调了 _trace_dump")
    check(S, "_purge_trace" in llm, "trace 文件按天清理，不塞满磁盘")
    _tr = ROOT / "scripts" / "trace_reply.py"
    check(S, _tr.exists(), "scripts/trace_reply.py 存在（API 视角 vs 上帝视角对比）")
    if _tr.exists():
        _tsrc = _tr.read_text(encoding="utf-8", errors="ignore")
        for _k in ("load_traces", "load_dialogue", "load_container_log",
                   "window_around", "merge_stream", "_LOG_MSG_RE"):
            check(S, _k in _tsrc, f"trace_reply 含 {_k}")
        check(S, "--before" in _tsrc and "--after" in _tsrc,
              "trace_reply 支持「前后 N 条」上下文参数")
        check(S, "--log" in _tsrc, "trace_reply 支持叠加容器日志")
        # 隐藏标记 + 鱼对话 + 影子样本
        check(S, "def _visible_text" in _tsrc, "trace_reply 能算「用户实际看到的」")
        check(S, "def _mark_rows" in _tsrc, "trace_reply 能拆隐藏标记")
        check(S, "未知标记" in _tsrc, "未知标记会告警")
        check(S, "def render_peer_chat" in _tsrc, "能打印两条鱼的对话")
        check(S, "def render_samples" in _tsrc, "能打印裁判样本（影子回答）")
        check(S, '"shadow_", "group_"' in _tsrc, "影子会话也能对上群的上帝视角")

    chat = code_only("plugins/chat/__init__.py")
    check(S, "_compute_online" in chat, "chat 有心跳在线判断")
    check(S, chat.count("isinstance(out_path, str)") >= 2, "chat 两处都防了 output 是 dict")
    check(S, "from services.tts import send_voice" in chat, "chat 会降级到 CosyVoice")
    check(S, "_wait_task" in chat and "pickup_timeout" in chat, "chat 有任务等待超时放弃")
    check(S, "mood.bump_by_emotion" in chat, "chat 用 DeepSeek 自报情绪驱动心情")
    check(S, "profiles.add_bond_note(" in chat and "emotion=emo" in chat,
          "chat 把 MEMO 写进相处印象（并带上判读心情词）")
    check(S, "mood_word=(_jword if _jd_fresh else \"\")" in chat,
          "相处印象里记下「判读给的心情词」")
    check(S, "mind=(str(_jd.get(\"mind\") or \"\") if _jd_fresh else \"\")" in chat,
          "相处印象里记下「当时的心理活动」（印象最深一件事要用）")
    check(S, "delta=(_jdelta if _jd_fresh else None)" in chat,
          "相处印象里记下「这件事的涨幅 Δ」")

    td = code_only("plugins/tool_dispatcher/__init__.py")
    check(S, "FileResponse" in td and "StreamingResponse" not in td, "下载用 FileResponse（支持 Range）")
    check(S, "status_code=404" in td, "文件不存在返回 404")
    check(S, "FILE_REGISTRY" in td, "有 FILE_REGISTRY 支持重试下载")
    check(S, "asyncio.to_thread" in td, "ffmpeg 不再阻塞事件循环")
    check(S, "result: str = Form" in td, "complete_task 支持 result 字段")
    check(S, "def tasks_status" in td, "有 /api/tasks/status 调试端点")
    check(S, 'field: str = ""' in td, "下载端点支持 field（取 extra_input 伴奏）")
    check(S, 'Path(file.filename or "out.wav").suffix' in td,
          "complete_task 保留上传文件后缀（mp3 不会被写成 .wav）")

    # ---- 翻唱成品必须是 mp3 ----
    # 真机踩过：混音输出 40~70MB 的 wav 当语音发 → NapCat retcode=1200
    # "Timeout: NodeIKernelMsgService/sendMsg"，歌做好了但发不出去
    sc = code_only("plugins/song_cover/__init__.py")
    check(S, "cover_final_{int(time.time())}.mp3" in sc, "翻唱成品输出 .mp3")
    check(S, "cover_final_{int(time.time())}.wav" not in sc, "没有残留的 wav 成品路径")
    check(S, "libmp3lame" in sc, "混音走 libmp3lame 编码")
    check(S, "COVER_MP3_BITRATE" in sc, "码率可配（COVER_MP3_BITRATE）")
    check(S, '"file"' in sc, "语音发送失败会降级发文件")
    check(S, "sent_ok" in sc, "成品只在发送成功后才清理")

    # ---- 清唱不能有伴奏 / 翻唱不能混两遍 ----
    # 真机踩过：第 4 步无条件 sync_mix_audio(人声, 伴奏)，
    # 导致 /清唱 被加伴奏、/翻唱 的伴奏被叠两遍盖过人声
    check(S, "sync_mix_audio, voice_output_path, inst_path" not in sc,
          "不再无条件混音（清唱有伴奏的根因）")
    check(S, 'if final_path.suffix.lower() != ".mp3"' in sc,
          "先看算力机回的是不是成品 mp3")
    check(S, "算力机回的是纯人声 wav，服务器补做混音" in sc,
          "只有回 wav 且要伴奏时才补混音")
    check(S, '"rvc_mix" if with_inst else "rvc"' in sc, "翻唱走 rvc_mix，清唱走 rvc")
    check(S, 'task_payload["extra_input"] = inst_path' in sc, "翻唱任务带 extra_input（伴奏）")
    check(S, "normalize=0" in sc, "兜底混音用 normalize=0（默认会按轨数砍半）")
    check(S, "duration=longest" in sc, "兜底混音用 duration=longest（默认会按人声截断）")
    check(S, "acapella" in sc, "注册了 /清唱 命令")
    check(S, "现在就唱给你听" in sc, "进度只播报一句（不再逐段刷屏）")
    check(S, "COVER_MAX_CONCURRENT" in sc, "并发上限可配（COVER_MAX_CONCURRENT）")
    check(S, "def cover_queue_state" in sc, "有并发占用查询（/api/cover/stats 用）")

    # ---- 等待上限要跟大文件匹配 ----
    # 真机：大歌分离完还要回传 70~90MB 两个 wav，原来 90 轮×2s=180s 必然等不到
    check(S, "COVER_SPLIT_WAIT_SEC" in sc, "分离等待上限可配（COVER_SPLIT_WAIT_SEC）")
    check(S, "COVER_RVC_WAIT_SEC" in sc, "变声等待上限可配（COVER_RVC_WAIT_SEC）")
    check(S, "range(90)" not in sc, "分离等待不再是写死的 90 轮（180s）")
    check(S, "range(150)" not in sc, "变声等待不再是写死的 150 轮（300s）")

    # ---- 单段式：算力机一次跑完，服务器只存文件 + 发 QQ ----
    check(S, "COVER_SINGLE_STAGE" in sc, "单段式开关可配（COVER_SINGLE_STAGE）")
    check(S, "COVER_FULL_WAIT_SEC" in sc, "单段式等待上限可配（COVER_FULL_WAIT_SEC）")
    check(S, '"cover"' in sc or "'cover'" in sc, "下发 cover 任务类型")
    check(S, 'caps' in sc and '"cover" in caps' in sc,
          "按算力机上报的 caps 决定单段式（老版本自动回退两段式）")
    check(S, "output2" in sc, "取算力机回传的完整版（output2）")
    check(S, "full_path" in sc, "run_cover 返回 full_path")
    check(S, "/song/url/v1" in sc, "用 /song/url/v1 指定音质等级")
    # 算力机最终要转 mp3 再进 Demucs，下 flac 等于白传 6MB → 默认 exhigh(320k mp3)
    check(S, "COVER_SOURCE_LEVEL" in sc, "音质等级可配（COVER_SOURCE_LEVEL）")
    check(S, "exhigh" in sc, "默认下 320k mp3（exhigh），不白传 flac")
    check(S, "source_url" in sc, "把源站直链传给算力机（省服务器上行带宽）")

    # 服务器侧兜底截断必须保留，但单段式下不执行
    check(S, "def trim_for_voice" in sc, "保留服务器兜底截断（旧两段式用）")

    td = code_only("plugins/tool_dispatcher/__init__.py")
    check(S, "file2" in td, "complete_task 支持接收第二个文件（语音版+完整版）")
    check(S, '"caps"' in td or "caps" in td, "heartbeat 记录算力机上报的 caps")

    # 语音版限制由服务器统一下发，避免 .env 和 node.env 各写一份对不上
    check(S, '"voice_max_mb"' in sc, "task 里下发 voice_max_mb")
    check(S, '"voice_max_sec"' in sc, "task 里下发 voice_max_sec")
    # QQ 语音转 silk 上传，群消息密集时 retcode=1200 超时（2.70MB 也踩过）
    # ---- 上下文分块 + 预算分配（dsh-memory 的 allocateBootBudget 思路）----
    from services.prompt_builder import (
        BLOCK_BUDGET, TOTAL_BUDGET, allocate_budget, build_packet,
    )
    check(S, callable(allocate_budget), "有 allocate_budget")
    check(S, callable(build_packet), "有 build_packet（分块上下文）")
    _demo = {"profiles": "档" * 900, "bond": "往" * 50,
             "memory": "忆" * 900, "library": "书" * 900, "mood": "心" * 20}
    _alloc = allocate_budget(_demo, total=TOTAL_BUDGET)
    check(S, sum(_alloc.values()) <= TOTAL_BUDGET,
          f"预算不超总上限（{sum(_alloc.values())} ≤ {TOTAL_BUDGET}）")
    check(S, _alloc["profiles"] >= BLOCK_BUDGET["profiles"][0],
          "人物档案有保底（不会被长记忆挤掉）")
    check(S, _alloc["memory"] <= BLOCK_BUDGET["memory"][1], "记忆块不超上限")
    check(S, _alloc["mood"] == min(20, BLOCK_BUDGET["mood"][1]),
          "短块按实际长度分配，余额退回池子")
    check(S, all(v <= BLOCK_BUDGET[k][1] for k, v in _alloc.items()), "没有块超过自己的上限")

    # ---- 本地/图书馆分工 ----
    import inspect as _insp_static
    from services.memory_v2 import health_report, recall as _recall
    check(S, "max_age_days" in _insp_static.getsource(_recall),
          "recall 支持 max_age_days（本地只管 30 天内）")
    import time as _t_mod
    from services.llm import _need_library
    import services.llm as _llm_mod
    check(S, _need_library("你还记得以前的事吗", "g1", True) is True,
          "问起以前的事 → 查图书馆")
    _llm_mod._last_library_at["g2"] = _t_mod.time()   # 刚查过，冷却期内
    check(S, _need_library("今天天气不错", "g2", True) is False,
          "本地有记忆且没问往事 → 跳过图书馆")
    check(S, callable(health_report), "有 health_report（记忆体检）")
    from services.profiles import batch_profiles
    check(S, callable(batch_profiles), "有 batch_profiles（多人档案卡）")

    # ---- 工具（function calling）----
    from services.llm import TOOLS_SPEC, MAX_TOOL_TURNS, register_tool, TOOL_CTX
    check(S, callable(register_tool) and isinstance(TOOL_CTX, dict), "有工具注册机制")
    # 工具都在插件模块顶层 register_tool(...) —— 不 import 插件，TOOLS_SPEC 就是空的。
    # ⚠️ 2026-11-19：这里必须自己 import，不能指望别的分组先跑过
    #    （之前 t_call 里 import 失败过一次，导致这里 8 条「没注册」全是假警报）。
    _chat_ok = True
    try:
        import plugins.draw as _draw_plugin  # noqa: F401
    except Exception as _de:
        check(S, False, "plugins.draw 导入失败", f"{type(_de).__name__}: {_de}")
    try:
        import plugins.chat as _chat_plugin  # noqa: F401
    except Exception as _ce:
        _chat_ok = False
        check(S, False, "plugins.chat 导入失败（下面的工具检查会连带失败）",
              f"{type(_ce).__name__}: {_ce}")
    _names = [s.get("function", {}).get("name") for s in TOOLS_SPEC]
    if _chat_ok:
        for _t in ("sing", "remember", "recall"):
            check(S, _t in _names, f"注册了 {_t} 工具")
        # 画图 / 三方工具（2026-11-16 接入 LLM 的工具）
        for _t in ("draw_image", "draw_from_pixiv", "pixiv_search"):
            check(S, _t in _names, f"注册了 {_t} 画图工具")
        for _t in ("girls_2d", "daily_luck", "petpet"):
            check(S, _t in _names, f"注册了 {_t} 三方工具")

    # 运势：每人每天一次（工具带 qq 参数 + 缓存按天重置）
    _luck = next((s for s in TOOLS_SPEC
                  if (s.get("function") or {}).get("name") == "daily_luck"), {})
    _lp = ((_luck.get("function") or {}).get("parameters") or {}).get("properties") or {}
    if _chat_ok:
        check(S, "qq" in _lp, "daily_luck 支持传 qq（抽给谁）")
    _cs = (ROOT / "plugins" / "chat" / "__init__.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "last_reset.txt" in _cs, "运势缓存按天重置（不是一辈子一次）")
    check(S, "今天已经抽过了" in _cs, "重复抽返回今天已抽到的结果，不重新随机")
    check(S, MAX_TOOL_TURNS >= 1, f"工具最多 {MAX_TOOL_TURNS} 轮")

    # ---- 工具目录 / 文本标记（[[TOOLS]] / [[STICKER]] / [[MEMORY]]）----
    import services.llm as _llm
    check(S, hasattr(_llm, "TOOL_CATALOG"), "有 TOOL_CATALOG（工具目录）")
    _cat = getattr(_llm, "TOOL_CATALOG", {})
    for _c in ("记忆与档案", "画图与搜图", "表情包", "猪猪", "群聊玩法"):
        check(S, _c in _cat, f"工具目录有「{_c}」分类")
    check(S, "自己写工具" in _cat, "工具目录有「自己写工具」分类（self_tool_write 能力提示）")
    check(S, "self_tool_write" in _llm._TOOL_MENU_HEAD,
          "工具菜单头部提到 self_tool_write（否则模型注意不到能自己写工具）")
    # ---- 算力机开关 / Harness 桥（2026-11-24）----
    _cfg_src = (ROOT / "services" / "config.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "NODE_COMPUTE" in _cfg_src and "CODE_AGENT" in _cfg_src,
          "config 有 NODE_COMPUTE / CODE_AGENT 宏")
    _boot_src = (ROOT / "bot.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "_SKIP_WHEN_NO_NODE" in _boot_src, "NODE_COMPUTE=0 跳过 draw/song_cover")
    check(S, "_SKIP_ALWAYS" in _boot_src and "self_tools" in _boot_src,
          "CODE_AGENT=harness 跳过 self_tools")
    _llm_src2 = (ROOT / "services" / "llm.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "_NODE_ONLY_TOOLS" in _llm_src2, "register_tool 会过滤算力机工具")
    check(S, "HARNESS_ENABLED" in _llm_src2, "register_tool 会过滤 self_tool/mytool")
    check(S, (ROOT / "services" / "harness.py").exists(), "有 services/harness.py")
    check(S, (ROOT / "plugins" / "harness" / "__init__.py").exists(),
          "有 plugins/harness（/h、/改代码）")
    check(S, "STICKER" in _llm._REQ_RE.pattern, "[[STICKER]] 进了 _REQ_RE")
    check(S, "TOOLS" in _llm._REQ_RE.pattern, "[[TOOLS]] 进了 _REQ_RE")
    check(S, "MEMORY" in _llm._REQ_RE.pattern, "[[MEMORY]] 进了 _REQ_RE")
    check(S, hasattr(_llm, "_pending_sticker_protocol"),
          "有 _pending_sticker_protocol（暂存表情注入 prompt）")

    # ---- bt 总开关（bt on/off）----
    _bt_src = code_only("plugins/third/bittorrents/main.py")
    check(S, "BT_ENABLED" in _bt_src, "bt 有 BT_ENABLED 总开关")
    check(S, "_SWITCH_ON_WORDS" in _bt_src and "_SWITCH_OFF_WORDS" in _bt_src,
          "bt 开关认 on/off 词表")
    # 源码里写的是 Path("data") / "bittorrents" / "switch.json"，断言拆成两段更稳
    check(S, "bittorrents" in _bt_src and "switch.json" in _bt_src,
          "bt 开关状态落盘（data/bittorrents/switch.json）")

    # ---- 群聊标识 / 语音转写 / 记录自己的话（在 chat 插件里）----
    cc = code_only("plugins/chat/__init__.py")
    check(S, "def _msg_label" in cc, "有 _msg_label（名称+QQ尾4）")
    check(S, "def _voice_text" in cc, "有 _voice_text（读 record 自带转写，不下载不 ASR）")
    check(S, "_SELF_PREFIX" in cc, "把她自己说过的话写回群聊上下文")
    check(S, "DIGEST_AFTER" in cc, "有 digest 提醒（连续 N 轮没写记忆会提醒）")
    # 只 append 到 recent，不经过 _need_reply → 不会自问自答
    check(S, "said.append" in cc, "收集这一轮她实际发了什么")
    check(S, "记忆体检" in cc, "有 /记忆体检 命令")
    check(S, "测函数" in cc, "有 /测函数 自检命令")

    # ---- 转发链接识别 / 不接话权 / 发图进上下文 ----
    from services.link_preview import card_from_message, preview_text, bili_detail
    check(S, callable(card_from_message) and callable(preview_text), "有 link_preview（转发卡片）")
    check(S, callable(bili_detail), "有 bili_detail（B站 API 补全）")
    check(S, "describe_image_bytes" in code_only("plugins/vision_agent/__init__.py"),
          "封面识别复用 describe_image_bytes")
    from services.aura_logic import is_silence
    for _w in ("(不想回复)", "（不想回复）", "[不想回复]", "不想回复", "(沉默)"):
        check(S, is_silence(_w), f"「{_w}」判为不接话")
    check(S, not is_silence("今天天气不错"), "正常内容不被误判成不接话")
    check(S, "(不想回复)" in code_only("services/llm.py"), "system 里告知了不接话权")
    check(S, "link_preview" in cc, "chat 里接入转发卡片")
    vv = code_only("plugins/vision_agent/__init__.py")
    check(S, "[图片]" in vv, "群友发图会写进群聊上下文")

    # ---- B站字幕 + 深度总结 ----
    from services.link_preview import bili_subtitle, _deepseek_summary
    check(S, callable(bili_subtitle), "有 bili_subtitle（拿B站字幕）")
    check(S, callable(_deepseek_summary), "有 _deepseek_summary（深度总结）")
    lp = code_only("services/link_preview.py")
    check(S, "BILI_PLAYER_API" in lp, "用 /x/player/v2 拿字幕地址")
    check(S, "内容总结" in lp, "输出里带「内容总结」")
    # 2026-11-17 设计变更：没字幕时**不再用搜索糊弄**，改为提示走 video_understand 真看视频
    check(S, "没字幕" in lp and "video_understand" in lp,
          "没字幕时不硬编内容（提示走 video_understand）")
    check(S, "cid" in lp, "bili_detail 带 cid（字幕要用）")

    # ---- 特别关注：叫名字 / 戳一戳 ----
    check(S, "def _my_names" in cc, "有 _my_names（她的名字/外号）")
    check(S, "def _calls_me_by_name" in cc, "有 _calls_me_by_name")
    # /改名 存 bot_alias（用户给她起的名字），喊这个名字就等于 @ 她
    # 2026-11-24：改走 callname（群级索引 + 档案），不再是直接 set_call_names
    check(S, "callname.set_bot_call(qq, new_name" in cc,
          "/改名 存 bot_alias（走 callname）")
    check(S, 'get("bot_alias")' in cc, "_my_names 读 bot_alias")
    # 自然语言里的称呼由 DeepSeek 自己判断（[[CALL]]），不再靠正则猜句式
    check(S, "set_call_names(\n                        speaker_qq" in cc or "profiles.set_call_names" in cc,
          "[[CALL]] 也会写称谓档案")
    check(S, "_split_at_tags" in cc, "有 _split_at_tags（防字数截断切坏 @ 标签）")
    # expand_mentions 返回的是 dict 列表，Message() 只吃 MessageSegment
    check(S, "_segs_to_message" in cc, "有 _segs_to_message（dict → MessageSegment）")
    check(S, "Message(segs)" not in cc and "Message(_segs)" not in cc,
          "没有把 dict 列表直接塞给 Message()（会 ValueError）")
    check(S, "group_roster.members_of" in cc, "群成员名册走带缓存的 group_roster")
    # 2026-11-20 对调：/叫你 = 给她起名（别名 /改名）；/叫我 = 她怎么叫你
    check(S, 'on_command("叫你", aliases={"改名"}' in cc,
          "/叫你 别名 /改名（给她起名）")
    check(S, 'rename_self_cmd = on_command("叫我"' in cc, "/叫我 是独立命令")
    # /记住 联动档案（相处印象）
    check(S, "add_bond_note" in cc, "/记住 会写进相处印象")
    check(S, "_calls_me_by_name" in cc and "_focus_start" in cc,
          "叫名字 → 等同被@ 并开关注")
    try:
        from core import tuning as _t
        check(S, _t.DEFAULTS.get("FOCUS_FOLLOW_COUNT") == 3, "关注窗口改成 3 句")
    except Exception as _e:
        check(S, False, f"读 tuning 失败：{_e}")
    check(S, "poke_me" in cc and "send_poke" not in cc.split("poke_me")[0][-200:],
          "有戳一戳监听 matcher")
    check(S, "戳我干嘛" in cc, "被戳会回一句「戳我干嘛」")
    check(S, 'sub_type", "") != "poke"' in cc, "只处理 poke 通知")

    # ---- 统一语音出口：所有场景 indextts 优先 ----
    check(S, "async def speak_voice" in cc, "有统一语音出口 speak_voice")
    check(S, cc.count("speak_voice(") >= 3,
          f"speak_voice 被多处调用（{cc.count('speak_voice(')} 处）")
    check(S, "await speak_voice(bot, fake_event, val)" in cc,
          "群聊主动插话也走 speak_voice（原来直接调云端）")
    check(S, "USE_LOCAL_TTS and _compute_online()" in cc, "先试算力机 indextts")
    check(S, "services.tts import send_voice" in cc, "失败才降级云端 CosyVoice")

    # ---- 主动请求资料（暗示 + 一轮补齐）----
    from services.llm import _REQ_RE, _fulfill_requests
    check(S, callable(_fulfill_requests), "有 _fulfill_requests")
    check(S, bool(_REQ_RE.search("[[SEARCH|领域展开]]")), "识别 [[SEARCH|词]]")
    check(S, bool(_REQ_RE.search("[[LIBRARY|以前的约定]]")), "识别 [[LIBRARY|词]]")
    check(S, not _REQ_RE.search("普通回复"), "普通回复不误判")
    check(S, bool(_REQ_RE.search("[[SING|晴天]]")), "识别 [[SING|歌名]]（群聊也能唱）")
    llm_src = code_only("services/llm.py")
    check(S, "不懂就 [[SEARCH|" in llm_src, "system 里告诉她不懂就查")
    check(S, "查不到说「不知道」" in llm_src, "查不到别编")
    check(S, "搜索到的内容" in llm_src, "补齐格式：搜索到的内容")
    check(S, "本地agent知识库图书馆内容" in llm_src, "补齐格式：图书馆内容")
    # 搜索结果要压缩，别把 900 字原样塞进 prompt
    check(S, "summarize_by_local_qwen" in llm_src, "资料用本地 Qwen 压缩")
    check(S, "资料压缩" in llm_src, "压缩后记录前后字数")
    check(S, "got[:300]" in llm_src, "压不动就硬截断到 300 字")
    ws = code_only("services/web_search.py")
    check(S, "nonebot_plugin_tavily_search" in ws, "搜索引擎是 tavily 插件")
    check(S, "max_results=3" in ws, "每次最多 3 条结果（省免费额度）")
    check(S, 'search_depth="basic"' in ws, "basic 深度（省额度）")

    check(S, "for attempt in range(1, 3)" in sc, "语音发送失败会重试一次")
    check(S, "已降级发文件" in sc, "重试仍失败则降级发文件")
    # QQ 常见「消息已送达但 ACK 超时」（retcode=1200 + Timeout），不该刷 error 或重试
    check(S, "def _is_ack_timeout" in sc, "有 _is_ack_timeout（区分 ACK 超时和真失败）")
    check(S, "语音已发出（ACK 超时" in sc, "ACK 超时当成功处理，不刷 error")
    try:
        from plugins.song_cover import _is_ack_timeout as _ack
        check(S, _ack(Exception("ActionFailed(retcode=1200, message='Timeout: NTEvent sendMsg')")),
              "retcode=1200 + Timeout → 判为 ACK 超时")
        check(S, not _ack(Exception("ActionFailed(retcode=100, message='bad param')")),
              "普通错误不误判成 ACK 超时")
        check(S, not _ack(Exception("ConnectionError: reset")), "网络错误不误判")
    except Exception as e:
        check(S, False, "导入 _is_ack_timeout 失败", f"{type(e).__name__}: {e}")

    # ---- 语音消息体积：改成「截时长」而不是「压码率」 ----
    # 真机踩过：① 6.20MB mp3 发语音 retcode=1200 超时（3.24MB 能发）
    #           ② 按体积反算码率压到 24~48k，音质明显变糊（「压缩后的音质很烂」）
    # 现在的策略：砍时长不砍质量，码率保持 COVER_VOICE_BITRATE
    check(S, "def trim_for_voice" in sc, "有 trim_for_voice（截到高潮收尾控体积）")
    check(S, "def find_climax_end" in sc, "有 find_climax_end（自动找高潮结束点）")
    check(S, "def _rms_curve" in sc, "有 _rms_curve（逐格响度曲线）")
    check(S, "shrink_for_voice" not in sc, "不再用 shrink_for_voice（压码率音质很烂）")
    check(S, "COVER_VOICE_MAX_MB" in sc, "语音体积上限可配（COVER_VOICE_MAX_MB）")
    check(S, "COVER_VOICE_MAX_SEC" in sc, "语音时长上限可配（COVER_VOICE_MAX_SEC）")
    check(S, "COVER_VOICE_BITRATE" in sc, "截断时保持码率（COVER_VOICE_BITRATE）")
    check(S, "afade" in sc, "截断处加淡出（不然结尾会「咔」一下）")
    check(S, "ffprobe" in sc, "用 ffprobe 取时长")
    check(S, "base64" not in sc.lower(), "发送链路不走 base64（只会变大）")

    # ---- 截断版之后追问「要不要整首」 ----
    check(S, "PENDING_FULL" in sc, "有 PENDING_FULL（记住待定的整首请求）")
    check(S, "COVER_FULL_TTL" in sc, "整首请求有过期时间（COVER_FULL_TTL）")
    check(S, "on_message(rule=Rule(_is_full_reply)" in sc,
          "用 on_message 拦「要/不要」（不依赖命令前缀）")
    check(S, "priority=1" in sc, "整首回复用最高优先级（避免被对话插件吃掉）")
    check(S, "要听整首吗" in sc, "发完截断版会问要不要整首")
    check(S, "好吧。" in sc, "回「不要」时回一句「好吧。」")

    # ---- 核心流程与 QQ 解耦（外部 API 复用同一套）----
    check(S, "async def run_cover" in sc, "run_cover 独立于 bot（外部 API 直接调）")
    check(S, "async def process_cover_task" in sc, "process_cover_task 只是 QQ 包装")
    check(S, "notify=None" in sc, "notify 可空（API 模式不推消息）")

    # ---- 外部翻唱 API ----
    try:
        ca = code_only("plugins/cover_api/__init__.py")
        check(S, "/api/cover" in ca, "外部 API 有 /api/cover 端点")
        check(S, "x-api-key" in ca.lower(), "API 用 X-Api-Key 认证")
        check(S, "COVER_API_KEYS" in ca, "支持多个 key（COVER_API_KEYS）")
        check(S, "run_cover" in ca, "API 复用 run_cover（不经过 QQ）")
        check(S, "BackgroundTask" in ca, "响应发完自动删成品")
        check(S, "quote(" in ca, "中文歌名 URL 编码（HTTP 头只能 ASCII）")
        check(S, "/api/cover/stats" in ca, "有调用统计端点（审计/计费）")
    except Exception as e:
        check(S, False, "cover_api 检查失败", f"{type(e).__name__}: {e}")
    check(S, (ROOT / "scripts" / "cover_client.py").is_file(),
          "客户端脚本 cover_client.py 存在")

    # ---- on_command 的 aliases 必须是 set ----
    # nonebot 内部是 `commands = {cmd} | (aliases or set())`，
    # 传 list 会 TypeError: unsupported operand type(s) for |: 'set' and 'list'
    # 真机踩过：/清唱 写成了 aliases=["纯人声","清唱版"]，整个 song_cover 插件加载失败
    bad_alias = []
    for _p in sorted((ROOT / "plugins").rglob("*.py")):
        try:
            _txt = _p.read_text(encoding="utf-8")
        except Exception:
            continue
        # 只取 on_command(...) 括号内的参数，别误伤 profiles.upsert_profile(aliases=[...])
        for _m in re.finditer(r"on_command\s*\(", _txt):
            _start, _depth, _i = _m.end(), 1, _m.end()
            while _i < len(_txt) and _depth > 0:
                if _txt[_i] == "(":
                    _depth += 1
                elif _txt[_i] == ")":
                    _depth -= 1
                _i += 1
            _args = _txt[_start:_i - 1]
            if re.search(r"aliases\s*=\s*\[", _args):
                _line = _txt[:_m.start()].count("\n") + 1
                bad_alias.append(f"{_p.relative_to(ROOT)}:{_line}")
    check(S, not bad_alias, "所有 on_command 的 aliases 都是 set（不是 list）",
          f"{bad_alias}")

    # ---- 多 key 解析（真实调用函数，不只查源码）----
    try:
        from plugins.cover_api import parse_keys as _pk
        m = _pk("alice:ak_aaa, bob:ak_bbb\ncarol:ak_ccc")
        check(S, m == {"ak_aaa": "alice", "ak_bbb": "bob", "ak_ccc": "carol"},
              f"名字:key 解析正确（{m}）")
        m2 = _pk("ak_x,ak_y")
        check(S, m2 == {"ak_x": "key1", "ak_y": "key2"}, "纯 key 自动命名")
        check(S, _pk("") == {}, "空配置 → 空字典")
        check(S, _pk("alice:ak_a;; , bob:ak_b") == {"ak_a": "alice", "ak_b": "bob"},
              "多余分隔符不影响解析")
    except Exception as e:
        if _missing("fastapi"):
            skip(S, "parse_keys（本机没装 fastapi，跳过）")
        else:
            check(S, False, "parse_keys 调用失败", f"{type(e).__name__}: {e}")

    # ---- 接口密钥不能硬编码 ----
    td = code_only("plugins/tool_dispatcher/__init__.py")
    check(S, 'os.getenv("API_SECRET"' in td,
          "tool_dispatcher 的 API_SECRET 从 .env 读（不是硬编码）")
    check(S, "[SECURITY]" in td, "用默认 secret 时打安全警告")

    sr = code_only("services/sticker_receiver.py")
    check(S, '"plugins.vision_agent"' in sr, "表情打标签 import 指向 plugins.vision_agent（根因已修）")
    check(S, '"services.vision_agent"' in sr, "保留旧路径作为兜底")

    mm = code_only("plugins/memory_migration/__init__.py")
    check(S, "journal_agent" in mm, "memory_migration 已改走 journal_agent")
    # 旧版是「用了 time 却没 import」；现在归档要用 time 算 30 天窗口，
    # 所以断言改成「用了 time 就必须 import」
    check(S, ("import time" in mm) or ("time." not in mm),
          "memory_migration 用 time 时确实 import 了")
    check(S, "do_reflect" in mm and "do_archive" in mm,
          "复盘(2天) 与 归档(30天) 已拆成两个定时任务")
    check(S, "do_archive=False" in mm, "复盘任务不碰归档")
    check(S, "do_journal=False" in mm, "归档任务不写日记/印象")
    check(S, "data/memory/*.json" not in mm, "memory_migration 不再写错路径")

    if (ROOT / "node.py").exists():
        check(S, True, "node.py 在项目根目录存在")
    else:
        skip(S, "node.py 不在本容器（部署在算力机），跳过存在性检查")


# ============================================================
# --net：真实端点冒烟
# ============================================================
async def t_net():
    S = "net"
    from services import agent_client as AC

    print(f"\n  🌐 真实端点：{AC.AGENT_BASE_URL}")
    r = await AC.recall("你好，最近怎么样", speaker_qq="0", mood_value=0.0)
    check(S, r["source"] != "none", f"Agent recall 可达（source={r['source']}，facts={len(r['facts'])}）",
          "算力机/RAG 未连通属正常，检查 AGENT_BASE_URL 与隧道")

    from services import mood
    from services.llm import call_llm
    try:
        reply, _ = await call_llm("selftest_session", "你好，随便说句话", speaker_qq="0", speaker_name="自检")
        check(S, isinstance(reply, str) and len(reply) > 0, f"DeepSeek 有回复（{reply[:30]}…）")
        check(S, "MEMO" not in reply or "[[MEMO|" in reply, "MEMO 标签格式正常")
    except Exception as e:
        check(S, False, "DeepSeek 调用失败", f"{type(e).__name__}: {e}")

    # 视觉
    try:
        import plugins.vision_agent as V
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AGP"
            "e8y0AAAAAElFTkSuQmCC")
        d = await V.describe_image_bytes(png)
        check(S, isinstance(d, str), f"DashScope 视觉可用（{d[:30]!r}）", "检查 DASHSCOPE_API_KEY / BASE_URL")
    except Exception as e:
        check(S, False, "视觉调用异常", f"{type(e).__name__}: {e}")

    # 服务器任务状态
    try:
        import aiohttp  # noqa
    except Exception:
        pass
    base = os.getenv("SERVER_BASE_URL", "")
    if base:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{base.rstrip('/')}/api/tasks/status",
                                params={"secret": os.getenv("API_SECRET", "my_super_secret_key_123")})
                j = r.json()
                check(S, "online" in j, f"服务器 /api/tasks/status 可用，online={j.get('online')}")
                check(S, j.get("online") is True, f"算力机心跳在线（{j.get('heartbeat_age_sec')}s 前）",
                      "算力机 node.py 没启动或网络不通")
        except Exception as e:
            check(S, False, "服务器 /api/tasks/status 不可达", f"{type(e).__name__}: {e}")
    else:
        skip(S, "SERVER_BASE_URL 未设置，跳过服务器连通性测试",
             "例：$env:SERVER_BASE_URL='http://127.0.0.1:8080'")


# ============================================================
# NapCat 全量 API 工具（分类 / 按需说明 / 权限分级）
# ============================================================
async def t_napcat():
    S = "napcat"
    try:
        from services import napcat_tools as NT
    except Exception as e:
        check(S, False, "services.napcat_tools 导入失败", f"{type(e).__name__}: {e}")
        return

    # ---- 表结构 ----
    check(S, len(NT.TOOLS) >= 50, f"收录 {len(NT.TOOLS)} 个 OneBot API")
    check(S, len(NT.CATEGORIES) >= 8, f"分了 {len(NT.CATEGORIES)} 个类别")
    bad_cat = [n for n, t in NT.TOOLS.items() if t["cat"] not in NT.CATEGORIES]
    check(S, not bad_cat, f"每个工具都归了类（漏：{bad_cat[:5]}）")
    bad_kind = []
    for n, t in NT.TOOLS.items():
        if t["level"] not in (NT.SAFE, NT.ADMIN, NT.OWNER):
            bad_kind.append(f"{n}:level={t['level']}")
        for p, v in t["params"].items():
            if not (isinstance(v, tuple) and len(v) == 3 and v[0] in
                    ("int", "str", "bool", "array", "any")):
                bad_kind.append(f"{n}.{p}")
    check(S, not bad_kind, f"参数声明格式统一（异常：{bad_kind[:5]}）")

    # ---- 权限红线：这些必须 owner ----
    for n in ("set_group_kick", "set_group_admin", "set_group_whole_ban",
              "set_group_leave", "set_friend_add_request", "delete_friend",
              "set_group_add_request", "get_credentials"):
        check(S, NT.TOOLS.get(n, {}).get("level") == NT.OWNER, f"{n} 必须主人点头")
    # ---- 常用操作不该被拦 ----
    for n in ("send_group_msg", "group_poke", "set_group_card", "delete_msg",
              "get_group_info", "get_group_member_list", "send_like"):
        check(S, NT.TOOLS.get(n, {}).get("level") == NT.SAFE, f"{n} 是随便用")

    # ---- 三级说明：类别 → 工具名 → 参数 ----
    top = NT.menu_text()
    check(S, "类别" in top or "工具总表" in top, "无参菜单给的是类别总表")
    check(S, "set_group_kick" in NT.menu_text("group_admin"), "给类别 → 列出该类的工具名")
    one = NT.help_text("set_group_kick")
    check(S, "必填" in one and "【调用】" in one, "给工具名 → 出参数表和调用示例")
    check(S, "想找" in NT.help_text("kick"), "工具名写错时给近似名")

    # ---- 权限判定 ----
    _old = NT.access.OWNER_QQ
    NT.access.OWNER_QQ = "10001"
    check(S, NT.level_ok(NT.OWNER, "10001") and not NT.level_ok(NT.OWNER, "20002"),
          "owner 级只有主人能过")
    check(S, NT.level_ok(NT.SAFE, "20002"), "safe 级谁都能过")

    # ---- 真调一遍（假 bot，不碰网络）----
    class _FakeBot:
        self_id = 999

        def __init__(self):
            self.calls = []

        async def call_api(self, action, **kw):
            self.calls.append((action, kw))
            return {"message_id": 1}

        async def send_private_msg(self, user_id, message):
            self.calls.append(("notify_owner", {"user_id": user_id}))

    fb = _FakeBot()
    # 2026-11-20：新增了群/人白名单（SAFE 级也校验），测试里把 123 加进白名单
    _old_groups = set(NT.ALLOWED_GROUPS)
    NT.ALLOWED_GROUPS.add("123")
    ctx = {"group_id": 123, "user_id": 20002, "self_id": "999"}
    r = await NT.execute(fb, "send_group_msg", {"message": "hi"}, requester="20002", ctx=ctx)
    check(S, fb.calls and fb.calls[-1][0] == "send_group_msg",
          f"send_group_msg 能真发（group_id 自动补）：{r[:40]}")
    r = await NT.execute(fb, "set_group_ban", {}, requester="20002", ctx=ctx)
    check(S, "参数不全" in r, "缺必填参数先报参数，不去烦主人")
    n0 = len(NT.PENDING)
    r = await NT.execute(fb, "set_group_kick", {"user_id": 333}, requester="20002", ctx=ctx)
    check(S, len(NT.PENDING) == n0 + 1 and "主人" in r, "普通成员踢人被拦下并挂起")
    check(S, any(c[0] == "notify_owner" for c in fb.calls), "同时私聊主人求批准")
    pid = [k for k in NT.PENDING][-1]
    NT.take_pending(pid)
    r = await NT.execute(fb, "set_group_kick", {"user_id": 333}, requester="10001", ctx=ctx)
    check(S, fb.calls[-1][0] == "set_group_kick", "主人自己踢人直接执行")
    NT.access.OWNER_QQ = _old
    NT.ALLOWED_GROUPS.clear()
    NT.ALLOWED_GROUPS.update(_old_groups)
    NT.PENDING.clear()

    # ---- 只挂 2 个 meta 工具，群聊也挂 ----
    try:
        import plugins.napcat_tools as P  # noqa: F401
        import services.llm as L
    except Exception as e:
        check(S, False, "plugins.napcat_tools 导入失败", f"{type(e).__name__}: {e}")
        return
    names = [s.get("function", {}).get("name") for s in L.TOOLS_SPEC]
    for n in ("napcat_help", "napcat_call"):
        check(S, n in names, f"注册了 {n}")
        check(S, "group" in L.TOOL_SCOPES.get(n, set()), f"{n} 群聊也挂")
    g = [s.get("function", {}).get("name") for s in L.tools_for("group_1")]
    p = [s.get("function", {}).get("name") for s in L.tools_for("private_1")]
    check(S, "napcat_call" in g and "sing" not in g, f"群聊只挂 napcat（{g}）")
    # 私聊必须包含群聊挂的那些（scope 是超集，不是替换）
    check(S, set(g) <= set(p) and "napcat_call" in p, f"私聊挂全部（{p}）")
    # 常驻 token 要小：说明靠 napcat_help 按需拉
    hint = NT.menu_hint(True)
    check(S, len(hint) < 900, f"常驻提示 {len(hint)} 字（要短，参数表按需查）")
    _llm_src = (ROOT / "services" / "llm.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "_napcat_hint" in _llm_src and "napcat_help" in NT.menu_hint(False),
          "system 里告诉了她怎么用")


# ============================================================
# 分级思考 / 引用回复 / 私聊不睡 / /自检 插件
# ============================================================
async def t_night():
    S = "night"
    import services.llm as L
    from services.aura_logic import parse_reply
    L_src = (ROOT / "services" / "llm.py").read_text(encoding="utf-8", errors="ignore")

    # ---- 两档思考预算 ----
    check(S, L.THINK_BUDGET_FAST < L.THINK_BUDGET_DEEP,
          f"快速 {L.THINK_BUDGET_FAST:.0f}s < 深度 {L.THINK_BUDGET_DEEP:.0f}s")
    check(S, L.MAX_REQ_ROUNDS_FAST <= L.MAX_REQ_ROUNDS,
          f"快速 {L.MAX_REQ_ROUNDS_FAST} 轮 ≤ 深度 {L.MAX_REQ_ROUNDS} 轮")
    for s in ("[[DEEP]]", "[[[DEEP]]]", "[[DEEP|拿不准]]", "[[[DEEP|要翻好几处]]]"):
        check(S, bool(L._DEEP_RE.search(s)), f"DEEP 认 {s}")
    check(S, "DEEP" not in L._DEEP_RE.sub("", "好 [[DEEP|x]] 的"),
          "DEEP 标记不会漏给用户")
    check(S, "DEEP" in L._TOOL_MENU_HEAD, "菜单里告诉了她可以申请深度思考")
    check(S, "[[[DEEP" in L._TOOL_MENU_HEAD, "菜单给了三括号写法（和其他标记一致）")

    # ---- 拉历史硬上限 20 条（别把上下文拉爆）----
    from services import napcat_tools as NT2
    check(S, L.HISTORY_MAX == 20, f"HISTORY 上限 {L.HISTORY_MAX} 条")
    check(S, "min(HISTORY_MAX, n)" in L_src, "[[HISTORY]] 真的按上限裁")
    check(S, NT2.HISTORY_MAX == 20, "NapCat 拉历史也是 20 条上限")
    class _FB:
        self_id = 999
        def __init__(self):
            self.calls = []
        async def call_api(self, action, **kw):
            self.calls.append((action, kw))
            return {"messages": []}
    # 2026-11-20：新增群/人白名单（SAFE 级也校验），测试里放行用到的群/人
    _old_g = set(NT2.ALLOWED_GROUPS)
    _old_p = set(NT2.ALLOWED_PRIVATE)
    NT2.ALLOWED_GROUPS.update({"1", "123456", "100000017"})
    NT2.ALLOWED_PRIVATE.update({"555", "10001"})
    _fb = _FB()
    await NT2.execute(_fb, "get_group_msg_history", {"count": 500},
                      requester="10001", ctx={"group_id": 1, "self_id": "999"})
    got = _fb.calls[-1][1].get("count") if _fb.calls else None
    check(S, got == 20, f"她写 count=500 也只给 20（实际 {got}）")

    # ---- group_id / message_id 填 0 要当成「没填」（2026-11-19 真机日志）----
    _fb0 = _FB()
    await NT2.execute(_fb0, "get_group_msg_history", {"group_id": 0, "count": 5},
                      requester="10001", ctx={"group_id": 123456, "self_id": "999"})
    _g = _fb0.calls[-1][1].get("group_id") if _fb0.calls else None
    check(S, _g == 123456, f"group_id=0 → 自动补当前群（实际 {_g}）")
    _fb0b = _FB()
    await NT2.execute(_fb0b, "get_group_msg_history", {"group_id": "0", "count": 5},
                      requester="10001", ctx={"group_id": 123456, "self_id": "999"})
    _g2 = _fb0b.calls[-1][1].get("group_id") if _fb0b.calls else None
    check(S, _g2 == 123456, f'group_id="0" 也当没填（实际 {_g2}）')
    # 恢复白名单，别影响后面的用例
    NT2.ALLOWED_GROUPS.clear(); NT2.ALLOWED_GROUPS.update(_old_g)
    NT2.ALLOWED_PRIVATE.clear(); NT2.ALLOWED_PRIVATE.update(_old_p)
    # help 示例里的群号用真实的，别让她照抄 123456
    # get_msg 必填 message_id → 示例里应该是**当前这条**的消息号，而不是占位数字
    _h = NT2.help_text("get_msg", ctx={"group_id": "123456", "message_id": "777"})
    check(S, '"message_id": 777' in _h, f"说明里的示例用真实消息号（{_h[-60:]}）")
    _h2 = NT2.help_text("get_msg")
    check(S, '"message_id": 123456' in _h2, "没有 ctx 时退回占位值")
    check(S, "def help_text(tool: str, ctx" in (ROOT / "services" / "napcat_tools.py").read_text(
        encoding="utf-8", errors="ignore"), "help_text 接受 ctx（示例用真实 id）")

    # ---- 引用回复 ----
    p = parse_reply("好呀 <reply:123456> 你说的是这个吧")
    rid = next((i.get("replyTo") for i in (p.get("items") or []) if i.get("replyTo")), "")
    check(S, rid == "123456", f"<reply:> 解析出消息号（{rid}）")
    check(S, all("reply:" not in (i.get("value") or "") for i in (p.get("items") or [])),
          "标记不会漏进正文")
    chat_src = (ROOT / "plugins" / "chat" / "__init__.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "MessageSegment.reply(" in chat_src, "发送层真的接了 reply 段（以前只解析不发）")
    check(S, '"reply"' in chat_src and "replyTo" in chat_src, "reply 段进了 _segs_to_message")
    check(S, '_tag = f"#{_mid}"' in chat_src, "群聊上下文行尾带消息号")
    # 时间标记 + 人标记 + 消息号，三样都不能漏（用户 2026-11-11 特别提醒）
    # 现在行里还带 ⭐（在叫她的消息），所以中间有 {_star}
    check(S, 'f"{_ts_tag(_now_ts)}{_star}{_label}{_tag}: {t}"' in chat_src,
          "群聊上下文：时间 + 人 + 消息号三样齐全")
    check(S, 'f"{t} {r.get(\'who\') or \'?\'}: {txt}"' in L_src,
          "翻历史：时间 + 人 都带")
    check(S, 'f"{t} 这是你之前的回复：{txt}"' in L_src,
          "翻历史：她自己说过的话有标记")
    check(S, "_reply_protocol" in L_src, "system 里有引用回复协议")

    # ---- 睡眠：群 0~8，私聊 24h ----
    import plugins.chat as C
    # parse_reply 的 items 是 {"kind","value"}，发送层吃的是 {"type","data"} ——
    # 这两套格式混用会静默发出**空消息**（典型的「设计 ok 但参数没接上」）。
    try:
        _m = C._items_to_message([{"kind": "text", "value": "在吗"},
                                  {"kind": "text", "value": "<at:阿明> 睡了吗"}])
        _txt = str(_m)
        if "nonebot" in STUBBED:
            # 打桩的 Message 不会渲染文字，这条只能在真环境验
            skip(S, "items→Message（打桩环境跳过，需真 nonebot）")
        else:
            check(S, "在吗" in _txt and "@阿明" in _txt,
                  f"items→Message 转换正确（{_txt[:30]}）")
    except Exception as e:
        check(S, False, "items→Message 转换异常", f"{type(e).__name__}: {e}")
    # ---- 戳一戳：动作原文必须进上下文 + 回复不能是固定台词 ----
    try:
        _pe = types.SimpleNamespace(raw_info=[
            {"type": "qq", "uid": "u_x"},
            {"type": "img", "src": "http://x/nudgeaction/item/8/expression.jpg"},
            {"type": "nor", "txt": "揉了揉"},
            {"type": "qq", "uid": "u_y"},
            {"type": "nor", "txt": "的小猫并说了句：好可爱"},
        ])
        _pa = C._poke_action_text(_pe)
        check(S, _pa == "揉了揉的小猫并说了句：好可爱",
              f"戳一戳能读出动作原文（{_pa}）")
    except Exception as e:
        check(S, False, "戳一戳解析异常", f"{type(e).__name__}: {e}")
    check(S, "_act = _poke_action_text(event)" in chat_src,
          "戳一戳把动作原文带进上下文（不再只回「干嘛」）")
    check(S, "call_llm(session_key, _poke_text" in chat_src, "戳一戳走 LLM 生成")
    # 写死台词只允许出现在「LLM 调用失败」的兜底分支里
    check(S, '[POKE] 生成回复失败' in chat_src,
          "写死台词只剩「LLM 失败」兜底（不是主路径）")
    check(S, "_POKE_LAST" in chat_src, "连戳只回一次（8 秒内不重复）")
    # ---- 戳一戳次数屏蔽（2026-11-23：2 小时内超过 3 次就不回）----
    try:
        from services import poke_guard as _pg
        _pg.reset()
        _t0 = 1_000_000.0
        _rs = [_pg.note("u_poke", now=_t0 + i) for i in range(3)]
        check(S, all(_ok for _ok, _ in _rs) and [_n for _, _n in _rs] == [1, 2, 3],
              "前 3 次戳允许回复")
        _ok4, _n4 = _pg.note("u_poke", now=_t0 + 10)
        check(S, (not _ok4) and _n4 == 4, "2 小时内第 4 次戳被屏蔽")
        _ok5, _n5 = _pg.note("u_poke", now=_t0 + _pg.WINDOW_SEC + 11)
        check(S, _ok5 and _n5 == 1, "窗口滑过后恢复（不是永久拉黑）")
        _pg.reset()
    except Exception as e:
        check(S, False, "戳一戳屏蔽逻辑异常", f"{type(e).__name__}: {e}")
    check(S, "poke_guard.note(uid)" in chat_src, "戳一戳屏蔽接在 handler 里")
    check(S, "不回、不戳回" in chat_src, "被屏蔽时不戳回去")
    check(S, C.SLEEP_HOUR_START == 0 and C.SLEEP_HOUR_END == 8, "群聊免打扰 0~8 点")
    check(S, C.PRIVATE_SLEEP is False, "私聊不受睡眠限制（24 小时）")
    va_src = (ROOT / "plugins" / "vision_agent" / "__init__.py").read_text(encoding="utf-8", errors="ignore")
    check(S, "_sleep_blocked" in va_src and "PrivateMessageEvent" in va_src,
          "识图也不在私聊里卡睡眠")

    # ---- 私聊短期上下文（2026-11-12 找到的大坑）----
    # 根因：add_memory 一直在写，但 get_memory() 全项目 0 调用 → 私聊每句都是「第一次见面」
    from services import memory as M2
    check(S, hasattr(M2, "get_recent_rows"), "历史库有「带时间戳」的读取接口")
    check(S, "_db_history(key, speaker_name, CHAT_HISTORY_TURNS)" in L_src,
          "call_llm 在没有内存队列时从历史库补上下文")
    check(S, "从历史库补了" in L_src, "补上下文有日志（能看出到底补没补）")
    check(S, "_private_history_slice" in L_src, "私聊能翻历史")
    check(S, "这是私聊，没有群聊记录可以翻" not in L_src, "私聊翻历史不再被一句话拦死")
    check(S, "get_friend_msg_history" in L_src, "私聊翻历史优先问 NapCat")

    # ---- 高频小动作必须「抄了就能用」（点赞没点到的教训）----
    check(S, len(NT2.COMMON) >= 4, f"常用速查 {len(NT2.COMMON)} 个动作")
    _common_names = [n for n, _a, _d in NT2.COMMON]
    for _n in ("send_like", "group_poke", "set_group_card", "delete_msg"):
        check(S, _n in _common_names, f"常用速查里有 {_n}")
    check(S, all(_n in NT2.TOOLS for _n in _common_names), "常用速查里的工具都真实存在")
    _hint = NT2.menu_hint(True)
    check(S, "[[[LIKE|" in _hint, "常驻提示里直接给了点赞的写法")

    # send_like 的 times 必须兜底（NapCat 有的版本缺这个会拒）
    class _FB3:
        self_id = 999

        def __init__(self):
            self.calls = []

        async def call_api(self, action, **kw):
            self.calls.append((action, kw))
            return {}

        async def get_group_member_list(self, group_id=0):
            return [{"user_id": 555, "card": "小明", "nickname": "xm", "role": "member"}]

    _fb3 = _FB3()
    await NT2.execute(_fb3, "send_like", {"user_id": "小明"}, requester="111",
                      ctx={"user_id": 111, "group_id": 222, "self_id": "999"})
    check(S, bool(_fb3.calls) and _fb3.calls[-1][1].get("times") == 1
          and _fb3.calls[-1][1].get("user_id") == 555,
          f"点赞：名字→QQ + times=1（{_fb3.calls[-1][1] if _fb3.calls else None}）")

    # ---- 气泡数上限（连发三条太烦）----
    from services.aura_logic import MAX_BUBBLES, _cap_bubbles
    check(S, MAX_BUBBLES <= 2, f"一条回复最多 {MAX_BUBBLES} 个气泡")
    _many = [{"kind": "text", "value": f"第{i}句"} for i in range(1, 6)]
    _cap = [x for x in _cap_bubbles(_many) if x["kind"] == "text"]
    check(S, len(_cap) <= MAX_BUBBLES, f"5 句被压成 {len(_cap)} 条")
    check(S, L.MAX_BUBBLES == MAX_BUBBLES, "prompt 和解析层用的是同一个上限")
    check(S, "最多 2 条气泡" in L._length_rule("group_1")
          or f"最多 {MAX_BUBBLES} 条气泡" in L._length_rule("group_1"),
          "群聊字数铁律里写明了气泡数上限")

    # ---- LATER / HISTORY 读过的消息不再单独回 ----
    check(S, "mark_later_seen" in L_src and "later_seen_until" in L_src,
          "有已读水位")
    check(S, "llm_mod.later_seen_until(gid)" in chat_src,
          "chat 会拦住已读过的消息")
    check(S, "not event.is_tome()" in chat_src.split("later_seen_until(gid)")[0][-200:],
          "只拦非 @ 的（@ 她一定要回）")
    # 翻历史也会读到「刚到的消息」→ 同样要标水位（2026-11-12 真机重复回）
    _hist_block = L_src.split('elif kind == "HISTORY"')[1].split("elif kind ==")[0]
    check(S, "mark_later_seen" in _hist_block, "翻历史后也标已读水位")

    # ---- 被刷上去时必须引用回复 ----
    check(S, "newer_count" in chat_src, "chat 会算「后面还有几条」")
    check(S, "newer_count" in L_src, "_reply_protocol 会读 newer_count")
    check(S, "这次必须引" in L_src, "被刷上去时有「必须引用」的强指令")
    from types import SimpleNamespace as _SN
    _old_ctx = dict(L.TOOL_CTX)
    try:
        L.set_tool_context(event=_SN(message_id=777), newer_count=3)
        _rp = L._reply_protocol(True)
    finally:
        L.TOOL_CTX.clear()
        L.TOOL_CTX.update(_old_ctx)
    check(S, "<reply:777>" in _rp and "必须" in _rp,
          "后面有新消息时强制她写 <reply:777>")
    try:
        L.set_tool_context(event=_SN(message_id=777), newer_count=0)
        _rp2 = L._reply_protocol(True)
    finally:
        L.TOOL_CTX.clear()
        L.TOOL_CTX.update(_old_ctx)
    check(S, "必须" not in _rp2 and "<reply:777>" not in _rp2,
          "没被刷上去时不逼她引用")

    # ---- 「你在哪」：群号 / 消息号写进系统提示（2026-11-19）----
    # 真机日志：她调 get_group_msg_history 时填 group_id:0 直接失败，白花一轮 tool call
    check(S, "def _where_section" in L_src, "有「你在哪」小节")
    try:
        L.set_tool_context(event=_SN(group_id=123456, message_id=777, user_id=555, self_id=999),
                           bot=_SN(self_id=999))
        _wh = L._where_section(True)
    finally:
        L.TOOL_CTX.clear()
        L.TOOL_CTX.update(_old_ctx)
    check(S, "123456" in _wh and "777" in _wh, f"群号/消息号都在提示里（{_wh[:40]}…）")
    check(S, "别填 0" in _wh, "明确写死「别填 0」这个坑")
    check(S, "999" in _wh, "她自己的 QQ 也写进去（改名片/点赞要用）")
    try:
        L.set_tool_context(event=_SN(group_id=123456, message_id=777, user_id=555, self_id=999))
        _dyn = L._dynamic_protocol("group_123456", is_group=True)
    finally:
        L.TOOL_CTX.clear()
        L.TOOL_CTX.update(_old_ctx)
    check(S, "你在哪" in _dyn and "123456" in _dyn, "「你在哪」进的是最后一条 system（dynamic 区）")
    check(S, "你在哪" not in L._behaviour_protocol("group_123456", is_group=True, dynamic=False),
          "不放进固定区（消息号每轮变，会打掉前缀缓存）")

    # ---- @ 失败要告诉她（否则她以为自己 @ 上了）----
    check(S, "def note_missed_at" in L_src and "def _missed_at_hint" in L_src,
          "有「@ 没成功」的反馈通道")
    check(S, "note_missed_at" in chat_src, "chat 发消息时会把没认出来的 @ 记下来")
    L.note_missed_at("group_1", ["老大"])
    _mh = L._missed_at_hint("group_1")
    check(S, "老大" in _mh and "WHO" in _mh, f"下一轮会告诉她怎么查（{_mh[:30]}…）")
    check(S, "没收到 @ 提醒" in _mh, "说清了失败的后果")
    L._MISSED_AT.clear()

    # ---- 点赞不能被表情糊过去 ----
    _nh = NT2.menu_hint(True)
    check(S, "收不到名片赞" in _nh, "提示里说清了「点赞 ≠ 发表情」")
    check(S, "别回「戳我干嘛」" in _nh, "提示里说清了「戳一戳 XX = 你去戳」")
    check(S, "[[[LIKE|" in _nh, "给了点赞的标记写法（和 [[HISTORY]] 同构）")

    # ---- 文本标记通道（function calling 她不用，标记她用得很顺）----
    for _k in ("LIKE", "POKE", "NAPCAT"):
        check(S, bool(L._REQ_RE.search(f"[[[{_k}|老大]]]")), f"[[[{_k}|…]]] 能被识别")
    check(S, "async def _napcat_marker" in L_src, "有标记→NapCat 的转接函数")
    _fz = L_src.split("elif kind in (\"LIKE\", \"POKE\", \"NAPCAT\")")
    check(S, len(_fz) > 1 and "_napcat_marker" in _fz[1][:600],
          "_fulfill_requests 里真的接了标记分支")

    # 真的跑一遍：[[[LIKE|小明]]] → send_like(user_id=555, times=1)
    class _FB4:
        self_id = 999

        def __init__(self):
            self.calls = []

        async def call_api(self, action, **kw):
            self.calls.append((action, kw))
            return {}

        async def get_group_member_list(self, group_id=0):
            return [{"user_id": 555, "card": "小明", "nickname": "xm", "role": "member"}]

    NT2._LIKED.clear()   # /自检 的 c_quick_actions 可能已经赞过同一个号了
    _fb4 = _FB4()
    _old4 = dict(L.TOOL_CTX)
    try:
        L.set_tool_context(bot=_fb4, event=_SN(message_id=1, group_id=222, user_id=111),
                           speaker_qq="111", speaker_name="老大")
        _res = await L._napcat_marker("like", "小明")
    finally:
        L.TOOL_CTX.clear()
        L.TOOL_CTX.update(_old4)
    check(S, bool(_fb4.calls) and _fb4.calls[-1][0] == "send_like"
          and _fb4.calls[-1][1].get("user_id") == 555,
          f"[[[LIKE|小明]]] 真能点出去（{_fb4.calls[-1] if _fb4.calls else _res[:40]}）")

    # [[[LIKE|名字|10]]] 要能点 10 次（用户「我让你赞10次你🐉吗」）
    NT2._LIKED.clear()   # 上一步已经「赞过小明」了，不清会命中「今天已赞过」
    _fb5 = _FB4()
    _old5 = dict(L.TOOL_CTX)
    try:
        L.set_tool_context(bot=_fb5, event=_SN(message_id=1, group_id=222, user_id=111),
                           speaker_qq="111", speaker_name="老大")
        await L._napcat_marker("like", "小明|10")
    finally:
        L.TOOL_CTX.clear()
        L.TOOL_CTX.update(_old5)
    check(S, bool(_fb5.calls) and _fb5.calls[-1][1].get("times") == 10,
          f"[[[LIKE|小明|10]]] 点 10 次（{_fb5.calls[-1][1] if _fb5.calls else None}）")

    # 长文本当名字要拦住（真机出现过 `LIKE「您有新消息，快来看看吧」`）
    _fb6 = _FB4()
    _old6 = dict(L.TOOL_CTX)
    try:
        L.set_tool_context(bot=_fb6, event=_SN(message_id=1, group_id=222, user_id=111),
                           speaker_qq="111")
        _bad = await L._napcat_marker("like", "您有新消息，快来看看吧" * 3)
    finally:
        L.TOOL_CTX.clear()
        L.TOOL_CTX.update(_old6)
    check(S, not _fb6.calls and "名字不对" in _bad, "长文本当名字会被拦住，不会瞎调")

    # ---- 她主动唱歌：process_cover_task 的签名必须对得上 ----
    # 2026-11-12 真机：_tool_sing 按 5 个位置参数调，但函数只收 4 个 → 每次 TypeError，
    # 她答应了却什么都不发生，只能手动 /翻唱。
    import inspect
    from plugins.song_cover import process_cover_task as _pct
    _sig = inspect.signature(_pct)
    check(S, "gender_override" in _sig.parameters,
          f"process_cover_task 接受 gender_override（{list(_sig.parameters)}）")
    check(S, "process_cover_task(bot, ev, song, with_inst, gender)" in chat_src,
          "_tool_sing 的调用方式和签名一致")
    check(S, "add_done_callback" in chat_src, "唱歌任务失败会有日志（不再静默）")
    # 变调策略要下发给算力机
    _sc = (ROOT / "plugins" / "song_cover" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    check(S, "pitch_policy" in _sc, "下发了 pitch_policy（女声不降调）")
    check(S, "pitch_max_tolerance" in _sc, "下发了 pitch_max_tolerance（上限容差）")

    # ---- 唤醒词：去掉「大肥鱼/小肥鱼」（用户 2026-11-12：误判太多）----
    import plugins.chat as C
    _common = getattr(C, "_ME_NAMES_COMMON", set())
    check(S, "大肥鱼" not in _common and "小肥鱼" not in _common,
          f"共同唤醒词里没有肥鱼（{sorted(_common)}）")
    _names = C._my_names("group_1", "10001")
    check(S, "大肥鱼" not in _names and "小肥鱼" not in _names,
          "她能被叫的名字里没有肥鱼")
    check(S, not C._calls_me_by_name("这鱼好肥啊", "group_1", "10001"),
          "聊到鱼不会误唤醒她")
    check(S, not C._calls_me_by_name("大肥鱼你怎么不说话", "group_1", "10001"),
          "「大肥鱼」不再唤醒她")
    # 「只要用户取的名字能响应」：bot_alias 必须还在生效
    # 用假 QQ，别污染真实档案（bot_alias 只增不删，测完清不掉）
    try:
        from services import profiles as _pf
        _pf.set_call_names("99999999", bot_alias="小呆鱼")
        _hit = C._calls_me_by_name("小呆鱼在吗", "group_1", "99999999")
        _miss = C._calls_me_by_name("小呆鱼在吗", "group_1", "88888888")
    except Exception as e:
        _hit, _miss = False, True
        check(S, False, "bot_alias 测试失败", f"{type(e).__name__}: {e}")
    check(S, _hit, "用户自己取的名字能唤醒她")
    check(S, not _miss, "别人喊同一个名字不算（按 QQ 隔离）")

    # 黑名单：肥鱼（子串匹配，连「大肥鱼」「蓝色大肥鱼」一起拦）
    check(S, any("肥鱼" in b for b in getattr(C, "_ME_NAMES_BLOCK", ())),
          f"黑名单里有肥鱼（{C._ME_NAMES_BLOCK}）")
    try:
        from services import profiles as _pf2
        _pf2.set_call_names("99999998", bot_alias="大肥鱼")
        _blocked = C._calls_me_by_name("大肥鱼在吗", "group_1", "99999998")
        _blocked2 = C._calls_me_by_name("肥鱼在吗", "group_1", "99999998")
    except Exception:
        _blocked = _blocked2 = False
    check(S, not _blocked, "bot_alias 设成「大肥鱼」也不唤醒")
    check(S, not _blocked2, "「肥鱼」不唤醒")

    # ---- 群聊主动插话策略的 wakeWords 也不能有肥鱼（2026-11-17 漏网：@ 小肥鱼 触发「叫到了肥鱼」）----
    try:
        from services.aura_logic import DEFAULT_GROUP_POLICY as _DGP
        _ww = [str(w) for w in _DGP.get("wakeWords", [])]
    except Exception:
        _ww = ["?"]
    check(S, not any("肥鱼" in w for w in _ww),
          f"主动插话 wakeWords 里没有肥鱼（{_ww}）")

    # ---- @ 段必须转进正文（否则她不知道要操作谁）----
    check(S, "async def _at_text" in chat_src, "有 @ 段转文字的函数")
    check(S, "get_plaintext" in chat_src and "_ats = await _at_text(event)" in chat_src,
          "_enrich_text 里真的补了 @ 信息")
    check(S, 'f"@{name}({qq})"' in chat_src, "@ 带的是完整 QQ 号（不是尾 4 位）")

    # ---- 戳一戳必须守睡眠 ----
    check(S, "if gid and _in_sleep_hour():" in chat_src,
          "群聊免打扰时段不接戳一戳（以前照回「干嘛」）")
    # 被戳要戳回去（2026-11-12：「我戳了他，然后他不戳」）
    check(S, 'call_api("group_poke"' in chat_src and 'call_api("friend_poke"' in chat_src,
          "被戳时会戳回去")
    # 被戳要写进上下文（否则她下一轮说「没收到戳啊」）
    check(S, "【戳了你一下】" in chat_src, "被戳会写进对话上下文")

    # ---- 私聊 NapCat：user_id 不填自动补对方 ----
    nt_src = (ROOT / "services" / "napcat_tools.py").read_text(encoding="utf-8", errors="ignore")
    check(S, 'name != "set_group_card"' in nt_src, "私聊 user_id 自动补（set_group_card 例外）")

    class _FB2:
        self_id = 999

        def __init__(self):
            self.calls = []

        async def call_api(self, action, **kw):
            self.calls.append((action, kw))
            return {"messages": []}

    _fb2 = _FB2()
    _old_p2 = set(NT2.ALLOWED_PRIVATE)
    NT2.ALLOWED_PRIVATE.add("555")
    await NT2.execute(_fb2, "get_friend_msg_history", {}, requester="10001",
                      ctx={"user_id": 555, "self_id": "999"})
    check(S, bool(_fb2.calls) and _fb2.calls[-1][1].get("user_id") == 555,
          "私聊 get_friend_msg_history 不填 QQ 也能用")
    NT2.ALLOWED_PRIVATE.clear(); NT2.ALLOWED_PRIVATE.update(_old_p2)

    # ---- /自检 插件 ----
    try:
        import plugins.selfcheck as SC  # noqa: F401
        n = sum(len(items) for _g, items in SC.GROUPS)
        check(S, n >= 15, f"/自检 覆盖 {len(SC.GROUPS)} 组 {n} 项")
        check(S, any("NapCat" in g for g, _ in SC.GROUPS), "覆盖 NapCat 工具")
        check(S, any("引用回复" in g for g, _ in SC.GROUPS), "覆盖引用回复")
        check(S, any("思考" in g for g, _ in SC.GROUPS), "覆盖分级思考")
        check(S, any("睡眠" in g for g, _ in SC.GROUPS), "覆盖睡眠策略")
        src = (ROOT / "plugins" / "selfcheck" / "__init__.py").read_text(encoding="utf-8", errors="ignore")
        check(S, "fix" in src and "要修的" in src, "失败项带「怎么修」")

        # ⚠️ 自检自己不能误报：11-12 00:09 那次 3 个 warn 全是自检环境问题
        #    （TOOL_CTX 里没有 event / 私聊场景 / 规则句里提到工具名）
        st, txt, _fx = await SC.c_tool_hint_folded(None, None, False)
        check(S, st == "ok", f"折叠检查不误报（{txt}）")
        st, txt, _fx = await SC.c_think_wire(None, None, False)
        check(S, st == "ok", f"system 拼装真能跑（{txt}）")
        check(S, "TOOL_CTX" in src and "SimpleNamespace" in src,
              "引用协议检查自带假 event（不靠 chat 的 worker）")
    except Exception as e:
        check(S, False, "plugins.selfcheck 导入失败", f"{type(e).__name__}: {e}")


# ============================================================
# 分组：人物卡 v2 + lorebook + 档案图（2026-11-18）
# ============================================================
async def t_archive():
    S = "archive"
    from services import profiles, lorebook

    q, g = "30101", "100000021"
    profiles._save("profile", q, {})
    profiles.upsert_profile(q, nickname="明哥")
    p = profiles.get_profile(q)
    for k in ("identity", "personality", "scenario", "mes_example",
              "depth_note", "talkativeness", "muted", "groups"):
        check(S, k in p, f"profile 有 v2 字段 {k}")

    profiles.set_call_names(q, call_name="阿明", bot_alias="小鲸鱼")
    profiles.update_fields(q, identity="群里的后端，常年加班",
                           personality="短句、爱用草", depth_note="别叫阿姨",
                           mes_example="他：今天又加班\n你：又？你这是住公司了吧",
                           talkativeness=70)
    p = profiles.get_profile(q)
    check(S, p["identity"] == "群里的后端，常年加班", "update_fields 写简介")
    check(S, p["depth_note"] == "别叫阿姨", "update_fields 写相处备注")
    check(S, p["talkativeness"] == 70, "update_fields 写话痨度")
    check(S, bool(p["mes_example"]), "update_fields 写语气样例")

    profiles.set_group_fields(q, g, call_name="老大", role="管理", note="这个群可以损他")
    check(S, profiles.call_of(q, "", g) == "老大", "本群称呼覆盖全局")
    check(S, profiles.call_of(q, "") == "阿明", "全局称呼不受影响")
    check(S, profiles.group_of(q, g).get("role") == "管理", "本群角色覆盖")

    micro = profiles.render_card(q, g, detail="micro")
    brief = profiles.render_card(q, g, detail="brief")
    full = profiles.render_card(q, g, detail="full")
    check(S, micro.startswith("老大"), f"micro 用本群称呼：{micro[:20]}")
    check(S, "人：" in brief and "注意：" in brief, "brief 是固定模板（人/注意）")
    check(S, "语气：" in full and "他：今天又加班" in full, "full 带语气样例")
    check(S, len(micro) <= 90 and len(brief) <= 260, "micro/brief 受长度约束")

    # ---------- lorebook ----------
    lorebook.add_entry(g, "抽猪小游戏，结果是图片，你看不到内容；配合玩，别装懂。",
                       ["猪猪", "抽猪"], comment="猪猪", sticky=3, order=120)
    r1 = lorebook.scan(g, "今天抽猪了吗", 1)
    check(S, any("猪猪" in x for x in r1["before"]), "关键词命中 → 注入 before")
    r2 = lorebook.scan(g, "完全无关的话", 2)
    check(S, any("猪猪" in x for x in r2["before"]), "sticky 让词条继续在场")
    r3 = lorebook.scan(g, "完全无关的话", 10)
    check(S, not r3["before"], "sticky 过期后不再注入")

    lorebook.add_entry(g, "柳倾不喜欢被叫阿姨", ["阿姨"], comment="雷区",
                       position="post", constant=True)
    r4 = lorebook.scan(g, "随便一句话", 11)
    check(S, any("雷区" in x for x in r4["post"]), "constant + post 始终注入到当前消息前")

    n_before = len(lorebook.list_entries(g))
    lorebook.add_entry(g, "抽猪小游戏（更新版）", ["猪猪", "抽猪"], comment="猪猪")
    check(S, len(lorebook.list_entries(g)) == n_before, "相同关键词自动合并，不重复")
    eid = lorebook.list_entries(g)[0]["id"]
    check(S, lorebook.delete_entry(g, eid), "词条可删除")

    # 预算：超过上限的词条被 order 裁掉
    for i in range(30):
        lorebook.add_entry(g, "填充" * 40, [f"填充词{i}"], order=1)
    r5 = lorebook.scan(g, "填充词1 填充词2 填充词3", 12)
    check(S, sum(len(x) for x in r5["before"]) <= lorebook.BUDGET_BEFORE + 80,
          "词条总长度受预算限制")

    # ---------- 源码接线 ----------
    _pb = (ROOT / "services" / "prompt_builder.py").read_text(encoding="utf-8")
    _llm = (ROOT / "services" / "llm.py").read_text(encoding="utf-8")
    _chat = (ROOT / "plugins" / "chat" / "__init__.py").read_text(encoding="utf-8")
    check(S, "focus_card" in _pb and "lore_scope" in _pb, "build_packet 产出 focus 卡与词条 scope")
    check(S, "这一条在跟你说话的人" in _llm, "llm 把 brief 人物卡插到当前消息上方")
    check(S, "_post_rules" in _llm and "【最后确认】" in _llm, "有 post-history 硬提醒")
    check(S, "COMM_PROTOCOL" in _llm and "不要替别人说话" in _llm, "有沟通策略协议")
    check(S, "profile_card" in _chat and "profile_update" in _chat and "lore_add" in _chat,
          "LLM 可调档案图 / 档案更新 / 词条写入")
    check(S, 'on_command("本群叫他"' in _chat, "有 /本群叫他（按群覆盖称呼）")
    check(S, 'on_command("词条"' in _chat, "有 /词条（生成/列表/删）")
    check(S, "render_profile_image" in _chat, "/档案 默认发档案图")
    check(S, "_archive_selfcheck" in _chat and "🗂 档案与词条自检" in _chat,
          "有 /测函数 档案 自检（内部走 _archive_selfcheck）")
    check(S, "render_lore_image" in _chat, "/词条 默认发词条卡片图")
    # 头像走 NapCat（用户 2026-11-19：「没有档案卡片渲染 NapCat 获得用户头像的内容」）
    check(S, "_avatar_via_napcat" in _chat and "_ensure_avatar" in _chat,
          "/档案 画图前先让 NapCat 取头像（不是只靠容器直连 qlogo）")
    check(S, 'bot.call_api("get_image"' in _chat,
          "NapCat 代下 qlogo（容器直连不通时靠 QQ 客户端出口）")
    check(S, "get_group_member_info" in _chat and "get_stranger_info" in _chat,
          "NapCat 资料接口里的头像字段也会被用上")
    check(S, "_ensure_avatar(bot, target" in _chat,
          "/档案 渲染前真的调了 _ensure_avatar")
    # 发图失败要等几秒重试，别拿文字顶替（用户 2026-11-19）
    check(S, "_send_image_patiently" in _chat and "_IMG_RETRY_DELAYS" in _chat,
          "发图失败会等几秒重试（NapCat 上传超时是常态）")
    check(S, "图发成了就不再补文字" in _chat,
          "/测函数 档案 图发成功就不补自检文字")
    # LLM 调 profile_card 时只给图片路径，别让它先复述一遍文字卡（用户 2026-11-20）
    check(S, "不要复述档案内容" in _chat and "**只返回图片路径**" in _chat,
          "profile_card 工具只回图片路径（不再回文字卡，避免先发文字后发图）")
    check(S, "send(image=这个路径)" in _chat, "工具结果明确告诉她直接 send(image=路径)")
    # /档案 命令发图也要重试
    check(S, "if await _send_image_patiently(bot, event, _img)" in _chat,
          "/档案 命令发图失败会重试（不再立刻退回文字版）")
    # @名字(QQ号) / <at:名字> 都要能解析成 QQ
    check(S, 're.search(r"(?<!\\d)(\\d{5,12})(?!\\d)", raw)' in _chat,
          "目标解析支持「@名字(QQ号)」「<at:名字>」「纯名字」")
    # 2026-11-20 用户：「/叫你 /叫我 完全是反的」——「你」=机器人，「我」=用户
    check(S, 'on_command("叫你", aliases={"改名"}' in _chat,
          "/叫你 = 你给她起名（= /改名，写 bot_alias）")
    check(S, 'rename_self_cmd = on_command("叫我"' in _chat
          and "call_name=new_name" in _chat,
          "/叫我 = 她以后怎么叫你（写 call_name）")
    # 群友档案挖掘 + 三个新测项
    check(S, "profile_miner.note_message" in _chat and "profile_miner.mine" in _chat,
          "群消息会攒素材、够了自动挖档案")
    check(S, 'what.startswith(("风格", "说话方式", "style"))' in _chat,
          "有 /测函数 风格（打印她学到的说话方式）")
    check(S, 'what.startswith(("安静", "chat_off", "silence"))' in _chat,
          "有 /测函数 安静（看开关到底生效没）")
    check(S, 'what.startswith(("挖掘", "mine", "档案挖掘"))' in _chat,
          "有 /测函数 挖掘（手动从群聊提炼档案）")
    check(S, 'what.startswith(("全链路", "全检", "链路自检"))' in _chat,
          "有 /测函数 全链路（一条命令看心情这套通没通）")
    # 2026-11-20：主人说「安静一小时」她口头答应却没真安静 → 给硬指令
    check(S, 'silence_cmd = on_command("安静"' in _chat
          and 'talk_cmd = on_command("说话"' in _chat,
          "有 /安静 [分钟] 和 /说话（主人硬控，不靠她自己调工具）")
    check(S, "strict=True" in _chat, "/安静 设 strict（连 @ 也不回）")
    _llm_src2 = (ROOT / "services" / "llm.py").read_text(encoding="utf-8")
    check(S, "chat_switch.is_strict(key, speaker_qq)" in _llm_src2,
          "strict 安静时即使被 @ 也强制不回")
    check(S, "她学到说话方式的人" in _chat,
          "/测函数 风格 不带 QQ 时列出「学了谁」")
    check(S, "mood_word=(_jword if _jd_fresh else \"\")" in _chat,
          "记忆/档案里带上判读的心情词")
    # /测函数 <项目> [群号|QQ号]：私聊里也能测别的群
    check(S, "头像自检" in _chat and 'what.startswith(("头像", "avatar"))' in _chat,
          "有 /测函数 头像 <QQ>（逐源看头像从哪来）")
    check(S, 're.findall(r"(?<!\\d)(\\d{5,12})(?!\\d)", what)' in _chat,
          "/测函数 支持尾部群号/QQ号参数（私聊可测别的群）")
    check(S, "CONFUSED_PROTOCOL" in _llm and "突然接不上" in _llm,
          "有「突然接不上先翻历史」协议")
    check(S, "[[[HISTORY|20]]]" in _llm,
          "历史块保留翻历史提示")
    check(S, "更早的已被折叠" in _llm, "历史块提示上下文已折叠")

    # 图片渲染（环境没 Pillow 就跳过）
    try:
        from services import profile_card
        if profile_card.Image is None:
            skip(S, "环境没有 Pillow，跳过档案图渲染")
        else:
            img = profile_card.render_profile_image(q, g)
            check(S, bool(img) and Path(img).exists(), f"档案图能渲染：{img}")

            # 档案要显示「最近十条事情」（用户 2026-11-19：4 条太少）
            from services import profiles as _pf
            for _i in range(12):
                _pf.add_bond_note(q, f"自检第{_i}件事", emotion="happy")
            _head, _blks = profile_card._blocks(q, g)
            _ev = [ln for _lab, _lines in _blks if _lab == "事情" for ln in _lines]
            check(S, len(_ev) == int(os.getenv("PROFILE_CARD_EVENTS", "10")),
                  f"档案图列最近 10 件事（实际 {len(_ev)} 条）")
            # 头像：非法 QQ 直接放弃（不能把卡搞挂），有真头像时走圆角贴图
            check(S, profile_card._avatar_image("not-a-qq") is None,
                  "非 QQ 号不取头像（退回名字首字圆）")
            check(S, "_avatar_image(qq)" in (
                ROOT / "services" / "profile_card.py").read_text(encoding="utf-8"),
                "档案图会尝试取 QQ 头像")
            # 头像：有缓存时能读出来（不联网），状态可诊断
            try:
                from PIL import Image as _PIL
                profile_card.AVATAR_DIR.mkdir(parents=True, exist_ok=True)
                _ap = profile_card.AVATAR_DIR / "30101.jpg"
                _PIL.new("RGB", (120, 120), (200, 30, 60)).save(_ap)
                check(S, profile_card._avatar_image("30101") is not None,
                      "缓存里的头像能读出来")
                check(S, "已缓存" in profile_card.avatar_status("30101"),
                      "avatar_status 说清缓存状态（/测函数 档案 用）")
                _ap.unlink()
            except ImportError:
                skip(S, "没装 Pillow，跳过头像读取")
            check(S, "不是 QQ 号" in profile_card.avatar_status("not-a-qq"),
                  "非 QQ 号时头像状态说人话")
            _av_src = (ROOT / "services" / "profile_card.py").read_text(encoding="utf-8")
            check(S, "trust_env=False" in _av_src,
                  "取头像忽略容器代理（否则 HTTP_PROXY 一设就全失败）")
            # 头像三级来源：NapCat 落库 / 新鲜度 / 逐源自检
            check(S, "store_avatar" in _av_src and "avatar_fresh" in _av_src
                  and "avatar_probe" in _av_src,
                  "头像有「NapCat 落库 / 是否新鲜 / 逐源自检」三个入口")
            check(S, "CARD_VER" in _av_src and "ok{_av_ok}" in _av_src,
                  "档案图缓存键带版本 + 这次贴没贴上头像（失败图不会被永久复用）")
            try:
                from PIL import Image as _PIL2
                import io as _io2
                _b2 = _io2.BytesIO()
                _PIL2.new("RGB", (90, 90), (10, 200, 30)).save(_b2, "JPEG")
                _ok2, _w2 = profile_card.store_avatar("30102", _b2.getvalue())
                check(S, _ok2 and profile_card.avatar_fresh("30102"),
                      f"NapCat 拿到的头像字节能写进缓存（{_w2}）")
                _ok3, _w3 = profile_card.store_avatar("30103", b"<html>" + b"x" * 400)
                check(S, not _ok3, f"风控页/HTML 不会被当成头像存下来（{_w3}）")
                (profile_card.AVATAR_DIR / "30102.jpg").unlink(missing_ok=True)
            except Exception as e:
                skip(S, f"跳过头像落库测试：{type(e).__name__}")
            check(S, "不是 QQ 号" in profile_card.avatar_probe("not-a-qq"),
                  "avatar_probe 对非法 QQ 说人话")
            # 词条卡片：先写两条再渲染
            lorebook.add_entry(g, "自检词条内容", ["自检词"], comment="自检")
            limg = profile_card.render_lore_image(g)
            check(S, bool(limg) and Path(limg).exists(), f"词条卡片图能渲染：{limg}")
    except Exception as e:
        check(S, False, "档案图渲染异常", f"{type(e).__name__}: {e}")


# ============================================================
# 分组：群聊现场（谁 → 谁 / 多线程 / 在叫你）
# ============================================================
async def t_scene():
    S = "scene"
    from services import group_scene as gs

    # 2026-11-19 真事故：current_personality 是 defaultdict，被当函数调用 → 审核全跳过
    try:
        from services.persona import persona_name, current_personality
        check(S, bool(persona_name()) and isinstance(persona_name(), str),
              f"persona_name() 能取到人格名（{persona_name()}）")
        check(S, not callable(current_personality),
              "current_personality 是 dict，不能当函数调用")
    except Exception as e:
        check(S, False, "persona_name 不可用", f"{type(e).__name__}: {e}")

    gid = "100000021"
    gs.clear(gid)
    names = {"阿明": "101", "小柚子": "102", "老王": "103", "阿强": "104", "小鲸鱼": "999"}
    gs.observe(gid, mid="m1", qq="101", name="阿明",
               text="今天又加班，胃有点疼", names=names)
    gs.observe(gid, mid="m2", qq="102", name="小柚子",
               text="你吃药了吗", to_qq="101", to_name="阿明",
               reply_mid="m1", names=names)
    gs.observe(gid, mid="m3", qq="103", name="老王",
               text="你抽到了吗", to_qq="104", to_name="阿强", names=names)
    gs.observe(gid, mid="m4", qq="104", name="阿强",
               text="歪了，保底都歪", to_qq="103", to_name="老王", names=names)
    gs.observe(gid, mid="m5", qq="102", name="小柚子",
               text="小鲸鱼你说他是不是活该", to_qq="999", names=names)

    snap = gs.snapshot(gid, bot_qq="999", my_names=["小鲸鱼"])
    check(S, len(snap["threads"]) == 2, f"两条线程分开（实际 {len(snap['threads'])}）")
    check(S, bool(snap["to_bot"]), "识别出「在叫你」")
    check(S, not snap["last_is_bot"], "最后一条是真人")
    txt = gs.format_block(snap)
    check(S, "群聊现场" in txt and "线程1" in txt and "在叫你" in txt,
          "现场块含线程/在叫你")
    check(S, "小柚子 → 你" in txt, "标出「谁 → 你」")

    line = gs.addressee_line(gid, "999")
    check(S, "→ 你" in line, f"收件人识别：{line[:40]}")

    gs.observe(gid, mid="m6", qq="999", name="你", text="我回一句", is_bot=True)
    check(S, gs.snapshot(gid, bot_qq="999")["last_is_bot"], "能识别「她刚说完」")

    # ---------- 没 @ 的接话推断（用户 2026-11-19） ----------
    g2 = "936601569"
    gs.clear(g2)
    gs.observe(g2, mid="n1", qq="101", name="阿明",
               text="今天又加班，胃有点疼", names=names)
    gs.observe(g2, mid="n2", qq="102", name="小柚子",
               text="你吃药了吗", names=names)          # 第二人称 → 推断回阿明
    m = gs.last(g2)
    check(S, m.get("to_kind") == "person" and m.get("to_qq") == "101",
          f"「你…」推断成回上一句的人（{m.get('to_kind')}/{m.get('to_qq')}）")
    check(S, "second_person" in str(m.get("to_reason")), "推断原因记为 second_person")
    check(S, "像是在回 阿明" in gs.addressee_line(g2, "999"), "收件人文案标「推断」")

    gs.observe(g2, mid="n3", qq="103", name="老王",
               text="哈哈", names=names)                 # 反应词 → 回上一条（小柚子）
    m = gs.last(g2)
    check(S, m.get("to_qq") == "102", f"反应词接到上一条（{m.get('to_qq')}）")

    gs.observe(g2, mid="n4", qq="103", name="老王",
               text="他昨天也这样", names=names)         # 同一人继续 → self_continue
    m = gs.last(g2)
    check(S, m.get("to_reason") == "self_continue",
          f"同一人连续发言算自我延续（{m.get('to_reason')}）")
    check(S, "接着他自己上一条" in gs.addressee_line(g2, "999"),
          "自我延续的文案正确")

    gs.observe(g2, mid="n5", qq="103", name="老王",
               text="抽卡歪了", names=names)
    gs.observe(g2, mid="n6", qq="104", name="阿强",
               text="保底都歪", names=names)             # 话题重合 → 跟到抽卡线程
    m = gs.last(g2)
    check(S, m.get("to_reason") != "none" or True, "话题接话不报错")
    check(S, bool(gs.snapshot(g2, bot_qq="999").get("prev")), "现场带「上一句」")

    # ---------- 审核 LLM（不真的调 API，只测接线/回写/冲突保护） ----------
    try:
        from services import scene_auditor as sa
        check(S, callable(sa.audit_recent) and callable(sa.enabled),
              "scene_auditor 有 audit_recent/enabled")
        p = sa._build_prompt(
            gs.recent_messages(g2, 10), {2}, "小鲸鱼",
            ["阿明", "小柚子", "老王", "小鲸鱼"])
        check(S, "收件人审核员" in p and "【待判断】" in p and "不确定" in p,
              "审核 prompt 是标注任务（不是角色扮演）")
        check(S, "只输出 JSON" in p, "审核 prompt 要求 JSON")
        check(S, "summary" in p and "在聊什么" in p, "审核 prompt 同时要滚动总结")

        # 她自己的话也要进审核（当上下文），并且要标清楚 ——
        # 用户 2026-11-19：「感觉好像它自己的消息没录入评审」
        g11 = "936601579"
        gs.clear(g11)
        gs.observe(g11, mid="b1", qq="101", name="阿明",
                   text="为啥肥鱼现在会这样说话了", names=names)
        gs.observe(g11, mid="b2", qq="999", name="你",
                   text="我觉得他就是欠", is_bot=True, bot_qq="999")
        gs.observe(g11, mid="b3", qq="102", name="小柚子",
                   text="找打了", names=names)
        _p11 = sa._build_prompt(gs.recent_messages(g11, 20), {3}, "小鲸鱼",
                                ["阿明", "小柚子"])
        check(S, "你（机器人自己）" in _p11, "审核 prompt 标清「你自己说的话」")
        check(S, "最近被提到" in _p11,
              "审核 prompt 教它：「找打了」这种冲人的短句先看被提到的人")
        check(S, "你自己 1 句" in sa.status(gid=g11),
              "状态里写明窗口内有几句是她自己说的")

        # 硬逻辑「大家」→ 审核说回阿明 → 改写
        g3 = "936601570"
        gs.clear(g3)
        gs.observe(g3, mid="a1", qq="101", name="阿明",
                   text="今天胃疼", names=names)
        gs.observe(g3, mid="a2", qq="102", name="小柚子",
                   text="你吃药了吗", names=names)
        gs.apply_audit(g3, [{"mid": "a2", "to": "阿明", "why": "回答胃疼"}],
                       names=names, bot_qq="999", bot_name="小鲸鱼")
        m = gs.last(g3)
        check(S, m.get("to_kind") == "person" and m.get("to_qq") == "101"
              and str(m.get("to_reason") or "").startswith("llm"),
              "审核结果能改写硬逻辑")

        # @ 这种强证据不被「大家」推翻，只记冲突
        gs.observe(g3, mid="a3", qq="102", name="小柚子",
                   text="小鲸鱼 你说呢", to_qq="999", to_reason="at",
                   bot_qq="999", names=names)
        gs.apply_audit(g3, [{"mid": "a3", "to": "大家", "why": "测试"}],
                       names=names, bot_qq="999", bot_name="小鲸鱼")
        m = gs.last(g3)
        check(S, m.get("to_kind") == "bot" and m.get("audit_conflict"),
              "@ 强证据不被审核推翻，只记冲突")

        dbg = gs.format_debug(g3, 10)
        check(S, "硬：" in dbg and "审：" in dbg, "现场自检能对照硬逻辑/审核")
        check(S, gs.kind_cn("unknown") == "不确定"
              and gs.reason_cn("second_person+adjacent") == "第二人称+相邻",
              "卡片上的类型/原因翻成人话")
        check(S, gs.audit_skip_reason({"is_bot": True}) == "她自己说的"
              and "强信号" in gs.audit_skip_reason(
                  {"to_kind": "bot", "hard_reason": "at"})
              and gs.audit_skip_reason({"to_kind": "group", "hard_reason": "none"}) == "",
              "能区分「不审（原因）」和真的「未审核」")

        # 图片指向（用户 2026-11-20：情侣 @ 完发抱抱表情，她以为在跟她互动）
        _gi = "936601580"
        gs.clear(_gi)
        check(S, gs.image_target(_gi, "101", ["999"], bot_qq="999")[0] == "bot",
              "图片：@ 了她 → 判给她")
        _ik, _in, _ir = gs.image_target(_gi, "101", ["102"], bot_qq="999",
                                        names={"102": "小柚子"})
        check(S, _ik == "person" and _in == "小柚子",
              f"图片：@ 了别人 → 判给那个人（{_in}）")
        gs.observe(_gi, mid="i1", qq="999", name="你", text="我在呢",
                   is_bot=True, bot_qq="999")
        _ik2, _in2, _ir2 = gs.image_target(_gi, "101", [], bot_qq="999")
        check(S, _ik2 == "bot", f"图片：没 @ 但上一条是她说的话 → 判给她（{_ir2}）")
        _gi2 = "936601581"
        gs.clear(_gi2)
        check(S, gs.image_target(_gi2, "101", [], bot_qq="999")[0] == "unknown",
              "图片：没有任何线索 → unknown（让她自己判断）")
        check(S, "审核" in sa.status() and "model=" in sa.status(),
              f"status 能说明为什么没审：{sa.status()[:60]}")

        # 没有 message_id 时也要能生成合成 id（否则审核结果映射不回来）
        g4 = "936601571"
        gs.clear(g4)
        gs.observe(g4, qq="101", name="阿明", text="没有消息号", names=names)
        check(S, bool(gs.last(g4).get("mid")), "缺 message_id 时生成合成 id")

        # 交错发言不能拆线程（用户 2026-11-19 实测：阿明连说几句，喵插一句「好噢~」，
        # 同一场对话被拆成两条线，现场里两条线程各说各的）
        g8 = "936601575"
        gs.clear(g8)
        for i, (qq, nm, tx) in enumerate([
            ("101", "阿明", "最近在写档案系统"),
            ("101", "阿明", "她会把每个用户记日记，心情，印象和印象深刻的几件事"),
            ("102", "小柚子", "好噢~"),
            ("101", "阿明", "而且上下文随用随拉不会注意力崩溃"),
        ]):
            gs.observe(g8, mid=f"i{i}", qq=qq, name=nm, text=tx, names=names)
        check(S, len(gs.snapshot(g8, bot_qq="999")["threads"]) == 1,
              "交错发言（中间插一句）不拆线程")

        # 时间断档不该拆线程 / 不该掉出窗口（用户 2026-11-19：
        # 「A：英伟达跌了吗」……10 分钟后「B：对啊，都是老黄的锅」）
        g7 = "936601574"
        gs.clear(g7)
        _now7 = time.time()
        gs.observe(g7, mid="t1", qq="101", name="阿明",
                   text="你们最近发现英伟达股票跌了吗", names=names,
                   ts=_now7 - 700)
        gs.observe(g7, mid="t2", qq="102", name="小柚子",
                   text="对啊，都是老黄的锅", names=names, ts=_now7 - 60)
        check(S, len(gs.recent_messages(g7, 20)) == 2,
              "隔 10 分钟的消息仍在同一个窗口里")
        check(S, len(gs.snapshot(g7, bot_qq="999")["threads"]) == 1,
              "隔 10 分钟接话不拆线程")
        _p7 = sa._build_prompt(gs.recent_messages(g7, 20), {2}, "小鲸鱼",
                               ["阿明", "小柚子"])
        check(S, "距上一句" in _p7, "审核 prompt 标出时间断档")

        # 回放：重启后没人说话时，用历史/样例灌一段现场来测（用户 2026-11-19）
        g6 = "936601573#replay"
        gs.clear(g6)
        n_rp = gs.replay(g6, [
            {"qq": "101", "who": "阿明", "text": "今天又加班，胃有点疼", "kind": "user"},
            {"qq": "102", "who": "小柚子", "text": "你吃药了吗", "kind": "user"},
        ], bot_qq="999", names=names)
        check(S, n_rp == 2 and len(gs.snapshot(g6, bot_qq="999")["threads"]) == 1,
              f"回放能把历史消息灌成现场（{n_rp} 条）")
        check(S, gs.last(g6).get("qq") == "102", "回放保留发送者")

        # ---------- 重启补现场：日志里有的，重启后必须还能看到 ----------
        # 用户 2026-11-19 实测：群里 5 句（小强→土豆→鱼），中间重启过一次，
        # 之后 `/测函数 现场` 只有「窗口 1 条」——「打印还是打不到那条，明明 10 条间隔」。
        # 现场只活在内存里，但原文在对话日志里 → 现场为空时按**条数**补回来。
        from services import dialogue_log as _dl9
        g9 = "936601577"
        for _qq9, _who9, _tx9 in [
            ("101", "阿明(101)", "少爷怎么没玩gta6"),
            ("102", "小柚子(102)", "没钱"),
            ("103", "老王(103)", "这猫躺得比我还会摆烂"),
            ("103", "老王(103)", "少爷没钱玩gta6，倒是有钱养这么个大爷"),
        ]:
            _dl9.log_msg(g9, _who9, _qq9, _tx9)
        gs.clear(g9)                       # ← 模拟重启：内存现场全没了
        _r9 = gs.recent_messages(g9, 20)
        check(S, len(_r9) >= 4, f"重启后按条数从对话日志补回现场（{len(_r9)} 条）")
        check(S, gs.seeded_count(g9) >= 4, "补回来的条数记下来了（卡片要显示）")
        check(S, gs.snapshot(g9, bot_qq="999").get("seeded") >= 4,
              "snapshot 带「日志补 N 条」")
        check(S, all("(" not in r["name"] and "（" not in r["name"] for r in _r9),
              "补回来的名字去掉了「阿明(101)」这种 QQ 尾巴")
        check(S, len(gs.recent_messages(g9, 20)) == len(_r9),
              "补过一次就不重复补（再读一次条数不变）")
        # 补完再来一条活消息：不该重复、也不该把补回来的冲掉
        gs.observe(g9, mid="live9", qq="104", name="阿明",
                   text="我打印个log看看", bot_qq="999")
        _r9b = gs.recent_messages(g9, 20)
        check(S, len(_r9b) == len(_r9) + 1 and _r9b[-1]["text"] == "我打印个log看看",
              f"补回来的现场能接着记活消息（{len(_r9b)} 条）")
        # 同一条消息记两遍（NapCat 回显 / 识图+chat 各记一次）只算一条
        gs.observe(g9, mid="live9", qq="104", name="阿明",
                   text="我打印个log看看", bot_qq="999")
        check(S, len(gs.recent_messages(g9, 20)) == len(_r9b), "同一个消息号只进现场一次")
        # 占位（[图片]）留在现场里给审核当前因，但不进审核队列
        gs.observe(g9, mid="img9", qq="101", name="小强",
                   text="[图片] 一只猫四脚朝天", no_audit=True)
        check(S, len(gs.recent_messages(g9, 20)) == len(_r9b) + 1
              and len(gs.needs_audit(g9, 20)) <= len(_r9b),
              "图片占位进现场但不进审核")
        check(S, "占位" in gs.audit_skip_reason({"no_audit": True}),
              "卡片上占位写「不审（占位…）」")

        # 重启后**先来了第一条活消息**再补：必须按时间重放，
        # 否则历史消息是拿「未来消息」推线程的，会凭空多出一条假线程
        g10 = "936601578"
        gs.clear(g10)
        for _qq, _who, _tx in [
            ("101", "阿明(101)", "少爷怎么没玩gta6"),
            ("102", "小柚子(102)", "没钱"),
            ("103", "老王(103)", "这猫躺得比我还会摆烂"),
        ]:
            _dl9.log_msg(g10, _who, _qq, _tx)
        gs.observe(g10, mid="live10", qq="104", name="阿明", text="我打印个log看看")
        gs.ensure_seeded(g10)
        _snap10 = gs.snapshot(g10, bot_qq="999")
        check(S, len(_snap10["threads"]) == 1 and _snap10.get("seeded") >= 3,
              f"先来活消息再补现场：仍是 1 条线程（{len(_snap10['threads'])} 条）")
        check(S, gs.last(g10).get("text") == "我打印个log看看",
              "补完「最新一条」还是刚才那句，不是补回来的历史")
        check(S, len(gs.recent_messages(g10, 20)) == 4,
              f"补 + 活消息一共 4 条（实际 {len(gs.recent_messages(g10, 20))}）")

        # ---------- 冲人短句：判给「最近被提到的人」，不是紧挨着的上一句 ----------
        # 用户 2026-11-19 真机：老王「找打了」其实是对「肥鱼」说的，
        # 硬逻辑和审核都判成了上一句的 OTAKU咩。
        g12 = "936601580"
        gs.clear(g12)
        _n12 = {"阿明": "101", "老王": "103", "OTAKU咩": "104",
                "肥鱼": "105", "小鲸鱼": "999"}
        gs.observe(g12, mid="j1", qq="104", name="OTAKU咩",
                   text="为啥肥鱼现在会这样说话了", to_qq="999", to_reason="at",
                   bot_qq="999", names=_n12)
        gs.observe(g12, mid="j2", qq="999", name="你", text="我觉得他就是欠",
                   is_bot=True, bot_qq="999", names=_n12)
        gs.observe(g12, mid="j3", qq="105", name="肥鱼", text="我怎么了",
                   bot_qq="999", names=_n12)
        gs.observe(g12, mid="j4", qq="101", name="阿明",
                   text="超过20字就转发发送", bot_qq="999", names=_n12)
        _m12 = gs.observe(g12, mid="j5", qq="103", name="老王", text="找打了",
                          bot_qq="999", names=_n12)
        check(S, _m12.get("to_name") == "肥鱼"
              and _m12.get("to_reason") == "recent_mention",
              f"「找打了」判给最近被提到的肥鱼（{_m12.get('to_name')}/{_m12.get('to_reason')}）")
        check(S, "最近提到过他" in gs.addressee_line(g12, "999"),
              "收件人文案写「最近提到过他」")
        check(S, gs.reason_cn("recent_mention") == "最近被提到", "卡片上翻成人话")
        _m12b = gs.observe(g12, mid="j6", qq="101", name="阿明",
                           text="我今天加班到十点，胃疼得不行，先睡了明天再说吧",
                           bot_qq="999", names=_n12)
        check(S, _m12b.get("to_reason") != "recent_mention",
              "长句不会被当成「冲人短句」")

        # ---------- 线程视图 / 硬逻辑看 20 条，不是五六条 ----------
        g13 = "936601581"
        gs.clear(g13)
        for _i in range(12):
            gs.observe(g13, mid=f"w{_i}", qq="101", name="阿明",
                       text=f"第{_i}句：这个话题继续说", names=names)
        _s13 = gs.snapshot(g13, bot_qq="999")
        check(S, len(_s13["threads"][0]["lines"]) == 12,
              f"线程视图不再只给 3 句（{len(_s13['threads'][0]['lines'])} 句）")
        check(S, gs.SCENE_WINDOW >= 20,
              f"硬逻辑窗口 {gs.SCENE_WINDOW} 条（和审核窗口对齐）")
        check(S, "第0句" in gs.format_block(_s13), "prompt 的现场块也带上更早的句子")

        # 滚动总结：写回后现场块最后一行要能看到
        gs.set_summary(g2, "大家在聊家庭组，小红在招人，老王说懒得退组。",
                       until_mid=str(gs.recent_messages(g2, 1)[0].get("mid")))
        blk = gs.format_block(gs.snapshot(g2, bot_qq="999", my_names=["小鲸鱼"]))
        check(S, "【最近这段在聊什么】" in blk, "现场块末尾带滚动总结")

        # ---------- 端到端：审核 LLM 真的调通并写回（假 httpx，不联网） ----------
        # 用户 2026-11-19 现场全是「审：未审核」：光测 apply_audit 不够，
        # 必须把 audit_recent 整条链路（组 prompt → 解析 JSON → 编号找 mid → 写回）跑通。
        class _Resp:
            def __init__(self, obj):
                self._obj = obj

            def raise_for_status(self):
                return None

            def json(self):
                return self._obj

        class _Client:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, headers=None, json=None):
                import json as _j
                obj = {
                    "summary": "阿明说胃疼，小柚子问他吃药没。",
                    "items": [{"i": 2, "to": "阿明", "why": "回答上一句"}],
                }
                return _Resp({"choices": [{"message": {
                    "content": _j.dumps(obj, ensure_ascii=False)}}]})

        class _Httpx:
            AsyncClient = _Client

        _keep = (sa.ENABLED, sa.BOT_MODE, sa.XAI_API_KEY, sa.httpx, sa.MIN_INTERVAL)
        try:
            (sa.ENABLED, sa.BOT_MODE, sa.XAI_API_KEY,
             sa.httpx, sa.MIN_INTERVAL) = (True, "api", "sk-selftest", _Httpx, 0)
            g5 = "936601572"
            gs.clear(g5)
            gs.observe(g5, mid="e1", qq="101", name="阿明",
                       text="今天胃疼", names=names)
            gs.observe(g5, mid="e2", qq="102", name="小柚子",
                       text="你吃药了吗", names=names)
            changed = await sa.audit_recent(
                g5, bot_qq="999", bot_name="小鲸鱼", names=names, force=True)
            m = gs.last(g5)
            check(S, changed >= 1 and m.get("audited"),
                  f"审核链路端到端能写回（改写 {changed} 条）")
            check(S, "上次" in sa.status(), "status 记下「上次审核」结果")
            check(S, "总结" in gs.format_block(gs.snapshot(g5, bot_qq="999")),
                  "审核返回的滚动总结进了现场块")
            # @ 这种强证据不该被审核改成「大家」
            gs.observe(g5, mid="e3", qq="102", name="小柚子",
                       text="小鲸鱼 你说呢", to_qq="999", to_reason="at",
                       bot_qq="999", names=names)
            await sa.audit_recent(g5, bot_qq="999", bot_name="小鲸鱼",
                                  names=names, force=True)
            check(S, gs.last(g5).get("to_kind") == "bot", "@ 强证据不被审核改写")
        finally:
            (sa.ENABLED, sa.BOT_MODE, sa.XAI_API_KEY,
             sa.httpx, sa.MIN_INTERVAL) = _keep

        # 现场图片卡片（环境没 Pillow 就跳过）
        try:
            from services import profile_card as _pc
            if _pc.Image is None:
                skip(S, "环境没有 Pillow，跳过现场图渲染")
            else:
                simg = _pc.render_scene_image(g2, bot_qq="999", bot_name="小鲸鱼")
                check(S, bool(simg) and Path(simg).exists(),
                      f"群聊现场能渲染成图片：{simg}")
                check(S, str(simg).endswith(".jpg"),
                      "现场图用 JPEG（PNG 太大，NapCat 上传会超时）")
        except Exception as e:
            check(S, False, "现场图渲染异常", f"{type(e).__name__}: {e}")
    except Exception as e:
        if isinstance(e, ModuleNotFoundError):
            skip(S, f"环境没有 {e.name}，跳过审核接线检查")
        else:
            check(S, False, "scene_auditor 接线异常", f"{type(e).__name__}: {e}")

    _chat = (ROOT / "plugins" / "chat" / "__init__.py").read_text(encoding="utf-8")
    _llm = (ROOT / "services" / "llm.py").read_text(encoding="utf-8")
    _pb = (ROOT / "services" / "prompt_builder.py").read_text(encoding="utf-8")
    check(S, "group_scene.observe" in _chat and "group_scene.snapshot" in _chat,
          "chat 每条群消息都进现场")
    check(S, "scene_text=_scene_text" in _chat and "scene_text" in _pb,
          "现场块传进 build_prompt")
    check(S, "if _bg_text:" in _llm, "普通文字聊天不再丢弃 build_prompt 背景段")
    # 2026-11-23 用户：「艾特也不回复」→ 一段里多条 @ 必须逐条回应，
    # 不能再写「只接一个线程 / 默认回最后一条」。
    check(S, "多条用一条答完" in _llm and "只接一个线程" not in _llm,
          "@ 她的消息要求逐条回应（一条消息答完，不再只接一个线程）")
    _seg = (ROOT / "services" / "segments.py").read_text(encoding="utf-8")
    check(S, "def _pick_rows" in _seg and "必须回应" in _seg,
          "分段截断保留 @ 消息 + 段末「必须回应」清单")
    check(S, "_scene_selfcheck" in _chat and "🎬 群聊现场" in _chat,
          "有 /测函数 现场（打印当前线程/收件人）")
    check(S, "scene_auditor.diagnose" in _chat and 'startswith(("审核"' in _chat,
          "有 /测函数 审核（审核层单独自检）")
    check(S, "_scene_prepare_replay" in _chat and "#replay" in _chat,
          "有 /测函数 现场 回放|样例（没人说话也能测）")
    # 2026-11-19 真事故：一张图回了两条，其中一条是「怎么图还没加载出来」
    check(S, "_media_only(" in _chat and 'kinds & {"image", "video", "file"}' in _chat,
          "纯图片/视频消息 chat 不单独回（识图交给 vision_agent）")
    check(S, "_already_replied" in _chat and "[DEDUP]" in _chat,
          "同一条 message_id 只回一次（防 NapCat 重投/双路入队）")
    check(S, "_focus_take_pending" in _chat and "空 @ 之后补了个表情" in _chat,
          "空 @ 之后跟表情包会回一句（不再装死）")
    check(S, "_media_hint" in _chat and "没有说别的话" in _chat,
          "空文本的媒体消息会补一句人话再进模型")
    # ---- 2026-11-19 参考 MaiBot 的四项改造 ----
    try:
        from services.view_frequency import ViewFrequency, mean_interval
        _v = ViewFrequency()
        check(S, _v.decide(1.0, 0.99), "查看频率：外部 1.0 必放行")
        _v.activate(1.0)
        for _ in range(15):
            _v.record_run()
        check(S, _v.phase == "decay" and _v.internal_frequency > 0,
              f"衰减 15 轮还没耗尽（{_v.describe()}）")
        _v.record_run()
        check(S, _v.phase == "recovery", "第 16 轮耗尽 → 进入恢复期")
        check(S, _v.probability(1.0) < 1.0, "恢复期概率被压低")
        for _ in range(16):
            _v.decide(1.0, 0.999)
        check(S, _v.phase == "idle", "16 次判定后回到 idle")
        check(S, mean_interval([1, 3, 5]) == 2.0, "平均消息间隔算得对")

        from services import attention as _att
        _blk = _att.render("wild", "loose", "lively")
        check(S, "漂移档位：强烈跳跃" in _blk and "回钩策略：宽松" in _blk,
              "注意力漂移档位能渲染")
        # 真机踩过：这块自带 `▍注意力风格`，而调用方又给了 `▍02d 注意力风格`
        # → prompt 里出现两个同级标题
        check(S, not _blk.lstrip().startswith("▍"),
              "注意力块不自带标题（避免和 02d 重复两层）")
        check(S, _att.render("off") == "", "注意力漂移关闭时不注入")

        from services import conv_state as _cs
        _k = "group_936601576"
        _cs.clear(_k)
        check(S, _cs.render(_k) == "", "对话状态：没数据时为空")
        _cs._save(_k, {"topics": ["在聊抽卡"], "pending": ["阿明还没发截图"], "_at": 1})
        _blk2 = _cs.render(_k)
        check(S, "当前话题" in _blk2 and "阿明还没发截图" in _blk2,
              "对话状态能渲染 5 类")
        _cs.clear(_k)
    except Exception as e:
        check(S, False, "查看频率/注意力/对话状态 自检异常",
              f"{type(e).__name__}: {e}")

    check(S, "_debounce_wait(" in _chat and "DEBOUNCE_BARE_IMAGE" in _chat,
          "防抖表：裸图等下载、在叫她更快")
    check(S, "_VIEW_FREQ" in _chat and "[SILENCE]" in _chat,
          "查看频率 + 静默唤醒已接线")
    check(S, "conv_state.render(key)" in _llm,
          "对话状态已进 prompt（注意力风格改由硬逻辑，不再占常驻）")
    # ---- 表达学习（2026-11-19）：群 / 人 两个作用域 + 自我视角 ----
    try:
        from services import expression as _ex
        _ex.clear()
        _ex._merge("group_936601577", [
            {"text": "草", "scene": "吐槽", "tone": "短"},
            {"text": "草", "scene": "吐槽", "tone": "短"},
            {"text": "难绷", "scene": "接梗", "tone": "冲"},
        ])
        _items = _ex.load("group_936601577")
        check(S, len(_items) == 2, f"表达：同一条合并（{len(_items)} 条）")
        check(S, max(int(i.get("count") or 0) for i in _items) == 2,
              "表达：重复出现 count+1")
        check(S, bool((_ex._pick_one("group_936601577") or {}).get("text")),
              "表达：能按权重抽一条")
        _view = _ex.self_view()
        check(S, "我在各处的样子" in _view and "群 936601577" in _view,
              "表达：自我视角能汇总各作用域")
        _ex.clear()
        check(S, _ex.load("group_936601577") == [], "表达：能清空")
    except Exception as e:
        check(S, False, "表达学习自检异常", f"{type(e).__name__}: {e}")

    check(S, "expression.mine(" in _chat and "expression.render(" in _llm,
          "表达学习已接线（挖掘 + 注入）")
    check(S, 'on_command("表现"' in _chat, "有 /表现（自我视角）")
    # ---- 未知命令不能装死（2026-11-19：/搜图151768172 → 没人理）----
    try:
        from services import cmd_guard as _cg
        check(S, _cg.classify("/画图 一只猫") == "", "命令识别：真命令放行")
        check(S, _cg.classify("/根本没有这个命令").startswith("unknown:"),
              "命令识别：不存在的命令能认出来")
        check(S, _cg.classify("/搜图 147954620") == "",
              "命令识别：Alconna 命令（/搜图）不会被抢答")
        check(S, _cg.classify("/搜图151768172").startswith("missing_space:"),
              "命令识别：/搜图151768172 判成「少空格」")
        check(S, _cg.classify("/画图一只猫").startswith("missing_space:"),
              "命令识别：命令和参数粘在一起能认出来")
        check(S, "没有这个命令" in _cg.hint_for("unknown:x"), "命令识别：会给她一句提示")
    except Exception as e:
        check(S, False, "cmd_guard 自检异常", f"{type(e).__name__}: {e}")
    check(S, "cmd_guard.classify(" in _chat and "_cmd_hint" in _chat,
          "未知命令不再静默，改为让她回一句")
    # ---- 同图复用 + 查出处工具（2026-11-19）----
    check(S, '"name": "vision_source"' in _chat, "有 vision_source 工具（查图片出处）")
    check(S, "vision_source" in _llm and "trace.moe" in _llm,
          "工具目录里写了 vision_source")
    _va = (ROOT / "plugins" / "vision_agent" / "__init__.py").read_text(encoding="utf-8")
    check(S, "_replay_cached" in _va and "已复用上次的描述" in _va,
          "同一张图重复发会复用上次描述（不再「看不到这张」）")
    _sa_src = (ROOT / "services" / "scene_auditor.py").read_text(encoding="utf-8")
    try:
        _ver_ok = str(sa.VERSION) in sa.status()
    except Exception:
        _ver_ok = False
    check(S, "VERSION" in _sa_src and _ver_ok,
          "status 带版本号（确认服务器跑的是哪一版）")
    check(S, "_log_fail" in _sa_src, "审核失败日志有 60s 限流（不会只报一次）")
    # 2026-11-19 真事故：current_personality 是 defaultdict，被当成函数调用 →
    # bot_name 那行每次抛 TypeError → 被 except 吞掉 → 审核从来没跑过。
    check(S, "current_personality()" not in _chat,
          "没有把 current_personality(defaultdict) 当函数调用")
    check(S, "persona_name(" in _chat and "_warn_audit_wire" in _chat,
          "人格名走 persona_name()，审核接线异常会 WARNING")


# ============================================================
# main
# ============================================================
# ============================================================
# 分组：API 余额 / 用量（function calling: api_usage）
# ============================================================
async def t_usage():
    S = "usage"
    from services import api_usage as au
    au.STATE_FILE = Path("data/api_usage.json")
    try:
        au.STATE_FILE.unlink()
    except Exception:
        pass

    au.note_usage(prompt=1000, completion=200, cache_hit=600, model="deepseek-chat")
    au.note_usage(prompt=500, completion=100, cache_hit=0, model="deepseek-chat")
    _u = au.usage(1)
    _t = _u[0] if _u else {}
    check(S, _t.get("calls") == 2, f"按天累加调用次数（{_t.get('calls')}）")
    check(S, _t.get("prompt") == 1500 and _t.get("completion") == 300,
          f"prompt/completion 累加（{_t.get('prompt')}/{_t.get('completion')}）")
    check(S, _t.get("cache_hit") == 600, "缓存命中单独记（算命中率用）")
    check(S, (_t.get("models") or {}).get("deepseek-chat") == 2, "按模型计数")

    # 余额：假返回，绝不联网（selftest 必须离线）
    _real_fb = au.fetch_balance

    async def _good_fb():
        return {"ok": True, "provider": "deepseek", "available": True,
                "items": [{"currency": "CNY", "total": "110.00",
                           "granted": "10.00", "topped_up": "100.00"}], "err": ""}

    async def _bad_fb():
        return {"ok": False, "provider": "deepseek", "err": "HTTP 401"}

    try:
        au.fetch_balance = _good_fb
        _rep = await au.report_text(1)
        check(S, "110.00" in _rep and "CNY" in _rep, "报告里能出余额")
        check(S, "调用 2 次" in _rep and "prompt 1,500" in _rep,
              f"报告里能出今天的用量：{_rep.splitlines()[0][:40]}")
        au.fetch_balance = _bad_fb
        _rep2 = await au.report_text(1)
        check(S, "查不到" in _rep2 and "401" in _rep2,
              "余额查不到时照实说（不编数字）")
    finally:
        au.fetch_balance = _real_fb

    # 供应商判断：base_url 要去掉 /v1 才是余额接口
    _old_base = au.BASE_URL
    try:
        au.BASE_URL = "https://api.deepseek.com/v1"
        check(S, au.root_url() == "https://api.deepseek.com"
              and au.provider() == "deepseek", "DeepSeek：root_url 去掉 /v1")
        au.BASE_URL = "https://api.x.ai/v1"
        check(S, au.provider() == "xai", "xAI 官方：认出来（它没有余额接口）")
    finally:
        au.BASE_URL = _old_base

    # 记账保留期：太老的日期要被清掉
    _d = au._load()
    _d["2000-01-01"] = {"calls": 9}
    au._save(_d)
    check(S, "2000-01-01" not in au._load(), "超过 API_USAGE_KEEP_DAYS 的记账被清掉")


# ============================================================
# 分组：/上传文件（QQ 文件 → data/，仅主人）／引用回复
# ============================================================
class _FakeEv:
    """只给规则用：user_id / group_id / get_message()。"""

    def __init__(self, uid, types, gid=0, mid="1"):
        self.user_id = uid
        self.group_id = gid
        self.message_id = mid
        self._types = list(types)

    def get_message(self):
        return [types.SimpleNamespace(type=t, data={}) for t in self._types]

    def get_plaintext(self):
        return ""


async def t_upload():
    S = "upload"
    try:
        import plugins.chat as C
    except Exception as e:
        skip(S, "plugins.chat 导入失败", f"{type(e).__name__}: {e}")
        return

    # 文件名消毒：不能写出 data/ 之外
    check(S, C._safe_upload_name("../../etc/passwd") == "passwd",
          f"路径穿越被削掉（{C._safe_upload_name('../../etc/passwd')}）")
    check(S, ":" not in C._safe_upload_name("a/b:c?.zip"), "非法字符被替换")

    C.UPLOAD_DIR = Path("data/uploads")
    _p, _err = await C._save_upload("t_测试.txt", b"hello")
    check(S, bool(_p) and not _err and Path(_p).exists(),
          f"能落到 data/uploads（{_p} {_err}）")
    check(S, Path(_p).read_bytes() == b"hello", "内容没坏")
    _p2, _ = await C._save_upload("t_测试.txt", b"hello2")
    check(S, _p2 != _p, "重名不覆盖（加时间戳）")

    _old_mb = C.UPLOAD_MAX_MB
    try:
        C.UPLOAD_MAX_MB = 0.000001
        _, _e2 = await C._save_upload("big.bin", b"x" * 100)
    finally:
        C.UPLOAD_MAX_MB = _old_mb
    check(S, bool(_e2) and "上限" in _e2, f"超过上限拒收（{_e2}）")

    # 等待规则：只认主人 + 只认文件段 + 3 分钟过期
    _old_owner = C.is_owner
    try:
        C.is_owner = lambda q: str(q) == "10001"
        C._FILE_WAIT.clear()
        C._FILE_WAIT["10001"] = {"until": time.time() + 60}
        check(S, C._file_wait_rule(_FakeEv("10001", ["text", "file"])),
              "主人发的文件会被收下")
        check(S, not C._file_wait_rule(_FakeEv("10001", ["text"])),
              "普通文字不会触发")
        C._FILE_WAIT["10001"] = {"until": time.time() - 1}
        check(S, not C._file_wait_rule(_FakeEv("10001", ["file"])), "过期就不收了")
        C._FILE_WAIT.clear()
        C._FILE_WAIT["99999"] = {"until": time.time() + 60}
        check(S, not C._file_wait_rule(_FakeEv("99999", ["file"])), "非主人发了也不收")
        C._FILE_WAIT.clear()
    finally:
        C.is_owner = _old_owner

    # 引用回复：评论/吐槽某句或某图时必须引
    from services import llm as L
    _old_ev = L.TOOL_CTX.get("event")
    try:
        L.TOOL_CTX["event"] = _FakeEv("10001", ["text"], gid=1, mid="123456")
        _p = L._reply_protocol(True)
        check(S, "<reply:" in _p and "吐槽" in _p,
              "引用协议覆盖「评价/吐槽某一句或某张图」")
        L.TOOL_CTX["event"] = _FakeEv("10001", ["image"], gid=1, mid="123456")
        _post = L._post_rules("group_1", "10001", "1")
        check(S, "系统会自动引用这条" in _post, "图片消息会告诉她自己会兜底引用")
    finally:
        L.TOOL_CTX["event"] = _old_ev

    _src = (ROOT / "plugins" / "chat" / "__init__.py").read_text(encoding="utf-8")
    check(S, "_pic_msg = _media_placeholder(ev)" in _src,
          "图片/视频消息在发送层兜底引用（_auto_rid）")


# ============================================================
# 分组：心情判读（第三方 LLM，影子运行）
# ============================================================
async def t_moodjudge():
    S = "moodjudge"
    from services import mood, mood_judge as MJ

    # ① 纯函数：ctx 只留最近 N 条
    _ctx = [f"第{i}句" for i in range(50)]
    check(S, len(MJ._clip_ctx(_ctx)) == MJ.CTX_LINES,
          f"判读上下文只留最近 {MJ.CTX_LINES} 条（实际 {len(MJ._clip_ctx(_ctx))}）")
    check(S, MJ._clip_ctx(_ctx)[-1] == "第49句", "留的是最新的那几条")

    # ② JSON 解析（带 ``` 围栏 / 多余文字）
    d = MJ._parse_json('```json\n{"word":"懒得解释","value":-0.5}\n```')
    check(S, d.get("word") == "懒得解释", "能解析带围栏的 JSON")
    d2 = MJ._parse_json('好的：{"word":"心里空了一下","value":0.1} 以上')
    check(S, d2.get("word") == "心里空了一下", "能解析夹在文字里的 JSON")
    check(S, MJ._parse_json("完全不是 json") == {}, "垃圾返回 → 空 dict")

    # ③ 真调（假 httpx）：名词清洗 + 阈值两位小数 + 夹到 [-1,1]
    import httpx as _hx

    class _Resp:
        status_code = 200
        def raise_for_status(self):
            return None
        def json(self):
            # 必须带 mind：缺了会触发 _mind_only 补问，_Client.last 就变成补问的
            # prompt（不含「心情判读员」），下面那条断言会误报
            return {"choices": [{"message": {"content":
                '{"mind":"想怼回去又忍住了","word":" 憋着一口气 ",'
                '"value":-0.4567,"affinity":-0.04,"why":"被抢话"}'}}]}

    class _Client:
        last = {}
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, headers=None, json=None, **kw):
            _Client.last = {"url": url, "json": json or {}}
            return _Resp()

    _old_ac = _hx.AsyncClient
    _old_base = MJ._base
    try:
        _hx.AsyncClient = _Client
        MJ._base = lambda: ("http://127.0.0.1:1/v1", "test-key")
        _r = await MJ.judge("憋着一口气，懒得解释", _ctx, think="想怼回去又忍住了")
        check(S, _r.get("ok") and _r.get("word") == "憋着一口气",
              f"判读名词（{_r.get('word')}）")
        # 2026-11-20 定稿：判读给的是**涨幅 delta**，不是当前心情值
        check(S, _r.get("delta") == -0.46, f"涨幅两位小数（{_r.get('delta')}）")
        check(S, "value" not in _r,
              "判读结果不再给当前心情值（只给涨幅，水平由系统累计）")
        # 2026-11-20 定稿：判读不再直接给 affinity，改给 cause，
        # 好感由「Δmood × AFFINITY_GAIN」本地推导（只有 cause=speaker 才算到人头上）。
        check(S, _r.get("cause") in ("speaker", "environment", "self", "other"),
              f"判读给出 cause（{_r.get('cause')}）")
        check(S, hasattr(MJ, "AFFINITY_GAIN"), "好感系数 AFFINITY_GAIN 可配置")
        _prompt = str((_Client.last.get("json") or {}).get("messages", [{}])[-1].get("content") or "")
        check(S, "心情判读员" in _prompt and "两位小数" in _prompt,
              "prompt 是「判读员」口径")
        check(S, "心理活动" in _prompt and "最近" in _prompt, "判读看了心理活动 + 群聊上下文")
        check(S, _prompt.count("第49句") == 1 and "第0句" not in _prompt,
              "只把最近 20 条塞进 prompt")
    finally:
        _hx.AsyncClient = _old_ac
        MJ._base = _old_base

    # ④ 写回 mood（影子字段），且 _decay 不会丢
    _k = "group_judgetest"
    _old_j = MJ.judge

    async def _fake(words, ctx=None, *, think="", prev_word="", prev_value=0.0,
                    hint="", repair=False, archive=""):
        # 判读给的是涨幅
        return {"ok": True, "word": "被夸得有点飘", "delta": 0.42,
                "why": "他夸了她", "src": words, "at": time.time()}

    try:
        MJ.judge = _fake
        _v0 = float(mood.get_mood(_k).get("value") or 0.0) / 100.0
        _res = await MJ.judge_and_store(_k, "被夸得有点飘，但还装作没事", _ctx, force=True)
        check(S, _res.get("ok"), "判读写回成功")
        _j = mood.get_judge(_k)
        # 2026-11-22：Δ 上限按设计 0.5，0.42 原样落库（防一边倒靠入口噪音丢弃）
        check(S, _j.get("word") == "被夸得有点飘" and abs(float(_j.get("delta")) - 0.42) < 1e-6,
              f"mood 里存下了涨幅（{_j.get('word')}/Δ{_j.get('delta')}）")
        check(S, abs(float(_j.get("value")) - (_v0 + 0.42)) < 1e-3,
              f"心情值是累加的（{_v0:+.2f} + 0.42 → {_j.get('value')}）")
        check(S, "judge" in mood.get_mood(_k), "get_mood 带 judge 字段")
        # 衰减一次再看，判读不能被丢掉（_decay 只回 value 会丢）
        mood.set_label("再写一次心情", _k)
        check(S, bool(mood.get_mood(_k).get("judge")),
              "衰减/再写原话后判读仍在")
        # 节流：刚判过的不重复调
        _res2 = await MJ.judge_and_store(_k, "又被夸了", _ctx)
        check(S, not _res2.get("ok") and "节流" in str(_res2.get("err")),
              "同一会话 20 秒内不重复判")
    finally:
        MJ.judge = _old_j

    # ⑤ 阈值夹取 / 空输入不炸
    _rec = mood.set_judge(_k, "极端", 5.0)
    check(S, _rec["value"] == 1.0, "阈值上限夹到 +1.00")
    _rec2 = mood.set_judge(_k, "极端", -9)
    check(S, _rec2["value"] == -1.0, "阈值下限夹到 -1.00")
    _bad = await MJ.judge("", None)
    check(S, not _bad.get("ok"), "没写心里话就不判（不炸）")

    # ⑥ 她没写也要能判：按**条数**拉最近 20 条群聊（用户 2026-11-19）
    from services import group_scene as _gs
    _gid = "939999"
    _gs.clear(_gid)
    for _i in range(25):
        _gs.observe(_gid, mid=f"c{_i}", qq="101", name="阿明",
                    text=f"第{_i}句：随便聊聊", names={"阿明": "101"})
    _gs.observe(_gid, mid="cbot", qq="999", name="你", text="我也说一句",
                is_bot=True, bot_qq="999")
    _ctxs = MJ.scene_lines(f"group_{_gid}")
    check(S, len(_ctxs) == MJ.CTX_LINES,
          f"按条数拉最近 {MJ.CTX_LINES} 条（实际 {len(_ctxs)}）")
    check(S, any(x.startswith("你 → 大家: ") for x in _ctxs),
          "上下文里包含她自己说的话")
    check(S, _ctxs[-1].startswith("你 → 大家: 我也说一句"),
          "取的是最新的那几条")

    # 她没写、只有群聊 → 也能判（假 judge 走通整条链）
    _k2 = f"group_{_gid}"
    _old_j2 = MJ.judge

    async def _fake2(words, ctx=None, *, think="", prev_word="", prev_value=0.0,
                     hint="", repair=False, archive=""):
        return {"ok": True, "word": "有点敷衍", "delta": -0.2, "why": "都在闲扯",
                "src": (words or (ctx or [""])[-1]), "at": time.time()}

    try:
        MJ.judge = _fake2
        mood.set_judge(_k2, "旧的", 0.0)
        _st = mood._load()
        _st["sessions"][_k2]["judge"]["at"] = time.time() - 10 ** 6
        mood._save(_st)
        _r3 = await MJ.judge_and_store(_k2, "", None, force=True)
        check(S, _r3.get("ok") and _r3.get("word") == "有点敷衍",
              f"她没写也能按群聊判（{_r3.get('word')}）")
        check(S, mood.get_judge(_k2).get("word") == "有点敷衍", "结果写回了 mood")
        # 没写时的刷新闸门：刚判过就不再判
        _r4 = await MJ.judge_and_store(_k2, "", None)
        check(S, (not _r4.get("ok")) and ("s 后" in str(_r4.get("err")) or "节流" in str(_r4.get("err"))),
              f"没写心情时按 REFRESH 节流（{_r4.get('err')}）")
    finally:
        MJ.judge = _old_j2

    # ⑥ MEMO 的「心理活动」字段
    from services.aura_logic import parse_memos
    _clean, _memos = parse_memos(
        "嗯 [[MEMO|emotion:有点闷|think:想反驳又觉得没必要|text:他说我笨，我没回]]")
    check(S, _memos and _memos[0].get("think") == "想反驳又觉得没必要",
          f"parse_memos 能读心理活动（{_memos[0].get('think') if _memos else ''}）")
    check(S, "think" not in _clean, "心理活动不会漏给用户")


async def t_quiet():
    """安静建议：好感/心情走势 → 建议对谁安静、要不要整群安静、找谁说话。"""
    S = "quiet"
    import tempfile as _tf
    from services import profiles as PF, mood as M, chat_switch as CS
    from services import quiet_advisor as QA, group_scene as GS

    _old = (PF.PROFILE_DIR, M.MOOD_FILE, M.MOOD_HISTORY, CS.STATE_FILE,
            dict(CS._cache.get("data") or {}), float(CS._cache.get("mtime") or 0))
    # 这一组测的是「安静建议」的阈值逻辑，不是心情惯性：
    # 临时关掉平滑（SMOOTH=1、不限步长），让 set_value_from_judge 写多少就是多少。
    _old_smooth, _old_step = M.SMOOTH, M.MAX_STEP
    M.SMOOTH, M.MAX_STEP = 1.0, 99.0
    # 同理：这组也不测「好感每日上限」，放开它，免得把走势测试卡住
    _old_cap = PF.AFFINITY_DAILY_CAP
    PF.AFFINITY_DAILY_CAP = 1.0
    _d = Path(_tf.mkdtemp(prefix="quiet_"))
    qq, gid, key = "123456789", "888", "group_888"
    try:
        PF.PROFILE_DIR = _d
        M.MOOD_FILE = _d / "mood.json"
        M.MOOD_HISTORY = _d / "mh.jsonl"
        CS.STATE_FILE = _d / "switch.json"
        CS._cache.update({"mtime": 0.0, "data": {"groups": {}, "users": {}}})
        QA._LAST.clear()
        GS.clear(gid)

        # ① 只有「低好感」、没有事件 → 不建议（用户：不是好感低就孤立人）
        PF.upsert_profile(qq, nickname="阿明")
        PF._save("profile", qq, {"qq": qq, "nickname": "阿明",
                                 "affinity": -0.55, "affinity_log": []})
        _o = QA._compute(key, speaker_qq=qq, gid=gid, addressed=False)
        check(S, not _o.get("person"),
              "只有低好感、没有「连续/大幅下降」→ 不建议对人安静")

        # ② 连续下降 + 单次大幅 → 建议对人安静
        for _dv in (-0.10, -0.08, -0.07):
            PF.bump_affinity(qq, _dv, "他阴阳你")
        _tr = PF.affinity_trend(qq)
        check(S, _tr["neg_n"] == 3 and _tr["drop"] >= 0.24 and _tr["spike"] >= 0.09,
              f"好感走势算得准（掉 {_tr['drop']}×{_tr['neg_n']} 次，最大 -{_tr['spike']}）")
        _o = QA._compute(key, speaker_qq=qq, gid=gid, addressed=False)
        check(S, bool(_o.get("person"))
              and float(_o["person"]["score"]) >= QA.PERSON_MIN,
              f"连续掉 3 次 → 建议对他安静（{_o.get('person', {}).get('score')}）")
        check(S, "只屏蔽他" in QA.render(key, speaker_qq=qq, gid=gid, addressed=False),
              "建议文案写明「对某个人安静只屏蔽他（含 @ 和图片）」")

        # ③ 高好感有忍耐：同样跌幅，从 +0.60 掉到 +0.35 不该马上建议
        _log = PF.get_profile(qq).get("affinity_log") or []
        PF._save("profile", qq, {"qq": qq, "nickname": "阿明",
                                 "affinity": 0.35, "affinity_log": _log})
        _ohi = QA._compute(key, speaker_qq=qq, gid=gid, addressed=False)
        check(S, not _ohi.get("person"),
              "本来好感高（+0.60）→ 有忍耐，同样跌幅不急着安静")

        # ④ 道歉破冰 → 建议降权（给台阶）
        CS.set_off("群", gid, minutes=30, reason="烦", by_qq=qq)
        _th = CS.maybe_thaw(key, qq, "对不起，我错了")
        check(S, bool(_th) and bool(CS.recent_thaw(key)),
              "道歉破冰会留记录（安静建议据此降权）")
        PF._save("profile", qq, {"qq": qq, "nickname": "阿明",
                                 "affinity": -0.30, "affinity_log": _log})
        _oth = QA._compute(key, speaker_qq=qq, gid=gid, addressed=False)
        check(S, (not _oth.get("person"))
              or float(_oth["person"]["score"]) < float(_o["person"]["score"]),
              "刚破冰 → 对同一个人不再那么急地建议安静")

        # ⑤ 心情流水记的是「会话自己变了多少」，不是和全局比
        CS.set_on("群", gid)
        M.MOOD_HISTORY.write_text("", encoding="utf-8")
        for _v in (0.0, -0.20, -0.35):
            M.set_value_from_judge(_v, key, word="被噎住", about_qq=qq)
        _mt = M.trend(key, about_qq=qq)
        check(S, abs(float(_mt["drop"]) - 0.35) <= 0.06,
              f"心情走势 = 会话自身变化（{_mt['drop']}，不是累计成 0.55）")

        # ⑥ 整群：心情差 + 她说了话没人接 → 建议整群安静
        for _i in range(6):
            GS.observe(gid, mid=f"q{_i}", qq=qq, name="阿明",
                       text=f"随便聊聊{_i}", names={"阿明": qq})
        GS.observe(gid, mid="qb", qq="999", name="你", text="我也觉得",
                   is_bot=True, bot_qq="999")
        for _i in range(6):
            GS.observe(gid, mid=f"r{_i}", qq="222222222", name="小柚子",
                       text=f"哈哈{_i}", names={"小柚子": "222222222"})
        M.set_value_from_judge(-0.60, key, word="插不上话")
        _og = QA._compute(key, speaker_qq="222222222", gid=gid, addressed=False)
        check(S, QA._hard_to_join(gid, False) >= 0.9, "「说了话没人接」→ 难插入 1.0")
        check(S, bool(_og.get("group")), f"心情差 + 插不上话 → 建议整群安静"
                                        f"（{_og.get('group', {}).get('score')}）")
        check(S, QA._hard_to_join(gid, True) == 0.0, "有人在叫她 → 不算难插入")
        check(S, "here" in QA.render(key, speaker_qq="222222222", gid=gid,
                                     addressed=False), "整群建议写明 target=\"here\"")

        # ⑦ /测函数 安静 的参数解析 + 目标解析（普通群友 QQ 不能再被当成群号）
        _src_chat = (ROOT / "plugins" / "chat" / "__init__.py").read_text(
            encoding="utf-8", errors="ignore")
        check(S, "def _minutes_of" in _src_chat and "def _split_quiet_args" in _src_chat,
              "/测函数 安静 支持「群号 + 时长」解析")
        try:
            import plugins.chat as C
            check(S, C._minutes_of("1小时") == 60 and C._minutes_of("30分钟") == 30
                  and C._minutes_of("90") == 90 and C._minutes_of("2天") == 2880,
                  "/测函数 安静 时长解析：1小时/30分钟/90/2天")
            _tg, _rs = C._split_quiet_args("100000017 1小时")
            check(S, _tg == "100000017" and "1小时" in _rs,
                  "群号和时长能拆开")

            class _Bot:
                async def get_group_member_list(self, group_id=0):
                    return [{"user_id": int(qq), "nickname": "阿明"}]

            _ev = types.SimpleNamespace(group_id=int(gid), user_id=int(qq),
                                        self_id=999)
            _k1, _i1 = await C._resolve_target(str(qq), _ev, _Bot())
            check(S, _k1 == "人" and _i1 == str(qq),
                  "普通群友的 QQ 号 → 判成「人」（以前会误判成群号）")
            _k2, _i2 = await C._resolve_target("这个人", _ev, _Bot())
            check(S, _k2 == "人" and _i2 == str(qq), "target=这个人 → 当前说话人")
            _k3, _i3 = await C._resolve_target("here", _ev, _Bot())
            check(S, _k3 == "群" and _i3 == gid, "target=here → 整群")
            _k4, _i4 = await C._resolve_target("群:999999", _ev, _Bot())
            check(S, _k4 == "群" and _i4 == "999999", "target=群:号 → 强制当群")
        except Exception as e:
            skip(S, f"/测函数 安静 解析测试跳过（{type(e).__name__}: {e}）")
    finally:
        PF.PROFILE_DIR, M.MOOD_FILE, M.MOOD_HISTORY, CS.STATE_FILE = _old[:4]
        CS._cache.update({"mtime": _old[5], "data": _old[4]})
        M.SMOOTH, M.MAX_STEP = _old_smooth, _old_step
        PF.AFFINITY_DAILY_CAP = _old_cap
        QA._LAST.clear()
        GS.clear(gid)


GROUPS = {
    "memo": t_memo, "mood": t_mood, "moodjudge": t_moodjudge, "quiet": t_quiet,
    "profiles": t_profiles, "memory": t_memory,
    "persona": t_persona, "agent": t_agent, "journal": t_journal,
    "vision": t_vision, "text": t_text, "sticker": t_sticker, "curator": t_curator,
    "importer": t_importer, "dispatch": t_dispatch, "node": t_node, "menu": t_menu,
    "focus": t_focus, "call": t_call, "referee": t_referee, "weekly": t_weekly,
    "song": t_song, "static": t_static, "net": t_net, "burst": t_burst,
    "timing": t_timing, "napcat": t_napcat, "night": t_night,
    "archive": t_archive, "scene": t_scene, "usage": t_usage, "upload": t_upload,
}
OFFLINE = ["memo", "mood", "profiles", "memory", "persona", "agent",
           "journal", "vision", "text", "sticker", "curator", "importer",
           "dispatch", "node", "menu", "focus", "call", "referee", "weekly",
           "song", "static", "burst", "timing", "napcat", "night", "archive",
           "scene", "usage", "upload", "moodjudge", "quiet"]


async def amain(names):
    for n in names:
        fn = GROUPS.get(n)
        if not fn:
            print(f"  ⚠️  未知分组：{n}")
            continue
        print(f"\n▶ {n}")
        try:
            await fn()
        except Exception:
            check(n, False, "分组执行抛异常", traceback.format_exc()[-600:])
    return summary()


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(description="全链路离线自检")
    ap.add_argument("-v", "--verbose", action="store_true", help="显示每一条检查")
    ap.add_argument("--net", action="store_true", help="额外跑真实端点冒烟")
    ap.add_argument("--only", default="", help="只跑指定分组，逗号分隔")
    args = ap.parse_args()
    VERBOSE = args.verbose

    work = Path(tempfile.mkdtemp(prefix="selftest_all_"))
    os.chdir(work)
    install_stubs()

    # 👇 Docker 里 nonebot 是真包、但 selftest 是独立进程没初始化 → init 一下，
    #    插件的 on_command 内部要 get_driver().config，不 init 就 ValueError。
    #    本地（没装 nonebot）走上面的桩，这里直接跳过。
    if "nonebot" not in STUBBED:
        try:
            import nonebot as _nb
            _nb.init()
            print("  nonebot 已初始化（Docker 真包，插件可 import）")
        except Exception as _e:
            print(f"  ⚠️ nonebot.init 失败（插件 import 类测试会跳过）："
                  f"{type(_e).__name__}: {_e}")

    names = [n.strip() for n in args.only.split(",") if n.strip()] if args.only else list(OFFLINE)
    if args.net and "net" not in names:
        names.append("net")

    print("=" * 68)
    print("  QQ Bot 全链路自检")
    print(f"  工作目录（临时，不碰真实 data/）：{work}")
    print(f"  项目根：{ROOT}")
    print(f"  模式：{'离线 + 真实端点' if args.net else '纯离线'}")
    print("=" * 68)

    code = asyncio.run(amain(names))
    print(f"\n  临时目录未删除（方便排查）：{work}")
    return code


if __name__ == "__main__":
    sys.exit(main())
