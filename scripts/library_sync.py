#!/usr/bin/env python3
"""把本地对话归档灌进算力机 RAG（「图书馆」），让它真的能回忆旧聊天。

背景（2026-11-24 实测）：
  算力机 8001 是 Agentic RAG v3，路由是 `/api/agent/query`、`/api/agent/smart-query`，
  **没有** `/api/agent/recall`。而且它库里的语料是 `poet.song.rank.*.json`（宋词排行），
  跟聊天记录完全无关 —— 所以她问「上次那个蛋糕」永远查不到。

这个脚本做两件事：
  1. 把 `data/dialogue/*.jsonl`（或你指定的目录）逐条转成纯文本，按天打包上传；
  2. 上传完调 `/api/build` 建索引。

用法：
    python scripts/library_sync.py --dry-run          # 只看会传什么
    python scripts/library_sync.py --dir data/dialogue
    python scripts/library_sync.py --dir /mnt/archive --build

注意：
  · 一次别传太多（几百 MB 会把算力机索引拖很久），按天分批；
  · 传完必须 `/api/build`，否则只是文件躺在 documents 里，检索不到；
  · 群聊内容涉及隐私，确认这台 RAG 只有你自己能访问。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from pathlib import Path

import requests

BASE = os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8001").rstrip("/")


def dialogue_to_text(path: Path) -> str:
    """jsonl → 「时间 谁: 内容」纯文本（检索友好，别塞 JSON 噪音）。"""
    lines = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                r = json.loads(raw)
            except Exception:
                continue
            ts = float(r.get("ts") or 0)
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""
            who = str(r.get("who") or r.get("qq") or "")
            text = str(r.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"{when} {who}: {text}")
    except Exception as e:
        print(f"  读失败 {path}: {e}")
    return "\n".join(lines)


def upload(name: str, content: bytes) -> bool:
    try:
        r = requests.post(
            f"{BASE}/api/upload",
            files={"file": (name, content, "text/plain")},
            timeout=120,
        )
        if r.status_code >= 400:
            print(f"  上传失败 {name}: HTTP {r.status_code} {r.text[:120]}")
            return False
        return True
    except Exception as e:
        print(f"  上传异常 {name}: {type(e).__name__}: {e}")
        return False


def build() -> bool:
    try:
        r = requests.post(f"{BASE}/api/build", json={}, timeout=1800)
        print(f"  建索引：HTTP {r.status_code} {r.text[:200]}")
        return r.status_code < 400
    except Exception as e:
        print(f"  建索引异常：{type(e).__name__}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="把对话归档灌进算力机 RAG")
    ap.add_argument("--dir", default="data/dialogue", help="jsonl 所在目录")
    ap.add_argument("--build", action="store_true", help="上传完自动建索引")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不上传")
    args = ap.parse_args()

    files = sorted(glob.glob(str(Path(args.dir) / "*.jsonl")))
    if not files:
        print(f"{args.dir} 里没有 .jsonl")
        return 1

    print(f"目标：{BASE}｜文件 {len(files)} 个｜dry-run={args.dry_run}")
    ok = 0
    total = 0
    for f in files:
        p = Path(f)
        text = dialogue_to_text(p)
        if not text.strip():
            continue
        data = text.encode("utf-8")
        total += len(data)
        print(f"  {p.name}: {len(text.splitlines())} 行 / {len(data) // 1024}KB")
        if args.dry_run:
            continue
        if upload(f"{p.stem}.txt", data):
            ok += 1

    print(f"合计 {total / 1024 / 1024:.1f}MB｜上传成功 {ok}/{len(files)}")
    if args.dry_run:
        print("（dry-run，没有真的上传）")
        return 0
    if args.build and ok:
        build()
    elif ok:
        print("提示：别忘了加 --build，否则索引不会更新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
