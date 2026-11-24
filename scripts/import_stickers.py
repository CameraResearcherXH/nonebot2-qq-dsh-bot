#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
把表情包批量导入 data/stickers（支持分类文件夹 / 平铺 / neko_xxx 命名）。

三种分类方式（--tag-from）：
  name    免费、最快。按中文名关键词 + 系列号兜底，准确率大概 6~7 成
  llm     推荐。把文件名批量发给 DeepSeek 让它分类，318 张约 7 个请求，很便宜
  vision  逐张调用视觉模型。⚠️ 需要 Pillow 才能处理 gif，服务器没装会自动跳过 gif

目录结构支持：
  A) 按分类分文件夹（最准，文件夹名即标签）
       表情包/开心/xxx.png
  B) 平铺（你现在这种）
       data/memes/neko_0003091b98e7_EX49_True_music.gif
       data/memes/neko_4b1576f9a927_N07_哦吼.gif

用法：
    # 先看会怎么分（不写任何东西）
    python scripts/import_stickers.py --src data/memes --tag-from llm --report

    # 确认无误后真导入（复制到 data/stickers/<标签>/）
    python scripts/import_stickers.py --src data/memes --tag-from llm

    # 不想复制一份（直接让 index.json 指向原文件，省 318 份磁盘）
    python scripts/import_stickers.py --src data/memes --tag-from llm --in-place

    # 只补登记（文件已经在 data/stickers 下，但 index.json 漏了）
    python scripts/import_stickers.py --src data/stickers --reindex

说明：
  · 直接写 index.json，不走 sticker_lib.add()，因为 add() 有「每小时最多收 10 张」的限制
  · md5(文件内容) 去重
  · 标签必须在 TAG_LIBRARY 里，非法标签自动落到「搞怪」
  · 导入后 DeepSeek 就能用 <sticker:标签> 自己挑表情发
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# 分类文件夹名 → 内部标签
FOLDER_MAP = {
    "开心": "开心", "快乐": "开心", "高兴": "开心", "喜悦": "开心", "笑": "开心", "happy": "开心",
    "生气": "生气", "愤怒": "生气", "火大": "生气", "angry": "生气",
    "难过": "难过", "伤心": "难过", "sad": "难过",
    "委屈": "委屈", "哭": "哭",
    "撒娇": "撒娇", "可爱": "撒娇", "卖萌": "撒娇", "贴贴": "撒娇",
    "搞怪": "搞怪", "整活": "搞怪", "沙雕": "搞怪", "抽象": "搞怪", "funny": "搞怪",
    "思考": "思考", "疑惑": "思考", "问号": "思考", "think": "思考",
    "打招呼": "打招呼", "问候": "打招呼", "你好": "打招呼", "hi": "打招呼",
    "无语": "无语", "沉默": "无语", "嘲笑": "嘲笑", "嘲讽": "嘲笑",
    "吃瓜": "吃瓜", "惊讶": "惊讶", "震惊": "惊讶", "困": "困", "累了": "困",
    "害羞": "害羞", "点赞": "点赞", "同意": "点赞", "狗头": "狗头",
}

# 文件名关键词 → 标签（顺序即优先级：越具体越靠前）
NAME_HINTS = [
    # 打招呼 / 出场
    ("叫我吗", "打招呼"), ("交个朋友", "打招呼"), ("在么", "打招呼"), ("你好", "打招呼"),
    ("探头", "打招呼"), ("敬礼", "打招呼"), ("举牌", "打招呼"), ("举火把", "打招呼"),
    ("合影", "打招呼"), ("发布会", "打招呼"), ("英雄降临", "打招呼"), ("变身", "打招呼"),
    ("watching-you", "打招呼"), ("盖帽", "打招呼"), ("拍按钮", "打招呼"),
    # 思考
    ("思考", "思考"), ("问号", "思考"), ("疑惑", "思考"), ("怎么个事", "思考"),
    ("大脑过载", "思考"), ("懵逼", "思考"), ("这有问题", "思考"), ("真的假的", "思考"),
    ("疯狂暗示", "思考"), ("灵光一闪", "思考"), ("开窍", "思考"), ("鬼点子", "思考"),
    ("大调查", "思考"), ("悟", "思考"), ("啥玩意", "思考"),
    # 难过 / 哭 / 委屈
    ("委屈", "委屈"), ("哭", "哭"), ("难过", "难过"), ("呜呜", "难过"), ("嘤嘤", "难过"),
    ("碎了", "难过"), ("燃尽", "难过"), ("肝不动", "难过"), ("穷", "难过"),
    ("苦酒", "难过"), ("难吃", "难过"), ("要死", "难过"), ("原地去世", "难过"),
    ("安详", "难过"), ("倒霉", "难过"), ("成盒", "难过"), ("绷不住", "难过"),
    # 生气
    ("气死", "生气"), ("生气", "生气"), ("炸毛", "生气"), ("哈气", "生气"),
    ("对线", "生气"), ("黑化", "生气"), ("扎小人", "生气"), ("敲猫", "生气"),
    ("打你", "生气"), ("脑瓜崩", "生气"), ("打脸", "生气"), ("恐吓", "生气"),
    ("终极侮辱", "生气"), ("拱火", "生气"), ("非常坏", "生气"), ("嫌弃", "生气"),
    ("呕", "生气"), ("哼", "生气"),
    # 嘲笑 / 无语
    ("嘲笑", "嘲笑"), ("邪恶", "嘲笑"), ("难绷", "嘲笑"), ("绷住", "嘲笑"),
    ("无语", "无语"), ("沉默", "无语"), ("啧啧", "无语"), ("哦吼", "无语"),
    ("噫", "无语"), ("放空", "无语"), ("嘘", "无语"), ("禁言", "无语"),
    ("不给", "无语"), ("拒绝", "无语"), ("不要不要", "无语"), ("禁止", "无语"),
    ("呵", "无语"), ("邪门", "无语"),
    # 困
    ("哈欠", "困"), ("午休", "困"), ("困", "困"), ("摸鱼", "困"), ("趴", "困"),
    ("刷刷", "困"),
    # 惊讶
    ("震撼", "惊讶"), ("惊讶", "惊讶"), ("震惊", "惊讶"), ("嗷呜", "惊讶"),
    ("哇", "惊讶"), ("惊", "惊讶"),
    # 吃瓜
    ("吃瓜", "吃瓜"), ("看戏", "吃瓜"), ("围观", "吃瓜"), ("偷看", "吃瓜"),
    ("看猪", "吃瓜"), ("看猪猪", "吃瓜"),
    # 点赞
    ("点赞", "点赞"), ("同意", "点赞"), ("一键三连", "点赞"), ("巴适", "点赞"),
    ("友好手势", "点赞"), ("大拇指", "点赞"),
    # 狗头
    ("狗头", "狗头"), ("doge", "狗头"),
    # 撒娇 / 贴贴
    ("求求", "撒娇"), ("理理我", "撒娇"), ("蹭蹭", "撒娇"), ("贴贴", "撒娇"),
    ("扑倒", "撒娇"), ("宠溺", "撒娇"), ("软糯", "撒娇"), ("舔", "撒娇"),
    ("摸下巴", "撒娇"), ("捏脸", "撒娇"), ("揉脸", "撒娇"), ("害羞", "撒娇"),
    ("亲亲", "撒娇"), ("舌吻", "撒娇"), ("求婚", "撒娇"), ("情侣杯", "撒娇"),
    ("好人喵", "撒娇"), ("妈妈", "撒娇"), ("娃娃机", "撒娇"), ("抱", "撒娇"),
    ("比心", "撒娇"), ("心心", "撒娇"), ("画爱心", "撒娇"), ("梳", "撒娇"),
    # 开心
    ("超开心", "开心"), ("开心", "开心"), ("好耶", "开心"), ("哈哈", "开心"),
    ("笑", "开心"), ("喵乐", "开心"), ("撒钱", "开心"), ("恭喜发财", "开心"),
    ("红包", "开心"), ("干杯", "开心"), ("上菜", "开心"), ("开饭", "开心"),
    ("好吃", "开心"), ("蹦", "开心"), ("喵喵get", "开心"), ("真香", "开心"),
    ("喜", "开心"),
    # 搞怪（放最后，兜底性强）
    ("抽象", "搞怪"), ("整活", "搞怪"), ("鬼脸", "搞怪"), ("杂技", "搞怪"),
    ("刷牙", "搞怪"), ("music", "搞怪"), ("捂嘴", "嘲笑"), ("搓手", "撒娇"),
    ("咬他", "生气"),
    ("滚筒洗猫", "搞怪"), ("套娃", "搞怪"), ("爆猫", "搞怪"), ("猫虫", "搞怪"),
    ("烟花", "搞怪"), ("太鼓", "搞怪"), ("电音", "搞怪"), ("贝斯", "搞怪"),
    ("吉他", "搞怪"), ("小提琴", "搞怪"), ("萨克斯", "搞怪"), ("大提琴", "搞怪"),
    ("主唱", "搞怪"), ("迫击炮", "搞怪"), ("电磁炮", "搞怪"), ("龙击炮", "搞怪"),
    ("巡飞弹", "搞怪"), ("扫射", "搞怪"), ("上膛", "搞怪"), ("铁锅", "搞怪"),
    ("桶装", "搞怪"), ("好多猫", "搞怪"), ("猪", "搞怪"), ("大角牛", "搞怪"),
    ("monkey", "搞怪"), ("giveyouup", "搞怪"), ("letyoudown", "搞怪"),
    ("ciallo", "搞怪"), ("smoker", "搞怪"), ("chapter", "搞怪"), ("钓鱼", "搞怪"),
]

# 系列号兜底（只在名字认不出时用）
SERIES_FALLBACK = {
    "EX": "搞怪",    # extra：特效/乐器/武器/红包
    "ADN": "搞怪",   # 鼠鼠系列
    "TT": "撒娇",    # 贴贴
    "特殊": "搞怪",
}

NAME_RE = re.compile(r"^neko_([0-9a-fA-F]{8,})_(.+)$")


# ============================================================
# 文件名解析
# ============================================================
def parse_neko_name(fname: str) -> tuple:
    """
    neko_0003091b98e7_EX49_True_music.gif
      → (code='EX49', series='EX', name='True_music')
    neko_cee0df5038c4_特殊02.gif
      → (code='特殊02', series='特殊', name='特殊02')
    """
    stem = Path(fname).stem
    m = NAME_RE.match(stem)
    rest = m.group(2) if m else stem
    if "_" in rest:
        code, name = rest.split("_", 1)
    else:
        code, name = rest, rest
    series = re.sub(r"\d+$", "", code) or code
    return code, series, name


def tag_from_name(fname: str) -> tuple:
    """返回 (tag, 依据说明)。认不出返回 ('', '')。"""
    code, series, name = parse_neko_name(fname)
    low = name.lower()
    for kw, tag in NAME_HINTS:
        if kw.lower() in low:
            return tag, f"名字含「{kw}」"
    for kw, tag in NAME_HINTS:
        if kw.lower() in code.lower():
            return tag, f"系列号含「{kw}」"
    if series.upper() in SERIES_FALLBACK:
        return SERIES_FALLBACK[series.upper()], f"系列 {series} 兜底"
    return "", ""


# ============================================================
# DeepSeek 批量分类
# ============================================================
def _extract_json(text: str):
    if not text:
        return None
    t = text.strip().replace("```json", "```")
    if t.startswith("```"):
        lines = t.split("\n")
        t = "\n".join(lines[1:-1] if lines and lines[-1].startswith("```") else lines[1:])
    try:
        return json.loads(t)
    except Exception:
        pass
    try:
        s, e = t.index("{"), t.rindex("}") + 1
        return json.loads(t[s:e])
    except Exception:
        return None


def classify_by_llm(names: list, tags: list, batch: int = 40) -> dict:
    """把文件名批量发给 DeepSeek 分类，返回 {文件名: 标签}"""
    import httpx

    base = (os.getenv("XAI_BASE_URL", "") or "").rstrip("/")
    key = os.getenv("XAI_API_KEY", "")
    model = os.getenv("XAI_MODEL", "deepseek-chat")
    if not base or not key:
        print("  ⚠️  XAI_BASE_URL / XAI_API_KEY 没配，无法用 LLM 分类")
        return {}

    result = {}
    total = len(names)
    for i in range(0, total, batch):
        chunk = names[i:i + batch]
        listing = "\n".join(f"{j + 1}. {n}" for j, n in enumerate(chunk))
        prompt = (
            "下面是一批表情包的文件名（中文部分是这张表情想表达的意思）。\n"
            "请为每一个分配**一个**情绪标签，只能从下面这些里选，不要自创：\n"
            f"{' / '.join(tags)}\n\n"
            f"{listing}\n\n"
            '只输出 JSON，格式：{"文件名": "标签"}，文件名要和上面给的完全一致，不要解释。'
        )
        try:
            with httpx.Client(timeout=90, trust_env=False) as c:
                r = c.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "temperature": 0.2, "max_tokens": 2000},
                )
            if r.status_code != 200:
                print(f"  ⚠️  LLM 分类失败 HTTP {r.status_code}: {r.text[:120]}")
                continue
            data = _extract_json(r.json()["choices"][0]["message"].get("content") or "")
            if not isinstance(data, dict):
                print("  ⚠️  LLM 返回的不是 JSON，跳过这批")
                continue
            for k, v in data.items():
                v = str(v).strip()
                if v in tags:
                    result[str(k).strip()] = v
        except Exception as e:
            print(f"  ⚠️  LLM 分类异常: {type(e).__name__}: {e}")
        print(f"  …LLM 已分类 {min(i + batch, total)}/{total}")
    return result


# ============================================================
# 视觉分类（可选）
# ============================================================
def classify_by_vision(path: Path) -> str:
    """逐张调视觉模型。⚠️ gif 需要 Pillow 取首帧，没装就跳过。"""
    try:
        import asyncio
        from plugins.vision_agent import describe_image_bytes
    except Exception as e:
        return ""

    data = path.read_bytes()
    if path.suffix.lower() == ".gif":
        try:
            from PIL import Image  # 服务器通常没装
            import io
            img = Image.open(io.BytesIO(data))
            img.seek(0)
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            data = buf.getvalue()
        except Exception:
            return ""   # 没 Pillow → 跳过 gif

    try:
        desc = asyncio.get_event_loop().run_until_complete(describe_image_bytes(data))
    except Exception:
        return ""
    if not desc:
        return ""
    for kw, tag in NAME_HINTS:
        if kw in desc:
            return tag
    return ""


# ============================================================
# 扫描
# ============================================================
def iter_images(src: Path):
    """产出 (文件, 文件夹名或'')"""
    subs = [d for d in src.iterdir() if d.is_dir()]
    if subs:
        for d in sorted(subs):
            for f in sorted(d.rglob("*")):
                if f.is_file() and f.suffix.lower() in IMG_EXTS:
                    yield f, d.name
    else:
        for f in sorted(src.rglob("*")):
            if f.is_file() and f.suffix.lower() in IMG_EXTS:
                yield f, ""


def resolve_folder_tag(name: str) -> str:
    n = str(name).strip().lower()
    if not n:
        return ""
    for k, v in FOLDER_MAP.items():
        if k.lower() == n:
            return v
    for k, v in FOLDER_MAP.items():
        if k.lower() in n:
            return v
    return ""


def main():
    ap = argparse.ArgumentParser(description="批量导入表情包")
    ap.add_argument("--src", required=True, help="表情包目录（分类子文件夹 / 平铺都行）")
    ap.add_argument("--tag", default="", help="强制指定一个标签（全部归到这一类）")
    ap.add_argument("--tag-from", default="name", choices=["name", "llm", "vision"],
                    help="分类方式：name(免费) / llm(推荐) / vision")
    ap.add_argument("--report", action="store_true", help="只报告分布，不写入")
    ap.add_argument("--in-place", action="store_true",
                    help="不复制文件，index.json 直接指向源路径（省磁盘）")
    ap.add_argument("--reindex", action="store_true", help="只补登记 data/stickers 下漏掉的文件")
    ap.add_argument("--judge", action="store_true",
                    help="让 DeepSeek 给所有没打过分的表情打精品分（导入后跑一次）")
    ap.add_argument("--prune", action="store_true",
                    help="触发容量维护：超上限就让 DeepSeek 判该删哪些")
    ap.add_argument("--clean-broken", action="store_true",
                    help="清掉 index 里「文件已不存在」的坏条目（旧测试残留）")
    ap.add_argument("--keep-cwd", action="store_true", help="调试用：不切到项目根（隔离测试 data 目录）")
    args = ap.parse_args()

    if not args.keep_cwd:
        os.chdir(ROOT)

    from services.sticker_library import (
        TAG_LIBRARY, STICKER_MAX, STICKER_ROOT, sticker_lib,
    )

    src = Path(args.src).expanduser()
    if not src.is_absolute():
        src = (ROOT / src).resolve()
    if not src.exists():
        print(f"❌ 目录不存在：{src}")
        return 1

    dry = args.report
    print("=" * 68)
    print("  表情包导入")
    print("=" * 68)
    print(f"  源目录：{src}")
    print(f"  目标  ：{'（原地登记，不复制）' if args.in_place else STICKER_ROOT.resolve()}")
    print(f"  分类法：{args.tag_from}")
    print(f"  标签表：{' / '.join(TAG_LIBRARY)}")
    print(f"  现有  ：{len(sticker_lib.index)} 张")
    print(f"  模式  ：{'仅报告（不写入）' if dry else '真导入'}")
    print("-" * 68)

    # ---------- 清理坏数据 ----------
    if args.clean_broken:
        h = sticker_lib.health()
        print(f"  库存 {h['total']} 条：可用 {h['usable']}，坏文件 {h['broken_file']}，"
              f"商城表情 {h['mface']}")
        for md5 in h["broken_ids"][:10]:
            info = sticker_lib.index.get(md5) or {}
            print(f"    坏条目 {md5[:16]}… tag={info.get('tag')!r} path={info.get('path')!r}")
        n = sticker_lib.clean_broken()
        h2 = sticker_lib.health()
        print(f"  ✅ 清理 {n} 条 → 现在 {h2['total']} 条，可用 {h2['usable']} 条")
        if h2["usable"] == 0:
            print("  ⚠️ 现在一张都没有了，接着跑导入：")
            print("     python scripts/import_stickers.py --src data/memes --tag-from llm --in-place")
        return 0

    # ---------- 打精品分 ----------
    if args.judge:
        from services import sticker_curator
        items = [{"md5": m, **i} for m, i in sticker_lib.index.items()
                 if not i.get("quality")]
        if not items:
            print("  ✅ 所有表情都已经有精品分了，不用重复打分")
            return 0
        print(f"  🤖 有 {len(items)} 张没有精品分，交给 DeepSeek 打分…")
        n = asyncio.run(sticker_curator.rate_batch(items))
        print(f"  ✅ 打分完成 {n}/{len(items)} 张")
        hi = sum(1 for i in sticker_lib.index.values() if float(i.get("quality", 0) or 0) >= 0.85)
        print(f"  精品（≥0.85）：{hi} 张 / 共 {len(sticker_lib.index)} 张")
        return 0

    # ---------- 容量维护 ----------
    if args.prune:
        from services import sticker_curator
        print(f"  当前 {len(sticker_lib.index)} 张，上限 {STICKER_MAX}")
        res = asyncio.run(sticker_curator.maintain_capacity())
        print(f"  结果：{res}")
        return 0

    # ---------- reindex ----------
    if args.reindex:
        added = 0
        for tag_dir in (sorted(STICKER_ROOT.iterdir()) if STICKER_ROOT.exists() else []):
            if not tag_dir.is_dir():
                continue
            for f in tag_dir.iterdir():
                if not f.is_file() or f.suffix.lower() not in IMG_EXTS:
                    continue
                md5 = sticker_lib.md5_of(f.read_bytes())
                if md5 in sticker_lib.index:
                    continue
                sticker_lib.index[md5] = {
                    "tag": tag_dir.name, "path": str(f), "used_count": 0,
                    "created_at": f.stat().st_mtime,
                }
                added += 1
        if added and not dry:
            sticker_lib._save()
        print(f"  ✅ reindex 补登记 {added} 张（{'未写入' if dry else '已保存'}）")
        return 0

    # ---------- 扫描 ----------
    files = list(iter_images(src))
    if not files:
        print(f"  ❌ 目录里没有图片（支持 {sorted(IMG_EXTS)}）")
        return 1
    print(f"  扫描到 {len(files)} 个图片文件")

    # ---------- 定标签 ----------
    plan = {}          # path -> (tag, 依据)
    unknown = []       # 名字认不出的

    for f, folder in files:
        if args.tag:
            plan[f] = (args.tag, "手动指定")
            continue
        ftag = resolve_folder_tag(folder) if folder else ""
        if ftag:
            plan[f] = (ftag, f"文件夹「{folder}」")
            continue
        ntag, why = tag_from_name(f.name)
        if ntag:
            plan[f] = (ntag, why)
        else:
            unknown.append(f)

    # LLM 补分
    if unknown and args.tag_from == "llm":
        print(f"\n  🤖 {len(unknown)} 个名字认不出，交给 DeepSeek 分类…")
        mapping = classify_by_llm([f.name for f in unknown], TAG_LIBRARY)
        still = []
        for f in unknown:
            tag = mapping.get(f.name)
            if tag:
                plan[f] = (tag, "DeepSeek 判定")
            else:
                still.append(f)
        unknown = still
        print(f"  ✅ DeepSeek 判定了 {len(mapping)} 个，还剩 {len(unknown)} 个")

    # 视觉补分
    if unknown and args.tag_from == "vision":
        print(f"\n  👁️ {len(unknown)} 个交给视觉模型（gif 需要 Pillow，可能跳过）…")
        for i, f in enumerate(unknown, 1):
            tag = classify_by_vision(f)
            if tag:
                plan[f] = (tag, "视觉判定")
            if i % 20 == 0:
                print(f"    …{i}/{len(unknown)}")

    # 剩下的兜底
    for f in unknown:
        if f not in plan:
            plan[f] = ("搞怪", "认不出→兜底")

    # ---------- 报告 ----------
    stats = {}
    reasons = {}
    for f, (tag, why) in plan.items():
        if tag not in TAG_LIBRARY:
            tag = "搞怪"
            plan[f] = (tag, "标签非法→兜底")
        stats[tag] = stats.get(tag, 0) + 1
        reasons[why.split("「")[0].split(" ")[0]] = reasons.get(why.split("「")[0].split(" ")[0], 0) + 1

    print("\n  分类分布：")
    for tag, n in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"    {tag:<6} {n:>4} 张")

    low_conf = [(f, plan[f][1]) for f in plan if "兜底" in plan[f][1] or "认不出" in plan[f][1]]
    if low_conf:
        print(f"\n  ⚠️  {len(low_conf)} 张是兜底归类的（建议用 --tag-from llm 再跑一次）：")
        for f, why in low_conf[:10]:
            print(f"    {f.name}  →  {plan[f][0]}（{why}）")
        if len(low_conf) > 10:
            print(f"    …还有 {len(low_conf) - 10} 张")

    if dry:
        print("\n  （--report 模式，未写入任何文件）")
        print("=" * 68)
        return 0

    # ---------- 写入 ----------
    print("\n  开始写入…")
    batch = 0
    dup = 0
    fail = 0
    for f, (tag, why) in plan.items():
        try:
            data = f.read_bytes()
        except Exception as e:
            fail += 1
            print(f"    ⚠️ 读不到 {f.name}: {e}")
            continue
        if len(data) < 64:
            fail += 1
            continue

        md5 = sticker_lib.md5_of(data)
        if md5 in sticker_lib.index:
            dup += 1
            continue

        if args.in_place:
            dst = f
        else:
            tag_dir = STICKER_ROOT / tag
            tag_dir.mkdir(parents=True, exist_ok=True)
            dst = tag_dir / f"{md5}{f.suffix.lower()}"
            try:
                dst.write_bytes(data)
            except Exception as e:
                fail += 1
                print(f"    ⚠️ 写不到 {dst}: {e}")
                continue

        sticker_lib.index[md5] = {
            "tag": tag,
            "path": str(dst),
            "used_count": 0,
            "created_at": time.time(),
        }
        batch += 1
        if batch % 50 == 0:
            sticker_lib._save()
            print(f"    …已写入 {batch} 张")

    if batch:
        sticker_lib._save()

    print("-" * 68)
    print(f"  写入 {batch} 张，重复跳过 {dup} 张，失败 {fail} 张")
    print("  最终库存：")
    final = {}
    for info in sticker_lib.index.values():
        t = info.get("tag", "?")
        final[t] = final.get(t, 0) + 1
    for tag, n in sorted(final.items(), key=lambda x: -x[1]):
        print(f"    {tag:<6} {n:>4} 张")
    print("=" * 68)
    print("  导入完成。DeepSeek 现在能用 <sticker:标签> 自己挑表情发了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

