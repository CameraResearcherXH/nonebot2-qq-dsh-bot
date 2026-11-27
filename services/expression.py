# services/expression.py
"""
表达学习（expression）——学「这个群 / 这个人怎么说话」，不是学知识。

和 lorebook 的分工（用户 2026-11-19）：
  · lorebook  = 知道什么（谁是谁、梗是什么意思、事实设定）
  · expression = 怎么说话（口癖、句式、语气词、爱怎么接话）

为什么要有：
  我们的人格卡是**全局固定**的，于是她在每个群、对每个人都用同一套腔调。
  真人不是这样：在 A 群爱用短句吐槽，对某个熟人语气会软下来。
  但**她自己知道**这些差异 —— 这就是用户说的「虽然在每个群每个人那里不太一样，
  但我清楚我自己的内心的感觉」。

三个作用域：
  group_<gid>   这个群的整体习惯（学所有人群聊时的说法）
  person_<qq>   和这个人说话时的习惯（学这个人自己的说法）
  自我视角       把上面两个合起来给她看：`self_view()`

用法：
  · chat 每攒够 N 条消息，后台 `mine()` 一次（一次调用同时产 group + persons）；
  · 回复前 `render(gid, qq)` 取 1 条群的 + 1 条这个人的，注入 prompt；
  · `/表现` 看自我视角，`/表现 清` 忘掉。

配置（.env 22 区）：
  EXPRESSION_ENABLED=true
  EXPRESSION_EVERY=40        # 每群攒多少条消息挖一次
  EXPRESSION_MIN_INTERVAL=600
  EXPRESSION_MAX_PER_SCOPE=30
  EXPRESSION_MODEL=（默认跟 XAI_MODEL）
"""
from core import tuning

import json
import os
import random
import re
import time
from pathlib import Path

try:
    import httpx
except Exception:
    httpx = None

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

from .config import BOT_MODE, PROXY, XAI_API_KEY, XAI_BASE_URL, XAI_MODEL
from . import ctx_budget

ENABLED = os.getenv("EXPRESSION_ENABLED", "true").lower() in ("1", "true", "yes", "on")
EVERY = tuning.i("EXPRESSION_EVERY")
MIN_INTERVAL = tuning.f("EXPRESSION_MIN_INTERVAL")
MAX_PER_SCOPE = tuning.i("EXPRESSION_MAX_PER_SCOPE")
# 2026-11-24：由 providers 决定（EXPRESSION_PROVIDER / EXPRESSION_MODEL）
from .providers import (resolve as _resolve_provider,
                        call_tokens as _call_tokens)  # noqa: E402

_EX_P = _resolve_provider("expression")
MODEL = _EX_P["model"]
XAI_API_KEY = _EX_P["api_key"]
XAI_BASE_URL = _EX_P["base_url"].rstrip("/")
XAI_MODEL = _EX_P["model"]
TIMEOUT = tuning.f("EXPRESSION_TIMEOUT")

EXP_DIR = Path("data/expression")
_LAST_MINE: dict = {}
_WARNED = set()


def enabled() -> bool:
    return bool(ENABLED and BOT_MODE != "mock" and XAI_API_KEY and httpx is not None)


# ============================ 存取 ============================
def _safe(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(s or ""))[:60] or "x"


def _path(scope: str) -> Path:
    return EXP_DIR / f"{_safe(scope)}.json"


def load(scope: str) -> list:
    try:
        p = _path(scope)
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else data
        return [x for x in (items or []) if isinstance(x, dict) and x.get("text")]
    except Exception:
        return []


def _save(scope: str, items: list) -> None:
    try:
        EXP_DIR.mkdir(parents=True, exist_ok=True)
        items = sorted(items, key=lambda x: float(x.get("at") or 0))[-MAX_PER_SCOPE:]
        _path(scope).write_text(
            json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[EXPR] 写入失败 {scope}: {type(e).__name__}: {e}")


def _merge(scope: str, new_items) -> int:
    """按文本去重合并；同一条出现多次 → count+1（说明确实是这个人的口癖）。"""
    cur = load(scope)
    index = {str(x.get("text")): x for x in cur}
    added = 0
    for it in new_items or []:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text") or "").strip()[:24]
        if not text:
            continue
        old = index.get(text)
        if old:
            old["count"] = int(old.get("count") or 1) + 1
            old["at"] = time.time()
        else:
            rec = {
                "text": text,
                "scene": str(it.get("scene") or "随口").strip()[:6],
                "tone": str(it.get("tone") or "自然").strip()[:6],
                "count": 1,
                "at": time.time(),
            }
            cur.append(rec)
            index[text] = rec
            added += 1
    _save(scope, cur)
    return added


def clear(scope: str = "") -> int:
    """scope 为空 → 清空全部（/表现 清）。"""
    n = 0
    try:
        files = [_path(scope)] if scope else list(EXP_DIR.glob("*.json"))
        for f in files:
            if f.exists():
                f.unlink()
                n += 1
    except Exception:
        pass
    return n


# ============================ 挖掘 ============================
def _build_prompt(rows) -> str:
    body = "\n".join(
        f"{str(n or q or '?')}: {str(t or '')[:60]}" for n, q, t in rows[-30:]
    )
    return (
        "你在观察一个 QQ 群的说话习惯。下面是最近的聊天记录（名字: 内容）。\n\n"
        f"{body}\n\n"
        "请找出两类**真正有辨识度**的说法：\n"
        "1) group：这个群特有的口癖、常用梗、语气词、句式（不是「哈哈哈」这种通用网络用语）；\n"
        "2) persons：其中某个人特有的说法（最多 2 个人）。\n\n"
        "每条要标：\n"
        "· scene：吐槽 / 附和 / 敷衍 / 安慰 / 接梗 / 自嘲 / 催 / 夸 / 问\n"
        "· tone：短 / 冲 / 软 / 阴阳 / 正经\n\n"
        "硬性要求：\n"
        "· 每条不超过 20 字，只留**说法本身**，不要带人名、QQ、链接、脏话；\n"
        "· 宁缺毋滥：看不出来就给空数组，别把普通句子凑数；\n"
        "· persons 里的 qq 必须是记录里出现过的 QQ 号。\n\n"
        '只输出 JSON：{"group":[{"text":"","scene":"","tone":""}],'
        '"persons":[{"qq":"","items":[{"text":"","scene":"","tone":""}]}]}'
    )


def _parse(text: str) -> dict:
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {}


async def mine(gid: str, rows, force: bool = False) -> int:
    """
    从最近一段群聊里挖表达。rows: [(名字, QQ, 文本), ...]。返回新增条数。

    一次调用同时产 group + persons，省一半 token。
    """
    gid = str(gid or "").strip()
    if not gid or not enabled() or not rows:
        return 0
    now = time.time()
    if not force and now - float(_LAST_MINE.get(gid) or 0) < MIN_INTERVAL:
        return 0
    _LAST_MINE[gid] = now

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "你只做说话风格归纳，不参与聊天，只输出 JSON。"},
            {"role": "user", "content": _build_prompt(rows)},
        ],
        "temperature": 0.3,
        **_call_tokens("expression", 600),
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {XAI_API_KEY}", "Content-Type": "application/json"}
    kwargs = {"timeout": TIMEOUT}
    if PROXY:
        kwargs["proxy"] = PROXY
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            try:
                r = await client.post(f"{XAI_BASE_URL}/chat/completions",
                                      headers=headers, json=payload)
                r.raise_for_status()
            except Exception:
                payload.pop("response_format", None)
                r = await client.post(f"{XAI_BASE_URL}/chat/completions",
                                      headers=headers, json=payload)
                r.raise_for_status()
            data = r.json()
            try:
                from . import llm_usage as _lu
                _lu.record_from_response(data, purpose="expression")
            except Exception:
                pass
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except Exception as e:
        if gid not in _WARNED:
            _WARNED.add(gid)
            logger.warning(f"[EXPR] {gid} 挖掘失败（不影响聊天）: {type(e).__name__}: {e}")
        else:
            logger.debug(f"[EXPR] {gid} 挖掘失败: {type(e).__name__}: {e}")
        return 0

    obj = _parse(content)
    added = 0
    g_items = obj.get("group") or []
    if isinstance(g_items, list) and g_items:
        added += _merge(f"group_{gid}", g_items)
    qqs = {str(q) for _, q, _ in rows if q}
    for p in (obj.get("persons") or []):
        if not isinstance(p, dict):
            continue
        qq = str(p.get("qq") or "").strip()
        items = p.get("items") or []
        if qq and qq in qqs and isinstance(items, list) and items:
            added += _merge(f"person_{qq}", items)
    if added:
        logger.info(f"[EXPR] {gid} 学到 {added} 条新说法")
    return added


# ============================ 取用 ============================
def _weight(it: dict) -> float:
    try:
        days = max(0.0, (time.time() - float(it.get("at") or 0)) / 86400)
    except Exception:
        days = 0.0
    return (1.0 + float(it.get("count") or 1)) / (1.0 + days * 0.5)


def _pick_one(scope: str) -> dict:
    items = load(scope)
    if not items:
        return {}
    weights = [_weight(x) for x in items]
    try:
        return random.choices(items, weights=weights, k=1)[0]
    except Exception:
        return random.choice(items)


def render(gid: str, qq: str = "") -> str:
    """
    回复前注入的风格块；没数据返回空串。

    ⚠️ 这里**不检查 enabled()**：已经学到的数据是免费的，
       就算临时没 key（挖不了新的），也不该让已有的表达失效。
    """
    lines = []
    g = _pick_one(f"group_{gid}") if gid else {}
    if g:
        lines.append(f"· 这个群常见的说法：{g.get('text')}（{g.get('scene')}·{g.get('tone')}）")
    p = _pick_one(f"person_{qq}") if qq else {}
    if p:
        lines.append(f"· 和他说话时你习惯：{p.get('text')}（{p.get('scene')}·{p.get('tone')}）")
    if not lines:
        return ""
    text = ("▍说话风格参考（学他们的语气，**不是知识**；别照抄原句、别每句都用）\n"
            + "\n".join(lines)
            + "\n用法：像他们那样说话，但说的是你自己的内容；同一个说法别连着用两次。")
    return ctx_budget.observe("expression", text, source=f"group_{gid}/{qq}")


def self_view(limit: int = 8) -> str:
    """「我在各处的样子」——把各 scope 的表达习惯汇总给她自己看。"""
    try:
        files = sorted(EXP_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        files = []
    if not files:
        return "🧬 我在各处的样子\n· 还没有学到东西（群里多聊几句就会开始记）"
    lines = ["🧬 我在各处的样子（我自己总结的，不是谁要求的）"]
    shown = 0
    total = 0
    for f in files:
        scope = f.stem
        items = load(scope)
        if not items:
            continue
        total += len(items)
        if shown >= limit:
            continue
        top = sorted(items, key=lambda x: (-(int(x.get("count") or 1)), -float(x.get("at") or 0)))[:2]
        tag = "群 " + scope[6:] if scope.startswith("group_") else "对 " + scope[7:]
        body = "；".join(f"{x.get('scene')}·{x.get('tone')}（{x.get('text')}）" for x in top)
        lines.append(f"· {tag}：{body}")
        shown += 1
    if total == 0:
        return "🧬 我在各处的样子\n· 还没有学到东西"
    lines.append(f"（共 {total} 条；/表现 清 可以全部忘掉）")
    return "\n".join(lines)


def status() -> str:
    try:
        files = list(EXP_DIR.glob("*.json"))
        n = sum(len(load(f.stem)) for f in files)
    except Exception:
        files, n = [], 0
    head = (f"表达学习：{'启用' if enabled() else '关闭'}"
            f"｜每 {EVERY} 条挖一次｜已有 {len(files)} 个作用域 / {n} 条")
    if not enabled():
        head += "｜原因：BOT_MODE=mock / 没 key / 没 httpx"
    return head
