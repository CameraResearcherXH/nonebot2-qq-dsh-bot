# services/sticker_curator.py
"""
表情库「策展人」。

分工：
  · 视觉模型（DashScope）—— 负责「这是什么图」，产出描述 + 标签
  · DeepSeek（本文件）    —— 负责「这张值不值得留」，产出精品分 + 淘汰名单

为什么不让代码自己算：
  「这张表情对她有没有用」不是 used_count 能算出来的。
  同一种情绪有 30 张时，该留哪张、该扔哪张，只有懂语境的模型能判。

所有函数都是**安全降级**的：LLM 不可用时返回 None / 空列表，
调用方自动退回「按确定性分数」的兜底策略，绝不因为网络问题卡住收表情。
"""
from core import tuning
import json
import os

import httpx
from nonebot import logger

from . import sticker_library as SL
from .sticker_library import TAG_LIBRARY, sticker_lib

# ⚠️ 不要在 import 时把 STICKER_MAX 拷成常量：
#    那样运行时改配置/测试改上限都不生效（原来就是这么错的）。
#    统一走 SL.STICKER_MAX 现取。

LLM_BASE = (os.getenv("XAI_BASE_URL", "") or "").rstrip("/")
LLM_KEY = os.getenv("XAI_API_KEY", "")
LLM_MODEL = os.getenv("XAI_MODEL", "deepseek-chat")
LLM_TIMEOUT = tuning.f("CURATOR_TIMEOUT")
ENABLED = os.getenv("STICKER_CURATOR", "true").lower() == "true"


def _extract_json(text: str):
    if not text:
        return None
    t = str(text).strip().replace("```json", "```")
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


async def _chat(prompt: str, max_tokens: int = 1500) -> dict | None:
    if not ENABLED or not LLM_BASE or not LLM_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=LLM_TIMEOUT, trust_env=False) as client:
            r = await client.post(
                f"{LLM_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_KEY}",
                         "Content-Type": "application/json"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                },
            )
        if r.status_code != 200:
            logger.warning(f"[CURATOR] LLM HTTP {r.status_code}: {r.text[:150]}")
            return None
        _d = r.json()
        try:
            from . import llm_usage as _lu
            _lu.record_from_response(_d, purpose="sticker")
        except Exception:
            pass
        return _extract_json(_d["choices"][0]["message"].get("content") or "")
    except Exception as e:
        logger.warning(f"[CURATOR] LLM 调用失败: {type(e).__name__}: {e}")
        return None


# ============================================================
# 1. 收藏时：这张值得收吗？
# ============================================================
async def judge_new(desc: str, tag: str, *, similar_desc: str = "",
                    tag_total: int = 0) -> dict | None:
    """
    返回 {"keep": bool, "quality": 0~1, "reason": "..."}，失败返回 None。

    只在「该标签库存已接近软上限」或「疑似重复」时才该调用 —— 别每张都调，费钱。
    """
    prompt = f"""你是当前人格的表情库管理员。她靠表情包表达情绪。

现在有一张新表情想入库：
  标签：{tag}
  画面描述：{desc}
  该标签现有库存：{tag_total} 张
{'  已有一张很像的：' + similar_desc if similar_desc else ''}

请判断这张值不值得收藏，并按 0~1 打分（0.9 以上是「精品，必留」）。
判断标准：
- 表达力强、情绪明确 → 高分
- 和已有表情高度重复 → 低分
- 画面模糊、看不出情绪、纯装饰 → 低分
- 这个标签已经很拥挤（{tag_total} 张），新图必须明显更好才值得留

只输出 JSON：{{"keep": true/false, "quality": 0.0~1.0, "reason": "一句话"}}"""

    data = await _chat(prompt, max_tokens=300)
    if not isinstance(data, dict):
        return None
    try:
        q = float(data.get("quality", 0.5))
    except Exception:
        q = 0.5
    return {
        "keep": bool(data.get("keep", True)),
        "quality": max(0.0, min(1.0, q)),
        "reason": str(data.get("reason") or "")[:80],
    }


# ============================================================
# 2. 超限时：删哪些？
# ============================================================
async def prune(need: int, candidates: list) -> list:
    """
    从候选池里挑出 need 个最不值得留的 md5。
    失败返回 []（调用方走 enforce_limit_hard 兜底）。
    """
    if need <= 0 or not candidates:
        return []

    lines = []
    for i, c in enumerate(candidates, 1):
        desc = (c.get("desc") or "（无描述）")[:60]
        lines.append(
            f"{i}. id={c['md5']} | 标签={c.get('tag','?')} | "
            f"用过{c.get('used_count', 0)}次 | 精品分{c.get('quality', 0.5)} | {desc}"
        )

    prompt = f"""你是当前人格的表情库管理员。表情库上限 {SL.STICKER_MAX} 张，现在超了，
需要淘汰 {need} 张。

下面是候选（都是「用得少 + 评分偏低」的），请挑出**最不值得保留**的 {need} 张：

{chr(10).join(lines)}

淘汰标准（按重要性排序）：
1. 同一情绪/场景已有更好的表达 → 淘汰差的那张
2. 描述看不出情绪、或纯装饰看不出用途 → 淘汰
3. 太冷门，日常聊天几乎用不上 → 淘汰
4. 反过来：情绪鲜明、通用性强、能补足标签空白的 → 一定保留

注意：不要因为某个标签张数多就把它全删光，每个标签至少留几张。
只输出 JSON：{{"drop": ["id1", "id2"]}}"""

    data = await _chat(prompt, max_tokens=1500)
    if not isinstance(data, dict):
        return []
    drop = data.get("drop") or []
    if not isinstance(drop, list):
        return []
    valid = {c["md5"] for c in candidates}
    out = [str(d) for d in drop if str(d) in valid]
    logger.info(f"[CURATOR] 🧹 DeepSeek 判定淘汰 {len(out)}/{need} 张")
    return out


# ============================================================
# 3. 批量打分（给存量库存补 quality）
# ============================================================
async def rate_batch(items: list, batch: int = 40) -> int:
    """
    给没有 quality 的表情批量打分，返回成功打分数量。
    items: [{"md5","tag","desc","used_count"}]
    """
    if not items:
        return 0
    rated = 0
    total = len(items)
    for i in range(0, total, batch):
        chunk = items[i:i + batch]
        lines = [
            f"{j + 1}. id={c['md5']} | 标签={c.get('tag','?')} | 描述：{(c.get('desc') or '无')[:50]}"
            for j, c in enumerate(chunk)
        ]
        prompt = f"""你是当前人格的表情库管理员。给下面每张表情打「精品分」（0~1）。

打分标准：
- 0.9~1.0：情绪极鲜明、通用性极强，聊天里随时能用
- 0.7~0.8：好用，情绪清楚
- 0.5~0.6：一般，能替代
- 0.3~0.4：画面模糊、看不出情绪
- 0.0~0.2：基本没用，纯装饰或看不懂

{chr(10).join(lines)}

只输出 JSON，key 是 id：{{"id1": 0.85, "id2": 0.6}}"""

        data = await _chat(prompt, max_tokens=1500)
        if not isinstance(data, dict):
            logger.warning(f"[CURATOR] 打分批次失败（{i}~{i + len(chunk)}）")
            continue
        for c in chunk:
            v = data.get(c["md5"])
            try:
                q = float(v)
            except Exception:
                continue
            sticker_lib.set_quality(c["md5"], q)
            rated += 1
        sticker_lib.save()
        logger.info(f"[CURATOR] 已打分 {min(i + batch, total)}/{total}")
    return rated


# ============================================================
# 3.5 按文件名批量分类（导入时用，不读图片，gif 也能分）
# ============================================================
async def classify_filenames(names: list, batch: int = 40) -> dict:
    """
    把一批文件名交给 DeepSeek 分类，返回 {文件名: 标签}。
    只看文件名里的中文意思，所以不需要读图片 —— gif 也能分类。
    """
    if not names:
        return {}
    result = {}
    total = len(names)
    for i in range(0, total, batch):
        chunk = names[i:i + batch]
        listing = "\n".join(f"{j + 1}. {n}" for j, n in enumerate(chunk))
        prompt = (
            "下面是一批表情包的文件名（中文部分是这张表情想表达的意思）。\n"
            "请为每一个分配**一个**情绪标签，只能从下面这些里选，不要自创：\n"
            f"{' / '.join(TAG_LIBRARY)}\n\n"
            f"{listing}\n\n"
            '只输出 JSON，格式：{"文件名": "标签"}，文件名要和上面给的完全一致，不要解释。'
        )
        data = await _chat(prompt, max_tokens=2000)
        if not isinstance(data, dict):
            logger.warning(f"[CURATOR] 文件名分类批次失败（{i}~{i + len(chunk)}）")
            continue
        for k, v in data.items():
            v = str(v).strip()
            if v in TAG_LIBRARY:
                result[str(k).strip()] = v
        logger.info(f"[CURATOR] 文件名分类 {min(i + batch, total)}/{total}")
    return result


# ============================================================
# 4. 一站式：收图前审查 + 收图后控容量
# ============================================================
async def review_before_collect(desc: str, tag: str) -> dict:
    """
    收表情前的审查。返回 {"accept": bool, "quality": float, "reason": str}
    只有在「该标签接近软上限」或「疑似重复」时才会真去问 LLM，其余直接放行。
    """
    tag_total = sticker_lib.tag_count(tag)
    similar = sticker_lib.find_similar(desc, tag=tag, threshold=0.8) if desc else None

    # 不拥挤、也不重复 → 直接放行，省钱
    if tag_total < SL.STICKER_TAG_SOFT_CAP and not similar:
        return {"accept": True, "quality": 0.5, "reason": "库容宽松，直接收"}

    if similar:
        sim_desc = sticker_lib.index.get(similar[0], {}).get("desc", "")
        logger.info(f"[CURATOR] 🔍 「{tag}」有相似表情（相似度 {similar[1]:.2f}），交 DeepSeek 判")
        verdict = await judge_new(desc, tag, similar_desc=sim_desc, tag_total=tag_total)
        if verdict and not verdict["keep"]:
            return {"accept": False, "quality": verdict["quality"],
                    "reason": f"重复：{verdict['reason']}"}
        if verdict:
            return {"accept": True, "quality": verdict["quality"],
                    "reason": verdict["reason"]}

    logger.info(f"[CURATOR] 🔍 「{tag}」已 {tag_total} 张，交 DeepSeek 判新图")
    verdict = await judge_new(desc, tag, tag_total=tag_total)
    if not verdict:
        return {"accept": True, "quality": 0.5, "reason": "LLM 不可用，放行"}
    return {"accept": verdict["keep"], "quality": verdict["quality"],
            "reason": verdict["reason"]}


async def maintain_capacity() -> dict:
    """
    容量维护：超上限就让 DeepSeek 判该删谁；LLM 不可用则按分数兜底。
    返回 {"before", "after", "removed", "by_llm"}
    """
    before = len(sticker_lib.index)
    if before <= SL.STICKER_MAX:
        return {"before": before, "after": before, "removed": 0, "by_llm": False}

    need = before - SL.STICKER_MAX + SL.STICKER_PRUNE_BUFFER
    cands = sticker_lib.prune_candidates(need)
    drop = await prune(need, cands)

    removed = sticker_lib.apply_prune(drop) if drop else 0
    by_llm = bool(drop)

    # LLM 没给够 / 没给 → 按分数硬删到上限
    if len(sticker_lib.index) > SL.STICKER_MAX:
        removed += sticker_lib.enforce_limit_hard()

    after = len(sticker_lib.index)
    logger.info(
        f"[CURATOR] 📦 容量维护：{before} → {after}（删 {removed} 张，"
        f"{'DeepSeek 判定' if by_llm else '分数兜底'}）"
    )
    return {"before": before, "after": after, "removed": removed, "by_llm": by_llm}
