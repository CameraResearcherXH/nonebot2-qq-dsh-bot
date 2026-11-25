#!/usr/bin/env python3
"""把本地对话归档灌进算力机 RAG（「图书馆」），让它真的能回忆旧聊天。

背景（2026-11-24 实测）：
  算力机 8001 是 Agentic RAG v3，路由是 `/api/agent/query`、`/api/agent/smart-query`，
  **没有** `/api/agent/recall`。而且它库里的语料是 `poet.song.rank.*.json`（宋词排行），
  跟聊天记录完全无关 —— 所以她问「上次那个蛋糕」永远查不到。

这个脚本做四件事（服务器侧，不需要改算力机代码）：
  1. 把 `data/dialogue/*.jsonl`（或指定目录）逐条转成纯文本，按天打包；
  2. **增量**：文件 sha256 没变就跳过（`--force` 可强制重传）；
  3. **去重**：同一文件里重复/近重复的行只留一条（`--no-dedup` 可关）；
  4. **分区标记**：正文头写 `# partition: dialogue|memory|...`，给后续 RAG 分区用。
  上传完可选调 `/api/build` 建索引。

用法：
    python scripts/library_sync.py --dry-run          # 只看会传什么（0 上传）
    python scripts/library_sync.py --dir data/dialogue
    python scripts/library_sync.py --dir /mnt/archive --build
    python scripts/library_sync.py --force --build     # 强制全量重传
    python scripts/library_sync.py --partition memory  # 换分区标记

注意：
  · 一次别传太多（几百 MB 会把算力机索引拖很久），按天分批；
  · 传完必须 `/api/build`，否则只是文件躺在 documents 里，检索不到；
  · 群聊内容涉及隐私，确认这台 RAG 只有你自己能访问。
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

BASE = os.getenv("AGENT_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
STATE_FILE = Path(os.getenv("LIBRARY_SYNC_STATE", "data/library_sync_state.json"))


def _norm_line(line: str) -> str:
    """去重口径：去掉时间戳/昵称，只比正文（避免同一个人复读被当成不同内容）。"""
    t = str(line or "").strip()
    t = re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d\s+", "", t)
    t = re.sub(r"^[^:：]{1,40}[:：]\s*", "", t)
    t = re.sub(r"\s+", "", t)
    return t[:120]


def dedup_lines(lines: list, *, enabled: bool = True) -> tuple[list, int]:
    if not enabled:
        return lines, 0
    seen, out, dup = set(), [], 0
    for ln in lines:
        key = _norm_line(ln)
        if not key:
            continue
        h = hashlib.md5(key.encode("utf-8", "ignore")).hexdigest()
        if h in seen:
            dup += 1
            continue
        seen.add(h)
        out.append(ln)
    return out, dup


def dialogue_to_text(path: Path, *, partition: str = "dialogue",
                     dedup: bool = True) -> tuple[str, dict]:
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
    before = len(lines)
    lines, dup = dedup_lines(lines, enabled=dedup)
    header = (f"# partition: {partition}\n"
              f"# source: {path.name}\n"
              f"# generated_at: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"# lines: {len(lines)}\n\n")
    return header + "\n".join(lines), {"lines": len(lines), "dup": dup,
                                      "raw_lines": before}


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as e:
        print(f"  状态写失败：{type(e).__name__}: {e}")


def upload(name: str, content: bytes) -> bool:
    try:
        import requests
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
        import requests
        r = requests.post(f"{BASE}/api/build", json={}, timeout=1800)
        print(f"  建索引：HTTP {r.status_code} {r.text[:200]}")
        return r.status_code < 400
    except Exception as e:
        print(f"  建索引异常：{type(e).__name__}: {e}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="把对话归档增量灌进算力机 RAG")
    ap.add_argument("--dir", default="data/dialogue", help="jsonl 所在目录")
    ap.add_argument("--partition", default="dialogue", help="正文头分区标记")
    ap.add_argument("--build", action="store_true", help="上传完自动建索引")
    ap.add_argument("--build-always", action="store_true", help="即使没变化也建索引")
    ap.add_argument("--force", action="store_true", help="忽略 sha256 状态，全量重传")
    ap.add_argument("--no-dedup", action="store_true", help="关闭行级去重")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不上传")
    args = ap.parse_args()

    files = sorted(glob.glob(str(Path(args.dir) / "*.jsonl")))
    if not files:
        print(f"{args.dir} 里没有 .jsonl")
        return 1

    state = _load_state()
    print(f"目标：{BASE}｜文件 {len(files)} 个｜dry-run={args.dry_run}"
          f"｜partition={args.partition}｜force={args.force}")
    changed = unchanged = uploaded = 0
    total_bytes = 0
    total_lines = total_dup = 0
    for f in files:
        p = Path(f)
        text, meta = dialogue_to_text(p, partition=args.partition,
                                      dedup=not args.no_dedup)
        if not text.strip():
            continue
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        name = f"{args.partition}__{p.stem}.txt"
        old = (state.get(name) or {})
        total_bytes += len(data)
        total_lines += meta["lines"]
        total_dup += meta["dup"]
        if (not args.force) and old.get("sha256") == digest:
            unchanged += 1
            print(f"  = {p.name}: 未变化（{meta['lines']} 行，去重 {meta['dup']}）")
            continue
        changed += 1
        print(f"  + {p.name}: {meta['lines']} 行 / {len(data) // 1024}KB"
              f"（去重 {meta['dup']}/{meta['raw_lines']}）")
        if args.dry_run:
            continue
        if upload(name, data):
            uploaded += 1
            state[name] = {"sha256": digest, "size": len(data),
                           "lines": meta["lines"], "at": time.time()}
            _save_state(state)

    print(f"合计 {total_bytes / 1024 / 1024:.1f}MB｜行 {total_lines}"
          f"（去重 {total_dup}）｜变化 {changed}｜未变 {unchanged}｜上传 {uploaded}")
    if args.dry_run:
        print("（dry-run，没有真的上传）")
        return 0
    if args.build and (uploaded or args.build_always):
        build()
    elif uploaded:
        print("提示：别忘了加 --build，否则索引不会更新")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
