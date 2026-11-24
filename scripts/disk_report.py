#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
disk_report.py —— 磁盘体检 + 安全清理。

为什么要有：2026-11-17 线上 `/全量测试` 报「剩余 2.01 GB」。
这个 bot 的磁盘是被这些「看起来不大、攒起来要命」的东西吃掉的：

    data/llm_trace/*.jsonl     每次模型调用一条，单条最多 60k 字
    data/dialogue/*.jsonl      每天上千条消息
    data/temp_audio/           翻唱中间产物（wav，几十 MB 一份）
    data/temp_img / temp_web_img
    data/voice_*.wav           直接写在 data 根目录的 TTS 产物（没人清）
    data/petpet/               头像表情模板（固定占用，别删）
    napcat/qq/                 QQ 客户端的日志/缓存（可能是大头）

用法：
    python scripts/disk_report.py                  # 只看，不删
    python scripts/disk_report.py --top 30         # 看前 30 大目录
    python scripts/disk_report.py --clean          # 按白名单清理（默认 2 天前）
    python scripts/disk_report.py --clean --days 7

安全原则：
  · 只删「临时产物 / 过期日志」，**不碰** memory / profiles / stickers /
    dialogue / mood / journals。
  · 只处理 DATA_DIR 之内、非符号链接的文件。
  · 先打印再删，删完报释放了多少。
"""
from __future__ import annotations

import argparse
import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("DATA_DIR", ROOT / "data"))


def _size(p: Path) -> int:
    try:
        if p.is_symlink():
            return 0
        if p.is_file():
            return p.stat().st_size
        total = 0
        for dirpath, dirnames, filenames in os.walk(p):
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    if not os.path.islink(fp):
                        total += os.path.getsize(fp)
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def _older_than(p: Path, cutoff: float) -> bool:
    try:
        return p.stat().st_mtime < cutoff
    except OSError:
        return False


def report(top: int = 20) -> None:
    print("=" * 62)
    print(f"磁盘：{DATA}")
    try:
        u = shutil.disk_usage(str(DATA))
        print(f"总 {_human(u.total)} ｜ 已用 {_human(u.used)} ｜ "
              f"剩余 {_human(u.free)}（{u.free / u.total * 100:.1f}%）")
        if u.free < 2 * 1024 ** 3:
            print("🔴 剩余 < 2GB：建议立刻 --clean")
        elif u.free < 5 * 1024 ** 3:
            print("🟡 剩余 < 5GB：建议清理")
    except Exception as e:
        print(f"读不到磁盘信息：{e}")

    print("\n" + "=" * 62)
    print(f"data 下各目录（前 {top} 大）")
    rows = []
    if DATA.exists():
        for p in DATA.iterdir():
            if p.name.startswith("."):
                continue
            rows.append((_size(p), p))
    rows.sort(reverse=True, key=lambda x: x[0])
    for size, p in rows[:top]:
        print(f"  {_human(size):>10}  {p.name}")

    # 明确告诉用户：这些永远不会被 --clean 动
    keep = [
        ("memory", "长期记忆 + blocks"),
        ("profiles", "人物档案 / 相处 / 风格"),
        ("stickers", "表情库"),
        ("memes", "表情素材库（/导入表情 用）"),
        ("dialogue", "对话落盘（周报用）"),
        ("weekly", "周报导出"),
        ("images", "图片素材"),
        ("jinriyunshi", "运势壁纸缓存（可手动删，插件会重下）"),
    ]
    print("\n🔒 不会被 --clean 动的（重要数据 / 素材）：")
    for name, note in keep:
        p = DATA / name
        if p.exists():
            print(f"  {_human(_size(p)):>10}  {name}/  — {note}")

    # 大文件
    big = []
    for dirpath, dirnames, filenames in os.walk(DATA):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
        for f in filenames:
            fp = Path(dirpath) / f
            try:
                if not fp.is_symlink():
                    big.append((fp.stat().st_size, fp))
            except OSError:
                pass
    big.sort(reverse=True, key=lambda x: x[0])
    print(f"\ndata 下最大的 {min(15, len(big))} 个文件")
    for size, p in big[:15]:
        print(f"  {_human(size):>10}  {p.relative_to(DATA)}")

    # napcat（只报不删）
    nap = ROOT / "napcat"
    if nap.exists():
        print(f"\nnapcat（只报告，不清理）：{_human(_size(nap))}")
        sub = sorted(((_size(p), p) for p in nap.iterdir() if p.is_dir()),
                     reverse=True, key=lambda x: x[0])
        for size, p in sub[:8]:
            print(f"  {_human(size):>10}  {p.name}/")

        # QQ 的日志/临时目录：这些通常可以清，但里面有账号数据，脚本不代删
        safe, safe_total = [], 0
        for dirpath, dirnames, _ in os.walk(nap):
            for d in list(dirnames):
                low = d.lower()
                if any(k in low for k in ("log", "temp", "cache", "crash")):
                    p = Path(dirpath) / d
                    s = _size(p)
                    safe.append((s, p))
                    safe_total += s
        safe.sort(reverse=True, key=lambda x: x[0])
        if safe:
            print(f"\n  可人工清理的 QQ 日志/临时目录（合计 {_human(safe_total)}）：")
            for size, p in safe[:8]:
                print(f"    {_human(size):>10}  {p.relative_to(ROOT)}")
            print("    ⚠️ 这些目录里有 QQ 运行数据，建议先停 NapCat 再删；")
            print("       或者只删其中的 *.qqxlog / *.log：")
            print("       find napcat/qq -name '*.qqxlog' -mtime +7 -delete")


# ---- 清理白名单：(路径 glob, 保留天数, 说明) ----
def _targets(days: int):
    trace_days = int(os.getenv("LLM_TRACE_KEEP_DAYS", "7"))
    return [
        (DATA / "temp_web_img", "*", days, "网页图片临时文件"),
        (DATA / "temp_img", "*", days, "图片临时文件"),
        (DATA / "temp_files", "*", days, "通用临时文件"),
        (DATA / "temp_audio", "*", days, "翻唱/语音中间产物"),
        (DATA, "voice_*.wav", days, "TTS 产物（根目录散落）"),
        (DATA / "llm_trace", "*.jsonl", trace_days, "模型调用 trace"),
        (DATA / "self_tools" / "runs", "*.log", 7, "自造工具运行日志"),
        (DATA / "full_test", "report_*.txt", 30, "全量测试报告"),
        # 只清回放/打分的中间结果，golden.jsonl 是基线数据集，永远保留
        (DATA / "eval", "replay.jsonl", 30, "回放结果"),
        (DATA / "eval", "judged.jsonl", 30, "打分结果"),
    ]


def clean(days: int) -> None:
    now = time.time()
    print("=" * 62)
    print(f"清理：只删 {days} 天前的临时产物（trace 按 LLM_TRACE_KEEP_DAYS）")
    freed = 0
    count = 0
    for d, pattern, keep, desc in _targets(days):
        if not d.exists():
            continue
        cutoff = now - keep * 86400
        files = [p for p in d.glob(pattern) if p.is_file() and not p.is_symlink()]
        old = [p for p in files if _older_than(p, cutoff)]
        if not old:
            continue
        size = sum(p.stat().st_size for p in old)
        print(f"  - {desc}：{len(old)} 个 / {_human(size)}（{d.name}/{pattern}）")
        for p in old:
            try:
                # 二次确认：必须真的在 DATA 之内
                p.resolve().relative_to(DATA.resolve())
                real = p.stat().st_size
                p.unlink()
                freed += real
                count += 1
            except Exception as e:
                print(f"    ! 删不掉 {p.name}: {type(e).__name__}")
    print(f"\n共删除 {count} 个文件，释放约 {_human(freed)}")
    if count == 0:
        print("没有可清理的（要么都很新，要么已经干净）。")


def main() -> int:
    ap = argparse.ArgumentParser(description="磁盘体检 / 安全清理")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--clean", action="store_true", help="真的删（默认只看）")
    ap.add_argument("--days", type=int, default=2, help="临时文件保留天数")
    a = ap.parse_args()

    report(a.top)
    if a.clean:
        print()
        clean(a.days)
        print()
        report(10)
    else:
        print("\n（只是查看。要清理加 --clean）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

