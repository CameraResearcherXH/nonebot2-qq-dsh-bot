# plugins/song_cover/__init__.py
from core import tuning
import os
import re
import json
import time
import shutil
import asyncio
import array
import math
import httpx
import subprocess
import tempfile
import wave
from pathlib import Path

from nonebot import on_command, on_message, logger
from nonebot.rule import Rule
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, Message, MessageSegment
from nonebot.params import CommandArg

from services.song_gender import detect_singer_gender, resolve_gender_chain

# ================= 配置 =================
NETEASE_API = os.getenv("NETEASE_API", "http://127.0.0.1:3000")
TEMP_AUDIO = Path("data/temp_audio")
TEMP_AUDIO.mkdir(parents=True, exist_ok=True)

# 男女各一套 RVC 参数（全部可用 .env 覆盖）
#
# ⚠️ 2026-11-12 重大修正（用户实测 4 首后定位）：
#   「《月声尽，心灯明》变出来如同念读，平平无奇，但原曲不是这样」——
#   **根因是 rms_mix_rate=0.25，不是音高**。
#   RVC 的 rms_mix_rate：0 = 用模型的音量包络（平），1 = 用原唱的音量包络（有起伏）。
#   0.25 意味着**原曲 75% 的强弱起伏被抹掉** → 唱什么都像念课文。
#   → 提到 0.8，把原唱的动态还回来。
#
#   protect=0.25 也偏低（默认 0.5）：它保护清音/气声不被强行变声。
#   太低 → 气声被压扁 → 「又夹又哑」。提到 0.4。
VOICE_PRESETS = {
    "female": {
        "voice": os.getenv("TARGET_VOICE_FEMALE", "kikiv2"),
        "pitch": tuning.i("RVC_PITCH_FEMALE"),
        "f0_method": os.getenv("RVC_F0_FEMALE", "rmvpe"),
        "index_rate": tuning.f("RVC_INDEX_RATE_FEMALE"),
        # 音量包络混合：越大越保留原唱的强弱起伏（0.25 → 念读感）
        "rms_mix_rate": tuning.f("RVC_RMS_FEMALE"),
        # 清音/气声保护：太低会把气声压扁成「夹」
        "protect": tuning.f("RVC_PROTECT_FEMALE"),
    },
    "male": {
        "voice": os.getenv("TARGET_VOICE_MALE", "kikiv2"),
        "pitch": tuning.i("RVC_PITCH_MALE"),
        "f0_method": os.getenv("RVC_F0_MALE", "rmvpe"),
        "index_rate": tuning.f("RVC_INDEX_RATE_MALE"),
        "rms_mix_rate": tuning.f("RVC_RMS_MALE"),
        "protect": tuning.f("RVC_PROTECT_MALE"),
    },
}
DEFAULT_PRESET_KEY = "female"

# 👇 这里是你提供的网易云 VIP Cookie，已直接注入
NETEASE_COOKIE = "JSESSIONID-WYYY=c0nvY%2FOsW%2BYx3%2Bm2XbhIvjsyi4fdZFl4n4GAQsIeWtlPnqCk27OAescEaqFTGDn%2B2FrKjb6sIcTVmQ24ddxSP7nOJd%2FVcNdqR8wFS1YJXM19t3egxhHyuJMZhHspJ6ZyA18Gk3cKeEnZmQHuQ7no6wj%2BQMIFxzZ%2F8FURos6mGl3RGk8B%3A1793795288632; _iuqxldmzr_=32; _ntes_nnid=5f16627ed08ec7bc2838555bf14efa17,1793793488645; _ntes_nuid=5f16627ed08ec7bc2838555bf14efa17; Hm_lvt_1483fb4774c02a30ffa6f0e2945e9b70=1793605088,1793793489; HMACCOUNT=B410DB14DA5AEB95; WEVNSM=1.0.0; WNMCID=rsnzws.1793793489268.01.0; NMTID=00OW_gtJ7GZZS7jZEvAl4lTst-nvxAAAAGhpmx5fw; WM_TID=mZ6%2BSj6J7S1FAREBBFfIRtaKXhOfZslu; WM_NI=gakzi3%2FD2NqUjUOj1fZqpd%2FGyMin%2FpTKsrS6uLjcrcQTX%2Bhkegk16wdKbxZs8w4neaSO1Mrm3izNnZWOCKVX%2BBC3OuMjkC1VwXrlV0HUvaOedgyxiM0n5X0y897Lf96WNEg%3D; WM_NIKE=9ca17ae2e6ffcda170e2e6ee94ce449ca6b983db6aaab08bb7c85e838f9f86c94ff7efbcb1e44bf5b2a587b32af0fea7c3b92aa5bfa59bec39ed9cfeaff45faa8b818bb46e9aafc085ae4dbb93a78fae7c9895bd93d23eb794a684f169938c98cceb61a7b9a5d2b57da895ae99d979fca9fc8fe86b9191ffaef167f1b7b6afed5295918dacd554e99e8dd4ce7f8fb6a5a4b45eb588be8dd53c8c8bba82ce6fa8b9b799ec47f4b2fbb9b373fce7f9ade533f68982a6e637e2a3; __snaker__id=zRgeNMx0PTahNMtE; gdxidpyhxdE=%2FIIaJMMrkcYQ%2F5oEo%2BLRZKoaS7Abbme9VqSyh5iQAfqWphxqnD1vELjUpw8%2Bo%2FbDxZTB5ahrYf%2FkfK44aHs2wVDM6N3YeN9vy85dgG4DSAj99vsnKBu8YmReeDHiO05oKgqp%2BpDNZxAJHfbVKK7oPy2ZR4dNgYqxOHeyGYbzwPS2cgV4%3A1793794390444; ntes_utid=tid._.vv6YOp2wYDRARgAVBEKdQoKOSlPKfnjI._.0; sDeviceId=YD-4yQP2HnEmw1FUhBAVEOZEtPfWxbaKiiI; MUSIC_U=00B78F26DA362856CCE8644724DA90E63BCF7182A92F26C47A06AE6D5E584D6F49853E56DA6DDBF59A1A0C866E652A1331B959AFEC18D049C0E0213C9B24C93B448140738947C043488D48BB0D1B3B942B15F30652FCFF217847830FC8A1D5F72A1AB3FF88079C05EBD550970B42C05B05123C78CA460D50E61A8A7694F796D9B294AAC3CE9ABB0DC741F19E37DFD14E7287762D63765650B09116C49326AB72912541843B64199914F9A7DBA3C57ED3E03CAC87FCB616580B154C63EAB0D53710278B7DBFE7F5722897D9CA63B30022544AA300257AE59D73F723B81D416EA262F668D36A3A15577ECD4468852E2F656532FA272EC5214443929B3F2AEFC5894C54738B7304AB1794E1048DD083B19F20C3F38CF91C60990937DEEFE172CEE8C35AC3252B7E71C2C11504EBCC26EFF02F2EBAD42530231520B9A1D5118C90DFC24EA9B98BF17E9E51C8C525BEAC6A81DC8206112A027172DB513A6744ACC144AA2F4924E89AA9440C58CC1B734427A6AEE9D0BBB340F030AECC6BB18ACCE96E6D7DC1FB51D44A899A21053BBF764F2607; __csrf=5766633216a2b16d0067d4b6de7b41f6; ntes_kaola_ad=1; Hm_lpvt_1483fb4774c02a30ffa6f0e2945e9b70=1793794170; playerid=17353781"

# 👇 核心并发控制：限制同时最多 N 首歌占用算力（env 可配）
#
# ⚠️ 注意这里限制的是「进入流程的歌数」，不是真正的并行度：
#    算力机 node.py 的 main() 是串行跑任务的，所以实际并发是 1。
#    这个信号量的作用是别让 10 个请求一起挤进去把任务队列搞乱。
#    QQ 的 /翻唱 和外部 API 共用这一个信号量。
COVER_MAX_CONCURRENT = max(1, tuning.i("COVER_MAX_CONCURRENT"))
cover_semaphore = asyncio.Semaphore(COVER_MAX_CONCURRENT)

cover = on_command("翻唱", priority=5, block=True)
# /清唱：只要变声后的人声，不带伴奏
acapella = on_command("清唱", aliases={"纯人声", "清唱版"}, priority=5, block=True)
# /测歌：只跑性别/音域判定链，不翻唱（排查「为什么这首歌被抬了 N 个半音」）
song_probe = on_command("测歌", aliases={"测试歌曲", "判歌"}, priority=5, block=True)

# 能力开关（/技能 关 语音/音乐）
from services import skill_switch as _sk   # noqa: E402
_sk.gate(cover, "sing", "翻唱")
_sk.gate(acapella, "sing", "清唱")
_sk.gate(song_probe, "sing", "测歌")


def cover_queue_state() -> dict:
    """当前并发占用情况，给 /api/cover/stats 和日志用。"""
    running = COVER_MAX_CONCURRENT - cover_semaphore._value
    return {
        "max_concurrent": COVER_MAX_CONCURRENT,
        "running": max(0, running),
        "free": max(0, cover_semaphore._value),
    }


# ================= 网易云链接解析 =================
# 支持这几种写法：
#   https://music.163.com/song?id=186016
#   https://music.163.com/#/song?id=186016
#   https://y.music.163.com/m/song?id=186016&userid=xxx
#   分享周杰伦的单曲《晴天》https://music.163.com/song?id=186016 ...
#   直接发歌曲 id：186016
_SONG_ID_PATTERNS = [
    re.compile(r"song\s*\?[^\n]{0,80}?id=(\d{4,})", re.I),
    re.compile(r"/song/(\d{4,})", re.I),
    re.compile(r"songId=(\d{4,})", re.I),
    re.compile(r"song_id=(\d{4,})", re.I),
]
_NETEASE_SHORT_RE = re.compile(r"https?://163cn\.tv/\S+", re.I)
# 歌单/专辑/电台链接里的 id 不是歌曲 id，不能拿去点歌
_NOT_SONG_RE = re.compile(r"(playlist|album|djradio|radio|dj)\s*\?", re.I)


def parse_netease_song_id(text: str) -> str:
    """从分享文本/链接里抠出网易云歌曲 id，抠不到返回空串。"""
    s = str(text or "").strip()
    if re.fullmatch(r"\d{5,}", s):
        return s
    for p in _SONG_ID_PATTERNS:
        m = p.search(s)
        if m:
            return m.group(1)
    # 兜底：裸 id=，但排除歌单/专辑/电台（那些 id 拿去点歌会查到完全无关的东西）
    if not _NOT_SONG_RE.search(s):
        m = re.search(r"id=(\d{4,})", s, re.I)
        if m:
            return m.group(1)
    return ""


async def resolve_short_link(text: str) -> str:
    """163cn.tv 短链跟随重定向拿到真实 URL，失败原样返回。"""
    m = _NETEASE_SHORT_RE.search(str(text or ""))
    if not m:
        return text
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(m.group(0))
            logger.info(f"[SONG] 短链展开: {m.group(0)} → {r.url}")
            return str(r.url)
    except Exception as e:
        logger.warning(f"[SONG] 短链展开失败: {type(e).__name__}: {e}")
        return text


async def resolve_song_id(text: str) -> str:
    """先按普通链接解析，解析不到再尝试展开短链。"""
    sid = parse_netease_song_id(text)
    if sid:
        return sid
    if "163cn.tv" in str(text):
        return parse_netease_song_id(await resolve_short_link(text))
    return ""


# ================= 兜底转码（正常流程走算力机，这里只防老版本 node.py） =================
def sync_mix_audio(vocal_path: str, inst_path: str, output_path: str) -> bool:
    """
    人声 + 伴奏 → mp3。**只做兜底**，正常流程全部在算力机做。

    ⚠️ 三个必须的参数：
       · normalize=0      amix 默认按输入数把每一轨砍半，人声一大伴奏就听不见
       · duration=longest 用 first 的话人声一短，伴奏直接被截断
       · alimiter          两轨都是原始音量，加限幅器防削波
    """
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-i", vocal_path, "-i", inst_path,
            "-filter_complex",
            "[0:a]volume=1.0[a0];[1:a]volume=1.0[a1];"
            "[a0][a1]amix=inputs=2:duration=longest:normalize=0,"
            "alimiter=level_in=1:limit=0.95[out]",
            "-map", "[out]", "-ar", "44100", "-ac", "2",
            "-codec:a", "libmp3lame", "-b:a", os.getenv("COVER_MP3_BITRATE", "192k"),
            str(output_path),
        ]

        logger.info(f"[MIX] 兜底混音: {output_path}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            size_mb = os.path.getsize(output_path) / 1048576 if os.path.exists(output_path) else 0
            logger.info(f"[MIX] ✅ 混音完成: {Path(output_path).name}（{size_mb:.2f} MB）")
            return True
        logger.error(f"[MIX] ❌ 混音失败: {result.stderr[-500:]}")
        return False
    except Exception as e:
        logger.error(f"[MIX] ❌ 混音异常: {type(e).__name__}: {e}")
        return False


def transcode_mp3(src_path: str, output_path: str) -> bool:
    """单轨转 mp3（清唱 / 算力机回了 wav 时用）。"""
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-i", src_path,
            "-ar", "44100", "-ac", "2",
            "-codec:a", "libmp3lame", "-b:a", os.getenv("COVER_MP3_BITRATE", "192k"),
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and os.path.exists(output_path):
            return True
        logger.error(f"[MIX] ❌ 转 mp3 失败: {result.stderr[-400:]}")
        return False
    except Exception as e:
        logger.error(f"[MIX] ❌ 转 mp3 异常: {type(e).__name__}: {e}")
        return False


def audio_duration(path) -> float:
    """用 ffprobe 拿时长（秒）。失败返回 0。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float((r.stdout or "0").strip() or 0)
    except Exception as e:
        logger.warning(f"[MIX] ffprobe 取时长失败: {type(e).__name__}: {e}")
        return 0.0


def _rms_curve(path, hop_sec: float = 0.5) -> list:
    """
    抽 8kHz 单声道，按 hop_sec 算响度曲线 [(起始秒, rms), ...]。失败返回 []。

    只为了找「高潮在哪结束」，8kHz 单声道足够，几分钟的歌一两秒算完。
    """
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix="_env.wav")
        os.close(fd)
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-ac", "1", "-ar", "8000", "-vn", tmp],
            capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0 or not os.path.exists(tmp):
            logger.warning(f"[VOICE] 响度曲线抽取失败: {(r.stderr or '')[-200:]}")
            return []
        with wave.open(tmp, "rb") as w:
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
        if sr <= 0 or len(raw) < sr * 2:
            return []
        a = array.array("h")
        a.frombytes(raw)
        step = max(1, int(sr * hop_sec))
        curve = []
        for start in range(0, len(a) - step + 1, step):
            seg = a[start:start + step]
            curve.append((start / sr, math.sqrt(sum(x * x for x in seg) / len(seg))))
        return curve
    except Exception as e:
        logger.warning(f"[VOICE] 响度曲线异常: {type(e).__name__}: {e}")
        return []
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


def find_climax_end(path) -> float:
    """
    找「高潮唱完」的时间点（秒）。0 表示没找到，调用方只按体积上限截。

    做法（0.5 秒一格）：
      1. 找全局最响的位置 = 高潮峰值
      2. 从峰值往后，找第一个「连续 3 格（1.5s）都低于峰值 55%」的位置 = 高潮收尾
      3. 再加 2 秒尾巴，把最后一句的混响放完

    保护：至少保留全曲 45%，避免前奏/副歌特别响的歌被截成十几秒。
    """
    curve = _rms_curve(path, 0.5)
    if len(curve) < 20:
        return 0.0
    peak = max(v for _, v in curve)
    if peak <= 0:
        return 0.0
    dur = curve[-1][0] + 0.5
    peak_idx = max(range(len(curve)), key=lambda i: curve[i][1])
    thr = peak * 0.55
    run = 0
    for i in range(peak_idx + 1, len(curve)):
        if curve[i][1] < thr:
            run += 1
            if run >= 3:
                end = curve[i - run + 1][0] + 2.0
                return round(max(end, dur * 0.45), 1)
        else:
            run = 0
    return round(dur, 1)


def trim_for_voice(src_path: str, max_mb: float, output_path: str,
                   max_sec: float = 0.0) -> tuple:
    """
    把成品截到「唱完高潮」为止，用来控制语音消息体积。**不降码率**。

    为什么不再压码率：上一版为了塞进 max_mb 会把码率砍到 24~48k，
    真机听下来音质明显变糊（「压缩后的音质很烂」）。
    现在改成「砍时长不砍质量」，码率保持 COVER_VOICE_BITRATE（默认 192k）。

    返回 (是否截断, 截断后的秒数)。False 表示原曲本来就够短，直接用原文件。
    """
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        dur = audio_duration(src_path)
        if dur <= 0:
            logger.warning("[VOICE] 拿不到时长，没法截断")
            return False, 0.0

        bitrate = os.getenv("COVER_VOICE_BITRATE", "192k")
        try:
            kbps = int(str(bitrate).lower().rstrip("k"))
        except Exception:
            kbps = 192
        # 体积允许的最大时长；0.92 留 8% 余量（mp3 头尾 / VBR 波动）
        limit = max_mb * 8 * 1024 / max(1, kbps) * 0.92
        if max_sec and max_sec > 0:
            limit = min(limit, float(max_sec))

        climax = find_climax_end(src_path)
        if climax > 0:
            logger.info(f"[VOICE] 高潮收尾约 {climax:.1f}s（全曲 {dur:.1f}s）")
            limit = min(limit, climax)

        limit = max(30.0, min(limit, dur))
        if limit >= dur - 2:
            logger.info(f"[VOICE] {dur:.1f}s 不用截断，直接发原文件")
            return False, dur

        fade = min(3.0, max(1.0, limit * 0.05))
        cmd = [
            "ffmpeg", "-y", "-i", str(src_path),
            "-t", f"{limit:.2f}",
            "-af", f"afade=t=out:st={max(0.0, limit - fade):.2f}:d={fade:.2f}",
            "-ar", "44100", "-ac", "2",
            "-codec:a", "libmp3lame", "-b:a", bitrate,
            str(output_path),
        ]
        logger.info(
            f"[VOICE] 截断语音：{dur:.1f}s → {limit:.1f}s，"
            f"码率保持 {bitrate}（不压音质）"
        )
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not os.path.exists(output_path):
            logger.error(f"[VOICE] ❌ 截断失败: {(r.stderr or '')[-400:]}")
            return False, 0.0
        mb = os.path.getsize(output_path) / 1048576
        logger.info(f"[VOICE] ✅ 截断完成: {mb:.2f} MB / {limit:.1f}s")
        return True, limit
    except Exception as e:
        logger.error(f"[VOICE] ❌ 截断异常: {type(e).__name__}: {e}")
        return False, 0.0


# ================= 「要不要整首」的待定请求 =================
# 发完截断版语音后问一句，用户回「要」就把整首发成文件给他。
# key = user_id，value = {"path", "title", "ts", "with_inst"}
PENDING_FULL: dict = {}
FULL_TTL = tuning.f("COVER_FULL_TTL")
_YES_WORDS = {"要", "1", "整首", "要整首", "发", "好的", "行", "听", "要听", "好"}
_NO_WORDS = {"不要", "0", "不用", "不了", "算了", "否", "别", "不需要", "不好"}
_ALL_WORDS = _YES_WORDS | _NO_WORDS


# 后台任务要留强引用，否则可能被 GC 提前回收（asyncio 的已知坑）
_BG_TASKS: set = set()


def _spawn(coro):
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)
    return t


async def _expire_pending(user_id: int, seconds: float):
    """到点自动清掉没回应的整首请求，别让文件一直躺在磁盘上。"""
    await asyncio.sleep(seconds)
    item = PENDING_FULL.pop(user_id, None)
    if not item:
        return
    try:
        Path(item["path"]).unlink(missing_ok=True)
        logger.info(f"[SONG] 整首请求超时（{seconds:.0f}s 没回应），已清理成品")
    except Exception:
        pass


def _pending_item(user_id: int):
    item = PENDING_FULL.get(user_id)
    if not item:
        return None
    if time.time() - item["ts"] > FULL_TTL:
        PENDING_FULL.pop(user_id, None)
        try:
            Path(item["path"]).unlink(missing_ok=True)
        except Exception:
            pass
        return None
    return item


async def _is_full_reply(event: MessageEvent) -> bool:
    if event.user_id not in PENDING_FULL:
        return False
    return event.get_plaintext().strip().lower() in _ALL_WORDS


# priority=1：比所有对话/指令都高，问完立刻拦，避免「要」被别的插件吃掉
full_reply = on_message(rule=Rule(_is_full_reply), priority=1, block=True)


@full_reply.handle()
async def _(bot: Bot, event: MessageEvent):
    item = _pending_item(event.user_id)
    PENDING_FULL.pop(event.user_id, None)
    if not item:
        await full_reply.finish()

    p = Path(item["path"])
    if event.get_plaintext().strip().lower() in _NO_WORDS:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
        await full_reply.finish("好吧。")

    if not p.exists():
        await full_reply.finish("诶，整首的文件不见了，再点一次吧。")
    try:
        await bot.send(event, MessageSegment(
            "file", {"file": f"file://{os.path.abspath(p)}", "name": p.name}
        ))
    except Exception as e:
        logger.warning(f"[SONG] 整首文件发送失败: {type(e).__name__}: {e}")
        await full_reply.finish("整首太大发不出去……你先听短的算了。")
    try:
        p.unlink(missing_ok=True)
    except Exception:
        pass
    await full_reply.finish("整首在这儿，收好。")


# ================= 核心流程（不依赖 bot，QQ 和外部 API 共用） =================
async def run_cover(song_name: str, with_inst: bool = True, notify=None,
                    gender_override: str = "", pitch_override=None,
                    deliver: dict | None = None) -> dict:
    """
    跑一遍翻唱，返回成品路径。**完全不碰 QQ**，所以外部 API 能直接调。

    notify: 可选协程 `async def f(msg: str)`，用来推进度（QQ 传 bot.send，API 传 None）
    返回:
      {"ok": bool, "path": str, "title": str, "song_id": str,
       "pitch": int, "gender": str, "size_mb": float, "error": str}

    ⚠️ 成品文件**不会**被删，调用方用完自己清（QQ 流程发完就删，API 交给 FileResponse）。
    """
    # 总开关最后一道闸：`/api/cover` 是**直接调 run_cover** 的（不经过 process_cover_task），
    # 所以闸必须放在这里，否则 /翻唱 off 之后外部客户端还能继续把算力机塞满。
    if not cover_on():
        logger.info(f"[SONG] 翻唱已关闭，拒绝《{song_name}》（来源：run_cover）")
        return {"ok": False, "path": "", "title": "", "song_id": "",
                "pitch": 0, "gender": "", "size_mb": 0.0,
                "error": "翻唱已关闭（算力机忙），等主人 /翻唱 on"}

    # 👇 支持「歌名 男/女」后缀，QQ 指令和外部 API 共用同一套解析。
    #    显式传了 gender_override 就以它为准，不被后缀覆盖。
    # 先剥「低音+N」（它在最末尾，性别后缀可能在它前面）
    song_name, _low_lift_env = parse_low_lift(song_name)
    if _low_lift_env:
        logger.info(
            f"[SONG] 低音区抬升已开启：F0 < {_low_lift_env.get('RVC_LOW_LIFT_HZ')}Hz "
            f"最多抬 {_low_lift_env.get('RVC_LOW_LIFT_SEMIS')} 半音"
            f"（过渡 ±{_low_lift_env.get('RVC_LOW_LIFT_FADE')}Hz）"
        )
    _name, _suffix_gender = split_gender_suffix(song_name)
    if _name != song_name:
        logger.info(f"[SONG] 从参数里识别到性别后缀：{_suffix_gender or '无'}，歌名《{_name}》")
        song_name = _name
    if not gender_override and _suffix_gender:
        gender_override = _suffix_gender

    res = {"ok": False, "path": "", "title": "", "song_id": "",
           "pitch": None, "gender": "", "size_mb": 0.0, "error": "",
           # 单段式：path=语音版（已截到高潮），full_path=完整版
           "full_path": "", "trimmed": False,
           # 走的哪条路：single=算力机一次到底 / legacy=服务器两段式（要 ffmpeg）
           "stage": ""}

    async def say(msg: str):
        if notify is None:
            return
        try:
            await notify(msg)
        except Exception as e:
            logger.warning(f"[SONG] 通知失败: {type(e).__name__}: {e}")

    if cover_semaphore.locked():
        _st = cover_queue_state()
        logger.info(
            f"[SONG] 排队等待（已在跑 {_st['running']}/{_st['max_concurrent']}）：《{song_name}》"
        )
    async with cover_semaphore:
        logger.info(f"[SONG] 开始处理{'翻唱' if with_inst else '清唱'}: 《{song_name}》")

        audio_path: Path | None = None
        final_path: Path | None = None
        vocal_path: str | None = None
        inst_path: str | None = None
        voice_output_path: str | None = None
        split_task_id = ""
        rvc_task_id = ""

        try:
            # ---------- 1. 解析链接 / 搜索 + 下载 ----------
            async with httpx.AsyncClient(timeout=15) as client:
                wanted_id = await resolve_song_id(song_name)
                songs = []
                if wanted_id:
                    try:
                        d = await client.get(f"{NETEASE_API}/song/detail", params={"ids": wanted_id})
                        songs = d.json().get("songs") or []
                        if songs:
                            logger.info(
                                f"[SONG] 从链接识别到 id={wanted_id}《{songs[0].get('name')}》"
                            )
                        else:
                            logger.warning(f"[SONG] 链接里的 id={wanted_id} 查不到详情，改走搜索")
                    except Exception as e:
                        logger.warning(f"[SONG] 查歌曲详情失败: {type(e).__name__}: {e}")

                if not songs:
                    search_res = await client.get(
                        f"{NETEASE_API}/search", params={"keywords": song_name, "limit": 1}
                    )
                    songs = search_res.json().get("result", {}).get("songs") or []
                if not songs:
                    res["error"] = f"找不到《{song_name}》"
                    await say(f"😭 找不到《{song_name}》这首曲子……")
                    return res

                song_id = str(songs[0]["id"])
                song_title = songs[0].get("name") or song_name
                res["song_id"] = song_id
                res["title"] = song_title

                # 音质等级：算力机最终会把源文件转成 mp3 再进 Demucs，
                # 所以下 flac（16MB）再转 320k mp3 等于白传 6MB 带宽。
                # 直接下 320k mp3 是等价的，传输量小一大截（家宽/小带宽服务器很关键）。
                # 想要无损：COVER_SOURCE_LEVEL=lossless
                _level = (os.getenv("COVER_SOURCE_LEVEL", "exhigh") or "exhigh").strip()
                audio_url = ""
                for _path, _params in (
                    ("/song/url/v1", {"id": song_id, "level": _level,
                                      "cookie": NETEASE_COOKIE}),
                    ("/song/url", {"id": song_id, "cookie": NETEASE_COOKIE}),
                ):
                    try:
                        d = await client.get(f"{NETEASE_API}{_path}", params=_params)
                        audio_url = (d.json().get("data") or [{}])[0].get("url") or ""
                    except Exception as e:
                        logger.warning(f"[SONG] {_path} 取地址失败: {type(e).__name__}: {e}")
                    if audio_url:
                        logger.info(f"[SONG] 音频地址来自 {_path}")
                        break
                if not audio_url:
                    res["error"] = "歌曲被锁（VIP Cookie 也下不了）"
                    await say("😭 这首曲子被锁了（即使有VIP Cookie也下不了），换一首试试？")
                    return res

                # ---------- 下载前先判性别，决定 RVC 参数 ----------
                # 三级链见 services.song_gender.resolve_gender_chain：
                #   L1 用户指定 → L2 歌手判定 → L3 交给算力机（均分100s 送 Qwen-Audio）
                singer_name = (songs[0].get("artists") or [{}])[0].get("name", "")
                gender_info = await resolve_gender_chain(
                    song_title, singer_name, gender_override
                )
                preset_key = (gender_info["gender"]
                              if gender_info["gender"] in VOICE_PRESETS else DEFAULT_PRESET_KEY)
                preset = VOICE_PRESETS[preset_key]
                res["gender"] = gender_info["gender"]
                _gsrc = gender_info.get("source") or "none"
                _glabel = {"override": "L1 用户指定",
                           "singer": "L2 歌手判定",
                           "none": "L3 交给算力机听人声"}.get(_gsrc, _gsrc)
                logger.info(
                    f"[GENDER] 《{song_title}》 歌手={gender_info.get('singer') or singer_name} "
                    f"→ {_glabel}：gender={gender_info['gender'] or '(待定)'} "
                    f"｜参数组 {preset_key} {preset} "
                    f"｜{gender_info.get('reason', '')}"
                )

                _suffix = ".flac" if ".flac" in audio_url.lower() else ".mp3"
                audio_path = TEMP_AUDIO / f"cover_{song_id}_{int(time.time())}{_suffix}"
                async with client.stream("GET", audio_url) as resp:
                    with open(audio_path, "wb") as f:
                        async for chunk in resp.aiter_bytes():
                            f.write(chunk)

            logger.info(f"[SONG] 《{song_title}》下载完成（{audio_path.name}）")

            # ---------- 2. 单段式：分离 + 测音高 + RVC + 混音 全在算力机 ----------
            # 为什么：两段式（split → rvc_mix）要在算力机和服务器之间**往返两次
            # 76MB 的 wav**（分离回传一次、RVC 前再下载一次）。一首 5 分钟的歌
            # 光传输就 3~6 分钟，而且任何一次超时，前面几分钟的 Demucs 全白跑。
            # 单段式只有最后 ~5MB 的 mp3 过网，中间文件全留在算力机本地。
            # 算力机心跳上报的 caps 里有 "cover" 才走这条，老版本自动退回两段式。
            from plugins.tool_dispatcher import STATUS
            caps = STATUS.get("caps") or set()
            if not caps:
                # 容器刚重启时 caps 还是空的（内存状态），要等算力机下一次心跳
                # （15s 一次）才有。不等待的话，重启后头几个请求会误走旧两段式，
                # 白白多传 152MB、还慢好几分钟。最多等 20 秒。
                logger.info("[SONG] 还没收到算力机心跳，最多等 20s 拿能力列表…")
                for _ in range(10):
                    await asyncio.sleep(2)
                    caps = STATUS.get("caps") or set()
                    if caps:
                        break
            single_stage = ("cover" in caps) and (
                os.getenv("COVER_SINGLE_STAGE", "true").strip().lower() != "false"
            )
            if not single_stage:
                # 走到这里只有两种可能：
                #   ① 算力机还是旧版 node.py（心跳 caps 里没有 cover）
                #   ② .env 里把 COVER_SINGLE_STAGE 设成了 false
                # 两段式要往返两次 76MB wav，而且服务器会用 ffmpeg 兜底混音 ——
                # 功能已经全挪到算力机了，正常不该再走这条。
                logger.warning(
                    f"[SONG] ⚠️ 走旧两段式（算力机 caps={sorted(caps) if caps else '空'}）"
                    f" —— 服务器会用 ffmpeg 兜底；确认算力机 node.py 是新版且心跳正常"
                )
            if single_stage:
                task_id = f"cover_{int(time.time())}"
                STATUS["tasks"][task_id] = {
                    "type": "cover",
                    "status": "pending",
                    "input": str(audio_path),
                    # 👇 让算力机优先自己从网易云 CDN 拉：服务器出方向带宽
                    #    （阿里云按上行计费，常见 1~5Mbps）是整条链路的瓶颈，
                    #    16MB 从服务器传要几分钟，算力机自己拉走家宽下行几秒搞定。
                    #    直连失败（防盗链/过期）会自动回退用 input。
                    "source_url": audio_url,
                    "with_inst": bool(with_inst),
                    "gender_hint": gender_info["gender"] or "",
                    # L1（用户指定）或 L2（歌手判定）成功 → 锁定，算力机直接采用，
                    # 不再跑 F0 性别判定、也不再调 Qwen-Audio。
                    # L3（都没定出来）→ 留空，算力机用「全曲均分 100s 人声 + Qwen-Audio」兜底。
                    "gender_lock": (gender_info["gender"]
                                    if gender_info.get("source") in ("override", "singer")
                                    else ""),
                    "gender_source": (gender_info.get("source")
                                      if gender_info.get("source") in ("override", "singer")
                                      else ""),
                    # 老版本 node.py 只认 force_gender，保留兼容
                    "force_gender": (gender_info["gender"]
                                     if gender_info.get("source") == "override" else ""),
                    "pitch_override": pitch_override,
                    "pitch": preset["pitch"],
                    # 👇 变调策略（2026-11-12 用户反馈）：
                    #    「情歌这种平调歌，起调很低，被降调就有点男声化了，可以不降」。
                    #    女声源降调 → 共振峰下移 → 听起来像男声，这是 RVC 的固有代价。
                    #    所以给算力机一个策略字段：
                    #      female_keep（默认）= 源是女声就**永不降调**，超上限也只 keep
                    #      auto              = 老行为（超上限就往下调）
                    #    算力机不认识这个字段会直接忽略，不影响老 node.py。
                    "pitch_policy": os.getenv("COVER_PITCH_POLICY", "female_keep"),
                    # 女声源允许超出模型上限多少比例还不降（默认 10%：540 → 594Hz）
                    "pitch_max_tolerance": tuning.f("COVER_PITCH_MAX_TOLERANCE"),
                    "bitrate": os.getenv("COVER_MP3_BITRATE", "192k"),
                    # 语音版限制由服务器统一下发：算力机不用再配一份，
                    # 免得 .env 和 node.env 各写一个值、两边对不上
                    "voice_max_mb": tuning.f("COVER_VOICE_MAX_MB"),
                    # QQ 语音条上限 60 秒，超过必超时 → 默认截 58 秒
                    "voice_max_sec": tuning.f("COVER_VOICE_MAX_SEC"),
                    "voice": preset["voice"],
                    "f0_method": preset["f0_method"],
                    "index_rate": preset["index_rate"],
                    "rms_mix_rate": preset["rms_mix_rate"],
                    "protect": preset["protect"],
                    # 👇 低音区抬升（/翻唱 晴天 低音+2 才带，默认空 = 不抬）
                    #    算力机把它当 env 覆盖传给 RVC 子进程，pipeline.py 从 env 读。
                    "rvc_env": _low_lift_env,
                    # 重启后靠它把成品补发回原会话（2026-11-24）
                    "deliver": dict(deliver or {}, label=str(song_title or song_name)[:60]),
                }
                logger.info(
                    f"[SONG] 下发单段式任务 {task_id}"
                    f"（{'翻唱' if with_inst else '清唱'}，分离+RVC+混音一次到底）"
                )
                wait_sec = tuning.f("COVER_FULL_WAIT_SEC")
                rounds = max(60, int(wait_sec / 2))
                out_path = None
                full_out = None
                for _ in range(rounds):
                    await asyncio.sleep(2)
                    t = STATUS["tasks"].get(task_id, {})
                    if t.get("status") == "completed":
                        out_path = t.get("output")
                        # 算力机截过断的话，第二个文件是完整版
                        full_out = t.get("output2")
                        break
                    if t.get("status") == "failed":
                        err = t.get("output")
                        logger.warning(f"[SONG] 单段式任务失败: {err}")
                        res["error"] = "翻唱失败"
                        await say("😭 唱失败了。")
                        return res
                if not out_path or not os.path.exists(str(out_path)):
                    res["error"] = "成品文件丢失（或等待超时）"
                    logger.error(f"[SONG] 单段式任务没有成品: {out_path!r}（等了 {rounds * 2}s）")
                    await say("😭 唱是唱了，但成品没回来。")
                    return res

                final_path = Path(str(out_path))
                voice_output_path = str(final_path)   # 让 finally 跳过它
                res.update({
                    "ok": True,
                    "path": str(final_path),
                    "size_mb": round(os.path.getsize(final_path) / 1048576, 2),
                    "stage": "single",
                })
                if full_out and os.path.exists(str(full_out)):
                    res["full_path"] = str(full_out)
                    res["trimmed"] = True
                    logger.info(
                        f"[SONG] 单段式完成《{song_title}》"
                        f"语音版 {res['size_mb']}MB + 完整版 "
                        f"{round(os.path.getsize(str(full_out)) / 1048576, 2)}MB"
                        f"（pitch 由算力机按实测 F0 算，见算力机日志 [PITCH]）"
                    )
                else:
                    logger.info(
                        f"[SONG] 单段式完成《{song_title}》{res['size_mb']}MB（未截断）"
                    )
                return res

            res["stage"] = "legacy"
            logger.info("[SONG] 算力机不支持单段式（caps 里没有 cover），走旧两段式")

            # ---------- 2'. 下发分离任务（旧两段式） ----------
            split_task_id = f"split_{int(time.time())}"
            STATUS["tasks"][split_task_id] = {
                "type": "split", "status": "pending",
                "input": str(audio_path), "output": "",
            }

            # ⚠️ 等待上限要跟大文件匹配：大歌分离完还要回传 70~90MB 两个 wav，
            #    原来写死 90 轮 × 2s = 180s，大歌必然等不到（分离成功了也白跑）。
            split_wait_sec = tuning.f("COVER_SPLIT_WAIT_SEC")
            split_rounds = max(15, int(split_wait_sec / 2))
            logger.info(f"[SONG] 等待分离完成，上限 {split_rounds * 2}s")
            for _ in range(split_rounds):
                await asyncio.sleep(2)
                task = STATUS["tasks"].get(split_task_id, {})
                if task.get("status") == "completed":
                    out = task.get("output")
                    if isinstance(out, dict):
                        vocal_path, inst_path = out.get("vocal"), out.get("inst")
                    break
                if task.get("status") == "failed":
                    res["error"] = "人声分离失败"
                    await say("😭 人声分离失败了。")
                    return res

            if not vocal_path or not inst_path or not (
                    os.path.exists(vocal_path) and os.path.exists(inst_path)):
                res["error"] = "分离返回的文件丢失"
                await say("😭 分离返回的文件丢失。")
                return res

            # ---------- 2.5 听人声定音高（比猜歌手可靠得多） ----------
            final_pitch = preset["pitch"]
            try:
                from services.song_gender import probe_vocal
                # 把前面判出的性别当先验传进去，压制 F0 倍频错误
                probe = await probe_vocal(vocal_path, gender_hint=gender_info["gender"])
                if probe.get("pitch") is not None:
                    final_pitch = int(probe["pitch"])
                logger.info(
                    f"[PITCH] 自适应结果 pitch={final_pitch}"
                    f"（来源={probe.get('source') or 'preset'}，{probe.get('reason')}）"
                )
            except Exception as e:
                logger.warning(
                    f"[PITCH] 自适应失败，沿用预设 {final_pitch}: {type(e).__name__}: {e}"
                )
            if pitch_override is not None:
                try:
                    final_pitch = int(pitch_override)
                    logger.info(f"[PITCH] 外部指定 pitch={final_pitch}（覆盖自适应结果）")
                except Exception:
                    pass
            res["pitch"] = final_pitch

            # ---------- 3. 下发变声（+ 混音）任务 ----------
            # /翻唱 → rvc_mix：算力机变声后直接混音成 mp3 回传（ffmpeg 全在算力机做）
            # /清唱 → rvc   ：只要人声
            rvc_task_id = f"rvc_{int(time.time())}"
            task_payload = {
                "type": "rvc_mix" if with_inst else "rvc",
                "status": "pending",
                "input": vocal_path,
                "with_inst": bool(with_inst),
                "bitrate": os.getenv("COVER_MP3_BITRATE", "192k"),
                "voice": preset["voice"],
                "pitch": final_pitch,
                "f0_method": preset["f0_method"],
                "index_rate": preset["index_rate"],
                "rms_mix_rate": preset["rms_mix_rate"],
                "protect": preset["protect"],
                "rvc_env": _low_lift_env,   # 低音区抬升（旧两段式也支持）
                "output": "",
            }
            if with_inst:
                # 伴奏走同一个 /api/download，加 ?field=extra_input
                task_payload["extra_input"] = inst_path
            STATUS["tasks"][rvc_task_id] = task_payload
            logger.info(
                f"[SONG] 下发{'翻唱（人声+伴奏）' if with_inst else '清唱（纯人声）'}任务 "
                f"{rvc_task_id} pitch={final_pitch}"
            )

            # RVC + 混音 + 回传 mp3：长歌（6 分钟以上）rmvpe 很吃时间，
            # 原来 150 轮 × 2s = 300s 不够，提到可配的 900s
            rvc_wait_sec = tuning.f("COVER_RVC_WAIT_SEC")
            rvc_rounds = max(30, int(rvc_wait_sec / 2))
            logger.info(f"[SONG] 等待变声完成，上限 {rvc_rounds * 2}s")
            for _ in range(rvc_rounds):
                await asyncio.sleep(2)
                task = STATUS["tasks"].get(rvc_task_id, {})
                if task.get("status") == "completed":
                    voice_output_path = task.get("output")
                    break
                if task.get("status") == "failed":
                    res["error"] = "变声失败"
                    await say("😭 变声失败了。")
                    return res

            if not voice_output_path or not os.path.exists(str(voice_output_path)):
                res["error"] = "变声返回文件丢失"
                await say("😭 变声返回文件丢失。")
                return res

            # ---------- 4. 收成品 ----------
            # ⚠️ 关键：算力机 rvc_mix 已经混好了 mp3，这里**绝不能再混一次**。
            #    之前就是这里无条件调 sync_mix_audio(人声, 伴奏)，
            #    导致 /翻唱 的伴奏被叠了两遍（盖过人声）、/清唱 也被加上了伴奏。
            final_path = Path(str(voice_output_path))
            if final_path.suffix.lower() != ".mp3":
                logger.warning(
                    f"[SONG] 算力机回的是 {final_path.suffix}（不是 mp3），服务器兜底转码"
                )
                out_mp3 = TEMP_AUDIO / f"cover_final_{int(time.time())}.mp3"
                if with_inst and inst_path and os.path.exists(inst_path):
                    # 只有「算力机没混音」的情况才在这里补混
                    logger.info("[SONG] 算力机回的是纯人声 wav，服务器补做混音")
                    ok = await asyncio.to_thread(
                        sync_mix_audio, str(final_path), inst_path, str(out_mp3)
                    )
                else:
                    ok = await asyncio.to_thread(transcode_mp3, str(final_path), str(out_mp3))
                if not ok:
                    res["error"] = "转码/兜底混音失败"
                    await say("🎤 混音炸了……")
                    return res
                final_path = out_mp3

            size_mb = os.path.getsize(final_path) / 1048576 if final_path.exists() else 0
            logger.info(f"[SONG] 成品就绪: {final_path}（{size_mb:.2f} MB）")
            res.update({"ok": True, "path": str(final_path), "size_mb": round(size_mb, 2)})
            return res

        except Exception as e:
            logger.error(f"[SONG] 翻唱异常: {type(e).__name__}: {e}")
            res["error"] = f"{type(e).__name__}: {e}"
            await say(f"😭 翻唱过程中出现异常: {type(e).__name__}")
            return res
        finally:
            # 只清中间文件，成品留给调用方（QQ 发完删 / API 交给 FileResponse）
            logger.info("[SONG] 清理中间文件...")
            for f in (audio_path, vocal_path, inst_path, voice_output_path):
                if not f:
                    continue
                p = Path(f)
                if final_path and p == final_path:
                    continue
                if p.exists():
                    try:
                        await asyncio.to_thread(os.remove, p)
                        logger.info(f"[SONG] 🧹 已清理: {p.name}")
                    except Exception as e:
                        logger.warning(f"[SONG] ⚠️ 清理失败: {p.name} - {e}")
            try:
                if split_task_id:
                    STATUS["tasks"].pop(split_task_id, None)
                if rvc_task_id:
                    STATUS["tasks"].pop(rvc_task_id, None)
            except Exception:
                pass


async def process_cover_task(bot: Bot, event: MessageEvent, song_name: str,
                             with_inst: bool = True, gender_override: str = ""):
    """
    /翻唱 /清唱 的后台任务：跑 run_cover，然后把成品发到 QQ。

    ⚠️ 2026-11-12 修的坑：`_tool_sing` 一直按 5 个位置参数调
    `process_cover_task(bot, ev, song, True, gender)`，但这个函数只收 4 个
    → **每次她主动唱歌都 TypeError**，只能靠手动 `/翻唱` 触发。
    现在把 gender_override 补上并透传给 run_cover。
    """
    async def notify(msg: str):
        await bot.send(event, msg)

    # 最后一道闸：/翻唱 off 期间任何入口（指令、她主动点歌、外部 API）都不许往下走
    if not cover_on():
        logger.info(f"[SONG] 翻唱已关闭，忽略《{song_name}》")
        await notify(_off_hint())
        return

    res = await run_cover(
        song_name, with_inst=with_inst, notify=notify,
        gender_override=gender_override or "",
        deliver={"gid": str(getattr(event, "group_id", "") or ""),
                 "uid": str(getattr(event, "user_id", "") or "")})
    if not res["ok"]:
        return

    final_path = Path(res["path"])
    size_mb = res["size_mb"]
    full_path = Path(res["full_path"]) if res.get("full_path") else None

    # 👇 语音消息有体积上限（QQ 要转 silk 再上传，6MB 以上 retcode=1200 超时）。
    #    但**不压码率** —— 压完音质很烂。
    #    单段式下算力机已经截好了（path=语音版，full_path=完整版），
    #    服务器这里完全不碰 ffmpeg；只有旧两段式才走兜底截断。
    voice_path = final_path
    trimmed_path = None
    trimmed_sec = 0.0
    if full_path and full_path.exists():
        logger.info(
            f"[VOICE] 算力机已截断：语音版 {size_mb:.2f}MB，完整版 "
            f"{os.path.getsize(full_path) / 1048576:.2f}MB（服务器不做音频处理）"
        )
    else:
        voice_max = tuning.f("COVER_VOICE_MAX_MB")
        voice_max_sec = tuning.f("COVER_VOICE_MAX_SEC")
        if size_mb > voice_max:
            trimmed = TEMP_AUDIO / f"voice_{int(time.time())}.mp3"
            ok, trimmed_sec = await asyncio.to_thread(
                trim_for_voice, str(final_path), voice_max, str(trimmed), voice_max_sec,
            )
            if ok:
                voice_path = trimmed
                trimmed_path = trimmed
                size_mb = os.path.getsize(trimmed) / 1048576
            else:
                logger.warning(f"[VOICE] 截断失败，直接试发原文件（{size_mb:.2f}MB）")

    abs_path = os.path.abspath(voice_path)
    sent_ok = False
    last_err = None
    # QQ 语音要先转 silk 再上传，群消息密集（同时有 LLM 在回复）时很容易
    # retcode=1200 "Timeout: NodeIKernelMsgService/sendMsg"。
    # 真机踩过：2.70MB 也失败过，不是体积问题，是 NapCat 当时忙。
    # 所以先重试一次（隔 3 秒让内核喘口气），再降级发文件。
    for attempt in range(1, 3):
        try:
            await bot.send(event, MessageSegment.record(file=f"file://{abs_path}"))
            sent_ok = True
            if attempt > 1:
                logger.info(f"[SONG] 语音第 {attempt} 次发送成功")
            break
        except Exception as e:
            if _is_ack_timeout(e):
                # 消息其实已经送达，只是 ACK 慢 —— 不重试、不刷 error
                logger.info(
                    f"[SONG] 语音已发出（ACK 超时，{size_mb:.2f}MB；"
                    f"NapCat 转码慢导致，可忽略）"
                )
                sent_ok = True
                break
            logger.warning(
                f"[SONG] 语音发送失败（{size_mb:.2f}MB，第 {attempt}/2 次）: "
                f"{type(e).__name__}: {str(e)[:160]}"
            )
            if attempt < 2:
                await asyncio.sleep(5)

    if not sent_ok:
        # 先用一句话探活：如果文字都发不出去，说明 NapCat/QQ 内核本身挂了，
        # 跟音频大小无关，再试文件也是白费，日志直接指出该重启谁。
        try:
            await bot.send(event, "（语音发不出去，改发文件试试…）")
        except Exception as e:
            logger.error(
                f"[SONG] 连文字都发不出去（{type(e).__name__}）—— "
                f"NapCat/QQ 内核异常，不是音频问题。"
                f"排查：docker compose logs napcat --tail 100 / docker compose restart napcat"
            )
            return

        # 语音发不出去时降级发文件，至少让她把成品拿到手
        try:
            await bot.send(event, MessageSegment(
                "file", {"file": f"file://{abs_path}", "name": voice_path.name}
            ))
            sent_ok = True
            logger.info("[SONG] 语音失败，已降级发文件")
        except Exception as e2:
            logger.error(f"[SONG] 文件也发不出去: {type(e2).__name__}: {e2}")
            try:
                await bot.send(
                    event,
                    f"😭 唱是唱完了，但发不出去（{size_mb:.1f}MB）。"
                    f"可能是 NapCat 超时，成品还在服务器上。"
                )
            except Exception:
                pass
            return

    if sent_ok:
        try:
            await bot.send(event, "哼，唱完了。不许笑！" if with_inst else "清唱版，没伴奏，敢笑打你。")
        except Exception:
            pass

        # 截断过的才问要不要整首（没截断的本来就发的是整首）
        if trimmed_path or full_path:
            # 完整版留给用户，语音版发完就删
            keep = full_path if (full_path and full_path.exists()) else final_path
            PENDING_FULL[event.user_id] = {
                "path": str(keep),
                "title": res.get("title") or "",
                "ts": time.time(),
                "with_inst": with_inst,
            }
            _spawn(_expire_pending(event.user_id, FULL_TTL))
            try:
                await bot.send(
                    event,
                    "刚才只唱到高潮。"
                    "要听整首吗？回「要」我把整首发文件给你，回「不要」就算了。",
                )
            except Exception:
                pass
            drop = trimmed_path or (final_path if full_path else None)
            if drop:
                try:
                    drop.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning(f"[SONG] 清理语音版失败: {e}")
            return

        try:
            final_path.unlink(missing_ok=True)
        except Exception as e:
            logger.warning(f"[SONG] 清理成品失败: {e}")


def _is_ack_timeout(e) -> bool:
    """
    判断是不是「消息其实已经发出去了，只是 NapCat 等 ACK 超时」。

    QQ 的 sendMsg 经常这样：NapCat 转 silk 慢（2 分钟音频尤甚），
    QQ 服务端的 onMsgInfoListUpdate 迟迟不回，OneBot 就抛
    `ActionFailed retcode=1200 Timeout: NTEvent ... sendMsg`。
    **但消息通常已经送达**，只是客户端没等到回执。

    这种错误重试没意义、刷 error 也没意义，直接当成功处理。
    """
    s = str(e)
    if "retcode=1200" not in s and "1200" not in s:
        return False
    low = s.lower()
    return "timeout" in low or "超时" in s


# ================= 性别后缀解析 =================
# 支持 `/翻唱 晴天 女`、`/翻唱 晴天 female`、API `{"query": "晴天 男"}`
# 规则：必须是**末尾**、且前面有空白 —— 这样《男孩》《女人花》这种歌名不会被误拆
_GENDER_SUFFIX_RE = re.compile(
    r"[\s,，]+(男|女|男生|女生|男声|女声|male|female)\s*$", re.IGNORECASE
)
_GENDER_WORD_MAP = {
    "男": "male", "男生": "male", "男声": "male", "male": "male",
    "女": "female", "女生": "female", "女声": "female", "female": "female",
}


def split_gender_suffix(text: str) -> tuple:
    """
    把「晴天 女」拆成 ("晴天", "female")。没有后缀返回 (原文, "")。

    ⚠️ 必须要求「末尾 + 前面有空白」：
        `/翻唱 男孩`     → 歌名就是《男孩》，不能拆
        `/翻唱 男孩 男`  → 歌名《男孩》 + 男声
    这个函数同时给 QQ 指令和外部 API 用，两边行为保持一致。
    """
    raw = str(text or "").strip()
    m = _GENDER_SUFFIX_RE.search(raw)
    if not m:
        return raw, ""
    name = raw[:m.start()].strip()
    if not name:            # 只有「女」没有歌名 → 不当后缀处理
        return raw, ""
    return name, _GENDER_WORD_MAP.get(m.group(1).lower(), "")


# ================= 低音区抬升（备用开关，默认关）=================
# 用户 2026-11-12：
#   「低音先改成备用的，比如我输入 /翻唱 *** 低音+2 这样调。
#     因为我听了 crepe 算法的，明显唱出来更好，没有那种下阶梯的感觉，也很自然。」
# → 主问题（句尾下楼梯）已经被 crepe 解决；低音区抬升只作为**手动**备用手段。
#
# 为什么不能整首 +2：RVC 是整体移调，共振峰跟着上移 → 高音哑、整体发尖。
# 所以只抬 F0 低于阈值的帧（实现在算力机 infer/vc/pipeline.py 的 low_lift_gain）。
#
# ⚠️ 副作用：低音到中音的音程会被压扁。原曲 200→350Hz（9.7 半音），
#    抬 2 个半音后变成 224→350Hz（7.8 半音），旋律线的低音部分会被拉平一点。
_LOW_LIFT_RE = re.compile(
    r"[\s,，]+(?:低音|low)\s*([+-]?\d+(?:\.\d+)?)\s*$", re.IGNORECASE
)


def parse_low_lift(text: str) -> tuple:
    """
    `/翻唱 晴天 低音+2` → ("晴天", {"RVC_LOW_LIFT_HZ": "260", ...})。

    没写这个后缀 → 返回 (原文, {})，算力机那边就完全不做低音抬升。
    """
    s = str(text or "")
    m = _LOW_LIFT_RE.search(s)
    if not m:
        return s, {}
    try:
        semis = float(m.group(1))
    except Exception:
        return s, {}
    name = _LOW_LIFT_RE.sub("", s).strip(" \t，,")
    if not name:            # 只有「低音+2」没有歌名 → 不当后缀
        return s, {}
    if abs(semis) < 0.05:   # 低音+0 = 不抬
        return name, {}
    return name, {
        "RVC_LOW_LIFT_HZ": os.getenv("COVER_LOW_LIFT_HZ", "260"),
        "RVC_LOW_LIFT_SEMIS": f"{semis:g}",
        "RVC_LOW_LIFT_FADE": os.getenv("COVER_LOW_LIFT_FADE", "40"),
    }


# ================= 指令入口（瞬间执行完毕，将任务丢入后台） =================
def _extract_song_arg(args: Message) -> str:
    """支持：歌名 / 网易云链接 / 分享文案 / 歌曲 id。"""
    song_name = args.extract_plain_text().strip()
    if not song_name:
        # QQ 的分享卡片里链接在 json/xml 段，extract_plain_text 拿不到，从原始消息里抠
        m = re.search(r"https?://[^\s\]\)\"']+", str(args))
        if m:
            song_name = m.group(0)
    return song_name


_USAGE = (
    "🐟 点歌？发歌名就行，也可以直接甩网易云链接：\n"
    "/翻唱 晴天          → 人声 + 伴奏（自动判男女）\n"
    "/清唱 晴天          → 只要人声，不带伴奏\n"
    "/翻唱 晴天 女       → 直接指定女声参数，跳过自动判断\n"
    "/翻唱 晴天 男       → 直接指定男声参数\n"
    "/翻唱 晴天 低音+2   → 只把低音段抬 2 个半音（备用，默认不用）\n"
    "/翻唱 https://music.163.com/song?id=186016\n"
    "/翻唱 off / on      → 暂停 / 恢复接单（算力机忙时用，主人专属）\n"
    "/测歌 晴天          → 只跑判定链（不翻唱），看性别/音域/变调怎么算的\n"
    "/测歌 上传          → 本地没有的歌：直接发音频文件，切出人声回传\n"
    "/测歌 上传 ab       → 上传后 rmvpe/crepe 各跑一遍，对比成品 F0"
)


# ================= 翻唱总开关 =================
# 用户 2026-11-12：「我想加个 /翻唱 off 和 /翻唱 on，
#                   当算力机繁忙的时候我可以通过这个开关。」
#
# 为什么落盘：算力机一忙可能要几小时，中间 bot 重启（改配置、崩了自动拉起）
# 不能把开关状态丢了 —— 否则重启后一群人继续点歌，算力机直接堵死。
_SWITCH_FILE = Path("data") / "cover_switch.json"
_SWITCH = {"on": True, "by": "", "ts": 0.0}
_SWITCH_ON = ("on", "开", "开启", "恢复", "启用", "1", "true")
_SWITCH_OFF = ("off", "关", "关闭", "停", "暂停", "停一下", "0", "false")


def _load_switch():
    try:
        if _SWITCH_FILE.exists():
            _d = json.loads(_SWITCH_FILE.read_text(encoding="utf-8"))
            if isinstance(_d, dict):
                _SWITCH["on"] = bool(_d.get("on", True))
                _SWITCH["by"] = str(_d.get("by") or "")
                _SWITCH["ts"] = float(_d.get("ts") or 0)
                if not _SWITCH["on"]:
                    logger.warning(f"[SONG] 翻唱开关是关闭状态（{_SWITCH['by'] or '?'} 关的），"
                                   f"发 /翻唱 on 恢复")
    except Exception as e:
        logger.warning(f"[SONG] 读翻唱开关失败: {type(e).__name__}: {e}")


def cover_on() -> bool:
    return bool(_SWITCH.get("on", True))


def set_cover(on: bool, by: str = "") -> None:
    _SWITCH["on"] = bool(on)
    _SWITCH["by"] = str(by or "")
    _SWITCH["ts"] = time.time()
    try:
        _SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SWITCH_FILE.write_text(json.dumps(_SWITCH, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[SONG] 写翻唱开关失败: {type(e).__name__}: {e}")
    logger.info(f"[SONG] 翻唱开关 → {'on' if on else 'off'}（by {by}）")


def _parse_switch(text: str):
    """`off` / `关` / `暂停` → False；`on` / `开` → True；其它 → None。"""
    t = str(text or "").strip().lower()
    if not t:
        return None
    head = re.split(r"[\s,，]+", t)[0]
    if head in _SWITCH_OFF:
        return False
    if head in _SWITCH_ON:
        return True
    return None


def _off_hint() -> str:
    _by = _SWITCH.get("by") or "主人"
    return (f"🛑 翻唱现在是关着的（{_by} 关的，算力机忙）\n"
            f"   等忙完让主人发 /翻唱 on")


_load_switch()


@cover.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    song_name = _extract_song_arg(args)

    # ---- /翻唱 on | off：算力机繁忙时手动暂停接单（主人/管理员专属）----
    _sw = _parse_switch(song_name)
    if _sw is not None:
        from services import access
        if access.level_of(event.user_id) == access.LEVEL_MEMBER:
            await bot.send(event, "这个开关只有主人或管理员能动。")
            return
        set_cover(_sw, by=str(event.user_id))
        if _sw:
            await bot.send(event, "✅ 翻唱已恢复 —— /翻唱、/清唱、/测歌 都能用了")
        else:
            await bot.send(event, "🛑 翻唱已暂停。\n"
                                  "   期间 /翻唱、/清唱、/测歌 都不接单，她也不会自己点歌。\n"
                                  "   算力机忙完发 /翻唱 on 恢复（重启 bot 也不会丢）")
        return

    if not cover_on():
        await bot.send(event, _off_hint())
        return

    if not song_name:
        await bot.send(event, _USAGE)
        return
    # 中间过程不再一条条播报，只在开头给一句、唱完给一句
    await bot.send(event, "好吧，你要等等哦，现在就唱给你听~")
    asyncio.create_task(process_cover_task(bot, event, song_name, with_inst=True))


@acapella.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if not cover_on():
        await bot.send(event, _off_hint())
        return
    song_name = _extract_song_arg(args)
    if not song_name:
        await bot.send(event, _USAGE)
        return
    await bot.send(event, "清唱啊……行吧，我清清嗓子~")
    asyncio.create_task(process_cover_task(bot, event, song_name, with_inst=False))


# ================= /测歌：只跑判定链，不翻唱 =================
def _fmt_hz(v) -> str:
    try:
        return f"{float(v or 0):.0f}"
    except Exception:
        return "0"


async def _resolve_song_meta(song_name: str) -> dict:
    """解析歌名/链接 → {ok,id,title,singer,url,error}。"""
    async with httpx.AsyncClient(timeout=15) as client:
        wanted_id = await resolve_song_id(song_name)
        songs = []
        if wanted_id:
            try:
                d = await client.get(f"{NETEASE_API}/song/detail",
                                     params={"ids": wanted_id})
                songs = d.json().get("songs") or []
            except Exception as e:
                logger.warning(f"[PROBE] 查详情失败: {type(e).__name__}: {e}")
        if not songs:
            try:
                r = await client.get(f"{NETEASE_API}/search",
                                     params={"keywords": song_name, "limit": 1})
                songs = r.json().get("result", {}).get("songs") or []
            except Exception as e:
                logger.warning(f"[PROBE] 搜索失败: {type(e).__name__}: {e}")
        if not songs:
            return {"ok": False, "error": f"找不到《{song_name}》"}
        sid = str(songs[0]["id"])
        title = songs[0].get("name") or song_name
        singer = (songs[0].get("artists") or [{}])[0].get("name", "")
        level = (os.getenv("COVER_SOURCE_LEVEL", "exhigh") or "exhigh").strip()
        url = ""
        for p, params in (
            ("/song/url/v1", {"id": sid, "level": level, "cookie": NETEASE_COOKIE}),
            ("/song/url", {"id": sid, "cookie": NETEASE_COOKIE}),
        ):
            try:
                d = await client.get(f"{NETEASE_API}{p}", params=params)
                url = (d.json().get("data") or [{}])[0].get("url") or ""
            except Exception as e:
                logger.warning(f"[PROBE] {p} 取地址失败: {type(e).__name__}: {e}")
            if url:
                break
        return {"ok": True, "id": sid, "title": title, "singer": singer, "url": url}


# ---------- 公共：等结果 / 渲染报告 / 发成品 ----------
async def _wait_probe_out(task_id: str, wait: float) -> dict:
    """轮询算力机结果，返回 dict（可能为空 = 超时）。"""
    from plugins.tool_dispatcher import STATUS
    out: dict = {}
    for _ in range(max(30, int(wait / 2))):
        await asyncio.sleep(2)
        t = STATUS["tasks"].get(task_id, {})
        if t.get("status") in ("completed", "failed"):
            out = t.get("output") or {}
            break
    return out if isinstance(out, dict) else {}


def _probe_report_lines(out: dict) -> list:
    """把算力机回传的判定数据渲染成一屏。"""
    _src = {"override": "L1 用户指定", "singer": "L2 歌手判定",
            "qwen": "L3 Qwen-Audio 兜底", "f0": "F0 分布",
            "f0-dominant": "F0 主导声部"}.get(out.get("gender_source"),
                                              out.get("gender_source"))
    _mr = out.get("model_range") or [0, 0]
    _pitch = out.get("pitch")
    _pitch_s = f"{int(_pitch):+d}" if isinstance(_pitch, (int, float)) else str(_pitch)
    _extra = ""
    if out.get("use_exact"):
        try:
            _extra = f"（精确模式 {float(out.get('exact') or 0):+.2f} 半音）"
        except Exception:
            _extra = "（精确模式）"

    lines = [
        "🔬 测歌结果",
        f"② 算力机判定：{out.get('verdict')}（来源 {_src}）",
        f"   F0 原始：{out.get('f0_verdict')}｜{out.get('f0_reason')}",
        f"   音域 min={_fmt_hz(out.get('min'))} p1={_fmt_hz(out.get('p1'))} "
        f"p10={_fmt_hz(out.get('p10'))} p50={_fmt_hz(out.get('p50'))} "
        f"p90={_fmt_hz(out.get('p90'))} p99={_fmt_hz(out.get('p99'))} "
        f"max={_fmt_hz(out.get('max'))}Hz",
        f"   帧数 {out.get('frames')}｜<220Hz 占 {out.get('low_pct')}%"
        f"｜模型音域 {_fmt_hz(_mr[0])}-{_fmt_hz(_mr[1])}Hz",
        f"③ 源判定：{'女声' if out.get('src_female') else '男声/未知'}"
        f"｜升调门槛 {_fmt_hz(out.get('lift_floor'))}Hz"
        f"｜采用 {_fmt_hz(out.get('used_lo'))}-{_fmt_hz(out.get('used_hi'))}Hz",
        f"④ 变调 pitch={_pitch_s}{_extra}",
        f"   {out.get('reason')}",
    ]
    ab = out.get("ab") or {}
    if isinstance(ab, dict) and ab:
        lines.append("⑤ F0 提取 A/B（同一段人声、同 pitch、同 RVC 参数，只换算法）")
        for _m, _d in ab.items():
            _d = _d if isinstance(_d, dict) else {}
            if not _d.get("ok"):
                lines.append(f"   {_m}：失败（{_d.get('error') or '无数据'}）")
                continue
            lines.append(
                f"   {_m}：p1={_fmt_hz(_d.get('p1'))} p50={_fmt_hz(_d.get('p50'))} "
                f"p99={_fmt_hz(_d.get('p99'))} max={_fmt_hz(_d.get('max'))}"
                f"｜<220Hz 占 {_d.get('low_pct')}%")
    return lines


async def _send_probe_files(bot: Bot, event: MessageEvent, out: dict):
    """把算力机回传的成品（人声 / A/B 两个版本）当文件发出去。"""
    files = out.get("files") or []
    if not files:
        return
    from nonebot.adapters.onebot.v11 import GroupMessageEvent
    for _p in files:
        fp = Path(str(_p))
        if not fp.exists():
            logger.warning(f"[PROBE] 成品文件不见了：{fp}")
            continue
        try:
            if isinstance(event, GroupMessageEvent):
                await bot.call_api("upload_group_file", group_id=event.group_id,
                                   file=str(fp.absolute()), name=fp.name)
            else:
                await bot.call_api("upload_private_file", user_id=event.user_id,
                                   file=str(fp.absolute()), name=fp.name)
        except Exception as e:
            logger.warning(f"[PROBE] 上传成品失败 {fp.name}: {type(e).__name__}: {e}")
        finally:
            try:
                fp.unlink(missing_ok=True)
            except Exception:
                pass


# ---------- 上传模式：本地没有的歌，直接发文件 ----------
_AUDIO_EXT = (".mp3", ".flac", ".wav", ".m4a", ".ogg", ".aac", ".opus", ".wma")
# user_id -> {"gender": "female"/"", "ab": bool, "ts": float}
PROBE_WAIT_UPLOAD: dict = {}
PROBE_UPLOAD_TTL = tuning.f("PROBE_UPLOAD_TTL")
_PROBE_UP_WORDS = ("上传", "upload", "传歌", "发歌")


def _probe_audio_seg(event: MessageEvent):
    """从消息里找音频 file 段（QQ 发「文件」，不是语音条）。"""
    for seg in event.get_message():
        if seg.type != "file":
            continue
        _nm = str(seg.data.get("file") or seg.data.get("name") or "").lower()
        if _nm.endswith(_AUDIO_EXT):
            return seg
    return None


async def _download_qq_file(bot: Bot, event: MessageEvent, seg, dest: Path) -> bool:
    """file 段 → 本地文件。优先 NapCat 本地路径（同机秒传），否则走 url 下载。"""
    _fid = seg.data.get("file_id")
    _url = seg.data.get("url") or ""
    if _fid:
        try:
            res = await bot.call_api("get_file", file_id=_fid)
            _url = res.get("url") or _url
            _local = res.get("file")
            if _local and os.path.exists(_local):
                shutil.copy(_local, dest)
                logger.info(f"[PROBE] NapCat 本地文件直接复制：{_local}")
                return dest.exists() and dest.stat().st_size > 1024
        except Exception as e:
            logger.warning(f"[PROBE] get_file 失败: {type(e).__name__}: {e}")
    if not _url:
        return False
    try:
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as c:
            async with c.stream("GET", _url) as r:
                if r.status_code != 200:
                    logger.warning(f"[PROBE] 下载文件 HTTP {r.status_code}")
                    return False
                with open(dest, "wb") as f:
                    async for chunk in r.aiter_bytes(1024 * 1024):
                        f.write(chunk)
    except Exception as e:
        logger.warning(f"[PROBE] 下载文件异常: {type(e).__name__}: {e}")
        return False
    return dest.exists() and dest.stat().st_size > 1024


async def _probe_upload_rule(event: MessageEvent) -> bool:
    info = PROBE_WAIT_UPLOAD.get(event.user_id)
    if not info:
        return False
    if time.time() - float(info.get("ts") or 0) > PROBE_UPLOAD_TTL:
        PROBE_WAIT_UPLOAD.pop(event.user_id, None)
        return False
    return _probe_audio_seg(event) is not None


probe_upload = on_message(rule=Rule(_probe_upload_rule), priority=1, block=True)


@probe_upload.handle()
async def _(bot: Bot, event: MessageEvent):
    info = PROBE_WAIT_UPLOAD.pop(event.user_id, None) or {}
    seg = _probe_audio_seg(event)
    if seg is None:
        await probe_upload.finish()
    if not cover_on():
        await bot.send(event, _off_hint())
        return

    raw_name = str(seg.data.get("file") or seg.data.get("name") or "upload.mp3")
    ext = Path(raw_name).suffix or ".mp3"
    dest = TEMP_AUDIO / f"probe_up_{event.user_id}_{int(time.time())}{ext}"
    await bot.send(event, "📥 收到，正在从 QQ 取文件…")
    if not await _download_qq_file(bot, event, seg, dest):
        await bot.send(event, "❌ 取文件失败（链接过期或文件太大）。再发一次试试")
        return
    _mb = dest.stat().st_size / 1048576
    _ab = bool(info.get("ab"))
    _title = Path(raw_name).stem
    logger.info(f"[PROBE] 上传落盘 {dest.name}（{_mb:.1f} MB）"
                f"ab={_ab}｜标题《{_title}》")

    # L1（用户在指令里写「女/男」）/ L2（歌名→歌手，多半判不出）→ 否则 L3 算力机兜底
    _g = await resolve_gender_chain(_title, "", info.get("gender") or "")
    _key = _g["gender"] if _g["gender"] in VOICE_PRESETS else DEFAULT_PRESET_KEY
    from plugins.tool_dispatcher import STATUS
    task_id = f"probe_{int(time.time())}"
    _task = {
        "type": "probe", "status": "pending",
        "input": str(dest), "source_url": "",
        "gender_hint": _g["gender"] or "",
        "gender_lock": _g["gender"] or "",
        "gender_source": (_g["source"]
                          if _g["source"] in ("override", "singer") else ""),
    }
    _task.update(_probe_task_payload(_g["gender"] or "", _ab))
    STATUS["tasks"][task_id] = _task
    logger.info(f"[PROBE] 下发上传测歌任务 {task_id}｜gender={_g['gender'] or '待定'}"
                f"（{_g['source']}）｜ab={_ab}")
    await bot.send(event, "\n".join([
        f"🎵 已交给算力机（{_mb:.1f} MB，{_key} 参数组）",
        "   ① Demucs 分离人声 → ② 全曲均分 100s 测音域 → ③ 判性别/变调",
        ("   ④ rmvpe / crepe 各跑一遍 RVC，成品和 F0 一起回给你"
         if _ab else "   ④ 回传分离出的人声"),
        "   大歌 3~10 分钟，别急",
    ]))

    _wait = float(os.getenv("PROBE_AB_WAIT_SEC" if _ab else "PROBE_WAIT_SEC",
                            "2400" if _ab else "900"))
    out = await _wait_probe_out(task_id, _wait)
    try:
        STATUS["tasks"].pop(task_id, None)
    except Exception:
        pass
    try:
        dest.unlink(missing_ok=True)
    except Exception:
        pass

    if out.get("error"):
        logger.warning(f"[PROBE] 上传测歌失败 {task_id}: {out['error']}")
        await bot.send(event, f"❌ 测歌失败：{out['error']}")
        return
    if not out:
        await bot.send(event, f"❌ 等了 {_wait:.0f}s 没结果，看算力机日志")
        return
    report = _probe_report_lines(out)
    logger.info("[PROBE] 上传测歌完成 → " + " ｜ ".join(report[1:]))
    await bot.send(event, "\n".join(report))
    await _send_probe_files(bot, event, out)


# `ab` 当**独立词**看（\bab\b）：歌名里带 ab 的（比如 "Abbey Road"）不该被误吃
_PROBE_AB_RE = re.compile(r"(?i)(?<![a-z])ab(?![a-z])")


def _probe_strip_flags(raw: str) -> tuple:
    """
    从参数里剥掉控制词，返回 (剩下的歌名或链接, 要不要 A/B)。

    ⚠️ 2026-11-12 真机踩过：用户发
    `/测歌 上传 ab 分享…的单曲《月声尽，心灯明》: https://163cn.tv/bkOZu6Mv`
    —— 老代码只要看到「上传」两个字就进上传模式，**把链接丢了**，白等一个文件。
    正确做法：控制词是**修饰**，剥掉之后还有东西（歌名/链接）就走网易云那条路。
    """
    s = str(raw or "")
    _ab = bool(_PROBE_AB_RE.search(s)) or ("对比" in s)
    s = _PROBE_AB_RE.sub(" ", s).replace("对比", " ")
    for _w in _PROBE_UP_WORDS:
        s = s.replace(_w, " ")
    return s.strip(" \t\r\n，,、:："), _ab


def _probe_task_payload(gender: str, ab: bool) -> dict:
    """测歌任务里和 A/B 相关的字段（两条入口共用，保证参数和真翻唱一致）。"""
    _key = gender if gender in VOICE_PRESETS else DEFAULT_PRESET_KEY
    _ps = VOICE_PRESETS[_key]
    return {
        "want_vocal": not ab,
        "f0_ab": ["rmvpe", "crepe"] if ab else [],
        # A/B 必须用和真翻唱一模一样的 RVC 参数，否则对比没有意义
        "index_rate": _ps["index_rate"],
        "rms_mix_rate": _ps["rms_mix_rate"],
        "protect": _ps["protect"],
    }


@song_probe.handle()
async def _(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    # /翻唱 off 期间测歌也停 —— 它一样要 Demucs + RVC，占的是同一台算力机
    if not cover_on():
        await bot.send(event, _off_hint())
        return
    raw, _ab = _probe_strip_flags(_extract_song_arg(args))
    _name, _suffix = split_gender_suffix(raw) if raw else ("", "")

    # 「/测歌 上传 女」剥完只剩一个性别词 —— split_gender_suffix 故意不拆单个词
    # （否则《男孩》会被当性别），这里自己兜一下，否则会去网易云搜一个叫「女」的歌。
    _bare_gender = _GENDER_WORD_MAP.get(raw.lower(), "")
    if _bare_gender:
        _suffix = _bare_gender
        raw = ""

    # ---- 剥完控制词什么都不剩 → 上传模式 ----
    if not raw:
        PROBE_WAIT_UPLOAD[event.user_id] = {
            "gender": _suffix, "ab": _ab, "ts": time.time()}
        logger.info(f"[PROBE] 等待上传 user={event.user_id} "
                    f"gender={_suffix or '自动'} ab={_ab}")
        await bot.send(event, "\n".join([
            "🔬 测歌（上传模式）已就绪 —— 直接把音频文件发过来（QQ 发「文件」，不是语音条）",
            f"   格式：{'/'.join(e.lstrip('.') for e in _AUDIO_EXT)}",
            f"   性别：{_suffix or '自动判定'}"
            f"｜{'跑 rmvpe/crepe A/B 对比' if _ab else '只回传分离出的人声'}",
            f"   {PROBE_UPLOAD_TTL / 60:.0f} 分钟内有效",
            "",
            "其它用法：",
            "/测歌 晴天          → 按歌名从网易云找",
            "/测歌 <网易云链接>   → 短链 163cn.tv 也认",
            "/测歌 月声尽 ab      → 网易云下载 + crepe/rmvpe 对比，成品发回来",
            "/测歌 上传 ab        → 本地没有的歌：发文件后跑对比",
        ]))
        return

    if _suffix:
        logger.info(f"[PROBE] 参数里的性别后缀：{_suffix}，歌名《{_name}》")
        raw = _name
    _eta = "3~10 分钟" if _ab else "1~3 分钟"
    await bot.send(event, f"🔬 开始测《{raw}》…下载 + 分离人声"
                          + ("，再用 rmvpe/crepe 各跑一遍 RVC" if _ab else "")
                          + f"，大概 {_eta}")

    meta = await _resolve_song_meta(raw)
    if not meta.get("ok"):
        await bot.send(event, f"😭 {meta.get('error')}")
        return
    if not meta.get("url"):
        await bot.send(event, "😭 这首取不到音频地址（可能被锁），换个版本试试")
        return

    # ---- 服务器侧：L1 用户指定 / L2 歌手判定 ----
    g = await resolve_gender_chain(meta["title"], meta["singer"], _suffix)
    _lab = {"override": "L1 用户指定", "singer": "L2 歌手判定",
            "none": "L3 交给算力机（均分100s + Qwen-Audio）"}.get(g["source"], g["source"])
    logger.info(f"[PROBE] 《{meta['title']}》{_lab} → gender={g['gender'] or '(待定)'}"
                f"｜{g['reason']}")
    await bot.send(event, "\n".join([
        f"🎵《{meta['title']}》— {meta['singer'] or '未知歌手'}（id {meta['id']}）",
        f"① {_lab}",
        f"   → {g['gender'] or '(未定，走算力机兜底)'}｜{g['reason']}",
    ]))

    # ---- 算力机：分离 + 音域 + L3 兜底（+ 可选 A/B）----
    from plugins.tool_dispatcher import STATUS
    task_id = f"probe_{int(time.time())}"
    _task = {
        "type": "probe", "status": "pending",
        "input": "", "source_url": meta["url"],
        "gender_hint": g["gender"] or "",
        "gender_lock": g["gender"] or "",
        "gender_source": (g["source"]
                          if g["source"] in ("override", "singer") else ""),
    }
    _task.update(_probe_task_payload(g["gender"] or "", _ab))
    STATUS["tasks"][task_id] = _task
    logger.info(f"[PROBE] 下发测歌任务 {task_id}｜gender_lock={g['gender'] or '无'}"
                f"（{g['source']}）｜ab={_ab}")
    await bot.send(event, "⏳ 已交给算力机：分离人声 → 全曲均分 100s 测音域 → 判性别"
                          + ("→ rmvpe/crepe 各跑一遍" if _ab else "") + "…")

    _wait = float(os.getenv("PROBE_AB_WAIT_SEC" if _ab else "PROBE_WAIT_SEC",
                            "2400" if _ab else "900"))
    out = await _wait_probe_out(task_id, _wait)
    try:
        STATUS["tasks"].pop(task_id, None)
    except Exception:
        pass
    if out.get("error"):
        logger.warning(f"[PROBE] 测歌失败 {task_id}: {out['error']}")
        await bot.send(event, f"❌ 测歌失败：{out['error']}")
        return
    if not out:
        await bot.send(event, f"❌ 等了 {_wait:.0f}s 没结果，看算力机日志")
        return

    report = _probe_report_lines(out)
    logger.info("[PROBE] 测歌完成 → " + " ｜ ".join(report[1:]))
    await bot.send(event, "\n".join(report))
    await _send_probe_files(bot, event, out)
