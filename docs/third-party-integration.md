# 第三方插件：接入 function calling 与「我们对上游改了什么」

> 相关：`docs/plugin-dev.md`（最小示例）、`docs/third-party-audit.md`（来源/许可证/风险）
> 代码：`services/plugin_registry.py`、`plugins/third/*/plugin.toml`、`plugins/napcat_tools`

## 1. 两种接入方式

第三方 nonebot 插件放进 `plugins/third/<名字>/` 后，有两种命运：

### A. 只当普通 nonebot 插件

`bot.py` 会 `load_plugins("plugins/third")`，插件的 `on_command` / `on_message`
照常工作（例如 `/来点美少女`、`/搜图`）。

- 优点：零改造，上游怎么写的就怎么跑；
- 缺点：**她（LLM）不知道有这个能力**，只能靠用户手动敲指令。

### B. 声明成 function calling 工具（推荐）

在插件目录放一个 `plugin.toml`，声明 `[[tools]]`：

```toml
[plugin]
name = "picsearch"
version = "0.1.0"
description = "以图搜源：TraceMoe / SauceNAO / Ascii2D / IQDB 聚合"

[[tools]]
name = "picsearch"
description = "给一张图片链接，查它的出处（动画/漫画/插画）"
level = "safe"                 # safe | confirm | owner
handler = "services.picsearch_tool:picsearch"
scope = ["private", "group"]
hold = true                    # 结果先暂存，由她决定发不发
parameters = { type = "object", properties = { image_url = { type = "string", description = "图片直链" } }, required = ["image_url"] }
```

启动时 `plugin_registry.register_all()` 会扫描所有 `plugin.toml`，
把 `handler` 注册进 LLM 的 function calling 列表。

#### 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `plugin.name` | ✅ | 唯一名字（也是 `/插件` 开关的 key） |
| `plugin.description` | ✅ | 给人和给模型看的简介 |
| `tools[].name` | ✅ | 函数名（模型看到的名字，建议蛇形） |
| `tools[].description` | ✅ | **写给模型看的**：什么时候用、参数是什么 |
| `tools[].level` | ✅ | `safe` 直接执行；`confirm` 私聊问主人；`owner` 只允许主人 |
| `tools[].handler` | ✅ | `模块:函数`，函数可以是 `async def` |
| `tools[].scope` |  | `["group","private"]`，默认都允许 |
| `tools[].hold` |  | `true` = 返回值作为「待发内容」暂存，她再决定发不发 |
| `tools[].parameters` |  | JSON Schema；不写就无参数 |

#### handler 约定

```python
async def picsearch(image_url: str) -> str:
    """返回一段给模型看的文本（不要自己 bot.send）。"""
    ...
```

- **不要自己发消息**：要么 `return` 文本让模型决定，要么 `hold=true` 走暂存；
- 耗时任务用 `asyncio.to_thread` / 异步 HTTP，别阻塞事件循环；
- 失败返回「人话」错误（「图片下载失败」），不要抛异常给模型；
- 需要真实 QQ 操作时用 `services.napcat_tools.execute`，不要直接 `bot.call_api`。

## 1.5 本机模式（`NODE_COMPUTE=0`）

如果用户不想搭算力机，`.env` 写：

```ini
NODE_COMPUTE=0
```

效果：

- 不加载本项目的 `plugins/draw`、`plugins/song_cover`；
- 不注册 `draw_image` / `draw_from_pixiv` / `pixiv_search` / `sing` / `video_understand`；
- 画图/唱歌由 `plugins/third` 里的现成插件接管（同名 `/画图`、`/唱歌` 不冲突）；
- 想让它们变成 function calling 工具，就按第 1 节写 `plugin.toml`。

工具目录会自动切成「本机模式」，提示模型先 `[[TOOLS|搜:画图]]` 找三方工具。

## 2. 模型怎么发现这些工具

三层，从粗到细：

1. **常驻菜单**（`services/llm.py::_TOOL_MENU_HEAD`）：只告诉模型「有分类，先查」；
2. `[[TOOLS|分类名]]`：展开某类工具的详细说明；
3. `[[TOOLS|搜:关键词]]`：搜具体工具。

分类清单写在人格卡「工具目录」一节（A 记忆 / B 查东西 / C 画图 / … / J 三方插件）。
新增插件后，建议在人格卡对应分类里加一行，否则模型不知道去哪个分类找。

> 为什么不把 60+ 工具全塞进 prompt？实测全量工具目录会多 3~4k token/轮，
> 而且模型会乱调。按需展开更稳。

## 3. 安装与安全

- `scripts/plugin_cli.py` 负责安装/更新/卸载；
- 安装前静态扫描：`subprocess` / `os.system` / `eval` / `exec` / 外联地址；
- 有风险的工具默认不注册，或降级为 `confirm`；
- `UPSTREAM.md` 必须写清来源和许可证（`scripts/audit_third_party.py` 会检查）。

## 4. 我们对上游插件改了什么

> 只列**行为相关**的适配；格式/日志类小修不展开。上游版本见各目录 `UPSTREAM.md`。

### 4.1 `picsearch`（本项目自建，2026-11-22）

- 新增：`plugins/third/picsearch` + `vendor/PicImageSearch`；
- 聚合 TraceMoe / SauceNAO / Ascii2D / IQDB，下载图片 → 并发搜索 → 去重排序 → 报告；
- `plugin.toml` 暴露 `picsearch` 工具（`hold=true`）；
- 修：不能在插件 import 时调 `register_all()`（会和 chat 插件循环导入）。

### 4.2 `girls_2d`（来点美少女）

- 删掉上游硬编码的 Windows 绝对路径（docker 里必崩）；
- 修 `image_data` 未定义就使用的 `UnboundLocalError`；
- `requests.get` 同步阻塞 → `httpx` 异步；
- 固定文件名 → uuid 临时文件，发完即删（并发不互相覆盖）。

### 4.3 `petpet`（头像表情包）

- 给 pydantic 显式 schema（`/兑换券` 报错真凶）；
- 所有下载/解码/发送都包 try，单独打日志；
- 大文件超时不代表没发出去 → 不重复发送；
- emoji 昵称渲染兜底；
- 写临时文件用 `file://` 发，不塞 `BytesIO`；
- 修「同一张图发两次」（`/这像画吗`）。

### 4.4 `pixivrank_search`（P 站排行/搜图）

- 图片先压缩再发（3.9MB 原图 NapCat 要传 4 分钟）；
- 超时两种来源都接住，失败不重试轰炸；
- 串行闸门（同时只跑一个搜索）；
- 从结果前 N 条随机抽 1 条；
- 修尺寸前缀不固定（`/c/480x960/` 只是其中一种）；
- 关键词用 `AllParam` 整体吞掉，支持多词；
- 命令统一成 `/搜图`（两字）；
- 无 token 时用匿名 PHPSESSID 兜底。

### 4.5 `steam_judger`（Steam 锐评）

- 出图链路自检（Chromium / chromedriver 定位）；
- 缺 selenium/Chrome 不再直接拦截，降级为文字；
- 处理 NapCat `retcode=1200 Timeout: NTEvent sendMsg`（消息其实可能已发出）；
- 只有真的拿到任务才 `task_done`，避免任务丢失；
- 限制并发/频率。

### 4.6 `gspanel`（原神面板/绑定）

- 新增独立 `/绑定`（不用每次 `/面板 绑定 xxx`）；
- 修 `KeyError: 'Costumes'`（刻晴等角色没有该字段）；
- `MessageSegment.image(bytes)` 让 NapCat 走错分支 → 改临时文件 `file://`；
- 把绑定/面板/队伍伤害注册成 function 工具；
- 用普通 `import`，不用 `require()`（避免 nonebot 插件加载顺序问题）。

### 4.7 `nonebot_plugin_rollpig_plus`（猪猪）

- 适配新版 NoneBot：无参数时 `CommandArg()` 可能给 `None`；
- 新增 `bot` 参数，支持 LLM function 工具直接调用（没有 matcher 的场景）；
- 身份判断改 `identity.is_self`：换号后旧号仍算 bot（回复预约/投票集合/烤自己）。

### 4.8 `bittorrents`（磁力搜索）

- HTTP 必须跟随重定向；
- 结果卡片不用发送者本人的 QQ/昵称（隐私）；
- 卡片套两层，避免长标题撑破。

### 4.9 `jinriyunshi`（今日运势）

- 和 chat 的 `daily_luck` 工具共用 `last_reset.txt`，每天只重置一次；
- 指令统一 `/` 前缀。

### 4.10 `VideoZoomer_master`（视频理解）

- 原样内置（上游自带 `patches/`），只做路径/依赖接入；
- 通过 `video_understand` 工具调用，结果走视觉通道。

## 5. 新增一个三方工具的最小步骤

1. 把插件放进 `plugins/third/<name>/`，补 `UPSTREAM.md`（来源/许可证）；
2. 如果只要指令，跳过 3；
3. 写一个薄 handler（建议放 `services/<name>_tool.py`），**不要**在 handler 里发消息；
4. 加 `plugin.toml`，声明 `[[tools]]`；
5. 在人格卡「工具目录」对应分类加一行；
6. 跑：
   ```bash
   python scripts/audit_third_party.py
   python scripts/test_new_features.py
   python scripts/verify_all.py
   ```
7. 真机验证：`/接入自检` 看工具是否注册，`/测函数 工具` 看模型能不能查到。

## 6. 常见坑

| 坑 | 现象 | 解法 |
|---|---|---|
| import 时注册 | 插件加载顺序死锁/循环导入 | 只声明 `plugin.toml`，让 `register_all` 统一注册 |
| handler 自己发消息 | 用户收到两条 / 工具结果丢 | `hold=true` 或 return 文本 |
| 同步 requests | 整个 bot 卡住 | `httpx` / `asyncio.to_thread` |
| 没写 parameters | 模型不知道传什么 | 写 JSON Schema |
| 工具名和内置重名 | 后注册的覆盖前面的 | 加前缀，如 `picsearch_` |
| 高风险工具 level=safe | 模型乱调删数据 | `confirm` / `owner` |
