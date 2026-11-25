# 部署指南（从零跑起来）

> 目标：一台 Linux 服务器（建议 2C4G 起）+ 一个 QQ 号 + 可选一台 GPU 算力机。
> 本文不包含任何真实密钥、QQ 号或服务器地址，请替换成你自己的。

## 0. 组成

```
NapCatQQ（QQ 协议端，Docker）
   │  OneBot v11（WebSocket）
   ▼
NoneBot2（本项目，Docker）
   ├─ plugins/   聊天 / 识图 / 画图 / 三方插件…
   ├─ services/  记忆 / 心情 / 好感 / 提示词…
   └─ core/      参数表 / 配置 / 流水线
   │  HTTP（可选）
   ▼
node.py（GPU 算力机：ComfyUI / RVC / IndexTTS）
```

最低配置：只跑聊天 + 记忆，不需要算力机。

## 1. 准备

- Linux（Ubuntu 22.04 验证过）、Docker + Docker Compose v2；
- 一个 QQ 小号（建议不要用主号）；
- 一个 DeepSeek（或其它 OpenAI 兼容）API Key；
- 可选：GLM Key（视觉）、DashScope Key（语音）、Tavily Key（搜索）。

## 2. 拉代码与配置

```bash
git clone <本仓库地址> qq-little-whale-bot
cd qq-little-whale-bot
cp .env.example .env
```

`.env` 最少要填：

```ini
# 框架
HOST=0.0.0.0
PORT=8080

# 白名单（换成你自己的）
SUPERUSERS=["123456789"]
ALLOWED_GROUPS=111111111,222222222
ALLOWED_PRIVATE=123456789

# NapCat 登录的 QQ
NAPCAT_ACCOUNT=123456789
# 她的人格 ID（换号时不要改，见 docs/memory-profile-design.md）
SELF_ID=123456789
SELF_ALIASES=

# 主对话模型（OpenAI 兼容）
XAI_BASE_URL=https://api.deepseek.com
XAI_API_KEY=sk-你的key
XAI_MODEL=deepseek-chat

# 角色扮演（可复用上面的 key）
ROLEPLAY_BASE_URL=https://api.deepseek.com
ROLEPLAY_MODEL=deepseek-flash

# 三方判读（心情 / 收发件人）
MOOD_JUDGE_MODEL=deepseek-chat
SCENE_AUDIT_MODEL=deepseek-chat
```

> 全部参数（含默认值和说明）见 [tuning.md](tuning.md)。
> `.env` 权限建议 `chmod 600`，**永远不要提交到 git**。

### 2.1 人格卡

```bash
mkdir -p services/Personality
cp personality.example.txt services/Personality/我的角色.txt
# 编辑成你自己的角色
```

`.env` 里：

```ini
DEFAULT_PERSONA=我的角色
```

## 3. 启动 NapCat（QQ 协议端）

`docker-compose.yml` 已经包含 `napcat` 服务：

```yaml
napcat:
  image: mlikiowa/napcat-docker:latest
  network_mode: "host"
  environment:
    ACCOUNT: "${NAPCAT_ACCOUNT:-}"
    WEBUI_PORT: "6099"
  volumes:
    - ./napcat/config:/app/napcat/config
    - ./napcat/qq:/app/.config/QQ
```

启动：

```bash
docker compose up -d napcat
docker logs -f napcat
```

第一次需要扫码：日志里会出现二维码路径（或打开 `http://服务器IP:6099` 用 WebUI 登录）。

登录后，给这个号建 OneBot v11 反向 WS：

`napcat/config/onebot11_<QQ号>.json`：

```json
{
  "network": {
    "httpServers": [],
    "httpSseServers": [],
    "httpClients": [],
    "websocketServers": [],
    "websocketClients": [{
      "url": "ws://127.0.0.1:8080/onebot/v11/ws",
      "token": "",
      "reconnect-interval": 3000,
      "enable": true
    }],
    "plugins": []
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": false,
  "imageDownloadProxy": "",
  "timeout": {"baseTimeout": 10000, "uploadSpeedKBps": 256,
              "downloadSpeedKBps": 256, "maxTimeout": 1800000}
}
```

> 本项目的 bot 是 **WS 服务端**（监听 8080），NapCat 是客户端。

### 3.1 掉线自动登录 / 自动重启（2026-11-24）

腾讯会周期性把快速登录 token 作废（日志：`快速登录错误：你的用户身份已失效`），
只靠 `restart: always` 不够。现在有两层：

1. **密码回退**：`.env` 里二选一填
   ```ini
   NAPCAT_QUICK_PASSWORD=你的QQ密码
   # 或者
   NAPCAT_QUICK_PASSWORD_MD5=密码的32位小写MD5
   ```
   改完必须 `sudo docker compose up -d --force-recreate napcat`（会重建容器并重新登录）。
   配了密码后，NapCat 掉线会优先用密码快速登录；没配就只会换一张二维码。
2. **宿主机看门狗**：`scripts/napcat_watchdog.sh`，建议 cron 每 2 分钟跑一次：
   ```bash
   chmod +x ~/qq-grok-bot/scripts/napcat_watchdog.sh
   crontab -e
   */2 * * * * /home/admin/qq-grok-bot/scripts/napcat_watchdog.sh
   ```
   - 容器没跑 → `docker start napcat`；
   - 登录失效 + **已配密码** → `docker restart napcat`（冷却 10 分钟，避免死循环）；
   - 日志出现「需要验证码 / 短信验证 / proofWaterUrl」→ **只记日志、不重启**，
     等你在 NapCat WebUI 完成验证（否则每次重启都会换一张验证码，永远追不上）；
   - 没配密码 → 只写 `data/napcat_watchdog.log`，不反复重启（否则二维码一直被作废）。
   - 同时把 `napcat/config/webui.json` 的 `autoLoginAccount` 设成当前 QQ，
     下次重启会优先尝试该账号。

> ⚠️ 密码登录也可能被腾讯要求设备验证 / 短信；看门狗能减少手动重启，但不能绕过风控。
> 根治仍是把 NapCat 放到国内 IP，并避免手机端和容器同时登录同一个号。

## 4. 启动 bot

```bash
docker compose up -d --build bot
docker logs -f qq-grok-bot
```

看到这些就算成功：

```
OneBot V11 | Bot <QQ> connected
[IDENTITY] 人格 ID = ...｜登录过的号 = ...
```

> ⚠️ **改 `.env` 后必须 `docker compose up -d --force-recreate bot`**。
> `docker compose restart` 不会重新读 `env_file`（踩过坑）。

## 5. 验证

```bash
# 代码/配置一致性（宿主机或容器内都行；容器内更准，见下方说明）
python3 scripts/verify_all.py

# 容器内（真实依赖 + 真实 .env）
docker exec qq-grok-bot python3 scripts/verify_all.py
docker exec qq-grok-bot python3 scripts/verify_rollbacks.py
docker exec qq-grok-bot python3 scripts/test_new_features.py
docker exec qq-grok-bot python3 scripts/audit_features.py --strict
```

> **为什么优先在容器内跑**：宿主机常常只有 python 3.8，缺 `nonebot/httpx`，
> 且 3.8 解析不了 3.9+ 语法（`match`、装饰器里套调用），会出一堆假失败。
> 容器是 3.11，才是线上真实环境。

### 5.1 三项「口径/接线」检查（2026-11-25 起）

```bash
# ① 调参表：AST 找真实读点 + 揪「模块级常量读了不用」
docker exec qq-grok-bot python3 scripts/check_tuning.py --strict

# ② 设计文档：默认只查「文档 ↔ 设计常量」；--runtime 额外查接线与线上覆盖
docker exec qq-grok-bot python3 scripts/check_design_docs.py --runtime --strict

# ③ 部署清单：仓库文件 md5 必须和清单一致
md5sum -c --quiet deploy_manifest.txt
```

`check_tuning` 的三条硬规则：

1. 数值型 `float(os.getenv("X", "1.0"))` 一律不允许（必须 `tuning.f/i`）；
2. `tuning.f/i/raw/get("X")` 的键必须注册在 `core/tuning.py::DEFAULTS`；
3. `FILES` 声明的文件里必须真有这个键的字面量，且模块级常量必须真被用到
   （`LEGACY` / `UNWIRED` 豁免）。**注释里写一句键名不再算接线。**

### 5.2 真实日志回滚（NapCat 日志 ↔ 设计文档）

```bash
# ⚠️ 用 docker logs --since 截窗口，不要只靠 --after：
#    [RECALL] 角色脑 … 这类行没有 "11-25 HH:MM" 前缀，--after 过滤不到。
docker logs --since 2h qq-grok-bot 2>&1 \
  | docker exec -i qq-grok-bot python3 scripts/verify_napcat_log.py \
      --log - --data-dir data
```

输出 `PASS / FAIL / SKIP` 三态；**SKIP 表示这段日志没覆盖到该场景，不是失败**。
重启/部署后建议只取部署之后的窗口（`--since 10m`），避免把旧版本的慢样本算进来。

### 5.3 升级一版代码的标准动作

```bash
# 1) 备份将被覆盖的文件
tar czf /home/admin/backup_$(date +%m%d_%H%M).tgz \
    core services plugins scripts docs deploy_manifest.txt

# 2) 上传（services/core/docs 是 admin 属主；plugins/scripts 是 root 属主）
rsync -az --no-group --no-owner --no-perms -R core services docs \
      qqbot:/home/admin/qq-grok-bot/
scp scripts/xxx.py qqbot:/tmp/ && \
  ssh qqbot "sudo install -m 644 -o root -g root /tmp/xxx.py /home/admin/qq-grok-bot/scripts/xxx.py"

# 3) 重启（bind mount，改完 restart 即生效）
docker restart qq-grok-bot && docker logs --tail 30 qq-grok-bot

# 4) 跑 5.0 / 5.1 / 5.2 的检查
```

群里发：

| 命令 | 期望 |
|---|---|
| `/自检` | NapCat 在线、模型可用 |
| `/接入自检` | 工具注册、目录、开关正常 |
| `/心情` | 当前心情 |
| `/档案 @某人` | 人物卡 |
| `/用量` | token 用量 + 图表 |
| `/测函数 prompt` | prompt 分段占比 + 缓存 |

## 5.5 算力机 / 本机插件（`NODE_COMPUTE`）

`.env`：

```ini
# 1 = 走 node.py（默认）
# 0 = 不加载本项目的 draw / song_cover，画图/唱歌交给 plugins/third 里的现成插件
NODE_COMPUTE=1
```

`NODE_COMPUTE=0` 时：

- `plugins/draw`、`plugins/song_cover` 不加载（同名 `/画图`、`/唱歌` 让给三方插件）；
- `draw_image` / `draw_from_pixiv` / `pixiv_search` / `sing` / `video_understand`
  这些 function 工具不注册；
- 工具目录会变成「本机模式」，提示模型先去 `[[TOOLS|搜:画图]]` 找三方工具。

把本机画图/唱歌插件放进 `plugins/third/<名字>/`，如果要让她能 function call，
按 [third-party-integration.md](third-party-integration.md) 写 `plugin.toml`。

## 5.6 改代码：self_tool / harness（`CODE_AGENT`）

```ini
# self_tool = 她自己写 Python 小工具（默认）
# harness   = 接本机 DeepSeek Harness，可以真的改仓库代码（self_tool 自动关闭）
# off       = 都不开
CODE_AGENT=self_tool
```

Harness 桥的配置与用法见 [harness-bridge.md](harness-bridge.md)。

## 5.7 模型自由搭配（每个角色单独选）

```ini
# 主聊天
ROLEPLAY_PROVIDER=deepseek
ROLEPLAY_MODEL=deepseek-flash
# 识图（也可以换 deepseek-flash / qwen3-vl-plus）
VISION_PROVIDER=glm
VISION_MODEL=glm-4v-plus
# 两个 chat 检测
JUDGE_PROVIDER=deepseek
AUDIT_PROVIDER=deepseek
```

查看当前分配：`/测函数 模型`。完整说明见 [model-providers.md](model-providers.md)。

## 5.8 群聊观察模式（`GROUP_WATCH_MODE`）

```ini
# full = 所有群消息都判定（默认，最像真人，也最费 token）
# lite = 省流：消息照记（白月光/心情不受影响），只回 @她 / 引用她 / 喊名字 / 收件人是她
GROUP_WATCH_MODE=full
```

省流模式下不主动插话、不做表达学习；上下文条数仍由 `MAX_GROUP_RECENT`（默认 16）控制，
**没有单独的「只拉 8 条」分支**（旧文档写错了，2026-11-25 更正）。私聊不受影响。

## 5.9 运行档位（`POWER_MODE`，和 5.8 正交）

```ini
# full = 所有段全开（默认）
# eco  = 节能：关掉花钱的判定类调用（审核 / 图书馆 / 主动插话 / 表达蒸馏），
#        但「记录」永远不省（上下文 / 白月光 / 风格语料 / 心情 touch / 硬信号）
POWER_MODE=full
```

两者关系：`GROUP_WATCH_MODE` 管**回不回**（接话范围），`POWER_MODE` 管**算不算**（成本档位）。
代码入口：`services/config.py::power_on(stage)`；eco 下 `/状态` 仍会显示档位。
建议组合：`lite + eco` = 只在叫她时才算；`full + eco` = 全都读、但便宜地读。

### 5.10 记忆双写（`MEMORY_DUAL_WRITE`，2026-11-25）

```ini
# 1（默认）= json 每写一条，同时镜像到 sqlite；0 = 只写 json
MEMORY_DUAL_WRITE=1
```

用途：先把 `data/memory.db` 喂满，再切 `MEMORY_BACKEND=sqlite`，避免「切完旧记忆搜不到」。
对账：`python3 scripts/check_memory_dual.py --strict`（要求 sqlite ≥ json 90%、
最近 200 条 turn_id 全在）。切后端前先备份 `data/memory.db`。

## 6. 可选：算力机（画图 / 翻唱 / 语音 / 视频）

在一台有 NVIDIA GPU 的机器上：

```bash
cp node.env.example node.env   # 填 API_SECRET / 模型路径 / 服务地址
python3 node.py --check        # 依赖自检
python3 node.py                # 启动，轮询服务器任务
```

`node.env` 里的 `API_SECRET` 必须和服务器 `.env` 一致。
详见 [node-compute.md](node-compute.md)。

## 7. 数据与备份

| 路径 | 内容 | 建议 |
|---|---|---|
| `data/dialogue/` | 对话流水（14 天） | 每天备份 |
| `data/memory*` | 记忆 | 每天备份 |
| `data/profiles/` | 档案 | 每天备份 |
| `data/mood*.json` | 心情 / 群心情 | 每周 |
| `data/white_moon.json` | 白月光统计 | 每周 |
| `data/llm_usage.db` | 用量 | 可定期归档 |

```bash
tar czf backup-$(date +%F).tgz data .env
```

`data/` 不要进 git，`.env` 不要进 git。

## 8. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 收不到消息 | NapCat 没登录 / 反向 WS 没配 | 看 `docker logs napcat`，检查 `onebot11_<QQ>.json` |
| 改了 `.env` 不生效 | 用了 `restart` | 改 `up -d --force-recreate bot` |
| 换了 QQ 号后失忆 | 改了 `SELF_ID` | `SELF_ID` 不动，旧号写进 `SELF_ALIASES` |
| 发图失败 / 很慢 | 原图太大 | 先压缩；NapCat 传 4MB 图会超时 |
| 工具调不到 | `plugin.toml` 没声明 / 分类没写进人格卡 | 看 [third-party-integration.md](third-party-integration.md) |
| 容器内 verify 说清单不一致 | 镜像是旧清单 | 在**宿主机**跑 `verify_deploy.py`，清单按实际文件重算 |
| 磁盘满 | 日志/图片/语音堆积 | 配 docker 日志轮转；定期清 `data/temp*` |

## 9. 安全

- `.env`、`data/`、`napcat/` 全部在 `.gitignore` 里；
- 不要把 `SUPERUSERS` 设成不认识的号；
- 高风险工具（踢人/禁言/退群）保持 `confirm` 或 `owner`；
- 反向代理只暴露必要端口，NapCat WebUI 不要裸奔公网。
