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
# 宿主机（代码/配置一致性）
python3 scripts/verify_all.py

# 容器内（功能链路）
docker exec qq-grok-bot python3 scripts/audit_features.py --strict
docker exec qq-grok-bot python3 scripts/selftest_all.py
docker exec qq-grok-bot python3 scripts/test_new_features.py
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
