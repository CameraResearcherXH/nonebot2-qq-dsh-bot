#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
build_gold_questions.py —— 从真实数据里生成「检索金标准集」草稿（不改线上）。

为什么这么做
------------
改检索算法之前，必须先有一把尺子。手写 100 道题容易脱离真实语料，
所以我们从 data/ 里**抽真实证据**反向出题：

    证据（记忆块 / 档案 / 心情流水 / bot 原话 / 图书馆文档）
      → 抽一个「稀有但可检索」的关键词
      → 生成一句她会听到的自然问法
      → 标准答案 = 那段原文（可逐字核对）

七类题（共 100 条）
------------------
    person_fact  15  档案：他是谁 / 什么身份
    event        20  记忆块：某件具体的事
    time         15  记忆块 + 时间索引：那是什么时候的事
    mood         15  心情流水：为什么对他心情变好/变差
    world        10  图书馆：某篇文档讲什么
    style        10  bot 原话：上次那句话她怎么回的
    mixed        15  档案 + 记忆 / 记忆 + 图书馆，必须两路都召回

输出
----
    data/eval/gold_questions_draft.jsonl   机器可读（评测用）
    data/eval/gold_questions_draft.md      人工批改用的表格
    data/eval/gold_questions_stats.json    分布统计

用法（在服务器仓库根目录）
--------------------------
    sudo python3 scripts/build_gold_questions.py
    sudo python3 scripts/build_gold_questions.py --limit 20 --no-fetch
    sudo python3 scripts/build_gold_questions.py --seed 11

注意
----
· 只读 data/，不写业务数据；输出只在 data/eval/。
· 题目是**草稿**：answer_key 由程序截取原文，人工需要过一遍（尤其 style/mixed）。
· 不含任何模型调用 → 跑一次 0 成本；生成阶段才花钱（见 eval_gold.py）。
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIALOGUE = ROOT / "data/dialogue"
BLOCKS = ROOT / "data/memory/blocks"
PROFILES = ROOT / "data/profiles"
MOOD_HISTORY = ROOT / "data/mood_history.jsonl"
EVAL_DIR = ROOT / "data/eval"
LIB_API = os.getenv("LIB_API", "http://127.0.0.1:8001")

CJK = re.compile(r"[\u4e00-\u9fa5]")
CJK_RUN = re.compile(r"[\u4e00-\u9fa5]{2,}")

# 高频虚词/口水词：出现在关键词里就丢弃
STOP_CHARS = set(
    "我你他她它的了是就都也还很太在有和与或不过吗呢吧啊呀哦嗯这那什么怎么为可以没有"
    "不是觉得知道一个一些现在时候自己大家人家要会能去来说做给对到得着过把被让又再才"
    "上下来去多少几岁点分秒天年月日号次遍句话字个只条张件位名样种"
)
STOP_TERMS = {
    "哈哈哈", "呵呵呵", "什么鬼", "怎么回", "不知道", "有点", "感觉", "好像",
    "其实", "然后", "所以", "因为", "如果", "可以", "这个", "那个", "我们", "你们",
    "他们", "自己", "现在", "刚才", "刚刚", "今天", "明天", "昨天", "时候",
}


# 零宽 / 双向控制符 / 变体选择符：昵称里常见，直接清掉
_ZW = re.compile(r"[\u200b-\u200f\u2060-\u206f\u1160\u3164\ufe00-\ufe0f\ufeff]")


def _strip_name(s: str) -> str:
    s = _ZW.sub("", str(s or ""))
    s = re.sub(r"[（(].*?[)）]", "", s)
    s = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]", "", s)
    s = re.sub(r"[\U0001D400-\U0001D7FF]", "", s)   # 数学字母（𝙁𝘽𝙄 这类）
    s = re.sub(r"\s+", "", s)
    return s.strip()[:12]


def _one_line(s: str, n: int = 140) -> str:
    s = _ZW.sub("", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n]


def load_profiles() -> dict:
    """qq -> {name, identity, personality, aliases, call_name}"""
    out = {}
    for p in glob.glob(str(PROFILES / "*_profile.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        qq = str(d.get("qq") or "")
        if not qq:
            continue
        out[qq] = {
            "qq": qq,
            "name": _strip_name(d.get("call_name") or d.get("nickname") or ""),
            "nickname": _strip_name(d.get("nickname") or ""),
            "identity": (d.get("identity") or "").strip(),
            "personality": (d.get("personality") or "").strip(),
            "aliases": [str(x) for x in (d.get("aliases") or []) if str(x).strip()],
            "call_name": (d.get("call_name") or "").strip(),
            "is_owner": bool(d.get("is_owner")),
        }
    return out


def load_blocks() -> list:
    out = []
    for p in glob.glob(str(BLOCKS / "*.json")):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        txt = (d.get("text_full") or d.get("summary") or "").strip()
        if not txt:
            continue
        out.append({
            "id": d.get("id") or Path(p).stem,
            "text": txt,
            "kind": d.get("kind") or "",
            "about_qq": str(d.get("about_qq") or ""),
            "ts": float(d.get("created_at") or 0),
            "importance": float(d.get("importance") or 0),
            "emotion_hit": bool(d.get("emotion_hit")),
        })
    return out


def load_mood() -> list:
    out = []
    if not MOOD_HISTORY.exists():
        return out
    for ln in open(MOOD_HISTORY, encoding="utf-8", errors="ignore"):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        reason = str(r.get("reason") or "").strip()
        if not reason or reason in ("judge-delta",) or len(reason) < 6:
            continue
        out.append({
            "ts": float(r.get("at") or 0),
            "delta": float(r.get("delta") or 0),
            "emotion": str(r.get("emotion") or ""),
            "reason": reason,
            "about_qq": str(r.get("about_qq") or ""),
        })
    return out


def load_dialogue() -> tuple:
    """返回 (user_msgs, bot_msgs)，只留可读文本。"""
    users, bots = [], []
    for p in sorted(glob.glob(str(DIALOGUE / "*.jsonl"))):
        day = Path(p).stem
        for ln in open(p, encoding="utf-8", errors="ignore"):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            t = (r.get("text") or "").strip()
            if not t or t.startswith("["):
                continue
            rec = {
                "ts": float(r.get("ts") or 0),
                "gid": str(r.get("gid") or ""),
                "qq": str(r.get("qq") or ""),
                "who": _strip_name(r.get("who") or ""),
                "text": t,
                "day": day,
            }
            if r.get("kind") == "bot" and 6 <= len(t) <= 90:
                bots.append(rec)
            elif r.get("kind") == "user" and 8 <= len(t) <= 70:
                users.append(rec)
    return users, bots


def build_global_ngrams(blocks: list, sample: int = 2600) -> Counter:
    """只在样本上统计，避免 8414 块把内存吃满。"""
    cnt = Counter()
    step = max(1, len(blocks) // sample)
    for b in blocks[::step]:
        txt = b["text"]
        if len(txt) > 400:
            txt = txt[:400]
        for run in CJK_RUN.findall(txt):
            L = len(run)
            for n in (3, 4):
                if L < n:
                    continue
                for i in range(L - n + 1):
                    cnt[run[i:i + n]] += 1
    return cnt


# 出现在词首/词尾就说明多半是跨词切出来的碎片（「用先转换」「使刚性更」）
FUNC_EDGE = set("用使看带把被就才又再很更不没有的了是在和跟给让对到得着过从向把将为以于而且但所如果因虽然")


def pick_term(text: str, gcnt: Counter, ban: set) -> str:
    """挑一个「块里出现、全局很少见」的 3~4 字词，作为检索线索。

    只要求「稀有 + 边界干净」：这是检索探针，不要求它是语法上的完整词。
    """
    best, best_score = "", -1e9
    for run in CJK_RUN.findall(text):
        L = len(run)
        for n in (4, 3):
            if L < n:
                continue
            for i in range(L - n + 1):
                t = run[i:i + n]
                if t in ban or t in STOP_TERMS:
                    continue
                if any(c in STOP_CHARS for c in t):
                    continue
                if t[0] in FUNC_EDGE or t[-1] in FUNC_EDGE:
                    continue
                if len(set(t)) == 1:
                    continue
                g = gcnt.get(t, 0)
                if g > 3:
                    continue
                # 越长越好、越稀有越好；出现两次扣分（可能是口头禅）
                score = n * 2.0 - math.log(g + 1) - (0.6 if text.count(t) > 1 else 0)
                if score > best_score:
                    best, best_score = t, score
    return best


def _top_terms(text: str, gcnt: Counter, k: int = 3) -> list:
    """取前 k 个稀有词（互不重叠），用于自动 groundedness 检查。"""
    scored = []
    for run in CJK_RUN.findall(text):
        L = len(run)
        for n in (4, 3):
            if L < n:
                continue
            for i in range(L - n + 1):
                t = run[i:i + n]
                if any(c in STOP_CHARS for c in t) or len(set(t)) == 1:
                    continue
                if t[0] in FUNC_EDGE or t[-1] in FUNC_EDGE:
                    continue
                g = gcnt.get(t, 0)
                if g > 5:
                    continue
                scored.append((n * 2.0 - math.log(g + 1), i, t))
    scored.sort(reverse=True)
    out = []
    for _, _, t in scored:
        if any(t in x or x in t for x in out):
            continue
        out.append(t)
        if len(out) >= k:
            break
    return out


def add_meta(items: list, blocks: list, gcnt: Counter) -> None:
    """补 expected_keywords（自动判分）和 distractors（精度测试）。"""
    by_qq = {}
    for b in blocks:
        if b["about_qq"]:
            by_qq.setdefault(b["about_qq"], []).append(b)

    for it in items:
        kws = []
        for g in it.get("gold") or []:
            for t in _top_terms(str(g.get("snippet") or ""), gcnt, 3):
                if t not in kws:
                    kws.append(t)
        it["expected_keywords"] = kws[:4]

        # 干扰项：同一个人、另一件事 —— 只召回到「这个人的其他事」算半错
        dis = []
        for g in it.get("gold") or []:
            qq = str(g.get("about_qq") or "")
            if not qq or g.get("source") != "memory_block":
                continue
            others = [b for b in by_qq.get(qq, [])
                      if b["id"] not in str(g.get("ref")) and len(b["text"]) >= 30]
            if others:
                o = others[0]
                dis.append({
                    "ref": f"memory/blocks/{o['id']}.json",
                    "snippet": _one_line(o["text"], 100),
                    "why": "同一个人的另一件事，不该替代标准证据",
                })
            break
        if dis:
            it["distractors"] = dis


def fetch_doc(name: str, timeout: float = 20.0) -> str:
    url = f"{LIB_API}/api/files/{urllib.parse.quote(name)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        return str(d.get("content") or "")
    except Exception:
        return ""


def library_titles() -> list:
    try:
        with urllib.request.urlopen(f"{LIB_API}/api/files", timeout=90) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
    except Exception:
        return []
    out = []
    for f in d.get("files") or []:
        n = str(f.get("name") or "")
        if not n:
            continue
        out.append({"name": n, "size": int(f.get("size") or 0),
                    "ext": str(f.get("ext") or "")})
    return out


# ============================================================
# 七类题目
# ============================================================
GENERIC_IDENTITY = ("普通成员", "群成员", "群内成员", "群友", "成员", "用户")


def _identity_ok(s: str) -> bool:
    s = (s or "").strip()
    if len(s) < 6:
        return False
    if s in GENERIC_IDENTITY:
        return False
    # 「群内普通成员」这种没有信息量，考不出档案质量
    if any(s == g or (g in s and len(s) <= len(g) + 3) for g in GENERIC_IDENTITY):
        return False
    return True


def q_person_fact(profiles: dict, rnd: random.Random, n: int) -> list:
    pool = [p for p in profiles.values()
            if _identity_ok(p["identity"]) and p["name"]]
    rnd.shuffle(pool)
    out = []
    for p in pool:
        if len(out) >= n:
            break
        tpl = rnd.choice([
            "你还记得「{name}」是做什么的吗？",
            "「{name}」是什么身份来着？",
            "「{name}」平时是个什么样的人？",
        ])
        out.append({
            "category": "person_fact",
            "question": tpl.format(name=p["name"]),
            "route": "character",
            "difficulty": "easy",
            "gold": [{
                "source": "profile",
                "ref": f"profiles/{p['qq']}_profile.json",
                "snippet": p["identity"] + ("｜" + p["personality"] if p["personality"] else ""),
                "ts": None,
            }],
            "answer_key": p["identity"],
            "must_not": ["不要把他人的身份安到别人头上"],
            "why": "档案是结构化事实，应该 0 成本直接命中",
        })
    return out


def q_event(blocks: list, gcnt: Counter, profiles: dict,
            rnd: random.Random, n: int, used_terms: set) -> list:
    pool = [b for b in blocks
            if b["kind"] in ("event", "experience", "fact")
            and b["about_qq"] and b["ts"] and 30 <= len(b["text"]) <= 260
            and b["importance"] >= 0.4]
    rnd.shuffle(pool)
    out = []
    for b in pool:
        if len(out) >= n:
            break
        who = (profiles.get(b["about_qq"]) or {}).get("name") or "有个群友"
        term = pick_term(b["text"], gcnt, used_terms | {who})
        if not term:
            continue
        used_terms.add(term)
        tpl = rnd.choice([
            "你还记得{who}提过「{term}」吗？当时是什么情况？",
            "{who}之前说的那个「{term}」，后来怎么样了？",
            "关于「{term}」，{who}当时是怎么说的？",
        ])
        out.append({
            "category": "event",
            "question": tpl.format(who=who, term=term),
            "route": "character",
            "difficulty": rnd.choice(["medium", "medium", "hard"]),
            "gold": [{
                "source": "memory_block",
                "ref": f"memory/blocks/{b['id']}.json",
                "snippet": _one_line(b["text"], 200),
                "ts": b["ts"],
                "about_qq": b["about_qq"],
                "term": term,
            }],
            "answer_key": _one_line(b["text"], 90),
            "must_not": ["不要编一个没发生过的后续", "不要把别人的事算到他头上"],
            "why": f"稀有词「{term}」应能直接命中该记忆块",
        })
    return out


def q_time(blocks: list, gcnt: Counter, profiles: dict,
           rnd: random.Random, n: int, used_terms: set) -> list:
    pool = [b for b in blocks
            if b["about_qq"] and b["ts"] and 30 <= len(b["text"]) <= 260
            and b["kind"] in ("event", "experience")]
    rnd.shuffle(pool)
    out = []
    for b in pool:
        if len(out) >= n:
            break
        who = (profiles.get(b["about_qq"]) or {}).get("name") or "有个群友"
        term = pick_term(b["text"], gcnt, used_terms | {who})
        if not term:
            continue
        used_terms.add(term)
        lt = time.localtime(b["ts"])
        day = time.strftime("%m-%d", lt)
        part = ("凌晨" if lt.tm_hour < 6 else "上午" if lt.tm_hour < 12
                else "下午" if lt.tm_hour < 18 else "晚上")
        out.append({
            "category": "time",
            "question": f"{who}说的「{term}」那件事，大概是哪天的？",
            "route": "character",
            "difficulty": "hard",
            "gold": [{
                "source": "memory_block",
                "ref": f"memory/blocks/{b['id']}.json",
                "snippet": _one_line(b["text"], 200),
                "ts": b["ts"],
                "about_qq": b["about_qq"],
                "term": term,
            }],
            "answer_key": f"{day} {part}（±1 天算对）",
            "must_not": ["不要凭感觉编日期"],
            "why": "考时间索引：只有按 (about_qq, ts) 查才稳",
        })
    return out


FEEL_WORDS = (
    "开心", "高兴", "难受", "生气", "烦", "委屈", "暖", "失望", "喜欢", "心动",
    "难过", "想哭", "哭", "笑", "羞", "酸", "心疼", "安心", "失落", "绷不住",
    "翘", "甜", "疼", "慌", "踏实", "被接住", "在意", "感动", "不舍", "愧疚",
)


def _mood_reason_ok(r: str) -> bool:
    """只留「真的在说心情」的 reason。

    mood_history 里混了不少 `judge-delta` 和技术提问（「滚动轴承预紧的目的是什么」），
    那些不是心情原因，出成题会误导评测。
    """
    if not (8 <= len(r) <= 100):
        return False
    if r.endswith(("？", "?")):
        return False
    if not any(k in r for k in FEEL_WORDS):
        return False
    return True


def q_mood(moods: list, profiles: dict, rnd: random.Random, n: int) -> list:
    pool = [m for m in moods if m["about_qq"] and abs(m["delta"]) >= 0.05
            and _mood_reason_ok(m["reason"])]
    rnd.shuffle(pool)
    out, seen, per_dir = [], set(), Counter()
    for m in pool:
        if len(out) >= n:
            break
        key = (m["about_qq"], m["reason"][:20])
        if key in seen:
            continue
        # 同一个人同一方向最多 2 题，避免「老大变好」刷屏
        dkey = (m["about_qq"], m["delta"] > 0)
        if per_dir[dkey] >= 2:
            continue
        seen.add(key)
        per_dir[dkey] += 1
        who = (profiles.get(m["about_qq"]) or {}).get("name") or "有个群友"
        up = m["delta"] > 0
        tpl = rnd.choice([
            "你上次为什么对{who}心情{dir}？",
            "还记得吗，{who}哪句话让你心情{dir}了？",
            "{who}之前做过什么让你心情{dir}的事？",
        ])
        out.append({
            "category": "mood",
            "question": tpl.format(who=who, dir="变好" if up else "变差"),
            "route": "character",
            "difficulty": "medium",
            "gold": [{
                "source": "mood_history",
                "ref": f"mood_history.jsonl#{int(m['ts'])}",
                "snippet": m["reason"],
                "ts": m["ts"],
                "about_qq": m["about_qq"],
                "delta": m["delta"],
            }],
            "answer_key": _one_line(m["reason"], 90),
            "must_not": ["不要把心情原因说成别人的事", "不要迁移到无关的人"],
            # 没有时间锚的「上次为什么」，同一个人同方向的事件都算对
            "accept": {"source": "mood_history", "about_qq": m["about_qq"],
                       "sign": "+" if m["delta"] > 0 else "-"},
            "why": "考心情事件检索：现在只存在 jsonl，还没进 sqlite",
        })
    # 兜底：严格条件不够 15 条时，放宽到「第一人称叙述」再补
    if len(out) < n:
        for m in pool:
            if len(out) >= n:
                break
            key = (m["about_qq"], m["reason"][:20])
            if key in seen:
                continue
            seen.add(key)
            who = (profiles.get(m["about_qq"]) or {}).get("name") or "有个群友"
            up = m["delta"] > 0
            out.append({
                "category": "mood",
                "question": f"你上次为什么对{who}心情{'变好' if up else '变差'}？",
                "route": "character",
                "difficulty": "medium",
                "gold": [{"source": "mood_history",
                          "ref": f"mood_history.jsonl#{int(m['ts'])}",
                          "snippet": m["reason"], "ts": m["ts"],
                          "about_qq": m["about_qq"], "delta": m["delta"]}],
                "answer_key": _one_line(m["reason"], 90),
                "must_not": ["不要把心情原因说成别人的事"],
                "accept": {"source": "mood_history", "about_qq": m["about_qq"],
                           "sign": "+" if m["delta"] > 0 else "-"},
                "why": "考心情事件检索（兜底样本）",
            })
    return out


def q_world(lib: list, rnd: random.Random, n: int, do_fetch: bool) -> list:
    out = []
    # 1) 已知的弱智吧文档，作为「小文档不被大语料淹没」的探针
    txt = fetch_doc("xiaoming.txt") if do_fetch else ""
    if txt:
        out.append({
            "category": "world",
            "question": "小明是怎么解决温室效应的？",
            "route": "library",
            "difficulty": "hard",
            "gold": [{"source": "library", "ref": "documents/xiaoming.txt",
                      "snippet": _one_line(txt, 160), "ts": None}],
            "answer_key": "小明为了光合作用变成了植物人",
            "must_not": ["不要答成「种树/减排」这类常识答案"],
            "why": "31 字节小文档，考的是排序能不能把它顶上来",
        })
    # 2) 中文标题的 md，按主题分散
    cand = [x for x in lib
            if x["ext"] == ".md" and 6 <= len(x["name"]) <= 40
            and len(CJK.findall(x["name"])) >= 4
            and 800 <= x["size"] <= 40000]
    buckets = {
        "poetry": ("诗", "词", "曲", "赋"),
        "history": ("史", "朝代", "战争", "年"),
        "math": ("数学", "函数", "概率", "定理", "几何"),
        "phys": ("物理", "量子", "相对论", "力"),
        "anime": ("萌娘", "动漫", "角色", "番"),
        "wiki": ("维基", "百科", "什么", "为什么"),
    }
    picked = []
    for name, kws in buckets.items():
        hit = [x for x in cand if any(k in x["name"] for k in kws)]
        rnd.shuffle(hit)
        for x in hit[:1]:
            picked.append((name, x))
    rnd.shuffle(cand)
    for x in cand:
        if len(picked) >= max(1, n - 1):
            break
        if all(x["name"] != y["name"] for _, y in picked):
            picked.append(("misc", x))
    for topic, x in picked:
        if len(out) >= n:
            break
        content = fetch_doc(x["name"]) if do_fetch else ""
        if not content:
            continue
        title = x["name"].rsplit(".", 1)[0]
        out.append({
            "category": "world",
            "question": rnd.choice([
                f"《{title}》讲的是什么？",
                f"{title} 里面主要说了什么？",
                f"你还记得《{title}》的内容吗？",
            ]),
            "route": "library",
            "difficulty": rnd.choice(["medium", "hard"]),
            "gold": [{"source": "library", "ref": f"documents/{x['name']}",
                      "snippet": _one_line(content, 200), "ts": None,
                      "topic": topic}],
            "answer_key": _one_line(content, 100),
            "must_not": ["不要把百科内容说成自己的经历"],
            "why": f"集合={topic}；考分区 + BM25/向量能否命中该文档",
        })
    return out


SYS_NAME_HINTS = ("有新消息", "系统", "通知", "助手", "机器人", "bot", "Bot", "服务号")


def q_style(users: list, bots: list, profiles: dict,
            rnd: random.Random, n: int) -> list:
    """找「用户说 X → 她紧跟着回 Y」的对。

    ⚠️ 必须是**紧邻**的一对：中间又插了别人的话，问题就有歧义，
    评测会误判检索错（2026-11-25 实测 style recall 只有 0.4 就是这个原因）。
    """
    pairs = []
    for p in sorted(glob.glob(str(DIALOGUE / "*.jsonl"))):
        day = Path(p).stem
        prev = {}
        for ln in open(p, encoding="utf-8", errors="ignore"):
            try:
                r = json.loads(ln)
            except Exception:
                continue
            t = (r.get("text") or "").strip()
            gid = str(r.get("gid") or "")
            ts = float(r.get("ts") or 0)
            kind = r.get("kind")
            if kind == "user" and t and not t.startswith("["):
                prev[gid] = {"text": t, "ts": ts, "qq": str(r.get("qq") or ""),
                             "gid": gid,
                             "who": _strip_name(r.get("who") or ""), "day": day}
            elif kind == "bot" and t and not t.startswith("[") and len(t) >= 10:
                u = prev.pop(gid, None)      # 用完即清：下一条 bot 不再复用
                if u and 0 <= ts - u["ts"] <= 180:
                    pairs.append((u, {"text": t, "ts": ts, "day": day}))
    pairs = [(u, b) for u, b in pairs
             if u["qq"] and not any(k in (u["who"] or "") for k in SYS_NAME_HINTS)
             and not u["text"].startswith(("您有", "【"))]
    rnd.shuffle(pairs)
    out, seen = [], set()
    for u, b in pairs:
        if len(out) >= n:
            break
        key = (u["gid"], u["text"][:16])
        if key in seen:
            continue
        seen.add(key)
        who = (profiles.get(u["qq"]) or {}).get("name") or u["who"] or "有个群友"
        out.append({
            "category": "style",
            "question": f"上次{who}说「{_one_line(u['text'], 40)}」，你是怎么回的？",
            "route": "character",
            "difficulty": "medium",
            "gold": [{
                "source": "dialogue",
                "ref": f"dialogue/{u['day']}.jsonl#{int(b['ts'])}",
                "snippet": _one_line(b["text"], 160),
                "ts": b["ts"],
                "gid": u["gid"],
            }],
            "answer_key": _one_line(b["text"], 90),
            "must_not": ["不用逐字复述，但语气/立场要一致"],
            "why": "考风格样本检索 + 是否把她自己的原话当成风格锚点",
            "judge": "semantic+style（人工或 LLM 1~5 分）",
        })
    return out


def q_mixed(blocks: list, gcnt: Counter, profiles: dict,
            rnd: random.Random, n: int, used_terms: set) -> list:
    pool = [b for b in blocks
            if b["about_qq"] and b["ts"] and 30 <= len(b["text"]) <= 240
            and (profiles.get(b["about_qq"]) or {}).get("identity")]
    rnd.shuffle(pool)
    out = []
    for b in pool:
        if len(out) >= n:
            break
        p = profiles[b["about_qq"]]
        who = p["name"] or "有个群友"
        term = pick_term(b["text"], gcnt, used_terms | {who})
        if not term:
            continue
        used_terms.add(term)
        out.append({
            "category": "mixed",
            "question": f"{who}是做什么的？{who}之前提的「{term}」又是怎么回事？",
            "route": "both",
            "difficulty": "hard",
            "gold": [
                {"source": "profile", "ref": f"profiles/{p['qq']}_profile.json",
                 "snippet": p["identity"], "ts": None},
                {"source": "memory_block", "ref": f"memory/blocks/{b['id']}.json",
                 "snippet": _one_line(b["text"], 200), "ts": b["ts"],
                 "about_qq": b["about_qq"], "term": term},
            ],
            "answer_key": f"{p['identity']}；{_one_line(b['text'], 70)}",
            "must_not": ["两问必须都答，不能只答一半"],
            "why": "两路证据，考路由 + 融合（少一路就是 0.5 分）",
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(EVAL_DIR / "gold_questions_draft.jsonl"))
    ap.add_argument("--md", default=str(EVAL_DIR / "gold_questions_draft.md"))
    ap.add_argument("--stats", default=str(EVAL_DIR / "gold_questions_stats.json"))
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument("--no-fetch", action="store_true", help="不访问 8001，world 题会变少")
    args = ap.parse_args()

    rnd = random.Random(args.seed)
    EVAL_DIR.mkdir(parents=True, exist_ok=True)

    profiles = load_profiles()
    blocks = load_blocks()
    moods = load_mood()
    users, bots = load_dialogue()
    print(f"[load] profiles={len(profiles)} blocks={len(blocks)} "
          f"mood={len(moods)} users={len(users)} bots={len(bots)}")

    gcnt = build_global_ngrams(blocks)
    print(f"[ngram] keys={len(gcnt)}")

    used_terms: set = set()
    items = []
    items += q_person_fact(profiles, rnd, 15)
    items += q_event(blocks, gcnt, profiles, rnd, 20, used_terms)
    items += q_time(blocks, gcnt, profiles, rnd, 15, used_terms)
    items += q_mood(moods, profiles, rnd, 15)
    lib = [] if args.no_fetch else library_titles()
    print(f"[library] files={len(lib)}")
    items += q_world(lib, rnd, 10, not args.no_fetch)
    items += q_style(users, bots, profiles, rnd, 10)
    items += q_mixed(blocks, gcnt, profiles, rnd, 15, used_terms)

    add_meta(items, blocks, gcnt)

    # 编号 + 默认预算（成本上限）
    for i, it in enumerate(items, 1):
        it["id"] = f"gq_{i:03d}"
        it.setdefault("gid", "")
        it.setdefault("judge", "evidence+semantic")
        it["token_budget"] = {"context": 1200, "answer": 160}
        it["review"] = {"status": "draft", "note": ""}

    order = ["person_fact", "event", "time", "mood", "world", "style", "mixed"]
    items.sort(key=lambda x: (order.index(x["category"]), x["id"]))

    with open(args.out, "w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")

    by_cat = Counter(it["category"] for it in items)
    by_route = Counter(it["route"] for it in items)
    stats = {
        "total": len(items),
        "by_category": {k: by_cat.get(k, 0) for k in order},
        "by_route": dict(by_route),
        "gold_evidence_avg": round(sum(len(it["gold"]) for it in items) / max(1, len(items)), 2),
        "seed": args.seed,
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_window": "memory 11-04~11-24 / dialogue 11-09~11-24",
    }
    Path(args.stats).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

    # 人工批改表
    lines = [
        "# 检索金标准集草稿（人工批改用）",
        "",
        f"- 生成时间：{stats['built_at']}｜seed={args.seed}",
        f"- 语料窗口：{stats['source_window']}",
        f"- 合计：{stats['total']} 条｜" + "｜".join(f"{k} {v}" for k, v in stats["by_category"].items()),
        "",
        "批改方法：每题只改 `answer_key` / `must_not`；不要改 `gold`（那是逐字原文）。",
        "题太烂就标 `review.status=drop`，后面补题。",
        "",
    ]
    for cat in order:
        sub = [x for x in items if x["category"] == cat]
        if not sub:
            continue
        lines += [f"## {cat}（{len(sub)}）", "", "| id | 问题 | 标准答案 | 路由 |", "|---|---|---|---|"]
        for x in sub:
            q = x["question"].replace("|", "／")
            a = str(x.get("answer_key") or "").replace("|", "／")[:70]
            lines.append(f"| {x['id']} | {q} | {a} | {x['route']} |")
        lines.append("")
    Path(args.md).write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"[out] {args.out}\n[out] {args.md}\n[out] {args.stats}")


if __name__ == "__main__":
    main()
