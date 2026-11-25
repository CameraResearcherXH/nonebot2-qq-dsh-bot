#!/usr/bin/env python3
# scripts/dump_tuning.py
"""把 core/tuning.py 的调参表导出成文档 / 终端表格。

用法：
    python3 scripts/dump_tuning.py            # 终端打印（按分组）
    python3 scripts/dump_tuning.py --write    # 生成 docs/tuning.md
    python3 scripts/dump_tuning.py --changed  # 只打印被环境变量改过的项
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import tuning  # noqa: E402

GROUP_TITLE = {
    "reply": "回复 / 限流 / 主动插话",
    "mood": "心情",
    "affinity": "档案 / 好感",
    "memory": "记忆",
    "sticker": "表情",
    "proactive": "主动私聊",
    "quiet": "安静 / 破冰",
    "prompt": "提示词",
    "llm": "模型调用",
    "usage": "用量监测",
    "audit": "回复审核",
    "expression": "表达 / 语气",
    "vision": "视觉",
    "draw": "画图",
    "media": "语音 / 音乐",
    "lore": "世界书",
    "safety": "安全 / 工具",
    "ui": "菜单",
    "other": "其他",
}


def group_of(key: str) -> str:
    """按 FILES 前缀 + 键名前缀粗分组（不依赖 tuning 内部结构）。"""
    f = tuning.FILES.get(key, "")
    if f.startswith("plugins/chat"):
        return "reply"
    if f.endswith("mood.py"):
        return "mood"
    if f.endswith("mood_judge.py"):
        return "mood"
    if "memory" in f or f.endswith("dialogue_log.py") or "group_scene" in f \
            or "group_roster" in f or "journal" in f or "daily_summary" in f:
        return "memory"
    if "sticker" in f:
        return "sticker"
    if f.endswith("proactive.py"):
        return "proactive"
    if f.endswith("quiet_advisor.py") or f.endswith("chat_switch.py"):
        return "quiet"
    if "profile" in f or f.endswith("profiles.py"):
        return "affinity"
    if f.endswith("prompt_builder.py") or f.endswith("persona.py"):
        return "prompt"
    if f.endswith("llm.py") or f.endswith("glm.py"):
        return "llm"
    if "usage" in f:
        return "usage"
    if "auditor" in f or "referee" in f:
        return "audit"
    if "expression" in f or "conv_state" in f:
        return "expression"
    if "vision" in f or "video" in f:
        return "vision"
    if "draw" in f:
        return "draw"
    if "song" in f or "agent_client" in f or "cover" in f:
        return "media"
    if "lore" in f:
        return "lore"
    if "napcat" in f or "config" in f or "self_tools" in f or "tool_dispatcher" in f:
        return "safety"
    if "help_menu" in f:
        return "ui"
    return "other"


def rows():
    out = []
    for key, cur, default, typ, doc, src in tuning.describe():
        out.append({"key": key, "cur": cur, "default": default, "type": typ,
                    "doc": doc, "src": src, "group": group_of(key)})
    return out


def fmt(v):
    try:
        f = float(v)
        if f == int(f):
            return str(int(f))
        return ("%.3f" % f).rstrip("0").rstrip(".")
    except Exception:
        return str(v)


def write_markdown(path: Path) -> None:
    rs = rows()
    by_group = {}
    for r in rs:
        by_group.setdefault(r["group"], []).append(r)
    lines = [
        "# 调参手册（tuning）",
        "",
        "> 本文件由 `python3 scripts/dump_tuning.py --write` 生成，**不要手改**。",
        "> 所有默认值来自 `core/tuning.py` 的 `DEFAULTS`。",
        "",
        "## 怎么改",
        "",
        "两种方式，二选一：",
        "",
        "1. **环境变量（推荐）**：在 `.env` 里写同名键，例如 `STICKER_COLLECT_MIN=0.3`，重启生效。",
        "2. **改默认值**：直接改 `core/tuning.py` 的 `DEFAULTS`（适合做发行版预设）。",
        "",
        "查看当前实际生效值（含 .env 覆盖）：`/调参` 或 `python3 scripts/dump_tuning.py`。",
        "",
        "单位约定：",
        "",
        "- 时间一律**秒**，除非键名带 `_H`（小时）/`_DAYS`（天）。",
        "- 概率/权重一律 **0~1**。",
        "- 心情内部口径 **-100~100**；判读模型给的是 -1.00~1.00，代码里会 ×100。",
        "- 生效位置标 **（设计，未接线）** 的键：设计已定、代码还没读，**改了不生效**；"
        "接线后会自动去掉标记。",
        "",
        "---",
        "",
        "## 参数总览",
        "",
        "| 分组 | 项数 |",
        "|---|---|",
    ]
    order = [g for g in GROUP_TITLE if g in by_group]
    for g in order:
        lines.append("| %s | %d |" % (GROUP_TITLE[g], len(by_group[g])))
    lines.append("| **合计** | **%d** |" % len(rs))
    lines.append("")

    for g in order:
        lines.append("## %s" % GROUP_TITLE[g])
        lines.append("")
        lines.append("| 参数 | 默认 | 说明 | 生效位置 |")
        lines.append("|---|---|---|---|")
        for r in sorted(by_group[g], key=lambda x: x["key"]):
            mark = "" if fmt(r["cur"]) == fmt(r["default"]) else " ⚠️"
            src = r["src"]
            if r["key"] in getattr(tuning, "UNWIRED", ()):
                src = "%s（设计，未接线）" % src
            lines.append("| `%s` | `%s`%s | %s | `%s` |" % (
                r["key"], fmt(r["default"]), mark, r["doc"] or "—", src))
        lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="生成 docs/tuning.md")
    ap.add_argument("--changed", action="store_true", help="只显示被环境变量覆盖的项")
    args = ap.parse_args()

    if args.write:
        p = ROOT / "docs" / "tuning.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(p)
        print("written:", p, "(%d params)" % len(tuning.DEFAULTS))
        return 0

    rs = rows()
    if args.changed:
        rs = [r for r in rs if fmt(r["cur"]) != fmt(r["default"])]
        if not rs:
            print("没有被环境变量覆盖的参数（全部使用默认值）。")
            return 0
    by_group = {}
    for r in rs:
        by_group.setdefault(r["group"], []).append(r)
    for g in [x for x in GROUP_TITLE if x in by_group]:
        print("\n== %s ==" % GROUP_TITLE[g])
        for r in sorted(by_group[g], key=lambda x: x["key"]):
            mark = "" if fmt(r["cur"]) == fmt(r["default"]) else "  ← .env 覆盖"
            print("  %-28s %-10s %s%s" % (r["key"], fmt(r["cur"]),
                                          (r["doc"] or "")[:44], mark))
    print("\n合计 %d 项；改过的 %d 项。" % (
        len(rows()), len([r for r in rows() if fmt(r["cur"]) != fmt(r["default"])])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
