"""图片文字增强（OpenCV）→ 视觉模型识别。

为什么要有（2026-11-27 用户）：
  「图片内容如果有字体，她老是看不清楚……识图是一方面，再加个从图片提取的字体。
    比如 llm 觉得字体看不清就去调用 opencv 插件提字。」

为什么不是「直接上 OCR 引擎」：
  服务器上只有 OpenCV（头像/表情包在用），**没有 tesseract / paddleocr**。
  装 PaddleOCR 要拉几百 MB 模型，还得处理中英混排和竖排 —— 不值得。
  实际瓶颈不是「没有 OCR」，而是**小字/低对比度/深底浅字**送进视觉模型时糊掉了。
  所以这里只做三件确定有效的事，再把结果交给**已经在用的视觉模型**：
    ① 放大（宽度不足 `OCR_UPSCALE_MIN_W` 就 cubic 放大）；
    ② CLAHE 局部对比度 + 锐化；
    ③ 用形态学梯度找**文字块**，逐块裁剪再放大 → 送模型逐块读。

依赖方向：plugins → services（本模块不 import 任何 plugins）。
"""
from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

MAX_SIDE = tuning.i("OCR_MAX_SIDE")
UPSCALE_MIN_W = tuning.i("OCR_UPSCALE_MIN_W")
MAX_CROPS = tuning.i("OCR_MAX_CROPS")
TIMEOUT = tuning.f("OCR_TIMEOUT")

OUT_DIR = Path(os.getenv("OCR_OUT_DIR", "data/ocr"))
PROMPT = (
    "只输出图片里**看得见的文字**，按原文换行；"
    "看不清的字用「□」代替，不要猜、不要翻译、不要解释。"
    "没有文字就只回「无文字」。"
)


# ---------------------------------------------------------------- OpenCV
def available() -> bool:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


def _encode(img) -> bytes:
    import cv2
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes() if ok else b""


def variants(data: bytes) -> list:
    """返回 [(说明, png_bytes)]：整图增强版 + 最多 `OCR_MAX_CROPS` 个文字块。

    OpenCV 不可用时返回 []（调用方给出可读的失败原因）。
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    if not data:
        return []
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return []

    h, w = img.shape[:2]
    if w and w < UPSCALE_MIN_W:
        s = min(3.0, UPSCALE_MIN_W / float(w))
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_CUBIC)
    h, w = img.shape[:2]
    if max(h, w) > MAX_SIDE:
        s = MAX_SIDE / float(max(h, w))
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    h, w = img.shape[:2]

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # 深底浅字 → 先反色，CLAHE 对两种极性都友好
    if float(gray.mean()) < 110:
        gray = 255 - gray
    gray = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)

    out = [("整图（放大+增强）", _encode(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)))]

    # 文字块：形态学梯度 → 闭运算把一行字连成块
    grad = cv2.morphologyEx(
        gray, cv2.MORPH_GRADIENT,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
    _, bw = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    closed = cv2.morphologyEx(
        bw, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (25, 9)))
    cnts = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                            cv2.CHAIN_APPROX_SIMPLE)[-2]
    boxes = []
    for c in cnts:
        x, y, bw_, bh_ = cv2.boundingRect(c)
        if bw_ < 40 or bh_ < 12 or bw_ * bh_ < 600:
            continue
        if bw_ / max(1, bh_) < 1.2:      # 细长竖条多是图标/边框
            continue
        boxes.append((bw_ * bh_, x, y, bw_, bh_))
    boxes.sort(reverse=True)

    for i, (_a, x, y, bw_, bh_) in enumerate(boxes[:MAX_CROPS], 1):
        pad = 10
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(w, x + bw_ + pad), min(h, y + bh_ + pad)
        if x1 - x0 < 20 or y1 - y0 < 10:
            continue
        crop = gray[y0:y1, x0:x1]
        crop = cv2.resize(crop, None, fx=1.5, fy=1.5,
                          interpolation=cv2.INTER_CUBIC)
        crop = cv2.filter2D(crop, -1,
                            np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]]))
        out.append(("文字块%d" % i, _encode(cv2.cvtColor(crop, cv2.COLOR_GRAY2BGR))))
    return out


def save_debug(items: list, tag: str = "") -> list:
    """把增强后的图存一份，方便 `/测函数` 或人工复核。返回路径列表。"""
    paths = []
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stem = "%s_%s" % (time.strftime("%H%M%S"), hashlib.md5(
            (tag or str(time.time())).encode()).hexdigest()[:6])
        for i, (_n, png) in enumerate(items or []):
            p = OUT_DIR / ("%s_%d.png" % (stem, i))
            p.write_bytes(png)
            paths.append(str(p))
    except Exception:
        pass
    return paths


# ---------------------------------------------------------------- 视觉模型
async def _ask(b64: str, prompt: str) -> str:
    """把一张图交给视觉模型读字；主 provider 失败就换 vision_fallback。"""
    import httpx
    from . import providers

    for role in ("vision", "vision_fallback"):
        try:
            p = providers.resolve(role)
        except Exception:
            continue
        if not p.get("api_key"):
            continue
        payload = {
            "model": p.get("model"),
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": "data:image/png;base64," + b64}},
                ],
            }],
            "temperature": 0,
            "max_tokens": 800,
        }
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as c:
                r = await c.post(
                    providers.chat_url(role),
                    headers={"Authorization": "Bearer %s" % p["api_key"],
                             "Content-Type": "application/json"},
                    json=payload)
                if r.status_code in (401, 403, 429, 500, 502, 503, 504):
                    logger.debug("[OCR] %s HTTP %s → 换下一家" % (role, r.status_code))
                    continue
                r.raise_for_status()
                data = r.json()
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                txt = str(msg.get("content") or "").strip()
                if txt:
                    return txt
        except Exception as e:
            logger.debug("[OCR] %s 失败：%s: %s" % (role, type(e).__name__, e))
            continue
    return ""


async def read_text(data: bytes, *, prompt: str = "") -> dict:
    """增强 + 逐块识别，返回 {ok, text, blocks, provider_hint, saved}。

    `blocks` 是成功读到字的块数；`saved` 是增强图的落盘路径（复核用）。
    """
    if not available():
        return {"ok": False, "text": "", "err": "OpenCV 不可用", "blocks": 0}
    items = variants(data)
    if not items:
        return {"ok": False, "text": "", "err": "不是有效图片", "blocks": 0}
    saved = save_debug(items, tag=str(len(data)))

    lines: list = []
    _seen: set = set()
    blocks = 0
    for _name, png in items:
        b64 = base64.b64encode(png).decode()
        txt = await _ask(b64, prompt or PROMPT)
        if not txt:
            continue
        if "无文字" in txt and len(txt) <= 6:
            continue
        blocks += 1
        for ln in txt.splitlines():
            ln = ln.strip()
            # 整图块和裁剪块会读到同一行，只差空格 → 归一化后去重
            key = " ".join(ln.split())
            if key and key not in _seen:
                _seen.add(key)
                lines.append(ln)
        if blocks >= 1 and len(lines) >= 12:     # 已经够多，别把 token 烧在重复块上
            break

    text = "\n".join(lines)[:1200]
    return {"ok": bool(text), "text": text, "blocks": blocks, "saved": saved}
