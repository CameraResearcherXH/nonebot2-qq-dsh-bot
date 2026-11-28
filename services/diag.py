# services/diag.py
"""
六环检索诊断（docs/overall-design-20261128.md §7）：

  写入归因 → 检索词 → 过滤 → 排序 → 注入 → 模型采纳

每一步只记**元数据**（关键词、数量、ID、分数、耗时），不记聊天正文。
写 data/diag/<day>.jsonl，一行一个事件，便于事后并排复盘（肥牛 case）。
关掉：DIAG=0。落盘失败绝不抛 —— 诊断不能拖垮聊天。
"""
import json
import logging
import os
import time
from pathlib import Path

_log = logging.getLogger(__name__)

ENABLED = os.getenv("DIAG", "1").lower() not in ("0", "false", "off")
DIAG_DIR = Path(os.getenv("DIAG_DIR", "data/diag"))
KEEP_DAYS = int(os.getenv("DIAG_KEEP_DAYS", "14") or 14)

_last_purge = 0.0


def emit(event: str, **fields) -> None:
    if not ENABLED:
        return
    try:
        rec = {"ts": round(time.time(), 3), "kind": str(event)}
        for k, v in fields.items():
            if k == "kind":
                k = "mkind"  # kind 是事件名，记忆的 kind 让位
            if v in (None, ""):
                continue
            if isinstance(v, str):
                v = v[:120]
            elif isinstance(v, (list, tuple)):
                v = list(v)[:20]
            rec[k] = v
        DIAG_DIR.mkdir(parents=True, exist_ok=True)
        p = DIAG_DIR / f"{time.strftime('%Y-%m-%d', time.localtime(rec['ts']))}.jsonl"
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        _purge()
    except Exception as e:
        _log.debug(f"[DIAG] 落盘失败: {type(e).__name__}: {e}")


def _purge() -> None:
    global _last_purge
    now = time.time()
    if now - _last_purge < 3600:
        return
    _last_purge = now
    try:
        cutoff = now - KEEP_DAYS * 86400
        for f in DIAG_DIR.glob("*.jsonl"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except Exception:
        pass


def digest(day: str = "") -> dict:
    """读当天 diag 文件，汇总成 /记忆体检 可打印的趋势（只读）。"""
    from collections import Counter
    day = day or time.strftime("%Y-%m-%d")
    out = {"day": day, "n": 0, "kinds": {}, "recall": None, "tools": None,
           "write": None, "view": None, "qwen": None, "node": None, "lag": None}
    p = DIAG_DIR / f"{day}.jsonl"
    if not p.exists():
        return out
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    out["n"] = len(rows)
    out["kinds"] = dict(Counter(r.get("kind") for r in rows))

    rec = [r for r in rows if r.get("kind") == "recall"]
    if rec:
        ms = [float(r.get("ms") or 0) for r in rec]
        out["recall"] = {"n": len(rec), "empty": sum(1 for r in rec if not r.get("n_pick")),
                         "ms": round(sum(ms) / len(ms))}
    tl = [r for r in rows if r.get("kind") == "tools"]
    if tl:
        ratio = [int(r.get("n") or 0) / max(1, int(r.get("total") or 0)) for r in tl]
        out["tools"] = {"n": len(tl), "mount": round(100 * sum(ratio) / len(ratio))}
    wr = [r for r in rows if r.get("kind") == "write"]
    if wr:
        empty = sum(1 for r in wr if not str(r.get("about") or ""))
        out["write"] = {"n": len(wr), "empty": round(100 * empty / len(wr))}
    vw = [r for r in rows if r.get("kind") == "view"]
    if vw:
        passed = sum(1 for r in vw if r.get("ok"))
        gates = Counter(r.get("gate") for r in vw if not r.get("ok"))
        out["view"] = {"n": len(vw), "pass": passed, "reject": dict(gates)}
    qw = [r for r in rows if r.get("kind") == "qwen"]
    if qw:
        out["qwen"] = {"n": len(qw), "ok": sum(1 for r in qw if r.get("ok"))}
    nd = [r for r in rows if r.get("kind") == "node"]
    if nd:
        out["node"] = dict(Counter(r.get("ev") for r in nd))
    lg = [r for r in rows if r.get("kind") == "lag"]
    if lg:
        out["lag"] = {"n": len(lg), "max_ms": max(int(r.get("ms") or 0) for r in lg)}
    return out
