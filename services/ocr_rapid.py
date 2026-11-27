# services/ocr_rapid.py
"""RapidOCR 本地文字识别（P1，2026-11-27）。

⚠️ 和 `services/ocr.py` 的分工（别混）：
  · 本文件 = **本地推理**（RapidOCR/onnxruntime），给识图主链路用：
    OpenCV 判「有字」→ 线程池 OCR → 原文进 ImageReport；
  · `services/ocr.py` = `plugins/ocr_lite` 的**旁路工具**（OpenCV 增强/裁剪 →
    视觉模型读字），参数是 `OCR_MAX_SIDE / OCR_UPSCALE_MIN_W / OCR_MAX_CROPS`。
  两套参数前缀不同（`OCR_RAPID_*` vs `OCR_*`），互不影响。

为什么单独做 OCR：
  视觉模型（qwen-vl / glm-4v）看**画面**可靠，但让它「逐字照抄」会改写、漏字、
  把弹幕当正文；OCR 是确定性的：像素 → 字，不带幻觉。
  实测一张 QQ 群截图（长边 1024）：4 个文本块全对，1.65s。

成本（本机轻量服务器实测：2 vCPU / 3.5GB）：
  · 首次加载 0.5s；onnxruntime 会话常驻 RSS ≈350MB，峰值 ≈556MB；
  · 单图 0.7~2.7s —— 所以**必须**在线程池里跑，且只在 OpenCV 判「有字」时才跑。

边界（fail-open）：
  · 没装 rapidocr / 模型加载失败 / 超时 / 解码失败 → 返回 []，调用方退回视觉模型的文字；
  · 单例 + 串行锁：2 vCPU 上并发 OCR 只会互相拖慢，还抢事件循环。

开关（core/tuning.py）：
  VISION_OCR=1            总开关（0 = 完全回到视觉模型识字）
  OCR_RAPID_MAX_SIDE=1024 送进 OCR 前的最长边
  OCR_RAPID_MIN_CONF=0.5  单块置信度下限
  OCR_RAPID_TIMEOUT=8     单图超时（秒）
  OCR_RAPID_THREADS=2     onnxruntime 线程数
"""
from __future__ import annotations

import asyncio
import os

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

ENABLED = bool(tuning.i("VISION_OCR"))
MAX_SIDE = max(320, tuning.i("OCR_RAPID_MAX_SIDE"))
MIN_CONF = max(0.0, min(1.0, tuning.f("OCR_RAPID_MIN_CONF")))
TIMEOUT = max(1.0, tuning.f("OCR_RAPID_TIMEOUT"))
THREADS = max(1, tuning.i("OCR_RAPID_THREADS"))

_ENGINE = None
_LOAD_FAILED = False
_IMPORT_OK = None          # None=还没试过；False=没装
_LOCK: asyncio.Lock | None = None


def available() -> bool:
    """装没装（只 import，不加载模型，毫秒级）。"""
    global _IMPORT_OK
    if _IMPORT_OK is None:
        try:
            import rapidocr_onnxruntime  # noqa: F401
            _IMPORT_OK = True
        except Exception as e:
            _IMPORT_OK = False
            logger.info(f"[OCR] 未安装 rapidocr（识字退回视觉模型）: {type(e).__name__}: {e}")
    return bool(_IMPORT_OK)


def _engine():
    """懒加载单例。第一次识图才吃那 350MB，平时不占内存。"""
    global _ENGINE, _LOAD_FAILED
    if _ENGINE is not None:
        return _ENGINE
    if _LOAD_FAILED or not ENABLED:
        return None
    try:
        # 必须在 import onnxruntime 之前设置，否则线程数已经定死了
        os.environ.setdefault("OMP_NUM_THREADS", str(THREADS))
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
        logger.info(f"[OCR] RapidOCR 已加载（threads={THREADS} max_side={MAX_SIDE}）")
    except Exception as e:
        _LOAD_FAILED = True
        logger.warning(f"[OCR] RapidOCR 加载失败（退回视觉模型识字）: {type(e).__name__}: {e}")
        return None
    return _ENGINE


def _decode(data: bytes):
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    try:
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        h, w = img.shape[:2]
        if max(h, w) > MAX_SIDE:
            s = MAX_SIDE / float(max(h, w))
            img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        return img
    except Exception as e:
        logger.debug(f"[OCR] 解码失败: {type(e).__name__}: {e}")
        return None


def read(data: bytes) -> list:
    """同步识图 → [{"text","conf","box","source":"ocr"}]。任何失败返回 []。"""
    if not (ENABLED and data) or not available():
        return []
    engine = _engine()
    if engine is None:
        return []
    img = _decode(data)
    if img is None:
        return []
    try:
        result, _elapse = engine(img)
    except Exception as e:
        logger.warning(f"[OCR] 识别异常: {type(e).__name__}: {e}")
        return []
    blocks = []
    for row in result or []:
        try:
            box, text, score = row[0], str(row[1] or "").strip(), float(row[2])
        except Exception:
            continue
        if not text or score < MIN_CONF:
            continue
        blocks.append({"text": text, "conf": round(score, 3),
                       "box": box, "source": "ocr"})
    return blocks


async def read_async(data: bytes, *, timeout: float = 0.0) -> list:
    """异步入口：线程池 + 串行锁 + 超时。绝不抛异常。"""
    if not (ENABLED and data) or not available():
        return []
    global _LOCK
    if _LOCK is None:
        _LOCK = asyncio.Lock()
    if _LOCK.locked():
        logger.info("[OCR] 上一张还在识别，本张跳过（不排队拖慢回复）")
        return []
    try:
        async with _LOCK:
            return await asyncio.wait_for(
                asyncio.to_thread(read, data), timeout=timeout or TIMEOUT)
    except asyncio.TimeoutError:
        logger.info(f"[OCR] 超时 {timeout or TIMEOUT:.0f}s，本张不识字")
    except Exception as e:
        logger.debug(f"[OCR] 异步识别失败（忽略）: {type(e).__name__}: {e}")
    return []


def text_of(blocks: list, limit: int = 0) -> str:
    """多块拼成一段（去重），按行。"""
    out, seen = [], set()
    for b in blocks or []:
        t = str((b or {}).get("text") or "").strip()
        k = " ".join(t.split())
        if t and k not in seen:
            seen.add(k)
            out.append(t)
    s = "\n".join(out)
    return s[:limit] if limit and limit > 0 else s
