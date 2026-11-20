#!/usr/bin/env python
"""
audit_prompt.py —— 审计「发给 DeepSeek 的 prompt」到底由什么组成、哪里重复。

起因（2026-11-11）：DeepSeek 自己反馈说
  「不是长的问题，关键是有些部分重复了，同一个规矩在人设、铁律、黑名单里各说一遍，
    我看的时候要来回对照，容易漏。真正该压缩的是把示例写得很长那些。」

这个脚本干三件事：
  1. 列出角色卡每个章节的字数（找出臃肿的章节）
  2. 列出 llm.py 里每个协议块的字数（找出能合并的块）
  3. 跨块找「重复的句子」——同一句话/同一个规矩在多处出现

用法：
    python scripts/audit_prompt.py            # 摘要
    python scripts/audit_prompt.py --dups     # 详细列出重复句
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERSONA_DIR = ROOT / "services" / "Personality"
LLM_PY = ROOT / "services" / "llm.py"

SEC_RE = re.compile(r"^([一二三四五六七八九十]+)、(.+)$")
RULE_RE = re.compile(r"^[─—\-=]{5,}$")


def cn_len(s: str) -> int:
    """按"字"算（中文按 1，英文/数字按 0.5 折算，别让 emoji 把数拉爆）。"""
    n = 0.0
    for ch in str(s or ""):
        n += 1.0 if "\u4e00" <= ch <= "\u9fff" else 0.5
    return int(n)


def persona_sections(path: Path) -> list:
    """把角色卡按「一、xxx」切段，返回 [(标题, 正文, 字数)]。"""
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    secs = []
    cur_title, cur_body = "（开头）", []
    for ln in lines:
        m = SEC_RE.match(ln.strip())
        if m:
            secs.append((cur_title, "\n".join(cur_body)))
            cur_title, cur_body = f"{m.group(1)}、{m.group(2)}", []
        else:
            cur_body.append(ln)
    secs.append((cur_title, "\n".join(cur_body)))
    return [(t, b, cn_len(b)) for t, b in secs]


def protocol_blocks() -> list:
    """
    扫 services/*.py 里所有「会进 prompt 的常量块」。

    抓法：`XXX_PROTOCOL = (` / `XXX_RULE = (` 这种大写常量，
    外加 llm.py 里直接 append 的 inline 块。不做完整 AST，够用就行。
    """
    blocks = []
    files = sorted((ROOT / "services").glob("*.py"))     # LLM_PY 就在里面，别再追加一次

    for f in files:
        src = f.read_text(encoding="utf-8", errors="ignore")

        # 1) 形如 XXX_PROTOCOL = ( "..." "..." )  —— 括号配对取出整块
        for m in re.finditer(r"^([A-Z][A-Z0-9_]{2,})\s*=\s*\(", src, re.M):
            name = m.group(1)
            start = m.end() - 1
            depth, i = 0, start
            while i < len(src):
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            body = src[start:i]
            if '"' not in body:
                continue
            text = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', body))
            if cn_len(text) < 20:
                continue
            blocks.append((f"{name}（{f.name}）", text, cn_len(text)))

        # 2) 三引号常量
        for m in re.finditer(r'^([A-Z][A-Z0-9_]{2,})\s*=\s*"""([\s\S]{40,8000}?)"""',
                             src, re.M):
            text = m.group(2)
            if cn_len(text) < 20:
                continue
            blocks.append((f"{m.group(1)}（{f.name}）", text, cn_len(text)))

        # 3) llm.py 里直接 append 的短块
        for m in re.finditer(
                r'msgs\.append\(\{"role":\s*"system",\s*"content":\s*\(([\s\S]{20,3000}?)\)\}\)',
                src):
            text = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))
            if cn_len(text) < 20:
                continue
            head = text.strip().splitlines()[0][:16]
            blocks.append((f"inline:{head}", text, cn_len(text)))
    return blocks


def split_sents(text: str) -> list:
    """切句：按句号/问号/换行切，去掉太短的。"""
    out = []
    for s in re.split(r"[。！？!?\n]+", str(text or "")):
        s = s.strip(" -·|:：\t")
        s = re.sub(r"^[一二三四五六七八九十\d]+[、.)）]\s*", "", s)
        # 纯分隔线/纯符号的行没有意义，跳过
        if not re.search(r"[\u4e00-\u9fff]", s):
            continue
        if cn_len(s) >= 8:
            out.append(s)
    return out


def find_dups(sources: list, min_len: int = 10) -> list:
    """跨来源找重复句子（完全包含也算）。返回 [(句, [来源名...])]。"""
    seen = []
    for name, text in sources:
        for s in split_sents(text):
            if cn_len(s) < min_len:
                continue
            key = re.sub(r"[\s，。！？、,.!?\"“”‘’（）()【】\[\]]", "", s)
            hit = None
            for i, (k, owners) in enumerate(seen):
                if key == k or (len(key) >= min_len and (key in k or k in key)):
                    hit = i
                    break
            if hit is None:
                seen.append((key, [(name, s)]))
            else:
                seen[hit][1].append((name, s))
    return [(owners[0][1], [o[0] for o in owners])
            for _, owners in seen if len({o[0] for o in owners}) > 1]


def main() -> int:
    ap = argparse.ArgumentParser(description="审计发给 DeepSeek 的 prompt 组成与重复")
    ap.add_argument("--dups", action="store_true", help="详细列出重复句")
    ap.add_argument("--persona", default="小鲸鱼proplus", help="人格卡文件名（不带 .txt）")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = PERSONA_DIR / f"{args.persona}.txt"
    if not p.exists():
        print(f"❌ 找不到人格卡：{p}")
        return 1

    print("=" * 66)
    print(f"① 角色卡《{p.stem}》章节字数（总 {cn_len(p.read_text(encoding='utf-8', errors='ignore'))} 字）")
    print("=" * 66)
    secs = persona_sections(p)
    total = sum(n for _, _, n in secs) or 1
    for title, _body, n in sorted(secs, key=lambda x: -x[2]):
        if n <= 0:
            continue
        bar = "█" * max(1, int(n / total * 40))
        print(f"{n:>5}  {n / total * 100:>4.0f}%  {bar}  {title}")

    print()
    print("=" * 66)
    print("② 协议块字数（llm.py 里单独 append 的 system 消息）")
    print("=" * 66)
    blocks = protocol_blocks()
    for name, _text, n in sorted(blocks, key=lambda x: -x[2]):
        print(f"{n:>5}  {name}")
    proto_total = sum(n for _, _, n in blocks)
    print(f"{proto_total:>5}  —— 协议块合计")

    print()
    print("=" * 66)
    print("③ 重复内容（同一句话/同一条规矩出现在多个地方）")
    print("=" * 66)
    sources = [(t, b) for t, b, _n in secs if _n > 0] + [(n, tx) for n, tx, _c in blocks]
    dups = find_dups(sources)
    if not dups:
        print("没有发现明显重复 ✓")
    else:
        print(f"发现 {len(dups)} 处跨块重复：")
        for s, owners in dups:
            uniq = sorted(set(owners))
            if args.dups:
                print(f"\n  · 「{s[:70]}」")
                print(f"    出现于：{' / '.join(uniq)}")
            else:
                print(f"  · {s[:46]}… ← {' / '.join(uniq)}")

    print()
    print("=" * 66)
    print("结论参考")
    print("=" * 66)
    persona_total = sum(n for _, _, n in secs)
    grand = persona_total + proto_total
    print(f"角色卡 {persona_total} 字（{persona_total / max(1, grand) * 100:.0f}%）"
          f"｜协议块 {proto_total} 字（{proto_total / max(1, grand) * 100:.0f}%）"
          f"｜合计 {grand} 字")
    if dups:
        print(f"重复 {len(dups)} 处 —— 先把这些合并掉，比删内容更值")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
