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
