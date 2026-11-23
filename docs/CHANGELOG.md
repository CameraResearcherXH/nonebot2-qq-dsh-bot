# 更新日志

## 2026-11-23

### 新增

- **白月光模式**（`services/white_moon.py`）
  - 统计**群友之间**「谁最常 @ / 引用 / 接谁的话」，两层：每个人的白月光 + 群白月光；
  - 她回某个人时借那个人的句式/口头禅，回大家时用群白月光；
  - `/测函数 白月光 [群号]` 查看；详见 [whitemoon.md](whitemoon.md)。
- **详细用量与 prompt 体检**
  - `/用量` 按用途拆分 token、缓存命中率、Top/Bottom，并出横向柱状 + 扇形图；
  - `/测函数 prompt` 给出角色卡/规矩/历史/心情/好感等分段占比、块内位置、缓存报告；
  - 图表从已下线的 Google Image Charts 切到 `image-charts.com`（可用 `USAGE_CHART_BASE` 自建）；
  - 详见 [usage-report.md](usage-report.md)。
- **以图搜源插件**（`plugins/third/picsearch` + `vendor/PicImageSearch`）
  - 聚合 TraceMoe / SauceNAO / Ascii2D / IQDB；
  - 通过 `plugin.toml` 暴露 `picsearch` function 工具（`hold=true`）；
  - 详见 [third-party-integration.md](third-party-integration.md)。
- **人格 ID 与登录 QQ 解耦**（`services/identity.py`）
  - `SELF_ID` 固定人格，`NAPCAT_ACCOUNT` 是当前登录号，`SELF_ALIASES` 是历史号；
  - 换号后旧号说过的话、@ 旧号的记录仍算「她」；记忆/好感/白月光不随 QQ 走。
- **戳一戳次数屏蔽**（`services/poke_guard.py`）
  - 同一个人 2 小时内戳超过 3 次 → 第 4 次起不回、也不戳回，只记上下文；
  - 滑动窗口，2 小时后自动恢复；阈值可调（`POKE_MAX_PER_WINDOW` / `POKE_WINDOW_HOURS`）。
- **部署文档 / 三方插件接入文档 / 记忆档案设计文档**
  - [deployment.md](deployment.md)、[third-party-integration.md](third-party-integration.md)、
    [memory-profile-design.md](memory-profile-design.md)。

### 优化

- **角色卡 + 规矩去重**：22,751 → 14,586 字（-35.9%），角色卡单项 12,378 → 6,343（-48.8%）；
  - 删掉与规矩块逐条重复的「说话铁律」17 条、「沟通策略」整节、「人格重申」重复段；
  - 合并「说话方式 / 节奏」两节；工具目录保留分类，只删重复的用法说明；
  - 规矩块合并「该不该接话」「接不上怎么办」里的重复段落。
- **收发件人贯通**：分段、判读、主模型上下文统一用 `group_scene.history_lines`
  （带「谁 → 谁」）；被截断时**保留所有 @ 她的行**，并列出「必须回应」清单。
- **多条 @ 一条答完**：一段里多人 @ 时，要求用一条消息全部答到，最多 2 条气泡，
  不再「只挑最后一条」。
- **关注期不乱接**：收件人明确是别人（`to_kind=person`、置信度 ≥0.5）→ 不回；
  判读模型「不确定 / 失败」→ 不回。
- **旁白剥离**：`strip_meta_talk()` 丢掉「串线」「不是跟我说的」「我插不上话」这类旁白；
  全丢=沉默。
- **模型分工**：角色扮演 `deepseek-flash`；心情判读/收发件人 `deepseek-chat`；
  GLM 只做视觉。
- **长回复不截断**：带 @ / 显式 `<reply:>` 的回复整段发完，不再按字数硬切。

### 修复

- **好感从未进 prompt**：`build_packet` 产出了 `relation`，但注入清单漏了 —— 补上并前移。
- **群聊分段永不执行**：`if not _seg_records` 缩进错位，群聊没有分段上下文 —— 修缩进。
- **重启后现场只剩最后一句**：现场只在内存，重启即空 —— `seed_from_log()` 按条数补回。
- **白月光写丢数据**：`_group()` 的 `setdefault` 写法会覆盖已有统计 —— 改为正确合并。
- **旧号被当群友**：身份判断用 `event.self_id`，换号后旧号变成陌生人 —— 改用别名表。
- **@ 了却不回**：prompt 里「只接一个线程 / 默认回最后一条」+ 截断丢掉 @ —— 已重写。
- **识别结果被念出来**：视觉标记/画面描述漏进正文 —— 提示词 + `strip_media_echo` 双保险。
- **NapCat 掉线收不到消息**：`.env` 缺 `NAPCAT_ACCOUNT` + 新号没有反向 WS 配置 —— 已补。
- **`/测函数 用量` 报 FinishedException**：`finish()` 放在 `try/except` 里被二次捕获 —— 已移出。
- **`/用量` 图表 404**：Google Image Charts 下线 —— 切 `image-charts.com`。

### 三方插件

- 新增 `picsearch`（以图搜源）；
- `girls_2d`：去硬编码路径、修 `UnboundLocalError`、同步改异步、uuid 临时文件；
- `petpet`：pydantic schema、异常兜底、大文件超时不重发、emoji 昵称、临时文件 `file://`；
- `pixivrank_search`：图片压缩、超时处理、串行闸门、随机抽取、尺寸前缀、`/搜图`；
- `steam_judger`：出图链路自检、可选 Chrome、`retcode=1200`、`task_done` 修正；
- `gspanel`：独立 `/绑定`、`KeyError: 'Costumes'`、图片发送、注册成工具；
- `rollpig_plus`：`CommandArg()` None 兼容、LLM 直调、身份别名感知；
- `bittorrents`：跟随重定向、隐私、卡片套层；
- `jinriyunshi`：共享每日重置、统一 `/` 前缀；
- 完整清单见 [third-party-integration.md](third-party-integration.md#4-我们对上游插件改了什么)。

### 部署 / 兼容

- 新增 `docs/deployment.md`：Docker + NapCat + `.env` + 验证 + 排错；
- `.env` 新增 `SELF_ID` / `SELF_ALIASES` / `POKE_MAX_PER_WINDOW` / `POKE_WINDOW_HOURS`；
- 改 `.env` 后必须 `docker compose up -d --force-recreate bot`（`restart` 不重读 env_file）；
- 代码闸门：`audit_features` 43 项、`test_new_features` 135 项、`selftest_all` 1781 项、
  `verify_all` 9 道、`verify_deploy` 94 个文件。
