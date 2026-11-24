# scripts/weekly_apply.py
"""
把网页版体检回来的纠正表灌进记忆。

流程：
    python scripts/weekly_export.py           → 拿到 weekly_xxx.md
    丢给 DeepSeek 网页版 → 它按格式回一份纠正表 → 你存成 corrections.txt
    python scripts/weekly_apply.py corrections.txt --dry-run   # 先看看会写什么
    python scripts/weekly_apply.py corrections.txt             # 真的写进去

纠正表格式（和 weekly_export.py 里给网页版的指令一致）：
    === CORRECTION ===
    C1 | 名词 | 错误写法 | 正确写法 | 依据
    C2 | 人设 | 哪句出戏了 | 应该怎么说
    C3 | 记忆 | 记错了什么 | 正确的事实
    === FACT ===
    F1 | 一条应该被记住的事实
    === PERSONA ===
    P1 | 一句话指出最需要改的人设问题
    === END ===
"""
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SECTION_RE = re.compile(r"^===\s*(CORRECTION|FACT|PERSONA|END)\s*===\s*$", re.IGNORECASE)

# 类型 → 记忆 kind / importance
KIND_MAP = {
    "名词": ("golden", 0.9),
    "回答": ("golden", 0.85),
    "记忆": ("correction", 0.9),
    "人设": ("persona", 0.8),
}


def _split_pipe(line: str) -> list:
    """`C1 | 名词 | a | b | c` → ['C1','名词','a','b','c']（去掉编号和空白）"""
    parts = [p.strip() for p in line.split("|")]
    if parts and re.match(r"^[CFP]\d+$", parts[0], re.IGNORECASE):
        parts = parts[1:]
    return [p for p in parts if p != ""]


def parse(text: str) -> dict:
    out = {"corrections": [], "facts": [], "persona": []}
    section = ""
    for raw in str(text or "").splitlines():
        line = raw.strip().strip("`").strip()
        if not line:
            continue
        m = SECTION_RE.match(line)
        if m:
            section = m.group(1).upper()
            continue
        if not line.startswith(("C", "F", "P")) and "|" not in line:
            continue
        parts = _split_pipe(line)
        if len(parts) < 1:
            continue
        if section == "CORRECTION" and len(parts) >= 3:
            out["corrections"].append({
                "type": parts[0],
                "wrong": parts[1],
                "right": parts[2],
                "basis": parts[3] if len(parts) > 3 else "",
            })
        elif section == "FACT" and len(parts) >= 1:
            out["facts"].append({"text": parts[0]})
        elif section == "PERSONA" and len(parts) >= 1:
            out["persona"].append({"text": parts[0]})
    return out


def apply(parsed: dict, dry_run: bool = False) -> dict:
    from services import memory_v2

    written = {"correction": 0, "fact": 0, "persona": 0}
    plan = []

    for c in parsed["corrections"]:
        kind, imp = KIND_MAP.get(c["type"], ("golden", 0.8))
        txt = f"【{c['type']}纠正】「{c['wrong']}」应改为「{c['right']}」"
        if c["basis"]:
            txt += f"（依据：{c['basis'][:60]}）"
        plan.append((kind, imp, txt))
        written["correction"] += 1

    for f in parsed["facts"]:
        plan.append(("fact", 0.8, f"【体检补充】{f['text']}"))
        written["fact"] += 1

    for p in parsed["persona"]:
        plan.append(("persona", 0.8, f"【人设提醒】{p['text']}"))
        written["persona"] += 1

    for kind, imp, txt in plan:
        print(f"  + ({kind}) {txt[:100]}")
        if not dry_run:
            try:
                memory_v2.remember(text=txt, kind=kind, importance=imp,
                                   tags=["weekly_check"])
            except Exception as e:
                print(f"    ✗ 写入失败：{type(e).__name__}: {e}")

    return written


def main():
    ap = argparse.ArgumentParser(description="把网页版体检结果灌回记忆")
    ap.add_argument("file", help="纠正表文件（网页版输出，存成 txt 即可）")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写入")
    a = ap.parse_args()

    # 记忆模块用相对路径 data/memory/...，先切到项目根
    os.chdir(ROOT)

    p = Path(a.file)
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        print(f"❌ 找不到文件：{p}")
        return

    parsed = parse(p.read_text(encoding="utf-8", errors="ignore"))
    total = (len(parsed["corrections"]) + len(parsed["facts"]) + len(parsed["persona"]))
    print(f"📄 解析到：纠正 {len(parsed['corrections'])} 条、"
          f"事实 {len(parsed['facts'])} 条、人设 {len(parsed['persona'])} 条（共 {total}）")
    if not total:
        print("⚠️ 一条都没解析出来。检查格式：`C1 | 名词 | 错 | 对 | 依据`，"
              "区块标题必须是 `=== CORRECTION ===`")
        return

    print("\n将写入以下记忆：")
    w = apply(parsed, dry_run=a.dry_run)
    print(f"\n{'（dry-run，未写入）' if a.dry_run else '✅ 已写入'}：{w}")


if __name__ == "__main__":
    main()

