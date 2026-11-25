# services/journal_agent.py
"""
两天一复盘（离线自我反思）。

链路：
  本地 data/ 打包  →  POST {AGENT_BASE_URL}/agent/reflection  →  算力机 qwen2
  回收 {journal, impression, archive, merge}
    · journal    →  data/journals/YYYY-MM-DD.md（永久保留）
    · impression →  profiles.set_long_term_impression()（写回档案，下次 Prompt 带上）
    · archive    →  memory_v2.mark_outdated()（只标过时，不删）

注意：这一层只做「打包 + 回写」，不生成任何日记内容。
     日记必须由算力机上的角色模型写，本地绝不编造。
"""
from core import tuning
import json
import os
import time
from pathlib import Path

from nonebot import logger

from . import agent_client, memory_v2, mood, profiles

JOURNAL_DIR = Path("data/journals")
PROFILE_DIR = Path("data/profiles")
PERSONALITY_DIR = Path(__file__).parent / "Personality"

WINDOW_DAYS = tuning.f("REFLECT_WINDOW_DAYS")
OWNER_QQ = os.getenv("OWNER_QQ", "")

# 复盘 Agent 的任务指令（随包一起发过去，算力机不需要内置）
REFLECT_INSTRUCTION = """你就是这个角色本人。下面是你过去两天的经历碎片、心情波动、人物画像和记忆目录。

请做三件事，严格按 JSON 返回：
1. journal：以第一人称写一篇「这两天的心路历程」，像写日记，不要用 Markdown 列表，
   把开心、烦躁、感动的事揉进去，200~400 字。要有情绪，不要总结体。
2. impression：从日记里提炼一句「对这个人的核心印象总结」，不超过 40 字。
   这是你以后对他说话时的固定人设锚点，要具体、要有温度，不要套话。
3. archive_keywords：判断哪些旧记忆已经过时/重复/不再需要，
   返回它们的**原文关键词**（不要编 id，id 你猜不准）。
   每条关键词要能在记忆原文里原样找到，5~15 字，最多 5 条；没有就空数组。

返回格式（只输出 JSON，不要任何解释）：
{"journal": "...", "impression": "...", "archive_keywords": ["白米饭", "翻唱"]}
"""


# ================= 打包 =================
def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _collect_persona() -> dict:
    """角色卡（从磁盘读，不依赖进程内 current_personality，重启也能复盘）"""
    out = {}
    if PERSONALITY_DIR.exists():
        for f in PERSONALITY_DIR.glob("*.txt"):
            try:
                out[f.stem] = f.read_text(encoding="utf-8").strip()[:1500]
            except Exception:
                pass
    return out


def _collect_profiles() -> dict:
    profiles_map, bonds, styles = {}, {}, {}
    if not PROFILE_DIR.exists():
        return {"profiles": profiles_map, "bonds": bonds, "styles": styles}
    for f in PROFILE_DIR.glob("*.json"):
        name = f.stem
        if name.endswith("_profile"):
            profiles_map[name[:-8]] = _read_json(f, {})
        elif name.endswith("_bond"):
            bonds[name[:-5]] = _read_json(f, {})
        elif name.endswith("_style"):
            styles[name[:-6]] = _read_json(f, {})
    return {"profiles": profiles_map, "bonds": bonds, "styles": styles}


def _collect_memories(since_ts: float) -> dict:
    """记忆目录 + 窗口内记忆的详情。

    ⚠️ 走 `memory_v2.list_items()` / `recall_detail()`，不要直接读 index.json：
    sqlite 后端没有那个文件，直接读会得到「0 条记忆」（2026-11-25 修）。
    """
    items = memory_v2.list_items(limit=300)
    recent = [
        d for d in items
        if max(float(d.get("created_at") or 0), float(d.get("last_hit_at") or 0))
        >= float(since_ts or 0)
    ]
    details = []
    for d in recent[:80]:
        try:
            _full = memory_v2.recall_detail(d["id"]) or ""
        except Exception:
            _full = ""
        details.append({
            "id": d["id"],
            "kind": d.get("kind", ""),
            "about_qq": d.get("about_qq", ""),
            "emotion_hit": d.get("emotion_hit", False),
            "importance": d.get("importance", 0.5),
            "summary": d.get("summary", ""),
            "text_full": _full[:800],
        })
    catalog = [
        {
            "id": d["id"],
            "summary": d.get("summary", ""),
            "kind": d.get("kind", ""),
            "about_qq": d.get("about_qq", ""),
            "emotion_hit": d.get("emotion_hit", False),
            "at": d.get("created_at", 0),
        }
        for d in sorted(items, key=lambda x: -float(x.get("created_at") or 0))[:120]
    ]
    return {
        "catalog": catalog,
        "window_details": details,
        "stats": memory_v2.stats(),
    }


def _collect_thoughts(since_ts: float, limit: int = 20) -> list:
    """推理模型的思维链（deepseek-reasoner 才会有），复盘时当参考。"""
    p = Path("data/thoughts.jsonl")
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines()[-400:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("at", 0) >= since_ts:
                out.append(rec)
    except Exception:
        pass
    return out[-limit:]


def _collect_journals(limit: int = 5) -> list:
    if not JOURNAL_DIR.exists():
        return []
    files = sorted(JOURNAL_DIR.glob("*.md"), reverse=True)[:limit]
    return [{"name": f.name, "text": f.read_text(encoding="utf-8")[:1200]} for f in files]


def collect_bundle(since_ts: float = None) -> dict:
    """把「档案 + 相处印象 + 心情 + 记忆目录与详情 + 角色卡 + 近期日记」打成一份包。"""
    now = time.time()
    since_ts = since_ts or (now - WINDOW_DAYS * 86400)
    bundle = {
        "task_type": "reflection",
        "instruction": REFLECT_INSTRUCTION,
        "window": {
            "since": since_ts,
            "until": now,
            "since_human": time.strftime("%Y-%m-%d %H:%M", time.localtime(since_ts)),
            "until_human": time.strftime("%Y-%m-%d %H:%M", time.localtime(now)),
            "days": WINDOW_DAYS,
        },
        "owner_qq": OWNER_QQ,
        "persona": _collect_persona(),
        "mood": {
            "value": mood.get_mood()["value"],
            "shifts": mood.recent_shifts(since_ts),
        },
        "journals": _collect_journals(),
        "thoughts": _collect_thoughts(since_ts),
    }
    bundle.update(_collect_profiles())
    bundle.update(_collect_memories(since_ts))
    return bundle


# ================= 回写 =================
def write_journal(text: str, *, window: dict = None, impression: str = "") -> Path:
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d")
    f = JOURNAL_DIR / f"{day}.md"
    stamp = time.strftime("%H:%M", time.localtime())
    body = [f"# {day} 的心路历程", ""]
    if window:
        body.append(f"> 复盘区间：{window.get('since_human', '')} ~ {window.get('until_human', '')}")
        body.append("")
    body.append(text.strip())
    if impression:
        body += ["", f"**核心印象**：{impression.strip()}"]
    body.append("")
    body.append(f"_（{stamp} 生成）_")
    with open(f, "a", encoding="utf-8") as fp:
        fp.write("\n".join(body) + "\n\n")
    return f


def _find_by_keyword(keyword: str) -> list:
    """在 active 记忆的 summary + 正文里找关键词，返回命中的 id 列表。"""
    kw = str(keyword or "").strip()
    if not kw:
        return []
    try:
        items = memory_v2.list_items(limit=500)
    except Exception:
        return []
    hits = []
    for d in items:
        try:
            _full = memory_v2.recall_detail(d["id"]) or ""
        except Exception:
            _full = ""
        blob = f"{d.get('summary', '')}\n{_full}"
        if kw in blob:
            hits.append(d["id"])
    return hits


def _pick_impression_target(bundle: dict) -> str:
    if OWNER_QQ:
        return OWNER_QQ
    # 没配主人 → 挑记忆最多的那个 qq
    counter = {}
    for d in bundle.get("catalog", []):
        qq = str(d.get("about_qq") or "")
        if qq:
            counter[qq] = counter.get(qq, 0) + 1
    return max(counter, key=counter.get) if counter else ""


def apply_reflection(data: dict, bundle: dict = None, *,
                     do_archive: bool = True, do_journal: bool = True) -> dict:
    """
    把 Agent 返回的 {journal, impression, archive} 落到本地。

    do_archive / do_journal 用来**拆开两种职责**：
      · 复盘（2 天一次）：只要 journal + impression（人格成长，不能降频）
      · 归档（30 天一次）：只要 archive（把过时记忆标掉，搬给图书馆）
    以前两件事绑在一次调用里，导致「归档想降频」就必然「人格也停滞」。
    """
    bundle = bundle or {}
    if not isinstance(data, dict):
        data = {}
    # 有些实现会包一层 {data: {...}} / {result: {...}}
    for key in ("data", "result", "reflection"):
        inner = data.get(key)
        if isinstance(inner, dict) and any(
            k in inner for k in ("journal", "impression", "archive")
        ):
            data = inner
            break
    result = {"journal": "", "impression": "", "archived": 0}

    # 算力机失败时回传的是 {"error": ...}，绝不能当成日记写进 journals/
    if data.get("error") and not data.get("journal"):
        logger.warning(f"[REFLECT] 算力机回传错误，不写日记: {data.get('error')}")
        result["error"] = str(data.get("error"))
        return result

    _j = data.get("journal") or ""
    if isinstance(_j, list):
        _j = "\n".join(str(x) for x in _j)
    journal = str(_j).strip() if do_journal else ""

    _i = data.get("impression") or ""
    if not do_journal:
        _i = ""
    if isinstance(_i, list):
        _i = "".join(str(x) for x in _i)
    impression = str(_i).strip()

    if journal:
        f = write_journal(journal, window=bundle.get("window"), impression=impression)
        result["journal"] = str(f)
        logger.info(f"[REFLECT] 📓 日记已写入 {f}")

    if impression:
        target = _pick_impression_target(bundle)
        if target:
            profiles.set_long_term_impression(target, impression)
            result["impression"] = impression
            logger.info(f"[REFLECT] 🎯 核心印象已写回档案 {target}：{impression}")

    # 👇 归档是独立职责（30 天一次），复盘任务里不碰它
    if not do_archive:
        return result

    # ① 显式 id（兼容老格式，但要防呆：id 必须真的存在）
    archive = data.get("archive") or []
    if isinstance(archive, list):
        for mid in archive:
            try:
                memory_v2.mark_outdated(str(mid))
                result["archived"] += 1
            except Exception as e:
                logger.debug(f"[REFLECT] 归档 {mid} 失败: {e}")

    # ② 关键词（推荐方式）：qwen 看不到完整 id，让它给原文关键词更安全
    keywords = data.get("archive_keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    if isinstance(keywords, list):
        for kw in keywords:
            kw = str(kw or "").strip()
            if not kw:
                continue
            hits = _find_by_keyword(kw)
            if len(hits) == 1:
                try:
                    memory_v2.mark_outdated(hits[0])
                    result["archived"] += 1
                    logger.info(f"[REFLECT] 🗂️ 关键词「{kw}」→ 归档 {hits[0]}")
                except Exception as e:
                    logger.debug(f"[REFLECT] 归档 {hits[0]} 失败: {e}")
            elif len(hits) > 1:
                # 命中多条就跳过：宁可少归档，也绝不误删
                logger.warning(f"[REFLECT] ⚠️ 关键词「{kw}」命中 {len(hits)} 条，跳过（避免误删）")
            else:
                logger.info(f"[REFLECT] 关键词「{kw}」没命中任何记忆，跳过")

    if result["archived"]:
        logger.info(f"[REFLECT] 🗂️ 共标记过时记忆 {result['archived']} 条")

    return result


# ================= 主流程 =================
async def run_reflection(since_ts: float = None, *, post: bool = True,
                         do_archive: bool = True, do_journal: bool = True) -> dict:
    """
    跑一次复盘 / 归档。

    两个职责已经拆开（见 memory_migration 的两个定时任务）：
      · 复盘（2 天）：do_archive=False，只产出 journal + impression
      · 归档（30 天）：do_journal=False，只把过时记忆标掉

    post=False 时只打包不发送（调试用，可以把包 dump 出来看）。
    """
    bundle = collect_bundle(since_ts)
    logger.info(
        f"[REFLECT] 打包完成：档案 {len(bundle.get('profiles', {}))} 份 / "
        f"记忆目录 {len(bundle.get('catalog', []))} 条 / "
        f"窗口内记忆 {len(bundle.get('window_details', []))} 条 / "
        f"心情波动 {len(bundle.get('mood', {}).get('shifts', []))} 次"
    )
    if not post:
        return {"ok": True, "bundle": bundle}

    # HTTP 优先，不通自动退回任务队列（node.py 拉取）
    data = await agent_client.reflection(bundle)
    if not data:
        # 两条通道都不通 → 只记录，绝不本地编造日记
        logger.warning("[REFLECT] ⚠️ 复盘失败：HTTP 与任务队列都没拿到结果")
        return {
            "ok": False,
            "error": "agent unreachable (http + queue both failed)",
            "bundle_size": len(json.dumps(bundle, ensure_ascii=False)),
        }

    applied = apply_reflection(data, bundle,
                               do_archive=do_archive, do_journal=do_journal)
    if do_journal:
        if not applied.get("journal") and not applied.get("impression"):
            logger.warning(f"[REFLECT] 复盘没有产出有效内容: {applied.get('error')}")
            return {"ok": False, "error": applied.get("error") or "empty reflection"}
    else:
        # 归档任务：没有 journal/impression 是正常的，只要没报错就算跑完
        if applied.get("error"):
            return {"ok": False, "error": applied.get("error")}
        if not applied.get("archived"):
            logger.info("[REFLECT] 归档任务：本轮没有需要标过时的记忆")
    return {"ok": True, **applied}
