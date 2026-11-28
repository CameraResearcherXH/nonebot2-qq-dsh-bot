# services/glm.py
"""
GLM-4-Flash 参考通道（参考1）。

定位：同一个模板，换一个模型再答一遍。
  我们把自己的那套东西（人设 / 档案 / 记忆 / 群聊上下文，也就是 build_prompt
  的产物）原封不动丢给 GLM，让它在同一个框架里给出第二个答案。
  这样两边的回答是可直接比较的 —— 名词、梗、事实对不上就是有问题。

为什么不用 GLM 裸答：裸答的风格/长度/称呼全不一样，diff 出来的全是噪音。
喂了同一份模板，diff 才有意义。

GLM-4-Flash 免费（有频率限制），所以可以每轮都跑一次。
"""
from core import tuning
import os

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

GLM_API_KEY = os.getenv("GLM_API_KEY", "")
GLM_BASE_URL = os.getenv(
    "GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4/chat/completions"
)
# 智谱 5.3-flash 是**多模态**的（同一个模型既能读文也能读图），
# 所以文本和视觉默认用同一个名字，不像 4 代那样要分 glm-4-flash / glm-4v-flash。
GLM_MODEL = os.getenv("GLM_MODEL", "glm-5.3-flash")
GLM_VISION_MODEL = os.getenv("GLM_VISION_MODEL", GLM_MODEL)
GLM_TIMEOUT = tuning.f("GLM_TIMEOUT")
# 5.3 支持思考模式。开了之后 thinking 会吃掉大量 token，
# max_tokens 太小会导致 content 直接为空（实测 20 就是空，30 才出内容）。
# 参考1 是「说人话」的任务，不需要思考，默认关。
GLM_THINKING = os.getenv("GLM_THINKING", "false").lower() == "true"
# 2026-11-28：GLM-5.3-flash **强制思考**（thinking.type 只能 enabled），
#   默认 reasoning_effort=max，实测 900 token 要 23s。参考1 只是「说人话」，
#   用 low 档就够；GLM-4 系仍按 GLM_THINKING 开关（它们没有档位）。
GLM_REASONING_EFFORT = (os.getenv("GLM_REASONING_EFFORT", "low") or "low").strip().lower()
GLM_MAX_TOKENS = tuning.i("GLM_MAX_TOKENS")

# 模型名降级链：不同账号/套餐上架的模型不一样，写死一个名字会直接 model_not_found。
# 按顺序试，第一个能用的记下来，下次直接用。
_MODEL_CANDIDATES = []
for _m in (os.getenv("GLM_MODEL_CANDIDATES") or
           f"{GLM_MODEL},glm-5.3-flash,glm-4-flash,glm-4v-flash,glm-4-air,glm-4-flash-250414"
           ).split(","):
    _m = _m.strip()
    if _m and _m not in _MODEL_CANDIDATES:
        _MODEL_CANDIDATES.append(_m)

# probe_glm.py 探测完会把结论写进 data/glm_model.txt，这里启动时读回来，
# 免得每次重启都要重新降级试一遍。
_working_model = None
try:
    _f = __import__("pathlib").Path("data/glm_model.txt")
    if _f.exists():
        _v = _f.read_text(encoding="utf-8").strip()
        if _v:
            _working_model = _v
except Exception:
    pass


def available() -> bool:
    return bool(GLM_API_KEY)


def _model_order(prefer: str = "", extra=None) -> list:
    order = []
    for m in (([prefer] if prefer else []) + list(extra or [])
              + ([_working_model] if _working_model else []) + _MODEL_CANDIDATES):
        if m and m not in order:
            order.append(m)
    return order


async def chat(
    messages: list,
    *,
    model: str = "",
    temperature: float = 0.6,
    max_tokens: int = 400,
    timeout: float = 0,
    candidates: list | None = None,
    remember: bool = True,
) -> str:
    """最底层的调用。模型名按降级链逐个试，全失败返回空字符串，绝不抛。

    candidates：额外候选（视觉用，避免落到纯文本模型上）
    remember=False：成功也不写 _working_model（视觉和文本分开记，别互相污染）
    """
    global _working_model
    if not GLM_API_KEY:
        logger.debug("[GLM] 没有 GLM_API_KEY，跳过")
        return ""

    import httpx
    last_err = ""
    for m in _model_order(model, candidates):
        payload = {
            "model": m,
            "messages": messages,
            "temperature": temperature,
            # thinking 会先吃掉一大截 token，给少了 content 会直接是空的
            "max_tokens": max(max_tokens, GLM_MAX_TOKENS) if GLM_THINKING else max_tokens,
        }
        _m_low = str(m or "").lower()
        if "glm-5" in _m_low or "5.3" in _m_low or "5.2" in _m_low:
            # 5.x 关不掉思考 → 显式给最低档，别让它默认 max（23s）
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = {
                "low": "low", "off": "low",
                "medium": "high", "high": "max",
            }.get(GLM_REASONING_EFFORT, "low")
        elif GLM_THINKING:
            payload["thinking"] = {"type": "enabled"}
        try:
            async with httpx.AsyncClient(timeout=timeout or GLM_TIMEOUT) as c:
                r = await c.post(
                    GLM_BASE_URL,
                    headers={"Authorization": f"Bearer {GLM_API_KEY}",
                             "Content-Type": "application/json"},
                    json=payload,
                )
            if r.status_code == 200:
                data = r.json()
                try:
                    from . import llm_usage as _lu
                    _lu.record_from_response(data, purpose="preflight")
                except Exception:
                    pass
                msg = data["choices"][0]["message"]
                content = msg.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        str(i.get("text", "")) for i in content if isinstance(i, dict)
                    )
                text = str(content or "").strip()
                if not text:
                    # 思考型模型可能把正文放在 reasoning_content，content 为空
                    rc = str(msg.get("reasoning_content") or "").strip()
                    if rc:
                        logger.info(f"[GLM] {m} content 为空，改用 reasoning_content")
                        text = rc
                if text:
                    if remember and m != _working_model:
                        logger.info(f"[GLM] ✅ 可用模型：{m}")
                        _working_model = m
                    return text
                last_err = f"{m} 返回空内容（thinking 吃掉了 token？调大 GLM_MAX_TOKENS）"
                logger.warning(f"[GLM] {last_err}")
                continue

            body = r.text[:160]
            last_err = f"HTTP {r.status_code}: {body}"
            logger.warning(f"[GLM] {m} → HTTP {r.status_code}: {body[:100]}")
            low = body.lower()
            # 模型不存在 / 没权限 → 换下一个
            if r.status_code in (400, 404) and (
                "model" in low or "not exist" in low or "not found" in low
            ):
                continue
            # 纯文本模型收到图片 → 1210 content.type 非法，换下一个（换到 4v 系列就能读图）
            if r.status_code == 400 and ("content.type" in low or "1210" in low):
                logger.info(f"[GLM] {m} 不支持图片，换下一个模型")
                continue
            if r.status_code in (401, 403):
                logger.error("❌ [GLM] key 无效或没权限，停止重试")
                return ""
            # 限流/欠费 → 也没必要换模型
            return ""
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            logger.warning(f"[GLM] {m} 请求异常: {last_err}")
            continue

    logger.error(
        f"[GLM] ❌ 所有候选模型都失败"
        f"（试过 {_model_order(model, candidates)}），最后错误：{last_err}")
    return ""


async def reply(system_prompt: str, prompt_text: str, *, max_tokens: int = 400) -> str:
    """
    参考1：拿我们自己的模板让 GLM 再答一遍。
      system_prompt = 我们的人设
      prompt_text   = build_prompt 的产物（档案 / 记忆 / 上下文全在里面）
    """
    msgs = []
    if str(system_prompt or "").strip():
        msgs.append({"role": "system", "content": str(system_prompt)[:4000]})
    msgs.append({"role": "user", "content": str(prompt_text or "")[:8000]})
    return await chat(msgs, max_tokens=max_tokens)


def _shrink(image_bytes: bytes) -> tuple:
    """按 VISION_MAX_SIDE 缩图并转 JPEG（服务层自己实现，不依赖 plugins）。"""
    if not image_bytes:
        return image_bytes, "image/jpeg"
    try:
        import io

        from PIL import Image
    except Exception:
        return image_bytes, "image/jpeg"
    max_side = int(tuning.i("VISION_MAX_SIDE") or 1568)
    try:
        im = Image.open(io.BytesIO(image_bytes))
        if getattr(im, "is_animated", False):
            try:
                im.seek(0)
            except Exception:
                pass
        im = im.convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90, optimize=True)
        out = buf.getvalue()
        if len(out) >= len(image_bytes):
            return image_bytes, "image/jpeg"
        return out, "image/jpeg"
    except Exception:
        return image_bytes, "image/jpeg"


async def describe_image(image_bytes: bytes, prompt: str, *, max_tokens: int = 120) -> str:
    """
    让 GLM-4V 也看一眼图（参考视觉）。
    图以 base64 传，智谱兼容这种写法；失败返回空，不影响主流程。
    """
    if not GLM_API_KEY or not image_bytes:
        return ""
    import base64
    try:
        # 大图先缩：7MB 原图直接 base64 → 9MB 请求体，慢且模型会自己再压（更糊）。
        image_bytes, mime = _shrink(image_bytes)
        b64 = base64.b64encode(image_bytes).decode("utf-8")
        msgs = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                {"type": "text", "text": str(prompt or "这张图里是什么？一句话说清。")},
            ],
        }]
        # ⚠️ 2026-11-24 实测（本账号）：
        #   · glm-5.3 / glm-4v-flash 读图直接 1210（content.type 只收 text）；
        #   · glm-5.3-flash 能读图，但「始终思考」，900 token 要 23s，太慢；
        #   · glm-4v-plus 4s 出正文，稳定。
        # 所以视觉单独一条候选链，成功也不写 _working_model（那是给文本用的）。
        return await chat(msgs, model=GLM_VISION_MODEL, temperature=0.2,
                          max_tokens=max(max_tokens, 200),
                          candidates=["glm-4v-plus", "glm-4v-flash"],
                          remember=False)
    except Exception as e:
        logger.warning(f"[GLM] 视觉调用异常: {type(e).__name__}: {e}")
        return ""
