#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""eval_intent.py —— 用真实 trace 离线评测「意图层」（2026-11-27 用户提案）。

它解决什么问题
--------------
`services/atmosphere.py` 的第一版意图层是**只加不减**的：
  · prompt 多四行（在聊 / 最后一句在问什么 / 该不该接 / 已有人答）；
  · `queries{memory,library,web}` 拿去驱动记忆/图书馆检索；
  · 8 行历史补〔已回〕。
这些都只是「多给模型一点信息」，错了顶多没用。

但它同时给了 `need_reply` —— 一旦拿它当闸门（`ATMO_GATE=1`，
主动插话时 `no` 就不开口），判错就会让她该说话时闭嘴。
所以开闸门前必须先证明它准：拿 `data/llm_trace` 的真实记录离线重放，
**用的是线上同一条 prompt**（`atmosphere.build_prompt`），不另写一份。

标签从哪来（弱标签，必须记住）
------------------------------
  · 正例：这一段里有 **⭐ / → 你**（在叫她）→ 应该接。输入用**整段**，
    和线上 `read(text=分段文本)` 一模一样；没有 ⭐ 的段（她在插话）不算正例；
  · 负例：同一窗口里**没有 ⭐、也没有〔已回〕**的历史行 → 她当时没接 → 不应该接。

这是「行为自监督」，不是人工标注：
  · 她没接 ≠ 不该接（可能在忙 / 在看戏 / 心情低）；
  · 她接了 ≠ 必须接（可能只是插话）。
所以分数只适合做**版本间比较**和**开闸门的门槛**，不是绝对准确率。
判错样本会全部落盘（`--out`），人工抽看 10 条比看总分有用。

三种跑法
--------
    # 1) 不花钱：验证脚本/解析/打分本身（用确定性规则假装模型）
    python scripts/eval_intent.py --mock

    # 2) 指定某天的 trace（复盘用）
    python scripts/eval_intent.py --trace data/llm_trace/2026-11-27.jsonl --limit 204

    # 3) 最近 N 天（默认 2 天）
    python scripts/eval_intent.py --days 2 --concurrency 4 --out data/eval/intent.jsonl

退出码：0 = 跑完（不管准确率）；1 = 没有可用样本 / 全部调用失败。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TRACE_DIR = Path(os.getenv("LLM_TRACE_DIR", "data/llm_trace"))
EVAL_DIR = Path(os.getenv("EVAL_DIR", "data/eval"))

# 这些前缀是「当前消息」块里附带的系统说明，不是群友说的话
_CUR_NOISE = ("【这是当前图片", "【这是当前视频", "（群友发了张图片）",
              "（群友发了段视频）", "----------- 以下是")

_HIST_RE = re.compile(r"^\d+\.\s+(.*)$")
_SEG_RE = re.compile(r"^\[(\d+)\]\s*(.*)$")
# 「→ 你」必须刚好是收件人：昵称「你已被管理员…禁言6小时」会被子串误判
# （和 services/atmosphere.py 的 _ADDR_RE 保持一致）。
_ADDR_RE = re.compile(r"→\s*你(?=[：:]|$)")
_MEDIA_RE = re.compile(r"（图片/表情）|\[图片\]|\[表情\]|\[视频\]|\[图片识别结果\]")

# 这些是「指令/玩法」，不该拿来判「要不要接话」
_CMD_HINTS = ("/测函数", "今日小猪", "今日运势", "来点美少女", "画图", "翻唱")


def _load_env_file(path: Path) -> None:
    """脚本可能脱离容器跑：优先读 .env（不覆盖已有环境变量）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if v[:1] not in ("'", '"') and " #" in v:
            v = v.split(" #", 1)[0].rstrip()
        v = v.strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_env_file(ROOT / ".env")


# ============================================================
# 1) 从 trace 还原「8 句窗口 + 当前这句」
# ============================================================
def parse_trace(rec: dict) -> tuple[list, list, str]:
    """返回 (历史行, 当前段的行, 当前段的原文)。

    历史行 / 段行都保留「谁 → 谁」前缀（和线上格式一致）；
    第三项是**线上真正传给 `atmosphere.read(text=...)` 的那段**（含段头/⭐），
    正例必须用它 —— 否则评测看到的输入和线上不是同一条。
    """
    hist: list = []
    for m in rec.get("msgs") or []:
        c = str(m.get("content") or "")
        if "【历史对话】共" not in c:
            continue
        for ln in c.splitlines():
            mm = _HIST_RE.match(ln.strip())
            if mm:
                hist.append(mm.group(1).strip())
    raw = ""
    for m in reversed(rec.get("msgs") or []):
        if str(m.get("role")) == "user":
            raw = str(m.get("content") or "")
            break
    seg: list = []
    for ln in raw.splitlines():
        s = ln.strip()
        if not s or s.startswith(_CUR_NOISE):
            continue
        mm = _SEG_RE.match(s)
        if mm:
            seg.append(mm.group(2).strip())
    return hist, seg, raw.strip()


def _clean_line(s: str) -> bool:
    """这条值不值得当样本（去掉媒体占位、指令、空行）。"""
    t = str(s or "").strip()
    if len(t) < 2:
        return False
    if _MEDIA_RE.search(t):
        return False
    return not any(k in t for k in _CMD_HINTS)


def build_samples(rec: dict, *, neg_per_trace: int = 2,
                  msgs_n: int = 8) -> list[dict]:
    """一条 trace → 1 个正例 + 最多 N 个负例（弱标签，见模块 docstring）。"""
    key = str(rec.get("key") or "")
    if not key.startswith("group_"):
        return []
    reply = str(rec.get("reply") or "").strip()
    if not reply or "(不想回复)" in reply:
        return []
    hist, seg, raw = parse_trace(rec)
    if not seg:
        return []
    t = str(rec.get("time") or "")
    gid = key.split("_", 1)[-1]

    out: list[dict] = []
    # ---- 正例：这一段里**有 ⭐ / → 你**（在叫她）→ 应该接 ----
    # 为什么用「整段」而不是「某一条」：线上传的就是整段（`read(text=分段文本)`），
    # 闸门要回答的也是「现在这段要不要开口」。没有 ⭐ 的段 = 她在插话 →
    # 没有「必须接」的正例，这条 trace 只贡献负例。
    if any(s.startswith("⭐") or _ADDR_RE.search(s) for s in seg):
        out.append({
            "label": "yes",
            "text": raw,
            "window": hist[-max(2, msgs_n - 1):],
            "trace_time": t, "gid": gid, "src": "addressed",
        })

    # ---- 负例：同窗口里没有 ⭐ / 〔已回〕 的历史行 → 她当时没接 ----
    cand = [(i, h) for i, h in enumerate(hist)
            if "⭐" not in h and "〔已回〕" not in h
            and not h.startswith("你 →") and _clean_line(h)]
    for i, h in cand[-max(0, int(neg_per_trace)):]:
        out.append({
            "label": "no",
            "text": h,
            "window": hist[max(0, i - max(2, msgs_n - 1)):i],
            "trace_time": t, "gid": gid, "src": "silent",
        })

    # 同一个窗口里正负例文本不能重复（否则等于同一个样本给了两个标签）
    seen, uniq = set(), []
    for s in out:
        k = (s["text"], s["label"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)
    return uniq


def iter_traces(paths: list[str], days: int = 2):
    if paths:
        files = [Path(p) for p in paths]
    else:
        cutoff = time.time() - max(1, days) * 86400
        files = sorted(f for f in TRACE_DIR.glob("*.jsonl")
                       if f.stat().st_mtime >= cutoff)
    for f in files:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


# ============================================================
# 2) 预测（真模型 / mock）
# ============================================================
def mock_predict(sample: dict) -> str:
    """确定性假模型：只认「⭐」和问号。用来验证脚本，不代表模型水平。"""
    t = str(sample.get("text") or "")
    if "⭐" in t or _ADDR_RE.search(t):
        return "yes"
    return "no"


async def _predict(sample: dict, sem: asyncio.Semaphore,
                   timeout: float, mock: bool) -> dict:
    async with sem:
        if mock:
            return {**sample, "pred": mock_predict(sample), "data": {"mock": True}}
        from services import atmosphere
        try:
            data = await atmosphere.classify(
                sample["window"], sample["text"],
                record_usage=False, timeout=timeout)
            return {**sample, "pred": atmosphere.need_reply_of(data) or "unsure",
                    "data": data, "err": ""}
        except Exception as e:
            return {**sample, "pred": "error", "data": {},
                    "err": f"{type(e).__name__}: {e}"}


# ============================================================
# 3) 打分
# ============================================================
def score(rows: list[dict]) -> dict:
    """把 unsure 当「不挡」（=yes），因为闸门只在 no 时闭嘴。"""
    conf = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    errs = 0
    for r in rows:
        pred, label = str(r.get("pred") or ""), str(r.get("label") or "")
        if pred == "error":
            errs += 1
            continue
        act = "no" if pred == "no" else "yes"     # 闸门视角
        if label == "yes" and act == "yes":
            conf["tp"] += 1
        elif label == "yes" and act == "no":
            conf["fn"] += 1
        elif label == "no" and act == "no":
            conf["tn"] += 1
        else:
            conf["fp"] += 1
    n = sum(conf.values())
    acc = (conf["tp"] + conf["tn"]) / n if n else 0.0
    prec = conf["tp"] / (conf["tp"] + conf["fp"]) if (conf["tp"] + conf["fp"]) else 0.0
    rec = conf["tp"] / (conf["tp"] + conf["fn"]) if (conf["tp"] + conf["fn"]) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    # 严格口径：unsure 不算对
    strict = sum(1 for r in rows
                 if str(r.get("pred")) in ("yes", "no")
                 and str(r.get("pred")) == str(r.get("label")))
    strict_acc = strict / n if n else 0.0
    return {"n": n, "errors": errs, "acc": acc, "strict_acc": strict_acc,
            "precision": prec, "recall": rec, "f1": f1, **conf}


def _report(rows: list[dict], out: Path, show: int = 8,
            mock: bool = False) -> int:
    sc = score(rows)
    print("\n================ 意图层离线评测 ================")
    print(f"样本            {sc['n']}（正例 {sc['tp'] + sc['fn']}｜"
          f"负例 {sc['tn'] + sc['fp']}）"
          + (f"｜调用失败 {sc['errors']}" if sc["errors"] else ""))
    print(f"准确率          {sc['acc'] * 100:.1f}%（unsure 按「不挡」算）")
    print(f"严格准确率      {sc['strict_acc'] * 100:.1f}%（unsure 算错）")
    print(f"正例召回        {sc['recall'] * 100:.1f}%"
          f"｜精确率 {sc['precision'] * 100:.1f}%｜F1 {sc['f1'] * 100:.1f}%")
    print(f"混淆矩阵        TP={sc['tp']} FP={sc['fp']} "
          f"TN={sc['tn']} FN={sc['fn']}")
    wrong = [r for r in rows
             if str(r.get("pred")) != "error"
             and ("no" if r.get("pred") == "no" else "yes") != str(r.get("label"))]
    if wrong:
        print(f"\n判错 {len(wrong)} 条（看前 {min(show, len(wrong))} 条）：")
        for r in wrong[:show]:
            print(f"  [{r['label']}→{r['pred']}] {str(r.get('text'))[:70]}")
            print(f"        窗口尾：{str((r.get('window') or [''])[-1])[:70]}")
    print(f"\n明细            {out}")
    ok = sc["acc"] >= 0.85 and sc["recall"] >= 0.85
    if mock:
        print("闸门建议        ⚠️ mock 模式只验证脚本，**不能**当开闸门依据")
    else:
        print("闸门建议        " + (
            "✅ 达到 85%/85% —— 可以请用户拍板开 ATMO_GATE=1"
            if ok else "❌ 未达 85%/85% —— ATMO_GATE 保持 0（只加不减）"))
    print("注意            弱标签（她没接≠不该接）；分数只做版本间比较。")
    print("==============================================")
    return 0 if sc["n"] else 1


# ============================================================
# 4) main
# ============================================================
async def cmd_run(args) -> int:
    samples: list[dict] = []
    for rec in iter_traces(args.trace, args.days):
        for s in build_samples(rec, neg_per_trace=args.neg_per_trace):
            samples.append(s)
        if args.limit and len(samples) >= args.limit:
            break
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        print("❌ 没有可用样本：检查 trace 路径 / 时间范围 / 是否只有私聊")
        return 1
    if args.show_prompt:
        from services import atmosphere
        print("---- prompt 样例（和线上一模一样）----")
        print(atmosphere.build_prompt(samples[0]["window"], samples[0]["text"]))
        print("------------------------------------")
    print(f"▶ 评测 {len(samples)} 条（正例 "
          f"{sum(1 for s in samples if s['label'] == 'yes')}）"
          f"｜{'mock' if args.mock else '真模型'}｜并发 {args.concurrency}")
    sem = asyncio.Semaphore(max(1, args.concurrency))
    rows = await asyncio.gather(*[
        _predict(s, sem, args.timeout, args.mock) for s in samples])

    out = Path(args.out) if args.out else EVAL_DIR / "intent.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return _report(list(rows), out, show=args.show, mock=args.mock)


def main() -> int:
    ap = argparse.ArgumentParser(description="意图层（need_reply）离线评测")
    ap.add_argument("--trace", action="append", default=[],
                    help="trace 文件（可多次传）；不传就按 --days 扫目录")
    ap.add_argument("--days", type=int, default=2)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--neg-per-trace", type=int, default=2,
                    help="每条 trace 抽几个「她没接」的负例（默认 2）")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--out", default="")
    ap.add_argument("--mock", action="store_true",
                    help="不调模型，用确定性规则假装模型（验证脚本本身）")
    ap.add_argument("--show", type=int, default=8, help="最多打印几条判错样本")
    ap.add_argument("--show-prompt", action="store_true",
                    help="先打印第一条的完整 prompt（确认和线上同一条）")
    args = ap.parse_args()
    return asyncio.run(cmd_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
