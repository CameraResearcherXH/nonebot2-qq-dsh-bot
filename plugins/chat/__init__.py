# plugins/chat/__init__.py
from core import tuning
import os, re, asyncio, time, random, base64, json
from collections import OrderedDict, defaultdict, deque
from pathlib import Path
from nonebot import on_message, on_command, on_notice, logger, get_driver, get_bot
from nonebot.adapters.onebot.v11 import (
    Bot, MessageEvent, GroupMessageEvent, PrivateMessageEvent, Message, MessageSegment,
    NoticeEvent,
)
from nonebot.rule import Rule
from nonebot.params import CommandArg

from services import llm as llm_mod
from services import group_tuning
from services.llm import call_llm, register_tool
from services.persona import (current_personality, persona_name,
                             PERSONALITY_MAP, DEFAULT_PERSONA, list_personas)
from services.text import limit_hanzi, hanzi_count, strip_html
from services.tts import AliyunVoiceTTS
# ⚠️ 故意**不**导入 services.reply.reply_with_multimodal：
#    它内部有 30% 概率直接调云端 CosyVoice，会绕过 indextts。
#    所有语音出口统一走本文件的 speak_voice（indextts → 云端 → 文字兜底）。

from services.sticker_receiver import (
    try_collect_sticker, list_pending, commit_pending,
    commit_pending_by_index, drop_pending, describe_pending,
)
from services.sticker_sender import send_sticker_by_tag
from services.sticker_library import sticker_lib

from services.aura_logic import (
    parse_reply, has_mention, expand_mentions, wants_voice, bubble_delay,
    strip_protocol_residue, clean_voice_text,
)

from services.access import is_owner, display_name, nickname_variants
from services import (profiles, memory_v2, mood, group_roster, referee,
                       dialogue_log, group_scene, scene_auditor, conv_state)
from services import view_frequency as vf_mod
from services import (attention, expression, chat_switch, proactive, mood_judge,
                      profile_miner, quiet_advisor, skill_switch, segments,
                      poke_guard)
from services import identity as identity_mod
from services.prompt_builder import build_prompt

# ================= 环境 =================
# 每次回复前默认往前带几条。用户 2026-11-12：
# 「每次回复前往前拉 10 条消息，重点关注艾特它或者叫它的人这些历史消息」
# 10 → 16（2026-11-13）：群里 10 句短话就能把一条 200 字的长方案挤出窗口，
# 她被 @ 时完全不知道用户说过什么。条数放宽 + 下面的「长消息补偿」双保险。
MAX_GROUP_RECENT = tuning.i("MAX_GROUP_RECENT")
# ---- 长消息补偿：从 dialogue_log 把被挤出窗口的长消息捞回来 ----
CTX_LONG_MSG_CHARS = tuning.i("CTX_LONG_MSG_CHARS")      # 多少字算长消息
CTX_LONG_MSG_KEEP = tuning.i("CTX_LONG_MSG_KEEP")          # 最多补几条
CTX_LONG_MSG_CHARS_MAX = tuning.i("CTX_LONG_MSG_CHARS_MAX")  # 单条最多给多少字
GROUP_NEED_AT = os.getenv("GROUP_NEED_AT", "true").lower() == "true"
# 表情包「收不收」由谁定：llm=她看完图自己决定（sticker_save）；auto=老行为，见到就收
STICKER_DECIDE = (os.getenv("STICKER_DECIDE", "llm") or "llm").strip().lower()
ALLOWED_GROUPS = set(g.strip() for g in os.getenv("ALLOWED_GROUPS", "").split(",") if g.strip())
ALLOWED_PRIVATE = set(p.strip() for p in os.getenv("ALLOWED_PRIVATE", "").split(",") if p.strip())

REPLY_INTERVAL = tuning.f("REPLY_INTERVAL")
# 连发合并窗口（秒）：同一用户在这段时间内连发多条消息时，合并成一条回复，
# 不要逐条弹（用户 2026-11-16 要求）。
MERGE_WINDOW = tuning.f("MERGE_WINDOW")
# ---- 防抖等待表（参考 MaiBot 的 debounce，2026-11-19）----
# 原来只有一个 MERGE_WINDOW：裸图（只有图没字）也按 0.6 秒算 →
# 图还没下载完就开始生成，模型只能回「怎么图还没加载出来」。
DEBOUNCE_SAME = tuning.f("DEBOUNCE_SAME")        # 同一个人续发
DEBOUNCE_OTHER = tuning.f("DEBOUNCE_OTHER")      # 换人说话（赶紧看）
DEBOUNCE_AT = tuning.f("DEBOUNCE_AT")            # 在叫她（越短越好）
DEBOUNCE_BARE_IMAGE = tuning.f("DEBOUNCE_BARE_IMAGE")  # 裸图等下载
DEBOUNCE_MAX = tuning.f("DEBOUNCE_MAX")          # 整批上限
RANDOM_CHAT_INTERVAL = tuning.i("RANDOM_CHAT_INTERVAL")

SLEEP_HOUR_START = tuning.i("SLEEP_HOUR_START")
SLEEP_HOUR_END = tuning.i("SLEEP_HOUR_END")
# 私聊要不要也遵守睡眠时段。默认 false —— 私聊 24 小时随时能聊。
PRIVATE_SLEEP = os.getenv("PRIVATE_SLEEP", "false").lower() in ("1", "true", "yes", "on")
# 早晚安主动问候已彻底移除（用户 2026-11-17：「还是没去掉，每天还是会讲」）。
# 之前的 GREETING_ENABLED 开关连同问候循环一起删了，不再读 env。

# ================= 回复字数 =================
# 群聊：≤20 汉字直接发文字；>20 汉字自动走「合并转发」卡片（不截断）。
#   （用户 2026-11-18 确认规定一直是这个；以前代码是硬截断，砍半句话。）
#   唯一例外是测试群（GROUP_CHAT_OVERRIDES 可单独放宽）。
# 私聊：同一条规矩，≤20 直接发，超过走合并转发。
GROUP_MAX_HANZI = tuning.i("GROUP_MAX_HANZI")
PRIVATE_MAX_HANZI = tuning.i("PRIVATE_MAX_HANZI")
# 系统长文（视频总结等）超过多少字才用合并转发（太短没必要做成卡片）
LONG_FORWARD_MIN = tuning.i("LONG_FORWARD_MIN")
RANDOM_CHAT_ENABLED = os.getenv("RANDOM_CHAT_ENABLED", "true").lower() == "true"
RANDOM_CHAT_MIN_MSGS = tuning.i("RANDOM_CHAT_MIN_MSGS")
RANDOM_CHAT_PROB = tuning.f("RANDOM_CHAT_PROB")


# ---- 单群插话频率覆盖（用户 2026-11-18：测试群要 1 分钟一次）----
# 格式：`群号:冷却秒:插话概率:每小时上限:最少几条消息`，多个群用逗号分隔，后面几项可省略。
#   例 `100000021:60:0.9:60:2`
#   = 这个群每 60 秒最多接一次、每次 90% 概率接、每小时最多 60 次、
#     群里有 2 条真人消息就考虑。
# ⚠️ 有覆盖的群默认**关掉关键词相关度门槛**（minRelevance=0 / interests 清空）：
#    接不接由模型自己判断（"是不是在找我 / 我能不能接"），
#    不再被「白米饭/游戏/猫」这种词表卡死 —— 那套只适合兜底，不适合测试群。
def _parse_group_interject_overrides(raw: str) -> dict:
    out = {}
    for part in str(raw or "").split(","):
        bits = [b.strip() for b in part.split(":")]
        if not bits or not bits[0]:
            continue

        def _num(idx, cast):
            if len(bits) > idx and bits[idx]:
                try:
                    return cast(bits[idx])
                except Exception:
                    return None
            return None

        ov = {"minRelevance": 0.0, "interests": []}
        _c, _p = _num(1, float), _num(2, float)
        _m, _n = _num(3, int), _num(4, int)
        if _c is not None:
            ov["cooldownSec"] = _c
        if _p is not None:
            ov["interjectChance"] = _p
        if _m is not None:
            ov["maxPerHour"] = _m
        if _n is not None:
            ov["minRecentMessages"] = _n
        out[bits[0]] = ov
    return out


GROUP_INTERJECT_OVERRIDES = _parse_group_interject_overrides(
    os.getenv("GROUP_INTERJECT_OVERRIDES", "")
)

USE_LOCAL_TTS = os.getenv("USE_LOCAL_TTS", "false").lower() == "true"
# 私聊「正在输入」状态（NapCat 的 set_input_status，群聊不可靠，只私聊用）
TYPING_STATUS = os.getenv("TYPING_STATUS", "true").lower() == "true"
SHORT_QUERY_WINDOW_SEC = tuning.i("SHORT_QUERY_WINDOW_SEC")

# 主动插话的「文字气泡」要不要随机转成语音（0 = 永远纯文字）
# ⚠️ 原来这段走 services.reply.reply_with_multimodal，它内部有 30% 概率直接调
#    **云端 CosyVoice**，完全绕过 indextts —— 用户实测「indextts 开着，群里还是龙呼呼」
#    就是这个路径。现在改走 speak_voice（indextts 优先，失败才降级云端）。
RANDOM_CHAT_VOICE_RATE = tuning.f("RANDOM_CHAT_VOICE_RATE")

# ================= 高刷消息队列清理 =================
# 问题：群里刷得快时，排队等她回复的消息会一直堆着。她一条要 10~30 秒，
#       等她一条条回完，回的全是几分钟前的旧话（用户实测的「一直在回很久之前的」）。
# 规则：BURST_WINDOW_SEC 秒内群里来了 BURST_MSG_COUNT 条 → 清空待处理队列，
#       只留「@ 我 / 喊我名字」的那几句，其余丢掉。正在处理的那条本来就已经出队，不受影响。
#       平时（刷得不快）完全按原样一条条回，不改变行为。
BURST_FLUSH_ENABLED = os.getenv("BURST_FLUSH_ENABLED", "true").lower() == "true"
BURST_WINDOW_SEC = tuning.f("BURST_WINDOW_SEC")
BURST_MSG_COUNT = tuning.i("BURST_MSG_COUNT")

# ================= 重复回复拦截 =================
# 真机踩过：#1 图片回复「你满意了？我还没说话呢」，
# 22 秒后插话又回「你满意了？我满意了」→ 群友「这肥鱼魔了」。
# 同一会话 REPLY_DEDUP_SEC 秒内，新回复和上一句相似度 ≥ REPLY_DEDUP_RATIO 就不发。
REPLY_DEDUP_SEC = tuning.f("REPLY_DEDUP_SEC")
REPLY_DEDUP_RATIO = tuning.f("REPLY_DEDUP_RATIO")

# ================= 被讨厌就闭嘴 =================
# 真机踩过：群友连着说「这逼鱼真烦人」「能不能关了先」，
# 她 5 分钟后还在自顾自回话 —— 因为系统根本不知道"被讨厌了"。
# 命中 → 该群静默 GROUP_MUTE_SEC 秒：不插话、不为图开口；@ 她或喊名字仍然回。
GROUP_MUTE_SEC = tuning.f("GROUP_MUTE_SEC")

# ================= 群聊不接纯问候 =================
# 用户 2026-11-11：很多群夜里只剩「早安/晚安」，她一句句接，把安静的环境吵了。
# 只跳过**纯问候**（去掉问候词和语气词后什么都不剩），
# 有实质内容的照常回：「早安，今天那个 bug 修了吗」→ 回。
SKIP_PURE_GREETING = os.getenv("SKIP_PURE_GREETING", "true").lower() == "true"

# ================= 操作性短指令不接 =================
# 「扫扫」「来了」「发一下」「装这个」—— 这是群友互相配合做事，不是在聊天。
# 真机踩过：群里在扫码登录游戏，她接了一句「阴阳师？扫这个能出SSR吗」→「扫你妈」。
# 只对**整句精确匹配**生效，所以「小肥鱼在吗」不会被误杀；@ 她也不受影响。
SKIP_OP_SHORT = os.getenv("SKIP_OP_SHORT", "true").lower() == "true"
_OP_SHORT = {
    "扫扫", "扫了", "扫一下", "扫这个", "扫码", "来了", "来了来了", "发下", "发一下",
    "发个", "发我", "装这个", "装了", "装好了", "上了", "上号", "开黑", "等下",
    "等等", "马上", "稍等", "好的", "好嘞", "行吧", "收到", "在么", "在吗",
}
# ================= 收尾语不接 =================
# 用户 2026-11-17：别人说「睡了」「算了」「行吧」的时候，她还在接。
# 只对**整句短收尾**生效（≤8 字、无问号），有实质内容的照常回。
SKIP_CLOSING = os.getenv("SKIP_CLOSING", "true").lower() == "true"
_CLOSING = {
    "算了", "行吧", "好吧", "没事", "就这样", "先这样", "不聊了", "不说了",
    "溜了", "走了", "我走了", "拜拜", "拜拜了", "886", "下线", "撤了",
    "吃饭去了", "打游戏去了", "睡了", "睡觉了", "先睡了", "晚安", "晚安啦",
}

_GREET_RE = re.compile(
    r"^(各位|大家|兄弟们|家人们|宝子们|米娜桑|群友们)?[\s,，:：、]*"
    r"((早安|早上好|早啊|早|晚安|晚上好|午安|安|睡了|睡啦|睡咯|睡觉了|去睡了|"
    r"先睡了|洗洗睡|晚安啦|困了|我睡了)"
    r"[\s啦咯了呀啊哦喔嗯~～!！。，,、]*)+$"
)

# ⚠️ 分成「强信号」和「弱信号」两档，否则 A 让 B 闭嘴也会把她静默掉。
#   强信号 = 句子里带了**指代她**的词（鱼/鲸鱼/机器人/bot/逼鱼），一定是冲她；
#   弱信号 = 「闭嘴」「滚」「别说了」这种通用骂人话，**必须再确认是冲她**才生效
#            （@ 她 / 喊她名字 / 她 2 分钟内刚说过话）。
_DISLIKE_STRONG_RE = re.compile(
    r"(这|那|你)(条|只|个)?\s*(肥鱼|鲸鱼|鱼|机器人|bot|ai|AI)"
    r".{0,8}(烦|吵|闭嘴|滚|关|疯|魔|傻|蠢|有病|恶心)"
    r"|(烦死|吵死).{0,6}(鱼|机器人|bot)"
    r"|(逼鱼|这鱼疯|这肥鱼魔|这鱼有病|破鱼|死鱼)",
    re.I,
)
_DISLIKE_WEAK_RE = re.compile(
    r"(闭嘴|滚蛋|滚开|滚|别说了|安静点|消停点|"
    r"能不能(把.{0,8})?(关了|关掉|闭麦)|(赶紧|快点?)(把.{0,8})?(关了|关掉|闭麦))",
    re.I,
)

# 最近一次"群友嫌她"的原话（gid → {"text", "ts"}）。会注入 prompt，
# 让她自己知道气氛变了 —— 光靠代码静默，她下一句还是会往上凑。
group_dislike_text = {}

# ================= 运行时状态 =================
waiting_for_sing = defaultdict(bool)
last_lyrics = defaultdict(str)
session_queues = defaultdict(asyncio.Queue)
session_last_reply = defaultdict(float)
session_workers = {}
recent_group_msgs = defaultdict(lambda: deque(maxlen=MAX_GROUP_RECENT))
# 👇 和 recent_group_msgs 一一对应的时间戳。
#    主动插话原来把 recentTimes 全写成 [now]*n，导致「最近 5 分钟有几条消息」
#    这道闸门永远通过（永远等于消息条数），插话频率就压不住。
recent_group_ts = defaultdict(lambda: deque(maxlen=MAX_GROUP_RECENT))
# 👇 判「群里刷得有多快」用的时间戳。和 recent_group_ts 分开存，因为
#    recent_group_ts 的长度受 MAX_GROUP_RECENT 限制（可能只有 3~8），
#    不够判「30 秒内 4 条」；而且这里只记**群友**的消息，不记她自己说的。
burst_msg_ts = defaultdict(lambda: deque(maxlen=64))
last_group_activity = defaultdict(float)
# 群里最后一个说话的真人（主动插话判断「是不是该避开某个人」用）
last_group_speaker: dict = {}
# worker 是否正在处理这个会话（主动插话的互斥闸门；以前只靠队列空/10s 代理锁）
_WORKER_BUSY: dict = {}


class _InterjectEvent:
    """主动插话用的最小事件壳：只给工具层提供 group_id/self_id/user_id。"""
    __slots__ = ("group_id", "self_id", "user_id")

    def __init__(self, gid, self_id):
        try:
            self.group_id = int(gid)
        except Exception:
            self.group_id = None
        self.self_id = str(self_id or "")
        self.user_id = 0
last_random_chat = defaultdict(float)
# 群被"讨厌"后的静默截止时间戳（见 _note_dislike）
group_mute_until = defaultdict(float)
# 每个人在本群最近说过的话（跨 8 条窗口）。
# 为什么要单独存：recent_group_msgs 只有 8 条，被 @ 时想回答「他到底在聊什么」，
# 光看 8 条往往不够 —— 他可能 20 条之前就开始说这件事了。
speaker_recent = defaultdict(lambda: defaultdict(lambda: deque(maxlen=6)))

# 上一句/上一批说话人（短句兜底）
last_text_map = {}
last_text_at = {}

# 她"主动说过话"的时间戳（群聊刹车用）
bot_reply_times = defaultdict(list)
interject_times = defaultdict(list)
# 插话循环「上次看过的最新人类消息时间」：同一段对话只看一次。
# 真机踩过（2026-11-18）：她问完「苹果猪是什么猪」，没人接话；
# 冷却 60s 后插话循环又看到同一段上下文，这次用语音自问自答了一句。
_interject_seen = defaultdict(float)
# 查看频率（2026-11-19）：刚说过话会自动收敛，安静久了更容易开口
_VIEW_FREQ = defaultdict(lambda: vf_mod.ViewFrequency())
_SILENCE_SEEN: dict = {}    # gid -> 已经给过「静默开口机会」的那段对话
_EXP_COUNT: dict = {}       # gid -> 攒了多少条真人消息（够数就挖一次表达）
_LAST_INTERVAL: dict = {}   # gid -> 上一轮窗口的平均消息间隔（预测用）

# 全局停用发图
async def _disabled_send_random_fishy(*args, **kwargs):
    return False

import services.meme
services.meme.send_random_fishy = _disabled_send_random_fishy


# ================= 发送语音（base64） =================
async def send_voice_base64(bot: Bot, event: MessageEvent, audio_path: str) -> bool:
    if not os.path.exists(audio_path):
        return False
    try:
        with open(audio_path, "rb") as f:
            b64_audio = base64.b64encode(f.read()).decode()
        await bot.send(event, MessageSegment.record(file=f"base64://{b64_audio}"))
        return True
    except Exception as e:
        logger.error(f"[VOICE] base64 发送失败: {e}")
        return False


async def speak_voice(bot: Bot, event: MessageEvent, text: str) -> bool:
    """
    **统一语音出口**：任何场景（私聊回复 / 群聊回复 / 主动插话）都先试算力机 indextts，
    失败才降级阿里云 CosyVoice，最后退回文字。

    之前群聊主动插话（_random_chat_loop）直接调 send_voice（云端），
    所以群里永远听不到她自己的声音，只有私聊才是 indextts —— 就是那个 bug。
    """
    # 语音朗读前最后一道清洗：`stick:开心` / `<sticker:开心>` / `[[THINK|…]]`
    # 都不能念出来（用户 2026-11-23）。
    text = clean_voice_text(text)
    if not text:
        return False
    sent = False
    _online = _compute_online()
    _caps = set()
    try:
        from plugins.tool_dispatcher import STATUS as _ST
        _caps = set(_ST.get("caps") or [])
    except Exception:
        pass
    logger.info(
        f"[TTS] 决策：USE_LOCAL_TTS={USE_LOCAL_TTS}｜算力机在线={_online}"
        f"｜caps={sorted(_caps) or '(空)'}"
        f"｜{'走本地 indextts' if (USE_LOCAL_TTS and _online) else '直接降级云端'}"
    )
    if USE_LOCAL_TTS and _online and _caps and "tts" not in _caps:
        logger.warning("[TTS] 算力机上报的 caps 里没有 tts —— 任务会被判 unpicked，检查 node.py 版本")

    # ① 首选：算力机 indextts（只在她在线时试，避免白等 25 秒）
    if USE_LOCAL_TTS and _online:
        try:
            from plugins.tool_dispatcher import STATUS
            task_id = f"chat_tts_{int(time.time() * 1000)}"
            STATUS["tasks"][task_id] = {
                "type": "tts", "status": "pending", "text": text, "output": "",
            }
            logger.info(f"[TTS] 已提交算力机任务 {task_id}，等 pickup…")
            # ⚠️ 等 240 秒而不是 120：算力机 IndexTTS 的 TTS_TIMEOUT 是 300 秒，
            #    首次加载模型就要几十秒，服务器先放弃就等于白跑（日志里 status=timeout）。
            st = await _wait_task(
                task_id,
                total=tuning.i("TTS_WAIT_TOTAL"),
                pickup_timeout=tuning.i("TTS_PICKUP_TIMEOUT"),
            )
            logger.info(f"[TTS] 任务结果 status={st.get('status')} output={str(st.get('output'))[:80]}")
            out_path = st.get("output") if st.get("status") == "completed" else None
            if isinstance(out_path, dict):
                logger.warning(f"[TTS] 算力机报错: {out_path}")
                out_path = None
            if isinstance(out_path, str) and out_path and os.path.exists(out_path):
                sent = await send_voice_base64(bot, event, out_path)
                try:
                    os.remove(out_path)
                except Exception:
                    pass
            if not sent:
                try:
                    del STATUS["tasks"][task_id]
                except Exception:
                    pass
                logger.warning(f"[TTS] 算力机 indextts 未完成（{st.get('status')}），降级云端")
        except Exception as e:
            logger.warning(f"[TTS] 算力机 indextts 异常: {type(e).__name__}: {e}")

    # ② 降级：阿里云 CosyVoice
    if not sent:
        try:
            from services.tts import send_voice
            sent = await send_voice(bot, event, text)
        except Exception as e:
            logger.warning(f"[TTS] 云端 CosyVoice 失败: {type(e).__name__}: {e}")
            sent = False

    # ③ 兜底：都失败就发文字
    if not sent:
        logger.warning("[TTS] 本地 + 云端都失败，退回文字")
        try:
            await bot.send(event, text)
        except Exception:
            pass
    return sent


async def _typing(bot: Bot, event: MessageEvent, on: bool = True) -> None:
    """私聊「正在输入」状态。失败静默（不同 NapCat 版本支持不一样）。"""
    if not TYPING_STATUS or isinstance(event, GroupMessageEvent):
        return
    try:
        await bot.call_api("set_input_status",
                           user_id=int(event.user_id),
                           event_type=1 if on else 0)
    except Exception:
        pass


SILENT_TIMEOUT = 300


def _in_sleep_hour() -> bool:
    """现在是不是「群聊该睡了」的时段（默认 0~8 点）。

    ⚠️ 只对**群聊**生效：用户 2026-11-11「群聊是有 0~8 点休息的，但是私聊没限制，
    可以 24 小时随时聊」。私聊要静默请单独设 PRIVATE_SLEEP=true。
    """
    h = time.localtime().tm_hour
    if SLEEP_HOUR_START <= SLEEP_HOUR_END:
        return SLEEP_HOUR_START <= h < SLEEP_HOUR_END
    return h >= SLEEP_HOUR_START or h < SLEEP_HOUR_END


def _get_session_key(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        return f"group_{event.group_id}"
    return f"private_{event.user_id}"


# ================= 关注会话（被 @ 之后连续跟几句） =================
# 需求：群里被 @ 一次之后，继续关注**这个人**的后续 N 条消息，
#       每条都回一次，一共 N+1 次对话；超过 FOCUS_TIMEOUT 秒没发满就自动退出。
# 实现要点：
#   · 按 (session, qq) 隔离，群里多个人互不干扰 —— 跟音频分离那套「个人异步」一个思路
#   · 只有被 @ 的那个人会被跟，别人插话不会消耗额度
#   · @ 那一次本身不消耗额度（额度是「后续还能接几条」）
FOCUS_FOLLOW_COUNT = tuning.i("FOCUS_FOLLOW_COUNT")   # 后续还接几条（需求：关注三句）
FOCUS_TIMEOUT = tuning.f("FOCUS_TIMEOUT")          # 秒

# {session_key: {qq: {"left": n, "until": ts, "started": ts}}}
focus_sessions = defaultdict(dict)


def _focus_start(session_key: str, qq, pending: bool = False) -> None:
    """被 @ / 戳 → 开启/重置关注。

    pending=True 表示这次 @ 是**空的**（只有 @，一个字没说）：
    对方多半是「先叫一下、马上补内容」。下一条同一人的消息直接算在跟她说话，
    不再让模型判（真机 2026-11-18：@ 空消息 → 14 秒后「发个表情包」被判「否（不回）」，
    用户看到的就是「艾特她发个表情包，她不回」）。
    """
    qq = str(qq)
    focus_sessions[session_key][qq] = {
        "left": FOCUS_FOLLOW_COUNT,
        "until": time.time() + FOCUS_TIMEOUT,
        "started": time.time(),
        "pending": bool(pending),
        "neg": 0,          # 连续判否次数（连续到阈值就结束关注，防一直跟着讲）
    }
    if FOCUS_FOLLOW_COUNT > 0:
        logger.info(
            f"[FOCUS] {session_key} 开始关注 {qq}："
            f"后续 {FOCUS_FOLLOW_COUNT} 条（{FOCUS_TIMEOUT:.0f}s 内有效）"
        )


def _focus_clean(session_key: str, qq) -> dict | None:
    """取关注状态，顺手清掉过期的。"""
    qq = str(qq)
    f = focus_sessions.get(session_key, {}).get(qq)
    if not f:
        return None
    if time.time() > f["until"] or f["left"] <= 0:
        focus_sessions[session_key].pop(qq, None)
        if f["left"] > 0:
            logger.info(f"[FOCUS] {session_key} 关注 {qq} 超时退出（还剩 {f['left']} 条没等到）")
        return None
    return f


def _focus_active(session_key: str, qq) -> bool:
    return _focus_clean(session_key, qq) is not None


def _focus_consume(session_key: str, qq) -> int:
    """回完一条后扣额度，返回剩余条数。"""
    f = _focus_clean(session_key, qq)
    if not f:
        return 0
    f["left"] -= 1
    f["until"] = time.time() + FOCUS_TIMEOUT   # 每次交互续期
    if f["left"] <= 0:
        focus_sessions[session_key].pop(str(qq), None)
        logger.info(f"[FOCUS] {session_key} 对 {qq} 的关注结束（已接满）")
        return 0
    logger.info(f"[FOCUS] {session_key} 对 {qq} 还剩 {f['left']} 条")
    return f["left"]


# ================= 群聊上下文的消息标识 =================
# 为什么要有这个：原来只存 `昵称: 内容`，群里两个同名的人分不清、
# 改群名片后历史称呼和现在对不上、模型也不知道该用哪个称呼。
# 现在统一成 `名称(QQ尾4)` —— 尾 4 位足够区分同名人，比 10 位全号省一半 token。
_LABEL_CACHE: dict = {}


def _clear_label(qq: str) -> None:
    """改称呼后清掉这个人的所有「群:QQ」标签缓存。"""
    qq = str(qq or "")
    if not qq:
        return
    for k in [k for k in _LABEL_CACHE if k == qq or k.endswith(":" + qq)]:
        _LABEL_CACHE.pop(k, None)

# 👇 digest 提醒（抄 dsh-plugin-memory 的 digest-guard）：
# 她经常聊完就忘、不写记忆，全靠两天复盘兜底。连续 N 轮一条都没记时，
# 在下一轮给她一句「该记了」的提醒（不是给用户看的）。
_no_memo_streak: dict = {}
DIGEST_AFTER = tuning.i("DIGEST_AFTER")


def _msg_label(event) -> str:
    """给一条消息打「谁说的」标签：称呼优先，兜底群名片，末尾带 QQ 尾 4 位。"""
    qq = str(getattr(event, "user_id", "") or "")
    gid = str(getattr(event, "group_id", "") or "")
    # 缓存键必须带群：同一个人在不同群可能叫不同名字（用户 2026-11-18）
    ck = f"{gid}:{qq}" if gid else qq
    if ck and ck in _LABEL_CACHE:
        return _LABEL_CACHE[ck]

    name = ""
    if qq:
        try:
            # 本群称呼 > 全局称呼 > 群名片
            name = profiles.call_of(qq, "", gid)
        except Exception:
            name = ""
    if not name:
        name = display_name(event) or qq or "某人"

    label = f"{name}({qq[-4:]})" if len(qq) >= 4 else name
    if ck:
        if len(_LABEL_CACHE) > 800:      # 简单上限，别让缓存无限涨
            _LABEL_CACHE.clear()
        _LABEL_CACHE[ck] = label
    return label


_AUDIT_WIRE_WARNED = set()


def _warn_audit_wire(key: str, e: Exception) -> None:
    """
    审核接线的异常：同一个位置只 WARNING 一次，之后 DEBUG。

    ⚠️ 2026-11-19 教训：这里以前是 `logger.debug`，而 `bot_name=current_personality(...)`
       每次都抛 TypeError，被这里吞掉 → **审核从来没跑过**，日志里一条都看不见，
       只有 `/测函数 现场` 显示满屏「审：未审核」。接线错必须能自己喊出来。
    """
    if key in _AUDIT_WIRE_WARNED:
        logger.debug(f"[SCENE-AUDIT] 接线异常 {key}: {type(e).__name__}: {e}")
    else:
        _AUDIT_WIRE_WARNED.add(key)
        logger.warning(
            f"[SCENE-AUDIT] 接线异常 {key}（这个位置的审核会一直被跳过）: "
            f"{type(e).__name__}: {e}")


def _bot_name(gid: str = "", self_id: str = "") -> str:
    """
    她**在这个群**叫什么（给群聊现场 / 审核 LLM 用）。

    以前这里写的是 `current_personality(...)` —— 那是个 defaultdict，一调就抛
    TypeError（2026-11-19 事故），现在走 persona_name()。
    优先级：她自己的档案称呼 > 人格名 > 小鲸鱼。
    """
    try:
        nm = profiles.call_of(str(self_id or ""), "", str(gid or ""))
    except Exception:
        nm = ""
    if nm:
        return nm
    try:
        nm = persona_name(f"group_{gid}" if gid else "") or "小鲸鱼"
    except Exception:
        nm = "小鲸鱼"
    # 「小鲸鱼proplus」这种内部文件名不是群友叫她的名字，去掉后缀
    for suffix in ("proplus", "pro", "plus"):
        if nm.lower().endswith(suffix) and len(nm) > len(suffix) + 1:
            nm = nm[: -len(suffix)]
            break
    return nm or "小鲸鱼"


# ==================== 她是谁：人格 ID（2026-11-23）====================
# 用户：「标签自己的话全部改成 NAPCAT_ACCOUNT……让她认为我就是 NAPCAT_ACCOUNT，
#       要不然 QQ 被封一个记忆就不能复用了？」
# 所以：写日志 / 判收件人 / 记忆 一律用 services.identity 里**落盘固定**的人格 ID；
# 实际登录过的号都是别名（旧号的历史发言也还是「她」）。
def _self_id(event=None, bot=None) -> str:
    """她的人格 ID（换号不变；没配 NAPCAT_ACCOUNT 时退回真实 self_id）。"""
    real = str(getattr(event, "self_id", "") or "") if event is not None else ""
    if not real and bot is not None:
        real = str(getattr(bot, "self_id", "") or "")
    try:
        if real:
            identity_mod.note_account(real)
        return identity_mod.canonical(real) or real
    except Exception:
        return real


def _is_self_qq(qq, event=None) -> bool:
    """这个 QQ 是不是她自己（当前号 + 所有历史登录号）。"""
    real = str(getattr(event, "self_id", "") or "") if event is not None else ""
    try:
        return identity_mod.is_self(qq, real)
    except Exception:
        return bool(qq) and str(qq) == real


def _canon_at(at_qqs) -> list:
    """@ 列表：历史号/当前号统一成人格 ID（去重保序）。"""
    out = []
    for q in at_qqs or []:
        q = str(q or "")
        if not q:
            continue
        try:
            q = identity_mod.canon(q) or q
        except Exception:
            pass
        if q not in out:
            out.append(q)
    return out


def _scene_names(gid: str) -> dict:
    """
    给 group_scene / 审核 LLM 的「名字 → QQ」表。

    ⚠️ 第一优先是**现场里真实用过的名字**（group_scene 记的 name，也就是 prompt
    里显示的那个名字）。以前只从档案里取，结果群里大部分人是没档案的裸号：
    审核 LLM 看到「阿明: 可以打扰一下测个东西吗」，返回 "to":"阿明"，
    我们这边对不上 QQ → 只能丢掉。用户 2026-11-19 现场全「审：未审核」就是这个。
    """
    out = {}
    gid = str(gid or "")
    try:
        for m in group_scene.recent_messages(gid, 60):
            nm = str(m.get("name") or "").strip()
            qq = str(m.get("qq") or "").strip()
            if nm and qq and nm != "你" and nm not in out:
                out[nm] = qq
    except Exception:
        pass
    try:
        qqs = list((speaker_recent.get(gid) or {}).keys())[-30:]
    except Exception:
        qqs = []
    for qq in qqs:
        try:
            p = profiles.get_profile(qq)
            cand = [profiles.call_of(qq, "", gid), p.get("nickname"),
                    p.get("call_name")] + list(p.get("aliases") or [])
            for nm in cand:
                nm = str(nm or "").strip()
                if len(nm) >= 2 and not nm.isdigit() and nm not in out:
                    out[nm] = str(qq)
        except Exception:
            continue
    return out


def _voice_text(event) -> str:
    """
    取 QQ 语音消息里**客户端已经转写好的文字**。

    ⚠️ 不需要下载 record 再自己调 ASR：NapCat 的 record 段本来就带 `text`
    （QQ 客户端自动转写的），直接读就行，省一次下载 + 一次 ASR 调用。
    """
    try:
        for seg in event.get_message():
            if seg.type == "record":
                return str(seg.data.get("text") or "").strip()
    except Exception:
        pass
    return ""


# ================= @ 标签 =================
# parse_reply 已经把模型的 [[AT|小明]] 转成内部写法 <at:小明>，
# 发送前由 expand_mentions 换成 NapCat 的真 at 段（QQ 里那个蓝色 @）。
_AT_TAG_STRIP = re.compile(r"<at:\s*([^>]{1,30}?)\s*>")


def _split_at_tags(text: str) -> tuple:
    """
    把 <at:名字> 从正文里摘出来。
    为什么：群聊每条气泡硬截断到 20 汉字，标签里的名字也算汉字，
    截在标签中间就会把 "<at:小明>" 切成 "<at:小" 直接发出去。
    返回 (名字列表, 纯正文)。
    """
    s = str(text or "")
    names = _AT_TAG_STRIP.findall(s)
    return names, _AT_TAG_STRIP.sub("", s).strip()


def _segs_to_message(segs) -> Message:
    """
    expand_mentions 返回的是 **dict 列表**（`{"type":"text","data":{...}}`），
    而 nonebot 的 `Message()` 只吃 MessageSegment / str —— 直接把 dict 塞进去会抛
    `ValueError: Unexpected type: <class 'dict'>`，整条回复发不出去。

    这里统一转一次。之前没炸是因为群成员名册一直拉不到（走的是纯文字分支），
    加了名册缓存之后名册能拉到了，才踩到这个雷。
    """
    out = []
    for s in segs or []:
        if isinstance(s, dict):
            t = str(s.get("type") or "text")
            d = s.get("data") or {}
            if t == "text":
                txt = str(d.get("text") or "")
                if txt:
                    out.append(MessageSegment.text(txt))
            elif t == "at":
                qq = str(d.get("qq") or "")
                if qq:
                    out.append(MessageSegment.at(qq))
            elif t == "reply":
                rid = str(d.get("id") or "")
                if rid:
                    out.append(MessageSegment.reply(int(rid)))
            else:
                try:
                    out.append(MessageSegment(t, **d))
                except Exception:
                    pass
        else:
            out.append(s)
    return Message(out)


# ================= 群消息发送限流 =================
# QQ 服务端硬限制：**同一个群每分钟最多 10 条**（retcode=1200 "本群每分钟只能发10条消息"）。
# 我们一次回复经常 3~5 个气泡，再叠加主动插话，很容易撞墙 → 整轮失败、后面的气泡全丢。
# 这里留 1 条余量（默认 9），撞上就等，等太久就丢弃剩余气泡（宁可少说，不要整轮报错）。
GROUP_SEND_LIMIT = tuning.i("GROUP_SEND_LIMIT")
GROUP_SEND_WINDOW = 60.0
GROUP_SEND_MAX_WAIT = tuning.f("GROUP_SEND_MAX_WAIT")
_group_send_times: dict = {}          # gid -> deque[ts]


def _msg_has_media(msg) -> bool:
    """这条消息里有没有图片/语音/视频（决定超时后要不要重试）。"""
    try:
        segs = list(msg) if isinstance(msg, Message) else [msg]
        return any(getattr(s, "type", "") in ("image", "record", "video")
                   for s in segs)
    except Exception:
        return False


async def _safe_send(bot, ev, msg, *, retries: int = 1, delay: float = 1.5) -> bool:
    """
    发一条消息，失败不抛异常。

    ⚠️ 为什么必须有这个：
      `bot.send` 抛 ActionFailed 会一路冒到 `_session_worker` 的 except，
      **整轮剩余气泡全部丢失**。真机踩过两种：
        · retcode=1200「本群每分钟只能发10条消息」→ 限流（已由 _wait_group_quota 挡）
        · retcode=1200「Timeout: NTEvent ... sendMsg EventRet: {}」→ QQ 协议层超时
      第二种是随机的，重试一次通常就好；还不行就跳过这条，别拖累整轮。
    """
    for i in range(retries + 1):
        try:
            await bot.send(ev, msg)
            return True
        except Exception as e:
            txt = f"{type(e).__name__}: {e}"
            _is_timeout = ("Timeout" in txt) or ("NTEvent" in txt)
            if _is_timeout:
                # ⚠️ 2026-11-23 重复消息根因：QQ 协议层超时后重试，第一次其实
                #    已经发出去了 → 群里看到两条一模一样的（连引用都相同）。
                #    文本一律不重试；只有图片/语音/视频才值得重试（上传超时常见）。
                if _msg_has_media(msg) and i < retries:
                    logger.warning(
                        f"[SEND] 媒体上传超时，{delay:.1f}s 后重试一次")
                    await asyncio.sleep(delay)
                    continue
                logger.warning(
                    "[SEND] QQ 协议层超时：按已送达处理，不重试（防重复消息）")
                return True
            logger.warning(f"[SEND] 这条发不出去，跳过（不影响后面）: {txt[:160]}")
            return False
    return False


async def _send_long_as_forward(bot, ev, text: str, reply_to: str = "") -> bool:
    """
    长文 → QQ「合并转发」卡片（群聊 / 私聊都走这里），不截断、不限字数。

    规矩（用户 2026-11-18 确认：一直是这条，不是新加的）：
      **20 字以内直接发；20 字以上用合并转发发。**
    以前群聊是硬截断（>20 字直接砍掉，经常砍在半句话上），和这条规矩正好相反。
    ⚠️ 带真 @ / 引用回复的文本没法塞进卡片，调用方要先判断，那两种才退回截断。

    reply_to：群聊里被 @ 时自动带上的「回复谁」。卡片本身不能带引用段，
    所以在第一段前面写一行「回复 @昵称」，让人一眼看出这是在回谁
    （用户 2026-11-18：她自己说话太多，长回复不标就不知道在回谁）。
    """
    text = str(text or "").strip()
    if not text:
        return False
    _reply_to = str(reply_to or "").strip()
    if _reply_to:
        text = f"回复 @{_reply_to}：{text}"
    # 优先按空行分段；没有空行就按换行分
    paras = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()] or \
        [p.strip() for p in text.splitlines() if p.strip()]
    if not paras:
        paras = [text]

    _name = "小鲸鱼"
    try:
        _name = persona_name() or _name
    except Exception:
        pass
    _uin = str(getattr(bot, "self_id", "") or "")

    nodes = []
    for p in paras:
        # 单节点也别太长，按 ~600 字切，保持卡片里每条读起来舒服
        for chunk in _chunk_text(p, 600):
            nodes.append({
                "type": "node",
                "data": {
                    "name": _name,
                    "uin": _uin,
                    "content": [{"type": "text", "data": {"text": chunk}}],
                },
            })
    if not nodes:
        return False
    _is_group = isinstance(ev, GroupMessageEvent)
    try:
        if _is_group:
            await bot.call_api("send_group_forward_msg",
                               group_id=int(ev.group_id), messages=nodes)
        else:
            await bot.call_api("send_private_forward_msg",
                               user_id=int(ev.user_id), messages=nodes)
    except Exception as e:
        # 老版本 NapCat 只认 generic 的，兜底再试一次
        try:
            if _is_group:
                await bot.call_api("send_forward_msg",
                                   group_id=int(ev.group_id), messages=nodes)
            else:
                await bot.call_api("send_forward_msg",
                                   user_id=int(ev.user_id), messages=nodes)
        except Exception as e2:
            logger.warning(
                f"[FWD] 合并转发失败，退回普通发送: "
                f"{type(e).__name__}: {e} / {type(e2).__name__}: {e2}"
            )
            return False
    logger.info(
        f"[FWD] {'群聊' if _is_group else '私聊'}长文已用合并转发发出"
        f"（{len(nodes)} 段 / {len(text)} 字）"
    )
    return True


async def _wait_group_quota(gid: str) -> bool:
    """
    群发送配额。返回 True 表示可以发（并已占用一个名额），False 表示要等太久，放弃。
    """
    from collections import deque
    dq = _group_send_times.get(gid)
    if dq is None:
        dq = deque()
        _group_send_times[gid] = dq

    now = time.time()
    while dq and now - dq[0] > GROUP_SEND_WINDOW:
        dq.popleft()

    if len(dq) < GROUP_SEND_LIMIT:
        dq.append(now)
        return True

    wait = GROUP_SEND_WINDOW - (now - dq[0]) + 0.5
    if wait > GROUP_SEND_MAX_WAIT:
        logger.warning(f"[CHAT] {gid} 配额已满，要等 {wait:.0f}s，放弃剩余气泡")
        return False
    logger.info(f"[CHAT] {gid} 群消息配额已满，等 {wait:.1f}s 再发")
    await asyncio.sleep(wait)
    dq.append(time.time())
    return True


# 群里另一只 bot：大部分时候不理它，偶尔接两句（不然显得像死机）
PEER_REPLY_PROB = tuning.f("PEER_REPLY_PROB")
# 它经常连发好几条，不加冷却会连着回好几次
PEER_REPLY_COOLDOWN = tuning.f("PEER_REPLY_COOLDOWN")
_last_peer_reply_ts = 0.0


# ================= 上下文时间戳 =================
# 每句上下文前面标上时间，模型才知道「隔了多久」——
# 不然「嗯」「在」这种短句它分不清是刚刚还是三小时前。
_TS_TAG = re.compile(r"^【\d{4}年\d{1,2}月\d{1,2}日\d{1,2}:\d{2}】")


def _ts_tag(ts: float = 0) -> str:
    t = time.localtime(ts or time.time())
    return time.strftime("【%Y年%m月%d日%H:%M】", t)


def _strip_ts(line: str) -> str:
    return _TS_TAG.sub("", str(line or "")).strip()


# 「这是你之前的回复：」——她自己的话用这个前缀，
# 比原来的 `你: ` 清楚得多（`你:` 容易被模型当成"对方在跟你说话"）
_SELF_PREFIX = "这是你之前的回复："


def _media_placeholder(event) -> str:
    """纯媒体消息（图片/视频/语音）在上下文里的占位符。

    真机踩过（2026-11-18）：群里 /今日运势、/抽小猪 这种玩法发出去的是图片卡片，
    `get_plaintext()` 是空的 → 她上下文里什么都没有 → 群友拿着结果跟她聊，
    她只能反问「猪猪是啥」。
    """
    try:
        types = {seg.type for seg in event.get_message()}
    except Exception:
        return ""
    for t, label in (("image", "[图片]"), ("mface", "[表情]"),
                     ("video", "[视频]"), ("record", "[语音]")):
        if t in types:
            return label
    return ""


# 被动心情观察的节流（秒）：群里有消息就让她「心里过一遍」，
# 但她没回复时不必每条都判（judge_and_store 里还有 REFRESH 5 分钟）。
_MOOD_WATCH_AT: dict = {}
MOOD_WATCH_SEC = tuning.f("MOOD_WATCH_SEC")


def _maybe_mood_watch(session_key: str, gid: str, text: str,
                      name: str = "", speaker_qq: str = "") -> None:
    """
    被动心情观察（2026-11-20）。

    用户原话：「如果是心情不好变冷肯定也会看消息，就算不说话拿到消息也会有心理活动，
    不能只根据回复的时候带心理活动，那样如果越冷判断的次数越少，心情越难回升。」

    以前只在「她生成回复之后」判读 → 心情差 → 不说话 → 判读更少 → 更难回升（死锁）。
    现在只要群里有消息就（60s 节流 + 服务内 5 分钟 REFRESH）跑一次：
      · 只调判读模型，不调主模型 → 不会让她多说话；
      · 产出的 mind 当成「空回复但有内心活动」落盘，数值照常小步涨落。
    """
    if not mood_judge.ENABLED:
        return
    key = str(session_key or "")
    now = time.time()
    if now - float(_MOOD_WATCH_AT.get(key) or 0) < MOOD_WATCH_SEC:
        return
    ctx = list(recent_group_msgs.get(str(gid), []))[-mood_judge.CTX_LINES:]
    if not ctx:
        return
    _MOOD_WATCH_AT[key] = now
    # ⚠️ 被动观察也带 speaker_qq：她安静着不回话时，惹她的人照样要掉好感，
    #    否则「越冷越不判、越不判越难回升」会变成「越冷越不会对他失望」。
    #    判读仍然以最后 2~3 条为主，偶尔归错人影响也只是一次 ±0.10。
    _spawn_mood_judge(key, "", ctx, "",
                      src=f"{name}: {text}".strip(": "), reply="",
                      speaker_qq=str(speaker_qq or ""), passive=True,
                      addressed=False,
                      hint="她这次没说话，只是看到群里在聊")


def _spawn_mood_judge(session_key: str, label: str, ctx_lines, think: str = "",
                      src: str = "", reply: str = "", speaker_qq: str = "",
                      passive: bool = False, hint: str = "",
                      repair: bool = False, force: bool = False,
                      turn_id: str = "", addressed: bool = False) -> None:
    """
    后台跑心情判读（影子）。异常自己吞掉，别让 create_task 冒「never retrieved」。

    src   = 她这条在接的那句话（谁说了什么）
    reply = 她自己说的那句
    两个都落盘，`/测函数 心情` 才能逐条对照「哪句话 → 什么心里话」。
    """
    # 👇 心理活动先落盘：用户 2026-11-19「我看这些内容没有心里话啊，只有回复」——
    #    以前 think 只活到这一次判读，重判 / 事后看 / 复盘全都丢了。
    if think:
        try:
            mood_judge.note_think(session_key, think, src=src, reply=reply)
        except Exception as e:
            logger.debug(f"[MOOD-JUDGE] 记心理活动失败: {type(e).__name__}: {e}")
    try:
        mood_judge.note_think_stat(bool(think))     # 量化 [[THINK]] 有没有执行
    except Exception:
        pass
    if not think and not passive:
        logger.info(f"[MOOD-JUDGE] {session_key} 这条回复没带 [[THINK]]，"
                    f"只能靠群聊判（协议在 06b）")

    async def _run():
        try:
            # 她没写 THINK → 先补一句心里话，再判读（这样每条回复都有记录）。
            # 被动观察（passive）不补：那条不是她说的，补了会污染「逐条对照」。
            if not think and not passive:
                try:
                    await mood_judge.backfill_think(session_key, src=src, reply=reply)
                except Exception as e:
                    logger.debug(f"[MOOD-JUDGE] 补心里话失败: {type(e).__name__}: {e}")
            _res = await mood_judge.judge_and_store(
                session_key, label, ctx_lines,
                think=think, speaker_qq=speaker_qq,
                hint=hint, repair=repair, force=force,
                turn_id=turn_id,
                # 👇 这两项决定 Δ 会不会被压小（2026-11-22）：
                #    passive  = 她没说话，只是看到 → 旁观不该背锅
                #    addressed = 这条是不是在叫她 → 不是冲她来的最多 ±0.05
                passive=bool(passive), addressed=bool(addressed)) or {}
            # 被动观察：把判读出来的 mind 也当成「她心里的活动」落一条，
            # 这样「没说话但心里有想法」也能在 /测函数 心情 里看到。
            if passive and _res.get("ok") and _res.get("mind"):
                try:
                    mood_judge.note_think(session_key, _res["mind"],
                                          src=src, reply="", auto=True)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"[MOOD-JUDGE] 后台判读异常: {type(e).__name__}: {e}")

    try:
        asyncio.create_task(_run())
    except Exception as e:
        logger.debug(f"[MOOD-JUDGE] 起任务失败: {type(e).__name__}: {e}")


def _apply_mood_label(parsed: dict, session_key: str) -> str:
    """
    她写了 `[[MOOD|…]]` → 记成**她自己写的心情原话**（自由文本，不是固定枚举）。

    用户 2026-11-19：「心情不能是我们指定的几种，不能在我们限制的几种心情范围内。
    她要自己有写心情的权利。」—— 数值只影响语气浓淡，原话才是她此刻的感觉。
    """
    try:
        label = str((parsed or {}).get("mood") or "").strip()
        if not label:
            return ""
        mood.set_label(label, session_key)
        logger.info(f"[MOOD] 她自己写的心情：{label[:40]}")
        return label
    except Exception as e:
        logger.debug(f"[MOOD] 写心情原话失败: {type(e).__name__}: {e}")
        return ""


def _append_self_line(gid, text, ts: float | None = None) -> bool:
    """把她自己发出去的话记进群上下文（带「这是你之前的回复：」标记）。

    为什么要去重：她自己的回复会走**两条路**进上下文 ——
      ① worker 发完手动记一条；
      ② NapCat 把她自己发的消息回显成事件，_need_reply 又记一条。
    不去重的话同一句话出现两遍，模型会以为"这句被说了两次"，
    既浪费上下文，也容易顺着自己再答一遍（用户 2026-11-18 反馈「接话两次」）。
    """
    gid = str(gid)
    body = str(text or "").strip()
    if not body:
        return False
    # 长回复（>20 字会走合并转发）别把整篇塞进上下文：留个开头就够她认出「我说过」
    if len(body) > 300:
        body = body[:300].rstrip() + "…"
    ts = float(ts or time.time())
    try:
        for ln in list(recent_group_msgs.get(gid) or [])[-6:]:
            if _SELF_PREFIX in ln and ln.split(_SELF_PREFIX, 1)[-1].strip() == body:
                return False
        recent_group_msgs[gid].append(f"{_ts_tag(ts)}{_SELF_PREFIX}{body}")
        recent_group_ts[gid].append(ts)
        return True
    except Exception:
        return False


def _at_targets(event) -> set:
    """消息里 @ 了谁（QQ 号集合）。"""
    out = set()
    try:
        for seg in event.get_message():
            if seg.type == "at":
                qq = str(seg.data.get("qq") or "")
                if qq:
                    out.add(qq)
    except Exception:
        pass
    return out


# ================= 她能被怎么叫醒 =================
# 规则（按用户要求）：
#   ① **共同词**：所有人喊都算。**默认留空** —— 用户 2026-11-12：
#      「把大小肥鱼这个匹配去掉，只要用户取的名字可以响应，误判太多了」。
#      以前默认是「大肥鱼,小肥鱼」，群里聊到鱼、聊到胖子都可能撞上，
#      她老被莫名唤醒。要加回来自己配 ME_COMMON_NAMES=xxx,yyy。
#   ② **人格名**：她自己的名字（小鲸鱼 / 小鲸鱼proplus），全局共享，可关。
#   ③ **私人称呼**（bot_alias）：某个群友单独给她起的名字，
#      **只有那个人**喊才算 —— 由 _calls_me_by_name 传 speaker_qq 保证。
#      「只要用户取的名字能响应」靠的就是这条。
_ME_NAMES_COMMON = {
    n.strip() for n in os.getenv("ME_COMMON_NAMES", "").split(",") if n.strip()
}

# ④ **黑名单**：这些词永远不当唤醒词，不管从哪来（共同词 / 人格名 / bot_alias）。
#    用户 2026-11-12：「肥鱼这个也要去掉，这个触发率更高」——
#    光清空 ME_COMMON_NAMES 不够：只要有人把 bot_alias 设成「肥鱼」、
#    或者人格名/别名里带这俩字，还是会被叫醒。
#    判断用**子串**（b in n）：这样「大肥鱼」「蓝色大肥鱼」也会一起被剔除。
_ME_NAMES_BLOCK = tuple(
    n.strip() for n in os.getenv("ME_BLOCK_NAMES", "肥鱼").split(",") if n.strip()
)
ME_USE_PERSONA_NAME = os.getenv("ME_USE_PERSONA_NAME", "true").lower() == "true"
_BV_RE_LOCAL = re.compile(r"BV[0-9A-Za-z]{10}")


def _my_names(session_key: str = "", speaker_qq: str = "") -> set:
    """
    她当前能被叫醒的名字，分三类：

      ① 共同词（_ME_NAMES_COMMON）：所有人喊都算，**默认空**（要就自己配）
      ② 人格名：她自己的名字（小鲸鱼 / 小鲸鱼proplus），全局共享
      ③ **这个用户专属的称呼**（profile.bot_alias）：
         只有**给他设过名字的那个人**喊才生效 —— 别人喊同一个词不算。
         这是「先匹配 QQ，针对人」的关键。
    """
    names = set(_ME_NAMES_COMMON)

    if ME_USE_PERSONA_NAME:
        try:
            from services import persona as _p
            cur, _ = _p.persona_for(session_key)
            if cur:
                names.add(str(cur))
                # 去掉 proplus 之类的后缀（"小鲸鱼proplus" → "小鲸鱼"）
                for suffix in ("proplus", "pro", "plus"):
                    if cur.lower().endswith(suffix) and len(cur) > len(suffix) + 1:
                        names.add(cur[: -len(suffix)])
            for alias, target in (_p.PERSONA_ALIASES or {}).items():
                if target == cur:
                    names.add(str(alias))
        except Exception:
            pass

    # ③ 只有「本人」的专属称呼：靠 speaker_qq 隔离，别人喊同一个词不会被唤醒
    if speaker_qq:
        try:
            alias = str(profiles.get_profile(str(speaker_qq)).get("bot_alias") or "").strip()
            if alias:
                names.add(alias)
        except Exception:
            pass

    # ④ 黑名单过滤（子串）：只要名字里带「肥鱼」，不管来自哪里都剔掉
    if _ME_NAMES_BLOCK:
        names = {n for n in names
                 if not any(b in str(n) for b in _ME_NAMES_BLOCK)}

    return {n.strip() for n in names if n and len(str(n).strip()) >= 2}


def _calls_me_by_name(text: str, session_key: str = "", speaker_qq: str = "") -> bool:
    """消息里直接叫了她的名字/外号 → 当作被 @。"""
    t = str(text or "")
    if not t or len(t) > 120:
        return False
    return any(n in t for n in _my_names(session_key, speaker_qq))


# ================= 合并转发（聊天记录）=================
def _seg_plain(content) -> str:
    """把转发消息里的 message 字段（list[dict] 或 str）压成纯文字。"""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    out = []
    for seg in content:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type")
        d = seg.get("data") or {}
        if t == "text":
            out.append(str(d.get("text") or ""))
        elif t in ("image", "mface"):
            out.append("[图片]")
        elif t == "face":
            out.append("[表情]")
        elif t == "at":
            out.append(f"@{d.get('qq')}")
        elif t == "record":
            out.append("[语音]")
        elif t == "video":
            # 合并转发里夹视频：不补的话整条被 continue 掉，她完全不知道有这段视频
            out.append("[视频]")
    return " ".join(x for x in out if x).strip()


def _seg_forward_ids(body) -> list:
    """从消息段列表里挖出嵌套的 forward id（转发套转发）。"""
    out = []
    for s in (body or []):
        if isinstance(s, dict) and s.get("type") == "forward":
            fid = str((s.get("data") or {}).get("id") or "")
            if fid:
                out.append(fid)
    return out


async def _forward_rows(bot, fid: str, *, max_items: int = 20,
                        depth: int = 0, max_depth: int = 2) -> tuple:
    """
    取一条合并转发 → ([(名字, 文本)], 顶层条数)。

    ⚠️ 嵌套转发必须递归：真机 2026-11-12 踩过 ——
    一个 5 条的转发里夹了一条「转发里面包转发」，
    `_seg_plain` 对 forward 段返回空串 → 那一整条被 `continue` 掉，
    日志就是「5 条，取了 4 条」，她完全不知道那条说了什么。
    """
    try:
        data = await bot.get_forward_msg(id=fid)
    except Exception as e:
        logger.warning(f"[FWD] 取转发内容失败 id={fid}: {type(e).__name__}: {e}")
        return [], 0

    msgs = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(msgs, list):
        return [], 0

    rows = []
    for m in msgs[:max_items]:
        if not isinstance(m, dict):
            continue
        sender = m.get("sender") or {}
        name = str(sender.get("card") or sender.get("nickname")
                   or m.get("nickname") or "某人").strip()[:16]
        body = m.get("message") or m.get("content")
        txt = _seg_plain(body)
        subs = _seg_forward_ids(body)
        if txt:
            rows.append((name, txt))
        if not subs:
            continue

        # 这一段里套了转发（可能还带一句话）—— 把里面也摊开
        if depth >= max_depth:
            rows.append((name, "[转发内容，嵌套太深没展开]"))
            continue
        inner = []
        for sid in subs[:3]:
            sub_rows, _sub_n = await _forward_rows(
                bot, sid, max_items=min(max_items, 10),
                depth=depth + 1, max_depth=max_depth,
            )
            inner += sub_rows
        if not inner:
            rows.append((name, "[转发内容取不到]"))
            continue
        for iname, itxt in inner:
            rows.append((f"{name}→{iname}", itxt))
    return rows, len(msgs)


async def _forward_text(event, max_items: int = 20, max_chars: int = 800) -> str:
    """
    「转发聊天记录」（OneBot 的 forward 段）→ 可读文本。

    为什么之前没通：`_enrich_text` 只检查了 json / xml（链接卡片），
    而 QQ 的合并转发是 **forward 段**，plaintext 是空的，
    于是走到主入口 `if not text: return` 被整条丢掉。
    """
    try:
        segs = event.get_message()
    except Exception:
        return ""
    fwd_ids = [str(s.data.get("id") or "") for s in segs if s.type == "forward"]
    fwd_ids = [x for x in fwd_ids if x]
    if not fwd_ids:
        return ""

    try:
        bot = get_bot()
    except Exception:
        bot = None
    if bot is None:
        return ""

    for fid in fwd_ids[:1]:
        rows, total = await _forward_rows(bot, fid, max_items=max_items)
        if not rows:
            continue

        lines, used = [], 0
        for name, txt in rows:
            if used + len(txt) > max_chars:
                break
            lines.append(f"{name}: {txt}")
            used += len(txt)

        if lines:
            logger.info(f"[FWD] 转发聊天记录 {total} 条，展开后 {len(rows)} 条，"
                        f"取了 {len(lines)} 条")
            return (f"【转发的聊天记录，共 {total} 条，下面是前 {len(lines)} 条】\n"
                    + "\n".join(lines))
    return ""


async def _at_text(event: MessageEvent) -> str:
    """
    把 @ 段转成 `@名字(QQ号)` 写进正文。

    ⚠️ 这是 2026-11-12 的一个大坑：`event.get_plaintext()` 只取 text 段，
    **at 段整个被丢掉**。所以「你戳一下 @某人」到她眼里变成「你戳一下」——
    她不知道戳谁，只能从上下文里猜，于是猜了最熟的称呼（= 说话的人自己），
    结果「让她戳 A，她一直戳 B」。

    带上**完整 QQ 号**（不是尾 4 位）：她要拿这个号去 [[[POKE|QQ号]]]，
    尾 4 位不够用。
    """
    try:
        segs = [s for s in event.get_message() if s.type == "at"]
    except Exception:
        return ""
    if not segs:
        return ""

    gid = getattr(event, "group_id", None)
    members = []
    if gid:
        try:
            members = await group_roster.members_of(get_bot(), str(gid))
        except Exception:
            members = []

    out = []
    for s in segs:
        qq = str((s.data or {}).get("qq") or "").strip()
        if not qq:
            continue
        if qq in ("all", "everyone"):
            out.append("@全体成员")
            continue
        name = ""
        for m in members:
            if str(m.get("user_id")) == qq:
                name = str(m.get("card") or m.get("nickname") or "").strip()
                break
        out.append(f"@{name}({qq})" if name else f"@{qq}")
    return " ".join(out)


# ================= 把「空消息」补成有内容的消息 =================
# ================= 引用回复：把被引用的原文取回来 =================
# 引用块统一用 〔〕 包起来，方便别处整块剥掉（判「是不是在叫她」时不该被引用内容干扰）
_QUOTE_BLOCK = re.compile(r"〔引用[^〕]*〕")
QUOTE_MAX_CHARS = tuning.i("QUOTE_MAX_CHARS")


def _quote_from_msg(d, max_chars: int) -> str:
    """把 get_msg 的返回整理成「谁 + 什么时候 + 说了什么」（单行）。"""
    if not isinstance(d, dict):
        return ""
    sender = d.get("sender") or {}
    uid = str(d.get("user_id") or sender.get("user_id") or "")
    name = (str(sender.get("card") or "").strip()
            or str(sender.get("nickname") or "").strip()
            or (f"QQ{uid[-4:]}" if uid else "某人"))
    ts = float(d.get("time") or 0)
    hhmm = time.strftime("%H:%M", time.localtime(ts)) if ts else ""

    body = []
    try:
        for seg in d.get("message") or []:
            if isinstance(seg, dict):
                st = seg.get("type")
                sd = seg.get("data") or {}
            else:
                st = getattr(seg, "type", "")
                sd = getattr(seg, "data", {}) or {}
            if st == "text":
                body.append(str(sd.get("text") or ""))
            elif st == "at":
                body.append("@" + str(sd.get("qq") or ""))
            elif st in ("image", "mface"):
                body.append("[图]")
            elif st == "record":
                body.append("[语音]")
            elif st == "face":
                body.append("[表情]")
            elif st == "video":
                body.append("[视频]")
            elif st in ("json", "xml"):
                # 引用的是分享卡片：把卡片标题 + 链接补回来，
                # 否则「这个视频能看到吗」她手里没 url，没法调 video_understand。
                try:
                    from services.link_preview import card_from_segments
                    card = card_from_segments([seg])
                    if card:
                        _p = [str(card.get("title") or "")]
                        if card.get("url"):
                            _p.append(str(card.get("url")))
                        body.append("[卡片] " + " ".join(x for x in _p if x).strip())
                except Exception:
                    pass
            elif st == "reply":
                # ⚠️ 不递归展开被引用消息里的「引用」段：
                #    多层引用时会把折叠的旧消息一层层读出来（用户 2026-11-23）。
                continue
    except Exception:
        pass
    txt = "".join(body).strip() or str(d.get("raw_message") or "").strip()
    if not txt:
        return ""
    txt = re.sub(r"\s+", " ", txt)
    if len(txt) > max_chars:
        txt = txt[:max_chars] + "…"
    who = name + (f"({uid[-4:]})" if len(uid) >= 4 else "")
    # 只作指代，不是让她复述的素材（用户 2026-11-23）。
    return (f"〔引用 {who}{(' ' + hhmm) if hhmm else ''} 的消息"
            f"（只作指代，回复时不要复述原文）：{txt}〕")


async def _reply_quote(event: MessageEvent, max_chars: int | None = None) -> str:
    """
    入站消息如果是「引用回复」，把被引用那条的原文取回来。

    为什么必须有（2026-11-13 实测事故）：
      用户发了一条 200 字视觉方案（#569883285），然后**引用它**说「帮我看下这个方案」。
      reply 段只带一个 id，原文完全没展开 → 她看到的只有「帮我看下这个方案」，
      「这个方案」指什么根本不知道，只能拿最近的短消息瞎猜（猜成了别人的「5秒一帧」）。
      引用回复是群里最常见的指代方式，不展开等于每句话都缺主语。

    结果缓存到 event 上：`_need_reply` 和主入口都会走 `_enrich_text`，
    不缓存的话同一个 id 会拉两遍 get_msg。
    """
    rid = ""
    try:
        for seg in event.get_message():
            if seg.type == "reply":
                rid = str((seg.data or {}).get("id") or "").strip()
                break
    except Exception:
        rid = ""
    # 有些适配器不把 reply 放进 message 段，只在 event.reply 上给 message_id。
    if not rid:
        try:
            rid = str(getattr(getattr(event, "reply", None),
                              "message_id", "") or "").strip()
        except Exception:
            rid = ""
    if not rid:
        return ""

    ck = f"_quote_{rid}"
    cached = getattr(event, ck, None)
    if cached is not None:
        return cached

    out = ""
    try:
        try:
            _mid = int(rid)
        except (TypeError, ValueError):
            _mid = rid          # 少数适配器的 message_id 是字符串
        d = await get_bot().call_api("get_msg", message_id=_mid)
        out = _quote_from_msg(d, max_chars or QUOTE_MAX_CHARS)
    except Exception as e:
        # 消息太旧被删 / 权限不够 / 号不对 —— 都不该影响主流程
        logger.debug(f"[QUOTE] 取被引用消息失败（{rid}）: {type(e).__name__}: {e}")

    try:
        setattr(event, ck, out)
    except Exception:
        pass
    return out


async def _enrich_text(event: MessageEvent) -> str:
    """
    有些消息的 plaintext 天生是空的：
      · 语音：文字在 record 段的 `text` 字段（QQ 客户端已转写）
      · 转发卡片：文字在 json/xml 段（标题/UP主/简介）
    不补的话，主入口那句 `if not text: return` 会把它们整个丢掉 ——
    「只转发不写字」「只发语音」这两种情况她就完全没反应。

    结果缓存在 event 上：`_need_reply`（群聊）和主入口都要用，
    不缓存会把 B 站接口拉两遍。
    """
    cached = getattr(event, "_enriched_text", None)
    if cached is not None:
        return cached

    t = event.get_plaintext().strip()

    # 👇 @ 段必须单独补回来（get_plaintext 会把 at 整个丢掉）
    #    不补的话「你戳一下 @某人」在她眼里就是「你戳一下」，她只能瞎猜戳谁。
    try:
        _ats = await _at_text(event)
        if _ats:
            t = f"{t} {_ats}".strip() if t else _ats
    except Exception as e:
        logger.debug(f"[AT] 补 @ 信息失败: {type(e).__name__}: {e}")

    # 👇 引用回复：把被引用那条的原文展开（get_plaintext 会把 reply 段整个丢掉）
    #    不展开的话「帮我看下这个方案」里的「这个方案」她完全不知道指什么。
    try:
        _q = await _reply_quote(event)
        if _q:
            t = f"{t} {_q}".strip() if t else _q
            # 把被引用的原文单独存一份：回复里如果原样照抄开头，输出层要剥掉。
            try:
                _qm = re.search(
                    r"消息（只作指代，回复时不要复述原文）：(.+)〕\s*$", _q, re.S)
                if _qm:
                    setattr(event, "_quote_text", _qm.group(1).strip())
            except Exception:
                pass
            logger.info(f"[QUOTE] 展开引用：{_q[:60]}")
    except Exception as e:
        logger.debug(f"[QUOTE] 展开失败: {type(e).__name__}: {e}")

    # 👇 语音要有明确标记：她得知道「这是别人说的话（客户端转写的）」，
    #    而不是「用户打出来的字」——否则容易把转写误差当成对方真这么说。
    vtext = _voice_text(event)
    if vtext:
        t = (f"【这是一条语音消息，QQ 客户端自动转写的内容如下，可能有错字】{vtext}"
             if not t else
             f"{t}\n【这条消息附带一条语音，客户端自动转写的内容如下，可能有错字】{vtext}")

    # ①.5 视频段：plaintext 是空的。
    # 不补标记的话，@她 发一段视频会被主入口 `if not text: return` 整个丢掉 ——
    # 她既不评论也不说"看不了"，对方以为她装死（2026-11-17 审计）。
    # 这里只补「有这段视频」，不假装看到了画面：要真看内容得走 B 站 video_understand。
    try:
        if any(s.type == "video" for s in event.get_message()):
            _vnote = "【对方发了一段视频：你只能看到「有一段视频」，看不到画面内容】"
            t = f"{t}\n{_vnote}" if t else _vnote
    except Exception:
        pass

    # ② 合并转发（聊天记录）：forward 段，plaintext 是空的
    try:
        fwd = await _forward_text(event)
        if fwd:
            t = f"{t}\n{fwd}" if t else fwd
    except Exception as e:
        logger.debug(f"[FWD] 转发解析失败: {type(e).__name__}: {e}")

    # ③ 链接卡片（json/xml 段）
    try:
        if any(s.type in ("json", "xml") for s in event.get_message()):
            from services.link_preview import preview_text
            card = await preview_text(event)
            if card:
                t = (f"{t}\n{card}" if t else card)[:1200]
    except Exception as e:
        logger.debug(f"[LINK] 卡片预览失败: {type(e).__name__}: {e}")

    # ④ 普通网页链接：私聊 / 叫了她 / 明确让看时，抓正文喂给她。
    #    以前只有 B 站卡片预览，普通链接她只能回「我点不开」（用户 2026-11-23）。
    try:
        from services import web_page as _wp
        _urls = [u for u in _wp.find_urls(t) if not _wp.should_skip(u)]
        if _urls:
            # 只给「允许的私聊 / 叫了她 / 主人明确让看」抓，别让群里每个带链接的
            # 闲聊都触发一次外部请求。
            _is_private = (isinstance(event, PrivateMessageEvent)
                           and str(event.user_id) in ALLOWED_PRIVATE)
            try:
                _called = bool(event.is_tome())
            except Exception:
                _called = False
            _asks = bool(re.search(
                r"看|读|打开|做一下|做题|这个链接|网页|文章|链接|内容", t))
            _owner_asks = _asks and is_owner(str(event.user_id))
            if _is_private or _called or _owner_asks:
                _pg = await _wp.fetch_page(_urls[0])
                if _pg.get("text"):
                    t = (f"{t}\n\n【链接内容 · {_pg.get('title') or _urls[0]}】\n"
                         f"{_pg['text']}\n"
                         f"（这是系统帮你抓到的网页正文，直接根据它回答/做事；"
                         f"不要说「点不开」「看不了」。）")
                    logger.info(
                        f"[WEB] 已抓取网页正文 {len(_pg['text'])} 字：{_urls[0][:80]}")
                elif _pg.get("error"):
                    logger.info(
                        f"[WEB] 链接没抓到正文：{_urls[0][:80]} → {_pg['error']}")
    except Exception as e:
        logger.debug(f"[WEB] 网页读取失败: {type(e).__name__}: {e}")

    try:
        setattr(event, "_enriched_text", t)
    except Exception:
        pass
    return t


# ================= 是否该处理这条消息 =================
def _addressed_to_me(event, text: str) -> bool:
    """
    这条是不是**冲她说的**（@ 了她 / 喊了她名字）。

    用途：群聊上下文里给这类消息打 ⭐，让她一眼看出哪几句是在跟她说话。
    用户 2026-11-12：「重点关注艾特它或者叫它的人这些历史消息」——
    以前 10 条上下文平铺，她分不出哪句是对她说的、哪句是群友互相聊，
    接话就容易接错人（「让她戳 A 却戳 B」那类问题也是同一类）。
    """
    try:
        if event.is_tome():
            return True
    except Exception:
        pass
    low = str(text or "").lower()
    if not low:
        return False
    try:
        return any(n and n.lower() in low for n in _my_names())
    except Exception:
        return False


# ================= 指令前缀判定 =================
# 除 `/` 之外，三方插件还常用 `.` 前缀（jinriyunshi 的 rule 就硬编码 `.今日运势`）。
# 2026-11-13 实测：`.今日运势` 被 chat 当成聊天处理 → 她先回一句废话，运势图才出来，
# 一次指令触发两轮回复，那句废话还进了对话上下文，污染后续。
_CMD_HARD = "/／!！"                 # 这些开头一定是指令（正常聊天不会这么起头）
_CMD_DOT_BAD = " .．。、,，…"        # `.` 后面跟这些是省略号/聊天，不是指令


def _looks_like_command(text: str) -> bool:
    """这条消息是不是「指令」（聊天插件该完全放行：不接话、不记上下文）。"""
    if not text:
        return False
    c = text[0]
    if c in _CMD_HARD:
        return len(text) > 1
    if c == ".":
        if len(text) < 2 or text[1] in _CMD_DOT_BAD:
            return False
        # 只认「. + 中文」这种真指令（`.今日运势`）；
        # `.NET` / `.5` 这类不算，免得把正常聊天吃掉
        return "\u4e00" <= text[1] <= "\u9fff"
    return False


# ================= 关注期间：这条后续消息是不是真的在跟她说 =================
# 这块的两次翻车（都要记住）：
#   ① 最初「被 @ 后无条件跟 3 句」→ 群友嫌她一直跟着讲；
#   ② 于是加了一道「这句是不是在跟我说」的模型判定 → 又把**真找她的**拦掉了
#      （2026-11-18 真机：空 @ → 14 秒后「发个表情包」被判「否（不回）」）。
#
# 现在的平衡（用户 2026-11-18 定）：
#   · 误判成「回」的代价 = 多发一条，而且有「3 条额度 + 120s」兜着；
#   · 误判成「不回」的代价 = 用户觉得她装死。
#   → 所以**默认偏向回**，只靠「强否定信号 + 额度 + 连续否」来防纠缠。
#
# 判定顺序（用户 2026-11-18：**能交给模型判的就交给模型**，别用正则猜意图）：
#   ① 叫名字 / 空 @ 的补话（结构信号，不是猜语义）  → 回
#   ② @ 了别人 / 引用回复别人（看消息结构）          → 不回（扣 1 条额度）
#   ③ 刚被 @ 完的第一条后续                        → 回（"叫完她接着说"最常见）
#   ④ 其余全部交给模型，三档：是 / 否 / 不确定 → **只有明确「是」才回**
#   ⑤ 每判一次否扣 1 条额度；连续否 N 次直接结束关注
#
# ⚠️ 2026-11-23 用户：「收件人不是大家、是一个群友的情况下他也回复，接的词不达意」。
#    以前默认「不确定 → 回 / 宁可多回一句」，结果关注期里别人互相聊天她也接。
#    现在反过来：**不确定 = 不回**；收件人明确是别人（→ 某人，置信 ≥0.5）也不回。
#    @ 她 / 喊名字仍然必回（那条不走关注判定，直接 True）。
_FOCUS_JUDGE_ENABLED = os.getenv("FOCUS_JUDGE_ENABLED", "true").lower() == "true"
_FOCUS_JUDGE_TIMEOUT = tuning.f("FOCUS_JUDGE_TIMEOUT")
# 刚被 @ 完的第一条后续是否无条件回。默认关（交给模型判），避免她接别人之间的第一句。
_FOCUS_FIRST_FOLLOW_ALWAYS = os.getenv("FOCUS_FIRST_FOLLOW_ALWAYS", "false").lower() == "true"
# 连续判否几次就结束关注（防"一直跟着讲"）
_FOCUS_NEG_STREAK_STOP = tuning.i("FOCUS_NEG_STREAK_STOP")


def _focus_penalize(session_key: str, qq) -> int:
    """判否：扣 1 条额度 + 记一次「否」；连续否到阈值就直接结束关注。

    关注额度本来是「还能接几条」。判否说明对方已经转去跟别人聊了，
    这条机会就不该再留着 —— 这就是「别一直跟着讲」的刹车。
    返回剩余额度（0 = 关注已结束）。
    """
    f = _focus_clean(session_key, qq)
    if not f:
        return 0
    f["neg"] = int(f.get("neg") or 0) + 1
    f["left"] = int(f.get("left") or 0) - 1
    if f["left"] <= 0 or f["neg"] >= _FOCUS_NEG_STREAK_STOP:
        focus_sessions[session_key].pop(str(qq), None)
        logger.info(
            f"[FOCUS] {session_key} 结束关注 {qq}"
            f"（判否 {f['neg']} 次 / 剩 {max(0, f['left'])} 条）"
        )
        return 0
    logger.info(f"[FOCUS] {session_key} 判否扣额度，{qq} 还剩 {f['left']} 条")
    return f["left"]


def _focus_mark_yes(session_key: str, qq) -> None:
    """判是：把「连续否」计数清零（说明还在跟她聊）。"""
    f = _focus_clean(session_key, qq)
    if f:
        f["neg"] = 0


def _reply_to_other(event, self_id: str) -> bool:
    """这条是不是在**引用回复别人**（不是她）。"""
    try:
        rp = getattr(event, "reply", None)
        if rp is None:
            return False
        uid = str(getattr(getattr(rp, "sender", None), "user_id", "") or "")
        # 引用的可能是旧号（换号前的她）→ 也算「不是别人」
        return bool(uid) and not _is_self_qq(uid, event)
    except Exception:
        return False


def _addressed_elsewhere(event, self_id: str) -> str:
    """明显在找别人：@ 了别人 / @全体 / 引用回复别人。返回原因（空串 = 没有）。"""
    try:
        others = {x for x in _at_targets(event) if not _is_self_qq(x, event)}
        if others:
            return "@了别人"
    except Exception:
        pass
    if _reply_to_other(event, self_id):
        return "引用回复别人"
    return ""


async def _judge_focus_follow(text: str, gid: str, speaker_qq: str, event=None) -> bool:
    """关注期间的一条未 @ 消息：判断要不要接。

    设计原则：**宁可多回一句，也别把找她的漏掉**（额度/连续否负责刹车）。
    """
    t = str(text or "").strip()
    if not t:
        return False
    key = f"group_{gid}"
    sq = str(speaker_qq)

    # ① 直接叫了名字/外号 → 明确是叫她
    if _calls_me_by_name(t, key, sq):
        _focus_mark_yes(key, sq)
        return True

    try:
        _f = _focus_clean(key, sq)
    except Exception:
        _f = None

    # ② 上一条是「空 @」（只 @ 没说内容）→ 这条就是补上的话，必然在跟她说
    if _f and _f.get("pending"):
        _f["pending"] = False
        logger.info(f"[FOCUS] 上一条是空 @，「{t[:20]}」按补话处理 → 回")
        _focus_mark_yes(key, sq)
        return True

    # ③ 强否定：@ 了别人 / 引用回复别人 → 不回（这种"跟着回"才是真的烦人）
    if event is not None:
        _why = _addressed_elsewhere(event, _self_id(event))
        if _why:
            logger.info(f"[FOCUS] 「{t[:20]}」{_why} → 不回")
            _focus_penalize(key, sq)
            return False

    # ③b 收发件人明确是别人 → 不回（用户 2026-11-23）。
    #     「收件人不是大家、是一个群友的情况下他也回复」——群友之间没 @ 也会聊，
    #     光看 @/引用 判不出来；这里直接用 group_scene 已经判好的「谁 → 谁」。
    try:
        _lm = group_scene.last(gid)
        if _lm and not _lm.get("is_bot"):
            _lk = str(_lm.get("to_kind") or "").strip().lower()
            _lq = str(_lm.get("to_qq") or "")
            try:
                _lc = float(_lm.get("to_conf") or 0.0)
            except Exception:
                _lc = 0.0
            if (_lk == "person" and _lq and _lc >= 0.5
                    and not _is_self_qq(_lq, event)):
                _ln = str(_lm.get("to_name") or _lq)
                logger.info(f"[FOCUS] 「{t[:20]}」收件人是 {_ln}（不是你）→ 不回")
                _focus_penalize(key, sq)
                return False
    except Exception as e:
        logger.debug(f"[FOCUS] 收件人检查失败: {type(e).__name__}: {e}")

    # ④ 刚被 @ 完的第一条后续 → 回（"叫完她接着说"最常见）
    if (_FOCUS_FIRST_FOLLOW_ALWAYS and _f
            and int(_f.get("left") or 0) >= FOCUS_FOLLOW_COUNT
            and int(_f.get("neg") or 0) == 0):
        logger.info(f"[FOCUS] 刚被 @ 后的第一条后续「{t[:20]}」→ 回")
        return True

    # ⑤ 剩下的交给模型，三档：是 / 否 / 不确定（只有明确「否」才不回）
    if not _FOCUS_JUDGE_ENABLED:
        return True          # 关掉判定 = 关注期内都接（额度照样兜底）
    if not getattr(llm_mod, "XAI_API_KEY", ""):
        return True          # 没 key 判不了，按"是"处理，别装死
    ctx = list(recent_group_msgs.get(str(gid), []))[:-1][-6:]
    ctx_str = "\n".join(f"- {c}" for c in ctx) or "（无）"
    names = "、".join(sorted(_my_names(key, sq))) or "小鲸鱼"
    prompt = (
        f"你是群里一个叫 {names} 的机器人。刚才有人 @ 过你"
        f"（可能只 @ 了一下、没说话），你在关注他接下来说的几句话。\n"
        f"现在他发了一句（这句没有 @ 你）：{t}\n\n"
        f"最近群聊上下文：\n{ctx_str}\n\n"
        f"判断标准：\n"
        f"· 让你做事（发/来/唱/画/查/说…）、问你问题、接着你上一句往下说 → 是\n"
        f"· 明显在跟别人聊、@ 了别人、在说第三个人 → 否\n"
        f"· 看不出来 / 两种都说得通 → 不确定\n\n"
        f"请只判断：这句话是不是在跟 {names} 说、或是在接着 {names} 刚才的话往下聊？"
        f"只回三个词之一：是 / 否 / 不确定。"
    )
    try:
        import httpx
        kwargs = {"timeout": _FOCUS_JUDGE_TIMEOUT}
        if getattr(llm_mod, "PROXY", None):
            kwargs["proxy"] = llm_mod.PROXY
        async with httpx.AsyncClient(**kwargs) as c:
            r = await c.post(
                f"{llm_mod.XAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {llm_mod.XAI_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": llm_mod.XAI_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.0, "max_tokens": 8},
            )
        if r.status_code != 200:
            logger.warning(f"[FOCUS] 关注判断 HTTP {r.status_code} → 按不回处理")
            _focus_penalize(key, sq)
            return False
        ans = str(r.json()["choices"][0]["message"].get("content") or "").strip()
        if ans.startswith("否"):
            logger.info(f"[FOCUS] 关注期间判断「{t[:30]}」→ 否（不回，扣 1 条）")
            _focus_penalize(key, sq)
            return False
        if ans.startswith("是"):
            logger.info(f"[FOCUS] 关注期间判断「{t[:30]}」→ 是（回）")
            _focus_mark_yes(key, sq)
            return True
        # 不确定 / 乱答 → **不回**（2026-11-23 用户：别人互相聊天别接）。
        # 直接 @ / 喊名字的必回，不依赖这条判定，所以这里保守不会漏。
        logger.info(f"[FOCUS] 关注期间判断「{t[:30]}」→ 不确定（{ans[:8]}）→ 不回")
        _focus_penalize(key, sq)
        return False
    except Exception as e:
        logger.warning(
            f"[FOCUS] 关注判断失败（按不回处理）: {type(e).__name__}: {e}")
        _focus_penalize(key, sq)
        return False


def _is_sticker_msg(event) -> bool:
    """这条消息是表情包（mface，或 image 段带 sub_type=1 / summary 含「表情」）。"""
    try:
        for s in event.get_message():
            if s.type == "mface":
                return True
            if s.type == "image":
                sub = s.data.get("sub_type")
                if sub == 1 or (sub is None and "表情" in str(s.data.get("summary") or "")):
                    return True
    except Exception:
        return False
    return False


def _media_hint(event) -> str:
    """「只发了个表情/图」时补给人话，别让模型看到空字符串。"""
    try:
        segs = list(event.get_message())
    except Exception:
        return ""
    for s in segs:
        if s.type == "mface":
            return "发了一个表情包"
        if s.type == "image":
            sub = s.data.get("sub_type")
            if sub == 1 or (sub is None and "表情" in str(s.data.get("summary") or "")):
                return "发了一个表情包"
    if any(s.type == "image" for s in segs):
        return "发了一张图片"
    if any(s.type == "video" for s in segs):
        return "发了一段视频"
    return ""


# 防抖用的「上一批」状态：session_key -> (时间, 发送者 QQ)
_DEBOUNCE_LAST: dict = {}


def _debounce_wait(session_key: str, event, text: str) -> tuple:
    """
    这批消息要等多久再处理，返回 (秒, 原因)。

    参考 MaiBot 的防抖表（2026-11-19），但保留我们原来的 MERGE_WINDOW 做底：
      · 同一个人续发 → DEBOUNCE_SAME（他可能还没说完）
      · 换人说话   → DEBOUNCE_OTHER（别人插话了，赶紧看）
      · 在叫她     → 压到 DEBOUNCE_AT（越快越好）
      · 裸图       → 至少 DEBOUNCE_BARE_IMAGE（等图片下载完，否则模型只看到「[图片]」）
      · 整批上限   → DEBOUNCE_MAX
    """
    now = time.time()
    author = str(getattr(event, "user_id", "") or "")
    prev = _DEBOUNCE_LAST.get(session_key)
    if prev and now - float(prev[0] or 0) <= 30:
        same = (author == str(prev[1] or ""))
        wait = DEBOUNCE_SAME if same else DEBOUNCE_OTHER
        why = "同人续发" if same else "换人"
    else:
        wait = MERGE_WINDOW
        why = "安静开批"
    try:
        if _media_only(event, text):
            wait = max(wait, DEBOUNCE_BARE_IMAGE)
            why += "｜裸图等下载"
    except Exception:
        pass
    try:
        if getattr(event, "is_tome", lambda: False)():
            wait = min(wait, DEBOUNCE_AT)
            why += "｜在叫她"
    except Exception:
        pass
    wait = max(0.0, min(wait, DEBOUNCE_MAX))
    _DEBOUNCE_LAST[session_key] = (now, author)
    return wait, why


def _focus_take_pending(session_key: str, qq) -> bool:
    """上一条是「空 @」（pending）→ 返回 True 并清掉标记（只用一次）。"""
    f = _focus_clean(session_key, qq)
    if f and f.get("pending"):
        f["pending"] = False
        return True
    return False


def _media_only(event, text: str) -> bool:
    """
    这条消息**只有**图片/视频/文件，没有任何可读文字。

    为什么要单独判（用户 2026-11-19）：
      识图走 vision_agent（它拿到描述后自己入队）；但 vision 会因为
      冷却 / 同一张图去重 / 免打扰而跳过，这时 chat 这条线就会接手 ——
      模型只看得到「[图片]」占位符，回出来就是
      「我白饭都吃完了，怎么图还没加载出来」这种废话，和正常回复一起发出去 = 一张图两条。
    """
    if str(text or "").strip():
        return False
    try:
        kinds = {s.type for s in event.get_message()}
    except Exception:
        return False
    return bool(kinds & {"image", "video", "file"}) and "record" not in kinds


_CMD_GUARD_WARNED = {"done": False}


def _cmd_hint_of(text: str) -> tuple:
    """
    「这个 /命令 到底存不存在」→ (分类, 给模型看的提示)。

    ⚠️ 故意**延迟导入**：`services/cmd_guard.py` 是 2026-11-19 新加的文件，
    部署时漏传过一次，结果 `plugins/chat` 整个 import 失败（整个聊天插件都没加载）。
    现在缺文件只会退化成「所有命令都当存在」（老行为），不会连累聊天主流程。
    """
    try:
        from services import cmd_guard
        _uc = cmd_guard.classify(text)
        return _uc, (cmd_guard.hint_for(_uc) if _uc else "")
    except Exception as e:
        if not _CMD_GUARD_WARNED["done"]:
            _CMD_GUARD_WARNED["done"] = True
            logger.warning(
                f"[CMD] cmd_guard 不可用（{type(e).__name__}: {e}）→ "
                f"命令识别降级为老行为。检查 services/cmd_guard.py 是否上传。")
        return "", ""


async def _early_observe(event: MessageEvent) -> None:
    """破冰 + 心情观察。

    ⚠️ 2026-11-20 审计（C1/D3）：这两件事原来写在 `_need_reply` 里，而
    `_need_reply` 是 chat matcher 的 Rule —— 任何 priority<10 && block=True
    的插件（vision_agent p5 / sticker_intent p2 / 各种 on_command）一命中，
    Rule 根本不执行 → 用户道歉了她还在冷战、图片消息永远不判心情。
    现在拆成独立的 priority=1、block=False 观察器，先于所有插件执行。
    """
    text = event.get_plaintext().strip()
    try:
        _sk = (f"group_{event.group_id}" if isinstance(event, GroupMessageEvent)
               else f"private_{event.user_id}")
        _thawed = chat_switch.maybe_thaw(_sk, str(event.user_id), text)
        if _thawed:
            logger.info(f"[SWITCH] 破冰：{event.user_id} 道歉/哄她 → 解除安静"
                        f"（原来的原因：{_thawed}）")
            try:
                event._thaw_note = _thawed
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"[SWITCH] 破冰判断失败: {e}")
    try:
        if isinstance(event, GroupMessageEvent):
            _maybe_mood_watch(f"group_{event.group_id}", str(event.group_id),
                              text, display_name(event), str(event.user_id))
    except Exception as e:
        logger.debug(f"[MOOD-WATCH] 触发失败: {type(e).__name__}: {e}")
    # 对话原文落盘（唯一权威源）。放在 priority=1 的观察器里，
    # 这样任何 priority<10 && block=True 的插件都不会让它丢上下文。
    try:
        _t = await _enrich_text(event)
        _self = _self_id(event)
        if isinstance(event, GroupMessageEvent):
            _gid = str(event.group_id)
            _uid = str(event.user_id)
            if _gid in ALLOWED_GROUPS and not _is_self_qq(_uid, event):
                # 👇 群聊分段：每条先记进当前段，worker 等这一段说完再一起处理。
                #    这样不会「高频率提问时一条一回」，心情/收发件人也按整段判。
                try:
                    segments.push(
                        f"group_{_gid}", event, _t,
                        name=display_name(event), uid=_uid,
                        mentioned=bool(getattr(event, "is_tome", lambda: False)()),
                        reply_id=str(getattr(event, "reply", None) or ""),
                        gid=_gid,
                        # mid 用来回填「谁 → 谁」：这里还拿不到收发件人，
                        # `_need_reply` 里 group_scene.observe 之后会 set_addressee
                        mid=str(getattr(event, "message_id", "") or ""))
                except Exception as _se:
                    logger.debug(f"[SEGMENT] 入段失败: {type(_se).__name__}: {_se}")
                dialogue_log.log_msg(
                    _gid, _msg_label(event), _uid, _t,
                    kind="peer" if _uid in referee.PEER_QQS else "user")
                # 群友档案「挖矿」：攒够发言数就在后台提炼一次。
                # ⚠️ 这个模块 2026-11-20 就写好了，但一直没人调用 ——
                #    所以「群友档案只有称呼、没有内容和事情」的问题其实没被修上。
                try:
                    if profile_miner.note_message(_gid, _uid):
                        asyncio.create_task(profile_miner.mine(_uid, _gid))
                except Exception as e:
                    logger.debug(f"[PROFILE-MINE] 触发失败: {type(e).__name__}: {e}")
        elif isinstance(event, PrivateMessageEvent):
            _uid = str(event.user_id)
            if _uid in ALLOWED_PRIVATE and not _is_self_qq(_uid, event):
                dialogue_log.log_msg(f"private_{_uid}", _msg_label(event),
                                     _uid, _t, kind="user")
    except Exception as e:
        logger.debug(f"[EARLY] 对话落盘失败: {type(e).__name__}: {e}")
    # 有人聊天 → 心情空闲衰减重新计时。
    # 放在观察器里（而不是 _need_reply），这样 block=True 的插件命中时也不会漏掉。
    try:
        if isinstance(event, GroupMessageEvent):
            mood.touch(f"group_{event.group_id}")
        elif isinstance(event, PrivateMessageEvent):
            mood.touch(f"private_{event.user_id}")
    except Exception:
        pass


async def _need_reply(event: MessageEvent) -> bool:
    text = event.get_plaintext().strip()
    # 破冰 / 心情观察已移到 _early_observe（priority=1, block=False），
    # 这里只做「回不回」的纯判断，避免被上游 block 插件打断。
    # 👇 核心拦截：指令一律不接，直接放行给其他插件。
    #    指令本身不进对话上下文（噪音），但**「谁用了什么功能」要留一条**：
    #    否则群友抽完运势 / 抽完小猪来跟她聊，她连"你们在说什么"都不知道，
    #    只能反问「猪猪是啥」（用户 2026-11-18 反馈）。
    if _looks_like_command(text):
        # 👇 先确认这条命令**真的存在**（用户 2026-11-19：有人 @ 她发
        #    `/搜图151768172`，既没这个命令、参数还少了空格 → 没人处理 → 她装死）。
        #    认识 → 放行给别的插件；不认识 → 当成普通消息，让她像人一样回一句。
        _uc, _uhint = _cmd_hint_of(text)
        if _uc:
            # ⚠️ 提示只挂在这个 event 上，不写进 _enriched_text ——
            #    否则它会跟着消息进群聊上下文/记忆，后面几轮都带着一句"系统提示"。
            try:
                event._cmd_hint = _uhint
            except Exception:
                pass
            logger.info(f"[CMD] 不认识的命令「{text[:24]}」→ 按普通消息处理（{_uc}）")
        else:
            try:
                if isinstance(event, GroupMessageEvent):
                    _cg = str(event.group_id)
                    _cuid = str(event.user_id)
                    # 主人自己的诊断指令不进上下文（/全量测试、/测函数 会刷屏）
                    _diag = text.startswith(("/全量测试", "/测函数", "/接入自检", "/自检"))
                    if (_cg in ALLOWED_GROUPS and not _diag
                            and not _is_self_qq(_cuid, event)):
                        recent_group_msgs[_cg].append(
                            f"{_ts_tag(time.time())}{_msg_label(event)}: "
                            f"【用了功能】{text[:40]}")
                        recent_group_ts[_cg].append(time.time())
            except Exception:
                pass
            return False
    # 👇 纯媒体消息（只有图片/视频/文件，没有字）在 chat 这条路上**拿不到任何内容**：
    #    识图由 vision_agent 带着识别结果自己入队。这里要是也回，模型只看到「[图片]」，
    #    就会说「我白饭都吃完了，怎么图还没加载出来」——一张图回两条（用户 2026-11-19）。
    #    注意：@ 她的图片走 active_vision（priority=5, block=True），不经过这里。
    if _media_only(event, text):
        _mkey = f"group_{event.group_id}" if isinstance(event, GroupMessageEvent) else ""
        # 例外：他刚「空 @」过她（pending），紧接着补了个表情包 ——
        # 这就是他要给她看的东西，不回等于装死（用户 2026-11-19 实测：
        # 戳一下 → @ 一下 → 发个表情 → 她彻底不理了）。
        if _mkey and _is_sticker_msg(event) and _focus_take_pending(_mkey, event.user_id):
            try:
                event._enriched_text = "【对方发了一个表情包，没有说别的话】"
            except Exception:
                pass
            logger.info("[MEDIA] 空 @ 之后补了个表情 → 按「补话」处理，回一句")
        else:
            # 👇 只记一条占位进「群聊现场」：图/视频/文件本身不回，但**前因要留下**。
            #    不然下一句「这猫躺得比我还会摆烂」在审核眼里是凭空冒出来的。
            #    no_audit=True：占位没有字可判收件人，不浪费一次审核调用。
            try:
                _mgid = str(getattr(event, "group_id", "") or "")
                _self_m = _self_id(event)
                # 她自己发出去的图（NapCat 回显）不算「群友发了张图」
                if (_mgid and _mgid in ALLOWED_GROUPS
                        and not _is_self_qq(str(event.user_id), event)):
                    _rp_m = getattr(event, "reply", None)
                    group_scene.observe(
                        _mgid,
                        mid=str(getattr(event, "message_id", "") or ""),
                        qq=str(event.user_id),
                        name=profiles.call_of(str(event.user_id),
                                              display_name(event), _mgid)
                        or _msg_label(event),
                        text=_media_placeholder(event) or "【媒体消息】",
                        reply_mid=str(getattr(_rp_m, "message_id", "") or ""),
                        is_bot=False, bot_qq=_self_m, names=_scene_names(_mgid),
                        no_audit=True,
                    )
            except Exception as e:
                logger.debug(f"[MEDIA] 现场占位失败: {type(e).__name__}: {e}")
            logger.debug("[MEDIA] 纯媒体消息 → 交给 vision_agent，chat 不单独回")
            return False
    sleep_now = _in_sleep_hour()

    if isinstance(event, GroupMessageEvent):
        gid = str(event.group_id)
        uid = str(event.user_id)
        # NapCat 会把她自己发的消息也回显成事件 —— 那条不能算进高刷计数，
        # 否则她自己连回几句就把群判成「刷屏」了。
        _is_self = _is_self_qq(uid, event)
        # 👇 重启后的第一条消息：先把最近 N 条从对话日志补回现场，**再**记这一条。
        #    顺序反了的话，补回来的历史是拿「未来消息」推线程的，会多出一条假线程
        #    （用户 2026-11-19：重启后 /测函数 现场 只有「窗口 1 条」）。
        #    正常运行时现场有几十条，ensure_seeded 里第一行就 return，零成本。
        if gid in ALLOWED_GROUPS and not _is_self:
            try:
                group_scene.ensure_seeded(gid)
            except Exception as e:
                logger.debug(f"[SCENE] 补现场失败: {type(e).__name__}: {e}")
        # ⚠️ 不要按时间清空上下文（用户 2026-11-19）：
        #   A 说「你们发现英伟达跌了吗」→ 10 分钟后 B「对啊，都是老黄的锅」，
        #   清空后她只看到后半句，等于没有上下文。改成插一条「隔了 N 分钟」分隔线，
        #   时间信息交给模型自己判断（每行本来就带时间戳）。
        _silent = time.time() - last_group_activity.get(gid, 0)
        if _silent > SILENT_TIMEOUT:
            try:
                recent_group_msgs[gid].append(
                    f"——（群里安静了 {int(_silent // 60)} 分钟，下面是接着上面说的）——")
                recent_group_ts[gid].append(time.time())   # 两个 deque 必须等长
            except Exception:
                pass

        # 语音转写 + 转发卡片预览（结果会缓存到 event 上，主入口直接复用）
        t = await _enrich_text(event)
        # 判「是不是在叫她」时把引用块剥掉：
        # 引用别人的话里出现她名字 ≠ 在叫她（否则别人转述一句就被莫名唤醒）
        _t_own = _QUOTE_BLOCK.sub("", t).strip() or t

        # 👇 群里另一只 bot：它的回复要进上下文（她得知道对方说了什么），
        #    但**默认不触发我们回复**，否则两个 bot 会互聊到天亮。
        #    真正的用途是拿它做三方核对，见 services/referee.py。
        _is_peer = uid in referee.PEER_QQS

        if _is_self:
            # 她自己（以及本账号上其它插件）发出去的话：
            # 统一记成「这是你之前的回复：…」，并和 worker 手动记的那条去重。
            # 以前这里按普通群友记成「小鲸鱼(123456789): xxx」，模型分不清是不是自己说的。
            _t_self = t or _media_placeholder(event)
            if _t_self and gid in ALLOWED_GROUPS:
                _append_self_line(gid, _t_self, time.time())
            try:
                _brp = getattr(event, "reply", None)
                # 她自己的话统一记成人格 ID（换号后日志/现场仍能连上）
                _me = _self_id(event)
                group_scene.observe(
                    gid, mid=str(getattr(event, "message_id", "") or ""),
                    qq=_me or uid, name="你", text=_t_self, is_bot=True,
                    reply_mid=str(getattr(_brp, "message_id", "") or ""),
                    bot_qq=_me or uid, names=_scene_names(gid),
                )
            except Exception:
                pass
            last_group_activity[gid] = time.time()
            # 记完就结束：自己的消息不该走后面的「要不要回复」判断
            # （否则她自己的话里带名字时会给自己开一个关注会话，把插话也堵掉）
            return False
        elif t and (gid in ALLOWED_GROUPS or gid in referee.PEER_GROUPS):
            _label = _msg_label(event)
            _now_ts = time.time()
            # 👇 群聊现场：补「谁 → 谁」，按回复链/提及/参与者聚线程
            try:
                _rp = getattr(event, "reply", None)
                _rq = str(getattr(getattr(_rp, "sender", None), "user_id", "") or "")
                _rmid = str(getattr(_rp, "message_id", "") or "")
                _atq = _canon_at([q for q in _at_targets(event)
                                  if q not in ("all", "everyone")])
                _me = _self_id(event)
                if event.is_tome():
                    _atq = [_me] + [q for q in _atq if q != _me]
                if _rq and _is_self_qq(_rq, event):
                    _rq = _me
                _scene_meta = group_scene.observe(
                    gid,
                    mid=str(getattr(event, "message_id", "") or ""),
                    qq=uid,
                    name=profiles.call_of(uid, display_name(event), gid) or _label,
                    text=_t_own,
                    to_qq=(_atq[0] if _atq else _rq),
                    to_reason=("at" if _atq else ("reply" if _rq else "")),
                    reply_mid=_rmid,
                    is_bot=False,
                    bot_qq=_me,
                    names=_scene_names(gid),
                )
                # 👇 回填分段的「谁 → 谁」：分段在 priority=1 就入 buffer 了，
                #    那时候收发件人还没算出来（用户 2026-11-22：
                #    「三方判心情 / 上下文分段都没用上收发件人标签」）。
                try:
                    segments.set_addressee(
                        f"group_{gid}",
                        str(getattr(event, "message_id", "") or ""),
                        to_kind=str((_scene_meta or {}).get("to_kind") or ""),
                        to_name=str((_scene_meta or {}).get("to_name") or ""),
                        to_qq=str((_scene_meta or {}).get("to_qq") or ""),
                        to_conf=float((_scene_meta or {}).get("to_conf") or 0.0))
                except Exception:
                    pass
                # 🌙 白月光统计（群友之间，不是她自己）：
                #    A @ 了谁 / 引用回复了谁 / 没 @ 但在接谁的话。
                try:
                    from services import white_moon as _wm
                    _from_name = (profiles.call_of(uid, display_name(event), gid)
                                  or _label)
                    _to_qqs = [q for q in (_atq or [])
                               if q and not _is_self_qq(q, event)]
                    for _tq in _to_qqs:
                        _tn = profiles.call_of(str(_tq), "", gid) or str(_tq)
                        _wm.note_incoming(
                            gid, uid, _from_name, str(_tq), _tn,
                            kind="at", self_qq=_me)
                    if _rq and not _is_self_qq(_rq, event):
                        _tn = profiles.call_of(str(_rq), "", gid) or str(_rq)
                        _wm.note_incoming(
                            gid, uid, _from_name, str(_rq), _tn,
                            kind="quote", self_qq=_me)
                    _mk = str((_scene_meta or {}).get("to_kind") or "")
                    _mq = str((_scene_meta or {}).get("to_qq") or "")
                    if (_mk == "person" and _mq and _mq not in _to_qqs
                            and _mq != str(_rq or "")
                            and not _is_self_qq(_mq, event)):
                        _mn = str((_scene_meta or {}).get("to_name") or _mq)
                        _wm.note_incoming(
                            gid, uid, _from_name, _mq, _mn,
                            kind="addressed", self_qq=_me)
                except Exception as _e:
                    logger.debug(f"[MOON] 统计失败: {type(_e).__name__}: {_e}")
            except Exception:
                pass
            # 👇 表达学习：每攒够 N 条真人消息，后台挖一次「这个群 / 这个人怎么说话」。
            #    学的是**语气和句式**（和 lorebook 的知识互补），失败静默，不影响聊天。
            try:
                if expression.enabled():
                    _EXP_COUNT[gid] = int(_EXP_COUNT.get(gid) or 0) + 1
                    if _EXP_COUNT[gid] >= expression.EVERY:
                        _EXP_COUNT[gid] = 0
                        _erows = [
                            (m.get("name") or m.get("qq") or "?",
                             str(m.get("qq") or ""), str(m.get("text") or ""))
                            for m in group_scene.recent_messages(gid, 30)
                            if not m.get("is_bot") and str(m.get("text") or "").strip()
                        ]
                        if len(_erows) >= 8:
                            asyncio.create_task(expression.mine(gid, _erows))
            except Exception:
                pass
            # 👇 行尾带上消息号：她要回「前面某条」时才能引用回复。
            #    用户 2026-11-11：「@ 她那句被刷上去了，就引用回复，NapCat 权限已经给了」。
            #    格式 `【时间】小明(1234)#5678: 文本` —— 后面所有解析都是按 ": " 切的，不受影响。
            _mid = str(getattr(event, "message_id", "") or "")
            _tag = f"#{_mid}" if _mid else ""
            # ⭐ = 这条在叫她（@ 或喊名字）。放在名字**前面**，
            # 这样 `...: 文本` 那套按 ": " 切的解析完全不受影响。
            _star = ("⭐" if (_addressed_to_me(event, t) and not _is_self) else "")
            recent_group_msgs[gid].append(
                f"{_ts_tag(_now_ts)}{_star}{_label}{_tag}: {t}")
            recent_group_ts[gid].append(_now_ts)
            # 高刷判定用：按**会话**记，且**不记 NapCat 自己发的那条**。
            # 为什么按会话：每个群、每个私聊各算各的 ——
            #   一个群刷屏只能清这个群的队列，绝不能牵连别的群和别人。
            if not _is_self:
                burst_msg_ts[f"group_{gid}"].append(_now_ts)
                # 记住每个人最近说过的话（跨 8 条窗口），被 @ 时用得上
                try:
                    # 只留他自己打的字：引用块剥掉、只取第一行 —— 否则 60 字配额
                    # 会被引用的原文占满，speaker_recent 就看不到他本人说了什么。
                    _brief = _QUOTE_BLOCK.sub("", t.split("\n")[0]).strip()[:60]
                    speaker_recent[gid][uid].append(f"{_ts_tag(_now_ts)}{_brief}")
                except Exception:
                    pass
            last_group_activity[gid] = time.time()
            if not _is_self:
                last_group_speaker[gid] = uid
                # 心情 touch 已移到 _early_observe（不再依赖 Rule 是否执行）
            # 👇 群友在嫌弃她（「真烦人」「能不能关了先」）→ 这个群静默一会儿。
            #    只压主动开口，@ 她 / 喊名字照常回。
            #    弱信号（「闭嘴」这种）必须带一条"冲她"的证据，否则 A 让 B 闭嘴也会误伤。
            if not _is_self:
                _bt = bot_reply_times.get(gid) or []
                _replied_recently = bool(_bt) and (
                    int(time.time() * 1000) - int(_bt[-1]) < 120000
                )
                _note_dislike(
                    gid, t,
                    to_me=event.is_tome(),
                    called_me=_calls_me_by_name(_t_own, f"group_{gid}", uid),
                    replied_recently=_replied_recently,
                )
            # 对话落盘已移到 _early_observe（priority=1, block=False），
            # 这里不再写，避免上游 block 插件导致上下文丢失（2026-11-20 D1）。

        if _is_peer:
            # 场景③：两条鱼对话。绝大多数不回，偶尔接一句，别让它显得像死机。
            # 加冷却：它连发 3 条时不要每条都掷骰子，否则会连着回三次。
            global _last_peer_reply_ts
            _now = time.time()
            if (_now - _last_peer_reply_ts > PEER_REPLY_COOLDOWN
                    and random.random() < PEER_REPLY_PROB):
                _last_peer_reply_ts = _now
                logger.info(f"[PEER] {gid} 偶尔接一句那只鱼的话")
                return True
            return False

        # 👇 有人 @ 了那只鱼（没 @ 我们）→ 记下问题 + 上下文，
        #    等它答完用我们这一套跑「影子回答」做核对（不发群）。
        if t and gid in referee.PEER_GROUPS and (_at_targets(event) & referee.PEER_QQS):
            referee.note_peer_question(
                gid, t,
                context=list(recent_group_msgs.get(gid, []))[-referee.CTX_LINES:],
                speaker_qq=uid,
                speaker_name=_msg_label(event),
            )

        if sleep_now:
            return False
        if gid not in ALLOWED_GROUPS:
            return False

        # 👇 安静闸门（2026-11-20 审计 B1）：
        #    以前这里完全不查 chat_switch，安静期间每条消息仍然入队、仍然调一次模型
        #    （只是 llm.py 内部强制 stay_silent）→ 白烧 token + 延迟。
        #    上下文/记忆在上面已经记完，所以这里直接不入队不影响「她照常读」。
        try:
            _off, _why = chat_switch.is_off(f"group_{gid}", uid)
            if _off:
                _strict = chat_switch.is_strict(f"group_{gid}", uid)
                if event.is_tome() and chat_switch.ANSWER_WHEN_CALLED and not _strict:
                    logger.info(f"[SWITCH] 安静中，但被 @ → 给一次回应机会（{_why}）")
                else:
                    logger.info(f"[SWITCH] 安静中 → 不入队（{_why}）")
                    return False
        except Exception as e:
            logger.debug(f"[SWITCH] 查询安静失败: {type(e).__name__}: {e}")

        # 👇 LATER 等消息期间已经读过的消息，不再单独回一次（用户 2026-11-12：
        #    「拉了未来记录的时候实际上是已读然后总结回复之前那条了。
        #      之后如果并非艾特就不要再对新的一句再回一次了。」）。
        #    她等 15 秒时把这几句读进上下文、并在这轮回复里一起照顾到了；
        #    之后它们再各自触发一次完整回复 = 同一件事回两遍。
        #    **@ 她的一定要回**，所以只拦非 @ 的。
        if (not event.is_tome()
                and float(getattr(event, "time", 0) or 0)
                <= llm_mod.later_seen_until(gid)):
            logger.info(
                f"[LATER] {gid} 消息 {getattr(event, 'message_id', '')} "
                f"她等消息时已经读过 → 不再单独回一次"
            )
            return False
        # 👇 纯问候不接（「早安」「晚安啦」「睡了」）——
        #    很多群夜里只剩这个，她一句句接等于把人吵醒。
        # ⚠️ 但 @ 她的不拦（2026-11-17 审计）：@ 一句「晚安」是明确的社交动作，
        #    大概率就是想被回一句；一刀切会显得很没礼貌。交给模型判断。
        if SKIP_PURE_GREETING and not event.is_tome() and _is_pure_greeting(t):
            logger.debug(f"[GREET] {gid} 纯问候，不回：{t[:20]}")
            return False
        # 👇 操作性短指令不接（「扫扫」「来了」「发一下」）——
        #    群里在互相配合做事，她插一句就是干扰。@ 她时不受影响。
        if SKIP_OP_SHORT and not event.is_tome() and _is_op_short(t):
            logger.debug(f"[OP] {gid} 操作性短指令，不接：{t[:12]}")
            return False
        # 👇 收尾语不接：别人已经说「算了 / 先这样 / 拜拜」了，再接就是硬凑
        if SKIP_CLOSING and not event.is_tome() and _is_closing(t):
            logger.debug(f"[CLOSE] {gid} 收尾语，不接：{t[:12]}")
            return False
        if GROUP_NEED_AT and not event.is_tome():
            # 👇 关注会话：被 @ 之后的后续几条，即使没 @ 也可能接，
            #    但要结合上下文先判断是不是在跟她说，不能每条都答（用户 2026-11-17）。
            if _focus_active(f"group_{gid}", event.user_id):
                # 把 event 一起传进去：要判「这条是不是 @ 了别人 / 引用别人」，
                # 只看文本是判不出来的（这是上次把真找她的拦掉的另一半原因）。
                return await _judge_focus_follow(
                    _t_own, gid, str(event.user_id), event=event)
            # 👇 直接叫她名字/外号（「小鲸鱼你觉得呢」）→ 等同被 @，
            #    并开启三句关注，这样她接完还能继续跟上
            if _calls_me_by_name(_t_own, f"group_{gid}", str(event.user_id)):
                _focus_start(f"group_{gid}", event.user_id)
                logger.info(f"[FOCUS] {gid} 被叫名字触发关注（{event.user_id}）")
                return True
            return False
        return True

    if isinstance(event, PrivateMessageEvent):
        # 👇 私聊 24 小时都在：用户 2026-11-11「私聊没限制，可以随时聊」。
        #    以前这里跟群聊共用 sleep_now，半夜找她永远是静默的。
        #    真要半夜也静默，设 PRIVATE_SLEEP=true。
        if sleep_now and PRIVATE_SLEEP:
            return False
        if str(event.user_id) in ALLOWED_PRIVATE:
            # 私聊也单独算高刷（同一个人连发 4 条，队列同样会堆），
            # 但只影响这一个私聊会话，跟任何群、任何人无关。
            if not _is_self_qq(str(event.user_id), event):
                burst_msg_ts[f"private_{event.user_id}"].append(time.time())
                # 私聊原文落盘 / mood.touch 已移到 _early_observe（D1）
            # 安静闸门（私聊）：照记上下文，但不入队、不调模型。
            try:
                _poff, _pwhy = chat_switch.is_off(
                    f"private_{event.user_id}", str(event.user_id))
                if _poff:
                    logger.info(f"[SWITCH] 私聊安静中 → 不入队（{_pwhy}）")
                    return False
            except Exception:
                pass
            return True
        return False

    return False


def _bili_url_in(text: str) -> str:
    m = re.search(
        r"https?://(?:b23\.tv|[a-z.]*bilibili\.com/video)/[^\s\]〕】，。！？、]+",
        str(text or ""),
    )
    return m.group(0) if m else ""


# ================= 独立表情收集器（不依赖「要不要回复」） =================
# 根因（2026-11-17）：try_collect_sticker 原来只挂在 chat 主 handler 里，
#   而主 handler 被 _need_reply 拦着 —— 群里没 @ 她就根本不回复，于是也根本不收表情。
#   这里单独开一个 on_message（block=False），只要有可收集的表情段就收，
#   收完照常放行给识图/聊天，互不干扰。
def _has_collectible_sticker(event) -> bool:
    try:
        for seg in event.get_message():
            if seg.type == "mface":
                return True
            if seg.type == "image":
                sub = seg.data.get("sub_type")
                if sub == 1 or (sub is None and "表情" in str(seg.data.get("summary") or "")):
                    return True
        return False
    except Exception:
        return False


def _can_collect_here(event) -> bool:
    """只在她真正活跃的范围收（和回复范围一致），别把无关群的图也吞进表情库。"""
    if isinstance(event, PrivateMessageEvent):
        return str(getattr(event, "user_id", "") or "") in ALLOWED_PRIVATE
    if isinstance(event, GroupMessageEvent):
        return str(getattr(event, "group_id", "") or "") in ALLOWED_GROUPS
    return False


def _has_mface(event) -> bool:
    """商城表情（mface）—— 没有图片可看，收不收不影响聊天，直接收最省事。"""
    try:
        return any(s.type == "mface" for s in event.get_message())
    except Exception:
        return False


def _sticker_collect_rule(event) -> bool:
    # 别收自己发出去的表情（NapCat 会把 bot 自己的消息回显成事件）
    if _is_self_qq(getattr(event, "user_id", ""), event):
        return False
    if not (_can_collect_here(event) and _has_collectible_sticker(event)):
        return False
    # 👇 2026-11-19：图片表情包先给她看，收不收由她调 sticker_save 决定。
    #    以前是「识图之前就自动收」—— 她连看都没看到就进了库（老流程先决策）。
    #    商城表情 mface 没有画面可看，仍然直接收。
    if STICKER_DECIDE == "llm" and not _has_mface(event):
        return False
    return True


sticker_collect = on_message(rule=Rule(_sticker_collect_rule), priority=4, block=False)


@sticker_collect.handle()
async def _(bot: Bot, event: MessageEvent):
    try:
        await try_collect_sticker(bot, event, event.get_message())
    except Exception as e:
        logger.debug(f"[STICKER] 独立收集器异常: {type(e).__name__}: {e}")


# 破冰 + 心情观察：priority=1、block=False，先于所有插件执行。
# 这样 vision_agent(p5) / sticker_intent(p2) / 各种 on_command 命中时，
# 破冰和心情观察也不会被跳过（2026-11-20 审计 C1/D3）。
early_observe = on_message(priority=1, block=False)


@early_observe.handle()
async def _early_observe_handler(event: MessageEvent):
    try:
        await _early_observe(event)
    except Exception as e:
        logger.debug(f"[EARLY] 观察失败: {type(e).__name__}: {e}")


chat = on_message(rule=Rule(_need_reply), priority=10, block=False)

# 2026-11-20 工程结构化：把实现注册到 core，供新插件统一调用
try:
    from core import pipeline as _pipeline
    from core import llm_gateway as _gateway
    _pipeline.set_observer(_early_observe)
    _pipeline.set_decider(_need_reply)
    _gateway.register(call_llm)
    from services import llm_usage as _lu
    _gateway.register_usage_hook(
        lambda usage, purpose="chat", session="": _lu.record_from_response(
            {"usage": usage}, purpose=purpose, session=session))
    from services import plugin_registry as _pr
    _pr.register_all()
except Exception as _e:  # core 缺失时不影响主流程
    logger.debug(f"[CORE] 注册失败: {type(_e).__name__}: {_e}")


# ================= /表情统计 =================
emoji_stats_cmd = on_command("表情统计", priority=5, block=True)


@emoji_stats_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    from services.sticker_receiver import STATS
    from services.sticker_library import count_importable

    s = sticker_lib.stats()
    usable = s.get("usable", 0)
    broken = s.get("broken_file", 0)

    if s["total"] == 0:
        await bot.send(event, "我的表情库还是空空的…要不你发我几张喜欢的表情？")
        return

    lines = ["📊 我的表情库现状："]
    lines.append(f"· 总共有 {s['total']} 张（图片 {s['image']} + 商城 {s['mface']}）")
    lines.append(f"· 上限 {s['limit']} 张，已用 {s['usage']}%，精品 {s['premium']} 张")
    lines.append(f"· 能发出去 {usable} 张" + (f"，坏数据 {broken} 张" if broken else ""))
    if s["created_today"]:
        lines.append(f"· 今天新收了 {s['created_today']} 张")
    lines.append(f"· 累计用过 {sticker_lib.total_used()} 次")
    if STATS.get("sent_today"):
        lines.append(f"· 今天我发了 {STATS['sent_today']} 次")

    # 👇 关键诊断：库存里有但发不出去
    if usable == 0:
        lines.append("")
        lines.append("⚠️ 一张都发不出去！索引里的文件都不存在（多半是旧测试残留）。")
        lines.append("   修：python scripts/import_stickers.py --clean-broken")
    elif broken:
        lines.append("")
        lines.append(f"⚠️ 有 {broken} 条是坏数据（文件已被删），会自动跳过不参与挑选。")

    # 👇 关键诊断：素材在那儿但没导入
    n_importable, where = count_importable()
    if n_importable:
        lines.append("")
        lines.append(f"📦 发现 {n_importable} 张还没入库的素材：{'、'.join(where)}")
        lines.append("   这些只是文件，不入库等于没有。跑：")
        lines.append("   python scripts/import_stickers.py --src data/memes --tag-from llm --in-place")

    tag_items = sorted(s["by_tag"].items(), key=lambda x: -x[1]["count"])
    if tag_items:
        lines.append("")
        lines.append("🎨 标签分布：")
        for tag, info in tag_items[:10]:
            top_mark = "🔥" if info["top_used"] >= 3 else ""
            lines.append(f"· {tag} × {info['count']} 张 {top_mark}")

    pending = list_pending(event.get_session_id())
    if pending:
        lines.append("")
        lines.append(f"🗂️ 还有 {len(pending)} 张暂存着等我挑：")
        for i, p in enumerate(pending[:5], start=1):
            desc = p.get("desc") or "看不出来是啥"
            lines.append(f"  {i}. {desc[:40]}")

    await bot.send(event, "\n".join(lines))


# ================= /记忆统计 =================
mem_stats_cmd = on_command("记忆统计", priority=5, block=True)


@mem_stats_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    s = memory_v2.stats()
    await bot.send(
        event,
        f"🧠 记忆库：共 {s['total']} 条（活跃 {s['active']}，已过时 {s['outdated']}，情绪刻痕 {s['emotional']}）",
    )


# ================= /心情 =================
mood_cmd = on_command("心情", priority=5, block=True)


@mood_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    # 查的是「当前这个会话里」的心情（心情已分层：全局 + 每个群/私聊各一份）
    m = mood.get_mood(_get_session_key(event))
    v = m["value"]
    # 👇 有她自己写的心情原话就先显示原话（用户 2026-11-19：
    #    「心情不能是我们指定的几种，她要自己有写心情的权利」）。
    #    数值只是「语气浓淡」，别拿它当她的心情讲出来。
    label = str(m.get("label") or "").strip()
    if label:
        txt = label
    elif v >= 40:
        txt = "（还没写）心情挺松快的"
    elif v >= 15:
        txt = "（还没写）还行"
    elif v <= -40:
        txt = "（还没写）有点闷，不想理人"
    elif v <= -15:
        txt = "（还没写）有点低落"
    else:
        txt = "（还没写）平平无奇"
    owner_tag = " (你是主人)" if is_owner(event.user_id) else ""
    _jd = m.get("judge") or {}
    _extra = ""
    if _jd.get("word"):
        try:
            _jv = float(_jd.get("value") or 0.0)
        except Exception:
            _jv = 0.0
        _extra = f"\n· 判读：{_jd['word']}（{_jv:+.2f}）"
    await bot.send(event, f"🌊 心情：{txt}{_extra}\n"
                          f"· 语气值 {v:+.0f}（只影响活泼/冷淡，不是心情本身）"
                          f"{owner_tag}")


# ================= /安静 [分钟] / /说话：主人直接控制 =================
# 用户 2026-11-20：在群里说「让她安静一小时」，她口头答应了，过一会儿又说话。
# 两个原因：① 她只是嘴上答应、没真调 chat_switch；② 真调了，但
#   CHAT_OFF_ANSWER_WHEN_CALLED=true 时「被 @」仍然会回。
# 所以给主人一条**硬指令**：/安静 60 → strict=True，连 @ 都不回。
silence_cmd = on_command("安静", priority=4, block=True)
talk_cmd = on_command("说话", aliases={"解除安静"}, priority=4, block=True)

# 非核心能力总开关（2026-11-20）：核心功能不受影响
skill_cmd = on_command("技能", aliases={"skill", "功能"}, priority=1, block=True)


@skill_cmd.handle()
async def _skill_cmd(event: MessageEvent, arg=CommandArg()):
    if not is_owner(str(event.user_id)):
        await skill_cmd.finish("这个只有主人能改。")
    raw = str(arg or "").strip()
    if not raw or raw in ("状态", "列表", "list"):
        await skill_cmd.finish(skill_switch.status_text())
    parts = raw.replace("，", ",").split()
    act = parts[0].lower()
    target = parts[1] if len(parts) > 1 else "全部"
    if act in ("开", "on", "开启", "打开"):
        value = True
    elif act in ("关", "off", "关闭", "禁用"):
        value = False
    else:
        await skill_cmd.finish(
            "用法：/技能 关 全部 ｜ /技能 开 搜索,画图 ｜ /技能 状态")
    for t in target.replace(",", " ").split():
        skill_switch.set_enabled(t, value)
    await skill_cmd.finish(("已开启" if value else "已关闭") + "：" + target
                           + "\n" + skill_switch.status_text())


# token 用量报表（2026-11-20）：按 purpose 给出最贵/最省
usage_cmd = on_command("用量", aliases={"token", "费用"}, priority=1, block=True)


@usage_cmd.handle()
async def _usage_cmd(event: MessageEvent, arg=CommandArg()):
    if not is_owner(str(event.user_id)):
        await usage_cmd.finish("这个只有主人能看。")
    from services import llm_usage, usage_chart
    raw = str(arg or "").strip().lower()
    days = 7 if raw in ("7", "周", "week", "7天") else 1
    text = llm_usage.report_text(days)
    # `/用量 文字` 只看数字，不拉图（图床挂了也不影响查账）
    if any(w in raw for w in ("文字", "text", "nochart")):
        await usage_cmd.finish(text)
    paths = await usage_chart.download_charts(days)
    if not paths:
        await usage_cmd.finish(text)
    msg = Message(text)
    for p in paths:
        msg += MessageSegment.image(f"file://{Path(p).resolve()}")
    await usage_cmd.finish(msg)


# 调参表（2026-11-20）：所有阈值的当前生效值都在 core/tuning.py
tuning_cmd = on_command("调参", aliases={"tuning", "参数"}, priority=1, block=True)


@tuning_cmd.handle()
async def _tuning_cmd(event: MessageEvent, arg=CommandArg()):
    if not is_owner(str(event.user_id)):
        await tuning_cmd.finish("这个只有主人能看。")
    from core import tuning as _t
    kw = str(arg or "").strip()
    rows = _t.describe()
    if kw and kw not in ("全部", "all"):
        rows = [r for r in rows if kw.lower() in r[0].lower()
                or kw in (r[4] or "")]
    if not rows:
        await tuning_cmd.finish("没找到匹配的参数。用法：/调参 关键词｜/调参 全部")
    def _fmt(v):
        try:
            fv = float(v)
            return str(int(fv)) if fv == int(fv) else ("%.3f" % fv).rstrip("0")
        except Exception:
            return str(v)
    out = ["▍调参（当前生效值 / 默认值）"]
    for key, cur, default, _typ, doc, _src in rows[:60]:
        mark = "" if _fmt(cur) == _fmt(default) else " ←改"
        out.append(f"· {key} = {_fmt(cur)}（默认 {_fmt(default)}）{mark}"
                   f"　{(doc or '')[:22]}")
    if len(rows) > 60:
        out.append(f"…… 还有 {len(rows) - 60} 项，用 /调参 关键词 缩小范围")
    out.append("说明见 docs/tuning.md；改值写 .env 同名键后重启。")
    await tuning_cmd.finish("\n".join(out))


_DUR_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(小时|钟头|hours?|hour|hr|h|分钟|分|minutes?|min|m|天|d)",
    re.I)


def _minutes_of(raw: str, default: float = 60) -> float:
    """'1小时'→60，'30分钟'→30，'90'→90，'2天'→2880。认不出来用 default。"""
    t = str(raw or "")
    m = _DUR_RE.search(t)
    if not m:
        m2 = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])", t)
        return float(m2.group(1)) if m2 else float(default)
    try:
        v = float(m.group(1))
    except Exception:
        return float(default)
    unit = str(m.group(2) or "").lower()
    if unit in ("小时", "钟头", "hour", "hours", "hr", "h"):
        v *= 60
    elif unit in ("天", "d"):
        v *= 1440
    return v


def _split_quiet_args(raw: str) -> tuple[str, str]:
    """把 '100000017 1小时' 拆成 (群号/QQ, '1小时')。第一个 5~12 位数字当目标。"""
    t = str(raw or "").strip()
    m = re.search(r"(?<!\d)(\d{5,12})(?!\d)", t)
    if not m:
        return "", t
    return m.group(1), (t[: m.start()] + " " + t[m.end():]).strip()


@silence_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(str(event.user_id)):
        await silence_cmd.finish("只有主人能让我安静。")
    raw = args.extract_plain_text().strip()
    # 支持私聊里指定群：/安静 100000017 1小时
    _tgt, _rest = _split_quiet_args(raw)
    minutes = int(max(1, min(72 * 60, _minutes_of(_rest, 60))))
    gid = _tgt or str(getattr(event, "group_id", "") or "")
    uid = str(event.user_id)
    if gid:
        chat_switch.set_off("群", gid, minutes=minutes, reason="主人要求",
                            by="owner", strict=True)
        logger.info(f"[SWITCH] 主人要求 {gid} 强制安静 {minutes} 分钟")
        await silence_cmd.finish(
            f"🔇 群 {gid} 从现在起安静 {minutes} 分钟：\n"
            f"· 不主动接话，**连 @ 也不回**（strict）\n"
            f"· 消息照收照记，心情照常判\n"
            f"· 想恢复发 /说话 {gid}")
    chat_switch.set_off("人", uid, minutes=minutes, reason="主人要求",
                        by="owner", strict=True)
    logger.info(f"[SWITCH] 主人要求私聊 {uid} 强制安静 {minutes} 分钟")
    await silence_cmd.finish(f"🔇 私聊安静 {minutes} 分钟（连 @ 也不回）。想恢复发 /说话")


@talk_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(str(event.user_id)):
        await talk_cmd.finish("只有主人能解除。")
    raw = args.extract_plain_text().strip()
    _tgt, _rest = _split_quiet_args(raw)
    uid = str(event.user_id)
    gid = str(getattr(event, "group_id", "") or "")
    if _tgt:
        kind = "群" if _tgt in ALLOWED_GROUPS else "人"
        had = chat_switch.set_on(kind, _tgt)
        _what = f"{kind} {_tgt}"
    elif gid and any(k in raw for k in ("这个人", "他", "她", "私聊")):
        had = chat_switch.set_on("人", uid)
        _what = f"人 {uid}"
    else:
        had = (chat_switch.set_on("群", gid) if gid
               else chat_switch.set_on("人", uid))
        _what = gid or uid
    logger.info(f"[SWITCH] 主人解除安静：{_what}（之前{'是' if had else '没'}安静）")
    await talk_cmd.finish("好，我正常说话了。" if had else "我本来就在正常说话呀。")


# ================= /叫你 <名字>：你给**她**起名（= /改名）=================
# ⚠️ 2026-11-20 用户反馈「/叫你 /叫我 完全是反的」：
#   在 QQ 里跟机器人说话，**「你」= 机器人、「我」= 用户**，所以：
#     /叫你 小鲸鱼  → 她以后叫自己小鲸鱼（bot_alias，喊这个名字 = @ 她）
#     /叫我 老大    → 她以后管你叫老大（call_name）
#   以前这两个写反了（/叫你 改的是用户昵称），现在对调。
rename_cmd = on_command("叫你", aliases={"改名"}, priority=5, block=True)


@rename_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    new_name = args.extract_plain_text().strip()
    if not new_name:
        await bot.send(event, "你想叫我什么？")
        return
    if len(new_name) > 12:
        await bot.send(event, "名字太长了，短一点吧。")
        return
    qq = str(event.user_id)
    # bot_alias：这个人怎么叫她。写进档案后，喊这个名字就等于 @ 她
    # （`_my_names` 会读 bot_alias），所以不用每次真 @。
    profiles.set_call_names(qq, bot_alias=new_name)
    await bot.send(event, f"好呀，那你就叫我「{new_name}」吧～以后喊我就行。")


# ================= /叫我 <名字>：她以后怎么叫你 =================
rename_self_cmd = on_command("叫我", priority=5, block=True)


@rename_self_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    new_name = args.extract_plain_text().strip()
    if not new_name:
        await bot.send(event, "你要我叫你什么？")
        return
    if len(new_name) > 12:
        await bot.send(event, "名字太长了，短一点吧。")
        return
    qq = str(event.user_id)
    # 存 call_name（「她怎么叫他」），不覆盖 nickname —— nickname 是群名片，
    # 每轮都会被写回，存不住约定。
    profiles.set_call_names(qq, call_name=new_name)
    _clear_label(qq)
    await bot.send(event, f"（点头）好的，以后我就叫你「{new_name}」啦。")


# ================= /记住 <事实> =================
remember_cmd = on_command("记住", priority=5, block=True)


@remember_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    fact = args.extract_plain_text().strip()
    if not fact:
        await bot.send(event, "你想让我记住什么？")
        return
    qq = str(event.user_id)
    memory_v2.remember(text=fact, kind="fact", about_qq=qq, importance=0.8)
    profiles.add_bond_note(qq, fact[:40])
    await bot.send(event, "✅ 记住了，我写进小本本里了。")


# ================= /叫他（支持 @人 或 QQ号） =================
call_other_cmd = on_command("叫他", priority=5, block=True)


@call_other_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(event.user_id):
        await bot.send(event, "只有主人能改别人的档案哦")
        return

    target_qq = None
    rest = []

    for seg in args:
        if seg.type == "at":
            target_qq = str(seg.data.get("qq", ""))
        elif seg.type == "text":
            rest.append(seg.data.get("text", ""))

    raw = "".join(rest).strip()
    parts = raw.split(maxsplit=1)

    if target_qq is None:
        if parts and parts[0].isdigit():
            target_qq = parts[0]
            new_name = parts[1].strip() if len(parts) > 1 else ""
        else:
            await bot.send(event, "格式：/叫他 @某某 名字  或  /叫他 123456 名字")
            return
    else:
        new_name = parts[0].strip() if parts else ""

    if not target_qq or not new_name:
        await bot.send(event, "我还缺一个名字，或者你还没 @ 人。格式：/叫他 @某某 名字")
        return

    profiles.set_call_names(target_qq, call_name=new_name)
    _clear_label(target_qq)
    await bot.send(event, f"好的，以后我管 QQ {target_qq} 叫「{new_name}」。")


# ================= 头像：优先让 NapCat 去拿 =================
# 用户 2026-11-19：「没有档案卡片渲染 NapCat 获得用户头像的内容」。
# 为什么要走 NapCat：容器直连 qlogo 不一定通（没网 / 被风控 / 代理只给 LLM 用），
# 而 NapCat 是真正的 QQ 客户端 —— 它拿头像的成功率天然高一个量级。
# 顺序：缓存 → NapCat 资料接口 → NapCat 代下 qlogo → 容器直连 → 名字首字。
_AVATAR_SAID: set = set()


async def _http_image(url: str) -> bytes:
    """下图片，只认图片魔数（qlogo 会「HTTP 200 + 风控页 HTML」）。"""
    try:
        import httpx
        async with httpx.AsyncClient(
                timeout=10, follow_redirects=True, trust_env=False,
                headers={"User-Agent": "Mozilla/5.0 (compatible; qqbot)"}) as c:
            r = await c.get(url)
        if r.status_code == 200 and len(r.content) > 200:
            if r.content[:4].startswith((b"\xff\xd8", b"\x89PNG", b"GIF8", b"RIFF", b"BM")):
                return r.content
    except Exception:
        pass
    return b""


def _read_napcat_file(p: str) -> bytes:
    """读 NapCat 落盘的图片：两个容器把 ./napcat/qq 都挂在 /app/.config/QQ，路径一致。"""
    s = str(p or "").strip()
    if not s:
        return b""
    if s.startswith("file://"):
        s = s[7:]
    cands = [s]
    if s.startswith("/") and not s.startswith("/app/"):
        cands.append("/app" + s)
    for cand in cands:
        try:
            q = Path(cand)
            if q.is_file() and q.stat().st_size > 200:
                return q.read_bytes()
        except Exception:
            continue
    return b""


async def _avatar_via_napcat(bot, qq: str, gid: str = "") -> tuple:
    """让 NapCat（QQ 客户端）拿头像。返回 (bytes, 说明)；拿不到返回 (b"", 原因)。"""
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return b"", "不是 QQ 号"
    notes = []
    # ① NapCat 的资料接口里如果带头像地址，直接下（各版本字段名不一样，全试）
    infos = []
    if gid:
        try:
            infos.append(("群成员资料", await bot.call_api(
                "get_group_member_info", group_id=int(gid), user_id=int(qq))))
        except Exception as e:
            notes.append(f"群成员资料 {type(e).__name__}")
    try:
        infos.append(("陌生人资料",
                      await bot.call_api("get_stranger_info", user_id=int(qq))))
    except Exception as e:
        notes.append(f"陌生人资料 {type(e).__name__}")
    for label, d in infos:
        if not isinstance(d, dict):
            continue
        for k in ("avatar", "avatarUrl", "avatar_url", "headimg",
                  "head_img", "headimgurl"):
            u = str(d.get(k) or "").strip()
            if not u.startswith("http"):
                continue
            data = await _http_image(u)
            if data:
                return data, f"{label}.{k}"
            notes.append(f"{label}.{k} 下载失败")
    # ② 让 NapCat 自己下 qlogo —— QQ 客户端出口，容器直连不通时这条能救
    try:
        from services import profile_card as _pc
        urls = _pc._avatar_urls(qq)
    except Exception:
        urls = (f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640",)
    for url in urls:
        try:
            r = await bot.call_api("get_image", file=url)
        except Exception as e:
            notes.append(f"get_image {type(e).__name__}")
            break
        if not isinstance(r, dict):
            continue
        data = _read_napcat_file(r.get("file") or "")
        if data:
            return data, "NapCat 代下(本地文件)"
        u = str(r.get("url") or "").strip()
        if u.startswith("http"):
            data = await _http_image(u)
            if data:
                return data, "NapCat 代下(URL)"
        notes.append("NapCat 代下没拿到文件")
    return b"", "；".join(notes[:3]) or "NapCat 没给出头像"


async def _ensure_avatar(bot, qq: str, gid: str = "") -> str:
    """保证 data/avatars/<qq>.jpg 能用。返回一句给人看的说明（/测函数 档案 会打）。"""
    from services import profile_card as _pc
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return f"{qq or '?'} 不是 QQ 号"
    if _pc.avatar_fresh(qq):
        return "缓存"
    # ⚠️ 必须限时：/档案 是交互命令，NapCat 掉线时 get_image 会挂到超时，
    #    不能让人等一分钟才看到卡片。
    try:
        data, why = await asyncio.wait_for(
            _avatar_via_napcat(bot, qq, gid), timeout=8)
    except asyncio.TimeoutError:
        data, why = b"", "NapCat 超时"
    except Exception as e:
        data, why = b"", f"NapCat {type(e).__name__}"
    if data:
        ok, w = _pc.store_avatar(qq, data)
        if ok:
            logger.info(f"[PROFILE-CARD] {qq} 头像由 NapCat 取到（{why}）")
            return f"NapCat 取到（{why}）"
        why = f"{why}｜{w}"
    try:
        ok, w = await asyncio.wait_for(
            asyncio.to_thread(_pc._avatar_download, qq), timeout=25)
    except asyncio.TimeoutError:
        ok, w = False, "直连超时"
    if ok:
        return f"直连下载（{w}）"
    if qq not in _AVATAR_SAID:
        _AVATAR_SAID.add(qq)
        logger.warning(f"[PROFILE-CARD] {qq} 头像拿不到：NapCat {why}｜直连 {w}")
    return f"没拿到（NapCat {why}｜直连 {w}）"


# ================= 发图：失败要多等几秒重试 =================
# 用户 2026-11-19：「应该多等会图片，不发文字的」。
# 真机现象：760x1500 的卡片 PNG 上传慢，NapCat 先回 ActionFailed(retcode=12)，
# 但图其实**还在上传队列里，两分钟后照样到**（日志里「发送超时/失败」后面图还是出来了）。
# 所以：失败 → 等几秒重发（缓存已就绪，第二次基本就成），绝不拿一大段文字顶替图片。
_IMG_RETRY_DELAYS = (4, 8, 12)


async def _send_image_patiently(bot, event, path: str,
                                delays: tuple = _IMG_RETRY_DELAYS) -> bool:
    """发一张图，失败就等几秒重发。返回最终是否发出去。"""
    if not path:
        return False
    seg = MessageSegment.image(f"file://{Path(path).resolve()}")
    tries = max(1, len(delays) + 1)
    for i in range(tries):
        try:
            await bot.send(event, seg)
            if i:
                logger.info(f"[IMG] 第 {i + 1} 次发图成功：{Path(path).name}")
            return True
        except Exception as e:
            logger.warning(f"[IMG] 第 {i + 1}/{tries} 次发图失败："
                           f"{type(e).__name__}: {str(e)[:80]}")
            if i < tries - 1:
                await asyncio.sleep(delays[min(i, len(delays) - 1)])
    return False


# ================= /档案 [@人 / QQ号] =================
profile_view_cmd = on_command("档案", priority=5, block=True)


@profile_view_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    target = None
    rest = []
    _want_text = False
    for seg in args:
        if seg.type == "at":
            target = str(seg.data.get("qq", ""))
        elif seg.type == "text":
            _t = str(seg.data.get("text", ""))
            if "文字" in _t:
                _want_text = True
                _t = _t.replace("文字", "")
            rest.append(_t)
    if target is None:
        raw = "".join(rest).strip()
        target = raw if raw.isdigit() else str(event.user_id)

    p = profiles.get_profile(target)
    b = profiles.get_bond(target)
    style = profiles.style_summary(target)

    lines = [f"📄 QQ {target} 的档案："]
    lines.append(f"· 群名片/昵称：{p.get('nickname') or '(未设置)'}")
    lines.append(f"· 我叫他：{p.get('call_name') or '(还没定)'}")
    lines.append(f"· 他叫我：{p.get('bot_alias') or '(还没定)'}")
    lines.append(f"· 别名：{', '.join(p.get('aliases', [])) or '(无)'}")
    lines.append(f"· 是否主人：{'是' if p.get('is_owner') else '否'}")
    if style:
        lines.append(f"· 说话方式：{style}")
    if p.get("topics"):
        lines.append(f"· 常聊话题：{', '.join(str(x) for x in p['topics'][:8])}")
    if p.get("last_seen_msg"):
        lines.append(f"· 上次聊到：{p['last_seen_msg']}")
    if p.get("long_term_impression"):
        lines.append(f"· 长期印象：{p['long_term_impression']}")

    # ---- 相处经历：全部列出（带时间 + 心情 + 重要度），不再只留最后一次 ----
    notes = b.get("notes") or []
    if notes:
        show = notes[-20:]
        lines.append(f"\n🧠 相处经历（共 {len(notes)} 条）：")
        for n in show:
            at = float(n.get("at") or 0)
            ts = time.strftime("%m-%d %H:%M", time.localtime(at)) if at else "--"
            mark = profiles.EMOTION_MARK.get(n.get("emotion") or "", "")
            star = "★" if float(n.get("weight") or 1.0) >= 1.5 else ""
            lines.append(f"  [{ts}] {mark}{star}{n.get('text', '')}")
        if len(notes) > len(show):
            lines.append(f"  …还有 {len(notes) - len(show)} 条更早的（超过 20 条只显示最近 20 条）")
    else:
        lines.append("\n🧠 相处经历：还没有记录")

    # ---- 长期记忆（memory_v2 里挂在这个人身上的）----
    try:
        from services import memory_v2 as _mv
        idx = _mv._load_index()
        mine = [d for d in idx.get("items", [])
                if str(d.get("about_qq") or "") == str(target)]
        mine.sort(key=lambda d: d.get("created_at", 0), reverse=True)
    except Exception as e:
        mine = []
        logger.debug(f"[PROFILE] 读记忆失败: {e}")
    if mine:
        lines.append(f"\n📚 长期记忆（共 {len(mine)} 条）：")
        for d in mine[:12]:
            ts = time.strftime("%m-%d", time.localtime(d.get("created_at", 0) or 0))
            lines.append(f"  [{ts}] ({d.get('kind')}) {str(d.get('summary') or '')[:60]}")
        if len(mine) > 12:
            lines.append(f"  …还有 {len(mine) - 12} 条")
    else:
        lines.append("\n📚 长期记忆：还没有挂在他身上的记忆")

    # 👇 默认发档案图片（用户 2026-11-18）；`/档案 文字` 或渲染失败时退回文字
    if not _want_text:
        try:
            from services import profile_card as _pc
            _gid_t = str(getattr(event, "group_id", "") or "")
            # 👇 先让 NapCat 把头像是弄进缓存，再画图（否则左上角只剩名字首字）
            _av_why = await _ensure_avatar(bot, target, _gid_t)
            logger.debug(f"[PROFILE-CARD] {target} 头像：{_av_why}")
            # 放到线程里：Pillow 画图是阻塞的，别卡事件循环
            _img = await asyncio.to_thread(
                _pc.render_profile_image, target, _gid_t)
            if _img:
                # 发图失败要重试（NapCat 上传大图常超时），别一失败就把整段文字倒出来
                if await _send_image_patiently(bot, event, _img):
                    return
                await bot.send(event, "🖼 档案图还在上传，稍等；没到就再发一次 /档案")
                return
        except Exception as e:
            logger.debug(f"[PROFILE] 档案图渲染失败，退回文字: {type(e).__name__}: {e}")

    await bot.send(event, "\n".join(lines))


# ================= /本群叫他 <@人 / QQ> <名字>：按群覆盖称呼 =================
group_call_cmd = on_command("本群叫他", priority=5, block=True)


@group_call_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    gid = str(getattr(event, "group_id", "") or "")
    if not gid:
        await bot.send(event, "这个只能在群里用；私聊直接 /叫我 名字 就行。")
        return

    target_qq = None
    rest = []
    for seg in args:
        if seg.type == "at":
            target_qq = str(seg.data.get("qq", ""))
        elif seg.type == "text":
            rest.append(str(seg.data.get("text", "")))
    parts = "".join(rest).strip().split()
    if target_qq is None and parts and parts[0].isdigit():
        target_qq = parts[0]
        parts = parts[1:]
    new_name = parts[0].strip() if parts else ""
    if not target_qq or not new_name:
        await bot.send(event, "格式：/本群叫他 @某某 名字（只在这个群里这么叫）")
        return
    if target_qq != str(event.user_id) and not is_owner(event.user_id):
        await bot.send(event, "只有主人能改别人在这个群的称呼。")
        return

    profiles.set_group_fields(target_qq, gid, call_name=new_name)
    _clear_label(target_qq)
    await bot.send(event, f"好，在这个群里我管 QQ {target_qq} 叫「{new_name}」。")


# ================= /词条：本群设定库（生成 / 列表 / 删） =================
lore_cmd = on_command("词条", priority=5, block=True)


@lore_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(event.user_id):
        await bot.send(event, "只有主人能管理词条。")
        return
    from services import lorebook

    gid = str(getattr(event, "group_id", "") or "")
    scope = gid or f"private_{event.user_id}"
    _raw = args.extract_plain_text().strip()
    _want_text = "文字" in _raw
    arg = _raw.replace("文字", "").strip()

    if arg in ("生成", "seed", "导入", "从档案生成"):
        qqs = []
        if gid:
            try:
                members = await group_roster.members_of(bot, gid)
                qqs = [str(m.get("user_id") or "") for m in members]
            except Exception:
                qqs = []
        if not qqs:
            qqs = [str(event.user_id)]
        n = lorebook.seed_from_profiles(scope, qqs)
        await bot.send(event, f"✅ 从现有档案生成了 {n} 条人物词条（已去重）。")
        return

    if arg.startswith(("删", "删除", "del")):
        eid = arg.replace("删除", "").replace("删", "").replace("del", "").strip()
        if not eid:
            await bot.send(event, "格式：/词条 删 <编号>（编号看 /词条 列表）")
            return
        ok = lorebook.delete_entry(scope, eid)
        await bot.send(event, "✅ 删掉了" if ok else "没找到这个词条编号")
        return

    items = lorebook.list_entries(scope)
    if not items:
        await bot.send(event, "本群词条还是空的。发 /词条 生成 可以先把现有档案灌进去。")
        return

    # 👇 默认发词条卡片图（用户 2026-11-19）；`/词条 文字` 或渲染失败时发文字
    if not _want_text:
        try:
            from services import profile_card as _pc
            _img = _pc.render_lore_image(
                scope, title=f"{'本群' if gid else '私聊'}词条")
            if _img:
                await bot.send(event, MessageSegment.image(f"file://{Path(_img).resolve()}"))
                return
        except Exception as e:
            logger.debug(f"[LORE] 词条图渲染失败，退回文字: {type(e).__name__}: {e}")

    lines = [f"📚 本{'群' if gid else '私聊'}词条 {len(items)} 条："]
    for e in items[:20]:
        keys = ",".join(e.get("keys") or [])[:28]
        lines.append(
            f"· {e.get('id')} 【{e.get('comment') or '?'}】{keys}"
            f"｜{str(e.get('content') or '')[:36]}"
        )
    if len(items) > 20:
        lines.append(f"…还有 {len(items) - 20} 条")
    lines.append("\n删：/词条 删 <编号>｜从档案生成：/词条 生成")
    await bot.send(event, "\n".join(lines))


# ================= /测语音：诊断为什么没用本地 indextts =================
tts_test_cmd = on_command("测语音", priority=5, block=True)


@tts_test_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    _arg = str(args or "").strip().lower()
    lines = ["🎙 TTS 诊断"]
    _age = -1.0
    try:
        from plugins.tool_dispatcher import STATUS, HEARTBEAT_TIMEOUT
        last = float(STATUS.get("last_heartbeat") or 0)
        _age = (time.time() - last) if last else -1
        lines.append(f"· USE_LOCAL_TTS = {USE_LOCAL_TTS}")
        lines.append(f"· 插话转语音概率 = {RANDOM_CHAT_VOICE_RATE}")
        lines.append(
            f"· 算力机心跳：{'从未收到' if _age < 0 else f'{_age:.0f}s 前'}"
            f"（阈值 {HEARTBEAT_TIMEOUT}s）"
        )
        lines.append(f"· 判定在线：{_compute_online()}")
        caps = sorted(STATUS.get("caps") or [])
        lines.append(f"· 上报能力 caps：{caps or '(空 —— node.py 没上报 tts？)'}")
        tasks = STATUS.get("tasks") or {}
        pending = [k for k, v in tasks.items() if v.get("status") == "pending"]
        lines.append(f"· 卡住的任务：{len(pending)} 个")
    except Exception as e:
        lines.append(f"· 读不到任务表：{type(e).__name__}: {e}")

    if USE_LOCAL_TTS and _compute_online():
        lines.append("\n结论：应该走本地 indextts。如果实际听到云端音色，看日志里 [TTS] 任务结果。")
    elif not USE_LOCAL_TTS:
        lines.append("\n结论：.env 里 USE_LOCAL_TTS 不是 true → 永远走云端。")
    else:
        lines.append(
            "\n结论：算力机被判离线 → 语音降级云端。"
            "检查 node.py 心跳间隔，或把 HEARTBEAT_TIMEOUT_SEC 调大。"
        )
    lines.append("\n（想看真跑结果：发 `/测语音 跑`）")
    await bot.send(event, "\n".join(lines))

    # ---------- `/测语音 跑`：真跑一次，看卡在哪一步 ----------
    # 只打配置看不出问题 —— 必须真的提交任务，才能知道是
    # 「没提交 / 没被拉走 / indextts 报错 / 产物路径不对 / 发送失败」哪一环。
    if _arg not in ("跑", "run", "试", "1", "test"):
        return
    if not (USE_LOCAL_TTS and _compute_online()):
        await bot.send(event, "⚠️ 现在本来就走云端，真跑也是龙呼呼。先修好上面那几行。")
        return
    await bot.send(event, "🔬 真跑一次本地 indextts（首次加载模型可能要几十秒，成功会直接发一条语音）…")
    t0 = time.time()
    ok = await speak_voice(bot, event, "语音测试，听得清吗")
    await bot.send(
        event,
        f"{'✅ 通了' if ok else '❌ 没通'}｜耗时 {time.time() - t0:.1f}s\n"
        f"对照日志三行定位：\n"
        f"① `[TTS] 决策：… 走本地 indextts` —— 没这行就是根本没提交\n"
        f"② `[TTS] 已提交算力机任务 …` —— 提交了\n"
        f"③ `[TTS] 任务结果 status=…` —— status=unpicked 说明算力机没这个处理器；"
        f"status=completed 但没声音，看 output 路径\n"
        f"（听到云端音色 = ②③ 之后打了 `降级云端`）"
    )


# ================= /看模板：打印最近一次发给 DeepSeek 的完整输入 =================
show_tpl_cmd = on_command("看模板", priority=5, block=True)


def _chunk_text(s: str, size: int = 1200) -> list:
    s = str(s or "")
    return [s[i:i + size] for i in range(0, len(s), size)] or [""]


def _block_title(body: str) -> str:
    """从块内容里猜一个标题（第一个 ▍ / 【】 / 首行），用来出目录。"""
    s = str(body or "").lstrip()
    for ln in s.splitlines()[:8]:
        ln = ln.strip()
        if not ln:
            continue
        if ln.startswith("▍"):
            return ln[:28]
        m = re.match(r"【([^】]{1,18})】", ln)
        if m:
            return m.group(0)
        if "角色卡" in ln or ln.startswith("DeepSeek"):
            return "▍角色卡"
        return ln[:28]
    return "（空）"


@show_tpl_cmd.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    _arg = str(args or "").strip()
    _full = _arg in ("全", "全部", "all", "full")
    key = _get_session_key(event)
    msgs = llm_mod.last_messages(key) or llm_mod.last_messages()
    if not msgs:
        await bot.send(event, "还没有调用记录。先随便跟她说一句，再来 /看模板。")
        return

    total = sum(len(str(m.get("content") or "")) for m in msgs)
    _is_g = isinstance(event, GroupMessageEvent)

    # ① 先给目录：每块的标题 + 字数，一眼看清结构和冗余
    toc = [f"📋 最近一次发给 DeepSeek 的输入：{len(msgs)} 块 / 共 {total} 字"]
    for i, m in enumerate(msgs, 1):
        _b = m.get("content")
        _b = _b if isinstance(_b, str) else str(_b)
        _role = "👤user" if m.get("role") == "user" else "⚙️sys"
        toc.append(f"#{i} {_role} {len(_b):>5} 字  {_block_title(_b)}")
    toc.append("（想看全文发 /看模板 全）")
    await bot.send(event, "\n".join(toc))

    if not _full:
        await bot.send(event, "下面按块打印（块顺序就是真实请求顺序）…")

    sent = 0
    _limit = 60 if _full else 14
    for i, m in enumerate(msgs, 1):
        role = m.get("role")
        raw = m.get("content")
        body = raw if isinstance(raw, str) else f"[多模态内容] {str(raw)[:300]}"
        block = f"── #{i} {role}（{len(body)} 字）──\n{body}"
        for piece in _chunk_text(block, 1200):
            if _is_g:
                if not await _wait_group_quota(str(event.group_id)):
                    await bot.send(event, "（群消息配额不足，后面的块没发完，建议私聊用 /看模板）")
                    return
            try:
                await bot.send(event, piece)
            except Exception as e:
                logger.warning(f"[TPL] 发送失败: {e}")
                return
            sent += 1
            if sent >= _limit:
                await bot.send(event, f"（太长，只打印到第 {_limit} 段，剩下的看日志）")
                for j, mm in enumerate(msgs[i:], i + 1):
                    logger.info(f"[TPL] #{j} {mm.get('role')}: {str(mm.get('content'))[:800]}")
                return


# ================= 「发个表情包」确定性接管 =================
# 用户 2026-11-18：群里 @ 她说「发个表情包」，先被关注判定挡成「不回」；
# 就算回了，模型也可能只嘴上答应、不真发。
# 这类请求不需要模型思考 —— 在跟她说话时直接甩一张；群友互相要图不受影响。
_STICKER_REQ_RE = re.compile(
    # 「你发个表情包」要算，「我发个表情包」不算 —— 所以只认第二人称/祈使开头
    r"^(?:请|你|能不能|可不可以|能|帮我|给我|替我|来|发|整|搞|甩|丢|再)"
    r"[^。！？!?\n]{0,6}?(?:表情包|表情|梗图)"
)


def _is_sticker_request(text: str) -> bool:
    s = str(text or "").strip()
    # 「收个表情包」是让她收藏，不是让她发 —— 别抢表情收集的活
    if "收" in s:
        return False
    return bool(_STICKER_REQ_RE.match(s))


# ================= 主入口 =================
@chat.handle()
async def _(bot: Bot, event: MessageEvent):
    # 👇 不能只用 get_plaintext()：语音 / 转发卡片的 plaintext 是空的，
    #    这里补成「[语音] 内容」「标题+UP主+简介」，否则下面 `if not text: return` 会丢掉它们。
    text = await _enrich_text(event)
    session_key = _get_session_key(event)

    # ---------- 关注会话：被 @ 就开启「后续还跟几条」 ----------
    # 注意：@ 这一次本身不消耗额度，额度是「后续还能接几条」
    if isinstance(event, GroupMessageEvent) and event.is_tome():
        # 空 @（只 @ 了一下、一个字没说）→ 标 pending：
        # 下一条同一人的消息直接算在跟她说话，不再让模型判「是不是在说我」。
        _focus_start(session_key, event.user_id, pending=not text.strip())

    # ---------- 确定性「发个表情包」----------
    # 只在「确实在跟她说话」时触发（@ / 叫名字 / 关注期内），别抢群友互相要图的话。
    if _is_sticker_request(text):
        _addressed_here = bool(
            event.is_tome()
            or _calls_me_by_name(text, session_key, str(event.user_id))
            or (isinstance(event, GroupMessageEvent)
                and _focus_active(session_key, event.user_id))
        )
        if _addressed_here:
            try:
                if await send_sticker_by_tag(bot, event, ""):
                    logger.info(f"[STICKER] 「{text[:20]}」→ 直接甩一张（不走模型）")
                    if isinstance(event, GroupMessageEvent):
                        _append_self_line(str(event.group_id), "[表情:随机]")
                        # 关注期里回了就扣额度（和 worker 里同一套规则：@ 那次不扣）
                        if not event.is_tome():
                            _focus_consume(session_key, event.user_id)
                    return
            except Exception as e:
                logger.debug(f"[STICKER] 确定性发图失败，走正常回复: {type(e).__name__}: {e}")

    # ---------- 她挑：收/不收 支持编号 ----------
    # 注：表情收集已挪到上面的独立 on_message（sticker_collect），
    #     这里只保留「她挑」的收/不收处理，避免同一个消息重复收两次。
    m = re.match(r"^<collect:\s*([\d,，\s]+)>", text)
    if m:
        idxs = [int(x) for x in re.findall(r"\d+", m.group(1))]
        n = commit_pending_by_index(event.get_session_id(), idxs)
        if n > 0:
            await bot.send(event, f"嘿嘿，收了 {n} 张。")
        return

    m = re.match(r"^收\s*([\d,，\s]+)$", text)
    if m:
        idxs = [int(x) for x in re.findall(r"\d+", m.group(1))]
        n = commit_pending_by_index(event.get_session_id(), idxs)
        if n > 0:
            await bot.send(event, f"收好啦，{n} 张入库~")
        else:
            await bot.send(event, "没对上哪张，你再看看？")
        return

    if text in ("收", "收下", "要", "要了", "收那张", "收下那张", "好", "可以", "行"):
        pending = list_pending(event.get_session_id())
        if pending:
            n = commit_pending(event.get_session_id())
            if n > 0:
                await bot.send(event, f"嘿嘿，收了 {n} 张，回头我发给你看~")
                return

    if text in ("不要", "不收", "算了", "不", "跳过", "别", "不要了"):
        n = drop_pending(event.get_session_id())
        if n > 0:
            await bot.send(event, "好吧，下次再挑。")
            return

    # ---------- 测试命令 ----------
    if "测大肥鱼" in text:
        await bot.send(event, "✅ 收到，正在发语音和图片...")
        from services.tts import send_voice
        from services.meme import send_random_fishy
        await send_voice(bot, event, "这是硬编码测试语音，如果听到说明功能通了")
        await send_random_fishy(bot, event)
        return

    if text.startswith("/写歌") or text.startswith("／写歌") or text.startswith("/写rap") or text.startswith("／写rap"):
        await _handle_sing(bot, event, session_key, text)
        return

    if text.startswith("/persona") or text.startswith("／persona"):
        await _handle_persona(bot, event, session_key, text)
        return

    if waiting_for_sing.get(session_key):
        handled = await _handle_sing_waiting(bot, event, session_key, text)
        if handled:
            return

    if not text:
        # 走到这里说明是「媒体消息但没文字」：给模型一句人话，别让它看到空串。
        _hint = _media_hint(event)
        if not _hint:
            return
        text = f"【对方{_hint}，没有说别的话】"

    # ---------- 确定性「看视频」接管 ----------
    # 2026-11-17 改规则：
    #   · 私聊：转发 B 站卡片 / 发链接 → 自动排队看，不用再说「看看这个」。
    #   · 群聊：必须「引用回复那张卡片 + @她」才触发，避免群里随手转发就被开看。
    _wurl = _bili_url_in(text)
    _watch_ok = False
    if _wurl:
        if isinstance(event, GroupMessageEvent):
            try:
                if event.is_tome() and _bili_url_in(await _reply_quote(event)):
                    _watch_ok = True
            except Exception:
                pass
        else:
            _watch_ok = True
    if _watch_ok:
        _ensure_video_worker()
        if not _video_queue.full():
            await _video_queue.put((bot, event, _wurl))
            await bot.send(event, "收到，我去看看这个视频，看完告诉你内容～")
            text += (
                "\n（这个视频系统已经在后台帮你排队看了，"
                "你不用再调 video_understand，等总结发回来就行）"
            )

    # ---------- 入队 ----------
    ctx = None
    if isinstance(event, GroupMessageEvent):
        gid = str(event.group_id)
        recent = list(recent_group_msgs[gid])
        if recent:
            ctx = recent[:-1] if len(recent) > 1 else recent
        # 👇 长消息补偿（2026-11-13 实测事故）：
        #    用户在群里发 200+ 字视觉方案，随后 10 句短话把长消息挤出了
        #    recent_group_msgs 窗口；她翻历史时又被等长 70 字截断 →
        #    最后只看到别人说的「5秒一帧」，完全不知道方案存在。
        #    这里从 dialogue_log（落盘的完整记录）把窗口外的长消息捞回来。
        try:
            # 按条数取（用户反复强调：记录按条数，不按时间）——
            # 以前 load_range(1) 只读今天的，凌晨/隔天就捞不到那条长消息了。
            from services.dialogue_log import load_recent
            _long_recs = [
                r for r in load_recent(gid, limit=80)
                if r.get("kind") != "bot"
                and len(str(r.get("text") or "")) > CTX_LONG_MSG_CHARS
            ]
            if _long_recs:
                _seen = "\n".join(ctx or [])
                _extra = []
                for r in _long_recs[-CTX_LONG_MSG_KEEP:]:
                    _t = str(r.get("text") or "")
                    # 已经在窗口里就别重复（窗口存的是完整文本，用前 40 字判重）
                    if _t[:40] and _t[:40] in _seen:
                        continue
                    _hhmm = time.strftime(
                        "%H:%M", time.localtime(float(r.get("ts") or 0)))
                    _extra.append(
                        f"{_hhmm} {r.get('who') or '?'}"
                        f"【长消息·完整内容】: {_t[:CTX_LONG_MSG_CHARS_MAX]}")
                if _extra:
                    ctx = list(ctx or []) + [
                        "【更早的长消息（已被短消息挤出最近窗口，但内容重要，必须看）】"
                    ] + _extra
                    logger.info(
                        f"[CTX] 补回 {len(_extra)} 条窗口外的长消息（gid={gid}）")
        except Exception as e:
            logger.debug(f"[CTX] 长消息补偿失败: {e}")
        # 👇 被 @ / 喊名字时，额外附上**这个人**最近说过的话。
        #    为什么：群聊只给最近 8 条，但他可能 20 条之前就开始说这件事了。
        #    她要回「他到底在聊什么」，光看最后 8 条经常不够。
        try:
            _uid = str(event.user_id)
            # ⚠️ handler 里这条消息的文本变量叫 text（不是 t，t 是 _need_reply 里的）
            _need_more = event.is_tome() or _calls_me_by_name(
                text, f"group_{gid}", _uid)
            _sp = list(speaker_recent[gid].get(_uid) or [])
            # 去掉当前这一条（它在 recent_group_msgs 里已经有了）
            if _sp and text and _sp[-1].endswith(str(text)[:60]):
                _sp = _sp[:-1]
            if _need_more and len(_sp) >= 2:
                ctx = list(ctx or [])
                # ⚠️ 人标记别漏：这块是「同一个人之前说的话」，标题必须写清是谁，
                #    否则她会当成群里别人说的（用户 2026-11-11 特别提醒过）。
                ctx.append(
                    f"【{_msg_label(event)} 刚才还说过】（同一个人，帮你理解他在聊什么）\n"
                    + "\n".join(_sp[-3:])
                )
        except Exception as e:
            logger.debug(f"[CTX] 附加上文失败: {e}")

    # ---------- 高刷清理（每个会话独立）----------
    # 群里/私聊里刷得太快，就别把旧消息排队了 —— 否则她一条条回完，
    # 回的全是几分钟前的旧话。
    #   · 只保留「@ 我 / 喊我名字」的那几句 + 即将入队的这条最新消息
    #   · 只清**这个 session_key** 的聊天队列：
    #     一个群刷屏绝不会牵连别的群、别人的私聊
    #   · 不碰 indextts / 翻唱这类任务 —— 它们在 tool_dispatcher 的任务表里，
    #     走的是算力机长任务，跟聊天队列完全无关（见 _flush_stale_queue 注释）
    if BURST_FLUSH_ENABLED:
        _n = _burst_count(session_key)
        if _n >= BURST_MSG_COUNT:
            _dropped = _flush_stale_queue(session_key)
            _where = session_key.replace("group_", "群").replace("private_", "私聊")
            if _dropped:
                _preview = "｜".join(
                    str(it[1])[:12] for it in _dropped[:3] if len(it) > 1
                )
                logger.info(
                    f"[BURST] {_where} 近 {BURST_WINDOW_SEC:.0f}s 来了 {_n} 条"
                    f"（阈值 {BURST_MSG_COUNT}）→ 清掉排队中的 {len(_dropped)} 条，"
                    f"只回 @ 我的 + 最新这条｜丢弃：{_preview}"
                )
            else:
                logger.debug(
                    f"[BURST] {_where} 近 {BURST_WINDOW_SEC:.0f}s 来了 {_n} 条，"
                    f"但队列里没有可清的（都在处理或都是 @）"
                )

    queue = session_queues[session_key]
    await queue.put((event, text, ctx))
    if session_key not in session_workers or session_workers[session_key].done():
        session_workers[session_key] = asyncio.create_task(_session_worker(bot, session_key))


# ================= 写歌处理 =================
async def _handle_sing(bot: Bot, event: MessageEvent, session_key: str, text: str):
    topic = re.sub(r"^[/／]写(歌|rap)", "", text).strip()
    if not topic:
        await bot.send(event, "你想让我写关于什么主题的 Rap 呀？")
        return
    await bot.send(event, "哼…等我酝酿一下灵感，找找 Beat……")
    prompt = (
        f"请写一段关于《{topic}》的短诗或者Rap歌词，不超过 50 个字。"
        f"要极尽优美，微醺空灵，而且要有说唱的节奏感和押韵。"
        f"严禁使用任何解释说明的文字，直接输出歌词内容。"
        f"词里要多用逗号、句号、省略号制造停顿节奏。"
    )
    try:
        raw_lyrics, _ = await call_llm(f"rap_{session_key}", prompt)
        raw_lyrics = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9，。！？、：；\n]", "", raw_lyrics)
        last_lyrics[session_key] = raw_lyrics
        await bot.send(event, f"看好词了，这是本小姐为你写的 Rap：\n{raw_lyrics}")
        waiting_for_sing[session_key] = True
        await bot.send(event, "（期待地看着你）要我用这段词…来一段 Rap 吗？")
    except Exception as e:
        await bot.send(event, f"脑壳疼，没灵感了…… ({e})")


async def _handle_persona(bot: Bot, event: MessageEvent, session_key: str, text: str):
    arg = text.replace("/persona", "").replace("／persona", "").strip()
    if not arg:
        cur = current_personality.get(session_key, DEFAULT_PERSONA)
        size = len(PERSONALITY_MAP.get(cur, ""))
        await bot.send(
            event,
            f"当前人格：{cur}（{size} 字）\n输入 /persona list 看全部，或 /persona 人格名 切换。",
        )
        return
    if arg in ("list", "列表"):
        lines = [f"· {n}（{len(PERSONALITY_MAP[n])} 字）" for n in list_personas()]
        names = "\n".join(lines) or "（没有可用人格）"
        await bot.send(
            event,
            f"🐟 目前可用人格（全部来自 services/Personality/*.txt 原文）：\n{names}\n\n"
            f"👉 输入 `/persona 人格名` 切换",
        )
        return
    if arg in PERSONALITY_MAP:
        current_personality[session_key] = arg
        await bot.send(event, f"（尾巴拍拍）切换成功！我现在是【{arg}】模式啦。")
        return

    # 名字不存在就不切，避免"说切了其实还是默认人格"的人设分裂
    avail = "、".join(list_personas()) or "（无）"
    await bot.send(event, f"（歪头）没有「{arg}」这个人格哦。\n可用：{avail}")


async def _handle_sing_waiting(bot: Bot, event: MessageEvent, session_key: str, text: str) -> bool:
    tl = text.lower()
    if any(w in tl for w in ["不唱", "算了", "下次", "不了", "别唱", "闭嘴"]):
        waiting_for_sing[session_key] = False
        await bot.send(event, "哼，算你识相，那本小姐不唱了。")
        return True
    if not any(w in tl for w in ["唱", "好", "可以", "行", "来", "嗯"]):
        return False

    waiting_for_sing[session_key] = False
    await bot.send(event, "咳咳……听好了，Give me the beat！")
    lyrics = last_lyrics.get(session_key, "嗯…忘记刚才写了什么了")

    persona = current_personality.get(session_key, "元气可爱")
    external_personas = ["口蜜腹剑鱼", "病娇鱼", "杠精鱼", "伪君子鱼", "圣母婊鱼", "色情鱼", "闷骚鱼"]
    is_external = persona in external_personas

    # ① 首选：算力机 indextts（在线才试）
    if is_external and USE_LOCAL_TTS and _compute_online():
        from plugins.tool_dispatcher import STATUS
        task_id = f"rap_tts_{int(time.time())}"
        STATUS["tasks"][task_id] = {"type": "tts", "status": "pending", "text": lyrics, "output": ""}
        logger.info(f"[CHAT] 触发本地 KKI 念 Rap，下发任务: {task_id}")
        st = await _wait_task(task_id, total=240, pickup_timeout=30)
        if st.get("status") == "completed":
            out_path = st.get("output")
            # 算力机失败时 output 是 {"error": ...} 字典，不是路径，别拿去 os.path.exists
            if isinstance(out_path, dict):
                logger.warning(f"[CHAT] 本地 Rap TTS 报错: {out_path}")
                out_path = None
            if isinstance(out_path, str) and out_path and os.path.exists(out_path):
                await send_voice_base64(bot, event, out_path)
                try:
                    os.remove(out_path)
                except Exception:
                    pass
            else:
                await bot.send(event, "（本地生成失败，文件丢失）")
            try:
                del STATUS["tasks"][task_id]
            except Exception:
                pass
            return True
        logger.warning(f"[CHAT] 本地 Rap TTS 未完成（{st.get('status')}），降级到云端 CosyVoice")
        await bot.send(event, "（算力机没连上，我用云端嗓子唱给你听）")

    # ② 降级：云端 CosyVoice

    logger.info("[CHAT] 触发云端 Rap...")
    clean_lyrics = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9，。！？、：；]", "", lyrics)
    audio_data = await AliyunVoiceTTS.sing_tts(clean_lyrics)
    if audio_data:
        try:
            from pathlib import Path
            rap_path = Path("data") / f"rap_{int(time.time())}_{random.randint(1000, 9999)}.wav"
            rap_path.parent.mkdir(exist_ok=True)
            from services.tts import clean_old_audio
            clean_old_audio(max_keep=10)
            with open(rap_path, "wb") as f:
                f.write(audio_data)
            await send_voice_base64(bot, event, str(rap_path.absolute()))
        except Exception:
            await bot.send(event, "呜呜，嗓子突然哑了，没唱出来……")
    else:
        await bot.send(event, "呜呜，这 Beat 不行，没唱出来……")
    return True


# ================= 单条消息处理 =================
def _compute_online() -> bool:
    """
    算力机是否在线（看 tool_dispatcher 记录的心跳时间）。
    在线才走本地 indextts，否则直接降级云端 CosyVoice，省掉 25 秒白等。
    """
    try:
        from plugins.tool_dispatcher import STATUS, HEARTBEAT_TIMEOUT
        last = STATUS.get("last_heartbeat") or 0
        return bool(last) and (time.time() - last) < HEARTBEAT_TIMEOUT
    except Exception:
        return False


async def _wait_task(task_id: str, *, total: int = 120, pickup_timeout: int = 25) -> dict:
    """
    等算力机把任务做完。
    ⚠️ 关键：如果任务一直停在 pending（说明 node.py 根本没有这个处理器 / 算力机掉线），
       就 pickup_timeout 秒后提前放弃 —— 原来固定等 120~180 秒，
       用户发一句语音要干等两分钟才变成文字。
    """
    try:
        from plugins.tool_dispatcher import STATUS
    except Exception as e:
        logger.warning(f"[TASK] 取不到任务表: {e}")
        return {"status": "no_status"}

    waited = 0
    while waited < total:
        await asyncio.sleep(1)
        waited += 1
        st = STATUS["tasks"].get(task_id) or {}
        status = st.get("status")
        if status == "completed":
            return st
        if status in ("failed", "timeout"):
            return st
        if status == "pending" and waited >= pickup_timeout:
            return {"status": "unpicked"}
    return {"status": "timeout"}


# ================= 被讨厌就闭嘴 =================
def _note_dislike(gid: str, text: str, *, to_me: bool = False,
                  called_me: bool = False, replied_recently: bool = False) -> bool:
    """
    群友在嫌弃她吗？命中就给这个群挂一个静默窗口。

    分两档（防止「A 让 B 闭嘴」把她静默掉）：
      · 强信号：句子里有指代她的词（鱼/鲸鱼/机器人/逼鱼…）+ 负面词 → 一定是冲她
      · 弱信号：「闭嘴」「滚」「别说了」这种通用骂人话 → 还要有一条"冲她"的证据：
          @ 了她 / 喊了她名字 / 她 2 分钟内刚说过话
    只压制「主动开口」（插话 / 为图片开口），**@ 她或喊名字仍然回** ——
    被人叫了还不理，那是另一种没礼貌。
    """
    if not GROUP_MUTE_SEC or not text:
        return False
    t = str(text)
    try:
        strong = bool(_DISLIKE_STRONG_RE.search(t))
        weak = bool(_DISLIKE_WEAK_RE.search(t))
    except Exception:
        return False
    if not strong and not weak:
        return False
    if not strong and not (to_me or called_me or replied_recently):
        # 弱信号又没有任何"冲她"的证据 → 大概率在说别人，别自作多情
        logger.debug(f"[MUTE] {gid} 「{t[:20]}」看起来不是在说她 → 不静默")
        return False

    now = time.time()
    group_dislike_text[gid] = {"text": t[:60], "ts": now}
    if now >= group_mute_until.get(gid, 0):     # 只在刚开始静默时打一次，别刷屏
        logger.warning(
            f"[MUTE] {gid} 检测到群友不满（「{t[:30]}」"
            f"{'｜指向她' if strong else '｜@/喊名字/刚说过话'}）→ "
            f"主动闭嘴 {GROUP_MUTE_SEC:.0f}s（@ 她 / 喊名字仍会回）"
        )
    group_mute_until[gid] = now + GROUP_MUTE_SEC
    return True


def _mute_note(gid: str) -> str:
    """
    把「刚被嫌烦」这件事告诉 DeepSeek —— 光靠代码静默不够，
    她得知道自己现在不受欢迎，否则下一条照样往上凑。
    """
    info = group_dislike_text.get(str(gid)) or {}
    if not info:
        return ""
    if time.time() - float(info.get("ts") or 0) > GROUP_MUTE_SEC:
        return ""
    return (
        "▍群里的气氛 · 注意\n"
        f"刚才有人说「{info.get('text', '')}」——他们嫌你吵了。\n"
        "接下来：只在明确叫你（@ 你、喊你名字）的时候才开口；别人闲聊别插嘴。\n"
        "⚠️ 如果是明确叫你做事（答题、看链接、帮忙、回答问题），**正常做完**，"
        "别用「等会」「选完给你」敷衍；只有纯闲聊才一句短的，别贫、别追问、别解释。"
    )


def _is_muted(gid: str) -> bool:
    """这个群现在是不是该闭嘴。"""
    try:
        return time.time() < float(group_mute_until.get(str(gid), 0) or 0)
    except Exception:
        return False


def _is_pure_greeting(text: str) -> bool:
    """
    这条消息是不是「只有问候，没有别的内容」。

    「早安」「晚安啦」「各位早上好」「睡了睡了」→ True
    「早安，今天那个 bug 修了吗」→ False（有实质内容，该回）
    """
    s = str(text or "").strip()
    if not s:
        return False
    # 去掉 [图片]/[表情]/<at:...> 这类标记，只留文字
    s = re.sub(r"\[[^\]]{0,12}\]|<[^>]{0,40}>", "", s)
    s = re.sub(r"\s+", "", s)
    if not s or len(s) > 16:
        return False
    return bool(_GREET_RE.match(s))


def _is_closing(text: str) -> bool:
    """是不是「聊完了」的收尾语（睡了/算了/先这样/拜拜）。"""
    s = re.sub(r"\[[^\]]{0,12}\]|<[^>]{0,40}>", "", str(text or ""))
    s = re.sub(r"[\s\W_]+", "", s)
    if not s or len(s) > 8 or "?" in str(text) or "？" in str(text):
        return False
    return s in _CLOSING


def _is_op_short(text: str) -> bool:
    """
    这条是不是「操作性短指令」（扫扫 / 来了 / 发一下 / 装这个）。

    只做**整句精确匹配**（去标点后 ≤4 字且在表里），
    所以「小肥鱼在吗」「好的我来」这种不会被误杀。
    """
    s = re.sub(r"\[[^\]]{0,12}\]|<[^>]{0,40}>", "", str(text or ""))
    s = re.sub(r"[\s\W_]+", "", s)
    if not s or len(s) > 4:
        return False
    return s in _OP_SHORT


def _text_similar(a: str, b: str) -> float:
    """两句话有多像（0~1）。用 difflib，不引额外依赖。"""
    import difflib
    x = re.sub(r"\s+", "", str(a or ""))
    y = re.sub(r"\s+", "", str(b or ""))
    if not x or not y:
        return 0.0
    return difflib.SequenceMatcher(None, x, y).ratio()


def _is_repeat(new: str, prev: str) -> tuple:
    """
    新回复是不是在重复上一句。返回 (是否重复, 相似度)。

    两条判据（任一命中就算重复）：
      ① 整体相似度 ≥ REPLY_DEDUP_RATIO
      ② **前缀高度重合**且占了短句一半以上 —— 治「同一句话换个结尾」：
         「你满意了？我还没说话呢」vs「你满意了？我满意了」相似度只有 0.70，
         单靠 ① 会漏掉，但前缀 6 个字一样，明显是在重复。
    短句（<5 字）不走 ②，否则「哈哈哈」和「哈哈」会被误判。
    """
    x = re.sub(r"\s+", "", str(new or ""))
    y = re.sub(r"\s+", "", str(prev or ""))
    if not x or not y:
        return False, 0.0
    sim = _text_similar(x, y)
    # 太短的（「嗯」「哈哈」「草」）不判：这类词天然会重复，
    # 而且单发一次无伤；判了反而让她像哑巴。
    if len(x) < 4 or len(y) < 4:
        return False, sim
    if sim >= REPLY_DEDUP_RATIO:
        return True, sim
    n = min(len(x), len(y))
    if n >= 5:
        pre = 0
        for i in range(n):
            if x[i] != y[i]:
                break
            pre = i + 1
        if pre >= 5 and pre >= 0.5 * n:
            return True, sim
    return False, sim


# ================= 高刷队列清理 =================
def _burst_count(session_key: str, window: float = None) -> int:
    """
    这个会话（群 / 私聊）最近 window 秒内来了几条**别人**的消息（含刚刚这条）。

    入参是 session_key（group_xxx / private_yyy），所以天然按会话隔离。
    NapCat 回显的她自己那条已经在 _need_reply 里排除了。

    ⚠️ 只统计、**不删**旧时间戳 —— 删了就没法用更大的窗口复查了。
       内存由 deque(maxlen=64) 兜住，不会涨。
    """
    w = BURST_WINDOW_SEC if window is None else window
    dq = burst_msg_ts.get(session_key)
    if not dq:
        return 0
    now = time.time()
    n = 0
    for ts in reversed(dq):          # 从最新往回数，一超出窗口就停
        if now - ts > w:
            break
        n += 1
    return n


def _is_mention_item(item, session_key: str = "") -> bool:
    """
    队列里这一条是不是「在叫我」：真 @，或者喊了我名字/称呼。

    高刷清理时**只留这类**，其余（关注期顺手接的闲聊）全部丢掉。
    """
    try:
        ev, text, _ctx = item
    except Exception:
        return False
    try:
        _tome = getattr(ev, "is_tome", None)
        if callable(_tome) and _tome():
            return True
    except Exception:
        pass
    try:
        return _calls_me_by_name(str(text or ""), session_key or _get_session_key(ev),
                                 str(getattr(ev, "user_id", "")))
    except Exception:
        return False


def _flush_stale_queue(session_key: str) -> list:
    """
    清空**这个会话**的待处理聊天队列，只保留「在叫我」的消息，返回被丢掉的那些。

    ⚠️ 正在处理的那条早就被 worker `get()` 取走了，不在队列里，所以不受影响。
    ⚠️ 保留的条目会**原样放回**（顺序不变），因为它们还要正常回复。
    ⚠️ **只动聊天队列**（session_queues[session_key]）：
       · indextts 语音、翻唱、音频分离这些长任务在 tool_dispatcher 的任务表里
         （STATUS["tasks"]），由算力机异步跑，跟聊天队列是两套东西，
         清队列永远不会打断或取消它们。
       · **影子回答**（referee 场景②，蒸馏用、不发群）也不在这里：
         它是 referee 直接 await _SHADOW_FN 跑的，问题记录靠
         note_peer_question 存在 referee._peer_questions 里，两者都跟
         session_queues 无关 —— 清队列清不到它。
    ⚠️ 按 session_key 隔离：group_xxx 和 private_yyy 各清各的，互不牵连。
    """
    q = session_queues.get(session_key)
    if q is None:
        return []
    keep, dropped = [], []
    while True:
        try:
            item = q.get_nowait()
        except asyncio.QueueEmpty:
            break
        except Exception:
            break
        (keep if _is_mention_item(item, session_key) else dropped).append(item)
    for item in keep:
        q.put_nowait(item)
    return dropped


# 同一条消息只回一次：NapCat 重投、vision 与 chat 双路入队，都会让同一个
# message_id 进两次队列 —— 群里看到的就是「一张图回两条」（用户 2026-11-19）。
_REPLIED_MIDS: "OrderedDict" = OrderedDict()


def _already_replied(key: str, mid: str, ttl: float = 600.0) -> bool:
    """第一次见到这条消息返回 False 并登记；再见到返回 True（跳过）。"""
    if not mid:
        return False
    now = time.time()
    k = f"{key}:{mid}"
    if k in _REPLIED_MIDS:
        return True
    _REPLIED_MIDS[k] = now
    for kk, ts in list(_REPLIED_MIDS.items()):
        if now - float(ts or 0) > ttl:
            _REPLIED_MIDS.pop(kk, None)
    while len(_REPLIED_MIDS) > 2000:
        _REPLIED_MIDS.popitem(last=False)
    return False


async def _session_worker(bot: Bot, session_key: str):
    queue = session_queues[session_key]
    last = session_last_reply[session_key]
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=30)
        except asyncio.TimeoutError:
            session_workers.pop(session_key, None)
            return
        _mid0 = str(getattr(item[0], "message_id", "") or "")
        if _already_replied(session_key, _mid0):
            logger.info(f"[DEDUP] {session_key} 消息 {_mid0} 已经处理过 → 跳过（防一张图回两条）")
            continue
        now_mono = time.monotonic()
        # 单群可以覆盖回复间隔（测试群压到 4s，见 services/group_tuning.py）
        wait = (last + group_tuning.reply_interval(session_key, REPLY_INTERVAL)) - now_mono
        if wait > 0:
            await asyncio.sleep(wait)

        # ---- 群聊分段：等这一段说完再回（用户 2026-11-22）----
        # 以前只合并「同一用户连续几条」，群里多人高频率提问时仍会一条一回，
        # 队列越堆越长。现在：所有群消息先进入 segments 的 buffer，worker 被触发后
        # 等「安静 SEGMENT_WAIT_SEC 秒」或「攒满 SEGMENT_MAX_SEC 秒」，
        # 把整段一起取走 —— 回复、心情判读、收发件人判断都用这一段。
        _seg_records = []
        _seg_addressed = None
        if session_key.startswith("group_"):
            _seg_records = await segments.wait_close(session_key)
            if _seg_records:
                # ⚠️ 2026-11-23 串线根因：一段 3 条消息被整段处理了一次，
                #    但队列里还留着这 3 条各自的 item → 本轮结束后它们会被
                #    当成「新消息」再单独回一遍（实测同两条回了三次）。
                #    整段取走后，把段内每条 mid 都登记为已处理，后续队列项直接 DEDUP。
                for _r in _seg_records:
                    _rm = str(_r.get("mid") or "")
                    if _rm:
                        _already_replied(session_key, _rm)
                ev = _seg_records[-1].get("ev") or item[0]
                text = segments.format(_seg_records)
                _gid_s = str(getattr(ev, "group_id", "") or "")
                # 👇 上下文和判心情**同源同条数**（用户 2026-11-22）：
                #    都读 group_scene 里「收发件人标签处理过的历史」，
                #    不再一边用 recent_group_msgs、一边让判读自己拉 20 条。
                _seg_mids = {str(r.get("mid") or "") for r in _seg_records
                             if str(r.get("mid") or "")}
                # mid 缺失时按 (qq|文本前缀) 兜底排除
                _seg_txts = {f"{r.get('uid') or ''}|{str(r.get('text') or '')[:40]}"
                             for r in _seg_records if str(r.get("text") or "")}
                # 只用「处理过的历史」；现场没料就空着，绝不再退回未处理的
                # recent_group_msgs（用户 2026-11-23：「上下文一定要收发件人处理过」）。
                ctx = group_scene.history_lines(
                    _gid_s, mood_judge.CTX_LINES,
                    exclude_mids=_seg_mids, exclude_texts=_seg_txts)
                # 整段里只要有一条 @ 了她 / 被判定为对她说 / 喊了她名字，就算在叫她。
                # ⚠️ 不能只看 mentioned：喊名字（「小鲸鱼你觉得呢」）没有 at 段，
                #    以前会被 _seg_addressed=False 覆盖掉 → 被叫了也不回。
                _seg_addressed = any(
                    segments.is_addressed(r)
                    or _calls_me_by_name(
                        str(r.get("text") or ""), session_key,
                        str(r.get("uid") or ""))
                    for r in _seg_records
                )
                logger.info(f"[SEGMENT] {session_key} 这一段 {len(_seg_records)} 条，一起处理")
            else:
                ev, text, ctx = item
        else:
            # 私聊保持原样：同一用户快速连发多条 → 合成一条回复
            _dw, _dwhy = _debounce_wait(session_key, item[0], str(item[1] or ""))
            if _dw > 0:
                logger.debug(f"[DEBOUNCE] {session_key} 等 {_dw:.1f}s（{_dwhy}）")
                await asyncio.sleep(_dw)
            _batch = [item]
            _uid = getattr(item[0], "user_id", None)
            _dq = getattr(queue, "_queue", None)
            while _dq:
                _nxt = _dq[0]
                if _uid is not None and getattr(_nxt[0], "user_id", None) == _uid:
                    _batch.append(queue.get_nowait())
                else:
                    break
            if len(_batch) > 1:
                ev = _batch[0][0]
                ctx = _batch[0][2]
                _texts = [str(t).strip() for (_, t, _) in _batch if str(t).strip()]
                text = ("（对方连发了几条，综合考虑一起回，别逐条回）\n"
                        + "\n".join(f"{i}. {t}" for i, t in enumerate(_texts, 1)))
                logger.info(f"[MERGE] {session_key} 合并了 {len(_batch)} 条连发消息")
            else:
                ev, text, ctx = item
        # 分段为空（例如刚重启、buffer 被取走 / 高刷清队）也走同一条链路：
        # 群聊上下文统一用「收发件人处理过的历史」，不再用 recent_group_msgs 原串。
        # ⚠️ 2026-11-23：这段原来缩进在「私聊 else」里面 → 群聊根本没执行到，
        #    分段为空时又把未处理的 recent_group_msgs 塞给模型。
        if session_key.startswith("group_") and not _seg_records:
            _gid_norm = str(getattr(ev, "group_id", "") or "")
            _hl = group_scene.history_lines(_gid_norm, mood_judge.CTX_LINES)
            if _hl:
                ctx = _hl
            else:
                ctx = []
        speaker_qq = str(ev.user_id)
        speaker_name = display_name(ev)

        # 🌙 这次回复的对象：决定 prompt 里用谁的白月光说话方式。
        #    · 这一段里有 @ 她 / 喊她名字的 → 回最后那个叫她的人
        #    · 没 @ 但在关注期（在接某个人的话）→ 回那个人
        #    · 都没有（回大家 / 吐槽）→ 空，用群白月光
        _moon_target_qq = ""
        try:
            if _seg_records:
                _addr_recs = [
                    r for r in _seg_records
                    if segments.is_addressed(r)
                    or _calls_me_by_name(
                        str(r.get("text") or ""), session_key,
                        str(r.get("uid") or ""))
                ]
                if _addr_recs:
                    _moon_target_qq = str(_addr_recs[-1].get("uid") or "")
            if (not _moon_target_qq
                    and session_key.startswith("group_")):
                try:
                    if _focus_active(session_key, ev.user_id):
                        _moon_target_qq = str(ev.user_id)
                except Exception:
                    pass
        except Exception:
            _moon_target_qq = ""

        # 2026-11-20 记录模型 v2：每个 turn 先落一条 pending（记忆全记）。
        # 判读回来再由 mood_judge.patch_turn 补 delta/salience/valence/mood_word。
        _turn_id = "%s:%s" % (session_key, getattr(ev, "message_id", "") or int(time.time() * 1000))
        try:
            memory_v2.upsert_turn({
                "turn_id": _turn_id,
                "session": session_key,
                "gid": str(getattr(ev, "group_id", "") or ""),
                "about_qq": speaker_qq,
                "kind": "event",
                "title": str(text or "")[:30],
                "detail": str(text or ""),
                "source": "turn",
                "importance": 0.5,
            })
        except Exception as e:
            logger.debug(f"[TURN] 写 pending 失败: {type(e).__name__}: {e}")

        # 2026-11-20 审计 A3：标记「这个会话正在处理」，主动插话看到就跳过。
        _WORKER_BUSY[session_key] = True
        try:
            # ---------- 1) 更新人物档案（A） ----------
            try:
                profiles.upsert_profile(
                    speaker_qq,
                    nickname=speaker_name,
                    aliases=nickname_variants(ev),
                    owner=is_owner(speaker_qq),
                    level="owner" if is_owner(speaker_qq) else "member",
                )
                profiles.touch_profile(speaker_qq, text)
                # 说话方式（是否有 face）
                has_face = any(seg.type in ("face", "mface") for seg in ev.get_message())
                profiles.update_style(speaker_qq, msg_len=len(text), has_emoji=has_face)
                # 👇 攒够发言就后台挖一次档案（简介/性格/事情）。
                #    以前只有群名片和别名，档案卡上「内容」和「事情」永远是空的
                #    （用户 2026-11-20：「群友档案只有称呼，没有内容和事情」）。
                _gid_p = str(getattr(ev, "group_id", "") or "")
                if profile_miner.note_message(_gid_p, speaker_qq):
                    logger.info(f"[PROFILE-MINE] {speaker_qq} 攒够了，后台挖档案")

                    async def _mine_bg(_q=speaker_qq, _g=_gid_p):
                        try:
                            await profile_miner.mine(_q, _g)
                        except Exception as _e:      # 别让 create_task 冒未取回异常
                            logger.debug(f"[PROFILE-MINE] 后台挖掘异常: "
                                         f"{type(_e).__name__}: {_e}")

                    asyncio.create_task(_mine_bg())
            except Exception as e:
                logger.debug(f"[PROFILE] 更新失败: {e}")

            # ---------- 2) 关键词情绪：降级为兜底（主通道是 DeepSeek 的 [[MEMO]]） ----------
            delta, strong_emotion = 0.0, False
            try:
                delta, strong_emotion = mood.judge_message_mood(text)
            except Exception as e:
                logger.debug(f"[MOOD] 关键词兜底判断失败: {e}")

            # ---------- 2.5) 这条消息里的人称（@ 了谁 / 是不是在叫她） ----------
            # 用户 2026-11-18：她分不清你我他。把「这条 @ 的是谁」明确写进 prompt，
            # 并把那个人的档案一起带上（她自己说「理论上该查那个人的档案」）。
            _focus_qqs = []
            try:
                _focus_qqs = [
                    str(q) for q in _canon_at(_at_targets(ev))
                    if str(q) not in ("all", "everyone")
                    and not _is_self_qq(q, ev)
                ]
                _rp = getattr(ev, "reply", None)
                _rq = str(getattr(getattr(_rp, "sender", None), "user_id", "") or "")
                if _rq:
                    _rq = identity_mod.canon(_rq) or _rq
                if (_rq and not _is_self_qq(_rq, ev)
                        and _rq not in _focus_qqs):
                    _focus_qqs.append(_rq)
            except Exception:
                _focus_qqs = []
            try:
                _addressed = bool(getattr(ev, "is_tome", lambda: False)()) \
                    or _calls_me_by_name(
                        _QUOTE_BLOCK.sub("", text).strip() or text,
                        session_key, speaker_qq,
                    )
            except Exception:
                _addressed = bool(getattr(ev, "is_tome", lambda: False)())
            # 群聊分段：整段里只要有一条 @ 了她 / 点名她，就算在叫她
            if _seg_addressed is not None:
                _addressed = bool(_seg_addressed)

            # ---------- 3) 组装上下文（记忆 + 档案 + 心情） ----------
            # 群聊现场：谁 → 谁、几条线程、这条是不是在叫她
            _scene_text = ""
            try:
                _gid_now = str(getattr(ev, "group_id", "") or "")
                if _gid_now:
                    _self_id_now = _self_id(ev, bot)
                    # 👇 先让「审核 LLM」纠错/补齐硬逻辑，再组装现场
                    #    只审 大家/不确定/低置信 的消息；@/引用 不审。
                    #    失败/超时静默退回硬逻辑，不影响回复。
                    if scene_auditor.enabled():
                        try:
                            await asyncio.wait_for(
                                scene_auditor.audit_recent(
                                    _gid_now, bot_qq=_self_id_now,
                                    bot_name=_bot_name(_gid_now, _self_id_now),
                                    names=_scene_names(_gid_now),
                                ),
                                timeout=max(2.0, scene_auditor.TIMEOUT + 1),
                            )
                        except Exception as e:
                            _warn_audit_wire("回复前审核", e)
                    _snap = group_scene.snapshot(
                        _gid_now,
                        bot_qq=_self_id_now,
                        my_names=_my_names(session_key, speaker_qq),
                    )
                    _scene_text = group_scene.format_block(_snap)
                    _addr_line = group_scene.addressee_line(_gid_now, _self_id_now)
                    if _addr_line:
                        _scene_text = (
                            "▍这条消息的收件人\n" + _addr_line
                            + (("\n\n" + _scene_text) if _scene_text else "")
                        )
            except Exception as e:
                logger.debug(f"[SCENE] 现场组装失败: {type(e).__name__}: {e}")
                _scene_text = ""
            try:
                text_for_llm = build_prompt(
                    session_key=session_key,
                    user_text=(text
                               + ("\n" + str(getattr(ev, "_cmd_hint", "") or "")
                                  if getattr(ev, "_cmd_hint", "") else "")
                               # 破冰提示：他刚道歉，给她一个台阶（回不回仍然她自己定）
                               + (f"\n（他刚在哄你/道歉了。你之前安静是因为："
                                  f"{getattr(ev, '_thaw_note', '')}。"
                                  "想原谅就正常说话；还不想理就 stay_silent。）"
                                  if getattr(ev, "_thaw_note", "") else "")),
                    speaker_qq=speaker_qq,
                    speaker_name=speaker_name,
                    persona_text="",  # 人格由 call_llm 内部处理
                    # 刚被嫌烦过 → 让 DeepSeek 也知道气氛变了
                    # ⚠️ 这里是 worker，事件变量叫 ev（不是 event）——
                    #    写错会抛 NameError，被下面的 except 吞掉 → 整个上下文退回纯文本
                    atmosphere=(_mute_note(str(ev.group_id))
                                if isinstance(ev, GroupMessageEvent) else ""),
                    focus_qqs=_focus_qqs,
                    addressed=_addressed,
                    scene_text=_scene_text,
                )
            except Exception as e:
                logger.warning(f"[PROMPT] 组装上下文失败，退回纯文本: {e}")
                text_for_llm = text

            # 👇 digest 提醒：连续好几轮一条记忆都没写，提醒她该记了
            _streak = _no_memo_streak.get(session_key, 0)
            if _streak >= DIGEST_AFTER:
                text_for_llm = (
                    f"（你已经连续 {_streak} 轮没记任何东西了。"
                    f"如果刚才聊的事值得记住，用 [[MEMO|...]] 记一条；"
                    f"真的没什么值得记的就不用管。）\n\n" + text_for_llm
                )
                logger.info(f"[DIGEST] {session_key} 连续 {_streak} 轮没写记忆，已提醒")
                _no_memo_streak[session_key] = 0

            # 把 bot/event 交给工具层：sing 工具要在后台真的去唱，需要它们
            # addressed：这条消息是不是在叫她 —— 群聊里没被叫时，禁掉唱歌和语音
            try:
                _addressed = bool(
                    (isinstance(ev, GroupMessageEvent) and ev.is_tome())
                    or _calls_me_by_name(text, session_key, str(ev.user_id))
                )
                if _seg_addressed is not None:
                    _addressed = bool(_seg_addressed)
                # 👇 这条消息后面又来了几条（她被刷上去了没）
                #    用户 2026-11-12：「并没有对回复的那句引用回复」——
                #    她回的是被刷上去的那条，但协议里写着「回当前这条不用引用」，
                #    于是不引用 → 群友看不出她在回谁。这里把「后面还有几条」告诉她，
                #    让她必须引用。
                _newer = 0
                try:
                    _gid_s = str(getattr(ev, "group_id", "") or "")
                    _mid_s = f"#{getattr(ev, 'message_id', '')}"
                    if _gid_s:
                        _lines = list(recent_group_msgs.get(_gid_s, []))
                        for _i, _ln in enumerate(_lines):
                            if _mid_s in _ln.split(": ", 1)[0]:
                                _newer = max(0, len(_lines) - _i - 1)
                                break
                except Exception:
                    _newer = 0

                llm_mod.set_tool_context(bot=bot, event=ev, speaker_qq=speaker_qq,
                                         addressed=_addressed,
                                         newer_count=_newer,
                                         # NapCat 工具通知主人时要写清「谁」在要求做事
                                         speaker_name=str(speaker_name or ""))
            except Exception:
                pass

            # ---------- 3.5) 事前参考：GLM 先拿同一份模板答一遍 ----------
            # 最终决策权在 DeepSeek：它看到两个版本，自己决定采信哪些名词。
            # 拿不到（没 key / 短寒暄 / 超时）就静默跳过，不影响正常回复。
            _persona_text = ""
            try:
                from services.persona import persona_for as _pf
                _persona_text = _pf(session_key)[1] or ""
            except Exception:
                _persona_text = ""
            _prompt_base = text_for_llm      # 注入参考之前的原始模板（事后 diff 要用这份）
            _ref1 = ""
            if referee.PREFLIGHT_ENABLED:
                try:
                    # 加超时：GLM 慢就不等它，别把回复拖成 8 秒
                    _ref1 = await asyncio.wait_for(
                        referee.preflight(session_key, text, _prompt_base, _persona_text),
                        timeout=referee.PREFLIGHT_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        f"[REF] preflight 超时（>{referee.PREFLIGHT_TIMEOUT}s），"
                        f"本轮不用参考，直接回"
                    )
                    _ref1 = ""
                except Exception as e:
                    logger.debug(f"[REF] preflight 异常（忽略）: {e}")
            if _ref1:
                text_for_llm = (
                    f"{text_for_llm}\n\n"
                    f"【另一个模型对同一段上下文的回答（仅供参考，可能有错）】\n{_ref1}\n\n"
                    f"【要求】它只是参考，**不是你要回的内容**；"
                    f"如果它和当前消息无关、或者明显在答别的事，**直接忽略，别顺着它接**。"
                    f"事实、专有名词、人名作品名可以参考它；"
                    f"但语气、称呼、要不要说这些由你自己决定，用你自己的话说，"
                    f"不要照抄，也不要提「参考」「另一个模型」这类字眼。"
                )

            # 显式告诉 call_llm 这轮带的是图还是视频 —— 别让它靠 prompt 里的字面量去猜。
            # ⚠️ 视频（B 站抽帧总结 / 第三方视频解析）也会走这条通道，话术必须区分，
            #    否则会出现「这段视频…这张图…」自相矛盾（2026-11-17）。
            _media_kind = llm_mod.media_kind_of(text_for_llm)
            _is_vision = (_media_kind == "image")
            # 私聊先挂「正在输入」，等模型回来再取消（失败静默）
            await _typing(bot, ev, True)
            try:
                reply, is_query_image = await call_llm(
                    session_key, text_for_llm, group_context=ctx,
                    speaker_qq=speaker_qq, speaker_name=speaker_name,
                    is_vision=_is_vision, media_kind=_media_kind,
                    moon_target_qq=_moon_target_qq,
                )
            finally:
                await _typing(bot, ev, False)

            # ---------- 4) 拆出 [[MEMO]] / [[CALL]] / [[FOCUS]] ----------
            # 带上被引用原文：她如果开头照抄，parse_reply 会剥掉（2026-11-23）。
            parsed = parse_reply(reply, quote_text=getattr(ev, "_quote_text", ""))

            # 👇 她自己在回哪一句？日志里直接看得到，用来诊断「是不是在接历史旧话」
            _focus = str(parsed.get("focus") or "").strip()
            if _focus and _focus not in ("当前", "current", "0", "现在"):
                try:
                    _n = int(_focus)
                except Exception:
                    _n = 0
                if _n > 0:
                    _line = (ctx[_n - 1] if _n <= len(ctx) else "（编号越界）")
                    logger.warning(
                        f"[FOCUS] ⚠️ 她回的是历史第 {_n}/{len(ctx)} 条：{str(_line)[:70]}"
                    )
                else:
                    logger.info(f"[FOCUS] 标注无法解析：{_focus!r}")
            elif _focus:
                logger.info("[FOCUS] ✅ 她回的是当前消息")
            else:
                logger.info("[FOCUS] 未标注（按当前消息算）")
            memos = parsed.get("memos") or []
            memo_emotions = []
            # 她自己写的心情原话（[[MOOD|…]]），和 MEMO 独立
            _mood_label = _apply_mood_label(parsed, session_key)
            # 👇 判读（影子运行）：她写的感觉 + 心理活动 + **最近 20 条群聊**
            #    → 第三方模型给「心情名词 + -1.00~1.00」。先只记不用，
            #    验证准了再切进 mood 的所有链路（用户 2026-11-19）。
            try:
                # 心里话两个来源：[[THINK|…]]（每轮必写）+ MEMO 的 think:（触动时）
                _tparts = [str(parsed.get("think") or "").strip()]
                _tparts += [str(mm.get("think") or "").strip() for mm in memos]
                _think = "；".join([x for x in _tparts if x])
                # 她没写也判：服务里按**条数**拉最近 20 条群聊，5 分钟一次。
                # src/reply 一起落盘 —— 用户 2026-11-20：
                # 「它总是综合最后在一起算一件事，而不是每句话都反馈心理活动。」
                _myreply = "；".join(
                    str(i.get("value") or "") for i in (parsed.get("items") or [])
                    if i.get("kind") == "text").strip()
                # 他刚道歉/哄她 → 这是「修复事件」：强制判一次（绕过节流），
                # 让判读把好感回暖、心情回升写进去。用户 2026-11-20：
                # 「道歉可以破冰，可以回暖好感以及回升一些心情。」
                _thaw_note = str(getattr(ev, "_thaw_note", "") or "")
                _judge_src = (segments.short(_seg_records, n=4)
                              if _seg_records else f"{speaker_name}: {text}")
                # 👇 她明确引用/回复了某一条 → 这次心情/好感算到**那个人**头上，
                #    不再默认算给「这一段最后一个说话的人」（回复错人的另一半原因）。
                _reply_to_qq = ""
                try:
                    _rid0 = ""
                    for _it in (parsed.get("items") or []):
                        _r0 = str(_it.get("replyTo") or "").strip()
                        if _r0:
                            _rid0 = _r0
                            break
                    if _rid0 and _seg_records:
                        for _r in _seg_records:
                            if str(_r.get("mid") or "") == _rid0:
                                _reply_to_qq = str(_r.get("uid") or "")
                                break
                    if _reply_to_qq and _reply_to_qq != speaker_qq:
                        logger.info(f"[REPLY] 她回的是 {_reply_to_qq}"
                                    f"（这一段最后说话人是 {speaker_qq}）")
                except Exception:
                    _reply_to_qq = ""
                _judge_qq = _reply_to_qq or speaker_qq
                _spawn_mood_judge(
                    session_key, _mood_label, ctx, _think,
                    src=_judge_src,
                    reply=_myreply, speaker_qq=_judge_qq,
                    hint=(f"他刚道歉/哄她（原来的心结：{_thaw_note}）"
                          if _thaw_note else ""),
                    repair=bool(_thaw_note), force=bool(_thaw_note),
                    turn_id=_turn_id,
                    addressed=bool(_addressed))
            except Exception as e:
                logger.debug(f"[MOOD-JUDGE] 触发失败: {type(e).__name__}: {e}")

            # 处理过的整段存一份：下一轮上下文 / 翻历史都读这份，
            # 不再把 NapCat 原生消息直接塞给模型。
            try:
                if _seg_records:
                    _mv = float(mood.get_mood(session_key).get("value") or 0.0) / 100.0
                    segments.add_processed(
                        session_key, text, mood=_mood_label, value=_mv,
                        addressee=("叫了她" if _seg_addressed else "大家"))
            except Exception as _se:
                logger.debug(f"[SEGMENT] 存处理过的段失败: {type(_se).__name__}: {_se}")

            # ---------- 4.0) 称谓档案：她自己判断「我们以后怎么互相称呼」 ----------
            # 和 `/叫你`（给她起名）/ `/叫我`（她怎么叫你）的区别：
            #   那两条是硬规则指令（用户必须背格式）；
            #   这里是软表达（「我可以叫你小鲸鱼吗？」），由 DeepSeek 自己决定要不要记。
            for c in (parsed.get("calls") or []):
                try:
                    bot_alias = str(c.get("bot") or "").strip()
                    user_name = str(c.get("user") or "").strip()
                    if not bot_alias and not user_name:
                        continue
                    # ⚠️ 用 call_name 而不是 nickname：nickname 每轮都会被群名片
                    #    覆盖写回（worker 第 1 步），存不住聊出来的称呼。
                    profiles.set_call_names(
                        speaker_qq, call_name=user_name, bot_alias=bot_alias
                    )
                    # 称呼变了 → 群聊标签缓存作废，否则上下文里还是旧名字
                    if user_name:
                        _clear_label(speaker_qq)
                        group_roster.invalidate(getattr(ev, "group_id", "") or "")
                    try:
                        memory_v2.remember(
                            text=(
                                f"我和{speaker_name}的称呼："
                                f"他叫我「{bot_alias or '没定'}」，"
                                f"我叫他「{user_name or speaker_name}」"
                            ),
                            kind="call",
                            about_qq=speaker_qq,
                            importance=0.85,
                        )
                    except Exception:
                        pass
                    logger.info(
                        f"[CALL] {speaker_qq} 称谓更新："
                        f"他叫她「{bot_alias or '—'}」/ 她叫他「{user_name or '—'}」"
                    )
                except Exception as e:
                    logger.debug(f"[CALL] 写称谓失败: {e}")

            # 判读结果（够新才用）：决定这条记忆的「情绪刻痕」强度
            _jd = {}
            try:
                _jd = mood.get_judge(session_key)
            except Exception:
                _jd = {}
            # ⚠️ 两件事必须分开（用户 2026-11-20 反复强调）：
            #   · _jv      = 当前心情**水平**（现在进行时）→ 只影响"这句话怎么说"
            #   · _jdelta  = 这一次的**涨幅**（判读原值之差）→ 记忆刻痕 / 好感 / 档案
            #   以前这里用 abs(_jv)（水平）当刻痕，等于"我现在很开心 → 这条记忆很重要"，
            #   是错的。应该是"这件事让我忽然开心了多少"。
            _jv = float(_jd.get("value") or 0.0)
            _jdelta = float(_jd.get("delta") or 0.0)
            _jsal = min(1.0, abs(_jdelta) / 0.5)
            # 判读给的心情词：记忆/档案都带上它（用户 2026-11-20：
            # 「记忆记录、档案记录可以记录心里话加上 llm 判断的心情」）
            _jword = str(_jd.get("word") or "").strip()
            _jd_fresh = bool(_jd) and (
                time.time() - float(_jd.get("at") or 0) <= mood_judge.FRESH_SEC)

            for memo in memos:
                emo = memo.get("emotion") or "neutral"
                memo_emotions.append(emo)
                mtext = memo["text"]
                imp = memo.get("importance")
                if imp is None:
                    imp = 0.6 if emo == "neutral" else 0.75
                # ① 长期记忆（带情绪刻痕）
                # kind:bond = 「相处经历」（不需要触动，只记人跟人之间发生了什么）
                # 它只进 bond 时间线 + 低权重记忆，**不动心情** ——
                # 否则「他让我唱歌」这种中性事件会把心情拉平。
                _is_bond = str(memo.get("kind") or "").lower() == "bond"
                # 👇 「发件人 / 收件人标签」落到记忆上（用户 2026-11-19）：
                #    记忆要带群号，才能实现 memory_v2.group_ok() 的「群内记忆只在群内用」。
                #    以前 group_ok 的过滤逻辑存在，但**没有任何代码写「群号:」前缀** →
                #    过滤永远不生效，别的群的事会漏到当前群（等于没有隔离）。
                _mtext = mtext
                _mgid = str(getattr(ev, "group_id", "") or "")
                if _mgid:
                    _mtext = f"（群号:{_mgid}）{mtext}"
                # 👇 刻痕门槛（2026-11-22）：只收「真的动过」的事。
                #   以前写成 `emo != neutral` → 她随手写个「开心/烦躁」就进刻痕，
                #   结果记忆和档案里全是流水账（用户：「标了很多很平常的事情」）。
                #   现在：有判读就只看 |Δ|；没判读才退回 MEMO，而且只认强情绪。
                _memo_min = float(tuning.f("MEMORY_EMOTION_MIN_DELTA"))
                _strong_emo = str(emo) in ("sad", "angry", "lonely", "touched",
                                           "love", "excited")
                _hit_by_judge = bool(_jd_fresh) and abs(float(_jdelta or 0)) >= _memo_min
                _hit_by_memo = (not _jd_fresh) and _strong_emo
                _emo_hit = (not _is_bond) and (_hit_by_judge or _hit_by_memo)
                try:
                    memory_v2.remember(
                        text=_mtext,
                        kind=memo.get("kind") or "impression",
                        about_qq=speaker_qq,
                        tags=memo.get("tags") or [],
                        importance=0.45 if _is_bond else float(imp),
                        # 刻痕 = |Δ|（这件事让她变了多少），不是当前心情水平。
                        # 判读够新就只看 Δ；没有判读才退回 MEMO 自报的情绪。
                        emotion_hit=_emo_hit,
                        emotion_weight=(0.0 if _is_bond
                                        else (_jsal if _jd_fresh
                                              else memory_v2.emotion_weight_of(emo))),
                        # 她当时的心情（水平）：只作为"这条记忆发生时的底色"记录
                        mood_word=(_jword if _jd_fresh else ""),
                        mood_value=(_jv if _jd_fresh else None),
                        # 涨幅三件套：记忆加粗 / 正负 / 检索加成 都靠它
                        delta=(_jdelta if _jd_fresh else None),
                        salience=(_jsal if _jd_fresh else None),
                        valence=((1.0 if _jdelta > 0 else -1.0 if _jdelta < 0 else 0.0)
                                 if _jd_fresh else None),
                        # 她当时的心里话：档案「印象最深刻的一件事」要连它一起给
                        mind=(str(_jd.get("mind") or "") if _jd_fresh else ""),
                        # 群隔离：sqlite 后端用 gid 列，JSON 后端仍靠文本前缀
                        gid=_mgid,
                        session=session_key,
                    )
                except Exception as e:
                    logger.debug(f"[MEMO] 写长期记忆失败: {e}")
                # ② 相处经历 / 相处印象：**只记影响过关系的事**（档案收敛）
                #    2026-11-20：中性经历只进记忆目录，不进档案，否则档案变流水账。
                # 档案只收「让她心情明显动过」的事：判据是 |Δ|，不是当前水平。
                # 0.15 ≈ 一次"明显被戳到"；小事（±0.05）不写进档案。
                # 档案同样只看「真的动过」：|Δ| >= 门槛；没判读时才退回强情绪。
                _bond_worthy = (_hit_by_judge
                                or ((not _jd_fresh) and _strong_emo))
                if _bond_worthy:
                    try:
                        profiles.add_bond_note(
                            speaker_qq, mtext, emotion=emo,
                            mood_word=(_jword if _jd_fresh else ""),
                            mind=(str(_jd.get("mind") or "") if _jd_fresh else ""),
                            delta=(_jdelta if _jd_fresh else None))
                    except Exception as e:
                        logger.debug(f"[MEMO] 写相处印象失败: {e}")
                else:
                    logger.debug("[MEMO] 中性经历只进记忆，不进档案")
                # ③ 心情值（只有真正触动的那类才动心情）
                if not _is_bond:
                    try:
                        # 传**原话**：`emotion:被夸得有点飘` 这种自由描述要存成她的心情
                        # （固定 9 个词只用来估数值方向）
                        mood.bump_by_emotion(
                            memo.get("emotion_raw") or emo,
                            reason=mtext, about_qq=speaker_qq,
                            session_key=session_key,
                        )
                    except Exception as e:
                        logger.debug(f"[MEMO] 心情更新失败: {e}")

            # digest 计数：这轮记了 → 清零；没记 → 累加（到阈值会在下一轮提醒）
            _no_memo_streak[session_key] = 0 if memos else (
                _no_memo_streak.get(session_key, 0) + 1
            )

            if memos:
                logger.info(
                    f"[MEMO] 她这一轮自己记了 {len(memos)} 条"
                    f"（{', '.join(memo_emotions)}）：{[m['text'][:20] for m in memos]}"
                )
            elif delta and not getattr(mood, "MOOD_FROM_JUDGE", True):
                # 她这轮没打 MEMO → 才退回关键词兜底（仅在 MOOD_FROM_JUDGE=false 时）。
                # 2026-11-20：数值只认第三方判读，关键词猜的不算。
                try:
                    mood.bump_mood(delta, session_key=session_key)
                    mood.log_shift(delta, emotion="keyword", reason=text[:60],
                                   about_qq=speaker_qq, session_key=session_key)
                except Exception as e:
                    logger.debug(f"[MOOD] 兜底心情更新失败: {e}")

            # ---------- 5) 写入这一轮的对话事件（给下一轮 recall 用） ----------
            # ⚠️ 存的是「去掉 MEMO 的干净正文」，否则协议会污染记忆
            clean_reply = " ".join(
                i["value"] for i in (parsed.get("items") or []) if i["kind"] == "text"
            ).strip() or reply
            try:
                if clean_reply and len(clean_reply.strip()) >= 5:
                    memory_v2.remember(
                        # 用「这是你之前的回复」而不是「我：」，避免模型把两条都当成别人说的
                        text=f"{speaker_name}：{text}\n{_SELF_PREFIX}{clean_reply[:120]}",
                        kind="event",
                        about_qq=speaker_qq,
                        importance=0.5,
                        emotion_hit=bool(memo_emotions) or strong_emotion,
                        # 维度②：取这一轮里最强的那个情绪
                        emotion_weight=(
                            max([memory_v2.emotion_weight_of(e) for e in memo_emotions])
                            if memo_emotions
                            else (0.8 if strong_emotion else 0.0)
                        ),
                    )
            except Exception as e:
                logger.debug(f"[MEM] 写记忆失败: {e}")

            if parsed["silent"]:
                logger.info(f"[CHAT] {session_key} 潜水")
                # 沉默也记一次「刚处理过」：安静状态下一批消息接连进来时，
                # 别每条都立刻再调一次模型（REPLY_INTERVAL 至少隔一下）。
                last = time.monotonic()
                session_last_reply[session_key] = last
                continue

            items = parsed["items"]
            if not items:
                continue

            # 👇 [[LONG]] 长文：私聊才需要标记（合并成一条走转发卡片）。
            #    群聊不靠标记：超过 20 字系统会自动合并转发，这里只把标记剥掉。
            if isinstance(ev, GroupMessageEvent):
                items = [
                    ({**i, "value": re.sub(r"^\s*\[{2,3}\s*LONG\s*\]{2,3}\s*", "",
                                           str(i["value"]))}
                     if i["kind"] == "text" else i)
                    for i in items
                ]
            elif any(i["kind"] == "text" and str(i["value"]).lstrip().startswith("[[LONG]]")
                     for i in items):
                _texts, _others = [], []
                for i in items:
                    if i["kind"] != "text":
                        _others.append(i)
                        continue
                    v = str(i["value"])
                    if v.lstrip().startswith("[[LONG]]"):
                        v = v.lstrip()[len("[[LONG]]"):]
                    if v.strip():
                        _texts.append(v.strip())
                _joined = "\n\n".join(_texts)
                items = ([{"kind": "text", "value": "[[LONG]]" + _joined}]
                         if _joined else []) + _others
                if not items:
                    continue

            # 👇 重复回复拦截：同一会话短时间内说过太像的话就不再发。
            #    真机踩过：图片回复「你满意了？我还没说话呢」，
            #    22 秒后插话又回「你满意了？我满意了」→ 群友「这肥鱼魔了」。
            _new_text = "".join(i["value"] for i in items if i["kind"] == "text").strip()
            _prev_text = last_text_map.get(session_key) or ""
            _prev_at = float(last_text_at.get(session_key) or 0)
            if (_new_text and _prev_text
                    and time.time() - _prev_at < REPLY_DEDUP_SEC):
                _dup, _sim = _is_repeat(_new_text, _prev_text)
                if _dup:
                    logger.info(
                        f"[DEDUP] {session_key} {time.time() - _prev_at:.0f}s 内"
                        f"说过太像的话（相似度 {_sim:.2f}）→ 这条不发"
                        f"｜新：{_new_text[:30]}｜旧：{_prev_text[:30]}"
                    )
                    continue

            # 👇 群里没在叫她 → 不许自己发语音（语音条在群里很突兀）
            #    用户 2026-11-11：「【玩】这类在群聊里最好是艾特了才用」
            #    注意顺序：先拦「她自己主动发的」，再走下面的 wants_voice
            #    （对方明确要语音是另一回事，那个该给）
            _is_grp = isinstance(ev, GroupMessageEvent)
            _called_me = _is_grp and (
                ev.is_tome() or _calls_me_by_name(text, session_key, str(ev.user_id))
            )
            if _is_grp and not _called_me and any(i["kind"] == "voice" for i in items):
                logger.info("[CHAT] 群里没被叫 → 语音降级成文字")
                items = [({"kind": "text", "value": i["value"]}
                          if i["kind"] == "voice" else i) for i in items]

            # 对方要求语音 → 整段变语音
            if wants_voice(text) and not any(i["kind"] == "voice" for i in items):
                all_text = "，".join([i["value"] for i in items if i["kind"] == "text"])
                if all_text and len(all_text) <= 300:
                    items = [{"kind": "voice", "value": all_text}] + [i for i in items if i["kind"] != "text"]

            # 心情影响语音/表情概率（B）—— 取当前会话的心情，不是全局
            try:
                factors = mood.mood_factors(session_key)
            except Exception:
                factors = {"voice": 1.0, "sticker": 1.0, "delay": 1.0, "chatty": 1.0}

            # 情绪影响打字节奏：生气/烦躁 → 回得快而短；撒娇/被戳中 → 慢一点
            _emo_delay = 1.0
            if any(e in ("angry", "annoyed") for e in memo_emotions):
                _emo_delay = 0.6
            elif any(e in ("love", "touched", "excited") for e in memo_emotions):
                _emo_delay = 1.35

            # 准备真 @：她要 @ 人才去拉名册（带缓存，10 分钟内不重复拉）
            members = []
            if isinstance(ev, GroupMessageEvent) and any(
                has_mention(i["value"]) for i in items if i["kind"] == "text"
            ):
                members = await group_roster.members_of(bot, ev.group_id)
                if not members:
                    logger.warning("[CHAT] 群成员名册拉不到，@ 会退化成文字")

            # 按顺序发送
            _is_group = isinstance(ev, GroupMessageEvent)

            # ⚠️ 这个列表之前漏了初始化：第一个气泡发完后 said.append 会抛 NameError，
            #    被外层 except 吞掉 → 多气泡回复只发出去第一条，后面的全丢。
            said = []

            # 👇 群聊被 @ 时自动引用回复（用户 2026-11-18）：
            #    她自己话多、消息容易被自己刷上去，光靠模型写 <reply:> 不可靠，
            #    所以系统兜底：这一轮**第一条**发出去的消息带上引用段。
            #    只在「被真 @」时加，喊名字/普通接话不加，避免每条都挂个引用框。
            _auto_rid = ""
            _quote_left = False
            _auto_call = ""
            try:
                # 用户 2026-11-19：「她对某句话或者某图评论/吐槽的时候能引用回复」。
                # 被 @ 时系统兜底引用（老行为）；**图片/视频**也兜底引用 ——
                # 「这图笑死」不引用那张图，群友根本不知道在说哪张。
                _pic_msg = _media_placeholder(ev) in ("[图片]", "[视频]")
                # 👇 2026-11-22「偶尔回复错人」：分段里可能有好几个人，
                #    ev 是**最后一条**。如果这一轮是「有人 @ 了她、后面别人又聊了几句」，
                #    自动引用最后一条就会引到别人身上。
                #    所以优先引「这一段里最后一条真的 @ 了她」的消息。
                _mens = [r for r in (_seg_records or []) if r.get("mentioned")]
                if _is_group and _mens:
                    _auto_rid = str(_mens[-1].get("mid") or "")
                    _auto_call = str(_mens[-1].get("name") or "")
                elif _is_group and (ev.is_tome() or _pic_msg):
                    _auto_rid = str(getattr(ev, "message_id", "") or "")
                    _auto_call = ""
                    if _pic_msg and _seg_records:
                        _auto_rid = str(_seg_records[-1].get("mid") or _auto_rid)
                _quote_left = bool(_auto_rid)
            except Exception:
                _auto_rid, _quote_left = "", False
            if _auto_rid and not _auto_call:
                try:
                    _auto_call = profiles.call_of(str(ev.user_id), speaker_name)
                except Exception:
                    _auto_call = speaker_name or ""
            for idx, item in enumerate(items):
                # 👇 QQ 硬限制「本群每分钟只能发 10 条消息」，
                #    多气泡回复很容易撞上，撞了就是 retcode=1200 整轮失败。
                if _is_group:
                    if not await _wait_group_quota(str(ev.group_id)):
                        logger.warning(
                            f"[CHAT] {ev.group_id} 群消息配额不足，丢弃剩余 "
                            f"{len(items) - idx} 个气泡"
                        )
                        break

                if idx > 0:
                    await asyncio.sleep(
                        bubble_delay(item.get("value", ""))
                        * factors["delay"] * _emo_delay / 1000.0
                    )

                kind = item["kind"]
                val = item["value"]

                if kind == "text":
                    # 👇 兜底清 HTML：搜索结果里的 <br> 被模型抄进回复时，用户会直接看到标签
                    val = strip_html(val)
                    # 👇 最后一道：协议标记（THINK / TOOLS / 半截标签）绝不能发出去
                    val = strip_protocol_residue(val)
                    # 👇 引用回复：她写了 <reply:消息号> 就真的引那条。
                    #    parse_reply 只负责把标记抠出来塞进 item["replyTo"]，
                    #    真正发 reply 段在这里 —— 之前这段根本没接，标记被静默丢掉了。
                    _explicit_rid = str(item.get("replyTo") or "").strip()
                    _rid = _explicit_rid
                    # 模型没自己引用 → 用系统兜底的「被 @ 自动引用」。
                    # 自动引用**不阻止**长文走合并转发（显式引用才阻止）。
                    _auto_used = False
                    if not _rid and _quote_left and _is_group:
                        _rid = _auto_rid
                        _quote_left = False
                        _auto_used = True
                    # 👇 先把 @ 标签摘出来：字数硬截断会把 "<at:小明>" 切一半变成乱码
                    at_names, body = _split_at_tags(val)
                    val = body
                    # 👇 [[LONG]] 是兼容老提示：写了就强制转发（群聊不靠标记，
                    #    超过阈值本来就会自动转发）。
                    _has_long_tag = val.strip().startswith("[[LONG]]")
                    if _has_long_tag:
                        val = val.strip()[len("[[LONG]]"):].strip()
                    _long = _has_long_tag and not _is_group
                    # 👇 群聊规矩（用户 2026-11-18）：**20 字内直接发，20 字以上合并转发**。
                    #    以前是 >20 字直接截断，经常砍在半句话上。
                    #    带真 @ / 引用回复的没法塞进卡片，这两种才退回截断。
                    if _is_group:
                        _g_limit = group_tuning.max_hanzi(session_key, GROUP_MAX_HANZI)
                        if not _long and hanzi_count(val) > _g_limit:
                            if not at_names and not _explicit_rid:
                                if await _send_long_as_forward(
                                        bot, ev, val,
                                        reply_to=_auto_call if _auto_used else ""):
                                    said.append(
                                        f"回复@{_auto_call}：{val}" if _auto_used else val)
                                    continue
                                # 转发失败 → 宁可长一点也不砍半句话：带引用直接发整段
                                if _auto_used:
                                    await _safe_send(
                                        bot, ev,
                                        MessageSegment.reply(int(_rid)) + val,
                                    )
                                    said.append(val)
                                    continue
                                val = limit_hanzi(val, _g_limit)
                            # ⚠️ 2026-11-23 用户：「接一句词不达意」。
                            #    带 @ / 显式引用的长回复以前会被 limit_hanzi 硬切在 20 字，
                            #    半句话直接发出去 → 看起来就是答非所问。
                            #    QQ 本身能发长消息，这里**不截断**，整段发完整。
                    else:
                        # 👇 私聊同一条规矩（用户 2026-11-18）：20 字内直接发，超过走合并转发。
                        if not _long and hanzi_count(val) > PRIVATE_MAX_HANZI:
                            if not at_names and not _rid:
                                if await _send_long_as_forward(bot, ev, val):
                                    said.append(val)
                                    continue
                                val = limit_hanzi(val, PRIVATE_MAX_HANZI)
                            # 带 @ / 引用 → 保留全文，不截断（同上）
                    if not val.strip() and not at_names:
                        # 这个气泡是空的、没发出去 → 把自动引用还给下一个气泡
                        if _auto_used:
                            _quote_left = True
                        continue
                    # 👇 [[LONG]]（兼容老标记）：写了就转发，不截断。
                    #    带 @ 的走不了转发（卡片里放不了真 @）。
                    if _long and val.strip() and not at_names:
                        if await _send_long_as_forward(bot, ev, val):
                            said.append(val)
                            continue
                        # 转发失败 → 掉到下面普通发送（_long 已跳过截断，直接发整段）
                    # @ 放最前面（QQ 里本来就是这么说话的）
                    if at_names:
                        val = "".join(f"<at:{n}>" for n in at_names[:3]) + val

                    if members:
                        segs, missed = expand_mentions([{"type": "text", "data": {"text": val}}], members)
                        if _rid:
                            segs = [{"type": "reply", "data": {"id": _rid}}] + segs
                        await _safe_send(bot, ev, _segs_to_message(segs))
                        if missed:
                            logger.info(f"[CHAT] 没认出来的 @：{'、'.join(missed[:3])}")
                            # 👇 她不知道自己 @ 失败了（对方没收到提醒），
                            #    下一轮 system 里明确告诉她，让她改用 [[[WHO|名字]]] 查准。
                            try:
                                llm_mod.note_missed_at(_get_session_key(ev), missed)
                            except Exception:
                                pass
                    elif _rid:
                        # 私聊 / 名册拉不到 → 用 reply 段 + 纯文字
                        await _safe_send(
                            bot, ev,
                            MessageSegment.reply(int(_rid))
                            + _AT_TAG_STRIP.sub(lambda m: "@" + m.group(1), val),
                        )
                    else:
                        # 私聊 / 名册拉不到 → 退化成文字 @名字，别把 <at:> 发给用户
                        await _safe_send(
                            bot, ev,
                            _AT_TAG_STRIP.sub(lambda m: "@" + m.group(1), val),
                        )
                    if _rid:
                        logger.info(f"[CHAT] 引用回复 {_rid}")
                    said.append(_AT_TAG_STRIP.sub(lambda m: "@" + m.group(1), val))

                elif kind == "voice":
                    logger.info(f"[CHAT] 语音: {val[:20]}...")
                    # 统一出口：indextts 优先 → CosyVoice 降级 → 文字兜底
                    sent = await speak_voice(bot, ev, val)
                    said.append(f"[语音] {val}" if sent else val)

                elif kind == "sticker":
                    try:
                        # 2026-11-20 审计 E2：以前忽略返回值，失败也记「已发」，
                        # 而失败函数内部还会额外发一句「没有 X 这个标签」→ 双重错误。
                        _st_ok = await send_sticker_by_tag(bot, ev, val)
                        if _st_ok:
                            said.append(f"[表情:{val}]")
                        else:
                            logger.info(f"[CHAT] 表情没发出去（标签不存在/库为空）：{val}")
                    except Exception as e:
                        logger.warning(f"[CHAT] 发表情失败: {e}")

                elif kind == "poke":
                    try:
                        kwargs = {"user_id": int(ev.user_id)}
                        if isinstance(ev, GroupMessageEvent):
                            kwargs["group_id"] = int(ev.group_id)
                        await bot.call_api("send_poke", **kwargs)
                        said.append("[戳一戳]")
                    except Exception:
                        pass

                elif kind == "image":
                    # 图片也能带引用：被 @ 后第一张图挂上引用框，一眼看出在回谁
                    if _quote_left and _is_group and _auto_rid:
                        try:
                            await _safe_send(
                                bot, ev,
                                MessageSegment.reply(int(_auto_rid))
                                + MessageSegment.image(val),
                            )
                            _quote_left = False
                        except Exception:
                            await _safe_send(bot, ev, MessageSegment.image(val))
                    else:
                        await _safe_send(bot, ev, MessageSegment.image(val))
                    said.append("[图片]")

            # 30% 概率补表情（心情影响概率）
            # ⚠️ 她这条已经自己发过表情就不再补，否则一次回复连甩两张
            try:
                already_sticker = any(i["kind"] == "sticker" for i in items)
                if not already_sticker and random.random() < 0.3 * max(0.2, factors["sticker"]):
                    all_text = " ".join(i["value"] for i in items if i["kind"] == "text")
                    mapping = {
                        "哈哈": "开心", "好耶": "开心", "笑死": "嘲笑", "乐": "搞怪",
                        "无语": "无语", "难绷": "搞怪", "草": "搞怪", "委屈": "委屈",
                        "吃瓜": "吃瓜", "震惊": "惊讶", "困": "困", "生气": "生气",
                        "难过": "难过", "呜呜": "难过", "抱抱": "撒娇", "哼": "撒娇",
                        "？": "思考", "你好": "打招呼", "谢谢": "点赞",
                    }
                    for kw, tag in mapping.items():
                        if kw in all_text:
                            await asyncio.sleep(1.2)
                            await send_sticker_by_tag(bot, ev, tag)
                            said.append(f"[表情:{tag}]")
                            break
            except Exception:
                pass

            # 👇 把她自己这一轮说的话也写进群聊上下文。
            #    不写的话，下一轮她完全看不到自己刚说过什么 → 会重复、会自相矛盾。
            #    注意：这里只 append，**不触发回复**（worker 内部，不经过 _need_reply），
            #    所以不会出现「自己回复自己」的死循环。
            #    ⚠️ 一条一条记（不拼成「A；B」）：NapCat 会把每条气泡分别回显，
            #       拼起来的话去重对不上，同一句话会在上下文里出现两遍。
            if said and _is_group:
                for _one in said:
                    _append_self_line(str(ev.group_id), _one)

            # 落盘她自己的话（周报要看「这周她答了什么」）
            if said:
                try:
                    dialogue_log.log_msg(
                        str(ev.group_id) if _is_group else f"private_{speaker_qq}",
                        "你", _self_id(ev, bot), "；".join(said), kind="bot",
                        extra={"to": speaker_qq},
                    )
                except Exception:
                    pass
                # 👇 三方核对：我们这条回复要不要拿去和 GLM/那只鱼比
                try:
                    if speaker_qq in referee.PEER_QQS:
                        # 场景③：我们在回那只鱼 → 只留档，不做对错核对
                        referee.record_peer_chat(str(ev.group_id), "us", "；".join(said))
                    else:
                        referee.note_our_reply(
                            str(ev.group_id), text, "；".join(said),
                            prompt_text=_prompt_base, persona=_persona_text,
                            ref1=_ref1,        # preflight 已经生成过就别再调一次 GLM
                        )
                except Exception as e:
                    logger.debug(f"[REF] 登记失败: {e}")

            # 记录她在群里说过话（供 _random_chat_loop 刹车）
            if isinstance(ev, GroupMessageEvent):
                gid = str(ev.group_id)
                bot_reply_times[gid].append(int(time.time() * 1000))
                bot_reply_times[gid] = bot_reply_times[gid][-30:]

            # 更新上一句（短句兜底）
            last_text_map[session_key] = text
            last_text_at[session_key] = time.time()

            # 2026-11-20 审计 G4：以前只更新 session_last_reply，
            # 循环里的局部 last 不动 → 第二条起 wait 恒为负，REPLY_INTERVAL 失效。
            last = time.monotonic()
            session_last_reply[session_key] = last

            # 👇 关注会话：回完一条就扣一次额度（@ 那次不扣，额度是「后续还能接几条」）
            if isinstance(ev, GroupMessageEvent) and not ev.is_tome():
                _focus_consume(session_key, ev.user_id)

        except Exception:
            logger.exception(f"[WORKER] reply failed {session_key}")
        finally:
            # ⚠️ 必须复位：否则第一次回复后这个会话的主动插话会被永久跳过。
            _WORKER_BUSY[session_key] = False


# ================= 主动插话 =================
async def _random_chat_loop(bot: Bot):
    from services.aura_logic import (
        policy_for, topic_relevance, decide_group_reply, parse_reply, bubble_delay,
    )

    await asyncio.sleep(15)
    while True:
        try:
            if RANDOM_CHAT_ENABLED and skill_switch.is_enabled("proactive.interject"):
                now_ts = time.time()
                now_ms = int(now_ts * 1000)

                for gid in list(ALLOWED_GROUPS):
                    # 这个群有没有单独的频率覆盖（测试群 1 分钟一次那种）
                    _ov = GROUP_INTERJECT_OVERRIDES.get(gid) or {}
                    _cool = float(_ov.get("cooldownSec", RANDOM_CHAT_INTERVAL))
                    _prob = float(_ov.get("interjectChance", RANDOM_CHAT_PROB))
                    _min_msgs = int(_ov.get("minRecentMessages", RANDOM_CHAT_MIN_MSGS))
                    # 每个群按自己的心情算插话概率（心情现在是分层的）
                    try:
                        factors = mood.mood_factors(f"group_{gid}")
                    except Exception:
                        factors = {"chatty": 1.0}
                    act = last_group_activity.get(gid, 0)
                    if now_ts - act > 300:
                        recent_group_msgs[gid].clear()
                        continue
                    if now_ts - last_random_chat.get(gid, 0) < _cool:
                        continue
                    # 👇 群里正在刷屏时别主动插话：本来也接不上话，
                    #    而且这时候她要回的是「最新那条」，再插一句只会更吵。
                    if BURST_FLUSH_ENABLED and _burst_count(f"group_{gid}") >= BURST_MSG_COUNT:
                        continue
                    # 👇 刚被群友嫌弃过（「真烦人」「能不能关了先」）→ 别往上凑
                    if _is_muted(gid):
                        continue
                    # 👇 她自己把这个群设成安静了（CHAT_OFF）→ 主动插话也停。
                    #    安静是"只读"，不是"只对 @ 生效"。
                    try:
                        if chat_switch.is_off(f"group_{gid}")[0]:
                            continue
                        # 2026-11-20 审计 A2：只查群不查人 → 她对某个人 strict 安静后，
                        # 主动插话仍可能接那个人的话。这里补「最后说话人」的检查。
                        _spk = str(last_group_speaker.get(gid) or "")
                        if _spk and chat_switch.is_off(f"group_{gid}", _spk)[0]:
                            continue
                    except Exception:
                        pass

                    # 👇 最新一句如果是「某个人 → 另一个群友」的私聊式对话，别插话
                    #    （用户 2026-11-23：「收件人不是大家、是一个群友的情况下他也回复」）。
                    #    用 group_scene 已经判好的「谁 → 谁」，不靠关键词猜。
                    try:
                        _lm = group_scene.last(gid)
                        if _lm and not _lm.get("is_bot"):
                            _lk = str(_lm.get("to_kind") or "").strip().lower()
                            _lq = str(_lm.get("to_qq") or "")
                            try:
                                _lc = float(_lm.get("to_conf") or 0.0)
                            except Exception:
                                _lc = 0.0
                            if (_lk == "person" and _lq and _lc >= 0.5
                                    and not _is_self_qq(_lq)):
                                logger.debug(
                                    f"[GROUP] {gid} 最新一句是 "
                                    f"{_lm.get('name') or '?'} → "
                                    f"{_lm.get('to_name') or _lq}，不插话")
                                continue
                    except Exception:
                        pass

                    # 👇 关注期（刚被 @ / 叫过名字，后面还要接 3 句）里不要主动插话，
                    #    否则「正常回复」和「主动插话」两条线同时说，看起来像对着旧话反复回。
                    _focus_on = False
                    try:
                        _fs = focus_sessions.get(f"group_{gid}") or {}
                        _now_f = time.time()
                        _focus_on = any(
                            _now_f < float(v.get("until", 0))
                            for v in _fs.values() if isinstance(v, dict)
                        )
                    except Exception:
                        _focus_on = False
                    if _focus_on:
                        continue

                    # 👇 有正常回复在排队 / 刚回完 → 别插话。
                    #    真机踩过（2026-11-18）：她刚回完一句，插话循环又接了一句，
                    #    群里看到的就是「同一条消息她回了两次」。
                    _sk = f"group_{gid}"
                    # 2026-11-20 审计 A3：worker 正在处理这个会话时不要插话。
                    # 以前只靠「队列非空 / 10s 内回过」当代理锁，worker 在
                    # 防抖/审核/生成的中途队列是空的 → 会出现「刚回完又插一句」。
                    if _WORKER_BUSY.get(_sk):
                        continue
                    try:
                        _q = session_queues.get(_sk)
                        if _q is not None and not _q.empty():
                            continue
                    except Exception:
                        pass
                    if time.monotonic() - float(session_last_reply.get(_sk, 0) or 0) < 10:
                        continue

                    # 👇 只拿**真人**说的话：把「你: …」（她自己说的）剔掉。
                    #    原来把自己的话也算进上下文，她会顺着自己的话再接一遍 ——
                    #    表现就是「在回复过的文本上再回复几遍」。
                    recent = list(recent_group_msgs.get(gid, []))
                    _ts = list(recent_group_ts.get(gid, []))
                    if len(_ts) != len(recent):
                        _ts = [time.time()] * len(recent)
                    pairs = [
                        (t, m) for t, m in zip(_ts, recent)
                        if _SELF_PREFIX not in m          # 剔掉她自己说的话
                    ]
                    pairs = pairs[-6:]
                    if len(pairs) < _min_msgs:
                        continue
                    # 👇 同一段对话只看一次：最新的人类消息没变化 → 不再开口。
                    #    真机踩过（2026-11-18）：她问完「苹果猪是什么猪」没人接，
                    #    冷却 60s 后循环又看到同一段上下文，这次用语音自问自答。
                    #    「看过」的基准取两者最大：上次判断过的消息 + 她自己最后开口的时间。
                    #    她已经在这条人类消息之后说过话 → 说明这句已经处理过了。
                    _latest_human_ts = float(pairs[-1][0] or 0)
                    _last_bot_ts = (
                        float(bot_reply_times[gid][-1]) / 1000.0
                        if bot_reply_times[gid] else 0.0
                    )
                    _seen_ts = max(float(_interject_seen.get(gid, 0) or 0), _last_bot_ts)
                    if _latest_human_ts <= _seen_ts:
                        # 这段对话已经看过了。以前直接 continue（等下一批新消息），
                        # 结果「大家聊完突然安静」时她永远不会补一句。
                        # 现在按真实消息间隔预测「下一条该到了」：超过还没来 → 给一次机会，
                        # 同一段静默只给一次（用户 2026-11-19，参考 MaiBot 的静默唤醒）。
                        _cur_iv = vf_mod.mean_interval(list(recent_group_ts.get(gid, [])))
                        _iv = vf_mod.predict_interval(_cur_iv, _LAST_INTERVAL.get(gid))
                        _LAST_INTERVAL[gid] = _cur_iv
                        _quiet = now_ts - float(act or 0)
                        if not _iv or _quiet < max(8.0, 1.5 * _iv):
                            continue
                        if _SILENCE_SEEN.get(gid) == act:
                            continue
                        _SILENCE_SEEN[gid] = act
                        logger.info(
                            f"[SILENCE] {gid} 安静 {_quiet:.0f}s（预测间隔 {_iv:.1f}s）"
                            f"→ 给一次开口机会")

                    recent_texts = [
                        _strip_ts(m).split(": ", 1)[-1] for _, m in pairs if ": " in m
                    ]
                    recent_times = [int(t * 1000) for t, _ in pairs]
                    if not recent_texts:
                        continue

                    # 👇 这里**不再**用正则预筛「最新一句值不值得接」。
                    #    用户 2026-11-18：「不要用正则去唤醒，很容易误触发，
                    #    LLM 本身就是一种智能判断，让它自己决定接话还是安静」。
                    #    所以：频率闸门（冷却/额度）负责"能不能说"，
                    #    "该不该说 / 说什么"全部交给下面的模型判断。

                    # 关系越熟越可能搭话；全是陌生人就少插嘴（0.6~1.4 倍）
                    _imax = 0.0
                    try:
                        for _q in list(speaker_recent.get(gid, {}).keys())[-5:]:
                            _imax = max(_imax, profiles.intimacy(_q))
                    except Exception:
                        _imax = 0.0
                    global_pol = {
                        "interjectChance": min(
                            1.0,
                            _prob * factors["chatty"] * (0.6 + 0.8 * _imax),
                        ),
                        "cooldownSec": _cool,
                        "quietHours": [SLEEP_HOUR_START, SLEEP_HOUR_END],
                        "minRecentMessages": _min_msgs,
                    }
                    # 有单群覆盖就交给 policy_for 合并（覆盖里默认 minRelevance=0，
                    # 也就是「不看关键词，交给模型判断要不要接」）
                    pol = policy_for(global_pol, {gid: _ov} if _ov else None, gid)
                    rel = topic_relevance(recent_texts, pol["interests"])

                    # ⚠️ 随机抽签只留一层（2026-11-19）：
                    #    以前 decide_group_reply 内部 `rand() < chance` 抽一次，
                    #    外面查看频率又抽一次 —— 双重随机，效果是"概率莫名其妙偏低"。
                    #    这里传 rand=0 让它只做**硬闸门**（静默时段/冷却/每小时上限/最少条数），
                    #    真正的"这一轮想不想说"交给下面的 _VIEW_FREQ（带惯性）。
                    decision = decide_group_reply({
                        "policy": pol,
                        "relevance": rel,
                        "rand": lambda: 0.0,
                        "engagedUntil": 0,
                        "mentioned": False,
                        "repliedToBot": False,
                        "fromOwner": False,
                        "text": recent_texts[-1],
                        "now": now_ms,
                        "hour": time.localtime().tm_hour,
                        # ⚠️ 用真实时间戳，不能再用 [now_ms]*n —— 那等于告诉它
                        #    「最近 5 分钟有 n 条消息」，闸门永远通过
                        "recentTimes": recent_times,
                        "interjectTimes": interject_times[gid][-20:],
                        "botReplyTimes": bot_reply_times[gid][-30:],
                    })

                    if not decision["reply"]:
                        logger.debug(f"[GROUP] {gid} 不接话: {decision['reason']}")
                        continue

                    # 查看频率：刚连说过几句 → 内部频率已经衰减，这里会自然收敛；
                    # 久没开口 → 恢复期逐步放开（用户 2026-11-19，参考 MaiBot）。
                    # 外部频率 = 基础概率 × 相关度加成（和 aura_logic 原来的算法一致）
                    _ext = float(pol.get("interjectChance") or _prob)
                    try:
                        if float(pol.get("minRelevance") or 0) > 0:
                            _ext = min(0.8, _ext * (1 + 4 * float(rel or 0)))
                    except Exception:
                        pass
                    _ext = min(1.0, max(0.0, _ext))
                    _vf = _VIEW_FREQ[gid]
                    if not _vf.decide(_ext, random.random()):
                        logger.info(
                            f"[VIEW] {gid} 本轮不放行（{_vf.describe()}｜external={_ext:.2f}）")
                        continue
                    _vf.activate(_ext)

                    logger.info(f"[GROUP] {gid} 主动插话: {decision['reason']}")
                    # 先占住这个群的插话名额：LLM 调用/发送失败也不要在下个 tick 重试，
                    # 否则同一条话会被插两次（"接话两次"的另一半原因）。
                    last_random_chat[gid] = time.time()
                    # 这段上下文已经看过（不管最后是开口还是潜水）→ 下个 tick 不再重复判断
                    _interject_seen[gid] = _latest_human_ts

                    # 上下文给她最近 8 条，**必须是收发件人处理过的历史**
                    # （用户 2026-11-23：「这些上下文一定要收发件人处理过」）。
                    # history_lines 里也包含她自己刚说的（带「你 → 大家」）。
                    # 注意 pairs（判相关度/决定要不要插）仍然只算真人，避免她顺着自己接。
                    _ctx_lines = group_scene.history_lines(
                        gid, mood_judge.CTX_LINES) or []
                    # 群聊现场：给插话用的「谁 → 谁 / 几条线程 / 谁在叫」
                    _scene_txt = ""
                    try:
                        _bid = _self_id(bot=bot)
                        if scene_auditor.enabled():
                            try:
                                await asyncio.wait_for(
                                    scene_auditor.audit_recent(
                                        gid, bot_qq=_bid,
                                        bot_name=_bot_name(gid, _bid),
                                        names=_scene_names(gid),
                                    ),
                                    timeout=max(2.0, scene_auditor.TIMEOUT + 1),
                                )
                            except Exception as e:
                                _warn_audit_wire("插话前审核", e)
                        _snap = group_scene.snapshot(
                            gid, bot_qq=_bid,
                            my_names=_my_names(f"group_{gid}", ""),
                        )
                        _scene_txt = group_scene.format_block(_snap)
                    except Exception:
                        _scene_txt = ""
                    # ⚠️ 这里**不再**把 ctx_str 拼进 prompt —— group_context 已经会以
                    #    【历史对话】的形式进 prompt，再贴一遍等于同样的内容出现两次，
                    #    既费 token 又让模型以为"这段话被强调过"。
                    # 👇 所有群共用的「接话底层逻辑」（用户 2026-11-18）：
                    #    先分类（在找我 / 我可以接 / 不关我事），再决定怎么说。
                    #    分类由模型自己做，代码只负责频率和额度。
                    prompt = (
                        "（系统提示：下面是群里刚才的对话。先判断这条是不是在找你，再决定怎么接。）\n"
                        + ((_scene_txt + "\n\n") if _scene_txt else "")
                        + "【第一步·这属于哪种】\n"
                        "A. 在找你 → 问你问题、点你名字、让你做事、接着你上一句话说的 → **你要回答**\n"
                        "B. 没在问你，但话题你能接 → **可以接一句**吐槽/附和\n"
                        "C. 跟你无关 → 他们在互相配合做事（发文件、扫码登录、装东西、约时间、"
                        "报数据、甩链接）、只是「扫扫」「来了」「发一下」这种短指令、"
                        "或者你半懂不懂 → 只输出 (不想回复)\n"
                        "【第二步·按类型说】\n"
                        "· A：正面回答他问的那件事，一句话说完，别反问、别绕、别总结\n"
                        "· B：只接**一个点**，贴着他们正在说的那句话；别跳到别的话题、"
                        "别说教、别追问第二句\n"
                        "· 拿不准是 A 还是 C → 输出 (不想回复)。硬猜比不说话难看得多\n"
                        "【通用】说人话、短句；不要提「系统提示」「分类」「A/B/C」这些字眼。\n"
                        "【只接一个线程】群里可能同时在聊几件事：只接上面【群聊现场】里的**一个**线程。\n"
                        "  挑法：①「在叫你」→ 必须接；② 有人在提问 / 吐槽 / 有明显情绪 → 可以接；\n"
                        "  ③ 两个人正在配合做事（发文件、扫码、装东西、约时间）→ 不要接。\n"
                        "  回的时候说清你在回谁（@ 他 / 引用那句），别的线程别混进来、别总结。\n"
                        "【别接自己的话】上下文里「这是你之前的回复：」开头的是**你自己**说的，"
                        "那不是新消息：不要回应、不要自问自答，也别把你说过的话当话题。\n"
                        f"【别人最新一句（只判断这句）】{_strip_ts(pairs[-1][1])}\n"
                        "【例子·照着这个尺度判】\n"
                        "· 他：「@老王 文件发你了，装一下」→ (不想回复)（他们在互相做事，没你的事）\n"
                        "· 他：「这破服务器又崩了」→ 「又崩？我这刚连上」（没问你，但你能接一句）\n"
                        "· 他：「小鲸鱼 这配置咋样」→ 「能跑，但内存给少了」（在问你，正面答）\n"
                        "· 他：「坏了 她知道啥子是骂人」→ (不想回复)（他们在议论你，不是问你；"
                        "别凑上去问「啥子是啥子」）\n"
                        "· 他：「你会不会唱歌」→ 「会啊，想听啥」（在问你，但只是问，别自己开唱）"
                    )
                    # 2026-11-20 审计 A1/A1b：插话也要有 tool context，且 addressed=False。
                    # 以前这里直接 call_llm → 插话时所有工具返回「缺会话信息」，
                    # 而且按「在叫她」处理，可能突然发语音/唱歌。
                    _old_ctx = dict(llm_mod.TOOL_CTX)
                    try:
                        llm_mod.set_tool_context(
                            bot=bot,
                            event=_InterjectEvent(gid, getattr(bot, "self_id", "")),
                            speaker_qq="",
                            speaker_name="",
                            addressed=False,
                            newer_count=0,
                        )
                        reply, _ = await call_llm(
                            f"group_{gid}", prompt, group_context=_ctx_lines)
                        if not reply:
                            continue
                        parsed = parse_reply(reply)
                        _mood_label2 = _apply_mood_label(parsed, f"group_{gid}")
                        try:
                            _t2 = [str(parsed.get("think") or "").strip()]
                            _t2 += [str(mm.get("think") or "").strip()
                                    for mm in (parsed.get("memos") or [])]
                            _think2 = "；".join([x for x in _t2 if x])
                            _reply2 = "；".join(
                                str(i.get("value") or "")
                                for i in (parsed.get("items") or [])
                                if i.get("kind") == "text").strip()
                            _src2 = str(pairs[-1][1] if pairs else "").strip()
                            _spawn_mood_judge(
                                f"group_{gid}", _mood_label2,
                                _ctx_lines, _think2,
                                src=_src2, reply=_reply2)
                        except Exception as e:
                            logger.debug(f"[MOOD-JUDGE] 插话触发失败: {type(e).__name__}: {e}")

                        # 主动插话时她自己的 MEMO 也要落地（她自己经历的事）
                        for memo in (parsed.get("memos") or []):
                            emo = memo.get("emotion") or "neutral"
                            try:
                                memory_v2.remember(
                                    text=memo["text"],
                                    kind=memo.get("kind") or "impression",
                                    about_qq="",
                                    tags=memo.get("tags") or [],
                                    importance=float(memo.get("importance") or 0.6),
                                    emotion_hit=(emo != "neutral"),
                                )
                                mood.bump_by_emotion(
                                    memo.get("emotion_raw") or emo,
                                    reason=memo["text"],
                                    session_key=f"group_{gid}",
                                )
                            except Exception as e:
                                logger.debug(f"[MEMO] 群聊插话记忆失败: {e}")

                        if parsed["silent"]:
                            logger.info(f"[GROUP] {gid} 潜水")
                            continue
                        items = parsed["items"]
                        if not items:
                            continue
                        # 👇 主动插话只发**一条**：连发两条在群里看起来就是「她接话两次」
                        items = items[:1]

                        for idx, item in enumerate(items):
                            # 主动插话同样受群每分钟 10 条限制
                            if not await _wait_group_quota(str(gid)):
                                logger.warning(f"[GROUP] {gid} 配额不足，放弃剩余气泡")
                                break
                            if idx > 0:
                                await asyncio.sleep(bubble_delay(item.get("value", "")) / 1000.0)
                            kind = item["kind"]
                            val = item["value"]

                            if kind == "text":
                                fake_event = GroupMessageEvent(
                                    time=int(time.time()), self_id=bot.self_id, post_type="message",
                                    sub_type="normal", user_id=int(bot.self_id), message_type="group",
                                    message_id=0, message=Message(val), original_message=Message(val),
                                    raw_message=val, font=0,
                                    sender={"user_id": int(bot.self_id), "nickname": "Bot", "card": ""},
                                    group_id=int(gid), anonymous=None, to_me=False,
                                )
                                # 主动插话也可能 @ 人：能拉到名册就发真 @，否则退化成文字
                                _at_names, _body = _split_at_tags(strip_html(val))
                                _g_limit = group_tuning.max_hanzi(gid, GROUP_MAX_HANZI)
                                _sent = False
                                # 👇 和主回复同一条规矩：20 字以上走合并转发（不截断）；
                                #    带 @ 的塞不进卡片，才退回截断。
                                if not _at_names and hanzi_count(_body) > _g_limit:
                                    _sent = await _send_long_as_forward(bot, fake_event, _body)
                                if not _sent:
                                    _body = limit_hanzi(_body, _g_limit)
                                if not _sent and _at_names:
                                    _mem = await group_roster.members_of(bot, gid)
                                    if _mem:
                                        _segs, _ = expand_mentions(
                                            [{"type": "text", "data": {"text":
                                                "".join(f"<at:{n}>" for n in _at_names[:2]) + _body}}],
                                            _mem,
                                        )
                                        try:
                                            await bot.send(fake_event, _segs_to_message(_segs))
                                            _sent = True
                                        except Exception as e:
                                            logger.debug(f"[GROUP] @ 发送失败: {e}")
                                    if not _sent:
                                        _body = "".join(f"@{n}" for n in _at_names[:2]) + _body
                                if not _sent:
                                    # ⚠️ 这里原来调 services.reply.reply_with_multimodal，
                                    #    它内部 30% 概率直接调云端 CosyVoice → 绕过 indextts，
                                    #    群里听到的就是龙呼呼。现在统一走 speak_voice。
                                    if (RANDOM_CHAT_VOICE_RATE > 0
                                            and random.random() < RANDOM_CHAT_VOICE_RATE):
                                        # speak_voice 内部：indextts → 云端 → 文字兜底
                                        await speak_voice(bot, fake_event, _body)
                                    else:
                                        await _safe_send(bot, fake_event, _body)
                            elif kind == "voice":
                                fake_event = GroupMessageEvent(
                                    time=int(time.time()), self_id=bot.self_id, post_type="message",
                                    sub_type="normal", user_id=int(bot.self_id), message_type="group",
                                    message_id=0, message=Message(val), original_message=Message(val),
                                    raw_message=val, font=0,
                                    sender={"user_id": int(bot.self_id), "nickname": "Bot", "card": ""},
                                    group_id=int(gid), anonymous=None, to_me=False,
                                )
                                try:
                                    # 走统一出口：indextts 优先，失败才降级云端
                                    await speak_voice(bot, fake_event, val)
                                except Exception as e:
                                    logger.warning(f"[GROUP] 语音发送失败: {e}")
                            elif kind == "sticker":
                                try:
                                    fake_event = GroupMessageEvent(
                                        time=int(time.time()), self_id=bot.self_id, post_type="message",
                                        sub_type="normal", user_id=int(bot.self_id), message_type="group",
                                        message_id=0, message=Message(val), original_message=Message(val),
                                        raw_message=val, font=0,
                                        sender={"user_id": int(bot.self_id), "nickname": "Bot", "card": ""},
                                        group_id=int(gid), anonymous=None, to_me=False,
                                    )
                                    await send_sticker_by_tag(bot, fake_event, val)
                                except Exception:
                                    pass
                            elif kind == "poke":
                                try:
                                    await bot.call_api("send_poke", group_id=int(gid))
                                except Exception:
                                    pass

                        # 👇 她自己插的这句话也要进群上下文，否则下一轮她根本不知道
                        #    自己刚说过什么（用户 2026-11-18：「不清楚自己上一句话」）。
                        #    以前插话只写 memory_v2，不写 recent_group_msgs。
                        for _it in items:
                            if _it.get("kind") == "text":
                                _append_self_line(gid, strip_html(_it.get("value") or ""))
                            elif _it.get("kind") == "voice":
                                # 语音也要记下**说了什么**，否则上下文里只剩 [语音]，
                                # 她下一轮不知道自己刚才说过什么（会重复/自问自答）
                                _append_self_line(gid, f"[语音] {_it.get('value') or ''}".strip())

                        last_random_chat[gid] = time.time()
                        interject_times[gid].append(now_ms)
                        interject_times[gid] = interject_times[gid][-20:]
                        bot_reply_times[gid].append(now_ms)
                        bot_reply_times[gid] = bot_reply_times[gid][-30:]

                        # 她主动说话也写记忆（她自己经历，同样去掉 MEMO）
                        try:
                            said = " ".join(
                                i["value"] for i in items if i["kind"] == "text"
                            ).strip() or reply
                            memory_v2.remember(
                                text=f"我在群 {gid} 主动说了：{said[:120]}",
                                kind="experience",
                                about_qq="",
                                importance=0.4,
                            )
                        except Exception:
                            pass

                        await asyncio.sleep(random.uniform(2.0, 5.0))
                    except Exception as e:
                        logger.error(f"[GROUP] {gid} 插话失败: {e}")
                    finally:
                        # 恢复插话前的 tool context（不要污染正常回复）
                        try:
                            llm_mod.TOOL_CTX.clear()
                            llm_mod.TOOL_CTX.update(_old_ctx)
                        except Exception:
                            pass
                        # 一轮完整跑完（含"潜水"/失败）→ 扣一次查看预算
                        _VIEW_FREQ[gid].record_run()

        except Exception:
            logger.exception("[RANDOM_CHAT] 循环异常")
        await asyncio.sleep(15)


# ================= /表现 —— 她自己的表达状态（用户 2026-11-19）=================
# 「虽然在每个群每个人那里不太一样，但我清楚我自己的内心的感觉」
# 表达学习按 群 / 人 两个作用域各存一份，这里把它们汇总给她自己（和主人）看。
_express_cmd = on_command("表现", priority=5, block=True)


@_express_cmd.handle()
async def _(event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(str(event.user_id)):
        await _express_cmd.finish("仅主人可用。")
    arg = args.extract_plain_text().strip().lower()
    if arg in ("清", "clear", "忘掉", "重置"):
        n = expression.clear()
        await _express_cmd.finish(f"已经忘掉全部表达（清了 {n} 个作用域）。")
    await _express_cmd.finish(expression.self_view() + "\n\n" + expression.status())


# ================= /测函数 —— 在 QQ 里直接验证新链路 =================
# 目的：不用翻日志，发一条命令就能看到各环节的实际输出。
_test_fn = on_command("测函数", priority=5, block=True)


@_test_fn.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not is_owner(str(event.user_id)):
        await _test_fn.finish("仅主人可用。")
    _raw_args = args.extract_plain_text().strip()
    what = _raw_args.lower()

    # 👇 2026-11-19：支持「/测函数 现场 123456789」——
    #    以前只能用「当前会话」，私聊里根本没法看某个群的现场/审核/档案。
    #    只认 5~12 位纯数字，所以「用量 7」「字幕 BV1xx411c7mD」不受影响。
    _tgt = re.findall(r"(?<!\d)(\d{5,12})(?!\d)", what)[:2]
    if _tgt:
        what = re.sub(r"(?<!\d)\d{5,12}(?!\d)", " ", what)
        what = re.sub(r"\s+", " ", what).strip()
    # 现场/审核/心情/标识/关注/外号：第一个数字当群号
    _tgt_gid = _tgt[0] if _tgt else ""
    # 档案：第一个当 QQ 号，第二个（可选）当群号
    _tgt_qq = _tgt[0] if _tgt else ""
    _tgt_gid2 = _tgt[1] if len(_tgt) > 1 else ""

    if not what:
        await _test_fn.finish(
            "🧪 /测函数 <项目>\n"
            "· 标识 — 这条消息会被标成什么\n"
            "· 语音 — 语音转写能不能读到\n"
            "· 预算 — 各块预算分配结果\n"
            "· 包 — 当前上下文分块 + 字数\n"
            "· 图书馆 — 要不要查图书馆\n"
            "· 工具 — 已注册的工具列表\n"
            "· 关注 — 当前关注会话状态\n"
            "· 外号 — 她现在能被叫的名字\n"
            "· 猪猪 — 逐个跑猪猪功能（加「全」也测昨日/烤猪）\n"
            "· 原神 — 面板插件自检（依赖/资源/API）\n"
            "· 档案 — 人物卡/本群称呼/词条触发/档案图 自检\n"
            "· 现场 — 当前群聊现场（图片）；没人说话时用「现场 回放 / 样例」\n"
            "· 审核 — 审核 LLM 这一层：为什么没审/返回了什么/改了几条\n"
            "· 搜图 — 拿这个会话最近一张图，看综合搜源（TraceMoe/SauceNAO/Ascii2D/IQDB）结果\n"
            "· 分段 — 群聊「等一段说完再回」：现在攒了几条 / 参数 / 最近处理过的段\n"
            "· 群心情 — 这个群 5 小时净变化 / 喜欢讨厌人数 / 单人 0.4 / 整群 0.7\n"
            "· 面板 [群号] [QQ号] — 只打「心情栏 + 好感栏」两段原样模板\n"
            "· 模板 [群号] [QQ号] — 她这一轮真正看到的全部内容（人格/规矩/上下文块/好感/心情/工具）\n"
            "· 链路 — 上一次真实请求塞进了哪些块/工具（查「参数没传通」）\n"
            "· 主动 — 主动私聊现在会想找谁（只看不发）\n"
            "· 用量 — API 余额 + 本机记账的调用量（token/次数/缓存命中）\n"
            "· 心情 — 她的原话 / 第三方判读（名词+阈值）/ 老数值 对照（加「重判」重跑）\n"
            "· 头像 QQ号 — 头像从哪来：NapCat / 直连 / 为什么没拿到\n"
            "· 全链路 — 一条命令看心情这套通没通（判读/数值/插话/安静/记忆/档案）\n"
            "· 安静 [群号/QQ] [时长] — 强制安静（连 @ 也不回；默认 60 分钟）\n"
            "  · 带时长 = 真的安静，如 /测函数 安静 100000017 1小时\n"
            "  · 不带时长 = 只看状态 + 安静建议（好感/心情走势、群里插不插得上话）\n"
            "  · 加「解除」= 开回来，如 /测函数 安静 解除 100000017\n"
            "· /说话 [群号/QQ] — 解除安静\n"
            "· 风格 — 列出她学到说话方式的人；/测函数 风格 QQ号 看某人详情\n"
            "· 白月光 [群号] — 她在这个群最常引用/回复/@谁 + 学到的说话方式\n"
            "· prompt — 今日每段占比/关注度 + 各用途缓存命中率\n"
            "· 挖掘 QQ号 群号 — 手动从群聊提炼这个人的档案（简介/性格/事情）\n"
            "· 字幕 BV号 — 测B站字幕提取\n"
            "\n"
            "📌 后面可以带群号/QQ号，私聊里也能测别的群：\n"
            "· /测函数 现场 123456789（群号）\n"
            "· /测函数 审核 123456789\n"
            "· /测函数 档案 987654321（QQ号） 123456789（群号，可省）\n"
            "· /测函数 心情 123456789"
        )

    if what in ("标识", "label"):
        gid = _tgt_gid or str(getattr(event, "group_id", "private"))
        await _test_fn.finish(
            f"标识：{_msg_label(event)}\n"
            f"会话：{gid}\n"
            f"最近 3 条上下文：\n"
            + "\n".join(list(recent_group_msgs.get(gid, []))[-3:] or ["（空）"])
        )

    if what in ("语音", "voice"):
        vt = _voice_text(event)
        await _test_fn.finish(
            f"语音转写：{vt!r}\n"
            "（空 = 这条不是语音，或 QQ 没给转写文本）"
        )

    if what in ("预算", "budget"):
        from services.prompt_builder import BLOCK_BUDGET, TOTAL_BUDGET, allocate_budget
        demo = {
            "profiles": "档" * 500,
            "bond": "往" * 200,
            "memory": "忆" * 600,
            "library": "书" * 100,
            "mood": "心" * 50,
        }
        alloc = allocate_budget(demo, total=TOTAL_BUDGET)
        lines = [f"总预算 {TOTAL_BUDGET} 字，各块（保底/上限 → 实分）："]
        for k, v in alloc.items():
            lo, hi = BLOCK_BUDGET[k]
            lines.append(f"· {k}: {lo}/{hi} → {v}（内容 {len(demo[k])} 字）")
        lines.append(f"合计 {sum(alloc.values())} 字")
        await _test_fn.finish("\n".join(lines))

    if what in ("包", "packet"):
        from services.prompt_builder import build_packet
        pk = build_packet(
            session_key="group_test",
            user_text="测试一下上下文分块",
            speaker_qq=str(event.user_id),
            speaker_name=display_name(event),
        )
        # _meta 是给程序看的，不算 prompt 块
        if not [k for k, v in pk.items() if k != "_meta" and isinstance(v, str) and v]:
            await _test_fn.finish("上下文包是空的（记忆/档案都没内容？）")
        lines = ["📦 上下文分块："]
        for k, v in pk.items():
            if not isinstance(v, str):      # _meta 是给程序看的，不是 prompt 块
                continue
            head = v.split("\n", 1)[0]
            lines.append(f"· {k}（{len(v)} 字）：{head}")
        await _test_fn.finish("\n".join(lines))

    if what in ("图书馆", "library"):
        await _test_fn.finish(
            f"要不要查图书馆：{llm_mod._need_library('测试', 'group_test', True)}\n"
            f"冷却 {llm_mod.LIBRARY_COOLDOWN:.0f}s\n"
            f"上次查询：{llm_mod._last_library_at.get('group_test', 0):.0f}"
        )

    if what in ("工具", "tools"):
        if not llm_mod.TOOLS_SPEC:
            await _test_fn.finish("没有注册任何工具")
        is_g = isinstance(event, GroupMessageEvent)
        lines = [f"🔧 已注册 {len(llm_mod.TOOLS_SPEC)} 个工具"
                 f"（当前会话会挂 {len(llm_mod.tools_for('group_x' if is_g else 'private_x'))} 个）："]
        for s in llm_mod.TOOLS_SPEC:
            fn = s.get("function", {})
            sc = llm_mod.TOOL_SCOPES.get(fn.get("name"), {"private"})
            lines.append(f"· {fn.get('name')}［{'/'.join(sorted(sc))}］"
                         f" — {str(fn.get('description'))[:36]}")
        lines.append(f"最多 {llm_mod.MAX_TOOL_TURNS} 轮；NapCat 全量 API 走 "
                     f"napcat_help / napcat_call（/工具表 看明细）")
        await _test_fn.finish("\n".join(lines))

    if what.startswith(("链路", "pipeline", "接线")):
        # /测函数 链路 —— 上一次真实请求「到底塞了什么进去」
        # 用户 2026-11-19：「很多功能只是设计上 ok，说不定很多参数传值没通」。
        import json as _json
        _p = Path("data") / "pipeline_last.json"
        if not _p.exists():
            await _test_fn.finish(
                "还没有快照：先在这个会话说一句话让她真的生成一次回复，再来测。")
        try:
            _all = _json.loads(_p.read_text(encoding="utf-8") or "{}")
        except Exception as _e:
            await _test_fn.finish(f"读快照失败：{type(_e).__name__}: {_e}")
        # 按会话取：以前是全局一份，私聊完再看就变成别的会话了
        if isinstance(_all, dict) and "ts" in _all:
            _all = {str(_all.get("key") or ""): _all}
        _key_now = _get_session_key(event)
        _d = (_all or {}).get(_key_now) or {}
        if not _d:
            _others = [k for k in (_all or {}) if k]
            await _test_fn.finish(
                f"这个会话（{_key_now}）还没有快照。\n"
                + (f"有快照的会话：{'、'.join(_others[:6])}\n" if _others else "")
                + "先在这个会话里说一句话（让她真的生成一次回复），再来测。")
        _ago = int(time.time() - float(_d.get("ts") or 0))
        _n_tools = len(_d.get("tools") or [])
        _lines = [
            f"🔗 上一次真实请求的接线（{_ago}s 前｜v{_d.get('ver') or '?'}）",
            f"· 会话 {_d.get('key')}｜人格 {_d.get('persona')}｜媒体 {_d.get('media')}",
            f"· 上下文 {_d.get('ctx_n')} 条｜system {_d.get('sys_chars')} 字"
            f"｜user {_d.get('user_chars')} 字",
            f"· 安静 {_d.get('off')}（强制不回 {_d.get('forced_silent')}）"
            f"｜注意力档位 {_d.get('drift')}",
            f"· 工具 {_n_tools} 个（约 {_n_tools * 80} token）："
            + "、".join(x for x in (_d.get("tools") or []) if x),
        ]
        if _n_tools > 20:
            _lines.append(
                "  ⚠️ 工具偏多：群聊每轮都挂很贵、也容易乱挑。"
                "想精简在 .env 写 GROUP_TOOL_DENY=名字1,名字2（私聊不受影响）")
        _blk = _d.get("blocks") or []
        _lines.append(f"▍进来的块（{len(_blk)}）：")
        for _b in _blk:
            _lines.append(f"  {_b}")
        # 该有的块没出现 → 说明某一环没接上（这就是"设计 ok 但没通"）
        _expect = {
            "00b": "当前群号/消息号（TOOL_CTX 没 event？）",
            "00c": "你的地盘（ALLOWED_* 没读到？）",
            "02d": "注意力风格（attention 关了？）",
            "说话风格参考": "表达学习（还没学到东西）",
        }
        _miss = [f"{k}：{v}" for k, v in _expect.items()
                 if not any(str(b).startswith(k) for b in _blk)]
        # 对话状态单独判：它是「攒够才压」的，没出现很可能是正常
        if not any(str(b).startswith("这段对话之前的状态") for b in _blk):
            try:
                _st = conv_state.get(_key_now) or {}
                _last = float(conv_state._LAST_AT.get(_key_now) or 0)
            except Exception:
                _st, _last = {}, 0.0
            if _st:
                _why = "已经压过但这次没注入（检查 render 是否为空）"
            elif _last:
                _why = (f"上次尝试 {int((time.time() - _last) / 60)} 分钟前，"
                        "但没压出内容（LLM 返回空/解析失败？）")
            else:
                _why = ("还没压过：需要同一会话 ≥6 条消息、且距上次 ≥"
                        f"{int(conv_state.MIN_INTERVAL)}s（正常，不用管）")
            _miss.append(f"这段对话之前的状态：{_why}")
        if _miss:
            _lines.append("⚠️ 没看到这些块：")
            _lines.extend(f"  · {x}" for x in _miss)
        else:
            _lines.append("✅ 该有的块都在")
        await _test_fn.finish("\n".join(_lines))

    if what.startswith(("主动", "proactive")):
        # /测函数 主动 —— 现在她会想找谁（不会真发）
        _lines = [proactive.status_text(), "", "现在评估（只看不发）："]
        if not proactive.ENABLED:
            _lines.append("· 总开关关着（PROACTIVE_PRIVATE=false）")
        for _q in sorted(ALLOWED_PRIVATE):
            try:
                _i = proactive.score_of(_q)
            except Exception as _e:
                _lines.append(f"· {_q} 评估异常：{type(_e).__name__}: {_e}")
                continue
            _nm = profiles.call_of(_q, "") or _q
            if _i.get("ok"):
                _lines.append(
                    f"· {_nm}({_q}) ✅ 分数 {_i['score']}｜心情 {_i['chatty']}"
                    f"｜亲密 {_i['intimacy']}｜漂移 {_i['drift']}"
                    f"｜隔了 {_i['idle_h']}h")
            else:
                _lines.append(f"· {_nm}({_q}) ✗ {_i.get('why')}")
        await _test_fn.finish("\n".join(_lines))

    if what in ("关注", "focus"):
        gid = _tgt_gid or str(getattr(event, "group_id", "private"))
        sk = f"group_{gid}"
        cur = focus_sessions.get(sk, {}) or {}
        if not cur:
            await _test_fn.finish(f"{sk} 当前没有关注对象")
        lines = [f"{sk} 关注列表（{FOCUS_FOLLOW_COUNT} 句 / {FOCUS_TIMEOUT:.0f}s）："]
        for qq, f in cur.items():
            left = int(f.get("until", 0) - time.time())
            lines.append(f"· {qq}：还剩 {f.get('left')} 句，{left}s 后过期")
        await _test_fn.finish("\n".join(lines))

    if what in ("外号", "names"):
        gid = _tgt_gid or str(getattr(event, "group_id", "private"))
        ns = sorted(_my_names(f"group_{gid}", str(event.user_id)))
        await _test_fn.finish(
            "她现在能被叫的名字（叫到就等于被 @）：\n" + "、".join(ns)
            + "\n（其中带 * 的是你用 /改名 给她起的）"
            + (f"\n* {profiles.get_profile(str(event.user_id)).get('bot_alias')}"
               if profiles.get_profile(str(event.user_id)).get("bot_alias") else "")
        )

    if what.startswith(("搜图", "出处", "trace")):
        # /测函数 搜图 —— 用这个会话最近一张图，把 trace.moe 的原始结果全打出来。
        # 为什么要有：用户 2026-11-19「用了一个月没有一次认出一张图」，
        # 光看日志只有一句「不采信」，不知道是相似度低还是结果分散。这里一次看全。
        try:
            from plugins import vision_agent as _va
            _img = _va.last_image(_get_session_key(event)) or {}
            _url = str(_img.get("url") or "")
        except Exception as e:
            _url = ""
            logger.debug(f"[TEST-FN] 取最近图片失败: {e}")
        if not _url:
            await _test_fn.finish("这个会话还没有识过的图：先发一张，再来测。")
        try:
            import httpx as _hx
            async with _hx.AsyncClient(timeout=20, follow_redirects=True) as _c:
                _r = await _c.get(_url)
            _data = _r.content
        except Exception as e:
            await _test_fn.finish(f"图片下载失败：{type(e).__name__}: {e}")
        from services import image_search as _is, vision_verify as _vv
        _all = await _is.search(_data)
        _res = await _vv.trace_moe(_data)
        _lines = ["▍综合搜图（TraceMoe / SauceNAO / Ascii2D / IQDB）",
                  _is.format_results(_all, limit=10) or "（所有源都没结果）"]
        if not _res:
            _lines.append("· trace.moe 单独判定：无结果（非动漫 / 纯色挡掉 / 额度用完）")
            await _test_fn.finish("\n".join(_lines))
        _lines += [
            f"· trace.moe 门槛：采信 ≥{_vv.TRACE_MIN_SIM:.2f}"
            f"｜高置信 ≥{_vv.TRACE_STRONG_SIM:.2f}"
            f"｜单帧直通 ≥{_vv.TRACE_SINGLE_SIM:.2f}",
            f"· trace.moe 原始返回：{_res.get('raw', '?')} 条",
        ]
        for _d in (_res.get("debug") or []):
            _lines.append(f"· {_d['work']} 相似度 {_d['sim']:.2f}"
                          + (f" 第{_d['ep']}集" if _d.get("ep") else ""))
        if _res.get("work"):
            _lines.append(
                f"判定：命中（{_res.get('confidence')}）《{_res['work']}》"
                + (f" 第{_res.get('episode')}集" if _res.get("episode") else "")
                + f" 相似度 {_res.get('similarity', 0):.2f}"
            )
        else:
            _cands = "、".join(f"《{c['work']}》{c['similarity']:.2f}"
                               for c in (_res.get("candidates") or []))
            _lines.append(f"判定：不采信（只当线索）→ {_cands or '无候选'}")
        await _test_fn.finish("\n".join(_lines))

    if what.startswith(("模板", "prompt", "上下文全文", "system")):
        # /测函数 模板 [群号] [QQ号] —— 打印她这一轮**真正看到的**全部内容。
        # 用户 2026-11-22：「模板有很多重复的」→ 不再由我重新拼一遍（重拼必然重复），
        # 直接读 llm._LAST_MSGS：那是真正发出去的那一份。
        _gid_t = _tgt_gid or str(getattr(event, "group_id", "") or "")
        _qq_t = (_tgt_gid2
                 or ("" if _tgt_gid else str(getattr(event, "user_id", "") or "")))
        _key_t = f"group_{_gid_t}" if _gid_t else f"private_{_qq_t}"

        _msgs = []
        try:
            _msgs = llm_mod.last_messages(_key_t) or llm_mod.last_messages()
        except Exception:
            _msgs = []

        if _msgs:
            _total = sum(len(str(m.get("content") or "")) for m in _msgs)
            _toc = [f"📋 这一轮真正发给模型的输入：{len(_msgs)} 块 / 共 {_total} 字"]
            for _i, _m in enumerate(_msgs, 1):
                _b = _m.get("content")
                _b = _b if isinstance(_b, str) else str(_b)
                _role = "👤user" if _m.get("role") == "user" else "⚙️sys"
                _toc.append(f"#{_i} {_role} {len(_b):>5} 字  {_block_title(_b)}")
            _tools = []
            try:
                _tools = llm_mod.tools_for(_key_t)
            except Exception:
                _tools = []
            _toc.append(f"🔧 工具 {len(_tools)} 个："
                        + "、".join((t.get("function") or {}).get("name") or "?"
                                   for t in _tools[:20]))
            _toc.append("（下面是全文；太长会分批）")
            await bot.send(event, "\n".join(_toc))
            _sent = 0
            for _i, _m in enumerate(_msgs, 1):
                _raw = _m.get("content")
                _body = _raw if isinstance(_raw, str) else f"[多模态] {str(_raw)[:300]}"
                for _piece in _chunk_text(f"── #{_i} {_m.get('role')}（{len(_body)} 字）──\n{_body}",
                                          1200):
                    await bot.send(event, _piece)
                    await asyncio.sleep(0.4)
                    _sent += 1
                    if _sent >= 40:
                        await bot.send(event, "（太长，先打到这里；完整内容在 data/llm_trace）")
                        await _test_fn.finish()
            await _test_fn.finish()

        # 还没有真实请求（刚重启）→ 退回重建，但只打印 packet 里**没有**的块
        from services import prompt_builder as _pb
        from services.persona import persona_for as _persona_for
        _sections = []
        try:
            _pname, _ptext = _persona_for(_key_t)
            _sections.append((f"人格 system（{_pname}）", _ptext or "（空）"))
        except Exception as e:
            _sections.append(("人格 system", f"读取失败：{type(e).__name__}: {e}"))
        try:
            _proto = llm_mod._behaviour_protocol(
                _key_t, is_group=bool(_gid_t), has_ctx=True,
                addressed=True, dynamic=False)
            _sections.append(("规矩（固定区）", _proto or "（空）"))
            _dyn = llm_mod._dynamic_protocol(_key_t, is_group=bool(_gid_t))
            if _dyn:
                _sections.append(("规矩（每轮动态区）", _dyn))
        except Exception as e:
            _sections.append(("规矩块", f"读取失败：{type(e).__name__}: {e}"))
        _pk = {}
        try:
            _pk = _pb.build_packet(
                session_key=_key_t, user_text="（模板自检）",
                speaker_qq=_qq_t, speaker_name=display_name(event),
                gid=_gid_t, addressed=True, record=False) or {}
            for _k, _v in _pk.items():
                if isinstance(_v, str) and _v.strip():
                    _sections.append((f"上下文块 · {_k}", _v))
        except Exception as e:
            _sections.append(("上下文块", f"读取失败：{type(e).__name__}: {e}"))
        # relation / mood 已经在 packet 里就不再重复打印
        if "relation" not in _pk:
            try:
                _rel = profiles.relation_block(_qq_t, _gid_t, max_len=600)
                if _rel:
                    _sections.append(("关系底色", _rel))
            except Exception:
                pass
        if "mood" not in _pk:
            try:
                _sections.append(("心情", mood.status_block(_key_t) or "（空）"))
            except Exception:
                pass
        try:
            _tools = llm_mod.tools_for(_key_t)
            _lines = [f"共 {len(_tools)} 个"]
            for _t in _tools:
                _fn = (_t or {}).get("function") or {}
                _lines.append(f"· {_fn.get('name')}：{str(_fn.get('description') or '')[:160]}\n"
                              f"  参数 {json.dumps(_fn.get('parameters') or {}, ensure_ascii=False)[:300]}")
            _sections.append((f"工具 tools_for（{_key_t}）", "\n".join(_lines)))
        except Exception as e:
            _sections.append(("工具", f"读取失败：{type(e).__name__}: {e}"))
        _all = [f"▍{n}（{len(str(b))} 字）\n{b}" for n, b in _sections]
        _msg = ("（还没有真实请求，下面是重建版；块已去重）\n\n" + "\n\n".join(_all))
        _chunks, _cur = [], ""
        for _ln in _msg.split("\n"):
            if len(_cur) + len(_ln) + 1 > 1200 and _cur:
                _chunks.append(_cur)
                _cur = _ln
            else:
                _cur = (_cur + "\n" + _ln) if _cur else _ln
        if _cur:
            _chunks.append(_cur)
        for _i, _c in enumerate(_chunks, 1):
            await bot.send(event, f"（{_i}/{len(_chunks)}）\n{_c}")
            await asyncio.sleep(0.4)
        await _test_fn.finish()

    if what.startswith(("面板", "两栏", "心情好感")):
        # /测函数 面板 [群号] [QQ号] —— 只打「心情」和「好感」两栏（原样，不重拼）。
        _gid_p = _tgt_gid or str(getattr(event, "group_id", "") or "")
        _qq_p = _tgt_gid2 or (str(getattr(event, "user_id", "") or "")
                              if not _tgt_gid else "")
        _key_p = f"group_{_gid_p}" if _gid_p else _get_session_key(event)
        _nm_p = ""
        try:
            _nm_p = profiles.call_of(_qq_p, display_name(event), _gid_p) or _qq_p
        except Exception:
            _nm_p = _qq_p
        _lines = ["▍心情栏（这个会话，当前状态）"]
        try:
            _lines.append(mood.status_block(_key_p) or "（空）")
        except Exception as e:
            _lines.append(f"读取失败：{type(e).__name__}: {e}")
        _lines += ["", f"▍好感栏（对 {_nm_p}）"]
        try:
            _rel = profiles.relation_block(_qq_p, _gid_p, max_len=900)
            _lines.append(_rel or "（空：还没有好感/相处记录）")
        except Exception as e:
            _lines.append(f"读取失败：{type(e).__name__}: {e}")
        try:
            _mv = float(mood.get_mood(_key_p).get("value") or 0.0) / 100.0
            _av = float(profiles.get_affinity(_qq_p))
            _iv = float(profiles.intimacy(_qq_p))
            _lines += ["", f"原始值：心情 {_mv:+.2f}｜好感 {_av:+.2f}"
                           f"｜亲近 {_iv:.2f}"]
        except Exception:
            pass
        _msg = "\n".join(_lines)
        for _i, _c in enumerate(_chunk_text(_msg, 1200), 1):
            await bot.send(event, _c)
            await asyncio.sleep(0.3)
        await _test_fn.finish()

    if what.startswith(("群心情", "group_mood", "群氛围")):
        _gm_reset = "重置" in what or "清空" in what
        # /测函数 群心情 [群号] —— 独立于整体心情的那一层：
        # 5 小时窗口净变化 / 喜欢讨厌人数 / 单人 0.4 / 整群 0.7+3 人 / 多说话乘数。
        from services import group_mood as _gm
        gid = _tgt_gid or str(getattr(event, "group_id", "") or "")
        key = f"group_{gid}" if gid else _get_session_key(event)
        _head = ""
        if _gm_reset:
            try:
                _n = _gm.reset(key)
                _head = f"已清空 {key} 的 {_n} 条群心情事件。\n\n"
            except Exception as e:
                _head = f"清空失败：{type(e).__name__}: {e}\n\n"
        try:
            _txt = _gm.status_text(key)
        except Exception as e:
            _txt = f"读取失败：{type(e).__name__}: {e}"
        await _test_fn.finish((_head + "▍群心情（独立于整体心情）\n" + _txt)[:1800])

    if what.startswith(("分段", "segment", "攒段")):
        # /测函数 分段 [群号] —— 看「等一段说完再回」这一层到底在干什么：
        #   · 参数是多少（安静几秒算说完 / 最长攒多久 / 最多几条）
        #   · 现在 buffer 里攒着哪几条、最后一条多久之前
        #   · 最近处理过的段（心情 + 收发件人）
        # 用户 2026-11-22：「高频率群提问时一条一回很阻塞」→ 改成分段后要能自证。
        from services import segments as _seg
        gid = _tgt_gid or str(getattr(event, "group_id", "") or "")
        key = f"group_{gid}" if gid else _get_session_key(event)
        buf = _seg.current(key)
        lines = [
            f"▍群聊分段（{key}）",
            f"· 参数：安静 {_seg.WAIT_SEC:.0f}s 算说完"
            f"｜最长 {_seg.MAX_SEC:.0f}s"
            f"｜最多 {_seg.MAX_MSGS} 条"
            f"｜留 {_seg.HISTORY_KEEP} 段",
            f"· 当前攒着：{len(buf)} 条",
        ]
        if buf:
            _now = time.time()
            for r in buf[-10:]:
                _t = time.strftime("%H:%M:%S",
                                   time.localtime(float(r.get("ts") or 0)))
                lines.append(
                    f"  {_t} {r.get('name') or r.get('uid') or '?'}"
                    f" {_seg.addressee_of(r)}"
                    f"：{str(r.get('text') or '（图片/表情）')[:40]}")
            _idle = _now - float(buf[-1].get("ts") or _now)
            lines.append(
                f"· 最后一条在 {_idle:.1f}s 前"
                f"（再过 {max(0.0, _seg.WAIT_SEC - _idle):.1f}s 就算这一段说完）")
        rows = _seg.recent_processed(key, 3)
        if rows:
            lines.append("· 最近处理过的段：")
            for r in rows:
                _t = time.strftime("%H:%M",
                                   time.localtime(float(r.get("at") or 0)))
                lines.append(
                    f"  [{_t}] 心情 {r.get('mood') or '-'}"
                    f"（{float(r.get('value') or 0):+.2f}）"
                    f"｜收发件人 {r.get('addressee') or '-'}")
                lines.append("    " + (str(r.get("text") or "")
                                       .split("\n")[-1][:80]))
        else:
            lines.append("· 还没有处理过的段"
                         "（这是内存态，重启会清空；跑几轮群聊再看）")
        await _test_fn.finish("\n".join(lines)[:1800])

    if what.startswith("字幕"):
        bv = what.replace("字幕", "").strip().upper()
        if not _BV_RE_LOCAL.search(bv):
            await _test_fn.finish("用法：/测函数 字幕 BV1xx411c7mD")
        from services.link_preview import bili_detail, bili_subtitle
        d = await bili_detail(f"https://www.bilibili.com/video/{bv}")
        if not d:
            await _test_fn.finish("拿不到这个视频的信息（BV号对不对？）")
        sub = await bili_subtitle(d.get("bvid") or "", d.get("cid") or 0)
        await _test_fn.finish(
            f"《{d.get('title')}》UP主 {d.get('owner')}\n"
            f"字幕：{'有，' + str(len(sub)) + ' 字' if sub else '没有（会退化为简介+搜索）'}\n"
            f"{sub[:200] if sub else ''}"
        )

    if what.startswith(("猪猪", "rollpig", "小猪")):
        # 「/测函数 猪猪 全」= 连 yesterday / roast_self 一起跑（会写数据）
        await _test_fn.finish(await _rollpig_selfcheck(event, full=("全" in what)))

    if what.startswith(("原神", "面板", "genshin")):
        await _test_fn.finish(await _genshin_selfcheck())

    if what.startswith(("头像", "avatar")):
        # /测函数 头像 [QQ] —— 逐源打印：缓存 / NapCat / 直连 各自什么结果
        _qq_a = _tgt_qq or str(getattr(event, "user_id", "") or "")
        try:
            from services import profile_card as _pc
            _src = await _ensure_avatar(bot, _qq_a, _tgt_gid2)
            _txt = await asyncio.to_thread(_pc.avatar_probe, _qq_a)
        except Exception as e:
            await _test_fn.finish(f"头像自检失败：{type(e).__name__}: {e}")
        await _test_fn.finish(f"头像来源：{_src}\n\n{_txt}")

    if what.startswith(("全链路", "全检", "链路自检")):
        # /测函数 全链路 [群号] —— 一条命令看「心情这套到底通没通」
        import json as _json_c
        _gid_c = _tgt_gid or str(getattr(event, "group_id", "") or "")
        _sk_c = f"group_{_gid_c}" if _gid_c else _get_session_key(event)
        _out = ["🔗 全链路自检", f"· 会话：{_sk_c}"]

        def _ok(cond, txt):
            _out.append(("✅ " if cond else "❌ ") + txt)

        _out.append("【心情判读】")
        _out.append(mood_judge.status_text())
        _jd_c = mood.get_judge(_sk_c) or {}
        _mv = float((mood.get_mood(_sk_c) or {}).get("value") or 0.0)
        if _jd_c.get("word"):
            _ok(True, f"最近判读：{_jd_c['word']}"
                      f"（{float(_jd_c.get('value') or 0):+.2f}）"
                      f"｜心情值 {_mv / 100:+.2f}")
        else:
            _ok(False, "还没有判读结果（她回一句 / 或发「/测函数 心情 重判」）")
        _ok(bool(getattr(mood, "MOOD_FROM_JUDGE", False)),
            "数值只认判读："
            + ("是（关键词/自报不再改数值）" if getattr(mood, "MOOD_FROM_JUDGE", False)
               else "**否** —— 还在用关键词/自报，和判读会打架"))

        _out.append("【下游有没有跟着心情走】")
        _f = mood.mood_factors(_sk_c)
        _out.append(f"· 活泼/插话 {_f.get('chatty', 1):.2f}｜表情 {_f.get('sticker', 1):.2f}"
                    f"｜延迟 {_f.get('delay', 1):.2f}｜语音 {_f.get('voice', 1):.2f}")
        _ok(abs(_f.get("chatty", 1) - 1) > 1e-6 or abs(_mv) < 1e-6,
            "插话/表情概率确实按心情在缩放（因子≠1）")

        _out.append("【安静开关】")
        try:
            _off_c, _why_c = chat_switch.is_off(_sk_c)
            _ok(True, "安静中：" + (_why_c or "（没写原因）") if _off_c else "正常（没安静）")
            _out.append("· 被 @ 时仍给选择："
                        + ("是" if chat_switch.ANSWER_WHEN_CALLED else "否（强制不回）"))
        except Exception as e:
            _ok(False, f"查安静失败 {type(e).__name__}")

        _out.append("【安静建议（只算不改状态）】")
        try:
            _qa = quiet_advisor.render(
                _sk_c,
                speaker_qq=str(getattr(event, "user_id", "") or ""),
                gid=_gid_c,
                addressed=bool(getattr(event, "is_tome", lambda: False)()),
                dry_run=True,     # 自检不能把真正的建议冷却掉
            )
            if _qa:
                _ok(True, "现在有建议：\n" + _qa)
            else:
                _out.append("· 现在没有值得说的（好感/心情走势没到线，或已经在安静中）")
        except Exception as e:
            _ok(False, f"算安静建议失败 {type(e).__name__}: {e}")

        _out.append("【心里话】")
        _out.append("· " + mood_judge.think_stat_text().lstrip("· "))
        _ok(bool(mood_judge.recent_think_items(_sk_c, 1)), "有落盘的心里话记录")

        _out.append("【档案 / 好感】")
        try:
            _out.append("· " + profile_miner.status_text().splitlines()[0])
        except Exception:
            pass
        try:
            _spk_c = str(getattr(event, "user_id", "") or "")
            if _spk_c.isdigit():
                _out.append(f"· 对你的好感 {profiles.get_affinity(_spk_c):+.2f}"
                            f"｜亲近度 {profiles.intimacy(_spk_c):.2f}")
        except Exception:
            pass
        try:
            from services import memory_v2 as _mv2
            _items = (_mv2._load_index() or {}).get("items") or []
            # 只看**最近 50 条**：老记忆是改功能之前写的，本来就没有 mood_word，
            # 拿全量算比例会一直是 0/2383，看不出「新链路通没通」。
            _items.sort(key=lambda d: float(d.get("created_at") or 0), reverse=True)
            _recent = _items[:50]
            _with = [d for d in _recent if d.get("mood_word")]
            _ok(bool(_with),
                f"最近 50 条记忆里带判读心情的：{len(_with)} 条"
                f"（全库 {len(_items)} 条）")
        except Exception as e:
            _out.append(f"· 记忆检查失败 {type(e).__name__}")

        _out.append("【说话风格样本】")
        try:
            _rows = []
            for _f2 in sorted(Path("data/profiles").glob("*_style.json")):
                try:
                    _d2 = _json_c.loads(_f2.read_text(encoding="utf-8"))
                except Exception:
                    continue
                _n2 = int(_d2.get("_n") or 0)
                if _n2 >= 3:
                    _rows.append(_n2)
            _ok(bool(_rows), f"有 {len(_rows)} 个人的说话方式样本（≥3 句）")
        except Exception as e:
            _out.append(f"· 风格检查失败 {type(e).__name__}")
        await _test_fn.finish("\n".join(_out))

    if what.startswith(("prompt", "提示词", "关注度", "分段关注")):
        # /测函数 prompt —— 今日每段占比/关注度 + 各 purpose 缓存命中
        # 和 /用量 一样：文字报表 + 横向柱状图 + 扇形图（image-charts 渲染）
        from services import prompt_report
        _ptext = prompt_report.report_text()
        if any(w in what for w in ("文字", "text", "nochart")):
            await _test_fn.finish(_ptext)
        _paths = await prompt_report.download_charts()
        if not _paths:
            await _test_fn.finish(_ptext)
        _msg = Message(_ptext)
        for _p in _paths:
            _msg += MessageSegment.image(f"file://{Path(_p).resolve()}")
        await _test_fn.finish(_msg)

    if what.startswith(("白月光", "月光", "moon")):
        # /测函数 白月光 [群号] —— 她在这个群最常引用/回复/@谁，以及学到的说话方式
        from services import white_moon
        _wm_gid = _tgt_gid or (
            str(event.group_id) if isinstance(event, GroupMessageEvent) else "")
        await _test_fn.finish(white_moon.status_text(_wm_gid))

    if what.startswith(("风格", "说话方式", "style")):
        # /测函数 风格 [QQ] [群号] —— 看「她学到的说话方式」到底存了什么
        if not _tgt_qq:
            # 不带 QQ → 列出本群/全局有样本的人，一眼看出「学了谁、学了多少」
            import json as _json_s
            _rows = []
            for _f in sorted(Path("data/profiles").glob("*_style.json")):
                try:
                    _d = _json_s.loads(_f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                _n = int(_d.get("_n") or 0)
                if _n < 3:
                    continue
                _q = _f.name.split("_")[0]
                _nm = ""
                try:
                    _nm = profiles.call_of(_q, "", "") or \
                        str(profiles.get_profile(_q).get("nickname") or "")
                except Exception:
                    _nm = ""
                _rows.append((_n, _q, _nm, float(_d.get("avg_len") or 0),
                              int(_d.get("uses_emoji") or 0)))
            _rows.sort(reverse=True)
            if not _rows:
                await _test_fn.finish(
                    "🗣 还没有任何人的说话方式样本（要同一个人发够 3 句才开始统计）")
            _l2 = [f"🗣 她学到说话方式的人（{len(_rows)} 个，按样本数排）："]
            for _n, _q, _nm, _al, _ue in _rows[:15]:
                _l2.append(f"· {_nm or '?'}({_q})：{_n} 句"
                           f"｜平均 {_al:.1f} 字｜表情 {'是' if _ue else '否'}")
            _l2.append("· 看某个人详情：/测函数 风格 QQ号 [群号]")
            await _test_fn.finish("\n".join(_l2))
        _qq_s = _tgt_qq
        try:
            from services import profiles as _pf_s
            _st = _pf_s.get_style(_qq_s)
            _sum = _pf_s.style_summary(_qq_s)
            _p_s = _pf_s.get_profile(_qq_s)
        except Exception as e:
            await _test_fn.finish(f"取说话方式失败：{type(e).__name__}: {e}")
        _l = [
            f"🗣 说话方式（QQ {_qq_s}）",
            f"· 总结：{_sum or '（样本不足 3 条）'}",
            f"· 原始：平均 {float(_st.get('avg_len') or 0):.1f} 字"
            f"｜用表情 {'是' if _st.get('uses_emoji') else '否'}"
            f"｜样本 {_st.get('_n', 0)} 条",
            f"· 群名片：{_p_s.get('nickname') or '?'}"
            f"｜她叫他：{_p_s.get('call_name') or '（未设）'}"
            f"｜存在感 {_p_s.get('talkativeness')}",
        ]
        try:
            _gid_s = (_tgt_gid2 or _tgt_gid
                      or str(getattr(event, "group_id", "") or ""))
            _rows = [r for r in dialogue_log.load_recent(_gid_s, limit=300)
                     if str(r.get("qq") or "") == str(_qq_s)][-5:]
            if _rows:
                _l.append("· 他最近说的（用来对照她学得像不像）：")
                for _r in _rows:
                    _l.append("   · " + str(_r.get("text") or "")[:60])
        except Exception:
            pass
        await _test_fn.finish("\n".join(_l))

    if what.startswith(("安静", "chat_off", "silence")):
        # 两种用法：
        #   /测函数 安静 [群号/QQ] [时长] —— **真的**安静这个目标（主人测试用）
        #   /测函数 安静 [群号/QQ]        —— 只看状态（不带时长 = 不改状态）
        # 用户 2026-11-20：「我是否可以指令测试单独安静某个群呢？比如 /测函数 安静 群号 1小时」
        _rest_all = re.sub(r"^(安静|chat_off|silence)", "", _raw_args,
                           flags=re.I).strip()
        _target, _rest = _split_quiet_args(_rest_all)
        _dur = _DUR_RE.search(_rest)
        _clear = bool(re.search(r"解除|恢复|打开|说话|^on$", _rest, re.I))
        _l = ["🔇 安静开关"]
        _cur_gid = str(getattr(event, "group_id", "") or "")
        _uid_q = str(getattr(event, "user_id", "") or "")

        # 目标 → (kind, ident)
        _kind = _ident = ""
        if _target:
            _low_t = _rest.lower()
            if re.search(r"(群|group)", _low_t, re.I) or _target in ALLOWED_GROUPS:
                _kind, _ident = "群", _target
            elif (re.search(r"(人|qq|私聊|user)", _low_t, re.I)
                  or _target in ALLOWED_PRIVATE):
                _kind, _ident = "人", _target
            elif _target == _cur_gid:
                _kind, _ident = "群", _target
            else:
                _kind, _ident = "群", _target      # 默认按群号（测试指令，写错会立刻在状态里看出来）
        elif _cur_gid:
            _kind, _ident = "群", _cur_gid
        elif _uid_q:
            _kind, _ident = "人", _uid_q

        if _ident and (_dur or _clear):
            try:
                if _clear:
                    _had = chat_switch.set_on(_kind, _ident)
                    _l.append(f"✅ 已解除 {_kind} {_ident} 的安静"
                              + ("（之前确实安静着）" if _had else "（本来就没安静）"))
                else:
                    _mins = int(max(1, min(72 * 60, _minutes_of(_rest, 60))))
                    _rec = chat_switch.set_off(
                        _kind, _ident, minutes=_mins, reason="主人测试",
                        by="owner", strict=True)
                    _when = (f"{_mins} 分钟后自动恢复" if _rec.get("until")
                             else "一直安静，直到手动解除")
                    _l.append(f"✅ 已让 {_kind} {_ident} 安静 {_mins} 分钟（strict，连 @ 也不回）"
                              f"：{_when}")
                    logger.info(f"[SWITCH] /测函数 安静：{_kind} {_ident} "
                                f"强制安静 {_mins} 分钟")
            except Exception as e:
                _l.append(f"❌ 设置失败：{type(e).__name__}: {e}")

        try:
            _l.append(chat_switch.status_text())
        except Exception as e:
            _l.append(f"· 读状态失败：{type(e).__name__}: {e}")
        for _k, _label in ((f"group_{_ident}" if _kind == "群" and _ident else "", "群"),
                           (f"private_{_ident}" if _kind == "人" and _ident else "", "人")):
            if not _k:
                continue
            try:
                _off, _why = chat_switch.is_off(_k)
                _st = chat_switch.is_strict(_k)
                _l.append(f"· {_label} {_k}：{'安静中（' + _why + '）' if _off else '正常'}"
                          + ("｜**强制**（连 @ 也不回）" if _st else ""))
            except Exception as e:
                _l.append(f"· {_label} {_k}：查失败 {type(e).__name__}")
        _l.append(f"· 被 @ 时还给不给选择："
                  f"{'给（会回）' if chat_switch.ANSWER_WHEN_CALLED else '不给（强制不回）'}"
                  f"（CHAT_OFF_ANSWER_WHEN_CALLED）")
        # 安静建议：把「好感/心情走势 + 难插入」摊开，方便调阈值
        _l.append("")
        _l.append("【安静建议（只算不改状态）】")
        try:
            _l.append(quiet_advisor.status_text(
                f"group_{_ident}" if _kind == "群" and _ident else
                (f"private_{_ident}" if _ident else _get_session_key(event)),
                speaker_qq=_uid_q, gid=(_ident if _kind == "群" else ""),
                addressed=bool(getattr(event, "is_tome", lambda: False)())))
        except Exception as e:
            _l.append(f"· 算建议失败：{type(e).__name__}: {e}")
        _l.append("· 用法：/测函数 安静 [群号/QQ] [时长] = 真的安静；"
                  "不带时长 = 只看状态；加「解除」= 开回来。")
        await _test_fn.finish("\n".join(_l))

    if what.startswith(("挖掘", "mine", "档案挖掘")):
        # /测函数 挖掘 [QQ] [群号] —— 手动跑一次「从群聊提炼档案」
        _qq_m = _tgt_qq or str(getattr(event, "user_id", "") or "")
        _gid_m = (_tgt_gid2 or _tgt_gid
                  or str(getattr(event, "group_id", "") or ""))
        if not _gid_m:
            await _test_fn.finish("私聊里要带群号：/测函数 挖掘 123456 群号")
        _l = ["⛏ 档案挖掘（手动）", profile_miner.status_text(), ""]
        try:
            _r = await asyncio.wait_for(
                profile_miner.mine(_qq_m, _gid_m, force=True),
                timeout=profile_miner.TIMEOUT + 5)
        except Exception as e:
            await _test_fn.finish("\n".join(_l) + f"\n· 跑挂了：{type(e).__name__}: {e}")
        if not _r.get("ok"):
            _l.append(f"· 没挖成：{_r.get('err')}")
        else:
            _l.append(f"· 素材 {_r.get('n')} 句")
            _l.append(f"· 写入字段：{'、'.join(_r.get('updated') or []) or '（无）'}")
            _l.append(f"· 新增事情：{_r.get('events')} 条")
            for _k2, _v2 in (_r.get("fields") or {}).items():
                _l.append(f"   · {_k2}：{_v2}")
            _l.append("→ 用 /测函数 档案 " + _qq_m + " " + _gid_m + " 看卡片")
        await _test_fn.finish("\n".join(_l))

    if what.startswith(("档案", "人物卡", "词条", "profile", "lore")):
        _res = await _archive_selfcheck(bot, event, qq=_tgt_qq, gid=_tgt_gid2)
        if _res:
            await _test_fn.finish(_res)
        await _test_fn.finish()          # 图发成了：静默结束，不补文字

    if what.startswith(("审核", "audit")):
        # 只测「审核 LLM」这一层：为什么没审 / 返回了什么 / 改了几条
        _gid1 = _tgt_gid or str(getattr(event, "group_id", "") or "")
        if not _gid1:
            await _test_fn.finish("私聊里要带群号：/测函数 审核 123456789")
        try:
            _txt = await asyncio.wait_for(
                scene_auditor.diagnose(
                    _gid1,
                    bot_qq=_self_id(event),
                    bot_name=_bot_name(_gid1, _self_id(event)),
                    names=_scene_names(_gid1),
                ),
                timeout=max(4.0, scene_auditor.TIMEOUT + 2),
            )
        except Exception as e:
            logger.warning(f"[SCENE-AUDIT] 审核自检失败: {type(e).__name__}: {e}")
            _txt = f"🩺 审核自检\n· 跑挂了：{type(e).__name__}: {e}\n{scene_auditor.status()}"
        await _test_fn.finish(_txt)

    if what.startswith(("现场", "线程", "scene")):
        # 现场自检顺便强制跑一次审核 LLM，方便对照硬逻辑
        _gid0 = _tgt_gid or str(getattr(event, "group_id", "") or "")
        if not _gid0:
            await _test_fn.finish("私聊里要带群号：/测函数 现场 123456789")
        _self0 = _self_id(event)
        _scope, _snames, _tag = _gid0, _scene_names(_gid0), ""
        # 👇 没人说话也能测：/测函数 现场 回放（今天日志）/ 样例（内置）
        #    灌进 `<gid>#replay` 这个独立 scope，不碰真实现场。
        if any(k in what for k in ("回放", "历史", "样例", "假")):
            _scope, _snames, _tag = _scene_prepare_replay(
                _gid0, sample=("样例" in what or "假" in what), bot_qq=_self0)
            if not _scope:
                await _test_fn.finish("🎬 群聊现场\n· 回放失败：这个群没有可用消息。")
            logger.info(f"[SCENE] 回放 {_tag} → {_scope}")
        try:
            if _scope and scene_auditor.enabled():
                _pending = len(group_scene.needs_audit(_scope))
                logger.info(f"[SCENE-AUDIT] 自检强制审核 {_scope}，待审 {_pending} 条")
                _changed = await asyncio.wait_for(
                    scene_auditor.audit_recent(
                        _scope, bot_qq=_self0,
                        bot_name=_bot_name(_gid0, _self0),
                        names=_snames, force=True,
                    ),
                    timeout=max(2.0, scene_auditor.TIMEOUT + 1),
                )
                # ⚠️ 一定要打出来：以前这里不打，出问题只能看到「待审 7 条」，
                #    然后现场全是「审：未审核」，完全不知道卡在哪一步。
                logger.info(
                    f"[SCENE-AUDIT] 自检审核完成：改写 {_changed} 条｜"
                    + scene_auditor.status().replace("\n", " ｜ ")
                )
            else:
                logger.info(f"[SCENE-AUDIT] 自检跳过：{scene_auditor.status()}")
        except Exception as e:
            logger.warning(f"[SCENE-AUDIT] 自检审核失败: {type(e).__name__}: {e}")
        # 👇 默认发图片卡片（用户 2026-11-19：现场文字太长，log 会被反感）
        _img = ""
        try:
            from services import profile_card as _pc
            _img = _pc.render_scene_image(
                _scope,
                bot_qq=_self0,
                bot_name=_bot_name(_gid0, _self0),
            )
        except Exception as e:
            logger.warning(f"[SCENE-CARD] 渲染异常: {type(e).__name__}: {e}")
        if _img:
            # ⚠️ 失败要等几秒重发，**不要**退回文字：实测 NapCat 上传超时会 ActionFailed，
            #    但图两分钟后照样发出来了（用户 2026-11-19），补一段文字就是刷屏。
            if await _send_image_patiently(bot, event, _img):
                return
            logger.warning("[SCENE-CARD] 三次都失败（图可能仍在队列，不再补文字）")
            await _test_fn.finish("🖼 现场图还在上传，稍等；没到就再发一次 /测函数 现场")
        logger.warning("[SCENE-CARD] 现场图渲染返回空（Pillow/字体？），退回文字")
        await _test_fn.finish(_scene_selfcheck(event, gid=_scope))

    if what.startswith(("心情", "mood")):
        # 心情判读（影子）：她的原话 / 第三方判读 / 老数值 三者对照
        # 带群号就查那个群的心情（私聊里也能看群）
        _sk = f"group_{_tgt_gid}" if _tgt_gid else _get_session_key(event)
        try:
            _m = mood.get_mood(_sk)
        except Exception as e:
            await _test_fn.finish(f"取心情失败：{type(e).__name__}: {e}")
        _jd = _m.get("judge") or {}
        # 判读用的上下文：按**条数**拉最近 20 条（现场优先，私聊退回落盘日志）
        _ctxj = []
        try:
            _ctxj = mood_judge.scene_lines(_sk)
        except Exception as e:
            logger.debug(f"[MOOD-JUDGE] 取上下文失败: {e}")
        if "重置" in what:
            # /测函数 心情 重置 —— 把历史误判累积出来的 -1 清回 0
            try:
                _nv = mood.reset(_sk)
            except Exception as e:
                await _test_fn.finish(f"重置失败：{type(e).__name__}: {e}")
            await _test_fn.finish(
                f"已把 {_sk} 的心情重置为 {_nv:+.2f}。\n"
                "（历史判读流水还在 data/mood_history.jsonl，可以复盘）")
        if "重判" in what:
            # 她没写心情也能判：按最近 20 条群聊判断（用户 2026-11-19）
            _label0 = str(_m.get("label") or "").strip()
            if not _label0 and not _ctxj:
                await _test_fn.finish(
                    "既没有她写的心情、也拉不到上下文，没法判。\n"
                    "先让这个会话有几条消息，再来测。")
            try:
                _r = await asyncio.wait_for(
                    mood_judge.judge_and_store(_sk, _label0, _ctxj, force=True),
                    timeout=mood_judge.TIMEOUT + 2)
                if not _r.get("ok"):
                    await _test_fn.finish(
                        f"重判没成：{_r.get('err')}\n\n"
                        + mood_judge.trace_text(_sk))
                _jd = mood.get_judge(_sk)
            except Exception as e:
                await _test_fn.finish(f"重判失败：{type(e).__name__}: {e}")
        _lines = [
            "🧭 心情判读（影子运行，还没接进行为）",
            mood_judge.status_text(),
            f"· 会话：{_sk}",
            f"· 她自己写的：{_m.get('label') or '（还没写）'}",
            f"· 判读名词：{_jd.get('word') or '（还没判）'}",
        ]
        # 她的「心里话」（MEMO 的 think:）——以前判完就丢，现在落盘，这里能看见
        # 👇 逐条对照，而不是只给一句汇总（用户 2026-11-20：
        #    「它总是综合最后在一起算一件事，而不是每句话都反馈心理活动」）
        try:
            _th = mood_judge.recent_think_items(_sk, 5)
        except Exception:
            _th = []
        if _th:
            _lines.append("· 她的心里话（逐条，最新在下）：")
            for _it in _th:
                _ts = time.strftime("%H:%M",
                                    time.localtime(float(_it.get("at") or 0)))
                _src = str(_it.get("src") or "").strip()
                _rp = str(_it.get("reply") or "").strip()
                _auto_tag = "（补）" if _it.get("auto") else ""
                _lines.append(f"   {_ts}「{str(_it.get('text') or '')[:36]}」{_auto_tag}")
                if _src:
                    _lines.append(f"        接的是：{_src[:46]}")
                if _rp:
                    _lines.append(f"        她说：{_rp[:46]}")
        else:
            _lines.append("· 她的心里话：（没有 —— 她还没写过 [[THINK]]）")
        try:
            _lines.append(mood_judge.think_stat_text())
        except Exception:
            pass
        if _jd.get("value") is not None:
            try:
                _lines.append(f"· 阈值：{float(_jd.get('value')):+.2f}"
                              f"（-1.00 极差 / 0 平静 / +1.00 极好）")
            except Exception:
                pass
        if _jd.get("mind"):
            # 她自己没写 MEMO 时，由判读模型推断一句「心里话」——
            # 用户 2026-11-19：「我看这些内容没有心里话啊，只有回复。」
            _lines.append(f"· 判读出的心里话：「{_jd['mind']}」")
        elif _jd.get("word"):
            _lines.append("· 判读出的心里话：（这次模型没给，再发「重判」一次）")
        if _jd.get("why"):
            _lines.append(f"· 依据：{_jd['why']}")
        if _jd.get("src"):
            _lines.append(f"· 判读输入（她的话，或最后一条）：{_jd['src']}")
        if _jd.get("at"):
            _lines.append("· 判于：" + time.strftime(
                "%m-%d %H:%M:%S", time.localtime(float(_jd["at"]))))
        # 心情值对外统一 -1.00~+1.00（内部存 -100~100，所以 /100）
        _lines.append(
            f"· 心情值：{float(_m.get('value') or 0) / 100:+.2f}"
            "（-1 最难受 / 0 平静 / +1 最开心）—— 只由判读的 delta 增减")
        # 👇 「这次到底跑没跑、卡在哪」——用户 2026-11-19：以前这里只有「还没判」，
        #    log 里也看不到，等于没有诊断。
        _lines.append("— 上次尝试 / 闸门 —")
        _lines.append(mood_judge.trace_text(_sk, _jd))
        # 让她能直接看到「到底按几条、哪几条判的」——**全部列出来**，
        # 只给 5 条用户会说「工具还是不够 20 条」（2026-11-19）。
        _lines.append(f"· 判读上下文：按条数拉了最近 {len(_ctxj)} 条"
                      f"（上限 {mood_judge.CTX_LINES}）")
        for _i, _x in enumerate(_ctxj, 1):
            _lines.append(f"   {_i:>2}. {_x[:90]}")
        if not _ctxj:
            _lines.append("   （还没拉到：这个会话最近没消息，或现场是空的）")
        _lines.append("· 想让它重判一次：/测函数 心情 重判（她没写也能按群聊判）")
        await _test_fn.finish("\n".join(_lines))

    if what.startswith(("用量", "余额", "usage", "api")):
        # 两套账分开看：
        #   · llm_usage —— 按 purpose / model 的 token 归因（含 judge / audit 两个三方 LLM）
        #   · api_usage —— 供应商余额（DeepSeek /user/balance）
        # 以前这里只调 api_usage，所以「哪块最贵」「三方 LLM 用了多少」永远看不到。
        #
        # ⚠️ 2026-11-23 修：finish() 抛的 FinishedException 以前被外层
        #    `except Exception` 接住 → 报表发完又补一句「查用量失败：FinishedException」。
        #    现在 try 只包「组装文本」，finish 放在 try 外面。
        _d = 1
        _m = re.search(r"\d+", what)
        if _m:
            _d = max(1, min(30, int(_m.group(0))))
        try:
            from services import llm_usage as _lu, api_usage as _au
            _out = [_lu.report_text(_d)]
            try:
                _bal = await _au.fetch_balance()
                if _bal.get("ok") and _bal.get("items"):
                    for _it in _bal["items"]:
                        _out.append(
                            f"💰 余额（{_bal.get('provider')}）：{_it.get('total')} "
                            f"{_it.get('currency') or ''}"
                            f"（赠送 {_it.get('granted')}｜充值 {_it.get('topped_up')}）")
                elif _bal.get("ok"):
                    _out.append("💰 " + (_bal.get("note") or "供应商没返回余额"))
                else:
                    _out.append(f"💰 余额查不到：{_bal.get('err') or '未知原因'}")
            except Exception as _e:
                _out.append(f"💰 余额查不到：{type(_e).__name__}: {_e}")
            _text = "\n\n".join(_out)
        except Exception as e:
            _text = f"查用量失败：{type(e).__name__}: {e}"

        # 图表：默认带图；`/测函数 用量 文字` 只看数字
        _paths = []
        if not any(w in what for w in ("文字", "text", "nochart")):
            try:
                from services import usage_chart as _uc
                _paths = await _uc.download_charts(_d)
            except Exception as _e:
                logger.warning(f"[USAGE] 图表生成失败（忽略）: {type(_e).__name__}: {_e}")
        if not _paths:
            await _test_fn.finish(_text)
        _msg = Message(_text)
        for _p in _paths:
            _msg += MessageSegment.image(f"file://{Path(_p).resolve()}")
        await _test_fn.finish(_msg)

    await _test_fn.finish(f"不认识的测项：{what}")


# ================= /上传文件：把 QQ 文件下载到 data/（仅主人）=================
# 用户 2026-11-19：「我需要一个可以下载文件到 data/ 目录的功能，只能主人使用。
#   比如我发 /上传文件，然后循环 3 分钟等我的文件。接收到 qq 文件就下载。」
# 为什么不用「直接发文件就存」：群里谁都能发文件，那不成了公共投毒口。
_FILE_WAIT: dict = {}
_FILE_WAIT_TTL = tuning.f("UPLOAD_WAIT_SEC")     # 默认 3 分钟
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "data/uploads"))
UPLOAD_MAX_MB = tuning.f("UPLOAD_MAX_MB")

upload_cmd = on_command("上传文件", aliases={"收文件", "存文件"}, priority=4, block=True)


def _safe_upload_name(name: str) -> str:
    """文件名消毒：去掉路径和 Windows 非法字符，别让人用 ../../ 写出去。"""
    n = Path(str(name or "").replace("\\", "/")).name
    n = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", n).strip(" .")
    return (n or f"upload_{int(time.time())}")[:120]


async def _fetch_qq_file(bot: Bot, event: MessageEvent, data: dict) -> tuple:
    """
    取 QQ 文件的字节。返回 (文件名, bytes, 错误)。
    NapCat 的 file 段有时直接带 url；没有就调 get_group_file_url / get_private_file_url。
    """
    data = data or {}
    name = _safe_upload_name(
        str(data.get("file") or data.get("name") or data.get("file_name") or ""))
    url = str(data.get("url") or "").strip()
    fid = str(data.get("file_id") or data.get("id") or "").strip()
    if not url and fid:
        try:
            if isinstance(event, GroupMessageEvent):
                r = await bot.call_api(
                    "get_group_file_url", group_id=int(event.group_id),
                    file_id=fid, busid=int(data.get("busid") or 0))
            else:
                r = await bot.call_api(
                    "get_private_file_url", user_id=int(event.user_id), file_id=fid)
            url = str((r or {}).get("url") or (r or {}).get("file") or "")
        except Exception as e:
            return name, b"", f"取下载地址失败：{type(e).__name__}: {e}"
    if not url:
        return name, b"", "没拿到下载地址（NapCat 可能不支持取文件链接）"
    try:
        import httpx
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            body = r.content
    except Exception as e:
        return name, b"", f"下载失败：{type(e).__name__}: {e}"
    return name, body, ""


async def _save_upload(name: str, body: bytes) -> tuple:
    """存到 data/uploads/YYYYMMDD/。返回 (路径, 错误)。"""
    if not body:
        return "", "文件是空的"
    mb = len(body) / 1024 / 1024
    if mb > UPLOAD_MAX_MB:
        return "", f"文件 {mb:.1f}MB，超过上限 {UPLOAD_MAX_MB:.0f}MB"
    try:
        d = UPLOAD_DIR / time.strftime("%Y%m%d")
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        if p.exists():      # 重名别覆盖，加个时间戳
            p = p.with_name(f"{p.stem}_{int(time.time())}{p.suffix}")
        p.write_bytes(body)
        return str(p), ""
    except Exception as e:
        return "", f"落盘失败：{type(e).__name__}: {e}"


@upload_cmd.handle()
async def _(bot: Bot, event: MessageEvent):
    if not is_owner(str(event.user_id)):
        await upload_cmd.finish("只有主人能往 data/ 传文件。")
    # ① 命令这条本身就带着文件 → 直接收
    seg = next((s for s in event.get_message() if s.type == "file"), None)
    if seg is not None:
        name, body, err = await _fetch_qq_file(bot, event, seg.data or {})
        if err:
            await upload_cmd.finish(f"❌ {err}")
        p, err = await _save_upload(name, body)
        if err:
            await upload_cmd.finish(f"❌ {err}")
        await upload_cmd.finish(
            f"✅ 已存：{p}\n· {name}｜{len(body) / 1024:.0f} KB")
    # ② 没带 → 挂 3 分钟等
    _FILE_WAIT[str(event.user_id)] = {"until": time.time() + _FILE_WAIT_TTL}
    await upload_cmd.finish(
        f"好，{_FILE_WAIT_TTL / 60:.0f} 分钟内把文件发过来（QQ 的「文件」，"
        f"不是图片/语音）。只收你一个人的，别人发的不算。")


def _file_wait_rule(event: MessageEvent) -> bool:
    uid = str(event.user_id)
    info = _FILE_WAIT.get(uid)
    if not info:
        return False
    if time.time() > float(info.get("until") or 0):
        _FILE_WAIT.pop(uid, None)
        return False
    if not is_owner(uid):
        return False
    return any(s.type == "file" for s in event.get_message())


upload_recv = on_message(rule=Rule(_file_wait_rule), priority=1, block=True)


@upload_recv.handle()
async def _(bot: Bot, event: MessageEvent):
    _FILE_WAIT.pop(str(event.user_id), None)
    seg = next((s for s in event.get_message() if s.type == "file"), None)
    name, body, err = await _fetch_qq_file(bot, event, seg.data or {})
    if err:
        await upload_recv.finish(f"❌ {err}（再发一次 /上传文件 重试）")
    p, err = await _save_upload(name, body)
    if err:
        await upload_recv.finish(f"❌ {err}")
    await upload_recv.finish(
        f"✅ 已存：{p}\n· {name}｜{len(body) / 1024:.0f} KB")


# ================= /记忆体检（抄 dsh-plugin-memory 的 checkup）=================
mem_check = on_command("记忆体检", aliases={"记忆检查"}, priority=5, block=True)


@mem_check.handle()
async def _(event: MessageEvent):
    if not is_owner(str(event.user_id)):
        await mem_check.finish("这个只有主人能看哦。")
    try:
        r = memory_v2.health_report()
    except Exception as e:
        await mem_check.finish(f"体检失败：{type(e).__name__}: {e}")

    lines = [
        "🧠 记忆库体检",
        f"· 总数 {r['total']}（活跃 {r['active']} / 过期 {r['outdated']}）",
        f"· 带情绪刻痕 {r['emotion_hit']} 条",
    ]
    if r["kinds"]:
        lines.append("· 类型：" + "、".join(f"{k} {v}" for k, v in r["kinds"]))
    if r["tags"]:
        lines.append("· 热门主题：" + "、".join(f"{t}({c})" for t, c in r["tags"]))
    if r["dup_total"]:
        lines.append(f"⚠️ 疑似重复 {r['dup_total']} 对，最像的 3 对：")
        for d in r["dups"][:3]:
            lines.append(f"  {d['sim']} 「{d['a_text']}」≈「{d['b_text']}」")
    else:
        lines.append("✅ 没有明显重复")
    if r["stale_total"]:
        lines.append(f"💤 长期没被想起 {r['stale_total']} 条，最久的 3 条：")
        for s in r["stale"][:3]:
            lines.append(f"  {s['days']} 天 「{s['text']}」")
    await mem_check.finish("\n".join(lines))


def _items_to_message(items) -> Message:
    """
    parse_reply 的 items → 可直接发送的 Message。

    为什么不用 _segs_to_message：那个吃的是 `{"type","data"}` 段，
    而 parse_reply 给的是 `{"kind","value"}` —— 直接塞进去会得到一条空消息
    （这就是典型的「设计 ok 但参数没接上」）。
    """
    out = []
    for it in items or []:
        k = str(it.get("kind") or "")
        v = str(it.get("value") or "").strip()
        if k == "text" and v:
            # 最后一道：任何协议标记都不许出现在发出去的文字里
            v = strip_protocol_residue(v).strip()
            if not v:
                continue
            _ats, _body = _split_at_tags(v)
            for _nm in _ats:
                out.append(MessageSegment.text(f"@{_nm} "))
            if _body.strip():
                out.append(MessageSegment.text(_body.strip()))
        elif k == "sticker":
            try:
                pk = sticker_lib.pick_by_tag(v) or sticker_lib.pick_any() or {}
                _p = Path(str(pk.get("path") or ""))
                if str(_p) and _p.exists():
                    out.append(MessageSegment.image(_p))
            except Exception as e:
                logger.debug(f"[PROACTIVE] 挑表情失败: {e}")
        # voice 在主动私聊里降级成文字（已经在 text 里），不单独处理
    return Message(out)


# ================= 主动找私聊的人聊天 =================
# 用户 2026-11-19：「自动接话…根据心情去选择是否主动找某些用户聊天。当然群里不要加这个功能。」
# 群里已经有 _random_chat_loop（看当下对话插一句）；这里是**看关系和时间**主动找人。
# 判定逻辑全在 services/proactive.py（纯函数，能单测），这里只负责「调模型 + 发出去」。
async def _send_proactive(bot: Bot, cand: dict) -> bool:
    qq = str(cand.get("qq") or "")
    if not qq:
        return False
    key = f"private_{qq}"
    name = str(cand.get("name") or qq)
    lines = list(cand.get("lines") or [])

    text = proactive.prompt_block(cand)
    if lines:
        text += "\n\n【你们最近的对话】\n" + "\n".join(lines[-8:])

    _old = dict(llm_mod.TOOL_CTX)
    try:
        # 主动找人没有 event（不是谁发消息触发的），但要让工具层知道「跟谁说话」
        llm_mod.set_tool_context(bot=bot, event=None, speaker_qq=qq,
                                 speaker_name=name, addressed=True, newer_count=0)
        reply, _ = await call_llm(key, text, group_context=lines or None,
                                  speaker_qq=qq, speaker_name=name)
    except Exception as e:
        logger.warning(f"[PROACTIVE] 生成失败: {type(e).__name__}: {e}")
        return False
    finally:
        llm_mod.TOOL_CTX.clear()
        llm_mod.TOOL_CTX.update(_old)

    parsed = parse_reply(reply)
    _apply_mood_label(parsed, f"private_{qq}")
    if parsed.get("silent"):
        logger.info(f"[PROACTIVE] {name}({qq}) 想了想，决定不打扰（stay_silent）")
        proactive.mark_skipped(qq, "stay_silent")
        return False

    items = parsed.get("items") or []
    clean = " ".join(str(i.get("value") or "") for i in items
                     if i.get("kind") == "text").strip()
    if not clean:
        proactive.mark_skipped(qq, "空回复")
        return False

    if proactive.DRY_RUN:
        logger.info(f"[PROACTIVE][DRY] 本来要发给 {name}({qq})：{clean[:60]}")
        proactive.mark_skipped(qq, "dry-run")
        return False

    try:
        _msg = _items_to_message(items)
        if not len(_msg):
            proactive.mark_skipped(qq, "转消息为空")
            return False
        await bot.send_private_msg(user_id=int(qq), message=_msg)
    except Exception as e:
        logger.warning(f"[PROACTIVE] 发给 {qq} 失败: {type(e).__name__}: {e}")
        return False

    # 落盘 + 记忆：下次她自己能想起来「我昨天主动找过他」
    try:
        dialogue_log.log_msg(key, _bot_name(), _self_id(bot=bot),
                             clean, kind="bot")
    except Exception:
        pass
    try:
        memory_v2.remember(text=f"我主动找{name}聊天：{clean[:80]}",
                           kind="event", about_qq=qq, importance=0.55)
    except Exception:
        pass
    proactive.mark_sent(qq)
    logger.info(f"[PROACTIVE] ✅ 主动私聊 {name}({qq})：{clean[:60]}")
    return True


async def _proactive_private_loop(bot: Bot):
    await asyncio.sleep(90)          # 启动后别立刻发，先让别的初始化跑完
    while True:
        try:
            if proactive.ENABLED and skill_switch.is_enabled("proactive.private"):
                cand = proactive.pick(ALLOWED_PRIVATE)
                if cand:
                    logger.info(
                        f"[PROACTIVE] 选中 {cand.get('name')}({cand['qq']})："
                        f"分数 {cand.get('score')}（心情 {cand.get('chatty')} × "
                        f"亲密 {cand.get('intimacy')} × 漂移 {cand.get('drift')}，"
                        f"隔了 {cand.get('idle_h')}h）")
                    await _send_proactive(bot, cand)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[PROACTIVE] 循环异常: {type(e).__name__}: {e}")
        await asyncio.sleep(max(60.0, proactive.CHECK_SEC))


# ================= 每日 0 点总结 =================
# 短历史每天新开（CHAT_HISTORY_DAILY），第二天她看不到昨天聊了啥 ——
# 这个循环每天 0:05 把昨天的对话压成「昨日总结」，第二天注入 prompt 做交接。
async def _daily_summary_loop():
    await asyncio.sleep(60)
    _last_day = ""
    while True:
        try:
            now = time.localtime()
            if now.tm_hour == 0 and now.tm_min >= 5 and _last_day != time.strftime("%Y-%m-%d", now):
                _last_day = time.strftime("%Y-%m-%d", now)
                yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
                logger.info(f"[SUMMARY] 开始总结 {yesterday}")
                try:
                    from services.daily_summary import summarize_day
                    n = await summarize_day(yesterday)
                    logger.info(f"[SUMMARY] {yesterday} 完成，{n} 个会话")
                except Exception as e:
                    logger.warning(f"[SUMMARY] 总结失败: {type(e).__name__}: {e}")
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[SUMMARY] 循环异常: {e}")
            await asyncio.sleep(300)


# ================= 启动 =================
_loop_flags = {"random": False, "summary": False, "proactive": False}


@get_driver().on_bot_connect
async def _start_loops(bot: Bot):
    if RANDOM_CHAT_ENABLED and not _loop_flags["random"]:
        _loop_flags["random"] = True
        asyncio.create_task(_random_chat_loop(bot))
    if not _loop_flags["summary"]:
        _loop_flags["summary"] = True
        asyncio.create_task(_daily_summary_loop())
    # 主动私聊：群里不挂（群里走 _random_chat_loop）
    if proactive.ENABLED and not _loop_flags["proactive"]:
        _loop_flags["proactive"] = True
        asyncio.create_task(_proactive_private_loop(bot))
        logger.info(f"[PROACTIVE] 主动私聊已启动（每 {proactive.CHECK_SEC:.0f}s 评估一次，"
                    f"每人每天 ≤{proactive.MAX_PER_DAY} 次）")


# ================= send：发不发、发什么，由她决定 =================
# 用户 2026-11-18：工具**先不主动 send**。运势/猪猪/美少女/表情包这些工具
# 只把内容取回来（图片会被拦成「待发」），她看完再决定要不要调 send。
def _pending_box() -> list:
    box = llm_mod.TOOL_CTX.get("pending_sends")
    if box is None:
        box = []
        llm_mod.TOOL_CTX["pending_sends"] = box
    return box


def _pending_sent() -> set:
    """已经发过的待发编号（不 pop，避免后一个编号错位）。"""
    done = llm_mod.TOOL_CTX.get("pending_sent")
    if done is None:
        done = set()
        llm_mod.TOOL_CTX["pending_sent"] = done
    return done


# send 工具允许读取的本地目录（防止她把容器内任意文件发出去）
# 只放运行产物目录；代码目录、/etc、~/.ssh 一律拒绝。
_SEND_LOCAL_ROOTS = [
    Path("data").resolve(),
    Path("tmp").resolve(),
    Path("/tmp").resolve(),
    Path("comfy_workflows").resolve(),
]


def _safe_send_path(p: str):
    """把本地路径限制在运行产物目录内。返回 Path 或 None。"""
    try:
        rp = Path(p).expanduser().resolve()
    except Exception:
        return None
    for root in _SEND_LOCAL_ROOTS:
        try:
            rp.relative_to(root)
            return rp
        except Exception:
            continue
    return None


async def _tool_send(args: dict) -> str:
    """把待发内容 / 文字 / 图片 / 语音发到当前会话。"""
    from pathlib import Path
    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")
    if not bot or not ev:
        return "现在没法发（缺会话信息）"

    box = _pending_box()
    done = _pending_sent()
    msg = Message()
    used = []

    # ① 工具拦下来的待发内容（编号从 1 开始，工具结果里会告诉她）
    _p = args.get("pending")
    if _p not in (None, "", 0, "0"):
        _ids = _p if isinstance(_p, (list, tuple)) else [_p]
        for _raw in _ids:
            try:
                _i = int(_raw)
            except Exception:
                continue
            if 1 <= _i <= len(box) and _i not in done:
                msg += box[_i - 1]
                used.append(_i)

    # ② 她自己写的文字 / @ / 图片 / 语音
    _text = str(args.get("text") or "").strip()
    if _text:
        msg += MessageSegment.text(_text)
    _at = str(args.get("at") or "").strip()
    if _at.isdigit():
        msg += MessageSegment.at(int(_at))
    for _key, _seg in (("image", MessageSegment.image), ("voice", MessageSegment.record)):
        _f = str(args.get(_key) or "").strip()
        if not _f:
            continue
        if _f.startswith(("http://", "https://", "base64://")):
            msg += _seg(_f)
            continue
        # file:// 或裸路径 → 必须在允许的运行产物目录内
        _raw = _f[7:] if _f.startswith("file://") else _f
        _sp = _safe_send_path(_raw)
        if _sp is None:
            logger.warning(f"[SEND-TOOL] 拒绝发送越界路径: {_f[:120]}")
            return (f"这个路径不能发（只允许 data/、tmp/、comfy_workflows/ 下的文件）："
                    f"{_f[:80]}")
        msg += _seg(f"file://{_sp}")

    if not msg:
        return "没说要发什么（pending / text / image / voice 至少给一个）"

    try:
        await bot.send(event=ev, message=msg)
    except Exception as e:
        if _is_send_timeout(e):
            return "发出去了（图片可能晚几秒到）"
        logger.warning(f"[SEND-TOOL] 发送失败: {type(e).__name__}: {e}")
        return f"没发出去：{type(e).__name__}"

    done.update(used)
    return "发出去了"


register_tool(
    {
        "type": "function",
        "function": {
            "name": "send",
            "description": (
                "把内容发到当前会话。工具取到的内容（运势卡片、猪猪卡片、美少女图、"
                "表情包等）**不会自动发**：工具结果里会写「编号 N：…（还没发出去）」，"
                "你决定要发就调 send(pending=N)。也可以直接用 text/image/voice 发。"
                "不想发就别调 —— 但别跟对方说已经发了。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pending": {"type": "integer", "description": "工具结果里的待发编号（1、2…）"},
                    "text": {"type": "string", "description": "要发的文字"},
                    "image": {"type": "string", "description": "图片路径或 URL"},
                    "voice": {"type": "string", "description": "语音文件路径"},
                    "at": {"type": "string", "description": "要 @ 的 QQ 号"},
                },
                "required": [],
            },
        },
    },
    _tool_send,
    scope=("private", "group"),
)


# ================= DeepSeek 可调用的工具（只在私聊生效）=================
# 目的：让她能主动做事，而不是只能等 `/翻唱` 这种正则指令。
# 比如「我答应过你 11 月 5 日唱晴天」→ 她可以直接调 sing 去唱。
async def _tool_sing(args: dict) -> str:
    song = str(args.get("song") or "").strip()
    if not song:
        return "没说要唱什么"
    gender = str(args.get("gender") or "").strip().lower()
    if gender not in ("male", "female"):
        gender = ""
    # 模式：清唱 = 只要人声，不带伴奏。不填（或填翻唱）就走原来的带伴奏。
    # `[[SING|歌名|清唱]]` 的第三段也会从 llm 那边传进 mode。
    mode = str(args.get("mode") or "").strip().lower()
    with_inst = mode not in ("清唱", "清唱版", "纯人声", "acapella", "vocal", "no_inst")
    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")
    if not bot or not ev:
        return "现在没法唱（缺少会话信息）"
    try:
        from plugins.song_cover import cover_on, process_cover_task
    except Exception as e:
        return f"唱歌功能没加载：{type(e).__name__}"
    # 👇 /翻唱 off 期间她也别自己点歌，否则算力机还是被塞满
    if not cover_on():
        return ("翻唱现在是关着的（主人关的，算力机忙），唱不了。"
                "别答应她，直接跟对方说现在不接单，等主人 /翻唱 on。")
    # 后台跑，不阻塞这一轮回复；唱完她自己会把成品发出来
    _task = asyncio.create_task(process_cover_task(bot, ev, song, with_inst, gender))

    def _on_done(t):
        # 👇 create_task 里抛的异常默认会被静默吞掉 —— 之前 process_cover_task
        #    签名不匹配（多传了 gender），每次她主动唱歌都 TypeError 却一点日志都没有，
        #    表现就是「她答应了但什么都没发生，只能手动 /翻唱」。
        try:
            _exc = t.exception()
        except Exception:
            _exc = None
        if _exc:
            logger.warning(f"[TOOL] 唱歌任务失败《{song}》: "
                           f"{type(_exc).__name__}: {_exc}")

    _task.add_done_callback(_on_done)
    return (f"已经{'开唱' if with_inst else '开始清唱'}《{song}》了，几分钟后发出来")


async def _tool_remember(args: dict) -> str:
    text = str(args.get("text") or "").strip()
    if not text:
        return "没说要记什么"
    qq = str(llm_mod.TOOL_CTX.get("speaker_qq") or "")
    try:
        imp = float(args.get("importance") or 0.7)
    except Exception:
        imp = 0.7
    try:
        memory_v2.remember(text=text, kind="impression", about_qq=qq,
                           importance=max(0.1, min(1.0, imp)))
    except Exception as e:
        return f"记失败了：{type(e).__name__}"
    return "记住了"


async def _tool_recall(args: dict) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "没说要查什么"
    qq = str(llm_mod.TOOL_CTX.get("speaker_qq") or "")
    # 2026-11-20 审计 F2：[[PROFILE]] / [[MEMORY]] 这条路径以前没传 gid → 跨群泄漏
    _ev = llm_mod.TOOL_CTX.get("event")
    _gid = str(getattr(_ev, "group_id", "") or "")
    try:
        hits = memory_v2.recall(query, limit=5, speaker_qq=qq, gid=_gid)
    except Exception as e:
        return f"查失败了：{type(e).__name__}"
    if not hits:
        return "没查到相关的记忆"
    return "；".join(f"{h['summary']}（{h['ago']}）" for h in hits)


register_tool(
    {
        "type": "function",
        "function": {
            "name": "sing",
            "description": (
                "给她唱一首歌（默认翻唱，带伴奏）。唱完会自动把成品发出来，"
                "你只需要说一句确认的话，不要假装已经唱完。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "song": {"type": "string", "description": "歌名，例如 晴天"},
                    "gender": {
                        "type": "string",
                        "enum": ["male", "female"],
                        "description": "原唱性别，不确定就别填",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["翻唱", "清唱"],
                        "description": "不填就是翻唱（带伴奏）；对方要清唱/干声才填清唱",
                    },
                },
                "required": ["song"],
            },
        },
    },
    _tool_sing,
)

register_tool(
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "把重要的事写进长期记忆（承诺、对方的重要信息、你自己想记住的事）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要记住的内容，一句话"},
                    "importance": {"type": "number", "description": "重要程度 0~1，默认 0.7"},
                },
                "required": ["text"],
            },
        },
    },
    _tool_remember,
)

register_tool(
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "主动查长期记忆（想不起某件事的时候用）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要查的关键词或问题"},
                },
                "required": ["query"],
            },
        },
    },
    _tool_recall,
)


# ================= 档案 / 词条工具（2026-11-18） =================
async def _resolve_target_qq(name: str) -> str:
    """
    把「名字 / @ / QQ号」解析成 QQ 号；群里走名册，私聊只认当前人。

    ⚠️ 模型传进来的格式五花八门（用户 2026-11-20：「群里 @ 她再看另一个人的档案」）：
      · `123456`
      · `@小明(123456)`   ← 正文里 @ 段就是长这样（_at_text 拼的）
      · `<at:小明>` / `[at:qq=123456]` ← 模型把标签原样抄进来
      · `小明`
    以前只认纯数字和名册精确名 → 前两种直接解析失败，她就说「没找到这个人」。
    """
    raw = str(name or "").strip()
    # ① 能抠出 5~12 位 QQ 就直接用（@名字(QQ) / qq=123 / 纯数字都覆盖）
    m = re.search(r"(?<!\d)(\d{5,12})(?!\d)", raw)
    if m:
        return m.group(1)
    # ② 把 <at:小明> / [at:小明] 拆成名字
    raw = re.sub(r"[\[<]{1,2}\s*at\s*:?\s*([^\]>]*)[\]>]{1,2}", r"\1", raw,
                 flags=re.IGNORECASE)
    raw = raw.lstrip("@＠").strip()
    # ③ 去掉模型可能加的书名号/引号
    raw = raw.strip("「」『』\"'“” ")
    if not raw:
        return ""
    ev = llm_mod.TOOL_CTX.get("event")
    bot = llm_mod.TOOL_CTX.get("bot")
    gid = getattr(ev, "group_id", 0) or 0
    if raw and gid and bot:
        try:
            members = await group_roster.members_of(bot, gid)
            hit = group_roster.find(members, raw, limit=1)
            if hit:
                return str(hit[0].get("user_id") or "")
        except Exception:
            pass
    return ""


def _lore_scope() -> str:
    """lorebook 的命名空间：群用群号，私聊用 private_QQ（私聊也有自己的词条）。"""
    ev = llm_mod.TOOL_CTX.get("event")
    gid = str(getattr(ev, "group_id", "") or "")
    if gid:
        return gid
    uid = str(getattr(ev, "user_id", "") or llm_mod.TOOL_CTX.get("speaker_qq") or "")
    return f"private_{uid}" if uid else ""


async def _tool_profile_card(args: dict) -> str:
    name = str(args.get("name") or args.get("qq") or "").strip()
    qq = await _resolve_target_qq(name) if name else str(
        llm_mod.TOOL_CTX.get("speaker_qq") or "")
    if not qq:
        return "没找到这个人（群里没这个名字，或你只给了个外号）"
    ev = llm_mod.TOOL_CTX.get("event")
    gid = str(getattr(ev, "group_id", "") or "")
    try:
        from services import profiles, profile_card
        card = profiles.render_card(qq, gid, detail="full", max_len=420)
        # 先让 NapCat 拿头像（LLM 调工具时也一样，不能只靠容器直连）
        try:
            _b = llm_mod.TOOL_CTX.get("bot")
            if _b is None:
                from nonebot import get_bot
                _b = get_bot()
            await _ensure_avatar(_b, qq, gid)
        except Exception:
            pass
        img = await asyncio.to_thread(profile_card.render_profile_image, qq, gid)
    except Exception as e:
        return f"取档案失败：{type(e).__name__}"
    if not card and not img:
        return "这个人的档案还是空的（只知道名字，别的没记过）"
    # ⚠️ 只回图片路径，**不要回文字卡**：
    #    以前返回「文字卡 + 图片路径」，模型会先把文字卡复述一遍，再调 send 发图，
    #    用户就收到「先一段文字、再一张卡片」（2026-11-20 反馈）。
    if img:
        return (f"档案图片已生成：{img}\n"
                "→ 直接 send(image=这个路径) 发出去。"
                "**不要复述档案内容，也不要先发一段文字**，只发图。")
    return (card or "") + "\n（图片没渲染出来，只能先发这段文字）"


async def _tool_profile_update(args: dict) -> str:
    """LLM 学到的稳定人物信息写回档案（不碰称呼/主人这些敏感字段）。"""
    name = str(args.get("name") or "").strip()
    speaker = str(llm_mod.TOOL_CTX.get("speaker_qq") or "")
    target = await _resolve_target_qq(name) if name else speaker
    if not target:
        return "没找到要更新的人"
    if target != speaker and not is_owner(speaker):
        return "只有主人能改别人的档案；你自己的可以直接写"
    fields = {}
    for k in ("identity", "personality", "scenario", "depth_note"):
        v = str(args.get(k) or "").strip()
        if v:
            fields[k] = v
    ex = str(args.get("mes_example") or "").strip()
    if ex:
        fields["mes_example"] = ex
    if "talkativeness" in args:
        fields["talkativeness"] = args.get("talkativeness")
    # 「我在他面前是什么样」—— 撒娇/高冷/可爱/中二…（她自己的姿态）
    stance = str(args.get("stance") or "").strip()
    if not fields and not stance:
        return "没给要更新的内容"
    try:
        if fields:
            profiles.update_fields(target, **fields)
        if stance:
            profiles.set_stance(target, stance)
    except Exception as e:
        return f"写档案失败：{type(e).__name__}"
    return (f"写进 {profiles.call_of(target, target)} 的档案了"
            + (f"（你在他面前：{stance}）" if stance else ""))


async def _tool_lore_add(args: dict) -> str:
    content = str(args.get("content") or "").strip()
    keys_raw = str(args.get("keys") or "").strip()
    if not content or not keys_raw:
        return "词条至少要有 keys（触发词）和 content（内容）"
    keys = [k.strip() for k in re.split(r"[,，、/|]", keys_raw) if k.strip()]
    scope = _lore_scope()
    if not scope:
        return "现在没有会话，写不了词条"

    def _i(name, default):
        try:
            return int(args.get(name) if args.get(name) not in (None, "") else default)
        except Exception:
            return default

    try:
        from services import lorebook
        eid = lorebook.add_entry(
            scope, content, keys,
            secondary=[k.strip() for k in re.split(r"[,，、/|]", str(args.get("secondary") or "")) if k.strip()],
            comment=str(args.get("comment") or "").strip(),
            constant=(bool(args.get("constant")) if "constant" in args else None),
            order=_i("order", None),
            position=(str(args.get("position") or "").strip() or None),
            sticky=_i("sticky", None),
            cooldown=_i("cooldown", None),
            delay=_i("delay", None),
            probability=_i("probability", None),
            group=str(args.get("group") or "").strip(),
            source="llm",
            confidence=0.7,
        )
    except Exception as e:
        return f"写词条失败：{type(e).__name__}"
    return f"词条已记下（{eid}）：{keys_raw}" if eid else "词条没写进去（内容或触发词为空）"


async def _tool_vision_source(args: dict) -> str:
    """
    查一张图的出处（trace.moe 以图搜番）。

    用户 2026-11-19：群里发图后问「这张能搜到出处吗」，她没有工具可用，
    只能瞎猜。这里给她一个真能查的：不给参数就查「群里最近那张图」。
    """
    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")
    url = str(args.get("image_url") or "").strip()
    mid = str(args.get("message_id") or "").strip()

    # ① 指定消息号 → 从那条消息里抠图片链接
    if not url and mid and bot:
        try:
            data = await bot.call_api("get_msg", message_id=int(mid))
            for seg in (data.get("message") or []):
                if seg.get("type") == "image":
                    url = str((seg.get("data") or {}).get("url") or "")
                    break
        except Exception as e:
            logger.debug(f"[VISION-TOOL] 取消息 {mid} 失败: {type(e).__name__}: {e}")

    # ② 没指定 → 用这个会话最近一张图（vision_agent 识过的那张）
    if not url and ev is not None:
        try:
            from plugins import vision_agent as _va
            url = str(_va.last_image(_get_session_key(ev)).get("url") or "")
        except Exception:
            url = ""

    if not url:
        return ("我这边没有可搜的图：让他把图再发一次，或者引用那张图再问我一句。")

    try:
        import httpx
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url)
        if r.status_code != 200 or len(r.content) < 1024:
            return "图片没下载下来（链接可能过期了），让他重发一次。"
    except Exception as e:
        return f"图片下载失败：{type(e).__name__}"

    try:
        # 2026-11-22：不再裸调 trace.moe，走 PicImageSearch 综合搜
        # （TraceMoe / SauceNAO / Ascii2D / IQDB），单 trace.moe 搜错的概率太高。
        from services import image_search
        results = await image_search.search(r.content)
    except Exception as e:
        return f"搜图失败：{type(e).__name__}: {e}"

    if not results:
        return ("综合搜图（TraceMoe/SauceNAO/Ascii2D/IQDB）都没搜到："
                "可能不是动漫/插画截图，或者画面太糊/太碎。别硬编出处，直接跟他说没搜到。")

    body = image_search.format_results(results, limit=6)
    return ("综合搜图结果（按相似度排序；不同源可能指向同一张图）：\n"
            + body
            + "\n\n用的时候说清楚是哪个源、把握大不大；"
              "相似度低就只当线索（「有点像《X》」），别当事实。")


register_tool(
    {
        "type": "function",
        "function": {
            "name": "vision_source",
            "description": (
                "查一张图的出处（综合以图搜源：动漫截图 → 哪部番第几集；"
                "插画/网图 → Pixiv/Twitter 等原始出处）。"
                "用户问「这张图出自哪」「帮我搜下出处」「这是哪个番」时用。"
                "不填参数 = 查这个会话最近发的那张图；也可以给 message_id 或图片链接。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "那张图的消息号（可选）"},
                    "image_url": {"type": "string", "description": "图片直链（可选）"},
                },
                "required": [],
            },
        },
    },
    _tool_vision_source,
    scope=("private", "group"),
)


# ================= 沉默权 / 安静开关 / 自己收表情 =================
# 用户 2026-11-19：
#   「加入 CHAT_OFF 后她可以读到别人发的消息，但自动强制所有回复里走一个不回复函数。
#     CHAT_ON 是正常状态。她可以根据心情选择某天某时某刻安静下来，
#     等心情好了她也可以自己打开开关。她有沉默安静的权利。」
async def _tool_stay_silent(args: dict) -> str:
    """这一轮不说话。真正的「不回」在 call_llm 里落地（reply 变成 (不想回复)）。"""
    reason = str(args.get("reason") or "").strip()[:60]
    llm_mod.TOOL_CTX["silent"] = True
    llm_mod.TOOL_CTX["silent_reason"] = reason
    logger.info(f"[SILENT] 她选择不说话：{reason or '（没写原因）'}")
    return "好，这次不说了。系统不会发任何内容出去（但你读到的、记下的都还在）。"


register_tool(
    {
        "type": "function",
        "function": {
            "name": "stay_silent",
            "description": (
                "这一轮选择不说话。群里在刷图、别人在互相聊天、你没心情、"
                "看不懂、插不上话 —— 都可以调它，系统不会发任何消息。"
                "这是你的权利，不用勉强接话。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "为什么不想说（给自己看的，20字内）"},
                },
                "required": [],
            },
        },
    },
    _tool_stay_silent,
    scope=("private", "group"),
)


# 群名缓存：`chat_switch(action="list")` 要用群名，别每次都拉一遍
_GROUP_NAME_CACHE: dict = {"at": 0.0, "names": {}}


async def _group_names(bot) -> dict:
    if time.time() - float(_GROUP_NAME_CACHE.get("at") or 0) < 600:
        return _GROUP_NAME_CACHE.get("names") or {}
    names = {}
    try:
        rows = await bot.call_api("get_group_list")
        for r in (rows or []):
            gid = str(r.get("group_id") or "")
            if gid:
                names[gid] = str(r.get("group_name") or "")
    except Exception as e:
        logger.debug(f"[SWITCH] 拉群列表失败: {e}")
    _GROUP_NAME_CACHE.update({"at": time.time(), "names": names})
    return names


async def _resolve_target(target: str, event, bot=None) -> tuple:
    """
    把「这里 / 这个人 / 群 123 / QQ 456 / 主人」解析成 ('群'|'人', id)。

    ⚠️ 2026-11-20 修的真 bug（用户问「安静不是按群或者 QQ 号分吗」时查出来的）：
      以前不在 ALLOWED_PRIVATE 里的普通群友，传 QQ 号会被兜底成 `("群", qq)` ——
      于是「对这个群友安静」变成了「关掉一个不存在的群」，静默失效，
      status 里还会显示「群 123456789」。
    现在顺序：显式前缀 → 这里/这个人 → 白名单 → **当前说话人** → 本群成员 → 措辞 → 群。
    """
    t = str(target or "here").strip()
    low = t.lower()
    here_gid = str(getattr(event, "group_id", "") or "")
    here_uid = str(getattr(event, "user_id", "") or "")

    # ① 显式前缀：人:123 / qq123 / 群:123 / group123
    m = re.match(r"^(?:群|group|g)\s*[:：]?\s*(\d{5,12})$", t, re.I)
    if m:
        return "群", m.group(1)
    m = re.match(r"^(?:人|qq|user|私聊)\s*[:：]?\s*(\d{5,12})$", t, re.I)
    if m:
        return "人", m.group(1)

    # ② 这里 / 这个人
    if low in ("here", "这里", "本群", "当前", "当前群"):
        if here_gid:
            return "群", here_gid
        return "人", here_uid
    if low in ("这个人", "他", "她", "说话的人", "当前说话人", "speaker",
               "this", "对方"):
        return ("人", here_uid) if here_uid else ("", "")
    if low in ("me", "我", "自己"):
        return "人", _self_id(event)
    if low in ("owner", "主人", "master"):
        from services import access
        return ("人", access.OWNER_QQ) if access.OWNER_QQ else ("", "")

    nums = re.findall(r"\d{5,12}", t)
    if not nums:
        return "", ""
    n = nums[0]
    # ③ 白名单：群号 → 群；私聊名单/主人 → 人
    if n in ALLOWED_GROUPS:
        return "群", n
    from services import access as _acc
    if n in ALLOWED_PRIVATE or n == _acc.OWNER_QQ or n in (_acc.ADMIN_QQS or set()):
        return "人", n
    # ④ **当前说话人**：最常见的情况（她不想理刚刚惹她的那个人）
    if here_uid and n == here_uid:
        return "人", n
    # ⑤ 本群成员 → 人（拉一次群成员，有缓存；失败就算了）
    if bot is not None and here_gid:
        try:
            _members = await group_roster.members_of(bot, here_gid)
            if any(str((x or {}).get("user_id") or "") == n for x in (_members or [])):
                return "人", n
        except Exception:
            pass
    # ⑥ 措辞里明显在说「人」
    if any(k in t for k in ("人", "他", "她", "私聊", "qq")):
        return "人", n
    return "群", n


async def _tool_chat_switch(args: dict) -> str:
    action = str(args.get("action") or "list").strip().lower()
    target = str(args.get("target") or "here").strip()
    reason = str(args.get("reason") or "").strip()[:60]
    try:
        minutes = float(args.get("minutes") or 0)
    except Exception:
        minutes = 0.0

    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")

    # ---- list：她能去哪儿、能跟谁私聊、主人是谁、现在谁被静音 ----
    if action in ("list", "ls", "看", "列表"):
        names = await _group_names(bot) if bot else {}
        lines = ["你能出现的地方："]
        for gid in sorted(ALLOWED_GROUPS):
            _n = names.get(gid) or ""
            _off, _why = chat_switch.is_off(f"group_{gid}")
            lines.append(f"· 群 {gid}" + (f"《{_n}》" if _n else "")
                         + ("｜安静中" if _off else ""))
        if ALLOWED_PRIVATE:
            _who = []
            for qq in sorted(ALLOWED_PRIVATE):
                try:
                    _cn = profiles.call_of(qq, "", "") or ""
                except Exception:
                    _cn = ""
                _who.append(f"{_cn}({qq})" if _cn else qq)
            lines.append("能私聊的人：" + "、".join(_who))
        try:
            from services import access
            _owners = sorted(access.ADMIN_QQS or {access.OWNER_QQ})
            _owners = [x for x in _owners if x]
            if _owners:
                lines.append("主人（SUPERUSERS）：" + "、".join(_owners))
        except Exception:
            pass
        _st = chat_switch.status_text()
        lines.append(_st)
        lines.append("→ 想安静：chat_switch(action=\"off\", target=\"群号或QQ\", minutes=30, "
                     "reason=\"心情\")；想说话：action=\"on\"。")
        return "\n".join(lines)

    if not ev:
        return "现在拿不到当前会话（缺少 event），没法切。"

    kind, ident = await _resolve_target(target, ev, bot)
    if not kind:
        return f"没认出「{target}」是群还是人 —— 直接给群号或 QQ 号。"

    if action in ("off", "安静", "mute", "闭嘴"):
        rec = chat_switch.set_off(
            kind, ident, minutes=minutes, reason=reason,
            # 「被人惹到才不想说话」→ 记下是谁，他道歉就能自动破冰
            by_qq=(ident if kind == "人" else ""),
            # 对人安静必须 strict：用户 2026-11-20「可以选择不接话那个人，
            # 以及那个人的艾特和图片」—— 不 strict 的话他一 @ 就破功。
            strict=(kind == "人"))
        _when = ("一直安静，直到你自己开" if not rec.get("until")
                 else f"{int(minutes)} 分钟后自动恢复")
        logger.info(f"[SWITCH] {kind} {ident} → 安静（{reason or '没说原因'}，{_when}）")
        if kind == "人":
            return (f"好，{ident} 这个人我先不理了（{_when}）。"
                    f"他的 @ 和图片我也不接；群里别人照常。想说话随时 action=\"on\"。")
        return (f"好，{kind} {ident} 我先安静下来了（{_when}）。"
                f"消息我照样看得见、也记得住，只是不回。想说话随时 action=\"on\"。")

    if action in ("on", "说话", "unmute", "开"):
        had = chat_switch.set_on(kind, ident)
        logger.info(f"[SWITCH] {kind} {ident} → 恢复（之前{'是' if had else '没'}安静）")
        _extra = ""
        if kind == "群":
            # 群开了、但当前这个人还被单独静音 → 明确告诉她，否则会以为「开了还是不理我」
            try:
                _uid_on = str(getattr(ev, "user_id", "") or "")
                if _uid_on and chat_switch.is_off(f"group_{ident}", _uid_on)[0]:
                    _extra = ("（不过这个人还被单独静音着；要解除就说 "
                              "action=\"on\", target=\"这个人\"）")
            except Exception:
                pass
        return (f"{kind} {ident} 已经恢复正常了。{_extra}" if had
                else f"{kind} {ident} 本来就没关，我现在是正常状态。{_extra}")

    if action in ("status", "状态"):
        return chat_switch.status_text()

    return "action 只能是 list / off / on / status。"


register_tool(
    {
        "type": "function",
        "function": {
            "name": "chat_switch",
            "description": (
                "你的「安静开关」。可以看自己在哪些群/能跟谁聊（含主人），"
                "也可以只屏蔽某个人，或让整个群安静。静音后消息照收照记，但不回。\n"
                "· 不想理某个人（他惹你不开心）→ target=\"这个人\"（或他的名字/QQ）："
                "他的 @ 和图片也不接，群里别人照常。\n"
                "· 群里插不上话 / 心情不好 → target=\"here\"：整个群安静。\n"
                "心情不好/想安静一会儿就 off（可带 minutes 自动恢复），想说话了就 on；"
                "对方道歉/哄你可以 on 回来给台阶。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "off", "on", "status"],
                               "description": "list=看范围/状态；off=静音；on=恢复；status=现在谁被静音"},
                    "target": {"type": "string",
                               "description": ("here=整个当前会话（群里=整群）；"
                                               "这个人=当前说话的人；"
                                               "也可以写名字 / QQ 号 / 群号 / 主人")},
                    "minutes": {"type": "number",
                                "description": "安静多少分钟后自动恢复；0 或不填=一直安静"},
                    "reason": {"type": "string", "description": "为什么想安静（20字内）"},
                },
                "required": ["action"],
            },
        },
    },
    _tool_chat_switch,
    scope=("private", "group"),
)


async def _tool_sticker_save(args: dict) -> str:
    """
    自己决定要不要把这张图收进表情库。

    用户 2026-11-19：识图流程里「是否收为表情包」应该是她决定的，
    不该由 sticker_collect 在她看到之前就自动收掉。
    """
    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")
    mid = str(args.get("message_id") or "").strip()
    if not bot or not ev:
        return "现在拿不到这条消息，收不了。"
    msg = ev.get_message()
    if mid and mid != str(getattr(ev, "message_id", "") or ""):
        try:
            data = await bot.call_api("get_msg", message_id=int(mid))
            msg = Message((data or {}).get("message") or [])
        except Exception as e:
            return f"取消息 {mid} 失败：{type(e).__name__}"
    try:
        n = await try_collect_sticker(bot, ev, msg, force=True)
    except Exception as e:
        return f"收表情失败：{type(e).__name__}: {e}"
    if not n:
        return "这张图里没找到能收的表情（可能不是表情包，或者已经在库里了）。"
    return f"收好了（{n} 张），下次想用就能发出来。"


register_tool(
    {
        "type": "function",
        "function": {
            "name": "sticker_save",
            "description": (
                "把刚看到的这张表情包/图收进自己的表情库。"
                "觉得好笑、想以后用来回话才收；不喜欢就别收。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "那张图的消息号；不填=当前这条"},
                },
                "required": [],
            },
        },
    },
    _tool_sticker_save,
    scope=("private", "group"),
)


# ================= API 余额 / 用量（只挂私聊，且只认主人）=================
async def _tool_api_usage(args: dict) -> str:
    """
    查 API 余额 + 本机今天的用量。

    ⚠️ 只在私聊挂（scope=("private",)）并且只认主人：
       群里任何人问「你还有多少钱」都不该答 —— 余额是主人的账。
    """
    who = str(llm_mod.TOOL_CTX.get("speaker_qq") or "")
    if not is_owner(who):
        return "这个只有主人能看。"
    try:
        days = int((args or {}).get("days") or 1)
    except Exception:
        days = 1
    try:
        from services import api_usage as _au
        return await _au.report_text(days=days)
    except Exception as e:
        return f"查余额失败：{type(e).__name__}: {e}"


register_tool(
    {
        "type": "function",
        "function": {
            "name": "api_usage",
            "description": (
                "查 API 账户余额 + 本机今天/最近几天的调用用量（次数、token、缓存命中率）。"
                "主人问「还有多少钱」「今天用了多少 token」时才用；"
                "查到什么就说什么，别自己折算成钱。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer",
                             "description": "最近几天，默认 1（只要今天）"},
                },
                "required": [],
            },
        },
    },
    _tool_api_usage,
    scope=("private",),
)


async def _tool_hash_decode(args):
    """解 32/40/64 位十六进制哈希（常见短语/4 位数字）。"""
    a = args or {}
    q = str(a.get("hash") or a.get("text") or "").strip()
    try:
        from services import hash_solver
        r = hash_solver.solve(q)
    except Exception as e:
        return f"哈希反查失败：{type(e).__name__}: {e}"
    if not r.get("hash"):
        return "没看出这是哈希：32 位是 MD5，40 位是 SHA1，64 位是 SHA256。把那一串原样给我。"
    if r.get("plain"):
        return (f"这是 {str(r['algo']).upper()}；原文是「{r['plain']}」"
                f"（本地字典比对出来的，不是解密）。")
    return (f"这是 {str(r['algo']).upper()} 哈希（{r['hash'][:12]}…）。"
            "本地字典没查到原文——哈希不可逆，只能靠字典/彩虹表/猜原文；"
            "可以问他原文大概几个字、是不是中文、有没有加盐。")


register_tool(
    {
        "type": "function",
        "function": {
            "name": "hash_decode",
            "description": (
                "解 32/40/64 位十六进制哈希（MD5/SHA1/SHA256）。"
                "群友发一串哈希让你猜原文、或说「这是用 XX 加密的」时调用。"
                "本地字典能解常见短语（如 我爱你）和 4 位数字；查不到就直说不可逆。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hash": {"type": "string",
                             "description": "要解的哈希值（32/40/64 位十六进制）"},
                },
                "required": ["hash"],
            },
        },
    },
    _tool_hash_decode,
    scope=("private", "group"),
)


async def _tool_web_fetch(args):
    """打开一个 URL 并返回正文；也用来提交 GET 表单（参数拼进 URL）。"""
    a = args or {}
    url = str(a.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "给我一个完整的 http/https 链接。"
    try:
        from services import web_page
        r = await web_page.fetch_page(url, timeout=20)
    except Exception as e:
        return f"打开失败：{type(e).__name__}: {e}"
    if r.get("text"):
        return (f"标题：{r.get('title') or '（无）'}\n"
                f"{r['text'][:3000]}")
    return f"打开失败：{r.get('error') or '空页面'}"


register_tool(
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "打开一个 http/https 链接并返回网页正文。"
                "用户让你看网页、或做题/填表后要提交时用："
                "把答案参数拼进页面给的提交 URL，再调这个打开它，"
                "返回的正文里通常有分数/提交结果。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string",
                            "description": "完整 URL（含 ?a=b&c=d 查询参数）"},
                },
                "required": ["url"],
            },
        },
    },
    _tool_web_fetch,
    scope=("private", "group"),
)


register_tool(
    {
        "type": "function",
        "function": {
            "name": "profile_card",
            "description": (
                "查某个人的档案卡片（人/简介/最近十件事/关系/语气/注意 + QQ 头像）。"
                "**只返回图片路径**：拿到后直接 send(image=路径)，不要复述内容、不要先发文字。"
                "「我的档案」「你还记得我吗」→ name 留空（= 当前说话人）；"
                "「XX 的档案」/ 他 @ 了某人 → name 填那个人的名字或 QQ 号（@名字(QQ号) 也行）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "群名片/称呼/QQ号；不填就是当前说话人"},
                },
                "required": [],
            },
        },
    },
    _tool_profile_card,
    scope=("private", "group"),
)

register_tool(
    {
        "type": "function",
        "function": {
            "name": "profile_update",
            "description": (
                "把聊天里确认过的、稳定的人物信息写回档案。只写你确定的事，别猜。"
                "identity=简介（他是谁），personality=性格/说话风格，"
                "scenario=在群里的状态，depth_note=30字内的相处备注/雷区，"
                "mes_example=2~3 句语气样例（他：… / 你：…），"
                "stance=**你在他面前的样子**（撒娇/高冷/可爱/中二/认真…，20字内）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "写谁；不填=当前说话人"},
                    "identity": {"type": "string"},
                    "personality": {"type": "string"},
                    "scenario": {"type": "string"},
                    "depth_note": {"type": "string"},
                    "mes_example": {"type": "string"},
                    "stance": {"type": "string",
                               "description": "你在TA面前的样子（撒娇/高冷/可爱/中二…）"},
                    "talkativeness": {"type": "integer", "description": "0~100 话痨度"},
                },
                "required": [],
            },
        },
    },
    _tool_profile_update,
    scope=("private", "group"),
)

register_tool(
    {
        "type": "function",
        "function": {
            "name": "lore_add",
            "description": (
                "往本群（或本私聊）的设定库里写一条词条：梗、事件、群规、两个人关系都行。"
                "只有聊到 keys 里的词才会注入，平时不占 token。"
                "同一个梗/同一个人不要重复写，关键词相同会自动合并。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "词条内容，60~150 字，写清楚「是什么 + 你怎么应对」"},
                    "keys": {"type": "string", "description": "触发词，逗号分隔，例如：猪猪,抽猪,烤猪"},
                    "secondary": {"type": "string", "description": "副关键词（可选），用于 A+B 同时出现才触发"},
                    "comment": {"type": "string", "description": "短标题，例如：猪猪"},
                    "constant": {"type": "boolean", "description": "true=永远注入（群规/边界用）"},
                    "order": {"type": "integer", "description": "顺序，越大越优先，默认100"},
                    "position": {"type": "string", "enum": ["before", "post"], "description": "before=历史前，post=当前消息前"},
                    "sticky": {"type": "integer", "description": "激活后持续几轮"},
                    "cooldown": {"type": "integer", "description": "几轮内不再触发"},
                    "delay": {"type": "integer", "description": "第几轮之后才允许触发"},
                    "probability": {"type": "integer", "description": "0~100 出现概率"},
                    "group": {"type": "string", "description": "同组词条只取一个（可选）"},
                },
                "required": ["content", "keys"],
            },
        },
    },
    _tool_lore_add,
    scope=("private", "group"),
)


# ================= 三方功能工具（让 LLM 真调，不再只动嘴） =================
# 用户 2026-11-16：「加的三方功能都是 llm 先语言回复而不是调用工具」。
# 之前只把它们写进 TOOL_CATALOG（认知层），LLM 只能介绍/安利。这里把
# 「无参数、立即出结果」的抽成真工具，LLM 调了就发图/发结果。
_GIRL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36"
    )
}


async def _tool_girls_2d(args: dict) -> str:
    """来点美少女 / 来点二次元 —— 随机一张二次元图。"""
    import httpx
    from nonebot.adapters.onebot.v11 import MessageSegment
    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")
    if not bot or not ev:
        return "现在没法发图（缺少会话信息）"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get("https://www.dmoe.cc/random.php", headers=_GIRL_HEADERS)
        if r.status_code != 200 or len(r.content) < 1024:
            return "美少女图接口没取到，稍后再试"
        await bot.send(event=ev, message=MessageSegment.image(r.content))
        return "已经发了一张美少女图"
    except Exception as e:
        return f"发图失败：{type(e).__name__}"


def _is_send_timeout(exc: BaseException) -> bool:
    """判断是不是「图片上传超时」而不是真的发不出去。

    NapCat 上传大图/GIF 常要几十秒，nonebot 默认 API 超时 30s 会先报
    NetworkError(send_msg timeout) 或 ActionFailed(retcode=1200)，但图往往几秒后照样到。
    这两种都按「可能稍后到」处理，别急着跟用户说失败（pixivrank 同款处理）。
    """
    info = getattr(exc, "info", None)
    if isinstance(info, dict) and info.get("retcode") == 1200:
        return True
    msg = str(exc).lower()
    return "timeout" in msg or "retcode=1200" in msg or "send_msg" in msg


def _luck_daily_reset(YS) -> None:
    """每天第一次抽签时清空运势缓存。

    第三方插件（jinriyunshi）只做了 `today_cache[user_id]` 的用户锁，
    **从来没按天清过** —— 一个人抽过一次，之后永远返回同一个结果。
    这里补一个日期标记文件，跨天就把缓存清掉（直接改内存里的同一个 dict，
    插件的 `/今日运势` 命令也会一起受益）。
    """
    import time as _t
    from pathlib import Path
    try:
        marker = Path(YS.CACHE_DIR) / "last_reset.txt"
        today = _t.strftime("%Y-%m-%d")
        last = ""
        try:
            last = marker.read_text(encoding="utf-8").strip()
        except Exception:
            last = ""
        if last != today:
            YS.today_cache.clear()
            YS.save_cache(YS.today_cache)
            marker.write_text(today, encoding="utf-8")
            logger.info(f"[LUCK-TOOL] 运势缓存跨天重置（{last or '空'} → {today}）")
    except Exception as e:
        logger.debug(f"[LUCK-TOOL] 跨天重置失败（不影响抽签）: {type(e).__name__}: {e}")


async def _tool_daily_luck(args: dict) -> str:
    """今日运势 —— 每人每天只抽一次（结果缓存，重复抽返回同一份）。"""
    import random
    import time
    from pathlib import Path
    from nonebot.adapters.onebot.v11 import Message, MessageSegment
    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")
    if not bot or not ev:
        return "现在没法抽签（缺少会话信息）"
    try:
        from plugins.third import jinriyunshi as YS
        YUNSHI_DATA = YS.YUNSHI_DATA
        get_random_pool_image = YS.get_random_pool_image
    except Exception as e:
        return f"运势模块没加载：{type(e).__name__}"
    try:
        _luck_daily_reset(YS)
        # 抽给谁：模型传 qq（「给小明抽」）；不传 = 当前说话人
        target = str(args.get("qq") or llm_mod.TOOL_CTX.get("speaker_qq") or ev.user_id)
        target = "".join(ch for ch in target if ch.isdigit()) or str(ev.user_id)
        if target == str(ev.user_id):
            nickname = getattr(ev.sender, "nickname", "") or f"用户{target[-4:]}"
        else:
            nickname = f"QQ{target[-4:]}"

        cache = YS.today_cache
        # 👇 用户锁：今天抽过就直接给同一份，不再重新随机
        if target in cache:
            data = cache[target] or {}
            info = YUNSHI_DATA[int(data.get("level") or 0)]
            stars = str(data.get("stars") or "")
            text = data.get("text") or info["texts"][int(data.get("text_index") or 0)]
            logger.info(f"[LUCK-TOOL] {target} 今天已经抽过，返回缓存结果")
            return (f"今天已经抽过了（每人每天一次），结果没变：\n"
                    f"{info['title']}｜{stars}\n{text}\n"
                    "直接告诉他今天抽过了、把结果说给他听，别再抽一次。")

        # 抽签：70% 走「四项各 0~2 相加」的钟形，30% 全随机（和插件一致）
        if random.random() < 0.7:
            a, b, c, d = (random.randint(0, 2) for _ in range(4))
            level = a + b + c + d
        else:
            level = random.randint(0, 8)
            while True:
                a, b, c = (random.randint(0, 2) for _ in range(3))
                d = level - (a + b + c)
                if 0 <= d <= 2:
                    break
        text_index = random.randint(0, 2)
        stars = "★" * level + "☆" * (8 - level)
        info = YUNSHI_DATA[level]
        # 写回插件的缓存 → 插件自己的 /今日运势 也会返回同一份（真·用户锁）
        cache[target] = {
            "level": level, "text_index": text_index, "stars": stars,
            "detail": f"财运({a})+姻缘({b})+事业({c})+人品({d})",
        }
        try:
            YS.save_cache(cache)
        except Exception as _se:
            logger.debug(f"[LUCK-TOOL] 缓存落盘失败（不影响本次）: {_se}")
        msg = Message(
            f"@{nickname}，阁下的今日运势是：\n"
            f"{info['title']}\n{stars}\n{info['texts'][text_index]}\n"
            "仅供娱乐｜相信科学｜请勿迷信"
        )
        img_path = get_random_pool_image()
        if img_path:
            # ⚠️ 图池返回的是相对路径（data/jinriyunshi/...），NapCat 的 file://
            #    需要绝对路径，否则 ActionFailed。petpet 工具同样 resolve 过。
            img_path = str(Path(img_path).resolve())
            # 压到最长边 1280：wallhaven 原图常 2~5MB，NapCat 上传超时，压完几百 KB 快很多
            try:
                from PIL import Image as _PILImage
                _im = _PILImage.open(img_path).convert("RGB")
                _w, _h = _im.size
                _max_side = 1280
                if max(_w, _h) > _max_side:
                    _scale = _max_side / max(_w, _h)
                    _im = _im.resize(
                        (int(_w * _scale), int(_h * _scale)),
                        _PILImage.Resampling.LANCZOS,
                    )
                _tmp_dir = Path("data") / "temp" / "luck"
                _tmp_dir.mkdir(parents=True, exist_ok=True)
                _tmp = _tmp_dir / f"luck_{int(time.time() * 1000)}.jpg"
                _im.save(_tmp, "JPEG", quality=85)
                img_path = str(_tmp.resolve())
            except Exception as _ce:
                logger.warning(f"[LUCK-TOOL] 压缩图片失败（用原图）: {type(_ce).__name__}: {_ce}")
            msg += MessageSegment.image(f"file://{img_path}")
        await bot.send(event=ev, message=msg)
        return (f"已经抽了今日运势（每人每天一次）：{info['title']}｜{stars}\n"
                f"{info['texts'][text_index]}")
    except Exception as e:
        # NapCat 传图常超时但图稍后到，按成功处理（pixivrank 同款）
        if _is_send_timeout(e):
            logger.info(f"[LUCK-TOOL] 发送超时（图可能稍后到），按成功处理: {type(e).__name__}")
            return "已经抽了今日运势"
        _info = getattr(e, "info", None)
        logger.warning(f"[LUCK-TOOL] 抽签发送失败: {type(e).__name__}: {e}"
                       + (f" info={_info}" if _info else ""))
        return f"抽签失败：{type(e).__name__}"


register_tool(
    {
        "type": "function",
        "function": {
            "name": "girls_2d",
            "description": (
                "来一张随机的二次元美少女图。用户说「来点美少女」「来点二次元」"
                "「发张图看看」这类想看妹子图时用。"
                "图不会自动发：结果里会给编号，想发就调 send(pending=编号)。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    _tool_girls_2d,
    scope=("private", "group"),
    hold=True,
)

register_tool(
    {
        "type": "function",
        "function": {
            "name": "daily_luck",
            "description": (
                "给用户抽今天的运势签（附一张图）。用户说「测测运势」「今日运势」"
                "「我运气怎么样」时用，直接抽。"
                "抽到后内容不会自动发：结果里会给编号，想发就调 send(pending=编号)。\n"
                "⚠️ **每人每天只能抽一次**：今天抽过的人再抽，会返回他今天已经抽到的结果，"
                "不会重新随机。对方硬要再抽就告诉他「今天抽过啦，明天再来」。\n"
                "注意：「今日小猪」「今日猪猪」「烤群友」是猪猪养成游戏，不是运势/抽签——"
                "抽猪用 rollpig 工具，别用这个。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "qq": {
                        "type": "string",
                        "description": (
                            "抽给谁（对方的 QQ 号）。省略 = 当前说话人。"
                            "只有对方明确说「给某某抽」时才填。"
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    _tool_daily_luck,
    scope=("private", "group"),
    hold=True,
)


class _RollpigToolMatcher:
    """假 matcher：让猪猪插件里靠 matcher.finish/send 的 handler 能被 function 工具直接复用。

    finish 抛 FinishedException（nonebot 控制流），tool 层捕获后正常返回；
    send 转 bot.send。
    """

    def __init__(self, bot, ev):
        self._bot = bot
        self._ev = ev

    async def send(self, msg):
        try:
            await self._bot.send(event=self._ev, message=msg)
        except Exception as e:
            logger.warning(f"[ROLLPIG-TOOL] 假 matcher.send 失败: {type(e).__name__}: {e}")

    async def finish(self, msg=None):
        if msg:
            await self.send(msg)
        from nonebot.exception import FinishedException
        raise FinishedException

    def stop_propagation(self):
        # 插件 handler 里会调；假 matcher 没有事件传播，空实现即可
        return None


# ---- action 归一化 ----
# 模型不一定老实按 enum 填：可能写中文（「明日小猪」「我的猪圈」），
# 也可能干脆不填（默认 today）。一个参数写错就回「没这个猪猪操作」太蠢了。
_ROLLPIG_ACTIONS = ("today", "yesterday", "tomorrow", "random", "find",
                    "sty", "week", "catalog", "roast_self", "roast_member")
_ROLLPIG_ALIASES = (
    ("烤群友", "roast_member"), ("烤人", "roast_member"), ("烤ta", "roast_member"),
    ("烤自己", "roast_self"), ("今日烤猪", "roast_self"), ("烤猪", "roast_self"),
    ("昨日", "yesterday"), ("昨天", "yesterday"), ("回顾", "yesterday"),
    ("明日", "tomorrow"), ("明天", "tomorrow"), ("预测", "tomorrow"),
    ("图鉴", "catalog"),
    ("本周", "week"), ("这周", "week"),
    ("猪圈", "sty"), ("收集", "sty"),
    ("找猪", "find"), ("找", "find"), ("搜索", "find"),
    ("随机", "random"),
    ("今日", "today"), ("今天", "today"), ("抽猪", "today"),
)


def _norm_action(raw) -> str:
    a = str(raw or "").strip().lower()
    if not a:
        return "today"
    if a in _ROLLPIG_ACTIONS:
        return a
    for kw, act in _ROLLPIG_ALIASES:
        if kw in a:
            return act
    return a


async def _tool_rollpig(args: dict) -> str:
    """猪猪功能包：按 action 分发（today/yesterday/tomorrow/random/find/sty/week/catalog
    /roast_self/roast_member）。

    2026-11-16：用户要求把猪猪当功能包，LLM 直接拿 event.user_id 调，不逼群友发 /指令。
    """
    import random
    import datetime
    from nonebot.adapters.onebot.v11 import Message, MessageSegment
    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")
    if not bot or not ev:
        return "现在没法玩猪猪（缺会话信息）"

    action = _norm_action(args.get("action"))
    keyword = str(args.get("keyword") or "").strip()
    try:
        count = max(1, min(int(args.get("count") or 1), 10))
    except Exception:
        count = 1
    user_id = str(ev.user_id)

    async def _send(msg) -> bool:
        try:
            await bot.send(event=ev, message=msg)
            return True
        except Exception as e:
            if _is_send_timeout(e):
                logger.info(f"[ROLLPIG-TOOL] 发送超时（图可能稍后到）: {type(e).__name__}")
                return True
            logger.warning(f"[ROLLPIG-TOOL] 发送失败: {type(e).__name__}: {e}")
            return False

    try:
        from plugins.third.nonebot_plugin_rollpig_plus.store import store
        from plugins.third.nonebot_plugin_rollpig_plus.helpers import (
            get_event_group_id,
            get_event_user_name,
            send_rendered_pig,
        )
    except Exception as e:
        return f"猪猪模块没加载：{type(e).__name__}"
    group_id = get_event_group_id(ev)
    nickname = getattr(ev.sender, "card", "") or getattr(ev.sender, "nickname", "") or f"用户{user_id[-4:]}"

    try:
        # ---- 今日小猪 ----
        if action == "today":
            from plugins.third.nonebot_plugin_rollpig_plus.roll_flow import (
                RECORDED_PIG_RESOURCE_MISSING_TEXT,
                resolve_daily_pig,
            )
            from plugins.third.nonebot_plugin_rollpig_plus.reservation_flow import (
                deliver_newly_ready_reservations,
            )
            resolution = await resolve_daily_pig(user_id, group_id, include_progress=True)
            if resolution.recorded_pig_missing:
                return RECORDED_PIG_RESOURCE_MISSING_TEXT
            if resolution.missing_resources or not resolution.pig:
                return "猪圈塌房了（数据缺失）"
            await send_rendered_pig(
                None, ev, resolution.pig,
                extra_text=resolution.growth_text,
                ex_level=resolution.ex_level or 0,
                # ⚠️ 预约结算放到独立任务：hold 只拦「当前工具任务」的发送，
                #    否则预约通知会被一起扣成待发，她只发猪卡就丢了。
                after_send=(
                    (lambda: asyncio.create_task(
                        deliver_newly_ready_reservations(str(ev.self_id))))
                    if resolution.was_auto_created else None
                ),
                bot=bot,
            )
            return "已经抽了今天的小猪"

        # ---- 明日预测 ----
        if action == "tomorrow":
            from plugins.third.nonebot_plugin_rollpig_plus.texts import TOMORROW_TEXTS
            await _send(MessageSegment.text(random.choice(TOMORROW_TEXTS)))
            return "已经发了明日预测"

        # ---- 昨日回顾 ----
        if action == "yesterday":
            from plugins.third.nonebot_plugin_rollpig_plus.roll_flow import ensure_yesterday_pig
            from plugins.third.nonebot_plugin_rollpig_plus.yesterday_recap import (
                YesterdayPigResourceMissingError,
                build_yesterday_recap,
            )
            from plugins.third.nonebot_plugin_rollpig_plus.yesterday_card_renderer import (
                render_yesterday_recap_card,
            )
            from plugins.third.nonebot_plugin_rollpig_plus.runtime import rollpig_date_str
            from plugins.third.nonebot_plugin_rollpig_plus.texts import YESTERDAY_MAKEUP_TEXTS
            from plugins.third.nonebot_plugin_rollpig_plus.resource_manager import PIG_LIST
            from plugins.third.nonebot_plugin_rollpig_plus.store.cloud import (
                CloudMakeupUnsupportedError,
            )
            makeup_created = False
            try:
                target_date = rollpig_date_str(-1)
                recap = await build_yesterday_recap(user_id, group_id=group_id, date_str=target_date)
                if recap is None:
                    if not PIG_LIST:
                        return "小猪资源暂时不可用，无法补签。"
                    makeup_created = await ensure_yesterday_pig(user_id, target_date)
                    recap = await build_yesterday_recap(user_id, group_id=group_id, date_str=target_date)
            except CloudMakeupUnsupportedError as error:
                return str(error)
            except YesterdayPigResourceMissingError as error:
                return "昨天那只猪暂时不在当前资源包里。"
            if recap is None:
                return "补签结果暂时无法读取，请稍后再试。"
            render_result = await render_yesterday_recap_card(recap)
            msg = Message()
            if makeup_created:
                msg += MessageSegment.text(random.choice(YESTERDAY_MAKEUP_TEXTS))
            msg += MessageSegment.image(render_result.data)
            if await _send(msg):
                return "已经发了昨日回顾"
            return "昨日回顾没发出去"

        # ---- 我的猪圈 ----
        if action == "sty":
            from plugins.third.nonebot_plugin_rollpig_plus.roll_flow import build_pigsty_growth_summary
            from plugins.third.nonebot_plugin_rollpig_plus.resource_manager import PIG_LIST
            draw_state = await store.get_draw_state(user_id)
            total_pigs = len(PIG_LIST)
            if total_pigs <= 0:
                return "猪图鉴为空，请先检查资源文件。"
            if not draw_state.pig_ids:
                return "你的猪圈空空如也！"
            text = build_pigsty_growth_summary(nickname, draw_state, total_pigs)
            if await _send(MessageSegment.text(text)):
                return "已经发了猪圈总结"
            return "猪圈总结没发出去"

        # ---- 本周小猪 ----
        if action == "week":
            from plugins.third.nonebot_plugin_rollpig_plus.card_renderer import render_weekly_pig_image
            from plugins.third.nonebot_plugin_rollpig_plus.resource_manager import (
                find_image_file,
                get_pig_by_id,
            )
            from plugins.third.nonebot_plugin_rollpig_plus.runtime import rollpig_today
            today = rollpig_today()
            images_to_merge = []
            for i in range(7):
                d = today - datetime.timedelta(days=(6 - i))
                pig = get_pig_by_id(await store.get_pig_by_date(user_id, d.isoformat()))
                if pig:
                    img_file = find_image_file(pig["id"])
                    if img_file:
                        images_to_merge.append(img_file)
            if not images_to_merge:
                return "你这周还没抽过猪呢！"
            image_data = await render_weekly_pig_image(images_to_merge)
            msg = (MessageSegment.text(f"你这周变了 {len(images_to_merge)} 次猪！")
                   + MessageSegment.image(image_data))
            if await _send(msg):
                return "已经发了本周总结"
            return "本周总结没发出去"

        # ---- 小猪图鉴 ----
        if action == "catalog":
            from plugins.third.nonebot_plugin_rollpig_plus.config import plugin_config
            from plugins.third.nonebot_plugin_rollpig_plus.catalog_renderer import render_catalog_image
            from plugins.third.nonebot_plugin_rollpig_plus.resource_manager import PIG_LIST
            if not plugin_config.rollpig_catalog_enabled:
                return "图片版小猪图鉴当前未启用。"
            page = 1
            if keyword:
                # 模型常写「第2页」「2 页」，只抠数字，别因为多了个字就报错
                _pm = re.search(r"\d+", keyword)
                if not _pm:
                    return "页码需要是数字。"
                page = max(1, int(_pm.group(0)))
            if not PIG_LIST:
                return "猪图鉴为空，请先检查资源文件。"
            snapshot = await store.get_catalog_snapshot(user_id, days=14)
            if not snapshot.draw_state.pig_ids:
                return "你的猪圈空空如也！发送「今日小猪」开始收集。"
            pic = await render_catalog_image(
                user_name=get_event_user_name(ev), snapshot=snapshot, page=page,
            )
            if await _send(MessageSegment.image(pic)):
                return "已经发了小猪图鉴"
            return "图鉴没发出去"

        # ---- 随机小猪 ----
        if action == "random":
            from plugins.third.nonebot_plugin_rollpig_plus.pighub_service import (
                build_pighub_image_url,
                pighub_service,
            )
            if not await pighub_service.ensure_ready():
                return "连不上 PigHub，请稍后再试。"
            selected = pighub_service.sample(count)
            if not selected:
                return "PigHub 图片索引为空，请稍后再试。"
            pig = selected[0]
            image_url = build_pighub_image_url(pig)
            if not image_url:
                return "PigHub 返回了异常图片数据。"
            msg = MessageSegment.image(image_url)
            if count > 1:
                msg += MessageSegment.text(f"\n（随机小猪一次先发 1 张）")
            if await _send(msg):
                return "已经发了随机小猪"
            return "随机小猪没发出去"

        # ---- 找猪 ----
        if action == "find":
            from plugins.third.nonebot_plugin_rollpig_plus.pighub_service import (
                build_pighub_image_url,
                pighub_service,
            )
            if not keyword:
                return "没说找什么猪，问清楚关键词。"
            if not await pighub_service.ensure_ready():
                return "连不上 PigHub，请稍后再试。"
            found_pigs = pighub_service.search(keyword)
            if not found_pigs:
                return f"没找到叫「{keyword}」的猪。"
            pig = found_pigs[0]
            image_url = build_pighub_image_url(pig)
            if not image_url:
                return "搜索结果数据异常。"
            msg = MessageSegment.image(image_url)
            if len(found_pigs) > 1:
                msg += MessageSegment.text(f"\n共找到 {len(found_pigs)} 张，先发第 1 张。")
            if await _send(msg):
                return "已经发了找猪结果"
            return "找猪结果没发出去"

        # ---- 今日烤猪（烤自己）----
        if action == "roast_self":
            from plugins.third.nonebot_plugin_rollpig_plus.roll_flow import (
                RECORDED_PIG_RESOURCE_MISSING_TEXT,
                resolve_daily_pig,
            )
            from plugins.third.nonebot_plugin_rollpig_plus.roast_flow import (
                RoastFoodMissingError,
                build_self_roast_data,
                pick_self_roast_block_text,
            )
            from plugins.third.nonebot_plugin_rollpig_plus.reservation_flow import (
                deliver_newly_ready_reservations,
            )
            from plugins.third.nonebot_plugin_rollpig_plus.store.models import RoastEvent
            from plugins.third.nonebot_plugin_rollpig_plus.texts import AUTO_ROLL_ROAST_TEXTS
            resolution = await resolve_daily_pig(user_id, group_id)
            original_pig = resolution.pig
            if resolution.recorded_pig_missing:
                return RECORDED_PIG_RESOURCE_MISSING_TEXT
            if resolution.missing_resources or not original_pig:
                return "猪圈埋房了（数据缺失）"
            auto_roll_hint = ""
            if resolution.was_auto_created:
                auto_roll_hint = random.choice(AUTO_ROLL_ROAST_TEXTS).format(name=original_pig["name"]) + "\n"
            block_text = pick_self_roast_block_text(original_pig)
            if block_text:
                await _send(MessageSegment.text(block_text))
                return "已经处理了今日烤猪"
            try:
                roasted_pig_data, food_name = await build_self_roast_data(original_pig)
            except RoastFoodMissingError as e:
                return str(e)
            if group_id:
                await store.append_roast_event(RoastEvent(
                    event_type="self_roast",
                    attacker_id=user_id, target_id=user_id,
                    attacker_name=nickname, target_name=nickname,
                    food=food_name, group_id=group_id,
                ))
            await send_rendered_pig(
                None, ev, roasted_pig_data,
                extra_text=auto_roll_hint,
                cache_final_card=False,
                # 同上：预约结算用独立任务，别被 hold 扣住
                after_send=(
                    (lambda: asyncio.create_task(
                        deliver_newly_ready_reservations(str(ev.self_id))))
                    if resolution.was_auto_created else None
                ),
                bot=bot,
            )
            return "已经烤了今天的自己"

        # ---- 烤群友（@ 目标）----
        if action == "roast_member":
            from nonebot.exception import FinishedException
            from plugins.third.nonebot_plugin_rollpig_plus.handlers.roast import _handle_member_roast
            if not isinstance(ev, GroupMessageEvent):
                return "烤群友只能在群里用。"
            try:
                await _handle_member_roast(_RollpigToolMatcher(bot, ev), bot, ev)
            except FinishedException:
                pass
            return "已经处理了烤群友"

        return f"没这个猪猪操作：{action}"
    except Exception as e:
        logger.warning(f"[ROLLPIG-TOOL] 猪猪操作失败 action={action}: {type(e).__name__}: {e}")
        return f"猪猪操作失败：{type(e).__name__}"


class _CaptureBot:
    """只收不发（自检用）：把工具想发的内容记下来，不真发、不刷屏。"""

    def __init__(self, self_id=""):
        self.self_id = self_id
        self.calls = []

    async def send(self, event=None, message=None, **kw):
        try:
            self.calls.append(llm_mod._msg_brief(message))
        except Exception:
            self.calls.append("[消息]")
        return None

    async def call_api(self, *a, **kw):
        self.calls.append(f"[API {a[0] if a else '?'}]")
        return None


_SCENE_SAMPLE = [
    {"qq": "10001", "who": "阿明", "text": "今天又加班，胃有点疼", "kind": "user"},
    {"qq": "10002", "who": "小柚子", "text": "你吃药了吗", "kind": "user"},
    {"qq": "10003", "who": "老王", "text": "抽卡又歪了", "kind": "user"},
    {"qq": "10004", "who": "阿强", "text": "保底都歪，麻了", "kind": "user"},
    {"qq": "10002", "who": "小柚子", "text": "小鲸鱼 你说他是不是活该", "kind": "user"},
]


def _scene_prepare_replay(gid: str, *, sample: bool = False,
                          limit: int = 12, bot_qq: str = "") -> tuple:
    """
    造一段「现场」用来测审核（用户 2026-11-19：重启后没人说话，现场只剩线程 0）。

    数据来源：今天对话日志里这个群的真实消息（最近 limit 条）；没有就用内置样例。
    灌进独立 scope `<gid>#replay`，**不碰真实现场**，测完不影响她聊天。

    返回 (scope, names, 说明)；灌不进去时 scope 为空串。
    """
    gid = str(gid or "").strip()
    if not gid:
        return "", {}, ""
    rows = []
    if not sample:
        try:
            # 回放按条数取（不是「今天的」）：隔天想回放昨晚的现场也应该能回放
            for rec in dialogue_log.load_recent(gid, limit=48):
                if not str(rec.get("text") or "").strip():
                    continue
                # 日志里的 who 是「阿明(1135)」这种带 QQ 尾巴的标签，回放时去掉，
                # 否则卡片上名字很脏、审核 LLM 也照着抄
                _nm = re.sub(r"[（(]\d{2,}[)）]\s*$", "",
                             str(rec.get("who") or "")).strip()
                rows.append(dict(rec, who=_nm or rec.get("who") or ""))
        except Exception:
            rows = []
        rows = rows[-limit:]
    tag = f"日志回放 {len(rows)} 条" if rows else ""
    if not rows:
        rows = [dict(r) for r in _SCENE_SAMPLE]
        tag = f"内置样例 {len(rows)} 条"
    names = dict(_scene_names(gid) or {})
    for r in rows:                     # 样例/回放的 QQ 也要进名字表，审核结果才映射得回来
        nm = str(r.get("who") or "").strip()
        qq = str(r.get("qq") or "").strip()
        if nm and qq and nm not in names:
            names[nm] = qq
    scope = f"{gid}#replay"
    n = group_scene.replay(scope, rows, bot_qq=str(bot_qq or ""), names=names)
    return (scope if n else ""), names, tag


def _scene_selfcheck(event, gid: str = "") -> str:
    """打印当前群聊现场：几条线程、谁→谁、谁在叫她。"""
    gid = str(gid or getattr(event, "group_id", "") or "")
    if not gid:
        return "🎬 群聊现场\n· 这个只能在群里看（私聊没有多线程）。"
    self_id = _self_id(event)
    uid = str(getattr(event, "user_id", "") or "")
    snap = group_scene.snapshot(
        gid, bot_qq=self_id, my_names=_my_names(f"group_{gid.split('#')[0]}", uid))
    addr = group_scene.addressee_line(gid, self_id) or "（还没有消息）"
    body = group_scene.format_block(snap) or "（还没有线程）"
    debug = group_scene.format_debug(gid, limit=12)
    audit_status = scene_auditor.status(gid=gid) + "\n" + attention.status()
    return (
        "🎬 群聊现场\n" + audit_status + "\n\n" + addr + "\n\n" + body
        + f"\n\n线程数：{len(snap.get('threads') or [])}"
        + f"｜在叫你：{len(snap.get('to_bot') or [])}"
        + f"｜最后是她：{'是' if snap.get('last_is_bot') else '否'}"
        + "\n\n" + debug
    )


async def _archive_selfcheck(bot, event, qq: str = "", gid: str = "") -> str:
    """
    人物卡 v2 / 本群称呼 / 词条触发 / 档案图 一条命令全测。

    qq / gid 可以被 /测函数 档案 <QQ> [群号] 覆盖 —— 私聊里也能查某个群的某个人。
    """
    lines = ["🗂 档案与词条自检"]
    qq = str(qq or getattr(event, "user_id", "") or "")
    gid = str(gid or getattr(event, "group_id", "") or "")
    if not qq:
        return "🗂 档案与词条自检\n· 拿不到你的 QQ"

    p = profiles.get_profile(qq)
    need = ("identity", "personality", "scenario", "mes_example",
            "depth_note", "talkativeness", "muted", "groups")
    miss = [k for k in need if k not in p]
    lines.append("· v2 字段：" + ("齐" if not miss else "缺 " + "、".join(miss)))
    lines.append(f"· 称呼：全局「{profiles.call_of(qq, '') or '未设'}」"
                 + (f"｜本群「{profiles.call_of(qq, '', gid)}」" if gid else ""))

    try:
        micro = profiles.render_card(qq, gid, detail="micro", max_len=70)
        brief = profiles.render_card(qq, gid, detail="brief", max_len=260)
        full = profiles.render_card(qq, gid, detail="full")
        lines.append(f"· 人物卡：micro {len(micro)} 字 / brief {len(brief)} 字 / full {len(full)} 字")
        lines.append("  " + (micro or "（空：档案还没内容）"))
    except Exception as e:
        lines.append(f"· 人物卡：渲染异常 {type(e).__name__}: {e}")

    # 词条：用独立 selftest_ 命名空间，不污染真实群词条
    try:
        from services import lorebook
        scope = f"selftest_{gid or qq}"
        eid = lorebook.add_entry(
            scope, "自检词条：抽猪小游戏，配合玩，别装懂。",
            ["自检猪猪"], comment="自检", sticky=2,
        )
        r1 = lorebook.scan(scope, "今天自检猪猪了吗", 1)
        r2 = lorebook.scan(scope, "完全无关", 2)
        r3 = lorebook.scan(scope, "完全无关", 99)
        lines.append(
            "· 词条：命中" + ("✅" if r1["before"] else "❌")
            + "｜sticky" + ("✅" if r2["before"] else "❌")
            + "｜过期" + ("✅" if not r3["before"] else "❌")
        )
        lorebook.delete_entry(scope, eid)
    except Exception as e:
        lines.append(f"· 词条：异常 {type(e).__name__}: {e}")

    # 档案图（含头像：拿不到要说清为什么，别只显示「没头像」）
    try:
        from services import profile_card
        try:
            _av_why = await _ensure_avatar(bot, qq, gid)
            lines.append(f"· 头像来源：{_av_why}")
            lines.append("· " + await asyncio.to_thread(profile_card.avatar_status, qq))
        except Exception as e:
            lines.append(f"· 头像：检查失败 {type(e).__name__}: {e}")
        img = await asyncio.to_thread(profile_card.render_profile_image, qq, gid)
        if img:
            lines.append("· 档案图：已生成")
            # ⚠️ 发图失败要重试，**不要**拿这段自检文字顶替图（用户 2026-11-19）
            if await _send_image_patiently(bot, event, img):
                logger.info("[ARCHIVE] 自检：\n" + "\n".join(lines))
                return ""                     # 图发成了就不再补文字
            lines.append("· 档案图没发出去（NapCat 上传超时，等了几轮重试都不成）")
            lines.append(f"  · 图已生成：{img}（再发一次 /测函数 档案 即可）")
        else:
            lines.append("· 档案图：渲染失败（看日志 [PROFILE-CARD]）")
    except Exception as e:
        lines.append(f"· 档案图：异常 {type(e).__name__}: {e}")

    lines.append("· 真机验证：/档案 看完整档案图；/本群叫他 @人 名字 改本群称呼；/词条 生成 把档案灌成词条")
    return "\n".join(lines)


async def _genshin_selfcheck() -> str:
    """原神面板插件（plugins/third/gspanel）自检：依赖 / 资源 / API。"""
    lines = ["🎮 原神面板自检"]
    try:
        from plugins.third.gspanel.data_source import _HTMLRENDER_ERR
        from plugins.third.gspanel.__utils__ import CHAR_DATA, LOCAL_DIR
    except Exception as e:
        return f"🎮 原神面板自检\n· 插件加载失败：{type(e).__name__}: {e}"
    lines.append(
        "· 出图依赖 htmlrender："
        + ("没装（只能文字版）" if _HTMLRENDER_ERR else "已装")
    )
    try:
        import playwright  # noqa: F401
        _pp = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        _kernels = ""
        if _pp:
            try:
                _kernels = "、".join(sorted(os.listdir(_pp))[:3])
            except Exception:
                _kernels = "目录不存在"
        lines.append(f"· playwright：已装（内核：{_kernels or '默认目录'}）")
    except Exception as e:
        lines.append(f"· playwright：没装（{type(e).__name__}）")
    lines.append(f"· 角色数据：{len(CHAR_DATA)} 个")
    try:
        tpl = sorted(p.name for p in LOCAL_DIR.glob("*.html"))
        lines.append(
            f"· 模板：{len(tpl)} 个"
            + (f"（{'、'.join(tpl[:3])}…）" if tpl else "（没有，出不了图）")
        )
    except Exception:
        pass
    try:
        from plugins.third.gspanel.data_source import queryPanelApi
        d = await asyncio.wait_for(queryPanelApi("100000001"), timeout=20)
        if isinstance(d, dict) and d.get("error"):
            lines.append(f"· Enka API：通（测试 UID 返回：{str(d['error'])[:40]}）")
        else:
            lines.append(f"· Enka API：通（返回 {len(str(d))} 字符）")
    except Exception as e:
        lines.append(f"· Enka API：异常 {type(e).__name__}: {str(e)[:40]}")
    return "\n".join(lines)


async def _rollpig_selfcheck(event, full: bool = False) -> str:
    """逐个跑猪猪 action（capture bot，不刷屏），返回体检报告。"""
    import time as _time
    lines = ["🐷 猪猪自检"]
    try:
        from plugins.third.nonebot_plugin_rollpig_plus.config import plugin_config
        from plugins.third.nonebot_plugin_rollpig_plus.resource_manager import PIG_LIST
        from plugins.third.nonebot_plugin_rollpig_plus.store import store
    except Exception as e:
        return f"🐷 猪猪自检\n· 模块加载失败：{type(e).__name__}: {e}"
    lines.append(
        f"· 资源 {len(PIG_LIST)} 只｜存储 {plugin_config.rollpig_storage_backend}"
        f"｜图鉴 {'开' if plugin_config.rollpig_catalog_enabled else '关'}"
    )
    try:
        _st = await store.get_draw_state(str(event.user_id))
        lines.append(f"· 档案：已收集 {len(_st.pig_ids)} 只")
    except Exception as e:
        lines.append(f"· 档案读取失败：{type(e).__name__}")
    try:
        from plugins.third.nonebot_plugin_rollpig_plus.pighub_service import pighub_service
        _ok = await asyncio.wait_for(pighub_service.ensure_ready(), timeout=20)
        lines.append(f"· PigHub：{'可用' if _ok else '连不上'}"
                     f"（索引 {len(pighub_service.images)}）")
    except Exception as e:
        lines.append(f"· PigHub：异常 {type(e).__name__}")

    # 默认只跑不改数据的；「全」才跑 yesterday / roast_self
    acts = ["today", "tomorrow", "sty", "week", "catalog", "random", "find"]
    if full:
        acts += ["yesterday", "roast_self"]

    _old = llm_mod.TOOL_CTX.copy()
    _cap = _CaptureBot(getattr(event, "self_id", ""))
    llm_mod.set_tool_context(
        bot=_cap, event=event,
        speaker_qq=str(getattr(event, "user_id", "")),
        speaker_name=display_name(event),
    )
    try:
        for _act in acts:
            _args = {"action": _act}
            if _act == "find":
                _args["keyword"] = "猪"
            _t0 = _time.time()
            try:
                _res = await asyncio.wait_for(_tool_rollpig(_args), timeout=60)
                _cost = _time.time() - _t0
                _sent = " / ".join(x for x in _cap.calls if x)[:50]
                _cap.calls.clear()
                lines.append(
                    f"· {_act}：{str(_res)[:60]}"
                    f"（{_cost:.1f}s｜{'要发：' + _sent if _sent else '无内容'}）"
                )
            except asyncio.TimeoutError:
                lines.append(f"· {_act}：超时（>60s）")
            except Exception as e:
                lines.append(f"· {_act}：失败 {type(e).__name__}: {str(e)[:60]}")
    finally:
        llm_mod.set_tool_context(**_old)
    if not full:
        lines.append("· （yesterday / roast_self 会写数据，要看加「全」）")
    return "\n".join(lines)


register_tool(
    {
        "type": "function",
        "function": {
            "name": "rollpig",
            "description": (
                "猪猪养成游戏功能包。用户说「今日小猪/今日猪猪/抽猪」「昨天我是什么猪」"
                "「明日小猪」「随机来只猪」「找只XX猪」「我的猪圈」「本周小猪」「小猪图鉴」"
                "「烤自己/今日烤猪」「烤群友/烤 XX」时用。"
                "action 选：today/yesterday/tomorrow/random/find/sty/week/catalog/roast_self/roast_member。"
                "拿不准英文就填中文说法（「明日小猪」「我的猪圈」「图鉴」），系统会自动识别。"
                "find 要 keyword；random 可选 count；catalog 可选 page（放在 keyword 里）。"
                "roast_member 是烤群友（消息里 @ 谁就烤谁，只能群聊）。目标默认说话人自己。"
                "抽到的卡片不会自动发：结果里会给编号，想发就调 send(pending=编号)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["today", "yesterday", "tomorrow", "random", "find", "sty", "week", "catalog", "roast_self", "roast_member"],
                        "description": "猪猪操作类型",
                    },
                    "keyword": {"type": "string", "description": "找猪关键词（find 时必填），或图鉴页码（catalog 时）"},
                    "count": {"type": "integer", "description": "随机小猪数量（random 时可选，默认1）"},
                },
                "required": ["action"],
            },
        },
    },
    _tool_rollpig,
    scope=("private", "group"),
    hold=True,
)


async def _tool_steam_judge(args: dict) -> str:
    """Steam 毒舌锐评 —— 解析主页链接 → 入队，后台 worker 生成分析后发回来。"""
    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")
    if not bot or not ev:
        return "现在没法做 Steam 分析（缺会话信息）"
    link = str(args.get("link") or "").strip()
    # 参数没给链接时，从原始消息里挖一个 steamcommunity 链接兜底
    if not link:
        try:
            import re as _re
            _raw = ev.get_plaintext()
            _m = _re.search(r"https?://(?:www\.)?steamcommunity\.com/[^\s]+", _raw)
            if _m:
                link = _m.group(0)
        except Exception:
            pass
    if not link:
        return "没说 Steam 个人主页链接，问清楚再分析。"
    try:
        from plugins.third.steam_judger import (
            MAX_QUEUE_SIZE,
            STEAM_LINK_TIP,
            _parse_profile_link,
            analysis_queue,
            processing_or_queued_ids,
            resolve_vanity_url,
        )
    except Exception as e:
        return f"Steam 分析模块没加载：{type(e).__name__}"
    try:
        kind, value = _parse_profile_link(link)
    except ValueError as e:
        return f"链接不对：{e}。{STEAM_LINK_TIP}"
    try:
        steam_id = value if kind == "profiles" else await resolve_vanity_url(value)
    except Exception as e:
        logger.warning(f"[STEAM-TOOL] 链接解析失败: {type(e).__name__}: {e}")
        return "链接解析失败，请换成 /profiles/ 形式的主页链接再试。"
    if steam_id in processing_or_queued_ids:
        return "这个 Steam ID 的分析已经在队列里了，稍等出结果。"
    if analysis_queue.full():
        return f"分析队列满了（{MAX_QUEUE_SIZE}/{MAX_QUEUE_SIZE}），稍后再试。"
    try:
        processing_or_queued_ids.add(steam_id)
        await analysis_queue.put((bot, ev, steam_id))
        return f"已提交 Steam 分析（队列第 {analysis_queue.qsize()} 个），几分钟后把锐评发回来"
    except Exception as e:
        processing_or_queued_ids.discard(steam_id)
        logger.warning(f"[STEAM-TOOL] 入队失败: {type(e).__name__}: {e}")
        return f"提交失败：{type(e).__name__}"


register_tool(
    {
        "type": "function",
        "function": {
            "name": "steam_judge",
            "description": (
                "分析用户的 Steam 游戏库并生成毒舌锐评（会先回「在分析了」，几分钟后发结果卡片）。"
                "用户发「steam分析/steam锐评 + Steam个人主页链接」时用。"
                "从用户消息里提取 steamcommunity.com/id/xxx 或 /profiles/xxx 形式的链接填到 link 参数。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "link": {"type": "string", "description": "Steam 个人主页链接（steamcommunity.com/id/xxx 或 /profiles/xxx）"},
                },
                "required": ["link"],
            },
        },
    },
    _tool_steam_judge,
    scope=("private", "group"),
)


async def _tool_petpet(args: dict) -> str:
    """头像表情包：支持高频模板（摸/亲/贴/撕/丢/拍/打拳/舔/追火车/锤/砸/吸/顶）。

    目标 @ 某人 / target 传 qq 或「自己」，省略 = 摸说话人自己。
    """
    import time as _time
    from pathlib import Path as _P
    from nonebot.adapters.onebot.v11 import MessageSegment
    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")
    if not bot or not ev:
        return "现在没法做表情（缺会话信息）"

    action = str(args.get("action") or "摸").strip()

    # ---- 解析目标：@ 的人 > 参数里的 qq/自己 > 说话人自己 ----
    target_qq = ""
    try:
        for seg in ev.get_message():
            if seg.type == "at":
                target_qq = str(seg.data.get("qq", ""))
                break
    except Exception:
        pass
    _t = str(args.get("target") or "").strip()
    if _t and _t.isdigit():
        target_qq = _t
    elif _t in ("自己", "我"):
        target_qq = str(ev.user_id)
    if not target_qq:
        target_qq = str(ev.user_id)

    try:
        from plugins.third.petpet.utils import UserInfo
        from plugins.third.petpet.depends import get_user_info, download_image
        from plugins.third.petpet import functions as _pf
        _gid = str(getattr(ev, "group_id", "") or "")
        user = UserInfo(qq=target_qq, group=_gid)
        await get_user_info(bot, user)
        await download_image(user)
        target_img = user.img

        # action → (meme name, 调用方式)
        _actions = {
            "摸": ("petpet", "img_arg"),
            "亲": ("kiss", "sender"),
            "贴": ("rub", "sender"),
            "撕": ("rip", "img_list"),
            "丢": ("throw", "img"),
            "拍": ("pat", "img"),
            "打拳": ("punch", "img"),
            "舔": ("prpr", "img"),
            "追火车": ("chase_train", "img"),
            "锤": ("hammer", "img"),
            "砸": ("smash", "img"),
            "吸": ("suck", "img"),
            "顶": ("play", "img"),
            "抱": ("hold_tight", "img"),
            "手枪": ("gun", "img"),
        }
        if action not in _actions:
            return f"不支持这个表情动作「{action}」。能做的：{' / '.join(_actions.keys())}"
        meme_name, kind = _actions[action]
        fn = getattr(_pf, meme_name, None)
        if fn is None:
            return f"表情模板 {meme_name} 没找到"

        if kind == "img":
            out = fn(img=target_img)
        elif kind == "img_arg":
            out = fn(img=target_img, arg="圆")
        elif kind == "img_list":
            out = fn(user_imgs=[target_img])
        else:  # sender：亲/贴需要发送者头像
            sender = UserInfo(qq=str(ev.user_id))
            await get_user_info(bot, sender)
            await download_image(sender)
            out = fn(user_imgs=[target_img], sender_img=sender.img)

        data = out.getvalue() if hasattr(out, "getvalue") else bytes(out)
        ext = ".gif" if data[:6] in (b"GIF87a", b"GIF89a") else ".jpg"
        tmp_dir = _P("data") / "temp" / "petpet"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"llm_petpet_{meme_name}_{int(_time.time() * 1000)}{ext}"
        tmp.write_bytes(data)
        await bot.send(event=ev, message=MessageSegment.image(f"file://{tmp.resolve()}"))
        return f"已经做了「{action}」头像表情包"
    except Exception as e:
        # retcode=1200 / NetworkError 是 NapCat 传图的 API 超时，图通常几秒后到
        if _is_send_timeout(e):
            logger.info(f"[PETPET-TOOL] 发送超时（图可能稍后到），按成功处理: {type(e).__name__}")
            return f"已经做了「{action}」头像表情包"
        logger.warning(f"[PETPET-TOOL] 表情包失败 action={action}: {type(e).__name__}: {e}")
        return "头像表情包没做成，直接告诉对方没成。"


register_tool(
    {
        "type": "function",
        "function": {
            "name": "petpet",
            "description": (
                "做头像表情包。用户说「摸摸/rua」「亲亲」「贴贴」「撕」「丢」「拍」「打拳」"
                "「舔」「追火车」「锤」「砸」「吸」「顶」「抱」「手枪」时用。"
                "action 从这些里选：摸/亲/贴/撕/丢/拍/打拳/舔/追火车/锤/砸/吸/顶/抱/手枪。"
                "目标 @ 某人（消息里 @ 了谁就做谁）、或 target 传 qq/「自己」，省略=说话人自己。"
                "做好的表情包不会自动发：结果里会给编号，想发就调 send(pending=编号)。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "表情动作：摸/亲/贴/撕/丢/拍/打拳/舔/追火车/锤/砸/吸/顶/抱/手枪",
                    },
                    "target": {"type": "string", "description": "可选：目标 qq 号或「自己」；不填且消息里 @ 了人就做被 @ 的，否则做说话人自己"},
                },
                "required": [],
            },
        },
    },
    _tool_petpet,
    scope=("private", "group"),
    hold=True,
)


# ================= 看视频工具（video_understand） =================
# LLM 判断需要「看视频本体」时调用：B站视频 → 下载 → 抽帧 → GLM视觉逐帧理解 → DeepSeek 总结。
_video_queue: "asyncio.Queue" = asyncio.Queue(maxsize=8)
_video_worker_task = None


def _ensure_video_worker():
    global _video_worker_task
    if _video_worker_task is None or _video_worker_task.done():
        _video_worker_task = asyncio.create_task(_video_worker())


async def _video_worker():
    # 依赖先导入：失败就记完整 traceback，别让 worker 静默死掉。
    try:
        from services import video_understand as VU
        from services.link_preview import bili_detail, bili_subtitle_segments, bili_cid
    except Exception as e:
        logger.exception(f"[VIDEO-TOOL] 依赖导入失败: {type(e).__name__}: {e}")
        return

    logger.info("[VIDEO-TOOL] 看视频 worker 已启动")
    while True:
        bot = ev = url = None
        task_dir = None
        try:
            bot, ev, url = await _video_queue.get()
            logger.info(f"[VIDEO-TOOL] 收到任务：{str(url)[:60]}")
            logger.info("[VIDEO-TOOL] 解析链接中（bili_detail + cid 兜底）…")
            detail = await bili_detail(url)
            bvid = detail.get("bvid") or VU.extract_bvid(url)
            buvid = detail.get("buvid") or ""
            cid = int(detail.get("cid") or 0) or await bili_cid(bvid, buvid)
            title = detail.get("title") or ""
            desc = detail.get("desc") or ""
            logger.info(f"[VIDEO-TOOL] bvid={bvid!r} cid={cid} title={title[:20]!r}")
            if not bvid or not cid:
                await bot.send(event=ev, message="这个 B 站链接拿不到视频信息，看不了。")
                continue
            # 不再自动发「我去看了」：工具结果已经告诉她「已排队」，
            # 这句话该由她自己说（用户 2026-11-18：工具先不主动 send）。
            # 字幕先拿（很快）：全文给总结用，时间戳段给「关键位置多抽高清帧」用
            logger.info("[VIDEO-TOOL] 拉取字幕段…")
            segments = await bili_subtitle_segments(bvid, cid, buvid)
            subtitle = "".join(t for _, _, t in segments).strip()[:4000]
            key_times = VU.key_times_from_segments(segments)
            logger.info(f"[VIDEO-TOOL] 字幕 {len(segments)} 段 → 关键帧时间点 {len(key_times)} 个")
            task_dir = VU.DATA_ROOT / f"{bvid}_{int(time.time())}"
            video_path = await VU.download_video(bvid, cid, task_dir, buvid=buvid)
            if not video_path:
                await bot.send(event=ev, message="视频下载失败了（可能未公开或接口限制），看不成了。")
                continue
            logger.info("[VIDEO-TOOL] 下载完成，开始抽帧")
            # 整段每 5 秒一帧 + 字幕关键位置高清帧，全部按时间顺序看完再一次性总结
            frames = await VU.sample_frames(video_path, task_dir, key_times=key_times)
            logger.info(f"[VIDEO-TOOL] 抽到 {len(frames)} 帧，开始逐帧视觉理解")
            frame_desc = await VU.understand_frames(frames, title)
            logger.info(f"[VIDEO-TOOL] 视觉理解完成"
                        f"（{len(frame_desc.splitlines()) if frame_desc else 0} 行），开始总结")
            summary = await VU.summarize(title, desc, frame_desc, subtitle)
            if not summary:
                summary = frame_desc or "看了，但没能总结出内容（画面信息太少）。"
            # 👇 长文规矩（用户 2026-11-17）：群聊不发长文，私聊长文走合并转发卡片。
            #    视频总结天生就是长文 —— 以前直接 bot.send 一整段，群里必刷屏。
            _full = f"【{title}】内容总结：\n{summary}"
            if isinstance(ev, GroupMessageEvent):
                # 群聊规矩：20 字以上走合并转发。视频总结天然是长文，
                # 以前砍成 60 字 +「想看详细的私聊我」，等于白看一遍。
                if not await _send_long_as_forward(bot, ev, _full):
                    _short = limit_hanzi(summary, GROUP_MAX_HANZI * 3) or summary[:60]
                    await bot.send(
                        event=ev,
                        message=f"【{title}】{_short}…（想看详细的私聊我）",
                    )
            elif isinstance(ev, PrivateMessageEvent) and len(summary) >= LONG_FORWARD_MIN:
                if not await _send_long_as_forward(bot, ev, _full):
                    await bot.send(event=ev, message=_full)
            else:
                await bot.send(event=ev, message=_full)
            # 👇 把「看了这个视频」写进长期记忆 + 档案相处经历：
            #    否则总结发完就没了，DeepSeek 下轮被问「你之前看的视频讲了啥」答不上来。
            try:
                _who = str(getattr(ev, "user_id", "") or "")
                memory_v2.remember(
                    text=f"看过的B站视频《{title}》：{summary}",
                    kind="fact",
                    about_qq=_who,
                    tags=["视频", "B站"],
                    importance=0.7,
                )
                _label = _msg_label(ev) if ev else ""
                profiles.add_bond_note(_who, f"看了{_label}发来的B站视频《{title}》")
                logger.info(f"[VIDEO-TOOL] 视频总结已写入记忆/档案（about_qq={_who}）")
            except Exception as _e:
                logger.warning(f"[VIDEO-TOOL] 写记忆失败: {type(_e).__name__}: {_e}")
            logger.info("[VIDEO-TOOL] 总结已发回")
        except Exception as e:
            logger.exception(f"[VIDEO-TOOL] 看视频失败: {type(e).__name__}: {e}")
            if bot and ev:
                try:
                    await bot.send(event=ev, message="看视频时出错了，稍后再试。")
                except Exception:
                    pass
        finally:
            if task_dir is not None:
                await VU.cleanup_dir(task_dir)
            try:
                _video_queue.task_done()
            except Exception:
                pass


async def _tool_video_understand(args: dict) -> str:
    """看 B 站视频并理解内容（下载→抽帧→视觉→总结）。"""
    bot = llm_mod.TOOL_CTX.get("bot")
    ev = llm_mod.TOOL_CTX.get("event")
    if not bot or not ev:
        return "现在没法看视频（缺会话信息）"
    url = str(args.get("url") or "").strip()
    if not url:
        try:
            import re as _re
            _raw = ev.get_plaintext()
            _m = _re.search(r"https?://(?:b23\.tv|www\.bilibili\.com|bilibili\.com)/[^\s]+", _raw)
            if _m:
                url = _m.group(0)
        except Exception:
            pass
    if not url:
        return "没说要看哪个 B 站视频。"
    _ensure_video_worker()
    if _video_queue.full():
        return "看视频的队列满了，稍后再试。"
    await _video_queue.put((bot, ev, url))
    return f"已排队看视频（第 {_video_queue.qsize()} 个），看完把内容总结发回来"


register_tool(
    {
        "type": "function",
        "function": {
            "name": "video_understand",
            "description": (
                "下载并**真正看**一个 B 站视频，理解它讲了什么（整段抽帧+视觉理解+总结）。"
                "用户转 B 站视频/小程序卡片、或发 B 站链接，并问「讲了什么」「能看到吗」"
                "「能不能看」「内容是什么」时，**必须调用这个工具**，别拿标题/封面/搜索糊弄。"
                "url 从消息里提取 B 站链接（bilibili.com/video/BV... 或 b23.tv 短链）。"
                "会先回「在看了」，几分钟后把内容总结发回来。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "B 站视频链接（bilibili.com/video/BV... 或 b23.tv）"},
                },
                "required": ["url"],
            },
        },
    },
    _tool_video_understand,
    scope=("private", "group"),
)


# ================= 被戳一戳 → 等同被 @（三句关注）=================
# 需求：有人戳她，她用**自己的语气**回一句，并开启跟 @ 一样的三句关注。
_POKE_LAST: dict = {}          # session_key -> 上次回戳的时间（连戳只回一次）


def _poke_action_text(event) -> str:
    """
    从 poke 的 raw_info 里拼出「他做了什么 / 说了什么」。

    OneBot 的 poke 通知长这样（真实日志）：
      raw_info=[{type:qq}, {type:img, src:...nudgeaction/item/8/expression.jpg},
                {type:nor, txt:"揉了揉"}, {type:qq},
                {type:nor, txt:"的小猫并说了句：好可爱"}]
    拼起来 = 「揉了揉的小猫并说了句：好可爱」—— 这是她判断"该疑惑还是该害羞"的
    唯一线索。以前整段 raw_info 被丢掉，所以她只能回一句写死的「干嘛」。
    """
    parts = []
    for it in (getattr(event, "raw_info", None) or []):
        if isinstance(it, str):
            if it.strip():
                parts.append(it.strip())
            continue
        if not isinstance(it, dict):
            continue
        txt = str(it.get("txt") or "").strip()
        if txt:
            parts.append(txt)
    return "".join(parts).strip()[:60]


poke_me = on_notice(priority=1, block=False)


@poke_me.handle()
async def _(bot: Bot, event: NoticeEvent):
    try:
        if getattr(event, "notice_type", "") != "notify":
            return
        if getattr(event, "sub_type", "") != "poke":
            return
        if not _is_self_qq(getattr(event, "target_id", ""), event):
            return                      # 戳的是别人（不是她的任何号），不管

        uid = str(getattr(event, "user_id", "") or "")
        gid = getattr(event, "group_id", None)
        session_key = f"group_{gid}" if gid else f"private_{uid}"

        # 👇 群聊免打扰时段不接戳（用户 2026-11-12：
        #    「0~8 点群里禁言，但被戳一戳还是会回复干嘛」）。
        #    以前这个 handler 完全没看睡眠开关 —— 群里 0~8 点 @ 都不回，
        #    结果有人戳一下就冒出来说「干嘛」，比不回还怪。
        #    私聊不受限（私聊 24 小时都在），所以只在有 gid 时拦。
        if gid and _in_sleep_hour():
            logger.info(f"[POKE] {session_key} 免打扰时段"
                        f"（{SLEEP_HOUR_START}~{SLEEP_HOUR_END} 点），不接戳")
            return

        # 👇 先把「被戳」写进对话上下文（不管回不回都记，她知道被戳了几次）。
        #    用户 2026-11-12：「我问他，他说没收到戳啊」—— poke 是 notice 事件，
        #    以前只在这里回一句就完了，**没进 recent_group_msgs**，
        #    所以她下一轮看上下文时压根不知道刚被戳过，只能说「没收到」。
        # 戳一戳自带的动作/台词（「揉了揉…并说了句：好可爱」）—— 以前整个丢掉了
        _act = _poke_action_text(event)
        _nm = _msg_label(event) if gid else ""
        try:
            _now_p = time.time()
            recent_group_msgs[str(gid)].append(
                f"{_ts_tag(_now_p)}{_nm or ('QQ' + uid)}【戳了你一下】"
                f"{_act or '（没有附加动作）'}: （不是消息，是戳一戳）"
            )
            recent_group_ts[str(gid)].append(_now_p)
        except Exception:
            pass

        # 👇 戳一戳次数屏蔽（2026-11-23 用户）：
        #    「同一个人 2 小时内戳超过 3 次就不回，2 小时后再看」
        #    窗口内第 4 次起：不回、也不戳回，只记上下文；滑出窗口后自动恢复。
        _allow, _n = poke_guard.note(uid)
        if not _allow:
            logger.info(
                f"[POKE] {uid} 在 {poke_guard.WINDOW_SEC / 3600:.0f}h 内第 {_n} 次戳"
                f"（上限 {poke_guard.MAX_PER_WINDOW}）→ 不回、不戳回，只记上下文")
            return

        _focus_start(session_key, uid)   # 走和 @ 完全一样的关注逻辑

        # 👇 戳回去（用户 2026-11-12：「我戳了他，然后他不戳」）。
        #    以前只回一句「干嘛」，不戳回 —— 对方觉得她在装死。
        #    戳一戳是双向动作，被戳了就戳回去，这才是「有来有回」。
        #    注意：别连戳，一次就够（QQ 连戳会被风控）。
        try:
            if gid:
                await bot.call_api("group_poke", group_id=int(gid), user_id=int(uid))
            else:
                await bot.call_api("friend_poke", user_id=int(uid))
            logger.info(f"[POKE] {session_key} 已戳回 {uid}")
        except Exception as e:
            logger.warning(f"[POKE] 戳回 {uid} 失败: {type(e).__name__}: {e}")

        # 👇 不再用写死的台词（用户 2026-11-19：「她回复了一句没有疑惑标签的表情。
        #    实际上是有的」—— 根因是戳一戳的 raw_info 里明明有「揉了揉…并说了句：好可爱」，
        #    我们却只回一句「干嘛」，等于把对方的动作和话全丢了）。
        #    现在：把动作原文写进上下文 + 让她自己按当前心情/关系回一句。
        _act_s = f"（{_act}）" if _act else ""
        # 连戳几下只回一次：8 秒内的后续戳只记上下文，不再生成（否则连发三条很吵）
        _now_p2 = time.time()
        _last = float(_POKE_LAST.get(session_key) or 0)
        if _now_p2 - _last < 8:
            logger.info(f"[POKE] {session_key} 连续戳（{_now_p2 - _last:.1f}s）→ 只记上下文，不再回")
            return
        _POKE_LAST[session_key] = _now_p2

        _poke_text = (
            f"【（系统）{_nm or ('QQ' + uid)} 戳了你一下{_act_s}。"
            f"这不是一条消息，是戳一戳；括号里就是他刚做的动作/说的话。"
            f"用你自己的语气回一句（可以疑惑、无语、反问、撒娇），"
            f"别用固定台词，也别解释「这是戳一戳」。】"
        )
        _pctx = list(recent_group_msgs.get(str(gid)) or [])[-6:] if gid else None
        _old_ctx = dict(llm_mod.TOOL_CTX)
        try:
            llm_mod.set_tool_context(bot=bot, event=event, speaker_qq=uid,
                                     speaker_name=_nm or uid,
                                     addressed=True, newer_count=0)
            reply, _ = await call_llm(session_key, _poke_text, group_context=_pctx,
                                      speaker_qq=uid, speaker_name=_nm or uid)
        except Exception as e:
            logger.warning(f"[POKE] 生成回复失败: {type(e).__name__}: {e}")
            reply = random.choice(["干嘛", "戳我干嘛", "嗯？"])
        finally:
            llm_mod.TOOL_CTX.clear()
            llm_mod.TOOL_CTX.update(_old_ctx)

        _pp = parse_reply(reply)
        _apply_mood_label(_pp, session_key)
        if _pp.get("silent"):
            logger.info(f"[POKE] {session_key} 选择不回这一下")
            return
        _pmsg = _items_to_message(_pp.get("items") or [])
        if not len(_pmsg):
            # ⚠️ 不能回退成 reply 原文：里面可能只剩 [[THINK|…]] / [[MOOD|…]] 这类
            #    隐藏标记（items 空就是因为正文全是标记），发出去用户会看到代码。
            _pmsg = Message(MessageSegment.text("干嘛"))
        try:
            if gid:
                await bot.send_group_msg(group_id=int(gid), message=_pmsg)
            else:
                await bot.send_private_msg(user_id=int(uid), message=_pmsg)
        except Exception:
            await bot.send(event, _pmsg)
        logger.info(f"[POKE] {session_key} 被 {uid} 戳了{_act_s}，已戳回 + 开启三句关注")
    except Exception as e:
        logger.debug(f"[POKE] 处理失败: {type(e).__name__}: {e}")


# ================= 群里另一只 bot：拿它的回复做三方核对 =================
# 为什么要单独一个 matcher：
#   `_need_reply` 对那只鱼的消息默认 return False（不能让两个 bot 互聊），
#   但我们仍然要**收到**它说的话去做核对，所以得在 chat 之前拦一道。
# 注意优先级要比 chat(10) 小，block=False（不挡住 chat 自己记上下文）。
peer_watch = on_message(priority=6, block=False)


@peer_watch.handle()
async def _(bot: Bot, event: MessageEvent):
    try:
        if not isinstance(event, GroupMessageEvent):
            return
        gid = str(event.group_id)
        uid = str(event.user_id)
        if gid not in referee.PEER_GROUPS or uid not in referee.PEER_QQS:
            return

        text = await _enrich_text(event)
        if not text:
            return
        # 它在跟我们说话吗？（at 了我们就说明是两条鱼在对话 → 只留档不核对）
        talking_to_us = any(_is_self_qq(q, event) for q in _at_targets(event))
        logger.info(
            f"[PEER] 收到 {uid} 的回复（{len(text)} 字）"
            f"{'，是在跟我们说话' if talking_to_us else '，去核对'}"
        )
        # 鱼经常连发好几条 → 先进缓冲区，静默几秒合并成一条再核对
        referee.queue_peer_reply(gid, text, talking_to_us=talking_to_us)
    except Exception as e:
        logger.debug(f"[PEER] 监听失败: {type(e).__name__}: {e}")


# ================= 影子回答（场景②：鱼被 @ 时，我们也答一遍但不发群）=================
async def _shadow_answer(
    question: str, context: list, speaker_qq: str = "",
    speaker_name: str = "", gid: str = "",
) -> tuple:
    """
    用我们这一套（人格 + 档案 + 记忆 + 群上下文）对「鱼被 @ 的那个问题」答一遍，
    但**绝不发到群里**，只拿去和鱼/GLM 的答案比对。

    session_key 用 shadow_{gid}：独立命名空间，不会污染真实会话的历史。
    """
    sk = f"shadow_{gid or 'x'}"
    qq = str(speaker_qq or "0")
    name = str(speaker_name or "群友")

    try:
        prompt_text = build_prompt(
            session_key=sk, user_text=question,
            speaker_qq=qq, speaker_name=name, persona_text="",
        )
    except Exception as e:
        logger.debug(f"[SHADOW] build_prompt 失败: {type(e).__name__}: {e}")
        prompt_text = question

    try:
        reply, _ = await call_llm(
            sk, prompt_text, group_context=context or None,
            speaker_qq=qq, speaker_name=name,
        )
    except Exception as e:
        logger.warning(f"[SHADOW] 生成失败: {type(e).__name__}: {e}")
        return "", prompt_text

    try:
        parsed = parse_reply(reply)
        txt = "；".join(i["value"] for i in parsed.get("items", []) if i["kind"] == "text")
    except Exception:
        txt = str(reply or "")
    logger.info(f"[SHADOW] 影子回答（不发群）：{txt[:60]}")
    return txt, prompt_text


referee.set_shadow_fn(_shadow_answer)
