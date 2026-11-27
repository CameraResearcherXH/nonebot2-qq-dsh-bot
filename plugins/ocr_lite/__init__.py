"""plugins/ocr_lite —— 图里字看不清时，用 OpenCV 增强后再让视觉模型读。

用户 2026-11-27：
  「图片内容如果有字体，她老是看不清楚……识图是一方面，再加个从图片提取的字体。
   比如 llm 觉得字体看不清就去调用 opencv 插件提字。可以简单写个这样的插件。」

为什么不装 OCR 引擎：服务器上只有 OpenCV（没有 tesseract / paddleocr），
装 PaddleOCR 要几百 MB 模型；而真正的瓶颈是「小字/低对比度送进视觉模型时糊了」。
所以这个插件做的是：**放大 + CLAHE + 锐化 + 按文字块裁剪**，再交给已经在用的
视觉 provider（`services/ocr.py`，主 provider 失败自动换 `vision_fallback`）。

用法（她自己在 function call 里调）：
    ocr_image()                 # 读**当前这条消息**里的图
    ocr_image(image="/path.png")  # 指定文件
    ocr_image(image="https://…")  # 指定 URL
"""
from __future__ import annotations

from pathlib import Path

import httpx
from nonebot import logger

from services import llm as llm_mod
from services import ocr

_DESC = (
    "把图片里的**文字**读出来（OpenCV 放大/增强/按文字块裁剪后交给视觉模型）。"
    "适用：截图、带字的梗图、作业/表格、游戏界面、封面标题等「字太小/太糊看不清」的情况。"
    "不传 image 就读当前这条消息里的图。只返回文字，不描述画面。"
)


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.content


async def _bytes_of(args: dict) -> tuple:
    """取图：显式路径/URL 优先，否则用当前消息里的图片段。"""
    src = str((args or {}).get("image") or "").strip()
    if src:
        if src.startswith(("http://", "https://")):
            try:
                return await _download(src), ""
            except Exception as e:
                return b"", "下载图片失败：%s: %s" % (type(e).__name__, e)
        p = Path(src)
        if p.exists():
            return p.read_bytes(), ""
        return b"", "找不到这个文件：%s" % src

    ev = llm_mod.TOOL_CTX.get("event")
    if ev is None:
        return b"", "没有指定图片，当前也没有消息上下文。"
    try:
        for seg in ev.get_message():
            if getattr(seg, "type", "") != "image":
                continue
            d = getattr(seg, "data", {}) or {}
            url = str(d.get("url") or "")
            if url:
                try:
                    return await _download(url), ""
                except Exception:
                    pass
            f = str(d.get("file") or "")
            if f and Path(f).exists():
                return Path(f).read_bytes(), ""
    except Exception as e:
        return b"", "取图失败：%s: %s" % (type(e).__name__, e)
    return b"", "这条消息里没有图片（图可能在更早的消息里）。"


async def _tool_ocr(args: dict) -> str:
    data, err = await _bytes_of(args or {})
    if err:
        return err
    r = await ocr.read_text(data, prompt=str((args or {}).get("prompt") or ""))
    if not r.get("ok"):
        return ("没读到字（%s）。可以让她把图截大一点、只截文字区域再发一次。"
                % (r.get("err") or "模型说图里没有文字"))
    logger.info("[OCR] 读到 %d 块 / %d 字" % (r.get("blocks") or 0, len(r.get("text") or "")))
    return "图里的文字（OpenCV 增强后识别，按原文）：\n" + str(r.get("text") or "")


def _register() -> None:
    llm_mod.register_tool(
        {
            "type": "function",
            "function": {
                "name": "ocr_image",
                "description": _DESC,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "image": {
                            "type": "string",
                            "description": "可选：图片路径或 URL；不填 = 当前消息里的图",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "可选：想让它重点读什么（默认只读全部文字）",
                        },
                    },
                },
            },
        },
        _tool_ocr,
        scope=("private", "group"),
    )


try:
    if not ocr.available():
        logger.warning("[OCR] OpenCV 不可用 → ocr_image 工具不注册")
    else:
        _register()
        logger.info("[OCR] 已注册工具 ocr_image（OpenCV 增强 + 视觉模型读字）")
except Exception as e:  # pragma: no cover
    logger.warning("[OCR] 注册失败：%s: %s" % (type(e).__name__, e))
