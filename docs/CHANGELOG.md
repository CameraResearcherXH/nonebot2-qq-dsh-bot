# 更新日志

## 2026-11-27（第四十七轮：L0 规则自检落地 + 戳一戳 1 次）

**1. 戳一戳限流：2 小时 3 次 → 1 次（用户拍板）**

- `POKE_MAX_PER_WINDOW` 默认 3→1：窗口内只回 1 次，第 2 次起不回也不戳回；
- 实测 `note()` 第 1 次 `(True,1)`、第 2 次 `(False,2)`。

**2. L0 规则自检（docs/addressing-design.md §7.7.1，之前只写在文档里）**

| 检查 | 判据 | 动作 |
|---|---|---|
| 整条复读 | 与最近 3 条自己消息（10 分钟内）3-gram Jaccard ≥0.8 | **这条不发** |
| 长度异常 | 群聊文字泡 >40 汉字 | 只标记（上游已合并转发，不截断） |
| 问句未答 | 对方有「？」且回复只有「嗯/哈哈/草/？」 | 只标记 `unanswered` |
| 数字/专名缺失 | 对方有数字/英文专名，回复一个都没有 | 只标记 `missing_ref` |
| vent 禁方案词 | `kind=vent` 且含「建议你/你可以/首先/其次/不妨/你应该/试试看」 | **机械删掉含禁词的句子**；删完不足 4 汉字则保留原文（fail-open） |

- 新增 `services/selfcheck.py`（纯规则，<10ms，任何异常返回原回复）；
- `plugins/chat`：send 前钩子（dedup 之前）；`reply_pipeline.build` 返回值新增 `intent`，
  复用 atmosphere 已算好的 `kind`，不额外调模型；
- 新增 tuning：`REPLY_SELFCHECK=1`、`SELFCHECK_RECENT_N=3`、`SELFCHECK_WINDOW_SEC=600`、
  `SELFCHECK_SIM=0.8`、`SELFCHECK_LONG_HANZI=40`；
- badcase 落 `data/selfcheck.jsonl`（离线看命中率）。

**3. 顺手修的真 bug：dedup 一直在跟「对方原话」比**

`last_text_map[session_key] = text` 存的是**对方原话**，而 dedup 拿它跟「她的新回复」比 →
几乎永远不命中。改成存她实际发出去的 `said`，并同步写 `recent_self_texts` 给 L0 复读用。

**4. 外部智能体 4 条复核（2 真 / 1 半 / 1 错）**

- 真：kind 只是提示不是路由（L0 先补硬约束，kind 分叉暂不做）；
- 真：自检层没接上（本轮已落 L0；文档 §7.7 状态同步更正）；
- 半：VLM 仍写「关键 1 句」是**故意兜底**，OCR 有结果时它不进 report，不会冲突；
- 错：`ATMO_TIMEOUT` 是 5s 且**并行**跑（`create_task`），不阻塞主链路。

**5. 验证**

- `check_runtime_wiring` **69 PASS / 0 FAIL**（新增 7 条 L0 断言）；
- `check_tuning` PASS、`audit_coverage --strict` 0 硬伤；
- 真机：`[SELFCHECK] flags=missing_ref` 落盘；修复「ask 误用整段 prompt」后新记录 `flags=[]`；
- napcat 发送正常，0 Traceback。

## 2026-11-27（第四十六轮：P1 RapidOCR 落地 + 视觉默认 qwen）

**1. 用户问「轻量服务器带得动 OCR 吗」→ 先实测再决定**

服务器 2 vCPU / 3.5GB，实测 RapidOCR（PP-OCRv4 mobile）：

| 指标 | 实测 |
|---|---|
| 首次加载 | 0.5s（懒加载，第一张有字的图才吃内存） |
| 常驻 / 峰值内存 | ≈350MB / ≈490MB |
| 单图（长边 1024） | 0.7~2.7s（群截图 2.0s） |
| 磁盘 | 依赖 + 模型 ≈400MB |
| 准确率 | 群截图 4/4 块全对（测试图里 `OCR_TEST_MARKER_42` 被原样读出，确认不是幻觉） |

结论：带得动，装了。

**2. OCR 接入（不增加主链路延迟）**

| 改动 | 文件 | 说明 |
|---|---|---|
| 新增 OCR 模块 | `services/ocr_rapid.py` | 单例懒加载；线程池 + 串行锁（上一张没完就跳过）+ 8s 超时；置信度<0.5 丢弃；任何失败返回 `[]`（fail-open） |
| OCR 与视觉并行 | `services/vision_verify.py` | OpenCV 判「有字」才起 OCR 任务，和 qwen 调用同时跑；文字合并 **OCR > VLM**；`rec["ocr_blocks"]` 进 report/缓存 |
| 视觉 prompt 减负 | `services/vision_verify.py` | 文字行从「逐字照抄 150 字」→「只抄最关键 1 句（≤30 字）」；`VISION_OCR=0` 自动退回旧 prompt |
| report 标来源 | `services/vision_report.py` | `text_blocks[].source=ocr/vlm`；prompt 行写「（OCR）」或「（识图模型读的，可能不准）」 |
| 依赖 | `requirements.txt` / `Dockerfile` | `onnxruntime==1.18.1`（1.32 会强拉 numpy>=2，imageutils 要 <2）；rapidocr 本体 `--no-deps` 装，避免它把 opencv-headless 换成带 GUI 的 opencv-python |

**3. 视觉默认改 qwen，第二家变成「可选」**

- 用户拍板：GLM 额度用完，先靠 qwen；但保留可选视觉模型的能力。
- `services/providers.py`：`vision` 默认 glm→**qwen**；新增 `vision_second`（默认空 = 关闭，
  不拿 `LLM_PROVIDER` 兜底）；`vision_fallback` 默认 qwen→glm。
- 新增 `services/vlm.py`：任意 role 的通用视觉调用（OpenAI 兼容）；第二视觉源不再写死 `glm.py`。
- `VISION_GLM_MODE` → `VISION_SECOND_MODE`（旧名保留兼容）；日志/报告里的「GLM识图」改成实际 provider 名。
- `.env` 新增：`VISION_OCR=1`、`OCR_RAPID_MAX_SIDE=1024`、`OCR_RAPID_MIN_CONF=0.5`、`OCR_RAPID_TIMEOUT=8`、`OCR_RAPID_THREADS=2`、`VISION_SECOND_MODE=on_miss`。
- ⚠️ 新增的是 `services/ocr_rapid.py`：`services/ocr.py` 已存在（ocr_lite 旁路工具），没有覆盖它。

**4. 验证**

- `check_tuning` PASS、`audit_coverage --strict` 0 硬伤、`check_runtime_wiring` **62 PASS / 0 FAIL**；
- 端到端（真图 + 真 qwen）：`text_source=ocr`、4 块、report 出现「图里文字（OCR 原文）」；
- 启动 0 Traceback；`[OCR] RapidOCR 已加载（threads=2 max_side=1024）`。

## 2026-11-27（第四十五轮：收发件人 v2 设计定稿 + P0 落地）

**1. 设计先行（用户要求「先落文档，检查没问题再写代码」）**

- `docs/addressing-design.md` 新增 **§7 v2**（v1 原样保留）：大纲 + 4 张图 +
  收件人四级/动作矩阵、话题漂移、意图类型、ImageReport、自检、预算、开关、验收；
- 写完后做了一次**锐评**，初稿 10 处想当然的地方全部改掉（§7.12.1 有表），其中三条最关键：
  1. `level` **只作用于「主动插话」**——正常回复时消息已入队，拦不了（`need_reply` 是流水线内算的）；
  2. 初稿 L0 里「自动补一句回答 / 换个开头重说」不是规则能做的 → L0 只做机械修正；
  3. 「P0 就删视觉 prompt 的逐字抄」会让截图类消息**变瞎子**（服务器还没 OCR）→ 推迟到 P1；
- `docs/vision-pipeline.md` 顶部加一行指向 §7.6（不新增文档）。

**2. P0 代码（等价重构 + 只加一行，默认不改变现有行为）**

| 改动 | 文件 | 说明 |
|---|---|---|
| 新增 `ImageReport` | `services/vision_report.py` | OpenCV 算清晰度/文字区域占比；`build/render_line/brief/should_audit`；不调模型、不写盘 |
| 识图接入 | `plugins/vision_agent/__init__.py` | 一次产出 report，三处复用：`fake_text`（加「画面质量」一行）/ 群聊上下文摘要 / 缓存；审核只在「有字 + 字里有 @/称呼」时才送 |
| 第二家视觉按需 | `services/vision_verify.py` | `VISION_GLM_MODE=on_miss`：trace 命中或 qwen 已给专名就不再问 GLM（`always` 可回退） |
| 意图类型 | `services/atmosphere.py` | 同一次调用多出 `kind`（ask/vent/meme/statement/comfort/command），渲染成一行「这句是：…」；白名单校验 |
| 参数 | `core/tuning.py` | `VISION_REPORT=1`、`VISION_GLM_MODE=on_miss`、`VISION_TEXT_MAX=60`、`ATMO_KIND=1` |
| 校验 | `scripts/check_runtime_wiring.py` | 新增 9 条断言（report 字段/文字来源/brief/审核门槛/kind 白名单与渲染） |

**3. 真机验证（21:18 上线，21:26 取样）**

```text
[VISION] report sha=2dff6ee668c9cc73 sharp=7940(clear) text=无 glm=on_miss cost=11126ms
[ATMO] 100000016 气氛=日常｜类型=ask｜意图=yes（带⭐且在直接问她）｜直接回应她的问话，别绕开
[ATMO] 100000016 气氛=日常｜类型=statement｜意图=no（最后两条都不是对她说的）
prompt 里：· 这句是：ask（正面答，别绕）
```

- `check_tuning` PASS、`check_runtime_wiring` **62 PASS**、清单 **126/126**、0 Traceback；
- **实测基线：单张图识图 11.1s**（trace + 视觉 + 搜图）→ 这正是 §7.8 里「先量基线再定目标」的原因，
  P1 要把「文字」从视觉模型挪到 RapidOCR、并把 GLM 调用次数压到 0~1。

**4. 本轮没做（按设计文档的 P1/P2）**

L0 机械自检、L2 观望、话题漂移（要先用 trace 标定阈值）、RapidOCR、异步自检回填。

## 2026-11-27（第四十四轮：输出上限 4000 / 仓库与服务器对账 / 脱敏后首次提交）

**1. 主聊天输出上限 1200 → 4000（用户：「不要 think 占满输出」）**

- `ROLEPLAY_MAX_TOKENS` 默认 4000（`core/tuning.py`），`.env` / `.env.example` 同步写死 4000；
- 长答模式 `LONG_DOC_MAX_TOKENS` 2000 → 4000（同一个模型、同一个 thinking 问题）；
- `services/env_guard.py` 启动体检：阈值 2048 → 4000，且不再只看 `GLM_THINKING` ——
  `model` 里带 `reason/think/flash` 的推理型模型也会提示；
- 实测容器内 `providers.resolve('roleplay')['max_tokens'] = 4000`，启动日志
  `[ENV] 启动配置体检通过`（不再告警）。

**2. 仓库 ↔ 服务器对账（`rsync -c` 全量比对）**

48 处差异，逐条看过：

| 类别 | 数量 | 处理 |
|---|---|---|
| 只在本机（`comfy_workflows/` `config/` `node_config/` `setup_*.ps1` `subscribe.txt` `.xray_runtime/xray.exe`） | 12 | 属算力机/本地资源，服务器不需要，保持只在本机（`xray.exe` 已 gitignore） |
| 只差文件末尾空行（`plugins/third/*` 15 个 + `pixivrank_search` 3 个 + `UPSTREAM.md` 7 个） | 35 | 用**仓库版**对齐服务器（纯空白，无行为变化）；`UPSTREAM.md` 仓库版还更准（补了 girls_2d 的上游地址） |
| 真实差异：`plugins/nonebot_plugin_tavily_search` | 1 | 仓库版多了 `/技能 关 搜索` 的 `skill_switch.gate(...)`，服务器是旧版 → **已补上服务器** |

对账后 `verify_deploy` 125/125、`verify_all --full` 11/11。

**3. 脱敏（首次提交前）**

- 真实服务器 IP（文档 3 处、`node.py`、`scripts/xray_proxy.py` 示例、`patches/all.patch` 3 处）
  → `YOUR_SERVER_IP` / `127.0.0.1`；
- `node.py` 的 `SERVER_IP` / `API_SECRET` 不再有硬编码默认值（旧的弱默认密钥已删），
  缺配置时启动会打印明确提示 → 算力机在 `node.env` 里配；
- `scripts/make_dist.py` 的 `PRIVATE_HOSTS` 改为 `DIST_PRIVATE_HOSTS` 环境变量；
- `test.py` 的 `API_SECRET` 改为读环境变量；
- `.env` / `.env.working` / `data/` / `node.env` 继续 gitignore（只提交 `.env.example`，其中全部为空值）；
- `.gitignore` 新增根目录噪音清单（`3a_zh.txt` `d3.txt` `e1.txt` `v1.txt` `jy.tex` `help.txt`
  `t_*_zh.txt` `test.py`）和 `patches/*.tar.gz`；
- ⚠️ **发行前扫描发现 `scripts/subscribe.txt`（68KB，base64 里是真实 vmess/ss 节点 + UUID）
  在上一版发行仓里就已经存在**。本次已从发行仓删除，并加进 `make_dist` 排除清单 +
  `.gitignore`；**那批节点凭据建议尽快轮换**（历史 commit 里仍能看到）。

## 2026-11-27（第四十三轮：send 工具把 `<sticker:吃瓜>` 当话发出去）

**证据（NapCat + bot 双份日志，不是猜）**

```text
napcat 18:03:58  大肥鱼 | 发送 -> 群聊 [闲聊的书友群(100000016)]
                 这群已经变成图片展览馆了 |||<sticker:吃瓜>
bot    18:03:59  [TOOL] send({'text': '这群已经变成图片展览馆了 |||<sticker:吃瓜>'}) → 发出去了
```

第四十一轮的 `clean_outgoing()` 只剥了方括号协议（`[[TOOLS|…]]` / 裸标记 / 动作括号），
**没管 `<sticker:…>` 和 `|||`** —— 这两样只有 `parse_reply`（正文路径）会处理，
send 工具的 text 参数直接进 `MessageSegment.text()`，于是原样进群。

**修复**

1. `aura_logic.clean_outgoing()` 补三类发送标签：`<sticker:…>`、`<reply:…>`/`<at:…>`/`<poke/>`、
   `<voice>…</voice>`（保留里面的话），并把 `|||` 换成换行（气泡分隔符不能外发）；
2. 新增 `aura_logic.extract_stickers()`：send 工具先把 `<sticker:吃瓜>` 摘出来，
   **真发一张表情**（`send_sticker_by_tag`，精确→近义→静默跳过）；
3. 只有表情标签（洗完是空）时不再被当成「协议标记」拦掉；表情库没有该 tag 时
   回模型「库里没有，别跟对方说你发了表情」，避免「说了没做」。

**校验**：`check_runtime_wiring` 新增 4 条（`<sticker:>`/`|||` 不外发、标签能摘出、
只含表情不被拦、`<voice>` 保留正文）；表情库实测有 9 张「吃瓜」，会真的发出去。

## 2026-11-27（第四十二轮：意图理解层「只加不减」+ 离线评测脚本）

用户拍板「两个同时做，一起测」：把 11-26 的「读气氛」升级成「读气氛 + 判意图」，
同时把「到底准不准」做成可复现的离线评测 —— 不靠手感开闸门。

**1. 意图层（`services/atmosphere.py`，同一次调用，不增加一次请求）**

- 输出从 5 个字段扩到 `mood/who/feel/others/tip` + `topic/asks/need_reply/why/replied/keywords/queries{memory,library,web}`；
- **只加不减**：老四行照样在，新四行追加（`ATMO_INTENT=1`；设 0 退回 11-26 的块）；
- `ATMO_MAX_CHARS` 240 → 400（不截掉最后两行）；
- `read()` 新增 `text=`（当前这句）和 `replied_mids=`（8 行历史补〔已回〕）；
  以前只读历史、不含当前这句 → `need_reply` 判的不是她要回的那条；
- `/测函数 气氛` 兼 `/测函数 意图`，状态里带意图/检索词/闸门开关。

**2. 检索词真的驱动取数（`services/reply_pipeline.py`）**

- `queries.memory` 优先当记忆/档案库检索词；`queries.library` 非空 → 也查图书馆；
- `queries.web` 只渲染一行「可能要现查：X（要就自己搜，别编）」，不替她发请求；
- `/测函数 流水线` 多一行「意图：need_reply=…｜在问：…｜检索词 …」；
- 顺手修掉真机 17:54 抓到的重复标题：`称呼` 块不再出现「▍称呼｜…」+「▍你俩的称呼」两个标题。

**3. 插话闸门（默认关）**

- `ATMO_GATE=0`（tuning 默认）：打开后主动插话时 `need_reply=no` → 这一轮不开口；
  只挡插话，不影响 @ 她的正常回复；缓存超过 TTL 就不挡。
- 代码已接（`plugins/chat` 插话循环），但**没有评测达标前不许开** —— 见 DESIGN §8.1。

**4. 离线评测（`scripts/eval_intent.py`）**

- 用 `data/llm_trace` 重放，**和线上同一条 prompt**（`atmosphere.build_prompt`）；
- 弱标签：正例=她当时回了的那条（优先 ⭐），负例=同窗口里她当时没接的历史行；
- 输出准确率 / 严格准确率 / 正例召回 / 精确率 / F1 / 混淆矩阵 + 判错样本；
- `--mock` 不花钱（验证解析/打分）；真跑：`--trace data/llm_trace/2026-11-27.jsonl --limit 204`；
- 门槛：准确率 ≥85% 且正例召回 ≥85% 才建议开闸门。

**首轮实测（2026-11-27 上午 192 条样本 / 110 正例，真模型）**

```text
准确率 96.9%｜正例召回 100.0%｜精确率 94.8%｜F1 97.3%（TP=110 FP=6 TN=76 FN=0）
```

评测过程中修掉两个真 bug（不是调参）：

1. 一段 20 条时 ⭐ 常在第 3~5 条，`text[:800]` 把它截掉 → 新增
   `atmosphere.compact_segment()`：只留「在叫她的 + 最后 2 条」，段头写「其中 N 条在叫你」；
2. `"→ 你" in line` 会把昵称「你已被管理员…禁言6小时」误判成「在叫她」→ 改
   `_ADDR_RE = →\s*你(?=[：:]|$)`（eval 里同一份正则）。

6 条误报里 4 条是弱标签噪音（正文点名「鱼宝/大肥鱼」但那一轮她没回），真错约 2 条。
**闸门仍保持 0，等用户拍板。**

**校验**：`check_runtime_wiring` 新增 10 条（向后兼容 / 归一化 / queries 过滤 /
只加不减 / 闸门默认关 / replied_mids 下传 / 评测 1 正 2 负 / 打分）；
`check_design_docs` 新增第 ⑧ 条叙述断言（`ATMO_GATE` 默认 0 + 文档必须写评测门槛）。

## 2026-11-27（第四十一轮：工具调用降级 —— 不许把调用写法当话发出去）

**根因（有日志，不是猜）**

```
11-27 16:16:34 [TOOL] send({'text': '[[TOOLS|画图与搜图]]'}) → 发出去了
（你刚才已经通过工具把这些内容发出去了：[[TOOLS|画图与搜图]]）
```

模型把「工具目录标记」当成话交给了 `send` 工具。`[[TOOLS|…]]` 只由
`_fulfill_requests`（处理**正文**）展开，**send 的参数没走这条清洗** → 标记原样进群。
工具不可用/超时时，模型更容易乱抓一个标记当「解释」发出去。

**四层修复**

1. **出站清洗收口**：新增 `aura_logic.clean_outgoing()` —— 剥协议标记
   （`[[X|…]]` / `[[X]]`）、**裸标记**（`TOOLS|画图`）、动作括号（`（笑）`）。
   `send` 的参数和「发送前最后一道」都用它。
2. **只收到标记就不发**：`_tool_send` 清洗后为空 → 拦下，回模型
   「这是给你自己看的协议标记，不是要发的话；要说话直接写」。
3. **调用前预检（降级）**：`llm.set_availability(fn)` 由 chat 注入 ——
   检查 `/技能` 开关 + 算力机心跳 + `caps`。不可用的工具**不挂**，
   并在 prompt 里加一句「这些工具现在用不了：X（原因），别调用、别假装做到了」。
   算力机能力映射：`draw/tts/cover/rvc/split/video_understand`。
4. **运行期失败**：handler 抛异常 → 回 `【工具失败】…这一轮不要再调它；也不要跟对方说已经做好了`，
   并记进本回合失败集合，**再调直接拒绝**（避免「越失败越重试」）。

**实测**

- 单测（容器内）：预检过滤 ✅、失败文案 ✅、二次拒绝 ✅、清洗 3 例 ✅
- 真机 17:32:36：`[TOOLS] 本轮不挂 1 个：video_understand(算力机没上报 video_understand)`
  → 摘掉后正常回复（11.9s，0 报错）
- `verify_all --full` **11/11**；清单 **124/124**

**回滚**：服务器上留了 `services/llm.py.bak_1729`、`services/aura_logic.py.bak_1729`、
`plugins/chat/__init__.py.bak_1729`。

## 2026-11-27（第四十轮：私聊 20 拍板 + 引用串线修复 + 图片文字插件）

**1. 私聊保持 20（用户拍板）**

- 不改 40。文档写清：`PRIVATE_MAX_HANZI` / `OUT_BUBBLE_CHARS` 都是
  **「超过就发合并转发」的阈值，不是字数上限**（`plugins/chat` 里
  `>阈值 → _send_long_as_forward`，转发失败才 `limit_hanzi` 砍字）。
  所以「私聊 20」= 私聊超过 20 加权字就走卡片，话不会被砍短。

**2. 引用串线（真 bug，已修）**

- 现象（用户）：「我说了一句话，之前有个人 @ 她，她接了我的话，
  却引用了上次 @ 那个人的消息」。
- 根因：`plugins/chat` 自动引用兜底无条件取「段内最后一条 @ 她的」
  （`_mens[-1]`），哪怕那之后别人又说了话、她实际回的是后面那句。
- 修：只有「@ 她的那条**就是段内最后一条**」才自动引用；
  最后一条是「引用她」→ 落到 `ev.is_tome()` 引最后一条；
  最后一条是别人普通插话 → **不自动引用**（让模型自己写 `<reply:>`）。

**3. 图片文字：`services/ocr.py` + `plugins/ocr_lite`（新）**

- 服务器只有 OpenCV（无 tesseract/paddleocr），而瓶颈是「小字/低对比度送进视觉模型糊了」。
  所以做的是：**宽度不足 1400 就放大 → CLAHE + 锐化 → 形态学找文字块 → 逐块裁剪再放大**，
  再交给现有视觉 provider（主 provider 429/5xx 自动换 `vision_fallback`）。
- 注册成工具 `ocr_image`（scope=private+group）：不传参数就读**当前消息里的图**，
  也可以传路径/URL；归到 `skill_switch` 的 `vision` 组（`/技能 关 视觉` 能关）。
- 实测（容器内，一张 900×420、字号 0.35~0.7、灰字的测试图）：
  `ok=True blocks=4`，四行全读对（含最低对比度那行 `OCR_TEST_MARKER_42`）。
- 新 tuning 键：`OCR_MAX_SIDE=2200` / `OCR_UPSCALE_MIN_W=1400` /
  `OCR_MAX_CROPS=3` / `OCR_TIMEOUT=45`。

**校验**：`verify_all --full` **11/11**（含「设计文档」闸门）；清单 **124/124**；
`audit_coverage` 开关未覆盖 0。

## 2026-11-27（第三十九轮：第二轮外部审计 12 条复核 —— 全是文档滞后，9 处已改）

完整逐条判定见 [design-audit-response-20261127b.md](design-audit-response-20261127b.md)。
**结论：7 条真（文档滞后）、3 条部分真、2 条理解问题；没有一条是代码行为错。**

**真问题（已改文档）**

1. §0.1「`tuning.UNWIRED` 已清空」→ 实际还剩 `AFFINITY_GAIN`（设计先行键）；
2. §8「`mood_judge._step` 是待收口双真相」→ 2026-11-26 就改成 `tuning.f()` 了；
3. §3.3「15 token + 3 字符」→ 实际 **13 + 5**（`long_doc_*` 11-27 从 ×1.5 改 ×1.0）；
4. §3.3「风格 ≤730 字符（token 约 487）」→ 单位说反：**730 是 token 值，字符上限 1095**；
5. §3.2 v1 公式 `×trust_speed` → 代码是 `×T(ts)`，`T=0.6+0.8×ts`；
6. §3.1 全局锚点少写 `×GLOBAL_SHARE(0.35)`；
7. §9 红线「单泡 ≤40 字」→ **私聊生成侧仍 `PRIVATE_MAX_HANZI=20`**，已标注「群聊 40 / 私聊 20」。

**自查额外修 2 处**：§3.1 静默锚点「30min」→ 实际 35min（2100s）；§3.3 第 320 行仍写
「2200 = live 总池」，与同节 261-268 行的警告矛盾 → 改「动态块池」。

**部分真（结论过强，已补说明）**

- 「卡尔曼沦为残差」：`P` 管的是**脱敏**（连续判读 → K↓ → 同样的话影响递减），
  且 `dv` 喂好感尾部（一见钟情/心碎）—— 复杂度高但每块有读点。已补进 §3.1。
- 「情绪自指环正反馈」：文档确实只写了「防回声」，没讨论失控风险 → 已进 §8.1 已知债
  （靠半衰期/P 衰减/锚点被动限速，**没有主动刹车**）。
- 「预算名存实亡」：261-268 行早写了警告，是 320 行旧措辞自相矛盾（已改）；
  「默认 CTX_ENFORCE=0」是 tuning 默认，**线上 .env=1**。

**理解问题**

- 「五六条写入口」：`bump_by_emotion` 默认**只写 label 不动数值**、`judge_message_mood`
  只在回滚模式用、`fast_guard` 线上无调用（文档已注明只被 test 用）→ 默认确实只有一条数值链路。
- 「v2 默认关却背 30+ 参数」：实际 51 个键，属实，但 DESIGN 55/238 行已写明「默认 v1、实验特性」
  → 是设计债，已进 §8.1。

**新增 §8.1「已知债」**：v2 参数 / `fast_guard` / 回滚分支 / 预算 enforcement 边界 /
正反馈风险，每条写清「现在的真实状态 + 还债前提」，避免下次再被当成「文档撒谎」。

**`check_design_docs` 新增「叙述型事实」断言（7 条，默认模式就跑）**：

`tuning.UNWIRED` 的数量/键名、`mood_judge` 是否还有硬编码步长、`ctx_budget` 的
13 token / 5 字符分类、`style_total` 字符上限 1095、全局锚点 `GLOBAL_SHARE`、
静默锚点 35min、私聊 `PRIVATE_MAX_HANZI`。
负向测试：把文档里这 4 处改错 → 直接 4 个 FAIL（以前永远绿）。

**没改业务代码**；`check_design_docs --strict` PASS。
唯一待产品决定：**#12 私聊 20 字要不要也改成 40**（数据见下）。

> 补充数据（决定 #12 用）：今天 93 个私聊气泡里 **48 个（52%）加权字数 >20**，
> 而发送侧 `>PRIVATE_MAX_HANZI(20)` 会**走合并转发卡片**（转发失败才砍字）。
> 也就是说私聊有一半回复是「聊天记录卡片」，不是普通消息。群聊 50% >20 但阈值是 40（p90=35）。

## 2026-11-27（第三十八轮：按上帝视角复盘落 5 项「接歪/人称」修复）

复盘见 [reply-audit-20261127.md](reply-audit-20261127.md)（204 条群回复，人工看 30 条）。
这一轮把里面「便宜且低风险」的全做了。

**prompt 层（4 句，`services/segments.py` 段头）**

1. `<reply:>` **只能引本段里的消息号**；更早的消息用名字提，别引用
   （引了会被系统丢掉，还容易答成旧话题 —— 真机 11:02 就是拿窗口外的 mid 答旧问题）。
2. **没有 ⭐ 就是你在插话**：只接最后 2 条里的话题；最后两条是图片/表情就当话题断了，别翻旧账。
3. **插话时不要用「你」**，用「他/她/名字」。
4. `[[PROFILE|我]]`（或名字留空）= 查**说话人自己** —— 群友打「查看档案/我的档案」不会说查谁。

**代码层（3 处）**

5. **插话段裁短**：`segments.format()` 检测到整段没有 ⭐ 时，只给最后
   `SEG_INSERT_WINDOW=5` 条（新 tuning 键）。以前给 13 条，她容易捡起前面早就翻篇的话题。
6. **昵称含人称代词 → 加「」**：`services/text.render_name()`。
   `我的床会长猫`、`请给我很多很多token`、`你已被管理员…禁言6小时` 这类昵称
   直接写成 `名字 → 大家: …` 会被模型当成句子（人称直接错）。
   `clean_name` 也一起收口到 `services/text.py`（group_scene 保留同名导出）。
7. **`[[PROFILE|]]` 不再空转**：`_pname` 为空/「我/自己/本人」时直接取 `TOOL_CTX["speaker_qq"]`，
   群友说「查看档案」能真的出卡片，而不是只写一句 `[[THINK|又是查档案]]` 然后沉默。

**校验**

`check_runtime_wiring` 新增 4 条断言（插话段裁到 5 条 / 有 ⭐ 不裁 / 昵称加引号 / 普通名字不加），
共 **34 PASS**；`check_names` / `check_tuning` / `audit_coverage` / `check_design_docs` 全 PASS；
容器内 `verify_all --full` 10/10。

**没做（等产品决定）**：`查看档案/看档案` 加进 `/档案` 的 aliases ——
加了这些词就不再进聊天，她就不能拿它们开玩笑了。

## 2026-11-27（第三十七轮：日预算刹车 + 24h 标签审核修两处误导）

**1. 日预算刹车（用户要求：200w token / 天）**

- 新增 `services/budget_guard.py`，四档：
  `off`（预算 0）→ `ok` → **`soft`（90%，她照常回，但 prompt 加一句「这轮说完就说要休息」）**
  → **`rest`（100%，主聊天不调模型；每个会话当天第一次回一句固定话术，之后静默）**。
- 硬停不只拦主聊天：`mood_judge`（占 ~24%）和 `scene_auditor`（~7%）在 `rest` 档也直接跳过，
  心情/标签停在原地，明天恢复 —— 这样「后续不消耗」是真的，而不是只省掉 55%。
- 每会话每天只通知一次（`data/budget_notice.json`，跨重启有效），避免刷屏。
- 生效日期 `LLM_BUDGET_START_DATE=YYYY-MM-DD`：设成明天 = 今天不刹车
  （避免「当天已经用超了、一上线就闭嘴」）。
- `/测函数 用量` 多一行：`日预算 x / y（z%）｜档位 soft｜生效日 …`。

**2. 24h 标签审核（`docs/tag-audit-20261127.md`，抽样 475 行）**

- **真 bug（已修）**：`event.is_tome()` 在 NapCat 上不只为「@ 她」为真（引用她也真、实测 @ 别人也出现过），
  旧代码只要 `is_tome` 就把她插到 @ 名单最前 → 「萌萌香 @冬眠耗子」被记成 **`→ 你`**，
  她会以为整段都在跟自己说。改成「@ 名单里真有她才排第一；@ 别人就保留别人；没 @ 才兜底」。
- **零宽字符（已修）**：17% 的行发言者名字带 U+2067 之类不可见字符（`下个路口见⁧⁧喵⁧⁧`），
  新增 `group_scene.clean_name()` 在**入库 + 渲染**两处清掉，正常中文/emoji 不动。
- 其余记录在案：同一 QQ 多昵称（18 个）、纯命令词标 `→ 你`（合理）、
  `→ 你` 里 67% 没有 @（玩梗软命中，观察）。
- 〔已回〕标记正常（今天 trace 里 278 次），`→ 大概X` / `→ X?` 在样本里 0 次。

**3. 校验**

`check_runtime_wiring` 新增 8 条行为断言（预算 4 档 / 通知每会话一次 / 话术非空 / 昵称清洗），
共 30 PASS；`check_names` / `check_tuning` / `audit_coverage` / `check_design_docs` 全 PASS。

## 2026-11-27（第三十六轮：清 5 个死函数 + 接 3 个「文档承诺了没实现」）

`audit_wiring.py` 的 9 个零引用公开函数逐个查了「当初为谁写的」，按结论处理，
不留「写了没人用」也不留「删了才发现有人要」。

**删（5 个，都有更准的替代 / 是冗余包装）**

| 函数 | 当初目的 | 为什么删 |
|---|---|---|
| `group_scene.qq_of_name()` | 白月光统计「她 **@** 的是谁」：显示名 → QQ | 后来走 `find_by_mid()`（引用消息号 → 现场记录，不怕改名重名）；取名走反向 `profiles.call_of(qq)` |
| `memdb.style_samples_of()` | 从 sqlite 影子表取 N 句做 MMR | MMR 真实现在 `style_vault.samples(diverse=True)`（数据源 jsonl），这是**第二份实现** |
| `memdb._style_sim()` + `STYLE_MMR_*` 在 memdb 的读点 | 上面那个函数的相似度 | 随上面一起删（`STYLE_MMR_*` 仍由 style_vault 读） |
| `memdb.threads_of()` | 线程镜像 `upsert_thread()` 的读端 | 现场线程一直在内存 `group_scene._THREADS`，重启靠 `seed_from_log()`，没人查 sqlite |
| `mood.state_word()` | v → 心情名词 | `label_for()` 在 decay/observe 时已把名词写进 `rec["label"]`，`/测函数 心情` 直接读它 |
| `reply_pipeline.last_text()` | 「给 `/看模板 流水线` 用」 | 那条命令用的是 `dump_text()`（已含全文） |

**接（3 个，都是「文档/注释承诺了、代码没做」）**

1. **`llm_usage.budget_exceeded()` → 真的日预算告警**：以前只有 `report_text()` 里
   显示「今日预算 x/y」，而 `help_menu/topics.py` 写着「超了会在日志告警」——
   承诺是空的。现在 `record()` 记完账顺手检查（**每 5 分钟最多一条**，
   新增 `today_used()` 复用），超了打 `[USAGE] 今日 token 预算已超：…（只告警不拦请求）`。
   拦请求会让聊天突然不吭声，所以只告警。
2. **`mood.set_sensitivity()` → `/测函数 心情 敏感度 [1.3]`**：以前 `sensitivity()`
   有真实调用（`mood_judge`），但**没有任何写入口** → 永远 1.0，旋钮是死的。
   现在可以看/设（夹在 `SENS_MIN~SENS_MAX`，默认 1.0）。
3. **`relationship.audit_text()` → `/测函数 关系 [QQ号]`**：docstring 一直写着
   「给 `/测函数 关系` 用」，但这条命令从来没存在过。现在能看最近 10 条
   信任/尊重/好感事件流水 + 当前四量。

**保留 1 个**：`memdb.profile_of()` —— 检索二期读 sqlite 档案镜像时用，写端
（`upsert_profile`，2 个调用点）已经在跑。

**验证**：`check_names` / `check_tuning` / `audit_coverage --strict` /
`check_design_docs --strict` 全 PASS；`audit_wiring` 纯死代码 9 → 1（只剩
`profile_of`）；真机 `verify_all --full` 10/10。

## 2026-11-27（第三十五轮：把第三十四轮剩下的设计缺口全部落到代码）

第三十四轮把 3 条设计缺口「只写清、不改行为」，这一轮全部接线，并用
**直接 import 运行时的行为断言**兜底（不再只比「文档 ↔ 常量」）。

**1. long_doc 证据 6000 / 图书馆 3000 真正接线**

- 新增 `services/long_doc.py`：`ENABLED / MAX_TOKENS / EVIDENCE / PIECES=3 /
  PIECE_CHARS=1900 / query_of() / is_long_doc()`。判定逻辑从 `llm.py` 抽出，
  `llm.py` 保留同名别名，`reply_pipeline`（线上真正的取数入口）读同一份，
  不再有第二份真相。
- `prompt_builder.build_packet(long_doc=True)`：`memory_index` 上限从
  `CTX_EVIDENCE=500` 放开到 `CTX_LONG_DOC_EVIDENCE=6000`，`library` 放到
  `CTX_LONG_DOC_LIBRARY=3000`（都是**字符**口径，`ctx_budget` 倍率 1.5→1.0），
  并按 3 篇 × ≤1900 渲染**正文**，不再只给 40 字目录。
- `reply_pipeline` 的记忆 / 图书馆两个阶段同样按 `long_doc` 取预算。
- 新参数 `LONG_DOC_PIECES` / `LONG_DOC_PIECE_CHARS` 进 `core/tuning.py`。

**2. `±3 一天最多一次` 从 prompt 纪律变成代码计数**

- `mood.cap_daily_impact(session_key, impact)`：按**会话 × 自然日**计数，
  超 `MOOD_IMPACT3_DAILY_MAX=1` 后降到 `MOOD_IMPACT3_OVER_TO=2`（**降档，不丢弃**）。
- `mood_judge` 调用它，trace 里同时留 `impact_raw`（原判）与 `impact`（实际用），
  日计数 `impact3_day / impact3_n` 随衰减保留。
- 设为 0 可回滚成「不限」。

**3. 锚点硬死区 → 软死区（smoothstep）**

- `mood._anchor_rec` 用 `r²(3−2r)` 平滑权重：`|diff|→0` 权重→0、`→0.15` 权重→1，
  边界处与全速连续；不再是「差一点点就完全不动」的 bang-bang。
- 新增 `_anchor_diff()` 供诊断（`/测函数 心情` 与单测用），DESIGN 注明
  「仍是每窗 EMA，不是一次性对齐」。

**4. 新增 `scripts/check_runtime_wiring.py`（22 项行为断言）**

直接 `import` 运行时模块断言行为，补上「文档↔常量自证」的盲区：

- long_doc 两个预算 = 6000/3000 字符、`3×1900 ≤ 6000`、记忆块 **>500 且 ≤6000**、
  块里真有正文；常态块仍是 ≤500 的目录；
- `±3` 第一次生效、同日第二次降 `±2`（正负都测）、计数按会话隔离、`±2` 不受日限；
- 锚点软死区内仍会挪、0.149/0.151 两侧连续（差 <0.005）、小抖动位移 <0.002；
- `P` 回补只补不降、能从地板抬起；
- `mood.observe` 有真实调用点、且不被 `_skip_update` 整条挡住；
- 整份 prompt 有 `prompt_total` 记账、超 `soft_cap` 会记超限。

**5. 文档对齐**

- DESIGN §0 的 long_doc 从【半接线】改【已接线】，写明 `services/long_doc.py`
  与字符口径；§3.3 与 ADR-039 同步。

**6. 上线时 `verify_all` 抓到的两处红（已修）**

- **`llm.py` 引用了未定义的 `_prompt_chars`**：外层 `except` 把它吞成
  `[WIRE] 接线快照写入失败`（debug 级），结果 `data/pipeline_last.json`
  **从加上 `prompt_chars` 那天起就没写成功过**，`/测函数 链路` 一直是旧数据。
  现在按「所有 message + 工具 schema」真实计算，并顺手接上
  `ctx_budget.observe_prompt_total()`（之前只有单测在调它）；
  `/测函数 链路` 也补上这一行（`整份 prompt N 字（软上限 2700，超了只告警不裁）`），
  以前只落了盘、没人显示，等于还是看不见。
- **`audit_wiring` 把 `dep_trace` 误报成孤儿模块**：它唯一调用点在 `bot.py`，
  而脚本不扫 `bot.py` → 补上扫描。同时校准口径：**孤儿模块/工具链断 = 阻塞；
  零引用公开函数 = 提示不阻塞**（死代码≠接线断裂，删或接线是产品决定），
  但仍在输出里逐个列出，不许当「已接线」自证。
- **`audit_coverage` 把 LEGACY 键当「参数空转」**：`MAX_BUBBLES` 11-26 已合并进
  `OUT_BUBBLES_MAX` 并进 `tuning.LEGACY`，本来就该 0 读点，但闸门只排除了
  `UNWIRED` → 接线闸门一直红。现在 `LEGACY` 也排除（`UNWIRED`=未接线的设计键、
  `LEGACY`=已退休的键，两者都不该有读点）。

**仍记录在案**：9 个零引用公开函数 —— 已在同一天的**第三十六轮**逐个处理
（删 5 / 接 3 / 留 1），见上面那一节。

## 2026-11-27（第三十四轮：外部审计 16 条复核，12 真 8 修）

完整逐条判定见 [design-audit-response-20261127.md](design-audit-response-20261127.md)。

**真 bug（已修）**

1. `P_EVENT_CAP(0.20) < P0(0.25)`：旧写法 `min(max(floor,cap), p+boost)` 会把 P
   **调低**；改成 `mood.event_p_boost()`「只补不降」，加 2 条单测。
2. `relation_relevance < 0.25` 以前**连心情都不步进**，而 prompt 教模型 world/self 填 0 →
   她自己的情绪永远写不进 v。现在只挡好感/记忆/群心情，**心情与锚点照常步进**。
3. 全局锚点没乘 `GLOBAL_SHARE`（事件乘了、锚点没乘）→ 统一乘 0.35。
4. 静默锚点 `THINK_AGE=1800s` 与 `IDLE_MIN=30min` 完全相等 + tick 5min → 几乎死分支；
   改成 **2100s**。
5. `/测函数 心情` 读 `t["value"]` 但 trace 存的是 `v` → 永远 +0.00；已改。

**文档/常量错（已修）**

6. `ctx_total()=4620` 是 **token 值之和**，不是字符；新增 `ctx_total_chars()`
   （6540 / 7170），`check_design_docs` 用 `ctx_budget.limit_chars` 反算交叉校验。
7. long_doc 证据 6000 / 图书馆 3000 **没接线**（`observe` 返回值被丢、块上限 500、staged 4 条）
   → §0.1 改【半接线】，§3.3 注明「设计口径，未接线」。
8. `PROMPT_BUDGET=2200` 只约束动态块；persona/protocol 等 `CTX_ENFORCE=0` 不裁
   → §3.3 加显式警告。
9. §3.1 首条/稳态数字改成实测：conf=0.9 → 0.0906、conf=0.6 → 0.0683、稳态 0.0148。
10. 候选池「8000→400」实际是 **48**（`max(48, min(400, limit×8))`）→ 文档改。
11. §0「好感只吃 dv」与 §3.2「吃 Δ+dv」矛盾；§3.2 ±0.10 → **±0.15（AFF_V1_CAP）**。
12. `MOOD_HARD_CAP`「不触发」、`MOOD_MAX_DELTA` 假 LEGACY、`AFFINITY_GAIN` 只有赋值
    → 描述修正；`MOOD_MAX_DELTA` 移出 LEGACY；`AFFINITY_GAIN` 进 `UNWIRED`。

**设计缺口（先写清，不改行为）**

- `±3 一天最多一次`只靠 prompt 纪律，代码无计数 → DESIGN 标「已知缺口」；
- 锚点死区 0.15 让 EMA 变分段混入（稳态锯齿）→ 文档补说明，不改参数。

> 这 3 条缺口已在同一天的**第三十五轮**全部接线（`long_doc` 证据 6000/图书馆 3000、
> `±3` 日计数、锚点软死区），并加 `scripts/check_runtime_wiring.py` 做行为断言。

**理解问题（1 条）**

- 「锚点不是绝对测量」：判读看不到当前 v 是**反回声**的故意设计（ADR-029），
  不是 bug；改的是文档措辞。

## 2026-11-27（第三十三轮：输出 token 修复 + 识图切 qwen + 校验脚本按新设计校准）

**1. 主聊天输出 token 被写死 800（严重逻辑问题，已修）**

- 根因：`services/llm.py` 主循环 `"max_tokens": 800`、`_fulfill_requests` 重新生成也写死 800
  —— 改 `.env` 的 `GLM_MAX_TOKENS` / 任何输出配置都不生效；GLM 当主模型时 thinking 吃光 token，
  正文为空、无法回复。
- 修：输出上限进 `providers.resolve(role)`：
  - 优先级 `<ROLE>_MAX_TOKENS` > `LLM_MAX_TOKENS` > `GLM_MAX_TOKENS(provider=glm)` > 默认
    （主聊天 `ROLEPLAY_MAX_TOKENS=1200`，其它 `LLM_DEFAULT_MAX_TOKENS=800`）；
  - 按 provider 上限 clamp（GLM 4096 / DeepSeek 8192 / Qwen 8192 / OpenAI 16384）；
  - OpenAI 推理模型（o1/o3/o4/gpt-5）自动用 `max_completion_tokens`；
  - `config.ROLEPLAY_MAX_TOKENS/_FIELD` 暴露给 `llm.py`，主循环 / 补资料 / 写歌都用它；
  - `env_guard` 启动时提醒「GLM + thinking 但上限 <2048」和「超过 provider 上限会被 clamp」；
  - 辅助角色（判读/审核/会话状态/表达/档案挖掘/气氛）也改走
    `providers.call_tokens(role, wanted)`：GLM 开 thinking 时自动抬到 `GLM_MAX_TOKENS`，
    OpenAI 推理模型自动换字段，避免「返回空 JSON」。

**2. 识图暂时切到 qwen**

- 线上 `.env`：`VISION_PROVIDER=qwen`、`VISION_MODEL=qwen3-vl-plus`（GLM 余额仍不足）。
- ⚠️ `.env` 改动必须 `docker compose up -d --force-recreate bot`；`docker restart` **不会**重读
  `env_file`（第一次就是这么没生效的）。

**3. `verify_napcat_log.py` 按新设计校准（5 个 FAIL → 口径修正）**

- P4：窗口大小读 `MOOD_JUDGE_CTX`，并排除「预算只够 N 窗」的截断（P4b 已覆盖）；
- O5：允许 `OUT_BUBBLES_MAX + 1`（DESIGN §9.1 表情/语音例外）；
- S4b：50ms 是目标不是红线 → 改为性能观测项，不判 FAIL；
- M1h：过滤 `mood_history` 里旧格式（-100~100）行，避免量纲混算误报反向；
- M10h：`|impact|≥2` 回补 P 会让步长回升（用户拍板），跳过这类对比；
- 默认报告名改 `docs/napcat-log-check-YYYYMMDD.md`，不再覆盖手写 `verification-report-*.md`。

**验证**：`test_new_features` 本地 256/1（BILI 依赖）；容器内待跑；
`check_tuning` PASS；`verify_deploy` 119/119。

## 2026-11-26（第三十二轮：接话前读气氛 + 引用互刷修复 + 识图自动降级）

**1. 接话前「读气氛」**（用户：群友气氛很压抑、有人在哭，她忽然进来哈哈哈）

- 新增 `services/atmosphere.py`：读最近 **8 句标签语**（`group_scene.history_lines`，带 `谁 → 谁` / ⭐），
  由标签 LLM（`providers.resolve("audit")`，temperature=0）输出
  `mood / who / feel / others / tip`（整体氛围 / 谁在难过 / 其他人状态 / 接话注意）；
- `reply_pipeline` 新增第 **⓪ 步「气氛」**（与 ①②③ 并行，块放最前），`/测函数 气氛 [刷新]` 可看；
- **只作提示，不改心情/好感**；有人哭/说难受/告别时 prompt 强制 `mood` 落低落类；
- 缓存 `ATMO_TTL=90s`、超时 `ATMO_TIMEOUT=5s`、失败 60s 内不重试；5 个 `ATMO_*` 键进 `tuning`。

**2. 两个 AI 互相引用回复（17:08:49~17:09:13 月下橘引用她 4 次）**

- 根因：NapCat 的 `event.reply.sender` 经常是空的，`_quote_me()` 只读它 →
  引用她永远判 False → `_at_me()` 退回 `is_tome()`（它同时含「引用」）→ 引用被当成 @ 必回；
- 修：`_quote_me()` 改成三级判定（get_msg 记下的被引用者 QQ → `reply.sender` →
  被引用 mid 在群现场里 `is_bot`）；`_reply_quote` 顺手把 `_quote_qq` 记在 event 上；
- 护栏加强：同一对 120s 内引用 ≥3 次 → **冷却 5 分钟**（`[LOOP]` 日志带剩余秒数），
  冷却期内「引用她」一律不接（真 @ 仍必回）。
- **2026-11-27 真机复查补刀（更隐蔽的根因）**：`_at_me()` 第一行
  `if event.to_me: return True` 提前返回，而 `to_me` **同样包含引用她** →
  `_quote_me` 永远走不到。16h 日志：64 次引用她、软通道 0 次、`[LOOP]` 0 次。
  已改成「① 先看消息里有没有真 `at` 段指向她 → ② 再判 `_quote_me` → ③ 最后 `to_me` 兜底」；
  假事件验证：引用她 `_at_me=False`、真 @ `_at_me=True`。

**3. 识图「老是说看不到」**

- 根因：GLM 返回 `HTTP 429 / 1113 余额不足或无可用资源包`；旧代码遇到 429 直接 `return ""`，
  **备用模型和备用 provider 一次都没试过**；
- 修：429/5xx 改为「换下一个模型」，额度用尽/key 无权限把整家 provider 拉黑；
  新增 `vision_fallback` 角色（默认 qwen / `DASHSCOPE_API_KEY`，可用 `VISION_FALLBACK_*` 覆盖）；
- 去重修复：旧代码在**识别前**就写 `_seen`，失败后同一张图 24h 都判「已处理过，但没有缓存描述」
  → 现在**成功才写**，失败只清 pending（2 分钟内不重复触发），同一张图失败后可重试。
- **2026-11-27 真机复查补刀**：上次成功的 `qwen3-vl-plus` 被 `insert(0)` 进**主 provider**
  列表 → 拿 GLM 的 endpoint 去打 qwen 模型名（404）→ 再吃 GLM 429，每张图白烧 2 次。
  现在「cached 属于备用 provider 时，备用整体排前」；并去掉本账号 403 的
  `qwen2.5-vl-72b-instruct`（失败时白等 40s）。

**4. 图书馆（RAG）客户端：加 `rag_search` 第一兜底**

- 实测：重建索引后 `/api/agent/smart-query` 卡 **>70s**（LLM 规划层），
  而 `POST /api/agent/execute-tool?tool_name=rag_search` 1~9s 就返回、还带来源；
- `agent_client._recall_impl` 顺序改为：`recall`（404 跳过）→ **`rag_search`** → `smart-query`；
- `_rag_tool_facts()` 会把 RAG 转义的中文 `\\uXXXX` 还原、按「片段」切分、
  过滤掉整段是宋词/结构化文档的 JSON（`paragraphs + author`），每条截 160 字；
- 「通了但没命中」不再判「图书馆不可用 10 分钟」；只有两个端点都失败才暂停。
- 新键 `AGENT_RAG_TOOL_TIMEOUT=12s`。

**5. 图书馆边界：不把记忆搬进 RAG（用户 2026-11-26 纠正）**

- 记忆 = 本地 sqlite（`memdb`，按人/时间/情绪/标签）；图书馆 = **可选**外部语料/久远归档。
  两者不是一套库，也不该每天把 `data/dialogue` 同步过去（会重复、会顶掉真正久远的事、
  没搭 RAG 的部署行为不一致）。
- `scripts/library_sync.py` 加闸门：`--dir` 必须显式给；`data/dialogue`/`memory`
  默认**拒绝**，要放 >30 天归档必须 `--allow-memory --min-age-days 30`；
  分区默认从 `dialogue` 改成 `archive`。
- `docs/retrieval-design.md §10` 写清「RAG 可选、记忆不搬」；没 RAG 时 `agent_client`
  返回空并暂停 10 分钟，聊天/记忆/心情不受影响。

**6. C 方案：个人档案库（本地，独立于 RAG）**

- 新增 `services/archive.py` + `data/archive.db`（sqlite + FTS5）：
  只存 `ARCHIVE_MIN_AGE_DAYS=30` 天以前的旧对话/旧记忆，毫秒级，**不依赖 RAG**；
- 迁移 `scripts/archive_migrate.py`（幂等，按 `hash(gid|qq|ts|text)` 去重，不删源文件/源表）；
  `/测函数 档案库 [迁移]` 可看/手动搬；
- `reply_pipeline` 新增「档案库」块（和「记忆」并行，块里写明“只你自己能查”）；
- 新增 `[[ARCHIVE|关键词]]` 标记：查 30 天以前的旧事；`[[LIBRARY|词]]` 只查外部图书馆；
- 新键 `ARCHIVE_ENABLED/MIN_AGE_DAYS/TOP/MAX_CHARS`；
- 这样没有 RAG 的部署也有「近期记忆 + 个人档案库」，只是没有外部图书馆。

**未做**：算力机 `node.py` 本轮未改（用户手动拷贝合入验证）；RAG 索引构建仍在算力机后台跑。

## 2026-11-26（第三十一轮：文档一致性 + 图书馆客户端增强 + 工具收权限）

完整清单见 [design-consistency-audit-20261126.md](design-consistency-audit-20261126.md)。

**文档冲突（6 处，全修）**

| # | 冲突 | 处置 |
|---|---|---|
| 1 | `proactive-design` 说「`AROUSAL_*` 在 `UNWIRED` 里」 | `UNWIRED` 早空、`AROUSAL_LEVEL_*` 已接线 → 改成「已接线，但不进 `chatty`」 |
| 2 | `affect-and-response` 图里 `impact -2~+2` | 改 7 档 `-3~+3` |
| 3 | `affinity §2.12`「只有 5 档、尾部永不触发」 | 加注「那是 11-25 的现状；现在 7 档 + 回补 P 能到 0.135」 |
| 4 | `emotion-annotation-spec`「五档锚点」vs 线上 7 档 | 加警示：**人工标注集仍 5 档**（已标完别改），线上 7 档 |
| 5/6 | 锚点「每 20 轮」、`FTS5 + BM25` | 上一轮已修，本轮复核一致 |

**图书馆（RAG）客户端 3 处增强**（RAG 本体在算力机，没动）

1. `_RECALL_PROMPT` 从 `"{q}"` 改成注入**日期 + 说话人**（RAG 才知道「上周」「他」指谁）；
2. 新增 `clean_facts()`：**去重 + 丢掉「没有找到」行 + 每条截 160 字**；
3. 触发词补**明确时间回溯词**（上周/上个月/去年/前天/大前天/昨天/那次/当年/小时候）。

**`self_tool_run` 群聊只读（audit 待办 #4）**

`self_tool_write` / `self_tool_run` / 自造工具 `mytool_*` 全部改成 `scope=EXEC_SCOPE`（默认只私聊）；
`self_tool_file` 仍群聊可用。想放开：`SELF_TOOL_EXEC_SCOPE=private,group`。

**仍未做（已写清原因）**：工具收敛（36→20）、`proactive`×`stay_silent` 互斥、
语义标注跑完 200 条、图书馆评测补到 50 题。

---

## 2026-11-26（第三十轮：三个检索拍板落地 + 突发事件回补 P + 引用回复交给模型）

**三个拍板（按建议方案做）**

| # | 做法 |
|---|---|
| T1 时间窗 | `salience>=0.5` → **0.8**（只有极强刻痕才可能绕过），且绕过的候选打分 **×`MEM_OLD_FACTOR`(0.5)**。普通情绪记忆不再无视 30 天窗 |
| K2 关键词 | 查询侧**只用 bigram**（有 bigram 就丢掉单字），单字只在整条查询没有 bigram 时兜底。索引侧暂留单字（重建 FTS 要停写，留维护窗口） |
| K3 BM25 | 键名 `RET_W_BM25` 保留（改键名会让线上 `.env` 失效），DOCS + `retrieval-design` 正名为「token 覆盖率，没有 idf」 |

**突发事件回补 P（用户拍板，带刹车）**

稳态 `P` 长期贴 `P_floor≈0.005` → `K≈0.04` → `±3` 只挪 **0.012**，等于「突发事件不存在」。
现在 `|impact|≥2` 时 `P⁻ = min(0.20, P⁻+0.05)`：

| 稳态下 | 改之前 | 改之后 |
|---|---|---|
| `±1` | 0.007 | **0.007（不变）** |
| `±2` | ≈0.012 | **0.074** |
| `±3` | ≈0.012 | **0.135** |

封顶 0.20 是刹车：连刷 `±3` 时 `K≤0.63`、`±3` 最多 0.176，不会吃满 `HARD_CAP`。

**引用回复从硬逻辑交给模型（用户拍板）**

真机：两个角色扮演 AI 互相引用 → 无限对轰。根因是 nonebot 的 `event.is_tome()`
**同时包含「@她」和「引用回复她」**，而所有「必回」判定都用它。

- 拆成 `_at_me()`（纯 @，仍然必回）和 `_quote_me()`（引用她，**软信号**）；
- 引用她不再绕过冷却 / 纯问候 / 收尾语 / `GROUP_NEED_AT`，改为**进队列让她自己决定**
  （她有 `send` / `stay_silent`）；
- 新增结构化反循环护栏：同一对人在 120s 内互相引用 ≥3 次 → `[LOOP]` 不再硬接；
- `send` 工具加 `quote` 参数 —— 引用回复成了她自己的 function call；
- 协议 `▍02 接话` 加一句：「引用回复你 → 通常回一句；发现对方也是 AI 在互刷就停」。

**文档待办清理**：`mood-judge-audit-20261125.md` 的 6 条待办逐条核过状态
（2 条已做 / 1 条半做 / 3 条未做），`retrieval-memory-audit-20261126.md` 的
T1/K2/K3 标 ✅、C2 标「已知限制并说明为什么故意不改」。

---

## 2026-11-26（第二十九轮：记忆/检索全链路审计 + 清过期测试）

完整报告见 [retrieval-memory-audit-20261126.md](retrieval-memory-audit-20261126.md)。

**修掉的真 bug（4 个）**

| # | 问题 | 影响 |
|---|---|---|
| 1 | `recall_planned` 的 FTS 查询词 `sorted(qset)[:24]` 按**码点**截断 | 单字（的/了/我）挤掉 bigram → 中文关键词检索退化成「最近 N 条」 |
| 2 | `impression` 不在 `TAU_KIND` | 「印象最深的一件事」落到 `TAU_DEFAULT=30`，和一次性事件同寿命（补 `MEM_TAU_IMPRESSION=180`） |
| 3 | 目录 `total` 用全库 count | 看某个群/某个人的目录时「共 N 条」和 `has_more` 全错 |
| 4 | `dynamic=True` 时协议超预算从尾部裁 | `▍12 引用回复` 这类每轮规矩被静默裁掉 |

**顺手补的日志缺口**：`log_mood_event` 没传 `impact` → 库里全是 0，
「按档位分布」永远看不到 ±1/±2/±3。

**清掉的过期测试（6 条）**

- `t_memory` 3 条：memory 后端早已默认 sqlite，测试还在读 `index.json`
  → 现在显式 `MV.BACKEND="json"` 测**回滚路径**（sqlite 由 `check_memory_dual` 覆盖）；
- `t_protocol` 2 条：协议小节早已从「▍00~▍11 全都要有」改成「编号+名字，02b/02c/02d/07 已删」
  → 改成断言「必须有的 9 节 + 群聊 04 + 不含 07/12/13」；
- `selftest_all` 2 条：7 档扩档后 `|value|=0.4567` 落到 ±3（旧断言写 -2）、
  `AFF_V1_CAP` 从 0.10 提到 0.15。

**与 dsh 的对比**（详见报告 §5）：两者目标不同（我们解决跨月跨群「记不记得住谁」，
dsh 解决单次会话上下文过长），只抄了它的 `digest-guard` 与 `checkup` 两个点；
它的排序公式我没有源码，报告里已标明哪些是推断。

**验证**：`selftest_all` **1797/0/4 跳过**；`test_regressions` **99/0**；
`check_tuning` / `check_design_docs` / `check_names` PASS；容器内 `verify_deploy` 115/115。

---

## 2026-11-26（第二十八轮：上线 + 节流不再丢 THINK + 体检脚本）

**上线（15:29-15:40）**：`core / services / plugins / scripts / docs / deploy_manifest` 全量上传，
`docker compose up -d --no-deps bot` 重建 + 重启；容器内 `verify_deploy` **115/115 一致**；
启动无 Traceback；QQ 已连上。线上现在是 `AFF_MODEL=v1` + `AFF_V1_CAP=0.15` + `MOOD_ANCHOR_WEIGHT=0.45`。

**上线前发现的两件事**

| # | 发现 | 处置 |
|---|---|---|
| 1 | 容器里跑的是 11-25 22:03 的旧代码（今天两轮 ADR 都没上线） | 全量上传后 115/115 |
| 2 | `deploy_manifest.txt` **不在挂载里**，容器读的是镜像里那份 52 条的旧清单 → `verify_deploy` 一直报「52 文件 / 一致 0」，误导排查两次 | compose 增加 `./deploy_manifest.txt:/app/deploy_manifest.txt:ro`；`docs/deployment.md §5.3` 加「单文件挂载必须 `cp` 不能 `install`」的坑 |

**节流不再丢 THINK（用户实测：她每句都写，是 cd 隔离了）**

线上 6h：`无 THINK → 跳过判读` 94 次、`她没写 THINK` 54 次、真正写好感 14 次。
根因不是她不写，而是**链路把 THINK 丢了**：

- ① `MIN_INTERVAL=60s` 内又来一条 → 直接 return，THINK 丢；下一窗没新话 → 整窗不判；
- ② 判读预算触顶 → 同上；
- ③ 有的调用路径只落盘、没把 `think=` 传进来。

修法（**不动 8 句一窗、不动 60s 节流**，避免 cd 变长）：
- 节流/预算用完时把 THINK 推进 `_PENDING_THINK`（上限 8 条，>10min 丢弃）；
- 下次真判读合并进 `think`（`"；".join(队列 + 本次)`），判完清空；
- `wrote` 为假时兜底用 `mood_think.json` 里 `at > 上次判读时间` 的新 THINK（≤3 条，不含 auto）；
- `/测函数 心情` 显示「攒着的 THINK：N 条」。

**体检脚本（用户要求「每天打印一次」）**

- `scripts/daily_report.py`（容器内，只读）：心情/好感/判读链路/今日事件/成本 +
  13 条「期望 vs 实测」；会**模拟 full 与 eco 两个档位**看 4 个段开关；`--json`/`--no-save`；
  有 ⚠️ 退出码 1；落 `data/reports/daily/YYYY-MM-DD.txt`。
- `scripts/check_live.sh`（部署机宿主）：容器状态 / verify_deploy / bot 日志关键行 + 报错计数 /
  napcat 日志 / 数值体检 / 回滚备忘。用法见 `docs/deployment.md §5.2b`。

**验证**：`check_tuning` / `check_design_docs` / `check_names` PASS；`test_new_features` 新增 4 条
（节流不丢 THINK、下次合并判、已判过不重复、落盘兜底）。

---

## 2026-11-26（第二十七轮：外部审计 —— 2 个真 bug + 1 个死护栏）

> 触发：外部评审指出「ADR-042 与 ADR-043 在算术上不成立，AFF_V1_CAP=0.36 永远不可达，
> 两个校验脚本依然全绿」。逐条核验：**4 条真、2 条半真、3 条评审看错了版本**。

**真问题（已修）**

| # | 问题 | 修法 |
|---|---|---|
| 1 | `AFF_V1_CAP=0.36` **不可达**：dv 项天花板 `0.30×AFF_W_MAX(0.42)=0.126`，事件项 ≈0.010 → 实测最大 **0.136**。这是 ADR-028 刚批评过的「恒不触发的死护栏」，而且被 ADR-043 当证据用 | `AFF_V1_CAP` 改 **0.15**（= 日上限）；ADR-043 补「数值验算」段；测试改成断言「尾部 ≈ `DV_K×AFF_W_MAX`」+「CAP ≤ 可达值×1.3」 |
| 2 | **日上限在当天第一件事上失效**：`if _used * d > 0`，`_used==0` 时 `0*d==0` 不成立 → 第一件事完全不受限，可一次写满单次上限；之后 `_room` 变负数，当天同向全部 `return`。真实语义变成「当天第一句话决定全天额度」 | 改 `if _used * d >= 0`，`_room = max(0.0, CAP-abs(_used))`。新增 2 条回归 |
| 3 | `observe` 里的「静默 30min 回补 P」是**死分支**：`_anchor_rec` 每窗写 `anchor_at=now`，`_one` 每窗写 `updated_at=now` → 只要模型每窗都给 `anchor_level`，条件恒 False | 删掉 `or idle`；静默回补只由 `anchor_tick` 负责。§3.1/§1.6 写明 |
| 4 | 文档把事件项写成 `0.25×\|Δ\|`，**漏了 `gain`(≈0.10)**，把事件项夸大约 12 倍——而这正是用来论证「日常变慢」的量 | ADR-043 / §0 / §2.4b 补全 `×gain(≈0.10)`，并给出 `\|Δ\|=0.22 → 0.0044` |

**取向问题（采纳，已改）**

- **回声**：`anchor_level` 成主项后，prompt 仍写「当前心情 +0.35」→ 模型最省力就是照抄（ADR-029 明确警告过）。已**删掉当前数值**，只留上一轮的词。
- **prompt 自相矛盾**：`±3 要大胆` vs `anchor 宁可填 0`。改成「照心理活动填真实档位，只有真看不出才填 0；别一律填 0（会每窗往基线拽）」。
- **主项绕过滤波器**：§3.1 加一条「谁在管事」的说明——`v` 的主项是 `W×conf×(z_abs−v)`，`P/Q/R/K` 只决定残差（稳态 `K≈0.15~0.2`，±3 只挪 0.04~0.05）。避免读者以为卡尔曼在主导。

**评审看错了版本（不改，留证据）**

- `_skip_update` **不是** `not wrote and …`：2026-11-26 已改成 `_relevance < mood.RELEVANCE_GATE`（`mood_judge.py:1215`）。
- `_step` **没有**硬编码 0.40/0.22/0.10：已读 `tuning.MOOD_STEP_*`（`mood_judge.py:854`）。
- 「`dv` 不含 `anchored` → 两个 ADR 断开 → 必须把 anchored 加进 dv」：**事实对、结论错**。
  `dv` 只含事件步进是**有意**的：锚点是「对她水平的重新估计」，不是这个人造成的事件；
  加进好感就变成「她今天心情好，顺手记到旁边人头上」，违反唯一原则。
  已补 `dv_total`（诊断字段）+ 在 §0/§2.4b/§1.6 写明边界。
  另外评审自己的表里 `dv=0.35` 已经 ≥0.30，尾部本来就可达——「跳不起来」不成立。

**小账（已修）**：§5 术语表删「400→50 预过滤」；§3.5 `bot` 行注明「仅当 cause=speaker」；
§0.1「8 条 / 5min」改成「8 条，5min 不可达」；ADR-041「位置排在最后」改成「排在 040 之后」。

**验证**：`check_tuning` / `check_design_docs` / `check_names` / `verify_deploy` PASS；
`test_new_features` 该段新增 4 条（尾部≈天花板、CAP 非死护栏、日上限管第一件事、额度用完不再涨）。

---

## 2026-11-26（第二十六轮：v1 双吃 Δ+dv；anchor_level 必填且占大头）

> 用户拍板两句：
> ① 「v1 可以吃个 `_delta` 和 `dv` 的系数，不就都吃上，好感也不会那么快了，也能拟合上；
>    当然还有一见钟情那几种突变性事件。」
> ② 「锚点要不要强制模型填 `anchor_level`；可以做个权重，`anchor_level` 也占一大部分。」

**ADR-043：v1 同时吃 `Δ` 和 `dv`**

- `profiles.affinity_v1_step(delta, dv, a, …)`：
  `Δaff = clamp(0.25×|Δ|×gain×trust_speed×novelty×k_eff×(1−|a|) + 0.30×三段式(dv), ±0.36)`。
- 旧 v1 只吃名义步长 `Δ`（等效系数 1.0），事件一重就按满格走 → 好感比心情实际位移快得多。
  现在 `Δ` 项压到 0.25（日常变慢），`dv` 项走三段式，`|dv|≥0.30` 的**尾部（一见钟情/心碎）
  仍能一次跳 ±0.11~0.13**（旧 `±0.10` 单次夹会把它削平）。⚠️ 这里当时写 `AFF_V1_CAP=0.36` 是错的：
  `0.30×AFF_W_MAX(0.42)=0.126`，0.36 永远不可达，已在第二十七轮改成 0.15。
- `bump_affinity` 的单次夹从 `±0.10` 改为 `±AFF_V1_CAP`。
- `mood_judge` v1 分支改调 `affinity_v1_step`，日志 `好感v1 …（Δ…/dv…/cause/target）`。

**ADR-042：`anchor_level` 必填 + 每窗按权重混入**

- prompt：`anchor_level` 从「有把握才填、每 20 轮一次」改为**必填**，并明确
  「它占最终心情的大头，不是这件事的影响（那是 desirability）」。
- `mood.observe`：每窗把 `v` 向 `clamp(baseline + map(level))` 拉 `MOOD_ANCHOR_WEIGHT(0.45)×conf`，
  单次仍 `≤ MOOD_ANCHOR_MAX_MOVE(0.20)`；`P` 只在「每 20 窗 / 静默 30min」回补
  （每窗都补会让 `P` 永不降、滤波器失效）。
- `MOOD_ANCHOR_GAIN(0.25)` 现在只给**独立锚点**（静默定时器 / 手动）。
- 合规可见：新增 `ANCHOR_STAT` + `anchor_stat_text()`，`/测函数 心情` 能看到
  「anchor_level 命中 N/M（必填）」；缺失时 INFO 提示且本窗不锚。

**验证**：`check_tuning` / `check_design_docs` / `check_names` PASS；
`test_new_features` 新增 7 条（v1 双吃 5 条 + 锚点权重 2 条 + prompt 必填 1 条）。
`profiles.py` 补 `from __future__ import annotations`，本地 3.8 也能 import 验证。

---

## 2026-11-26（第二十五轮：外部审计修复 —— 假绿 + 5 个真 bug）

> 触发：外部评审指出「文档宣称唯一准，但模型中午已被代码替换；check_design_docs
> 依然 ✅ PASS，因为它校验的是文档↔design_constants，而 impact_max 硬编码 2」。
> 逐条核实后：**假绿属实，5 个真 bug 属实**。

**假绿（已修，最关键）**

- `core/design_constants.py`：`impact_max` 从硬编码 2 改为读 `MOOD_IMPACT_MAX`；
  `step_3` / `impact_max` 进 `KEYS["MOOD"]`（120 → 122 键）。
- `scripts/check_design_docs.py`：`check_mood` 新增「观测档位 -3/+3」和 `step_3` 校验。
  以后文档写回 ±2 会红。
- `docs/DESIGN.md §3.1/§3.2`：5 档 → 7 档、`±3 → 0.40`、`0.35 会被触发`；
  §3.2 加 v2 段落（痕迹求和/三段式/门槛），注明 ±0.10 / 7 天半衰只描述 v1。

**真 bug（已修）**

| # | 问题 | 修法 |
|---|---|---|
| 1 | `_ctx_of` 只要群里有历史就返回「最后 8 条」，把 `split_windows` 的窗口丢掉 → 5 窗判同一份输入 | `ctx_lines` 非空必须用它，现场只兜底 |
| 2 | `_post_rules` 用 `GROUP_MAX_HANZI=20`，覆盖生成侧的 `OUT_BUBBLE_CHARS=40` | 改用 `OUT_BUBBLE_CHARS`，与发送层一致 |
| 3 | passive 窗 `await judge_and_store(passive=True)`：一次 LLM + 一次预算，最后 `if passive: return` 全丢 → 40 条段 5 次调用 4 次白烧 | passive 窗只 `note_window` 记账，不调模型（省 ~80% 判读调用） |
| 4 | `anchor_tick` 只扫 `group_`，私聊永不静默锚 | 加 `private_` |
| 5 | `anchor_tick` 的 `recent_think` 不判时效，拿几个月前的心里话永远锚 | 新增 `MOOD_ANCHOR_THINK_AGE_SEC=1800`，只认 30 分钟内的 THINK |
| 6 | `anchor_tick` 直接调 `judge()`，绕过 `_calls_left`，§3.3 预算对它无效 | 调前查预算并记 `_CALLS` |
| 7 | `mood._load` 裸 `except: pass`，mood.json 损坏 → 全员心情静默归零 | `logger.error` + 备份成 `.corrupt-<ts>.json` |

**验证**：`check_tuning` / `check_design_docs` PASS；`test_new_features` 203 PASS / 5 FAIL
（5 个均为裸镜像缺 httpx，与本次无关）。

**文档治理（G1~G10 + D/E 组，本轮全改）**

| # | 问题 | 修法 |
|---|---|---|
| G1 | 两个 ADR-038 重号 | 证据选法那条改 **ADR-041**，注明原编号 |
| G2 | §3.6 G7 仍写「✅ 护栏 v=min(v,−0.45)」 | 改「⚠️ 已删除（ADR-038）」 |
| G3 | §7.1 「步进 + 快速通道（先不含锚点）」是化石 | 改「步进 + 锚点」，注明两句以前写反 |
| G4 | §1 图注「写回下一轮（ADR-013）」 | 改 ADR-038，注明快速通道已删 |
| G5 | §0.1 「15 个块级 CTX_*」 | 改 **18 个**（15 token + 3 字符） |
| G6 | §6 层→代码漏 8 个模块 | 补 relationship/persona_traits/ctx_budget/style_vault/white_moon/mmr/proactive/reply_pipeline |
| G7 | §2 地图缺 4 篇 | 补 mood-judge-audit / affinity-quantification / verification-runbook / tuning |
| G8 | §8.1「数字只在 tuning」但 mood_judge 硬编码 0.40/0.22/0.10 | 标注为**已知双真相**，待收口 |
| G9 | §9.10「不许 except: pass」但多处裸奔 | 标注现状：`mood._load` 已修，其余待收口 |
| G10 | ADR-008 只覆盖 mood | 注明 `relationship._save` 无 flock 的已知缺口 |
| D | §3.3 三层预算混用（2120/2200/4620/5290） | 加三层对照表；persona 400 注明运行时上限；风格 450→**730 字符** |
| E | §3.4 检索 4 组矛盾 | 候选配额 4/3/2 注明「不是最终条数」；补 8000→400→50→5 换算；选法顺序固定 ①~⑤；`RET_MAX_PER_PERSON` 注明独立键；图书馆 14s 注明降级 |
| 3.5 | `resolve_target` 低置信默认说话人 | 文档改为「低置信退回 speaker（安全默认）」，与代码一致 |

**第二轮（同日，剩余 3 项也改完）**

| 项 | 改法 |
|---|---|
| 4620 vs 5290 | `ctx_total(all_blocks=True)` 新增全量口径：常态 **4620** / 全量 **5770**（含 tools/multitarget/style_total；long_doc 是替代不叠加）。文档 §3.3 写明「都不是 live 总池，总池是 2200」 |
| `neg_weight` 跨量污染 | `relationship.bump` **删掉**对 trust/respect 负向乘 `neg_weight(affinity)`：宽容只作用于好感，信任被骗就是被骗 |
| `REL_DAILY_CAP` 一键两义 | DOCS 改「信任+尊重每日净变化上限（不是好感）」；`AFFINITY_DAILY_CAP` 从 `LEGACY` 移出（它仍在 `profiles.bump_affinity` 跑，是 v1 好感的日上限） |

验证：`check_design_docs` / `check_tuning` / `check_names` PASS；`test_new_features` 203/5（同前，5 个缺 httpx）。

**第三轮（自审：又挖出 3 个自己埋的 bug + 4 个结构问题）**

| # | 问题 | 证据 | 修法 |
|---|---|---|---|
| 1 | **迁移腰斩** | `_aff_ensure` 用 `affinity_at` 当痕迹时间：0.90（30 天前）→ **0.45**；0.60（90 天前）→ **0.19** | 痕迹时间改 `now`；实测 0.90/0.60/0.30 不变 |
| 2 | **陪伴绕过门槛** | `add_affinity_comp` 无 tier cap，新号每天 6 条闲聊一年到 **0.66** | 补 cap；实测卡 **0.12** |
| 3 | **conf 重复乘** | `_aff_w_from_dv` 又乘 `confidence`，但 `dv` 已含 K（K 含 conf）——文档写「不能乘」，代码又乘 | 删掉；参数保留只为兼容 |
| 4 | 旧高好感用户切 v2「冻住」 | 迁移后 `aff_peaks=0`，门槛按 peaks 算 → 30 天老用户 cap 0.32 | 迁移时按 `|a|×10` 反推 peaks/hurts/warm_days |
| 5 | `_COVERAGE` 还是「看到过」 | passive 窗不判，但 coverage 记了全部窗的 msg_id | 只记最后一窗（判过的） |
| 6 | `MAX_BUBBLES` / `OUT_BUBBLES_MAX` 两键同义 | aura_logic + llm 各读一个，靠测试断言同步 | 两处都改读 `OUT_BUBBLES_MAX`；`MAX_BUBBLES` 进 LEGACY |
| 7 | `AFF_W_MAX=0.42` 像死护栏 | `dv≤0.35` → 实际最大 0.362 | **保留 0.42**（降 0.36 会让心碎 −0.41→−0.36、0.9→0.54），文档注明「为 ±4/放宽预留，不是死护栏」 |
| 8 | `tuning.md` 缺 37 个新键 | 调参文档里 AFF_*/MOOD_STEP_3 一个都没有 | `dump_tuning.py --write` 重新生成（455 params） |

**新增自动化验收（9 条，`test_new_features.py`）**：迁移不跳变 ×3、一见钟情 w≈0.362、心碎 w≈−0.410、
新号门槛 0.12、陪伴不绕门槛、a=0.90 心碎 Δ≈−0.41、dv=0 不写。
`test_new_features` 从 203 涨到 **212 PASS / 5 FAIL**（仍是缺 httpx）。

**术语统一（同日，用户要求）**：股市名词全部换成心情语言——

| 原 | 新 |
|---|---|
| 黑天鹅 | **心碎**（负向剧变：被背叛/被羞辱） |
| 涨停 / 跌停 | **骤暖 / 骤冷**（显著事件） |
| 起飞 | **心动**（正向剧变） |
| 跌停板 | **单次上限**（`AFF_W_MAX`） |
| 股票式走势 | **起伏式走势** |

「涨跌幅」这个概念保留（只取涨跌、不取当前值），但不再出现金融名词。
涉及 `affinity-quantification-design.md` / `DESIGN.md` / `tuning.py` / `simulate_affinity.py` /
`test_new_features.py` / 本文件；`tuning.md` 已重新生成。

**示例去金融化（同日，用户提醒）**

`group_scene` / `scene_auditor` / `chat` / `audit_pipeline` / `selftest_all` 里曾用
「A：英伟达跌了吗 → 10 分钟后 B：都是老黄的锅」做「**跨 10 分钟话题承接**」的示例。
它只是示例（说明窗口要按条数不按时间），**不是事件→心情的映射**，但容易被误读。
已全部换成中性「新番」示例（`新番看了吗 → 都是原作的锅`），断言同步。

同时核实全链路：**心情/好感的唯一输入是她的 `[[THINK]]` → `impact` → `dv`**；
`judge_message_mood` 的关键词只用于两处——① 记忆显著性 `strong_emotion`；
② `MOOD_FROM_JUDGE=false` 的回滚模式。默认（`true`）**不改心情、不改好感**。

**示例改成「心理活动起点」（同日，用户指出）**

判定类示例以前写的是「A 说了一句话 → 好感 +0.1」——**这是反例**，
等于用对方的话猜她的心情。已改：

- `affinity-quantification-design.md §2.11`：正例改成
  `【群聊】→【她的 THINK】→【判读】→【心情 dv】→【好感】` 五段；
  并新增**反例**：直接按「这句话是安慰 → 好感 +0.1」是错的。
  理由写清楚：同一个 A 说同一句话，她心里可能是「鼻子有点酸」（+0.18），
  也可能是「又来这套」（−0.06）——**只有 THINK 知道是哪一个**。
- `group_scene` / `scene_auditor` / `chat` / `selftest_all` 的对话示例**保留**
  （它们测的是「上下文窗口」，不是判定），但各加一行：
  「这只是判读的上下文；心情/好感仍只来自她的 `[[THINK]]`，对话本身不直接改数值」。

**因果链定死（同日，用户强调「不要乱捞值」）**

`DESIGN.md` 新增三条硬约束，并在 §1 图里画出来：

```
收发件人（外部信息，只帮她理解「谁对谁说」）
    ↓
她的 [[THINK]]（她理解后的第一人称心理活动）  ← 心情的唯一来源
    ↓
判读 → impact → mood.observe → dv
    ↓
好感（只吃 dv，不读对话 / 关键词 / 事件类型）
```

- §0 新增第 0 条（原来三句话变四句话）；
- §1 图重画：`IN（外部）→ G（生成）→ THINK → JUDGE（只读 THINK）→ KAL（impact→dv）→ ST / REL`；
- §3.1 加「输入只有 `[[THINK]]`」行；§3.2 加「好感只吃 dv，不读对话」。

**代码全检结论**：心情写入口只有 `mood.observe`（判读）、`bump_by_emotion`（被
`MOOD_FROM_JUDGE=true` 挡住）、`bump_mood`（关键词，仅回滚模式）；
好感写入口只有 `add_affinity_event(dv)` / `bump_affinity(delta)`，都在 `mood_judge`。
`plugins/chat` 只读 affinity，`group_mood` 不写个人好感，`profile_update` 不能写 affinity/mood。
**没有旁路。**

**判读 prompt 的判分依据改成心理活动（同日）**

`mood_judge` 的 `desirability` 档位表原来用事件举例（「被夸、被逗笑」「被冒犯、被无视」），
容易被读成「看到'被夸'就 +1」。已改成**她心里会怎么写**：

| 档位 | 原来（事件） | 现在（心理活动） |
|---|---|---|
| +2 | 被认真夸、被记住细节、被哄好 | 心里写「被戳到了」「鼻子有点酸」 |
| +1 | 被接话、被逗笑、被轻轻调侃 | 心里写「嘴上不认，其实挺受用」 |
| −1 | 被忽略一句、被轻慢 | 心里写「算了」「有点膈应」 |
| −2 | 被冒犯、被当众下不来台、被敷衍 | 心里写「下不来台」「这人怎么这样」 |

并加了一句总纲：

> ⚠️ **判分依据只有她的【心理活动】**：同一件事，她心里写「受用」就是 +，
> 写「烦」就是 −。**不要按「这件事通常算好事/坏事」直接给分**——她理解错了也按她的理解评。

**第四轮（同日，修「论证层」：文档迁就代码/迁就结论的 6 处）**

| # | 问题 | 改法 |
|---|---|---|
| 1 | §0 宪法写「好感只吃 dv」，但默认 v1 吃 `_delta` | §0/§3.2 改成「v2 吃 dv；**v1（默认）吃档位名义步长**」，并写明「切 v2 前本条后半句不成立」 |
| 2 | §3.1 用 v2 的 0.362 为 0.35 上限辩护 | 改成「v1 下 ±3 只比 ±2 多约 1.8 倍，**没有尾部通道**；±3 被削平 31% 且无补偿，是已知取舍」 |
| 3 | §3.4 说 `RET_MAX_PER_PERSON` 是独立键 | 改成代码真实公式 `cap = max(1, min(MAX_PER_PERSON, round(limit×RATIO)))`——**联合约束** |
| 4 | §3.4 反向保底「插到第 1 位」 | 改成「**替换 `picked[-1]` 末位**」；代码里没有插位逻辑 |
| 5 | §3.3 全量 5770 = 聚合量重复相加 | `ctx_total(all_blocks=True)` 不再加 `style_total`（已含分项）→ **5040**；文档同步 |
| 6 | §3.4「预过滤 50」是幽灵阶段 | 删掉；全库只有 `RET_PLAN_CAND=400` |
| 7 | §3.5 `group → world` 与代码不符 | 改成「实际是 `speaker`」；`resolve_target` 只认 `to_kind=="person"` |
| 8 | ADR-041 插在 038/039 之间 | 移到 ADR-040 之后 |
| 9 | 注释里「评价者 B/C/F/G9」无出处 | 改成「（2026-11-26 审计修复）」 |
| 10 | `_step = 0.40/0.22/0.10` 硬编码 | 改读 `tuning.MOOD_STEP_3/2/1`——`MOOD_STEP_2` 现在同时管心情/好感/刻痕 |
| 11 | `_skip_update` 恒 False（`not wrote` 已提前 return） | 改成 `_relevance < RELEVANCE_GATE`——闸门真正生效 |

**顺带把「文档比代码乐观」的 5 处数字改成真实值**

| 位置 | 旧 | 新 |
|---|---|---|
| ADR-007 | 全局是「独立慢变量」 ✅ | ⚠️ 半实现：全局 = 会话位移 ×35% 累加，最吵的群主导 |
| 判读窗口 | 「8 条或 5min」 | 段 ≤30s，「5min」永远到不了 |
| 首条位移 | ≈0.085 | conf=0.9 时 0.085；**conf=0.6 时只有 0.004** |
| 单窗 token | ≤1.2k | **实际 ≈2k**（规则段 ~1.1k 字 + 8×160） |
| §3.3 单位 | 同一节 token/字符混用 | 明确 `CTX_*` 是 token ×1.5，profile/evidence/summary 是字符 ×1.0 |

验证：check_design_docs / check_tuning / check_names PASS；test_new_features **212/5**。

## 2026-11-26（好感量化 v2 设计稿：先出公式和数字，不动线上）

- 现状实测：好朋友一个月不聊天，好感 **−0.37**；挚友 **−0.44**；三个月 **−0.78**。
  根因是指数衰减（7 天半衰期）**一路衰减到 0**，不是 0.15/天那个事件上限。
- 新模型（设计稿，开关 `AFF_MODEL` 默认仍 v1）：
  **两分量** `bond`（投入决定的底子，不衰减）+ `heat`（幂律衰减），
  事件步长 `(1−|a|)^γ` 两头饱和，负面事件 ×1.7。
- 参考：Ebbinghaus 遗忘曲线 / Wixted & Ebbesen (1991) 幂律优于指数 /
  Anderson & Schooler (1991) 与 ACT-R / Roberts & Dunbar (2011) 关系维持成本 /
  Rusbult 投资模型 / Baumeister (2001) 负面偏差 / Gottman 5:1 /
  Altman & Taylor 社会渗透理论 / OCC。
- 数值：好朋友 1 月 −0.17、3 月 −0.24、1 年 −0.29；挚友 1 月 −0.18 但下限 0.60；
  讨厌的人 1 月只回血 0.18。单次事件：挚友 +2 → +0.018、−2 → −0.068。
- 产物：`docs/affinity-quantification-design.md` + `scripts/simulate_affinity.py`
  （可改参数直接看曲线）。**没有改线上逻辑**，等拍板 3 个参数再接线。
- **v2.1 追加**（同日）：改成 **ACT-R 式痕迹求和** `a = bond + Σ w·(1+Δt/10)^-0.5`，
  天生解决「幂律不能增量叠加」；给出**事件映射表**（impact ±1/±2 在 a=−0.8~0.9 各推多少）、
  **道歉破冰通道**（−0.60 连续 3 次道歉 → −0.17）、**陪伴抵消系数**
  （稳住 0.3 约 1.2 句/天，稳住 0.5 约 3.7 句/天，光靠陪伴天花板 0.63~0.75）。
- **v2.2 追加**（参数拍板：`AFF_TRACE_T=10`、`AFF_COMP_CAP=0.80`、`AFF_REPAIR_BASE=0.16`）：
  解决「0.8 之后怎么继续涨到 0.9 / 0.99」——
  **巩固**（`κ` 从 a=0.75 的 0% 线性升到 a=0.95 的 60%，正向事件的一部分直接沉淀进
  不衰减的 `bond`）、**深层投入**（`I_deep`，把 `bond_max` 从 0.60 抬到 0.90）、
  **分层门槛**（只封上涨不回溯）。硬上限 `AFF_MAX=0.99`。
- **v2.3 修正（最重要的一条）**：**好感 = 寄存在每个人那里的心情涨落历史**。
  - 痕迹权重从 `impact` 档位换成 `mood.observe` 的实际位移 **`dv`**：
    `w = 0.60·|dv|·(1-|a|)^0.70`（正向）/ `×1.6·(1-|a|)^0.35`（负向），单次夹 ±0.20；
  - **删掉所有名词判读**（不再有 `milestone=help/together/vulnerable/repair`）：
    `peaks`（`|dv|≥0.15`）、`repairs`（负 peak 后 7 天暖回来）、`warm_days` 全部从 dv 序列自动统计；
  - 巩固的 `κ` 加了 **`warmth7` 闸门**（最近 7 天正向 dv 之和）：光好感高、最近没暖过 → 不沉淀；
  - 门槛改成 `peaks≥3`（挚友）/ `peaks≥8 且 repairs≥1`（知己）；
  - 模拟：一年都到 0.92 的两条路，**停聊一年后巩固的剩 0.78、没巩固的只剩 0.69**；
    `repairs=0` 时哪怕 bond 0.90 也卡在 0.92（进不了知己）。
- **v2.4 追加（起伏化）**：好感取的是**涨跌幅**不是当前值，且像心情起伏一样有骤暖/骤冷/心碎。
  三段式 `w_from_dv`：
  - 日常 `|dv|<0.15`：`0.60·|dv|·(1-|a|)^0.70`（线性 + 强饱和，高好感推不动）；
  - 显著 `0.15~0.30`：`1.6·|dv|^1.5·(1-|a|)^0.35`（凸性放大，骤暖/骤冷）；
  - 尾部 `|dv|≥0.30`：`(0.06+2.0·|dv|^1.8)·(1-|a|)^0.15`（心碎/一见钟情，几乎不饱和）；
  - 单次夹 ±0.42（单次上限）；`a<0` 时正向跳变 ×0.70（有防备）。
  - 关键数字：a=0.90 时 `dv=+0.35` → **+0.256**（一次到 0.99），`dv=−0.35` → **−0.410**
    （一次到 0.49）；`dv` 从 0.22→0.30 只多 36%，w 却多 75%（凸性）。
  - 模拟 24 个月：单月跳变 +0.39 / −0.39，其余月份缓慢爬——不再是平滑曲线。
- **v2.5 追加（回到原设计：判读档位 + 刻度重标定）**：
  - 原设计 `emotion-dynamics-design.md` §1 是「LLM 给位移档位 → 查表」，方向对；
    但只有 5 档（−2~+2），`dv` 最大 ~0.16，**触发不了好感尾部 0.30**——
    「一见钟情」在数学上不存在。改成 **7 档 −3~+3**（`MOOD_STEP_3=0.40`），
    ±3 = 「她今天只会想这一件」（一见钟情 / 被背叛），一天最多一次。
  - 落地原审计 §4 的**两段式**：判读给 `desirability(-3~+3) × cared(0/1/2) × about`，
    代码合成 `impact`——`cared=0`（看戏）显式不扣分。
    下游群心情/刻痕用 `min(|impact|,2)`，行为不变。
  - **好感刻度重标定**（用户提的）：0.60~0.75 = 很好/喜欢（旧 0.85~0.9），
    **0.75~0.85 = 暧昧（旧 0.90）**，0.85+ = 爱，0.95+ = 唯一的爱；
    负值：−0.60~−0.75 反感/记恨（旧 −0.8）、−0.75~−0.90 仇视（旧 −1.0）、
    −0.90~−1.0 决裂。门槛表同步改成 眼熟0.15/熟人0.30/朋友0.45/很好0.60/
    暧昧0.75(peaks3)/爱0.85(peaks5)/唯一的爱0.95(peaks8+repairs1)。
  - 新模拟：只陪伴 1 年 0.62（很好）；每周被夸 0.70；每 30 天心动 0.87（爱）；
    **只有「吵过又暖回来」才到 0.99**（repairs≥1）。
- **v2.5 已接线（2026-11-26，默认 AFF_MODEL=v1 可回滚）**：
  - `core/tuning.py`：新增 `MOOD_STEP_3=0.40`、`MOOD_IMPACT_MAX=3`、35 个 `AFF_*` 键；
  - `services/mood.py`：`observe()` 接受 ±3，`_step_of` 三档；
  - `services/mood_judge.py`：prompt 改 7 档 + `cared` 两段式；
    好感写入分 v1/v2，v2 吃 `_dv_actual`（不再乘 K/gain/tsf 重复因子），
    删掉 `_cause=="speaker"` 限制（只看 target）；
  - `services/profiles.py`：新增 `affinity_now()` / `add_affinity_event(dv)` /
    `add_affinity_comp()`，痕迹求和 + 三段式 + 巩固 + 门槛 + 旧档案迁移；
  - 容器 3.11 验证：a=0.90 时 dv=−0.35 → **−0.410**（0.90→0.49）；
    新号连刷被门槛卡 0.12；巩固只在 dv>0 且 warmth7 够时沉淀。
  - 切 v2：`.env` 写 `AFF_MODEL=v2` 重启；观察 `[AFFv2]` / `[MOOD-JUDGE] 好感v2`。

## 2026-11-26（第二十三轮：@ 后续被冷却吞 / 亲近突降 / B站短链 / 部署侧三类问题）

**我们服务器上发现的**

- **@ 她之后下一句不理**：`GROUP_REPLY_COOLDOWN`（45s）在「关注窗口」判断**之前**执行，
  被 @ 后开启的 3 条关注全被 `[COOLDOWN] 刚回过 Ns，这条没点她` 吞掉。
  现在关注窗口内的人不再受这条冷却限制（真机日志已复现，修完有断言防回归）。
- **「好感 3 小时掉 0.5」**：不是好感衰减，是 11-25 把 `intimacy` 改成饱和曲线
  `score/(score+3)`，15 个群友的亲近从 1.00 一夜掉到 0.50（实测最大正好 −0.50）。
  回退成「线性 + 软上限 0.98」：只把恰好 1.00 压到 0.98，其余不变。
  好感本身：7 天半衰期 × 深度系数、单次 ≤±0.10、每日净变化 ≤0.15，逐条核对无异常
  （近 36h 最大 3 小时跌幅 −0.033）。
- **B 站测试题被导向视频**：`b23.tv` 短链展开时，如果最终页不是视频，会去 HTML 里抓
  「任意 BV」再改写成 `/video/BVxxx` —— 活动页/测试题/专栏里常嵌着推荐视频的 BV。
  现在只要 302 落在 `bilibili.com` 就原样返回，只有真正的 JS 跳转页才抠 BV。
- **红包**：查证 NapCat 全包没有任何红包 action，OneBot 也没有；结论与方案见
  [red-packet-feasibility.md](red-packet-feasibility.md)（发红包不可行，收需要 NapCat 原生插件 + owner 授权）。

**别人部署我们的 code 后发现的**

- **20 字 vs 1000 字才转合并转发**：代码默认 `OUT_BUBBLE_CHARS=40` / `GROUP_MAX_HANZI=20` /
  `LONG_FORWARD_MIN=120`，线上被写成 1000 才会整段刷屏。现在启动时体检，
  超过合理范围直接 `[ENV] ⚠️` 告警（`services/env_guard.py`）。
- **时不时不理人（私聊 @ 也不理）**：`ALLOWED_GROUPS` / `ALLOWED_PRIVATE` 为空时，
  消息在准入层就被丢掉，表现就是「完全不回」。`.env.example` 不再把白名单清成空，
  改成 `123456789` 占位并注释「必须改成你自己的」；启动也会告警。
- **每次回复卡 4 分钟**：补资料轮次只在「轮与轮之间」检查预算，单个慢工具
  （网页/视频/图书馆/算力机）就能把一次回复拖到几分钟。现在每轮补资料都受整轮预算约束
  （`asyncio.wait_for`，最多 30s 且不超过剩余预算），超时直接开口。
- **脱敏把配置一起脱掉**：以前「键名里含 KEY/TOKEN/PASS」就清空，
  `GLM_MAX_TOKENS=800`、`VIDEO_MAX_KEY_FRAMES=12` 这种普通参数被误清。
  现在只有「值看起来真是密钥」才清空；数字/布尔/短枚举保留。

## 2026-11-25（第二十二轮：三项工程债闭环 + 8 个「假接线」键收口）

- **`check_tuning` 改 AST**：不再用「文件里出现键名字符串」判断接线——
  注释/docstring 不再算数；新增规则「模块级常量读了必须真被用到」。
  一次就抓出 8 个旧版全漏的键（见下）。宿主 py3.8 解析不了的文件自动降级正则。
- **`check_design_docs --runtime`**：默认仍只查「文档 ↔ 设计常量」；
  加 `--runtime` 后用 AST 证明 120 个设计键都有真实读点，并核对
  `.env` 覆盖后的不变式（`PROMPT_BUDGET ≥ 2120`、`CTX_STYLE_TOTAL×1.5 ≥ 730`、
  每块 `CTX_* ≥ 设计目标`）。容器内 PASS。
- **8 个假接线/死键收口**：
  - `ADDR_RECENT_MENTION` → 短句冲人推断（两处硬编码 0.55/0.5 改用键值）；
  - `LORE_SCAN_DEPTH` → `lorebook.scan()` 只匹配最近 N 行（depth 语义）；
  - `LLM_USAGE_KEEP_DAYS` → `llm_usage.init()` 启动清理超期明细；
  - `REL_INTIMACY_HALF_LIFE_DAYS` → `profiles.intimacy()` 的 30 天半衰期；
  - `RET_MEMORY_TOP` → `build_packet` 记忆块默认条数（4 条）；
  - 删除 `MOOD_PASSIVE_MAX_DELTA`（ADR-038 已删被动通道）、
    `MAX_REPLY_CHARS`（从来没有读点）、`SHORT_QUERY_WINDOW_SEC`（从未实现）。
  调参键 420 → **418**。
- **宿主 py3.8 兼容**：`chat` 的两处 `@get_driver().on_bot_connect` 改成
  `@_driver.on_bot_connect`；`check_names` 的 `ast.MatchAs` 加 `getattr` 兜底。
  现在本地也能跑完整 AST 检查（容器 3.11 行为不变）。
- **测试同步**：`test_new_features` / `audit_features G16` 的「被动观察 Δ 上限」
  断言改成「旁观窗口早退、不写状态」，与 ADR-038 一致。
- **全链路自检从 22 项失败修到 0**（`verify_all --full` 现在 10/10）。
  其中 3 条是**真 bug**（不是测试问题）：
  ① `memdb.recall_catalog` 缺目录编号 `no`（sqlite 与 json 行为不一致）；
  ② `memdb.remember` 丢掉调用方显式传的 `tags`（`weekly_apply` 的
     `weekly_check` 全丢）；
  ③ `journal_agent` / 周报直接读 `index.json`，sqlite 后端永远「0 条记忆」
     → 新增 `memory_v2.list_items()` / `memdb.list_memories()` 统一读接口。
  其余为设计变更后的测试漂移（ADR-038 的 THINK/旁观、kind τ 遗忘曲线、
  40 字铁律、top-N 工具、impact 步进），以及 `recall` 缓存导致的用例互相污染。
- **验证**：容器内 `verify_all` 9/9、`verify_rollbacks` 19/0、
  `test_new_features` 222/0、`audit_features` 52/0、
  `check_tuning`/`check_design_docs --runtime` PASS、
  部署后日志回滚 6 PASS / 0 FAIL / 13 SKIP、`deploy_manifest` 112 文件 md5 全绿。
  详见 `docs/verification-report-20261125-debt.md`。

## 2026-11-25（第二十一轮：最后 3 个 RET 键接线，UNWIRED 清零）

- `RET_LIB_LATENCY_MS`：`agent_client.recall` 记录整次图书馆调用耗时，
  超目标只打 warning，不阻断。
- `RET_WORLD_TOP`：RAG 回来的 facts 只留最靠前的 N 条再进 prompt（默认 3）。
- `RET_STYLE_TOP`：白月光 `_raw_examples` 默认取几句原话（默认 3），
  不再在各调用点写死 `3`。
- `tuning.UNWIRED` **清空**；`check_tuning` / `audit_coverage` 全部 PASS。

## 2026-11-25（第二十轮：CTX 预算校准 + RRF 框架）

- **CTX_* 校准**：按线上真实块长（字符/1.5，留 ~20% 余量）调整
  `CTX_PERSONA=700 / CTX_PROTOCOL=1500 / CTX_STATE=240 / CTX_POST_RULES=320 /
  CTX_RHYTHM=180 / CTX_WHITE_MOON=180 / CTX_STYLE=250 / CTX_STYLE_TOTAL=650 /
  CTX_ANCHOR=200 / CTX_MULTITARGET=120`。
  这样 `CTX_ENFORCE=1` 只裁异常膨胀，不会把正常块裁成残句。
- **RRF 框架**：`memdb.recall_planned` 支持 `RET_FUSION_RRF=1`：
  按 struct/bm25/tag/strength/recency/salience 六路排名做
  `weight/(RET_RRF_K+rank)` 融合，再 ×100 保持阈值量级。
  默认 `0`（加权求和）；200 条记忆自检索：加权 R@1 0.300 / R@5 0.405，
  RRF R@1 0.240 / R@5 0.430 —— **R@5 更好、R@1 更差**，暂不默认开，
  等向量路接上或线上 A/B 确认「多一条证据」比「第一条更准」更重要。
- `tuning.UNWIRED` 从 4 降到 **3**（只剩 `RET_LIB_LATENCY_MS/RET_STYLE_TOP/RET_WORLD_TOP`）。

## 2026-11-25（第十九轮：CTX_* 15 个块级预算接线）

- 新增 `services/ctx_budget.py`：统一读 15 个块级 `CTX_*`，按
  `chars ≈ token × 1.5` 统计每个块实际字数 vs 预算。
- **默认 `CTX_ENFORCE=0`：只统计/告警，不裁剪**。因为线上协议块 2089 字
  vs `CTX_PROTOCOL=180`（774%）、节奏块 228 vs 60（380%）——直接硬裁会残句。
  确认某个块该裁，再把 `CTX_ENFORCE=1`。
- 接线点：persona / protocol / tools / anchor / multitarget / state / history /
  post_rules / expression / rhythm / white_moon / style / style_total /
  long_doc_evidence / long_doc_library。
- 新增 `/测函数 上下文`：看每块 `实际/预算`、超限次数、峰值。
- `tuning.UNWIRED` 从 19 降到 **4**（只剩检索分路键）。

## 2026-11-25（第十八轮：服务器侧二期——ADR-008 单状态源 / P6 评分工具）

- **ADR-008 单一状态源**：`mood` 所有写入口加 `@_serialized`（RLock 串行
  `load → 改 → save`），`_save` 用 `flock` 保护 `tmp → os.replace`（跨进程）。
  读接口不抢锁（原子替换保证读到完整旧/新文件）。
  验证：`scripts/verify_state_lock.py`，8 线程 × 5 次 × 1.0 = 40 内部点，
  线上/本地都得到 **40.00**，不丢更新。
- **P6 情绪标注评分**：新增 `scripts/eval_emotion_labels.py`，支持
  单人 vs `auto_suggest`、双人 A/B 重叠集；算 impact QWK、完全一致率、
  ±1 一致率、target 准确率、arousal kappa、relation MAE、confidence ECE。
  200 条 label 目前仍为空，脚本会明确提示「先标注」。
- **R3 服务器侧前置**：`scripts/library_sync.py` 增加
  ① sha256 增量（未变跳过，`--force` 全量）；② 行级去重（同一文件内重复/近重复）；
  ③ 正文头 `# partition: dialogue` 分区标记；④ `--dry-run` 统计。
  服务器 dry-run：17 文件 3.1MB，去重 **4271/35922 行（12%）**。
  真正的分区索引仍要在算力机 RAG 侧做。
- **二期归属澄清**：R3 图书馆分区/去重/增量、R4 向量+RRF、ADR-025 本地识图库
  **需要算力机**（服务器侧可先做上传前增量/去重/分区标记）；ADR-004 事件研究
  需要先定「预期」口径；ADR-008 / P6 / CTX_* 是服务器侧。

## 2026-11-25（第十七轮：检索二期 / 位置指标 / p95 周报 / 切 sqlite）

### 检索二期（倒 U / τ_kind / MMR）
- **显著性倒 U**：`memdb.salience_weight(u)` —— 小冲击几乎不记、中等最显著、
  极端回落到 0.6（`MEM_SAL_*`）。`storage_strength` 改按
  `0.15 + 0.45w + 0.20×unexpected + 0.20×relation_relevance`，负性 ×1.15。
- **τ_kind**：提取强度按 kind 衰减（event 30 / bond 90 / fact 365 /
  preference 180 / style 365 天）；json 与 sqlite 两个后端同口径。
- **MMR**：新增 `services/mmr.py`；证据先多取 `limit×MEM_MMR_CAND` 条，
  再按 `λ×相关性 − (1−λ)×最大相似度` 选（`MEM_MMR_LAMBDA=0.7`），
  同一件事被记多遍不再占满 5 条证据。风格样本同样走 MMR（`STYLE_MMR_*`）。
- 修：sqlite 路径以前 `memdb` 只返回 `limit` 条，`_diversify` 没有替换空间；
  现在多取候选（`mark_hit=False`），最终选中的才 `note_hits()` 回补提取强度。

### 注意力位置指标
- `eval_gold.py` 新增 `gold_position`（0 前 / 1 后，生产最相关在最后）。
- `--stage position` 导出 gold 放头/中/尾的三份回放集；
  `scripts/position_report.py` 算 `lost_in_middle`（中 vs 尾差 <10% 才 PASS）。

### p95 周报
- `reply_pipeline` 每轮把各步耗时写 `data/metrics/pipeline_stages.jsonl`
  （`PIPE_METRICS=1`），内存保留 `PIPE_STAGE_SAMPLES` 个样本；
  `/测函数 流水线` 直接显示 p50/p95 并标「最慢」。
- 新增 `services/perf_stats.py` + `scripts/p95_report.py`；
  `weekly_export.py` 周报增加「七、性能 p95」，只优化最慢一块。

### 记忆后端
- `.env.example` 增加 `MEMORY_BACKEND=json|sqlite` 与切换/回滚说明；
  切前跑 `python scripts/check_memory_dual.py --strict`。

## 2026-11-25

### 实现（第十六轮：把 92 个「设计键」接上线，剩 22 个明确挂账）

- **心情 P2（唯一更新链路）**：`mood.observe()` —— 判读只报 `impact ∈ -2..+2`，
  代码查表得步长，经伪观测 `z = clamp(v + dir×step×sens)`、`K=P/(P+R)`、
  饱和 `×(1−0.6|v|)`、硬上限 0.35 更新 `v`；`P` 随观测下降、按 `Q` 随时间回升。
  半衰期按作用域：群 30min / 私聊 2h / 全局 6h。回滚：`MOOD_UPDATE=delta`。
- **锚点**：每 20 个窗口或静默 30min，用 `anchor_level(-2..+2)` 做一次绝对纠偏，
  带死区 0.15、单次上限 0.20、`P` 回补 0.02（`MOOD_ANCHOR_*`）。
- **快速通道护栏**：@她/引用她 + 强负面词 → `v=min(v,−0.45)`（轻负面 −0.25），
  **只降不升**，并把 `P×1.5`（让下一轮判读能拉回）。不写好感。
- **关系四量 P3**：新增 `services/relationship.py`（trust/respect 写入口 + 半衰期 +
  记仇半衰期 + 日上限 + 审计流水）；`profiles.decay_affinity()` 按深度系数衰减好感；
  intimacy 仍是派生量。判读新增 `relation{trust,respect}` / `relation_relevance` / `target`。
- **输出气泡**：`OUT_BUBBLE_CHARS=40` / `OUT_BUBBLES_MAX=2`；超过 2 泡把尾部合并后走
  合并转发，**不截断**（旧行为是 20 字硬切/转发）。
- **收发件人**：`ADDR_*` 全部接进 `group_scene._infer_addressee`，展示分三档
  （确定 / 像在回 / 信号太弱）。
- **检索**：`RET_EVIDENCE_MAX/MEMORY_TOP/MAX_PER_PERSON/SAME_PERSON_RATIO/NEUTRAL_QUOTA`
  接进 `memory_v2.recall`（按人分组 + 反向保底）；`RET_CHAR_LATENCY_MS` 超时告警。
- **上下文**：`CTX_PROFILE/EVIDENCE/SUMMARY/SOFT_CAP` 接进 `prompt_builder`；
  其余 17 个块级键 live 没有装配点，仍在 `tuning.UNWIRED`。
- **运行档位**：新增 `POWER_MODE=full|eco`（`config.power_on`），和 `GROUP_WATCH_MODE`
  正交：lite 管「回不回」，eco 管「算不算」（审核/图书馆/主动/表达蒸馏）。
- **修 bug**：`GROUP_WATCH_MODE=lite` 的分支以前被缩进在 `except` 里，
  只有安静查询抛异常才生效 —— 正常路径下 lite 完全等于 full。
- **线上验证**：新增 `scripts/verify_napcat_log.py` + [verification-runbook.md](verification-runbook.md)：
  扫码登录后拉 `docker logs`，按 M1/M3/M4/M8/M10/P4/O4/O5/R*/G36 对账并生成报告。

## 2026-11-24

### 修复（第十五轮：同一首歌被重复翻唱 / 整首文件误报失败）

- 现象：群里连着出现 3 条《喜帖街》语音 + 每条约 2 条提示（「哼，唱完了」「要听整首吗」）。
  用户问「是超时重复发送吗？」——**不是**，是三个独立翻唱任务。
- 根因：
  1. `sing` 去重 key 是「会话#人」：三个人分别点同一首 → 各跑一次，成品完全一样；
  2. 「要听整首」的待定请求 `PENDING_FULL` 按**点歌人 user_id** 存，
     但提示发在群里：别人回「要」匹配不上 → 落到聊天 → 又调一次 `sing` → 重唱；
  3. 整首文件发送遇到 `retcode=1200 Timeout` 一律当失败，还补一句「整首太大发不出去」，
     其实文件已经进队列（NapCat 里看得到「文件发送中」）。
- 修法：
  - `sing` 去重改成群里按「群#歌名」（私聊仍按人），同一首 15 分钟只跑一次；
  - `PENDING_FULL` 改按会话（`group_<gid>` / `private_<uid>`），群里任何人回「要」都能命中；
    群里只认明确词（要/整首/要整首/要听），不拦日常的「好/行/1」；
  - 整首文件 `_is_ack_timeout` 按成功处理，不再误报「发不出去」；
  - 重启恢复任务时**跳过已完成**的，避免把刚发过的语音再补一遍。

### 补充（第十四轮：harness 桌面版能不能接）

- 用户提问：「用户用的 harness 是桌面版，拿不到 token 或我们要的内容吧？」
- 结论：**对，桌面版大概率接不上本桥**。本桥只认两种非交互入口：
  - `cli`：PATH 里能跑 `dsh run --cwd … --prompt …`（桌面版一般不装 CLI）；
  - `http`：DSH 暴露 HTTP 接口 + Bearer token（桌面版 token 存在自己配置里，不导出）。
- 新增 `/h 自检`（`harness.probe()`）：逐项检查 CLI 命令在不在、`--help` 能不能跑、
  HTTP 地址通不通、token 配没配，并直接给结论（换 cli / 换 http / 走 MCP）。
- `_call_cli` 遇到 `FileNotFoundError` 不再只说「启动失败」，而是指出「桌面版一般不带 CLI」。
- 文档 `docs/harness-bridge.md` 新增 §9「桌面版怎么接」：
  1. 不用 harness，保持 `CODE_AGENT=self_tool`（默认，推荐给普通用户）；
  2. 桌面版直接打开仓库 + `AGENTS.md`/MCP（推荐给要改代码的人）；
  3. 桌面版开了本地 API/CLI 才用本桥。
- 说明：本桥只带 ≤800 字上下文（人格/心情/好感/白月光/3 条记忆），不传聊天记录；
  token 只是 DSH 给的，桌面版不给就没有可填的。

### 修复（第十三轮：画图/翻唱重启后不丢结果）

- **现象**：19:12 / 19:25 / 19:29 / 19:37 下发的翻唱、19:23-19:40 的画图，成品都没出来。
- **根因**：算力机任务表 `STATUS["tasks"]` 只在内存里。机器人一重启：
  1. 算力机算完回传 `/api/tasks/complete` → `task not found` → **成品直接丢**；
  2. 等待协程被取消，就算文件还在也没人发到群里。
  （19:21 / 19:23 / 19:40 的几次部署重启正好打断了在跑的任务。）
- **修法**：
  - `tool_dispatcher` 把任务表落盘 `data/node_tasks.json`（5s 一次 + 拉取/回传时），
    启动时恢复（6 小时内）；
  - 画图/翻唱任务写入 `deliver={gid,uid,label}`；OneBot 连上后启动恢复协程：
    继续等结果，完成后**补发到原会话**；超时/失败则发一句说明；
  - `_tool_sing._on_done` 不再把取消（重启）当异常打一屏 `Exception in callback`。
- 注意：重启**之前**已经丢的任务没有 `deliver`，补不回来；这次之后重启可续。

### 修复（第十二轮：图书馆真能查 / 群聊工具放开 / 引用别挂错图）

- **图书馆一直 0 条（真 bug）**：
  - `AGENT_RECALL_TIMEOUT` 之外新增 `AGENT_LIBRARY_TIMEOUT`（默认 14s）；
    以前 `smart-query` 写死 2.5s，而 RAG 实测 5~14s → 每条消息都超时、
    然后「图书馆不可用 10 分钟」，所以永远 0 条；
  - `reply_pipeline` 把图书馆查询**提前到总结之前并行发起**，不再串行叠加；
  - 只对「像在问事/提旧事」的消息查（问号/记得/上次/谁/什么/怎么…），
    纯插科打诨不花这 5~14s；
  - 同一问题 10 分钟内复用缓存（空结果 2 分钟）；
  - `is_no_answer()` 过滤 RAG 的「没有/无法回答/问题不完整」，不再把「没有」当事实；
  - `_looks_like_chat()` 不再因为来源是 `/app/documents/*.json` 就全丢
    （以前把 `documents/xiaoming.txt` 这类归档也误杀），只排除诗词语料 + 百科式定义；
  - 新增 `/测函数 图书馆 <问题>`：直接在 QQ 里看 RAG 命中什么、耗时多少。
- **群聊里找不到翻唱工具**：`sing` / `remember` / `recall` 之前只挂私聊
  （`register_tool` 默认 `scope=("private",)`）。现在三个都挂 `("private","group")`，
  群里点歌、记承诺、查记忆都能用。
- **引用回复又挂错（第二次）**：这一轮在回「在开会」，系统却把最后那张表情包自动引上了。
  - 自动引用图/视频时，只有这个气泡正文真在说图（图/猫/这张/表情/照片/画面…）才引，
    否则先不引；
  - post_rules 明确：不同人/不同事分气泡（`|||`），每个气泡 `#消息号` 引自己回的那条，
    别把两件事塞进一个引用里；
  - **第三个根因（19:36 取名）**：模型给第 1 个气泡写了 `<reply:ヽ>`，
    第 2 个气泡（该给猫）却被系统自动引用又挂回 ヽ ——
    因为显式引用没有消耗掉 `_quote_left`。现在显式引用也会 `_quote_left=False`，
    后面气泡不再自动引；
  - `_reply_protocol` 增加：给人取名/点评某人/答应某人请求时，
    最新一条不是他发的就 `<reply:他最后那句>` 或写清「@谁」。
- **发行仓补齐**：`docs/agent-pipeline.md`、`scripts/pixiv_get_*.py`、
  `plugins/third/**/patches/*.patch` 不再被误排除；`roll.py` 与服务器版本对齐。

### 补充（第十一轮补：流水线按人查看 / 档案白月光占位）

- `/看模板 流水线` 支持可选 `@人` / `QQ号`：
  - 不带人：打印上一次真实回复的流水线；
  - 带人：用这个人在当前群的最近一句话**现跑一遍** `reply_pipeline.build`，
    排查“这个人的档案为什么不对”。
- `/看模板 流水线` 顶部增加「这次是回：谁（QQ）｜会话」+「要回的原话」，
  避免把“上一条群消息的取数”误当成 `/档案`。
- 档案卡「白月光」块不再直接消失：没统计出来也显示
  「还没统计出来（他 @/引用谁最多就会记）」；reply_pipeline 档案块同样保留占位。
- `mood_judge.summarize` 遇到 `[图片]/[表情]/[视频]` 时总结「他在接哪件事/群里在聊什么」，
  不再输出「内容未显示完整」这类元评论。
- **图片/视频不再走 pipeline 的「总结」步**：`reply_pipeline` 检测到
  `[图片识别结果]/[视频解析结果]/[图片]/[表情]/[视频]` 时直接跳过总结，
  检索词改用「某人 图片」——避免把视觉报告重新压回 prompt 让她先描述图。
  视觉分支自己的「不要复述画面」规则保留不动（`llm.py` + `vision_agent`）。
- **表情描述过滤**：`aura_logic.strip_sticker_description()` 清掉
  `（发个嘲笑表情）` / `给你一个嘲笑表情` / `嘲笑表情 |||<sticker:嘲笑>` 里的文字描述，
  只发图；正常聊「这个表情好可爱」不误伤。
- **〔已回〕误判（真 bug）**：worker 防重投的 `_REPLIED_MIDS` 在进流水线**之前**
  就登记了当前 mid，导致每条新消息都被判「已回=是」，模型只敢补一句。
  现在拆成两个集合：
  - `_REPLIED_MIDS` = 见过/处理过（只用于 DEDUP）；
  - `_DONE_MIDS` = **真的发出去过回复**（历史块和 pipeline 的〔已回〕只认它）。
  发送成功后由 `_mark_replied_done()` 写入；沉默不写。
- NapCat 看门狗：日志出现「需要验证码 / proofWaterUrl / sms-verify」时只记日志、
  **不自动重启**，等人工在 WebUI 完成验证；无密码模式不会反复作废二维码。

### 修复（第十一轮：上下文总结 / 档案联动 / 心里话 / 表情静默 / 流水线可看）

- **总结把「身高 192」理解成「192 关」**：
  - `mood_judge.summarize()` 现在带最近 10 条上下文，先判断「这句在接哪句」；
  - 硬性要求数字/单位按字面保留，不自己补成关卡/等级/分数；
  - `reply_pipeline` ④ 把 `group_scene.history_lines` 一起传进去；
  - `scene_auditor` 的滚动总结也加了同一条数字纪律。
- **档案卡和称呼/白月光不联动**：
  - `profile_card._blocks` 的称呼改读 `callname`（`user_call_of` / `bot_call_of`），
    新增「称呼」「白月光」两块；不再把历史 aliases 当称呼展示；
  - `/档案 文字` 同样读 callname + `white_moon.profile_note`；
  - `profiles.call_of()` 统一先问 callname 索引，history/审核/工具不再各叫一个名；
  - `set_call_names()` 换称呼时自动摘掉旧 call_name 的 aliases；
  - `/删称呼 QQ 阿明` 支持**点名删一个别名**（`callname.clear_name` + `profiles.remove_alias`）。
- **又说心里话**：`aura_logic.strip_inner_thought()` 清正文里的
  `（心里话：…）` / `内心：…` / `（内心 OS：…）` / `【心里话】…`；
  `THINK_PROTOCOL` 同步禁止正文出现这些字样。
- **表情标签不存在时的提示词**：`sticker_sender` 不再发
  「没有「X」这个标签的表情哦」，只在日志留痕；宁愿不发，也不发提示。
- **流水线可看**：`reply_pipeline` 产出加 `▍PIPELINE｜…` 头；
  新增 `/看模板 流水线`（只打流水线，不混人格/规矩/历史）、
  `/测函数 流水线 全`；`prompt_report` 认识该块；`PIPELINE_BUDGET` 7s → 12s。
- **关键规矩补进 post_rules**：不认错昵称、不引错话、带〔已回〕的消息不再回。
- **NapCat 掉线看门狗**：`scripts/napcat_watchdog.sh` + cron 每 2 分钟；
  容器挂了自动 start；登录失效且 `.env` 配了 `NAPCAT_QUICK_PASSWORD(_MD5)` 才 restart
  （10 分钟冷却）；没配密码只记日志，不反复作废二维码。详见 `docs/deployment.md` 3.1。

### 修复 + 精简（第九轮：称呼第三人称 / 删称呼 / 规矩块压缩）

- **称呼记错人（真 bug）**：
  - `[[CALL]]` 新增 `target`：给第三个人定称呼必须写 `target:名字/QQ`，不写默认当前说话人；
  - 模型没写 target、但正文有「叫他/喊他/管他叫」等第三人称线索 → 先按名字解析；
    解析不到或解析回说话人 → **直接跳过，绝不写回说话人**；
  - 新增 `/删称呼 [@人]`：两个方向 + `aliases` + 名册缓存一起清，解决「删不掉」；
  - `set_call` 工具支持 `who=both`、`name=删`（留空也当删除）；
  - `profiles.clear_call_names()` 会移除旧 call_name 的 aliases，避免名册又把错名字找回来。
- **固定区规矩大砍（-83%）**：`_behaviour_protocol` 8398 → 1429 字，
  `_post_rules` 1010 → 202 字。
  - **整节删除**：02b 沟通、02c 接不上、02d 注意力、07 关注谁；
    分别交给 post_rules / 历史块 / 硬逻辑 / `focus_sessions`。
  - 保留的只有**协议**：工具目录、长度一句、接话一句、查询一句、`[[CALL]]`、
    `[[MEMO]]`、`[[THINK]]`、`[[MOOD]]`、`<sticker>`、底线一句、人称、NapCat。
  - 工具菜单改动态分类 + 先 `[[TOOLS|分类]]` / `搜:词`；NapCat 参数按需 `napcat_help`。
  - 哈希/网页提交从 post_rules 移回工具 description，不再每轮重复。
- 被删掉的人设卡规则仍保留在系统协议里（括号/称呼/被纠正/红线），selftest 已同步。

### 新增（第八轮：说话语料库 / 自定义风格上传 / 人设卡再瘦身）

- **说话语料库 `services/style_vault.py`**：每个人的原话按群存进
  `data/style_corpus/<gid>/<qq>.jsonl`（默认每人最近 200 条）。
  **省流模式也会记**（只写文本、不调模型），学风格不再依赖短期窗口。
- **白月光风格改成「摘要 + 原话」**：`render_style_only()` 除了风格摘要，
  再带 2~3 句这个人的原话，模型才能真正学到句长/标点/口头禅。
- **自定义风格上传 → 主人特批**：
  - `/白月光样本 @某人`：本人上传自己的说话样本（一段话 / 多行 Q&A / 引用消息）；
  - `/白月光特批 @某人`：仅主人，通过后才进 prompt；
  - `/白月光驳回 @某人`、`/白月光特批 列表`、`/白月光样本 查看`。
  存储 `data/style_vault.json`，只有 `approved` 的会生效。
- **人设卡再去掉「底线」和「说话案例」**：只剩「你是谁（含外貌）/ 性格」；
  红线（不碰政治/敏感、不越界、不装懂）移进固定区 `▍09 别学坏`。
- 调参新增：`STYLE_VAULT_ENABLED / STYLE_VAULT_MAX_SAMPLES /
  STYLE_VAULT_SAMPLE_CHARS / STYLE_VAULT_MAX_UPLOAD_CHARS`。

### 修复（第八轮补：人设卡瘦身后的回归）

- **人设卡删掉的规则没有丢**：
  - 「不要用括号」在 `THINK_PROTOCOL`；
  - 「别人纠正你先重新理解、被纠正不丢人」+「问的是你对 X 的称呼，不是你的名字」
    移进 `COMM_PROTOCOL`（七条 → 九条）。
  `selftest_all` 的 4 条旧断言改成检查 `services/llm.py`，不再要求人设卡里必须有。
- **新命令进菜单**：`/白月光样本`、`/白月光特批`、`/白月光驳回`
  补进 `plugins/help_menu` 的称呼区块和 `/帮助` 全量清单。
- **`audit_pipeline [8]` 过时断言**：`MOOD_FROM_JUDGE=true` 时她自报 emotion
  只写心情原话、不动数值；测试改成断言 `label`，不再误报「心情没变化」。

### 调整（第七轮：流水线可核对 / 白月光进档案 / 人设卡瘦身）

- **流水线每块加 ≤30 字小标题**：`▍状态｜心情值 · 当前状态 · 那件事的心理`、
  `▍档案｜称呼 · 身份 · 好感 · 印象最深的事` 等，`/测函数 模板` 一眼能对上顺序。
  完整对照表见 [reply-pipeline.md](archive/reply-pipeline.md)。
- **每一步都留状态**：`stages` 记 `✓ / 数字 / 0 / — / ×异常名`，
  `/测函数 流水线` 直接显示上一次哪一步没拿到；失败不再静默消失。
- **心情块改用 `mood.status_block`**：一次给全 心情词+数值 / 刚才那下 / 心里话 /
  峰值事件+当时心理 / 群聊总结 / 对他的好感走势；拿不到时兜底成「心情值 ±N」一行，
  **保证「此刻状态」永远在**。
- **白月光写进档案块**：`white_moon.profile_note` 现在同时给「是谁 + 分数 + 风格摘要」；
  回复前的风格块改用 `render_style_only`，只给「怎么借」，不再重复「是谁」。
- **人设卡瘦身**：`小鲸鱼proplus.txt` 只保留「你是谁（含外貌）/ 性格」，
  原卡备份为 `小鲸鱼proplus.txt.bak`（不会被 `*.txt` 扫描）。
  长度/分条/记忆/表情/心里话/心情/人称/工具目录全部交给固定区协议。

### 重构（第六轮：称呼唯一入口 / 回复前动态取数流水线）

- **称呼统一入口 `services/callname.py`**：`他叫她` / `她叫他` 两个方向各一个字段，
  群级索引（`data/callname.json`）+ 档案双写，永不再互相覆盖。
  - `/叫你 X` = 他叫她 X（喊这个名字 = 在叫她，**群内硬匹配**）；
  - `/叫我 Y` = 她叫他 Y；`/叫他 @某人 Z` = 她叫他 Z；
  - `[[CALL]]` 会做方向纠错（模型把两个方向写反时自动换回来）；
  - `profile_update` 里带「叫我/叫你/称呼」的内容不再写进「注意」，自动救回 callname；
  - 新工具 `set_call(who=user|bot, name)`，方向显式，不靠模型分你我。
- **回复前动态取数流水线 `services/reply_pipeline.py`**：
  固定上下文只留 人设卡 + 工具 + 标签处理过的上下文 + 称呼；
  拿到要回的那句之后按顺序取：心情值/当前状态 → 对方档案+好感+印象最深的一件事 →
  判读模型对这句话的总结 → 用总结查记忆 → 用总结查图书馆（标注人称不是现在的你我他）→
  这条消息是否已回过 → 白月光风格 → 综合回复。
  每一步独立降级、总预算 `PIPELINE_BUDGET=7s`（记忆与图书馆并行），
  `/测函数 流水线` 看每步耗时、`/测函数 称呼` 看称呼索引。
- 有流水线时，主 prompt 不再重复注入 心情/好感/记忆目录/图书馆/白月光（省 token、避免说两遍）。
- 白月光/表达学习在学风格前先洗掉协议残留（`〔引`、`引用`、`<reply:>`、`⭐`），
  之前把「引用」当成了某人的口头禅。

### 修复（第五轮：识图原图 / 图书馆路由 / 调用可见）

- **识图还是糊**：QQ 消息段里的 `url` 常是预览图。现在先问 NapCat `get_image`
  拿原图地址和本地缓存，再把所有候选都下一遍**取字节数最大**的那份；
  如果消息段自报 `file_size` 比拿到的大 15% 以上，日志直接警告「可能还是压缩图」。
- **图书馆（算力机 RAG）根本没在工作**：实测 8001 是 Agentic RAG v3，
  路由是 `/api/agent/query`、`/api/agent/smart-query`，**没有** `/api/agent/recall`、
  `/api/agent/reflection` —— 我们一直在打 404，所以图书馆永远空。
  现在按「结构化 → smart-query → query」三级降级，404 只试一次；
  发现库里语料不是聊天记录（实测是 `poet.song.rank.*` 宋词）→ 自动暂停 6 小时并告警。
- **回忆超时**：`AGENT_RECALL_TIMEOUT` 6 → 3 秒（它在回复关键路径上）。
- **兜底命中不算「本地搜到」**：`recent_about` 的兜底结果不再阻止图书馆查询。
- **调用可见**：新增 `/测函数 记忆` —— 记忆库条数、召回次数/命中率/兜底次数、
  图书馆调用/成功/暂停状态。
- **新增 `scripts/library_sync.py`**：把 `data/dialogue/*.jsonl` 转纯文本上传到 RAG
  并建索引，图书馆要真的能回忆旧聊天，得先跑它。

### 修复（第四轮：记忆回想 / 艾特插队 / 唱歌重复）

- **记忆回想变少**：召回用的 query 从「整段带行号/⭐/名字前缀的文本」改成
  「最近 3 句正文」（`_recall_query`），关键词没命中时再补「关于这个人的最近几条」
  （`memory_v2.recent_about`）；上下文里已回过的消息打 `〔已回〕`；
  规矩新增「提到上次/之前/相似的事 → 先 [[MEMORY|关键词]] 翻一遍」。
- **艾特插队**：分段 buffer 里出现 @ 她 / 引用她 → `_URGENT` 标记，worker 只留 0.35s
  合并窗口就处理，并且跳过 `REPLY_INTERVAL`（以前要排队等安静窗口）。
- **一次点三首歌**：`sing` 工具加在途/近期去重（同会话上一首没唱完、同一首歌 15 分钟内
  → 不重复点），对应真机「让她随便唱首歌，结果发了三次歌」。
- **协议残留**：`〔引〕` / 半个 `〔` 一律清掉（上一轮已修 `〔引用…〕`）。

### 体检

- 新增 `docs/review-2026-11-24.md`（内部，不进发行仓）：DSH 桥 vs Aurabot 能力对照、
  重复调用清单、死代码、两处设计注意点。

### 新增（第三轮：模型自由搭配 / 省流模式 / 引用修正）

- **模型自由搭配**（`services/providers.py`）：主聊天 / 心情判读 / 收发件人审核 / 识图 /
  风格学习 / 对话状态 / 档案挖掘，每个角色单独配 provider + 模型 + base_url + key，
  支持 deepseek / glm / qwen / xai / openai / 自己的中转站；`/测函数 模型` 查看。
  详见 [model-providers.md](model-providers.md)。
- **群聊「全局 / 省流」模式**（`GROUP_WATCH_MODE`）：`lite` 下消息照常记录
  （现场 / 白月光 / 心情都不受影响），只有 @她 / 引用她 / 喊名字 / 收件人是她 才入队回复；
  不主动插话、不做表达学习；私聊不受影响。
- **引用精确到消息号**：`<reply:#消息号>`；发送前校验真实 mid，行号自动映射，非法就不引用，
  修「引用回复回错人（回成上面/下面那条）」。
- **同一件事不回两遍**：`GROUP_REPLY_COOLDOWN`（默认 45s），刚回过这个群时非点名消息只进上下文。
- **协议残留清理**：`〔引用…〕` / `〔引〕` / 半个 `〔` 不再出现在气泡里。

### 调整（第二轮：响应速度 / 识图 / 心情）

- **回复更快**：群聊分段等待 6s → 3.5s；@ 她 / 喊名字时只等 1.2s（`SEGMENT_WAIT_FAST`）；
  同一会话回复间隔 `.env` `REPLY_INTERVAL` 20 → 6；多气泡停顿上限 4.5s → 1.6s。
  新增 `[LAT]` 日志（段等待 / 审核 / 模型 / 发送 / 总计），方便继续定位。
- **识图更清楚**：默认改为「自己下载 → 压到 1568 长边（quality 90）→ base64 传」，
  不再把 QQ 链接丢给模型；`VISION_PROVIDER=glm` 走智谱，默认 `glm-4v-plus`
  （实测 3~4s；`glm-5.3-flash` 能读图但「始终思考」要 23s）。
  Qwen 兜底候选改成实测可用的 `qwen3-vl-plus` 等（`Qwen3.5-Flash` 会 404 / 30~50s）。
- **心情真的参与决策**：关注期新增 `MOOD_FOLLOW_MIN=-35` —— 心情低于它就不再追着接话，
  只回明确 @ / 喊名字的（以前关注期完全不看心情）。

### 新增

- **算力机开关 `NODE_COMPUTE`**
  - `1`（默认）= 走 `node.py`：画图 / 翻唱 / 语音 / 视频理解排任务给算力机；
  - `0` = 不加载本项目 `draw` / `song_cover`，不注册 `draw_image` / `draw_from_pixiv` /
    `pixiv_search` / `sing` / `video_understand`，画图/唱歌交给 `plugins/third` 的本机插件；
  - 工具目录会切成「本机模式」，提示模型先 `[[TOOLS|搜:画图]]` 找三方工具。
- **DeepSeek Harness 改代码桥 `CODE_AGENT=harness`**
  - QQ（主人）`/h <需求>` 或 `/改代码 <需求>` → 本机 DSH → 结果原样回 QQ；
  - 支持 `HARNESS_BACKEND=cli|http`，`{prompt}/{cwd}/{session}` 占位；
  - 带上人格/心情/好感/白月光/相关记忆（`HARNESS_CONTEXT=1`）；
  - 与 `self_tool` 互斥：harness 模式不加载 `plugins/self_tools`，也不注册 `self_tool_*`/`mytool_*`；
  - 只有主人能用，输出脱敏、超时 kill、分段发送。

### 修复

- **其他人格卡仍出现「鲸鱼尾巴 / 呆毛」**：prompt 里写死的人格专属词已清掉：
  - `[[MEMO]]` 示例的「尾巴都摇起来了」→ 通用描述；
  - `[[CALL]]` 示例的「小鲸鱼」→ 通用名字；
  - 人称规则、每日总结、表情管理员、档案卡页脚、`_bot_name` 兜底、切人格文案等；
  - 「被讨厌就闭嘴」的强信号不再写死「鱼/鲸鱼」，改用「机器人/bot/AI + 当前人格名」。
- **`/测函数 prompt` 换人格卡后认不出角色卡**：改为「第一条 system 消息 = 角色卡」，
  不再靠 `DeepSeek 小鲸鱼` 前缀匹配（自定义人设卡也能正确统计）。
- **发行仓清理**：移除误入的 34MB `scripts/.xray_runtime/xray.exe`；
  `.env.example` 里私人基础设施地址（阿里云专属 workspace、自建站点）改为公开默认值；
  `services/tts.py` 的兜底 host 改为公共域名。

### 文档

- 新增 `docs/harness-bridge.md`（DSH 桥：配置、后端、安全、与 Aurabot 的区别）；
- `docs/deployment.md` 增加 `NODE_COMPUTE` / `CODE_AGENT` 两节；
- `docs/features.md` 增加第 17/18 节。

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
