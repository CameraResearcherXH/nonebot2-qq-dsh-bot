# services/dialogue_log.py
"""
对话流水落盘（含图片），给「每周导出 → 网页版体检 → 纠正回流」这条链路供料。

为什么需要单独存：
  recent_group_msgs 只活在内存里，重启就没了，也没法按周导出。
  而周报要看的恰恰是「这周她说了什么、配了什么图、答得对不对」。

存储：
  data/dialogue/2026-11-09.jsonl   一行一条消息
  data/images/{fp}.jpg             压缩后的图（最长边 800、约 100KB）
  DIALOGUE_KEEP_DAYS 天后自动清掉（默认 14 天，别把磁盘塞满）
"""
from __future__ import annotations
from core import tuning

import hashlib
import json
import os
import time
from pathlib import Path

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

DIA_DIR = Path("data/dialogue")
IMG_DIR = Path("data/images")
KEEP_DAYS = tuning.i("DIALOGUE_KEEP_DAYS")
IMG_MAX_SIDE = tuning.i("DIALOGUE_IMG_SIDE")
IMG_MAX_BYTES = int(os.getenv("DIALOGUE_IMG_BYTES", str(200 * 1024)))
ENABLED = os.getenv("DIALOGUE_LOG", "true").lower() == "true"


def _day_file(ts: float | None = None) -> Path:
    stamp = time.strftime("%Y-%m-%d", time.localtime(ts or time.time()))
    return DIA_DIR / f"{stamp}.jsonl"


def log_msg(
    gid: str,
    who: str,
    qq: str,
    text: str,
    *,
    kind: str = "user",
    img: str = "",
    vision: str = "",
    extra: dict | None = None,
) -> None:
    """记一条对话。失败绝不抛（别因为日志把聊天搞挂）。"""
    if not ENABLED:
        return
    try:
        DIA_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.time(),
            "gid": str(gid or ""),
            "who": str(who or "")[:40],
            "qq": str(qq or ""),
            "kind": kind,                    # user / bot / peer
            "text": str(text or "")[:1000],
        }
        if img:
            rec["img"] = str(img)
        if vision:
            rec["vision"] = str(vision)[:300]
        if extra:
            rec.update({k: v for k, v in extra.items() if v not in (None, "")})
        with _day_file().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug(f"[DIA] 写对话日志失败: {type(e).__name__}: {e}")


def save_image(image_bytes: bytes, fp: str = "") -> str:
    """
    压缩后存图，返回相对 data/ 的路径（`images/xxx.jpg`）。失败返回空。
    同一张图（同指纹）只存一次。
    """
    if not image_bytes:
        return ""
    try:
        IMG_DIR.mkdir(parents=True, exist_ok=True)
        fp = fp or hashlib.sha1(image_bytes).hexdigest()[:16]
        p = IMG_DIR / f"{fp}.jpg"
        if p.exists():
            return f"images/{fp}.jpg"

        data = image_bytes
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
            im.thumbnail((IMG_MAX_SIDE, IMG_MAX_SIDE))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=78, optimize=True)
            data = buf.getvalue()
            if len(data) > IMG_MAX_BYTES:
                im.thumbnail((480, 480))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=65, optimize=True)
                data = buf.getvalue()
        except Exception:
            pass                        # 没装 Pillow 就原图落盘

        p.write_bytes(data)
        return f"images/{fp}.jpg"
    except Exception as e:
        logger.debug(f"[DIA] 存图失败: {type(e).__name__}: {e}")
        return ""


def purge(days: int = 0) -> int:
    """清掉过期对话和没人引用的图片。返回删除的对话文件数。"""
    days = days or KEEP_DAYS
    cutoff = time.time() - days * 86400
    removed = 0
    try:
        for f in DIA_DIR.glob("*.jsonl"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except Exception:
                pass
    except Exception:
        pass

    # 图片：超过保留期的直接删（按 mtime，够用）
    try:
        for f in IMG_DIR.glob("*.jpg"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass
    return removed


def load_recent(gid: str = "", limit: int = 30, days: int = 0) -> list:
    """
    按**条数**取最近的对话（从最新文件往回读，凑够 limit 就停）。

    为什么不按天：用户反复强调「记录应该按条数，而不是时间」——
    一个群安静两天，`load_range(1)` 就什么都读不到，可最近 20 条其实好好躺在文件里。
    时间只作为**安全上限**（days>0），不是切片条件。

    gid 为空 = 不按会话过滤（各会话混在一起，按时间正序返回）。
    """
    out = []
    try:
        files = sorted(DIA_DIR.glob("*.jsonl"), reverse=True)
    except Exception:
        return []
    if days > 0:
        files = files[:days + 1]
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if gid and str(rec.get("gid") or "") != str(gid):
                continue
            out.append(rec)
            if len(out) >= max(1, int(limit)):
                out.reverse()
                return out
    out.reverse()
    return out


def load_range(days: int = 7) -> list:
    """读最近 N 天的对话，按时间正序返回。"""
    out = []
    now = time.time()
    for i in range(days + 1):
        f = _day_file(now - i * 86400)
        if not f.exists():
            continue
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            continue
    out.sort(key=lambda x: x.get("ts", 0))
    return out
