# services/vision_report.py
"""ImageReport：一张图只产出一份「结构化事实」（2026-11-27 P0）。

为什么要有（用户 2026-11-27）：
  「识图要么先在前面理解一遍做标签，然后角色扮演 llm 还要再去识图一遍，
    然后还要再识字一遍」—— 同一张图被读了 2~6 次，而且「识字」交给了最不擅长
    识字的视觉模型；标签 LLM 只拿到 60 字摘要，判不出「这张图是发给谁的」。

设计（见 docs/addressing-design.md §7.6）：
  · **质量/有没有字** 用 OpenCV 算（确定性、毫秒级、不幻觉）；
  · **画面/类型/情绪** 由视觉模型给一次；
  · **文字** P0 仍来自视觉模型（标 source=vlm，只当线索）；P1 装 RapidOCR 后改 ocr；
  · 产出一次，三处复用：角色扮演 prompt / 收件人判定（group_scene）/ 记忆。

边界：
  · 本模块**不调模型**、不写盘（缓存沿用 vision_verify 的 fingerprint 缓存）；
  · OpenCV 不可用时 `quality_of()` 返回 {}，调用方按「没有质量信息」降级，不报错。
"""
from __future__ import annotations

import hashlib
import re

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

ENABLED = bool(tuning.i("VISION_REPORT"))
# 进 prompt 的文字上限（全文留在 report/缓存里，别把 token 撑爆）
TEXT_MAX = max(0, tuning.i("VISION_TEXT_MAX"))

# 清晰度分档（Laplacian 方差；先把最长边缩到 1024 再算，保证不同分辨率可比）
SHARP_BLUR = 50.0
SHARP_OK = 150.0
# 文字区域占比低于这个值 → 判「没有字」，不触发 OCR
TEXT_RATIO_MIN = 0.005

_ADDR_HINT = re.compile(r"@|「[^」]{1,10}」")


def _resize_max(img, side: int = 1024):
    import cv2
    h, w = img.shape[:2]
    if max(h, w) <= side:
        return img
    s = side / float(max(h, w))
    return cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)


def quality_of(data: bytes) -> dict:
    """OpenCV：清晰度 + 文字区域占比。返回 {} 表示算不了（没装 cv2 / 不是图）。"""
    if not data:
        return {}
    try:
        import cv2
        import numpy as np
    except Exception:
        return {}
    try:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return {}
        h0, w0 = img.shape[:2]
        img = _resize_max(img)
        h, w = img.shape[:2]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
        label = ("blur" if sharp < SHARP_BLUR
                 else "slightly_blur" if sharp < SHARP_OK else "clear")
        # 纯色/近纯色（表情底色、白底长图）：不是「糊」，别让她说「图有点糊」
        if float(gray.std()) < 3.0:
            return {
                "w": int(w0), "h": int(h0),
                "sharpness": 0.0, "quality": "clear",
                "text_ratio": 0.0, "has_text": False,
            }
        if sharp < SHARP_BLUR:
            # 糊到这种程度时，形态学会把噪点连成「字块」（实测糊图误报 6.5%），
            # 而且真糊了也读不出来 → 直接判「无字」，别触发 OCR / 审核。
            return {
                "w": int(w0), "h": int(h0),
                "sharpness": round(sharp, 1), "quality": label,
                "text_ratio": 0.0, "has_text": False,
            }

        # 文字粗筛：形态学梯度 → Otsu → 横向闭运算把一行字连成块
        grad = cv2.morphologyEx(
            gray, cv2.MORPH_GRADIENT,
            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)))
        _, bw = cv2.threshold(grad, 0, 255,
                              cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        closed = cv2.morphologyEx(
            bw, cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (25, 9)))
        cnts = cv2.findContours(closed, cv2.RETR_EXTERNAL,
                                cv2.CHAIN_APPROX_SIMPLE)[-2]
        area = 0
        for c in cnts:
            x, y, bw_, bh_ = cv2.boundingRect(c)
            if bw_ < 40 or bh_ < 12 or bw_ * bh_ < 600:
                continue
            if bw_ / max(1, bh_) < 1.2:      # 细长竖条多是图标/边框
                continue
            area += bw_ * bh_
        ratio = area / float(max(1, w * h))
        return {
            "w": int(w0), "h": int(h0),
            "sharpness": round(sharp, 1),
            "quality": label,
            "text_ratio": round(ratio, 4),
            "has_text": bool(ratio >= TEXT_RATIO_MIN),
        }
    except Exception as e:
        logger.debug(f"[VISION] 质量评估失败: {type(e).__name__}: {e}")
        return {}


def build(rec: dict, data: bytes = b"", *, cost_ms: int = 0,
          partial: bool = False) -> dict:
    """把「视觉模型结果 + OpenCV 质量」合成一份 ImageReport。

    `rec` 是 `services/vision_verify.recognize()` 的返回（scene/work/char/text/kind/…）。
    """
    rec = rec or {}
    q = quality_of(data)
    text = str(rec.get("text") or "").strip()
    rep = {
        "sha1": hashlib.sha1(data).hexdigest()[:16] if data else "",
        "w": q.get("w", 0), "h": q.get("h", 0),
        "sharpness": q.get("sharpness", 0.0),
        "quality": q.get("quality", ""),
        "has_text": q.get("has_text", bool(text)),
        "text_ratio": q.get("text_ratio", 0.0),
        # P0：文字来自视觉模型 → source=vlm（只当线索）；P1 换成 ocr 才算事实
        "text_blocks": ([{"text": text, "conf": None, "source": "vlm"}]
                        if text else []),
        "kind": str(rec.get("kind") or ""),
        "scene": str(rec.get("scene") or ""),
        "char": str(rec.get("char") or ""),
        "work": str(rec.get("work") or ""),
        "char_conf": 0.9 if rec.get("verified") else 0.0,
        "sources": sorted((rec.get("sources") or {}).keys()),
        "cost_ms": int(cost_ms or 0),
        "partial": bool(partial),
    }
    return rep


def text_of(rep: dict, limit: int = 0) -> str:
    """report 里的文字（多块拼起来，去重），默认按 TEXT_MAX 截。"""
    out, seen = [], set()
    for b in (rep or {}).get("text_blocks") or []:
        t = str((b or {}).get("text") or "").strip()
        k = " ".join(t.split())
        if t and k not in seen:
            seen.add(k)
            out.append(t)
    s = "\n".join(out)
    n = int(limit or TEXT_MAX or 0)
    return s[:n] if n > 0 else s


def render_line(rep: dict) -> str:
    """给角色扮演 prompt 的一行「画面质量」（≤40 字）。"""
    rep = rep or {}
    if not rep:
        return ""
    bits = []
    q = str(rep.get("quality") or "")
    sharp = rep.get("sharpness") or 0
    if q == "blur":
        bits.append(f"偏糊（清晰度 {sharp:.0f}）")
    elif q == "slightly_blur":
        bits.append(f"一般（清晰度 {sharp:.0f}）")
    elif q:
        bits.append(f"清晰（{sharp:.0f}）")
    txt = text_of(rep)
    if txt:
        bits.append(f"图里文字 {len(txt)} 字（识图模型读的，可能不准）")
    elif rep.get("has_text"):
        bits.append("图里有字但没读清")
    return ("【画面质量】" + "；".join(bits)) if bits else ""


def brief(rep: dict, width: int = 40) -> str:
    """给 group_scene / 群聊上下文的一行摘要。"""
    rep = rep or {}
    scene = str(rep.get("scene") or "").strip()
    txt = text_of(rep, 20).replace("\n", " ")
    s = scene[:width] or "（没描述出来）"
    if txt:
        s += f"｜字:{txt}"
    return f"[图片] {s}"


def should_audit(rep: dict) -> bool:
    """这张图要不要送审核 LLM（有字 + 字里有 @/称呼 才送，别每张都花一次调用）。"""
    rep = rep or {}
    if not rep.get("has_text"):
        return False
    return bool(_ADDR_HINT.search(text_of(rep, 200)))
