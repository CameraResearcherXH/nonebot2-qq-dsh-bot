#!/usr/bin/env python3
# scripts/audit_features.py
"""功能链路体检：把 docs/feature-audit.md 的断点变成可重跑的静态检查。

用法：
    python3 scripts/audit_features.py            # 打印 PASS/FAIL
    python3 scripts/audit_features.py --strict   # 有 FAIL 时退出码 1

设计说明：
- 只做静态文本检查，不 import 项目（避免触发 nonebot 初始化 / 需要 3.11）。
- 每条检查对应 docs/feature-audit.md 的一个断点编号。
- 修复 behavior-wiring 后，A/B/C/D/E/G 组应全部 PASS；
  F 组属于 record-v2 / recall-mood-sticker 阶段。
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read(rel):
    p = ROOT / rel
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def func_body(src, name, indent=0):
    """按缩进粗略截取函数体（够静态检查用）。支持 async def 与多行签名。"""
    m = re.search(
        r"(?m)^( *)(?:async\s+)?def %s\s*\(" % re.escape(name), src)
    if not m:
        return ""
    lines = src[m.start():].splitlines()
    start_idx = 0
    for i, line in enumerate(lines[:120]):
        if line.rstrip().endswith(":"):
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines):
                nxt = len(lines[j]) - len(lines[j].lstrip(" "))
                if nxt > indent:
                    start_idx = i + 1
                    break
    out = []
    for line in lines[start_idx:]:
        if not line.strip():
            out.append(line)
            continue
        cur = len(line) - len(line.lstrip(" "))
        if cur <= indent and not line.lstrip().startswith("#"):
            break
        out.append(line)
    return "\n".join(out)


def enclosing_func(src, needle):
    """返回包含 needle 的最内层函数名（粗略）。"""
    idx = src.find(needle)
    if idx < 0:
        return ""
    head = src[:idx]
    best = ""
    for m in re.finditer(r"(?m)^( *)def (\w+)\s*\(", head):
        best = m.group(2)
    return best


# ---------------- 检查项 ----------------
# (编号, 说明, 组, 检查函数)
def check_c1():
    """破冰必须能独立于 _need_reply 执行。"""
    src = read("plugins/chat/__init__.py")
    body = func_body(src, "_need_reply")
    outside = src.replace(body, "")
    return "maybe_thaw" in outside, "maybe_thaw 只在 _need_reply 内" if "maybe_thaw" in body else "未找到 maybe_thaw"


def check_a1():
    src = read("plugins/chat/__init__.py")
    body = func_body(src, "_random_chat_loop")
    return ("set_tool_context" in body), "插话路径未设置 tool context"


def check_a1b():
    src = read("plugins/chat/__init__.py")
    body = func_body(src, "_random_chat_loop")
    return ("addressed=False" in body or "addressed = False" in body), "插话未显式 addressed=False"


def check_a2():
    src = read("plugins/chat/__init__.py")
    body = func_body(src, "_random_chat_loop")
    # 必须是带第二参数的 is_off（人对人），或 is_strict
    ok = ("is_strict" in body) or re.search(
        r"is_off\([^)]*,\s*\w+", body) is not None
    return bool(ok), "插话只查群安静，不查最后说话人"


def check_a3():
    src = read("plugins/chat/__init__.py")
    ok = (("_WORKER_BUSY" in src) or ("session_locks" in src)
          or ("_session_lock(" in src))
    return ok, "插话与 worker 没有 per-session 互斥"


def check_b1():
    src = read("plugins/chat/__init__.py")
    body = func_body(src, "_need_reply")
    return ("chat_switch.is_off" in body), "_need_reply 不查 chat_switch"


def check_d1():
    src = read("plugins/chat/__init__.py")
    body = func_body(src, "_need_reply")
    bad = "dialogue_log.log_msg" in body
    return (not bad), "_need_reply 内仍有 dialogue_log.log_msg（Rule 带副作用）"


def check_g4():
    src = read("plugins/chat/__init__.py")
    body = func_body(src, "_session_worker")
    # 正常回复分支与沉默分支都要更新 last
    n = len(re.findall(r"(?m)^\s*last\s*=\s*time\.monotonic\(\)", body))
    return (n >= 2), "worker 的 last 只在沉默分支更新（找到 %d 处）" % n


def check_e2():
    src = read("plugins/chat/__init__.py")
    ok = ("_sent_sticker" in src) or re.search(r"=\s*await\s+send_sticker_by_tag", src) is not None
    return bool(ok), "表情发送未检查返回值"


def check_g5():
    src = read("services/text.py")
    body = func_body(src, "limit_hanzi")
    # 只数汉字（_HAN.match）且没有按显示宽度计数 → FAIL
    ok = ("_char_w" in body) or ("wcwidth" in body) or ("_HAN.match" not in body)
    return bool(ok), "limit_hanzi 只数汉字"


def check_g3():
    """`.env` 里的 MAX_GROUP_RECENT 不能把代码默认 16 覆盖回小值。

    注意：`.env` 常是 600 root（docker 用 env_file 读），普通用户读不到。
    读不到就跳过，不要误判成「缺失」——真正的权威核对是在容器里：
        docker exec qq-grok-bot python3 -c "from core import tuning; print(tuning.i('MAX_GROUP_RECENT'))"
    """
    p = ROOT / ".env"
    try:
        env = p.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return True, "", "没有 .env，用代码默认 16"
    except PermissionError:
        return True, "", ".env 不可读（权限受限）已跳过，容器内核对"
    m = re.search(r"(?m)^\s*MAX_GROUP_RECENT\s*=\s*(\d+)", env)
    if not m:
        return True, "", "未设置，用代码默认 16"
    v = int(m.group(1))
    return (v >= 16,
            "MAX_GROUP_RECENT=%d（应 ≥16，代码默认 16）" % v,
            "MAX_GROUP_RECENT=%d" % v)


def check_g9():
    """.env 不能把「心情惯性」调回没有：SMOOTH 过大 / 判读间隔过短 → 小事也会大幅波动。

    真机上踩过：代码默认改成 0.35/60，但 .env 里还写着 1.0/20，等于白改。
    """
    p = ROOT / ".env"
    try:
        env = p.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return True, "", "没有 .env，用代码默认"
    except PermissionError:
        return True, "", ".env 不可读（权限受限）已跳过"
    bad = []
    m = re.search(r"(?m)^\s*MOOD_JUDGE_SMOOTH\s*=\s*([\d.]+)", env)
    if m and float(m.group(1)) > 0.6:
        bad.append("MOOD_JUDGE_SMOOTH=%s（>0.6 心情没惯性，小事也会大跳）" % m.group(1))
    m = re.search(r"(?m)^\s*MOOD_JUDGE_MIN_INTERVAL\s*=\s*([\d.]+)", env)
    if m and float(m.group(1)) < 30:
        bad.append("MOOD_JUDGE_MIN_INTERVAL=%s（<30s 刷屏时会来回震荡）" % m.group(1))
    return (not bad), "；".join(bad), "心情惯性参数正常"


def check_g11():
    """「涨幅驱动」不能被改回「水平驱动」（用户 2026-11-20 反复强调）。

    必须同时满足：
      1. 记忆/档案的刻痕用 Δ（mood_judge 用判读原值之差算 _delta）；
      2. chat 里不再用 abs(_jv)（当前心情水平）判断「值不值得记」；
      3. json 后端也能 patch_turn（否则默认部署下刻痕写不进去）；
      4. 好感有每日净变化上限（防「持续跌」一天到底）。
    """
    chat = read("plugins/chat/__init__.py")
    mj = read("services/mood_judge.py")
    mv = read("services/memory_v2.py")
    pf = read("services/profiles.py")
    bad = []
    if "abs(_jv) >= 0.3" in chat or "min(1.0, abs(_jv))" in chat:
        bad.append("chat 仍用当前心情水平 abs(_jv) 当刻痕")
    if ('_delta = round(float(res.get("delta") or 0.0), 4)' not in mj
            and "_delta = round(float(res[\"value\"]) - _prev_v" not in mj):
        bad.append("mood_judge 没有把判读的 delta 当作涨幅")
    if "mood.apply_delta(" not in mj:
        bad.append("心情值不是用 apply_delta 累加（又变回覆盖式了）")
    if "MOOD_LABELS" not in read("services/mood.py"):
        bad.append("心情名词没有按数值分档")
    if "_PATCHABLE" not in mv:
        bad.append("memory_v2 的 json 后端没有 patch_turn")
    if "AFFINITY_DAILY_CAP" not in pf:
        bad.append("好感没有每日上限")
    return (not bad), "；".join(bad), "涨幅驱动 / json 刻痕 / 好感日上限 都正常"


def check_g12():
    """峰值事件 / 印象最深一件事 / 空闲衰减 不能回退（用户 2026-11-20）。

    要求：
      1. 心情流水要带 mind（峰值事件要显示当时的心理活动）；
      2. mood.peak_event 存在，且 status_block 真的用了它；
      3. 峰值事件有 2 小时保质期（MOOD_PEAK_WINDOW_SEC）；
      4. 档案里要显示「你与他印象最深刻的一件事」；
      5. 记忆层能取 |Δ| 最大的那件事；
      6. 空闲衰减回到「5 分钟 0.01」。
    """
    md = read("services/mood.py")
    pf = read("services/profiles.py")
    mv = read("services/memory_v2.py")
    tn = read("core/tuning.py")
    bad = []
    if "def peak_event(" not in md:
        bad.append("mood.py 没有 peak_event（峰值事件算法）")
    if "peak_event(session_key)" not in md:
        bad.append("status_block 没用峰值事件")
    if "MOOD_PEAK_WINDOW_SEC" not in md:
        bad.append("峰值事件没有 2 小时保质期")
    if "mind=" not in md:
        bad.append("心情流水没带心理活动 mind")
    if "印象最深刻的一件事" not in pf:
        bad.append("relation_block 没有「印象最深刻的一件事」")
    if "def deepest_event(" not in mv:
        bad.append("memory_v2 没有 deepest_event")
    if "'MOOD_IDLE_AFTER': 300.0" not in tn:
        bad.append("空闲衰减不是 5 分钟（MOOD_IDLE_AFTER != 300）")
    if "last_active_at" not in func_body(md, "_decay"):
        bad.append("_decay 丢了 last_active_at（空闲衰减永远不生效）")
    if "_idle_decay(rec, now)" not in func_body(md, "touch"):
        bad.append("touch 没有先结算沉默期间的自减")
    return (not bad), "；".join(bad), "峰值事件 / 印象最深一件事 / 空闲衰减 正常"


def check_g13():
    """群心情必须是独立一层，不能又被合并回整体心情（用户 2026-11-20）。

    要求：
      1. services/group_mood.py 存在，有 record / snapshot / chat_factor；
      2. 判读写回时把 group_ 会话的 Δ 记进群心情；
      3. 群心情参与「愿不愿意在这个群多说话」（mood.mood_factors）；
      4. quiet_advisor 用单人 0.4 / 整群 0.7+3 人两个阈值；
      5. 主动私聊看「当前心情 + 好感」；
      6. 窗口是 5 小时。
    """
    gm = read("services/group_mood.py")
    mj = read("services/mood_judge.py")
    md = read("services/mood.py")
    qa = read("services/quiet_advisor.py")
    pa = read("services/proactive.py")
    tn = read("core/tuning.py")
    bad = []
    if not gm:
        bad.append("services/group_mood.py 不存在")
    for fn in ("def record(", "def snapshot(", "def chat_factor("):
        if fn not in gm:
            bad.append(f"group_mood 缺少 {fn}")
    if "group_mood.record(" not in mj:
        bad.append("判读写回没有记群心情")
    if "group_mood.chat_factor(" not in md:
        bad.append("群心情没有参与「愿不愿意多说话」")
    if "group_mood.snapshot(" not in qa or "group_mood.person_of(" not in qa:
        bad.append("安静建议没有用群心情")
    if "GROUP_MOOD_PERSON_DROP" not in qa or "GROUP_MOOD_GROUP_DROP" not in qa:
        bad.append("安静建议没有单人 0.4 / 整群 0.7 阈值")
    if "get_affinity(" not in pa:
        bad.append("主动私聊没有看人物好感")
    if "'GROUP_MOOD_WINDOW_SEC': 18000.0" not in tn:
        bad.append("群心情窗口不是 5 小时（18000s）")
    return (not bad), "；".join(bad), "群心情 / 单人 0.4 / 整群 0.7 / 好感参与主动私聊 正常"


def check_g14():
    """协议残留清理不能回退（2026-11-22 真机：心里话 / 工具标记漏给用户）。

    要求：
      1. aura_logic 有 strip_protocol_residue；
      2. parse_reply 的正文和 _push_text 的气泡都过一遍；
      3. chat 的正常发送路径和 _items_to_message 也过一遍（最后一道防线）。
    """
    al = read("services/aura_logic.py")
    chat = read("plugins/chat/__init__.py")
    bad = []
    if "def strip_protocol_residue(" not in al:
        bad.append("aura_logic 没有 strip_protocol_residue")
    if "strip_protocol_residue(raw)" not in al:
        bad.append("parse_reply 没清协议残留")
    if "strip_protocol_residue(t)" not in al:
        bad.append("_push_text 没清协议残留")
    if "strip_protocol_residue(val)" not in chat:
        bad.append("chat 正常发送路径没清协议残留")
    if "strip_protocol_residue(v)" not in chat:
        bad.append("_items_to_message 没清协议残留")
    if "strip_stage_direction" not in al:
        bad.append("没拦「旁白/心理活动」当正文发出去")
    if "soften_harsh" not in al:
        bad.append("没拦攻击性词（去死/找死）")
    return (not bad), "；".join(bad), "协议残留 / 旁白拦截 正常"


def check_g15():
    """分段回复 / 用量归因 / 以图搜源 不能回退（2026-11-22）。

    要求：
      1. 群聊走 segments.wait_close（等一段说完再回），不再一条一回；
      2. 翻历史优先用 group_scene（收发件人处理过的），不直接塞原生消息；
      3. llm_usage 报表按模型拆开（judge/audit 两个三方 LLM 要看得到）；
      4. PicImageSearch 已 vendored，picsearch 插件已注册；
      5. vision_verify 不再自己拼 trace.moe HTTP，走 image_search。
    """
    chat = read("plugins/chat/__init__.py")
    llm = read("services/llm.py")
    lu = read("services/llm_usage.py")
    vv = read("services/vision_verify.py")
    bad = []
    if "segments.wait_close(" not in chat:
        bad.append("群聊 worker 没走 segments.wait_close")
    if "segments.push(" not in chat:
        bad.append("群消息没进分段 buffer")
    if "group_scene.recent_messages(gid, n)" not in llm:
        bad.append("翻历史没优先用 group_scene（收发件人处理过的）")
    if "按模型" not in lu:
        bad.append("用量报表没有按模型拆开")
    if not (ROOT / "plugins/third/picsearch/plugin.toml").exists():
        bad.append("缺少 plugins/third/picsearch/plugin.toml")
    if not (ROOT / "services/image_search.py").exists():
        bad.append("缺少 services/image_search.py")
    if "image_search.trace_moe_raw(" not in vv:
        bad.append("vision_verify 还在自己拼 trace.moe HTTP")
    if "_extra_sources" not in vv or "搜图源（SauceNAO" not in vv:
        bad.append("识图没把多源搜图结果交给角色扮演模型")
    # /测函数 要能自证这两层（分段 / 群心情）
    if 'what.startswith(("分段"' not in chat:
        bad.append("缺少 /测函数 分段")
    if 'what.startswith(("群心情"' not in chat:
        bad.append("缺少 /测函数 群心情")
    # vendored 库必须在根目录（services 不能 import plugins）
    if not (ROOT / "vendor/PicImageSearch/__init__.py").exists():
        bad.append("vendor/PicImageSearch 不存在")
    # ⚠️ 2026-11-22 事故：plugin.toml 的 handler 不能指向 plugins.third.*
    #    register_all() 会在 nonebot 之前 import 它 → load_plugins 直接 RuntimeError。
    for toml in (ROOT / "plugins/third").glob("*/plugin.toml"):
        txt = toml.read_text(encoding="utf-8", errors="ignore")
        if 'handler = "plugins.third.' in txt:
            bad.append("%s 的 handler 指向 plugins.third（会被提前 import 导致加载失败）"
                       % toml.relative_to(ROOT))
    return (not bad), "；".join(bad), "分段 / 用量按模型 / 以图搜源 / 测函数 正常"


def check_g16():
    """收发件人贯通 + 数值封顶 + 模板自检（用户 2026-11-22）。

    要求：
      1. 分段带收发件人（segments.addressee_of / set_addressee），
         chat 在 group_scene.observe 之后回填；
      2. 三方判心情的上下文也带「谁 → 谁」（mood_judge.scene_lines）；
      3. 群心情净变化封顶 ±1（net_raw 保留原始求和），单条事件有上限；
      4. 心情半衰期可调、接近边界时同方向阻尼（不许钉死 ±1）；
      5. 记忆刻痕 / 档案只收 |Δ| 达标的事（MEMORY_EMOTION_MIN_DELTA）；
      6. 有 /测函数 模板（把她看到的每一块原样打出来）。
    """
    seg = read("services/segments.py")
    gs = read("services/group_scene.py")
    mj = read("services/mood_judge.py")
    gm = read("services/group_mood.py")
    md = read("services/mood.py")
    chat = read("plugins/chat/__init__.py")
    tn = read("core/tuning.py")
    bad = []
    if "def addressee_of(" not in seg or "def set_addressee(" not in seg:
        bad.append("segments 没有收发件人渲染/回填")
    if "segments.set_addressee(" not in chat:
        bad.append("chat 没有把 group_scene 的收发件人回填给分段")
    if "→ {to}:" not in gs:
        bad.append("统一历史没带「谁 → 谁」")
    if "net_raw" not in gm or "min(1.0, net)" not in gm:
        bad.append("群心情净变化没封顶 ±1")
    if "MAX_DELTA" not in gm:
        bad.append("群心情没有单条事件上限")
    # 心情必须按设计 §13.1 线性累加；不许再用「饱和阻尼」这种补丁
    if "room ** 1.5" in md:
        bad.append("心情又被加了饱和阻尼（设计是 clamp(v+Δ)，防一边倒靠入口噪音丢弃）")
    if "MOOD_NOISE_DELTA" not in mj or "NOISE_DELTA" not in mj:
        bad.append("Δ 入口没有噪音丢弃（旁观会被算成负分）")
    if "PASSIVE_MAX_DELTA" not in mj:
        bad.append("被动观察没有 Δ 上限（她没说话也会被扣心情）")
    if "passive=bool(passive)" not in chat:
        bad.append("chat 没把 passive 传给判读（旁观不背锅失效）")
    if "MOOD_HALF_LIFE_HOURS" not in md or "MOOD_HALF_LIFE_HOURS" not in tn:
        bad.append("心情半衰期不可调")
    if "MEMORY_EMOTION_MIN_DELTA" not in chat:
        bad.append("记忆刻痕没有 |Δ| 门槛")
    if "def reset(" not in md:
        bad.append("心情没有重置（历史误判的 -1 清不掉）")
    if 'what.startswith(("模板"' not in chat:
        bad.append("缺少 /测函数 模板")
    return (not bad), "；".join(bad), "收发件人 / Δ护栏 / 模板自检 正常"


def check_g17():
    """统一历史 + 模板去重（用户 2026-11-22）。

    要求：
      1. group_scene.history_lines 是唯一出口（带收发件人）；
      2. 判心情 / 主模型上下文 都读它，条数一致（MOOD_JUDGE_CTX=8）；
      3. 主模型不再另拉一份 recent_group_msgs / napcat 原生消息；
      4. packet 在有 brief 人物卡时不再塞 micro 卡（同一个人不描述两遍）；
      5. llm 给 focus 卡不再套第二层 ▍标题；
      6. /测函数 模板 打的是真实请求（last_messages），不是重拼。
    """
    gs = read("services/group_scene.py")
    mj = read("services/mood_judge.py")
    chat = read("plugins/chat/__init__.py")
    pb = read("services/prompt_builder.py")
    llm = read("services/llm.py")
    tn = read("core/tuning.py")
    bad = []
    if "def history_lines(" not in gs:
        bad.append("group_scene 没有 history_lines（统一历史出口）")
    if "group_scene.history_lines(" not in mj:
        bad.append("判读上下文没走统一历史")
    if "group_scene.history_lines(" not in chat:
        bad.append("主模型上下文没走统一历史")
    if "'MOOD_JUDGE_CTX': 8," not in tn:
        bad.append("判读上下文不是 8 条")
    if "_give_focus" not in pb:
        bad.append("packet 还在重复塞 micro 人物卡")
    if '"▍这一条在跟你说话的人' in llm:
        bad.append("focus 卡还套了第二层标题")
    if "last_messages(_key_t)" not in chat:
        bad.append("/测函数 模板 没读真实请求")
    return (not bad), "；".join(bad), "统一历史 / 模板去重 正常"


def check_g18():
    """回复对象正确性 + 心情/好感两栏（用户 2026-11-22）。

    要求：
      1. 分段每行带消息号，并告诉模型「回谁就 <reply:消息号>」；
      2. 自动引用优先引「这一段里 @ 了她」那条，不是最后一条；
      3. 她引用谁，判读/好感就算到谁头上（不是最后说话人）；
      4. 有 `/测函数 面板` 只打心情 + 好感两栏；
      5. 找不到关联时允许沉默（不许凭空开新话题）。
    """
    seg = read("services/segments.py")
    chat = read("plugins/chat/__init__.py")
    llm = read("services/llm.py")
    bad = []
    if "<reply:" not in seg:
        bad.append("分段没告诉模型怎么引用某一条")
    if "_mens" not in chat:
        bad.append("自动引用没优先选「@ 了她」那条")
    if "_judge_qq" not in chat:
        bad.append("判读归属没按她引用的人算")
    if 'what.startswith(("面板"' not in chat:
        bad.append("缺少 /测函数 面板（心情+好感两栏）")
    if "往上往下都找不到关联" not in llm:
        bad.append("没有「找不到关联就沉默」的兜底")
    return (not bad), "；".join(bad), "回复对象 / 两栏面板 正常"


def check_g7():
    src = read("services/mood.py")
    body = func_body(src, "get_mood")
    ok = ("_dirty" in body) or ("_save(" not in body)
    return ok, "get_mood 读接口仍在写盘"


def check_f1():
    src = read("services/memory_v2.py")
    body = func_body(src, "recall")
    ok = (("bm25_norm" in body) or ("0.03 +" in body)
          or ("0.15 +" in body) or ("0.15+" in body))
    return ok, "BM25 仍是硬门槛"


def check_f2():
    src_p = read("services/proactive.py")
    src_c = read("plugins/chat/__init__.py")
    ok_p = re.search(r"memory_v2\.recall\([^)]*gid=", src_p, re.S) is not None
    ok_c = re.search(r"memory_v2\.recall\([^)]*gid=", src_c, re.S) is not None
    return (ok_p and ok_c), "proactive / [[PROFILE]] 的 recall 未传 gid"


def check_f2b():
    src = read("plugins/chat/__init__.py")
    n = len(re.findall(r"memory_v2\.remember\(", src))
    n_gid = len(re.findall(r"memory_v2\.remember\([\s\S]{0,900}?gid=", src))
    return (n_gid >= 1), "remember 调用 %d 处，带 gid 的 %d 处" % (n, n_gid)


def check_g1():
    src = read("plugins/chat/__init__.py")
    ok = ("patch_salience" in src) or ("upsert_turn" in src)
    return ok, "记忆刻痕仍用上一轮 abs(value)"


def check_a3b():
    """_WORKER_BUSY 必须在 finally 里复位，否则第一次回复后插话永久失效。"""
    src = read("plugins/chat/__init__.py")
    body = func_body(src, "_session_worker")
    has_set = "_WORKER_BUSY[session_key] = True" in body
    has_reset = "_WORKER_BUSY[session_key] = False" in body
    if not has_set:
        return False, "_WORKER_BUSY 未置位"
    return has_reset, "置位后没有复位（插话会永久失效）", "置位/复位成对"


def check_g8():
    """mood.touch 必须清掉 last_decay_at，否则刚聊完会一步衰减到 0。"""
    src = read("services/mood.py")
    body = func_body(src, "touch")
    ok = "last_decay_at" in body
    return ok, "touch 未清 last_decay_at（会瞬间衰减到 0）", "last_decay_at 已清"


def check_a4():
    """心情 touch 必须在 _early_observe 里（不能只挂在 Rule 上）。"""
    src = read("plugins/chat/__init__.py")
    body = func_body(src, "_early_observe")
    ok = "mood.touch(" in body
    return ok, "mood.touch 只挂在 Rule，block 插件命中时不会刷新", "touch 在观察器内"


def check_g19():
    """识图描述不能发出去（用户 2026-11-23：「偶尔还是会发送出图片的描述」）。

    要求：
      1. aura_logic 有 strip_media_echo，并在 _push_text 里调用（输出层兜底）；
      2. llm.py 视觉分支最后一条 system 明确「不要复述画面」；
      3. vision_agent 给模型的提示词也明确禁止复述画面。
    """
    al = read("services/aura_logic.py")
    llm = read("services/llm.py")
    va = read("plugins/vision_agent/__init__.py")
    bad = []
    if "def strip_media_echo" not in al:
        bad.append("aura_logic 缺 strip_media_echo")
    if "strip_media_echo(" not in func_body(al, "_push_text"):
        bad.append("_push_text 没调用 strip_media_echo")
    if "不要复述画面" not in llm:
        bad.append("视觉分支缺「不要复述画面」提示")
    if "绝对不要在回复里复述画面" not in va:
        bad.append("vision_agent 提示词没收紧")
    return (not bad), "；".join(bad), "识图描述不回声 / 标记不漏"


def check_g20():
    """用量图表 + 查用量不再误报 FinishedException（2026-11-23）。

    要求：
      1. /用量 与 /测函数 用量 都接 usage_chart.download_charts；
      2. 图表同时有横向排行（cht=bhg）和扇形（cht=p3）；
      3. 用量文本在 try 内组装，finish 放在 try 外 —— 否则 finish 抛的
         FinishedException 会被 `except Exception` 接住，多发一条「查用量失败」。
    """
    chat = read("plugins/chat/__init__.py")
    uc = read("services/usage_chart.py")
    bad = []
    if "download_charts" not in chat:
        bad.append("/用量 没接图表")
    if "cht=bhg" not in uc:
        bad.append("缺横向排行图")
    if "cht=p3" not in uc:
        bad.append("缺扇形图")
    if '_text = "\\n\\n".join(_out)' not in chat:
        bad.append("用量文本没在 try 内组装（finish 可能被 except 接住）")
    return (not bad), "；".join(bad), "用量图表 / finish 控制流正常"


def check_g21():
    """引用不复述 + 语音不念工具词（2026-11-23）。

    要求：
      1. 输出层有 `_QUOTE_ECHO_RE`，`_push_text` 会剥掉 `〔引用…〕`；
      2. `parse_reply` 的 `<voice>` 分支调用 `clean_voice_text`；
      3. `clean_voice_text` 会清 `stick:…` / 标签；
      4. 末尾硬要求明确「不要在回复里复述、朗读引用内容」。
    """
    al = read("services/aura_logic.py")
    llm = read("services/llm.py")
    chat = read("plugins/chat/__init__.py")
    bad = []
    if "_QUOTE_ECHO_RE" not in al:
        bad.append("aura_logic 缺引用块剥离")
    if "clean_voice_text" not in func_body(al, "parse_reply"):
        bad.append("parse_reply 的 voice 分支没清洗")
    if "def clean_voice_text" not in al or "stick" not in al:
        bad.append("缺 clean_voice_text / stick 清洗")
    if "不要在回复里复述、朗读引用内容" not in llm:
        bad.append("末尾硬要求缺引用复述禁令")
    if "clean_voice_text(text)" not in chat:
        bad.append("speak_voice 没做最后清洗")
    return (not bad), "；".join(bad), "引用不复述 / 语音不念工具词"


def check_g22():
    """普通网页链接要能读正文（2026-11-23）。

    要求：
      1. services/web_page.py 有 fetch_page / html_to_text / should_skip；
      2. chat 的 _enrich_text 会调用 web_page.fetch_page；
      3. 末尾硬要求明确「有【链接内容】就直接用，不要说点不开」。
    """
    wp = read("services/web_page.py")
    chat = read("plugins/chat/__init__.py")
    llm = read("services/llm.py")
    bad = []
    for name in ("def fetch_page", "def html_to_text", "def should_skip"):
        if name not in wp:
            bad.append(f"web_page 缺 {name}")
    if "web_page" not in chat or "fetch_page" not in chat:
        bad.append("_enrich_text 没接网页读取")
    if "【链接内容】" not in llm:
        bad.append("末尾硬要求没提【链接内容】")
    return (not bad), "；".join(bad), "普通网页可读 / 链接内容直接用"


def check_g23():
    """群聊分段不能截断网页正文（2026-11-23）。

    真机：群聊里 2488 字的链接正文被 `format` 截到 220 字，只剩一个「A」，
    她看不到题目，只能回「等会给你答案」。
    """
    sg = read("services/segments.py")
    bad = []
    if "_SEG_LINK_MAX" not in sg or "【链接内容" not in sg:
        bad.append("segments 缺链接正文放宽")
    if "_clip_text" not in sg:
        bad.append("push 没走 _clip_text")
    if "_LINK_MARK" not in sg:
        bad.append("format 没识别链接标记")
    return (not bad), "；".join(bad), "群聊链接正文不截断"


def check_g24():
    """群聊分段不能把同一条消息回多次（2026-11-23 串线）。

    真机：一段 3 条消息被整段处理一次，队列里剩下的 item 又被单独处理两次，
    同两条消息回了三次。要求整段取走后把段内每条 mid 登记为已处理。
    """
    chat = read("plugins/chat/__init__.py")
    body = func_body(chat, "_session_worker")
    bad = []
    if "_seg_records = await segments.wait_close" not in body:
        bad.append("没找到分段取走")
    if "for _r in _seg_records" not in body:
        bad.append("没遍历段内消息")
    if "_already_replied(session_key, _rm)" not in body:
        bad.append("没把段内 mid 登记为已处理（会重复回）")
    return (not bad), "；".join(bad), "分段消息不会被队列重复处理"


def check_g25():
    """哈希反查工具（2026-11-23）。

    群友用「我爱你」做 SHA-256 测试；模型不该靠猜。
    要求：
      1. services/hash_solver.py 有 solve/detect；
      2. chat 注册了 hash_decode；
      3. TOOL_CATALOG「查东西」里有 hash_decode，模型才知道调。
    """
    hs = read("services/hash_solver.py")
    chat = read("plugins/chat/__init__.py")
    llm = read("services/llm.py")
    bad = []
    if "def solve" not in hs or "def detect" not in hs:
        bad.append("hash_solver 缺 solve/detect")
    if '"name": "hash_decode"' not in chat:
        bad.append("chat 没注册 hash_decode")
    if "hash_decode" not in llm:
        bad.append("TOOL_CATALOG 没写 hash_decode")
    return (not bad), "；".join(bad), "哈希反查工具已接入"


def check_g26():
    """做题/填表能直接提交（2026-11-23）。

    要求：
      1. chat 注册了 web_fetch（打开 URL / GET 提交）；
      2. TOOL_CATALOG 里有 web_fetch；
      3. 末尾硬要求提醒「把答案拼进提交 URL，用 web_fetch 打开」；
      4. skill_switch 的 search 组覆盖 web_fetch。
    """
    chat = read("plugins/chat/__init__.py")
    llm = read("services/llm.py")
    sk = read("services/skill_switch.py")
    bad = []
    if '"name": "web_fetch"' not in chat:
        bad.append("chat 没注册 web_fetch")
    if "web_fetch" not in llm:
        bad.append("TOOL_CATALOG 没写 web_fetch")
    if "拼进提交 URL" not in llm:
        bad.append("末尾没提醒直接提交")
    if "web_fetch" not in sk:
        bad.append("skill_switch 没覆盖 web_fetch")
    return (not bad), "；".join(bad), "网页提交工具已接入"


def check_g27():
    """文本超时不能重试（2026-11-23 重复消息）。

    真机：QQ 协议层超时后 `_safe_send` 重试一次，第一次其实已经送达 →
    群里出现两条一模一样、引用也相同的消息。
    要求：只有图片/语音/视频重试；纯文本超时按已送达处理。
    """
    chat = read("plugins/chat/__init__.py")
    body = func_body(chat, "_safe_send")
    bad = []
    if "_msg_has_media" not in chat:
        bad.append("缺 _msg_has_media")
    if "_msg_has_media(msg)" not in body:
        bad.append("_safe_send 没区分媒体/文本")
    if "按已送达处理，不重试" not in body:
        bad.append("文本超时仍会重试")
    return (not bad), "；".join(bad), "文本超时不重试（防重复消息）"


def check_g28():
    """@ 她的消息不能被分段窗口漏掉（2026-11-23）。

    真机：一段 5 条，第 4 条是「老大 @她：我的今日小猪呢，刚刚怎么不理我」，
    第 5 条是别人问别的 → 模型只回第 5 条，@ 的那条被整段回复漏掉。
    要求：
      · segments 标 ⭐ / 截断保留 addressed / 段末列「必须回应」；
      · group_scene.history_lines 也带 ⭐（prompt 让模型找 ⭐，以前没有）；
      · prompt 不许再说「只接一个线程」「默认回最后一条」。
    """
    seg = read("services/segments.py")
    gs = read("services/group_scene.py")
    llm = read("services/llm.py")
    chat = read("plugins/chat/__init__.py")
    bad = []
    if "def is_addressed(" not in seg:
        bad.append("segments 缺 is_addressed")
    if "def _pick_rows(" not in seg:
        bad.append("segments 缺 _pick_rows")
    if "必须回应" not in seg:
        bad.append("segments 缺「必须回应」清单")
    if "每一条 ⭐ 都必须回应" not in seg:
        bad.append("segments 没要求逐条回应")
    if "⭐" not in func_body(gs, "history_lines"):
        bad.append("history_lines 没打 ⭐")
    if "只接一个线程" in llm or "只接**一个**线程" in llm:
        bad.append("prompt 仍写「只接一个线程」")
    if "多条 @ 你" not in llm:
        bad.append("prompt 缺「多条 @ 你」规矩")
    if 'session_key.startswith("group_") and not _seg_records' not in chat:
        bad.append("群聊无分段时没回退到处理过的历史")
    return (not bad), "；".join(bad), "@ 消息逐条回应 / 上下文只用处理过的历史"


def check_g29():
    """别人互相聊天时别乱接 + 不发旁白（2026-11-23）。

    用户：
      · 「收件人不是大家、是一个群友的情况下他也回复，接的词不达意」
      · 「有的时候说心里话，这是谁和谁说不是和我说，发出来了」
    要求：
      · 关注期：收件人明确是别人 → 不回；不确定 → 不回（不再「宁可多回」）。
      · 主动插话：最新一句是「某人 → 另一个群友」→ 跳过。
      · 插话上下文也用处理过的 history_lines，不用 recent_group_msgs 原串。
      · 输出层剥掉「谁跟谁说 / 我插不上话 / 系统怎么回」这类旁白。
      · 多条 @ 用一条消息答完（不一人一条，避免撞发送上限）。
    """
    chat = read("plugins/chat/__init__.py")
    al = read("services/aura_logic.py")
    llm = read("services/llm.py")
    seg = read("services/segments.py")
    bad = []
    focus = func_body(chat, "_judge_focus_follow")
    if "收件人是" not in focus:
        bad.append("关注期没查收件人是不是别人")
    if "不确定（{ans[:8]}）→ 不回" not in focus:
        bad.append("关注期「不确定」仍按回处理")
    loop = func_body(chat, "_random_chat_loop")
    if "不插话" not in loop or "to_kind" not in loop:
        bad.append("主动插话没挡「某人 → 群友」")
    if "_ctx_lines = group_scene.history_lines" not in loop:
        bad.append("插话上下文仍用未处理原串")
    if "def strip_meta_talk" not in al or "strip_meta_talk(t)" not in al:
        bad.append("输出层没剥「点评对话本身」旁白")
    if "别点评对话本身" not in llm:
        bad.append("prompt 没禁止点评对话")
    if "一条消息全部答完" not in seg:
        bad.append("多条 @ 仍可能一人一条")
    if "整段发完整" not in chat:
        bad.append("带 @/引用的长回复仍会被截断")
    return (not bad), "；".join(bad), "别人互聊不乱接 / 旁白不外发 / 多条 @ 一条答完"


def check_g30():
    """白月光模式 / prompt 体检 / 好感注入（2026-11-23）。

    用户：
      · 「心情和好感并没有关注太多」→ relation 以前根本没进 prompt；
      · 「统计她更容易引用回复/艾特谁，学那个人的说话方式」→ 白月光；
      · 「想看 prompt 每段关注度 + 缓存命中率」→ /测函数 prompt。
    """
    wm = read("services/white_moon.py")
    llm = read("services/llm.py")
    prof = read("services/profiles.py")
    chat = read("plugins/chat/__init__.py")
    pr = read("services/prompt_report.py")
    bad = []
    for fn in ("def note_incoming(", "def person_moon(", "def group_moon(",
               "def render(", "def profile_note("):
        if fn not in wm:
            bad.append(f"white_moon 缺 {fn}")
    if '"relation", "mood"' not in llm:
        bad.append("relation/mood 没挪到 depth 区")
    if "white_moon.render" not in llm:
        bad.append("prompt 没注入白月光")
    if "white_moon.profile_note" not in prof:
        bad.append("人物档案没标白月光")
    if "from services import white_moon" not in chat or "_wm.note_incoming(" not in chat:
        bad.append("群聊入口没统计「A @/引用 B」")
    if "prompt_report.report_text" not in chat:
        bad.append("/测函数 prompt 没接")
    if "def section_report" not in pr or "def cache_report" not in pr:
        bad.append("prompt_report 不完整")
    return (not bad), "；".join(bad), "白月光统计/注入 + 好感进 depth + prompt 体检"


def check_g31():
    """角色扮演 provider 拆分 + prompt 图表（2026-11-23）。

    用户：「角色扮演用 deepseek 4.1-flash（API 名 deepseek-flash）；
           两个三方检测心情和收发件人的用 chat；GLM 只做视觉。」
    要求：
      · 主聊天/工具循环走 ROLEPLAY_*（DeepSeek deepseek-flash）；
      · 心情判读/收发件人审核仍走 deepseek-chat；
      · GLM 文本 preflight 关闭（REFEREE_PREFLIGHT=false），GLM 只留视觉；
      · `/测函数 prompt` 和 `/用量` 一样出横向柱状 + 扇形图。
    """
    cfg = read("services/config.py")
    llm = read("services/llm.py")
    chat = read("plugins/chat/__init__.py")
    pr = read("services/prompt_report.py")
    uc = read("services/usage_chart.py")
    bad = []
    for k in ("ROLEPLAY_API_KEY", "ROLEPLAY_BASE_URL", "ROLEPLAY_MODEL"):
        if k not in cfg:
            bad.append(f"config 缺 {k}")
    if "deepseek-flash" not in cfg:
        bad.append("角色扮演默认模型不是 deepseek-flash")
    if "ROLEPLAY_BASE_URL" not in llm or "ROLEPLAY_MODEL" not in llm:
        bad.append("主聊天没用 ROLEPLAY_*")
    _env = read(".env")
    if _env and "REFEREE_PREFLIGHT=false" not in _env:
        bad.append("GLM 文本 preflight 还开着")
    if "prompt_report.download_charts" not in chat:
        bad.append("/测函数 prompt 没发图表")
    if "def chart_urls" not in pr or "def download_charts" not in pr:
        bad.append("prompt_report 缺图表函数")
    if "def build_charts" not in uc:
        bad.append("usage_chart 缺通用 build_charts")
    return (not bad), "；".join(bad), "角色扮演 deepseek-flash / 三方 chat / GLM 视觉 / prompt 图表"


def check_g32():
    """人格 ID 与登录 QQ 解耦（2026-11-23）。

    用户：「标签自己的话全部改成 NAPCAT_ACCOUNT……包括判定收发件人发给她
           也是 NAPCAT_ACCOUNT……让她认为我就是 NAPCAT_ACCOUNT，
           要不然我 QQ 被封一个记忆就不能复用了？」

    要求：
      · services/identity.py：人格 ID 落盘固定，登录过的号都是别名；
      · 群聊现场/对话日志里她自己的发言统一记成人格 ID；
      · 「@ 的是不是她 / 收件人是不是她」认所有别名（含旧号）；
      · 第三方插件里判 bot 的地方也走别名。
    """
    bad = []
    try:
        idn = read("services/identity.py")
    except Exception as e:
        return False, f"读 services/identity.py 失败: {e}"
    for k in ("def canonical", "def is_self", "def note_account",
              "def canon", "_scan_bot_accounts", "_env_aliases"):
        if k not in idn:
            bad.append(f"identity 缺 {k}")
    if "SELF_ALIASES" not in idn:
        bad.append("identity 不支持 SELF_ALIASES 旧号配置")
    chat = read("plugins/chat/__init__.py")
    for k in ("def _self_id(", "def _is_self_qq(", "def _canon_at(",
              "identity_mod.note_account"):
        if k not in chat:
            bad.append(f"chat 缺 {k}")
    if "str(bot.self_id), \"；\".join(said)" in chat:
        bad.append("对话日志仍用 bot.self_id 记她的话")
    gs = read("services/group_scene.py")
    for k in ("def _is_bot_qq(", "def _to_is_bot(", "_identity.canon(qq)"):
        if k not in gs:
            bad.append(f"group_scene 缺 {k}")
    wm = read("services/white_moon.py")
    if "_identity.is_self(a)" not in wm or "_identity.is_self(b)" not in wm:
        bad.append("white_moon 没按别名排除她自己")
    llm = read("services/llm.py")
    if "identity_mod.is_self" not in llm:
        bad.append("私聊历史没按别名认「这是你之前的回复」")
    third = read("plugins/third/nonebot_plugin_rollpig_plus/handlers/roast.py")
    if "_identity.is_self" not in third:
        bad.append("rollpig 回复预约没按别名认 bot")
    env = read(".env")
    if env and "NAPCAT_ACCOUNT=" not in env:
        bad.append(".env 没配 NAPCAT_ACCOUNT")
    return (not bad), "；".join(bad), "人格 ID 落盘 / 旧号别名 / 日志与收发件人统一"


def check_g33():
    """戳一戳次数屏蔽（2026-11-23）。

    用户：「2 小时内该用户戳超过三次则不予回复戳一戳消息，2 小时后再看。」
    要求：
      · services/poke_guard.py 用滑动窗口按用户计数；
      · 第 4 次起 handler 直接 return（不回、也不戳回），但上下文照记；
      · 阈值走 core/tuning.py（POKE_MAX_PER_WINDOW / POKE_WINDOW_HOURS）。
    """
    bad = []
    try:
        pg = read("services/poke_guard.py")
    except Exception as e:
        return False, f"读 services/poke_guard.py 失败: {e}"
    for k in ("def note", "def count", "WINDOW_SEC", "MAX_PER_WINDOW"):
        if k not in pg:
            bad.append(f"poke_guard 缺 {k}")
    tun = read("core/tuning.py")
    for k in ("POKE_MAX_PER_WINDOW", "POKE_WINDOW_HOURS"):
        if k not in tun:
            bad.append(f"tuning 缺 {k}")
    chat = read("plugins/chat/__init__.py")
    if "poke_guard.note(uid)" not in chat:
        bad.append("chat 没接 poke_guard")
    if "不回、不戳回" not in chat:
        bad.append("被屏蔽时没写明不戳回")
    if chat.find("poke_guard.note(uid)") > chat.find('call_api("group_poke"'):
        bad.append("屏蔽判断在戳回之后（顺序反了）")
    return (not bad), "；".join(bad), "2 小时滑动窗口 / 第 4 次不回不戳回 / 可调阈值"


def check_g34():
    """算力机开关 / Harness 桥 / self_tool 互斥（2026-11-24）。

    用户：
      · 「NODE_COMPUTE=1 走 node，0 的时候算力机功能可以调用 third 里的本机插件」
      · 「加个 aurabot 那种 deepseek harness 回话链接 QQ 会话，可以直接改代码」
      · 「跟 self_tool 冲突，就 .env 用 0/1 关掉 self_tool 打开 harness」
    """
    bad = []
    cfg = read("services/config.py")
    for k in ("NODE_COMPUTE", "CODE_AGENT", "HARNESS_ENABLED"):
        if k not in cfg:
            bad.append(f"config 缺 {k}")
    boot = read("bot.py")
    if "_SKIP_WHEN_NO_NODE" not in boot:
        bad.append("bot.py 没按 NODE_COMPUTE 跳过 draw/song_cover")
    if "CODE_AGENT" not in boot or "self_tools" not in boot:
        bad.append("bot.py 没按 CODE_AGENT 跳过 self_tools")
    llm = read("services/llm.py")
    for k in ("_NODE_ONLY_TOOLS", "HARNESS_ENABLED", "name.startswith(\"self_tool_\")"):
        if k not in llm:
            bad.append(f"llm 缺 {k}")
    if "if not NODE_COMPUTE:" not in llm:
        bad.append("工具目录没按 NODE_COMPUTE 切换")
    try:
        hs = read("services/harness.py")
    except Exception as e:
        return False, f"读 services/harness.py 失败: {e}"
    for k in ("HARNESS_CLI", "HARNESS_URL", "def ask", "def redact", "HARNESS_OWNER_ONLY"):
        if k not in hs:
            bad.append(f"harness 缺 {k}")
    try:
        hp = read("plugins/harness/__init__.py")
    except Exception as e:
        return False, f"读 plugins/harness 失败: {e}"
    for k in ("harness_ask", "harness_status", "harness_reset", "is_owner"):
        if k not in hp:
            bad.append(f"harness 插件缺 {k}")
    env = read(".env")
    if env:
        for k in ("NODE_COMPUTE=", "CODE_AGENT=", "HARNESS_BACKEND="):
            if k not in env:
                bad.append(f".env 缺 {k}")
    return (not bad), "；".join(bad), "NODE_COMPUTE / CODE_AGENT / harness 桥 / self_tool 互斥"


def check_g35():
    """模型自由搭配（2026-11-24）。

    用户：「有的用户直接用 deepseek v4 flash 做视觉，有些想换 glm 做主对话模型。
           env 可以配各种 key 接口，自由搭配识图和对话的模型，两个 chat 检测也能选。」
    """
    bad = []
    try:
        pv = read("services/providers.py")
    except Exception as e:
        return False, f"读 services/providers.py 失败: {e}"
    for k in ("ROLES", "BUILTIN", "def resolve", "def chat_url",
              "ROLEPLAY", "JUDGE", "AUDIT", "VISION"):
        if k not in pv:
            bad.append(f"providers 缺 {k}")
    if "PROVIDER_" not in pv or "_BASE_URL" not in pv:
        bad.append("providers 不支持自定义中转站")

    cfg = read("services/config.py")
    if "_resolve_provider(\"roleplay\")" not in cfg:
        bad.append("config 的 ROLEPLAY_* 没走 providers")

    for f, role in (("services/mood_judge.py", "judge"),
                    ("services/scene_auditor.py", "audit"),
                    ("services/conv_state.py", "conv_state"),
                    ("services/expression.py", "expression"),
                    ("services/profile_miner.py", "profile")):
        src = read(f)
        if f'_resolve_provider("{role}")' not in src:
            bad.append(f"{f} 没走 providers({role})")

    vis = read("plugins/vision_agent/__init__.py")
    for k in ("_vision_targets", 'providers.resolve("vision")', "_VISION_FALLBACKS"):
        if k not in vis:
            bad.append(f"vision_agent 缺 {k}")

    env = read(".env")
    if env and "模型自由搭配" not in env:
        bad.append(".env 没有模型搭配说明")
    return (not bad), "；".join(bad), "每个角色可选 provider/模型，识图与两个 chat 检测都能换"


def check_g36():
    """群聊全局 / 省流模式（2026-11-24）。

    用户：「让用户选择关注群消息，或者接话/回复时拉附近 8 条上下文……
           判定是否接话用硬逻辑（收发件人 + 艾特）……不影响群白月光，私聊不变。」
    """
    bad = []
    cfg = read("services/config.py")
    if "GROUP_WATCH_MODE" not in cfg:
        bad.append("config 缺 GROUP_WATCH_MODE")
    chat = read("plugins/chat/__init__.py")
    if 'GROUP_WATCH_MODE == "lite"' not in chat:
        bad.append("chat 没有省流分支")
    if "硬信号命中" not in chat:
        bad.append("省流分支没有硬信号判定")
    if 'GROUP_WATCH_MODE != "lite"' not in chat:
        bad.append("省流模式没关掉主动插话/表达学习")
    # 省流分支必须在「白月光统计」之后、且在「安静开关」之后
    i_moon = chat.find("_wm.note_incoming(")
    i_lite = chat.find('GROUP_WATCH_MODE == "lite"')
    i_switch = chat.find("chat_switch.is_off(")
    if i_moon < 0 or i_lite < 0 or i_switch < 0:
        bad.append("找不到白月光/省流/安静开关的位置")
    else:
        if not (i_moon < i_lite):
            bad.append("省流分支跑在白月光统计之前（白月光会失效）")
        if not (i_switch < i_lite):
            bad.append("省流分支跑在安静开关之前（会绕过 /安静）")
    env = read(".env")
    if env and "GROUP_WATCH_MODE=" not in env:
        bad.append(".env 缺 GROUP_WATCH_MODE")
    return (not bad), "；".join(bad), "full=全部消息都判定｜lite=只回 @/引用/喊名字/收件人是她"


def check_g37():
    """引用精确到消息号 + 同一件事不回两遍（2026-11-24）。

    用户：「回复两次」「引用回复的时候回错人，回成上面那条或者下面那条」。
    """
    bad = []
    chat = read("plugins/chat/__init__.py")
    for k in ("_valid_mids", "不在这一段里", "是行号 →"):
        if k not in chat:
            bad.append(f"chat 缺引用校验 {k}")
    if "GROUP_REPLY_COOLDOWN" not in chat:
        bad.append("chat 没有群聊回复冷却")
    seg = read("services/segments.py")
    if "不是方括号里的编号" not in seg:
        bad.append("segments 没提示「用 #mid 不是行号」")
    al = read("services/aura_logic.py")
    if "〔[^〕]{0,300}〕" not in al:
        bad.append("aura_logic 没清 〔…〕 协议残留")
    if "<reply:\\s*#?(\\d+)" not in al:
        bad.append("aura_logic 不认 <reply:#id>")
    return (not bad), "；".join(bad), "引用按真实 mid（行号自动映射/非法丢弃）+ 群聊冷却防回两遍"



def check_g38():
    """记忆召回：干净 query + 人物兜底 + 已回标记（2026-11-24）。

    用户：「最近很少主动去找记忆了，提起相似的事情还是要回想起来一下，
          包括一个人。并不是只翻档案。」
    """
    bad = []
    pb = read("services/prompt_builder.py")
    for k in ("_recall_query", "memory_v2.recent_about", "_RECALL_SKIP_HEAD"):
        if k not in pb:
            bad.append(f"prompt_builder 缺 {k}")
    mem = read("services/memdb.py")
    if "def recent_about(" not in mem:
        bad.append("memdb 缺 recent_about")
    mv = read("services/memory_v2.py")
    if "def recent_about(" not in mv:
        bad.append("memory_v2 没透传 recent_about")
    gs = read("services/group_scene.py")
    if "replied_mids" not in gs or "〔已回〕" not in gs:
        bad.append("history_lines 没标〔已回〕")
    llm = read("services/llm.py")
    if "[[MEMORY|关键词]]" not in llm or "提到往事就必须回想" not in llm:
        bad.append("规矩里没要求「提到往事先翻记忆」")
    return (not bad), "；".join(bad), "干净召回 query / 人物近期兜底 / 上下文标〔已回〕/ 规矩要求回想"


def check_g39():
    """@ 插队 + 唱歌去重（2026-11-24）。

    用户：「我记得我们艾特是插队的」「群里有人让她唱首歌，结果她发了三次歌」。
    """
    bad = []
    seg = read("services/segments.py")
    for k in ("_URGENT", "插队"):
        if k not in seg:
            bad.append(f"segments 缺 {k}")
    if "if _URGENT.get(key) and size(key) > 0:" not in seg:
        bad.append("wait_close 没处理插队")
    chat = read("plugins/chat/__init__.py")
    for k in ("_SING_RECENT", "别重复点歌", "[URGENT]"):
        if k not in chat:
            bad.append(f"chat 缺 {k}")
    return (not bad), "；".join(bad), "@ 段立刻处理 + 跳过回复间隔；sing 在途/近期去重"



def check_g40():
    """图书馆路由 + 识图原图 + 调用可见性（2026-11-24）。

    用户：「napcat 已登录，我需要查看对图书馆和记忆的调用。这两项感觉她根本不用。」
    """
    bad = []
    ac = read("services/agent_client.py")
    for k in ("SMART_QUERY_ENDPOINT", "_looks_like_chat", "stats_text",
              "_RECALL_ENDPOINT_MISSING"):
        if k not in ac:
            bad.append(f"agent_client 缺 {k}")
    if "/api/agent/smart-query" not in ac:
        bad.append("agent_client 没有 smart-query 兜底")
    pb = read("services/prompt_builder.py")
    if "RECALL_STATS" not in pb or "recall_stats_text" not in pb:
        bad.append("prompt_builder 没有召回统计")
    vis = read("plugins/vision_agent/__init__.py")
    for k in ("优先原图", "get_image", "可能还是压缩图"):
        if k not in vis:
            bad.append(f"vision_agent 缺 {k}")
    chat = read("plugins/chat/__init__.py")
    if "recall_stats_text" not in chat or "stats_text" not in chat:
        bad.append("/测函数 记忆 没接统计")
    return (not bad), "；".join(bad), "图书馆走真实路由 + 语料不匹配自动暂停；识图多源取原图；/测函数 记忆"



CHECKS = [
    ("C1", "破冰可独立执行", "behavior", check_c1),
    ("A1", "插话设置 tool context", "behavior", check_a1),
    ("A1b", "插话 addressed=False", "behavior", check_a1b),
    ("A2", "插话查人对人安静", "behavior", check_a2),
    ("A3", "插话/worker 互斥", "behavior", check_a3),
    ("A3b", "worker 忙标记复位", "behavior", check_a3b),
    ("A4", "touch 在观察器内", "behavior", check_a4),
    ("B1", "_need_reply 查安静开关", "behavior", check_b1),
    ("D1", "Rule 不带记账副作用", "behavior", check_d1),
    ("G4", "REPLY_INTERVAL 的 last 更新", "behavior", check_g4),
    ("E2", "表情发送检查返回值", "behavior", check_e2),
    ("G5", "limit_hanzi 非汉字也计数", "behavior", check_g5),
    ("G3", "MAX_GROUP_RECENT=16", "config", check_g3),
    ("G7", "get_mood 不写盘", "config", check_g7),
    ("G8", "touch 清空闲基准", "config", check_g8),
    ("G9", "心情惯性不被 .env 覆盖", "config", check_g9),
    ("G11", "涨幅驱动（不是水平驱动）", "record", check_g11),
    ("G12", "峰值事件 / 印象最深一件事", "record", check_g12),
    ("G13", "群心情（单人 0.4 / 整群 0.7）", "record", check_g13),
    ("G14", "协议残留清理（不漏心里话）", "record", check_g14),
    ("G15", "分段回复 / 用量 / 以图搜源", "record", check_g15),
    ("G16", "收发件人贯通 / Δ 护栏 / 模板", "record", check_g16),
    ("G17", "统一历史 / 模板去重", "record", check_g17),
    ("G18", "回复对象 / 心情好感两栏", "record", check_g18),
    ("G19", "识图描述不回声 / 标记不漏", "record", check_g19),
    ("G20", "用量图表 / finish 控制流", "record", check_g20),
    ("G21", "引用不复述 / 语音不念工具词", "record", check_g21),
    ("G22", "普通网页可读 / 链接内容直接用", "record", check_g22),
    ("G23", "群聊链接正文不截断", "record", check_g23),
    ("G24", "分段消息不重复回（串线）", "record", check_g24),
    ("G25", "哈希反查工具", "record", check_g25),
    ("G26", "网页提交工具（web_fetch）", "record", check_g26),
    ("G27", "文本超时不重试（防重复）", "record", check_g27),
    ("G28", "@ 消息逐条回应 / 上下文已处理", "record", check_g28),
    ("G29", "别人互聊不乱接 / 旁白不外发", "record", check_g29),
    ("G30", "白月光 / prompt 体检 / 好感注入", "record", check_g30),
    ("G31", "角色扮演 deepseek-flash / 三方 chat / prompt 图表", "record", check_g31),
    ("G32", "人格 ID 与登录 QQ 解耦（换号不丢记忆）", "record", check_g32),
    ("G33", "戳一戳次数屏蔽（2h / 3 次）", "record", check_g33),
    ("G34", "算力机开关 / Harness 桥 / self_tool 互斥", "record", check_g34),
    ("G35", "模型自由搭配（识图/对话/两个检测都能换）", "record", check_g35),
    ("G36", "群聊全局 / 省流模式", "record", check_g36),
    ("G37", "引用精确到消息号 / 同一件事不回两遍", "record", check_g37),
    ("G38", "记忆召回（干净 query / 人物兜底 / 已回标记）", "record", check_g38),
    ("G39", "@ 插队 / 唱歌去重", "record", check_g39),
    ("G40", "图书馆路由 / 识图原图 / 调用可见", "record", check_g40),
    ("F1", "BM25 降为加分", "recall", check_f1),
    ("F2", "recall 传 gid", "recall", check_f2),
    ("F2b", "remember 带群号", "recall", check_f2b),
    ("G1", "Δ 时序（patch_salience）", "record", check_g1),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    rows = []
    failed = 0
    for code, desc, group, fn in CHECKS:
        try:
            r = fn()
        except Exception as e:  # 检查本身出错也算失败
            r = (False, "check error: %s: %s" % (type(e).__name__, e))
        ok = bool(r[0])
        # 支持 (ok, fail_detail) / (ok, fail_detail, ok_detail)
        if ok:
            detail = r[2] if len(r) > 2 else "OK"
        else:
            detail = r[1] if len(r) > 1 else ""
        if not ok:
            failed += 1
        rows.append((code, group, desc, "PASS" if ok else "FAIL", detail))

    print("=" * 78)
    print("功能链路体检  %s" % ("(strict)" if args.strict else ""))
    print("=" * 78)
    print("%-5s %-9s %-26s %-6s %s" % ("编号", "组", "检查项", "结果", "说明"))
    print("-" * 78)
    for code, group, desc, status, detail in rows:
        print("%-5s %-9s %-26s %-6s %s" % (code, group, desc, status, detail))
    print("-" * 78)
    print("合计 %d 项，FAIL %d 项" % (len(rows), failed))
    print("详见 docs/feature-audit.md")

    if args.strict and failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
