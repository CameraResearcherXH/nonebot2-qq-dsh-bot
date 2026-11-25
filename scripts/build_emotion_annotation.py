#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_emotion_annotation.py —— 生成 200 条「情绪判读」人工标注候选（0 模型调用）。

为什么需要
----------
100 条检索集只测「取没取到」，测不了「impact=-1 到底是不是 -1」。
情绪模块要闭环，必须有 200 条人工标注作为外部效标（见 docs/emotion-annotation-spec.md）。

做什么
------
从最近 20 天群聊里分层抽 200 条「她可能看到并受影响」的消息：
  praise 25 / care 25 / criticism 35 / ignore 20 / tease 25 /
  world 20 / apology 15 / neutral 20 / hard 15

每条带：
  · 上下文（前 3 / 后 3 条）
  · 收件人猜测（名字/称呼，低置信）
  · auto_suggest（规则粗判，**标注时不要看**，只用于事后对比）
  · 空的 label（impact/arousal/dominance/target/relation_relevance/unexpected/confidence/reason）

输出
----
  data/eval/emotion_annotation_200.jsonl
  data/eval/emotion_annotation_200.md     人工标注表（逐条块状，方便打勾）
  data/eval/emotion_annotation_stats.json

用法
----
  sudo python3 scripts/build_emotion_annotation.py
  sudo python3 scripts/build_emotion_annotation.py --seed 7 --limit 200
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import random
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DIALOGUE = DATA / "dialogue"
PROFILES = DATA / "profiles"
CALLNAME = DATA / "callname.json"
OUT_DIR = DATA / "eval"

_ZW = re.compile(r"[\u200b-\u200f\u2060-\u206f\u1160\u3164\ufe00-\ufe0f\ufeff]")
CJK = re.compile(r"[\u4e00-\u9fa5]")

POS = ("夸", "厉害", "聪明", "可爱", "喜欢你", "好棒", "谢谢", "抱抱", "贴贴",
       "爱你", "辛苦", "别累", "我帮你", "真乖", "好听", "好看", "牛", "强")
NEG = ("傻", "蠢", "滚", "闭嘴", "烦", "废物", "弱智", "恶心", "去死", "有病",
       "垃圾", "菜鸡", "狗东西", "猪", "妈的", "草泥马", "脑残", "智障")
HARD_NEG = ("废物", "滚", "去死", "弱智", "脑残", "智障", "垃圾", "狗东西")
CARE = ("谢谢", "抱抱", "贴贴", "别累", "我帮你", "辛苦", "早点睡", "多喝", "注意身体")
APOLOGY = ("对不起", "抱歉", "我的错", "别生气", "原谅", "我错了")
SARCASM = ("呵呵", "真棒呢", "你可真行", "真厉害呢", "好棒棒", "厉害了", "可真行")
COLD = {"哦", "嗯", "行", "好", "随便", "。。。", "...", "？", "?", "哦。", "嗯。",
        "行吧", "好吧", "6", "666", "笑死"}


def clean(s: str) -> str:
    return _ZW.sub("", str(s or "")).strip()


def load_names() -> dict:
    """qq -> 显示名；以及 name -> qq（长名优先）。"""
    qq2name, name2qq = {}, {}
    for p in glob.glob(str(PROFILES / "*_profile.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        qq = str(d.get("qq") or "")
        if not qq:
            continue
        nm = clean(d.get("call_name") or d.get("nickname") or "")[:12]
        qq2name[qq] = nm
        for x in [nm, clean(d.get("nickname"))[:12]] + [clean(a)[:12] for a in (d.get("aliases") or [])]:
            if len(x) >= 2:
                name2qq.setdefault(x, qq)
    return {"qq2name": qq2name, "name2qq": name2qq}


def load_bot_aliases() -> set:
    out = set()
    try:
        d = json.load(open(CALLNAME, encoding="utf-8"))
        for g in (d.get("groups") or {}).values():
            out |= set((g.get("bot") or {}).keys())
        out |= set((d.get("global") or {}).get("bot", {}).keys())
    except Exception:
        pass
    out |= {"小鲸鱼", "肥鱼", "鱼宝", "魔鱼", "白饭", "小肥鱼"}
    return {x for x in out if x}


def load_dialogue() -> dict:
    """gid -> 按时间排序的记录。"""
    by_gid = defaultdict(list)
    for p in sorted(glob.glob(str(DIALOGUE / "*.jsonl"))):
        day = Path(p).stem
        for ln in open(p, encoding="utf-8", errors="ignore"):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            t = clean(r.get("text"))
            if not t:
                continue
            by_gid[str(r.get("gid") or "")].append({
                "ts": float(r.get("ts") or 0),
                "gid": str(r.get("gid") or ""),
                "qq": str(r.get("qq") or ""),
                "who": clean(r.get("who") or "")[:16],
                "kind": str(r.get("kind") or ""),
                "text": t,
                "day": day,
            })
    for g in by_gid.values():
        g.sort(key=lambda x: x["ts"])
    return by_gid


def is_noise(t: str) -> bool:
    # 冷落类（「哦」「嗯」「？」）本身很短，不能按长度当噪声
    if t.strip() in COLD:
        return False
    if len(t) < 3:
        return True
    if t.startswith(("[", "【", "/", "／", "!", "！")):
        return True
    if re.fullmatch(r"[\W_]+", t):
        return True
    return False


def classify(t: str, is_bot_preceded: bool = False) -> str:
    if any(k in t for k in APOLOGY):
        return "apology"
    # 反讽：明面夸 + 语气词，或同一句里又夸又骂
    if any(k in t for k in SARCASM):
        return "hard"
    has_pos = any(k in t for k in POS)
    has_neg = any(k in t for k in NEG)
    if has_pos and has_neg:
        return "hard"
    if has_pos and t.rstrip().endswith(("呢", "呵", "哈")):
        return "hard"
    # 调侃要早于批评：「哈哈 你怎么这么菜」「你是不是傻？」（非重度辱骂）
    mild = any(k in t for k in ("菜", "笨", "傻", "丑", "肥", "猪", "拉胯", "弱"))
    if mild and not any(k in t for k in HARD_NEG) and (
            "哈" in t or "笑" in t or "乐" in t or t.rstrip().endswith(("？", "?"))):
        return "tease"
    if has_neg:
        return "criticism"
    if any(k in t for k in CARE):
        return "care"
    if has_pos:
        return "praise"
    if is_bot_preceded and t.strip() in COLD:
        return "ignore"
    return "neutral"


def auto_suggest(cat: str, text: str) -> dict:
    strong = any(k in text for k in HARD_NEG)
    table = {
        "praise": (+2 if any(k in text for k in ("喜欢你", "爱你", "最", "一直")) else +1, "mid"),
        "care": (+1, "low"),
        "criticism": (-2 if strong else -1, "high" if strong else "mid"),
        "ignore": (-1, "low"),
        "tease": (-1, "mid"),
        "world": (-1, "mid"),
        "apology": (+1, "mid"),
        "neutral": (0, "low"),
        "hard": (-1, "mid"),
    }
    impact, arousal = table.get(cat, (0, "low"))
    return {
        "impact": impact,
        "arousal": arousal,
        "dominance": "low" if cat in ("criticism", "ignore") else "mid",
        "target": "world" if cat == "world" else "recipient",
        "relation_relevance": 0.0 if cat == "world" else 1.0,
        "unexpected": 0.8 if cat == "hard" else (0.5 if cat not in ("neutral",) else 0.0),
    }


def guess_recipient(text: str, speaker: str, names: dict, bot_alias: set) -> dict:
    for nm in sorted(names["name2qq"], key=lambda x: -len(x)):
        if len(nm) >= 2 and nm in text and nm != speaker:
            return {"qq": names["name2qq"][nm], "name": nm, "conf": 0.7,
                    "signals": ["name"]}
    if any(b in text for b in bot_alias):
        return {"qq": "", "name": "她", "conf": 0.8, "signals": ["bot_name"]}
    if "你" in text and "你们" not in text:
        return {"qq": "", "name": "上一句的人", "conf": 0.6, "signals": ["second_person"]}
    return {"qq": "", "name": "", "conf": 0.0, "signals": []}


def build_candidates(by_gid: dict, names: dict, bot_alias: set) -> dict:
    """category -> [candidate]"""
    pools = defaultdict(list)
    seen = set()
    for gid, rows in by_gid.items():
        for i, r in enumerate(rows):
            if r["kind"] != "user":
                continue
            t = r["text"]
            if is_noise(t) or len(t) > 120:
                continue
            h = hashlib.md5(t.encode("utf-8")).hexdigest()
            if h in seen:
                continue
            prev = rows[i - 1] if i > 0 else None
            bot_preceded = bool(prev and prev["kind"] == "bot")
            cat = classify(t, bot_preceded)
            # world：两个人对骂，且都没提她
            if (any(k in t for k in NEG) and prev and prev["kind"] == "user"
                    and prev["qq"] != r["qq"] and any(k in prev["text"] for k in NEG)
                    and not any(b in t or b in prev["text"] for b in bot_alias)):
                cat = "world"
            seen.add(h)
            ctx_b = [x["text"][:80] for x in rows[max(0, i - 3):i]]
            ctx_a = [x["text"][:80] for x in rows[i + 1:i + 4]]
            pools[cat].append({
                "category": cat,
                "gid": gid,
                "ts": r["ts"],
                "day": r["day"],
                "speaker": {"qq": r["qq"], "name": r["who"] or names["qq2name"].get(r["qq"], "")},
                "recipient_guess": guess_recipient(t, r["who"], names, bot_alias),
                "text": t,
                "context_before": ctx_b,
                "context_after": ctx_a,
                "auto_suggest": auto_suggest(cat, t),
            })

    # ignore 单独扫：她说完 → 对方只回「哦/嗯/行/随便/…」
    for gid, rows in by_gid.items():
        for i, r in enumerate(rows):
            if r["kind"] != "bot":
                continue
            for j in range(i + 1, min(len(rows), i + 4)):
                u = rows[j]
                if u["kind"] != "user":
                    continue
                if u["ts"] - r["ts"] > 180:
                    break
                t = u["text"].strip()
                if t not in COLD:
                    break
                h = hashlib.md5(("ignore:" + t + str(u["ts"])).encode()).hexdigest()
                if h in seen:
                    break
                seen.add(h)
                pools["ignore"].append({
                    "category": "ignore", "gid": gid, "ts": u["ts"], "day": u["day"],
                    "speaker": {"qq": u["qq"],
                                "name": u["who"] or names["qq2name"].get(u["qq"], "")},
                    "recipient_guess": {"qq": "", "name": "她", "conf": 0.9,
                                        "signals": ["cold_reply"]},
                    "text": t,
                    "context_before": [x["text"][:80] for x in rows[max(0, i - 2):j]],
                    "context_after": [x["text"][:80] for x in rows[j + 1:j + 3]],
                    "auto_suggest": auto_suggest("ignore", t),
                })
                break
    return pools


QUOTA = {
    "praise": 25, "care": 25, "criticism": 35, "ignore": 20, "tease": 25,
    "world": 20, "apology": 15, "neutral": 20, "hard": 15,
}


def pick(pools: dict, rnd: random.Random, limit: int) -> list:
    for v in pools.values():
        rnd.shuffle(v)
    out, gid_cnt, spk_cnt, days = [], Counter(), Counter(), set()
    total = sum(QUOTA.values())

    def ok(c):
        if gid_cnt[c["gid"]] >= max(6, int(limit * 0.65)):
            return False
        if spk_cnt[c["speaker"]["qq"]] >= 18:
            return False
        return True

    # 第一轮按配额
    for cat, q in QUOTA.items():
        n = 0
        for c in pools.get(cat, []):
            if n >= q or len(out) >= limit:
                break
            if not ok(c):
                continue
            out.append(c)
            gid_cnt[c["gid"]] += 1
            spk_cnt[c["speaker"]["qq"]] += 1
            days.add(c["day"])
            n += 1
    # 不够就按「信息量」补：tease/hard/world 优先，neutral 最后
    if len(out) < limit:
        order = ("tease", "hard", "world", "criticism", "praise", "care",
                 "apology", "ignore", "neutral")
        rest = [c for cat in order for c in pools.get(cat, [])]
        rnd.shuffle(rest)
        for c in rest:
            if len(out) >= limit:
                break
            if not ok(c) or c in out:
                continue
            out.append(c)
            gid_cnt[c["gid"]] += 1
            spk_cnt[c["speaker"]["qq"]] += 1
            days.add(c["day"])
        # 还差 → 放宽配额，避免整批被 neutral 填满
        if len(out) < limit:
            for c in rest:
                if len(out) >= limit:
                    break
                if c in out:
                    continue
                out.append(c)
                gid_cnt[c["gid"]] += 1
                spk_cnt[c["speaker"]["qq"]] += 1
                days.add(c["day"])
    rnd.shuffle(out)
    return out[:limit]


def write_md(items: list, path: Path) -> None:
    lines = [
        "# 情绪判读标注表（200 条）",
        "",
        "> 规则见 `docs/emotion-annotation-spec.md`。**不要看 auto_suggest**，先自己判，再对比。",
        "> 五档：-2 羞辱/背叛 · -1 冷落/轻调侃 · 0 普通 · +1 被夸/被接住 · +2 被认真对待",
        "",
    ]
    for it in items:
        ts = time.strftime("%m-%d %H:%M", time.localtime(it["ts"]))
        lines += [
            f"## {it['id']}｜{it['category']}｜{it['gid']}｜{ts}",
            f"- 说话人：{it['speaker']['name']}（{it['speaker']['qq']}）",
            f"- 收件人猜测：{it['recipient_guess']['name'] or '不确定'}"
            f"（{it['recipient_guess']['conf']}，{','.join(it['recipient_guess']['signals']) or '无'}）",
        ]
        for x in it["context_before"]:
            lines.append(f"  - 上文：{x}")
        lines.append(f"- **这句**：{it['text']}")
        for x in it["context_after"]:
            lines.append(f"  - 下文：{x}")
        lines += [
            "",
            "| impact | arousal | dominance | target | relation_relevance | unexpected | confidence |",
            "|---|---|---|---|---|---|---|",
            "| ☐-2 ☐-1 ☐0 ☐+1 ☐+2 | ☐low ☐mid ☐high | ☐low ☐mid ☐high "
            "| ☐speaker ☐recipient ☐world | ☐0 ☐0.5 ☐1 | ☐0 ☐0.5 ☐1 | ____ |",
            "",
            f"理由（≤30 字）：____________________　反讽：☐是 ☐否",
            "",
            "---",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument("--out", default=str(OUT_DIR / "emotion_annotation_200.jsonl"))
    ap.add_argument("--md", default=str(OUT_DIR / "emotion_annotation_200.md"))
    ap.add_argument("--stats", default=str(OUT_DIR / "emotion_annotation_stats.json"))
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    names = load_names()
    bot_alias = load_bot_aliases()
    by_gid = load_dialogue()
    pools = build_candidates(by_gid, names, bot_alias)
    print("[pools]", {k: len(v) for k, v in sorted(pools.items())})

    items = pick(pools, rnd, args.limit)
    for i, it in enumerate(items, 1):
        it["id"] = f"emo_{i:03d}"
        it["msg_ids"] = []
        it["label"] = {
            "impact": None, "arousal": None, "dominance": None,
            "target": None, "relation_relevance": None, "unexpected": None,
            "confidence": None, "reason": "", "is_sarcasm": None,
        }
        it["meta"] = {"labeler": "", "round": 1, "status": "draft", "note": ""}
        it["split"] = "overlap" if i <= 50 else "single"   # 前 50 条做双人重叠

    items.sort(key=lambda x: x["ts"])
    for i, it in enumerate(items, 1):
        it["id"] = f"emo_{i:03d}"

    with open(args.out, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    write_md(items, Path(args.md))

    cat = Counter(it["category"] for it in items)
    gids = Counter(it["gid"] for it in items)
    days = {it["day"] for it in items}
    stats = {
        "total": len(items),
        "by_category": dict(cat),
        "by_gid_top": gids.most_common(5),
        "days_covered": len(days),
        "overlap": sum(1 for it in items if it["split"] == "overlap"),
        "seed": args.seed,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    Path(args.stats).write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"[out] {args.out}\n[out] {args.md}\n[out] {args.stats}")


if __name__ == "__main__":
    main()
