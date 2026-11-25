# services/mmr.py
"""MMR（Maximal Marginal Relevance）通用选择器。

为什么单独一个模块：
  · 检索证据（memory_v2）和风格样本（style_vault）都要「相关 + 不重复」；
  · 两处各写一遍容易出现 λ 口径不一致，评测也没法复用。

公式（Carbonell & Goldstein, 1998）：
    MMR = λ × rel(i) − (1−λ) × max_{j∈已选} sim(i,j)

约定：
  · rel 由调用方归一化到 0~1（本模块只做相对比较，不再猜量纲）；
  · sim 由调用方给（token Jaccard / 长度桶 / 语气桶都行），返回 0~1；
  · λ=1 退化成纯相关性排序，λ=0 退化成纯多样性。
"""
from __future__ import annotations


def pick(items: list, limit: int, *, relevance, similarity,
         lam: float = 0.7) -> list:
    """从 items 里贪心选 limit 条。

    items      : 任意对象列表
    relevance  : fn(item) -> float（0~1）
    similarity : fn(a, b) -> float（0~1）
    """
    items = list(items or [])
    limit = max(0, int(limit or 0))
    if limit <= 0 or not items:
        return []
    if limit >= len(items):
        return items[:limit]
    lam = max(0.0, min(1.0, float(lam or 0.7)))

    rel = {}
    for i, it in enumerate(items):
        try:
            rel[i] = max(0.0, min(1.0, float(relevance(it) or 0.0)))
        except Exception:
            rel[i] = 0.0

    picked, rest = [], list(range(len(items)))
    while rest and len(picked) < limit:
        best_i, best_s = None, None
        for i in rest:
            if picked:
                dup = 0.0
                for j in picked:
                    try:
                        dup = max(dup, float(similarity(items[i], items[j]) or 0.0))
                    except Exception:
                        continue
            else:
                dup = 0.0
            s = lam * rel[i] - (1.0 - lam) * dup
            if best_s is None or s > best_s:
                best_i, best_s = i, s
        if best_i is None:
            break
        picked.append(best_i)
        rest.remove(best_i)
    return [items[i] for i in picked]
