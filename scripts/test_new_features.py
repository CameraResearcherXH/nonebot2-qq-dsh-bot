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
    # P2 起半衰期按作用域取（群 30min / 私聊 2h / 全局 6h），不再固定 12h：
    #   50 先按 950s 衰减，再按 5 分钟 2 步走 -2。期望值从 tuning 现算，别写死。
    _hl_h = float(_mood._HL_MIN[_mood._scope_of("idle")]) / 60.0
    _exp = 50.0 * (0.5 ** (950.0 / 3600.0 / _hl_h)) - 2.0
    check("沉默后 touch 先结算自减（2 步）", abs(_after - _exp) < 1.0,
          f"{_after} vs {_exp:.2f}")
finally:
    os.chdir(_old_cwd)

# ---- 心情口径：设计 §3.1（clamp(v+Δ)）+ Δ 入口护栏参数（2026-11-22）----
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
    # ADR-038：没 THINK 就不改心情，被动观察通道已删除。
    # 旧测试还在断言 MOOD_PASSIVE_MAX_DELTA=0.03，会随着键一起消失而假失败。
    check("被动观察通道已删除（无 MOOD_PASSIVE_MAX_DELTA）",
          "MOOD_PASSIVE_MAX_DELTA" not in _tn.DEFAULTS,
          "DEFAULTS 里已无此键")
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
    _v0 = float(_mg.get_mood("g_guard").get("value") or 0.0) / 100.0
    _r = _aio.run(_mj.judge_and_store("g_guard", "", ["a"], force=True,
                                     passive=True, addressed=False))
    _v1 = float(_mg.get_mood("g_guard").get("value") or 0.0) / 100.0
    # 2026-11-25：旁观窗口没有她的 THINK → 不改心情（以前是封顶 ±0.03）
    check("旁观窗口（无 THINK）不写心情",
          _r.get("passive") is True and abs(_v1 - _v0) < 1e-4,
          f"v {_v0} → {_v1}, passive={_r.get('passive')}")
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

    # 2026-11-25 ADR-038：没有她自己的话/THINK，即使是 force 也不写状态
    _v2 = float(_mg.get_mood("g_guard2").get("value") or 0.0) / 100.0
    _mj.judge = _mk(0.18, "speaker")
    _r = _aio.run(_mj.judge_and_store("g_guard2", "", ["a"], force=True,
                                     addressed=True))
    _v3 = float(_mg.get_mood("g_guard2").get("value") or 0.0) / 100.0
    check("无 THINK 的窗口不写心情（force 也不行）",
          _r.get("passive") is True and abs(_v3 - _v2) < 1e-4,
          f"v {_v2} → {_v3}, passive={_r.get('passive')}")

    # 好感必须**双向**：负向事件要能把它拉下来，正向要能拉上去
    from services import profiles as _pf2
    _pf2.bump_affinity("7770001", 0.06, reason="setup")
    _a0 = _pf2.get_affinity("7770001")
    _mj.judge = _mk(-0.22, "speaker")
    _aio.run(_mj.judge_and_store("g_aff", "", ["a"], think="被噎得有点闷",
                                 force=True, speaker_qq="7770001",
                                 addressed=True))
    _a1 = _pf2.get_affinity("7770001")
    check("好感能降（负向事件）", _a1 < _a0 - 1e-6, f"{_a0} → {_a1}")
    _mj.judge = _mk(0.22, "speaker")
    _aio.run(_mj.judge_and_store("g_aff", "", ["a"], think="被夸得有点飘",
                                 force=True, speaker_qq="7770001",
                                 addressed=True))
    _a2 = _pf2.get_affinity("7770001")
    check("好感能涨（正向事件）", _a2 > _a1 + 1e-6, f"{_a1} → {_a2}")
    check("好感单次步长对称（±0.10 内）",
          abs(_a1 - _a0) <= 0.1001 and abs(_a2 - _a1) <= 0.1001,
          f"{_a0}→{_a1}→{_a2}")
finally:
    os.chdir(_old_cwd)

# ---- 图书馆触发门槛（2026-11-25：闲聊不该每轮都花 5~14s 查 RAG）----
try:
    from services import reply_pipeline as _rp
    check("图书馆：闲聊不查", not _rp._want_library("今天这鸡蛋灌饼也太贵了吧"))
    check("图书馆：提旧事要查", _rp._want_library("你还记得上次那个事吗"))
    check("图书馆：真问句要查", _rp._want_library("那个东西多少钱？"))
except Exception as _e:
    check("图书馆触发门槛", False, f"{type(_e).__name__}: {_e}")

# ---- 气泡上限两处必须一致（2026-11-25，P3-6）----
try:
    from core import tuning as _tn
    check("气泡上限两处一致（MAX_BUBBLES == OUT_BUBBLES_MAX）",
          _tn.i("MAX_BUBBLES") == _tn.i("OUT_BUBBLES_MAX"),
          f"{_tn.i('MAX_BUBBLES')} vs {_tn.i('OUT_BUBBLES_MAX')}")
except Exception as _e:
    check("气泡上限两处一致", False, f"{type(_e).__name__}: {_e}")

# ---- P2 状态机 synthetic 序列（2026-11-25）：Q / 单调 / 步长递减 ----
# 这条不依赖真实流量，能直接抓「Q 恒 0」「K 不递减」。
_old_cwd = os.getcwd()
os.chdir(_tmp)
os.environ["MOOD_FILE"] = str(_tmp / "mood_seq.json")
try:
    from services import mood as _ms

    try:
        _ms.reset("group_seq")
    except Exception:
        pass
    _vs = []
    for _i in range(5):
        _vs.append(float(_ms.observe("group_seq", 1, 0.8)["v"]))
    check("M1 同向 5 次单调递增",
          all(_vs[i + 1] > _vs[i] for i in range(4)), str(_vs))

    # 静默 6h：P 必须按 Q 回升（以前 dt_h 恒 0，这条会红）
    _st = _ms._load()
    _rec = _st["sessions"].get("group_seq") or {}
    _rec["P"] = 0.01
    _rec["updated_at"] = time.time() - 6 * 3600
    _st["sessions"]["group_seq"] = _rec
    _ms._save(_st)
    _r5 = _ms.observe("group_seq", 0, 0.8)
    check("M5 静默 6h 后 P 回升（Q 生效）",
          float(_r5.get("P_prior") or 0) > 0.2, str(_r5.get("P_prior")))

    try:
        _ms.reset("group_seq2")
    except Exception:
        pass
    _d1 = abs(float(_ms.observe("group_seq2", 1, 0.8)["dv"]))
    _d2 = abs(float(_ms.observe("group_seq2", 1, 0.8)["dv"]))
    check("M10 同档第二次位移更小（K 递减）", _d2 <= _d1 + 1e-9,
          f"{_d1:.4f} → {_d2:.4f}")
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
    # 默认 borrow=0：只给「关系参照」1 行事实，不借句式（style §4.3）
    _blk0 = _wm.render("g1", "10001", "阿明")
    check("白月光默认只给事实行",
          "不借句式" in _blk0 and "笑不活了" not in _blk0, _blk0)
    # 显式打开借风格：仍要带比例，且受字数上限约束
    _wm.BORROW = 1
    _wm.BORROW_CHARS = 600
    _blk = _wm.render("g1", "10001", "阿明")
    check("回阿明时借阿明白月光的风格",
          "阿明 的白月光" in _blk and "笑不活了" in _blk
          and "30%" in _blk, _blk)
    _blk_all = _wm.render("g1", "", "")
    check("回大家时借群白月光风格",
          "这个群的白月光" in _blk_all and "笑不活了" in _blk_all, _blk_all)
    _old_expr = _wm.EXPRESSIVENESS
    _wm.EXPRESSIVENESS = 0.2
    _cold = _wm._borrow_ratio()
    _wm.EXPRESSIVENESS = _old_expr
    check("冷漠人设把借风格压到 0.2", _cold <= 0.2 + 1e-9, str(_cold))
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


# ---- 记忆召回 / 插队 / 唱歌去重（2026-11-24 第四轮）----
try:
    from services import prompt_builder as _pb
    _q = _pb._recall_query(
        "（这一段群聊共 2 条，按时间顺序，最后一条最新。\n"
        "[1] ⭐#123 阿明(1001) → 你：上次那个蛋糕是哪家的\n"
        "[2] #124 小红(1002) → 大家：我也想吃")
    check("召回 query 去掉了行号/⭐/名字前缀",
          "上次那个蛋糕是哪家的" in _q and "[1]" not in _q and "⭐" not in _q,
          _q)

    from services import memory_v2 as _mv
    check("记忆有「关于这个人最近的事」兜底",
          hasattr(_mv, "recent_about")
          and isinstance(_mv.recent_about("100000000", limit=2), list))

    from services import group_scene as _gs
    import inspect as _insp
    _sig = _insp.signature(_gs.history_lines)
    check("历史块支持标记〔已回〕", "replied_mids" in _sig.parameters)

    from services import segments as _sg
    check("分段支持 @ 插队标记", hasattr(_sg, "_URGENT"))
except Exception as _e:
    check("记忆/插队可导入", False, f"{type(_e).__name__}: {_e}")

try:
    _chat_src2 = (ROOT / "plugins" / "chat" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    check("唱歌有在途/近期去重",
          "_SING_RECENT" in _chat_src2 and "别重复点歌" in _chat_src2)
    check("@ 消息跳过回复间隔（插队）",
          "[URGENT]" in _chat_src2 and "_is_at" in _chat_src2)
except Exception as _e:
    check("插队/唱歌可读源码", False, f"{type(_e).__name__}: {_e}")


# ---- 图书馆路由 / 原图 / 调用统计（2026-11-24 第五轮）----
try:
    from services import agent_client as _ac
except Exception as _e:
    # 本地可能没装 aiohttp（容器里有）→ 跳过，别算失败
    print(f"SKIP 图书馆/识图（{type(_e).__name__}: {_e}）")
    _ac = None

if _ac is not None:
    check("图书馆：宋词语料会被判成「不是聊天」",
          _ac._looks_like_chat({"answer": "参考来源: /app/documents/poet.song.rank.1.json"})
          is False)
    check("图书馆：聊天语料放行",
          _ac._looks_like_chat({"answer": "阿明: 上次那个蛋糕是街角那家"}) is True)
    check("图书馆：通用问答不当记忆",
          _ac._looks_like_chat({"answer": "白饭是指煮熟的白色稻米。"}) is False)
    check("图书馆：有 smart-query 兜底路由",
          _ac.SMART_QUERY_ENDPOINT.endswith("/api/agent/smart-query"))
    check("图书馆：统计可读", "图书馆" in _ac.stats_text(), _ac.stats_text())
    check("图书馆：recall 404 只试一次", hasattr(_ac, "_RECALL_ENDPOINT_MISSING"))

    from services import prompt_builder as _pb3
    check("记忆召回统计可读", "记忆召回" in _pb3.recall_stats_text())

    _vis_src = (ROOT / "plugins" / "vision_agent" / "__init__.py").read_text(
        encoding="utf-8", errors="ignore")
    check("识图优先原图（多源取最大）",
          "优先原图" in _vis_src and "max(cands" in _vis_src)
    check("识图会报告「拿到的可能还是压缩图」",
          "可能还是压缩图" in _vis_src)


# ---- 称呼统一入口 / 动态流水线（2026-11-24 第六轮）----
try:
    from services import callname as _cn
    from pathlib import Path as _P

    check("称呼：我叫你X = 她叫他",
          ("user", "阿明") in _cn.parse_direction("我叫你阿明"),
          str(_cn.parse_direction("我叫你阿明")))
    check("称呼：你叫我X = 他叫她",
          ("bot", "小鱼") in _cn.parse_direction("你叫我小鱼"),
          str(_cn.parse_direction("你叫我小鱼")))
    check("称呼：一句话两个方向都能抽",
          set(_cn.parse_direction("我叫你阿明，你叫我小鱼")) ==
          {("user", "阿明"), ("bot", "小鱼")},
          str(_cn.parse_direction("我叫你阿明，你叫我小鱼")))

    _bot, _user = _cn.normalize_pair("阿明", "小鱼", speaker_name="阿明",
                                     bot_names={"小鱼", "小鲸鱼"})
    check("称呼：模型写反了会自动换回来",
          (_bot, _user) == ("小鱼", "阿明"), f"{_bot}/{_user}")
    _bot2, _user2 = _cn.normalize_pair("小鱼", "阿明", speaker_name="阿明",
                                       bot_names={"小鱼", "小鲸鱼"})
    check("称呼：写对了不动", (_bot2, _user2) == ("小鱼", "阿明"), f"{_bot2}/{_user2}")

    _old = _cn.FILE
    _cn.FILE = _P(_tmp) / "callname.json"
    _cn.reset()
    _cn.set_bot_call("10001", "小鱼", "g1")
    _cn.set_user_call("10001", "阿明", "g1")
    check("称呼：群级索引能读回", "小鱼" in _cn.bot_names("g1"), str(_cn.bot_names("g1")))
    check("称呼：喊这个名字能认出是谁起的名",
          _cn.who_calls_me("小鱼你在吗", "g1") == "10001",
          _cn.who_calls_me("小鱼你在吗", "g1"))
    check("称呼：她叫他读回", _cn.user_call_of("10001", "g1") == "阿明",
          _cn.user_call_of("10001", "g1"))
    check("称呼：别的群不串", "小鱼" not in _cn.bot_names("g2"))
    _cn.FILE = _old
    _cn.reset()

    from services import reply_pipeline as _rp
    check("流水线：统计可读", "流水线" in _rp.stats_text(), _rp.stats_text())
except Exception as _e:
    check("称呼/流水线可导入", False, f"{type(_e).__name__}: {_e}")

# ---- 判读窗口（P4：段内消息不能丢）----
try:
    from services import mood_judge as _mj
    _recs = [{"mid": str(1000 + i), "ts": 1000.0 + i * 10, "text": "m%d" % i}
             for i in range(40)]
    _wins = _mj.split_windows(_recs, size=8, max_sec=300)
    _mids = [str(r["mid"]) for w in _wins for r in w]
    check("窗口：40 条切成 5 窗", len(_wins) == 5, str(len(_wins)))
    check("窗口：段内消息全覆盖且不重复",
          _mids == [r["mid"] for r in _recs], str(len(_mids)))
    _slow = [{"mid": str(i), "ts": i * 200.0} for i in range(4)]   # 跨 600s
    _w2 = _mj.split_windows(_slow, size=8, max_sec=300)
    check("窗口：慢群按 5min 先到先判", len(_w2) >= 2, str(len(_w2)))
except Exception as _e:
    check("窗口可导入", False, "%s: %s" % (type(_e).__name__, _e))

# ---- a/d（唤醒/掌控）：置信加权 + 独立半衰期 ----
try:
    import services.mood as _M
    _oldf = _M.MOOD_FILE
    _M.MOOD_FILE = _tmp / "mood_ad.json"
    _M.update_ad("group_t", 0.8, 0.8, confidence=0.2)
    _lo = _M.ad_of("group_t")["a"] - _M.AD_BASE_A
    _M.MOOD_FILE.unlink()
    _M.update_ad("group_t", 0.8, 0.8, confidence=0.9)
    _hi = _M.ad_of("group_t")["a"] - _M.AD_BASE_A
    check("a/d：低置信移动 ≤ 高置信的一半", _lo <= 0.5 * _hi + 1e-9,
          "%.3f/%.3f" % (_lo, _hi))
    check("a/d：高置信确实移动了", _hi > 0.1, "%.3f" % _hi)
    _M.MOOD_FILE = _oldf
except Exception as _e:
    check("a/d 可导入", False, "%s: %s" % (type(_e).__name__, _e))

# ---- 判读预算（每会话每小时上限）----
try:
    from services import mood_judge as _mj2
    _mj2._CALLS.clear()
    _now = time.time()
    check("判读预算：初始 = 上限",
          _mj2._calls_left("budget_t", _now) == _mj2.JUDGE_MAX_PER_HOUR,
          str(_mj2._calls_left("budget_t", _now)))
    _mj2._CALLS["budget_t"] = [_now] * _mj2.JUDGE_MAX_PER_HOUR
    check("判读预算：用完 = 0", _mj2._calls_left("budget_t", _now) == 0)
    _mj2._CALLS["budget_t"] = [_now - 4000] * 50      # 全是 1 小时前
    check("判读预算：过期不占额度",
          _mj2._calls_left("budget_t", _now) == _mj2.JUDGE_MAX_PER_HOUR)
    _mj2._CALLS.clear()
except Exception as _e:
    check("判读预算可导入", False, "%s: %s" % (type(_e).__name__, _e))

# ---- P4 检索规划器（结构化过滤在前，打分在后）----
try:
    _a = memdb.remember(text="老大今天夸我唱歌好听", kind="event",
                        about_qq="20001", gid="p4g", delta=0.2, salience=0.6)
    _b = memdb.remember(text="老大昨天骂我笨死了", kind="event",
                        about_qq="20001", gid="p4g", delta=-0.3, salience=0.7)
    _c = memdb.remember(text="小明说他要去上海出差", kind="event",
                        about_qq="20002", gid="p4g", delta=0.0)
    check("P4：新记忆自动打标签",
          memdb.stats()["tag_coverage"] >= 0.99, str(memdb.stats()["tag_coverage"]))
    _neg = memdb.recall_planned("老大", direction="neg", limit=5, mark_hit=False)
    check("P4：情绪方向是过滤不是加分",
          bool(_neg) and all(float(h["delta"] or 0) < 0 for h in _neg),
          str(len(_neg)))
    _per = memdb.recall_planned("", tag_values=["20002"], limit=5, mark_hit=False)
    check("P4：标签过滤",
          bool(_per) and all(h["about_qq"] == "20002" for h in _per))
    _hard = memdb.recall_planned("", speaker_qq="20001", person_hard=True,
                                 limit=5, mark_hit=False)
    check("P4：人过滤",
          bool(_hard) and all(h["about_qq"] in ("20001", "") for h in _hard))
    _q = memdb.recall_planned("上海", limit=1, mark_hit=False)
    check("P4：关键词命中", bool(_q) and "上海" in _q[0]["summary"])
    _r0 = memdb._conn().execute(
        "select retrieval_strength from memories where id=?", (_c,)).fetchone()[0]
    memdb.recall_planned("上海", limit=1, mark_hit=True)
    _r1 = memdb._conn().execute(
        "select retrieval_strength from memories where id=?", (_c,)).fetchone()[0]
    check("P4：命中回补提取强度", _r1 > _r0, "%s->%s" % (_r0, _r1))
    memdb.upsert_entity(qq="20001", name="老大", aliases=["老大"])
    check("P4：实体反查", memdb.resolve_person("老大") == "20001",
          memdb.resolve_person("老大"))
    memdb.log_mood_event(about_qq="20001", dv=-0.2)
    check("P4：心情事件可查",
          len(memdb.mood_events_of("20001", direction="-")) >= 1)
    _bt = memdb.backfill_tags(force=True)
    check("P4：补标签幂等", _bt["coverage"] >= 0.99, str(_bt))
except Exception as _e:
    check("P4 规划器可导入", False, "%s: %s" % (type(_e).__name__, _e))

# ---- P2 状态机 / P3 关系四量（2026-11-25）----
_old_cwd = os.getcwd()
_p2dir = tempfile.mkdtemp(prefix="p2_")
os.chdir(_p2dir)
os.environ["MOOD_FILE"] = os.path.join(_p2dir, "mood.json")
os.environ["REL_FILE"] = os.path.join(_p2dir, "rel.json")
try:
    import importlib
    from services import mood as _m2
    importlib.reload(_m2)
    try:
        _m2.MOOD_FILE.unlink()
    except Exception:
        pass
    _o1 = _m2.observe("group_1", 1, 0.9)
    check("P2：impact+1 高置信步进 ≈0.08~0.12", 0.07 <= _o1["v"] <= 0.13, str(_o1))
    _o2 = _m2.observe("group_1", 0, 0.9)
    check("P2：impact=0 不动", abs(_o2["v"] - _o1["v"]) < 1e-6, str(_o2))
    _o3 = _m2.observe("group_1", 1, 0.2)
    check("P2：低置信单次位移更小", abs(_o3["dv"]) < abs(_o1["dv"]),
          f"{_o3['dv']} vs {_o1['dv']}")
    _m2.reset("group_1")
    _m2.observe("group_1", 2, 0.9)
    _mv = _m2.anchor("group_1", 2, 0.9, baseline=0.0)
    check("P2：锚点向上纠偏", _mv > 0, str(_mv))
    _m2.reset("group_1")
    _m2.observe("group_1", 2, 0.9)
    _pv = _m2.state_of("group_1")["P"]
    _fv = _m2.fast_guard("group_1", light=False)
    _st2 = _m2.state_of("group_1")
    check("P2：快速护栏只降不升且抬 P",
          _fv <= -0.44 and _st2["P"] > _pv,
          f"{_fv}/{_pv}->{_st2['P']}")
    from services import relationship as _rl
    _rl.REL_FILE = _rl.Path(os.path.join(_p2dir, "rel2.json"))
    _t0 = _rl.state_of("20001")["trust"]
    _t1 = _rl.bump("20001", "trust", 1, reason="说到做到", relevance=1.0)
    check("P3：trust 正向步长", _t1 > _t0, f"{_t0}->{_t1}")
    _t2 = _rl.bump("20001", "trust", -1, reason="失信", relevance=1.0)
    check("P3：trust 负向步长更大", (_t1 - _t2) > (_t1 - _t0), f"{_t1}->{_t2}")
    check("P3：关系四量可读",
          set(_rl.state_of("20001")) == {"affinity", "trust", "respect", "intimacy"})
except Exception as _e:
    check("P2/P3 可导入", False, f"{type(_e).__name__}: {_e}")
finally:
    os.chdir(_old_cwd)

# ---- 收发件人归因（D21 / R1）----
try:
    from services.mood_judge import resolve_target as _rt
    check("ADDR：A 对 B 说 → recipient",
          _rt("speaker", "111", "222", "person", 0.95) == ("recipient", "222"),
          str(_rt("speaker", "111", "222", "person", 0.95)))
    check("ADDR：低置信退回说话人",
          _rt("speaker", "111", "222", "person", 0.5) == ("speaker", "111"),
          str(_rt("speaker", "111", "222", "person", 0.5)))
    check("ADDR：@她 → speaker",
          _rt("speaker", "111", "111", "bot", 0.99) == ("speaker", "111"),
          str(_rt("speaker", "111", "111", "bot", 0.99)))
    check("ADDR：群气氛不落到人头上",
          _rt("environment", "111", "222", "person", 0.95)[0] == "world")
except Exception as _e:
    check("ADDR 可导入", False, f"{type(_e).__name__}: {_e}")

# ---- 2026-11-26 回归：亲近曲线 / B站短链 / 启动配置体检 ----
try:
    from services.profiles import intimacy_from_score as _ifs
    check("INT：亲近线性 + 软上限（score=3 → 0.98）",
          abs(_ifs(3.0) - 0.98) < 1e-9, str(_ifs(3.0)))
    check("INT：score=1.5 → 0.50（不再被饱和曲线砍半）",
          abs(_ifs(1.5) - 0.5) < 1e-9, str(_ifs(1.5)))
    check("INT：score=100 → 0.98（不会到 1.00）",
          abs(_ifs(100.0) - 0.98) < 1e-9, str(_ifs(100.0)))
except Exception as _e:
    check("INT 可导入", False, f"{type(_e).__name__}: {_e}")

try:
    from services.link_preview import pick_final_url as _pick
    _quiz = "https://www.bilibili.com/blackboard/activity-abc.html"
    check("BILI：活动页/测试题保持原样（不被改成视频）",
          _pick(_quiz, "推荐视频 BV1atse6EEfx") == _quiz, _pick(_quiz, "BV1atse6EEfx"))
    check("BILI：直播页保持原样",
          _pick("https://live.bilibili.com/123", "") == "https://live.bilibili.com/123")
    check("BILI：JS 跳转页才从 HTML 抠 BV",
          _pick("https://b23.tv/abc", "var x='BV1atse6EEfx'")
          == "https://www.bilibili.com/video/BV1atse6EEfx")
    check("BILI：video 页原样返回",
          _pick("https://www.bilibili.com/video/BV1atse6EEfx?p=1", "")
          == "https://www.bilibili.com/video/BV1atse6EEfx?p=1")
except Exception as _e:
    check("BILI 可导入", False, f"{type(_e).__name__}: {_e}")

try:
    from services import env_guard as _eg
    _save_env = {k: os.environ.get(k) for k in
                 ("ALLOWED_GROUPS", "ALLOWED_PRIVATE", "OWNER_QQ", "SUPERUSERS")}
    for _k in _save_env:
        os.environ.pop(_k, None)
    _w = _eg.check()
    check("ENV：白名单为空必须告警",
          any("ALLOWED_GROUPS" in x for x in _w)
          and any("ALLOWED_PRIVATE" in x for x in _w), str(_w[:2]))
    os.environ["ALLOWED_GROUPS"] = "111"
    os.environ["ALLOWED_PRIVATE"] = "222"
    os.environ["OWNER_QQ"] = "222"
    _w2 = _eg.check()
    check("ENV：填好后不再告警白名单",
          not any("ALLOWED_GROUPS" in x or "ALLOWED_PRIVATE" in x for x in _w2))
    for _k, _v in _save_env.items():
        if _v is None:
            os.environ.pop(_k, None)
        else:
            os.environ[_k] = _v
except Exception as _e:
    check("ENV 可导入", False, f"{type(_e).__name__}: {_e}")

# ---- 好感 v2（AFF_MODEL=v2）：迁移 / 三段式 / 门槛 / 陪伴 ----
try:
    from services import profiles as _pf
    _aff_dir = _tmp / "profiles"
    _aff_dir.mkdir(exist_ok=True)
    _pf.PROFILE_DIR = _aff_dir
    _pf.AFF_MODEL = "v2"

    def _seed(qq, **kw):
        d = {"qq": qq, "affinity": 0.0}
        d.update(kw)
        _pf._save("profile", qq, d)

    # 1) 迁移不跳变（旧档案没有 aff_traces，用当前 affinity 初始化）
    for _i, (_a, _d) in enumerate([(0.90, 30), (0.60, 90), (0.30, 200)]):
        _qq = "9000%d" % _i
        _seed(_qq, affinity=_a, affinity_at=time.time() - 86400 * _d)
        _got = _pf.affinity_now(_qq)
        check("AFF：迁移不跳变 %.2f/%d天" % (_a, _d), abs(_got - _a) < 0.02,
              "%.3f" % _got)

    # 2) 三段式权重（日常 / 尾部）
    _w1 = _pf._aff_w_from_dv(0.35, 0.0)
    check("AFF：一见钟情 w≈0.362", abs(_w1 - 0.362) < 0.01, "%.3f" % _w1)
    _w2 = _pf._aff_w_from_dv(-0.35, 0.90)
    check("AFF：心碎 w≈-0.410", abs(_w2 + 0.410) < 0.01, "%.3f" % _w2)

    # 3) 新号事件被门槛卡在 0.12
    _qq = "90010"
    _pf.add_affinity_event(_qq, 0.35)
    _a3 = _pf.affinity_now(_qq)
    check("AFF：新号 +0.35 被门槛卡 0.12", abs(_a3 - 0.12) < 0.02, "%.3f" % _a3)

    # 4) 陪伴不能绕过门槛（60 次中性陪伴）
    _qq = "90011"
    for _ in range(60):
        _pf.add_affinity_comp(_qq)
    _a4 = _pf.affinity_now(_qq)
    check("AFF：陪伴不能绕过门槛", _a4 <= 0.14, "%.3f" % _a4)

    # 5) 心碎：a=0.90 一次 -0.35 → 约 -0.41
    _qq = "90012"
    _seed(_qq, aff_bond=0.78, affinity=0.90,
          aff_traces=[[time.time(), 0.12]],
          aff_peaks=5, aff_repairs=1, aff_warm_days=60,
          affinity_at=time.time() - 86400 * 300,
          affinity_log=[{"at": time.time() - 86400 * 300}])
    _b = _pf.affinity_now(_qq)
    _a5 = _pf.add_affinity_event(_qq, -0.35)
    check("AFF：a=0.90 心碎 Δ≈-0.41", abs((_a5 - _b) + 0.41) < 0.02,
          "%+.3f" % (_a5 - _b))

    # 6) dv=0 不写
    _qq = "90013"
    check("AFF：dv=0 不写", _pf.add_affinity_event(_qq, 0.0) == 0.0)
except Exception as _e:
    check("AFF v2 可跑", False, f"{type(_e).__name__}: {_e}")

# ---- 好感 v1 双吃（ADR-043）+ 锚点权重（ADR-042）----
try:
    from services import profiles as _pf3, mood as _mg3, mood_judge as _mj3
    _pf3.AFF_MODEL = "v1"
    _we = _pf3.affinity_v1_step(0.40, 0.0, 0.30)
    _wm = _pf3.affinity_v1_step(0.0, 0.09, 0.30)
    check("AFFv1：delta 项 > 0", _we > 0, "%+.4f" % _we)
    check("AFFv1：dv 项 > 0（Δ=0 也吃）", _wm > 0, "%+.4f" % _wm)
    # 尾部必须落在「dv 项天花板 = DV_K × AFF_W_MAX」附近（不是随便 >0.10 就算过）
    _reach = _pf3.AFF_V1_DV_K * _pf3.AFF_W_MAX
    _wt = _pf3.affinity_v1_step(0.40, 0.35, 0.0)
    check("AFFv1：一见钟情 ≈ DV_K×AFF_W_MAX",
          abs(_wt - _reach) < 0.02, "%+.4f（天花板 %.3f）" % (_wt, _reach))
    _wh = _pf3.affinity_v1_step(-0.40, -0.35, 0.0)
    check("AFFv1：心碎 ≈ -(DV_K×AFF_W_MAX)",
          abs(_wh + _reach) < 0.02, "%+.4f" % _wh)
    # 单次上限不能是「恒不触发」的死护栏（ADR-028 刚批过这种常量）
    check("AFFv1：单次上限不是死护栏",
          _pf3.AFF_V1_CAP <= _reach * 1.3 + 1e-9,
          "CAP %.3f vs 可达 %.3f" % (_pf3.AFF_V1_CAP, _reach))
    _wc = _pf3.affinity_v1_step(1.0, 0.35, 0.0)
    check("AFFv1：单次上限 AFF_V1_CAP 生效",
          abs(_wc) <= _pf3.AFF_V1_CAP + 1e-9, "%+.4f" % _wc)

    # 日上限必须管住「当天第一件事」（旧 `_used*d > 0` 在 _used==0 时整条跳过）
    _q = "90099"
    _pf3._save("profile", _q, {"qq": _q, "affinity": 0.0})
    _b1 = _pf3.bump_affinity(_q, 0.36, reason="t")
    check("AFFv1：日上限管住当天第一件事", abs(_b1) <= 0.151, "%+.3f" % _b1)
    _b2 = _pf3.bump_affinity(_q, 0.36, reason="t")
    check("AFFv1：当天额度用完同向不再涨",
          abs(_b2 - _b1) < 1e-6, "%+.3f → %+.3f" % (_b1, _b2))

    _mv = _mg3._anchor_rec({"value": 0.0}, 2, 1.0, 0.0,
                           gain=_mg3.ANCHOR_WEIGHT)
    check("锚点：每窗权重 W=0.45（单次 ≤0.20）", abs(_mv - 0.20) < 1e-6,
          "%+.3f" % _mv)
    _mv2 = _mg3._anchor_rec({"value": 0.0}, 2, 1.0, 0.0,
                            gain=_mg3.ANCHOR_GAIN)
    check("锚点：独立锚点仍用 gain=0.25", abs(_mv2 - 0.175) < 1e-6,
          "%+.3f" % _mv2)
    # P 回补「只补不降」：P0=0.25 > CAP=0.20 时，±2 不能把 P 调低
    check("P 回补只补不降（P0 > CAP）",
          abs(_mg3.event_p_boost(0.25, 2) - 0.25) < 1e-9,
          "%.3f" % _mg3.event_p_boost(0.25, 2))
    check("P 回补仍会从地板抬起",
          abs(_mg3.event_p_boost(0.005, 2) - 0.055) < 1e-9,
          "%.3f" % _mg3.event_p_boost(0.005, 2))
    _pr = _mj3._build_prompt("", "她心里有点亮", ["a → 你：在吗"])
    check("判读 prompt：anchor_level 必填",
          "anchor_level" in _pr and "必填" in _pr,
          _pr[_pr.find("anchor_level"):][:60])

    # 2026-11-27（外部审计）：锚点死区改成软死区 —— 边界必须连续，小抖动可忽略
    _a_in = _mg3._anchor_rec({"value": 20.01}, 1, 1.0, 0.0,
                             gain=_mg3.ANCHOR_WEIGHT)
    _a_out = _mg3._anchor_rec({"value": 20.0}, 1, 1.0, 0.0,
                              gain=_mg3.ANCHOR_WEIGHT)
    check("锚点软死区：边界连续（0.149 vs 0.151 差 <0.005）",
          abs(abs(_a_in) - abs(_a_out)) < 0.005,
          "%.4f vs %.4f" % (_a_in, _a_out))
    _a_tiny = _mg3._anchor_rec({"value": 34.0}, 1, 1.0, 0.0,
                               gain=_mg3.ANCHOR_WEIGHT)
    check("锚点软死区：小抖动可忽略（diff=0.01 → <0.002）",
          abs(_a_tiny) < 0.002, "%+.4f" % _a_tiny)

    # 2026-11-27（外部审计）：±3 一天最多一次，落到代码计数
    _old_mf = _mg3.MOOD_FILE
    _mg3.MOOD_FILE = _tmp / "mood_cap.json"
    _mg3.MOOD_FILE.write_text('{"global": {"value": 0.0}, "sessions": {}}',
                              encoding="utf-8")
    check("±3 当日第一次生效", _mg3.cap_daily_impact("group_cap", 3) == 3)
    check("±3 当日第二次降为 ±2", _mg3.cap_daily_impact("group_cap", 3) == 2)
    check("±3 计数按会话隔离", _mg3.cap_daily_impact("group_cap2", 3) == 3)
    check("±2 不受日限", _mg3.cap_daily_impact("group_cap", 2) == 2)
    _mg3.MOOD_FILE = _old_mf
except Exception as _e:
    check("AFFv1 / 锚点可跑", False, f"{type(_e).__name__}: {_e}")

# ---- 2026-11-27：long_doc 证据 6000 / 图书馆 3000 真接线 ----
try:
    from services import prompt_builder as _pb3, long_doc as _ld3, ctx_budget as _cb3
    check("long_doc 证据预算 6000 字符",
          _cb3.limit_chars("long_doc_evidence") == 6000,
          str(_cb3.limit_chars("long_doc_evidence")))
    check("long_doc 图书馆预算 3000 字符",
          _cb3.limit_chars("long_doc_library") == 3000,
          str(_cb3.limit_chars("long_doc_library")))
    _hits3 = [{"summary": "s%d" % i, "ago": "3天前",
               "text_full": ("长文证据%d-" % i) * 900} for i in range(1, 5)]
    _pk_long = _pb3.build_packet(session_key="group_ld", user_text="详细讲讲这本书",
                                 hits=_hits3, memory_limit=8, total_budget=12000,
                                 long_doc=True, record=False)
    _pk_short = _pb3.build_packet(session_key="group_ld", user_text="在吗",
                                  hits=_hits3, memory_limit=8, total_budget=0,
                                  long_doc=False, record=False)
    _lm = len(str(_pk_long.get("memory_index") or ""))
    _sm = len(str(_pk_short.get("memory_index") or ""))
    check("long_doc 记忆块 > 500 字且 ≤6000", 500 < _lm <= 6000, str(_lm))
    check("long_doc 带正文而不是 40 字目录",
          "长文证据" in str(_pk_long.get("memory_index") or ""),
          str(_pk_long.get("memory_index") or "")[:30])
    check("常态记忆块仍是短目录（≤520）", _sm <= 520, str(_sm))
except Exception as _e:
    check("long_doc 接线可跑", False, f"{type(_e).__name__}: {_e}")

# ---- 节流不该丢 THINK（2026-11-26 用户实测：她每句都写，是 cd 隔离了）----
_old_cwd = os.getcwd()
os.chdir(_tmp)
os.environ["MOOD_FILE"] = str(_tmp / "mood_thinkq.json")
os.environ["MOOD_THINK_FILE"] = str(_tmp / "think_thinkq.json")
os.environ["MOOD_JUDGE_TRACE"] = "0"
try:
    import asyncio as _aio4
    from services import mood_judge as _mj4

    _seen = []

    async def _fake_j(words, ctx=None, **kw):
        _seen.append(kw.get("think") or "")
        return {"ok": True, "word": "x", "desirability": 1, "cared": 2,
                "impact": 1, "anchor_level": 0, "cause": "speaker",
                "confidence": 0.8, "why": "w", "mind": "m", "src": "s",
                "at": time.time(), "relation_relevance": 0.8,
                "relation_trust": 0, "relation_respect": 0}

    _mj4.judge = _fake_j
    _mj4.MIN_INTERVAL = 60
    _mj4.REFRESH = 0
    _aio4.run(_mj4.judge_and_store("g_q", "", ["a"], think="第一句心里话",
                                   addressed=True))
    _r2 = _aio4.run(_mj4.judge_and_store("g_q", "", ["a"], think="第二句心里话",
                                         addressed=True))
    check("节流时 THINK 不丢（进队列）",
          _mj4.pending_think_count("g_q") == 1,
          f"pending={_mj4.pending_think_count('g_q')} err={_r2.get('err')}")
    _mj4._LAST_AT["g_q"] = time.time() - 999
    _aio4.run(_mj4.judge_and_store("g_q", "", ["a"], think="第三句心里话",
                                   addressed=True))
    _merged = _seen[-1] if _seen else ""
    check("下次判读把攒着的 THINK 一起判",
          "第二句心里话" in _merged and "第三句心里话" in _merged,
          _merged[:60])
    check("已判过的 THINK 不重复判", "第一句心里话" not in _merged, _merged[:60])

    _mj4.MIN_INTERVAL = 0
    _mj4.note_think("g_q2", "落盘的那句心里话")
    _aio4.run(_mj4.judge_and_store("g_q2", "", ["a"], addressed=True))
    check("调用方没传 think 时，用落盘的新 THINK",
          "落盘的那句心里话" in (_seen[-1] if _seen else ""),
          (_seen[-1] if _seen else "")[:60])
except Exception as _e:
    check("THINK 队列可跑", False, f"{type(_e).__name__}: {_e}")
finally:
    os.environ.pop("MOOD_JUDGE_TRACE", None)
    os.chdir(_old_cwd)

# ---- 接话前「读气氛」（2026-11-26 用户要求：群友在哭，别忽然哈哈哈）----
try:
    from services import atmosphere as _atmo

    _d = {"mood": "压抑", "who": "月下橘", "feel": "难过，像在哭",
          "others": "在斗图刷屏，没人接她", "tip": "先安慰她，别用哈哈哈开头"}
    _blk = _atmo.render(_d, n=8)
    check("气氛块带整体/谁在难过/接话注意",
          "压抑" in _blk and "月下橘" in _blk and "别用哈哈哈" in _blk,
          _blk[:90])
    check("气氛解析吃 markdown 代码块",
          (_atmo._parse('```json\n{"mood":"低落","who":"甲"}\n```') or {}).get("mood")
          == "低落")
    check("气氛解析拒绝空对象", _atmo._parse("{}") == {})
except Exception as _e:
    check("读气氛可跑", False, f"{type(_e).__name__}: {_e}")

# ---- rag_search 兜底解析（2026-11-26：smart-query 卡 >70s）----
try:
    from services import agent_client as _ac

    _raw = {
        "success": True,
        "output": (
            "找到 2 个相关片段:\n\n"
            "**片段 1** (来源: /app/documents/x.txt)\n"
            "\"\\u5c0f\\u660e \\u6e29\\u5ba4\\u6548\\u5e94\"\n\n"
            "**片段 2** (来源: /app/documents/467.json)\n"
            "\"{\\\"title\\\":\\\"x\\\",\\\"author\\\":\\\"y\\\","
            "\\\"paragraphs\\\":[\\\"z\\\"]}\""
        ),
    }
    _fs = _ac._rag_tool_facts(_raw)
    check("rag_search 结果能还原中文", any("小明" in x for x in _fs), str(_fs))
    check("rag_search 过滤宋词 JSON",
          bool(_fs) and all("paragraphs" not in x for x in _fs), str(_fs))
    check("rag_search 失败返回空", _ac._rag_tool_facts({"success": False}) == [])
except ModuleNotFoundError as _e:
    # 本机（IDE）没有 aiohttp/nonebot；容器里会真的跑上面三条。
    check("rag_search 解析（跳过：本机缺依赖）", True, str(_e))
except Exception as _e:
    check("rag_search 解析可跑", False, f"{type(_e).__name__}: {_e}")

# ---- 输出 token 可配置（2026-11-27 用户真机：GLM 主模型被卡 800）----
try:
    from services import providers as _pv

    _pv.reset()
    os.environ["ROLEPLAY_MAX_TOKENS"] = "2048"
    _pv.reset()
    check("输出 token 读 ROLEPLAY_MAX_TOKENS",
          _pv.resolve("roleplay")["max_tokens"] == 2048,
          str(_pv.resolve("roleplay")))
    os.environ["ROLEPLAY_MAX_TOKENS"] = "99999"
    _pv.reset()
    check("输出 token 按 provider 上限 clamp",
          _pv.resolve("roleplay")["max_tokens"] <= 8192,
          str(_pv.resolve("roleplay")))
    os.environ.pop("ROLEPLAY_MAX_TOKENS", None)
    os.environ["ROLEPLAY_PROVIDER"] = "glm"
    os.environ["GLM_MAX_TOKENS"] = "2048"
    os.environ.setdefault("GLM_API_KEY", "test-key")
    _pv.reset()
    check("GLM 主模型兼容旧的 GLM_MAX_TOKENS",
          _pv.resolve("roleplay")["max_tokens"] == 2048,
          str(_pv.resolve("roleplay")))
    check("OpenAI 推理模型用 max_completion_tokens",
          _pv.token_field("openai", "o1-mini") == "max_completion_tokens")
    os.environ.pop("ROLEPLAY_PROVIDER", None)
    os.environ.pop("GLM_MAX_TOKENS", None)
    _pv.reset()
except Exception as _e:
    check("输出 token 可配置", False, f"{type(_e).__name__}: {_e}")

# ---- 个人档案库（C 方案：30 天以前、本地、只她自己能查）----
try:
    from services import archive as _ar

    _tmp_arc = Path(tempfile.mkdtemp()) / "arc.db"
    _ar.DB_PATH = _tmp_arc
    _ar._INITED["ok"] = False
    try:
        _ar._LOCAL.conn.close()
    except Exception:
        pass
    _ar._LOCAL.conn = None
    _now = time.time()
    _ar.add(gid="g1", qq="1001", who="阿明", ts=_now - 40 * 86400,
            text="阿明说奶奶住院了，他很担心")
    _ar.add(gid="g1", qq="1002", who="小红", ts=_now - 5 * 86400,
            text="小红昨天说新番很好看")
    _ar.add(gid="g1", qq="1001", who="阿明", ts=_now - 40 * 86400,
            text="阿明说奶奶住院了，他很担心")
    _h = _ar.recall("奶奶 住院", gid="g1", limit=4)
    check("档案库：只回 30 天以前",
          len(_h) == 1 and "奶奶" in _h[0]["text"], str(_h))
    check("档案库：重复写入只留一条", _ar.stats()["count"] == 2, str(_ar.stats()))
    check("档案库：近期的不进档案",
          all("新番" not in x["text"] for x in _ar.recall("新番", gid="g1")))
    check("档案库：渲染块带时间/人",
          "阿明" in _ar.render(_h) and "天前" in _ar.render(_h), _ar.render(_h))
except Exception as _e:
    check("个人档案库可跑", False, f"{type(_e).__name__}: {_e}")

# ---- relevance 低不能挡心情（2026-11-27 外部审计，真问题）----
try:
    import asyncio as _aio5
    from services import mood as _mg5, mood_judge as _mj5

    os.environ["MOOD_FILE"] = str(_tmp / "mood_rel.json")
    os.environ["MOOD_JUDGE_TRACE"] = "0"

    async def _fake_rel(words, ctx=None, **kw):
        return {"ok": True, "word": "空落落", "desirability": 2, "cared": 2,
                "impact": 2, "anchor_level": 1, "cause": "self",
                "confidence": 0.85, "why": "想起以前", "mind": "有点空",
                "src": "", "at": time.time(), "relation_relevance": 0.0,
                "relation_trust": 0, "relation_respect": 0}

    _mj5.judge = _fake_rel
    _mj5.MIN_INTERVAL = 0
    _mj5.REFRESH = 0
    _before = float(_mg5.get_mood("g_rel").get("value") or 0)
    _aio5.run(_mj5.judge_and_store(
        "g_rel", "", ["a"], think="想起以前有点空", addressed=True))
    _after = float(_mg5.get_mood("g_rel").get("value") or 0)
    check("relevance 低也照常更新心情（只挡好感/记忆）",
          abs(_after - _before) > 1e-6,
          f"{_before:+.2f} -> {_after:+.2f}")
except Exception as _e:
    check("relevance 低也更新心情", False, f"{type(_e).__name__}: {_e}")
finally:
    os.environ.pop("MOOD_JUDGE_TRACE", None)

# ---- 引用互刷护栏（2026-11-26 真机：月下橘 45s 内引用她 4 次）----
try:
    from services import quote_guard as _qg

    _qg.reset()
    _g = "g_loop_test_1126"
    _hits = [_qg.hit(_g, "10001", "20002") for _ in range(3)]
    check("同一对 120s 内引用 3 次 → 触发反循环",
          _hits[:2] == [False, False] and _hits[2] is True, str(_hits))
    check("触发后进入冷却，继续不接",
          _qg.hit(_g, "10001", "20002") is True)
    check("不同的一对不受影响", _qg.hit(_g, "10001", "30003") is False)
except Exception as _e:
    check("引用互刷护栏可跑", False, f"{type(_e).__name__}: {_e}")

print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败：" + "、".join(FAIL))
sys.exit(1 if FAIL else 0)

