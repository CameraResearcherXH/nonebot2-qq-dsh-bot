# plugins/help_menu/topics.py
"""
功能详解。每个主题返回一段可以直接发到 QQ 的文字。

原则：
  · 只说**真的实现了**的东西（写之前都对着代码核过）
  · 说明「怎么用 + 背后怎么跑的 + 坏了怎么查」，不只是命令列表
  · 关键参数尽量从 env 现读，别写死
"""
import os


def _env(k, d):
    return os.getenv(k, d)


# ============================================================
# 人格
# ============================================================
def persona() -> str:
    try:
        from services.persona import (
            DEFAULT_PERSONA, PERSONALITY_MAP, PERSONA_FILES, list_personas,
        )
        cur = DEFAULT_PERSONA
        size = len(PERSONALITY_MAP.get(cur, ""))
        names = list_personas()
        src = PERSONA_FILES[cur].name if cur in PERSONA_FILES else "（缺失）"
    except Exception:
        cur, size, names, src = "?", 0, [], "?"
    return (
        "🎭 人格 —— 详解\n"
        "\n"
        "【机制】\n"
        "System Prompt = services/Personality/<名字>.txt 的原文，\n"
        "一个字符都不加、不套壳、不拼接。\n"
        f"当前：{cur}（{size} 字，来自 {src}）\n"
        "\n"
        "【命令】\n"
        "· /persona — 看当前人格和字数\n"
        "· /persona list — 列出全部人格\n"
        "· /persona 名字 — 切换（别名：小鲸鱼 / 鲸鱼 / kki）\n"
        "\n"
        "【可用】\n"
        f"{'、'.join(names) if names else '（无）'}\n"
        "\n"
        "【改人格】\n"
        "· 直接改 txt 内容 → 调 persona.reload_personas() 热重载\n"
        "· 加新人格：往目录里丢一个 <名字>.txt 即可，重启生效\n"
        f"· 换默认：.env 里 DEFAULT_PERSONA=<名字>\n"
        "\n"
        "⚠️ 别在 txt 里再写「你叫 XXX」之类的身份设定，\n"
        "   和卡片里的设定打架会让人格分裂。"
    )


# ============================================================
# 记忆
# ============================================================
def memory() -> str:
    return (
        "🧠 记忆 —— 详解\n"
        "\n"
        "【三层结构】\n"
        "1. 短期（SQLite data/bot.db）—— 只做原始流水存档\n"
        "2. 长期（data/memory/）—— BM25 + 遗忘曲线 + 情绪刻痕\n"
        "3. 相处印象（data/profiles/*_bond.json）—— 带情绪的刻痕\n"
        "\n"
        "【她怎么记】\n"
        "她在回复末尾自己写：\n"
        "  [[MEMO|emotion:happy|text:他夸我可爱我很开心]]\n"
        "这一行用户看不到，会被拆出来：\n"
        "  · 写进长期记忆（带 emotion_hit 标记）\n"
        "  · 写进相处印象（带 emotion）\n"
        "  · 心情按 emotion 估个方向（happy +10 / angry -16 …）\n"
        "一条回复最多 2 条 MEMO。\n"
        "\n"
        "【心情是她自己写的一句话】\n"
        "没有候选词、没有固定档位 —— 她想写什么写什么：\n"
        "  [[MOOD|被夸得有点飘，但还装作没事]]\n"
        "这一行也不发给用户，只存成她此刻的心情（默认 12 小时内有效），\n"
        "下一轮 prompt 里直接用她的原话，而不是「你现在心情不错」这种模板。\n"
        "MEMO 里的 emotion 也可以直接写自由描述，会一并当心情原话存下来。\n"
        "\n"
        "【语气值（不是心情本身）】\n"
        "另有 -100~100 的数值，12 小时半衰期回落，只影响\n"
        "主动插话概率 / 自动补表情概率 / 回复延迟。\n"
        "流水记在 data/mood_history.jsonl（带她写的原话）。\n"
        "\n"
        "【两天一复盘】\n"
        "每 2 天 20:00 自动打包（档案+印象+心情+记忆+角色卡+日记）\n"
        "→ 发给算力机 qwen2 → 写 data/journals/日期.md\n"
        "→ 提炼一句「核心印象」写回档案，下次聊天带上。\n"
        "\n"
        "【按需查（她自己拉，2026-11-16）】\n"
        "· 每轮自动注入 5 条记忆目录（编号+时间+心情+摘要），不全量塞\n"
        "· 想看全文 → 写 [[MEMORY|关键词]] 拉目录，可翻页、可展开某条\n"
        "· 按时间查：[[MEMORY|前天上午]] / [[MEMORY|昨天]] / [[MEMORY|3天前]]\n"
        "  —— 时间词直接转成日期段，翻那段时间的记忆\n"
        "\n"
        "【命令】\n"
        "· /心情 — 她自己写的心情（+ 语气值）\n"
        "· /记忆统计 — 记忆库统计\n"
        "· /记忆体检 — 查重复/长期没想起的（仅主人）\n"
        "· /档案 [@人] — 档案+相处印象+情绪刻痕\n"
        "· /记住 事情 — 手动塞一条\n"
        "· /复盘 — 立刻做一次（仅主人）"
    )


# ============================================================
# 表情
# ============================================================
def sticker() -> str:
    def _st():
        from services.sticker_library import STICKER_MAX, TAG_LIBRARY, sticker_lib
        s = sticker_lib.stats()
        return s, STICKER_MAX, TAG_LIBRARY
    try:
        s, cap, tags = _st()
        head = (f"当前：{s['total']} 张，能发 {s['usable']} 张，"
                f"上限 {cap}，精品 {s['premium']}")
    except Exception:
        head, tags = "当前：读取失败", []

    # 👇 关键提示：素材在那儿但没入库
    try:
        from services.sticker_library import count_importable
        n_imp, where = count_importable()
        if n_imp:
            head += (f"\n\n📦 还有 {n_imp} 张未入库素材（{'、'.join(where)}）\n"
                     f"   这些只是文件，不入库等于没有 → 发 /导入表情")
    except Exception:
        pass

    return (
        "🎨 表情包 —— 详解\n"
        "\n"
        "【分工】\n"
        "· 收表情 + 分类 → 视觉模型（看画面给标签）\n"
        "· 留哪些 → DeepSeek 判精品（打分 + 淘汰名单）\n"
        "· 发哪些 → DeepSeek 按心情自己挑\n"
        "\n"
        "【怎么发】\n"
        "她在回复里写一行 <sticker:标签>，系统替她挑一张发出去。\n"
        "标签：" + " / ".join(tags[:8]) + "\n"
        "      " + " / ".join(tags[8:]) + "\n"
        "\n"
        "【怎么收】\n"
        "· 你发表情 → 视觉识图（GLM 优先，qwen 兜底，60 字画面描述）→ 打标签\n"
        "· 该标签已满 120 张 / 和已有表情很像 → 先问 DeepSeek 值不值得收\n"
        "· STICKER_COLLECT_MODE=ask 时先暂存，她按心情自己决定收不收：\n"
        "  [[STICKER|列表]] 看暂存 / [[STICKER|收|编号|心情标签]] 收下 / [[STICKER|不要]] 删掉\n"
        "  你也可以发「收」「收 1 3」「不收」替她处理\n"
        "\n"
        "【容量】\n"
        f"上限 {_env('STICKER_MAX', '1000')} 张。超了就让 DeepSeek 判淘汰名单，\n"
        "LLM 不可用时按「用过次数+新鲜度+精品分」兜底硬删。\n"
        "\n"
        "【命令】\n"
        "· /导入表情 — 把 data/memes 里的素材入库（几秒）\n"
        "· /导入表情 精细 — 入库后再让 DeepSeek 重分兜底项\n"
        "· /导入表情 复制 <目录> — 按子文件夹当标签整批导入（疑惑→思考、问候→打招呼；大图缩到 512；从 data/uploads 导入会自动用这个模式）\n"
        "· /发表情 [标签] — 立刻让她发一张，验证发送链路通不通\n"
        "· /表情统计 — 库存 / 健康度 / 标签分布\n"
        "· /表情健康 — 只查健康度\n"
        "· /清理表情 — 清掉文件已丢失的坏条目\n"
        "· 收 / 收 1 3 / 不收 — 处理暂存\n"
        "\n"
        "【她为什么不主动发表情】\n"
        "· 她发不发由 DeepSeek 自己决定（<sticker:标签> 协议），不是随机硬塞\n"
        "· 先确认「能发出去 N 张」不是 0 —— 库存里全是坏路径时她会静默失败\n"
        "· 想立刻验证：发 /发表情 搞怪，链路通就说明是模型自己不想发\n"
        "· 另外每条回复后还有 30% 概率按关键词补一张（心情越好越爱发）\n"
        "\n"
        f"{head}"
    )


# ============================================================
# 识图
# ============================================================
def vision() -> str:
    mode = _env("VISION_REPLY_MODE", "llm").lower()
    return (
        "👁️ 识图 —— 详解\n"
        "\n"
        "【什么时候识图】\n"
        "· 群里别人发图：需要群最近 5 分钟有活动，且该群 5 分钟内没识过图\n"
        "· @她 + 图：无冷却，每次必识\n"
        "· 私聊发图：5 分钟冷却\n"
        "· 0–6 点她不工作\n"
        "\n"
        "【链路（LLM 主导）】\n"
        "取图（链接→get_image→本地文件，三级兜底）\n"
        "→ 压缩（最长边 1024，转 JPEG）\n"
        "→ GLM / qwen 识图（优先传图片链接，省掉上传大图）\n"
        "→ trace.moe 以图搜番（动漫截图 → 番名/集数/角色候选）\n"
        "→ **把全部证据交给 DeepSeek**，由她决定：接话 / stay_silent / sticker_save 收表情\n"
        f"（VISION_REPLY_MODE={mode}：llm=她决策；mentioned=只在叫她时回；always=每张都回）\n"
        "GIF：全部帧拼成一张网格图给视觉模型；均分三帧给 trace.moe\n"
        "\n"
        "【模型】\n"
        f"VISION_MODEL={_env('VISION_MODEL', 'Qwen3.5-Flash')}\n"
        "模型名自动降级 + 端点自动降级（workspace → 百炼公网），\n"
        "成功的记进 data/vision_model.txt，下次直接用。\n"
        "\n"
        "【坏了怎么查】\n"
        "日志搜 [VISION]，每次尝试都会打：模型 @ 端点 载体 → HTTP xxx（耗时）\n"
        "· HTTP 404 model_not_found → 模型名不对，跑 scripts/probe_models.py\n"
        "· 一直没反应 → 图太大超时，确认装了 Pillow\n"
        "· DASHSCOPE_API_KEY 为空 → 检查 .env"
    )


# ============================================================
# 语音
# ============================================================
def voice() -> str:
    local = _env("USE_LOCAL_TTS", "true").lower() == "true"
    return (
        "🎙️ 语音 —— 详解\n"
        "\n"
        "【三级降级】\n"
        "① 算力机 index-tts（只有算力机在线才试）\n"
        "② 阿里云 CosyVoice（离线/本地失败自动降）\n"
        "③ 纯文字（两边都挂才用）\n"
        f"当前 USE_LOCAL_TTS={local}\n"
        "\n"
        "【算力机在线判断】\n"
        "看 STATUS['last_heartbeat']，30 秒内有心跳算在线。\n"
        "离线时 0 秒直接进 CosyVoice，不会白等。\n"
        "\n"
        "【两种触发】\n"
        "· 你说「语音」「念出来」→ 整段转语音\n"
        "· 她自己写 <voice>要念的话</voice> → 那一句转语音\n"
        "\n"
        "【Rap / 写歌】\n"
        "本地 index-tts 出声 → RVC 变声成大肥鱼音色（pitch=0）\n"
        "→ 失败就上传原音，不会整个失败"
    )


# ============================================================
# 翻唱
# ============================================================
def cover() -> str:
    return (
        "🎤 翻唱 —— 详解\n"
        "\n"
        "【两种模式】\n"
        "· /翻唱 晴天 → 人声 + 伴奏，混成成品\n"
        "· /清唱 晴天 → 只要变声后的人声，不带伴奏\n"
        "\n"
        "【三种点法】\n"
        "· /翻唱 晴天                     按歌名搜\n"
        "· /翻唱 https://music.163.com/song?id=186016\n"
        "· /翻唱 186016                   直接给歌曲 id\n"
        "链接支持 music.163.com / y.music.163.com / 163cn.tv 短链，\n"
        "分享文案里夹着的链接也能认。歌单/专辑链接会被忽略（id 不是歌曲的）。\n"
        "\n"
        "【完整链路（必须算力机在线）】\n"
        "1. 服务器：解析链接或网易云搜歌 → 下载 → 生成 split 任务\n"
        "2. 算力机：拉任务 → ffmpeg 转 WAV → Demucs 分离人声+伴奏\n"
        "3. 算力机：回传双文件（song_cover 依赖 output['vocal']/['inst']）\n"
        "4. 服务器：听人声定音高（见下）→ 下发 rvc_mix / rvc 任务\n"
        "5. 算力机：RVC 变声 → **混音也在这里做** → 回传 mp3\n"
        "6. 服务器：直接发语音（不再做 ffmpeg）\n"
        "\n"
        "【为什么混音放在算力机】\n"
        "· 少一次 70MB 往返（人声+伴奏本来就在算力机本地）\n"
        "· 混音前会用 volumedetect 打出两轨平均音量，\n"
        "  伴奏要是 -50dB 以下日志里直接警告（服务器那版看不出来）\n"
        "· amix 必须 normalize=0 + duration=longest，\n"
        "  否则默认按轨数砍半、按人声长度截断 → 听起来「只有人声没伴奏」\n"
        "\n"
        "【性别判断三级降级】\n"
        "① DeepSeek 常识判断（快，覆盖绝大多数知名歌手）\n"
        "② Tavily 联网搜索歌手资料 + DeepSeek 归纳（冷门/虚拟歌手/只有翻唱版）\n"
        "③ 都判不出就留空，等分离出人声后直接听（见下）\n"
        "\n"
        "【音高自适应（关键）】\n"
        "分离出人声后实测基频中位数，用 12·log2(220/实测) 算半音数：\n"
        "· 男声约 120Hz → +10 半音\n"
        "· 女声约 210Hz → +1 半音\n"
        "Qwen-Audio（百炼，DASHSCOPE_API_KEY）会再听一遍做交叉验证，\n"
        "两者冲突时以实测 F0 为准（它给的「低/中/高」太主观）。\n"
        "都测不出才退回预设：女声 +12 / 男声 -12。\n"
        "基准点可调：RVC_TARGET_F0=220\n"
        "\n"
        "【耗时】\n"
        "下载+分离 1~3 分钟，音高分析 1~3 秒，RVC 30 秒~2 分钟，混音 10 秒。\n"
        "\n"
        "【坏了怎么查】\n"
        "· 算力机 60 秒没接任务 → 提示「算力机离线」\n"
        "· 算力机控制台：看 [song]/[vocal]/[inst] 下载进度\n"
        "· 报「分离返回的文件丢失」→ 算力机回传的是单文件（应该双文件）\n"
        "· 报「算力机处理失败：{...}」→ 括号里就是算力机回的错误原因\n"
        "· 歌做好了发不出去（retcode=1200 Timeout）→ 之前是 60MB 的 wav，\n"
        "  现在混音直接出 128k mp3，正常 3~5MB。还失败会降级发文件。\n"
        "· 日志看 [GENDER]（判性别来源）/ [PITCH]（自适应结果）/ [F0]（实测基频）\n"
        "\n"
        "【注意】\n"
        "翻唱不依赖云端 TTS，纯本地算力。算力机不在就完全做不了。"
    )


# ============================================================
# 写歌
# ============================================================
def song() -> str:
    return (
        "🎵 写歌 —— 详解\n"
        "\n"
        "【命令】/写歌 主题  或  /写rap 主题\n"
        "\n"
        "【链路】\n"
        "1. DeepSeek 按她的性格写歌词（含结构标记）\n"
        "2. 算力机 index-tts 唱出来 → RVC 变声\n"
        "3. 算力机不在 → 降级云端 CosyVoice 唱\n"
        "4. 两边都挂 → 只发歌词文字\n"
        "\n"
        "【和 /翻唱 的区别】\n"
        "· /写歌 = 原创歌词 + 她的声音\n"
        "· /翻唱 = 现成的歌 + 分离 + 变声\n"
        "\n"
        "【注意】\n"
        "写歌时**不注入** MEMO / 表情 / 字数协议，\n"
        "因为歌词需要长度和结构，套那些约束会写不出来。"
    )


# ============================================================
# 算力机
# ============================================================
def compute() -> str:
    return (
        "🖥️ 算力机（node.py）—— 详解\n"
        "\n"
        "【作用】\n"
        "跑重活：Demucs 分离、RVC 变声、IndexTTS、两天复盘。\n"
        "它**主动轮询**服务器，所以服务器不需要能反向访问它。\n"
        "\n"
        "【启动】\n"
        "算力机上：python node.py（依赖 requests）\n"
        f"轮询：心跳 {_env('HEARTBEAT_INTERVAL', '15')}s / 拉任务 {_env('POLL_INTERVAL', '5')}s\n"
        "\n"
        "【支持 5 种任务】\n"
        "· split — 翻唱分离，回传人声+伴奏双文件\n"
        "· audio_separation — 手动人声分离，单文件（可转 mp3）\n"
        "· rvc — 变声（pitch/f0_method/index_rate/rms_mix_rate/protect）\n"
        "· tts — IndexTTS2 合成 + RVC 变声\n"
        "· reflection — 两天复盘，本地 qwen2 写日记回传 JSON\n"
        "\n"
        "【算力机上的依赖服务】\n"
        "· 8005 — Demucs 分离\n"
        "· 7860 — IndexTTS2（TTS_URL）\n"
        "· 11434 — Ollama qwen2（复盘）\n"
        "· RVC 目录 + kikiv2.pth / kikiv2.index\n"
        "\n"
        "【排查】\n"
        "· 服务器：GET /api/tasks/status?secret=... 看心跳年龄和任务列表\n"
        "· 算力机：控制台看 💓心跳 / 🔔收到任务 / ❌失败原因\n"
        "· 任务卡住不动 → 算力机没启动，或 API_SECRET 不一致\n"
        "\n"
        "【离线影响】\n"
        "翻唱 → 做不了\n"
        "写歌/Rap/语音 → 降级云端 CosyVoice\n"
        "复盘 → 任务排队，等它上线后补跑\n"
        "识图/聊天/表情 → 完全不受影响（都在服务器本地）"
    )


# ============================================================
# 搜索
# ============================================================
def search() -> str:
    return (
        "🔍 联网搜索 —— 详解\n"
        "\n"
        "【命令】/搜索 问题  或  /tavily 问题\n"
        "\n"
        "【链路】\n"
        "Tavily API 搜索 → 结果压成摘要 → 喂 DeepSeek → 她用自己语气回答\n"
        "\n"
        "【自动触发】\n"
        "聊天时她会先判断「这句需不需要查实时信息」，\n"
        "需要才去搜，不是每句都搜。\n"
        "\n"
        "【依赖】\n"
        ".env 里 TAVILY_API_KEY，没配就只能手动 /搜索 且会报错"
    )


# ============================================================
# 回复频率
# ============================================================
def frequency() -> str:
    need_at = _env("GROUP_NEED_AT", "true").lower() == "true"
    prob = _env("RANDOM_CHAT_PROB", "0.08")
    interval = _env("RANDOM_CHAT_INTERVAL", "900")
    min_msgs = _env("RANDOM_CHAT_MIN_MSGS", "5")
    reply_gap = _env("REPLY_INTERVAL", "20")
    burst_n = _env("BURST_MSG_COUNT", "4")
    burst_w = _env("BURST_WINDOW_SEC", "30")
    return (
        "⏱️ 回复频率 —— 详解\n"
        "\n"
        "【必回】\n"
        f"· 群聊：{'@ 她才回（不 @ 时她可能自己插话）' if need_at else '每条都回（GROUP_NEED_AT=false，很吵）'}\n"
        "· 私聊：必回\n"
        "· 0–6 点：全部静默，@ 也不回\n"
        "· 回复长度：20 汉字以内直接发，超过自动做「合并转发」卡片（不截断）；\n"
        "  测试群单独放宽到 45 汉字（GROUP_CHAT_OVERRIDES 可配）\n"
        f"· 同一会话两次回复至少隔 {reply_gap} 秒\n"
        "\n"
        "【高刷清队列】\n"
        f"群/私聊在 {burst_w} 秒内来了 {burst_n} 条 → 清掉排队等回复的旧消息，\n"
        "只留「@ 我 / 喊我名字」的和最新那条（每个群、每个私聊各算各的，\n"
        "她自己的消息不算数；indextts 语音、翻唱这类任务不受影响）。\n"
        "\n"
        "【主动插话】\n"
        "每 15 秒扫一次，要过 7 道闸门：\n"
        "1. 群里最近 300 秒有消息\n"
        f"2. 距上次插话 ≥ {interval} 秒\n"
        f"3. 缓存消息 ≥ {min_msgs} 条\n"
        "4. 不在 0–6 点\n"
        "5. 这一小时插话 < 6 次\n"
        "6. 话题相关度 ≥ 0.25（兴趣词：白米饭/游戏/猫/狗/睡觉/熬夜/天气/表情包/奶茶）\n"
        f"7. 群里没在刷屏（{burst_w} 秒内 < {burst_n} 条）\n"
        "\n"
        "【单群覆盖】（测试群可以单独放开）\n"
        f"GROUP_INTERJECT_OVERRIDES={_env('GROUP_INTERJECT_OVERRIDES', '') or '（未配）'}\n"
        "格式：群号:冷却秒:插话概率:每小时上限:最少几条消息\n"
        "· 被覆盖的群**不再看关键词相关度**，由模型自己判断这条属于：\n"
        "  A 在找我（要回答） / B 我可以接一句 / C 不关我事（不回复）\n"
        "· 例 100000021:60:0.9:60:2 = 每分钟最多接一次\n"
        "\n"
        f"概率 = min(0.8, {prob} × 心情系数 × (1 + 4×相关度))\n"
        "· 命中 1 个兴趣词 + 心情 0 → 约 40%\n"
        "· 心情 -80 → 降到 8% 左右\n"
        "实际上限：每 15 分钟最多 1 次，每小时最多 6 次。\n"
        "\n"
        "【表情】\n"
        "· 她主动 <sticker:标签> → 100% 发\n"
        "· 自动补一张：30% × 心情系数，且回复里要含「哈哈/笑死/无语/委屈」\n"
        "\n"
        "【识图】\n"
        "被动（别人发图）每群 5 分钟 1 次；@她发图无冷却。\n"
        "\n"
        "【字数】\n"
        f"群聊 {_env('GROUP_MAX_HANZI', '20')} 字以内直接发，超过走「合并转发」卡片（不截断）；"
        f"私聊同一条规矩（{_env('PRIVATE_MAX_HANZI', '20')} 字）。"
        "写多长她自己判断，测试群可单独放宽。"
    )


# ============================================================
# 部署与运维
# ============================================================
def deploy() -> str:
    return (
        "🚀 部署与运维 —— 详解\n"
        "\n"
        "【改代码后】\n"
        "· 只改了 .py → docker compose restart bot（bind mount 已挂）\n"
        "· 改了 requirements.txt / Dockerfile → docker compose build && up -d\n"
        "· 改了 .env → docker compose up -d --force-recreate bot（restart 不重读 env_file）\n"
        "\n"
        "【Harness 改代码（CODE_AGENT=harness，仅主人）】\n"
        "· /h 或 /harness — 看当前 harness 会话 / 后端 / 工作目录\n"
        "· /h 新 — 开新会话；/h <需求>、/改代码 <需求> — 交给 DSH 改代码\n"
        "· self_tool 会自动关闭；配置见 docs/harness-bridge.md\n"
        "\n"
        "【校验有没有传坏】\n"
        "本地生成过 deploy_manifest.txt（md5 清单）：\n"
        "  cd ~/qq-grok-bot && md5sum -c deploy_manifest.txt\n"
        "全是 OK 才算传对。磁盘满时 scp 会静默截断文件！\n"
        "\n"
        "【磁盘 / 日志】\n"
        "· df -h → 看还剩多少\n"
        "· docker system df → 看谁占的\n"
        "· docker builder prune -af → 清构建缓存（经常几个 G）\n"
        "· 日志已配轮转：每容器 3×20MB，不会无限涨\n"
        "\n"
        "【自检】\n"
        "docker exec -it qq-grok-bot python scripts/selftest_all.py\n"
        "· 离线 248 项，全绿才算健康\n"
        "· 「打桩的包」里不该出现 nonebot / aiohttp / Pillow\n"
        "\n"
        "【模型探测】\n"
        "python scripts/probe_models.py --chat\n"
        "列出 workspace 真实可用的模型名\n"
        "\n"
        "【表情导入】\n"
        "python scripts/import_stickers.py --src data/memes --tag-from llm --in-place\n"
        "python scripts/import_stickers.py --judge     # 打精品分\n"
        "python scripts/import_stickers.py --prune     # 手动触发容量维护\n"
        "python scripts/import_stickers.py --clean-broken  # 清坏数据"
    )


# ============================================================
# 画图
# ============================================================
def draw() -> str:
    return (
        "🖼️ 画图 —— 详解\n"
        "\n"
        "【命令】\n"
        "· /画图 描述 — 文生图（中文描述，自动翻 danbooru tags）\n"
        "· /画图 on | off — 暂停 / 恢复接单（主人/管理员）\n"
        "· /以图绘图 重绘|画风|姿势|线稿|景深 描述 + 图 — 以图绘图\n"
        "\n"
        "【角色名自动搜图（2026-11-16）】\n"
        "/画图 带具体角色名（芙宁娜/纳西妲/初音ミク…）→ 自动先 P 站搜图，\n"
        "再「重绘」保留角色面部，不是纯文生图瞎画；泛称（白裙女孩）才走文生图。\n"
        "\n"
        "【链路】\n"
        "服务器翻 tags → tool_dispatcher 任务队列 type=draw → 算力机 ComfyUI :8188\n"
        "→ 出图回传 PNG。算力机冷启动第一张最慢（要加载模型）。\n"
        "\n"
        "【LoRA 常驻栈（node.env 配，算力机侧）】\n"
        f"SD_LORAS = {_env('SD_LORAS', '（未配）')}\n"
        "\n"
        "【坏了怎么查】\n"
        "· 算力机心跳 caps 里没有 draw → ComfyUI 没起（跑 node.py --check）\n"
        "· /画图 一直不出图 → 算力机日志看 prompt_id / execution_error\n"
        "· 报 LoRA 文件找不到 → node.py 会拉 /object_info 自动映射，还找不到就是真缺文件\n"
    )


# ============================================================
# 群聊玩法（三方功能）
# ============================================================
def fun() -> str:
    return (
        "🎮 群聊玩法 —— 详解\n"
        "\n"
        "【她能直接调的（她自己调工具，不用你发指令）】\n"
        "· 美少女图 — 说「来点美少女」→ 她调 girls_2d 发一张\n"
        "· 今日运势 — 说「测运势」→ 她调 daily_luck 抽签（每人每天一次，重复抽返回同一份）\n"
        "· P站找图 — 说「找 XX 的图」→ 她调 pixiv_search\n"
        "· 头像表情包 — 说「摸 @某人」→ 她调 petpet 摸一下\n"
        "\n"
        "【要参数 / 要你本人操作的】\n"
        "· /steam分析 Steam主页链接 — Steam 游戏库 AI 毒舌锐评\n"
        "· /绑定 9位UID — 绑定原神 UID（之后 /面板 刻晴 不用再带 UID；/解绑 解除）\n"
        "· /面板 [uid] [角色] / /队伍伤害 [uid] — 原神面板（没装 htmlrender 时给文字版）\n"
        "\n"
        "【猪猪（今日小猪）】\n"
        "· 今日小猪 / 昨日小猪 / 明日小猪 / 随机小猪 / 找猪 关键词\n"
        "· 我的猪圈 / 小猪图鉴 / 本周小猪\n"
        "· 烤群友 @目标 / 今日烤猪 / 加急生火 @目标 / 烤箱续火\n"
        "· 猪圈日报 / 小猪日报 开启|关闭|状态\n"
        "\n"
        "【bt 磁力搜索】\n"
        "· bt [排序] [关键词] — 磁力搜索（排序：相关度/大小/热门/时间）\n"
        "· btp [磁力链接] — 预览\n"
        "· bt on | off — 总开关（主人/管理员，状态落盘重启不丢）\n"
        "\n"
        "【一键自检】\n"
        "· /接入自检 — 测画图/记忆/表情包/三方是否都接进 llm（仅主人）\n"
    )


# ============================================================
# 安静开关 / 沉默权
# ============================================================
def silence() -> str:
    return (
        "🤫 安静开关（CHAT_OFF / CHAT_ON）—— 详解\n"
        "\n"
        "【她自己的开关】\n"
        "· chat_switch(action=\"off\", target=\"群号或QQ\", minutes=30, reason=\"心情\")\n"
        "  静音后：消息照收、记忆照写、心情照变，只是不回。\n"
        "· chat_switch(action=\"on\", target=\"…\")  想说话了随时开，不用等谁批准。\n"
        "· chat_switch(action=\"list\")  看自己能去哪些群、能跟谁私聊、谁是主人、现在谁被静音。\n"
        "\n"
        "【怎么保证「真不回」】\n"
        "不是靠提示词祈祷：安静时 API 层强制 tool_choice=stay_silent，\n"
        "模型只能选「不回复」这一个函数。\n"
        "\n"
        "【但她还听得见】\n"
        "上下文、群聊现场、长期记忆、心情都照常更新 —— 回头能接上话，\n"
        "不会因为安静过就断片。\n"
        "\n"
        "【参数】\n"
        f"CHAT_OFF_LLM={_env('CHAT_OFF_LLM', 'true')}"
        "（false=安静时干脆不调模型，省钱）\n"
        f"CHAT_OFF_ANSWER_WHEN_CALLED={_env('CHAT_OFF_ANSWER_WHEN_CALLED', 'true')}"
        "（有人 @ 她时给不给她选择权）\n"
        f"CHAT_OFF_MAX_HOURS={_env('CHAT_OFF_MAX_HOURS', '72')}（单次最长，防她忘了开回来）\n"
        "状态存 data/chat_switch.json，重启不丢。"
    )


def switch() -> str:
    return (
        "🎛 开关 / 用量 / 调参 —— 详解\n"
        "\n"
        "【/技能 —— 关掉不需要的能力】\n"
        "· /技能                看所有能力的开关状态\n"
        "· /技能 关 全部        核心以外的全部关掉（急停）\n"
        "· /技能 关 搜索        关一组（search/draw/media/sticker/social/proactive/third_party）\n"
        "· /技能 开 搜索,画图   重新打开\n"
        "· 核心不可关：回复判定、对话生成、记忆读写、心情/好感、记账。\n"
        "· 状态存 data/skills.json；SKILLS_MASTER=off 可一键全关（改 .env 重启）。\n"
        "\n"
        "【/用量 —— token 花在哪】\n"
        "· /用量       今日：按用途（chat/tool_loop/judge/audit/vision…）的 token、占比、延迟\n"
        "· /用量 7     最近 7 天\n"
        "· 默认附两张图：横向排行 + 扇形占比；只想看数字就 /用量 文字。\n"
        "· 报表末尾给「最贵 Top5 / 最省 Bottom5」，用来判断该关哪个旁路。\n"
        "· 预算：LLM_DAILY_BUDGET 超了会在日志告警（0 = 不告警）。\n"
        "\n"
        "【/调参 —— 所有阈值在哪】\n"
        "· /调参           列全部阈值的当前值（和默认值对比，被 .env 改过的会标 ←改）\n"
        "· /调参 心情      只查相关项\n"
        "· 唯一来源是 core/tuning.py；改值写 .env 同名键后重启即可。\n"
        "· 完整表格见 docs/tuning.md。\n"
    )


# ============================================================
# 索引
# ============================================================
TOPICS = {
    "人格": persona,
    "开关": switch,
    "技能": switch,
    "用量": switch,
    "token": switch,
    "调参": switch,
    "参数": switch,
    "记忆": memory,
    "表情": sticker,
    "识图": vision,
    "视觉": vision,
    "语音": voice,
    "翻唱": cover,
    "写歌": song,
    "rap": song,
    "算力机": compute,
    "计算": compute,
    "搜索": search,
    "安静": silence,
    "沉默": silence,
    "chat_off": silence,
    "频率": frequency,
    "部署": deploy,
    "运维": deploy,
    "日志": deploy,
    "画图": draw,
    "画画": draw,
    "以图绘图": draw,
    "玩法": fun,
    "三方": fun,
    "猪猪": fun,
    "小猪": fun,
    "运势": fun,
    "bt": fun,
    "磁力": fun,
    "自检": fun,
}

INDEX = (
    "📚 功能详解 —— 主题索引\n"
    "\n"
    "用法：/功能菜单 <主题>\n"
    "\n"
    "🎭 人格 — 角色卡机制、切换、改法\n"
    "🧠 记忆 — MEMO 协议、长期记忆、两天复盘、时间搜索\n"
    "🎨 表情 — 收集、分类、策展、自主发送\n"
    "👁️ 识图 — 触发条件、冷却、模型降级\n"
    "🎙️ 语音 — 三级降级、Rap、变声\n"
    "🎤 翻唱 — 完整链路、耗时、排错\n"
    "🎵 写歌 — 原创歌词 + 她的声音\n"
    "🖥️ 算力机 — node.py 部署、任务、排查\n"
    "🖼️ 画图 — 文生图、以图绘图、角色名自动搜图\n"
    "🎮 群聊玩法 — 猪猪、运势、美少女图、头像表情包、Steam、原神面板、bt\n"
    "🔍 搜索 — 联网搜索\n"
    "🤫 安静 — CHAT_OFF 沉默权（只读不回，但照样听得见）\n"
    "🎛 开关 — /技能 能力开关、/用量 token 归因、/调参 阈值表\n"
    "⏱️ 频率 — 回复/插话/表情/识图的完整参数\n"
    "🚀 部署 — docker、校验、日志、自检\n"
    "\n"
    "例：/功能菜单 算力机　|　/功能菜单 玩法"
)


def get(topic: str) -> str:
    """按关键词取详解，找不到返回空串。"""
    t = str(topic or "").strip().lower()
    if not t:
        return ""
    for key, fn in TOPICS.items():
        if key.lower() in t:
            return fn()
    return ""
