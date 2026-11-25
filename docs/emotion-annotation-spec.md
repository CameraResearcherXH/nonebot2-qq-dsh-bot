# 情绪标注规范（200 条）

> **导航**：[设计主文档](DESIGN.md) ｜ 相关：[情绪](emotion-dynamics-design.md) · [收发件人](addressing-design.md) · [依据](design-foundations.md)
> **本文回答**：200 条怎么抽、每条标什么、怎么判、怎么算一致率、怎么用。
> **本文不回答**：模型怎么调、代码怎么写。
> 数据文件在 `data/eval/`（含真实昵称，**不进仓库**）；本文只写规范。

---

## 0. 为什么需要它

100 条金标准测的是「**取没取到**」；
但它测不了「**判读档位对不对**」——即 `impact=-1` 到底是不是 -1。

没有这把尺子，§1.6 的步长、§1.7 的系数、`confidence` 的校准都只是自洽。
所以需要 **200 条人工标注事件**，作为情绪模块的外部效标（G3）。

---

## 1. 抽样：标签源是**她自己的心理活动**（2026-11-25 修正）

> ⚠️ **旧版做法（按关键词从群聊抽 200 条）已废弃**：那是拿外部对话去猜她的心情。
> 产物 `emotion_annotation_200*` / `emotion_annotation_v1_1*` 只留档，不要再用于评测。

正确口径：

```
输入（模型看到的） = src   她当时在回的那句话 + 上下文
标签（人标的）     = think 她自己写的 [[THINK|…]]
```

数据来源：

| 文件 | 内容 |
|---|---|
| `data/mood_think.json` | `{session_key: [{at, text, src, reply, auto}]}`，`text` 就是她的心理活动 |
| `data/mood_history.jsonl` | `{at, delta, emotion, reason, about_qq, value}`，**只作参考，不当标签** |

抽样规则（`scripts/build_emotion_annotation_think.py`）：

- 只取 `auto=false`（她自己写的，不是后台补写的）；
- 必须有 `src`（否则判不了 target）；
- 按 `(think, src)` 去重；
- 按时间等距取到目标条数（默认 200）；
- `category` 只为分组统计：impact>0 → `praise`，<0 → `criticism`，=0 → `neutral/world`。

**标注时只看 `think`，不要用 `src` 去猜她「应该」是什么心情。**
`src` 只回答一个问题：这件事关于谁（target）。

---

## 2. 每条标什么（字段）

```json
{
  "id": "ethink_001",
  "channel": "group",
  "session": "group_100000016",
  "at": 1795...,
  "think": "又绕回肥鱼了，烦",
  "src": "苏木微雪: 是让你养一条小肥鱼",
  "reply": "我哪有肥鱼可以养……",
  "judge": {"emotion": "被绕得有点烦", "delta": 2.94, "reason": "..."},
  "category": "criticism",

  "label": {
    "impact": -1,
    "arousal": "mid",
    "dominance": "mid",
    "target": "recipient",
    "relation_relevance": 1.0,
    "unexpected": 0.5,
    "confidence": 0.9,
    "reason": "熟人当众调侃，不算恶意",
    "is_sarcasm": false
  },
  "meta": {
    "labeler": "A",
    "round": 1,
    "status": "draft",
    "note": ""
  }
}
```

### 2.1 各字段取值

| 字段 | 取值 | 说明 |
|---|---|---|
| `impact` | `-2 / -1 / 0 / +1 / +2` | 这件事让她的**效价**往哪动了多少 |
| `arousal` | `low / mid / high` | 激动程度（不是好坏） |
| `dominance` | `low / mid / high` | 有没有掌控感 |
| `target` | `speaker / recipient / other / world / self` | 这件事**关于谁**（5 值，见 ADR-021） |
| `relation_relevance` | `0 / 0.5 / 1` | 和「她与 target 的关系」相关程度 |
| `unexpected` | `0 / 0.5 / 1` | 偏离预期程度（意外） |
| `confidence` | `0~1` | 标注者对自己判断的把握 |
| `reason` | ≤30 字 | 一句话理由（事件，不是心理活动） |
| `is_sarcasm` | bool | 是否反讽/正话反说 |

### 2.2 五档锚点（必须背下来）

| impact | 含义 | 例句 |
|---|---|---|
| **-2** | 被当众羞辱 / 被重要的人否定 / 背叛 | 「你就是个废物，滚」 |
| **-1** | 被冷落一句 / 被轻轻调侃 / 小失望 | 「随便吧」「你怎么这么菜」 |
| **0** | 普通接话，没有情绪 | 「今天更新了吗」 |
| **+1** | 被夸一句 / 被接住 / 小开心 | 「你今天挺可爱」 |
| **+2** | 被认真对待 / 重要的人表达在乎 | 「我一直记得你说过的话」 |

### 2.3 边界规则（先看这 8 条）

| 情况 | 规则 |
|---|---|
| 熟人调侃 vs 陌生人嘲讽 | 同句，熟人 -1，陌生人 -2 |
| 公开 vs 私下 | 公开重一档 |
| 第 1 次 vs 第 N 次 | 第 N 次降一档（重复不叠加） |
| 有道歉 vs 没道歉 | 有道歉，负面 +1 档回补 |
| 骂的是别人（world） | 只改心情，`relation_relevance=0` |
| 反讽（「真棒呢」） | 按**实际含义**标，不按字面 |
| 引用里骂她 | 算 -2，且 `is_sarcasm` 不一定要 true |
| 她主动挑事 | 也按「她被怎么对待」标，不替她辩护 |
| **对方只回短句**（哦/嗯/？/6） | **不算冷落**；看她自己的反应，没反应就 0 |

> ⚠️ `impact` 标的是**她的心理状态**，不是对方的行为。判据优先级：
> ① 她自己的下一句（炸毛/委屈/害羞/道谢/照常聊）；② 明确针对她的评价行为（辱骂/叫她滚/道歉/夸她/贴贴）；
> ③ 两者都没有 → 0。详见 `docs/emotion-labeling-guide.md` §0.1。

---

## 3. 标注流程

```text
① 自动生成 200 条候选（含上下文、收件人猜测、auto_suggest）
② 标注者 A 全标（200 条）
③ 标注者 B 标其中 50 条（重叠集，随机抽，不看 A）
④ 算一致率：
   · impact 二次加权 kappa ≥ 0.60
   · target 准确率 ≥ 0.80
   · arousal 准确率 ≥ 0.60
⑤ 分歧 ≥2 档的条目 → 第 3 人裁决（adjudication）
⑥ 冻结为 v1（`status=agreed`），之后改题要升版本
```

### 3.1 盲标要求

- 标注界面**不显示** `auto_suggest`（避免锚定）；它只用于事后对比「规则 vs 人」。
- 不显示模型判读结果。
- 顺序随机，不按群/时间聚在一起。

### 3.2 AI 预标 + 复核（2026-11-25 实做）

```text
预标  scripts/prelabel_emotion_llm.py      独立模型（GLM）盲标，不给 category / auto_suggest
复核  scripts/finalize_emotion_labels.py   机械归一（0/0.5/1、arousal/dominance/confidence）+ 逐条改标
准入  scripts/check_emotion_labels.py --strict   枚举 / 取值 / 标签退化，任何硬错误都 FAIL
产物  data/eval/emotion_annotation_200_v1.jsonl   meta.status = ai_reviewed_v1
补充  scripts/sample_addressed_emotion.py   按「@她 / 引用她 / 喊她名字」重抽
      scripts/merge_emotion_additions.py    v1 + 70 条冲她说 → v1.1（270 条）
```

**`ai_reviewed_v1` / `ai_reviewed_v1_1` 不等于 §3 ⑥ 的冻结 v1**：它们只是「机器复核过的初稿」。
人工抽检（重点看 impact≠0、hard/tease、以及复核改过的 39 条）后，
把 `meta.labeler` 改成本人、`meta.status` 改成 `agreed`，才算冻结。
复核发现与剩余风险见 `docs/emotion-annotation-review-20261125.md`；
人工怎么标、怎么查见 `docs/emotion-labeling-guide.md`（配 111 条清单）。

### 3.3 一致性指标

```text
impact:  quadratic weighted kappa（5 档有序）
arousal: Cohen's kappa（3 档）
target:  accuracy
relation_relevance: MAE
confidence 校准: ECE（把 confidence 分 5 桶，比较置信 vs 实际正确率）
```

目标：

| 指标 | 目标 |
|---|---|
| impact QWK | ≥0.60 |
| impact 完全一致率 | ≥0.50 |
| impact ±1 一致率 | ≥0.85 |
| target 准确率 | ≥0.80 |
| arousal 准确率 | ≥0.60 |

---

## 4. 怎么用（评测脚本的口径）

```text
输入：200 条 label + 模型对同一窗口的输出
输出：
  · impact_exact / impact_within1 / impact_MAE
  · arousal_acc
  · target_acc
  · relation_rel_MAE / unexpected_MAE
  · ECE（confidence 校准）
  · 分类别混淆矩阵（tease 最容易判错）
  · 按 category 的误差（看是「骂」判不准还是「调侃」判不准）
```

上线门槛：

```text
impact_within1 ≥ 0.85
target_acc    ≥ 0.80
ECE           ≤ 0.15
```

不达标 → 只调映射表/案例库，不动主模型。

---

## 5. 文件与工具

| 文件 | 内容 | 是否进仓库 |
|---|---|---|
| **`data/eval/emotion_think_candidates.jsonl`** | **心理活动候选（190 条，标签源）** | ❌ |
| **`data/eval/emotion_annotation_think_v1.jsonl`** | **当前版（`ai_reviewed_think_v1`）** | ❌ |
| `data/eval/emotion_annotation_think_v1.md` | 复核表（think / src / judge / label） | ❌ |
| `data/eval/emotion_annotation_think_v1_report.json` | 分布与规模 | ❌ |
| `data/eval/emotion_annotation_200*.jsonl` `emotion_annotation_v1_1*` | **旧口径（按对话猜心情），已废弃，仅留档** | ❌ |
| `data/eval/emotion_annotation_stats.json` | 旧版分层统计 | ❌ |
| **`scripts/build_emotion_annotation_think.py`** | **从 THINK 抽候选** | ✅ |
| **`scripts/finalize_emotion_think.py`** | **THINK → 标签 → v1** | ✅ |
| `scripts/build_emotion_annotation.py` | 旧版关键词抽样（留档） | ✅ |
| `scripts/prelabel_emotion_llm.py` | 旧版 AI 预标（留档） | ✅ |
| `scripts/finalize_emotion_labels.py` | 归一规则 `_normalize`（被 THINK 版复用） | ✅ |
| `scripts/sample_addressed_emotion.py` | 旧版按收件人重抽（留档） | ✅ |
| `scripts/merge_emotion_additions.py` | 旧版合并（留档） | ✅ |
| `scripts/check_emotion_labels.py` | 格式/取值/退化校验 | ✅ |
| `scripts/eval_emotion_labels.py` | 一致率评分 | ✅ |
| `docs/emotion-annotation-spec.md`（本文） | 规范 | ✅ |

生成命令：

```bash
sudo python3 scripts/build_emotion_annotation.py
sudo python3 scripts/build_emotion_annotation.py --limit 200 --seed 7
```

---

## 6. 和 100 条检索集的关系

| | 100 条检索集 | 200 条情绪集 |
|---|---|---|
| 测什么 | 取没取到 | 判得对不对 |
| 标签 | 证据 ref | impact/arousal/target |
| 费用 | 0（本地） | 0（人工） |
| 归属 | G4 检索 | G3 情绪 |
| 共用 | 同一批对话窗口 | 同一批对话窗口 |

**两把尺子都齐了，情绪/检索才算真正闭环。**
