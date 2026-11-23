#!/usr/bin/env python3
# scripts/audit_third_party.py
"""扫描 plugins/third，生成 docs/third-party-audit.md（来源 / 许可证 / 风险）。

用法：python3 scripts/audit_third_party.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import plugin_registry as reg  # noqa: E402


def main():
    rows = reg.scan()
    lines = ["# 第三方插件审计", "",
             "| 目录 | 上游 | 许可证 | UPSTREAM | 风险 |",
             "|---|---|---|---|---|"]
    for e in rows:
        p = ROOT / "plugins" / "third" / e["dir"]
        up = "待确认"
        lic_up = ""
        f = p / "UPSTREAM.md"
        if f.exists():
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("upstream:"):
                    up = line.split(":", 1)[1].strip()
                elif line.startswith("license:"):
                    lic_up = line.split(":", 1)[1].strip()
        lic = "见目录" if e["has_license"] else "⚠️ 未声明"
        # UPSTREAM.md 里明确写了许可证就以它为准（有些插件没有单独 LICENSE 文件）
        if lic_up and lic_up not in ("待确认", "未声明", ""):
            lic = lic_up
        risks = reg.audit_plugin(p)
        risk = "、".join(sorted({w for w, _ in risks})) or "无"
        lines.append("| `%s` | %s | %s | %s | %s |" % (
            e["dir"], up[:90], lic,
            "✅" if e["has_upstream"] else "❌ 缺失", risk[:60]))
    # vendor/ 下的第三方库（services 直接依赖，不在 plugins/third）
    vdir = ROOT / "vendor"
    vendored = []
    if vdir.exists():
        for d in sorted(vdir.iterdir()):
            if not d.is_dir() or d.name.startswith(("_", ".")):
                continue
            up, lic = "待确认", "待确认"
            f = d / "UPSTREAM.md"
            if f.exists():
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("upstream:"):
                        up = line.split(":", 1)[1].strip()
                    elif line.startswith("license:"):
                        lic = line.split(":", 1)[1].strip()
            has_lic = any((d / n).exists() for n in ("LICENSE", "COPYING", "LICENSE.txt"))
            vendored.append((d.name, up, lic, has_lic))
    if vendored:
        lines += ["", "## vendor/（services 直接依赖的第三方库）", "",
                  "| 目录 | 上游 | 许可证 | LICENSE 文件 |",
                  "|---|---|---|---|"]
        for name, up, lic, has_lic in vendored:
            lines.append("| `%s` | %s | %s | %s |" % (
                name, up[:90], lic, "✅" if has_lic else "⚠️ 缺失"))

    out = ROOT / "docs" / "third-party-audit.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("已生成：%s（%d 个插件 / %d 个 vendored 库）"
          % (out, len(rows), len(vendored)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
