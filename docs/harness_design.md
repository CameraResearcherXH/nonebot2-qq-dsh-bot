# Harness 框架设计文档

> 版本：v1.1（2026-11-12）
> 状态：**核心假设已修正** —— 原 v1.0 假设「自建 agent loop」，但 DeepSeek 官方已开源 Harness（dsh，MIT，"一切皆插件"，连 Agent Loop 和沙箱都是插件）。
> 选型与成本结论见 **`harness_stack_selection.md`**，本文作废范围与保留范围如下：

| 本文章节 | 状态 | 说明 |
|---|---|---|
| §3 后端契约 / §6 安全 / §7 会话 / §8.1 挂载坑 / §8.5 事件节流 | **仍然有效** | 这些是 dsh 给不了的部分，继续按本文实现 |
| §4 Agent Loop / §5 工具清单 / §10 分期 | **作废** | 改由 dsh 的 Agent Loop 插件与内置工具承担 |
| §10 分期 | **被替换** | 见 `harness_stack_selection.md` §8 的期 0~期 6 |

> 目标读者：框架实现者、部署者、后续接入方

---

## 1. 定位

### 1.1 一句话

一套**与执行位置解耦的 Agent 运行时**：同一个 agent loop、同一套工具定义，可以在本地进程、Docker 容器、远程算力机三种后端上跑，通过 QQ / HTTP / CLI 任意前端接入。

### 1.2 为什么要解耦

用户诉求原文：

> 文件读取解析编译、log 等工具，可以在云算力机，也可以在本地，也可以在 docker 内。

这句话翻译成技术约束就是：**「工具能做什么」和「工具在哪做」必须分开**。

如果按常规写法（工具里直接 `open()` / `subprocess`），那么「跑在算力机」就要重写一遍工具，「跑在容器」再写一遍，三份实现三份 bug，而且 prompt 里描述的工具语义会跟着漂移。

所以本设计的第一原则：

> **所有工具只依赖后端契约的 6 个原语，不直接触碰文件系统和进程。**

### 1.3 非目标

明确不做的事，避免范围蔓延：

- 不做通用 CI/CD（只管一次 agent 会话的生命周期）
- 不做多租户计费（先做单租户，契约里预留 tenant 字段）
- 不做模型训练/微调
- 不替代 Git（只调 git，不实现版本控制）

---

## 2. 总体架构

### 2.1 四层

```
┌─────────────────────────────────────────────────────────┐
│ L1 接入层  Frontend                                     │
│    QQ(NoneBot) / HTTP API / CLI                         │
│    职责：把外部请求变成 Turn，把事件变成人类可读输出      │
└────────────────────────┬────────────────────────────────┘
                         │ Turn / Event
┌────────────────────────▼────────────────────────────────┐
│ L2 编排层  Orchestrator                                 │
│    AgentLoop（状态机） / Session（会话） / Approval（审批）│
│    Budget（预算） / EventBus（事件总线）                  │
│    职责：决定「下一步做什么」，不关心「在哪做」           │
└────────────────────────┬────────────────────────────────┘
                         │ ToolCall / ToolResult
┌────────────────────────▼────────────────────────────────┐
│ L3 工具层  Tools                                        │
│    只读组 / 写入组 / 执行组 / 控制组                     │
│    职责：把意图翻译成后端原语调用，做参数校验与结果整形    │
└────────────────────────┬────────────────────────────────┘
                         │ Backend Primitive（6 个）
┌────────────────────────▼────────────────────────────────┐
│ L4 执行后端层  Backends                                 │
│    LocalBackend / DockerBackend / RemoteBackend         │
│    职责：真正碰文件系统和进程，负责隔离与限额             │
└─────────────────────────────────────────────────────────┘
```

### 2.2 为什么是四层而不是三层

关键在 L3 和 L4 的切分。

如果把工具和后端合成一层，那么「路径越权检查」这种安全逻辑会散落在 15 个工具里，忘一个就是洞。

切成两层后：

- **L4 独占**：路径 jail、资源限额、超时、输出截断、进程隔离 —— 安全边界只有一处
- **L3 独占**：参数语义校验、结果整形（给模型看的形式）、审批级别标注

一条铁律：

> **任何工具都不允许自己拼路径、自己起进程。** L3 拿到的 `path` 是相对路径，交给 L4 之前不做任何文件系统操作。

---

## 3. 核心契约

### 3.1 后端契约 Backend Contract

这是整个设计的支点。任何执行位置，只要能实现这 6 个方法，就能被框架使用。

```python
from dataclasses import dataclass
from typing import Protocol

@dataclass
class ExecResult:
    exit_code: int
    stdout: str            # 已按 output_max 截断
    stderr: str
    duration_ms: int
    truncated: bool        # stdout/stderr 是否被截断（截断时另有 full_log_path）
    full_log_path: str     # 截断时的完整输出落盘路径，供 read_log 工具读

@dataclass
class FileStat:
    exists: bool
    is_dir: bool
    size: int
    mtime: float

class Backend(Protocol):
    # --- 文件面 ---
    async def put(self, rel: str, data: bytes) -> None: ...
    async def get(self, rel: str) -> bytes: ...
    async def list(self, rel: str, depth: int = 1) -> list[dict]: ...
    async def stat(self, rel: str) -> FileStat: ...

    # --- 进程面 ---
    async def exec(
        self,
        argv: list[str],            # 必须是数组，永不过 shell
        cwd: str = ".",
        timeout: int = 120,
        env: dict | None = None,
        stdin: bytes | None = None,
    ) -> ExecResult: ...

    # --- 生命周期 ---
    async def close(self) -> None: ...
```

设计要点：

| 决策 | 理由 |
|---|---|
| `argv: list[str]` 而非 `cmd: str` | 从根上消除命令注入。工具层不允许拼字符串命令。 |
| `rel` 全部是相对路径 | 路径 jail 在 L4 统一做 `realpath` 校验，L3 永远拿不到绝对路径。 |
| `exec` 返回 `full_log_path` | 大输出（编译报错几千行）不能全塞进模型上下文，落盘 + `read_log` 按需读。 |
| `close()` 而非 `destroy()` | Docker 后端可能是一次性容器，也可能复用；语义交给实现。 |

### 3.2 工具契约 Tool Contract

工具用 OpenAI function-calling 的 schema 描述，附两个元字段：

```python
@dataclass
class Tool:
    name: str
    spec: dict                  # {"type":"function","function":{...}} 标准 schema
    level: str                  # "read" | "write" | "danger"
    needs_backend: bool         # 纯逻辑工具（finish/ask_user）为 False
    handler: Callable           # async (args: dict, ctx: ToolCtx) -> str
```

`level` 决定审批行为（见 §6.3），`needs_backend` 决定是否要注入 Backend 实例。

**为什么 `level` 标在工具上而不是靠运行时猜**：审批必须在工具执行**之前**发生。事后检测到危险已经晚了。

### 3.3 三种后端实现

| 实现 | 怎么跑 | 适用 | 隔离强度 |
|---|---|---|---|
| `LocalBackend` | 直接 `subprocess`，jail 到 workspace 根 | 本地开发、自用 | 弱（同进程用户权限） |
| `DockerBackend` | `docker run --rm` 一次性容器，`-v workspace:/ws`，或 `docker exec` 进常驻容器 | 对外提供服务、多租户 | 强（namespace + cgroup） |
| `RemoteBackend` | 复用现有 `node.py` pull 队列，把 `exec` 包成 task | 云算力机（GPU 场景） | 强（物理隔离） |

**RemoteBackend 的复用**：现有 `plugins/tool_dispatcher` 已经有完整的任务总线：

```
STATUS["tasks"][task_id] = {"type":..., "status":..., "input":..., "output":...}
GET  /api/tasks/pull?secret=       # 算力机拉任务
POST /api/tasks/complete           # 算力机回传（支持文件 + JSON）
GET  /api/download/{task_id}       # 算力机下载输入文件
```

RemoteBackend 只需新增一种 task type `harness_exec`，payload 是 `{argv, cwd, timeout, stdin}`，回传 `{exit_code, stdout, stderr, duration_ms}` + 变更文件包。**不需要新开端口、不需要反向隧道**（pull 模型天然穿透 NAT）。

代价是延迟：一轮 `exec` 往返约 1~3 秒。所以 RemoteBackend 适合"编译/跑测试/GPU 任务"，不适合高频小 IO。设计上允许**混合后端**：

```python
class HybridBackend(Backend):
    """文件面走本地（快），进程面按命令类型路由到远端（重）。"""
```

这个留到 v1.2，v1 先做单选。

---

## 4. Agent Loop

### 4.1 状态机

```
        ┌──────────────────────────────────────┐
        │                                      │
  IDLE ─┴─▶ PLANNING ──▶ ACTING ──▶ OBSERVING ─┘
                │            │            │
                │            │            └─▶ 预算耗尽 ─▶ DONE(budget)
                │            └─▶ 需要审批 ─▶ WAITING_APPROVAL
                │                                  │
                │                             批准 ─┘ / 拒绝 ─▶ ABORTED
                └─▶ 模型调 finish ─▶ DONE(ok)
                    任一阶段异常 ─▶ FAILED
```

### 4.2 一轮循环的精确语义

```python
async def step(self):
    # 1) 组装上下文：system + 历史 turns + 工具 spec
    msgs = self.build_messages()

    # 2) 问模型（function calling）
    resp = await self.llm.chat(msgs, tools=self.tools.specs())

    # 3) 没有 tool_call → 视为自然语言回复，记入 turns，本轮结束
    if not resp.tool_calls:
        self.turns.append(assistant(resp.content))
        return StepResult(kind="text", text=resp.content)

    # 4) 有 tool_call → 逐个执行（支持并行只读）
    results = await self.execute(resp.tool_calls)

    # 5) 回填 tool 消息，进入下一轮
    self.turns.extend(results)
    return StepResult(kind="tools", results=results)
```

### 4.3 预算与终止

| 预算 | 默认 | 触发后 |
|---|---|---|
| `max_steps` | 24 | 强制收敛：注入一条 system 提示「步数用尽，请立即 finish 并总结已完成的部分」，再给 1 轮 |
| `wall_clock` | 900s | 立即中断，返回已完成的 diff + 未完成说明 |
| `token_budget` | 120k | 同上 |
| `same_tool_fail_streak` | 3 | 同一工具连续 3 次失败 → 停止重试，转为 `ask_user` |

**为什么要有"强制收敛轮"**：直接截断会让用户拿到一个半成品且没有解释。给模型一次机会自己总结，体验完全不同。

### 4.4 只读工具并行

同一轮里如果多个 `tool_call` 的 `level == "read"`，并行执行（`asyncio.gather`）。写和执行类必须串行——顺序语义对文件修改是必须的。

---

## 5. 工具清单

分四组。`level` 列即审批级别。

### 5.1 只读组（level = read）

| 工具 | 参数 | 说明 |
|---|---|---|
| `read_file` | `path, start?, end?` | 按行读，默认全文但受 `output_max` 截断 |
| `list_dir` | `path, depth=1` | 返回 `[{name, type, size}]` |
| `grep` | `pattern, path=".", glob?, max_hits=50` | 基于 `rg`，无 rg 时降级 Python `re` |
| `read_log` | `path, tail=200, level?, since?` | 专读日志：支持按级别过滤、按时间戳过滤 |
| `parse_file` | `path, kind?` | 结构化解析：json/csv/xlsx/docx/pdf/png(OCR)/二进制头 |
| `git_status` | — | `git status --porcelain` + 分支 + ahead/behind |
| `git_diff` | `path?, staged=False` | 输出 unified diff |
| `git_log` | `n=10` | 最近提交 |

`parse_file` 的设计意图：**避免把二进制/大文件塞进上下文**。比如 xlsx 只回 `{"sheets":[{name, rows, cols, header}], "sample": 前5行}`，pdf 只回 `{"pages":N, "text_len":M, "outline":[...]}`。模型先看结构，再决定读哪段。

### 5.2 写入组（level = write）

| 工具 | 参数 | 说明 |
|---|---|---|
| `write_file` | `path, content` | 整文件覆盖。仅在新建时用。 |
| `edit_file` | `path, old_string, new_string, replace_all=False` | 精确替换。**改已有文件必须用这个**，防止整文件重写丢失内容。 |
| `apply_patch` | `patch` | unified diff 格式批量应用 |
| `git_commit` | `message, paths?` | 只在 harness 分支上提交 |

**为什么强制 `edit_file` 优先**：整文件 `write_file` 在长文件上极易截断/丢行，而且模型会"顺手"改掉不相关的部分。`edit_file` 要求 `old_string` 在文件中唯一，天然形成一道校验。

### 5.3 执行组（level = danger）

| 工具 | 参数 | 说明 |
|---|---|---|
| `run_shell` | `argv[], cwd?, timeout?` | 通用命令。**必须是数组**。 |
| `compile` | `target?, extra?` | 按项目类型自动选命令（见下） |
| `run_test` | `cmd?` | 同上，跑测试 |
| `install_dep` | `manager, packages[]` | pip/npm/apt |

`compile` 的自动识别（按 workspace 根特征文件）：

| 特征文件 | 命令 |
|---|---|
| `pyproject.toml` / `*.py` | `python -m compileall -q .`（**语法闸**，见 §8.1） |
| `package.json` + `tsconfig.json` | `npx tsc --noEmit` |
| `Cargo.toml` | `cargo check` |
| `CMakeLists.txt` | `cmake -B build && cmake --build build` |
| `go.mod` | `go build ./...` |

### 5.4 控制组（needs_backend = False）

| 工具 | 参数 | 说明 |
|---|---|---|
| `finish` | `summary, changed_files[]` | 正常结束。**必填 summary**，用于 QQ 回推。 |
| `ask_user` | `question, options?` | 中途提问，loop 挂起等用户回复 |
| `plan` | `steps[]` | 显式写计划到 turns（不产生副作用，纯为了让后续推理更稳） |

---

## 6. 安全设计

### 6.1 路径 jail（L4 强制）

```python
def resolve(self, rel: str) -> Path:
    if os.path.isabs(rel):
        raise JailError("绝对路径不允许")
    p = (self.root / rel).resolve()          # resolve 会解开 symlink
    if not str(p).startswith(str(self.root_resolved) + os.sep):
        raise JailError(f"越界: {rel}")
    return p
```

三个必须堵的口子：

1. `..` 穿越 → `resolve()` 后前缀检查
2. 符号链接逃逸 → `resolve()` 解链后再检查（**先 resolve 再比前缀**，顺序不能反）
3. 大小写/短名（Windows）→ 比较时统一 `casefold`

### 6.2 命令分级

不做纯白名单（太死，agent 会卡住），做**黑名单 + 高危参数检测**：

```python
DENY = [
    (r"^rm$",           {"-rf", "-fr", "--recursive"}),
    (r"^git$",          {"push", "reset --hard", "clean -fd"}),
    (r"^docker$",       {"*"}),          # 整个 docker 命令禁掉
    (r"^systemctl$",    {"*"}),
    (r"^kill$",         {"-9"}),
    (r"^chmod$",        {"777"}),
    (r"^curl$",         {"|"}),          # curl | sh
]
PROTECTED_PATHS = [".env", ".ssh/", "docker-compose.yml", ".git/config"]
```

命中即 `level = danger`，进审批闸。

### 6.3 三级审批

| level | 行为 | 通知 |
|---|---|---|
| `read` | 直接执行 | 静默（计入事件流，不单独推 QQ） |
| `write` | 直接执行 | 执行后推 diff 摘要 |
| `danger` | **阻塞**，等批准 | 立即推 QQ，附完整命令 |

审批请求结构（复用 `napcat_tools` 的 `APPROVE_TTL` 思路）：

```python
{"id":"ap_7f3a", "session_id":"h_...", "tool":"run_shell",
 "argv":["git","push"], "reason":"模型想推送分支",
 "expires_at": 1762900000, "status":"pending"}
```

用户回 `/h 批准 ap_7f3a` 或 `/h 拒绝 ap_7f3a`。超时（默认 600s）自动拒绝并终止本轮。

**默认拒绝**，不是默认允许——这是安全设计里唯一不可商量的地方。

### 6.4 资源限额

| 项 | 默认 | 实现 |
|---|---|---|
| 单命令超时 | 120s | `asyncio.wait_for` + 杀进程组 |
| 输出上限 | 64KB | 超出落 `full_log_path` |
| 单会话磁盘 | 2GB | 定期 `du` 检查 |
| 并发会话 | 2 | 信号量 |
| 每 5 分钟改代码任务 | 3 | 令牌桶 |

---

## 7. 会话与状态

### 7.1 存储

`data/harness/sessions/<session_id>.json`，一个会话一个文件（避免并发写同一个大 json）：

```json
{
  "session_id": "h_20261112_ab12cd",
  "frontend_key": "qq:private:123456",
  "backend": {"kind": "local", "root": "data/harness/ws/h_20261112_ab12cd"},
  "repo": {"url": null, "base_branch": "main", "work_branch": "harness/20261112-ab12cd"},
  "state": "idle",
  "budget": {"steps_used": 7, "steps_max": 24, "tokens_used": 18400, "deadline": 1762900000},
  "turns": [
    {"role": "user", "content": "把 cover 开关默认改成 off", "ts": 1762899000},
    {"role": "assistant", "tool_calls": [{"id":"c1","function":{"name":"grep","arguments":"{...}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "plugins/song_cover/__init__.py:41:..."}
  ],
  "pending_approvals": [],
  "changed_files": ["plugins/song_cover/__init__.py"],
  "created_at": 1762898000,
  "last_active": 1762899000
}
```

`turns` 只保留最近 N 轮（默认 40），更早的摘要成一条 `{"role":"system","content":"[历史摘要] ..."}`——和现有 `services/llm.py` 的 `prompt_builder` 思路一致。

### 7.2 前端映射

`data/harness/index.json`：

```json
{"qq:private:123456": "h_20261112_ab12cd", "qq:group:987654": "h_20261112_ee9988"}
```

私聊按 `user_id`，群按 `group_id`（群里是共享一个会话，符合"群里一起讨论同一件事"的直觉）。

### 7.3 工作区隔离

**每个会话一个 git worktree，不是直接改主工作区**：

```bash
git worktree add ../ws/h_xxx -b harness/20261112-ab12cd
```

理由：改坏了 `git worktree remove` 一条命令回滚，主干永远干净。`/h 应用` 才 `git merge` 到主干。

---

## 8. 与现有 QQ bot 的集成

### 8.1 挂载模式的坑（必须先堵）

`docker-compose.yml` 里：

```yaml
volumes:
  - ./plugins:/app/plugins
  - ./services:/app/services
  - ./bot.py:/app/bot.py
```

好处：harness 改完宿主机仓库，重启容器即生效，不用 rebuild。

**坏处**：如果 harness 改到一半 bot 重启，加载到半成品 → `SyntaxError` → 容器起不来，只能 SSH 救。

三道闸：

1. **compileall 闸**：`/h 重启` 之前强制跑 `python -m compileall -q plugins services bot.py`，非 0 直接拒绝并回传报错
2. **worktree 隔离**：见 §7.3，主干不动
3. **`/h 回滚`**：`git checkout .` + 重启，一条命令

### 8.2 复用清单（不改现有代码）

| 复用 | 用途 |
|---|---|
| `tool_dispatcher`: `STATUS["tasks"]` + `/api/tasks/pull|complete|download` | RemoteBackend 的传输层 |
| `services/agent_client.py`: `_post_json` 的降级风格 | 所有对外 HTTP 都"挂了返回 None 不抛" |
| `services/llm.py`: `register_tool` / `TOOL_SCOPES` / `TOOL_CTX` | 把 harness 包装成"她"的一个工具 |
| `services/access.py`: `level_of` | 权限判定 |
| `services/napcat_tools.py`: `APPROVE_TTL` 机制 | 审批闸的设计参考 |
| `plugins/tool_dispatcher`: `driver.server_app.include_router` | 挂新 HTTP 端点 |

### 8.3 新增文件清单

```
plugins/harness/
  __init__.py        # 指令注册 + 会话路由
  commands.py        # /h 子命令解析
  notify.py          # QQ 节流推送 + 折叠成文件
  approval.py        # 审批闸
  api.py             # POST /api/harness/event 端点

services/harness/
  __init__.py
  loop.py            # AgentLoop 状态机
  llm.py             # LLMAdapter（DeepSeek / GLM / 本地 qwen）
  tools/
    __init__.py      # 注册表
    readonly.py      # §5.1
    write.py         # §5.2
    exec.py          # §5.3
    control.py       # §5.4
  backends/
    __init__.py
    base.py          # §3.1 契约
    local.py
    docker.py
    remote.py
  session.py         # 持久化
  jail.py            # 路径 jail + 命令分级
  events.py          # 事件模型 + 聚合器
```

### 8.4 QQ 侧指令

| 指令 | 作用 |
|---|---|
| `/h <自然语言>` | 对当前会话说一句，触发 loop |
| `/h 新 [后端]` | 开新会话，可选 `local` / `docker` / `remote` |
| `/h 现状` | 当前会话：后端、分支、步数、改过的文件 |
| `/h 批准 <id>` / `/h 拒绝 <id>` | 审批 |
| `/h 应用` | 把 worktree 分支 merge 回主干 |
| `/h 回滚` | 丢弃本会话所有改动 |
| `/h 重启` | compileall 校验通过后重启 bot 容器 |
| `/h 停` | 中断当前 loop |

### 8.5 事件回推协议

```http
POST /api/harness/event
Content-Type: application/json

{
  "session_id": "h_20261112_ab12cd",
  "seq": 12,
  "kind": "thinking | tool | diff | need_approval | done | error",
  "title": "改了 1 个文件",
  "text": "song_cover/__init__.py: COVER_DEFAULT 改成 False",
  "files": [{"name": "changes.patch", "path": "/app/data/harness/out/x.patch"}],
  "ts": 1762899123
}
```

QQ 侧聚合规则：

| kind | 处理 |
|---|---|
| `thinking` / `tool` | 同类 3 秒合并，最多推 1 条/3 秒，超时丢弃不补 |
| `diff` | 不推正文，累积；`done` 时打包成 `.patch` 发文件 |
| `need_approval` | **不合并，立即推**，附命令原文 |
| `done` / `error` | 立即推，附摘要 |

单条 ≤ 200 字（对齐现有 `MAX_REPLY_CHARS`），超出截断 + "…（详见文件）"。

---

## 9. 配置项

```bash
# --- 总开关 ---
HARNESS_ENABLED=true
HARNESS_OWNER_ONLY=true              # 只允许 owner 触发

# --- 后端 ---
HARNESS_BACKEND=local                # local | docker | remote
HARNESS_WORKSPACE_ROOT=data/harness/ws
HARNESS_DOCKER_IMAGE=python:3.11-slim
HARNESS_DOCKER_NETWORK=none          # 默认无网络，需要装依赖时临时开
HARNESS_REMOTE_BASE_URL=http://127.0.0.1:8080
HARNESS_REMOTE_SECRET=               # 复用 API_SECRET

# --- 预算 ---
HARNESS_MAX_STEPS=24
HARNESS_WALL_CLOCK=900
HARNESS_TOKEN_BUDGET=120000
HARNESS_CMD_TIMEOUT=120
HARNESS_OUTPUT_MAX=65536

# --- 限流 ---
HARNESS_MAX_SESSIONS=2
HARNESS_TASKS_PER_5MIN=3

# --- LLM ---
HARNESS_LLM_BASE_URL=                # 空则复用现有 DeepSeek 配置
HARNESS_LLM_API_KEY=
HARNESS_LLM_MODEL=deepseek-chat

# --- 审批 ---
HARNESS_APPROVE_TTL=600
```

---

## 10. 分期与验收

### 期 1：最短闭环（半天）

范围：`Backend` 契约 + `LocalBackend` + 只读组 8 个工具 + `AgentLoop` 最小状态机 + `/h <话>` + `/h 现状`

**验收标准**（必须可复现）：
> 在 QQ 私聊发 `/h 翻唱开关存在哪个文件，默认值是什么`，30 秒内收到回答，内容包含 `plugins/song_cover/__init__.py` 和正确的默认值。

### 期 2：能改代码（1 天）

范围：写入组 + `edit_file` + worktree 隔离 + diff 打包回传 + 事件节流 + `/h 应用` `/h 回滚` `/h 重启`

**验收标准**：
> `/h 把翻唱开关的默认值改成 False` → 收到 `changes.patch` 文件 → `/h 应用` → `/h 重启` → 重启后默认关闭。

### 期 3：多后端 + 安全（1 天）

范围：`DockerBackend` + `RemoteBackend` + 执行组 + 审批闸 + `register_tool` 工具化 + 限流

**验收标准**：
> ① `HARNESS_BACKEND=docker` 时期 1/期 2 验收全过；
> ② `HARNESS_BACKEND=remote` 时能通过算力机跑 `nvidia-smi`；
> ③ `/h 执行 git push` 必须阻塞等批准，拒绝后不执行。

---

## 11. 风险矩阵

| 风险 | 后果 | 对策 | 章节 |
|---|---|---|---|
| harness 改坏代码 | bot 起不来，只能 SSH 救 | worktree 隔离 + compileall 闸 + `/h 回滚` | §8.1 |
| 路径逃逸 | 改到宿主机任意文件 | L4 统一 jail，先 resolve 再比前缀 | §6.1 |
| 命令注入 | RCE | `argv` 数组，永不过 shell | §3.1 |
| 无限循环烧钱 | 账单爆炸 | `max_steps` + `token_budget` + 5min 令牌桶 | §4.3 §6.4 |
| 输出刷屏 | 被踢出群 / 被禁言 | 节流 + 折叠成文件 | §8.5 |
| 后端不可用 | 任务静默卡死 | 显式报错，**不降级到 local**（安全考虑） | — |
| 模型幻觉改错文件 | 改到无关代码 | `edit_file` 要求 `old_string` 唯一 + diff 全量回传 | §5.2 |

**关于"后端不可用"为什么不做降级**：`docker` 后端挂了自动退到 `local`，等于把隔离墙拆了还悄悄不说。宁可失败，不可静默降级。

---

## 12. 待定项

| 项 | 选项 | 建议 |
|---|---|---|
| 混合后端 | 文件面 local + 进程面 remote | v1.2 再做，v1 单选 |
| 多租户 | 会话级隔离 vs 用户级隔离 | 先会话级，`frontend_key` 里已含用户标识 |
| 会话共享 | 群里多人同时 `/h` 怎么排队 | 期 2 定：先做「后到者等前者结束」 |
| 视觉输入 | QQ 发图 → 让 harness 看图改 UI | 依赖现有 `vision_agent`，v1.3 |

---

## 附录 A：一次完整会话的时序

```
用户: /h 把翻唱开关默认改成 False
  │
  ├─ L1 解析 → Turn(user, "把翻唱开关默认改成 False")
  ├─ L2 载入会话 h_xxx（backend=local, worktree=/ws/h_xxx）
  │
  ├─ step1  LLM → tool_call: grep("COVER_ENABLED|cover_switch", ".")
  │        L3 → L4.list/get → 命中 plugins/song_cover/__init__.py:41
  │        Event(kind=tool, "grep 命中 1 处")
  │
  ├─ step2  LLM → tool_call: read_file("plugins/song_cover/__init__.py", 30, 60)
  │        Event(kind=tool, 静默)
  │
  ├─ step3  LLM → tool_call: edit_file(path, "COVER_ENABLED = True", "COVER_ENABLED = False")
  │        L4.put → 写入 worktree
  │        Event(kind=diff, "song_cover/__init__.py -1/+1")
  │
  ├─ step4  LLM → tool_call: compile()
  │        L4.exec(["python","-m","compileall","-q","."]) → exit 0
  │        Event(kind=tool, "编译通过")
  │
  └─ step5  LLM → tool_call: finish("已把默认值改为 False，编译通过")
           Event(kind=done, files=[changes.patch])
  │
  ▼
QQ 收到: "已把默认值改为 False，编译通过" + changes.patch
用户: /h 应用 && /h 重启
```
