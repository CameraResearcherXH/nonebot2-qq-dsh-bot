# services/profile_miner.py
"""
群友档案「挖矿」——把群聊里攒够的发言提炼成档案内容。

用户 2026-11-20：
  「群里记录群友的档案只有称呼，没有内容和事情。」

为什么会这样：`profiles.upsert_profile()` 只写群名片/别名，
`touch_profile()` 只记「上次聊到」；真正的「简介 / 性格 / 事情」全靠她写
`[[MEMO|kind:bond|...]]`，而 MEMO 是「真的触动才写」——群里闲聊基本不写。
结果所有人的档案都是空壳。

这里的做法（和心情判读同一套思路：让第三方模型干脏活）：
  · 每收到一条群消息就计数；
  · 同一个人在同一个群攒够 `PROFILE_MINE_MIN_MSGS` 条、且距上次挖掘超过
    `PROFILE_MINE_INTERVAL_H` 小时 → 后台跑一次提炼；
  · 提炼只用**这个人自己说过的话**（不拿别人的话往他身上安）；
  · 产出 identity / personality / scenario / depth_note + 最多 3 条「事情」，
    写回 profiles（事情进 bond 时间线，档案卡上的「事情」就有了）。

失败绝不抛：拿不到素材、模型抽风、JSON 坏 → 记 debug 返回空。
"""
from __future__ import annotations
from core import tuning

import json
import os
import re
import time
from pathlib import Path

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

ENABLED = os.getenv("PROFILE_MINE_ENABLED", "true").lower() in ("1", "true", "yes", "on")
# 2026-11-24：由 providers 决定（PROFILE_PROVIDER / PROFILE_MODEL）
from .providers import resolve as _resolve_provider  # noqa: E402

_PROFILE_P = _resolve_provider("profile")
MODEL = _PROFILE_P["model"]
XAI_API_KEY = _PROFILE_P["api_key"]
XAI_BASE_URL = _PROFILE_P["base_url"].rstrip("/")
XAI_MODEL = _PROFILE_P["model"]
TIMEOUT = tuning.f("PROFILE_MINE_TIMEOUT")
MIN_MSGS = tuning.i("PROFILE_MINE_MIN_MSGS")     # 攒多少条发言挖一次
INTERVAL_H = tuning.f("PROFILE_MINE_INTERVAL_H")  # 同一个人多久挖一次
SAMPLE_MAX = tuning.i("PROFILE_MINE_SAMPLE")      # 一次喂多少条他的发言
STATE_FILE = Path(os.getenv("PROFILE_MINE_FILE", "data/profile_mine.json"))

_CACHE: dict = {}


def _load() -> dict:
    global _CACHE
    if _CACHE:
        return _CACHE
    try:
        _CACHE = json.loads(STATE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        _CACHE = {}
    return _CACHE


def _save(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except Exception as e:
        logger.debug(f"[PROFILE-MINE] 存状态失败: {type(e).__name__}: {e}")


def note_message(gid: str, qq: str) -> bool:
    """
    记一条消息。返回 True 表示「该挖了」（调用方起后台任务）。
    """
    if not ENABLED:
        return False
    gid = str(gid or "").strip()
    qq = str(qq or "").strip()
    if not gid or not qq.isdigit():
        return False
    # 她自己的号（当前号 / 历史号）不建档案：换号后旧号不该变成「陌生群友」
    try:
        from services import identity as _identity
        if _identity.is_self(qq):
            return False
    except Exception:
        pass
    d = _load()
    g = d.setdefault(gid, {})
    rec = g.setdefault(qq, {"n": 0, "at": 0})
    rec["n"] = int(rec.get("n") or 0) + 1
    last = float(rec.get("at") or 0)
    due = (rec["n"] >= MIN_MSGS
           and (not last or time.time() - last > INTERVAL_H * 3600))
    if due:
        rec["n"] = 0
        rec["at"] = time.time()
        _save(d)
        return True
    # ⚠️ 不能每条消息都写盘（群聊一秒钟好几条）：每 5 条落一次，
    #    进程重启最多丢 4 条计数，无所谓。
    if rec["n"] % 5 == 0:
        _save(d)
    return False


def _base() -> tuple:
    p = _resolve_provider("profile")
    return (p["base_url"].rstrip("/"), (p["api_key"] or "").strip())


def _samples(qq: str, gid: str) -> tuple:
    """这个人自己在群里说过的话（按条数取最近 N 条）。返回 (名字, [句子])。"""
    rows = []
    try:
        from . import dialogue_log
        for r in dialogue_log.load_recent(gid, limit=400):
            if str(r.get("qq") or "") != str(qq):
                continue
            txt = re.sub(r"\s+", " ", str(r.get("text") or "")).strip()
            if txt:
                rows.append(txt[:120])
    except Exception:
        rows = []
    name = ""
    try:
        from . import profiles
        name = profiles.call_of(qq, "", gid) or profiles.get_profile(qq).get("nickname") or ""
    except Exception:
        pass
    return str(name or qq), rows[-SAMPLE_MAX:]


def _build_prompt(name: str, qq: str, samples: list) -> str:
    body = "\n".join(f"{i}. {x}" for i, x in enumerate(samples, 1))
    return (
        "你在给一个 QQ 群成员建「人物档案」。只根据**他本人说过的话**总结，"
        "不要编造、不要把别人的话算在他头上。\n\n"
        f"【他】{name}（QQ {qq}）\n"
        f"【他最近在群里说过的话（共 {len(samples)} 条，最后一条最新）】\n{body}\n\n"
        "请输出 JSON（字段没有证据就留空字符串，不要瞎猜）：\n"
        '{"identity":"他是谁：身份/职业/在群里是什么角色（20字内）",'
        '"personality":"性格和说话风格（30字内，要具体，别写「很好相处」这种空话）",'
        '"scenario":"他在群里通常什么状态：潜水/活跃/爱抬杠/爱分享（20字内）",'
        '"note":"和他相处的注意点：雷区、偏好（20字内）",'
        '"events":[{"text":"他做过的一件具体的事（20字内）",'
        '"emotion":"happy/sad/angry/annoyed/love/neutral"}]}\n\n'
        "events 最多 3 条，只要**具体发生过**的事（「他说要考公」「他分享了炒股亏损图」），"
        "不要写「他聊过天」这种废话。没有就空数组。\n"
        "只输出 JSON，不要 markdown、不要解释。"
    )


def _parse_json(text: str) -> dict:
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        d = json.loads(t)
        return d if isinstance(d, dict) else {}
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
                return d if isinstance(d, dict) else {}
            except Exception:
                pass
    return {}


def _clip(s, n: int) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()[:n]


async def mine(qq: str, gid: str, *, force: bool = False) -> dict:
    """
    挖一次。返回 {"ok":bool, "updated":[...], "events":n, "err":...}。
    """
    qq = str(qq or "").strip()
    gid = str(gid or "").strip()
    if not ENABLED and not force:
        return {"ok": False, "err": "PROFILE_MINE_ENABLED=false"}
    if not qq.isdigit():
        return {"ok": False, "err": "不是 QQ 号"}
    name, samples = _samples(qq, gid)
    if len(samples) < 3:
        return {"ok": False, "err": f"他在这个群只说过 {len(samples)} 句，素材不够"}
    base, key = _base()
    if not base or not key:
        return {"ok": False, "err": "没配 XAI_BASE_URL / XAI_API_KEY"}

    payload = {
        "model": MODEL or "deepseek-chat",
        "messages": [
            {"role": "system", "content": "你只输出 JSON，不聊天。"},
            {"role": "user", "content": _build_prompt(name, qq, samples)},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.post(f"{base}/chat/completions",
                             headers={"Authorization": f"Bearer {key}"},
                             json=payload)
            r.raise_for_status()
            data = r.json()
            try:
                from . import llm_usage as _lu
                _lu.record_from_response(data, purpose="profile_miner")
            except Exception:
                pass
        content = str(((((data or {}).get("choices") or [{}])[0]
                        .get("message") or {}).get("content")) or "")
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}

    d = _parse_json(content)
    if not d:
        return {"ok": False, "err": f"模型没给 JSON：{content[:80]}"}

    fields = {}
    for k, n in (("identity", 60), ("personality", 80),
                 ("scenario", 60), ("depth_note", 60)):
        v = _clip(d.get(k), n)
        if v and v not in ("无", "未知", "空", "null", "none"):
            fields[k] = v

    # 事情：进 bond 时间线，档案卡上的「事情」就是这些
    added = 0
    try:
        from . import profiles
        old = " ".join(profiles.bond_texts(qq))
        for ev in (d.get("events") or [])[:3]:
            if not isinstance(ev, dict):
                continue
            txt = _clip(ev.get("text"), 40)
            if not txt or txt in old:
                continue
            emo = str(ev.get("emotion") or "").strip().lower()
            # 2026-11-25：只有带明确情绪的事件才进档案；中性事件留在记忆里。
            if not emo or emo in ("neutral", "无", "none", "平静"):
                continue
            try:
                profiles.add_bond_note(qq, txt, emotion=emo, weight=1.0)
                added += 1
            except Exception:
                continue
        if fields:
            profiles.update_fields(qq, **fields)
    except Exception as e:
        return {"ok": False, "err": f"写档案失败：{type(e).__name__}: {e}"}

    logger.info(f"[PROFILE-MINE] {qq}({gid}) 更新 "
                f"{'/'.join(fields) or '无字段'} + {added} 件事"
                f"（素材 {len(samples)} 句）")
    return {"ok": True, "updated": sorted(fields), "events": added,
            "fields": fields, "n": len(samples)}


def status_text() -> str:
    d = _load()
    lines = [f"档案挖掘：{'开' if ENABLED else '关'}｜model={MODEL or '?'}"
             f"｜攒 {MIN_MSGS} 句挖一次｜{INTERVAL_H:.0f}h 冷却｜样本上限 {SAMPLE_MAX}"]
    pend = []
    for gid, g in (d or {}).items():
        for qq, rec in (g or {}).items():
            n = int((rec or {}).get("n") or 0)
            if n:
                age_h = (time.time() - float((rec or {}).get("at") or 0)) / 3600
                pend.append(f"· {gid}/{qq}：{n} 句（上次挖 {age_h:.1f}h 前）")
    if pend:
        lines.extend(pend[:10])
    else:
        lines.append("· 还没有在攒的人（或者都挖过了）")
    return "\n".join(lines)
