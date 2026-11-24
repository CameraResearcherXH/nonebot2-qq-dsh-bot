# 模型自由搭配（provider 注册表）

> 代码：`services/providers.py`
> 查看当前分配：`/测函数 模型`

2026-11-24 起，**每个角色都能单独选 provider + 模型**，不用改代码：

| 角色 | 干什么 | 默认 provider | 默认模型 |
|---|---|---|---|
| `ROLEPLAY` | 主聊天 / 工具循环 | `deepseek` | `deepseek-chat`（你可以在 `.env` 里写 `deepseek-flash`） |
| `JUDGE` | 心情判读 | `deepseek` | `deepseek-chat` |
| `AUDIT` | 收发件人审核（scene_auditor） | `deepseek` | `deepseek-chat` |
| `VISION` | 识图 | `glm` | `glm-4v-plus` |
| `EXPRESSION` | 说话风格学习 | `deepseek` | `deepseek-chat` |
| `CONV_STATE` | 对话状态 | `deepseek` | `deepseek-chat` |
| `PROFILE` | 档案挖掘 | `deepseek` | `deepseek-chat` |
| `REFEREE` | 三方差分 / 参考 | `deepseek` | `deepseek-chat` |

## 1. 写法

每个角色四个键，写几个算几个，没写的用默认：

```ini
<ROLE>_PROVIDER=deepseek|glm|qwen|xai|openai|custom|<自定义名>
<ROLE>_MODEL=模型名
<ROLE>_BASE_URL=https://xxx/v1
<ROLE>_API_KEY=sk-xxx
```

`<ROLE>_BASE_URL / _API_KEY / _MODEL` **永远优先**于 provider 的默认值。

## 2. 常见搭配

```ini
# ① 主聊天用 DeepSeek V4.1 Flash（推荐）
ROLEPLAY_PROVIDER=deepseek
ROLEPLAY_MODEL=deepseek-flash

# ② 主聊天换 GLM
ROLEPLAY_PROVIDER=glm
ROLEPLAY_MODEL=glm-5.3-flash

# ③ 识图用 DeepSeek V4 Flash（有些用户就是这么配的）
VISION_PROVIDER=deepseek
VISION_MODEL=deepseek-flash

# ④ 识图用 GLM 4V（默认；4s 出结果，能读截图小字）
VISION_PROVIDER=glm
VISION_MODEL=glm-4v-plus

# ⑤ 两个 chat 检测分开用不同模型
JUDGE_PROVIDER=deepseek
JUDGE_MODEL=deepseek-chat
AUDIT_PROVIDER=qwen
AUDIT_MODEL=qwen3-vl-plus
```

## 3. 用自己的中转站

```ini
PROVIDER_MY_BASE_URL=https://your-proxy/v1
PROVIDER_MY_API_KEY=sk-xxx
PROVIDER_MY_MODEL=deepseek-chat

ROLEPLAY_PROVIDER=my
```

`my` 只是名字，随便起（大小写不敏感，`-` 会转成 `_`）。

## 4. key 从哪来

provider 内置的 key 会按顺序找，兼容历史变量：

| provider | 找这些变量 |
|---|---|
| `deepseek` | `DEEPSEEK_API_KEY` → `XAI_API_KEY` |
| `glm` | `GLM_API_KEY` |
| `qwen` | `DASHSCOPE_API_KEY` |
| `xai` | `XAI_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `custom` | `LLM_API_KEY` |
| 自定义名 | `PROVIDER_<NAME>_API_KEY` |

## 5. 注意

- 所有 provider 都走 **OpenAI 兼容**的 `/chat/completions`（DeepSeek / GLM / 通义 / xAI / OpenAI / 大多数中转站都是）。
- 视觉模型必须真的收图：纯文本模型（如 `glm-5.3`、`glm-4v-flash` 在本账号）会返回 `1210`，
  代码会自动换下一个候选模型；`/测函数 模型` 能看到当前落到哪个。
- `glm-5.3-flash` 能读图，但「始终思考」不支持关闭，900 token 要 ~23s；日常识图用 `glm-4v-plus`。
- 改完 `.env` 要 `docker compose up -d --force-recreate bot`（`restart` 不重读 env_file）。
