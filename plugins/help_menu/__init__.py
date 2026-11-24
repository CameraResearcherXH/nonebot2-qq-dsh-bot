# plugins/help_menu/__init__.py
"""
/功能菜单 —— 查看所有功能和使用方式。

设计要点：
  · 独立插件，不碰 chat（chat 是最脆的文件，能不加就不加）
  · 内容**动态生成**：人格列表、表情库存、心情值都从实际数据读，
    不会出现"菜单写了但功能没有"或"库存显示 0 其实有 318 张"
  · 用「· 命令 — 说明」格式，不靠空格对齐（QQ 里中文双宽，对齐必然歪）
  · 分 3 条消息发，避免一条超长被截断
"""
from core import tuning
import os

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.params import CommandArg

from . import topics as T

menu_cmd = on_command(
    "功能菜单",
    aliases={"菜单", "功能"},
    priority=3,
    block=True,
)

# /帮助 = 全部指令一次性列出（不分组、不遗漏）
help_cmd = on_command(
    "帮助",
    aliases={"help", "指令", "全部指令", "命令"},
    priority=3,
    block=True,
)

# ============================================================
# 全部指令清单（一条消息发完）
# 新增命令时**必须**加到这里 —— 自检会扫 on_command 比对，漏了会报错
# ============================================================
COMMANDS = [
    ("📖 帮助 / 菜单", [
        ("/帮助", "全部指令一览（就是这条）"),
        ("/功能菜单", "分组菜单（3 条）"),
        ("/功能菜单 详情", "主题索引"),
        ("/功能菜单 算力机", "某个功能的完整说明"),
    ]),
    ("🛠 改代码 / Harness（CODE_AGENT=harness，仅主人）", [
        ("/h 或 /harness", "看当前 harness 会话 / 后端 / 工作目录"),
        ("/h 新", "开一个新会话"),
        ("/h <需求>", "交给本机 DeepSeek Harness 改代码"),
        ("/改代码 <需求>", "同上"),
    ]),
    ("🎭 人格与称呼", [
        ("/persona", "看当前人格"),
        ("/persona list", "列出全部人格"),
        ("/persona 名字", "切换人格（小鲸鱼/鲸鱼/kki）"),
        ("/叫我 名字", "她以后这么叫你"),
        ("/叫你 名字", "给她起名（= /改名，以后喊这名字就等于@她）"),
        ("/叫他 @某人 名字", "设定她对别人的称呼"),
        ("/本群叫他 @某人 名字", "只在这个群里这么叫（同一个人不同群不同身份）"),
        ("/删称呼 [@某人]", "删掉记错的称呼（她叫他 / 他叫她 一起清）"),
        ("/白月光样本 [@某人]", "本人上传说话样本（一段话 / 多行 Q&A / 引用消息）"),
        ("/白月光样本 查看", "看自动语料条数 / 自定义样本审批状态"),
        ("/白月光特批 @某人", "仅主人：通过后回他就用这套风格"),
        ("/白月光驳回 @某人", "仅主人：驳回"),
    ]),
    ("🧠 记忆与状态", [
        ("/心情", "看她的心情值"),
        ("/记忆统计", "长期记忆库统计"),
        ("/记忆体检", "查重复/长期没想起的（仅主人）"),
        ("/测函数 标识|语音|预算|包|图书馆|工具", "自检新链路（仅主人）"),
        ("/测函数 用量", "API 余额 + 本机记账的调用量（仅主人）"),
        ("/测函数 模型", "看每个角色当前用哪家模型（主聊天/判读/审核/识图，仅主人）"),
        ("/上传文件", "3 分钟内把你发的 QQ 文件下载到 data/（仅主人）"),
        ("/安静 [分钟]", "让这个群/私聊强制安静（连 @ 也不回，默认 60 分钟，仅主人）"),
        ("/说话", "解除安静"),
        ("/测语音", "诊断为什么没用本地 indextts（仅主人）"),
        ("/看模板 [流水线|全] [@人/QQ]", "打印最近一次发给 DeepSeek 的完整输入；「流水线」只看动态取数块，可带 @人/QQ 按那个人现跑一遍（仅主人）"),
        ("/档案 [@人]", "发一份档案图片（加「文字」则发文字版）"),
        ("/词条", "发词条卡片图（加「文字」发文字版）｜/词条 生成｜/词条 删 编号（仅主人）"),
        ("/表现", "她对各个群/各个人说话的样子（仅主人）｜/表现 清 全部忘掉"),
        ("/记住 事情", "手动塞一条记忆"),
        ("/复盘", "立刻做一次两天复盘（仅主人）"),
        ("/归档记忆", "把近 30 天记忆归档（仅主人）"),
    ]),
    ("🎨 表情包", [
        ("/导入表情", "把 data/memes 素材入库"),
        ("/导入表情 精细", "入库后让 DeepSeek 重分兜底项"),
        ("/导入表情 data/memes", "指定目录导入"),
        ("/导入表情 复制 data/uploads/xxx", "复制进 data/stickers（大图缩到 512，uploads 被清也不怕）"),
        ("/发表情 [标签]", "立刻让她发一张（验证发送链路，仅主人）"),
        ("/表情统计", "库存 / 健康度 / 标签分布"),
        ("/表情健康", "只查健康度"),
        ("/清理表情", "清掉文件已丢失的坏条目"),
        ("/识表情", "给表情打标签（别名：分标签/表情识别）"),
        ("/收表情", "收藏表情（别名：存表情/收藏表情）"),
        ("收 / 收 1 3 / 不收", "处理暂存的表情（ask 模式）"),
    ]),
    ("🎵 语音与音乐", [
        ("/写歌 主题", "写词并唱出来"),
        ("/写rap 主题", "同上（说唱）"),
        ("/翻唱 歌名", "下载→分离→变声→混音，人声+伴奏"),
        ("/翻唱 网易云链接", "直接甩歌曲链接/歌曲 id 也行"),
        ("/清唱 歌名", "同上但不要伴奏，只要人声"),
        ("/测歌 歌名", "只跑判定链（性别/音域/变调），不翻唱"),
        ("/测歌 上传", "本地没有的歌：发音频文件，切人声回传（加 ab 跑 crepe 对比）"),
        ("/翻唱 off / on", "暂停 / 恢复接单（算力机忙时用，主人专属）"),
        ("/取消", "取消待上传的音频"),
    ]),
    ("🖼️ 画图", [
        ("/画图 描述", "算力机 SDXL 文生图（中文描述即可）"),
        ("/画图 芙宁娜…", "带角色名 → 自动 P站搜图 + 重绘保留面部"),
        ("/画图 off / on", "暂停 / 恢复接单（主人/管理员）"),
        ("/以图绘图 线稿 描述 + 图", "草图线稿上色（ControlNet Lineart）"),
        ("/以图绘图 姿势 描述 + 图", "参考图锁姿势（ControlNet OpenPose）"),
        ("/以图绘图 景深 描述 + 图", "参考图锁景深/透视（ControlNet Depth）"),
        ("/以图绘图 画风 描述 + 图", "参考图画风迁移（IP-Adapter）"),
        ("/以图绘图 重绘 描述 + 图", "直接重绘参考图（img2img）"),
    ]),
    ("🎮 群聊玩法", [
        ("来点美少女 / 来点二次元", "她调工具随机发一张二次元图"),
        ("今日运势 / 今日人品", "她调工具抽今天的运势签"),
        ("摸 @某人 / rua", "头像表情包（她也能主动摸熟人）"),
        ("找 XX 的图", "她调 pixiv_search 搜图"),
        ("今日小猪 / 昨日小猪 / 随机小猪", "猪猪抽卡（群友发指令玩）"),
        ("烤群友 @目标 / 加急生火 @目标", "烤猪玩法"),
        ("我的猪圈 / 小猪图鉴 / 本周小猪", "猪圈收藏与成长"),
        ("/steam分析 主页链接", "Steam 游戏库 AI 毒舌锐评"),
        ("/绑定 9位UID", "绑定原神 UID（之后面板不用带 UID）"),
        ("/面板 [uid] [角色] / /队伍伤害 [uid]", "原神角色/队伍面板"),
        ("bt [排序] [关键词] / bt on|off", "磁力搜索 + 总开关（主人/管理员）"),
    ]),
    ("🔍 工具", [
        ("/搜索 问题", "联网搜索"),
        ("/tavily 问题", "同上"),
    ]),
    ("🧰 QQ 操作（她自己动手）", [
        ("/工具表", "她手上有哪些 NapCat API（分类）（仅主人）"),
        ("/工具表 群管理", "看某一类的工具名和参数（仅主人）"),
        ("/工具表 set_group_ban", "看单个工具的完整参数（仅主人）"),
        ("/批准 编号", "主人点头，执行她刚才没权限做的事（仅主人）"),
        ("/拒绝 编号", "拒绝那个操作（仅主人）"),
    ]),
    ("🎛 开关与用量（仅主人）", [
        ("/技能", "看所有非核心能力的开关状态"),
        ("/技能 关 搜索", "按组/单个关掉能力（搜索/画图/表情/语音/主动开口…）"),
        ("/技能 开 搜索,画图", "重新打开"),
        ("/用量", "今日 token 用量：按用途排行 + 横向/扇形图，最贵/最省各 5 项"),
        ("/用量 7", "最近 7 天"),
        ("/调参", "所有阈值当前生效值（改 .env 同名键即可覆盖）"),
        ("/调参 关键词", "只查相关参数，例如 /调参 心情"),
    ]),
    ("🩺 体检", [
        ("/自检", "一键跑全部集成（快检，几秒）（仅主人）"),
        ("/自检 全", "连 DeepSeek/本地Qwen/算力机一起实测（仅主人）"),
        ("/全量测试", "端到端串通所有功能（模型/记忆/QQ/媒体/数据，仅主人）"),
        ("/全量测试 全", "深检：额外测本地Qwen/GLM/算力机图书馆（仅主人）"),
        ("/全量测试 记忆", "只跑某一组（环境/模型/对话/QQ/媒体/扩展/数据）"),
        ("/全量测试 报告", "重发上一次的完整报告（仅主人）"),
        ("/接入自检", "测画图/记忆/表情包/三方是否都接进 llm（仅主人）"),
        ("/测函数 工具", "看模型现在挂着哪些工具（仅主人）"),
    ]),
]

PASSIVE = (
    "🤖 不用命令的（她自动做）\n"
    "· 群里 @ 她 → 必回，之后还会关注你的后续 2 条（120 秒内）\n"
    "· 私聊 → 必回\n"
    "· 发图 → 自动识图，可能记进记忆\n"
    "· 发表情 → 可能被收藏\n"
    "· 她自己会发表情 / 发语音 / 打 MEMO 记事情"
)


def render_commands() -> list:
    """把 COMMANDS 渲染成几条消息（太长就拆）。"""
    head = "📋 全部指令\n"
    chunks, cur = [], head
    for group, items in COMMANDS:
        block = f"\n{group}\n" + "\n".join(f"{c} — {d}" for c, d in items) + "\n"
        if len(cur) + len(block) > 1200:
            chunks.append(cur.rstrip())
            cur = ""
        cur += block
    if cur.strip():
        chunks.append(cur.rstrip())
    chunks.append(PASSIVE)
    return chunks

GROUP_MAX_HANZI = tuning.i("GROUP_MAX_HANZI")
PRIVATE_MAX_HANZI = tuning.i("PRIVATE_MAX_HANZI")
GROUP_NEED_AT = os.getenv("GROUP_NEED_AT", "true").lower() == "true"
USE_LOCAL_TTS = os.getenv("USE_LOCAL_TTS", "true").lower() == "true"


def _safe(fn, default):
    """任何一项取不到都不能让菜单崩掉。"""
    try:
        return fn()
    except Exception:
        return default


# ============================================================
# 各区块
# ============================================================
def _persona_block() -> str:
    def _cur():
        from services.persona import DEFAULT_PERSONA, PERSONALITY_MAP, list_personas
        return DEFAULT_PERSONA, len(PERSONALITY_MAP.get(DEFAULT_PERSONA, "")), list_personas()

    cur, size, names = _safe(_cur, ("?", 0, []))
    return (
        "🎭 人格\n"
        "· /persona — 看当前人格\n"
        "· /persona list — 列出全部人格\n"
        "· /persona 名字 — 切换人格\n"
        f"当前：{cur}（{size} 字）\n"
        f"可选：{'、'.join(names) if names else '（无）'}"
    )


def _call_block() -> str:
    return (
        "👤 称呼 / 说话风格\n"
        "· /叫我 名字 — 她以后这么叫你\n"
        "· /叫你 名字（= /改名）— 给她起名（以后喊这名字 = @她）\n"
        "· /叫他 @某人 名字 — 设定她对别人的称呼\n"
        "· /本群叫他 @某人 名字 — 只在这个群里这么叫（同一个人不同群不同身份）\n"
        "· /删称呼 [@某人] — 删掉记错的称呼（两个方向一起清）\n"
        "· /白月光样本 [@某人] — 本人上传说话样本（等主人特批）\n"
        "· /白月光样本 查看 — 看自动语料条数 / 审批状态\n"
        "· /白月光特批 @某人 — 仅主人：通过后回他就用这套风格\n"
        "· /白月光驳回 @某人 — 仅主人：驳回"
    )


def _memory_block() -> str:
    def _mood():
        from services import mood
        m = mood.get_mood()
        # 有她自己写的原话就显示原话（心情不是固定枚举，2026-11-19）
        label = str(m.get("label") or "").strip()
        if label:
            return label, ""
        v = m["value"]
        tag = ("语气挺松快" if v >= 60 else "语气还行" if v >= 20 else
               "语气一般" if v > -20 else "语气偏冷" if v > -60 else "语气很冷")
        return "（她还没写）", tag

    val, tag = _safe(_mood, ("?", ""))
    return (
        "🧠 记忆与状态\n"
        "· /心情 — 看她自己写的心情\n"
        "· /记忆统计 — 长期记忆库统计\n"
        "· /记忆体检 — 查重复/长期没想起的（仅主人）\n"
        "· /测函数 包|预算|标识|语音|图书馆|工具 — 自检新链路\n"
        "· /测函数 用量 — API 余额 + 本机记账的调用量（仅主人）\n"
        "· /测函数 模型 — 每个角色当前用哪家/哪个模型（仅主人）\n"
        "· /上传文件 — 3 分钟内把你发的 QQ 文件下载到 data/（仅主人）\n"
        "· /安静 [分钟] — 强制安静（连 @ 也不回，默认 60 分钟，仅主人）；/说话 解除\n"
        "· /测语音 — 诊断为什么没用本地 indextts\n"
        "· /看模板 [流水线|全] [@人/QQ] — 打印最近一次发给 DeepSeek 的完整输入；「流水线」只看动态取数块，可带人现跑\n"
        "· /档案 [@人] — 发一份档案图片（加「文字」则发文字版）\n"
        "· /词条 — 发词条卡片图（/词条 文字 发文字版；/词条 生成 从档案灌入，仅主人）\n"
        "· /表现 — 她对各个群/各个人说话的样子（仅主人；/表现 清 全部忘掉）\n"
        "· /记住 事情 — 手动塞一条记忆给她\n"
        "· /复盘 — 立刻做一次两天复盘（仅主人）\n"
        "· /归档记忆 — 把近 30 天记忆归档（仅主人）\n"
        f"当前心情：{val}" + (f"（{tag}）" if tag else "")
    )


def _sticker_block() -> str:
    def _st():
        from services.sticker_library import TAG_LIBRARY, count_importable, sticker_lib
        s = sticker_lib.stats()
        return (s.get("total", 0), s.get("limit", 1000),
                s.get("premium", 0), len(TAG_LIBRARY), count_importable())

    total, limit, premium, ntag, imp_info = _safe(_st, (0, 1000, 0, 0, (0, [])))
    n_imp, where = imp_info if isinstance(imp_info, tuple) else (0, [])
    hint = f"\n📦 未入库素材 {n_imp} 张（{'、'.join(where)}）→ 发 /导入表情" if n_imp else ""
    return (
        "🎨 表情包\n"
        "· /导入表情 — 把 data/memes 素材入库\n"
        "· /导入表情 复制 <目录> — 按子文件夹当标签整批导入（大图自动缩到 512）\n"
        "· /发表情 [标签] — 立刻让她发一张（测试用）\n"
        "· /表情统计 — 库存 / 健康度 / 标签分布\n"
        "· /识表情 — 给表情打标签（别名：分标签/表情识别）\n"
        "· /收表情 — 收藏表情（别名：存表情/收藏表情）\n"
        "· 收 — 收下暂存的表情（ask 模式）\n"
        "· 收 1 3 — 只收第 1、3 张\n"
        "· 不收 — 全部丢掉\n"
        f"当前：{total}/{limit} 张，精品 {premium} 张，{ntag} 个标签" + hint
    )


def _music_block() -> str:
    tts = "算力机 index-tts（离线自动降级云端 CosyVoice）" if USE_LOCAL_TTS else "云端 CosyVoice"
    return (
        "🎵 语音与音乐\n"
        "· /写歌 主题 — 写词并唱出来\n"
        "· /写rap 主题 — 同上（说唱）\n"
        "· /翻唱 歌名 — 下载→分离→变声→混音（人声+伴奏）\n"
        "· /翻唱 网易云链接 — 直接甩链接或歌曲 id 也行\n"
        "· /清唱 歌名 — 同上但不要伴奏，只要人声\n"
        "· /测歌 歌名 — 只跑判定链（性别/音域/变调），不翻唱\n"
        "· /测歌 上传 — 本地没有的歌：发音频文件，切人声回传（加 ab 跑 crepe 对比）\n"
        "· /翻唱 off | on — 暂停 / 恢复接单（算力机忙时用，主人专属）\n"
        "· /取消 — 取消待上传的音频\n"
        f"语音引擎：{tts}"
    )


def _tool_block() -> str:
    def _nap():
        from services.napcat_tools import IN_GROUP, stats
        return stats(), IN_GROUP

    info, in_group = _safe(_nap, ("（未加载）", False))
    return (
        "🔍 工具\n"
        "· /搜索 问题 — 联网搜索\n"
        "· /tavily 问题 — 同上\n"
        "· /工具表 — 她能自己调的 QQ 操作（仅主人）\n"
        "· /批准 编号 — 给危险操作点头（仅主人）\n"
        "· /拒绝 编号 — 不让她做那个操作（仅主人）\n"
        "· /自检 — 一键体检今晚所有集成（仅主人）\n"
        "· /自检 全 — 连模型/算力机一起实测（仅主人）\n"
        "· /全量测试 — 端到端串通所有功能，出带修复建议的报告（仅主人）\n"
        "· /全量测试 报告 — 重发上一次报告（仅主人）\n"
        "· /帮助 — 全部指令一次列出\n"
        "· /功能菜单 表情 — 只看某一类\n"
        "· /功能菜单 — 看完整菜单\n"
        f"NapCat 工具：{info}｜群聊挂载：{'是' if in_group else '否'}"
    )


def _draw_block() -> str:
    return (
        "🖼️ 画图\n"
        "· /画图 描述 — 算力机 SDXL 文生图\n"
        "· /画图 芙宁娜… — 带角色名自动 P站搜图+重绘\n"
        "· /画图 off|on — 暂停/恢复接单（主人/管理员）\n"
        "· /以图绘图 重绘|画风|姿势|线稿|景深 描述 + 图\n"
        "  重绘=改参考图，画风=风格迁移，姿势/景深=锁构图"
    )


def _fun_block() -> str:
    return (
        "🎮 群聊玩法\n"
        "· 来点美少女 / 今日运势 — 她调工具直接出\n"
        "· 摸 @某人 — 头像表情包（她能主动摸熟人）\n"
        "· 找 XX 的图 — P站搜图\n"
        "· 今日小猪 / 烤群友 @目标 / 我的猪圈 — 猪猪\n"
        "· /steam分析 链接 / 面板 uid — Steam锐评/原神面板\n"
        "· bt 关键词 / bt on|off — 磁力搜索+总开关\n"
        "· /接入自检 — 一键测这些有没有接进 llm（仅主人）\n"
        "详情：/功能菜单 玩法"
    )


def _passive_block() -> str:
    group_rule = ("群里 @ 她才回（不 @ 时她可能自己插话）" if GROUP_NEED_AT
                  else "群里每条都回（GROUP_NEED_AT=false，会比较吵）")
    return (
        "🤖 她自己的功能（不用命令）\n"
        f"· 回复：{group_rule}\n"
        "· 私聊：必回\n"
        f"· 字数：群聊 {GROUP_MAX_HANZI} 字内直接发、超过走合并转发；"
        f"私聊同一条规矩（{PRIVATE_MAX_HANZI} 字）\n"
        "· 发图：自动识图，觉得有意思会自己记下来\n"
        "· 发表情：按心情用 <sticker:标签> 自己挑表情发\n"
        "· 记忆：觉得值得记的会自己打 [[MEMO]] 存进长期记忆\n"
        "· 语音：想用语音说时会自己发语音气泡\n"
        "· 0–6 点她睡觉，@ 也不回"
    )


BLOCKS = {
    "人格": _persona_block,
    "称呼": _call_block,
    "记忆": _memory_block,
    "状态": _memory_block,
    "心情": _memory_block,
    "表情": _sticker_block,
    "画图": _draw_block,
    "玩法": _fun_block,
    "三方": _fun_block,
    "猪猪": _fun_block,
    "音乐": _music_block,
    "语音": _music_block,
    "工具": _tool_block,
    "被动": _passive_block,
}


# ============================================================
# 入口
# ============================================================
@help_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    """全部指令一次发完。"""
    for chunk in render_commands():
        await help_cmd.send(chunk)
    await help_cmd.finish("💡 想看某个功能的完整说明：/功能菜单 详情")


@menu_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    arg = str(args).strip()

    if arg:
        # ① /功能菜单 详情 —— 主题索引
        if arg in ("详情", "详解", "索引", "index", "list"):
            await menu_cmd.finish(T.INDEX)

        # ② /功能菜单 算力机 —— 该主题的完整说明（优先，内容更详细）
        detail = T.get(arg)
        if detail:
            await menu_cmd.finish(detail)

        # ③ 兼容旧的短查询（称呼 / 工具 / 被动 这类没有详解的）
        for key, fn in BLOCKS.items():
            if key in arg:
                await menu_cmd.finish(fn() + "\n\n💡 想看完整说明：/功能菜单 详情")

        await menu_cmd.finish("没有这个主题哦。\n\n" + T.INDEX)

    parts = [
        "📖 功能菜单（1/4）\n\n" + _persona_block() + "\n\n" + _call_block(),
        "📖 功能菜单（2/4）\n\n" + _memory_block() + "\n\n" + _sticker_block(),
        "📖 功能菜单（3/4）\n\n" + _draw_block() + "\n\n" + _fun_block(),
        "📖 功能菜单（4/4）\n\n" + _music_block() + "\n\n" + _tool_block()
        + "\n\n" + _passive_block(),
    ]
    for p in parts:
        await menu_cmd.send(p)
    await menu_cmd.finish("💡 全部指令一次列出：/帮助　｜　某项详解：/功能菜单 详情")
