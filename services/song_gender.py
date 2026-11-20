# services/song_gender.py
"""
判断原唱性别 + 音高，决定 RVC 变声参数。

两条独立的决策链，因为它们的**时机不同**：

【性别】下载音频之前就要定（决定音色和初版 pitch）
  L1  DeepSeek 常识判断              快（<1s），覆盖绝大多数知名歌手
  L2  Tavily 联网搜索 + DeepSeek 归纳  冷门歌手 / 虚拟歌手 / 只有翻唱版
  L3  留空 → 等分离出人声后用音频直接判（见 probe_vocal）

【音高】分离出人声之后才能定（听声音最准，比猜歌手可靠得多）
  · estimate_f0()      本地 ffmpeg 重采样 + 自相关算基频中位数（主力，零依赖）
  · qwen_audio_probe() 阿里百炼 Qwen-Audio 交叉验证（可选，失败不影响流程）
  · pitch_from_f0()    半音数 = 12·log2(目标基频 / 源基频)

为什么要自适应 pitch：原版是「男声 -12 / 女声 +12」硬编码，
但《晴天》搜到的是 RyaVocal 的翻唱版（本来就是女声），
再 +12 就飞到天上去了。直接测基频才靠谱。
"""
from core import tuning
import os
import re
import json
import math
import wave
import array
import base64
import asyncio
import tempfile
import statistics
import subprocess
from pathlib import Path

import httpx
from nonebot import logger

# 👇 适配你现有的环境变量名
DEEPSEEK_API_KEY = os.getenv("XAI_API_KEY", os.getenv("DEEPSEEK_API_KEY", ""))
DEEPSEEK_BASE_URL = os.getenv("XAI_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_API = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"

DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "").rstrip("/")
# Qwen-Audio 模型候选（百炼上换名字比较勤，逐个试）
# Qwen-Audio-3.0-ASR 是用户在控制台确认可用的那个；大小写/连字符变体都排上，
# 端点不认就自动往下试，全失败也只打 warning（本地 F0 才是主力）
QWEN_AUDIO_MODELS = [
    m.strip() for m in os.getenv(
        "QWEN_AUDIO_MODEL",
        "Qwen-Audio-3.0-ASR,qwen-audio-3.0-asr,qwen3-asr-flash,"
        "qwen-audio-turbo-latest,qwen-audio-turbo,qwen2-audio-instruct",
    ).split(",") if m.strip()
]

# 目标音色（kikiv2 女声）的典型基频中位数。自适应 pitch 的基准点。
TARGET_F0 = tuning.f("RVC_TARGET_F0")

# ================= 音色模型的音域（Hz）=================
# 为什么要这个：kiki.pth 这类 RVC 音色模型的音域是**有限**的。
# 源歌人声一旦超出（比如 A7 ≈ 3520Hz 的高音歌），RVC 输出会全程破音/发哑。
# 所以真正该做的是：把人声的**最低/最高音**整体移调，塞进模型音域，
# 而不是先判断男女再 ±12 —— 性别判定只决定音色，不决定能不能唱上去。
VOICE_F0_MIN = tuning.f("RVC_VOICE_F0_MIN")
VOICE_F0_MAX = tuning.f("RVC_VOICE_F0_MAX")


def pitch_for_range(lo: float, hi: float,
                    model_min: float = 0, model_max: float = 0) -> tuple:
    """
    按「这首歌人声的最低/最高音」算移调，让它整段落进音色模型的音域。

    参数：
      lo, hi      人声基频的 p10 / p90（不要用 min/max，会被个别异常帧带偏）
      model_min/max  音色模型音域，默认读 RVC_VOICE_F0_MIN / MAX

    返回 (半音数, 说明)

    逻辑：
      设移调 s 半音后，音域变成 [lo·2^(s/12), hi·2^(s/12)]，要求它 ⊆ [MIN, MAX]
        s ≥ 12·log2(MIN/lo)   （低音不能低于下限）
        s ≤ 12·log2(MAX/hi)   （高音不能高于上限）
      如果 0 落在这个区间里 → 保持原调（这是最好的情况，不动它）。
      如果人声跨度比模型音域还宽 → 塞不进去，**保低音**（低音优先，见下）。
    """
    mmin = float(model_min or VOICE_F0_MIN)
    mmax = float(model_max or VOICE_F0_MAX)
    lo, hi = float(lo or 0), float(hi or 0)

    if lo <= 0 or hi <= 0 or hi < lo:
        return 0, "音域数据无效，保持原调"
    if mmin <= 0 or mmax <= mmin:
        return 0, "模型音域配置无效，保持原调"

    # 人声跨度比模型音域还宽 → 无论怎么移都会有一头超出。
    # ⚠️ 只救低音（与算力机 node.py 的 pitch_for_range 保持一致）：
    #    低音掉到下限以下会直接变回男声，高音顶破上限只是那几句紧/略破。
    if (hi / lo) > (mmax / mmin):
        s = 12 * math.log2(mmin / lo) if lo < mmin else 12 * math.log2(mmax / hi)
        semi = int(math.ceil(s) if s > 0 else math.floor(s))
        return (semi,
                f"人声跨度 {lo:.0f}-{hi:.0f}Hz 比模型音域 {mmin:.0f}-{mmax:.0f}Hz 还宽，"
                f"只保低音 → {semi:+d} 半音（高音会超上限，优先保证女声）")

    s_lo = 12 * math.log2(mmin / lo)      # 至少要升这么多
    s_hi = 12 * math.log2(mmax / hi)      # 最多只能升这么多

    if s_lo <= 0 <= s_hi:
        return 0, f"人声 {lo:.0f}-{hi:.0f}Hz 已在模型音域 {mmin:.0f}-{mmax:.0f}Hz 内，保持原调"

    s = s_lo if s_lo > 0 else s_hi
    semi = int(math.ceil(s) if s > 0 else math.floor(s))
    return (semi,
            f"人声 {lo:.0f}-{hi:.0f}Hz → 移调 {semi:+d} 半音，"
            f"落进模型音域 {mmin:.0f}-{mmax:.0f}Hz")
# 自适应 pitch 的上下限，防止测错把歌毁了
PITCH_MIN = tuning.i("RVC_PITCH_MIN")
PITCH_MAX = tuning.i("RVC_PITCH_MAX")
# 源是女声时的移调上限（半音）。女→女只做音色转换，不该大幅降调，见 pitch_from_f0
SAME_GENDER_MAX_SEMIS = tuning.i("RVC_SAME_GENDER_MAX_SEMIS")
# 自相关归一化峰值门槛：低于它的帧算「无周期性」（气声/噪声/静音），直接丢弃。
# 气声唱法的基频被宽带噪声淹没，不设这个门槛就会测到噪声尖峰 → 基频偏高 → pitch 偏小 → 唱得低。
# 0.45 是实测定的：8kHz/40ms 窗下，纯白噪声 78 个 lag 的归一化峰值 max≈0.41、p90≈0.35，
# 而真人浊音（元音）通常在 0.6~0.95。取 0.45 刚好把噪声挡在外面、又不误杀正常唱段。
PERIODICITY_MIN = tuning.f("F0_PERIODICITY_MIN")

_SYS_PROMPT = """你是音乐信息助手。用户会给你一个歌曲名和歌手名（可能为空），
你只需判断这首歌的原唱歌手性别。只输出 JSON：
{"gender": "male" 或 "female" 或 "unknown", "singer": "歌手名", "reason": "一句话理由"}
不要输出任何其他文字。"""


# ============================================================
# 音高换算
# ============================================================
def pitch_from_f0(source_f0: float, target_f0: float = None,
                  gender: str = "") -> int:
    """
    把「源基频」换算成 RVC 需要的半音数。

    半音数 = 12 · log2(目标基频 / 源基频)
      男声 120Hz → 目标 220Hz → +10.5 → +10 半音
      女声 210Hz → 目标 220Hz → +0.8  → +1 半音
    比硬编码 ±12 精确得多，翻唱版/童声/特殊音色都不会跑偏。

    ⚠️ 为什么女声必须单独限幅（真机踩过，用户原话「气声一多就唱得很低」）：
      `f0_up_key` 本质是「变调半音数」，不是「对齐到某个基准」。
      源是女声、目标音色模型也是女声时，正确做法是 pitch≈0 —— 只换音色，不改调。
      但空灵/气声类女声基频常在 260~340Hz，按「一律对齐 220Hz」算会得到
      -5~-7 半音，整首歌被压低大半个音区，听起来就是「唱得很低」。
      越空灵（基频越高）→ 压得越狠 → 越烂，正好对上用户的体感。
      所以同性别（女→女）只允许 ±SAME_GENDER_MAX_SEMIS 的微调。
    """
    target = float(target_f0 or TARGET_F0)
    try:
        src = float(source_f0)
    except Exception:
        return 0
    if src <= 0 or target <= 0:
        return 0
    semis = 12.0 * math.log2(target / src)
    # 只有调用方显式给了 target_f0 才走「硬对齐」；否则女声/未知一律保持原调。
    # 真机踩过：女声歌 312Hz 被判 unknown → 退回 preset +12 → 整首升八度、极哑。
    g = str(gender or "").lower()
    if not target_f0 and g != "male":
        if 150.0 <= src <= 400.0:
            return 0          # 模型音域内 → 只换音色，不改调
        if src >= 150.0:
            semis = max(-SAME_GENDER_MAX_SEMIS, min(SAME_GENDER_MAX_SEMIS, semis))
        # src < 150 说明其实是男声（只是没判出来）→ 保留公式，抬到女声音域
    return int(max(PITCH_MIN, min(PITCH_MAX, round(semis))))


def f0_to_gender(f0: float) -> str:
    """粗分男女：基频中位数 > 165Hz 视为女声（人声学常用分界）。"""
    try:
        v = float(f0)
    except Exception:
        return "unknown"
    if v <= 0:
        return "unknown"
    if v >= 185:
        return "female"
    if v <= 155:
        return "male"
    return "unknown"


def f0_to_pitch_hint(f0: float) -> str:
    """粗分音区，给日志和 Qwen-Audio 交叉验证用。"""
    try:
        v = float(f0)
    except Exception:
        return "unknown"
    if v <= 0:
        return "unknown"
    if v < 130:
        return "low"
    if v < 210:
        return "mid"
    return "high"


# ============================================================
# 本地基频估算（主力）
# ============================================================
def f0_search_range(gender_hint: str = "") -> tuple:
    """
    按性别先验限定基频搜索范围，用来压制倍频/半频错误。

    真机踩过：周杰伦《晴天》测出 228.6Hz —— 正好是真实基频（~115Hz）的两倍。
    伴奏残留的高频谐波会在 T/2 处造出一个够高的局部峰，先验范围一限就没了。
    """
    g = str(gender_hint or "").lower()
    if g == "male":
        return 70.0, 200.0     # 男声典型 80~180
    if g == "female":
        return 150.0, 450.0    # 女声典型 165~350
    return 70.0, 450.0


def f0_from_samples(a, sr: int, fmin: float = 70.0, fmax: float = 450.0) -> float:
    """
    对单声道 16bit 采样做分帧自相关，返回基频中位数（Hz）。0 表示测不出。

    抽成独立函数是为了能在没有 ffmpeg 的机器上直接用合成波形做单元测试。

    为什么不用 librosa：镜像里没有，装 numpy+librosa 要 100MB+，
    而 8kHz 下「40ms 窗 × 94 个候选 lag」的纯 Python 自相关只要 1~2 秒，
    对一个本来就要跑 3 分钟的翻唱来说完全无感。
    """
    try:
        if not a or sr <= 0:
            return 0.0

        win = int(sr * 0.04)      # 40ms 窗
        hop = int(sr * 0.04)      # 40ms 步进
        min_lag = max(2, int(sr / max(50.0, float(fmax))))
        max_lag = max(min_lag + 4, int(sr / max(50.0, float(fmin))))
        if len(a) < win + max_lag + hop:
            return 0.0

        # 能量门限：滤掉静音和间奏，只测真正在唱的部分
        step = max(1, len(a) // 20000)
        sample = a[::step]
        rms = math.sqrt(sum(x * x for x in sample) / max(1, len(sample)))
        if rms <= 0:
            return 0.0
        thr = (rms * 0.6) ** 2 * win

        f0s = []
        n_lag = max_lag - min_lag
        for start in range(0, len(a) - win - max_lag, hop):
            frame = a[start:start + win]
            energy = sum(x * x for x in frame)
            if energy < thr:
                continue

            corr = []
            for lag in range(min_lag, max_lag):
                c = 0
                for i in range(0, win, 4):
                    c += frame[i] * a[start + i + lag]
                # 归一化成相关系数（≈[-1,1]），这样「峰有多高」才是周期性的真实度量。
                # 不归一化的话，气声段因为整体能量大，噪声尖峰也能当上最大值。
                # ×4 是因为 c 只累加了 win/4 个采样点，先还原到全窗尺度再除能量。
                corr.append(c * 4.0 / energy)

            best = max(corr)
            # 👇 气声/清音/噪声帧的周期性很差，归一化峰值上不去，直接丢掉。
            #    只留真正有稳定周期的浊音帧（元音），基频才不会被噪声带偏。
            if best < PERIODICITY_MIN:
                continue
            # ⚠️ 不能直接取全局最大 lag —— 会掉进「倍频陷阱」：
            #    220Hz 的周期是 36.36 个采样，整数 lag 取 36 有 0.36 的误差，
            #    而 3 倍周期 lag=109 只差 0.09，相关值反而更高，
            #    结果测出 73Hz（三分之一）。所以要取「第一个够高的局部峰」。
            peak_thr = best * 0.9
            best_lag = 0
            for k in range(1, n_lag - 1):
                if corr[k] >= peak_thr and corr[k] >= corr[k - 1] and corr[k] >= corr[k + 1]:
                    best_lag = min_lag + k
                    break
            if best_lag:
                f0s.append(sr / best_lag)

        if not f0s:
            logger.info(
                f"[F0] 没有帧达到周期性门槛 {PERIODICITY_MIN}"
                "（气声/清音为主，或整段没在唱）"
            )
            return 0.0

        med = statistics.median(f0s)
        good = [f for f in f0s if abs(f - med) <= med * 0.3]
        return round(statistics.median(good), 1) if good else round(med, 1)
    except Exception as e:
        logger.warning(f"[F0] 自相关异常: {type(e).__name__}: {e}")
        return 0.0


def estimate_f0(vocal_path: str, seconds: float = 20.0,
                gender_hint: str = "") -> float:
    """估算人声基频中位数（Hz）。返回 0 表示失败。

    做法：ffmpeg 抽前 N 秒 → 单声道 8kHz wav（自带抗混叠）→ 交给 f0_from_samples。
    gender_hint 有值时先按先验范围测，测不出再用全范围重测。
    """
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix="_f0.wav")
        os.close(fd)
        cmd = [
            "ffmpeg", "-y", "-i", str(vocal_path),
            "-t", str(seconds), "-ac", "1", "-ar", "8000", "-vn", tmp,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        if r.returncode != 0 or not os.path.exists(tmp):
            logger.warning(f"[F0] ffmpeg 重采样失败: {r.stderr[-200:] if r.stderr else ''}")
            return 0.0

        with wave.open(tmp, "rb") as w:
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
        if sr != 8000 or len(raw) < 8000 * 2:
            return 0.0

        a = array.array("h")
        a.frombytes(raw)

        lo, hi = f0_search_range(gender_hint)
        result = f0_from_samples(a, sr, lo, hi)
        if result <= 0 and gender_hint:
            logger.info(f"[F0] {gender_hint} 先验范围 {lo:.0f}~{hi:.0f}Hz 没测到，改用全范围")
            lo, hi = f0_search_range("")
            result = f0_from_samples(a, sr, lo, hi)

        if result <= 0:
            logger.warning("[F0] 没有测到有效基频（可能整段都是伴奏/静音）")
            return 0.0
        logger.info(
            f"[F0] 基频中位数 {result} Hz（范围 {lo:.0f}~{hi:.0f}Hz，"
            f"音区 {f0_to_pitch_hint(result)}，粗判 {f0_to_gender(result)}）"
        )
        return result
    except Exception as e:
        logger.warning(f"[F0] 估算异常: {type(e).__name__}: {e}")
        return 0.0
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


# ============================================================
# Qwen-Audio 交叉验证（可选，失败不影响流程）
# ============================================================
_AUDIO_PROMPT = (
    "这是一段歌曲的人声。请判断：\n"
    "1. 唱歌的是男声还是女声\n"
    "2. 音区偏低、中等还是偏高\n"
    '优先只输出 JSON：{"gender":"male|female","pitch":"low|mid|high"}\n'
    "如果不支持 JSON 输出，就用一句话说明是男声还是女声、音区如何。"
)

# 模型不按 JSON 回（ASR 类模型常见）时，从文字里猜
_MALE_HINTS = ("男声", "男性", "男生", "男人的声音", "male")
_FEMALE_HINTS = ("女声", "女性", "女生", "女人的声音", "female")


def _gender_from_text(text: str) -> str:
    """从模型的自然语言回复里猜性别。男女同时出现视为判不出（如「男女对唱」）。"""
    t = str(text or "").lower()
    has_m = any(k.lower() in t for k in _MALE_HINTS)
    has_f = any(k.lower() in t for k in _FEMALE_HINTS)
    if has_m and not has_f:
        return "male"
    if has_f and not has_m:
        return "female"
    return "unknown"


def _pitch_from_text(text: str) -> str:
    """从模型的自然语言回复里猜音区。"""
    t = str(text or "")
    if any(k in t for k in ("偏低", "低沉", "低音")):
        return "low"
    if any(k in t for k in ("偏高", "高亢", "高音")):
        return "high"
    if any(k in t for k in ("中等", "适中", "中间", "中音")):
        return "mid"
    return "unknown"


def _extract_json(text: str) -> dict:
    """从模型输出里抠出第一个 JSON 对象。"""
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]{0,400}?\}", str(text))
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}


def _audio_b64(vocal_path: str, seconds: float = 20.0) -> tuple:
    """抽一小段转成 mp3 base64（整段 wav 几十 MB，传不动）。"""
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix="_probe.mp3")
        os.close(fd)
        cmd = [
            "ffmpeg", "-y", "-i", str(vocal_path),
            "-t", str(seconds), "-ac", "1", "-ar", "16000",
            "-codec:a", "libmp3lame", "-b:a", "48k", "-vn", tmp,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        if r.returncode != 0 or not os.path.exists(tmp):
            return "", 0
        data = Path(tmp).read_bytes()
        return base64.b64encode(data).decode(), len(data)
    except Exception as e:
        logger.warning(f"[QWEN-AUDIO] 音频预处理失败: {type(e).__name__}: {e}")
        return "", 0
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


async def qwen_audio_probe(vocal_path: str) -> dict:
    """
    用阿里百炼 Qwen-Audio 听一段人声，判断男女 + 音区。

    返回 {"gender":..., "pitch":..., "model":..., "raw":...}，失败返回 {}。
    任何异常都吞掉 —— 这是锦上添花的一级，绝不能拖垮翻唱主流程。
    """
    if not DASHSCOPE_API_KEY:
        logger.info("[QWEN-AUDIO] 未配置 DASHSCOPE_API_KEY，跳过")
        return {}
    if not vocal_path or not os.path.exists(str(vocal_path)):
        return {}

    b64, nbytes = await asyncio.to_thread(_audio_b64, vocal_path)
    if not b64:
        return {}
    logger.info(f"[QWEN-AUDIO] 抽 {nbytes / 1024:.0f} KB 人声送模型判断…")

    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    base = DASHSCOPE_BASE_URL or "https://dashscope.aliyuncs.com"
    # 两种请求体格式都试：原生多模态 / OpenAI 兼容模式（百炼两个入口都开着）
    attempts = [
        (
            f"{base}/api/v1/services/aigc/multimodal-generation/generation",
            lambda m: {
                "model": m,
                "input": {"messages": [{"role": "user", "content": [
                    {"audio": f"data:audio/mp3;base64,{b64}"},
                    {"text": _AUDIO_PROMPT},
                ]}]},
            },
        ),
        (
            f"{base}/compatible-mode/v1/chat/completions",
            lambda m: {
                "model": m,
                "messages": [{"role": "user", "content": [
                    {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}},
                    {"type": "text", "text": _AUDIO_PROMPT},
                ]}],
            },
        ),
    ]

    for url, build in attempts:
        for model in QWEN_AUDIO_MODELS:
            try:
                async with httpx.AsyncClient(timeout=40, trust_env=False) as client:
                    resp = await client.post(url, json=build(model), headers=headers)
                if resp.status_code != 200:
                    logger.info(
                        f"[QWEN-AUDIO] {model} @ {url.split('/api/')[-1][:28]} "
                        f"HTTP {resp.status_code}"
                    )
                    continue
                data = resp.json()
                # 原生格式：output.choices[0].message.content[0].text
                text = ""
                out = data.get("output") or {}
                if isinstance(out.get("choices"), list) and out["choices"]:
                    content = (out["choices"][0].get("message") or {}).get("content")
                    if isinstance(content, list):
                        text = "".join(
                            str(c.get("text", "")) for c in content if isinstance(c, dict)
                        )
                    elif isinstance(content, str):
                        text = content
                # 兼容格式
                if not text:
                    try:
                        text = data["choices"][0]["message"]["content"]
                    except Exception:
                        text = ""
                obj = _extract_json(text)
                gender = str(obj.get("gender", "")).lower()
                pitch = str(obj.get("pitch", "")).lower()
                if gender not in ("male", "female"):
                    gender = "unknown"
                if pitch not in ("low", "mid", "high"):
                    pitch = "unknown"
                # ASR 类模型不会按 JSON 回，退到文字里猜
                if gender == "unknown":
                    gender = _gender_from_text(text)
                if pitch == "unknown":
                    pitch = _pitch_from_text(text)
                if gender == "unknown" and pitch == "unknown":
                    logger.info(f"[QWEN-AUDIO] {model} 返回无法解析: {str(text)[:160]!r}")
                    continue
                logger.info(f"[QWEN-AUDIO] ✅ {model} 判定 gender={gender} pitch={pitch}")
                return {"gender": gender, "pitch": pitch, "model": model, "raw": text[:300]}
            except Exception as e:
                logger.info(f"[QWEN-AUDIO] {model} 调用异常: {type(e).__name__}: {e}")

    logger.warning("[QWEN-AUDIO] 所有模型/端点都失败，改用本地 F0")
    return {}


# ============================================================
# 人声综合探测（分离之后调用）
# ============================================================
async def probe_vocal(vocal_path: str, gender_hint: str = "") -> dict:
    """
    听分离出来的人声，给出最终的 gender / pitch。

    gender_hint：前面 DeepSeek/Tavily 判出来的性别，用来限定 F0 搜索范围
                 （压制倍频错误，见 f0_search_range 的注释）。

    返回 {"ok": bool, "f0": float, "gender": str, "pitch": int|None,
          "hint": str, "source": str, "reason": str}

    优先级：本地 F0（精确）> Qwen-Audio 音区映射 > 判不出来（pitch=None，用 preset）
    Qwen-Audio 即使成功也只当交叉验证 —— 它给的「低/中/高」是主观描述，
    而 F0 是实测数字，换算出来的半音数不会差太远。
    """
    res = {"ok": False, "f0": 0.0, "gender": "unknown", "pitch": None,
           "hint": "unknown", "source": "", "reason": ""}

    f0 = await asyncio.to_thread(estimate_f0, vocal_path, 20.0, gender_hint)
    audio = await qwen_audio_probe(vocal_path)

    if f0 > 0:
        measured = f0_to_gender(f0)
        # 性别要参与 pitch 计算：女→女只做音色转换（限幅微调），
        # 否则空灵女声会被「对齐 220Hz」硬压下来，听感就是唱得很低。
        semi = pitch_from_f0(f0, gender=measured)
        res.update({
            "ok": True, "f0": f0,
            "gender": measured,
            "pitch": semi,
            "hint": f0_to_pitch_hint(f0),
            "source": "f0",
            "reason": f"实测基频 {f0}Hz → {semi:+d} 半音",
        })
        if measured == "female" and abs(semi) == SAME_GENDER_MAX_SEMIS:
            res["reason"] += f"（女声已限幅 ±{SAME_GENDER_MAX_SEMIS}，避免整体降调）"
        if gender_hint in ("male", "female"):
            res["reason"] += f"（按 {gender_hint} 先验范围搜索）"
            if res["gender"] != "unknown" and res["gender"] != gender_hint:
                # 实测与先验矛盾：先验来自常识/搜索，比 20 秒采样更可靠
                res["reason"] += f"，但与先验 {gender_hint} 矛盾"
                res["conflict"] = True
        if audio.get("gender") and audio["gender"] != "unknown":
            same = audio["gender"] == res["gender"]
            res["reason"] += (
                f"；Qwen-Audio 判 {audio['gender']}/{audio.get('pitch')}"
                f"（{'一致' if same else '不一致，以实测 F0 为准'}）"
            )
        return res

    # F0 测不出来 → 退到 Qwen-Audio 的音区映射
    if audio.get("gender") in ("male", "female"):
        gender = audio["gender"]
        hint = audio.get("pitch") if audio.get("pitch") in ("low", "mid", "high") else "mid"
        # 音区 → 半音：目标音色是女声，男声整体抬一个八度附近，女声基本不动
        table = {
            ("male", "low"): 12, ("male", "mid"): 10, ("male", "high"): 8,
            ("female", "low"): 4, ("female", "mid"): 0, ("female", "high"): -2,
        }
        pitch = table.get((gender, hint), 0)
        res.update({
            "ok": True, "gender": gender, "hint": hint, "pitch": pitch,
            "source": "qwen-audio",
            "reason": f"F0 测不出，Qwen-Audio 判 {gender}/{hint} → {pitch:+d} 半音",
        })
        return res

    res["reason"] = "本地 F0 和 Qwen-Audio 都判不出来，沿用预设参数"
    return res


# ============================================================
# 性别判断（下载之前调用）
# ============================================================
async def _ask_deepseek(user_msg: str, timeout: float = 8) -> dict:
    """问 DeepSeek 要一个 JSON。失败返回 {}。"""
    if not DEEPSEEK_API_KEY:
        return {}
    payload = {
        "model": os.getenv("XAI_MODEL", "deepseek-chat"),
        "messages": [
            {"role": "system", "content": _SYS_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(DEEPSEEK_API, json=payload, headers=headers)
            resp.raise_for_status()
            _d = resp.json()
            try:
                from . import llm_usage as _lu
                _lu.record_from_response(_d, purpose="song")
            except Exception:
                pass
            return _extract_json(_d["choices"][0]["message"]["content"])
    except Exception as e:
        logger.warning(f"[GENDER] DeepSeek 调用失败: {type(e).__name__}: {e}")
        return {}


async def _by_tavily(song_name: str, singer: str) -> dict:
    """
    L2：联网搜歌手性别，再让 DeepSeek 从搜索结果里归纳。

    为什么还要过一遍 DeepSeek：搜索结果是整段网页摘要，
    直接正则匹配「男/女」会把「男女对唱」「女粉丝」全算进去。
    """
    if not singer:
        return {}
    try:
        from .web_search import fetch_web_search
        text = await fetch_web_search(f"{singer} 歌手 是男歌手还是女歌手")
    except Exception as e:
        logger.info(f"[GENDER] Tavily 搜索不可用: {type(e).__name__}: {e}")
        return {}
    if not text:
        return {}

    msg = (
        f"歌曲：{song_name}\n歌手：{singer}\n\n"
        f"以下是联网搜索到的资料（可能有噪声，注意区分「歌手本人性别」"
        f"和「粉丝/角色/合作者」的性别）：\n\n{text[:1500]}\n\n"
        f"请据此判断这位歌手本人的性别。"
    )
    obj = await _ask_deepseek(msg, timeout=12)
    if not obj:
        return {}
    obj["_source"] = "tavily"
    return obj


async def detect_singer_gender(song_name: str, singer: str = "") -> dict:
    """
    返回 {"gender": "male"/"female"/"unknown", "singer": str, "reason": str,
          "source": "deepseek"/"tavily"/"none"}

    任何异常都回落 unknown，不阻断翻唱流程（后面还有 probe_vocal 兜底）。
    """
    fallback = {
        "gender": "unknown", "singer": singer or "",
        "reason": "判断失败，用默认参数", "source": "none",
    }

    # ---- L1：DeepSeek 常识 ----
    obj = await _ask_deepseek(f"歌曲名：{song_name}\n歌手：{singer or '未知'}")
    gender = str(obj.get("gender", "unknown")).lower()
    if gender in ("male", "female"):
        return {
            "gender": gender,
            "singer": obj.get("singer") or singer or "",
            "reason": obj.get("reason", ""),
            "source": "deepseek",
        }
    if not obj:
        logger.warning("[GENDER] DeepSeek 不可用，直接进 L2")

    # ---- L2：Tavily 联网搜索 ----
    logger.info(f"[GENDER] DeepSeek 判不出「{singer or song_name}」，改联网搜索")
    obj2 = await _by_tavily(song_name, singer)
    gender2 = str(obj2.get("gender", "unknown")).lower()
    if gender2 in ("male", "female"):
        return {
            "gender": gender2,
            "singer": obj2.get("singer") or singer or "",
            "reason": (obj2.get("reason") or "") + "（来自联网搜索）",
            "source": "tavily",
        }

    # ---- L3：交给 probe_vocal（分离后听人声）----
    fallback["reason"] = "常识和搜索都判不出，等分离后用音频判断"
    return fallback


async def resolve_gender_chain(song_name: str, singer: str = "",
                              override: str = "") -> dict:
    """
    ★ 统一的三级判定链（服务器侧能定的部分）★

      L1 用户指定（`/翻唱 晴天 女`）        → source="override"，算力机直接采用
      L2 歌手判定（DeepSeek 常识 → Tavily）→ source="singer"，算力机直接采用
      L3 都定不出                          → source="none"，交给算力机
                                              （全曲均分 ~100s 人声送 Qwen-Audio）

    返回 {"gender", "source", "singer", "reason"}
      gender: "male"/"female"/""（空 = 交给算力机兜底）

    为什么要在服务器先判：L1/L2 都是**下载之前**就能定的，定下来算力机就
    完全不用再猜性别了 —— 少一次联网、少一次听音频、行为也可预测。
    """
    ov = str(override or "").strip().lower()
    # 中文写法也认（`/翻唱 晴天 女` 走的是 split_gender_suffix，但 API/别处
    # 可能直接传「女」，这里兜一层，免得绕到联网判定去）
    ov = {"男": "male", "男性": "male", "男生": "male", "男的": "male",
          "女": "female", "女性": "female", "女生": "female", "女的": "female"}.get(ov, ov)
    if ov in ("male", "female"):
        return {"gender": ov, "source": "override", "singer": singer or "",
                "reason": "用户直接指定 → 跳过所有自动判定"}

    info = await detect_singer_gender(song_name, singer)
    g = str(info.get("gender") or "").lower()
    if g in ("male", "female"):
        return {"gender": g, "source": "singer",
                "singer": info.get("singer") or singer or "",
                "reason": f"{info.get('reason') or ''}（判定来源 {info.get('source')}）"}

    return {"gender": "", "source": "none", "singer": singer or "",
            "reason": info.get("reason") or "歌手判定不出 → 交给算力机听人声"}
