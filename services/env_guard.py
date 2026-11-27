"""启动期配置体检：把「静默失效」的配置变成一条显眼的 WARNING。

为什么要有（2026-11-26，来自别人部署后的真实反馈）：
  · `ALLOWED_GROUPS` / `ALLOWED_PRIVATE` 留空 → 群聊/私聊**全部不理**，
    用户看到的是「时不时不理人，私聊艾特都不理」，其实一条都没进来；
  · `OUT_BUBBLE_CHARS` / `GROUP_MAX_HANZI` 被写成 1000 → 长回复不再转合并转发，
    一条几千字直接刷屏（我们线上是 40 / 20）；
  · `USE_LOCAL_TTS=1` 但没有算力机 → 每条语音都要白等到超时才降级。

只打日志、不阻断启动；`/自检` 也会用到 `report_text()`。
"""
import os

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

# 回复长度相关的「正常范围」：超过就几乎肯定是配错了（不是调参）
_BUBBLE_MAX_SANE = 200
_HANZI_MAX_SANE = 200
_FORWARD_MIN_SANE = 2000


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _ids(key: str) -> list:
    return [x for x in _env(key).replace("，", ",").split(",") if x.strip()]


def check() -> list:
    """返回警告列表（同时逐条打 WARNING）。"""
    warns = []

    if not _ids("ALLOWED_GROUPS"):
        warns.append("ALLOWED_GROUPS 为空 → **所有群消息都不会回**（填上群号才生效）")
    if not _ids("ALLOWED_PRIVATE"):
        warns.append("ALLOWED_PRIVATE 为空 → **所有私聊都不会回**（填上 QQ 号才生效）")
    if not _ids("OWNER_QQ") and not _ids("SUPERUSERS"):
        warns.append("OWNER_QQ / SUPERUSERS 都为空 → 没有主人，管理类工具一律不可用")

    try:
        ob = int(tuning.i("OUT_BUBBLE_CHARS"))
        if ob > _BUBBLE_MAX_SANE:
            warns.append(
                f"OUT_BUBBLE_CHARS={ob} 过大（正常 40）→ 长回复不会转合并转发，"
                f"会整段刷屏")
    except Exception:
        pass
    try:
        gh = int(tuning.i("GROUP_MAX_HANZI"))
        if gh > _HANZI_MAX_SANE:
            warns.append(f"GROUP_MAX_HANZI={gh} 过大（正常 20）→ 单条会很长")
    except Exception:
        pass
    try:
        lf = int(tuning.i("LONG_FORWARD_MIN"))
        if lf > _FORWARD_MIN_SANE:
            warns.append(f"LONG_FORWARD_MIN={lf} 过大（正常 120）→ 系统长文不转卡片")
    except Exception:
        pass

    mode = _env("GROUP_WATCH_MODE", "full").lower()
    if mode not in ("full", "lite"):
        warns.append(f"GROUP_WATCH_MODE={mode!r} 不认识（只支持 full / lite，按 full 处理）")

    backend = _env("MEMORY_BACKEND", "json").lower()
    if backend not in ("json", "sqlite"):
        warns.append(f"MEMORY_BACKEND={backend!r} 不认识（只支持 json / sqlite）")

    if _env("USE_LOCAL_TTS", "false").lower() in ("1", "true", "yes", "on") \
            and _env("NODE_COMPUTE", "1") in ("0", "false", "off"):
        warns.append("USE_LOCAL_TTS=1 但 NODE_COMPUTE=0 → 语音会先白等本地 TTS 再降级")

    # 输出 token（2026-11-27 用户真机）：GLM 当主聊天模型时，800 会被 thinking 吃光，
    # 正文为空；改 .env 以前根本不生效（代码写死 800）。现在读配置，但要在启动时提醒。
    try:
        from . import providers
        p = providers.resolve("roleplay")
        mt = int(p.get("max_tokens") or 0)
        cap = int(providers.MAX_OUTPUT_TOKENS.get(p.get("provider"), 0) or 0)
        _model = str(p.get("model") or "").lower()
        _reasoning = ("reason" in _model or "think" in _model or "flash" in _model)
        if ((str(p.get("provider")) == "glm"
             and _env("GLM_THINKING", "").lower() in ("1", "true", "yes", "on"))
                or _reasoning) and mt < 4000:
            warns.append(
                f"主聊天模型 {p.get('model')} 是推理型/开了 thinking，但输出上限只有 {mt} → "
                f"thinking 会吃掉正文（用户 2026-11-27 拍板 4000）；"
                f"设 ROLEPLAY_MAX_TOKENS=4000")
        if cap and mt >= cap:
            warns.append(
                f"ROLEPLAY_MAX_TOKENS={mt} 已到 {p.get('provider')} 上限 {cap}"
                f"（再大会被 clamp，不会报错）")
    except Exception:
        pass

    for w in warns:
        logger.warning(f"[ENV] ⚠️ {w}")
    if not warns:
        logger.info("[ENV] 启动配置体检通过")
    return warns


def report_text() -> str:
    """给 /自检 的一小段：回复长度 / 观察模式 / 记忆后端。"""
    def _i(k, d=0):
        try:
            return int(tuning.i(k))
        except Exception:
            return d
    return (
        f"· 回复长度：单泡 ≤{_i('OUT_BUBBLE_CHARS')} 字 / ≤{_i('OUT_BUBBLES_MAX')} 泡"
        f"｜群 {_i('GROUP_MAX_HANZI')} / 私聊 {_i('PRIVATE_MAX_HANZI')} 字转卡片"
        f"｜长文卡片阈值 {_i('LONG_FORWARD_MIN')} 字\n"
        f"· 观察模式 {_env('GROUP_WATCH_MODE', 'full')}"
        f"｜记忆后端 {_env('MEMORY_BACKEND', 'json')}"
        f"｜群聊冷却 {tuning.f('GROUP_REPLY_COOLDOWN'):.0f}s\n"
        f"· 白名单：群 {len(_ids('ALLOWED_GROUPS'))} 个 / 私聊 {len(_ids('ALLOWED_PRIVATE'))} 个"
    )
