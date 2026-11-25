#!/usr/bin/env python3
# scripts/check_design_docs.py
"""设计口径一致性检查（DESIGN §3 ↔ core/design_constants.py）。

**它证明什么、不证明什么（2026-11-25 明确）**：
  默认模式只证明「文档写的数 = 设计常量」——即文档自洽，不证明运行时。
  运行时接线 / 线上 .env 覆盖是否把不变式搞坏，用 `--runtime` 单独查。

检查内容：
1. DESIGN §3.3 预算表必须**列全**每个块，且数值等于常量；
2. DESIGN §3.1 关键值（半衰期/步长/限幅/P0/Q/R）必须等于常量；
3. 全 docs 的 `target` 必须是 5 值（speaker/recipient/other/world/self），
   出现 3 值 `speaker/recipient/world` 直接判错（ADR-021）；
4. 子文档里重复声明预算键且值不同 → 警告（人工确认后修）。
5. `--runtime`：每个设计键都必须有真实读点（AST，排除 UNWIRED）；
   线上 .env 覆盖后仍要满足「单块上限 ≥ 该块设计目标」「风格合计 ≥ 分项和」
   「PROMPT_BUDGET ≥ 常态目标」。

退出码：有 FAIL = 1（CI 用）。--strict 时警告也算失败。

用法：
    python3 scripts/check_design_docs.py
    python3 scripts/check_design_docs.py --strict
    python3 scripts/check_design_docs.py --runtime     # 额外查运行时接线
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from core import design_constants as D  # noqa: E402

DESIGN = ROOT / "docs" / "DESIGN.md"
DOCS = ROOT / "docs"

NUM = re.compile(r"\d+(?:\.\d+)?")


# ---------------------------------------------------------------- 解析
def load_sections(text: str) -> dict[str, list[tuple[int, str]]]:
    """按 `### 3.x` 切段，返回 {section: [(行号, 行文本)]}。"""
    out: dict[str, list[tuple[int, str]]] = {}
    cur = None
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^###\s+(3\.\d+)", line)
        if m:
            cur = m.group(1)
            out[cur] = []
            continue
        if re.match(r"^##\s", line):
            cur = None
            continue
        if cur:
            out[cur].append((i, line))
    return out


def find_row(section: list[tuple[int, str]], *aliases: str):
    """找到第一行包含任一别名的**表格行**（跳过 > 开头的说明）。"""
    for ln, line in section:
        if line.strip().startswith(">"):
            continue
        if any(a in line for a in aliases):
            return ln, line
    return None, ""


def nums(line: str) -> list[float]:
    return [float(x) for x in NUM.findall(line)]


def has_num(line: str, want: float) -> bool:
    return any(abs(n - float(want)) < 1e-9 for n in nums(line))


# ---------------------------------------------------------------- 检查 1：预算表
# §3.3 是**设计目标**（BUDGET），不是运行时单块上限（CTX）。
# 2026-11-25：CTX_* 按线上真实块长校准后合计 4620，拿它比 §3.3 的 2120 会永远 FAIL。
BUDGET_ROWS = [
    ("persona", ("persona",), D.BUDGET["persona"]),
    ("expression", ("expression",), D.BUDGET["expression"]),
    ("protocol", ("协议",), D.BUDGET["protocol"]),
    ("state", ("状态卡",), D.BUDGET["state"]),
    ("profile", ("profile", "档案"), D.BUDGET["profile"]),
    ("summary", ("群摘要", "摘要"), D.BUDGET["summary"]),
    ("evidence", ("证据",), D.BUDGET["evidence"]),
    ("style", ("原话样本", "style_samples"), D.BUDGET["style"]),
    ("rhythm", ("表达节奏", "rhythm"), D.BUDGET["rhythm"]),
    ("history", ("历史",), D.BUDGET["history"]),
    ("post_rules", ("post_rules",), D.BUDGET["post_rules"]),
    ("anchor", ("引用锚点", "anchor"), D.BUDGET["anchor"]),
    ("tools", ("工具",), D.BUDGET["tools"]),
    ("multitarget", ("多目标",), D.BUDGET["multitarget"]),
    ("white_moon", ("白月光",), D.BUDGET["white_moon"]),
]


def check_budget(sec: list[tuple[int, str]]) -> list[str]:
    bad = []
    for key, aliases, want in BUDGET_ROWS:
        ln, line = find_row(sec, *aliases)
        if not line:
            bad.append("§3.3 缺块 `%s`（应在预算表里列 %d）" % (key, want))
            continue
        if not has_num(line, want):
            bad.append("§3.3 `%s` 值不等于 %d（第 %d 行：%s）"
                       % (key, want, ln, line.strip()[:80]))
    # 常态合计：与 ctx_total() 对得上（±20 只够「2.12k / 2120」的写法误差，
    # 不能拿「2.2k」糊过去 —— 那已经是 80 token 的漂移）
    ln, line = find_row(sec, "常态合计")
    if line:
        got = nums(line)
        want = D.budget_total()
        if not got or abs(got[0] * 1000 - want) > 20:
            bad.append("§3.3 常态合计与 ctx_total()=%d 不符（第 %d 行：%s）"
                       % (want, ln, line.strip()[:80]))
    else:
        bad.append("§3.3 缺「常态合计」行")
    body = "\n".join(l for _, l in sec)
    if "字符" not in body:
        bad.append("§3.3 预算单位必须是「字符」（与 PROMPT_BUDGET 同单位）")
    try:
        from core import tuning as _t
        _pb = float(_t.DEFAULTS.get("PROMPT_BUDGET") or 0)
        if _pb and abs(_pb - D.budget_total()) > max(150, 0.1 * D.budget_total()):
            bad.append("PROMPT_BUDGET=%.0f 与 §3.3 常态合计 %d 相差 >10%%"
                       % (_pb, D.budget_total()))
    except Exception:
        pass
    if "12k" not in body:
        bad.append("§3.3 缺 12k 硬上限行")
    elif "long_doc" not in body:
        bad.append("§3.3 12k 未写触发条件 `long_doc`")
    return bad


# ---------------------------------------------------------------- 检查 2：§3.1 关键值
def check_mood(sec: list[tuple[int, str]]) -> list[str]:
    bad = []
    rules = [
        (("半衰期",), [str(D.MOOD["half_life_group_min"]) + "min",
                       str(D.MOOD["half_life_private_min"] // 60) + "h",
                       str(D.MOOD["half_life_global_min"] // 60) + "h"]),
        (("观测步长",), [str(D.MOOD["step_1"]), str(D.MOOD["step_2"])]),
        (("人设敏感度",), [str(D.MOOD["sensitivity_min"]),
                           str(D.MOOD["sensitivity_max"])]),
        (("饱和",), [str(D.MOOD["saturation"])]),
        (("步进硬上限", "单次硬上限", "硬限幅"), [str(D.MOOD["hard_cap"])]),
        (("不确定度初值", "卡尔曼初值"), [str(D.MOOD["P0"])]),
        (("过程噪声",), [str(D.MOOD["Q_base"]), str(D.MOOD["Q_slope"])]),
        (("观测噪声",), [str(D.MOOD["R_base"]), str(D.MOOD["R_slope"])]),
        (("锚点触发",), [str(D.ANCHOR["every_turns"]),
                         str(D.ANCHOR["idle_min"])]),
        (("锚点增益",), [str(D.ANCHOR["gain"]),
                         str(D.ANCHOR["deadband"]),
                         str(D.ANCHOR["max_move"])]),
        # 快速通道 / 强负面护栏已于 ADR-038 删除（2026-11-25）：
        # 文档 §3.1 里那一行改成「已删除」，不再要求出现 0.45 / 0.25 / 1.5。
    ]
    for aliases, tokens in rules:
        ln, line = find_row(sec, *aliases)
        if not line:
            bad.append("§3.1 缺行：%s" % (aliases[0],))
            continue
        for t in tokens:
            if t not in line:
                bad.append("§3.1 `%s` 缺 `%s`（第 %d 行：%s）"
                           % (aliases[0], t, ln, line.strip()[:80]))
    return bad


# ---------------------------------------------------------------- 检查 2b：输出口径
def check_output(text: str) -> list[str]:
    bad = []
    if f"单泡 ≤{D.OUTPUT['bubble_chars']}" not in text:
        bad.append("§9.1 未写「单泡 ≤%d」" % D.OUTPUT["bubble_chars"])
    if f"≤{D.OUTPUT['bubbles_max']} 泡" not in text:
        bad.append("§9.1 未写「≤%d 泡」" % D.OUTPUT["bubbles_max"])
    if "规则可召回" not in text or "可护栏" not in text:
        bad.append("§9.3 未写「规则可召回、可护栏，不可判决」")
    return bad


# ---------------------------------------------------------------- 检查 2c：量纲
def check_units(text: str) -> list[str]:
    bad = []
    if "归一化空间" not in text:
        bad.append("§3.1 缺量纲声明：公式必须在 v∈[-1,1] 空间计算")
    if "×100" not in text:
        bad.append("§3.1 缺落盘口径：展示/落盘 ×100")
    return bad


# ---------------------------------------------------------------- 检查 3：target 5 值
THREE = re.compile(r"speaker\s*/\s*recipient(?!\s*/\s*other)")
SET_RE = re.compile(r"\{([^{}]*speaker[^{}]*)\}")
# 明确在描述「旧版/已废弃」的行不算违规（审计、ADR 理由里必须能引用旧值）
HISTORICAL = ("旧版", "旧的", "已废弃", "已删", "3 值", "三值", "M3", "ADR-021")


def check_target() -> list[str]:
    bad = []
    allowed = set(D.ENUMS["target"])
    for p in sorted(DOCS.glob("*.md")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if any(h in line for h in HISTORICAL):
                continue
            if THREE.search(line):
                bad.append("%s:%d 出现旧 3 值 `speaker/recipient`（应 5 值）\n      %s"
                           % (p.name, i, line.strip()[:100]))
            if "target" in line:
                for m in SET_RE.finditer(line):
                    items = {x.strip().strip("`'\"")
                             for x in re.split(r"[,，/|]", m.group(1))
                             if x.strip()}
                    items = {x for x in items
                             if x in ("speaker", "recipient", "other",
                                      "world", "self")}
                    if not items:
                        continue
                    unknown = items - allowed
                    if unknown:
                        bad.append("%s:%d target 含未定义值：%s"
                                   % (p.name, i, sorted(unknown)))
                    elif items == {"speaker", "recipient", "world"}:
                        bad.append("%s:%d target 仍是旧 3 值：%s（应 5 值，"
                                   "至少补 other）"
                                   % (p.name, i, sorted(items)))
    return bad


# ---------------------------------------------------------------- 检查 4：子文档重复预算
# 只认「键 + 紧跟的 3 位数」这种声明式写法，避免把「32 处协议」「protocol 1200 字」当冲突
DUP_RULES = [
    ("persona", D.BUDGET["persona"],
     re.compile(r"persona(?!_)[^\n\d]{0,40}?≤?\s*(?<!\d)(\d{3})(?!\d)"), ("合计", "不含")),
    ("custom", D.BUDGET["custom"],
     re.compile(r"custom(?!_)[^\n\d]{0,40}?≤?\s*(?<!\d)(\d{3})(?!\d)"), ("合计", "不含")),
    ("协议", D.BUDGET["protocol"],
     re.compile(r"协议(?!块)[^\n\d]{0,20}?≤?\s*(?<!\d)(\d{3})(?!\d)"), ("合计",)),
    ("证据", D.BUDGET["evidence"],
     re.compile(r"(?:证据|top5)[^\n]{0,30}?≤\s*(?<!\d)(\d{3})(?!\d)\s*token"), ("合计",)),
]


def check_duplicates() -> list[str]:
    warn = []
    for p in sorted(DOCS.glob("*.md")):
        # 报告/评审类文档会引用运行时数值（如「persona 700」），不参与「预算重复定义」检查。
        if (p.name == "DESIGN.md"
                or p.name.startswith(("verification-report", "review-",
                                      "mood-judge-audit", "affect-and-response"))):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for key, want, rx, skips in DUP_RULES:
                if any(s in line for s in skips):
                    continue
                m = rx.search(line)
                if not m:
                    continue
                got = float(m.group(1))
                if abs(got - want) > 1e-9 and not has_num(line, want):
                    warn.append("%s:%d `%s` 写成 %d（常量 %d）\n      %s"
                                % (p.name, i, key, got, want,
                                   line.strip()[:100]))
    return warn


# ---------------------------------------------------------------- 检查 0b：运行时镜像
def check_runtime() -> list[str]:
    """所有设计键必须住在 tuning.py；LEGACY 必须标 ⚠️。"""
    bad = []
    try:
        from core import tuning
    except Exception as e:  # noqa: BLE001
        return ["无法 import core.tuning：%s" % e]
    for sec, fields in D.KEYS.items():
        for field, key in fields.items():
            if key not in tuning.DEFAULTS:
                bad.append("tuning 缺设计键 %s（%s.%s）" % (key, sec, field))
            elif getattr(D, sec)[field] != tuning.DEFAULTS[key]:
                bad.append("%s.%s 与 tuning.%s 不一致" % (sec, field, key))
    for k in getattr(tuning, "LEGACY", ()):
        if not str(tuning.DOCS.get(k, "")).startswith("⚠️"):
            bad.append("LEGACY %s 的 DOCS 未加 ⚠️ 前缀" % k)
    return bad


# ---------------------------------------------------------------- 检查 0：常量自洽
def check_self() -> list[str]:
    bad = []
    if D.BUDGET["style_total"] > D.CTX["style_total"]:
        bad.append("风格目标 %d 超过运行时上限 %d"
                   % (D.BUDGET["style_total"], D.CTX["style_total"]))
    if D.BUDGET["style_total_custom"] > D.CTX["style_total"]:
        bad.append("custom 风格目标 %d 超过运行时上限 %d"
                   % (D.BUDGET["style_total_custom"], D.CTX["style_total"]))
    # 2026-11-25：运行时不变式 —— 各分项之和不能超过「风格合计上限」。
    # 以前只比 BUDGET，导致 style_total()=730 > CTX_STYLE_TOTAL=650 没人报。
    if D.style_total() > D.CTX["style_total"]:
        bad.append("运行时 style_total()=%d 超过 CTX_STYLE_TOTAL=%d"
                   % (D.style_total(), D.CTX["style_total"]))
    if D.style_total(custom=True) > D.CTX["style_total"]:
        bad.append("运行时 style_total(custom)=%d 超过 CTX_STYLE_TOTAL=%d"
                   % (D.style_total(custom=True), D.CTX["style_total"]))
    if not (1900 <= D.budget_total() <= 2300):
        bad.append("budget_total()=%d 不在 1.9k~2.3k，可能漏块/多块"
                   % D.budget_total())
    if set(D.AFFINITY_TARGETS) - set(D.ENUMS["target"]):
        bad.append("AFFINITY_TARGETS 含未定义 target")
    if len(D.ENUMS["target"]) != 5:
        bad.append("target 枚举不是 5 值")
    long_total = (D.budget_total() - D.BUDGET["evidence"]
                  + D.BUDGET["evidence_long"] + D.BUDGET["library_long"])
    if long_total > D.BUDGET["hard_cap"]:
        bad.append("long_doc 分配 %d 超过硬上限 %d"
                   % (long_total, D.CTX["hard_cap"]))
    need = D.budget_total(tools=True, custom=True, multitarget=True)
    if D.BUDGET["soft_cap"] < need:
        bad.append("soft_cap=%d < 最大组合 %d（工具+custom+多目标）"
                   % (D.CTX["soft_cap"], need))
    if D.ANCHOR["gain"] > 0.5:
        bad.append("锚点增益 %.2f 过大（>0.5 会一次拽飞）" % D.ANCHOR["gain"])
    lv = [D.ANCHOR["map"][k] for k in sorted(D.ANCHOR["map"])]
    if lv != sorted(lv) or len(set(lv)) != len(lv):
        bad.append("锚点映射不是严格单调递增")
    return bad


# ---------------------------------------------------------------- 检查 8：运行时接线（--runtime）
def check_runtime_wiring() -> tuple[list[str], list[str]]:
    """证明「设计键真的被代码读」+「线上覆盖没把不变式搞坏」。

    默认模式只比文档↔常量，读者容易误以为「校验过了 = 线上生效」。
    这里用 AST 扫真实读点（与 check_tuning 共用扫描器），再读一遍
    live 值（.env 覆盖后）核对几条硬不变式。
    """
    fails: list[str] = []
    notes: list[str] = []
    try:
        import check_tuning as ct
        from core import tuning
    except Exception as e:  # noqa: BLE001
        return ["--runtime 无法加载扫描器：%s" % e], notes

    repo = ct.scan_repo()
    used: set = set()
    for info in repo["files"].values():
        used |= info["keys"]
    unwired = set(getattr(tuning, "UNWIRED", ()))

    design_keys = []
    for fields in D.KEYS.values():
        design_keys.extend(fields.values())
    missing = [k for k in dict.fromkeys(design_keys)
               if k not in used and k not in unwired]
    for k in missing:
        fails.append("设计键 %s 没有任何代码读点（文档写了，运行时不生效）" % k)

    # ---- live 覆盖后的不变式 ----
    try:
        from services import ctx_budget
        scales = {b: s for b, (_k, s) in ctx_budget.BUDGET.items()}
    except Exception:  # noqa: BLE001
        scales = {}

    def live(key):
        try:
            return tuning.get(key)
        except Exception:  # noqa: BLE001
            return None

    pb = live("PROMPT_BUDGET")
    if pb is not None and float(pb) < D.budget_total():
        fails.append("线上 PROMPT_BUDGET=%s < 常态目标 %d（prompt 会放不下）"
                     % (pb, D.budget_total()))

    st = live("CTX_STYLE_TOTAL")
    if st is not None:
        need = D.style_total()
        if float(st) * 1.5 < need:
            fails.append("线上 CTX_STYLE_TOTAL=%s（≈%d 字符）< style_total()=%d"
                         % (st, int(float(st) * 1.5), need))

    for block, key in D.KEYS["CTX"].items():
        if block not in D.BUDGET:
            continue
        got = live(key)
        if got is None:
            continue
        scale = float(scales.get(block, 1.5))
        chars = int(float(got) * scale)
        if chars < D.BUDGET[block]:
            fails.append("线上 %s=%s（≈%d 字符）< 该块设计目标 %d"
                         % (key, got, chars, D.BUDGET[block]))

    enf = live("CTX_ENFORCE")
    if enf is not None and int(float(enf)) not in (0, 1):
        fails.append("线上 CTX_ENFORCE=%s 不是 0/1" % enf)

    # 覆盖清单（只提示，不判错）
    for k in dict.fromkeys(design_keys):
        cur = live(k)
        dft = tuning.DEFAULTS.get(k)
        try:
            if cur is not None and float(cur) != float(dft):
                notes.append("%s：默认 %s → 线上 %s" % (k, dft, cur))
        except Exception:  # noqa: BLE001
            pass
    return fails, notes


# ---------------------------------------------------------------- 检查 7：术语登记
# 公式里出现的符号必须在 §5 登记（§8 第 6 条）。这里只锁「必须存在」的清单：
# 删掉/改名会红；新加符号仍要人工登记（脚本猜不出哪些反引号是符号）。
REQUIRED_TERMS = (
    "观测 z", "Δ / Δv", "步进增益 K", "过程噪声 Q", "观测噪声 R",
    "存储强度", "提取强度", "显著性权重", "提取时间常数", "深度系数",
)


def check_terms(text: str) -> list[str]:
    m = re.search(r"^## 5\. 术语.*?(?=^## )", text, re.S | re.M)
    if not m:
        return ["§5 术语表缺失"]
    body = m.group(0)
    return ["§5 术语表缺 `%s`（§8 第 6 条：符号必须登记）" % t
            for t in REQUIRED_TERMS if t not in body]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="警告也算失败")
    ap.add_argument("--runtime", action="store_true",
                    help="额外校验运行时接线与线上覆盖（AST 扫描）")
    args = ap.parse_args()

    if not DESIGN.exists():
        print("❌ 找不到 docs/DESIGN.md")
        return 1
    text = DESIGN.read_text(encoding="utf-8")
    secs = load_sections(text)

    fails: list[str] = []
    fails += check_self()
    fails += check_runtime()
    fails += check_budget(secs.get("3.3", []))
    fails += check_mood(secs.get("3.1", []))
    fails += check_output(text)
    fails += check_units(text)
    fails += check_target()
    fails += check_terms(text)
    warns = check_duplicates()
    rt_notes: list[str] = []
    if args.runtime:
        rt_fails, rt_notes = check_runtime_wiring()
        fails += rt_fails

    print("=" * 70)
    print("设计口径检查  DESIGN §3 ↔ core/design_constants.py"
          + ("（含 --runtime 接线）" if args.runtime else "（仅文档↔常量）"))
    print("=" * 70)
    print("设计目标 budget_total=%d（有工具 %d / custom %d / 多目标 %d）｜"
          "运行时上限合计 ctx_total=%d｜风格 %d（custom %d）"
          % (D.budget_total(), D.budget_total(tools=True),
             D.budget_total(custom=True), D.budget_total(multitarget=True),
             D.ctx_total(), D.style_total(), D.style_total(custom=True)))
    print("target 枚举：%s" % (list(D.ENUMS["target"]),))
    print("-" * 70)

    if fails:
        print("❌ FAIL（%d）" % len(fails))
        for x in fails:
            print("   - " + x)
    else:
        print("✅ §3 口径与常量一致")

    if warns:
        print("⚠️  WARN（%d）子文档可能重复定义预算：" % len(warns))
        for x in warns:
            print("   - " + x)
    else:
        print("✅ 子文档无预算重复定义")

    if rt_notes:
        print("ℹ️  线上覆盖（--runtime）：")
        for x in rt_notes[:30]:
            print("   - " + x)

    print("-" * 70)
    ok = not fails and (not warns or not args.strict)
    print("结论：%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
