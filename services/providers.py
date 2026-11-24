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

# 角色 → (provider 环境变量前缀, 默认 provider)
ROLES = {
    "roleplay": ("ROLEPLAY", "deepseek"),
    "judge": ("JUDGE", "deepseek"),
    "audit": ("AUDIT", "deepseek"),
    "vision": ("VISION", "glm"),
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
    return (_env(f"{prefix}_PROVIDER") or _env("LLM_PROVIDER") or default).lower()


def resolve(role: str) -> dict:
    """返回 {provider, base_url, api_key, model}，带进程内缓存。

    ⚠️ 缓存只在「第一次调用」时算；跑测试要 `reset()` 清缓存。
    """
    role = str(role or "").strip().lower()
    if role in _CACHE:
        return dict(_CACHE[role])

    prefix, _default = ROLES.get(role, (role.upper(), "deepseek"))
    name = provider_of(role)
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

    out = {
        "provider": name,
        "base_url": base.rstrip("/"),
        "api_key": key,
        "model": model,
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
        lines.append(
            f"· {role:11s} {p['provider']:9s} {p['model']:22s} "
            f"{'key✓' if p['api_key'] else 'key✗'} {p['base_url']}")
    return "\n".join(lines)
