#!/usr/bin/env python3
# scripts/test_new_features.py
"""新增能力的离线自检（不依赖 nonebot，可在本地/CI 跑）。

覆盖：文字宽度计数、skill 开关、memdb、llm_usage、plugin_registry。
用法：python3 scripts/test_new_features.py
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = Path(tempfile.mkdtemp(prefix="qgb_test_"))
os.environ["MEMORY_DB"] = str(_tmp / "mem.db")
os.environ["LLM_USAGE_DB"] = str(_tmp / "usage.db")
os.environ["SKILLS_FILE"] = str(_tmp / "skills.json")
os.environ["SKILLS_MASTER"] = "on"
os.environ["GROUP_MOOD_FILE"] = str(_tmp / "group_mood.json")
os.environ["WHITE_MOON_FILE"] = str(_tmp / "white_moon.json")
os.environ["WHITE_MOON_MODE"] = "true"

from services import memdb, llm_usage, skill_switch  # noqa: E402
from services.text import hanzi_count, limit_hanzi  # noqa: E402
from services import plugin_registry  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL ") + name + (("  " + detail) if detail else ""))


# ---- 文字宽度 ----
check("英文长文计数 > 20", hanzi_count("hello world " * 20) > 20,
      str(hanzi_count("hello world " * 20)))
check("英文长文被截断", len(limit_hanzi("hello world " * 20, 20)) < 200)
check("20 个汉字不截断", limit_hanzi("一二三四五六七八九十一二三四五六七八九十", 20)
      == "一二三四五六七八九十一二三四五六七八九十")

# ---- skill 开关 ----
check("核心不可关", skill_switch.is_enabled("chat.reply") is True)
check("搜索默认开", skill_switch.is_enabled("web_search") is True)
skill_switch.set_enabled("search", False)
check("关搜索后 web_search 关", skill_switch.is_enabled("web_search") is False)
check("关搜索不影响画图", skill_switch.is_enabled("draw") is True)
skill_switch.set_enabled("全部", True)
check("全开后恢复", skill_switch.is_enabled("web_search") is True)

# ---- memdb ----
check("FTS5 可用", memdb.fts_available() is True)
mid = memdb.remember(text="他半夜陪我改代码到三点", kind="bond", about_qq="10001",
                     gid="100000017", importance=0.8, salience=0.9,
                     mood_word="被戳到")
memdb.remember(text="他问我在干嘛", kind="event", about_qq="10002",
               gid="100000017", importance=0.4, salience=0.1)
hits = memdb.recall("改代码", limit=3)
check("关键词召回命中", bool(hits) and "改代码" in hits[0]["summary"])
hits2 = memdb.recall("在吗", limit=3, gid="100000017")
check("无关键词也能召回近期", len(hits2) >= 1)
cat = memdb.recall_catalog("", limit=5, gid="100000017")
check("目录默认时间倒序", cat["mode"] == "time" and len(cat["items"]) == 2)
memdb.patch_turn("turn-x", salience=0.0)  # 不存在的 turn 返回 False
tid = memdb.upsert_turn({"turn_id": "s:1", "detail": "测试 turn", "session": "s",
                         "about_qq": "10001", "title": "测试"})
ok = memdb.patch_turn("s:1", delta=0.4, salience=0.8, valence=1.0,
                      mood_word="开心")
check("turn upsert + patch", bool(tid) and ok)
check("详情可展开", memdb.recall_detail(tid) == "测试 turn")

# ---- llm_usage ----
llm_usage.record(purpose="chat", model="m", prompt=100, completion=20)
llm_usage.record(purpose="judge", model="m", prompt=10, completion=2)
rep = llm_usage.report_text(1)
check("用量报表含 chat", "chat" in rep)
check("用量报表含 Top", "最贵 Top5" in rep and "最省 Bottom5" in rep)

# ---- 插件商城 ----
rows = plugin_registry.scan()
check("扫到第三方插件（≥9）", len(rows) >= 9, str(len(rows)))
check("每个插件都有 UPSTREAM", all(r["has_upstream"] for r in rows))

# ---- memory_v2（json 后端，默认路径）----
# 为什么单独测：memdb 测的是 sqlite 那条腿；默认跑的是 json 这条腿。
# 2026-11-20 就因为 BACKEND 常量被误删，sqlite 测试全过、json 路径全 NameError。
_old_cwd = os.getcwd()
os.chdir(_tmp)
os.environ.pop("MEMORY_BACKEND", None)
try:
    from services import memory_v2
    check("memory_v2 后端 = json", memory_v2.BACKEND == "json", memory_v2.BACKEND)
    _mid = memory_v2.remember(text="他半夜陪我改代码到三点", kind="bond",
                              about_qq="10001", importance=0.8)
    check("memory_v2.remember 返回 id", bool(_mid))
    check("memory_v2.recall 有结果", len(memory_v2.recall("改代码", limit=3)) >= 1)
    _cat = memory_v2.recall_catalog("", limit=5)
    check("memory_v2.recall_catalog 可用", isinstance(_cat, dict) and "items" in _cat)
    check("memory_v2.stats 可用", isinstance(memory_v2.stats(), dict))
    check("memory_v2.recall_detail 可展开", bool(memory_v2.recall_detail(_mid)))
    check("memory_v2.touch 不报错", memory_v2.touch(_mid) is None)
finally:
    os.chdir(_old_cwd)

# ---- 涨幅驱动（json 后端也要能写刻痕）----
_old_cwd = os.getcwd()
os.chdir(_tmp)
os.environ.pop("MEMORY_BACKEND", None)
try:
    from services import memory_v2 as _mv
    tid = _mv.upsert_turn({"turn_id": "s:delta1", "detail": "他忽然夸我好看",
                           "session": "s", "about_qq": "10001"})
    ok = _mv.patch_turn("s:delta1", delta=0.45, salience=0.9, valence=1.0,
                        mood_word="被夸得有点飘")
    item = [d for d in _mv._load_index()["items"] if d.get("turn_id") == "s:delta1"]
    check("json 后端 patch_turn 生效", ok and item)
    if item:
        it = item[0]
        check("json 后端存了 delta", float(it.get("delta") or 0) == 0.45,
              str(it.get("delta")))
        check("json 后端存了 salience(💥档)", float(it.get("salience") or 0) == 0.9)
    # 小事（Δ 小）刻痕低，大事（Δ 大）刻痕高
    _mv.upsert_turn({"turn_id": "s:delta2", "detail": "随便聊两句",
                     "session": "s", "about_qq": "10002"})
    _mv.patch_turn("s:delta2", delta=0.05, salience=0.1, valence=1.0)
    _small = [d for d in _mv._load_index()["items"] if d.get("turn_id") == "s:delta2"]
    check("小事刻痕 < 大事刻痕",
          bool(_small) and float(_small[0].get("salience") or 0) < 0.9)

    # 「印象最深的一件事」：取 |Δ| 最大的记忆，并带上当时的心理活动
    _mv.remember(text="他半夜给我送药", kind="event", about_qq="10001",
                 delta=-0.60, salience=1.0, mind="鼻子有点酸")
    _de = _mv.deepest_event("10001", min_abs=0.15)
    check("印象最深一件事取 |Δ| 最大",
          _de.get("text") == "他半夜给我送药", str(_de))
    check("印象最深一件事带心理活动", _de.get("mind") == "鼻子有点酸", str(_de))

    # 好感每日净变化上限
    from services import profiles as _pf
    _pf.PROFILE_DIR = _tmp / "profiles_delta"
    _pf.PROFILE_DIR.mkdir(exist_ok=True)
    _pf.AFFINITY_DAILY_CAP = 0.15
    _v = 0.0
    for _ in range(10):
        _v = _pf.bump_affinity("5550001", -0.05, reason="t")
    check("好感每日上限 ±0.15", abs(_v) <= 0.151, "%.3f" % _v)
finally:
    os.chdir(_old_cwd)

# ---- 心情峰值事件（涨幅最大 + 反向抵消 + 2 小时保质期）----
_old_cwd = os.getcwd()
os.chdir(_tmp)
os.environ["MOOD_FILE"] = str(_tmp / "mood_peak.json")
try:
    from services import mood as _mood
    from core import tuning as _t
    try:
        _mood.MOOD_HISTORY.unlink()
    except Exception:
        pass
    check("空闲 5 分钟开始衰减", float(_t.f("MOOD_IDLE_AFTER")) == 300.0,
          str(_t.f("MOOD_IDLE_AFTER")))
    check("每 5 分钟向 0 走 0.01",
          float(_t.f("MOOD_IDLE_TICK")) == 300.0
          and float(_t.f("MOOD_IDLE_STEP")) == 1.0)
    _mood.log_shift(-0.38, session_key="s", label="被当众说笨", mind="有点想哭")
    _mood.log_shift(0.18, session_key="s", label="他后来夸我", mind="没那么难受了")
    _mood.log_shift(0.22, session_key="s", label="他逗我笑", mind="算了")
    check("反向波动抵消后没有峰值", _mood.peak_event("s") == {},
          str(_mood.peak_event("s")))
    _mood.log_shift(-0.30, session_key="s", label="他又提了一次", mind="还是在意")
    _pk = _mood.peak_event("s")
    check("抵消后换下一个最大值",
          abs(float(_pk.get("delta") or 0) + 0.30) < 1e-6, str(_pk))
    # 当前心情是正值，但峰值是伤心事 → 状态块要明确说出来
    _st = _mood._load()
    _st.setdefault("sessions", {})["s"] = {"value": 40,
                                          "updated_at": time.time()}
    _mood._save(_st)
    _sb = _mood.status_block("s")
    check("正值心情遇到伤心峰值要说出来",
          "但是呢你最近经历了一件不愉快的事" in _sb, _sb[:100])
    check("峰值事件带心理活动", "（当时你心里：还是在意）" in _sb, _sb[:100])

    # 沉默 15 分钟后有人说话 → touch 先把该走的 2 步（0.02）结掉，再重新计时
    _st = _mood._load()
    _st.setdefault("sessions", {})["idle"] = {
        "value": 50, "updated_at": time.time() - 950,
        "last_active_at": time.time() - 950}
    _mood._save(_st)
    _mood.touch("idle")
    _after = float(_mood.get_mood("idle").get("value") or 0)
    # 50 先过 12h 半衰（950s 约 -0.76），再按 5 分钟 2 步走 -2 → 约 47.24
    check("沉默后 touch 先结算自减（2 步）", 47.0 < _after < 48.0, str(_after))
finally:
    os.chdir(_old_cwd)

# ---- 心情口径：设计 §13.1（clamp(v+Δ)）+ Δ 入口护栏参数（2026-11-22）----
_old_cwd = os.getcwd()
os.chdir(_tmp)
os.environ["MOOD_FILE"] = str(_tmp / "mood_sat.json")
try:
    from services import mood as _ms
    from core import tuning as _tn
    try:
        _ms.MOOD_FILE.unlink()
    except Exception:
        pass
    _v2 = 0.0
    for _i in range(8):
        _v2 = _ms.apply_delta(-0.20, "sat", word="烦")
    # 设计口径是线性累加：8 × -0.2 = -1.6 → clamp 到 -1.0
    check("心情按设计线性累加并封顶 -1", abs(_v2 + 1.0) < 1e-6, str(_v2))
    _v3 = _ms.apply_delta(0.20, "sat", word="被哄好")
    check("回暖按全额累加", abs(_v3 - (_v2 + 0.20)) < 1e-6, f"{_v2} → {_v3}")
    check("Δ 上限是设计值 0.5", abs(float(_tn.f("MOOD_MAX_DELTA")) - 0.5) < 1e-9,
          str(_tn.f("MOOD_MAX_DELTA")))
    check("Δ 噪音门槛 0.03", abs(float(_tn.f("MOOD_NOISE_DELTA")) - 0.03) < 1e-9,
          str(_tn.f("MOOD_NOISE_DELTA")))
    check("被动观察单次上限 0.03",
          abs(float(_tn.f("MOOD_PASSIVE_MAX_DELTA")) - 0.03) < 1e-9,
          str(_tn.f("MOOD_PASSIVE_MAX_DELTA")))
    check("非冲她来单次上限 0.05",
          abs(float(_tn.f("MOOD_ENV_MAX_DELTA")) - 0.05) < 1e-9,
          str(_tn.f("MOOD_ENV_MAX_DELTA")))
    # 重置：历史误判钉死的 -1 能清回 0
    _ms.reset("sat")
    check("心情可以重置回 0",
          abs(float(_ms.get_mood("sat").get("value") or 0.0)) < 1e-6,
          str(_ms.get_mood("sat").get("value")))
finally:
    os.chdir(_old_cwd)

# ---- 旁白/心理活动不能发出去（2026-11-22）----
_old_cwd = os.getcwd()
os.chdir(_tmp)
try:
    from services import aura_logic as _al
    _p1 = _al.parse_reply("（想了一会儿，还是不知道说啥好）")
    check("整条旁白 → 沉默", _p1.get("silent") and not _p1.get("items"),
          str(_p1.get("items")))
    _p2 = _al.parse_reply("（心里：有点烦）算了")
    _txt2 = "".join(str(i.get("value") or "") for i in _p2.get("items") or [])
    check("开头旁白被剥掉，留正文", _txt2 == "算了", _txt2)
    _p3 = _al.parse_reply("正常一句话（不是旁白）")
    _txt3 = "".join(str(i.get("value") or "") for i in _p3.get("items") or [])
    check("正常括号不误伤", _txt3 == "正常一句话（不是旁白）", _txt3)
    # 攻击性词：整句丢弃 / 换软，但不误伤「笑死」
    _p4 = _al.parse_reply("你去死吧")
    check("攻击性整句丢弃", not _p4.get("items"), str(_p4.get("items")))
    _p4b = _al.parse_reply("感冒还跑一千米，这不是找死吗")
    _txt4b = "".join(str(i.get("value") or "") for i in _p4b.get("items") or [])
    check("「找死」换软不丢整句", "太冒险了" in _txt4b, _txt4b)
    _p5 = _al.parse_reply("该死，忘了")
    _txt5 = "".join(str(i.get("value") or "") for i in _p5.get("items") or [])
    check("口癖词换软", _txt5 == "糟了，忘了", _txt5)
    _p6 = _al.parse_reply("笑死，这题我也会")
    _txt6 = "".join(str(i.get("value") or "") for i in _p6.get("items") or [])
    check("「笑死」不误伤", _txt6 == "笑死，这题我也会", _txt6)
    # 点评「谁跟谁说 / 系统怎么回」这类旁白不能发（2026-11-23）
    _p7 = _al.parse_reply(
        "串线这个是系统的问题，不是我乱接话；这几条本来就是分着回你们俩的")
    check("点评对话结构整句丢弃", not _p7.get("items"), str(_p7.get("items")))
    _p8 = _al.parse_reply("没招了，我就看着你们聊；上课去吧，晚点回来水")
    _txt8 = "".join(str(i.get("value") or "") for i in _p8.get("items") or [])
    check("只丢 meta 分句、保留正常内容",
          "上课去吧" in _txt8 and "看着你们" not in _txt8, _txt8)
    _p9 = _al.parse_reply("系统已经加上了")
    _txt9 = "".join(str(i.get("value") or "") for i in _p9.get("items") or [])
    check("正常「系统」不误伤", _txt9 == "系统已经加上了", _txt9)
    _p10 = _al.parse_reply("吃醋插件？那玩意又不是给我用的 ||| 你们这堆破事我也不想插嘴")
    _txt10 = "".join(str(i.get("value") or "") for i in _p10.get("items") or [])
    check("「你们这堆破事不想插嘴」被剥掉",
          "吃醋插件" in _txt10 and "破事" not in _txt10, _txt10)

    # ---- 识图描述 / 标记不能发出去（2026-11-23）----
    _m1 = _al.parse_reply(
        "【图片】一只淡蓝色的卡通仓鼠双臂交叉抱在胸前，咧着嘴，一脸得意的样子。"
        "|||这表情是在炫耀吗")
    _mt1 = [str(i.get("value") or "") for i in _m1.get("items") or []
            if i.get("kind") == "text"]
    check("画面描述整条丢弃，只留真正回复", _mt1 == ["这表情是在炫耀吗"], str(_mt1))
    _m2 = _al.parse_reply("【图片识别结果】啊……这个……")
    _mt2 = "".join(str(i.get("value") or "") for i in _m2.get("items") or [])
    check("识别标记不漏给用户",
          "图片识别结果" not in _mt2 and "啊……这个……" in _mt2, _mt2)
    _m3 = _al.parse_reply("图里那个人我认识")
    _mt3 = "".join(str(i.get("value") or "") for i in _m3.get("items") or [])
    check("正常提到图片不误伤", _mt3 == "图里那个人我认识", _mt3)

    # ---- 语音不许念出工具词 / 引用块不许照抄（2026-11-23）----
    check("语音 stick:开心 被清掉",
          _al.clean_voice_text("sticker：开心 今天好累") == "今天好累",
          _al.clean_voice_text("sticker：开心 今天好累"))
    _v1 = _al.parse_reply("<voice>好累啊 <sticker:开心></voice>")
    _vv1 = [i for i in _v1.get("items") or [] if i.get("kind") == "voice"]
    check("语音里的 <sticker:…> 不进 TTS",
          _vv1 and _vv1[0]["value"] == "好累啊", str(_v1.get("items")))
    _q1 = _al.parse_reply(
        "〔引用 阿明 12:00 的消息（只作指代，回复时不要复述原文）：今天吃了吗〕 吃了")
    _qt1 = "".join(str(i.get("value") or "") for i in _q1.get("items") or [])
    check("引用块不会被照抄进回复", _qt1 == "吃了", _qt1)
    _q2 = _al.parse_reply("今天吃了吗？吃了", quote_text="今天吃了吗")
    _qt2 = "".join(str(i.get("value") or "") for i in _q2.get("items") or [])
    check("开头照抄原文被剥掉", _qt2 == "吃了", _qt2)
    _q3 = _al.parse_reply("今天好累啊", quote_text="今天好累")
    _qt3 = "".join(str(i.get("value") or "") for i in _q3.get("items") or [])
    check("短引用不误伤正常口语", _qt3 == "今天好累啊", _qt3)
finally:
    os.chdir(_old_cwd)

# ---- 统一历史：处理过的历史（收发件人）供 判心情 / 上下文 / 翻历史 共用 ----
_old_cwd = os.getcwd()
os.chdir(_tmp)
try:
    from services import group_scene as _gs, mood_judge as _mj
    check("判读上下文 8 条（和主模型同源同条数）",
          int(_mj.CTX_LINES) == 8, str(_mj.CTX_LINES))
    _gid_h = "h_hist"
    _gs.clear(_gid_h)
    _gs.observe(_gid_h, mid="h1", qq="10001", name="阿明", text="在吗",
                to_qq="999", to_reason="at", bot_qq="999", names={})
    _gs.observe(_gid_h, mid="h2", qq="10002", name="小红", text="我先睡了",
                names={})
    _hl = _gs.history_lines(_gid_h, 8)
    check("统一历史带收发件人", any("阿明 → 你" in x for x in _hl)
          and all("→" in x for x in _hl), str(_hl))
    _sl = _mj.scene_lines(f"group_{_gid_h}", 8)
    check("判读上下文复用同一份历史", _sl == _hl[-len(_sl):] or set(_sl) <= set(_hl),
          str(_sl)[:120])
    # 当前这一段不能又在历史里出现（模板重复）
    _ex = _gs.history_lines(_gid_h, 8, exclude_mids={"h2"})
    check("历史排除当前段（按 mid）",
          all("我先睡了" not in x for x in _ex), str(_ex))
    _ex2 = _gs.history_lines(_gid_h, 8, exclude_texts={"10002|我先睡了"})
    check("历史排除当前段（按文本兜底）",
          all("我先睡了" not in x for x in _ex2), str(_ex2))
finally:
    os.chdir(_old_cwd)

# ---- Δ 入口护栏（2026-11-22：旁观不该被算成负分）----
_old_cwd = os.getcwd()
os.chdir(_tmp)
os.environ["MOOD_FILE"] = str(_tmp / "mood_guard.json")
os.environ["MOOD_THINK_FILE"] = str(_tmp / "think_guard.json")
try:
    import asyncio as _aio
    from services import mood_judge as _mj, mood as _mg

    _mj.MIN_INTERVAL = 0
    _mj.REFRESH = 0

    def _mk(d, cause):
        async def _f(words, ctx=None, **kw):
            return {"ok": True, "word": "x", "delta": d, "cause": cause,
                    "why": "w", "mind": "m", "src": "s", "at": time.time()}
        return _f

    _mj.judge = _mk(-0.07, "environment")
    _r = _aio.run(_mj.judge_and_store("g_guard", "", ["a"], force=True,
                                     passive=True, addressed=False))
    check("被动观察 Δ 封顶 0.03", abs(float(_r.get("delta")) + 0.03) < 1e-6,
          str(_r.get("delta")))
    _mj.judge = _mk(-0.02, "environment")
    _r = _aio.run(_mj.judge_and_store("g_guard", "", ["a"], force=True))
    check("Δ 噪音丢弃为 0", abs(float(_r.get("delta"))) < 1e-9, str(_r.get("delta")))
    _mj.judge = _mk(-0.18, "environment")
    _r = _aio.run(_mj.judge_and_store("g_guard", "", ["a"], force=True,
                                     addressed=False))
    check("不是冲她来 → 封顶 0.05", abs(float(_r.get("delta")) + 0.05) < 1e-6,
          str(_r.get("delta")))
    _mj.judge = _mk(0.18, "speaker")
    _r = _aio.run(_mj.judge_and_store("g_guard", "", ["a"], force=True,
                                     addressed=True))
    check("冲她来 → Δ 原样保留", abs(float(_r.get("delta")) - 0.18) < 1e-6,
          str(_r.get("delta")))
finally:
    os.chdir(_old_cwd)

# ---- 群心情（独立于整体心情，5 小时窗口，正负抵消）----
_old_cwd = os.getcwd()
os.chdir(_tmp)
try:
    from services import group_mood as _gm
    _gm.GROUP_MOOD_FILE = _tmp / "gm.json"
    _gm.record("group_1", -0.20, about_qq="10001", cause="speaker", why="a")
    _gm.record("group_1", -0.15, about_qq="10002", cause="speaker", why="b")
    _gm.record("group_1", -0.10, about_qq="10003", cause="speaker", why="c")
    _s = _gm.snapshot("group_1")
    check("群心情：3 人跌但没到 0.7 不整群",
          (not _s["group_quiet"]) and _s["neg_people"] == 3, str(_s["net"]))
    # 单条事件封顶 0.2：-0.30 只按 -0.20 记 → 净 -0.65（还没到 0.7）
    _gm.record("group_1", -0.30, about_qq="10004", cause="speaker", why="d")
    _s = _gm.snapshot("group_1")
    check("单条群心情事件封顶 0.2",
          abs(_s["net"] + 0.65) < 1e-6, str(_s["net"]))
    check("3~4 人但没到 0.7 → 不整群",
          not _s["group_quiet"], str(_s["net"]))
    _gm.record("group_1", -0.20, about_qq="10005", cause="speaker", why="d2")
    _s = _gm.snapshot("group_1")
    check("群净跌 ≥0.7 + 4 人 → 群聊不愉快",
          _s["group_quiet"] and _s["net"] <= -0.7, str(_s["net"]))
    _gm.record("group_1", 0.50, about_qq="10001", cause="speaker", why="e")
    _s = _gm.snapshot("group_1")
    check("群里的正值能抵消负值（+0.5 也封顶 0.2）",
          (not _s["group_quiet"]) and abs(_s["net"] + 0.65) < 1e-6, str(_s["net"]))
    # 单人：两条 -0.2 才到 0.4（单条 -0.45 只按 -0.2 记）
    _gm.record("group_1", -0.45, about_qq="10009", cause="speaker", why="f")
    _gm.record("group_1", -0.30, about_qq="10009", cause="speaker", why="f2")
    _s = _gm.snapshot("group_1")
    check("单人净跌 ≥0.4 → 单独不理他",
          any(p["qq"] == "10009" for p in _s["person_quiet"]), str(_s["person_quiet"]))
    check("单人净变化也封顶 ±1",
          all(abs(p["net"]) <= 1.0001 for p in _s["people"].values()),
          str(_s["people"]))
    check("群心情差 → 少说话乘数 < 1", _gm.chat_factor("group_1") < 1.0,
          str(_gm.chat_factor("group_1")))
    # 净变化封顶：连记 20 条 -0.2，net 也不能超过 -1
    for _i in range(20):
        _gm.record("group_1", -0.2, about_qq="10001", cause="speaker", why="x")
    _s = _gm.snapshot("group_1")
    check("群净变化封顶 ±1（原始求和保留）",
          _s["net"] >= -1.0001 and _s["net_raw"] <= -1.0,
          f"net={_s['net']} raw={_s['net_raw']}")
    # 5 小时前的事件必须过期（不参与净变化）
    _gm.record("group_1", -0.90, about_qq="10001", cause="speaker",
               why="old", at=time.time() - 20000)
    _s2 = _gm.snapshot("group_1")
    check("5 小时前的事件过期", abs(_s2["net"] - _s["net"]) < 1e-6, str(_s2["net"]))
    check("群心情状态文本可读", "喜欢比例" in _gm.status_text("group_1"))
finally:
    os.chdir(_old_cwd)

# ---- 群聊分段（等一段说完再回）----
_old_cwd = os.getcwd()
os.chdir(_tmp)
try:
    from services import segments as _sg
    _sg.push("group_seg", object(), "第一句", name="阿明", uid="10001",
             gid="1", mid="s1")
    _sg.set_addressee("group_seg", "s1", to_kind="person", to_name="小红",
                      to_conf=0.8)
    _sg.push("group_seg", object(), "第二句 @你", name="小红", uid="10002",
             mentioned=True, gid="1", mid="s2")
    _sg.set_addressee("group_seg", "s2", to_kind="bot", to_name="你", to_conf=0.99)
    _fmt = _sg.format(_sg.current("group_seg"))
    check("分段带收发件人（→ 某人 / → 你）",
          "阿明(10001) → 小红" in _fmt and "小红(10002) → 你" in _fmt,
          _fmt[-120:])
    check("分段带消息号 + 引用指引",
          "#s1" in _fmt and "<reply:" in _fmt, _fmt[:120])
    _recs = asyncio.run(
        _sg.wait_close("group_seg", wait_sec=0.05, max_sec=1, max_msgs=10))
    check("分段 wait_close 取走整段", len(_recs) == 2 and _sg.size("group_seg") == 0)
    _sg.add_processed("group_seg", _fmt, mood="有点高兴", value=0.4,
                      addressee="叫了她")
    check("处理过的段可回读", "有点高兴" in _sg.processed_text("group_seg", 1))
    _sg.push("group_seg", object(), "/命令不进段", name="阿明", uid="10001")
    check("指令不进聊天段", _sg.size("group_seg") == 0)
    # 群聊链接正文不能被 220 字截断（用户 2026-11-23：群聊做题看不到题目）
    _long = "题" * 1000
    _sg.push("group_link", object(),
             "看这个\n\n【链接内容 · 测试页】\n" + _long,
             name="老大", uid="10001", gid="1", mid="m-link")
    _fmt_link = _sg.format(_sg.current("group_link"))
    check("群聊链接正文不被截断", _long in _fmt_link, f"len={len(_fmt_link)}")
    # 2026-11-23：@ 她的消息不能被分段窗口挤掉，且必须明确要求回应
    _sg.push("group_at", object(), "普通闲聊", name="阿明", uid="10001",
             gid="1", mid="a0")
    _sg.push("group_at", object(), "我的今日小猪呢，刚刚怎么不理我",
             name="老大", uid="10009", mentioned=True, gid="1", mid="a1")
    for _i in range(25):
        _sg.push("group_at", object(), f"刷屏{_i}", name="路人", uid="10002",
                 gid="1", mid=f"b{_i}")
    _fmt_at = _sg.format(_sg.current("group_at"))
    check("分段 @ 消息标 ⭐", "⭐" in _fmt_at and "我的今日小猪呢" in _fmt_at,
          _fmt_at[-160:])
    check("分段超 20 条仍保留 @ 消息", "我的今日小猪呢" in _fmt_at)
    check("分段带「必须回应」清单", "必须回应" in _fmt_at)
    check("分段要求一条消息全部答完", "一条消息全部答完" in _fmt_at)
    check("分段截断时提示可翻 HISTORY", "被略" in _fmt_at and "HISTORY" in _fmt_at)
    # 历史行（主模型 8 条上下文）里 @ 她也要带 ⭐
    from services import group_scene as _gs
    _old_rm = _gs.recent_messages
    _gs.recent_messages = lambda gid, limit=20: [
        {"mid": "h1", "qq": "1", "name": "阿明", "text": "你们聊啥",
         "is_bot": False, "to_kind": "group"},
        {"mid": "h2", "qq": "2", "name": "小红", "text": "@你 今天吃啥",
         "is_bot": False, "to_kind": "bot", "to_name": "你", "to_conf": 0.99},
    ][-limit:]
    try:
        _hl = _gs.history_lines("g", 8)
    finally:
        _gs.recent_messages = _old_rm
    check("历史行 @ 她带 ⭐", any(x.startswith("⭐") for x in _hl), str(_hl))
finally:
    os.chdir(_old_cwd)

# ---- 用量报表：按模型拆开（judge / audit 两个三方 LLM 要看得到）----
llm_usage.record(purpose="judge", model="judge-model", prompt=100, completion=10)
llm_usage.record(purpose="audit", model="audit-model", prompt=200, completion=20)
_rep = llm_usage.report_text(1)
check("用量报表按模型拆开", "按模型" in _rep and "judge-model" in _rep
      and "audit-model" in _rep, _rep[-200:])

# ---- 用量图表：横向排行 + 扇形（2026-11-23）----
try:
    from services import usage_chart as _uc
    _fake = {"total": 100.0, "order": [
        ("chat", {"p": 90, "c": 0}), ("judge", {"p": 10, "c": 0})]}
    _its = _uc._pct_items(_fake)
    check("图表占比计算正确", _its == [("chat", 90.0), ("judge", 10.0)], str(_its))
    _old_snap = _uc.llm_usage.snapshot
    _uc.llm_usage.snapshot = lambda days: _fake
    try:
        _urls = _uc.chart_urls(1)
    finally:
        _uc.llm_usage.snapshot = _old_snap
    check("图表生成横向+扇形两张", len(_urls) == 2
          and "cht=bhg" in _urls[0] and "cht=p3" in _urls[1], str(_urls))
    check("图表带中文标题", "Token%20" in _urls[0] and "%E7%94%A8%E9%87%8F" in _urls[0],
          _urls[0][:160])
except Exception as _e:
    check("用量图表可导入", False, f"{type(_e).__name__}: {_e}")

# ---- 普通网页读取（2026-11-23）----
try:
    from services import web_page as _wp
    check("网页 URL 能提取",
          _wp.find_urls("看这个 https://example.com/a 好") == ["https://example.com/a"],
          str(_wp.find_urls("看这个 https://example.com/a 好")))
    check("B站/图片链接不抓",
          _wp.should_skip("https://b23.tv/x") and _wp.should_skip("https://a.com/x.png"))
    check("内网地址不抓",
          _wp.should_skip("http://127.0.0.1/x") and _wp.should_skip("http://192.168.1.2/x"))
    _h = _wp.html_to_text(
        "<html><title>T</title><h1>标题</h1><p>第一段</p><p>第二段</p>"
        "<script>alert(1)</script></html>")
    check("HTML 转文本保留段落",
          "标题" in _h and "第一段" in _h and "第二段" in _h and "alert" not in _h, _h)
except Exception as _e:
    check("网页读取可导入", False, f"{type(_e).__name__}: {_e}")

# ---- 哈希反查（2026-11-23）----
try:
    from services import hash_solver as _hs
    _target = "c0ad5411b19cfcba9d674d21411a970159f6ae4e180831ddd6a91797be547752"
    _r = _hs.solve(_target)
    check("SHA-256「我爱你」能反查到", _r.get("plain") == "我爱你", str(_r))
    check("哈希类型识别", _hs.detect(_target) == (_target, "sha256"), str(_hs.detect(_target)))
    check("普通文本不当哈希", _hs.detect("这不是哈希") == (None, None))
except Exception as _e:
    check("哈希反查可导入", False, f"{type(_e).__name__}: {_e}")

# ---- 白月光模式（2026-11-23，两层统计）----
try:
    from services import white_moon as _wm
    from services import expression as _ex
    _ex.EXP_DIR = _tmp / "expression"
    _ex.EXP_DIR.mkdir(parents=True, exist_ok=True)
    _ex._save("person_20002", [
        {"text": "笑不活了", "scene": "吐槽", "tone": "短", "count": 3, "at": time.time()},
        {"text": "这不科学", "scene": "惊讶", "tone": "软", "count": 2, "at": time.time()},
    ])
    # 阿明(10001) 最常 @ / 引用 小红(20002) → 阿明的白月光 = 小红
    _wm.note_incoming("g1", "10001", "阿明", "20002", "小红", kind="at")
    _wm.note_incoming("g1", "10001", "阿明", "20002", "小红", kind="at")
    _wm.note_incoming("g1", "10001", "阿明", "20002", "小红", kind="quote")
    # 小刚(10003) 分别 @ 小红 / 小美，群级看总数
    _wm.note_incoming("g1", "10003", "小刚", "20002", "小红", kind="at")
    _wm.note_incoming("g1", "10003", "小刚", "30003", "小美", kind="at")
    _pm = _wm.person_moon("g1", "10001")
    check("个人白月光：他 @/引用最多的人",
          _pm.get("qq") == "20002" and _pm.get("score") == 4, str(_pm))
    _gm = _wm.group_moon("g1")
    check("群白月光：被 @/引用最多的人",
          _gm.get("qq") == "20002", str(_gm))
    _blk = _wm.render("g1", "10001", "阿明")
    check("回阿明时用阿明白月光的风格",
          "阿明 的白月光" in _blk and "笑不活了" in _blk, _blk)
    _blk_all = _wm.render("g1", "", "")
    check("回大家时用群白月光风格",
          "这个群的白月光" in _blk_all and "笑不活了" in _blk_all, _blk_all)
    check("每个人档案带自己的白月光",
          "小红" in _wm.profile_note("10001", "g1"))
    check("不统计她本人 / @全体",
          _wm.person_moon("g1", "999") == {})
except Exception as _e:
    check("白月光可导入", False, f"{type(_e).__name__}: {_e}")

# ---- Prompt 分段 / 缓存体检（2026-11-23）----
try:
    from services import prompt_report as _pr
    check("prompt 段分类：心情", _pr._section("system", "▍现在的状态\n...") == "心情/状态")
    check("prompt 段分类：好感", _pr._section("system", "▍你对阿明的底色（长期）") == "好感底色")
    check("prompt 段分类：当前消息", _pr._section("user", "hi") == "当前消息")
    check("prompt 段分类：白月光", _pr._section("system", "▍白月光模式（...）") == "白月光")
    # 图表：横向柱状 + 扇形（和 /用量 同一套 image-charts 参数）
    import json as _json
    _td = _tmp / "traces"
    _td.mkdir(parents=True, exist_ok=True)
    (_td / "2099-01-01.jsonl").write_text(
        _json.dumps({"key": "group_1", "msgs": [
            {"role": "system", "content": "DeepSeek 小鲸鱼 · 角色卡\n一、你是谁\nxx"},
            {"role": "system", "content": "下面是几条规矩\n▍00 菜单\nxx"},
            {"role": "user", "content": "hi"},
        ]}, ensure_ascii=False) + "\n", encoding="utf-8")
    _old_td = _pr.TRACE_DIR
    _pr.TRACE_DIR = _td
    try:
        _purls = _pr.chart_urls("2099-01-01")
    finally:
        _pr.TRACE_DIR = _old_td
    check("prompt 图表生成横向+扇形",
          len(_purls) == 2 and "cht=bhg" in _purls[0] and "cht=p3" in _purls[1],
          str(_purls))
except Exception as _e:
    check("prompt 体检可导入", False, f"{type(_e).__name__}: {_e}")

# ---- 角色扮演 provider（2026-11-23：DeepSeek V4.1 Flash，三方仍 chat）----
try:
    from services import config as _cfg
    check("角色扮演 provider 走 DeepSeek",
          "deepseek" in _cfg.ROLEPLAY_BASE_URL.lower()
          and bool(_cfg.ROLEPLAY_MODEL),
          f"{_cfg.ROLEPLAY_BASE_URL} / {_cfg.ROLEPLAY_MODEL}")
    if os.getenv("XAI_BASE_URL"):
        check("三方判读仍是 DeepSeek chat",
              "deepseek" in _cfg.XAI_BASE_URL.lower()
              and _cfg.XAI_MODEL == "deepseek-chat",
              f"{_cfg.XAI_BASE_URL} / {_cfg.XAI_MODEL}")
except Exception as _e:
    check("角色扮演 provider 可导入", False, f"{type(_e).__name__}: {_e}")

# ---- 人格 ID：换号不丢记忆（2026-11-23）----
_old_cwd_id = os.getcwd()
os.chdir(_tmp)
try:
    import json as _json2
    from services import identity as _idn

    # 造一份「换号前」的对话日志：旧号 100000018 说过话（kind=bot）
    _dd = Path("data/dialogue")
    _dd.mkdir(parents=True, exist_ok=True)
    (_dd / "2026-11-20.jsonl").write_text(
        _json2.dumps({"ts": 1, "gid": "g1", "who": "你", "qq": "100000018",
                      "kind": "bot", "text": "旧号说过的话"},
                     ensure_ascii=False) + "\n", encoding="utf-8")

    _old_env = {k: os.environ.get(k) for k in
                ("NAPCAT_ACCOUNT", "SELF_ID", "SELF_ALIASES",
                 "SELF_ID_FILE", "SELF_ID_SCAN")}
    os.environ["NAPCAT_ACCOUNT"] = "100000010"
    os.environ.pop("SELF_ID", None)
    os.environ["SELF_ALIASES"] = "555555"
    os.environ["SELF_ID_FILE"] = str(Path("data/self_id.json"))
    os.environ["SELF_ID_SCAN"] = "true"
    _idn.reset()
    check("人格 ID 取 NAPCAT_ACCOUNT", _idn.canonical() == "100000010",
          _idn.canonical())
    check("旧号自动进别名（扫对话日志）",
          {"100000010", "100000018"} <= _idn.accounts(), str(_idn.accounts()))
    check("旧号也算她自己", _idn.is_self("100000018") and _idn.is_self("100000010"))
    check("SELF_ALIASES 配的旧号也算她自己", _idn.is_self("555555"))
    check("别人不算她自己", not _idn.is_self("100000003"))
    check("别名归一成人格 ID", _idn.canon("100000018") == "100000010")
    _idn.note_account("123456")
    check("新登录号并入同一人格",
          "123456" in _idn.accounts() and _idn.canonical() == "100000010")
    _idn.reset()
    check("落盘后重读人格 ID 不变", _idn.canonical() == "100000010")

    # 群聊现场：旧号发的言仍记成「她」，@ 旧号也算「→ 你」
    from services import group_scene as _gs2
    _gs2.clear("gid_swap")
    _gs2.observe("gid_swap", mid="m1", qq="100000018", name="你",
                 text="我之前说的", is_bot=True, bot_qq="100000010")
    _row = [m for m in _gs2.recent_messages("gid_swap", 5)
            if m.get("mid") == "m1"][0]
    check("旧号发言存成人格 ID", _row.get("qq") == "100000010", str(_row))
    _meta = _gs2.observe("gid_swap", mid="m2", qq="10001", name="阿明",
                         text="@100000018 你在吗", to_qq="100000018",
                         to_reason="at", bot_qq="100000010")
    check("@ 旧号判成「→ 你」", _meta.get("to_kind") == "bot"
          and _meta.get("to_qq") == "100000010", str(_meta))

    for _k, _v in _old_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
    _idn.reset()
except Exception as _e:
    check("人格 ID 可导入", False, f"{type(_e).__name__}: {_e}")
finally:
    os.chdir(_old_cwd_id)

# ---- 戳一戳次数屏蔽（2026-11-23）----
try:
    from services import poke_guard as _pguard
    _pguard.reset()
    _t0 = 2_000_000.0
    _rs = [_pguard.note("u_test", now=_t0 + i) for i in range(3)]
    check("戳一戳：前 3 次允许回复",
          all(_ok for _ok, _ in _rs) and [_n for _, _n in _rs] == [1, 2, 3], str(_rs))
    _ok4, _n4 = _pguard.note("u_test", now=_t0 + 10)
    check("戳一戳：2 小时内第 4 次屏蔽", (not _ok4) and _n4 == 4, str((_ok4, _n4)))
    _ok5, _n5 = _pguard.note("u_test", now=_t0 + _pguard.WINDOW_SEC + 11)
    check("戳一戳：窗口滑过后恢复", _ok5 and _n5 == 1, str((_ok5, _n5)))
    _ok_other, _ = _pguard.note("u_other", now=_t0 + 11)
    check("戳一戳：按用户独立计数", _ok_other is True)
    _pguard.reset()
except Exception as _e:
    check("戳一戳屏蔽可导入", False, f"{type(_e).__name__}: {_e}")

# ---- 算力机开关 / Harness 桥（2026-11-24）----
try:
    from services import config as _cfg2
    check("NODE_COMPUTE 默认开", _cfg2.NODE_COMPUTE is True)
    check("CODE_AGENT 默认 self_tool", _cfg2.CODE_AGENT == "self_tool", _cfg2.CODE_AGENT)
except Exception as _e:
    check("NODE_COMPUTE/CODE_AGENT 可读", False, f"{type(_e).__name__}: {_e}")

_old_cwd_h = os.getcwd()
os.chdir(_tmp)
_henv = {k: os.environ.get(k) for k in
         ("HARNESS_STATE", "HARNESS_BACKEND", "HARNESS_CLI", "HARNESS_CONTEXT",
          "TEST_TOKEN")}
try:
    os.environ["HARNESS_STATE"] = str(Path("data/harness.json"))
    os.environ["HARNESS_BACKEND"] = "cli"
    os.environ["HARNESS_CLI"] = "echo {prompt}"
    os.environ["HARNESS_CONTEXT"] = "0"
    os.environ["TEST_TOKEN"] = "abcdefgh12345678"
    from services import harness as _hs

    _hs.reset("private_1")
    _st = _hs.session_of("private_1")
    check("harness 会话有 id/cwd", bool(_st.get("id")) and bool(_st.get("cwd")), str(_st))
    _out = asyncio.run(_hs.ask("private_1", "改一下 README"))
    check("harness CLI 后端能返回结果", "改一下 README" in _out, _out[:120])
    _st2 = _hs.session_of("private_1")
    check("harness 会话复用", _st2.get("id") == _st.get("id"))
    _old = _st2.get("id")
    _st3 = _hs.reset("private_1")
    check("harness 重置换新会话", _st3.get("id") != _old)
    check("harness 输出脱敏", "[REDACTED]" in _hs.redact("token=abcdefgh12345678"))
    check("harness status 可用", "Harness 桥" in _hs.status("private_1"))
except Exception as _e:
    check("harness 可导入/可调用", False, f"{type(_e).__name__}: {_e}")
finally:
    for _k, _v in _henv.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
    os.chdir(_old_cwd_h)

print("-" * 60)
# ---- 模型自由搭配（2026-11-24）----
try:
    from services import providers as _pv
    # ⚠️ .env 里可能显式写了 ROLEPLAY_BASE_URL / VISION_BASE_URL，
    #    它们优先级最高，会把 provider 默认值盖掉 → 测试前先摘掉，结束再还回去。
    _saved_env = {}
    for _k in ("ROLEPLAY_BASE_URL", "ROLEPLAY_API_KEY", "ROLEPLAY_MODEL",
               "VISION_BASE_URL", "VISION_API_KEY", "VISION_MODEL",
               "VISION_PROVIDER"):
        if _k in os.environ:
            _saved_env[_k] = os.environ.pop(_k)
    _pv.reset()
    _rp = _pv.resolve("roleplay")
    check("providers：roleplay 默认 deepseek",
          "deepseek" in _rp["base_url"], str(_rp))
    check("providers：roleplay 有模型名", bool(_rp["model"]), _rp["model"])

    os.environ["ROLEPLAY_PROVIDER"] = "glm"
    os.environ["ROLEPLAY_MODEL"] = "glm-5.3-flash"
    _pv.reset()
    _rp2 = _pv.resolve("roleplay")
    check("providers：主聊天可切 GLM",
          "bigmodel" in _rp2["base_url"] and _rp2["model"] == "glm-5.3-flash",
          str(_rp2))

    os.environ["PROVIDER_MY_BASE_URL"] = "https://proxy.example/v1"
    os.environ["PROVIDER_MY_API_KEY"] = "[REDACTED_SECRET]"
    os.environ["PROVIDER_MY_MODEL"] = "my-model"
    os.environ["VISION_PROVIDER"] = "my"
    _pv.reset()
    _v = _pv.resolve("vision")
    check("providers：自定义中转站",
          _v["base_url"].endswith("/v1") and _v["model"] == "my-model"
          and _v["api_key"] == "[REDACTED_SECRET]", str(_v))
    check("providers：chat_url 拼对",
          _pv.chat_url("vision").endswith("/chat/completions"),
          _pv.chat_url("vision"))
    check("providers：status 每个角色都有",
          all(k in _pv.status() for k in ("roleplay", "judge", "audit", "vision")))

    os.environ["VISION_PROVIDER"] = "deepseek"
    os.environ["VISION_MODEL"] = "deepseek-flash"
    _pv.reset()
    _dv = _pv.resolve("vision")
    check("providers：识图可切 DeepSeek Flash",
          "deepseek" in _dv["base_url"] and _dv["model"] == "deepseek-flash",
          str(_dv))
    for _k in ("ROLEPLAY_PROVIDER", "ROLEPLAY_MODEL", "PROVIDER_MY_BASE_URL",
               "PROVIDER_MY_API_KEY", "PROVIDER_MY_MODEL", "VISION_PROVIDER",
               "VISION_MODEL"):
        os.environ.pop(_k, None)
    os.environ.update(_saved_env)
    _pv.reset()
except Exception as _e:
    check("providers 可导入", False, f"{type(_e).__name__}: {_e}")

# ---- 省流模式 / 精确引用（2026-11-24）----
try:
    from services import config as _cfg2
    check("群聊观察模式可选", _cfg2.GROUP_WATCH_MODE in ("full", "lite"),
          _cfg2.GROUP_WATCH_MODE)
    _chat_src = (ROOT / "plugins" / "chat" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    check("省流模式：硬信号才入队",
          'GROUP_WATCH_MODE == "lite"' in _chat_src
          and "硬信号命中" in _chat_src)
    check("省流模式：不主动插话",
          'GROUP_WATCH_MODE != "lite"' in _chat_src)
    check("引用按消息号校验（防引错人）",
          "_valid_mids" in _chat_src and "不在这一段里" in _chat_src)

    # 省流硬信号：直接调函数，别只做静态检查
    # ⚠️ 本地没装 nonebot 时 import 会失败 → 跳过，别算失败（容器里会跑）
    try:
        import plugins.chat as _C
    except Exception as _e:
        _C = None
        print(f"SKIP 省流硬信号（{type(_e).__name__}: {_e}）")

    if _C is not None:
        class _FakeEv:
            def __init__(self, tome=False):
                self._t = tome

            def is_tome(self):
                return self._t

        check("省流：@ 她是硬信号",
              _C._lite_hard_signal(_FakeEv(True), "1", "9") is True)
        check("省流：闲聊不是硬信号",
              _C._lite_hard_signal(_FakeEv(False), "1", "9", text="今天好热") is False)
        check("省流：收件人是她也算",
              _C._lite_hard_signal(_FakeEv(False), "1", "9",
                                   scene_meta={"to_kind": "bot"}) is True)

    from services import aura_logic as _al
    check("〔〕协议残留会被清掉",
          _al._QUOTE_ECHO_RE.sub("", "〔引〕你好〔引用 某人 12:00 的消息〕") == "你好",
          _al._QUOTE_ECHO_RE.sub("", "〔引〕你好〔引用 某人 12:00 的消息〕"))
except Exception as _e:
    check("省流/引用可导入", False, f"{type(_e).__name__}: {_e}")

print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败：" + "、".join(FAIL))
sys.exit(1 if FAIL else 0)

