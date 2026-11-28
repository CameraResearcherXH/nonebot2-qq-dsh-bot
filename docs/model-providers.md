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

## 6. 思考等级（2026-11-28）

用户原话：「我们的心理活动并非完全的深度思考，用户如果用哪些强制深度思考的模型就会超时。
建议对这类模型聊天默认 low，用户也可以自己在 `.env` 里改这个深度思考等级。
low 是为时间不超时考虑的。」

`REASONING_EFFORT`（默认 `low`）只对**推理型模型**注入，普通 chat 模型不受影响：

| 取值 | OpenAI / DeepSeek 系 | GLM-5.x（强制思考） | GLM-4.x | Qwen3 |
|---|---|---|---|---|
| `low`（默认） | `reasoning_effort: low` | `thinking:{type:enabled}` + `reasoning_effort: low` | `thinking:{type:disabled}` | `enable_thinking: false` |
| `medium` | `medium` | `reasoning_effort: high` | `thinking:{type:enabled}` | `enable_thinking: true` |
| `high` | `high` | `reasoning_effort: max` | `thinking:{type:enabled}` | `enable_thinking: true` |
| `off` | 最低档 | `reasoning_effort: low`（关不掉） | 关 | 关 |
| `auto` | 不注入（老行为） | 不注入 | 不注入 | 不注入 |

> **GLM-5.3-Flash 是强制思考模型**：`thinking.type` 只支持 `enabled`，**不能关闭**；
> 但它有 `reasoning_effort: low|high|max`（默认 `max`，实测 900 token 要 ~23s）。
> 所以对 GLM-5.x 不能传 `disabled`（会被忽略/报错），正确做法是
> `thinking:{type:enabled}` + `reasoning_effort:low`。
> 参考1 通道（`services/glm.py`）也走同一套，可用 `GLM_REASONING_EFFORT` 单独调。

判定「推理型模型」看模型名是否含 `reasoner / r1 / qwq / o1 / o3 / o4 / thinking / flash / deepseek-v4`
（当前主模型 `deepseek-flash` 就属于这一类，日志会打
`[LLM] ☁️ 使用角色扮演模型 deepseek-flash 生成最终回复（思考=low，推理模型已注入）`）。

> Qwen2.5 不认 `enable_thinking`（传了会 400），所以只对 `qwen3*` 注入。
> GLM-4.x 没有档位、但能关，所以直接关；GLM-5.x 关不掉，只能降到 `low`。

## 7. 每日 token 额度与 `/tokenadd`（2026-11-28）

- 基础额度：`.env` 的 `LLM_DAILY_BUDGET`（默认 2000000）；
- 主人可以在群里发 `/tokenadd 1000000` 给**今天**追加额度（`/tokenadd -500000` 减回去）；
- 追加值落 `data/budget_extra.json`（`{"day": "...", "extra": N}`），跨重启有效、次日自动失效；
- `/用量` 或 `/测函数 用量` 会显示「其中追加 N」。
