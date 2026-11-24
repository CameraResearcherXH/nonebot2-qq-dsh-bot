# DeepSeek Harness（DSH）QQ 会话桥

> 代码：`services/harness.py`、`plugins/harness/__init__.py`
> 开关：`CODE_AGENT=harness`（同时自动关闭 `self_tool`）
> 相关：`docs/third-party-integration.md`、`docs/deployment.md`

## 1. 它解决什么

原来的「自己写工具」（`self_tool_write/run`）只能让 bot 写一些**小脚本**，
改不了仓库里的真实代码，也没法跑测试、看 diff。

Harness 桥把 QQ 会话接到本机的 **DeepSeek Harness**：
你在 QQ 里说「把 /画图 改成支持 4 张图」，DSH 真的去读仓库、改文件、跑测试，
结果原样回到 QQ。

```
QQ 私聊/群（主人）
   │  /h <需求>
   ▼
NoneBot（本项目）
   ├─ 组装上下文：人格 / 心情 / 好感 / 白月光 / 相关记忆
   ├─ services/harness.py
   │     ├─ backend=cli  → 跑本地 dsh 命令（推荐）
   │     └─ backend=http → POST 到 DSH 的 HTTP 接口
   ▼
DeepSeek Harness（本机，能读写仓库 / 跑测试）
   │  返回文本（diff / 结果 / 报错）
   ▼
QQ（原样分段发回）
```

## 2. 和 Aurabot / dsh-release 的关系

- **Aurabot（dsh-aurabot-qq）** 是一个完整的 DSH 插件：它自己接管 QQ（SnowLuma / OneBot），
  自带人格、记忆、语音、空间、B 站。它和本项目是**两种路线**，不建议同时接管同一个 QQ 号。
- **nonebot-plugin-dsh-release** 只是「DSH 发版公告推送到群」，不是会话桥。
- 本桥是**轻量版**：QQ 仍然由本项目（NoneBot）接管，只把「改代码」这一件事交给 DSH。
  人格/好感/白月光/记忆仍用本项目的，不重复造一套。

如果你已经用 Aurabot 跑 QQ，就不需要本桥；如果你用本项目跑聊天，但想要 DSH 的改代码能力，
用本桥。

## 3. 配置

`.env`：

```ini
# 关掉 self_tool，打开 harness
CODE_AGENT=harness

# 后端一：本地 CLI（推荐）
HARNESS_BACKEND=cli
HARNESS_CLI=dsh run --cwd {cwd} --session {session} --prompt {prompt}

# 后端二：HTTP（DSH 暴露了接口时用）
# HARNESS_BACKEND=http
# HARNESS_URL=http://127.0.0.1:8787/api/chat
# HARNESS_TOKEN=

# 工作目录（相对项目根；也可写绝对路径）
HARNESS_CWD=.
# 是否把人格/心情/好感/白月光/记忆带给 DSH
HARNESS_CONTEXT=1
# 只有主人能用（强烈建议 1）
HARNESS_OWNER_ONLY=1
```

`{prompt}` / `{cwd}` / `{session}` 会被替换；命令用 `shlex.split` 解析，**不经过 shell**。

> 不同 DSH 版本的 CLI 参数可能不一样。先用 `dsh --help` 看你们那版的
> 「非交互执行一条 prompt」怎么写，再把模板填进 `HARNESS_CLI`。

## 4. 用法

只有主人（`SUPERUSERS`）能用：

| 命令 | 作用 |
|---|---|
| `/h` 或 `/harness` | 看当前会话 / 后端 / 目录 / 超时 |
| `/h 新` | 开一个新会话（换任务、上下文乱了） |
| `/h <需求>` | 把需求交给 harness |
| `/改代码 <需求>` | 同上 |

她也会自己判断：当你说「改一下这个功能」「加个 XX」「这个 bug 修一下」时，
模型会调 `harness_ask` 工具（结果直接原样发出来，不让模型二次改写代码）。

## 5. 上下文带什么

`HARNESS_CONTEXT=1` 时，任务前面会附一小段（不超过 ~800 字）：

- 【角色】当前人格名
- 【当前心情】她现在的状态
- 【你对某人的好感】好感名词
- 【白月光】回这个人时借的句式（如果有）
- 【相关记忆】和「代码/需求/修改」相关的 3 条

这样 DSH 改代码时会知道「这是谁在提需求、她平时怎么说话」，
但**不会**把整份聊天记录塞过去（省 token，也避免隐私扩散）。

## 6. 安全

- 默认只有主人能触发（`HARNESS_OWNER_ONLY=1`）；
- 输出会 redact 掉 `.env` 里的密钥值；
- 超时（默认 300s）会 kill 子进程；
- 输出截断到 `HARNESS_MAX_OUTPUT`（默认 3000 字），分段发；
- 建议 `HARNESS_CWD` 指向一个**独立 clone**，不要让 DSH 直接改线上目录；
- 改完让 DSH 跑 `python scripts/verify_all.py` 再上线。

## 7. 和 self_tool 的互斥

`CODE_AGENT` 三档：

| 值 | self_tool | harness | 适用 |
|---|---|---|---|
| `self_tool`（默认） | ✅ | ❌ | 只要小脚本能力 |
| `harness` | ❌（插件不加载，工具不注册） | ✅ | 要真的改仓库代码 |
| `off` | ❌ | ❌ | 都不开 |

`bot.py` 在 `CODE_AGENT=harness` 时直接跳过 `plugins/self_tools`；
`services/llm.py::register_tool` 也会拒绝 `self_tool_*` / `mytool_*`，双保险。

## 8. 常见问题

**Q：DSH 没装 / 命令写错？**
先 `/h 自检`（看缺 CLI 还是缺 URL/token）；再 `/h 测试一下` 会真的跑一次，
返回「找不到命令 / 退出码 N / HTTP 401」，按提示改 `HARNESS_CLI` / `HARNESS_URL`。

**Q：能群聊里用吗？**
可以，但建议只在私聊。群聊里别人也能看到 diff，且 `scope` 默认只挂私聊工具。

**Q：会不会把 API Key 发出去？**
`ask()` 返回前会 redact `.env` 里的密钥值；但 DSH 自己打印的环境变量不受我们控制，
建议在 DSH 侧也做脱敏。

**Q：能双向吗（DSH 主动发 QQ）？**
本桥目前是 QQ → DSH → QQ 的请求/响应。要让 DSH 主动推送，需要 DSH 调本项目的
HTTP 接口（可以后续加一个 `/api/harness/push`）。

## 9. 桌面版（dsh-desktop）怎么接

> 2026-11-24 用户提问：「用户用的 harness 是桌面版，他们拿不到 token、也拿不到我们要的内容吧？」
> 结论：**对，大概率接不上**，桌面版和 CLI/HTTP 版不是一回事。

本桥只认两种后端，都需要 DSH 主动暴露一个「非交互入口」：

| 后端 | 需要 DSH 提供 | 桌面版通常有没有 |
|---|---|---|
| `cli` | PATH 里能跑 `dsh run --cwd … --prompt …` | ❌ 一般不装 CLI，只装 GUI |
| `http` | 一个 HTTP 接口 + Bearer token | ❌ 登录 token 存在它自己配置里，不导出；接口多半也没开 |

所以桌面版用户直接填 `HARNESS_BACKEND=cli` 会得到「找不到命令 `dsh`」，
填 `http` 会卡在「没有 URL / token」。

**先自检**（新版）：

```text
/h 自检
```

会逐项告诉你：CLI 命令找不找得到、`--help` 能不能跑、HTTP 地址通不通、token 配没配。

### 桌面版用户的三条路（按推荐顺序）

1. **不用 harness，用 self_tool（默认）**
   `.env` 保持 `CODE_AGENT=self_tool`。她能写小脚本、查文件，只是不能改仓库代码。
   对普通用户够用，也不会因为桌面版接不上而报错。

2. **MCP 路线（推荐给「想让它改代码」的桌面版用户）**
   让桌面版 DSH 直接打开这个仓库目录，靠 `AGENTS.md` + MCP 工具去读代码/跑命令。
   QQ 侧不需要 harness 桥 —— 相当于「DSH 自己在电脑上改，QQ 只负责聊天」。
   这也是 `docs/harness_stack_selection.md` 里 L1/L2 的方案（不自研 IDE，直接用 dsh-desktop）。

3. **桌面版开了「本地 API / 命令行」再用本桥**
   如果你们那版桌面版设置里能打开：
   - 本地 HTTP API（给出端口和 token）→ `HARNESS_BACKEND=http` + `HARNESS_URL` + `HARNESS_TOKEN`；
   - 或者安装 CLI 组件 → `HARNESS_BACKEND=cli` + `HARNESS_CLI`。
   两个都没有，就只能走第 1/2 条。

### 关于 token 和「我们要的内容」

- **token**：本桥不生产 token，只是把 DSH 给的 token 放进 `Authorization: Bearer`。
  桌面版不导出 token，就没有可填的东西 —— 这不是配置写错。
- **内容**：本桥只把「人格/心情/好感/白月光/3 条记忆 + 需求」这 ≤800 字带过去（`HARNESS_CONTEXT=1`），
  不传聊天记录。桌面版如果只能人工复制粘贴，那这些上下文得用户自己贴。
