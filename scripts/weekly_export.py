# scripts/weekly_export.py
"""
每周导出：把这一周的对话 / 识图 / 记忆 / 三方差分样本打成一个文件，
你手动丢给 DeepSeek 网页版做体检，它按固定格式回一份纠正表，
再用 scripts/weekly_apply.py 把纠正灌回记忆。

用法（在项目根目录）：
    python scripts/weekly_export.py                # 最近 7 天
    python scripts/weekly_export.py --days 3
    python scripts/weekly_export.py --max-lines 400

产物：
    data/weekly/weekly_2026-11-09.md     ← 丢给网页版的就是这个
    data/weekly/weekly_2026-11-09/images/ ← 图片按 #编号 命名，方便你对着上传
"""
import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "data" / "weekly"

# ============================================================
# 给网页版的审查指令（必须是固定格式，apply 脚本才解析得了）
# ============================================================
CHECK_INSTRUCTION = """\
# 给体检智能体的指令（请严格照做）

你是一个严格的审查员。下面是一个 QQ 群聊机器人（人设「小鲸鱼」）一周的
真实对话记录、识图记录、记忆和心情。请你检查四件事：

1. **人设扮演**：她的回复像不像这个人设？有没有出戏、突然变客服腔、
   自称错乱、对谁都一个语气？
2. **记忆与心情**：记忆里有没有明显记错的（张冠李戴、时间错乱、把 A 的事记到 B 头上）？
   心情曲线和回复语气对得上吗？
3. **名词与梗**：作品名、角色名、番剧名、网络梗、专有名词有没有写错的？
   这是重点，请逐条对照图片和上下文。
4. **回答质量**：有没有答非所问、硬编事实、该说不懂却硬答的？

## 输出格式（必须严格照抄这个结构，否则程序解析不了）

```
=== CORRECTION ===
C1 | 名词 | 错误写法 | 正确写法 | 依据（哪一条对话/图）
C2 | 人设 | 哪句出戏了 | 应该怎么说
C3 | 记忆 | 记错了什么 | 正确的事实
=== FACT ===
F1 | 一条应该被长期记住的事实（她原来不知道或记错的）
F2 | ...
=== PERSONA ===
P1 | 一句话指出最需要改的人设问题
=== END ===
```

规则：
- `CORRECTION` 每行 5 段，用 ` | ` 分隔；类型只能是 名词 / 人设 / 记忆 / 回答
- `FACT` 每行 2 段
- 没问题就写 `=== CORRECTION ===` 后直接空一行接 `=== FACT ===`
- 不要写任何别的东西，不要解释，不要客套
"""


def _fmt_time(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts or 0))


def _load_jsonl(p: Path) -> list:
    out = []
    if not p.exists():
        return out
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _safe(s, n: int = 300) -> str:
    s = str(s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n] + "…"


def export(days: int = 7, max_lines: int = 0) -> Path:
    from services import dialogue_log, memory_v2, mood, profiles

    now = time.time()
    since = now - days * 86400
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    md_path = OUT_DIR / f"weekly_{stamp}.md"
    img_out = OUT_DIR / f"weekly_{stamp}_images"
    if img_out.exists():
        shutil.rmtree(img_out, ignore_errors=True)
    img_out.mkdir(parents=True, exist_ok=True)

    L = []
    add = L.append

    add(f"# 小鲸鱼周报 · {stamp}（最近 {days} 天）\n")
    add(CHECK_INSTRUCTION)

    # ---------------- 一、对话流水（带图）----------------
    msgs = [m for m in dialogue_log.load_range(days) if m.get("ts", 0) >= since]
    if max_lines and len(msgs) > max_lines:
        msgs = msgs[-max_lines:]

    add(f"\n## 一、对话流水（{len(msgs)} 条）\n")
    add("格式：`[时间] 谁: 内容`，`→` 开头的是她的回复，`[图#n]` 是配图（图片在 images 目录，按编号命名）\n")

    img_no = 0
    for m in msgs:
        who = _safe(m.get("who"), 30)
        kind = m.get("kind", "user")
        text = _safe(m.get("text"), 400)
        ts = _fmt_time(m.get("ts"))

        if m.get("img"):
            img_no += 1
            src = ROOT / "data" / m["img"]
            dst_name = f"{img_no:03d}_{Path(m['img']).name}"
            if src.exists():
                try:
                    shutil.copy2(src, img_out / dst_name)
                except Exception:
                    pass
            add(f"- [{ts}] {who}: {text} **[图#{img_no}]** `{dst_name}`")
            if m.get("vision"):
                add(f"    - 识别：{_safe(m.get('vision'), 200)}")
            if m.get("anime") or m.get("char"):
                add(f"    - 检索：{m.get('anime') or '?'} / {m.get('char') or '?'}"
                    f"（来源 {m.get('source') or '?'}）")
            continue

        prefix = "→ " if kind == "bot" else ("  〔另一只鱼〕" if kind == "peer" else "")
        add(f"- [{ts}] {prefix}{who}: {text}")

    # ---------------- 二、识图记录汇总 ----------------
    imgs = [m for m in msgs if m.get("img")]
    add(f"\n## 二、识图记录（{len(imgs)} 张）\n")
    if not imgs:
        add("（本周没有图片）\n")
    else:
        add("| # | 识别结果 | 检索来源 | 作品 | 角色 |")
        add("|---|---|---|---|---|")
        for i, m in enumerate(imgs, 1):
            add(f"| {i} | {_safe(m.get('vision'), 90)} | {m.get('source') or '-'} "
                f"| {m.get('anime') or '-'} | {m.get('char') or '-'} |")
        add("")

    # ---------------- 三、记忆 ----------------
    try:
        idx = memory_v2._load_index()
        items = [d for d in idx.get("items", []) if d.get("created_at", 0) >= since]
    except Exception:
        items = []
    add(f"\n## 三、本周新增记忆（{len(items)} 条）\n")
    if not items:
        add("（无）\n")
    for d in items[:80]:
        add(f"- [{_fmt_time(d.get('created_at'))}] ({d.get('kind')}) {_safe(d.get('summary'), 150)}")
    if items:
        add("")

    # ---------------- 四、心情 ----------------
    try:
        m = mood.get_mood()
        add(f"\n## 四、当前心情\n\n- 数值：{m.get('value')}｜状态：{m.get('label') or m.get('mood') or ''}\n")
    except Exception:
        add("\n## 四、当前心情\n\n（读不到）\n")

    # ---------------- 五、三方差分样本 ----------------
    samples = [s for s in _load_jsonl(ROOT / "data" / "referee_samples.jsonl")
               if s.get("ts", 0) >= since]
    add(f"\n## 五、三方差分样本（{len(samples)} 条，我们自己 vs GLM vs 另一只鱼）\n")
    if not samples:
        add("（无）\n")
    for i, s in enumerate(samples[-40:], 1):
        add(f"\n### 样本 {i}（{_fmt_time(s.get('ts'))}，场景 {s.get('scene')}）")
        add(f"- 问题：{_safe(s.get('question'), 150)}")
        add(f"- 我们的回答：{_safe(s.get('ours'), 250)}")
        if s.get("ref1"):
            add(f"- GLM 参考：{_safe(s.get('ref1'), 250)}")
        if s.get("ref2"):
            add(f"- 另一只鱼：{_safe(s.get('ref2'), 250)}")
        if s.get("changed"):
            add(f"- 已判定的差异：纠正={_safe(s.get('fix'), 120)}｜补充={_safe(s.get('add'), 120)}"
                f"（可信度 {s.get('conf')}）")
    if samples:
        add("")

    # ---------------- 六、人物档案 ----------------
    try:
        pdir = ROOT / "data" / "profiles"
        plines = []
        if pdir.exists():
            for f in list(pdir.glob("*.json"))[:60]:
                # profiles 目录里除了 xxx_profile.json 还有 xxx_style.json，别当成人
                if f.stem.endswith("_style"):
                    continue
                qq = f.stem.replace("_profile", "")
                try:
                    p = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                nm = p.get("call_name") or p.get("nickname") or qq
                bits = [f"{nm}(QQ {qq})"]
                if p.get("bot_alias"):
                    bits.append(f"他叫她「{p['bot_alias']}」")
                if p.get("long_term_impression"):
                    bits.append(_safe(p["long_term_impression"], 100))
                plines.append("- " + "｜".join(bits))
        add(f"\n## 六、人物档案（{len(plines)} 人）\n")
        add("\n".join(plines) if plines else "（无）")
        add("")
    except Exception:
        pass

    add("\n---\n\n**请现在按最上面的输出格式给出体检结果。**\n")

    md_path.write_text("\n".join(L), encoding="utf-8")

    # 清过期对话/图片
    try:
        dialogue_log.purge()
    except Exception:
        pass

    print(f"✅ 已导出：{md_path}")
    print(f"   对话 {len(msgs)} 条｜图片 {img_no} 张（{img_out}）｜记忆 {len(items)} 条｜样本 {len(samples)} 条")
    if img_no:
        print(f"   ⚠️ 图片在这个目录，上传网页版时按 [图#n] 编号对应：{img_out}")
    return md_path


def main():
    ap = argparse.ArgumentParser(description="导出周报给网页版体检")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--max-lines", type=int, default=0, help="最多导出多少条对话（0=不限）")
    a = ap.parse_args()
    # 模块里用的是相对路径 data/...，在别的目录下跑会找不到，先切到项目根
    os.chdir(ROOT)
    export(days=a.days, max_lines=a.max_lines)


if __name__ == "__main__":
    main()
