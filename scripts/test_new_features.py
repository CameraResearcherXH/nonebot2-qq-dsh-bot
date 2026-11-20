#!/usr/bin/env python3
# scripts/test_new_features.py
"""新增能力的离线自检（不依赖 nonebot，可在本地/CI 跑）。

覆盖：文字宽度计数、skill 开关、memdb、llm_usage、plugin_registry。
用法：python3 scripts/test_new_features.py
"""
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
                     gid="822659531", importance=0.8, salience=0.9,
                     mood_word="被戳到")
memdb.remember(text="他问我在干嘛", kind="event", about_qq="10002",
               gid="822659531", importance=0.4, salience=0.1)
hits = memdb.recall("改代码", limit=3)
check("关键词召回命中", bool(hits) and "改代码" in hits[0]["summary"])
hits2 = memdb.recall("在吗", limit=3, gid="822659531")
check("无关键词也能召回近期", len(hits2) >= 1)
cat = memdb.recall_catalog("", limit=5, gid="822659531")
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
check("扫到 9 个第三方插件", len(rows) == 9, str(len(rows)))
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
    _gm.record("group_1", -0.30, about_qq="10004", cause="speaker", why="d")
    _s = _gm.snapshot("group_1")
    check("群净跌 0.75 + 4 人 → 群聊不愉快",
          _s["group_quiet"] and abs(_s["net"] + 0.75) < 1e-6, str(_s["net"]))
    _gm.record("group_1", 0.50, about_qq="10001", cause="speaker", why="e")
    _s = _gm.snapshot("group_1")
    check("群里的正值能抵消负值", (not _s["group_quiet"])
          and abs(_s["net"] + 0.25) < 1e-6, str(_s["net"]))
    _gm.record("group_1", -0.45, about_qq="10009", cause="speaker", why="f")
    _s = _gm.snapshot("group_1")
    check("单人净跌 0.45 → 单独不理他",
          any(p["qq"] == "10009" for p in _s["person_quiet"]), str(_s["person_quiet"]))
    check("群心情差 → 少说话乘数 < 1", _gm.chat_factor("group_1") < 1.0,
          str(_gm.chat_factor("group_1")))
    # 5 小时前的事件必须过期（不参与净变化）
    _gm.record("group_1", -0.90, about_qq="10001", cause="speaker",
               why="old", at=time.time() - 20000)
    _s = _gm.snapshot("group_1")
    check("5 小时前的事件过期", abs(_s["net"] + 0.70) < 1e-6, str(_s["net"]))
    check("群心情状态文本可读", "喜欢比例" in _gm.status_text("group_1"))
finally:
    os.chdir(_old_cwd)

print("-" * 60)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败：" + "、".join(FAIL))
sys.exit(1 if FAIL else 0)
