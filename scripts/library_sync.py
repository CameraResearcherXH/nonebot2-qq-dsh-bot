#!/usr/bin/env python3
"""把**归档/外部语料**灌进算力机 RAG（「图书馆」）——**不是**把记忆搬过去。

⚠️ 2026-11-26 用户定边界（先读这段再用）：
  · **记忆 = 本地 sqlite**（`services/memdb.py`）：按人/时间/情绪/标签检索，30 天内够用；
  · **图书馆 = 可选的外部语料**（旧归档、文档、书）：RAG 只是「去档案馆翻旧报纸」；
  · 不要把 `data/dialogue` 每天同步过去 —— 那是**当前记忆的原文**，会造成
    ① 同一件事本地/RAG 各查一遍；② 检索把最近闲聊当「旧档案」顶掉真正久远的事；
    ③ 没搭 RAG 的部署（开源用户）行为不一致。
  · 确实要放 >30 天的归档：显式 `--allow-memory --min-age-days 30`，
    并且只放**已经从本地记忆滑出去**的部分。

原始用途：把本地对话归档灌进算力机 RAG（「图书馆」），让它真的能回忆旧聊天。

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

用法（图书馆 = 外部语料，不是记忆）：
    python scripts/library_sync.py --dir /path/to/docs --build     # 外部语料
    python scripts/library_sync.py --dir /mnt/archive --min-age-days 90 --build
    # 确实要把 >30 天归档也放进去（会明确二次确认）：
    python scripts/library_sync.py --dir data/dialogue --allow-memory --min-age-days 30 --build
    python scripts/library_sync.py --dir /path/to/docs --dry-run   # 只看会传什么

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


def build(documents_path: str = "./documents") -> bool:
    """建索引。

    2026-11-26 修：以前 `json={}` → 一律 HTTP 422 `missing documents_path`，
    所以「上传成功但检索不到」永远存在（文件躺在 documents 里，索引没重建）。
    RAG 侧的 `BuildRequest` 要求 `documents_path`，且**只允许 `./documents` 或 `./uploads`**
    （超范围会回 `400: 文档路径不在允许范围内`）。
    """
    try:
        import requests
        r = requests.post(f"{BASE}/api/build",
                          json={"documents_path": documents_path}, timeout=1800)
        print(f"  建索引：HTTP {r.status_code} {r.text[:200]}")
        return r.status_code < 400
    except Exception as e:
        print(f"  建索引异常：{type(e).__name__}: {e}")
        return False


def _day_of(path: Path) -> float:
    """从 `2026-11-26.jsonl` 取日期；取不到就用 mtime。返回当天 0 点的时间戳。"""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", path.stem)
    if m:
        try:
            return time.mktime((int(m.group(1)), int(m.group(2)),
                                int(m.group(3)), 0, 0, 0, 0, 0, -1))
        except Exception:
            pass
    try:
        return path.stat().st_mtime
    except Exception:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把「归档/外部语料」增量灌进算力机 RAG（记忆仍在本地 sqlite）")
    ap.add_argument("--dir", default="", help="jsonl 所在目录（必须显式指定）")
    ap.add_argument("--partition", default="archive", help="正文头分区标记（默认 archive）")
    ap.add_argument("--allow-memory", action="store_true",
                    help="确认这是归档、不是当前记忆（否则拒绝跑 data/dialogue）")
    ap.add_argument("--min-age-days", type=int, default=30,
                    help="只同步至少多少天前的文件（默认 30；0=不限）")
    ap.add_argument("--build", action="store_true", help="上传完自动建索引")
    ap.add_argument("--build-always", action="store_true", help="即使没变化也建索引")
    ap.add_argument("--force", action="store_true", help="忽略 sha256 状态，全量重传")
    ap.add_argument("--no-dedup", action="store_true", help="关闭行级去重")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不上传")
    args = ap.parse_args()

    if not str(args.dir or "").strip():
        print("❌ 必须显式指定 --dir。\n"
              "   图书馆不是记忆库：记忆在本地 sqlite（services/memdb.py）。\n"
              "   要放 >30 天归档：--dir data/dialogue --allow-memory --min-age-days 30")
        return 2
    _dir_l = str(args.dir).replace("\\", "/").lower()
    if ("dialogue" in _dir_l or "memory" in _dir_l) and not args.allow_memory:
        print("❌ 拒绝：`data/dialogue` 是当前记忆的原文，不是图书馆语料。\n"
              "   本地记忆（sqlite）已经能按人/时间/情绪/标签检索，不需要搬到 RAG；\n"
              "   没搭 RAG 的部署也不该因此行为不一致。\n"
              "   确实要放 >30 天归档：加 --allow-memory --min-age-days 30")
        return 2

    files = sorted(glob.glob(str(Path(args.dir) / "*.jsonl")))
    if args.min_age_days > 0:
        _cut = time.time() - args.min_age_days * 86400
        _kept = [f for f in files if _day_of(Path(f)) <= _cut]
        if len(_kept) != len(files):
            print(f"  只同步 {args.min_age_days} 天前的归档："
                  f"跳过 {len(files) - len(_kept)} 个较新的文件")
        files = _kept
    if not files:
        print(f"{args.dir} 里没有符合条件的 .jsonl")
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
