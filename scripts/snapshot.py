#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
snapshot.py —— 代码/配置快照与一键回滚。

为什么需要它
------------
这个项目的「智能」大量写在 prompt 和阈值里（services/llm.py、prompt_builder.py、
Personality/*.txt），改一处就可能让她的说话风格整体变化。真机上出问题时，
你需要的是「30 秒回到上一个已知好版本」，而不是翻聊天记录找哪次改动。

设计（三层回滚）
----------------
  ① 代码层：deploy_manifest.txt 是唯一可信清单（路径 + md5）。
     快照 = 把清单里的文件按原路径复制一份 + 记录 md5。
  ② 配置层：.env 一起快照（权限 600），回滚配置不用再问人。
  ③ 数据层：默认不碰（data 有自己的备份/30 天迁移）；
     加 `--with-data` 才带上 memory 索引 + profiles + mood（都很小）。

命令
----
    python scripts/snapshot.py create --note "全量修复第一批"
    python scripts/snapshot.py list
    python scripts/snapshot.py verify                 # 当前代码 vs 清单
    python scripts/snapshot.py diff 20261117_221500   # 当前 vs 某个快照
    python scripts/snapshot.py restore 20261117_221500

    # 带数据（memory 索引 / blocks / profiles / mood）
    python scripts/snapshot.py create --with-data

回滚后必须重启容器才生效：
    docker restart qq-grok-bot

注意
----
· 快照目录默认 `snapshots/`，可用 SNAPSHOT_DIR 改到别的盘。
· 只复制清单里列出的文件，不会误碰 data/ 和 venv/。
· restore 之前会自动再存一份 `pre_restore_*`，回滚也能回滚。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "deploy_manifest.txt"
SNAP_DIR = Path(os.getenv("SNAPSHOT_DIR", ROOT / "snapshots"))
ENV_FILE = ROOT / ".env"

# 清单之外、但回滚时必须一起带上的
EXTRA_FILES = [
    "deploy_manifest.txt",
    "requirements.txt",
    "docker-compose.yml",
    "Dockerfile",
    "bot.py",
    "node.py",
    "node.env",
]

# --with-data 时带上的小数据（大文件/日志不带）
DATA_FILES = ["data/mood.json", "data/mood_history.jsonl",
              "data/memory/index.json", "data/daily_summary.jsonl"]
DATA_DIRS = ["data/profiles", "data/memory/blocks"]


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_paths() -> list[str]:
    if not MANIFEST.exists():
        raise SystemExit(f"找不到 {MANIFEST}，先确认部署清单存在")
    out = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 32:
            out.append(parts[1].strip())
    return out


def _safe_rel(rel: str) -> Path:
    p = (ROOT / rel).resolve()
    p.relative_to(ROOT.resolve())      # 越界直接抛
    return p


def create(note: str = "", with_data: bool = False) -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest = SNAP_DIR / ts
    files_dir = dest / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    records, missing = [], []
    # 人格卡是 prompt 的一部分，必须进快照。
    # （清单里可能写的是旧文件名，这里直接扫目录，避免漏掉）
    persona = [str(p.relative_to(ROOT))
               for p in (ROOT / "services" / "Personality").glob("*.txt")]
    paths = list(dict.fromkeys(_manifest_paths() + EXTRA_FILES + persona))
    for rel in paths:
        src = _safe_rel(rel)
        if not src.is_file():
            missing.append(rel)
            continue
        dst = files_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        records.append({"path": rel, "md5": _md5(src), "size": src.stat().st_size})

    # .env（含密钥，权限收紧）
    env_saved = False
    if ENV_FILE.is_file():
        env_dir = dest / "env"
        env_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ENV_FILE, env_dir / ".env")
        try:
            os.chmod(env_dir / ".env", 0o600)
        except Exception:
            pass
        env_saved = True

    # 可选：小数据
    data_records = []
    if with_data:
        for rel in DATA_FILES:
            src = _safe_rel(rel)
            if src.is_file():
                dst = dest / "data" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                data_records.append({"path": rel, "md5": _md5(src)})
        for rel in DATA_DIRS:
            src = _safe_rel(rel)
            if not src.is_dir():
                continue
            for f in src.rglob("*"):
                if not f.is_file():
                    continue
                r = str(f.relative_to(ROOT))
                dst = dest / "data" / r
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                data_records.append({"path": r, "md5": _md5(f)})

    meta = {
        "ts": ts, "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note, "env": env_saved,
        "files": records, "data": data_records, "missing": missing,
    }
    (dest / "SNAPSHOT.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    print(f"✅ 快照 {ts}：{len(records)} 个文件"
          + (f" + {len(data_records)} 个数据文件" if data_records else "")
          + f"，{size / 1024 ** 2:.1f} MB → {dest}")
    if missing:
        print(f"⚠️ 清单里这些文件不存在（已跳过）：{missing}")
    if env_saved:
        print("   .env 已一并备份（权限 600）")
    return dest


def _load(name: str) -> tuple[Path, dict]:
    d = SNAP_DIR / name
    if not d.is_dir():
        cands = sorted(SNAP_DIR.glob(f"*{name}*"))
        if len(cands) == 1:
            d = cands[0]
        else:
            raise SystemExit(f"找不到快照 {name}（可用 list 查看）")
    meta = json.loads((d / "SNAPSHOT.json").read_text(encoding="utf-8"))
    return d, meta


def list_snaps() -> None:
    if not SNAP_DIR.exists():
        print("还没有快照。先跑：python scripts/snapshot.py create")
        return
    rows = sorted([p for p in SNAP_DIR.iterdir() if (p / "SNAPSHOT.json").exists()])
    if not rows:
        print("还没有快照。")
        return
    print(f"{'快照':<18} {'时间':<20} {'文件':>5} {'数据':>5}  备注")
    for d in rows:
        try:
            m = json.loads((d / "SNAPSHOT.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        print(f"{d.name:<18} {m.get('time', ''):<20} "
              f"{len(m.get('files') or []):>5} {len(m.get('data') or []):>5}  "
              f"{m.get('note', '')}")


def verify(name: str = "") -> int:
    """不传 name：当前代码 vs deploy_manifest；传 name：当前 vs 快照。"""
    if name:
        d, meta = _load(name)
        base = {r["path"]: r["md5"] for r in meta.get("files") or []}
        label = f"快照 {d.name}"
    else:
        base = {}
        for line in MANIFEST.read_text(encoding="utf-8").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 32:
                base[parts[1].strip()] = parts[0]
        label = "deploy_manifest"

    changed, missing = [], []
    for rel, want in base.items():
        p = _safe_rel(rel)
        if not p.is_file():
            missing.append(rel)
        elif _md5(p) != want:
            changed.append(rel)

    print(f"对照 {label}：{len(base)} 个文件")
    print(f"  变更 {len(changed)} ｜ 缺失 {len(missing)}")
    for x in changed:
        print(f"  ~ {x}")
    for x in missing:
        print(f"  ! {x}")
    return 0 if not changed and not missing else 1


def restore(name: str, assume_yes: bool = False) -> int:
    d, meta = _load(name)
    files = meta.get("files") or []
    if not files:
        raise SystemExit("这个快照里没有代码文件，拒绝恢复")

    # 先把「当前状态」也存一份，回滚本身可回滚
    print("→ 先备份当前状态…")
    pre = create(note=f"pre_restore_{d.name}")

    bad = []
    for r in files:
        src = d / "files" / r["path"]
        if not src.is_file():
            bad.append(r["path"])
        elif _md5(src) != r["md5"]:
            bad.append(r["path"] + "（md5 不符）")
    if bad:
        print(f"❌ 快照文件损坏，已中止：{bad[:5]}")
        return 2

    if not assume_yes:
        print(f"\n将用 {d.name} 覆盖 {len(files)} 个文件。")
        print("确认请加 --yes")
        print(f"（当前状态已备份到 {pre.name}）")
        return 0

    for r in files:
        src = d / "files" / r["path"]
        dst = _safe_rel(r["path"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    # 数据（如果快照里带了）
    data_files = meta.get("data") or []
    for r in data_files:
        src = d / "data" / r["path"]
        dst = _safe_rel(r["path"])
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    env_src = d / "env" / ".env"
    if env_src.is_file() and assume_yes:
        shutil.copy2(env_src, ENV_FILE)
        print("   .env 已恢复")

    print(f"✅ 已恢复到 {d.name}：代码 {len(files)} 个"
          + (f"，数据 {len(data_files)} 个" if data_files else ""))
    print("⚠️ 必须重启容器才生效：docker restart qq-grok-bot")
    print("   重启后跑一次 /全量测试 确认链路。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="代码/配置快照与回滚")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create")
    c.add_argument("--note", default="")
    c.add_argument("--with-data", action="store_true")

    sub.add_parser("list")

    v = sub.add_parser("verify")
    v.add_argument("name", nargs="?", default="")

    df = sub.add_parser("diff")
    df.add_argument("name")

    r = sub.add_parser("restore")
    r.add_argument("name")
    r.add_argument("--yes", action="store_true")

    a = ap.parse_args()
    if a.cmd == "create":
        create(a.note, a.with_data)
        return 0
    if a.cmd == "list":
        list_snaps()
        return 0
    if a.cmd == "verify":
        return verify(a.name)
    if a.cmd == "diff":
        return verify(a.name)
    if a.cmd == "restore":
        return restore(a.name, a.yes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
