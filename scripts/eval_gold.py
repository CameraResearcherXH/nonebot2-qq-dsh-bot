#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
eval_gold.py —— 检索金标准评测（默认 0 模型调用、0 费用）。

为什么要单独一个「检索评测」
----------------------------
回复质量差，有两种原因：
    ① 该给的东西没取到（检索问题）
    ② 取到了但没用好（生成问题）
混在一起打分，永远不知道改哪里。这个脚本只量 ①。

三个阶段（默认只跑第 1 个）
--------------------------
    plan       只算成本：多少题、多少次要联网、预计耗时/token
    retrieval  只跑检索，算 recall@k / MRR / 干扰项率（本地 0 费用）
    generate   把 top-k 证据**按预算截断**后导出成 replay 集，
               交给已有的 scripts/eval_replay.py 去跑生成（那一步才花钱）

关键设计：预算感知（对应「一次性喂太多 prompt 会成本危机」）
------------------------------------------------------------
· 每题的上下文上限 `--ctx-tokens`（默认 900），超了就截断；
· 最多注入 `--topk`（默认 5）条，每条最多 120 token；
· 本地检索 0 费用；图书馆只在 `--network` 时调用，且默认并发 2；
· `plan` 会先告诉你「这轮要花多少」，确认后再跑。

用法
----
    # 0 费用：先看计划
    python scripts/eval_gold.py --stage plan

    # 本地检索评测（角色脑：档案/记忆/心情/风格）
    python scripts/eval_gold.py --stage retrieval --limit 60

    # 带上图书馆（每题约 9s，10 道 world 题 ≈ 90s）
    python scripts/eval_gold.py --stage retrieval --network --limit 100

    # 导出生成阶段回放集（每题 ≤900 token 上下文）
    python scripts/eval_gold.py --stage generate --emit-replay data/eval/gold_replay.jsonl
    python scripts/eval_replay.py run --set data/eval/gold_replay.jsonl --limit 10

输出
----
    data/eval/gold_eval_report.json
    data/eval/gold_eval_report.md
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LIB_API = os.getenv("LIB_API", "http://127.0.0.1:8001")
CJK_RUN = re.compile(r"[\u4e00-\u9fa5]+")
LATIN = re.compile(r"[a-zA-Z0-9_]+")
_ZW = re.compile(r"[\u200b-\u200f\u2060-\u206f\u1160\u3164\ufe00-\ufe0f\ufeff]")


# ============================================================
# 工具
# ============================================================
def clean(s: str) -> str:
    return _ZW.sub("", str(s or "")).strip()


def tokens(s: str) -> list:
    s = clean(s).lower()
    out = LATIN.findall(s)
    for run in CJK_RUN.findall(s):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def est_tokens(text: str) -> int:
    """中文约 1 token / 1.5 字，英文约 1 token / 4 字符。"""
    t = str(text or "")
    cjk = len(CJK_RUN.findall(t))
    return int(cjk / 1.5 + (len(t) - cjk) / 4) + 1


class BM25:
    def __init__(self, docs: list, k1: float = 1.5, b: float = 0.75):
        self.docs = docs
        self.k1, self.b = k1, b
        self.tf = []
        self.len = []
        df = Counter()
        for d in docs:
            tk = tokens(d["text"])
            c = Counter(tk)
            self.tf.append(c)
            self.len.append(len(tk))
            df.update(c.keys())
        self.avg = (sum(self.len) / len(self.len)) if self.len else 1.0
        n = max(1, len(docs))
        self.idf = {t: math.log(1 + (n - v + 0.5) / (v + 0.5)) for t, v in df.items()}

    def search(self, q: str, topk: int = 10, restrict=None) -> list:
        qtk = tokens(q)
        if not qtk:
            return []
        scores = defaultdict(float)
        for t in qtk:
            idf = self.idf.get(t)
            if not idf:
                continue
            for i, c in enumerate(self.tf):
                if restrict is not None and not restrict(self.docs[i]):
                    continue
                f = c.get(t)
                if not f:
                    continue
                dl = self.len[i] or 1
                scores[i] += idf * (f * (self.k1 + 1)) / (
                    f + self.k1 * (1 - self.b + self.b * dl / self.avg))
        best = sorted(scores.items(), key=lambda kv: -kv[1])[:topk]
        return [dict(self.docs[i], score=s) for i, s in best]


def http_json(url: str, payload=None, timeout: float = 60.0):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


# ============================================================
# 索引
# ============================================================
class Brain:
    def __init__(self):
        self.profiles = {}
        self.blocks = []
        self.moods = []
        self.pairs = []
        self.name2qq = {}
        self._load()

    def _load(self):
        for p in glob.glob(str(DATA / "profiles/*_profile.json")):
            try:
                d = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            qq = str(d.get("qq") or "")
            if not qq:
                continue
            names = [clean(d.get("call_name")), clean(d.get("nickname"))]
            names += [clean(x) for x in (d.get("aliases") or [])]
            names = [x for x in names if len(x) >= 2]
            self.profiles[qq] = {
                "qq": qq,
                "names": names,
                "text": " ".join(filter(None, [
                    str(d.get("identity") or ""), str(d.get("personality") or ""),
                    " ".join(names)])),
                "ref": f"profiles/{qq}_profile.json",
            }
            for n in names:
                self.name2qq.setdefault(n, qq)

        for p in glob.glob(str(DATA / "memory/blocks/*.json")):
            try:
                d = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            txt = clean(d.get("text_full") or d.get("summary") or "")
            if not txt:
                continue
            bid = d.get("id") or Path(p).stem
            self.blocks.append({
                "text": txt,
                "ref": f"memory/blocks/{bid}.json",
                "about_qq": str(d.get("about_qq") or ""),
                "ts": float(d.get("created_at") or 0),
                "kind": str(d.get("kind") or ""),
            })

        mh = DATA / "mood_history.jsonl"
        if mh.exists():
            for ln in open(mh, encoding="utf-8", errors="ignore"):
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                reason = clean(r.get("reason"))
                if not reason:
                    continue
                ts = float(r.get("at") or 0)
                self.moods.append({
                    "text": reason,
                    "ref": f"mood_history.jsonl#{int(ts)}",
                    "about_qq": str(r.get("about_qq") or ""),
                    "ts": ts,
                    "delta": float(r.get("delta") or 0),
                })

        # 风格样本：用户一句 → 她回一句（180s 内）
        bots = defaultdict(list)
        for p in sorted(glob.glob(str(DATA / "dialogue/*.jsonl"))):
            day = Path(p).stem
            prev = {}
            for ln in open(p, encoding="utf-8", errors="ignore"):
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                t = clean(r.get("text"))
                if not t or t.startswith("["):
                    continue
                gid = str(r.get("gid") or "")
                ts = float(r.get("ts") or 0)
                if r.get("kind") == "user":
                    prev[gid] = {"text": t, "ts": ts, "qq": str(r.get("qq") or "")}
                elif r.get("kind") == "bot":
                    u = prev.get(gid)
                    if u and 0 <= ts - u["ts"] <= 180:
                        bots[day].append({
                            "text": u["text"] + " → " + t,
                            "ref": f"dialogue/{day}.jsonl#{int(ts)}",
                            "about_qq": u["qq"],
                            "ts": ts,
                            "bot_text": t,
                        })
        self.pairs = [x for v in bots.values() for x in v]

        self.bm_blocks = BM25(self.blocks)
        self.bm_moods = BM25(self.moods)
        self.bm_pairs = BM25(self.pairs)
        self.bm_profiles = BM25(list(self.profiles.values()))

    def find_qq(self, q: str) -> str:
        best = ""
        for n in self.name2qq:
            if len(n) >= 2 and n in q and len(n) > len(best):
                best = n
        return self.name2qq.get(best, "")

    # ---------- 各脑检索 ----------
    def profiles_hits(self, q: str, topk: int) -> list:
        out = []
        for h in self.bm_profiles.search(q, topk):
            out.append({"ref": h["ref"], "snippet": h["text"][:120],
                        "score": h["score"], "brain": "character"})
        return out

    def block_hits(self, q: str, topk: int) -> list:
        qq = self.find_qq(q)
        out = []
        for h in self.bm_blocks.search(q, topk, restrict=(lambda d: d["about_qq"] == qq) if qq else None):
            out.append({"ref": h["ref"], "snippet": h["text"][:160],
                        "score": h["score"] + (0.5 if qq and h["about_qq"] == qq else 0),
                        "brain": "character", "ts": h["ts"]})
        return out

    def mood_hits(self, q: str, topk: int) -> list:
        """心情检索：**先结构化过滤，再轻排序**。

        实测（2026-11-25）：只用 BM25 全文搜「为什么对他心情变好」，
        recall@5 只有 0.33 —— 问题里几乎没有和 reason 重合的词。
        正确做法是 (about_qq, 方向) 过滤 + 时间/强度排序，这正是
        `mood_events` 表要解决的问题。
        """
        qq = self.find_qq(q)
        up = any(k in q for k in ("变好", "开心", "高兴", "好起来"))
        down = any(k in q for k in ("变差", "生气", "难受", "烦", "失望"))
        cand = [m for m in self.moods if (not qq or m["about_qq"] == qq)]
        if up and not down:
            cand = [m for m in cand if m["delta"] > 0]
        elif down and not up:
            cand = [m for m in cand if m["delta"] < 0]
        qset = set(tokens(q))
        scored = []
        for m in cand:
            overlap = len(qset & set(tokens(m["text"])))
            # 「上次」→ 越近越优先；再叠强度
            recency = m["ts"] / 1e10
            scored.append((overlap * 0.5 + abs(m["delta"]) * 0.3 + recency, m))
        scored.sort(key=lambda x: -x[0])
        return [{"ref": m["ref"], "snippet": m["text"][:120], "score": s,
                 "brain": "character", "ts": m["ts"]} for s, m in scored[:topk]]

    def style_hits(self, q: str, topk: int) -> list:
        out = []
        for h in self.bm_pairs.search(q, topk):
            out.append({"ref": h["ref"], "snippet": h["bot_text"][:120],
                        "score": h["score"], "brain": "character", "ts": h["ts"]})
        return out


def library_hits(q: str, topk: int) -> list:
    """只检索不生成：rag_search 返回「来源: documents/xxx」。"""
    url = f"{LIB_API}/api/agent/execute-tool?tool_name=rag_search"
    try:
        d = http_json(url, {"query": q, "top_k": topk, "generate_answer": False}, timeout=90)
    except Exception as e:
        return [{"ref": "", "snippet": f"[ERR] {type(e).__name__}", "score": 0, "brain": "library"}]
    out = str(d.get("output") or "")
    hits = []
    for m in re.finditer(r"来源[:：]\s*/?(?:app/)?(documents/[^\s\)\]]+)", out):
        ref = m.group(1).strip("，。,.；;")
        hits.append({"ref": ref, "snippet": "", "score": 1.0 - len(hits) * 0.05,
                     "brain": "library"})
    return hits[:topk]


# ============================================================
# 打分
# ============================================================
def norm_ref(r: str) -> str:
    r = clean(r).lstrip("/")
    if r.startswith("app/"):
        r = r[4:]
    return r


def accept_refs(item: dict, brain: "Brain") -> set:
    """「同人同方向的任意心情事件都算对」——没有时间锚的问题只能这么判。"""
    acc = item.get("accept") or {}
    if not acc:
        return set()
    if acc.get("source") != "mood_history":
        return set()
    qq = str(acc.get("about_qq") or "")
    sign = acc.get("sign") or "+"
    return {m["ref"] for m in brain.moods
            if m["about_qq"] == qq and (m["delta"] > 0) == (sign == "+")}


def is_gold(hit: dict, item: dict, accept: set | None = None) -> bool:
    h = norm_ref(hit.get("ref"))
    if not h:
        return False
    if accept and h in accept:
        return True
    for g in item.get("gold") or []:
        g_ref = norm_ref(g.get("ref"))
        if not g_ref:
            continue
        if h == g_ref:
            return True
        # 图书馆返回 documents/x.md，gold 也是 documents/x.md：前缀一致即可
        if h.startswith("documents/") and g_ref.startswith("documents/"):
            if h.rsplit("/", 1)[-1] == g_ref.rsplit("/", 1)[-1]:
                return True
    return False


def is_distractor(hit: dict, item: dict) -> bool:
    h = norm_ref(hit.get("ref"))
    return any(h == norm_ref(d.get("ref")) for d in (item.get("distractors") or []))


def predict_route(q: str, brain: Brain) -> str:
    has_lib = ("《" in q) or ("讲的是什么" in q) or ("里面主要" in q)
    has_person = bool(brain.find_qq(q))
    if has_lib and has_person:
        return "both"
    if has_lib:
        return "library"
    return "character"


def retrieve(item: dict, brain: Brain, topk: int, network: bool) -> tuple:
    """返回 (hits, route, skipped_reason)"""
    q = item["question"]
    route = predict_route(q, brain)
    cat = item["category"]
    hits = []
    skipped = ""
    if route in ("character", "both"):
        if cat == "person_fact":
            hits += brain.profiles_hits(q, topk)
        elif cat == "mood":
            hits += brain.mood_hits(q, topk)
        elif cat == "style":
            hits += brain.style_hits(q, topk)
        elif cat == "mixed":
            # ⚠️ 两路分数不可比（不同语料的 BM25），不能直接排序合并，
            #    必须按来源配额：3 条事件 + 2 条档案（实测混合题因此从 0.867 起）
            hits += brain.block_hits(q, max(1, topk - 2))[:max(1, topk - 2)]
            hits += brain.profiles_hits(q, 2)[:2]
        else:
            hits += brain.block_hits(q, topk)
    if route in ("library", "both"):
        if not network:
            skipped = "library-skipped(--network off)"
        else:
            quota = 2 if route == "both" else topk
            hits += library_hits(q, quota)[:quota]
    # 去重（保持配额顺序，不再跨来源按分数重排）
    seen, out = set(), []
    for h in hits:
        k = norm_ref(h.get("ref"))
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        out.append(h)
    return out[:topk], route, skipped


def summarize(rows: list) -> dict:
    def agg(sub):
        n = len(sub)
        if not n:
            return {"n": 0}
        r1 = sum(1 for x in sub if x["rank"] and x["rank"] <= 1) / n
        r5 = sum(1 for x in sub if x["rank"] and x["rank"] <= 5) / n
        r10 = sum(1 for x in sub if x["rank"] and x["rank"] <= 10) / n
        mrr = sum((1.0 / x["rank"]) if x["rank"] else 0 for x in sub) / n
        dis = sum(1 for x in sub if x.get("distractor@5")) / n
        # gold_position：标准证据在「注入顺序」里的相对位置（0 前 / 1 后）。
        # 生产是「最相关放最后」，所以 rank=1 → 1.0；没命中 → 0。
        gpos = [float(x.get("gold_position") or 0.0) for x in sub]
        return {"n": n, "recall@1": round(r1, 3), "recall@5": round(r5, 3),
                "recall@10": round(r10, 3), "mrr": round(mrr, 3),
                "distractor@5": round(dis, 3),
                "gold_position": round(sum(gpos) / n, 3),
                "gold_position@hit": round(
                    sum(g for g in gpos if g > 0) / max(1, sum(1 for g in gpos if g > 0)), 3),
                "avg_ms": round(sum(x["ms"] for x in sub) / n, 1),
                "avg_ctx_tokens": round(sum(x["ctx_tokens"] for x in sub) / n, 1)}

    out = {"overall": agg(rows)}
    by_cat = defaultdict(list)
    for x in rows:
        by_cat[x["category"]].append(x)
    out["by_category"] = {k: agg(v) for k, v in sorted(by_cat.items())}
    out["route_acc"] = round(
        sum(1 for x in rows if x["route_pred"] == x["route_expected"]) / max(1, len(rows)), 3)
    out["skipped"] = sum(1 for x in rows if x.get("skipped"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", default=str(DATA / "eval/gold_questions_draft.jsonl"))
    ap.add_argument("--stage",
                    choices=["plan", "retrieval", "generate", "position"],
                    default="plan")
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--ctx-tokens", type=int, default=900)
    ap.add_argument("--network", action="store_true", help="图书馆走 8001（每题约 9s）")
    ap.add_argument("--emit-replay", default="")
    ap.add_argument("--out-json", default=str(DATA / "eval/gold_eval_report.json"))
    ap.add_argument("--out-md", default=str(DATA / "eval/gold_eval_report.md"))
    args = ap.parse_args()

    items = [json.loads(l) for l in open(args.set, encoding="utf-8") if l.strip()]
    items = items[:args.limit]

    # ---------- plan ----------
    if args.stage == "plan":
        by_route = Counter()
        for it in items:
            by_route[it.get("route") or "character"] += 1
        lib_n = by_route.get("library", 0) + by_route.get("both", 0)
        print("== 评测计划 ==")
        print(f"题数：{len(items)}｜路由分布：{dict(by_route)}")
        print(f"本地检索：{len(items)} 题 × ~50ms ≈ {len(items)*0.05:.1f}s（0 费用）")
        print(f"图书馆检索：{lib_n} 题 × ~9s ≈ {lib_n*9/2:.0f}s（并发 2，0 生成费用）")
        print(f"上下文上限：{args.topk} 条 × ≤120 token，总 ≤{args.ctx_tokens} token/题")
        print(f"生成阶段（可选）：{len(items)} 题 × ~{args.ctx_tokens} token 输入"
              f" ≈ {len(items)*args.ctx_tokens/1000:.0f}k token")
        print("→ 先跑 --stage retrieval 看 recall；确认后再 --emit-replay 去生成。")
        return

    brain = Brain()
    rows = []
    for it in items:
        t0 = time.time()
        hits, route, skipped = retrieve(it, brain, args.topk, args.network)
        ms = (time.time() - t0) * 1000
        acc = accept_refs(it, brain)
        rank = 0
        for i, h in enumerate(hits, 1):
            if is_gold(h, it, acc):
                rank = i
                break
        ctx = 0
        for h in hits:
            ctx += est_tokens(h.get("snippet") or "")
            if ctx > args.ctx_tokens:
                break
        # gold 在 prompt 里的相对位置：0=最前，1=最后（生产把最相关放最后）
        if rank and len(hits) > 1:
            gpos = round(1.0 - (rank - 1) / (len(hits) - 1), 3)
        else:
            gpos = 1.0 if rank else 0.0
        rows.append({
            "id": it["id"], "category": it["category"], "question": it["question"],
            "route_expected": it.get("route"), "route_pred": route,
            "rank": rank, "gold_position": gpos,
            "ms": round(ms, 1), "ctx_tokens": min(ctx, args.ctx_tokens),
            "skipped": skipped,
            "distractor@5": bool(any(is_distractor(h, it) for h in hits[:5])),
            "gold": [g.get("ref") for g in it.get("gold") or []],
            # 位置评测需要把上下文撑到接近线上预算（5×~240 字 ≈ 1k token），
            # 90 字太短，头/中/尾几乎测不出 Lost in the Middle。
            "hits": [{"ref": h.get("ref"), "score": round(float(h.get("score") or 0), 3),
                      "snippet": (h.get("snippet") or "")[:240]} for h in hits],
        })
        if args.stage == "retrieval" and len(rows) % 20 == 0:
            print(f"  ... {len(rows)}/{len(items)}")

    report = summarize(rows)
    report["set"] = args.set
    report["topk"] = args.topk
    report["network"] = args.network
    report["built_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    Path(args.out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out_json).write_text(json.dumps(
        {"summary": report, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    md = ["# 检索金标准评测报告", "",
          f"- 集合：`{args.set}`｜题数 {len(rows)}｜topk {args.topk}｜"
          f"图书馆 {'开' if args.network else '关'}",
          f"- 生成时间：{report['built_at']}", "",
          "## 总体", "", "| 指标 | 值 |", "|---|---|"]
    o = report["overall"]
    for k in ("recall@1", "recall@5", "recall@10", "mrr", "distractor@5",
              "gold_position", "gold_position@hit", "avg_ms", "avg_ctx_tokens"):
        md.append(f"| {k} | {o.get(k)} |")
    md += [f"| 路由准确率 | {report['route_acc']} |", ""]
    md += ["## 分类", "", "| 类 | n | R@1 | R@5 | MRR | 干扰@5 | 平均ms | 平均token |",
           "|---|---|---|---|---|---|---|---|"]
    for k, v in report["by_category"].items():
        md.append(f"| {k} | {v['n']} | {v.get('recall@1')} | {v.get('recall@5')} | "
                  f"{v.get('mrr')} | {v.get('distractor@5')} | {v.get('avg_ms')} | "
                  f"{v.get('avg_ctx_tokens')} |")
    worst = sorted([r for r in rows if not r["rank"]], key=lambda x: x["id"])[:15]
    md += ["", "## 未命中（先看这些）", "", "| id | 类 | 问题 | 期望证据 | 实际 top3 |", "|---|---|---|---|---|"]
    for r in worst:
        md.append(f"| {r['id']} | {r['category']} | {r['question'][:40]} | "
                  f"{','.join(x or '' for x in r['gold'])[:50]} | "
                  f"{','.join((h['ref'] or '')[:40] for h in r['hits'][:3])} |")
    Path(args.out_md).write_text("\n".join(md), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"[out] {args.out_json}\n[out] {args.out_md}")

    # ---------- position：导出「头/中/尾」三位置回放集 ----------
    # 只改证据顺序，题目/证据内容完全一样 → 差异可归因到位置（Lost in the Middle）。
    if args.stage == "position":
        out = []
        for r, it in zip(rows, items):
            acc = accept_refs(it, brain)
            gold = None
            rest = []
            for h in r["hits"]:
                if is_gold(h, it, acc) and gold is None:
                    gold = h
                elif h.get("ref"):
                    rest.append(h)
            gold_txt = (gold or {}).get("snippet") or str(it.get("answer_key") or "")
            if not gold_txt:
                gold_txt = "；".join(str(g.get("ref") or "") for g in it.get("gold") or [])
            gold_ref = (gold or {}).get("ref") or (
                (it.get("gold") or [{}])[0].get("ref") or "gold")
            # 4 条陪衬（不足就补同一条无关资料，保证三位置只差顺序）
            pool = []
            for h in rest[:4]:
                pool.append((h.get("ref") or "", h.get("snippet") or ""))
            while len(pool) < 4:
                pool.append((f"pad://{len(pool)}", "（无关资料：昨天群里在聊晚饭吃什么）"))
            for variant, pos in (("head", 0), ("mid", 2), ("tail", 4)):
                ev = list(pool)
                ev.insert(min(pos, len(ev)), (gold_ref, gold_txt))
                ctx = ("以下是检索到的证据（带出处；没有就直说没有）：\n"
                       + "\n".join(f"[{ref}] {txt}" for ref, txt in ev))
                out.append({
                    "id": f"{it['id']}-{variant}",
                    "key": it["category"],
                    "variant": variant,
                    "gold_ref": gold_ref,
                    "time": "",
                    "text": it["question"],
                    "baseline_reply": "",
                    "msgs": [
                        {"role": "system", "content": ctx},
                        {"role": "user", "content": it["question"]},
                    ],
                    "gold_answer": it.get("answer_key"),
                    "expected_keywords": it.get("expected_keywords") or [],
                })
        p = Path(args.emit_replay) if args.emit_replay else (
            DATA / "eval" / "position_replay.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in out) + "\n",
                     encoding="utf-8")
        print(f"[position] {p}（{len(out)} 条 = {len(rows)} 题 × 3 位置）")
        print("  下一步：python scripts/eval_replay.py run --set "
              f"{p} --deterministic --out data/eval/position_replay_out.jsonl")
        print("          python scripts/eval_replay.py judge --set "
              "data/eval/position_replay_out.jsonl --out data/eval/position_judged.jsonl")
        print("          python scripts/position_report.py --set "
              "data/eval/position_judged.jsonl")

    # ---------- generate：导出回放集（预算截断） ----------
    if args.stage == "generate" and args.emit_replay:
        out = []
        for r, it in zip(rows, items):
            ev = []
            budget = args.ctx_tokens
            # ⚠️ 反向注入：检索是「越靠前越相关」，但 prompt 要「最相关放最后」
            # （Lost in the Middle：中间最容易被忽略）
            for h in reversed(r["hits"]):
                s = h.get("snippet") or ""
                if not s:
                    continue
                if est_tokens(s) > budget:
                    s = s[:int(budget * 1.5)]
                if budget <= 0:
                    break
                ev.append(f"[{h['ref']}] {s}")
                budget -= est_tokens(s)
            ctx = ("以下是检索到的证据（带出处，按相关度从低到高排列，"
                   "**最相关的在最后**；没有就直说没有）：\n" + "\n".join(ev))
            out.append({
                "id": it["id"],
                "key": it["category"],
                "time": "",
                "text": it["question"],
                "baseline_reply": "",
                "msgs": [
                    {"role": "system", "content": ctx},
                    {"role": "user", "content": it["question"]},
                ],
                "gold_answer": it.get("answer_key"),
                "expected_keywords": it.get("expected_keywords") or [],
            })
        Path(args.emit_replay).write_text(
            "\n".join(json.dumps(x, ensure_ascii=False) for x in out) + "\n", encoding="utf-8")
        print(f"[replay] {args.emit_replay}（{len(out)} 条，每题 ≤{args.ctx_tokens} token）")


if __name__ == "__main__":
    main()
