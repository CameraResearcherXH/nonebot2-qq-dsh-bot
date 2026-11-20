# services/persona.py
"""
人格卡加载。

设计原则（重要）：
  **只认文件，不叠加任何内置内容。**

  system prompt 就是 services/Personality/<人格名>.txt 的原文，
  一个字符都不加、不改、不套壳。

  为什么：之前 `_WHALE_SHELL`（外貌+铁律）和 PERSONALITY_MAP 里的 7 条内置风格
  会和完整角色卡一起进 prompt，模型收到两套互相冲突的人设描述，
  注意力被摊薄、语气来回跳 —— 就是所谓的「人格分裂」。

  现在：
    · 默认人格 = 小鲸鱼proplus.txt（9141 字完整卡）原文
    · 只有显式 /persona 切换时，才换成另一份 txt 原文
    · 文件缺失时只给一句极简兜底，绝不拼一个"半成品人设"
"""
from core import tuning
import os
import re
from collections import defaultdict
from pathlib import Path

from nonebot import logger

PERSONALITY_DIR = Path(__file__).parent / "Personality"


def _norm_name(name: str) -> str:
    """允许传 '小鲸鱼proplus' / '小鲸鱼proplus.txt' / 'Personality/小鲸鱼proplus.txt'"""
    n = str(name or "").strip().strip('"').strip("'")
    if not n:
        return ""
    n = n.replace("\\", "/").split("/")[-1]
    if n.lower().endswith(".txt"):
        n = n[:-4]
    return n.strip()


def _loose_key(name: str) -> str:
    """宽松键：去掉 π / 空格 / 符号再比。

    2026-11-18 线上事故：人格文件叫 `小鲸鱼πproplus.txt`，而
    `DEFAULT_PERSONA=小鲸鱼proplus`（没有 π）→ 精确匹配失败 →
    `get_system_prompt` 直接回落成「你是群里的一个普通群友」，
    **人格卡整张没加载**，她还是一样说话，但所有性格/底线全丢了。
    这里把两边都归一化成 `小鲸鱼proplus` 再比，文件改名也不会静默失效。
    """
    n = _norm_name(name)
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", n).lower()


# 默认人格：直接对应 services/Personality/小鲸鱼proplus.txt
DEFAULT_PERSONA = _norm_name(os.getenv("DEFAULT_PERSONA", "小鲸鱼proplus")) or "小鲸鱼proplus"

# 常用别名 → 真实人格名（/persona 小鲸鱼 也能切）
PERSONA_ALIASES = {
    "小鲸鱼": "小鲸鱼proplus",
    "小鲸鱼proplus+": "小鲸鱼proplus",
    "鲸鱼": "小鲸鱼proplus",
    "kki": "小鲸鱼proplus",
    "KKI": "小鲸鱼proplus",
}

# 文件全缺失时的极简兜底（不是人格，只是一句"别乱说话"）
_EMERGENCY_PROMPT = "你是群里的一个普通群友，正常说话就行。"

# 群聊里角色卡最多注入多少字（0 = 不限，默认不限）。见 _trim_for_group 的说明。
GROUP_TRIM_CHARS = tuning.i("PERSONA_GROUP_CHARS")

# {人格名: txt 原文}（含别名）
PERSONALITY_MAP: dict[str, str] = {}
# {人格名: 文件路径}（含别名）
PERSONA_FILES: dict[str, Path] = {}
# 真正来自文件的名称（不含别名），/persona list 只显示这些
_REAL_NAMES: list = []


# ============================================================
# 人设热更新：每天最多重新扫 2 次目录
# ============================================================
# 用户 2026-11-11：「人设每天最多重新拉两次」。
# 人设卡 9000 字，每轮都读文件纯浪费（而且 prompt 前缀一变，缓存就失效）。
# 折中：默认全走内存，但每天给 2 次重新扫盘的机会 ——
# 改完人设不用重启容器，最多等 10 分钟就能生效。
PERSONA_MAX_RELOADS_PER_DAY = tuning.i("PERSONA_MAX_RELOADS_PER_DAY")
_PERSONA_RELOAD = {"day": "", "count": 0, "last": 0.0}


def _maybe_reload() -> None:
    """每天最多重新扫 PERSONA_MAX_RELOADS_PER_DAY 次（两次之间至少隔 10 分钟）。"""
    import time as _t
    today = _t.strftime("%Y-%m-%d")
    if _PERSONA_RELOAD["day"] != today:
        _PERSONA_RELOAD.update({"day": today, "count": 0, "last": _t.time()})
        return
    if _PERSONA_RELOAD["count"] >= PERSONA_MAX_RELOADS_PER_DAY:
        return
    if _t.time() - float(_PERSONA_RELOAD["last"] or 0) < 600:
        return
    _PERSONA_RELOAD["count"] += 1
    _PERSONA_RELOAD["last"] = _t.time()
    try:
        # 复用已有的 reload_personas()（它会先 clear 再重扫，比我直接 _load_all 干净）
        n = reload_personas()
        logger.info(
            f"🎭 [Persona] 今日第 {_PERSONA_RELOAD['count']}/"
            f"{PERSONA_MAX_RELOADS_PER_DAY} 次重新加载人格文件（{n} 个，改完不用重启）"
        )
    except Exception as e:
        logger.warning(f"🎭 [Persona] 重新加载失败: {type(e).__name__}: {e}")


def _load_all():
    """扫一遍 Personality 目录，把每个 txt 的**原文**装进 PERSONALITY_MAP。"""
    if not PERSONALITY_DIR.exists() or not PERSONALITY_DIR.is_dir():
        logger.error(
            f"❌ [Persona] 找不到人设目录: {PERSONALITY_DIR}\n"
            f"   将使用极简兜底 prompt，人格会变得很淡 —— 请确认目录已部署。"
        )
        return

    files = sorted(PERSONALITY_DIR.glob("*.txt"))
    logger.info(f"🔍 [Persona] 扫描人设目录 {PERSONALITY_DIR}，发现 {len(files)} 个 txt")

    for fp in files:
        name = fp.stem
        try:
            content = fp.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.error(f"❌ [Persona] 读取 {fp.name} 失败（可能是编码问题）: {e}")
            continue
        if not content:
            logger.warning(f"⚠️ [Persona] {fp.name} 是空文件，跳过")
            continue

        # 原文入库，不做任何拼接/裁剪
        PERSONALITY_MAP[name] = content
        PERSONA_FILES[name] = fp
        _REAL_NAMES.append(name)
        logger.info(f"✅ [Persona] 载入 {name}（{len(content)} 字，原文直出）")

        # 宽松别名：`小鲸鱼πproplus` 也能用 `小鲸鱼proplus` 选到
        _loose = _loose_key(name)
        if _loose and _loose != name and _loose not in PERSONALITY_MAP:
            PERSONALITY_MAP[_loose] = content
            PERSONA_FILES[_loose] = fp
            logger.info(f"   ↳ 归一化别名: {_loose}（文件名里的符号不影响选择）")

        # 文件名彻底坏掉（全是下划线/符号，例如 ___.txt）时，用正文首行标题补个别名，
        # 否则这个角色永远选不到。文件名正常时绝不加别名。
        if re.fullmatch(r"[_\W]+", name or ""):
            first_line = content.split("\n", 1)[0]
            alias = first_line.split("·")[-1].split("/")[0].strip() if "·" in first_line else ""
            if 2 <= len(alias) <= 10 and alias not in PERSONALITY_MAP and "角色卡" not in alias:
                PERSONALITY_MAP[alias] = content
                PERSONA_FILES[alias] = fp
                logger.info(f"   ↳ 文件名已损坏，额外注册别名: {alias}")


_load_all()

# 注册别名（指向同一份原文，不复制出新内容）
for _alias, _target in PERSONA_ALIASES.items():
    if _target in PERSONALITY_MAP and _alias not in PERSONALITY_MAP:
        PERSONALITY_MAP[_alias] = PERSONALITY_MAP[_target]
        PERSONA_FILES[_alias] = PERSONA_FILES[_target]


def _resolve_default_persona() -> None:
    """默认人格名对不上文件时，按归一化名找真实文件（并改正 DEFAULT_PERSONA）。"""
    global DEFAULT_PERSONA
    if DEFAULT_PERSONA in PERSONALITY_MAP:
        return
    _loose = _loose_key(DEFAULT_PERSONA)
    for _name in _REAL_NAMES:
        if _loose_key(_name) == _loose:
            logger.warning(
                f"🎭 [Persona] 默认人格 {DEFAULT_PERSONA!r} 没有同名文件，"
                f"按归一化名匹配到 {_name!r}（文件名符号不影响使用）"
            )
            DEFAULT_PERSONA = _name
            return


_resolve_default_persona()


# 当前各 session 用的人格名（默认 DEFAULT_PERSONA）
current_personality = defaultdict(lambda: DEFAULT_PERSONA)


def list_personas() -> list:
    """真实可用的人格（只算有文件的那几个，不含别名）"""
    return sorted(set(_REAL_NAMES))


def get_system_prompt(persona: str = "") -> str:
    """
    取 System Prompt —— **文件原文，原样返回**。
    回落顺序：指定人格 → DEFAULT_PERSONA → 极简兜底
    """
    _maybe_reload()
    for candidate in (_norm_name(persona), DEFAULT_PERSONA):
        if not candidate:
            continue
        # 精确 → 宽松（π / 符号 / 空格 不影响）
        content = PERSONALITY_MAP.get(candidate) or PERSONALITY_MAP.get(_loose_key(candidate))
        if content:
            return content

    logger.error(
        f"❌ [Persona] 人格 {persona!r} 和默认人格 {DEFAULT_PERSONA!r} 都没有对应文件，"
        f"已使用极简兜底。请检查 services/Personality/{DEFAULT_PERSONA}.txt 是否存在。"
    )
    return _EMERGENCY_PROMPT


def persona_name(session_key: str = "") -> str:
    """
    当前 session 的人格名（没设过就默认人格）。

    ⚠️ `current_personality` 是 **defaultdict**，不是函数！
       写 `current_personality()` 会抛
       `TypeError: 'collections.defaultdict' object is not callable`。
       2026-11-19 就是踩了这个：群聊现场 / 审核 LLM 的 `bot_name=current_personality()`
       每次都抛，异常又被 `except` 吞掉 → 审核从来没跑过，现场一直「审：未审核」。
       要名字请用这个函数。
    """
    return str(current_personality.get(session_key, DEFAULT_PERSONA) or DEFAULT_PERSONA)


def persona_for(session_key: str) -> tuple:
    """按 session 取 (人格名, System Prompt)。llm.py 用这个。"""
    name = current_personality.get(session_key, DEFAULT_PERSONA) or DEFAULT_PERSONA
    text = get_system_prompt(name)
    return name, _trim_for_group(session_key, text)


def _trim_for_group(session_key: str, text: str) -> str:
    """
    群聊里角色卡可以只注前 N 字（`PERSONA_GROUP_CHARS`，0 = 不限，默认不限）。

    ⚠️ 默认**关着**，原因见本文件开头：这套人格的设计原则是"原文直出、不叠加"，
       随便裁剪会让人设前后不一致（模型收到半截设定比收到两套设定更糟）。
       真要开，建议 ≥3000 字，且裁完自己读一遍看看有没有把底线规则切掉。
    """
    if GROUP_TRIM_CHARS <= 0:
        return text
    if not str(session_key).startswith("group_"):
        return text
    if len(text) <= GROUP_TRIM_CHARS:
        return text
    logger.info(
        f"🎭 [Persona] 群聊精简注入：{len(text)} → {GROUP_TRIM_CHARS} 字"
        f"（PERSONA_GROUP_CHARS，0 可关掉）"
    )
    return text[:GROUP_TRIM_CHARS] + (
        "\n\n（后面还有外貌、习惯、经历等细节，你心里有数，不用主动复述。）"
    )


def reload_personas() -> int:
    """热重载（改完 txt 不用重启）：返回重新载入的人格数。"""
    PERSONALITY_MAP.clear()
    PERSONA_FILES.clear()
    _REAL_NAMES.clear()
    _load_all()
    for _alias, _target in PERSONA_ALIASES.items():
        if _target in PERSONALITY_MAP and _alias not in PERSONALITY_MAP:
            PERSONALITY_MAP[_alias] = PERSONALITY_MAP[_target]
            PERSONA_FILES[_alias] = PERSONA_FILES[_target]
    logger.info(f"🔄 [Persona] 热重载完成，当前 {len(list_personas())} 个人格: {list_personas()}")
    return len(list_personas())


# 启动自检：把最终会用到的 prompt 长度打出来，一眼看出是不是完整卡
if DEFAULT_PERSONA in PERSONALITY_MAP:
    logger.info(
        f"🎭 [Persona] 默认人格: {DEFAULT_PERSONA}"
        f"（{len(PERSONALITY_MAP[DEFAULT_PERSONA])} 字，来自 {PERSONA_FILES[DEFAULT_PERSONA].name}）"
    )
else:
    logger.error(
        f"🎭 [Persona] 默认人格 {DEFAULT_PERSONA} 没有对应文件！"
        f"可用人格: {list_personas() or '（空）'}"
    )
