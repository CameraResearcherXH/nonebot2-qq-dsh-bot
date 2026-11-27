#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
test_regressions.py —— 2026-11-17 那批修复的「旧功能保留」核对脚本。

为什么要它
----------
这次改的是几个**全局公共函数**（记忆检索、心情、截断、上下文包、协议拼装），
任何一处改错都会表现成「她突然变笨了」，很难在真机上一眼定位。
这个脚本把「旧行为」和「新行为」都写成断言：

    旧行为必须原样保留（回归）
    新行为必须真的生效（修复）

用法
----
    # 容器内
    docker exec qq-grok-bot python scripts/test_regressions.py

    # 或者本地（有 nonebot 依赖的环境）
    python scripts/test_regressions.py

全部通过返回 0，有失败返回 1。
脚本只碰临时目录（/tmp 或 data/_regress_tmp），**不会动线上 data/**。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS, FAIL, SKIP = [], [], []


def check(name: str, cond, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  ✅ {name}")
    else:
        FAIL.append((name, detail))
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


def skip(name: str, why: str):
    SKIP.append((name, why))
    print(f"  ⏭ {name}（{why}）")


# ============================================================
# 0. 标记协议：所有 kind 仍能识别（旧的不能丢）
# ============================================================
def t_marker_protocol():
    print("\n[0] 标记协议")
    from services.llm import _REQ_RE, _drop_one_marker

    cases = {
        "SEARCH": "[[SEARCH|领域展开]]",
        "LIBRARY": "[[LIBRARY|以前的约定]]",
        "SING": "[[SING|晴天]]",
        "WHO": "[[WHO|小强]]",
        "HISTORY": "[[HISTORY|20]]",
        "MEMORY": "[[MEMORY|白米饭]]",
        "PROFILE": "[[PROFILE|小明]]",
        "ASKGLM": "[[ASKGLM|怎么办]]",
        "LATER": "[[LATER|8]]",
        "LIKE": "[[[LIKE|老大]]]",
        "POKE": "[[POKE|老大]]",
        "NAPCAT": "[[NAPCAT|set_group_card|{}]]",
        "TOOLS": "[[TOOLS|画图与搜图]]",
        "STICKER": "[[STICKER|列表]]",
        "LUCK": "[[LUCK]]",
        "video_understand": "[[video_understand|https://b23.tv/x]]",
    }
    for kind, sample in cases.items():
        m = _REQ_RE.search(sample)
        check(f"仍识别 {kind}", bool(m) and m.group(1).upper() == kind.upper(), sample)

    check("普通回复不误判", not _REQ_RE.search("今天吃什么"))

    # 新增：只摘一个，剩下的保留（旧版会把两个都删掉 → 第二个被静默吞）
    text = "先查 [[SEARCH|A]] 再记 [[MEMORY|B]]"
    one = _drop_one_marker(text, "SEARCH", "A")
    check("只摘除已处理的那一个", "[[SEARCH|A]]" not in one and "[[MEMORY|B]]" in one, one)
    two = _drop_one_marker(one, "MEMORY", "B")
    check("第二个也能被下一轮处理", "[[" not in two, two)


# ============================================================
# 1. 多标记串联：真的能跑两轮（旧版只跑一轮）
# ============================================================
def t_fulfill_rounds():
    print("\n[1] 补资料多轮")
    import services.llm as L
    import services.web_search as WS

    async def _fake_search(q):
        return f"（搜索 {q} 的结果）"

    old = getattr(WS, "fetch_web_search", None)
    WS.fetch_web_search = _fake_search

    class _Resp:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": self.content}}]}

    class _Client:
        def __init__(self, seq):
            self.seq = list(seq)
            self.calls = 0

        async def post(self, *a, **kw):
            self.calls += 1
            return _Resp(self.seq.pop(0))

    async def _run():
        client = _Client(["先查到了 [[MEMORY|B]]", "两件事都办好了"])
        msgs = []
        r1 = await L._fulfill_requests("我去查 [[SEARCH|A]] 和 [[MEMORY|B]]", msgs, client, {})
        r2 = await L._fulfill_requests(r1, msgs, client, {})
        return r1, r2, client.calls

    try:
        r1, r2, calls = asyncio.run(_run())
        check("第一轮后仍保留未处理的标记", "[[MEMORY|B]]" in r1, r1)
        check("第二轮把剩余标记处理掉", "[[" not in r2, r2)
        check("两轮各发一次请求", calls == 2, f"calls={calls}")
    finally:
        if old is not None:
            WS.fetch_web_search = old


# ============================================================
# 2. 结构化结果不被摘要压缩（旧版会压掉编号）
# ============================================================
def t_no_compress_structured():
    print("\n[2] 结构化结果不压缩")
    src = (ROOT / "services" / "llm.py").read_text(encoding="utf-8")
    seg = src.split("if raw_len > 400 and kind not in", 1)
    check("有压缩分支", len(seg) > 1)
    body = seg[1][:200] if len(seg) > 1 else ""
    for k in ("MEMORY", "PROFILE", "WHO", "STICKER", "HISTORY", "TOOLS"):
        check(f"{k} 不在压缩名单", k in body, body.replace("\n", " ")[:120])


# ============================================================
# 3. 记忆：旧行为（去重/衰减/排序）+ 新行为（原子写/目录不计命中）
# ============================================================
def t_memory():
    print("\n[3] 记忆")
    import services.memory_v2 as MV

    tmp = Path(tempfile.mkdtemp(prefix="regress_mem_"))
    old = (MV.MEM_DIR, MV.BLOCK_DIR, MV.INDEX_FILE)
    # 2026-11-26：这个用例测的是 **json 回滚路径**（index.json / 原子写 / 目录不计命中）。
    # 线上默认 MEMORY_BACKEND=sqlite，json 索引根本不会写 → 以前 3 条断言全红。
    # sqlite 路径由 scripts/check_memory_dual.py 覆盖，别在这里混着测。
    old_backend = MV.BACKEND
    MV.BACKEND = "json"
    try:
        MV.MEM_DIR = tmp
        MV.BLOCK_DIR = tmp / "blocks"
        MV.BLOCK_DIR.mkdir(parents=True, exist_ok=True)
        MV.INDEX_FILE = tmp / "index.json"

        a = MV.remember(text="小明喜欢吃白米饭，说要减肥", about_qq="1001")
        b = MV.remember(text="小明喜欢吃白米饭，说要减肥", about_qq="1001")
        check("完全重复仍复用同一条（旧行为）", a == b, f"{a} vs {b}")

        c = MV.remember(text="小红喜欢喝奶茶，天天喝", about_qq="1002")
        check("不同内容写新条", c != a)

        # 原子写：不能留下 .tmp
        check("索引原子写（无残留 tmp）",
              MV.INDEX_FILE.exists() and not (tmp / "index.json.tmp").exists())

        # 目录浏览不计命中（新行为）
        before = json.loads(MV.INDEX_FILE.read_text(encoding="utf-8"))
        hit0 = {d["id"]: d["hit_count"] for d in before["items"]}
        cat = MV.recall_catalog("白米饭", limit=10)
        check("目录能查到（旧行为）", bool(cat.get("items")), cat)
        after = json.loads(MV.INDEX_FILE.read_text(encoding="utf-8"))
        hit1 = {d["id"]: d["hit_count"] for d in after["items"]}
        check("目录浏览不计命中（新行为）", hit0 == hit1, f"{hit0} vs {hit1}")

        # 真召回计命中
        hits = MV.recall("白米饭")
        check("recall 仍能命中（旧行为）", bool(hits), hits)
        after2 = json.loads(MV.INDEX_FILE.read_text(encoding="utf-8"))
        hit2 = {d["id"]: d["hit_count"] for d in after2["items"]}
        check("真召回计命中（旧行为）", hit2.get(a, 0) == hit1.get(a, 0) + 1,
              f"{hit1.get(a)} -> {hit2.get(a)}")

        # 遗忘曲线：60 天前的中性记忆分数应低于强情绪记忆
        idx = json.loads(MV.INDEX_FILE.read_text(encoding="utf-8"))
        now = time.time()
        idx["items"].append({
            "id": "neutral60", "summary": "白米饭 普通的一天", "kind": "fact",
            "about_qq": "1001", "tags": [], "importance": 0.5, "emotion_hit": False,
            "emotion_weight": 0.0, "created_at": now - 60 * 86400,
            "last_hit_at": now - 60 * 86400, "hit_count": 0, "status": "active",
            "tokens": MV.tokenize("白米饭 普通的一天"),
        })
        idx["items"].append({
            "id": "strong60", "summary": "白米饭 那天她哭得很厉害", "kind": "impression",
            "about_qq": "1001", "tags": [], "importance": 0.9, "emotion_hit": True,
            "emotion_weight": 0.9, "created_at": now - 60 * 86400,
            "last_hit_at": now - 60 * 86400, "hit_count": 0, "status": "active",
            "tokens": MV.tokenize("白米饭 那天她哭得很厉害"),
        })
        MV._save_index(idx)
        got = {h["id"]: h["score"] for h in MV.recall("白米饭", limit=10)}
        check("情绪刻痕仍比中性记忆更容易被想起（旧行为）",
              got.get("strong60", 0) > got.get("neutral60", 0),
              f"strong={got.get('strong60')} neutral={got.get('neutral60')}")
    finally:
        MV.MEM_DIR, MV.BLOCK_DIR, MV.INDEX_FILE = old
        MV.BACKEND = old_backend
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 4. 心情：旧行为（无 session 时全额）+ 新行为（分层/迁移/上下限）
# ============================================================
def t_mood():
    print("\n[4] 心情")
    import services.mood as M

    tmp = Path(tempfile.mkdtemp(prefix="regress_mood_"))
    old = M.MOOD_FILE
    try:
        M.MOOD_FILE = tmp / "mood.json"

        # 旧行为：不带 session_key 时，delta 必须全额生效（selftest 依赖这个）
        M.bump_mood(20)
        v = M.get_mood()["value"]
        check("无 session 时心情全额变化（旧行为）", abs(v - 20) < 0.01, v)

        # 上下限
        M.bump_mood(999)
        check("上限钳制 100（旧行为）", M.get_mood()["value"] <= 100)
        M.bump_mood(-9999)
        check("下限钳制 -100（旧行为）", M.get_mood()["value"] >= -100)

        # 新行为：分层
        M.MOOD_FILE.unlink()
        M.bump_mood(-16, session_key="group_A")
        a = M.get_mood("group_A")["value"]
        b = M.get_mood("group_B")["value"]
        g = M.get_mood()["value"]
        check("本群吃满额（新行为）", abs(a + 16) < 0.01, a)
        check("全局只吃一部分（新行为）", abs(g + 16 * M.GLOBAL_SHARE) < 0.01, g)
        check("别的群只受轻微影响（新行为）", abs(b - g) < 0.01, f"B={b} global={g}")

        # 旧格式迁移
        M.MOOD_FILE.write_text(json.dumps({"value": 30.0, "updated_at": time.time()}),
                               encoding="utf-8")
        check("旧 mood.json 能迁移（兼容）", abs(M.get_mood()["value"] - 30) < 0.1,
              M.get_mood()["value"])

        # 情绪入口：默认（MOOD_FROM_JUDGE=true）只存原话、不动数值（2026-11-20 定稿）
        M.MOOD_FILE.unlink()
        v0 = M.get_mood()["value"]
        v1 = M.bump_by_emotion("happy")
        check("bump_by_emotion 默认不改数值（新行为）", abs(v1 - v0) < 0.01, v1)
        check("bump_by_emotion 仍存原话（新行为）",
              bool(M.get_mood().get("label")), M.get_mood().get("label"))
        # 回滚模式（MOOD_FROM_JUDGE=false）：旧行为必须还在
        _old_from = M.MOOD_FROM_JUDGE
        try:
            M.MOOD_FROM_JUDGE = False
            M.MOOD_FILE.unlink()
            v2 = M.bump_by_emotion("happy")
            check("bump_by_emotion 回滚模式仍工作（旧行为）", v2 > 0, v2)
            check("neutral 不改心情（旧行为）",
                  abs(M.bump_by_emotion("neutral") - v2) < 0.01)
        finally:
            M.MOOD_FROM_JUDGE = _old_from
    finally:
        M.MOOD_FILE = old
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 5. 文本截断：旧用例 + 新断句
# ============================================================
def t_text():
    print("\n[5] 截断")
    from services.text import limit_hanzi, hanzi_count

    check("旧用例：5 字截断", limit_hanzi("一二三四五六七八九十", 5) == "一二三四五…",
          limit_hanzi("一二三四五六七八九十", 5))
    check("旧用例：英文不受限", limit_hanzi("abc", 5) == "abc")
    _cut20 = limit_hanzi("啊" * 50, 20)
    # 20 个汉字 + 「…」（省略号算 0.5 字宽），不是硬砍到 20.0
    check("旧用例：50 字截到 20（+省略号）",
          20 <= hanzi_count(_cut20) <= 20.5, str(hanzi_count(_cut20)))
    check("旧用例：截断带省略号", _cut20.endswith("…"))
    check("limit<=0 原样返回", limit_hanzi("随便", 0) == "随便")

    cut = limit_hanzi("好的，我这就去看看那个东西到底怎么回事", 6)
    check("新行为：优先在标点处断句", cut == "好的，", cut)


# ============================================================
# 6. 上下文包：旧块 + 新块 + 预算
# ============================================================
def t_packet():
    print("\n[6] 上下文包")
    import services.memory_v2 as MV
    import services.prompt_builder as PB
    import services.mood as M

    tmp = Path(tempfile.mkdtemp(prefix="regress_pkt_"))
    old_mem = (MV.MEM_DIR, MV.BLOCK_DIR, MV.INDEX_FILE)
    old_mood = M.MOOD_FILE
    try:
        MV.MEM_DIR = tmp / "mem"
        MV.BLOCK_DIR = tmp / "mem" / "blocks"
        MV.BLOCK_DIR.mkdir(parents=True, exist_ok=True)
        MV.INDEX_FILE = tmp / "mem" / "index.json"
        M.MOOD_FILE = tmp / "mood.json"
        M.bump_mood(-50)

        MV.remember(text="小明说下周要去上海出差", about_qq="1001")
        pk = PB.build_packet(session_key="group_1", user_text="上海出差",
                             speaker_qq="1001", speaker_name="小明")

        check("有 _meta.memory_hits", isinstance(pk.get("_meta"), dict)
              and pk["_meta"].get("memory_hits", 0) >= 1, pk.get("_meta"))
        check("记忆目录不再被预算裁空（新行为）", bool(pk.get("memory_index")),
              list(pk.keys()))
        check("心情块仍在（旧行为）", bool(pk.get("mood")), list(pk.keys()))
        check("除 _meta 外全是字符串", all(isinstance(v, str)
              for k, v in pk.items() if k != "_meta"))
        for k in ("memory_index", "mood"):
            if pk.get(k):
                check(f"{k} 不超上限", len(pk[k]) <= PB.BLOCK_BUDGET[k][1] + 2,
                      f"{len(pk[k])} > {PB.BLOCK_BUDGET[k][1]}")

        # 预算分配旧行为
        demo = {"profiles": "档" * 500, "memory": "忆" * 600, "mood": "心" * 50}
        alloc = PB.allocate_budget(demo, total=PB.TOTAL_BUDGET)
        check("预算：保底生效（旧行为）",
              alloc["profiles"] >= PB.BLOCK_BUDGET["profiles"][0], alloc)
        check("预算：不超各自上限（旧行为）",
              all(v <= PB.BLOCK_BUDGET[k][1] for k, v in alloc.items()), alloc)
    finally:
        MV.MEM_DIR, MV.BLOCK_DIR, MV.INDEX_FILE = old_mem
        M.MOOD_FILE = old_mood
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================================
# 7. 协议拼装：固定区 00-11，动态区 12/13
# ============================================================
def t_protocol():
    print("\n[7] 协议拼装")
    import services.llm as L

    static = L._behaviour_protocol("group_1", is_group=True, has_ctx=True, dynamic=False)
    # 2026-11-26：协议小节改成「编号 + 名字」，且 02b/02c/02d/07 已删
    # （由硬逻辑/post_rules 兜底）。所以不再逐个断言 00~11 都存在 ——
    # 那样每删一节测试就红一次。改成断言「必须有的」+「必须没有的」。
    for sec in ("▍00 工具", "▍01 长度", "▍02 接话", "▍05 称呼",
                "▍06 记忆", "▍06b 心里话", "▍06c 心情", "▍09 底线", "▍10 人称"):
        check(f"固定区仍有 {sec}", sec in static)
    check("群聊固定区有 ▍04 叫人", "▍04 叫人" in static)
    check("固定区不含已删的 ▍07", "▍07" not in static)
    check("固定区不含 ▍12（缓存优化）", "▍12" not in static)
    check("固定区不含 ▍13（缓存优化）", "▍13" not in static)
    check("协议头声明「编号小的优先」", "编号小的优先" in static)

    # 私聊：04 群里怎么叫人 不该出现（旧行为）
    priv = L._behaviour_protocol("private_1", is_group=False, has_ctx=False, dynamic=False)
    check("私聊不注入「群里怎么叫人」（旧行为）", "▍04" not in priv)
    check("私聊仍有 ▍10 人称（旧行为）", "▍10" in priv)

    # 兼容：dynamic=True 时应把动态小节并回去
    class E:
        message_id = 987654
        group_id = 1
        user_id = 2
    L.set_tool_context(event=E(), newer_count=3)
    try:
        dyn = L._dynamic_protocol("group_1", is_group=True)
        check("动态区含 ▍12 + 当前消息号", "▍12" in dyn and "987654" in dyn, dyn[:80])
        merged = L._behaviour_protocol("group_1", is_group=True, has_ctx=True, dynamic=True)
        check("dynamic=True 仍可合并（兼容）", "▍12" in merged)
    finally:
        L.TOOL_CTX.clear()


# ============================================================
# 8. 视觉标记：单一常量
# ============================================================
def t_vision_tag():
    print("\n[8] 视觉标记")
    import services.llm as L
    check("VISION_TAG 常量存在", L.VISION_TAG == "[图片识别结果]")
    src = (ROOT / "plugins" / "vision_agent" / "__init__.py").read_text(encoding="utf-8")
    check("vision_agent 使用常量（不再写字面量）",
          "VISION_TAG" in src and "[图片识别结果] 群号" not in src)

    # 视频走同一条「当前媒体」通道时必须被认成 video，不能当成图
    check("VIDEO_TAG 常量存在", L.VIDEO_TAG == "[视频解析结果]")
    check("图片消息 → image", L.media_kind_of("[图片识别结果] 群号:1") == "image")
    check("视频消息 → video", L.media_kind_of("[视频解析结果] 群号:1") == "video")
    check("纯文字 → ''", L.media_kind_of("今天吃什么") == "")
    check("显式 is_vision 兼容", L.media_kind_of("x", True) == "image")

    # 结构化头两种形态都要解析出来（report 形态没有 `| 图片内容:`）
    _h1 = L._parse_media_head(
        "[图片识别结果] 群号:1 | 发送者QQ:2 | 发送者昵称:x | 图片内容:一只猫")
    check("老格式解析", _h1 and _h1[3] == "一只猫", _h1)
    _h2 = L._parse_media_head(
        "[图片识别结果] 群号:1 | 发送者QQ:2 | 发送者昵称:x\nqwen：一只猫\nGLM：一只狗")
    check("report 形态解析（desc 空、body 有内容）",
          _h2 and _h2[3] == "" and "qwen" in _h2[4], _h2)
    check("非结构化文本返回 None", L._parse_media_head("随便聊聊") is None)


# ============================================================
# 9. 自造工具沙箱：不泄漏密钥 / 危险操作仍拦
# ============================================================
def t_self_tools():
    print("\n[9] 自造工具沙箱")
    try:
        import plugins.self_tools as ST
    except Exception as e:
        skip("self_tools 导入", f"{type(e).__name__}: {e}")
        return

    os.environ["__REGRESS_SECRET__"] = "[REDACTED_SECRET]"
    try:
        env = ST._safe_env()
        check("子进程环境不含密钥变量",
              not any(k.endswith(("_KEY", "_TOKEN", "_SECRET")) for k in env),
              [k for k in env if "KEY" in k or "TOKEN" in k])
        check("子进程环境不含自定义密钥", "__REGRESS_SECRET__" not in env)
        check("PATH 仍在（能跑 python）", "PATH" in env)
    finally:
        os.environ.pop("__REGRESS_SECRET__", None)

    check("危险操作仍被拦（旧行为）", bool(ST._dangerous_hit("docker restart bot")))
    check("普通代码不误伤（旧行为）", not ST._dangerous_hit("print('hello')"))


# ============================================================
# 10. 纯函数：@ 解析 / 重复检测（旧行为）
# ============================================================
def t_pure_funcs():
    print("\n[10] 其它纯函数")
    from services.aura_logic import resolve_member, member_names
    members = [{"user_id": 555, "card": "小明（摸鱼）", "nickname": "xm"}]
    by = member_names(members)
    check("@ 解析：精确名（旧行为）", resolve_member("xm", by, set()) == "555")
    check("@ 解析：重名不猜（旧行为）", resolve_member("不存在", by, set()) == "")

    src = (ROOT / "plugins" / "chat" / "__init__.py").read_text(encoding="utf-8")
    # 2026-11-26：「@她」和「引用回复她」拆成 _at_me()/_quote_me()，两种写法都认
    check("@ 纯问候不再一刀切（新行为）",
          ("SKIP_PURE_GREETING and not _at_me(event)" in src
           or "SKIP_PURE_GREETING and not event.is_tome()" in src))
    check("私聊仍不受纯问候限制（旧行为）", "PRIVATE_SLEEP" in src)


def main() -> int:
    print("=" * 60)
    print("回归核对：旧功能保留 + 新修复生效")
    print("=" * 60)
    for fn in (t_marker_protocol, t_fulfill_rounds, t_no_compress_structured,
               t_memory, t_mood, t_text, t_packet, t_protocol,
               t_vision_tag, t_self_tools, t_pure_funcs):
        try:
            fn()
        except Exception as e:
            import traceback
            FAIL.append((fn.__name__, f"{type(e).__name__}: {e}"))
            print(f"  ❌ {fn.__name__} 崩溃：{type(e).__name__}: {e}")
            traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASS)} ｜ 失败 {len(FAIL)} ｜ 跳过 {len(SKIP)}")
    for name, why in FAIL:
        print(f"  ❌ {name} — {why}")
    for name, why in SKIP:
        print(f"  ⏭ {name} — {why}")
    print("=" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())

