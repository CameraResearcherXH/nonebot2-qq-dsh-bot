# services/sticker_library.py
from core import tuning
import hashlib
import json
import os
import time
import random
from pathlib import Path

try:
    from nonebot import logger
except Exception:  # 允许脱离 nonebot 跑脚本
    import logging

    logger = logging.getLogger(__name__)

STICKER_ROOT = Path("data/stickers")
STICKER_ROOT.mkdir(parents=True, exist_ok=True)
INDEX_FILE = STICKER_ROOT / "index.json"

# 标签表：DeepSeek 通过 <sticker:标签> 自主发表情，所以这里就是她能选的"情绪词表"。
# 后 5 个是为了对齐用户整理好的表情包分类（难过/撒娇/搞怪/思考/打招呼）。
TAG_LIBRARY = [
    "开心", "得意", "生气", "难过", "撒娇", "搞怪", "思考", "打招呼",
    "无语", "嘲笑", "委屈", "吃瓜", "惊讶", "困", "害羞", "点赞", "狗头", "哭",
]

# ================= 容量与策展 =================
# 库存上限。超了就淘汰「最不值得留的」，淘汰名单由 DeepSeek 判精品决定。
STICKER_MAX = tuning.i("STICKER_MAX")
# 每次超限时多清一点，避免刚清完又超
STICKER_PRUNE_BUFFER = tuning.i("STICKER_PRUNE_BUFFER")
# 单个标签软上限（超过后新图要过 DeepSeek 审查才收）
STICKER_TAG_SOFT_CAP = tuning.i("STICKER_TAG_SOFT_CAP")


IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

# 常见「原材料」目录：解压了表情包但还没导入时，这里会有一堆图
IMPORTABLE_DIRS = ("data/memes", "data/sticker_src", "data/表情包")


def count_importable() -> tuple:
    """
    统计「还没导入」的素材数量，返回 (总数, 命中的目录)。
    用来在 /表情统计 里提示用户：图在那儿但没入库，等于没有。

    ⚠️ 必须排除 index 里已经登记的路径：
       导入默认是 in-place（文件留在 data/memes 原地，index 只存路径），
       不排除的话导完 318 张还会一直显示「未入库 318 张」，纯误导。
    """
    indexed = set()
    for info in sticker_lib.index.values():
        if not isinstance(info, dict):
            continue
        p = str(info.get("path") or "")
        if not p:
            continue
        try:
            indexed.add(os.path.normcase(os.path.abspath(p)))
        except Exception:
            pass

    total = 0
    hit = []
    for d in IMPORTABLE_DIRS:
        p = Path(d)
        if not p.exists() or not p.is_dir():
            continue
        n = 0
        for f in p.rglob("*"):
            if not f.is_file() or f.suffix.lower() not in IMG_EXTS:
                continue
            try:
                if os.path.normcase(str(f.resolve())) in indexed:
                    continue
            except Exception:
                pass
            n += 1
        if n:
            total += n
            hit.append(f"{d}（{n} 张）")
    return total, hit


def _score(info: dict) -> float:
    """
    确定性价值分（0~1），只用来挑「淘汰候选池」，最终删不删由 DeepSeek 决定。
      used_count  用过几次（她真的挑过 → 有价值）
      last_picked 多久没被挑中（60 天没用 → 价值归零）
      quality     DeepSeek 打的精品分（0~1）
    """
    used = min(float(info.get("used_count", 0) or 0) / 10.0, 1.0)
    quality = float(info.get("quality", 0.5) or 0.5)
    last = info.get("last_picked_at") or info.get("created_at") or 0
    days = max(0.0, (time.time() - float(last)) / 86400.0)
    recency = max(0.0, 1.0 - days / 60.0)
    return round(0.5 * used + 0.3 * recency + 0.2 * quality, 4)


def _norm_desc(s: str) -> set:
    """把描述切成 2-gram 集合，用来判重。"""
    t = "".join(ch for ch in str(s or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    if len(t) < 2:
        return set(t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def desc_similarity(a: str, b: str) -> float:
    sa, sb = _norm_desc(a), _norm_desc(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)




class StickerLibrary:
    def __init__(self):
        self.index = {}
        self.hour_count = {}
        self._load()

    def _load(self):
        if INDEX_FILE.exists():
            try:
                self.index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
            except Exception:
                self.index = {}

    def _save(self):
        """
        原子写入 + 重试。

        为什么不能直接 write_text：
          · Windows 上文件被杀毒/其它句柄短暂占用 → PermissionError
          · 直接覆盖写时如果中途崩了（磁盘满、进程被杀）→ index.json 变半截
            （这个项目已经在 docker 构建时踩过一次"文件被截断"的坑）
        所以：先写 .tmp，再 os.replace 原子替换。
        """
        data = json.dumps(self.index, ensure_ascii=False, indent=2)
        tmp = INDEX_FILE.with_name(INDEX_FILE.name + ".tmp")
        for attempt in range(3):
            try:
                tmp.write_text(data, encoding="utf-8")
                os.replace(tmp, INDEX_FILE)   # 同一分区内原子替换
                return
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
            except Exception as e:
                logger.warning(f"[STICKER] 保存 index.json 失败: {type(e).__name__}: {e}")
                break
        # 兜底：直接覆盖（至少别把内存里的数据丢掉）
        try:
            INDEX_FILE.write_text(data, encoding="utf-8")
        except Exception as e:
            logger.error(f"[STICKER] ❌ index.json 写不进去: {type(e).__name__}: {e}")
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    @staticmethod
    def md5_of(data: bytes) -> str:
        return hashlib.md5(data).hexdigest()

    def already_exists(self, md5: str) -> bool:
        return md5 in self.index

    def check_hourly_limit(self, chat_id: str, limit: int = 10) -> bool:
        hour = int(time.time() // 3600)
        key = f"{chat_id}:{hour}"
        self.hour_count[key] = self.hour_count.get(key, 0)
        return self.hour_count[key] < limit

    def add(self, data: bytes, tag: str, chat_id: str, ext: str = ".png",
            desc: str = "", quality: float = 0.5,
            reaction: float = 0.0) -> dict:
        md5 = self.md5_of(data)
        if self.already_exists(md5):
            return {"ok": False, "reason": "已存在"}
        if not self.check_hourly_limit(chat_id):
            return {"ok": False, "reason": "这一小时收够了"}

        tag_dir = STICKER_ROOT / tag
        tag_dir.mkdir(parents=True, exist_ok=True)
        file_path = tag_dir / f"{md5}{ext}"
        file_path.write_bytes(data)

        self.index[md5] = {
            "tag": tag,
            "path": str(file_path),
            "used_count": 0,
            "created_at": time.time(),
            "desc": str(desc or "")[:200],       # 视觉描述，策展/判重靠它
            "quality": float(quality or 0.5),    # DeepSeek 打的精品分
            # 「这张图让她反应多大」（= 收集时的强度 Δ），不是她当时的心情水平。
            # 以后挑图/排序可以按这个来：反应大的图更容易被想起。
            "reaction": round(float(reaction or 0.0), 3),
        }
        self._save()
        key = f"{chat_id}:{int(time.time() // 3600)}"
        self.hour_count[key] = self.hour_count.get(key, 0) + 1
        return {"ok": True, "md5": md5, "path": str(file_path)}

    # ================= 健康检查 =================
    @staticmethod
    def is_usable(info: dict) -> bool:
        """
        这条到底能不能发出去？
          · 有 mface_id → 走 mface 发送（信任它，无法本地验证）
          · 否则必须有 path 且文件真的存在
        ⚠️ 之前不检查，index 里只要有条目就会去挑，
           结果挑到「文件已被删」的条目 → 发送静默失败 → 表现为"她从来不发表情"。
        """
        if not isinstance(info, dict):
            return False
        if info.get("mface_id"):
            return True
        p = str(info.get("path") or "")
        return bool(p) and os.path.exists(p)

    def health(self) -> dict:
        """库存健康度：多少能发、多少是坏数据。"""
        total = len(self.index)
        usable = broken_file = mface = 0
        broken_ids = []
        for md5, info in self.index.items():
            if not isinstance(info, dict):
                continue
            if info.get("mface_id"):
                mface += 1
                usable += 1
                continue
            p = str(info.get("path") or "")
            if p and os.path.exists(p):
                usable += 1
            else:
                broken_file += 1
                broken_ids.append(md5)
        return {
            "total": total, "usable": usable, "broken_file": broken_file,
            "mface": mface, "broken_ids": broken_ids,
        }

    def clean_broken(self) -> int:
        """清掉「path 不存在且没有 mface_id」的坏条目（不动 mface 条目）。"""
        h = self.health()
        removed = 0
        for md5 in h["broken_ids"]:
            info = self.index.get(md5) or {}
            if info.get("mface_id"):
                continue
            self.index.pop(md5, None)
            removed += 1
        if removed:
            self._save()
            logger.info(f"[STICKER] 🧹 清理 {removed} 条坏数据（文件已不存在）")
        return removed

    # ================= 挑选 =================
    def pick_by_tag(self, tag: str) -> dict | None:
        """
        加权随机：用得少的优先（保证多样性），quality 高的权重更大（精品优先）。
        weight = (1 / (1 + used_count)) * (0.5 + quality)

        ⚠️ 只从「真的能发出去」的条目里挑（见 is_usable）。
        """
        candidates = [
            (md5, info) for md5, info in self.index.items()
            if info.get("tag") == tag and self.is_usable(info)
        ]
        if not candidates:
            return None
        weights = []
        for _, info in candidates:
            used = float(info.get("used_count", 0) or 0)
            q = float(info.get("quality", 0.5) or 0.5)
            weights.append((1.0 / (1.0 + used)) * (0.5 + q))
        md5, info = random.choices(candidates, weights=weights, k=1)[0]
        info["used_count"] = info.get("used_count", 0) + 1
        info["last_picked_at"] = time.time()
        self._save()
        return {"md5": md5, **info}

    def pick_any(self) -> dict | None:
        """随便挑一张能发的（兜底用，避免标签不匹配时完全发不出去）。"""
        usable = [(m, i) for m, i in self.index.items() if self.is_usable(i)]
        if not usable:
            return None
        md5, info = random.choice(usable)
        info["used_count"] = info.get("used_count", 0) + 1
        info["last_picked_at"] = time.time()
        self._save()
        return {"md5": md5, **info}

    def resolve_tag(self, word: str) -> str:
        """把自由心情词收敛到已有标签（精确 → 包含 → 固定词），否则允许新词。

        2026-11-20：表情标签不再固定 18 词，但也不能每个词都建桶。
        新词先按包含关系挂到最接近的旧标签；确实全新的才返回原词。
        """
        w = str(word or "").strip()
        if not w:
            return "吃瓜"
        existing = {str(i.get("tag") or "") for i in self.index.values()}
        if w in existing or w in TAG_LIBRARY:
            return w
        # 已有标签 / 固定词里找包含关系（「得意洋洋」↔「得意」）
        for t in sorted(existing | set(TAG_LIBRARY), key=len, reverse=True):
            if t and (t in w or w in t):
                return t
        return w

    def tag_counts(self) -> dict:
        out = {}
        for info in self.index.values():
            t = str(info.get("tag") or "")
            if t:
                out[t] = out.get(t, 0) + 1
        return out

    def top_tags(self, n: int = 20) -> list:
        return [t for t, _ in sorted(self.tag_counts().items(),
                                     key=lambda x: -x[1])[:n]]

    def pick_by_fuzzy(self, tag: str) -> dict | None:
        """
        按自由心情词挑一张：库内 tag 和这个词互相包含就算命中。
        她写「得意洋洋」、库里存的是「得意」，也能发到那张。
        找不到返回 None（调用方再走随机兜底）。
        """
        t = str(tag or "").strip()
        if not t:
            return None
        hit_tag = ""
        for info in self.index.values():
            if not self.is_usable(info):
                continue
            _tag = str(info.get("tag") or "").strip()
            if _tag and (t in _tag or _tag in t):
                hit_tag = _tag
                break
        if not hit_tag:
            return None
        return self.pick_by_tag(hit_tag)

    # ================= 判重 =================
    def find_similar(self, desc: str, tag: str = "", threshold: float = 0.8):
        """同标签下找描述高度相似的已有表情（判重用）。返回 (md5, 相似度) 或 None。"""
        if not desc:
            return None
        best = None
        for md5, info in self.index.items():
            if tag and info.get("tag") != tag:
                continue
            sim = desc_similarity(desc, info.get("desc") or "")
            if sim >= threshold and (best is None or sim > best[1]):
                best = (md5, sim)
        return best

    def tag_count(self, tag: str) -> int:
        return sum(1 for i in self.index.values() if i.get("tag") == tag)

    # ================= 容量 / 淘汰 =================
    def over_limit(self) -> bool:
        return len(self.index) > STICKER_MAX

    def prune_candidates(self, n: int = 0) -> list:
        """
        挑出「最不值得留」的候选池（按确定性分数升序）。
        注意：这只是候选，真正删谁由 DeepSeek 判精品决定。
        """
        if n <= 0:
            n = max(len(self.index) - STICKER_MAX + STICKER_PRUNE_BUFFER, 10)
        ranked = sorted(self.index.items(), key=lambda kv: _score(kv[1]))
        return [{"md5": m, "score": _score(i), **i} for m, i in ranked[:n]]

    def set_quality(self, md5: str, quality: float):
        if md5 in self.index:
            self.index[md5]["quality"] = max(0.0, min(1.0, float(quality)))

    def remove(self, md5: str, delete_file: bool = True) -> bool:
        info = self.index.pop(md5, None)
        if not info:
            return False
        if delete_file:
            try:
                p = Path(str(info.get("path") or ""))
                # 只删 data/stickers 下的文件，--in-place 指向别处的不动
                if p.exists() and STICKER_ROOT.resolve() in p.resolve().parents:
                    p.unlink()
            except Exception:
                pass
        return True

    def apply_prune(self, md5_list: list, delete_file: bool = True) -> int:
        removed = 0
        for m in md5_list or []:
            if self.remove(str(m), delete_file=delete_file):
                removed += 1
        if removed:
            self._save()
            logger.info(f"[STICKER] 🗑️ 淘汰 {removed} 张，当前 {len(self.index)} 张")
        return removed

    def enforce_limit_hard(self, delete_file: bool = True) -> int:
        """纯按分数硬删到上限（LLM 不可用时的兜底，保证不会无限膨胀）。"""
        over = len(self.index) - STICKER_MAX
        if over <= 0:
            return 0
        ranked = sorted(self.index.items(), key=lambda kv: _score(kv[1]))
        victims = [m for m, _ in ranked[:over]]
        removed = self.apply_prune(victims, delete_file=delete_file)
        logger.info(
            f"[STICKER] ⚠️ 超出上限 {STICKER_MAX}，按分数兜底淘汰 {removed} 张"
        )
        return removed

    def save(self):
        self._save()

    def pick_for_reply(self, reply_text: str) -> dict | None:
        """兜底：DeepSeek 没指定 <sticker:标签> 时，从回复文本里猜一个标签。"""
        mapping = {
            "哈哈": "开心", "好耶": "开心", "笑死": "嘲笑", "无语": "无语",
            "委屈": "委屈", "吃瓜": "吃瓜", "震惊": "惊讶", "困": "困",
            "生气": "生气", "难过": "难过", "呜呜": "难过", "哭": "哭",
            "抱抱": "撒娇", "哼": "撒娇", "嗯？": "思考", "？": "思考",
            "草": "搞怪", "乐": "搞怪", "难绷": "搞怪", "你好": "打招呼",
        }
        for kw, tag in mapping.items():
            if kw in reply_text:
                pick = self.pick_by_tag(tag)
                if pick:
                    return pick
        return None

    # ================= 统计 =================
    def stats(self) -> dict:
        """返回库的分布统计"""
        total = len(self.index)
        by_tag = {}
        image_count = 0
        mface_count = 0
        created_today = 0
        today_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))

        for md5, info in self.index.items():
            tag = info.get("tag") or "未分类"
            by_tag.setdefault(tag, []).append({**info, "md5": md5})
            if info.get("mface_id"):
                mface_count += 1
            else:
                image_count += 1
            if info.get("created_at", 0) >= today_start:
                created_today += 1

        # 每个标签按使用次数排，并取最常见的一张
        tag_detail = {}
        for tag, items in by_tag.items():
            items_sorted = sorted(items, key=lambda x: -x.get("used_count", 0))
            top = items_sorted[0]
            tag_detail[tag] = {
                "count": len(items),
                "top_md5": top["md5"],
                "top_used": top.get("used_count", 0),
                "top_is_mface": bool(top.get("mface_id")),
                "top_summary": top.get("summary", ""),
            }

        premium = sum(1 for i in self.index.values() if float(i.get("quality", 0) or 0) >= 0.85)
        h = self.health()

        return {
            "total": total,
            "image": image_count,
            "mface": mface_count,
            "created_today": created_today,
            "by_tag": tag_detail,
            "limit": STICKER_MAX,
            "premium": premium,
            "usage": round(total / STICKER_MAX * 100, 1) if STICKER_MAX else 0.0,
            # 健康度：total 里有多少是真能发出去的
            "usable": h["usable"],
            "broken_file": h["broken_file"],
        }

    def total_used(self) -> int:
        return sum(info.get("used_count", 0) for info in self.index.values())

sticker_lib = StickerLibrary()
