# 用量查看与 prompt 体检

> 代码：`services/llm_usage.py`、`services/usage_chart.py`、`services/prompt_report.py`
> 命令：`/用量`、`/测函数 用量`、`/测函数 prompt`

## 1. 为什么要做

角色扮演的 prompt 很长（人设 + 规矩 + 历史 + 心情 + 档案），
真正烧钱的不是 completion，而是**每轮几万 token 的 prompt**。

只看「花了多少钱」不够，还要知道：

- 哪一类调用最贵（chat / 工具循环 / 心情判读 / 收发件人审核）；
- 缓存命中率多少（DeepSeek 的 prefix cache 命中价差很大）；
- prompt 里哪一段占得最多（人设？规矩？历史？）。

所以有两套报表：**用量** 和 **prompt 体检**。

## 2. 用量：`/用量`

每次模型调用都会记一条到 `data/llm_usage.db`（sqlite）：

| 字段 | 含义 |
|---|---|
| `purpose` | `chat` / `tool_loop` / `mood_judge` / `scene_audit` / `preflight` / `vision` … |
| `provider` | 按 `base_url` 判断（deepseek / glm / 其它） |
| `model` | 实际模型名 |
| `prompt` / `completion` | token 数 |
| `cache_hit` | 命中缓存的 prompt token（DeepSeek 返回 `prompt_cache_hit_tokens`） |

`/用量` 输出：

- 按用途汇总：次数、prompt/completion、缓存命中率；
- 最贵 Top5 / 最省 Bottom5；
- 横向柱状图 + 扇形图（见下）。

`/用量 文字` 只看数字，不发图。

## 3. 图表怎么来的

- Google Image Charts（`chart.googleapis.com`）已经下线，返回 404；
- 改用兼容同参数的 `image-charts.com`；
- 可用 `USAGE_CHART_BASE` 换成自建服务；
- 横向排行用 `cht=bhg`，扇形用 `cht=p3`（只画前 4 + other，避免标签重叠）；
- 图片下载到 `data/temp/` 后用 `file://` 发出去。

`services/usage_chart.py::build_charts(items, title, top_n, pie_top)` 是通用函数，
`/用量` 和 `/测函数 prompt` 共用。

## 4. prompt 体检：`/测函数 prompt`

它不调模型，只读 `data/llm_trace/*.jsonl` 里最近一次真实请求的 messages，
按「段」统计字数和占比：

| 段 | 识别方式（示例） |
|---|---|
| 角色卡 | `DeepSeek 小鲸鱼 · 角色卡` |
| 规矩/协议 | `▍00`…`▍13`、`下面是几条规矩` |
| 当前消息 | `user` 角色的当前输入 |
| 历史 | `【历史对话】` |
| 心情状态 | `▍现在的状态` |
| 好感底色 | `▍你对…的底色` |
| 人物卡 | `▍…（QQ` |
| 白月光 | `▍白月光模式` |

输出：

- 每段字数 / 占比（横向柱状图）；
- 占比饼图；
- 角色卡内部按「一、二、三…」拆，规矩按 `▍NN` 拆，给出每块字数和块内位置；
- 缓存报告：今天各类调用的次数、prompt 总量、命中率。

`/测函数 prompt 文字` 只出数字。

## 5. 缓存命中率怎么看

| 调用类型 | 典型命中率 | 原因 |
|---|---|---|
| `chat` | 70%~85% | 人设 + 规矩是固定前缀 |
| `tool_loop` | 90%+ | 同一轮工具调用复用前缀 |
| `mood_judge` / `scene_audit` | 0%~2% | prompt 开头就是动态内容，没有稳定前缀 |

判读/审核的 token 占比通常只有 10%~15%，所以**优先优化 chat**：
少调用、压静态块、别把动态内容塞到前面。

## 6. 一个真实例子（2026-11-23）

- `chat`：451 次，prompt 10.9M token，缓存命中 **80.2%**；
- `tool_loop`：99.2%；
- `preflight/judge/audit`：0%~1.7%，但 token 只占约 13%；
- prompt 占比：角色卡 43.5% + 规矩 38.2% = 81.7%，当前消息 3.7%，硬要求 3.2%，
  历史 2.4%，心情 1.4%，好感 0.1%。

> 这就是后来做「角色卡 / 规矩去重」的直接依据：先砍静态大头，收益最大。
