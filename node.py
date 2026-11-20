# ============================================================
# 本地算力机端：node.py（合并版）
#
# 合并原则：取两版之长，绝不二选一。
#   · 昨天版有 process_tts_task（IndexTTS2 + RVC 变声）→ 保留
#   · 今早版有 process_split_task（vocal+inst 双文件，/翻唱 依赖）→ 保留
#   · 今早版有 RVC 完整参数（index_rate/rms_mix_rate/protect）→ 保留
#   · 昨天版有 target_format=mp3 压制 → 保留
#
# 支持任务类型：
#   split              翻唱分离（人声 + 伴奏，双文件回传）
#   audio_separation   手动人声分离（单文件 + 可选 mp3）
#   rvc                变声
#   tts                IndexTTS2 合成 + RVC 变声
#   reflection         两天一复盘（bundle → 本地 qwen2 → JSON 回传）
# ============================================================
import time
import os
import re
import sys
import json
import math
import array
import base64
import contextlib
import tempfile
import wave
import statistics
import subprocess
import shutil
import threading
import requests
from pathlib import Path


# ================= 环境变量文件（node.env） =================
def _load_env_file(path: Path | None = None) -> None:
    """
    读同目录下的 node.env（每行 KEY=VALUE，# 开头是注释）。

    为什么不用 python-dotenv：算力机上不一定装了，而这里只需要最基础的功能。
    已有的系统环境变量优先（不覆盖），方便临时用 $env:XXX 调试。

    算力机可调项（写在 node.env 里）：
      API_SECRET=...               服务器和算力机共用的接口密钥（两边必须一致）
      MIX_VOCAL_OVER_INST_DB=3     人声比伴奏高多少 dB
      MIX_DRUM_GAIN=0.7            鼓单独乘的增益
      MIX_VOCAL_TREBLE_DB=0        人声高频补偿（治高音起不来）
      TTS_USE_RVC=true             语音是否再过一遍 RVC
      TEMP_DIR=./local_temp
    """
    p = path or (Path(__file__).resolve().parent / "node.env")
    if not p.exists():
        print(f"ℹ️ 没有 {p.name}，只用系统环境变量", flush=True)
        return
    loaded = 0
    try:
        # 记事本可能存成 UTF-8 BOM / GBK / UTF-16，逐个试
        text = ""
        for enc in ("utf-8-sig", "utf-8", "gbk", "utf-16"):
            try:
                text = p.read_text(encoding=enc)
                break
            except (UnicodeDecodeError, UnicodeError):
                continue
        if not text:
            print(f"⚠️ {p.name} 编码认不出来（另存为 UTF-8 试试）", flush=True)
            return

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            # ⚠️ 支持行内注释：`KEY=45   # 说明` → 45
            # 真机踩过：node.env 里写 `F0_ANALYZE_SEC=45   # 分析前多少秒`
            # 整串被当成值 → float() 抛 ValueError → 整条翻唱链路直接失败。
            # 规则和 dotenv 一致：`#` 前面必须有空白才算注释，
            # 这样值里本来就有的 #（密码 / URL fragment）不会被误切。
            if v[:1] in ("'", '"'):
                q = v[0]
                end = v.find(q, 1)
                v = v[1:end] if end > 0 else v[1:]
            else:
                v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
            if k and k not in os.environ:
                os.environ[k] = v
                loaded += 1
        print(f"⚙️ 已加载 {p.name}（{loaded} 项生效）", flush=True)
    except Exception as e:
        print(f"⚠️ 读取 {p.name} 失败: {type(e).__name__}: {e}", flush=True)


def _env_num(name: str, default: float) -> float:
    """
    读数字型环境变量，脏值自动回退，绝不让它把任务打死。

    node.env 是手写的，很容易写成 `KEY=45  # 说明` 或带单位。
    直接 float() 会抛 ValueError（真机踩过整条翻唱因此失败），
    所以统一走这里：能取到开头的数字就用，取不到就用默认值。
    """
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return float(default)
    txt = str(raw).strip()
    try:
        return float(txt)
    except Exception:
        pass
    m = re.match(r"[-+]?\d+(?:\.\d+)?", txt)
    if m:
        val = float(m.group(0))
        print(f"⚠️ [ENV] {name}={raw!r} 有多余内容，按 {val} 处理", flush=True)
        return val
    print(f"⚠️ [ENV] {name}={raw!r} 不是数字，用默认 {default}", flush=True)
    return float(default)


_load_env_file()

# ================= 基础配置 =================
os.environ['NO_PROXY'] = '*'
os.environ['HTTP_PROXY'] = ''
os.environ['HTTPS_PROXY'] = ''

SERVER_IP = os.getenv("SERVER_IP", "127.0.0.1")
SERVER_PORT = os.getenv("SERVER_PORT", "8080")
API_SECRET = os.getenv("API_SECRET", "my_super_secret_key_123")
BASE_URL = f"http://{SERVER_IP}:{SERVER_PORT}"

TEMP_DIR = Path(os.getenv("TEMP_DIR", "./local_temp"))
TEMP_DIR.mkdir(exist_ok=True)


def print_config() -> None:
    """启动时把实际生效的连接配置打出来（脱敏），连不上时一眼看出是不是配错了。"""
    sec = API_SECRET
    shown = f"{sec[:8]}...{sec[-4:]}" if len(sec) > 14 else f"{sec[:4]}***"
    print(f"🔗 目标服务器 {BASE_URL}", flush=True)
    print(f"🔑 API_SECRET {shown}（长度 {len(sec)}）", flush=True)
    print(f"📁 TEMP_DIR   {TEMP_DIR.resolve()}", flush=True)
    # 👇 一眼确认「单段式 cover 有没有注册」—— 服务器看到 caps 里有 cover 才走新链路
    try:
        caps = ", ".join(sorted(TASK_HANDLERS))
        flag = "✅ 含单段式 cover" if "cover" in TASK_HANDLERS else "❌ 没有 cover（旧版）"
        print(f"🧩 支持任务 {caps}", flush=True)
        print(f"🧩 {flag}", flush=True)
    except NameError:
        pass
    print(f"🎚️ 翻唱工作格式 {os.getenv('COVER_WORK_FORMAT', 'mp3')} / "
          f"码率 {os.getenv('COVER_WORK_BITRATE', '320k')} / "
          f"语音版 {os.getenv('COVER_VOICE_MAX_MB', '3.0')}MB "
          f"{os.getenv('COVER_VOICE_MAX_SEC', '120')}s", flush=True)
    print(f"🎧 Qwen-Audio {'已配置 key' if DASHSCOPE_API_KEY else '未配置（只用本地 F0 测音高）'}",
          flush=True)
    # 👇 启动就探测 IndexTTS 通不通。
    #    否则要等到第一次发语音才发现「indextts failed → 降级云端（龙呼呼）」。
    if TTS_ENGINE == "indextts":
        try:
            from urllib.parse import urlparse as _up
            _u = _up(TTS_URL)
            _h = _u.hostname or "127.0.0.1"
            _p = _u.port or 80
            if _port_open(_h, _p):
                print(f"🎙️ IndexTTS {TTS_URL} ✅ 端口在听", flush=True)
            else:
                _alive = [x for x in TTS_PORT_CANDIDATES if x != _p and _port_open(_h, x)]
                print(f"🎙️ IndexTTS {TTS_URL} ❌ 端口 {_p} 没人听"
                      f"（语音会降级云端）"
                      f"{f'；但这些端口在听：{_alive}' if _alive else ''}", flush=True)
                if not _alive:
                    _ports = _listening_ports()
                    print(f"   本机在监听的非系统端口：{_ports[:24]}"
                          f"{' …' if len(_ports) > 24 else ''}", flush=True)
                    print("   → IndexTTS2 的 webui/api 没启动，或者起在上面某个端口"
                          "（改 node.env 的 TTS_URL）", flush=True)
        except Exception:
            pass
    # 👇 启动就探 ComfyUI 通不通，别等第一张图才发现「画图离线」。
    try:
        _su = _urlparse(SD_API_URL)
        _sh = _su.hostname or "127.0.0.1"
        _sp = _su.port or 8188
        if _port_open(_sh, _sp):
            print(f"🎨 ComfyUI {SD_API_URL} ✅ 端口在听（画图可用）", flush=True)
        else:
            print(f"🎨 ComfyUI {SD_API_URL} ❌ 端口 {_sp} 没人听（画图功能暂不可用）", flush=True)
    except Exception:
        pass
    print(f"⏱️ 传输超时 上传 {UPLOAD_TIMEOUT_BASE:.0f}s+{UPLOAD_TIMEOUT_PER_MB:.0f}s/MB"
          f"（上限 {UPLOAD_TIMEOUT_MAX:.0f}s）/ 下载读 {DOWNLOAD_TIMEOUT_READ:.0f}s"
          f" / RVC {RVC_TIMEOUT:.0f}s", flush=True)
    # 👇 启动就查 rubberband：没有它伴奏移调会退回 asetrate+atempo，音质差一大截。
    #    等唱完一首才发现「镲片发碎」太晚了，不如启动就告诉人。
    try:
        if _ffmpeg_has_filter("rubberband"):
            print("🎼 ffmpeg rubberband 滤镜 ✅ 有（伴奏移调用高质量模式）", flush=True)
        else:
            print("🎼 ffmpeg rubberband 滤镜 ❌ 没有（这个构建是 --disable-librubberband）",
                  flush=True)
            print("   → 伴奏移调改用 librosa 相位声码器（RVC runtime 自带，不用下载）；"
                  "librosa 也没有才退回 asetrate", flush=True)
            print("   → 想更进一步：换一个带 --enable-librubberband 的 ffmpeg 构建"
                  "（Gyan/BtbN 的 full 版），复音素材瞬态会更好", flush=True)
    except Exception:
        pass
    if API_SECRET == "my_super_secret_key_123":
        print("⚠️ 用的是默认 secret —— node.env 里的 API_SECRET 可能没被读到", flush=True)

# 可选加速：把 node.py 的 HTTP 流量走本地代理（机场客户端如 Clash 的混合端口）。
# node.env 里配 NODE_PROXY=http://127.0.0.1:7897 即生效；留空=直连（默认）。
# ⚠️ 前提：代理客户端规则里必须让 127.0.0.0/8 直连、服务器 IP 走香港节点，
#    否则本地 ComfyUI/Demucs/TTS 会被误代理而连不上。
_NODE_PROXY = os.getenv("NODE_PROXY", "").strip()
PROXIES = {"http": _NODE_PROXY, "https": _NODE_PROXY} if _NODE_PROXY else {"http": None, "https": None}
CREATE_NO_WINDOW = 0x08000000  # Windows 下静默子进程

# 轮询节奏
# ⚠️ 原来每 2 秒发一次心跳 + 拉一次任务 = 每分钟 60 条 access log，
#    服务器上 docker log 就是这么堆起来的。现在降到 ~16 条/分钟。
#    HEARTBEAT_INTERVAL 必须小于服务器端 HEARTBEAT_TIMEOUT（默认 30s）
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "15"))
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))

# ================= 传输超时（按体积动态算，别写死） =================
# 真机踩过：一首 5 分钟的歌，分离回传是 vocal + inst 两个 wav 各 38MB，
# 合计 76MB。家宽上行波动时上传要 2~5 分钟，原来写死 timeout=180
# → ReadTimeout，前面几分钟的 Demucs 白跑。
# 所以超时按体积算：基础 300s + 每 MB 12s（76MB → 1212s），上限 1 小时。
UPLOAD_TIMEOUT_BASE = float(os.getenv("UPLOAD_TIMEOUT_BASE", "300"))
UPLOAD_TIMEOUT_PER_MB = float(os.getenv("UPLOAD_TIMEOUT_PER_MB", "12"))
UPLOAD_TIMEOUT_MAX = float(os.getenv("UPLOAD_TIMEOUT_MAX", "3600"))
# 下载：连接 10s；读超时指「多久没收到新数据」，慢速大文件给足
DOWNLOAD_TIMEOUT_READ = float(os.getenv("DOWNLOAD_TIMEOUT_READ", "180"))

# 直连网易云 CDN 时带的 UA（不带可能被防盗链拒）
NETEASE_UA = os.getenv(
    "NETEASE_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)


def upload_timeout(size_bytes) -> float:
    """按文件体积算上传超时（秒）。76MB → 300 + 912 = 1212s，上限 1 小时。"""
    try:
        mb = max(0.0, float(size_bytes or 0) / 1048576.0)
    except Exception:
        mb = 0.0
    return min(UPLOAD_TIMEOUT_MAX, UPLOAD_TIMEOUT_BASE + mb * UPLOAD_TIMEOUT_PER_MB)

# ================= TTS（供 chat 的 type:"tts" 用） =================
# TTS_ENGINE:
#   indextts （默认）IndexTTS2 的 /api/v1/tts，需要参考音 spk_audio，产物落在 outputs 目录
#   http           通用 HTTP：POST {"text": "..."} → wav 二进制 或 {"audio_url"/"output": ...}
#   cmd            命令行模板：TTS_CMD 里用 {text} / {output} 占位
TTS_ENGINE = os.getenv("TTS_ENGINE", "indextts").lower()
TTS_URL = os.getenv("TTS_URL", "http://127.0.0.1:7860/api/v1/tts")
TTS_PROMPT_WAV = os.getenv("TTS_PROMPT_WAV", str(TEMP_DIR / "prompt_kki_cut.wav"))
TTS_OUTPUT_DIR = Path(os.getenv("TTS_OUTPUT_DIR", "E:/deepseek/IndexTTS2.0_v3/outputs"))
TTS_CMD = os.getenv("TTS_CMD", "")
TTS_TIMEOUT = float(os.getenv("TTS_TIMEOUT", "300"))
# TTS 产物要不要再过一遍 RVC（保持大肥鱼音色）
TTS_USE_RVC = os.getenv("TTS_USE_RVC", "true").lower() == "true"
TTS_RVC_PITCH = os.getenv("TTS_RVC_PITCH", "0")
# TTS 变声用的 F0 算法。原来走 _run_rvc 的默认值 rmvpe —— 全站唯一漏网的一处。
# 翻唱早就全 crepe 了，这里也跟上，别让「她说话的声音」和「她唱的歌」用两套提取器。
TTS_RVC_F0 = os.getenv("TTS_RVC_F0", "crepe")

# ================= RVC 音色模型的音域（Hz）=================
# kiki.pth 这类音色模型的音域是**有限**的。
# 源歌人声一旦超出（比如 A7 ≈ 3520Hz 的高音歌），RVC 输出会全程破音/发哑。
# 正确做法：把人声的**最低/最高音**整体移调塞进模型音域，
# 而不是先判男女再 ±12 —— 性别只决定音色，不决定能不能唱上去。
VOICE_F0_MIN = float(os.getenv("RVC_VOICE_F0_MIN", "135"))
# 源本来就是女声时，「允许升调」的门槛（Hz）。
# 为什么单独一个值：RVC 变调是**整体移调**，共振峰会跟着一起上移 ——
# 女声歌只要 +2 半音，听感就是「夹着嗓子、发尖」。
# 女声歌的低音（110~150Hz）本来就是女声的低音，不需要抬；
# 只有低到 100 以下（真男声/分离残留低频）才值得动。
# 男声源仍然用 RVC_VOICE_F0_MIN（必须抬进女声区，否则变声出来还是男声）。
VOICE_F0_FEMALE_FLOOR = float(os.getenv("RVC_VOICE_F0_FEMALE_FLOOR", "100"))

# ===== 女声「降调」的三道闸（用户 2026-11-12 定）=====
# 背景：RVC 变调是**整体移调**，共振峰跟着一起走。女声歌往下降 → 共振峰下移
#       → 听感「有点男声化」。所以降调不能只看「高音超没超上限」。
#
# 规则（两条用户明确给的 + 一条保险）：
#   ① 整体高音区的歌（炉心融解那种 300~800Hz）→ **可以整体往下调**
#   ② 平调/宽跨度的歌（180~700、200~800）→ 也能降，但
#      **降完低音不能低于 150Hz**（高的哑就哑吧，低的必须保持女声）
#   ③ 高音只超一点点（<8%）→ **不值得降**：
#      情歌 563 vs 540 只超 4.3%，降 1 半音换来全曲共振峰下移，亏
# 地板（Hz）：降调后低音必须还在这个之上。
# ⚠️ 2026-11-12 三次校准：
#   150 → 那首 p10=192 降 3 半音变 161Hz，而 F0_MALE_MAX=155，161 只高 6Hz → 听感偏男
#   185 → 太保守，192 那种歌完全不降，高音全哑
#   165 → 实测校准：梁静茹《情歌》不降调时成品 p1=146Hz「刚好是女声」，
#         用户建议「在实测基础上 +10Hz 做地板」= 156；取 165（= F0_MIXED_LO，
#         混合区下界）更安全一点，正好卡在「开始偏男」那条线上。
VOICE_F0_FEMALE_DOWN_FLOOR = float(os.getenv("RVC_VOICE_F0_FEMALE_DOWN_FLOOR", "180"))

# 男声源变声后的**成品低音地板**（用户 2026-11-12：
# 「洋葱这个最低音调到男音了，男生歌最低必须 140Hz 要保留」）。
# 实测：洋葱 p1=96Hz +6 半音 → 本该 135Hz，但 RVC 输出实测 p1=70Hz
# （次谐波/气声伪影）。光靠升调参数压不住，得在混音时**高通切掉 140Hz 以下**。
VOICE_F0_MALE_FLOOR = float(os.getenv("RVC_VOICE_F0_MALE_FLOOR", "140"))
VOICE_F0_FEMALE_DOWN_TRIGGER = float(os.getenv("RVC_VOICE_F0_FEMALE_DOWN_TRIGGER", "0.08"))
# 女声最多降几个半音（降太多整体男声化，宁可高音哑）
VOICE_F0_FEMALE_MAX_DOWN = int(os.getenv("RVC_VOICE_F0_FEMALE_MAX_DOWN", "4"))

# ===== 男声「升调」按中位对齐（用户 2026-11-12）=====
# 老逻辑：把**最低音**抬到 VOICE_F0_MIN(135) → 平凡之路 lo=75 要 +11 半音
#        → 整首歌高一大截，听感「尖尖叫」（苏打绿《小情歌》就是这样）。
# 用户经验：「变 6 是地板，5 就男音」→ 升调量应该在 6~8 之间，不是 11。
# 新逻辑：把**中位 p50** 对齐到目标女声区（200Hz），再夹在 [MIN, MAX] 之间。
#   平凡之路 p50=135 → +6.8 → 7；小情歌 p50=146 → +5.4 → 夹到 6。
VOICE_MALE_TARGET_F0 = float(os.getenv("RVC_MALE_TARGET_F0", "200"))
VOICE_MALE_MIN_SEMIS = int(os.getenv("RVC_MALE_MIN_SEMIS", "6"))

VOICE_F0_MAX = float(os.getenv("RVC_VOICE_F0_MAX", "880"))
# 模型最舒服的音区中心，0 = 自动取 min/max 几何中心
VOICE_F0_CENTER = float(os.getenv("RVC_VOICE_F0_CENTER", "0"))
# keep   = 尽可能保留原调，只在必须时往下调（默认，听感最自然）
# center = 把整段往模型中心对齐（音域利用率高，但整体会变调）
VOICE_F0_MODE = os.getenv("RVC_VOICE_F0_MODE", "keep")
# 是否**总是**把原曲 max 对齐到 RVC_VOICE_F0_MAX（不管原曲高低）
#   false（默认）：只在超出上限时往下调，本来正常的歌保持原调
#   true         ：480→降到 420，240→升到 420，都尽量贴近目标
VOICE_F0_ALIGN_MAX = os.getenv("RVC_VOICE_F0_ALIGN_MAX", "false").lower() == "true"
# 算 pitch 时「最高音」取哪个值：
#   false（默认）= p99 —— 覆盖 99% 帧，抗个别异常帧（假音/喷麦/和声串音）
#   true        = max —— 绝对最高的一帧，最保守（宁可多降，也不让任何一句破）
VOICE_F0_USE_MAX = os.getenv("RVC_VOICE_F0_USE_MAX", "false").lower() == "true"
# 高低音**都**超出模型音域、只能救一头时，先救哪一头：
#   true （默认）= 保低音：把低音抬到下限之上，高音顶破上限就顶破（女声还在，那几句紧/略破）
#   false        = 保高音：把高音压回上限内，低音掉到下限以下（会跌回男声，等于白转）
# 用户 2026-11-10 定的规矩：先保证是女音，高音上不去就上不去。
VOICE_F0_LOW_FIRST = os.getenv("RVC_PITCH_LOW_FIRST", "true").lower() != "false"
RVC_PITCH_MIN = int(os.getenv("RVC_PITCH_MIN", "-24"))
RVC_PITCH_MAX = int(os.getenv("RVC_PITCH_MAX", "24"))

# 变调在哪一步做（两种都跑一遍，听哪个自然）：
#   rvc  = 原声不动，把半音数交给 RVC 内部变调（默认，通常更自然）
#   pre  = 先用 ffmpeg 把人声轨整体变调，再让 RVC pitch=0（共振峰一起移，音色会变）
COVER_PITCH_MODE = os.getenv("COVER_PITCH_MODE", "rvc").lower()

# 调性校验：混音前分别估「变声后人声」和「伴奏」的首调，不同调就报警。
# 纯 Python chroma，每轨约 2~3 秒，不依赖 numpy。
COVER_KEY_CHECK = os.getenv("COVER_KEY_CHECK", "true").lower() != "false"

# 分轨数：4 = 分离出 drums / bass / other / vocals。
# 伴奏按轨移调需要单轨，所以默认 4；嫌慢可以改 2（退回整体移调）。
try:
    COVER_STEMS = int(os.getenv("COVER_STEMS", "4"))
except Exception:
    COVER_STEMS = 4

# 精确对齐：true = 先用 ffmpeg 做**浮点**预变调，让成品 max 正好等于
# RVC_VOICE_F0_MAX（RVC 的 pitch 只吃整数半音，做不到精确）。
# 代价：走 pre 路径，共振峰跟着移，音色会有点变化。
COVER_PITCH_EXACT = os.getenv("COVER_PITCH_EXACT", "false").lower() == "true"

# 复盘用的本地 LLM
LLM_URL = os.getenv("LLM_URL", "http://127.0.0.1:11434/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5:7b")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "300"))

# ================= 你的 RVC 路径 =================
RVC_DIR = r"E:\deepseek\RVC20260723Nvidia\RVC20260718Nvidia"
RVC_PYTHON = f"{RVC_DIR}\\runtime\\python.exe"
RVC_SCRIPT = f"{RVC_DIR}\\my_kkI_rvc.py"
RVC_MODEL = r"E:\deepseek\RVC20260723Nvidia\RVC20260718Nvidia\assets\weights\kikiv2.pth"
RVC_INDEX = r"E:\deepseek\RVC20260723Nvidia\RVC20260718Nvidia\assets\weights\kikiv2.index"
# RVC 变声超时：原来写死 600s，长歌（6 分钟以上）+ rmvpe 会超。
# 真人实测 4 分钟歌约 1~2 分钟，给 1800 够 8~10 分钟的歌。
RVC_TIMEOUT = float(os.getenv("RVC_TIMEOUT", "1800"))


# ============================================================
# 心跳 / 下载 / 上传
# ============================================================
def send_heartbeat() -> bool:
    try:
        r = requests.get(f"{BASE_URL}/api/heartbeat",
                         params={"secret": API_SECRET, "caps": ",".join(_current_caps())},
                         timeout=5, proxies=PROXIES)
        return r.status_code == 200
    except Exception:
        return False


def download_file(url: str, out_path, expect_audio: bool = True,
                  retries: int = 4, label: str = "", extra_headers=None) -> bool:
    """
    带断点续传 / 内容检测 / 大小校验的下载，失败指数退避重试。
    返回 True/False（不抛异常），调用方统一用返回值判断。
    extra_headers：额外请求头（直连网易云 CDN 时要带 UA）。
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    part = out_path.with_suffix(out_path.suffix + ".part")
    tag = label or out_path.name

    for attempt in range(1, retries + 1):
        try:
            have = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={have}-"} if have > 0 else {}
            if extra_headers:
                headers.update(extra_headers)

            with requests.get(url, headers=headers, stream=True, proxies=PROXIES,
                              timeout=(10, DOWNLOAD_TIMEOUT_READ), allow_redirects=True) as r:
                if r.status_code == 404:
                    print(f"❌ [{tag}] 服务器 404（文件不存在或任务已被清理）", flush=True)
                    return False
                if r.status_code == 416:
                    r.close()
                    if part.exists():
                        part.replace(out_path)
                        print(f"✅ [{tag}] 已完整（416）", flush=True)
                        return True
                    return False
                if r.status_code >= 400:
                    print(f"❌ [{tag}] HTTP {r.status_code}", flush=True)
                    return False

                ctype = (r.headers.get("Content-Type") or "").lower()
                # 服务器出错时会回 JSON/HTML —— 千万别把它当音频存下来
                if expect_audio and ("text/html" in ctype or "application/json" in ctype):
                    print(f"❌ [{tag}] 返回的不是音频（{ctype}）：{r.text[:150]}", flush=True)
                    return False

                mode = "ab" if (have > 0 and r.status_code == 206) else "wb"
                if mode == "wb":
                    have = 0
                total_hdr = int(r.headers.get("content-length", 0) or 0)
                total = total_hdr + have
                print(f"📥 [{tag}] 下载中（已有 {have / 1024 / 1024:.1f} MB / 共 {total / 1024 / 1024:.1f} MB）", flush=True)

                done = have
                # ⚠️ 进度要勤打：原来每 5MB 才打一行，16MB 的文件只有 3 行，
                #    慢的时候看起来就像「卡死在 0%」，根本分不清是在下还是挂了。
                #    现在每 1MB 或每 3 秒打一次，带百分比和实时速度。
                next_mark = done + 1024 * 1024
                t0 = time.time()
                last_print = t0
                with open(part, mode) as f:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if done >= next_mark or now - last_print >= 3:
                            elapsed = max(0.1, now - t0)
                            speed = (done - have) / elapsed / 1024
                            pct = f"{done / total * 100:.0f}%" if total else "?"
                            print(f"  ⬇️ [{tag}] {done / 1048576:.1f}"
                                  f"/{total / 1048576:.1f} MB（{pct}，{speed:.0f} KB/s）",
                                  flush=True)
                            while next_mark <= done:
                                next_mark += 1024 * 1024
                            last_print = now
                    f.flush()
                    os.fsync(f.fileno())
                if done > have:
                    elapsed = max(0.1, time.time() - t0)
                    print(f"  ✅ [{tag}] 平均 {(done - have) / elapsed / 1024:.0f} KB/s"
                          f"（{done / 1048576:.1f} MB / {elapsed:.1f}s）", flush=True)

            size = part.stat().st_size
            if total and size < total:
                raise RuntimeError(f"下载不完整：{size}/{total} 字节")
            # ⚠️ 最小尺寸只对音频/文件生效；bundle JSON 可能只有几百字节，
            #    不能因为"小"就判定失败（否则复盘永远拿不到 bundle）
            if expect_audio and size < 1024:
                print(f"❌ [{tag}] 文件太小（{size} bytes），判定失败", flush=True)
                part.unlink(missing_ok=True)
                return False
            if size == 0:
                print(f"❌ [{tag}] 空文件", flush=True)
                part.unlink(missing_ok=True)
                return False

            part.replace(out_path)
            print(f"✅ [{tag}] 下载完成: {out_path.name}（{size / 1024 / 1024:.2f} MB）", flush=True)
            return True

        except Exception as e:
            print(f"⚠️ [{tag}] 下载第 {attempt}/{retries} 次失败: {type(e).__name__}: {e}", flush=True)
            if attempt == retries:
                part.unlink(missing_ok=True)
                return False
            time.sleep(min(10, 2 ** attempt))

    return False


def upload_complete(task_id: str, file_path=None, result=None, filename=None,
                    file2=None) -> bool:
    """
    回传任务结果：
      - file_path：二进制文件（wav/flac/mp3）
      - file2    ：可选的第二个文件（翻唱单段式：截断版 + 完整版一起传）
      - result   ：字典（复盘 JSON / 错误信息）
    失败任务也用 result={"error": ...} 回传，让服务器立刻知道失败，
    而不是让 STATUS 永远停在 processing。
    """
    url = f"{BASE_URL}/api/tasks/complete"
    try:
        if file_path is not None:
            fp = Path(file_path)
            f2 = Path(file2) if file2 else None
            if f2 and not f2.exists():
                f2 = None
            size = fp.stat().st_size if fp.exists() else 0
            if f2:
                size += f2.stat().st_size
            tmo = upload_timeout(size)
            extra = f" + {f2.name}" if f2 else ""
            print(f"📤 回传 {fp.name}{extra}（{size / 1048576:.1f} MB），超时 {tmo:.0f}s",
                  flush=True)
            with contextlib.ExitStack() as stack:
                fv = stack.enter_context(open(fp, "rb"))
                files = {"file": (filename or fp.name, fv, "application/octet-stream")}
                if f2:
                    fv2 = stack.enter_context(open(f2, "rb"))
                    files["file2"] = (f2.name, fv2, "application/octet-stream")
                data = {"task_id": task_id, "secret": API_SECRET}
                # 👇 允许「文件 + JSON」一起回传（/测歌 A/B 需要：成品 mp3 + 判定数据）
                #    以前带文件就丢掉 result，服务器只能二选一。
                if result:
                    data["result"] = json.dumps(result, ensure_ascii=False)
                r = requests.post(url, data=data, files=files, proxies=PROXIES, timeout=tmo)
        else:
            data = {
                "task_id": task_id,
                "secret": API_SECRET,
                "result": json.dumps(result or {}, ensure_ascii=False),
            }
            r = requests.post(url, data=data, proxies=PROXIES, timeout=60)

        if r.status_code == 200:
            print(f"✅ 任务 {task_id} 回传完成", flush=True)
            return True
        print(f"❌ 任务 {task_id} 回传失败 HTTP {r.status_code}: {r.text[:200]}", flush=True)
        return False
    except Exception as e:
        print(f"❌ 任务 {task_id} 回传异常: {type(e).__name__}: {e}", flush=True)
        return False


def _download_input(task_id: str, out_path, expect_audio: bool = True, label: str = "") -> bool:
    return download_file(
        f"{BASE_URL}/api/download/{task_id}?secret={API_SECRET}",
        out_path, expect_audio=expect_audio, label=label or task_id,
    )


# ============================================================
# 1. 翻唱分离（song_cover 用，必须回传 vocal + inst 两个文件）
# ============================================================
def _demucs_separate(input_wav: Path, want_inst: bool = True, stems: int = 2):
    """
    请求本地 Demucs，等它跑完。

    返回 (vocals_url, inst_urls, mode)：
      mode = "2stem"   服务返回 vocals + no_vocals（理想情况）
      mode = "4stem"   服务返回 drums/bass/other/vocals（stems=2 没生效）
      mode = "unknown" 名字都认不出

    ⚠️ 4-stem 必须特殊处理：
       旧逻辑「名字认不出就取 urls[1]」，在 4-stem 下拿到的是 drums.wav
       → 混出来的伴奏只有底鼓，其余乐器全丢（真机就是这样）。
       4-stem 时伴奏要用「原曲 − 人声」重建，或者把 drums+bass+other 三条加起来。
    """
    print("✂️ 请求本地 Demucs 分离…", flush=True)
    try:
        with open(input_wav, "rb") as f:
            resp = requests.post(
                "http://127.0.0.1:8005/separate",
                files={"file": (input_wav.name, f, "audio/wav")},
                data={"stems": str(int(stems))}, proxies=PROXIES, timeout=600,
            )
        job_id = resp.json().get("job_id")
        if not job_id:
            print(f"❌ 获取 job_id 失败: {resp.text[:200]}", flush=True)
            return None, [], "unknown"
    except Exception as e:
        print(f"❌ Demucs 请求异常: {type(e).__name__}: {e}", flush=True)
        return None, [], "unknown"

    print(f"⏳ 等待 Demucs GPU 处理 job={job_id}", flush=True)
    for _ in range(200):  # 最多等 10 分钟
        time.sleep(3)
        try:
            st = requests.get(f"http://127.0.0.1:8005/jobs/{job_id}",
                              proxies=PROXIES, timeout=30).json()
        except Exception as e:
            print(f"⚠️ 查询 Demucs 状态出错: {e}", flush=True)
            continue

        status = st.get("status")
        if status == "failed":
            print("❌ Demucs 分离失败", flush=True)
            return None, None
        if status != "completed":
            continue

        urls = st.get("result_urls", []) or []
        # ⚠️ 把完整列表打出来：stem 名字/顺序不对是「没有伴奏」的头号嫌疑
        print(f"✅ Demucs 分离完成，返回 {len(urls)} 个文件：{urls}", flush=True)

        def _abs(u):
            return f"http://127.0.0.1:8005{u}" if str(u).startswith("/") else u

        def _name(u):
            return str(u).rsplit("/", 1)[-1]

        # ⚠️ "no_vocals" 里也含 "vocals"，直接 `"vocals" in u` 在顺序反了的时候
        #    会把伴奏当人声取出来 —— 必须显式排除
        vocals_url = next(
            (u for u in urls if "vocals" in u and "no_vocals" not in u), None
        )
        if not vocals_url:
            vocals_url = urls[0] if urls else None
        if not vocals_url:
            return None, [], "unknown"

        # 2-stem：有现成的伴奏轨
        inst_2stem = next(
            (u for u in urls if any(k in u for k in
                                    ("no_vocals", "instrumental", "accompaniment", "karaoke"))),
            None,
        )
        # 4-stem：drums / bass / other 分开给
        stems_4 = [u for u in urls if any(k in u for k in
                                          ("drums", "bass", "other", "piano", "guitar"))]

        if stems >= 4 and len(stems_4) >= 2:
            # 明确要了 4 轨：即使服务顺手给了 no_vocals 也要用 stem，
            # 否则分轨移调那条路永远走不到（分轨才能给鼓/贝斯不同参数）。
            mode, inst_urls = "4stem", stems_4
        elif inst_2stem:
            mode, inst_urls = "2stem", [inst_2stem]
        elif len(stems_4) >= 2:
            mode, inst_urls = "4stem", stems_4
        else:
            mode = "unknown"
            inst_urls = [u for u in urls if u != vocals_url]

        print(f"🎯 人声={_name(vocals_url)}  模式={mode}  "
              f"伴奏轨={[_name(u) for u in inst_urls]}", flush=True)
        if mode == "4stem":
            print("ℹ️ 4-stem：伴奏 = drums + bass + other 相加"
                  "（可按轨移调；不能只取 urls[1]，那是 drums，会变成「只有底鼓」）",
                  flush=True)

        return (_abs(vocals_url),
                [_abs(u) for u in inst_urls] if want_inst else [],
                mode)

    print("❌ Demucs 超时", flush=True)
    return None, [], "unknown"


def process_split_task(task_id: str, task: dict):
    """翻唱：下载整首歌 → WAV → Demucs → 人声 + 伴奏 双文件回传"""
    print(f"\n{'=' * 50}", flush=True)
    print(f"📦 【翻唱分离】任务 {task_id}", flush=True)

    input_raw = TEMP_DIR / f"{task_id}_song.flac"
    if not _download_input(task_id, input_raw, label="song"):
        upload_complete(task_id, result={"error": "download failed"})
        return

    input_wav = TEMP_DIR / f"{task_id}_song.wav"
    print("🎵 FFmpeg 统一格式…", flush=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(input_raw.absolute()),
         "-ar", "44100", "-ac", "2", str(input_wav.absolute())],
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    )
    if not input_wav.exists():
        upload_complete(task_id, result={"error": "ffmpeg failed"})
        return

    vocals_url, inst_urls, mode = _demucs_separate(input_wav, want_inst=True)
    if not vocals_url:
        upload_complete(task_id, result={"error": "demucs failed"})
        return

    vocal_local = TEMP_DIR / f"{task_id}_vocal.wav"
    inst_local = TEMP_DIR / f"{task_id}_inst.wav"
    if not download_file(vocals_url, vocal_local, label="vocal"):
        upload_complete(task_id, result={"error": "vocal download failed"})
        return

    # ---- 取伴奏 ----
    # 4-stem 时不能直接拿某一条 stem（那会只有底鼓）：
    # 把 drums + bass + other 三条下载下来相加 = Demucs 原生伴奏（比相减干净）。
    inst_ok = False
    if mode == "4stem":
        stem_locals = []
        try:
            for i, u in enumerate(inst_urls):
                # 保留原始 stem 名（drums/bass/other），后面按名字给鼓单独压增益
                raw_name = str(u).rsplit("/", 1)[-1] or f"stem{i}.wav"
                p = TEMP_DIR / f"{task_id}_{raw_name}"
                if download_file(u, p, label=f"stem{i}"):
                    stem_locals.append(p)
            if stem_locals:
                inst_ok = _sum_files(
                    stem_locals, inst_local,
                    drum_gain=_env_num("MIX_DRUM_GAIN", 0.7),
                )
        finally:
            for p in stem_locals:
                p.unlink(missing_ok=True)
        if not inst_ok:
            print("↩️ stem 相加失败，改用「原曲 − 人声」重建伴奏", flush=True)
            inst_ok = _rebuild_inst(input_wav, vocal_local, inst_local)
    elif not inst_urls:
        inst_ok = _rebuild_inst(input_wav, vocal_local, inst_local)
    elif len(inst_urls) == 1:
        inst_ok = download_file(inst_urls[0], inst_local, label="inst")
    else:
        inst_ok = _download_and_sum(inst_urls, inst_local)

    if not inst_ok:
        upload_complete(task_id, result={"error": "inst build failed"})
        return

    # ---- 人声纯净度：4-stem 的 vocals 有时混着伴奏，那样 /清唱 就不干净了 ----
    vocal_fixed = None
    if mode == "4stem":
        db_orig = _probe_volume(input_wav, "原曲")
        db_vraw = _probe_volume(vocal_local, "Demucs人声")
        if db_orig > -900 and db_vraw > -900 and db_vraw > db_orig - 2:
            print("⚠️ 人声轨电平和原曲几乎一样 —— vocals 里混了伴奏，"
                  "改用「原曲 − 伴奏」重建人声", flush=True)
            fixed = TEMP_DIR / f"{task_id}_vocal_fixed.wav"
            if _rebuild_vocal(input_wav, inst_local, fixed):
                vocal_fixed = fixed
                vocal_local = fixed
                print("✅ 已重建人声（/清唱 也干净了）", flush=True)

    # ---- 分离质量检查：伴奏到底有没有声音、电平对不对 ----
    db_v = _probe_volume(vocal_local, "分离人声")
    db_i = _probe_volume(inst_local, "分离伴奏")
    if db_i < -55:
        print("⚠️ 伴奏几乎是静音 —— Demucs 返回的 inst 很可能不是伴奏轨", flush=True)
    elif db_v > -900 and db_i > -900 and (db_v - db_i) > 10:
        print(f"⚠️ 伴奏比人声低 {db_v - db_i:.1f} dB，再试一次重建伴奏", flush=True)
        rebuilt = TEMP_DIR / f"{task_id}_inst_rebuilt.wav"
        if _rebuild_inst(input_wav, vocal_local, rebuilt):
            db_r = _probe_volume(rebuilt, "重建伴奏")
            if db_r > db_i + 3:
                inst_local = rebuilt
                print(f"✅ 改用重建伴奏（{db_i:.1f} dB → {db_r:.1f} dB）", flush=True)
            else:
                rebuilt.unlink(missing_ok=True)
                print("ℹ️ 重建伴奏没有更好，仍用 Demucs 原始 inst", flush=True)

    # ⚠️ 必须双文件回传：song_cover 靠 output["vocal"] / output["inst"] 取值
    url = f"{BASE_URL}/api/tasks/complete"
    try:
        # 两个 wav 加起来 70~90MB，超时必须按体积算（原来写死 180 会 ReadTimeout）
        total = (vocal_local.stat().st_size if vocal_local.exists() else 0) + \
                (inst_local.stat().st_size if inst_local.exists() else 0)
        tmo = upload_timeout(total)
        print(f"📤 上传人声 + 伴奏（共 {total / 1048576:.1f} MB），超时 {tmo:.0f}s…", flush=True)
        with open(vocal_local, "rb") as fv, open(inst_local, "rb") as fi:
            files = {
                "vocal": ("vocal.wav", fv, "audio/wav"),
                "inst": ("inst.wav", fi, "audio/wav"),
            }
            data = {"task_id": task_id, "secret": API_SECRET}
            r = requests.post(url, data=data, files=files, proxies=PROXIES, timeout=tmo)
        print(f"✅ 分离双文件回传: HTTP {r.status_code}", flush=True)
        if r.status_code != 200:
            print(f"   服务器返回: {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"❌ 分离回传异常: {type(e).__name__}: {e}", flush=True)
        # 回传失败也要告诉服务器，否则任务永远停在 processing，
        # 服务器侧要傻等到自己的等待上限才报错
        upload_complete(task_id, result={"error": f"upload failed: {type(e).__name__}: {e}"})

    # 分离结果异常时保留文件，方便手动听一下到底是 Demucs 的问题还是混音的问题
    keep = db_i < -55 or (db_v > -900 and db_i > -900 and (db_v - db_i) > 10)
    if keep:
        print(f"🔍 分离结果异常，文件保留在 {TEMP_DIR}（前缀 {task_id}）便于手动检查：", flush=True)
        print(f"   人声 {vocal_local.name}（{db_v:.1f} dB）", flush=True)
        print(f"   伴奏 {inst_local.name}（{db_i:.1f} dB）", flush=True)
    else:
        for p in (input_raw, input_wav, vocal_local, inst_local, vocal_fixed):
            if p:
                p.unlink(missing_ok=True)


# ============================================================
# 2. 手动人声分离（tool_dispatcher 用，单文件 + 可选 mp3）
# ============================================================
def process_audio_separation_task(task_id: str, task: dict):
    target_format = (task.get("target_format") or "wav").lower()
    print(f"\n{'=' * 50}", flush=True)
    print(f"📦 【人声分离】任务 {task_id} → {target_format}", flush=True)

    input_raw = TEMP_DIR / f"{task_id}_raw.flac"
    if not _download_input(task_id, input_raw, label="raw"):
        upload_complete(task_id, result={"error": "download failed"})
        return

    input_wav = TEMP_DIR / f"{task_id}_input.wav"
    print("🎵 FFmpeg 统一格式…", flush=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(input_raw.absolute()),
         "-ar", "44100", "-ac", "2", str(input_wav.absolute())],
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    )
    if not input_wav.exists():
        upload_complete(task_id, result={"error": "ffmpeg failed"})
        return

    vocals_url, _inst_urls, _mode = _demucs_separate(input_wav, want_inst=False)
    if not vocals_url:
        upload_complete(task_id, result={"error": "demucs failed"})
        return

    out_wav = TEMP_DIR / f"{task_id}_vocals.wav"
    if not download_file(vocals_url, out_wav, label="vocals"):
        upload_complete(task_id, result={"error": "vocals download failed"})
        return

    final = out_wav
    out_mp3 = TEMP_DIR / f"{task_id}_vocals.mp3"
    if target_format == "mp3":
        print("🔄 转 MP3…", flush=True)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(out_wav.absolute()),
             "-b:a", "320k", str(out_mp3.absolute())],
            capture_output=True, creationflags=CREATE_NO_WINDOW,
        )
        if out_mp3.exists():
            final = out_mp3

    upload_complete(task_id, file_path=final, filename=f"{task_id}.{target_format}")

    for p in (input_raw, input_wav, out_wav, out_mp3):
        p.unlink(missing_ok=True)


# ============================================================
# 3. RVC 变声（song_cover 用，参数要完整）
# ============================================================
def _run_rvc(in_wav: Path, out_wav: Path, *, pitch, f0_method="rmvpe",
             index_rate=0.5, rms_mix_rate=0.25, protect=0.25,
             timeout=None, env_extra=None) -> bool:
    timeout = float(timeout or RVC_TIMEOUT)
    # 👇 每次变声都打一行，方便从算力机日志直接确认「这次到底用的什么 F0 算法」。
    #    以前不打，只能靠听感猜是 rmvpe 还是 crepe（2026-11-12 用户就在问这个）。
    print(f"🎛️ [RVC] f0_method={f0_method}｜pitch={pitch}｜index={index_rate} "
          f"rms={rms_mix_rate}｜protect={protect}", flush=True)
    cmd = [
        RVC_PYTHON, RVC_SCRIPT,
        "--input", str(in_wav.absolute()),
        "--output", str(out_wav.absolute()),
        "--model", RVC_MODEL,
        "--index", RVC_INDEX,
        "--pitch", str(pitch),
        "--f0_method", str(f0_method),
        "--index_rate", str(index_rate),
        "--rms_mix_rate", str(rms_mix_rate),
        "--protect", str(protect),
    ]
    # env_extra：给 RVC 子进程临时覆盖环境变量（pipeline.py 从 env 读参数，
    # 所以不用改 my_kkI_rvc.py 的命令行就能做参数 A/B）
    _env = None
    if env_extra:
        _env = {**os.environ, **{str(k): str(v) for k, v in env_extra.items()}}
        print(f"🧪 [RVC] env 覆盖 {env_extra}", flush=True)
    try:
        r = subprocess.run(cmd, cwd=RVC_DIR, capture_output=True, env=_env,
                           creationflags=CREATE_NO_WINDOW, timeout=timeout)
    except subprocess.TimeoutExpired:
        print("❌ RVC 超时", flush=True)
        return False
    if r.returncode != 0 or not out_wav.exists():
        err = (r.stderr or b"")[-600:].decode("gbk", errors="ignore")
        print(f"❌ RVC 失败 code={r.returncode}: {err}", flush=True)
        return False
    return True


def _ab_specs(raw) -> list:
    """
    把 task["f0_ab"] 归一成 [{"label","method","env"}]。

    支持两种写法（向后兼容）：
      ["rmvpe", "crepe"]                        → 只换 F0 算法
      [{"label": "crepe_low2", "f0_method": "crepe",
        "env": {"RVC_LOW_LIFT_HZ": "260"}}]     → 换算法 + 换 env 参数
    """
    out = []
    for item in (raw or []):
        if isinstance(item, dict):
            _m = str(item.get("f0_method") or item.get("method") or "rmvpe").strip().lower()
            _lb = str(item.get("label") or _m).strip() or _m
            _env = {str(k): str(v) for k, v in (item.get("env") or {}).items()}
        else:
            _m = str(item).strip().lower()
            _lb, _env = _m, {}
        if not _m:
            continue
        out.append({"label": _lb, "method": _m, "env": _env})
    return out


def process_rvc_task(task_id: str, task: dict):
    """变声。输入支持服务器 URL（task_id 下载）或本地路径。"""
    print(f"\n🎛️ 【RVC】任务 {task_id}", flush=True)

    vocal_path = None
    if task.get("input_url"):
        vocal_path = TEMP_DIR / f"{task_id}_vocal.wav"
        if not download_file(task["input_url"], vocal_path, label="vocal"):
            upload_complete(task_id, result={"error": "vocal download failed"})
            return
    elif task.get("input") and Path(str(task["input"])).exists():
        vocal_path = Path(str(task["input"]))
    else:
        # 默认：按 task_id 从服务器下载
        vocal_path = TEMP_DIR / f"{task_id}_vocal.wav"
        if not _download_input(task_id, vocal_path, label="vocal"):
            upload_complete(task_id, result={"error": "no input / download failed"})
            return

    eng_in = TEMP_DIR / f"{task_id}_rvc_in.wav"
    eng_out = TEMP_DIR / f"{task_id}_rvc_out.wav"
    shutil.copy(vocal_path, eng_in)

    pitch = task.get("pitch", 12)
    print(f"🎛️ RVC pitch={pitch}", flush=True)
    ok = _run_rvc(
        eng_in, eng_out,
        pitch=pitch,
        f0_method=task.get("f0_method", "rmvpe"),
        index_rate=task.get("index_rate", 0.5),
        rms_mix_rate=task.get("rms_mix_rate", 0.25),
        protect=task.get("protect", 0.25),
        env_extra=task.get("rvc_env"),
    )
    if not ok:
        upload_complete(task_id, result={"error": "rvc failed"})
        return

    upload_complete(task_id, file_path=eng_out, filename=f"{task_id}_rvc.wav")

    for p in (eng_in, eng_out):
        p.unlink(missing_ok=True)
    if vocal_path and vocal_path.parent == TEMP_DIR:
        vocal_path.unlink(missing_ok=True)


# ============================================================
# 3.5 翻唱成品：RVC + 混音（ffmpeg 全在算力机做，服务器不再合成）
# ============================================================
def _probe_volume(path: Path, label: str = "") -> float:
    """用 ffmpeg volumedetect 测平均音量（dB），测不出返回 -999。

    为什么要测：服务器那版混音出来「只有人声没有伴奏」，日志里
    ffmpeg returncode=0 看不出任何异常。在算力机把两轨音量打出来，
    一眼就能判断是 Demucs 给的 inst 有问题、还是混音参数把它压没了。
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", str(path.absolute()),
             "-af", "volumedetect", "-f", "null", os.devnull],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=600,
        )
        m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", r.stderr or "")
        if m:
            db = float(m.group(1))
            print(f"🔊 [{label}] 平均音量 {db:.1f} dB（{path.name}）", flush=True)
            return db
    except Exception as e:
        print(f"⚠️ [{label}] 音量检测失败: {type(e).__name__}: {e}", flush=True)
    return -999.0


# ============================================================
# 基频估算（把服务器的 services/song_gender.py 搬过来）
# ============================================================
# 为什么搬到算力机：单段式翻唱（cover 任务）在本地一次跑完
# 下载 → 分离 → 测音高 → RVC → 混音，中间不回传 76MB 的 wav。
# 音高必须在这里算，否则服务器拿不到 vocal 文件就没法测 F0。
F0_PERIODICITY_MIN = float(os.getenv("F0_PERIODICITY_MIN", "0.45"))
SAME_GENDER_MAX_SEMIS = int(os.getenv("RVC_SAME_GENDER_MAX_SEMIS", "3"))
TARGET_F0 = float(os.getenv("RVC_TARGET_F0", "220"))
PITCH_MIN = int(os.getenv("RVC_PITCH_MIN", "-24"))
PITCH_MAX = int(os.getenv("RVC_PITCH_MAX", "24"))


def _f0_search_range(gender_hint: str = "") -> tuple:
    """按性别先验限定搜索范围，压制倍频/半频错误（同 song_gender.py）。"""
    g = str(gender_hint or "").lower()
    if g == "male":
        return 70.0, 200.0
    # ⚠️ 上限原来是 450Hz —— 高音歌的 480Hz 直接测不到，p90 被硬砍，
    #    结果把高音歌当成普通女声处理。提到 800Hz（≈G5）才能覆盖真实高音。
    if g == "female":
        return 150.0, 800.0
    return 70.0, 800.0


# 男女声分界（唱歌场景，比说话略高）。可在 node.env 覆盖。
F0_MALE_MAX = float(os.getenv("F0_MALE_MAX", "155"))      # 中位数 <= 它 → 男
F0_FEMALE_MIN = float(os.getenv("F0_FEMALE_MIN", "185"))  # 中位数 >= 它 → 女
F0_MIXED_LO = float(os.getenv("F0_MIXED_LO", "165"))      # p10 低于它…
F0_MIXED_HI = float(os.getenv("F0_MIXED_HI", "235"))      # …且 p90 高于它 → 疑似对唱
# 倍频兜底：人声主旋律中位数超过这个值才怀疑是 2 倍频。
# ⚠️ 不能设太低 —— 真机踩过：女声歌真实基频 312Hz，被 300 的阈值误判成倍频，
#    强行降到 175Hz → 判成 unknown → 退回 preset +12 → 整首升八度，人声极哑。
#    340 是女高音流行歌曲的实际上限，再高才确实离谱。
F0_OCTAVE_SUSPECT = float(os.getenv("F0_OCTAVE_SUSPECT", "340"))


def _f0_frames(a, sr: int, fmin: float = 70.0, fmax: float = 450.0) -> list:
    """
    分帧自相关，返回 [(帧起始秒, f0), ...]，只含有稳定周期的浊音帧。

    归一化峰值低于 F0_PERIODICITY_MIN 的帧直接丢弃 —— 气声/噪声没有稳定周期，
    不滤的话噪声尖峰会被当成基频，测出来的值偏高。
    """
    try:
        if not a or sr <= 0:
            return []
        win = int(sr * 0.04)
        hop = int(sr * 0.04)
        min_lag = max(2, int(sr / max(50.0, float(fmax))))
        max_lag = max(min_lag + 4, int(sr / max(50.0, float(fmin))))
        if len(a) < win + max_lag + hop:
            return []

        step = max(1, len(a) // 20000)
        sample = a[::step]
        rms = math.sqrt(sum(x * x for x in sample) / max(1, len(sample)))
        if rms <= 0:
            return []
        thr = (rms * 0.6) ** 2 * win

        out = []
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
                corr.append(c * 4.0 / energy)
            best = max(corr)
            if best < F0_PERIODICITY_MIN:
                continue
            peak_thr = best * 0.9
            best_lag = 0
            for k in range(1, n_lag - 1):
                if corr[k] >= peak_thr and corr[k] >= corr[k - 1] and corr[k] >= corr[k + 1]:
                    best_lag = min_lag + k
                    break
            if best_lag:
                # ① 抛物线插值：整数 lag 的精度不够 —— 220Hz 的周期是 36.36 个采样，
                #    lag=36 差 0.36，lag=109（3 倍周期）只差 0.09，全局最大峰反而
                #    落在 3 倍周期上（历史 bug：测出 73Hz）。插值能把真峰精确定位。
                precise = float(best_lag)
                if 0 < k < n_lag - 1:
                    y0, y1, y2 = corr[k - 1], corr[k], corr[k + 1]
                    denom = y0 - 2 * y1 + y2
                    if abs(denom) > 1e-12:
                        delta = 0.5 * (y0 - y2) / denom
                        if abs(delta) <= 1.0:
                            precise = best_lag + delta
                out.append((start / sr, sr / precise))
        return out
    except Exception as e:
        print(f"⚠️ [F0] 自相关异常: {type(e).__name__}: {e}", flush=True)
        return []


def _f0_from_samples(a, sr: int, fmin: float = 70.0, fmax: float = 450.0) -> float:
    """只要基频中位数（兼容旧调用）。测不出返回 0.0。"""
    frames = _f0_frames(a, sr, fmin, fmax)
    if not frames:
        return 0.0
    f0s = [f for _, f in frames]
    med = statistics.median(f0s)
    good = [f for f in f0s if abs(f - med) <= med * 0.3]
    return round(statistics.median(good), 1) if good else round(med, 1)


def _probe_duration(path) -> float:
    """用 ffprobe 拿总时长（秒）。失败返回 0。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW, timeout=60,
        )
        return float((r.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _load_mono8k_at(path, start: float, dur: float):
    """抽 [start, start+dur) 这段 → 8kHz 单声道 array('h')。"""
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix="_f0s.wav")
        os.close(fd)
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{max(0.0, start):.2f}", "-i", str(path),
             "-t", f"{max(0.5, dur):.2f}", "-ac", "1", "-ar", "8000", "-vn", tmp],
            capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW, timeout=300,
        )
        if r.returncode != 0 or not os.path.exists(tmp):
            return None, 0
        with wave.open(tmp, "rb") as w:
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
        if sr != 8000 or len(raw) < 4000:
            return None, 0
        a = array.array("h")
        a.frombytes(raw)
        return a, sr
    except Exception:
        return None, 0
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


def _load_mono8k_sampled(path, budget_sec: float = 100.0, n: int = 6):
    """
    均匀采样：在**全曲**上等间隔取 n 段，总时长不超过 budget_sec，拼起来分析。

    为什么需要它：用户故意只给 100 秒预算（全曲分析太慢），
    但「只取前 100 秒」会漏掉曲末的高音 —— 真机踩过：前 100s p99=761Hz，
    全曲实际 1124Hz，结果降调不够、后面照样哑。
    均匀采样后，100 秒的预算也能摸到曲末，延迟几乎不变。
    """
    total = _probe_duration(path)
    if total <= 0:
        return _load_mono8k(path, budget_sec)
    if total <= budget_sec * 1.2:
        return _load_mono8k(path, total)      # 本来就短，直接全曲

    seg = max(4.0, budget_sec / n)
    chunks = []
    sr = 8000
    for i in range(n):
        center = total * (i + 0.5) / n        # 每段居中，避开最头最尾
        start = max(0.0, center - seg / 2.0)
        a, sr = _load_mono8k_at(path, start, seg)
        if a:
            chunks.append(a)
    if not chunks:
        return _load_mono8k(path, budget_sec)

    merged = array.array("h")
    for c in chunks:
        merged.extend(c)
    print(f"🎧 [F0] 均匀采样 {len(chunks)} 段 × {seg:.0f}s = "
          f"{len(merged) / sr:.0f}s（全曲 {total:.0f}s）", flush=True)
    return merged, sr


def _load_mono8k(path, seconds: float = 45.0):
    """ffmpeg 抽前 N 秒 → 8kHz 单声道 array('h')。失败返回 (None, 0)。"""
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix="_f0.wav")
        os.close(fd)
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-t", str(seconds),
             "-ac", "1", "-ar", "8000", "-vn", tmp],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=300,
        )
        if r.returncode != 0 or not os.path.exists(tmp):
            print(f"⚠️ [F0] ffmpeg 重采样失败: {(r.stderr or '')[-200:]}", flush=True)
            return None, 0
        with wave.open(tmp, "rb") as w:
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
        if sr != 8000 or len(raw) < 8000 * 2:
            return None, 0
        a = array.array("h")
        a.frombytes(raw)
        return a, sr
    except Exception as e:
        print(f"⚠️ [F0] 抽音频异常: {type(e).__name__}: {e}", flush=True)
        return None, 0
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


def pitch_for_range(lo: float, hi: float,
                    model_min: float = 0, model_max: float = 0,
                    model_center: float = 0, mode: str = "",
                    lift_floor: float = 0, src_female: bool = False,
                    mid: float = 0) -> tuple:
    """
    把人声**整段平移**进音色模型的音域 —— 不是判「合格/不合格」，而是找最佳落点。

    参数：
      lo, hi         人声基频的 p10 / p90（不用 min/max，会被个别异常帧带偏）
      model_min/max  音色模型音域，默认读 RVC_VOICE_F0_MIN / MAX
      model_center   模型最舒服的音区中心，默认取 min/max 的几何中心
      lift_floor     「低音低到多少才值得整首升调」的门槛，默认 = model_min。
                     ⚠️ 源本来就是女声时传 VOICE_F0_FEMALE_FLOOR（100）：
                        RVC 变调会连共振峰一起上移，女声歌 +2 半音就「夹嗓子发尖」，
                        而女声歌 110~150Hz 的低音本来就是女声的低音，不该抬。

    返回 (半音数, 说明)

    算法（中心对齐 + 边界约束）：
      1) 先把人声的几何中心 (lo·hi)^0.5 平移对准模型的中心 center
            s_center = 12·log2(center / √(lo·hi))
      2) 再算「不许出界」的可行区间：
            s ≥ 12·log2(MIN/lo)   低音不能低于下限
            s ≤ 12·log2(MAX/hi)   高音不能高于上限
      3) 把 s_center 夹进可行区间 —— 能对准中心就对准，对不准就贴边，
         保证整首歌都落在模型音域里。

    为什么不是「在里面就不动」：
      模型在音域**边缘**唱得最差。人声恰好落在 [130,880] 里但全挤在 850 附近，
      听起来一样会破。所以要往中心靠，而不是只求不出界。
    """
    mmin = float(model_min or VOICE_F0_MIN)
    mmax = float(model_max or VOICE_F0_MAX)
    # 升调门槛：低于它才允许整首往上移（默认跟模型下限一致 = 旧行为）
    floor = float(lift_floor or mmin)
    lo, hi = float(lo or 0), float(hi or 0)

    if lo <= 0 or hi <= 0 or hi < lo:
        return 0, "音域数据无效，保持原调"
    if mmin <= 0 or mmax <= mmin:
        return 0, "模型音域配置无效，保持原调"

    # ===== 男声：按**中位**对齐目标女声（用户 2026-11-12）=====
    # 老逻辑把最低音抬到 135Hz → 平凡之路 lo=75 要 +11，整首高一大截，尖。
    # 用户经验「变 6 是地板，5 就男音」→ 按 p50 对齐 200Hz，再夹 [6, RVC_PITCH_MAX]。
    if (not src_female) and mid and mid > 0 and VOICE_MALE_TARGET_F0 > 0:
        _s = 12 * math.log2(VOICE_MALE_TARGET_F0 / float(mid))
        semi = int(round(_s))
        _low = int(VOICE_MALE_MIN_SEMIS)
        _high = int(RVC_PITCH_MAX)
        if semi < _low:
            semi = _low
            _note = (f"男声 中位 {mid:.0f}Hz 对齐 {VOICE_MALE_TARGET_F0:.0f}Hz 只需 "
                     f"{_s:+.1f}，但低于地板 {_low} → 取 {_low} 半音"
                     f"（再低就变男音了）")
        elif semi > _high:
            semi = _high
            _note = f"男声 中位 {mid:.0f}Hz → 升调封顶 {_high} 半音"
        else:
            _note = (f"男声 中位 {mid:.0f}Hz → 对齐 {VOICE_MALE_TARGET_F0:.0f}Hz"
                     f"（{_s:+.1f} → {semi:+d} 半音；不再按最低音 {lo:.0f}Hz 抬到 {floor:.0f}，"
                     f"那样会 +11 尖掉）")
        return semi, _note

    mode = str(mode or VOICE_F0_MODE).lower()

    if mode == "keep":
        # ===== 对齐模式：不管升还是降，都把「原曲 max」对到目标 max 上 =====
        # 用 floor 保证输出 max 一定 ≤ 目标（不会超），同时尽量接近。
        #   480 → 420：12·log2(420/480) = -2.30 → floor -3 → 404Hz
        #   240 → 420：12·log2(420/240) = +9.69 → floor +9 → 404Hz
        if VOICE_F0_ALIGN_MAX:
            s = 12 * math.log2(mmax / hi)
            semi = int(math.floor(s))
            clamped = max(RVC_PITCH_MIN, min(RVC_PITCH_MAX, semi))
            new_hi = hi * (2 ** (clamped / 12))
            new_lo = lo * (2 ** (clamped / 12))
            gap = new_hi - mmax
            return (clamped,
                    f"max 对齐模式：原曲 max {hi:.0f}Hz → {clamped:+d} 半音"
                    f"｜**成品 {new_lo:.0f}-{new_hi:.0f}Hz**"
                    f"（目标 {mmax:.0f}，差 {gap:+.0f}Hz）")

        # ===== 默认：尽可能保留原调，只在**必须**时才移，且只移刚够的量 =====
        # ⚠️ 只能救一头时救哪一头（用户 2026-11-10 定）：**先救低音**。
        #    低音掉到模型下限以下 → RVC 直接跌回男声，女声都没了；
        #    高音顶破上限 → 只是那几句紧/略破，属于可接受的损失。
        s_lo = 12 * math.log2(floor / lo)    # >0 表示「要升这么多低音才够门槛」
        s_hi = 12 * math.log2(mmax / hi)     # <0 表示「要降这么多高音才不超上限」

        if s_lo <= 0 <= s_hi:
            return 0, (f"人声 {lo:.0f}-{hi:.0f}Hz 在可接受范围"
                       f"（模型 {mmin:.0f}-{mmax:.0f}Hz，升调门槛 {floor:.0f}Hz）"
                       f"→ 保持原调（不动）")

        if VOICE_F0_LOW_FIRST and s_lo > 0:
            # 低音不够 → 无条件抬到门槛之上，高音超上限也认（保住女声）
            semi = int(math.ceil(s_lo))
            if s_hi < 0:
                note = (f"高低**都**超（人声 {lo:.0f}-{hi:.0f}Hz vs 模型 "
                        f"{mmin:.0f}-{mmax:.0f}Hz）→ 只救低音，往上抬")
            else:
                note = f"低音 {lo:.0f}Hz 低于门槛 {floor:.0f}Hz → 往上调"
        elif s_hi < 0:
            # 低音够、只有高音超 → 往下调，但**不许把低音压出下限**
            semi = int(math.floor(s_hi))

            if src_female:
                # ===== 女声专用：三道闸（用户 2026-11-12）=====
                # ① 高音只超一点点 → 不值得降（降调代价是全曲共振峰下移）
                over = (hi / mmax - 1.0) if mmax > 0 else 0.0
                if over < VOICE_F0_FEMALE_DOWN_TRIGGER:
                    return (0,
                            f"高音 {hi:.0f}Hz 只超上限 {mmax:.0f}Hz 的 {over * 100:.1f}%"
                            f"（< {VOICE_F0_FEMALE_DOWN_TRIGGER * 100:.0f}%）→ **不降**："
                            f"降调会让全曲共振峰下移、听感男声化；"
                            f"这点高音略哑可以接受（低的必须保持女声）")
                # ② 降完低音不能低于 VOICE_F0_FEMALE_DOWN_FLOOR（默认 150Hz）
                _dl = 12 * math.log2(VOICE_F0_FEMALE_DOWN_FLOOR / lo) if lo > 0 else 0.0
                if _dl > 0:
                    # 低音本来就低于 150Hz → 既不能降（会掉回男声），也不升（共振峰上移发尖）
                    return (0,
                            f"低音 {lo:.0f}Hz 本来就低于 {VOICE_F0_FEMALE_DOWN_FLOOR:.0f}Hz"
                            f" → 不降也不升（保住女声，高音 {hi:.0f}Hz 顶破就顶破）")
                limit = int(math.ceil(_dl))       # ≤0，这是「能降多少」的极限
                if semi < limit:
                    semi = limit
                    note = (f"高音 {hi:.0f}Hz 超上限 {mmax:.0f}Hz → 只降到"
                            f"低音刚好 {VOICE_F0_FEMALE_DOWN_FLOOR:.0f}Hz 为止"
                            f"（再降就跌回男声）")
                else:
                    note = (f"高音 {hi:.0f}Hz 超上限 {mmax:.0f}Hz"
                            f"（超 {over * 100:.0f}%，整体音区够高，可以降）→ 往下调")
                # ③ 女声最多降 N 半音（降太多整体男声化）
                if VOICE_F0_FEMALE_MAX_DOWN > 0 and semi < -VOICE_F0_FEMALE_MAX_DOWN:
                    semi = -VOICE_F0_FEMALE_MAX_DOWN
                    note += (f"；女声最多降 {VOICE_F0_FEMALE_MAX_DOWN} 半音"
                             f"（再降整体男声化，高音剩下的就哑着）")
            else:
                limit = int(math.ceil(s_lo))      # ≤0，这是「能降多少」的极限
                if semi < limit:
                    semi = limit
                    note = (f"高音 {hi:.0f}Hz 超出上限 {mmax:.0f}Hz → 只降到"
                            f"低音刚好 {floor:.0f}Hz 为止（再降就跌回男声）")
                else:
                    note = f"高音 {hi:.0f}Hz 超出上限 {mmax:.0f}Hz → 往下调"
        else:
            # RVC_PITCH_LOW_FIRST=false 的旧行为：先保高音
            semi = int(math.ceil(s_lo))
            note = f"低音 {lo:.0f}Hz 低于下限 {mmin:.0f}Hz → 往上调（先保高音模式）"

        clamped = max(RVC_PITCH_MIN, min(RVC_PITCH_MAX, semi))
        new_lo = lo * (2 ** (clamped / 12))
        new_hi = hi * (2 ** (clamped / 12))
        # 变声后成品的实际 max 和目标的差：RVC 只吃整数半音，必然有残差
        gap = new_hi - mmax
        warn = ""
        if new_lo < floor * 0.98:
            warn = f"；低音 {new_lo:.0f}Hz 仍低于门槛 {floor:.0f}Hz（跨度太宽，救不回来）"
        elif new_hi > mmax * 1.02:
            warn = (f"；高音顶到 {new_hi:.0f}Hz（超上限 {new_hi - mmax:+.0f}Hz）"
                    f"—— 保低音优先，不为此降调")
        if clamped != semi:
            warn += f"；被 {RVC_PITCH_MIN}~{RVC_PITCH_MAX} 夹到 {clamped:+d}"
        return (clamped,
                f"人声 {lo:.0f}-{hi:.0f}Hz → {clamped:+d} 半音（{note}）"
                f"｜成品 {new_lo:.0f}-{new_hi:.0f}Hz（上限 {mmax:.0f}，距目标 {gap:+.0f}Hz）"
                f"{warn}")

    # ===== center：把整段往模型中心对齐（旧行为，保留可选）=====
    center = float(model_center or 0) or math.sqrt(mmin * mmax)
    center = max(mmin, min(mmax, center))

    s_center = 12 * math.log2(center / math.sqrt(lo * hi))
    s_lo = 12 * math.log2(mmin / lo)
    s_hi = 12 * math.log2(mmax / hi)

    if s_lo <= s_hi:
        s = min(max(s_center, s_lo), s_hi)
        note = "中心对齐" if abs(s - s_center) < 0.5 else "贴边（再往中心就出界了）"
    else:
        s = s_center
        note = "跨度超出音域，只能中心对齐（两端会轻微破音）"

    semi = int(round(s))
    clamped = max(RVC_PITCH_MIN, min(RVC_PITCH_MAX, semi))
    extra = "" if clamped == semi else f"，再被 {RVC_PITCH_MIN}~{RVC_PITCH_MAX} 夹到 {clamped:+d}"

    new_lo = lo * (2 ** (clamped / 12))
    new_hi = hi * (2 ** (clamped / 12))
    return (clamped,
            f"人声 {lo:.0f}-{hi:.0f}Hz → {clamped:+d} 半音（{note}）"
            f"＝ {new_lo:.0f}-{new_hi:.0f}Hz，模型音域 {mmin:.0f}-{mmax:.0f}Hz{extra}")


def exact_semitones(lo: float, hi: float,
                    model_min: float = 0, model_max: float = 0,
                    lift_floor: float = 0, src_female: bool = False) -> float:
    """
    算**浮点**半音，让变声后的成品 max 精确落在 model_max 上。

    为什么需要它：RVC 的 `f0_up_key` 只吃整数，480→420 需要 -2.30 半音，
    取整只能 -2（超）或 -3（低 16Hz）。想要「我调的 max 就是成品 max」，
    只能先用 ffmpeg 做浮点预变调（COVER_PITCH_EXACT=true）。

    lift_floor：低音低到多少才值得升调（源是女声时传 VOICE_F0_FEMALE_FLOOR）。
    """
    mmin = float(model_min or VOICE_F0_MIN)
    mmax = float(model_max or VOICE_F0_MAX)
    floor = float(lift_floor or mmin)
    lo, hi = float(lo or 0), float(hi or 0)
    if lo <= 0 or hi <= 0 or hi < lo:
        return 0.0
    if hi <= mmax and lo >= floor:
        return 0.0                      # 已在音域内 → 原调
    # ⚠️ 低音优先（同 pitch_for_range）：两端都超时先把低音抬进音域，
    #    高音超上限就超 —— 低音跌到下限以下会直接变回男声。
    if lo < floor:
        return 12 * math.log2(floor / lo)    # 升，让 min 正好对齐

    s = 12 * math.log2(mmax / hi)             # 降，让 max 正好对齐
    if src_female:
        # 女声降调三道闸（同 pitch_for_range，用户 2026-11-12）：
        over = (hi / mmax - 1.0) if mmax > 0 else 0.0
        if over < VOICE_F0_FEMALE_DOWN_TRIGGER:
            return 0.0                        # 只超一点点，不值得降
        _dl = 12 * math.log2(VOICE_F0_FEMALE_DOWN_FLOOR / lo) if lo > 0 else 0.0
        if _dl > 0:
            return 0.0                        # 低音本来就低于 150Hz，不降
        s = max(s, _dl)                       # 降完低音不低于 150Hz
        if VOICE_F0_FEMALE_MAX_DOWN > 0:
            s = max(s, -float(VOICE_F0_FEMALE_MAX_DOWN))
    return s


def shift_vocal(src: str, dst: str, semitones: float, sr: int = 44100) -> bool:
    """
    直接对**人声轨**做变调（保持时长不变），用于 COVER_PITCH_MODE=pre。

    asetrate 改采样率 → 音高和速度一起变；再用 atempo 把速度拉回来，时长不变。
    副作用：共振峰会跟着一起移（相当于「花栗鼠/低沉」效果），听感上和 RVC 内部
    变调不一样 —— 正好给用户 A/B 对比用。
    """
    if not semitones:
        return False
    rate = 2 ** (semitones / 12.0)
    af = f"asetrate={sr}*{rate:.6f},aresample={sr},atempo={1.0 / rate:.6f}"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-af", af, dst],
            check=True, capture_output=True, timeout=600,
        )
        return os.path.exists(dst) and os.path.getsize(dst) > 0
    except Exception as e:
        print(f"⚠️ [PITCH] 预变调失败（退回 RVC 内部变调）: {type(e).__name__}: {e}", flush=True)
        return False


def _probe_sr(path) -> int:
    """读音频采样率（ffprobe），失败返回 0。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate", "-of", "default=nw=1:nk=1",
             str(path)],
            capture_output=True, text=True, timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
        return int((r.stdout or "").strip().splitlines()[0])
    except Exception:
        return 0


def _ffmpeg_has_filter(name: str) -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                           capture_output=True, text=True, timeout=25,
                           creationflags=CREATE_NO_WINDOW)
        return name in (r.stdout or "")
    except Exception:
        return False


_HAS_RUBBERBAND = None

# 分轨移调时每条轨的 rubberband 参数（2026-11-12 定的策略）
#   drums → transients=crisp  鼓点/镲片最怕被相位声码器抹平，必须保瞬态
#   bass  → formant=preserved 低频周期长，保共振峰避免糊成一团
#   other → pitchq=quality    吉他/键盘等和声密集，用最高质量
#   mix   → 整体移调（|半音|≤1 时走这条）
_STEM_RB = {
    "drums": "transients=crisp:pitchq=quality",
    "bass": "formant=preserved:pitchq=quality",
    "other": "pitchq=quality",
    "mix": "formant=preserved:pitchq=quality",
}


# 没有 rubberband 时的第二选择：用 RVC runtime 里的 librosa 做相位声码器移调。
# 为什么不用 ffmpeg 的 asetrate：那是「改采样率 → 音高和速度一起变 → atempo 再拉回来」，
# 等于两次时域处理（重采样 + 时间伸缩），高频会有重采样毛刺；
# 相位声码器只做一次 STFT 域处理，不重采样。librosa 随 torchcrepe 已经装好了。
_LIBROSA_SHIFT_CODE = r"""
import sys
import numpy as np
import soundfile as sf
import librosa
src, dst, semis = sys.argv[1], sys.argv[2], float(sys.argv[3])
y, sr = sf.read(src, always_2d=True)
out = np.empty_like(y)
for c in range(y.shape[1]):
    out[:, c] = librosa.effects.pitch_shift(y=y[:, c], sr=sr, n_steps=semis)
sf.write(dst, out, sr)
"""

_HAS_LIBROSA_SHIFT = None


def _librosa_shift_ok() -> bool:
    """探一次：RVC runtime 的 python 有没有 librosa + soundfile。"""
    global _HAS_LIBROSA_SHIFT
    if _HAS_LIBROSA_SHIFT is None:
        try:
            r = subprocess.run(
                [RVC_PYTHON, "-c", "import librosa, soundfile; print('ok')"],
                capture_output=True, text=True, timeout=180,
                creationflags=CREATE_NO_WINDOW,
            )
            _HAS_LIBROSA_SHIFT = (r.returncode == 0 and "ok" in (r.stdout or ""))
        except Exception:
            _HAS_LIBROSA_SHIFT = False
        print(f"🎼 [伴奏] 移调后端：librosa（相位声码器）"
              f"{'可用' if _HAS_LIBROSA_SHIFT else '不可用 → 最后兜底 asetrate'}",
              flush=True)
    return bool(_HAS_LIBROSA_SHIFT)


def _stem_kind(name: str, idx: int) -> str:
    """从 stem 文件名认轨道类型；认不出就按 Demucs 默认顺序 drums/bass/other。"""
    n = str(name).lower()
    for k in ("drums", "bass", "other"):
        if k in n:
            return k
    return ("drums", "bass", "other")[idx] if idx < 3 else "other"


def build_inst_from_stems(stems: dict, semitones: float, out_wav: Path,
                          task_id: str, drum_gain: float = None) -> bool:
    """
    4-stem 伴奏：按轨移调后相加（2026-11-12 用户定的策略）。

      |半音| = 0    → 直接相加（不动）
      |半音| ≤ 1    → 先相加再整体移：只差一个半音，分轨收益 < 多跑两次 ffmpeg 的开销
      |半音| ≥ 2    → 每轨单独移（drums 保瞬态 / bass 保共振峰 / other 高质量）再相加

    为什么 |半音| ≥ 2 要分轨：整体移调会把鼓和镲一起移，
    相位声码器抹瞬态 → 镲片「发碎」、底鼓变形。分轨才能给每条轨用对的参数。
    ⚠️ 但鼓**不能不动**：流行/电子歌的 kick 常常是调过音的，
    鼓保持原调会和移过的 bass 在低频打架，所以鼓也要移，只是换参数。
    """
    parts = {k: Path(p) for k, p in (stems or {}).items() if p and Path(p).exists()}
    if not parts:
        return False
    s = float(semitones or 0)

    if abs(s) < 0.05:
        print("🎼 [伴奏] pitch=0，分轨直接相加（不移调）", flush=True)
        return _sum_files(list(parts.values()), out_wav, drum_gain=drum_gain)

    if abs(s) <= 1.0:
        _tmp_sum = TEMP_DIR / f"{task_id}_inst_sum.wav"
        if not _sum_files(list(parts.values()), _tmp_sum, drum_gain=drum_gain):
            return False
        ok = shift_inst(_tmp_sum, out_wav, s, "mix")
        _tmp_sum.unlink(missing_ok=True)
        if ok:
            print(f"🎼 [伴奏] 整体移调 {s:+.2f} 半音"
                  f"（|半音|≤1，分轨收益小于开销）", flush=True)
        return ok

    shifted, temp_files = [], []
    for kind, p in parts.items():
        dst = TEMP_DIR / f"{task_id}_stem_{kind}_sh.wav"
        if shift_inst(p, dst, s, kind):
            shifted.append(dst)
            temp_files.append(dst)
            print(f"🎼 [伴奏] {kind} 移调 {s:+.2f} 半音"
                  f"（{_STEM_RB.get(kind, 'pitchq=quality')}）", flush=True)
        else:
            shifted.append(p)
            print(f"⚠️ [伴奏] {kind} 移调失败，用原轨"
                  f"（这一层会和整体差 {s:+.2f} 半音）", flush=True)

    ok = _sum_files(shifted, out_wav, drum_gain=drum_gain)
    for p in temp_files:
        p.unlink(missing_ok=True)
    return ok


def shift_inst(src, dst, semitones: float, kind: str = "mix") -> bool:
    """
    伴奏移调（保持时长）。

    为什么必须有这一步：RVC 只移**人声**，伴奏是原调。
    只要 pitch≠0，人声和伴奏就不在同一个调上 ——
    降 1 个半音 = 差小二度，听起来「离调」；男声 +6 = 差纯四度，直接跑调。
    用户 2026-11-12 听出来的就是这个。

    优先用 rubberband（复音素材音质好得多，且能保留共振峰，乐器不会花栗鼠化）；
    没有这个滤镜就退回 asetrate+atempo（音质差但调性是对的，比离调强）。

    kind：分轨移调时传 drums / bass / other，用不同的 rubberband 参数
          （鼓保瞬态、贝斯保共振峰）。整体移调传 mix。
    """
    global _HAS_RUBBERBAND
    if abs(float(semitones)) < 0.05:
        return False
    rate = 2 ** (float(semitones) / 12.0)
    if _HAS_RUBBERBAND is None:
        _HAS_RUBBERBAND = _ffmpeg_has_filter("rubberband")
        print(f"🎼 [伴奏] rubberband 滤镜：{'有（音质优先）' if _HAS_RUBBERBAND else '没有（退回 asetrate）'}",
              flush=True)

    if _HAS_RUBBERBAND:
        _extra = _STEM_RB.get(kind, "pitchq=quality")
        af = f"rubberband=pitch={rate:.6f}:{_extra}"
        try:
            r = subprocess.run(
                ["ffmpeg", "-y", "-i", str(src), "-af", af, "-vn", str(dst)],
                capture_output=True, text=True, timeout=900,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 1024:
                return True
            print(f"⚠️ [伴奏] rubberband 移调失败: {(r.stderr or '')[-300:]}", flush=True)
            return False
        except Exception as e:
            print(f"⚠️ [伴奏] rubberband 移调异常: {type(e).__name__}: {e}", flush=True)
            return False

    # 没有 rubberband → 用 RVC runtime 里的 librosa（相位声码器）。
    # 为什么比 asetrate 好：asetrate 是「改采样率→音高速度一起变→atempo 再拉回来」，
    # 等于**两次**时域处理（重采样 + 时间伸缩），高频会有重采样毛刺；
    # 相位声码器只做一次 STFT 域处理，不重采样。
    if _librosa_shift_ok():
        try:
            r = subprocess.run(
                [RVC_PYTHON, "-c", _LIBROSA_SHIFT_CODE,
                 str(src), str(dst), f"{float(semitones):.6f}"],
                capture_output=True, text=True, timeout=1800,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 1024:
                return True
            print(f"⚠️ [伴奏] librosa 移调失败: {(r.stderr or '')[-400:]}", flush=True)
            return False
        except Exception as e:
            print(f"⚠️ [伴奏] librosa 移调异常: {type(e).__name__}: {e}", flush=True)
            return False

    # 最后兜底：asetrate + atempo（音质最差，但调性是对的）
    sr = _probe_sr(src) or 44100
    af = (f"asetrate={sr}*{rate:.6f},aresample={sr},"
          f"atempo={1.0 / rate:.6f}")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-af", af, "-vn", str(dst)],
            capture_output=True, text=True, timeout=900,
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 1024:
            print(f"⚠️ [伴奏] asetrate 移调失败: {(r.stderr or '')[-300:]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"⚠️ [伴奏] asetrate 移调异常: {type(e).__name__}: {e}", flush=True)
        return False


# 12 平均律音名 / Krumhansl-Kessler 调性模板
_PC_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_KK_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_KK_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def _goertzel_mag(samples, sr: int, freq: float) -> float:
    """单频幅度（Goertzel），纯 Python，不依赖 numpy。"""
    n = len(samples)
    if n < 64 or freq <= 0 or freq >= sr / 2:
        return 0.0
    w = 2.0 * math.pi * freq / sr
    coeff = 2.0 * math.cos(w)
    s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2 = s1
        s1 = s0
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - coeff * s1 * s2)) / n


def estimate_key(path, seconds: float = 12.0) -> dict:
    """
    估调性（首调）：返回 {ok, tonic, mode, name, score, chroma}。

    做法：12 平均律 chroma（C2~B6 逐半音 Goertzel 求和）+ Krumhansl-Kessler 模板，
    24 个大小调各转一次取相关度最高的。纯 Python，不用 numpy/librosa。

    ⚠️ 这是**粗估**：离调/转调/大量鼓点的曲子会飘。它的用途是「人声和伴奏是不是
    同一个调」的交叉验证，不是给人看的绝对真理。
    """
    fd, tmp = tempfile.mkstemp(suffix="_key.wav")
    os.close(fd)
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-t", f"{seconds:.1f}",
             "-ac", "1", "-ar", "4000", "-vn", tmp],
            capture_output=True, text=True, timeout=300,
            creationflags=CREATE_NO_WINDOW,
        )
        if r.returncode != 0 or not os.path.exists(tmp):
            return {"ok": False, "reason": "抽音频失败"}
        with wave.open(tmp, "rb") as w:
            sr = w.getframerate()
            raw = w.readframes(w.getnframes())
        if sr != 4000 or len(raw) < 4000 * 2 * 3:
            return {"ok": False, "reason": "样本太短"}
        a = array.array("h")
        a.frombytes(raw)
        # 归一化到 [-1,1]
        samples = [x / 32768.0 for x in a]

        chroma = [0.0] * 12
        # C2(36) ~ B6(95)，4000Hz 下上限约 1900Hz，够用
        for midi in range(36, 96):
            freq = 440.0 * (2.0 ** ((midi - 69) / 12.0))
            if freq >= sr / 2 - 50:
                break
            chroma[midi % 12] += _goertzel_mag(samples, sr, freq)

        _tot = sum(chroma) or 1.0
        chroma = [c / _tot for c in chroma]

        best = (-2.0, 0, "大调")
        for tonic in range(12):
            for mode, prof in (("大调", _KK_MAJOR), ("小调", _KK_MINOR)):
                rot = [prof[(i - tonic) % 12] for i in range(12)]
                _mu_p = sum(rot) / 12.0
                _mu_c = sum(chroma) / 12.0
                _num = sum((chroma[i] - _mu_c) * (rot[i] - _mu_p) for i in range(12))
                _dp = math.sqrt(sum((rot[i] - _mu_p) ** 2 for i in range(12)))
                _dc = math.sqrt(sum((chroma[i] - _mu_c) ** 2 for i in range(12)))
                corr = _num / (_dp * _dc) if _dp and _dc else 0.0
                if corr > best[0]:
                    best = (corr, tonic, mode)
        return {"ok": True, "score": best[0], "tonic": best[1], "mode": best[2],
                "name": f"{_PC_NAMES[best[1]]}{best[2]}", "chroma": chroma}
    except Exception as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _verdict_from_f0(p10: float, p50: float, p90: float) -> tuple:
    """
    按 F0 分布判男女 / 对唱。返回 (verdict, reason)。
    verdict: male / female / mixed / unknown

    为什么看分布而不是只看中位数：
      单人性别的 F0 是连续单峰；对唱会同时出现低频男声和高频女声，
      p10 被拉到男声区、p90 被拉到女声区，跨度明显拉开。
      只看中位数的话，对唱会得到中间值，反而判成 unknown。
    """
    # ① 跨度覆盖男女两个声部 → 疑似对唱
    if p10 < F0_MIXED_LO and p90 > F0_MIXED_HI:
        return "mixed", f"p10 {p10:.0f}Hz（男声区）↔ p90 {p90:.0f}Hz（女声区）"
    # ② 整体在女声区
    if p50 >= F0_FEMALE_MIN and p10 >= F0_MALE_MAX:
        return "female", f"整体 {p10:.0f}~{p90:.0f}Hz，中位 {p50:.0f}Hz"
    # ③ 整体在男声区
    if p50 <= F0_MALE_MAX and p90 <= F0_FEMALE_MIN:
        return "male", f"整体 {p10:.0f}~{p90:.0f}Hz，中位 {p50:.0f}Hz"
    # ④ 卡在交界，只能靠听一小段
    return "unknown", f"中位 {p50:.0f}Hz 卡在 {F0_MALE_MAX:.0f}~{F0_FEMALE_MIN:.0f}Hz 交界"


def _zcr(a) -> float:
    """
    过零率（Zero-Crossing Rate）—— 音色明亮度的廉价代理，纯 Python 够快。

    为什么加它（用户 2026-11-12）：
      「洋葱 +6 自然，小情歌 +6 就捏着嗓子，但他们音调上下差不多」
      → 光看 F0 分不开，区别在**音色**：吴青峰清亮、气声多；杨宗纬浑厚。
      高频/气声多 → 波形穿零更频繁 → ZCR 高。
      这个值不改任何参数，先只打日志攒数据，攒够几首再定阈值去调
      `protect` / `index_rate`（RVC 的音色维度），而不是继续动 pitch。
    """
    n = len(a)
    if n < 2:
        return 0.0
    z = 0
    prev = a[0]
    for i in range(1, n):
        cur = a[i]
        if (prev < 0) != (cur < 0):
            z += 1
        prev = cur
    return z / float(n - 1)


def analyze_f0(path, gender_hint: str = "", seconds: float = 45.0,
               sampled: bool = False) -> dict:
    """
    测人声基频「分布」（p10 / 中位 / p90），判男女或对唱。

    sampled=True 时不是「取前 N 秒」，而是**在全曲上均匀取 N 段**——
    同样 100 秒预算，既能覆盖曲末的高音，速度也不变慢。

    返回 {ok, frames, p10, p50, p90, p99, max, hi_pct, lo, hi, spread, verdict, reason}
    """
    empty = {"ok": False, "frames": 0, "p10": 0.0, "p50": 0.0, "p90": 0.0,
             "p99": 0.0, "max": 0.0, "hi_pct": 0.0, "zcr": 0.0,
             "lo": 0.0, "hi": 0.0, "spread": 0.0, "verdict": "unknown",
             "reason": "没测到有效基频"}
    if sampled:
        a, sr = _load_mono8k_sampled(path, seconds)
    else:
        a, sr = _load_mono8k(path, seconds)
    if not a:
        return empty

    lo_hz, hi_hz = _f0_search_range(gender_hint)
    frames = _f0_frames(a, sr, lo_hz, hi_hz)
    if not frames and gender_hint:
        print(f"ℹ️ [F0] {gender_hint} 先验 {lo_hz:.0f}~{hi_hz:.0f}Hz 没测到，改用全范围", flush=True)
        lo_hz, hi_hz = _f0_search_range("")
        frames = _f0_frames(a, sr, lo_hz, hi_hz)
    if not frames:
        print("⚠️ [F0] 没测到有效基频（气声/清音为主或整段没在唱）", flush=True)
        return empty

    f0s = sorted(f for _, f in frames)

    # 倍频兜底：中位数高得离谱（> F0_OCTAVE_SUSPECT，默认 300Hz）基本是测到了
    # 2 倍频 —— 348Hz 其实往往是 174Hz，会把整首歌硬降 3 个半音。
    # 人声主旋律中位数极少超过 300Hz（那是女高音的花腔区，不可能整首都在这）。
    # 做法：限制搜索上限重测一遍，结果落到合理范围就采用。
    p50 = statistics.median(f0s)
    if p50 > F0_OCTAVE_SUSPECT:
        hi2 = min(hi_hz, p50 * 0.75)
        if hi2 > lo_hz + 20:
            frames2 = _f0_frames(a, sr, lo_hz, hi2)
            if frames2:
                f0s2 = sorted(f for _, f in frames2)
                p50b = statistics.median(f0s2)
                # ⚠️ 判据必须看**比例**：真倍频的重测值应该约等于原值的 1/2 或 1/3。
                #    原来只看「重测值落在合理范围就采用」——真机踩过：
                #    高音歌真实中位 343Hz，限制上限后测出 191Hz（1.80 倍，不是倍频关系），
                #    却被当成倍频采用 → 高音歌被误判成普通女声，整首被 +10 半音推到哑。
                ratio = (p50 / p50b) if p50b > 0 else 0
                is_octave = (1.85 <= ratio <= 2.15) or (2.85 <= ratio <= 3.15)
                if is_octave and F0_MALE_MAX <= p50b <= F0_OCTAVE_SUSPECT:
                    print(f"⚠️ [F0] 中位 {p50:.0f}Hz 确认是 {ratio:.1f} 倍频 → 改用 {p50b:.0f}Hz",
                          flush=True)
                    frames, f0s = frames2, f0s2
                else:
                    print(f"ℹ️ [F0] 中位 {p50:.0f}Hz 偏高，核对后不是倍频"
                          f"（重测 {p50b:.0f}Hz 只有 {ratio:.2f} 倍）→ 保留，这是真高音", flush=True)

    n = len(f0s)
    p10 = f0s[max(0, int(n * 0.10))]
    p50 = statistics.median(f0s)
    p90 = f0s[min(n - 1, int(n * 0.90))]
    # p99 / max：用来校准「音色模型到底能唱到多高」。
    # p90 只覆盖 90% 的帧，高音歌最尖的那几句会漏掉；p99 更接近真实最高音，
    # 又比裸 max 抗个别异常帧。
    p1 = f0s[max(0, int(n * 0.01))]
    p99 = f0s[min(n - 1, int(n * 0.99))]
    mn = f0s[0]
    mx = f0s[-1]
    spread = round(p90 - p10, 1)
    # 高音帧占比：区分「真的唱上去了」和「孤立的哨音/倍频误判」
    #   >15%  = 真实持续高音，降调有效
    #   <5%   = 孤立的哨音或测量噪声，降调救不了，得单独处理那段
    hi_cnt = sum(1 for f in f0s if f > 800)
    hi_pct = hi_cnt * 100.0 / n
    # 低音段占比：<220Hz 的帧有多少（用户 2026-11-12：
    # 「每句句尾可能 200~300，实际这段感觉都没有 220Hz」）。
    # 源和成品各打一次，就能量化「变声把多少内容压到 220 以下」。
    _low_cnt = sum(1 for f in f0s if f < 220)
    low_pct = _low_cnt / n * 100 if n else 0.0

    verdict, reason = _verdict_from_f0(p10, p50, p90)
    print(f"🔎 [F0] {n} 帧 | 最低 min={mn:.0f} p1={p1:.0f} "
          f"| p10={p10:.0f} p50={p50:.0f} p90={p90:.0f} "
          f"| 最高 p99={p99:.0f} max={mx:.0f} "
          f"| 跨度={spread:.0f}Hz | >800Hz 占 {hi_pct:.1f}%（{hi_cnt}帧）"
          f"| **<220Hz 占 {low_pct:.1f}%**（{_low_cnt}帧）"
          f"→ {verdict}（{reason}）", flush=True)

    # 👇 音色维度：**只记录数值，不下结论**
    #    实测 4 首（2026-11-12）：自然组 情歌 0.1932 / 洋葱 0.2119；
    #    问题组 月声尽 0.2080 / 小情歌 0.2147 —— **完全重叠，ZCR 区分不了**。
    #    所以这里只留数值给后续标定（要区分得上谱质心/共振峰，不是过零率）。
    #    真正解决「念读」的是服务器端 rms_mix_rate（0.25 → 0.8），不是这里。
    zcr = _zcr(a)
    print(f"🎨 [TIMBRE] 过零率 {zcr:.4f}（仅记录：实测无法区分"
          f"「自然/捏嗓子」，别拿它调参）", flush=True)
    if hi_pct < 5 and hi_cnt > 0:
        print(f"   ↳ 高音帧很少（{hi_pct:.1f}%），大概率是哨音或孤立误判，"
              f"降调救不了这几句", flush=True)
    return {"ok": True, "frames": n,
            "min": round(mn, 1), "p1": round(p1, 1), "p10": round(p10, 1),
            "p50": round(p50, 1), "p90": round(p90, 1),
            "p99": round(p99, 1), "max": round(mx, 1),
            "lo": round(mn, 1), "hi": round(mx, 1),
            "hi_pct": round(hi_pct, 1), "zcr": round(zcr, 4),
            "low_pct": round(low_pct, 1),
            "spread": spread, "verdict": verdict, "reason": reason}


def estimate_f0(path, seconds: float = 20.0, gender_hint: str = "") -> float:
    """只要基频中位数（兼容旧调用）。测不出返回 0.0。"""
    stats = analyze_f0(path, gender_hint, seconds)
    return stats.get("p50", 0.0) if stats.get("ok") else 0.0


def _clip_segment(src, start_sec: float, dur: float, out) -> bool:
    """截 [start, start+dur) 到 out（16k 单声道 wav，给人声识别用）。"""
    try:
        cmd = ["ffmpeg", "-y", "-ss", f"{max(0.0, float(start_sec)):.2f}",
               "-i", str(src), "-t", f"{float(dur):.2f}",
               "-ac", "1", "-ar", "16000", "-vn", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=180)
        if r.returncode != 0 or not os.path.exists(str(out)):
            print(f"⚠️ [CLIP] 截 {start_sec:.1f}s 失败: {(r.stderr or '')[-200:]}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"⚠️ [CLIP] 截取异常: {type(e).__name__}: {e}", flush=True)
        return False


def _segment_near_f0(path, target_f0: float, dur: float = 5.0,
                     gender_hint: str = "", seconds: float = 60.0) -> float:
    """
    找「F0 均值最接近 target_f0」的连续 dur 秒起点（秒）。找不到返回 -1。

    用户要求：判不出性别时，按频率范围截一小段给人声识别，
    而不是把整首歌送上去 —— 尽量不用语音识别，用了也只用短段。
    """
    a, sr = _load_mono8k(path, seconds)
    if not a:
        return -1.0
    lo, hi = _f0_search_range(gender_hint)
    frames = _f0_frames(a, sr, lo, hi)
    if not frames:
        lo, hi = _f0_search_range("")
        frames = _f0_frames(a, sr, lo, hi)
    if not frames:
        return -1.0

    best_start, best_diff = -1.0, 1e9
    for t, _ in frames:
        win = [f for (tt, f) in frames if t <= tt < t + dur]
        if len(win) < 3:
            continue
        avg = sum(win) / len(win)
        d = abs(avg - float(target_f0))
        if d < best_diff:
            best_diff, best_start = d, t
    return best_start


def pitch_from_f0(source_f0: float, gender: str = "", target_f0=None) -> int:
    """
    基频 → RVC 的 f0_up_key（变调半音数）。

    ⚠️ 女声（以及判不出性别时）**一律保持原调 pitch=0**，只换音色不改调。
    真机踩过两次：
      ① 按「一律对齐 220Hz」算，空灵女声 300Hz 得到 -5 半音，整首压低
      ② 女声歌测出 312Hz 被判 unknown → 退回 preset pitch=+12，
         整首升一个八度，人声又闷又哑（用户原话「很哑」「要尽量保持原来的调」）

    只有男声才需要移调（抬到女声模型的音域），女→女本来就该 0。
    基频明显超出模型音域（<150 或 >400）时才允许 ±SAME_GENDER_MAX_SEMIS 微调。
    """
    target = float(target_f0 or TARGET_F0)
    try:
        src = float(source_f0)
    except Exception:
        return 0
    if src <= 0 or target <= 0:
        return 0
    semis = 12.0 * math.log2(target / src)
    g = str(gender or "").lower()
    if not target_f0 and g != "male":
        # 保持原调：模型能覆盖的范围（150~400Hz）直接不动
        if 150.0 <= src <= 400.0:
            return 0
        if src >= 150.0:
            # 偏高但没到男声区：只允许极小修正
            semis = max(-SAME_GENDER_MAX_SEMIS, min(SAME_GENDER_MAX_SEMIS, semis))
        # src < 150 说明其实是男声（只是没判出来）→ 保留公式，抬到女声音域
    return int(max(PITCH_MIN, min(PITCH_MAX, round(semis))))


def f0_to_gender(f0: float) -> str:
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


def _sum_files(paths, out_wav: Path, label: str = "stem",
               drum_gain: float = None) -> bool:
    """
    把多条音频相加成一条（4-stem 的 drums+bass+other → 伴奏）。

    drum_gain：单独压一点鼓。真实歌曲里各乐器不是同音量的，
    鼓通常最抢、也最容易把人声顶掉，稍微压 2~3dB 层次感反而更好。
    """
    parts = [Path(p) for p in paths if p and Path(p).exists()]
    if not parts:
        return False
    n = len(parts)
    print(f"➕ 合并 {len(parts)} 条 {label} → {out_wav.name}", flush=True)
    cmd = ["ffmpeg", "-y"]
    for p in parts:
        cmd += ["-i", str(p.absolute())]

    seg = []
    for i, p in enumerate(parts):
        g = 1.0
        if drum_gain is not None and "drum" in p.name.lower():
            g = float(drum_gain)
            print(f"🎚️ {p.name} 音量 ×{g:.2f}", flush=True)
        seg.append(f"[{i}:a]volume={g:.3f}[s{i}];")
    filt = "".join(seg) + "".join(f"[s{i}]" for i in range(n)) + \
        f"amix=inputs={n}:duration=longest:normalize=0[out]"
    cmd += ["-filter_complex", filt, "-map", "[out]",
            "-ar", "44100", "-ac", "2", str(out_wav.absolute())]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=600)
    except Exception as e:
        print(f"❌ 相加异常: {type(e).__name__}: {e}", flush=True)
        return False
    if r.returncode != 0 or not out_wav.exists():
        print(f"❌ 相加失败: {(r.stderr or '')[-400:]}", flush=True)
        return False
    return True


def _download_and_sum(urls, out_wav: Path) -> bool:
    """下载多条 stem 再相加（`_sum_files` 的下载版）。"""
    parts = []
    try:
        for i, u in enumerate(urls):
            p = TEMP_DIR / f"_stem_{i}_{int(time.time())}.wav"
            if not download_file(u, p, label=f"stem{i}"):
                return False
            parts.append(p)
        return _sum_files(parts, out_wav)
    finally:
        for p in parts:
            p.unlink(missing_ok=True)


def _subtract(minuend: Path, subtrahend: Path, out_wav: Path, what: str = "") -> bool:
    """
    out = minuend − subtrahend（把减数反相后相加）。

    Demucs 是加性分解（vocals + drums + bass + other = 原曲），
    所以「原曲 − 人声」≈ 伴奏，「原曲 − 伴奏」≈ 纯人声。
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(Path(minuend).absolute()), "-i", str(Path(subtrahend).absolute()),
        "-filter_complex",
        "[1:a]volume=-1.0[inv];[0:a][inv]amix=inputs=2:duration=first:normalize=0[out]",
        "-map", "[out]", "-ar", "44100", "-ac", "2",
        str(Path(out_wav).absolute()),
    ]
    print(f"🔧 相减重建{what or ''}（{Path(minuend).name} − {Path(subtrahend).name}）…", flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=600)
    except Exception as e:
        print(f"❌ 相减异常: {type(e).__name__}: {e}", flush=True)
        return False
    if r.returncode != 0 or not Path(out_wav).exists():
        print(f"❌ 相减失败: {(r.stderr or '')[-400:]}", flush=True)
        return False
    return True


def _rebuild_inst(original_wav: Path, vocal_wav: Path, out_wav: Path) -> bool:
    """用「原曲 − 人声」重建伴奏。"""
    return _subtract(original_wav, vocal_wav, out_wav, what="伴奏")


def _rebuild_vocal(original_wav: Path, inst_wav: Path, out_wav: Path) -> bool:
    """用「原曲 − 伴奏」重建人声（vocals stem 不干净时用，保证清唱里没有伴奏）。"""
    return _subtract(original_wav, inst_wav, out_wav, what="人声")


def _mix_to_mp3(vocal: Path, inst: Path, out_mp3: Path, bitrate: str = "192k",
                vocal_db: float = -999.0, inst_db: float = -999.0,
                target_diff: float = None, vocal_treble_db: float = None,
                vocal_highpass_hz: float = 0) -> bool:
    """
    人声 + 伴奏 → mp3。

    ⚠️ 三个关键参数（服务器那版「只有人声」就是栽在这上面）：
      · normalize=0      amix 默认按输入数把每一轨砍半，人声一大伴奏就没了
      · duration=longest 用 first 的话人声一短，伴奏直接被截断
      · alimiter          两轨都是原始音量，加限幅器防削波

    ⚠️ 电平平衡是**双向**的（踩过两次，两个方向都出过事）：
      · 第一次 人声 -17.7dB / 伴奏 -34.0dB → 伴奏完全听不见
      · 第二次 三条 stem 相加后伴奏反超人声 → 人声被埋
      所以统一按「人声比伴奏高 target_diff dB」来算伴奏增益，
      不管是抬还是压，都落到同一个目标差上。

    vocal_treble_db：人声高频补偿。RVC 变声后高音容易被模型音域上限压下去，
    给一点 treble 提升能让高音「起得来」（默认 0 不开，用 MIX_VOCAL_TREBLE_DB 调）。
    """
    target = float(target_diff if target_diff is not None
                   else os.getenv("MIX_VOCAL_OVER_INST_DB", "3"))
    treble = float(vocal_treble_db if vocal_treble_db is not None
                   else os.getenv("MIX_VOCAL_TREBLE_DB", "0"))

    i_gain = 1.0
    if vocal_db > -900 and inst_db > -900:
        diff = vocal_db - inst_db          # >0 表示人声更响
        # 让 inst_db + 20log10(i_gain) = vocal_db - target
        i_gain = 10 ** ((diff - target) / 20.0)
        i_gain = max(0.15, min(i_gain, 6.0))    # 限制在 ±16dB 内，别把噪声放大
        db_adj = 20 * math.log10(i_gain)
        if abs(db_adj) > 0.5:
            print(
                f"🎚️ 电平平衡：伴奏 {db_adj:+.1f} dB"
                f"（实测人声-伴奏 {diff:+.1f} dB → 目标 {target:+.1f} dB）",
                flush=True,
            )

    vocal_filter = f"[0:a]volume=1.000"
    # 👇 人声高通：切掉变声后低音区的次谐波/气声伪影（用户 2026-11-12：
    #    「洋葱最低音掉到男音了，男生歌最低必须 140Hz」）。
    #    实测洋葱源 p1=96Hz +6 半音本该 135Hz，RVC 输出却是 70Hz 的伪影，
    #    升调参数压不住，只能在成品上把地板切出来。
    if vocal_highpass_hz and vocal_highpass_hz > 20:
        vocal_filter += f",highpass=f={vocal_highpass_hz:.0f}:poles=2"
        print(f"🎚️ 人声高通 {vocal_highpass_hz:.0f}Hz（切掉低音伪影，保住下限）",
              flush=True)
    if abs(treble) > 0.01:
        vocal_filter += f",treble=g={treble:.1f}"
        print(f"🎚️ 人声高频补偿 {treble:+.1f} dB", flush=True)
    vocal_filter += "[a0]"

    cmd = [
        "ffmpeg", "-y", "-i", str(vocal.absolute()), "-i", str(inst.absolute()),
        "-filter_complex",
        f"{vocal_filter};[1:a]volume={i_gain:.3f}[a1];"
        "[a0][a1]amix=inputs=2:duration=longest:normalize=0,"
        "alimiter=level_in=1:limit=0.95[out]",
        "-map", "[out]", "-ar", "44100", "-ac", "2",
        "-codec:a", "libmp3lame", "-b:a", bitrate,
        str(out_mp3.absolute()),
    ]
    print(f"🎚️ 混音（人声 + 伴奏）→ {out_mp3.name}", flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=600)
    except Exception as e:
        print(f"❌ 混音异常: {type(e).__name__}: {e}", flush=True)
        return False
    if r.returncode != 0 or not out_mp3.exists():
        print(f"❌ 混音失败 code={r.returncode}: {(r.stderr or '')[-600:]}", flush=True)
        return False
    size_mb = out_mp3.stat().st_size / 1048576
    print(f"✅ 混音完成: {out_mp3.name}（{size_mb:.2f} MB）", flush=True)
    return True


def _to_mp3(src: Path, out_mp3: Path, bitrate: str = "192k",
            highpass_hz: float = 0) -> bool:
    """单轨转 mp3（清唱用）。highpass_hz>0 时切掉低音伪影（同混音分支）。"""
    _af = []
    if highpass_hz and highpass_hz > 20:
        _af = ["-af", f"highpass=f={highpass_hz:.0f}:poles=2"]
    cmd = [
        "ffmpeg", "-y", "-i", str(src.absolute()),
        *_af,
        "-ar", "44100", "-ac", "2",
        "-codec:a", "libmp3lame", "-b:a", bitrate,
        str(out_mp3.absolute()),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=600)
    except Exception as e:
        print(f"❌ 转 mp3 异常: {type(e).__name__}: {e}", flush=True)
        return False
    if r.returncode != 0 or not out_mp3.exists():
        print(f"❌ 转 mp3 失败: {(r.stderr or '')[-400:]}", flush=True)
        return False
    return True


def _to_wav(src: Path, out_wav: Path) -> bool:
    """任意音频 → 44.1k 立体声 wav（Demucs 输入 / COVER_WORK_FORMAT=wav 时用）。"""
    cmd = [
        "ffmpeg", "-y", "-i", str(src.absolute()), "-vn",
        "-ar", "44100", "-ac", "2", str(out_wav.absolute()),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=900)
    except Exception as e:
        print(f"❌ 转 wav 异常: {type(e).__name__}: {e}", flush=True)
        return False
    if r.returncode != 0 or not out_wav.exists():
        print(f"❌ 转 wav 失败: {(r.stderr or '')[-300:]}", flush=True)
        return False
    return True


def _is_mp3(path) -> bool:
    """看文件头判断是不是 mp3（ID3 标签 或 0xFFEx 帧同步）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        if len(head) < 2:
            return False
        return head[:3] == b"ID3" or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    except Exception:
        return False


# ============================================================
# 截断到高潮（原来在服务器 song_cover.py，搬到算力机）
# ============================================================
# 服务器只负责存文件和发 QQ，不做任何音频处理。
def _rms_curve(path, hop_sec: float = 0.5) -> list:
    """8kHz 单声道逐格响度曲线 [(起始秒, rms), ...]，用来找高潮收尾。"""
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix="_env.wav")
        os.close(fd)
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(path), "-ac", "1", "-ar", "8000", "-vn", tmp],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=600,
        )
        if r.returncode != 0 or not os.path.exists(tmp):
            print(f"⚠️ [VOICE] 响度曲线抽取失败: {(r.stderr or '')[-200:]}", flush=True)
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
        print(f"⚠️ [VOICE] 响度曲线异常: {type(e).__name__}: {e}", flush=True)
        return []
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


def find_climax_end(path) -> float:
    """找「高潮唱完」的时间点（秒）。0=没找到。至少保留全曲 45%。"""
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


def trim_mp3(src: Path, out: Path, max_mb: float = 3.0, max_sec: float = 120.0,
             bitrate: str = "192k") -> tuple:
    """
    截到高潮收尾，**不降码率**（压码率音质会很烂）。
    返回 (是否截断, 秒数)。False 表示原曲本来就够短。
    """
    try:
        dur = 0.0
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(src)],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=60,
        )
        dur = float((r.stdout or "0").strip() or 0)
        if dur <= 0:
            print("⚠️ [VOICE] 拿不到时长，没法截断", flush=True)
            return False, 0.0

        try:
            kbps = int(str(bitrate).lower().rstrip("k"))
        except Exception:
            kbps = 192
        limit = max_mb * 8 * 1024 / max(1, kbps) * 0.92
        if max_sec and max_sec > 0:
            limit = min(limit, float(max_sec))

        climax = find_climax_end(src)
        if climax > 0:
            print(f"🎯 [VOICE] 高潮收尾约 {climax:.1f}s（全曲 {dur:.1f}s）", flush=True)
            limit = min(limit, climax)

        limit = max(30.0, min(limit, dur))
        if limit >= dur - 2:
            print(f"ℹ️ [VOICE] {dur:.1f}s 不用截断", flush=True)
            return False, dur

        fade = min(3.0, max(1.0, limit * 0.05))
        cmd = [
            "ffmpeg", "-y", "-i", str(src), "-t", f"{limit:.2f}",
            "-af", f"afade=t=out:st={max(0.0, limit - fade):.2f}:d={fade:.2f}",
            "-ar", "44100", "-ac", "2",
            "-codec:a", "libmp3lame", "-b:a", bitrate,
            str(out),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=600)
        if r.returncode != 0 or not out.exists():
            print(f"❌ [VOICE] 截断失败: {(r.stderr or '')[-400:]}", flush=True)
            return False, 0.0
        print(f"✅ [VOICE] 截断 {dur:.1f}s → {limit:.1f}s"
              f"（{out.stat().st_size / 1048576:.2f} MB，码率保持 {bitrate}）", flush=True)
        return True, limit
    except Exception as e:
        print(f"❌ [VOICE] 截断异常: {type(e).__name__}: {e}", flush=True)
        return False, 0.0


# ============================================================
# Qwen-Audio 听人声判男女（key 从 node.env 来）
# ============================================================
# 服务器保留这套代码给以后别的功能用；翻唱链路的这次调用搬到这里，
# 因为人声 wav 不再回传服务器了，服务器根本听不到。
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", os.getenv("QWEN_AUDIO_API_KEY", ""))
DASHSCOPE_BASE_URL = os.getenv("DASHSCOPE_BASE_URL", "").rstrip("/")
QWEN_AUDIO_MODELS = [
    m.strip() for m in os.getenv(
        "QWEN_AUDIO_MODEL",
        "Qwen-Audio-3.0-ASR,qwen-audio-3.0-asr,qwen3-asr-flash,"
        "qwen-audio-turbo-latest,qwen-audio-turbo,qwen2-audio-instruct",
    ).split(",") if m.strip()
]
_AUDIO_PROMPT = (
    "这是一段歌曲的人声。请判断：1) 演唱者是男声还是女声；"
    "2) 音区偏低/中等/偏高。"
    '只输出 JSON：{"gender":"male|female","pitch":"low|mid|high"}，不要别的文字。'
)


def _audio_b64(vocal_path, seconds: float = 20.0, bitrate: str = "48k") -> tuple:
    """抽一小段转 mp3 base64（整段 wav 几十 MB，传不动）。"""
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix="_probe.mp3")
        os.close(fd)
        cmd = [
            "ffmpeg", "-y", "-i", str(vocal_path),
            "-t", str(seconds), "-ac", "1", "-ar", "16000",
            "-codec:a", "libmp3lame", "-b:a", bitrate, "-vn", tmp,
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=180)
        if r.returncode != 0 or not os.path.exists(tmp):
            return "", 0
        data = Path(tmp).read_bytes()
        return base64.b64encode(data).decode(), len(data)
    except Exception as e:
        print(f"⚠️ [QWEN-AUDIO] 音频预处理失败: {type(e).__name__}: {e}", flush=True)
        return "", 0
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


def _audio_b64_sampled(vocal_path, total_sec: float = 0,
                       n: int = 0, bitrate: str = "") -> tuple:
    """
    **全曲均分**抽 n 段、拼成一条音频送人声识别（而不是只看开头 20 秒）。

    为什么要这样（用户 2026-11-10 定的 L3 兜底）：
      只看开头 20 秒 → 遇到「前奏是念白/低语、副歌才是真音色」的歌就判错，
      而且和 F0 那 100 秒均分采样看的根本不是同一批位置，两边结论互相矛盾。
      现在两边看同一批位置：F0 用数值统计，Qwen 用耳朵听音色。

    total_sec：总共送多少秒（默认 QWEN_PROBE_SEC=100）
    n        ：切几段（默认 QWEN_PROBE_SEGS=6 → 每段约 16.7 秒）
    """
    total_budget = float(total_sec or _env_num("QWEN_PROBE_SEC", 100))
    segs = int(n or _env_num("QWEN_PROBE_SEGS", 6))
    segs = max(1, min(12, segs))
    br = bitrate or os.getenv("QWEN_PROBE_BITRATE", "32k")

    dur = _probe_duration(vocal_path)
    # 全曲还没预算长 → 整首都送（没必要切）
    if dur <= 0 or dur <= total_budget * 1.15:
        return _audio_b64(vocal_path, total_budget if dur <= 0 else dur, br)

    seg = total_budget / segs
    starts = []
    for i in range(segs):
        s = dur * i / segs                      # 均分起点
        s = min(s, max(0.0, dur - seg))
        starts.append(s)

    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(suffix="_probe_sampled.mp3")
        os.close(fd)
        parts = []
        for i, s in enumerate(starts):
            parts.append(
                f"[0:a]atrim=start={s:.2f}:end={s + seg:.2f},asetpts=N/SR/TB[a{i}]"
            )
        fc = ";".join(parts) + ";" + "".join(f"[a{i}]" for i in range(segs)) + \
            f"concat=n={segs}:v=0:a=1[out]"
        cmd = ["ffmpeg", "-y", "-i", str(vocal_path),
               "-filter_complex", fc, "-map", "[out]",
               "-ac", "1", "-ar", "16000",
               "-codec:a", "libmp3lame", "-b:a", br, "-vn", tmp]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           creationflags=CREATE_NO_WINDOW, timeout=240)
        if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 1024:
            print(f"⚠️ [QWEN-AUDIO] 均分采样失败，退回开头 {total_budget:.0f}s: "
                  f"{(r.stderr or '')[-200:]}", flush=True)
            return _audio_b64(vocal_path, total_budget, br)
        data = Path(tmp).read_bytes()
        print(f"🎧 [QWEN-AUDIO] 全曲均分 {segs} 段 × {seg:.1f}s = {seg * segs:.0f}s"
              f"（全曲 {dur:.0f}s，{len(data) / 1024:.0f} KB @{br}）", flush=True)
        return base64.b64encode(data).decode(), len(data)
    except Exception as e:
        print(f"⚠️ [QWEN-AUDIO] 均分采样异常: {type(e).__name__}: {e}", flush=True)
        return _audio_b64(vocal_path, total_budget, br)
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except Exception:
                pass


def _extract_json(text: str) -> dict:
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


def _gender_from_text(text: str) -> str:
    """ASR 类模型不按 JSON 回时，从中文描述里猜。"""
    t = str(text or "").lower()
    has_m = any(w in t for w in ("男声", "男性", "男生", "male", "男歌手"))
    has_f = any(w in t for w in ("女声", "女性", "女生", "female", "女歌手"))
    if has_m and has_f:
        return "unknown"      # 「男女对唱」不能算任何一边
    if has_m:
        return "male"
    if has_f:
        return "female"
    return "unknown"


def _pitch_from_text(text: str) -> str:
    t = str(text or "").lower()
    if any(w in t for w in ("偏低", "低沉", "低音", "low")):
        return "low"
    if any(w in t for w in ("偏高", "高亢", "高音", "high")):
        return "high"
    if any(w in t for w in ("中等", "适中", "mid")):
        return "mid"
    return "unknown"


def _dashscope_urls() -> tuple:
    """
    返回 (multimodal_url, chat_url)。

    ⚠️ DASHSCOPE_BASE_URL 的写法很混乱，真机踩过：
       用户填 `https://ws-xxx.maas.aliyuncs.com/compatible-mode/v1`（专属实例的完整 base），
       旧代码再拼 `/compatible-mode/v1/chat/completions`
       → `.../compatible-mode/v1/compatible-mode/v1/chat/completions` → **404**，
       6 个模型全部 404，看起来像「模型名不对」，其实是 URL 拼错了。
    """
    raw = (DASHSCOPE_BASE_URL or "").strip().rstrip("/")
    if not raw:
        raw = "https://dashscope.aliyuncs.com"
    if raw.endswith("/compatible-mode/v1"):
        root = raw[: -len("/compatible-mode/v1")]
        chat = raw + "/chat/completions"
    elif raw.endswith("/v1"):
        root = raw[:-3]
        chat = raw + "/chat/completions"
    else:
        root = raw
        chat = raw + "/compatible-mode/v1/chat/completions"
    return f"{root}/api/v1/services/aigc/multimodal-generation/generation", chat


def qwen_audio_probe(vocal_path, sampled: bool = False) -> dict:
    """
    用阿里百炼 Qwen-Audio 听人声，判断男女 + 音区。
    返回 {"gender","pitch","model","raw"}，失败返回 {}。任何异常都吞掉。

    sampled=True → 送**全曲均分**抽出来的 ~100 秒（L3 兜底用），
                   否则只送开头 20 秒（对唱分声部那种定向确认用）。
    """
    if not DASHSCOPE_API_KEY:
        print("ℹ️ [QWEN-AUDIO] 没配 DASHSCOPE_API_KEY，跳过（用本地 F0）", flush=True)
        return {}
    if not vocal_path or not os.path.exists(str(vocal_path)):
        return {}

    b64, nbytes = (_audio_b64_sampled(vocal_path) if sampled
                   else _audio_b64(vocal_path))
    if not b64:
        return {}
    print(f"🎧 [QWEN-AUDIO] 抽 {nbytes / 1024:.0f} KB 人声送模型判断…", flush=True)

    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
    }
    mm_url, chat_url = _dashscope_urls()
    print(f"🔗 [QWEN-AUDIO] 端点 {mm_url} | {chat_url}", flush=True)
    attempts = [
        (
            mm_url,
            lambda m: {"model": m, "input": {"messages": [{"role": "user", "content": [
                {"audio": f"data:audio/mp3;base64,{b64}"},
                {"text": _AUDIO_PROMPT},
            ]}]}},
        ),
        (
            chat_url,
            lambda m: {"model": m, "messages": [{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}},
                {"type": "text", "text": _AUDIO_PROMPT},
            ]}]},
        ),
    ]

    for url, build in attempts:
        for model in QWEN_AUDIO_MODELS:
            try:
                resp = requests.post(url, json=build(model), headers=headers,
                                     proxies=PROXIES, timeout=60)
                if resp.status_code != 200:
                    print(f"ℹ️ [QWEN-AUDIO] {model} HTTP {resp.status_code}", flush=True)
                    continue
                data = resp.json()
                text = ""
                out = data.get("output") or {}
                if isinstance(out.get("choices"), list) and out["choices"]:
                    content = (out["choices"][0].get("message") or {}).get("content")
                    if isinstance(content, list):
                        text = "".join(str(c.get("text", ""))
                                       for c in content if isinstance(c, dict))
                    elif isinstance(content, str):
                        text = content
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
                if gender == "unknown":
                    gender = _gender_from_text(text)
                if pitch == "unknown":
                    pitch = _pitch_from_text(text)
                if gender == "unknown" and pitch == "unknown":
                    print(f"ℹ️ [QWEN-AUDIO] {model} 无法解析: {str(text)[:120]!r}", flush=True)
                    continue
                print(f"✅ [QWEN-AUDIO] {model} 判定 gender={gender} pitch={pitch}", flush=True)
                return {"gender": gender, "pitch": pitch, "model": model, "raw": text[:300]}
            except Exception as e:
                print(f"ℹ️ [QWEN-AUDIO] {model} 异常: {type(e).__name__}: {e}", flush=True)

    print("⚠️ [QWEN-AUDIO] 全部失败，只用本地 F0", flush=True)
    return {}


def process_rvc_mix_task(task_id: str, task: dict):
    """
    翻唱成品：拉人声（+伴奏）→ RVC → 混音 → mp3 回传。

    task 字段：
      input        人声 wav（服务器上的路径，按 task_id 下载）
      with_inst    true=翻唱（人声+伴奏）  false=清唱（只有人声）
      pitch / voice / f0_method / index_rate / rms_mix_rate / protect
      bitrate      mp3 码率，默认 192k

    ⚠️ 伴奏走同一个 /api/download 端点，但加 ?field=extra_input 取另一个字段，
       这样不用给服务器加新路由。
    """
    with_inst = bool(task.get("with_inst", True))
    print(f"\n{'=' * 50}", flush=True)
    print(f"🎤 【{'翻唱' if with_inst else '清唱'}】任务 {task_id}", flush=True)

    vocal_path = TEMP_DIR / f"{task_id}_vocal.wav"
    if not _download_input(task_id, vocal_path, label="vocal"):
        upload_complete(task_id, result={"error": "vocal download failed"})
        return

    inst_path = TEMP_DIR / f"{task_id}_inst.wav"
    if with_inst:
        inst_url = (f"{BASE_URL}/api/download/{task_id}"
                    f"?secret={API_SECRET}&field=extra_input")
        if not download_file(inst_url, inst_path, label="inst"):
            upload_complete(task_id, result={"error": "inst download failed"})
            return

    eng_in = TEMP_DIR / f"{task_id}_rvc_in.wav"
    eng_out = TEMP_DIR / f"{task_id}_rvc_out.wav"
    shutil.copy(vocal_path, eng_in)

    pitch = task.get("pitch", 0)
    print(f"🎛️ RVC pitch={pitch}", flush=True)
    if not _run_rvc(
        eng_in, eng_out,
        pitch=pitch,
        f0_method=task.get("f0_method", "rmvpe"),
        index_rate=task.get("index_rate", 0.5),
        rms_mix_rate=task.get("rms_mix_rate", 0.25),
        protect=task.get("protect", 0.25),
        env_extra=task.get("rvc_env"),
    ):
        upload_complete(task_id, result={"error": "rvc failed"})
        return

    bitrate = str(task.get("bitrate") or "192k")
    out_mp3 = TEMP_DIR / f"{task_id}_final.mp3"

    if with_inst:
        # 伴奏跟着人声移同一个半音数，否则调性对不上（和单段式同一处修复）
        try:
            _p = float(pitch or 0)
        except Exception:
            _p = 0.0
        if abs(_p) >= 0.05:
            _inst_sh = TEMP_DIR / f"{task_id}_inst_shift.wav"
            if shift_inst(inst_path, _inst_sh, _p):
                inst_path = _inst_sh
                print(f"🎼 [伴奏] 已跟着人声移调 {_p:+.2f} 半音（对齐调性）", flush=True)
            else:
                print(f"⚠️ [伴奏] 移调失败 —— 会和人声差 {_p:+.2f} 半音", flush=True)
        # 混音前先量两轨音量：既用于诊断，也用来做增益补偿
        db_vocal = _probe_volume(eng_out, "变声后人声")
        db_inst = _probe_volume(inst_path, "伴奏")
        if db_inst < -55:
            print("⚠️ 伴奏几乎是静音 —— 检查 Demucs 分离出来的 inst 是否正确", flush=True)
        if not _mix_to_mp3(eng_out, inst_path, out_mp3, bitrate,
                           vocal_db=db_vocal, inst_db=db_inst):
            upload_complete(task_id, result={"error": "mix failed"})
            return
        _probe_volume(out_mp3, "成品")
    else:
        if not _to_mp3(eng_out, out_mp3, bitrate):
            upload_complete(task_id, result={"error": "transcode failed"})
            return

    upload_complete(task_id, file_path=out_mp3, filename=f"{task_id}.mp3")

    for p in (vocal_path, inst_path, eng_in, eng_out, out_mp3):
        p.unlink(missing_ok=True)


def process_probe_task(task_id: str, task: dict):
    """
    `/测歌` 专用：**只跑判定链**，不 RVC、不混音、不回传音频。

    意义：判定链和翻唱走的是同一个 `judge_vocal()`，
          所以这里测出来的性别/音域/pitch，就是真翻唱会用的那一套。
          以前只能靠猜「为什么这首歌被抬了 6 个半音」，现在直接看这一屏。
    """
    print(f"\n{'=' * 50}", flush=True)
    print(f"🔬 【测歌】任务 {task_id}（只判定，不翻唱）", flush=True)

    input_raw = TEMP_DIR / f"{task_id}_song.src"
    source_url = str(task.get("source_url") or "").strip()
    got = False
    if source_url:
        print("🌐 优先直连源站下载…", flush=True)
        got = download_file(source_url, input_raw, label="song-direct", retries=2,
                            extra_headers={"User-Agent": NETEASE_UA})
    if not got:
        got = _download_input(task_id, input_raw, label="song")
    if not got:
        upload_complete(task_id, result={"error": "song download failed"})
        return

    work_format = os.getenv("COVER_WORK_FORMAT", "mp3").strip().lower()
    if work_format == "mp3" and _is_mp3(input_raw):
        input_wav = input_raw
    elif work_format == "wav":
        input_wav = TEMP_DIR / f"{task_id}_song.wav"
        if not _to_wav(input_raw, input_wav):
            input_wav = input_raw
    else:
        input_wav = TEMP_DIR / f"{task_id}_song.mp3"
        if not _to_mp3(input_raw, input_wav, os.getenv("COVER_WORK_BITRATE", "320k")):
            input_wav = input_raw

    print("🔀 Demucs 分离人声（测歌不需要伴奏）…", flush=True)
    vocals_url, _inst_urls, _mode = _demucs_separate(input_wav, want_inst=False)
    if not vocals_url:
        upload_complete(task_id, result={"error": "demucs failed"})
        return
    vocal_local = TEMP_DIR / f"{task_id}_vocal.wav"
    if not download_file(vocals_url, vocal_local, label="vocal"):
        upload_complete(task_id, result={"error": "vocal download failed"})
        return

    _j = judge_vocal(vocal_local, task_id, task)
    st = _j.get("stats") or {}

    # ---------- 可选：把人声本身回传（本地没有这首歌时，分离结果值得留一份）----------
    out_files = []          # [(名字, 路径)]
    if task.get("want_vocal"):
        _vmp3 = TEMP_DIR / f"{task_id}_vocal.mp3"
        if _to_mp3(vocal_local, _vmp3, "192k"):
            out_files.append(("vocal", str(_vmp3)))
            print(f"🎤 [测歌] 人声已转 mp3：{_vmp3.name}", flush=True)

    # ---------- 可选：F0 提取算法 A/B（crepe vs rmvpe）----------
    # 为什么要在算力机上跑：怀疑「每句句尾下楼梯」是 rmvpe 在弱音/气声处失手。
    # 要证伪必须**只换一个变量** —— 同一段人声、同一个 pitch、同一套 RVC 参数，
    # 只改 f0_method，然后实测两边成品的 F0 分布。
    ab = {}
    _ab_specs = _ab_specs(task.get("f0_ab"))
    if _ab_specs:
        _ab_pitch = int(_j.get("pitch") or 0)
        _ab_idx = float(task.get("index_rate", 0.5))
        _ab_rms = float(task.get("rms_mix_rate", 0.8))
        _ab_pro = float(task.get("protect", 0.45))
        _ab_sec = _env_num("F0_ANALYZE_SEC", 100)
        _ab_hp = (VOICE_F0_FEMALE_DOWN_FLOOR if _j.get("src_female")
                  else VOICE_F0_MALE_FLOOR)
        print(f"🧪 [AB] 同一段人声跑 {len(_ab_specs)} 组"
              f"（pitch={_ab_pitch:+d}｜index={_ab_idx} rms={_ab_rms} "
              f"protect={_ab_pro}｜高通 {_ab_hp:.0f}Hz）"
              f"：{[s['label'] for s in _ab_specs]}", flush=True)
        for _sp in _ab_specs:
            _m, _lb, _envx = _sp["method"], _sp["label"], _sp["env"]
            _wav = TEMP_DIR / f"{task_id}_ab_{_lb}.wav"
            if not _run_rvc(vocal_local, _wav, pitch=_ab_pitch, f0_method=_m,
                            index_rate=_ab_idx, rms_mix_rate=_ab_rms,
                            protect=_ab_pro, env_extra=_envx):
                ab[_lb] = {"ok": False, "error": "rvc failed", "f0_method": _m}
                print(f"❌ [AB] {_lb} 跑 RVC 失败（看上面的 RVC 报错）", flush=True)
                continue
            _s = analyze_f0(_wav, "", _ab_sec, sampled=True)
            ab[_lb] = {
                "ok": bool(_s.get("ok")), "f0_method": _m,
                "p1": _s.get("p1"), "p10": _s.get("p10"), "p50": _s.get("p50"),
                "p90": _s.get("p90"), "p99": _s.get("p99"), "max": _s.get("max"),
                "low_pct": _s.get("low_pct"), "frames": _s.get("frames"),
            }
            print(f"🧪 [AB] {_lb} 成品 → min={_s.get('min')} p1={_s.get('p1')} "
                  f"p50={_s.get('p50')} p99={_s.get('p99')} "
                  f"| <220Hz 占 {_s.get('low_pct')}%", flush=True)
            _mp3 = TEMP_DIR / f"{task_id}_ab_{_lb}.mp3"
            if _to_mp3(_wav, _mp3, "192k", highpass_hz=_ab_hp):
                out_files.append((f"ab_{_lb}", str(_mp3)))
            _wav.unlink(missing_ok=True)

    payload = {
        "probe": True,
        "ok": bool(_j.get("ok")),
        "verdict": _j.get("verdict"),
        "gender_source": _j.get("gender_source"),
        "pitch": _j.get("pitch"),
        "exact": _j.get("exact"),
        "use_exact": _j.get("use_exact"),
        "src_female": _j.get("src_female"),
        "lift_floor": _j.get("lift_floor"),
        "used_lo": _j.get("lo"),
        "used_hi": _j.get("hi"),
        "p50": _j.get("p50"),
        "reason": _j.get("reason"),
        "min": st.get("min"), "p1": st.get("p1"), "p10": st.get("p10"),
        "p90": st.get("p90"), "p99": st.get("p99"), "max": st.get("max"),
        "frames": st.get("frames"),
        "low_pct": st.get("low_pct"),
        "f0_verdict": st.get("verdict"),
        "f0_reason": st.get("reason"),
        "model_range": [VOICE_F0_MIN, VOICE_F0_MAX],
        "ab": ab,
    }
    print(f"🔬 【测歌】判定结果 → 性别 {payload['verdict']}"
          f"（来源 {payload['gender_source']}）｜pitch {payload['pitch']}"
          f"｜{payload['reason']}", flush=True)

    # 回传：有成品就带文件 + JSON 一起走（服务器 /api/tasks/complete 认这种组合）
    # ⚠️ 上传通道只有 file / file2 两个槽，多出来的丢掉并记一行日志，别静默少东西。
    if len(out_files) > 2:
        print(f"⚠️ [测歌] 成品有 {len(out_files)} 个，回传通道只带得动 2 个，"
              f"丢弃 {[n for n, _ in out_files[2:]]}", flush=True)
        out_files = out_files[:2]
    if out_files:
        payload["files"] = [{"name": n, "path": p} for n, p in out_files]
        upload_complete(
            task_id,
            file_path=out_files[0][1],
            file2=(out_files[1][1] if len(out_files) > 1 else None),
            result=payload,
        )
    else:
        upload_complete(task_id, result=payload)

    for p in [input_raw, input_wav, vocal_local, *[Path(x[1]) for x in out_files]]:
        p.unlink(missing_ok=True)


def judge_vocal(vocal_path, task_id: str, task: dict) -> dict:
    """
    ★ 唯一的性别/音高判定链 ★  翻唱和 `/测歌` 都走这里，保证「测出来的就是翻唱会用的」。

    三级，命中即停：
      L1 用户指定（`/翻唱 晴天 女`）        → gender_source=override，直接锁定
      L2 歌手判定（DeepSeek 常识 / Tavily）  → gender_source=singer，直接锁定
      L3 兜底：全曲均分 ~100s 人声 → Qwen-Audio 判男女

    ⚠️ 音高/变调**始终**走音域逻辑（analyze_f0 + pitch_for_range），跟性别是两件事：
       性别只决定音色像不像，能不能唱上去看的是音域。

    返回 {"ok", "stats", "verdict", "gender_source", "pitch", "exact",
          "use_exact", "lo", "hi", "lift_floor", "src_female", "p50", "reason"}
    """
    gender_hint = str(task.get("gender_hint") or "").strip().lower()
    gender_lock = str(task.get("gender_lock") or task.get("force_gender") or "").strip().lower()
    gender_src = str(task.get("gender_source") or "").strip().lower()
    if gender_lock not in ("male", "female"):
        gender_lock = ""
    if gender_lock and not gender_src:
        gender_src = "override"

    # ---------- 音域分析（永远要跑：pitch 靠它）----------
    _f0_sec = _env_num("F0_ANALYZE_SEC", 100)
    _t0 = time.time()
    stats = analyze_f0(vocal_path, gender_hint or gender_lock, _f0_sec, sampled=True)
    print(f"⏱️ [F0] 分析耗时 {time.time() - _t0:.1f}s"
          f"（预算 {_f0_sec:.0f}s，全曲均分采样）", flush=True)
    p50 = float(stats.get("p50") or 0)
    verdict = str(stats.get("verdict") or "unknown")
    src = "f0"

    # ---------- L1 / L2：锁定 ----------
    if gender_lock:
        verdict = gender_lock
        src = gender_src or "override"
        _label = {"override": "L1 用户指定", "singer": "L2 歌手判定"}.get(src, f"锁定({src})")
        print(f"🎯 [GENDER] {_label} → 锁定 {verdict}，"
              f"跳过 F0 性别判定、跳过 Qwen-Audio", flush=True)
    else:
        print(f"🎯 [GENDER] 无指定、无歌手判定 → 看 F0 分布：{verdict}"
              f"（{stats.get('reason', '')}）", flush=True)
        # ---------- L3：兜底（F0 判不出 / 对唱）----------
        if verdict not in ("male", "female"):
            print("🎯 [GENDER] F0 判不出 → L3 兜底：全曲均分抽段送 Qwen-Audio", flush=True)
            got = qwen_audio_probe(vocal_path, sampled=True)
            g = str(got.get("gender") or "")
            if g in ("male", "female"):
                verdict = g
                src = "qwen"
                print(f"🎯 [GENDER] L3 Qwen-Audio 判 {g}"
                      f"（音区 {got.get('pitch')}，模型 {got.get('model')}）→ 采用", flush=True)
            elif p50 > 0:
                # Qwen 也判不出：按中位所在声部当主导（对唱场景）
                verdict = "female" if p50 >= F0_FEMALE_MIN else "male"
                src = "f0-dominant"
                print(f"🎯 [GENDER] L3 也判不出 → 按主导声部 {verdict}"
                      f"（中位 {p50:.0f}Hz）", flush=True)
            else:
                print("🎯 [GENDER] L3 也判不出且没有中位 → 不猜，保持原调", flush=True)

    # ---------- 音高：音域逻辑 ----------
    _lo = float(stats.get("p1") or stats.get("p10") or 0)
    _use = VOICE_F0_USE_MAX and "max" or "p99"
    _hi = float(stats.get(_use) or stats.get("p99") or stats.get("p90") or 0)
    _p99_raw = float(stats.get("p99") or stats.get("p90") or 0)
    # 源**本来就是女声**吗？决定「低音不够时要不要整首升调」。
    # RVC 变调是整体移调，共振峰会跟着上移 —— 女声歌 +2 半音就「夹着嗓子发尖」。
    _src_female = (
        verdict == "female"
        or p50 >= F0_FEMALE_MIN
        or (p50 >= F0_MALE_MAX and _p99_raw >= F0_FEMALE_MIN * 1.6)
    )
    _lift_floor = VOICE_F0_FEMALE_FLOOR if _src_female else VOICE_F0_MIN
    if _src_female:
        _lo = float(stats.get("p10") or _lo)

    out = {"ok": bool(stats.get("ok")), "stats": stats, "verdict": verdict,
           "gender_source": src, "p50": p50, "lo": _lo, "hi": _hi,
           "lift_floor": _lift_floor, "src_female": _src_female,
           "pitch": int(task.get("pitch") or 0), "exact": 0.0,
           "use_exact": False, "reason": ""}

    if _lo > 0 and _hi > _lo:
        print(
            f"🎛️ [PITCH] 音高侧源判定 {'女声' if _src_female else '男声/未知'}"
            f"（实测中位 {p50:.0f}Hz）｜性别 {verdict}（来源 {src}）"
            f"｜音域 最低 min={float(stats.get('min') or 0):.0f} "
            f"p1={float(stats.get('p1') or 0):.0f} p10={float(stats.get('p10') or 0):.0f}"
            f"｜p50={p50:.0f}｜最高 p99={_p99_raw:.0f} "
            f"max={float(stats.get('max') or 0):.0f}Hz"
            f"｜模型 {VOICE_F0_MIN:.0f}-{VOICE_F0_MAX:.0f}Hz"
            f"｜用 {_lo:.0f}-{_hi:.0f}Hz"
            f"｜升调门槛 {_lift_floor:.0f}Hz｜{VOICE_F0_MODE}",
            flush=True,
        )
        if _src_female:
            print(f"🎛️ [PITCH] 源是女声 → 不为了凑下限升调"
                  f"（只有低到 {_lift_floor:.0f}Hz 以下才动，避免共振峰上移发尖）",
                  flush=True)
        if COVER_PITCH_EXACT:
            _ex = exact_semitones(_lo, _hi, lift_floor=_lift_floor,
                                  src_female=_src_female)
            out["use_exact"] = True
            out["exact"] = _ex
            if abs(_ex) > 0.05:
                _ol = _lo * (2 ** (_ex / 12))
                _oh = _hi * (2 ** (_ex / 12))
                out["reason"] = (f"精确模式 {_ex:+.2f} 半音 → 成品 "
                                 f"{_ol:.0f}-{_oh:.0f}Hz（目标 max {VOICE_F0_MAX:.0f}）")
                print(f"🎛️ [PITCH] {out['reason']} → RVC pitch=0", flush=True)
            else:
                out["reason"] = f"精确模式：{_lo:.0f}-{_hi:.0f}Hz 已在音域内，原调不变"
                print(f"🎛️ [PITCH] {out['reason']}", flush=True)
        else:
            pitch, why = pitch_for_range(_lo, _hi, model_center=VOICE_F0_CENTER,
                                         lift_floor=_lift_floor,
                                         src_female=_src_female, mid=p50)
            out["pitch"] = pitch
            out["reason"] = why
            print(f"🎛️ [PITCH] {why} → pitch={pitch:+d}", flush=True)
    else:
        out["pitch"] = 0
        out["reason"] = "没有音域分布，保持原调 pitch=0（不用 preset 的 ±12）"
        print(f"🎛️ [PITCH] {out['reason']}", flush=True)
    return out


def process_cover_full_task(task_id: str, task: dict):
    """
    单段式翻唱：一次跑完 下载 → 分离 → 测音高 → RVC → 混音 → 回传 mp3。

    ⚠️ 为什么要把 split + rvc_mix 合成一个任务：
      两段式要在算力机和服务器之间**往返两次 76MB 的 wav**
      （分离回传 vocal+inst 76MB，RVC 前再下载一遍 76MB）。
      一首 5 分钟的歌光传输就 3~6 分钟，任何一次超时前面的 Demucs 全白跑。
      合成一个任务后只有最后 ~5MB 的 mp3 过网，中间文件全留在本地。

    task 字段：
      input          歌曲文件（服务器上的路径，按 task_id 下载）
      with_inst      true=翻唱（人声+伴奏）  false=清唱（只有人声）
      gender_hint    服务器判出来的性别（male/female/""），用来限定 F0 搜索范围
      pitch_override 指定了就跳过 F0 自适应
      voice / f0_method / index_rate / rms_mix_rate / protect / bitrate
    """
    with_inst = bool(task.get("with_inst", True))
    print(f"\n{'=' * 50}", flush=True)
    print(f"🎤 【单段式{'翻唱' if with_inst else '清唱'}】任务 {task_id}", flush=True)

    input_raw = TEMP_DIR / f"{task_id}_song.src"
    # ⚠️ 优先直连源站（网易云 CDN）下载：
    #    服务器出方向带宽是瓶颈（阿里云按上行计费，常见 1~5Mbps），
    #    16MB 的 flac 从服务器拉要几分钟；而算力机自己拉网易云走的是
    #    家宽下行（几十~几百 Mbps），几秒就完事，服务器一个字节都不用出。
    #    直连失败（防盗链/链接过期）再回退到从服务器下载。
    source_url = str(task.get("source_url") or "").strip()
    got = False
    if source_url:
        print("🌐 优先直连源站下载（不占服务器上行带宽）…", flush=True)
        got = download_file(
            source_url, input_raw, label="song-direct", retries=2,
            extra_headers={"User-Agent": NETEASE_UA},
        )
    if not got:
        if source_url:
            print("↩️ 源站直连失败，改从服务器下载（慢但能通）", flush=True)
        got = _download_input(task_id, input_raw, label="song")
    if not got:
        upload_complete(task_id, result={"error": "song download failed"})
        return

    # ---------- 0. 源文件 → 工作格式 ----------
    # 服务器给的是 flac（无损、体积大）。先转成 mp3 再进 Demucs，
    # 后面所有中间文件都是 mp3，磁盘和 IO 都省一大截。
    # 想保无损：node.env 设 COVER_WORK_FORMAT=wav
    work_format = os.getenv("COVER_WORK_FORMAT", "mp3").strip().lower()
    work_bitrate = os.getenv("COVER_WORK_BITRATE", "320k")
    if work_format == "mp3" and _is_mp3(input_raw):
        # 源本来就是 320k mp3（服务器 COVER_SOURCE_LEVEL=exhigh），
        # 再转一遍纯属二次有损，直接拿来用
        input_wav = input_raw
        ok = True
        print("ℹ️ 源文件已是 mp3，跳过转码（避免二次有损）", flush=True)
    elif work_format == "wav":
        input_wav = TEMP_DIR / f"{task_id}_song.wav"
        ok = _to_wav(input_raw, input_wav)
    else:
        input_wav = TEMP_DIR / f"{task_id}_song.mp3"
        ok = _to_mp3(input_raw, input_wav, work_bitrate)
    if not ok:
        print("⚠️ 转工作格式失败，直接用服务器给的源文件", flush=True)
        input_wav = input_raw
    else:
        print(f"🔄 源文件已转 {work_format}"
              f"（{input_raw.stat().st_size / 1048576:.1f} MB → "
              f"{input_wav.stat().st_size / 1048576:.1f} MB）", flush=True)

    # ---------- 1. Demucs 分离（全部本地，不回传 wav） ----------
    vocals_url, inst_urls, mode = _demucs_separate(input_wav, want_inst=True,
                                                   stems=COVER_STEMS)
    if not vocals_url:
        upload_complete(task_id, result={"error": "demucs failed"})
        return

    vocal_local = TEMP_DIR / f"{task_id}_vocal.wav"
    inst_local = TEMP_DIR / f"{task_id}_inst.wav"
    if not download_file(vocals_url, vocal_local, label="vocal"):
        upload_complete(task_id, result={"error": "vocal download failed"})
        return

    inst_ok = False
    # 4-stem 时先只下载、不合成：移调要按 |pitch| 分策略，而 pitch 得等判定链跑完。
    _stems = {}
    if mode == "4stem":
        for i, u in enumerate(inst_urls):
            raw_name = str(u).rsplit("/", 1)[-1] or f"stem{i}.wav"
            p = TEMP_DIR / f"{task_id}_{raw_name}"
            if download_file(u, p, label=f"stem{i}"):
                _stems[_stem_kind(raw_name, i)] = p
        if _stems:
            print(f"🎚️ [伴奏] 拿到 {len(_stems)} 条 stem：{list(_stems)}"
                  f"（等 pitch 定下来再按轨移调）", flush=True)
            inst_ok = True      # 真能不能合成，等后台线程跑完才知道
        else:
            print("↩️ stem 下载全失败，改用「原曲 − 人声」重建伴奏", flush=True)
            inst_ok = _rebuild_inst(input_wav, vocal_local, inst_local)
    elif not inst_urls:
        inst_ok = _rebuild_inst(input_wav, vocal_local, inst_local)
    elif len(inst_urls) == 1:
        inst_ok = download_file(inst_urls[0], inst_local, label="inst")
    else:
        inst_ok = _download_and_sum(inst_urls, inst_local)

    if with_inst and not inst_ok:
        upload_complete(task_id, result={"error": "inst build failed"})
        return

    # ---------- 2. 判性别 + 定音高（判定链见 judge_vocal）----------
    # L1 用户指定 → L2 歌手判定 → L3 全曲均分 100s 送 Qwen-Audio 兜底。
    # 音高/变调始终走音域逻辑，跟性别是两件事。
    pitch_override = task.get("pitch_override")
    pitch = int(task.get("pitch") or 0)
    # 下面「成品人声实测」要用：源人声音域（p1/p99）、源中位数、实际施加的半音数
    _lo = _hi = _p50_src = 0.0
    _shift = 0.0
    _lift_floor = VOICE_F0_MIN      # 升调门槛（源是女声时会放宽到 FEMALE_FLOOR）
    _src_female = False
    _verdict = "unknown"
    _gender_src = ""

    if pitch_override is not None:
        try:
            pitch = int(pitch_override)
            print(f"🎛️ [PITCH] 外部指定 pitch={pitch:+d}（跳过自适应）", flush=True)
        except Exception:
            pass
    else:
        _j = judge_vocal(vocal_local, task_id, task)
        if not _j.get("ok"):
            print("⚠️ [PITCH] 音域分析失败，保持原调继续跑", flush=True)
        _verdict = str(_j.get("verdict") or "unknown")
        _gender_src = str(_j.get("gender_source") or "")
        _p50_src = float(_j.get("p50") or 0)
        _lo = float(_j.get("lo") or 0)
        _hi = float(_j.get("hi") or 0)
        _lift_floor = float(_j.get("lift_floor") or VOICE_F0_MIN)
        _src_female = bool(_j.get("src_female"))
        if _j.get("use_exact"):
            # 精确模式：先用浮点预变调，再让 RVC pitch=0
            _ex = float(_j.get("exact") or 0.0)
            if abs(_ex) > 0.05:
                _shifted = str(Path(vocal_path).with_name("vocal_shifted.wav"))
                if shift_vocal(vocal_path, _shifted, _ex):
                    vocal_path = _shifted
                    _shift = _ex
                    print(f"🎛️ [PITCH] 精确模式：人声已预变调 {_ex:+.2f} 半音，"
                          f"RVC pitch=0", flush=True)
            pitch = 0
        else:
            pitch = int(_j.get("pitch") or 0)
            _shift = float(pitch)
            # pre 模式：先对人声轨整体变调，再让 RVC pitch=0
            if COVER_PITCH_MODE == "pre" and pitch:
                _shifted = str(Path(vocal_path).with_name("vocal_shifted.wav"))
                if shift_vocal(vocal_path, _shifted, pitch):
                    vocal_path = _shifted
                    print(f"🎛️ [PITCH] pre 模式：人声已预变调 {pitch:+d} 半音，"
                          f"RVC pitch 归 0", flush=True)
                    pitch = 0
    # 实际施加到人声上的半音数（pre/精确模式已经移过，pitch 归 0，这里补回来）
    if not _shift:
        _shift = float(pitch)

    # ---------- 2.5 伴奏准备（和 RVC 并行跑） ----------
    # 伴奏移调跑 ffmpeg（rubberband 吃 CPU），RVC 吃 GPU —— 并行不抢资源，
    # 大歌能省 30~60 秒。用户 2026-11-12：「异步跑扛得住」。
    _inst_ready = [False]
    _inst_err = []
    _inst_thread = None

    def _prepare_inst():
        try:
            if not with_inst:
                _inst_ready[0] = True
                return
            if _stems:
                _inst_ready[0] = build_inst_from_stems(
                    _stems, _shift, inst_local, task_id,
                    drum_gain=_env_num("MIX_DRUM_GAIN", 0.7),
                )
                if not _inst_ready[0]:
                    _inst_err.append("stem 合成失败")
            elif abs(_shift) >= 0.05:
                _tmp = TEMP_DIR / f"{task_id}_inst_shift.wav"
                if shift_inst(inst_local, _tmp, _shift, "mix"):
                    try:
                        os.replace(_tmp, inst_local)
                    except Exception:
                        pass
                    _inst_ready[0] = True
                else:
                    # 移调失败别判死任务：离调总比没伴奏强
                    _inst_ready[0] = inst_local.exists()
                    _inst_err.append(f"伴奏移调失败（会差 {_shift:+.2f} 半音）")
            else:
                _inst_ready[0] = inst_local.exists()
        except Exception as e:
            _inst_err.append(f"{type(e).__name__}: {e}")

    if with_inst:
        _inst_thread = threading.Thread(target=_prepare_inst, daemon=True,
                                        name=f"inst-{task_id}")
        _inst_thread.start()
        print(f"🧵 [伴奏] 后台线程已启动，和 RVC 并行"
              f"（策略：{'分轨' if _stems else '整体'}｜{_shift:+.2f} 半音）", flush=True)

    # ---------- 3. RVC 变声 ----------
    eng_in = TEMP_DIR / f"{task_id}_rvc_in.wav"
    eng_out = TEMP_DIR / f"{task_id}_rvc_out.wav"
    shutil.copy(vocal_local, eng_in)
    if not _run_rvc(
        eng_in, eng_out,
        pitch=pitch,
        f0_method=task.get("f0_method", "rmvpe"),
        index_rate=task.get("index_rate", 0.5),
        rms_mix_rate=task.get("rms_mix_rate", 0.25),
        protect=task.get("protect", 0.25),
        env_extra=task.get("rvc_env"),
    ):
        upload_complete(task_id, result={"error": "rvc failed"})
        return

    # ---------- 4. 混音 / 转 mp3 ----------
    bitrate = str(task.get("bitrate") or "192k")
    out_mp3 = TEMP_DIR / f"{task_id}_final.mp3"
    if with_inst:
        # 👇 成品人声实测音域：验证「我设的 RVC_VOICE_F0_MAX」到底有没有生效。
        #    只看预期值不算数 —— RVC 实际输出会受模型、f0 提取、混音影响，
        #    必须以**变声后这条人声轨的实测值**为准。
        try:
            _out = analyze_f0(eng_out, seconds=100000)      # 100000 秒 = 全曲
            if _out.get("ok"):
                _m50 = float(_out["p50"] or 0)
                _m_p1 = float(_out["p1"] or 0)
                _m_p99 = float(_out["p99"] or 0)
                # 👇 倍频复核：RVC 只做移调，成品中位数应该≈源中位数×2^(shift/12)。
                #    实测差 2 倍左右 → 是分析器抓到了二次谐波，不是真的唱高了。
                #    真机踩过：源 96-196Hz +4 半音，成品实测中位 381Hz，
                #    预期只有 191Hz（正好 2 倍）→ 白报一次「⚠️ 仍超出 540」。
                _exp50 = _p50_src * (2 ** (_shift / 12)) if _p50_src > 0 else 0
                _oct = (_m50 / _exp50) if _exp50 > 0 else 0
                _suspect = (1.7 <= _oct <= 2.3) or (2.7 <= _oct <= 3.3)
                if _suspect:
                    print(f"ℹ️ [成品人声] 实测中位 {_m50:.0f}Hz ≈ 预期 {_exp50:.0f}Hz 的 "
                          f"{_oct:.1f} 倍 → 判定为倍频误测，按 1/{_oct:.0f} 折算", flush=True)
                    _m_p1 /= _oct
                    _m_p99 /= _oct
                # 低音是否达标 = 还是不是女声（这是硬指标，优先于高音）
                # 门槛按源性别分：女声源 180Hz（用户定的地板），男声源 140Hz
                # （用户 2026-11-12：「男生歌最低必须 140Hz 要保留」）。
                _floor_hz = (VOICE_F0_FEMALE_DOWN_FLOOR if _src_female
                             else VOICE_F0_MALE_FLOOR)
                _low_ok = _m_p1 >= _floor_hz * 0.95
                _high_ok = _m_p99 <= VOICE_F0_MAX * 1.03
                print(
                    f"🔎 [成品人声] 全曲实测 p1={_m_p1:.0f} p10={_out['p10']:.0f} "
                    f"p50={_m50:.0f} p90={_out['p90']:.0f} p99={_m_p99:.0f} max={_out['max']:.0f}Hz"
                    f"｜低音 {_m_p1:.0f}/{_floor_hz:.0f} "
                    f"{'✅ 还在女声区' if _low_ok else '⚠️ 偏低（混音时会用高通切到地板）'}"
                    f"｜高音 {_m_p99:.0f}/{VOICE_F0_MAX:.0f} "
                    f"{'✅ 达标' if _high_ok else '⚠️ 超出（保低音优先，不为此降调）'}",
                    flush=True,
                )
            else:
                print(f"⚠️ [成品人声] 测不出基频（{_out.get('reason') or '未知'}）", flush=True)
        except Exception as e:
            print(f"⚠️ [成品人声] 音域实测失败: {type(e).__name__}: {e}", flush=True)

        # 👇 伴奏在后台线程里移调（和 RVC 并行），这里等它落地。
        #    伴奏必须跟人声走同一个半音数，否则调性对不上 → 听感「离调/跑调」。
        if _inst_thread is not None:
            _inst_thread.join()
            for _e in _inst_err:
                print(f"⚠️ [伴奏] {_e}", flush=True)
            if not _inst_ready[0]:
                upload_complete(task_id, result={"error": "inst build failed"})
                return

        if COVER_KEY_CHECK:
            _kv = estimate_key(eng_out)
            _ki = estimate_key(inst_local)
            if _kv.get("ok") and _ki.get("ok"):
                _same_key = (_kv["tonic"] == _ki["tonic"]
                             and _kv["mode"] == _ki["mode"])
                print(f"🎼 [调性] 人声 {_kv['name']}（{_kv['score']:.2f}）"
                      f"｜伴奏 {_ki['name']}（{_ki['score']:.2f}）"
                      f"→ {'✅ 同调' if _same_key else '⚠️ 不同调，检查 pitch / 伴奏移调'}",
                      flush=True)
            else:
                print(f"ℹ️ [调性] 测不出（人声 {_kv.get('reason') or '-'}"
                      f"｜伴奏 {_ki.get('reason') or '-'}）", flush=True)

        db_vocal = _probe_volume(eng_out, "变声后人声")
        db_inst = _probe_volume(inst_local, "伴奏")
        if db_inst < -55:
            print("⚠️ 伴奏几乎是静音 —— 检查 Demucs 分离出来的 inst 是否正确", flush=True)
        # 高通地板：女声源 180Hz / 男声源 140Hz，切掉变声后的低音伪影
        _hp = (VOICE_F0_FEMALE_DOWN_FLOOR if _src_female else VOICE_F0_MALE_FLOOR)
        if not _mix_to_mp3(eng_out, inst_local, out_mp3, bitrate,
                           vocal_db=db_vocal, inst_db=db_inst,
                           vocal_highpass_hz=_hp):
            upload_complete(task_id, result={"error": "mix failed"})
            return
        _probe_volume(out_mp3, "成品")
    else:
        _hp2 = (VOICE_F0_FEMALE_DOWN_FLOOR if _src_female else VOICE_F0_MALE_FLOOR)
        if not _to_mp3(eng_out, out_mp3, bitrate, highpass_hz=_hp2):
            upload_complete(task_id, result={"error": "transcode failed"})
            return

    # ---------- 5. 截出「语音版」，连「完整版」一起回传 ----------
    # 服务器不再需要 ffmpeg：语音版直接发，完整版等用户回「要」再发文件。
    # 两个加起来 ~8MB，比两段式的 152MB 往返省 95%。
    voice_max = float(task.get("voice_max_mb")
                      or _env_num("COVER_VOICE_MAX_MB", 3.0))
    # 语音版时长：服务器下发优先（COVER_VOICE_MAX_SEC=90）。
    # 注意 QQ 语音条本身约 60 秒上限，90 秒有概率发不出去 ——
    # 服务器侧发送失败会自动降级发文件，所以这里照截不误。
    voice_sec = float(task.get("voice_max_sec")
                      or _env_num("COVER_VOICE_MAX_SEC", 90))
    short_mp3 = TEMP_DIR / f"{task_id}_short.mp3"
    trimmed, secs = trim_mp3(out_mp3, short_mp3, voice_max, voice_sec, bitrate)
    if trimmed and short_mp3.exists():
        send_file = short_mp3
        send_name = f"{task_id}_short.mp3"
    else:
        send_file = out_mp3
        send_name = f"{task_id}.mp3"
    print(f"📦 [VOICE] 语音版 {send_file.stat().st_size / 1048576:.2f} MB，"
          f"完整版 {out_mp3.stat().st_size / 1048576:.2f} MB", flush=True)

    upload_complete(
        task_id,
        file_path=send_file,
        filename=send_name,
        file2=out_mp3 if trimmed else None,
    )

    for p in (input_raw, input_wav, vocal_local, inst_local, eng_in, eng_out,
              out_mp3, short_mp3, *_stems.values()):
        if p:
            p.unlink(missing_ok=True)


# ============================================================
# 4. TTS（chat 的 type:"tts"）
# ============================================================
# IndexTTS2 的 webui/api 常见端口。TTS_URL 连不上时会扫一遍，
# 命中了就自动改用那个端口 —— 省得为了一个端口号改 node.env 再重启。
TTS_PORT_CANDIDATES = [
    int(p.strip()) for p in
    os.getenv("TTS_PORT_CANDIDATES", "7860,7861,7862,8000,9880,5000").split(",")
    if p.strip().isdigit()
]
# 最近一次 TTS 失败的具体原因 —— 随任务结果回传，服务器日志直接能看到
_TTS_LAST_ERR = ""


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    """TCP 探测：这个端口上有没有人监听（比发 HTTP 请求快，也不会被 404 骗）。"""
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _tts_candidates() -> list:
    """
    返回要尝试的 TTS 地址列表：TTS_URL 优先，其次是「正在监听的候选端口」。

    为什么需要这个：真机踩过 —— 算力机上 IndexTTS2 没启动（或者起在别的端口），
    报错只有一句 `ConnectionError ... [WinError 10061] 目标计算机积极拒绝`，
    从日志上看不出「到底是没启动还是端口写错了」，只能靠人猜。
    """
    from urllib.parse import urlparse
    try:
        u = urlparse(TTS_URL)
        host = u.hostname or "127.0.0.1"
        scheme = u.scheme or "http"
        port = u.port or (443 if scheme == "https" else 80)
        path = u.path or "/api/v1/tts"
    except Exception:
        return [TTS_URL]

    urls = [TTS_URL]
    for p in TTS_PORT_CANDIDATES:
        if p == port:
            continue
        if _port_open(host, p):
            urls.append(f"{scheme}://{host}:{p}{path}")
    return urls


def _listening_ports() -> list:
    """
    本机所有 LISTENING 的 TCP 端口（只留 >=1024 的非系统端口）。

    为什么需要：IndexTTS2 的 webui 起了，但端口不在 TTS_PORT_CANDIDATES 里时，
    老日志只会说「常见端口全都没人听」，看不出它到底起在 7863 还是 8188。
    直接把本机真实监听列表打出来，一眼就能对上。
    """
    ports = set()
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
            timeout=8, creationflags=CREATE_NO_WINDOW,
        ).stdout
    except Exception:
        return []
    for line in out.splitlines():
        parts = line.split()
        # TCP  Local  Foreign  State  PID
        if len(parts) < 4 or parts[-2].upper() != "LISTENING":
            continue
        _port = parts[1].rsplit(":", 1)[-1]
        if _port.isdigit() and int(_port) >= 1024:
            ports.add(int(_port))
    return sorted(ports)


def _tts_diag() -> str:
    """TTS 连不上时给出**可执行结论**，而不是只丢一个 ConnectionError。"""
    from urllib.parse import urlparse
    try:
        u = urlparse(TTS_URL)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = u.path or "/api/v1/tts"
    except Exception:
        return f"TTS_URL 解析不了: {TTS_URL!r}"

    lines = [f"TTS_URL = {TTS_URL}（host={host} port={port} path={path}）"]
    if _port_open(host, port):
        lines.append(f"✅ 端口 {port} 在监听 → 服务活着，但接口 {path} 不对（看上面的 HTTP 状态码）")
    else:
        lines.append(f"❌ 端口 {port} 没人监听 → IndexTTS2 没启动，或者起在别的端口")
        alive = [p for p in TTS_PORT_CANDIDATES if p != port and _port_open(host, p)]
        if alive:
            lines.append(f"↳ 但这些端口有人在听：{alive} → 把 node.env 的 TTS_URL 改成对应端口")
        else:
            lines.append("↳ 常见端口全都没人听 → 先去算力机把 IndexTTS2 的 webui/api 启动起来")
            _alive_ports = _listening_ports()
            if _alive_ports:
                _shown = _alive_ports[:24]
                lines.append(f"↳ 本机正在监听的非系统端口：{_shown}"
                             f"{' …' if len(_alive_ports) > len(_shown) else ''}")
                lines.append("   如果 IndexTTS2 在里面，把 node.env 的 TTS_URL 改成对应端口；"
                             "不在里面就是真没启动")
    if not Path(TTS_PROMPT_WAV).exists():
        lines.append(f"❌ 参考音不存在：{TTS_PROMPT_WAV}（IndexTTS 必须有参考音才能合成）")
    return "\n".join("   " + s for s in lines)


def _tts_indextts(text: str) -> Path | None:
    """IndexTTS2：POST /api/v1/tts（spk_audio 参考音）→ 产物落在 outputs 目录"""
    global _TTS_LAST_ERR
    _TTS_LAST_ERR = ""
    prompt_wav = Path(TTS_PROMPT_WAV)
    if not prompt_wav.exists():
        _TTS_LAST_ERR = f"参考音不存在: {prompt_wav}"
        print(f"❌ 参考音不存在: {prompt_wav}（把 prompt_kki_cut.wav 放到 local_temp）", flush=True)
        return None
    if not TTS_OUTPUT_DIR.exists():
        _TTS_LAST_ERR = f"输出目录不存在: {TTS_OUTPUT_DIR}"
        print(f"❌ IndexTTS 输出目录不存在: {TTS_OUTPUT_DIR}（改 TTS_OUTPUT_DIR）", flush=True)
        return None

    req_start = time.time()
    data = {
        "client_id": "node_client", "text": text,
        "emo_mode": 0, "emo_weight": 0.20, "use_random": "false",
        "max_text_tokens_per_segment": 60,
        "do_sample": "true",
        "top_p": 0.35, "top_k": 15, "temperature": 0.30,
        "length_penalty": 0, "num_beams": 3, "repetition_penalty": 1.5,
        "max_mel_tokens": 4000,
        "interval_silence": 80, "file_naming": "title_time",
    }
    # 逐个候选地址试：TTS_URL 优先，其次是「正在监听的其他端口」
    resp = None
    used_url = ""
    for url in _tts_candidates():
        try:
            with open(prompt_wav, "rb") as f:
                files = {"spk_audio": (prompt_wav.name, f, "audio/wav")}
                r = requests.post(url, data=data, files=files,
                                  proxies=PROXIES, timeout=TTS_TIMEOUT)
            if r.status_code == 200:
                resp, used_url = r, url
                if url != TTS_URL:
                    print(f"ℹ️ IndexTTS 改用 {url}（TTS_URL 那个端口不通）", flush=True)
                break
            print(f"⚠️ IndexTTS {url} → HTTP {r.status_code}: {(r.text or '')[:160]}", flush=True)
        except Exception as e:
            print(f"⚠️ IndexTTS {url} → {type(e).__name__}: {str(e)[:160]}", flush=True)

    if resp is None:
        _diag = _tts_diag()
        _TTS_LAST_ERR = "IndexTTS 连不上｜" + _diag.replace("\n", " ").strip()
        print("❌ IndexTTS 全部地址都失败，诊断：", flush=True)
        print(_diag, flush=True)
        return None

    # 找最新的产物（并确认是本次生成的）
    for _ in range(10):
        time.sleep(1)
        wavs = list(TTS_OUTPUT_DIR.glob("*.wav"))
        if not wavs:
            continue
        wavs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
        latest = wavs[0]
        if latest.stat().st_mtime >= req_start - 5:
            print(f"✅ IndexTTS 输出: {latest.name}", flush=True)
            return latest
    _TTS_LAST_ERR = f"没有产出新 wav（TTS_OUTPUT_DIR={TTS_OUTPUT_DIR}）"
    print("❌ IndexTTS 没有产出新 wav（检查 TTS_OUTPUT_DIR 是否指对）", flush=True)
    return None


def _tts_http(text: str, out_path: Path) -> bool:
    """通用 HTTP TTS：返回 wav 二进制，或 {"audio_url"/"output"/"path"}"""
    try:
        resp = requests.post(
            TTS_URL,
            json={"text": text, "text_lang": "zh", "format": "wav", "stream": False},
            proxies=PROXIES, timeout=TTS_TIMEOUT,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"❌ HTTP TTS 请求异常: {type(e).__name__}: {e}", flush=True)
        return False

    ctype = (resp.headers.get("content-type") or "").lower()
    if "json" in ctype:
        d = resp.json()
        for key in ("output", "path", "file", "audio_path"):
            p = d.get(key)
            if p and Path(p).exists():
                out_path.write_bytes(Path(p).read_bytes())
                return out_path.stat().st_size > 0
        for key in ("audio_url", "url"):
            u = d.get(key)
            if u:
                if u.startswith("/"):
                    u = f"{TTS_URL.split('/api')[0]}{u}"
                return download_file(u, out_path, label="tts_url")
        print(f"⚠️ TTS 返回 JSON 但找不到音频字段: {list(d.keys())}", flush=True)
        return False

    out_path.write_bytes(resp.content)
    return out_path.stat().st_size > 0


def _tts_cmd(text: str, out_path: Path) -> bool:
    cmd = TTS_CMD.format(text=text, output=str(out_path.absolute()))
    print(f"🎙️ 执行 TTS_CMD: {cmd[:120]}", flush=True)
    r = subprocess.run(cmd, shell=True, capture_output=True)
    if r.returncode != 0:
        print(f"❌ TTS_CMD 失败: {r.stderr.decode('gbk', errors='ignore')[:400]}", flush=True)
        return False
    return out_path.exists() and out_path.stat().st_size > 0


def process_tts_task(task_id: str, task: dict):
    """
    task = {"text": "..."}（可选 prompt_wav / pitch）
    IndexTTS2 合成 → 可选 RVC 变声 → 上传 wav。
    """
    text = str(task.get("text") or task.get("input") or "").strip()
    if not text:
        upload_complete(task_id, result={"error": "empty text"})
        return

    print(f"\n🎤 【TTS】任务 {task_id}，文本 {len(text)} 字，引擎={TTS_ENGINE}", flush=True)
    raw_wav = TEMP_DIR / f"{task_id}_tts_raw.wav"

    if TTS_ENGINE == "indextts":
        produced = _tts_indextts(text)
        if not produced:
            # 带上具体原因，服务器日志里就能直接看到「为什么降级云端」
            upload_complete(task_id, result={"error": "indextts failed",
                                             "detail": _TTS_LAST_ERR or "unknown"})
            return
        shutil.copy(produced, raw_wav)
    elif TTS_ENGINE == "cmd":
        if not _tts_cmd(text, raw_wav):
            upload_complete(task_id, result={"error": "tts cmd failed"})
            return
    else:  # http
        if not _tts_http(text, raw_wav):
            upload_complete(task_id, result={"error": "tts http failed"})
            return

    if not raw_wav.exists() or raw_wav.stat().st_size < 1024:
        upload_complete(task_id, result={"error": "tts produced empty wav"})
        return

    # 可选：过一遍 RVC 保持大肥鱼音色（挂了就上传原音）
    final = raw_wav
    if TTS_USE_RVC:
        eng_in = TEMP_DIR / f"{task_id}_tts_in.wav"
        eng_out = TEMP_DIR / f"{task_id}_tts_out.wav"
        shutil.copy(raw_wav, eng_in)
        if _run_rvc(eng_in, eng_out, pitch=task.get("pitch", TTS_RVC_PITCH),
                    f0_method=task.get("f0_method") or TTS_RVC_F0, timeout=300):
            print("✅ TTS 已过 RVC 变声", flush=True)
            final = eng_out
        else:
            print("⚠️ RVC 失败，直接上传 TTS 原音", flush=True)
        eng_in.unlink(missing_ok=True)

    upload_complete(task_id, file_path=final, filename=f"{task_id}_tts.wav")

    for p in (raw_wav, TEMP_DIR / f"{task_id}_tts_out.wav"):
        p.unlink(missing_ok=True)


# ============================================================
# 5. 两天一复盘（reflection）
# ============================================================
def _extract_json(text: str) -> dict | None:
    """从 LLM 输出抠 JSON，容忍 ```json 包裹和前后废话"""
    if not text:
        return None
    t = text.strip()
    t = t.replace("```json", "```")
    if t.startswith("```"):
        lines = t.split("\n")
        t = "\n".join(lines[1:-1] if lines and lines[-1].startswith("```") else lines[1:])
    try:
        return json.loads(t)
    except Exception:
        pass
    try:
        s = t.index("{")
        e = t.rindex("}") + 1
        return json.loads(t[s:e])
    except Exception:
        return None


def _bundle_to_prompt(bundle: dict) -> str:
    """
    把服务器的 bundle 转成给 qwen2 的复盘指令。
    兼容两套字段名（新旧服务器版本都可能发过来）：
      memory_dir / catalog          —— 记忆目录
      memory_details / window_details —— 记忆详情
      mood_history / mood.shifts    —— 心情波动
      persona 可能是 str 也可能是 {名字: 文本}
    """
    def _lines(x, maxn=30):
        if isinstance(x, list):
            out = []
            for i in x[:maxn]:
                if isinstance(i, dict):
                    mid = i.get("id") or ""
                    emo = "❗" if i.get("emotion_hit") else ""
                    body = i.get("summary") or i.get("text_full") or i.get("text") or ""
                    out.append(f"- [{mid}]{emo} {str(body)[:120]}")
                else:
                    out.append(f"- {i}")
            return "\n".join(out)
        return str(x)

    profiles = bundle.get("profiles", {}) or {}
    bonds = bundle.get("bonds", {}) or {}
    memory_dir = bundle.get("memory_dir") or bundle.get("catalog") or []
    memory_details = bundle.get("memory_details") or bundle.get("window_details") or []
    mood_obj = bundle.get("mood") or {}
    mood_hist = bundle.get("mood_history") or mood_obj.get("shifts") or []
    journals = bundle.get("journals", []) or []
    ctx = bundle.get("contexts", []) or []
    thoughts = bundle.get("thoughts", []) or []
    window = bundle.get("window", {}) or {}

    persona = bundle.get("persona", "") or ""
    if isinstance(persona, dict):
        persona = next(iter(persona.values()), "") if persona else ""

    # 把 bonds 里的相处印象并进 profile 展示
    profile_view = {}
    for qq, p in profiles.items():
        if not isinstance(p, dict):
            continue
        item = dict(p)
        b = bonds.get(str(qq)) or {}
        if isinstance(b, dict) and b.get("notes"):
            item["bond_notes"] = b["notes"][-8:]
        profile_view[qq] = item

    return f"""你就是这个角色本人。现在做过去两天的自我复盘。

【复盘区间】{window.get('since_human', '?')} ~ {window.get('until_human', '?')}

【角色卡摘要】
{str(persona)[:800]}

【人物档案 + 相处印象（带情绪标签）】
{json.dumps(profile_view, ensure_ascii=False, indent=2)[:2000]}

【记忆目录（带 id 和情绪 ❗）】
{_lines(memory_dir, 60)}

【记忆详情（这两天的）】
{_lines(memory_details, 40)}

【心情波动流水】
{_lines(mood_hist, 40)}

【近期日记】
{_lines(journals, 5)}

【近期上下文片段】
{_lines(ctx, 30)}

【你当时的思考片段（推理模型才有）】
{_lines([str(t.get("reasoning", ""))[:200] for t in thoughts if isinstance(t, dict)], 15)}

请按以下 JSON 格式输出（只输出 JSON，不要任何解释、不要 Markdown）：
{{
  "journal": "第一人称写一段 200-400 字的日记，记录这两天发生了什么、你感受到什么",
  "impression": "对这个人的核心印象总结，不超过 40 字",
  "archive_keywords": ["过时记忆的原文关键词，例如：白米饭"]
}}

规则：
- journal 必须第一人称，像写日记，不要列点、不要 Markdown
- impression 是长期印象，不要复述具体事件
- archive_keywords 只列真正过时的记忆关键词，5~15 字，最多 5 条；无则空数组
- 宁少删不误删"""


def process_reflection_task(task_id: str, task: dict):
    """两天一复盘。服务器不一定能反连算力机，所以走任务队列。"""
    print(f"\n📓 【两天一复盘】任务 {task_id}", flush=True)

    bundle = task.get("bundle") or {}
    if not bundle:
        fpath = TEMP_DIR / f"{task_id}_bundle.json"
        if task.get("bundle_url"):
            if not download_file(task["bundle_url"], fpath, expect_audio=False, label="bundle"):
                upload_complete(task_id, result={"error": "bundle download failed"})
                return
        else:
            # 服务器的标准做法：bundle 就是任务的 input 文件
            if not _download_input(task_id, fpath, expect_audio=False, label="bundle"):
                upload_complete(task_id, result={"error": "no bundle"})
                return
        try:
            bundle = json.loads(fpath.read_text(encoding="utf-8"))
        except Exception as e:
            upload_complete(task_id, result={"error": f"bundle parse: {e}"})
            return

    instruction = bundle.get("instruction")
    prompt = _bundle_to_prompt(bundle)
    if instruction:
        prompt = f"{instruction}\n\n{prompt}"
    print(f"📝 复盘 Prompt {len(prompt)} 字，调用 {LLM_MODEL}…", flush=True)

    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 1200,
    }
    try:
        r = requests.post(LLM_URL, json=payload, proxies=PROXIES, timeout=LLM_TIMEOUT)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        upload_complete(task_id, result={"error": f"LLM call failed: {e}"})
        return

    parsed = _extract_json(content)
    if not isinstance(parsed, dict) or not parsed.get("journal"):
        print(f"❌ 复盘 JSON 解析失败，原始输出前 500 字: {content[:500]}", flush=True)
        # 退化成纯文本日记，至少不丢内容
        parsed = {"journal": content.strip()[:1500], "impression": "", "archive_keywords": []}

    parsed.setdefault("archive_keywords", [])
    if not isinstance(parsed.get("archive_keywords"), list):
        parsed["archive_keywords"] = []

    print(f"✅ 复盘完成：journal={len(parsed.get('journal', ''))}字，"
          f"impression={str(parsed.get('impression', ''))[:40]}，"
          f"archive_keywords={len(parsed['archive_keywords'])} 条", flush=True)

    upload_complete(task_id, result=parsed)


# ============================================================
# 画图（ComfyUI 本地服务）
# ============================================================
# 和 IndexTTS 一个模式：ComfyUI 是独立 HTTP 服务（默认 127.0.0.1:8188），
# node.py 只当客户端。模型常驻显存由 ComfyUI 自己管，node.py 环境零依赖
# （只多用了已经 import 的 requests）。
#
# task 字段（服务器 plugins/draw 下发）：
#   mode      txt2img（默认）/ img2img / lineart / pose / depth / style
#   prompt    正向 tags（服务器已经翻成 danbooru tags）
#   negative  负向 tags
#   width/height 底图尺寸（默认 1024）
#   steps/cfg/sampler/scheduler  采样参数
#   seed      随机种子（不传就随机）
#   input     参考图（img2img/lineart/pose/depth/style 需要，是服务器上的本地路径）
#
# workflow 模板（comfy_workflows/*.json，API format）：
#   用占位符注入参数，模板里写 __PROMPT__ 这种，代码按「字符串完全相等」替换。
#   txt2img/img2img/lineart 三个是核心节点就能跑的，随仓库下发；
#   pose/depth/style 依赖 ComfyUI 自定义节点，在算力机上用界面搭好
#   「Save (API Format)」导出后，把值替换成占位符放进 comfy_workflows/ 即可。
# ============================================================
from urllib.parse import urlparse as _urlparse

# 画图参数默认值（都可用 node.env 覆盖）
# 用户 2026-11-16 配方：config/prompt_presets.py（BASE_POSITIVE/BASE_NEGATIVE/GEN_PARAMS/I2I_PARAMS）。
# 优先级：任务参数 > node.env 的 SD_* > config/prompt_presets.py > 内置兜底。
# 文件没部署到算力机也不影响出图（自动回退内置默认）。
try:
    from config.prompt_presets import (
        BASE_POSITIVE as _PP_POSITIVE,
        BASE_NEGATIVE as _PP_NEGATIVE,
        GEN_PARAMS as _PP_GEN,
        I2I_PARAMS as _PP_I2I,
    )
except Exception:
    _PP_POSITIVE = _PP_NEGATIVE = ""
    _PP_GEN = _PP_I2I = {}

SD_API_URL = os.getenv("SD_API_URL", "http://127.0.0.1:8188").rstrip("/")
SD_TIMEOUT = float(os.getenv("SD_TIMEOUT", "600"))
SD_WORKFLOW_DIR = Path(os.getenv(
    "SD_WORKFLOW_DIR", str(Path(__file__).resolve().parent / "comfy_workflows")))
SD_MODEL = os.getenv("SD_MODEL", "NoobAI-XL-v1.1.safetensors")
SD_PROMPT_QUALITY = (
    os.getenv("SD_PROMPT_QUALITY")
    or _PP_POSITIVE
    or "masterpiece, best quality, newest, absurdres, highres, safe"
)
SD_NEGATIVE = (
    os.getenv("SD_NEGATIVE")
    or _PP_NEGATIVE
    or "nsfw, nsfw content, explicit, worst quality, old, early, low quality, lowres, "
       "normal quality, jpeg artifacts, signature, username, watermark, logo, text, "
       "bad anatomy, bad hands, mutated hands, extra fingers, missing fingers, "
       "extra digit, fewer digits, cropped, blurry, out of focus, rough sketch, "
       "messy lines, unfinished, mammal, anthro, furry, ambiguous form, feral, semi-anthro"
)
SD_WIDTH = int(os.getenv("SD_WIDTH", "1024"))
SD_HEIGHT = int(os.getenv("SD_HEIGHT", "1024"))
SD_STEPS = int(os.getenv("SD_STEPS") or _PP_GEN.get("steps") or 28)
# 图生图专用 steps（I2I_PARAMS 的 steps 覆盖 GEN_PARAMS 的 steps；没配方就用 SD_STEPS）
SD_I2I_STEPS = int(os.getenv("SD_I2I_STEPS") or _PP_I2I.get("steps") or SD_STEPS)
SD_CFG = float(os.getenv("SD_CFG") or _PP_GEN.get("cfg") or 5.5)
SD_SAMPLER = os.getenv("SD_SAMPLER") or _PP_GEN.get("sampler") or "euler_ancestral"
SD_SCHEDULER = os.getenv("SD_SCHEDULER") or _PP_GEN.get("scheduler") or "normal"
# HiRes 第二遍（4x-UltraSharp 放大后重采样）
SD_HI_STEPS = int(os.getenv("SD_HI_STEPS", "20"))
SD_HI_DENOISE = float(os.getenv("SD_HI_DENOISE", "0.4"))
SD_HI_SCALE = float(os.getenv("SD_HI_SCALE", "1.5"))
SD_UPSCALER = os.getenv("SD_UPSCALER", "4x-UltraSharp.pth")
# 图生图 / ControlNet 的强度（I2I_PARAMS 的 denoise 用于重绘/换角色）
SD_IMG2IMG_DENOISE = float(
    os.getenv("SD_IMG2IMG_DENOISE") or _PP_I2I.get("denoise") or 0.55)
SD_CONTROL_STRENGTH = float(os.getenv("SD_CONTROL_STRENGTH", "0.9"))
# ControlNet 模型名（算力机上 models/controlnet/ 里实际的文件名）
SD_CN_LINEART = os.getenv("SD_CN_LINEART", "diffusion_pytorch_model_promax.safetensors")
SD_CN_POSE = os.getenv("SD_CN_POSE", "diffusion_pytorch_model_promax.safetensors")
SD_CN_DEPTH = os.getenv("SD_CN_DEPTH", "diffusion_pytorch_model_promax.safetensors")

# ===== LoRA 常驻栈（所有画图任务自动叠加，最多 3 个）=====
# 格式：文件名:权重,文件名:权重（文件名 = models/loras/ 下的实际文件名）。
# 触发词用 SD_LORA_TRIGGERS 单独配（文件名=触发词1,触发词2;文件名=...）。
# 任务里带 loras 字段会整体覆盖这里的默认栈。
SD_LORAS = os.getenv(
    "SD_LORAS", "AddMicroDetails_NoobAI.safetensors:0.5,DetailTweakerXL.safetensors:0.5")
SD_LORA_TRIGGERS = os.getenv(
    "SD_LORA_TRIGGERS",
    "AddMicroDetails_NoobAI.safetensors=addmicrodetails;"
    "DetailTweakerXL.safetensors=add detail",
)


def _parse_loras(raw) -> list:
    """解析 LoRA 栈：'a.safetensors:0.5,b.safetensors:0.4' → [(name, weight), ...]（最多 3）。"""
    out = []
    if isinstance(raw, (list, tuple)):
        for it in raw:
            if isinstance(it, dict):
                name, weight = str(it.get("name") or "").strip(), it.get("weight")
            elif isinstance(it, (list, tuple)) and len(it) == 2:
                name, weight = str(it[0]).strip(), it[1]
            else:
                continue
            try:
                weight = float(weight if weight is not None else 1.0)
            except Exception:
                weight = 1.0
            if name:
                out.append((name, weight))
    else:
        for item in str(raw or "").split(","):
            item = item.strip()
            if not item:
                continue
            name, _, w = item.partition(":")
            name = name.strip()
            if not name:
                continue
            try:
                weight = float(w.strip() or "1.0")
            except Exception:
                weight = 1.0
            out.append((name, weight))
    return out[:3]


def _parse_lora_triggers(raw) -> dict:
    """解析触发词：'a.safetensors=t1,t2;b.safetensors=t3' → {name: 't1,t2'}。"""
    out = {}
    for item in str(raw or "").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, _, trig = item.partition("=")
        name = name.strip()
        if name:
            out[name] = trig.strip()
    return out


def _sd_alive() -> bool:
    """ComfyUI 端口通不通（决定心跳 caps 里带不带 draw）。"""
    try:
        u = _urlparse(SD_API_URL)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 8188)
        return _port_open(host, port)
    except Exception:
        return False


def _sd_upload_image(path: Path) -> str:
    """把参考图传到 ComfyUI 的 input 目录，返回 ComfyUI 里的文件名。"""
    with open(path, "rb") as f:
        r = requests.post(f"{SD_API_URL}/upload/image",
                          files={"image": (path.name, f, "image/png")},
                          proxies=PROXIES, timeout=60)
    r.raise_for_status()
    d = r.json()
    return str(d.get("name") or path.name)


def _sd_load_workflow(mode: str) -> dict:
    """按 mode 加载 workflow 模板；没有就用 txt2img 兜底，真没文件就报错。"""
    cands = [f"{mode}.json", "txt2img.json"]
    for name in cands:
        p = SD_WORKFLOW_DIR / name
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:
                raise RuntimeError(f"workflow {name} 解析失败: {e}") from e
    raise RuntimeError(f"缺 workflow 模板（{SD_WORKFLOW_DIR}/ 里没有 {mode}.json 也没有 txt2img.json）")


def _sd_fill_workflow(wf: dict, task: dict, image_name: str) -> dict:
    """把占位符替换成真实值。只做「字符串完全相等」的替换，绝不碰普通提示词内容。"""
    import random

    seed = task.get("seed")
    if seed is None:
        seed = random.randint(1, 2 ** 31 - 1)
    else:
        seed = int(seed)

    hi_w = int(round(int(task.get("width", SD_WIDTH)) * SD_HI_SCALE / 8) * 8)
    hi_h = int(round(int(task.get("height", SD_HEIGHT)) * SD_HI_SCALE / 8) * 8)
    mode = str(task.get("mode") or "txt2img")

    prompt = str(task.get("prompt") or "").strip()
    # 质量前缀只在文生图里自动加；以图绘图时用户描述 + 参考图已经足够，前缀会稀释图生图权重
    if mode == "txt2img" and SD_PROMPT_QUALITY and SD_PROMPT_QUALITY not in prompt:
        prompt = f"{SD_PROMPT_QUALITY}, {prompt}" if prompt else SD_PROMPT_QUALITY
    negative = str(task.get("negative") or SD_NEGATIVE).strip() or SD_NEGATIVE

    # ----- LoRA 栈 + 触发词 -----
    # task["loras"] 整体覆盖默认栈；task["lora_triggers"] 覆盖默认触发词表。
    raw_loras = _parse_loras(task.get("loras") if task.get("loras") is not None else SD_LORAS)
    triggers = _parse_lora_triggers(task.get("lora_triggers") or SD_LORA_TRIGGERS)
    extra_tags = []
    for name, _w in raw_loras:
        for t in str(triggers.get(name, "")).split(","):
            t = t.strip()
            if t and t not in prompt and t not in extra_tags:
                extra_tags.append(t)
    if extra_tags:
        prompt = f"{prompt}, {', '.join(extra_tags)}"
    # 配置名 → ComfyUI 实际识别名（文件在子目录/大小写不同时自动匹配；缺失则报清晰错误）
    loras = _sd_resolve_loras(raw_loras)

    # 占位符 → 类型化值。只有字符串值才会被替换（数字/布尔不碰）。
    values = {
        "__MODEL__": str(task.get("model") or SD_MODEL),
        "__PROMPT__": prompt,
        "__NEGATIVE__": negative,
        "__SEED__": seed,
        "__WIDTH__": int(task.get("width", SD_WIDTH)),
        "__HEIGHT__": int(task.get("height", SD_HEIGHT)),
        "__STEPS__": int(task.get(
            "steps", SD_I2I_STEPS if mode != "txt2img" else SD_STEPS)),
        "__CFG__": float(task.get("cfg", SD_CFG)),
        "__SAMPLER__": str(task.get("sampler") or SD_SAMPLER),
        "__SCHEDULER__": str(task.get("scheduler") or SD_SCHEDULER),
        "__DENOISE__": float(task.get("denoise", SD_IMG2IMG_DENOISE)),
        "__HI_WIDTH__": hi_w,
        "__HI_HEIGHT__": hi_h,
        "__HI_STEPS__": int(task.get("hi_steps", SD_HI_STEPS)),
        "__HI_DENOISE__": float(task.get("hi_denoise", SD_HI_DENOISE)),
        "__UPSCALER__": str(task.get("upscaler") or SD_UPSCALER),
        "__CONTROL_STRENGTH__": float(task.get("control_strength", SD_CONTROL_STRENGTH)),
        "__CONTROL_MODEL__": str(task.get("control_model") or SD_CN_LINEART),
        "__IMAGE__": image_name or "",
    }
    # LoRA 插槽（最多 3 个）；用不上的先填占位，随后由 _sd_trim_lora_slots 删节点
    for i in range(3):
        name, weight = loras[i] if i < len(loras) else ("None", 0.0)
        values[f"__LORA{i + 1}_NAME__"] = name
        values[f"__LORA{i + 1}_STRENGTH__"] = weight

    def walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, str) and v in values:
                    node[k] = values[v]
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(wf)
    # 没用到的 LoRA 插槽直接删节点重连（ComfyUI 不接受 "None" 这种占位 lora_name）
    _sd_trim_lora_slots(wf, len(loras))
    return wf


def _sd_resolve_loras(loras):
    """把 node.env 里配的 lora 名映射成 ComfyUI 实际识别的名字。

    常见坑：文件被放在 models/loras/loras/ 子目录，ComfyUI 的 lora_name 会带
    'loras\\' 前缀，而配置里只写裸文件名，直接提交会 value_not_in_list 400。
    这里拉一次 /object_info/LoraLoader 的可选列表，按 basename 兜底匹配；
    匹配不上就抛清晰错误（而不是让 ComfyUI 报一坨截断的 400）。
    """
    if not loras:
        return loras
    valid = []
    try:
        info = requests.get(f"{SD_API_URL}/object_info/LoraLoader",
                            proxies=PROXIES, timeout=30).json()
        req = info.get("LoraLoader", {}).get("input", {}).get("required", {})
        lst = req.get("lora_name") or []
        if lst and isinstance(lst[0], list):
            valid = [str(x) for x in lst[0]]
    except Exception:
        valid = []
    if not valid:
        # 拿不到列表就原样返回，交给 ComfyUI 校验
        return loras
    out = []
    for name, weight in loras:
        if name in valid:
            out.append((name, weight))
            continue
        base = os.path.basename(name.replace("\\", "/"))
        hit = next((c for c in valid
                    if os.path.basename(c.replace("\\", "/")) == base), None)
        if hit:
            print(f"🔧 LoRA 名已映射: {name} → {hit}", flush=True)
            out.append((hit, weight))
        else:
            raise RuntimeError(
                f"LoRA 文件找不到: {name}（ComfyUI 现有: {', '.join(valid) or '无'}）")
    return out


def _sd_trim_lora_slots(wf: dict, n_loras: int):
    """按实际 LoRA 数量裁剪 LoraLoader 链。

    模板约定节点 2/3/4 = LoraLoader1/2/3，节点 1 = CheckpointLoaderSimple；
    没用到第 3 个 LoRA 时把节点 4 删掉，并把所有指向它的 model/clip 边
    重连到最后一个在用的 LoRA（一个都没有则回退 checkpoint 节点 1）。
    """
    slots = ["2", "3", "4"]
    keep = max(0, min(n_loras, len(slots)))
    if keep == len(slots):
        return
    src = slots[keep - 1] if keep > 0 else "1"
    removed = set(slots[keep:])
    for nid in list(wf.keys()):
        if nid in removed:
            wf.pop(nid, None)
    for node in wf.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for k, v in list(inputs.items()):
            if isinstance(v, list) and len(v) == 2 and str(v[0]) in removed:
                inputs[k] = [src, v[1]]


def _sd_submit_and_wait(wf: dict) -> Path:
    """POST /prompt → 轮询 /history 拿图 → 下载到 TEMP_DIR，返回 png 路径。"""
    if not _sd_alive():
        raise RuntimeError(f"ComfyUI 没在听（{SD_API_URL}），画图服务离线")

    client_id = f"qqbot-node-{int(time.time() * 1000)}"
    # 冷启动时 ComfyUI 可能在 /prompt 里同步加载模型（SDXL 首次加载可达 30s+），
    # read timeout 30 会误杀。放宽到 300s，真正总时长仍由下面 SD_TIMEOUT 兜底。
    r = requests.post(f"{SD_API_URL}/prompt",
                      json={"prompt": wf, "client_id": client_id},
                      proxies=PROXIES, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"ComfyUI /prompt HTTP {r.status_code}: {r.text[:200]}")
    prompt_id = r.json().get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI /prompt 没返回 prompt_id: {r.text[:200]}")
    print(f"🎨 已提交 ComfyUI，prompt_id={prompt_id}", flush=True)

    deadline = time.time() + SD_TIMEOUT
    last = time.time()
    while time.time() < deadline:
        time.sleep(2)
        try:
            h = requests.get(f"{SD_API_URL}/history/{prompt_id}",
                             proxies=PROXIES, timeout=10).json()
        except Exception:
            continue
        item = h.get(prompt_id)
        if not item:
            if time.time() - last > 60:
                print(f"⏳ ComfyUI 还没开始执行（等 {int(time.time() - last)}s）…", flush=True)
                last = time.time()
            continue
        status = item.get("status", {})
        if status.get("status_str") == "error":
            # history 的 messages 是 [event_type, data] 列表；真正的错误在
            # execution_error 事件的 data.exception_message / traceback 里，
            # 只取 event_type 会得到一堆没用的 execution_start/execution_error。
            msgs = []
            for m in status.get("messages", []):
                if not (isinstance(m, list) and len(m) >= 2):
                    continue
                data = m[1]
                if isinstance(data, dict):
                    em = str(data.get("exception_message") or "").strip()
                    et = str(data.get("exception_type") or "").strip()
                    if em:
                        msgs.append(f"{et}: {em}" if et else em)
                    tb = data.get("traceback") or []
                    if tb:
                        msgs.append(" | ".join(str(x) for x in tb[-3:]))
                elif not msgs:
                    msgs.append(str(m[0]))
            detail = "; ".join(x for x in msgs if x) or "unknown"
            raise RuntimeError(f"ComfyUI 执行出错: {detail[:1500]}")
        if status.get("completed"):
            # 收集 output 图
            for _nid, out in item.get("outputs", {}).items():
                imgs = out.get("images") or []
                for im in imgs:
                    fname = im.get("filename")
                    sub = im.get("subfolder") or ""
                    ftype = im.get("type") or "output"
                    if not fname:
                        continue
                    dl = requests.get(
                        f"{SD_API_URL}/view",
                        params={"filename": fname, "subfolder": sub, "type": ftype},
                        proxies=PROXIES, timeout=120)
                    if dl.status_code != 200:
                        continue
                    out_p = TEMP_DIR / f"draw_{prompt_id}.png"
                    out_p.write_bytes(dl.content)
                    if out_p.stat().st_size > 1024:
                        return out_p
            raise RuntimeError("ComfyUI 执行完成但没找到输出图")
    raise RuntimeError(f"画图超时（>{int(SD_TIMEOUT)}s），ComfyUI 可能卡住了")


def process_draw_task(task_id: str, task: dict):
    """画图任务：文生图 / 以图绘图。出图 → 回传 png。"""
    mode = str(task.get("mode") or "txt2img")
    need_img = mode in ("img2img", "lineart", "pose", "depth", "style")
    print(f"\n🎨 【画图】任务 {task_id} mode={mode}，"
          f"prompt={str(task.get('prompt') or '')[:60]}…", flush=True)

    image_name = ""
    ref_path = None
    if need_img:
        ref_path = TEMP_DIR / f"{task_id}_ref.png"
        if not _download_input(task_id, ref_path, expect_audio=False, label="reference image"):
            upload_complete(task_id, result={"error": f"参考图下载失败（mode={mode}）"})
            return
        try:
            image_name = _sd_upload_image(ref_path)
            print(f"📎 参考图已上传 ComfyUI：{image_name}", flush=True)
        except Exception as e:
            upload_complete(task_id, result={"error": f"参考图上传失败: {e}"})
            ref_path.unlink(missing_ok=True)
            return

    try:
        wf = _sd_load_workflow(mode)
        wf = _sd_fill_workflow(wf, task, image_name)
        png = _sd_submit_and_wait(wf)
    except Exception as e:
        import traceback
        print(f"❌ 画图失败: {type(e).__name__}: {e}\n{traceback.format_exc()[:1200]}", flush=True)
        upload_complete(task_id, result={"error": f"{type(e).__name__}: {e}"})
        if ref_path:
            ref_path.unlink(missing_ok=True)
        return

    upload_complete(task_id, file_path=png, filename=f"{task_id}_draw.png",
                    result={"mode": mode, "prompt": str(task.get("prompt") or "")[:200]})
    png.unlink(missing_ok=True)
    if ref_path:
        ref_path.unlink(missing_ok=True)


# ============================================================
# 清理 + 主循环
# ============================================================
def clean_temp_dir():
    keep = {"prompt_kki_cut.wav"}
    for p in TEMP_DIR.glob("*"):
        if p.is_file() and p.name not in keep:
            try:
                p.unlink()
            except Exception:
                pass


TASK_HANDLERS = {
    "cover":             process_cover_full_task,     # 单段式翻唱：分离+RVC+混音一次到底
    "split":             process_split_task,          # 翻唱：双文件（旧两段式，保留兼容）
    "audio_separation":  process_audio_separation_task,  # 手动分离：单文件
    "rvc":               process_rvc_task,            # 只变声（清唱走这条）
    "rvc_mix":           process_rvc_mix_task,        # 变声 + 混音 → mp3（旧两段式）
    "tts":               process_tts_task,
    "reflection":        process_reflection_task,
    "probe":             process_probe_task,          # /测歌：只跑判定链，不翻唱
    "draw":              process_draw_task,           # 画图（ComfyUI 文生图 / 以图绘图）
}


def _current_caps() -> list:
    """上报能力列表。ComfyUI 不在线就把 draw 摘掉，
    服务器看 caps 里没 draw 会直接回「画图离线」，不会塞任务进来干等。"""
    caps = set(TASK_HANDLERS)
    if "draw" in caps and not _sd_alive():
        caps.discard("draw")
    return sorted(caps)


# ============================================================
# 依赖自检（python node.py --check 只跑这个）
# ============================================================
def _probe(url: str, timeout: float = 3.0):
    """能拿到任何 HTTP 响应就算活着（404 也算，说明服务在监听）。"""
    try:
        r = requests.get(url, proxies=PROXIES, timeout=timeout)
        return True, f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_services() -> bool:
    print("=" * 66)
    print("  算力机依赖自检")
    print("=" * 66)

    def _base(u: str, marker: str) -> str:
        return u.rsplit(marker, 1)[0] if marker in u else u

    http_items = [
        ("服务器 API", f"{BASE_URL}/api/heartbeat?secret={API_SECRET}"),
        ("Demucs 分离", "http://127.0.0.1:8005/"),
        ("IndexTTS2", _base(TTS_URL, "/api/")),
        ("Ollama 复盘", _base(LLM_URL, "/v1/") + "/api/tags"),
        ("ComfyUI 画图", SD_API_URL + "/"),
    ]
    ok_all = True
    for name, url in http_items:
        ok, info = _probe(url)
        print(f"  {'✅' if ok else '❌'} {name:<12} {url}")
        if not ok:
            print(f"       └─ {info}")
            ok_all = False

    file_items = [
        ("RVC python", RVC_PYTHON), ("RVC 脚本", RVC_SCRIPT),
        ("RVC 模型", RVC_MODEL), ("RVC index", RVC_INDEX),
        ("TTS 参考音", TTS_PROMPT_WAV),
    ]
    for name, path in file_items:
        ok = Path(path).exists()
        print(f"  {'✅' if ok else '❌'} {name:<12} {path}")
        if not ok:
            ok_all = False

    if TTS_ENGINE == "indextts":
        ok = TTS_OUTPUT_DIR.exists()
        print(f"  {'✅' if ok else '❌'} {'TTS 输出目录':<12} {TTS_OUTPUT_DIR}")
        if not ok:
            ok_all = False

    print("-" * 66)
    if ok_all:
        print("  ✅ 全部就绪")
    else:
        print("  ⚠️ 上面打 ❌ 的就是缺的东西，对应功能会失败或降级")
        print("     注意：Ollama 是给「两天复盘」用的；")
        print("           服务器端的本地压缩走的是它自己的 11434 隧道，和这里无关")
    print("=" * 66)
    return ok_all


def check_server_conn() -> bool:
    """
    专门排查「连不上服务器」：先测 TCP，再测 HTTP 心跳。

    用途：python node.py --net
    能区分三种情况：
      · TCP 超时      → 包被丢弃，云安全组 / 防火墙
      · TCP 拒绝      → 端口没监听，服务器容器没起来
      · HTTP 403      → 网络通，但 secret 不一致
    """
    import socket

    print("=" * 66, flush=True)
    print_config()
    print("=" * 66, flush=True)

    print(f"🔍 1/2 测 TCP {SERVER_IP}:{SERVER_PORT} …", flush=True)
    try:
        s = socket.create_connection((SERVER_IP, int(SERVER_PORT)), timeout=8)
        s.close()
        print(f"  ✅ TCP 通（端口 {SERVER_PORT} 可连）", flush=True)
    except socket.timeout:
        print("  ❌ TCP 超时 —— 包被丢弃，查阿里云安全组 / 服务器 ufw", flush=True)
        return False
    except ConnectionRefusedError:
        print("  ❌ TCP 被拒绝 —— 端口没监听（服务器容器起来了吗？）", flush=True)
        return False
    except Exception as e:
        print(f"  ❌ TCP 失败: {type(e).__name__}: {e}", flush=True)
        return False

    print(f"🔍 2/2 测 HTTP {BASE_URL}/api/heartbeat …", flush=True)
    try:
        r = requests.get(f"{BASE_URL}/api/heartbeat",
                         params={"secret": API_SECRET}, timeout=10, proxies=PROXIES)
        print(f"  ✅ HTTP {r.status_code}  {r.text[:120]}", flush=True)
        if r.status_code == 403:
            print("     → secret 不一致：对一下 node.env 和服务器 .env 的 API_SECRET", flush=True)
        return r.status_code == 200
    except Exception as e:
        print(f"  ❌ HTTP 失败: {type(e).__name__}: {e}", flush=True)
        return False


def main():
    # python node.py --check → 只做依赖自检，不进入轮询
    if "--check" in sys.argv or "check" in sys.argv[1:]:
        check_services()
        return

    # python node.py --net → 只测到服务器的连通性（排查连不上）
    if "--net" in sys.argv:
        ok = check_server_conn()
        print("=" * 66, flush=True)
        print("✅ 连接正常，可以直接 python node.py" if ok else
              "❌ 连不上，按上面的提示排查", flush=True)
        return

    print("=" * 66, flush=True)
    print_config()
    print("=" * 66, flush=True)
    # 算力机是「客户端」：主动轮询服务器拉任务，本机不监听任何端口。
    # 原来写「监听: xxx」会让人以为 8080 是算力机开的，排查时容易走偏。
    print(f"🚀 算力机搬运工已启动！（轮询目标 {BASE_URL}，本机不监听端口）", flush=True)
    print(f"🎤 TTS 引擎: {TTS_ENGINE} @ {TTS_URL}", flush=True)
    print(f"🧠 复盘 LLM: {LLM_MODEL} @ {LLM_URL}", flush=True)
    print(f"🎛️ RVC: {RVC_DIR}", flush=True)
    print(f"⏱️ 轮询：心跳 {HEARTBEAT_INTERVAL}s / 拉任务 {POLL_INTERVAL}s", flush=True)

    # 启动时先自检一遍依赖，缺什么当场就能看到（不用等某个功能报错才发现）
    check_services()
    clean_temp_dir()

    ok_streak = 0
    fail_streak = 0
    loop_err_streak = 0
    empty_polls = 0        # 连续空轮询次数（没任务时静默，但别让人以为卡死了）
    last_hb = 0.0
    hb_ok = False

    while True:
        try:
            now = time.time()

            # 👇 心跳每 HEARTBEAT_INTERVAL 秒一次（原来每次循环都发，日志被刷爆）
            if now - last_hb >= HEARTBEAT_INTERVAL or last_hb == 0:
                hb_ok = send_heartbeat()
                last_hb = now
                if not hb_ok:
                    fail_streak += 1
                    print(f"⚠️ 心跳失败（连续 {fail_streak} 次）", flush=True)
                    time.sleep(10 if fail_streak >= 10 else 3)
                    continue
                ok_streak += 1
                fail_streak = 0
                if loop_err_streak:
                    print(f"♻️ 连接已恢复（之前连续异常 {loop_err_streak} 次）", flush=True)
                loop_err_streak = 0
                if ok_streak == 1 or ok_streak % 20 == 0:
                    print(f"[{time.strftime('%H:%M:%S')}] 💓 心跳正常（已连续 {ok_streak} 次）", flush=True)

            if not hb_ok:
                time.sleep(POLL_INTERVAL)
                continue

            r = requests.get(f"{BASE_URL}/api/tasks/pull",
                             params={"secret": API_SECRET},
                             timeout=15, proxies=PROXIES)
            data = r.json()
            if not data.get("has_task"):
                # 没任务时原来完全静默 → 看起来像「卡住了」。
                # 改成第一次 + 每 12 次（约 1 分钟）报一次，证明轮询在跑。
                empty_polls += 1
                if empty_polls == 1 or empty_polls % 12 == 0:
                    print(f"⏳ 轮询正常，暂无任务（第 {empty_polls} 次，"
                          f"每 {POLL_INTERVAL:.0f}s 一次）", flush=True)
                time.sleep(POLL_INTERVAL)
                continue
            empty_polls = 0

            task_id = data["task_id"]
            task = data.get("task", {}) or {}
            task_type = task.get("type")

            print(f"\n🔔 收到任务：{task_id} type={task_type}", flush=True)

            handler = TASK_HANDLERS.get(task_type)
            if not handler:
                print(f"⚠️ 未知任务类型：{task_type}，回传失败标记", flush=True)
                upload_complete(task_id, result={"error": f"unknown type {task_type}"})
                continue

            try:
                handler(task_id, task)
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(f"💥 任务 {task_id} 处理异常：{e}\n{tb}", flush=True)
                upload_complete(task_id, result={"error": str(e), "trace": tb[-1500:]})

            time.sleep(1)

        except KeyboardInterrupt:
            print("\n👋 手动结束", flush=True)
            break
        except Exception as e:
            # ⚠️ 关键：连接被服务器 RST（容器重启/重建）之后，hb_ok 必须失效、
            #    last_hb 必须清零，否则会一直用旧状态去 pull，心跳要等满
            #    HEARTBEAT_INTERVAL 才重发，中间这段时间服务器会认为算力机离线。
            loop_err_streak += 1
            hb_ok = False
            last_hb = 0.0
            wait = min(30, 2 * loop_err_streak)
            hint = ""
            if isinstance(e, (ConnectionError, OSError)):
                hint = "（服务器断开/重启中，稍后自动重连）"
            print(f"💥 主循环异常（连续第 {loop_err_streak} 次）{hint}："
                  f"{type(e).__name__}: {e} → {wait}s 后重连", flush=True)
            time.sleep(wait)


if __name__ == "__main__":
    main()
