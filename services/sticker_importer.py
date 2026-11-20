# services/sticker_importer.py
"""
表情包导入核心逻辑。

一份逻辑，两个入口：
  · CLI：scripts/import_stickers.py
  · QQ 命令：/导入表情（plugins/sticker_admin）

关键事实：`data/memes/` 里的文件只是**素材**，不入库（不写 index.json）就等于没有。
"""
from core import tuning
import os
import re
import time
from pathlib import Path

from .sticker_library import (
    IMG_EXTS,
    STICKER_ROOT,
    TAG_LIBRARY,
    count_importable,
    logger,
    sticker_lib,
)

# 复制入库时把大图缩到多小（长边）。0/false = 不压。
STICKER_SHRINK = os.getenv("STICKER_SHRINK", "true").lower() in ("1", "true", "yes", "on")
STICKER_MAX_SIDE = tuning.i("STICKER_MAX_SIDE")

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

# 文件名关键词 → 标签（顺序即优先级）
NAME_HINTS = [
    ("叫我吗", "打招呼"), ("交个朋友", "打招呼"), ("在么", "打招呼"), ("你好", "打招呼"),
    ("探头", "打招呼"), ("敬礼", "打招呼"), ("举牌", "打招呼"), ("举火把", "打招呼"),
    ("合影", "打招呼"), ("发布会", "打招呼"), ("英雄降临", "打招呼"), ("变身", "打招呼"),
    ("watching-you", "打招呼"), ("盖帽", "打招呼"), ("拍按钮", "打招呼"),
    ("思考", "思考"), ("问号", "思考"), ("疑惑", "思考"), ("怎么个事", "思考"),
    ("大脑过载", "思考"), ("懵逼", "思考"), ("这有问题", "思考"), ("真的假的", "思考"),
    ("疯狂暗示", "思考"), ("灵光一闪", "思考"), ("开窍", "思考"), ("鬼点子", "思考"),
    ("大调查", "思考"), ("悟", "思考"), ("啥玩意", "思考"),
    ("委屈", "委屈"), ("哭", "哭"), ("难过", "难过"), ("呜呜", "难过"), ("嘤嘤", "难过"),
    ("碎了", "难过"), ("燃尽", "难过"), ("肝不动", "难过"), ("穷", "难过"),
    ("苦酒", "难过"), ("难吃", "难过"), ("要死", "难过"), ("原地去世", "难过"),
    ("安详", "难过"), ("倒霉", "难过"), ("成盒", "难过"), ("绷不住", "难过"),
    ("气死", "生气"), ("生气", "生气"), ("炸毛", "生气"), ("哈气", "生气"),
    ("对线", "生气"), ("黑化", "生气"), ("扎小人", "生气"), ("敲猫", "生气"),
    ("打你", "生气"), ("脑瓜崩", "生气"), ("打脸", "生气"), ("恐吓", "生气"),
    ("终极侮辱", "生气"), ("拱火", "生气"), ("非常坏", "生气"), ("嫌弃", "生气"),
    ("呕", "生气"), ("咬他", "生气"),
    ("嘲笑", "嘲笑"), ("邪恶", "嘲笑"), ("难绷", "嘲笑"), ("绷住", "嘲笑"), ("捂嘴", "嘲笑"),
    ("无语", "无语"), ("沉默", "无语"), ("啧啧", "无语"), ("哦吼", "无语"),
    ("噫", "无语"), ("放空", "无语"), ("嘘", "无语"), ("禁言", "无语"),
    ("不给", "无语"), ("拒绝", "无语"), ("不要不要", "无语"), ("禁止", "无语"),
    ("呵", "无语"), ("邪门", "无语"),
    ("哈欠", "困"), ("午休", "困"), ("困", "困"), ("摸鱼", "困"), ("趴", "困"),
    ("刷刷", "困"),
    ("震撼", "惊讶"), ("惊讶", "惊讶"), ("震惊", "惊讶"), ("嗷呜", "惊讶"),
    ("哇", "惊讶"), ("惊", "惊讶"),
    ("吃瓜", "吃瓜"), ("看戏", "吃瓜"), ("围观", "吃瓜"), ("偷看", "吃瓜"), ("看猪", "吃瓜"),
    ("点赞", "点赞"), ("同意", "点赞"), ("一键三连", "点赞"), ("巴适", "点赞"),
    ("友好手势", "点赞"), ("大拇指", "点赞"),
    ("狗头", "狗头"), ("doge", "狗头"),
    ("求求", "撒娇"), ("理理我", "撒娇"), ("蹭蹭", "撒娇"), ("贴贴", "撒娇"),
    ("扑倒", "撒娇"), ("宠溺", "撒娇"), ("软糯", "撒娇"), ("舔", "撒娇"),
    ("摸下巴", "撒娇"), ("捏脸", "撒娇"), ("揉脸", "撒娇"), ("害羞", "撒娇"),
    ("亲亲", "撒娇"), ("舌吻", "撒娇"), ("求婚", "撒娇"), ("情侣杯", "撒娇"),
    ("好人喵", "撒娇"), ("妈妈", "撒娇"), ("娃娃机", "撒娇"), ("抱", "撒娇"),
    ("比心", "撒娇"), ("心心", "撒娇"), ("画爱心", "撒娇"), ("搓手", "撒娇"),
    ("超开心", "开心"), ("开心", "开心"), ("好耶", "开心"), ("哈哈", "开心"),
    ("笑", "开心"), ("喵乐", "开心"), ("撒钱", "开心"), ("恭喜发财", "开心"),
    ("红包", "开心"), ("干杯", "开心"), ("上菜", "开心"), ("开饭", "开心"),
    ("好吃", "开心"), ("蹦", "开心"), ("喵喵get", "开心"), ("真香", "开心"), ("喜", "开心"),
    ("抽象", "搞怪"), ("整活", "搞怪"), ("鬼脸", "搞怪"), ("杂技", "搞怪"),
    ("刷牙", "搞怪"), ("music", "搞怪"),
    ("滚筒洗猫", "搞怪"), ("套娃", "搞怪"), ("爆猫", "搞怪"), ("猫虫", "搞怪"),
    ("烟花", "搞怪"), ("太鼓", "搞怪"), ("电音", "搞怪"), ("贝斯", "搞怪"),
    ("吉他", "搞怪"), ("小提琴", "搞怪"), ("萨克斯", "搞怪"), ("大提琴", "搞怪"),
    ("主唱", "搞怪"), ("迫击炮", "搞怪"), ("电磁炮", "搞怪"), ("龙击炮", "搞怪"),
    ("巡飞弹", "搞怪"), ("扫射", "搞怪"), ("上膛", "搞怪"), ("铁锅", "搞怪"),
    ("桶装", "搞怪"), ("好多猫", "搞怪"), ("猪", "搞怪"), ("大角牛", "搞怪"),
    ("monkey", "搞怪"), ("giveyouup", "搞怪"), ("letyoudown", "搞怪"),
    ("ciallo", "搞怪"), ("smoker", "搞怪"), ("chapter", "搞怪"), ("钓鱼", "搞怪"),
]

# 系列号兜底
SERIES_FALLBACK = {"EX": "搞怪", "ADN": "搞怪", "TT": "撒娇", "特殊": "搞怪"}

NAME_RE = re.compile(r"^neko_([0-9a-fA-F]{8,})_(.+)$")


# ============================================================
# 扫描 / 分类
# ============================================================
def find_source() -> tuple:
    """找一个有素材的目录，返回 (Path, 图片数) 或 (None, 0)。"""
    from .sticker_library import IMPORTABLE_DIRS
    for d in IMPORTABLE_DIRS:
        p = Path(d)
        if not p.exists() or not p.is_dir():
            continue
        n = sum(1 for f in p.rglob("*") if f.is_file() and f.suffix.lower() in IMG_EXTS)
        if n:
            return p, n
    return None, 0


def scan(src: Path) -> list:
    """
    产出 [(文件, 子目录名或'')]

    ⚠️ 旧版是「有子目录就只扫子目录，否则只扫顶层」，
       导致「顶层平铺 + 子目录」混排时顶层文件被静默漏掉。
       现在统一 rglob，第一级目录名当分类依据。
    """
    src = Path(src)
    out = []
    for f in sorted(src.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in IMG_EXTS:
            continue
        rel = f.relative_to(src).parts
        out.append((f, rel[0] if len(rel) > 1 else ""))
    return out


def parse_neko_name(fname: str) -> tuple:
    stem = Path(fname).stem
    m = NAME_RE.match(stem)
    rest = m.group(2) if m else stem
    if "_" in rest:
        code, name = rest.split("_", 1)
    else:
        code, name = rest, rest
    series = re.sub(r"\d+$", "", code) or code
    return code, series, name


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


def tag_from_name(fname: str) -> tuple:
    """返回 (tag, 依据)。认不出返回 ('', '')。"""
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


def classify(files: list, override_tag: str = "") -> dict:
    """
    按「文件夹 → 中文名关键词 → 系列号」定标签。
    返回 {Path: (tag, 依据)}
    """
    plan = {}
    for f, folder in files:
        if override_tag:
            plan[f] = (override_tag, "手动指定")
            continue
        ftag = resolve_folder_tag(folder) if folder else ""
        if ftag:
            plan[f] = (ftag, f"文件夹「{folder}」")
            continue
        ntag, why = tag_from_name(f.name)
        plan[f] = (ntag or "搞怪", why or "认不出→兜底")
    return plan


# ============================================================
# 写入
# ============================================================
def _maybe_shrink(data: bytes, suffix: str) -> bytes:
    """
    复制入库时把过大的静态图缩到 STICKER_MAX_SIDE（默认 512）。

    为什么：AI 出的图经常 1024²、一两 MB，QQ 表情发出去照样被压成小图，
    传得还慢。GIF 不动（一压动画就没了）。
    """
    if not STICKER_SHRINK or suffix == ".gif" or not data:
        return data
    try:
        import io

        from PIL import Image
        im = Image.open(io.BytesIO(data))
        if max(im.size) <= STICKER_MAX_SIDE:
            return data
        im.thumbnail((STICKER_MAX_SIDE, STICKER_MAX_SIDE))
        buf = io.BytesIO()
        if suffix in (".jpg", ".jpeg"):
            im.convert("RGB").save(buf, "JPEG", quality=88, optimize=True)
        else:
            im.convert("RGBA").save(buf, "PNG", optimize=True)
        out = buf.getvalue()
        return out if 0 < len(out) < len(data) else data
    except Exception as e:
        logger.debug(f"[STICKER] 压缩跳过（{type(e).__name__}: {e}）")
        return data


def apply(plan: dict, *, in_place: bool = True, shrink: bool = True) -> dict:
    """
    把分类结果写进 data/stickers/index.json。
    in_place=True 时不复制文件，index 直接指向素材原路径（省磁盘）。
    in_place=False 时复制进 data/stickers/<标签>/，顺便把大图缩小。
    """
    added = dup = fail = 0
    saved = 0
    by_tag = {}
    for f, (tag, _why) in plan.items():
        if tag not in TAG_LIBRARY:
            tag = "搞怪"
        try:
            data = Path(f).read_bytes()
        except Exception:
            fail += 1
            continue
        if len(data) < 64:
            fail += 1
            continue

        md5 = sticker_lib.md5_of(data)
        if md5 in sticker_lib.index:
            dup += 1
            continue

        if in_place:
            # 存绝对路径：is_usable() 用 os.path.exists 校验，
            # 相对路径依赖进程 cwd，一旦 cwd 变化（脚本/容器入口不同）全变坏数据。
            dst = Path(f).resolve()
        else:
            tag_dir = STICKER_ROOT / tag
            tag_dir.mkdir(parents=True, exist_ok=True)
            dst = tag_dir / f"{md5}{Path(f).suffix.lower()}"
            try:
                _body = _maybe_shrink(data, Path(f).suffix.lower()) if shrink else data
                dst.write_bytes(_body)
                saved += max(0, len(data) - len(_body))
            except Exception:
                fail += 1
                continue

        sticker_lib.index[md5] = {
            "tag": tag,
            "path": str(dst),
            "used_count": 0,
            "created_at": time.time(),
            "desc": "",
            "quality": 0.5,
        }
        added += 1
        by_tag[tag] = by_tag.get(tag, 0) + 1
        if added % 100 == 0:
            sticker_lib._save()

    if added:
        sticker_lib._save()
    # 打到 docker logs，方便不登服务器也能确认导入到底有没有生效
    logger.info(
        f"[STICKER] 导入完成：新增 {added}，重复 {dup}，失败 {fail}，"
        f"库存 {len(sticker_lib.index)}，分布 {by_tag}"
        + (f"，压缩省下 {saved / 1024 / 1024:.1f}MB" if saved else "")
    )
    return {"added": added, "dup": dup, "fail": fail, "by_tag": by_tag,
            "saved": saved}


def import_dir(src=None, in_place: bool = True, override_tag: str = "",
               shrink: bool = True) -> dict:
    """
    一站式导入：找目录 → 扫描 → 分类 → 写入。
    返回 {ok, src, scanned, added, dup, fail, by_tag}
    """
    if src is None:
        src, n = find_source()
        if not src:
            return {"ok": False, "error": "没找到素材目录（data/memes 等）"}
    src = Path(src)
    if not src.exists():
        return {"ok": False, "error": f"目录不存在：{src}"}

    files = scan(src)
    if not files:
        return {"ok": False, "error": f"{src} 里没有图片"}

    plan = classify(files, override_tag=override_tag)
    res = apply(plan, in_place=in_place, shrink=shrink)
    res.update({"ok": True, "src": str(src), "scanned": len(files)})
    return res


def retag(md5: str, tag: str) -> bool:
    """改单张的标签。"""
    if tag not in TAG_LIBRARY or md5 not in sticker_lib.index:
        return False
    sticker_lib.index[md5]["tag"] = tag
    sticker_lib._save()
    return True


def summary_line() -> str:
    """给命令回显用的一句话。"""
    h = sticker_lib.health()
    n_imp, where = count_importable()
    parts = [f"库存 {h['total']} 张（能发 {h['usable']}）"]
    if h["broken_file"]:
        parts.append(f"坏数据 {h['broken_file']}")
    if n_imp:
        parts.append(f"未入库 {n_imp} 张（{'、'.join(where)}）")
    return "，".join(parts)
