# scripts/diag_report.py
"""汇总 data/diag/*.jsonl（六环诊断），给部署后核对用。

用法：python scripts/diag_report.py [--dir data/diag] [--day 2026-11-28]

只读，不改任何数据。输出：
  · 各类事件计数；
  · 气氛 need_reply / 检索词来源分布；
  · 召回：次数、空结果、平均耗时、about_qq 命中说话人比例；
  · 写入：about_qq 空 / 非空计数（空是核对置空或 world/self）；
  · 工具：挂载数/总数、实际调用次数、挂了没调/调了没挂（工具 miss 线索）；
  · 归因核对：通过/置空及原因。
"""
import argparse
import json
import time
from collections import Counter
from pathlib import Path


def _load(d: Path):
    rows = []
    for f in sorted(d.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/diag")
    ap.add_argument("--day", default="")
    a = ap.parse_args()
    d = Path(a.dir)
    day = a.day or time.strftime("%Y-%m-%d")
    rows = _load(d)
    if a.day:
        rows = [r for r in rows
                if time.strftime("%Y-%m-%d", time.localtime(float(r.get("ts") or 0))) == day]
    print(f"事件总数 {len(rows)}（{d}/{day}）")
    print("按类型:", dict(Counter(r.get("kind") for r in rows)))

    rec = [r for r in rows if r.get("kind") == "recall"]
    if rec:
        empty = sum(1 for r in rec if not r.get("n_pick"))
        ms = [float(r.get("ms") or 0) for r in rec]
        hit = sum(int(r.get("hit_spk") or 0) for r in rec)
        picked = sum(int(r.get("n_pick") or 0) for r in rec)
        print(f"召回 {len(rec)} 次｜空 {empty}｜平均 {sum(ms) / len(ms):.0f}ms"
              f"｜选中 {picked} 条，其中 About=说话人 {hit}（{hit / max(1, picked):.0%}）")

    wr = [r for r in rows if r.get("kind") == "write"]
    if wr:
        empty = sum(1 for r in wr if not str(r.get("about") or ""))
        print(f"写入 {len(wr)} 条｜about_qq 空 {empty}（{empty / len(wr):.0%}）")

    tools = [r for r in rows if r.get("kind") == "tools"]
    calls = [r for r in rows if r.get("kind") == "tool"]
    if tools:
        ratio = [int(r.get("n") or 0) / max(1, int(r.get("total") or 0)) for r in tools]
        print(f"工具选择 {len(tools)} 次｜平均挂载比例 {sum(ratio) / len(ratio):.0%}")
    if calls:
        miss = sum(1 for r in calls if r.get("outcome") != "ok")
        print("工具调用:", dict(Counter(r.get("name") for r in calls)),
              f"｜失败/未挂 {miss}")

    attr = [r for r in rows if r.get("kind") == "attr"]
    if attr:
        bywhy = Counter(r.get("why") for r in attr)
        print(f"归因核对 {len(attr)} 次:", dict(bywhy))

    atmo = [r for r in rows if r.get("kind") == "atmo"]
    if atmo:
        print("气氛 need_reply:", dict(Counter(r.get("need") for r in atmo)))
    qs = [r for r in rows if r.get("kind") == "query"]
    if qs:
        print("检索词来源:", dict(Counter(r.get("src") for r in qs)))
    view = [r for r in rows if r.get("kind") == "view"]
    if view:
        gate = Counter((r.get("gate"), bool(r.get("ok"))) for r in view)
        print(f"接话机会 {len(view)} 次:", dict(gate))
    qw = [r for r in rows if r.get("kind") == "qwen"]
    if qw:
        print("本地Qwen:", dict(Counter(bool(r.get("ok")) for r in qw)))
    pr = [r for r in rows if r.get("kind") == "prompt"]
    if pr:
        tot = sum(int(r.get("total") or 0) for r in pr) or 1
        seg = Counter()
        for r in pr:
            for k, v in (r.get("seg") or {}).items():
                seg[k] += int(v or 0)
        share = ", ".join(f"{k} {v * 100 // tot}%" for k, v in seg.most_common(8))
        avg = sum(int(r.get("total") or 0) for r in pr) / len(pr)
        print(f"prompt {len(pr)} 次｜平均 {avg:.0f} 字符｜分段占比: {share}")


if __name__ == "__main__":
    main()
