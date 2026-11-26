# 验证报告：线上 4 个问题 + 部署侧 3 个问题（2026-11-26）

> 环境：容器 `qq-grok-bot`（Python 3.11）+ NapCat。所有结论都来自**线上真实数据/日志**，
> 不是推测；每条都写了「证据 → 根因 → 修法 → 验证」。

---

## A. 我们服务器上发现的 4 个问题

### A1. 被 @ 之后，下一句不理（只理了艾特）

**证据**（11-25 22:42~23:01 真实日志）：

```text
22:42:57 [FOCUS] group_100000016 开始关注 2073784966：后续 3 条（120s 内有效）
22:42:58 [COOLDOWN] 100000016 刚回过 8s，这条没点她 → 只记上下文
22:59:57 [FOCUS] group_100000016 对 2073784966 还剩 2 条
22:59:59 [COOLDOWN] 100000016 刚回过 2s，这条没点她 → 只记上下文
23:00:06 [COOLDOWN] 刚回过 8s … / 23:00:22 25s / 23:00:31 34s / 23:01:01 29s
```

**根因**：`GROUP_REPLY_COOLDOWN`（45s）的检查写在「关注窗口」判断**之前**。
被 @ 后明明开启了「后续 3 条」关注，但这 3 条全被冷却当成「没点她」丢掉了。

**修法**：冷却的豁免条件加上「这个人正在关注窗口里」：

```python
_focusing = _focus_active(f"group_{gid}", str(event.user_id))
_addressed_now = (event.is_tome() or _focusing or _calls_me_by_name(...))
```

**验证**：`audit_features G16` 新增断言（`_focusing = _focus_active(` 必须存在），
`audit_features --strict` 52/0；下一次真实 @ 后日志应显示 `[FOCUS]` 而不是 `[COOLDOWN]`。

### A2. 「很多群友好感下降很快，3 小时掉了 0.5」

**证据 1（好感流水）**：55 个有 `affinity_log` 的档案、284 条事件，全历史最差 3 小时窗口
−0.250（发生在 11-20 旧版本）；11-21 之后最差只有 −0.07。近 36h 单次步长 −0.005~−0.014，
`affinity_day_sum` 都在 −0.03 以内（每日上限 0.15）。

**证据 2（亲近公式）**：11-25 把 `intimacy` 改成了饱和曲线 `score/(score+3)`，逐人复算：

```text
737665381   score=2.99  旧 1.00 → 新 0.50   （差 −0.50）
1419481652  score=3.08  旧 1.00 → 新 0.51   （差 −0.49）
100000003  score=3.65  旧 1.00 → 新 0.55   （差 −0.45）
掉 ≥0.3 的 15 人 / 121 个有记录的人
```

**根因**：不是好感衰减坏了，是**亲近（intimacy）**公式改版，一次性把 15 个人的
「亲近」砍掉约 0.5，且发生在同一次部署 —— 群友看到的就是「好感一夜掉了 0.5」。

**修法**：回退成「线性 + 软上限 0.98」：

```python
base = min(0.98, score / 3.0)   # 只把恰好 1.00 压到 0.98
```

**验证**（线上真实档案，修复后）：

```text
737665381  0.975   1419481652 0.972   100000003 0.984   100000006 0.920
曲线：1.5 → 0.50｜3 → 0.98｜100 → 0.98
```

### A3. 红包：能不能让她收发

**证据**：NapCat 主 bundle 里的 `send_group_*` 只有
`send_group_msg / send_group_forward_msg / send_group_notice / send_group_sign /
send_group_ark_share / send_group_ai_record`；全包搜 `red_packet` **0 命中**
（`hongbao` 只出现在表情名里）。OneBot v11 也没有红包 action。

**结论**：
- **收**：需要装 NapCat **原生插件**（社区有 grabredbag），不是 bot 侧调 API 能完成；
  要实现「只有主人说了才收」，还得给插件加授权文件（改插件，不是我们的仓库）。
- **发**：需要 QQ 钱包支付，OneBot/NapCat 都没有暴露；客户端 UI 自动化风险极高，**不建议**。

详见 [red-packet-feasibility.md](red-packet-feasibility.md)。本轮**没有**加任何自动抢/发逻辑。

### A4. B 站测试题被导向视频

**根因**：`_expand_short` 在 `final` 里找不到 BV 时，会去返回的 HTML 里抓**任意** BV，
再改写成 `/video/BVxxx`。活动页/测试题/专栏里常嵌着推荐视频的 BV → 整条链接被改成视频。

**修法**：抽成纯函数 `pick_final_url(final, html)`：

1. `final` 落在 `bilibili.com` 的**任何**页面 → 原样返回（活动页/直播页/测试题都不动）；
2. 只有 `final` 还是 `b23.tv`（真正的 JS 跳转页）才从 HTML 抠 BV；
3. 都没有 → 返回 `final`。

**验证**（`test_new_features` 新增 4 条）：

```text
PASS BILI：活动页/测试题保持原样（不被改成视频）
PASS BILI：直播页保持原样
PASS BILI：JS 跳转页才从 HTML 抠 BV
PASS BILI：video 页原样返回
```

---

## B. 别人部署我们的 code 后发现的 3 个问题

### B1. 「我们是 20 字以上转合并转发，他们部署后是 1000 字以上」

**查证**：代码默认 `OUT_BUBBLE_CHARS=40`、`GROUP_MAX_HANZI=20`、`LONG_FORWARD_MIN=120`，
发行仓两个版本的 `.env.example` 都是这些值；不存在 1000 的默认值。
1000 只能来自对方自己改的 `.env`（或旧的 `data/group_tuning.json`）。

**修法**（让它不再静默）：
1. `services/env_guard.py`：启动时检查 `OUT_BUBBLE_CHARS > 200` / `GROUP_MAX_HANZI > 200` /
   `LONG_FORWARD_MIN > 2000` → `[ENV] ⚠️` 告警，并写明正常值；
2. `.env.example` 显式列出这 4 个键（`OUT_BUBBLE_CHARS / OUT_BUBBLES_MAX /
   LONG_FORWARD_MIN / GROUP_REPLY_COOLDOWN`），注释写明「是转卡片阈值，不是截断」；
3. `docs/deployment.md §5.4` 增加「关键 .env 对照表（线上值）」。

**验证**：`test_new_features` 的 `ENV：白名单为空必须告警` / `ENV：填好后不再告警白名单`
通过；容器启动日志 `[ENV] 启动配置体检通过`。

### B2. 「时不时不理人，私聊艾特都不理」

**根因（最可能）**：`ALLOWED_GROUPS` / `ALLOWED_PRIVATE` 为空时，消息在准入层就被丢掉：

```python
# plugins/chat/__init__.py
if gid not in ALLOWED_GROUPS: return False          # 群
return str(event.user_id) in ALLOWED_PRIVATE        # 私聊
```

而旧版 `.env.example` 把这两个键**清成空值**，新人填漏就是「完全不回」。

**修法**：
1. `.env.example` 不再清空白名单，改成 `123456789` 占位 + 注释「必须改成你自己的」；
2. 启动体检对空白名单告警（`ALLOWED_GROUPS 为空 → 所有群消息都不会回`）；
3. `docs/deployment.md` 常见问题补上这一条。

### B3. 「每次回复卡 4 分钟」

**根因**：多轮补资料循环只在「轮与轮之间」检查预算（`_left`），
单个慢工具（网页 18s / 视频 / 图书馆 / 算力机任务）不受整轮预算约束，
最多 6 轮就能把一次回复拖到几分钟。

**修法**：每轮补资料都套整轮预算：

```python
reply = await asyncio.wait_for(
    _fulfill_requests(...),
    timeout=max(3.0, min(30.0, _left)),
)
```

超时 → 摘掉标记直接开口（不再等）。

**顺带**：`services/env_guard.py` 还会提示
`USE_LOCAL_TTS=1` + `NODE_COMPUTE=0` 的矛盾（没有算力机时语音会先白等）。

### B4. 「脱敏把配置一起脱掉了」

**证据**：`make_dist.gen_env_example` 的规则是「键名含 `SECRET/KEY/TOKEN/COOKIE/PASS` → 清空」，
于是这些**普通参数**被误清：

```text
GLM_MAX_TOKENS=800          → 空（模型 thinking 长度）
VIDEO_MAX_KEY_FRAMES=12     → 空（视频抽帧数）
```

**修法**：只有「值看起来真是密钥」才清空（数字/布尔/短枚举保留），
`ALLOWED_* / OWNER_QQ / NAPCAT_ACCOUNT / SELF_ID` 用占位号。

**验证**（重生成的 `.env.example`）：

```text
GLM_MAX_TOKENS=800 # 开 thinking 时要调大（否则 content 为空）   ← 保留
VIDEO_MAX_KEY_FRAMES=12                                          ← 保留
ALLOWED_GROUPS=111111111 / ALLOWED_PRIVATE=123456789             ← 占位
XAI_API_KEY= / GLM_API_KEY= / API_SECRET= / PIXR_COOKIE=         ← 仍清空
```

---

## C. 验证汇总（容器内真实环境）

```text
verify_all.py --full        → 10/10 PASS
  ├ 语法编译 / 依赖方向 / 调参表 / 名字检查 / 挂载检查
  ├ 接线检查 / 功能链路（52 项）/ 第三方插件合规
  ├ 新功能单测 231 / 0（新增 9 条：亲近曲线 / B站短链 / 环境体检）
  └ 全链路自检 1801 项 / 0 失败
deploy_manifest.txt         → 113 文件 md5 全绿（本地 + 服务器）
启动日志                    → [ENV] 启动配置体检通过；人格 917 字；chat 插件加载成功
```

## D. 没有做的

- 红包自动收发：只出可行性结论，没加代码（见 A3）。
- `data/` 相关插件（猪猪/原神/表情包）在无数据时的表现：属于插件自身降级逻辑，
  本次只确认不影响聊天主链路。
