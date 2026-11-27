# services/skill_switch.py
"""非核心能力总开关（「/技能」宏）。

分工：
- `chat_switch` 管「理不理人」（安静/破冰）。
- `skill_switch` 管「会不会用某个能力」（搜索/画图/表情/主动插话…）。

三层：
1. 环境变量 `SKILLS_MASTER=off` → 所有非核心能力关闭（急停）。
2. 命令 `/技能` → 按组 / 单个开关，持久化到 `data/skills.json`。
3. 工具执行前、主动插话/主动私聊前调用 `is_enabled()`。

核心（不可关）：回复判定、对话生成、记忆读写、心情/好感、pipeline 记账、基础发送。
"""
import json
import os
import threading
import time
from pathlib import Path

STATE_FILE = Path(os.getenv("SKILLS_FILE", "data/skills.json"))
MASTER = os.getenv("SKILLS_MASTER", "on").strip().lower()

# 核心能力：任何情况下都不允许关（关了机器人就不成立了）
# 2026-11-20 补全：把「工具级核心」也列进来，否则它们既不在核心、又不在任何组，
# /技能 关 全部 也关不掉，状态面板里更是看不到（等于隐身）。
CORE_SKILLS = frozenset({
    # 能力级
    "chat.reply", "memory.read", "memory.write", "mood", "profile",
    "pipeline.observe", "send.basic",
    # 工具级（对话/记忆/发送/安静/档案，关了她就不成立）
    "recall", "remember", "stay_silent", "send", "chat_switch",
    "profile_card", "profile_update", "api_usage", "set_call",
})

# 分组：/技能 关 搜索  → 关掉整组
# ⚠️ 名字必须和 register_tool 里的 "name" 完全一致，否则关了没效果。
SKILL_GROUPS = {
    "search": {"web_search", "bilibili_search", "pixiv_search", "trace_moe",
               "hash_decode", "web_fetch"},
    "draw": {"draw_image", "draw_from_pixiv", "draw", "comfy_workflow"},
    "media": {"sing", "video_understand", "tts", "song", "cover", "rvc",
              "audio_split"},
    "sticker": {"sticker_save", "sticker_send"},
    "social": {"napcat_call", "napcat_help", "poke", "napcat_tools"},
    "self_tools": {"self_tool_file", "self_tool_run", "self_tool_write"},
    # CODE_AGENT=harness 时的改代码桥（和 self_tools 二选一）
    "code": {"harness_ask", "harness_status", "harness_reset"},
    # ocr_image：图里字看不清时用 OpenCV 增强再读（2026-11-27）
    "vision": {"vision_source", "ocr_image"},
    "lore": {"lore_add"},
    "proactive": {"proactive.interject", "proactive.private"},
    "third_party": {
        "girls_2d", "petpet", "rollpig", "steam_judge", "daily_luck",
        "genshin_bind", "genshin_panel", "genshin_team", "genshin_unbind",
        "char", "calc_gsm8k_reward",
    },
}

GROUP_LABEL = {
    "search": "搜索", "draw": "画图", "media": "语音/音乐",
    "sticker": "表情", "social": "社交/群管理", "self_tools": "自检工具",
    "code": "改代码", "vision": "视觉", "lore": "世界书", "proactive": "主动开口",
    "third_party": "第三方插件",
}

_LOCK = threading.Lock()
_CACHE = {"mtime": -1.0, "data": {}}


def _load() -> dict:
    try:
        m = STATE_FILE.stat().st_mtime
    except Exception:
        m = -1.0
    if _CACHE["mtime"] == m:
        return _CACHE["data"]
    data = {}
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("off_groups", [])
    data.setdefault("off_skills", [])
    _CACHE["mtime"], _CACHE["data"] = m, data
    return data


def _save(data: dict) -> None:
    with _LOCK:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
        try:
            _CACHE["mtime"] = STATE_FILE.stat().st_mtime
        except Exception:
            _CACHE["mtime"] = -1.0
        _CACHE["data"] = data


def master_off() -> bool:
    return MASTER in ("off", "0", "false", "no", "关")


def _group_of(name: str) -> str:
    n = str(name or "")
    for g, members in SKILL_GROUPS.items():
        if n in members:
            return g
    # 名称前缀推断（新加的工具只要命名规范，就自动落进对应组）
    if n.startswith(("web_", "bili", "pixiv", "trace_", "search")):
        return "search"
    if n.startswith(("draw", "comfy")):
        return "draw"
    if n in ("tts", "song", "cover", "rvc", "audio_split", "sing") \
            or n.startswith(("tts_", "song_", "video_")):
        return "media"
    if n.startswith("sticker"):
        return "sticker"
    if n in ("poke", "napcat_tools") or n.startswith("napcat"):
        return "social"
    if n.startswith("self_tool"):
        return "self_tools"
    if n.startswith("harness"):
        return "code"
    if n.startswith("vision"):
        return "vision"
    if n.startswith("lore"):
        return "lore"
    if n.startswith("proactive"):
        return "proactive"
    if n.startswith(("genshin", "girls", "petpet", "rollpig", "steam",
                     "daily_luck", "char", "calc_gsm8k")):
        return "third_party"
    return ""


def is_enabled(name: str, gid: str = "") -> bool:
    n = str(name or "").strip()
    if not n or n in CORE_SKILLS:
        return True
    if master_off():
        return False
    data = _load()
    if n in (data.get("off_skills") or []):
        return False
    g = _group_of(n)
    if g and g in (data.get("off_groups") or []):
        return False
    return True


def set_enabled(target: str, value: bool, gid: str = "") -> dict:
    """target: 组名（search/draw/...）、技能名、或 全部/all。返回新状态。"""
    t = str(target or "").strip().lower()
    data = _load()
    off_g = set(data.get("off_groups") or [])
    off_s = set(data.get("off_skills") or [])

    if t in ("全部", "all", "*"):
        if value:
            off_g.clear()
            off_s.clear()
        else:
            off_g = set(SKILL_GROUPS.keys())
            off_s = set()
    elif t in SKILL_GROUPS:
        (off_g.discard if value else off_g.add)(t)
    else:
        # 允许中文组名
        _rev = {v: k for k, v in GROUP_LABEL.items()}
        if t in _rev:
            g = _rev[t]
            (off_g.discard if value else off_g.add)(g)
        else:
            if t in CORE_SKILLS:
                return {"ok": False, "why": "核心能力不能关"}
            (off_s.discard if value else off_s.add)(t)

    data["off_groups"] = sorted(off_g)
    data["off_skills"] = sorted(off_s)
    data["updated_at"] = time.time()
    _save(data)
    return {"ok": True, "off_groups": data["off_groups"],
            "off_skills": data["off_skills"]}


def gate(cmd, skill: str, label: str = "") -> None:
    """给 nonebot 命令挂「能力被关掉就不执行」的前置 handler。

    必须放在原有 `@cmd.handle()` **之前**：先注册先执行，
    被关时 `finish()` 会中断这个 matcher，后面的正式 handler 不会跑。

    用法：
        skill_switch.gate(draw_cmd, "draw_image", "画图")
    """
    name = label or GROUP_LABEL.get(_group_of(skill), skill)

    @cmd.handle()
    async def _skill_gate():      # noqa: ANN202
        try:
            ok = is_enabled(skill)
        except Exception:
            ok = True
        if not ok:
            await cmd.finish("「%s」这个能力被主人关掉了（/技能 开 %s 可恢复）。"
                             % (name, name))


def snapshot() -> dict:
    data = _load()
    off_g = set(data.get("off_groups") or [])
    off_s = set(data.get("off_skills") or [])
    return {
        "master": "off" if master_off() else "on",
        "groups": {GROUP_LABEL.get(g, g): ("关" if g in off_g else "开")
                   for g in SKILL_GROUPS},
        "off_skills": sorted(off_s),
    }


def status_text() -> str:
    s = snapshot()
    lines = ["▍能力开关（核心功能不受影响）",
             "· 总闸：%s（SKILLS_MASTER）" % s["master"]]
    lines.append("· 分组：" + "  ".join(
        "%s=%s" % (k, v) for k, v in s["groups"].items()))
    if s["off_skills"]:
        lines.append("· 单独关闭：" + "、".join(s["off_skills"]))
    lines.append("用法：/技能 关 全部 ｜ /技能 开 搜索,画图 ｜ /技能 状态")
    return "\n".join(lines)
