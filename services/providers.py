# services/providers.py
"""模型 provider 注册表（2026-11-24 用户要求）。

用户原话：
  「有的用户直接用 deepseek v4 flash 做视觉，有些想换 glm 做主对话模型。
    我们 env 可以配各种 key 接口，用户可以自由选择搭配自己想要用来识图和对话的模型，
    包括两个 chat 检测，他们也可以选择模型。」

所以：**每个角色单独选 provider + 模型**，代码里不再写死某一家。

角色（role）：
    roleplay    主聊天 / 工具循环
    judge       心情判读
    audit       收发件人审核（scene_auditor）
    vision      识图
    expression  说话风格学习
    conv_state  对话状态
    profile     档案挖掘
    referee     三方差分 / 参考

.env 写法（每个角色都一样）：
    <ROLE>_PROVIDER=deepseek|glm|qwen|xai|openai|custom|<自定义名>
    <ROLE>_MODEL=deepseek-flash
    <ROLE>_BASE_URL=https://api.deepseek.com
    <ROLE>_API_KEY=sk-xxx

不写就用该 provider 的内置默认值；`<ROLE>_BASE_URL/_API_KEY/_MODEL` 永远优先。

内置 provider 的 key 会按顺序找（兼容历史变量）：
    deepseek → DEEPSEEK_API_KEY, XAI_API_KEY
    glm      → GLM_API_KEY
    qwen     → DASHSCOPE_API_KEY
    xai      → XAI_API_KEY
    openai   → OPENAI_API_KEY
    custom   → LLM_API_KEY

想加自己的中转站：
    PROVIDER_MYPROXY_BASE_URL=https://xxx/v1
    PROVIDER_MYPROXY_API_KEY=sk-xxx
    PROVIDER_MYPROXY_MODEL=deepseek-chat
    ROLEPLAY_PROVIDER=myproxy
"""
from __future__ import annotations

import os

from core import tuning

# 角色 → (provider 环境变量前缀, 默认 provider)
ROLES = {
    "roleplay": ("ROLEPLAY", "deepseek"),
    "judge": ("JUDGE", "deepseek"),
    "audit": ("AUDIT", "deepseek"),
    # 2026-11-27 用户拍板：「GLM 额度用完了，之后尽量靠 qwen 视觉，我们先用 qwen」。
    # 所以主视觉默认 qwen；GLM 变成「可选」——想用就写 VISION_PROVIDER=glm。
    "vision": ("VISION", "qwen"),
    # 第二视觉证据源（可选）：**默认关闭**（不配 VISION_SECOND_PROVIDER 就不调）。
    # 想要两家对拍就配 VISION_SECOND_PROVIDER=glm|openai|xxx + 对应 key/model。
    "vision_second": ("VISION_SECOND", ""),
    # 视觉备用 provider：主 provider 额度用尽/限流时自动换这家（默认 glm）。
    # 只配 `VISION_FALLBACK_PROVIDER/_MODEL/_BASE_URL/_API_KEY`，不影响主视觉。
    "vision_fallback": ("VISION_FALLBACK", "glm"),
    "expression": ("EXPRESSION", "deepseek"),
    "conv_state": ("CONV_STATE", "deepseek"),
    "profile": ("PROFILE", "deepseek"),
    "referee": ("REFEREE", "deepseek"),
}

# 内置 provider：base_url 默认值 + key 变量候选 + 默认模型
BUILTIN = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "key_envs": ("DEEPSEEK_API_KEY", "XAI_API_KEY"),
        "model": "deepseek-chat",
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "key_envs": ("GLM_API_KEY",),
        "model": "glm-4v-plus",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_envs": ("DASHSCOPE_API_KEY",),
        "model": "qwen3-vl-plus",
    },
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "key_envs": ("XAI_API_KEY",),
        "model": "grok-3-mini",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "key_envs": ("OPENAI_API_KEY",),
        "model": "gpt-4o-mini",
    },
    "custom": {
        "base_url": "https://api.deepseek.com",
        "key_envs": ("LLM_API_KEY",),
        "model": "deepseek-chat",
    },
}

# 各 provider 单次输出上限（超过会被 API 拒绝 / 截断）。
# 2026-11-27 用户反馈：用 GLM 当主聊天模型时输出被卡在 800，改 .env 也不生效 ——
# 根因是 llm.py 主循环写死 `max_tokens: 800`，根本没读任何配置。
MAX_OUTPUT_TOKENS = {
    "deepseek": 8192,
    "glm": 4096,
    "qwen": 8192,
    "xai": 8192,
    "openai": 16384,
    "custom": 8192,
}

# 新式 OpenAI 推理模型只认 `max_completion_tokens`，传 `max_tokens` 会 400。
_COMPLETION_FIELD_MODELS = ("o1", "o3", "o4", "gpt-5")

_CACHE: dict = {}


def _env(key: str, default: str = "") -> str:
    return str(os.getenv(key, default) or "").strip()


def _custom_spec(name: str) -> dict:
    """PROVIDER_<NAME>_BASE_URL / _API_KEY / _MODEL。"""
    up = name.upper().replace("-", "_")
    return {
        "base_url": _env(f"PROVIDER_{up}_BASE_URL", ""),
        "key_envs": (f"PROVIDER_{up}_API_KEY",),
        "model": _env(f"PROVIDER_{up}_MODEL", ""),
    }


def spec(name: str) -> dict:
    n = str(name or "").strip().lower()
    if n in BUILTIN:
        return BUILTIN[n]
    return _custom_spec(n) if n else BUILTIN["custom"]


def provider_of(role: str) -> str:
    role = str(role or "").strip().lower()
    prefix, default = ROLES.get(role, (role.upper(), "deepseek"))
    explicit = _env(f"{prefix}_PROVIDER")
    if explicit:
        return explicit.lower()
    if not default:
        # 默认值为空 = 该角色默认关闭（如 vision_second），
        # 不能拿 LLM_PROVIDER 兜底，否则用户配了主模型就被动多调一家视觉。
        return ""
    return (_env("LLM_PROVIDER") or default).lower()


def output_limit(role: str, default: int = 0) -> int:
    """这个角色一次最多能输出多少 token（2026-11-27）。

    优先级（高 → 低）：
      1) `<ROLE>_MAX_TOKENS`，如 `ROLEPLAY_MAX_TOKENS=4096`；
      2) `LLM_MAX_TOKENS`（全局覆盖，>0 才生效）；
      3) provider=glm 时兼容旧的 `GLM_MAX_TOKENS`（用户以前只写这个）；
      4) 角色默认（roleplay=`ROLEPLAY_MAX_TOKENS`，其它=`LLM_DEFAULT_MAX_TOKENS`）。
    最后按 provider 上限 clamp（GLM 4096 / DeepSeek 8192 / OpenAI 16384…）。
    """
    role = str(role or "").strip().lower()
    prefix, _ = ROLES.get(role, (role.upper(), "deepseek"))
    name = provider_of(role)

    val = _env(f"{prefix}_MAX_TOKENS")
    if not val:
        _g = int(tuning.i("LLM_MAX_TOKENS") or 0)
        if _g > 0:
            val = str(_g)
    if not val and name == "glm":
        # 兼容历史：GLM 用户以前只在 .env 写 GLM_MAX_TOKENS
        val = _env("GLM_MAX_TOKENS")
    try:
        n = int(float(val))
    except Exception:
        if default:
            n = int(default)
        else:
            n = int(tuning.i("ROLEPLAY_MAX_TOKENS") if role == "roleplay"
                    else tuning.i("LLM_DEFAULT_MAX_TOKENS"))
    cap = MAX_OUTPUT_TOKENS.get(name)
    if cap:
        n = min(n, cap)
    return max(64, n)


def call_tokens(role: str, wanted: int) -> dict:
    """单次辅助调用：返回 `{字段名: 实际 max_tokens}`，可直接 `**` 进 payload。

    `wanted` 是模块按任务定的预算（判读 90、审核 900…）。如果这个角色被切到
    GLM 且开了 thinking，thinking 会先吃 token → 用 `GLM_MAX_TOKENS` 兜底，
    避免返回空 JSON。其它 provider 原样返回。
    """
    p = resolve(role)
    field = str(p.get("max_tokens_field") or "max_tokens")
    val = int(wanted)
    if str(p.get("provider")) == "glm" and str(
            os.getenv("GLM_THINKING", "")).lower() in ("1", "true", "yes", "on"):
        val = max(val, int(tuning.i("GLM_MAX_TOKENS")))
    return {field: max(32, val)}


def token_field(name: str, model: str) -> str:
    """输出 token 的字段名：OpenAI 推理模型用 max_completion_tokens。"""
    m = str(model or "").lower()
    if str(name or "").lower() == "openai" and any(
            m.startswith(p) for p in _COMPLETION_FIELD_MODELS):
        return "max_completion_tokens"
    return "max_tokens"


def resolve(role: str) -> dict:
    """返回 {provider, base_url, api_key, model}，带进程内缓存。

    ⚠️ 缓存只在「第一次调用」时算；跑测试要 `reset()` 清缓存。
    """
    role = str(role or "").strip().lower()
    if role in _CACHE:
        return dict(_CACHE[role])

    prefix, _default = ROLES.get(role, (role.upper(), "deepseek"))
    name = provider_of(role)
    if not name:
        # 未配置的角色（如 vision_second）：返回空壳，调用方据此跳过，
        # 绝不 fallback 到 LLM_API_KEY —— 那会拿主模型的 key 去请求一个没配的端点。
        out = {"provider": "", "base_url": "", "api_key": "", "model": "",
               "max_tokens": 0, "max_tokens_field": "max_tokens"}
        _CACHE[role] = out
        return dict(out)
    sp = spec(name)

    base = (_env(f"{prefix}_BASE_URL")
            or (sp.get("base_url") or "").rstrip("/")
            or _env("LLM_BASE_URL")
            or BUILTIN["custom"]["base_url"])
    key = _env(f"{prefix}_API_KEY")
    if not key:
        for k in sp.get("key_envs") or ():
            if _env(k):
                key = _env(k)
                break
    if not key:
        key = _env("LLM_API_KEY")
    model = _env(f"{prefix}_MODEL") or sp.get("model") or "deepseek-chat"

    _def = (tuning.i("ROLEPLAY_MAX_TOKENS") if role == "roleplay"
            else tuning.i("LLM_DEFAULT_MAX_TOKENS"))
    out = {
        "provider": name,
        "base_url": base.rstrip("/"),
        "api_key": key,
        "model": model,
        # 2026-11-27：输出 token 上限也进注册表，代码里不许再写死 800。
        "max_tokens": output_limit(role, _def),
        "max_tokens_field": token_field(name, model),
    }
    _CACHE[role] = out
    return dict(out)


def chat_url(role: str) -> str:
    """OpenAI 兼容的 /chat/completions 完整地址。"""
    base = resolve(role)["base_url"].rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def reset() -> None:
    _CACHE.clear()


def status() -> str:
    """给 `/测函数 模型` 用：每个角色当前落到哪家、哪个模型、有没有 key。"""
    lines = ["▍模型分配"]
    for role in ROLES:
        p = resolve(role)
        if not p["provider"]:
            continue          # 未启用的可选角色（vision_second）不占版面
        lines.append(
            f"· {role:11s} {p['provider']:9s} {p['model']:22s} "
            f"{'key✓' if p['api_key'] else 'key✗'} {p['base_url']}")
    return "\n".join(lines)
