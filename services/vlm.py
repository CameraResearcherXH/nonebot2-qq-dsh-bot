# services/vlm.py
"""通用视觉模型调用（任意 provider 角色，2026-11-27）。

背景（用户 2026-11-27）：
  「GLM 额度用完了。之后尽量靠 qwen 视觉来吧。我们提供可选视觉模型。
    但是我们自己先用 qwen 视觉。」
  —— 主视觉走 `providers.resolve("vision")`（默认 qwen），
     第二视觉源走 `providers.resolve("vision_second")`（默认关闭，用户想开哪家配哪家）。

和 plugins/vision_agent._vl_describe 的区别：
  那个是「主识图链路」，带 URL/压缩/模型降级/额度兜底；
  这里只给**辅助证据源**用：一次调用、失败就静默返回空，不拖慢主链路。

用法：
    from services import vlm
    text = await vlm.describe("vision_second", image_bytes, prompt, max_tokens=400)
"""
from __future__ import annotations

import base64
import io

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

# 辅助视觉源不需要超大图：长边压到 1280、JPEG 85，请求体小一半，速度也快
MAX_SIDE = 1280
JPEG_Q = 85
TIMEOUT = max(5.0, tuning.f("VISION_TIMEOUT"))


def _shrink(data: bytes) -> tuple[bytes, str]:
    """尽量压缩；Pillow 不可用就原样发。"""
    try:
        from PIL import Image
    except Exception:
        return data, "image/jpeg"
    try:
        im = Image.open(io.BytesIO(data))
        fmt = (im.format or "JPEG").upper()
        if fmt == "GIF":
            im.seek(0)
        im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > MAX_SIDE:
            s = MAX_SIDE / float(max(w, h))
            im = im.resize((int(w * s), int(h * s)))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=JPEG_Q)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return data, "image/jpeg"


def _target(role: str) -> dict:
    """解析角色 → {provider, base_url, api_key, model}；没配齐返回 {}。"""
    try:
        from . import providers
        p = providers.resolve(role)
    except Exception as e:
        logger.debug(f"[VLM] 解析 {role} 失败: {type(e).__name__}: {e}")
        return {}
    if not all(str(p.get(k) or "").strip()
               for k in ("provider", "base_url", "api_key", "model")):
        return {}
    return p


def available(role: str) -> bool:
    return bool(_target(role))


async def describe(role: str, image_bytes: bytes, prompt: str, *,
                   max_tokens: int = 400, timeout: float = 0.0) -> str:
    """调一次视觉模型，返回正文；没配 / 失败 / 空返回 ""。"""
    if not image_bytes:
        return ""
    p = _target(role)
    if not p:
        return ""
    small, mime = _shrink(image_bytes)
    b64 = f"data:{mime};base64,{base64.b64encode(small).decode('utf-8')}"
    base = str(p["base_url"]).rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    payload = {
        "model": p["model"],
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": b64}},
            {"type": "text", "text": prompt},
        ]}],
        "temperature": 0.1,
        "max_tokens": int(max_tokens),
    }
    if str(p.get("max_tokens_field") or "") == "max_completion_tokens":
        payload["max_completion_tokens"] = payload.pop("max_tokens")
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout or TIMEOUT) as c:
            r = await c.post(url, headers={
                "Authorization": f"Bearer {p['api_key']}",
                "Content-Type": "application/json",
            }, json=payload)
        if r.status_code != 200:
            logger.info(f"[VLM] {role} HTTP {r.status_code}: {r.text[:120]}")
            return ""
        j = r.json()
        content = j["choices"][0]["message"].get("content") or ""
        if isinstance(content, list):     # 少数中转站返回分段 content
            content = "".join(str(x.get("text") or "") for x in content
                              if isinstance(x, dict))
        return str(content).strip()
    except Exception as e:
        logger.info(f"[VLM] {role} 调用失败: {type(e).__name__}: {e}")
        return ""
