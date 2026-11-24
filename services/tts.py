import os
import time, random, httpx
from pathlib import Path
from urllib.parse import urlparse
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from .config import DASHSCOPE_API_KEY, ALIYUN_VOICE_TYPE

# CosyVoice 的 MaaS 端点：优先从 DASHSCOPE_BASE_URL 取 host，
# 这样换 workspace 只要改 .env 一处（原来写死 host，换工作空间就 404）。
# 兜底用公共域名：专属 workspace（ws-xxxx）写在 .env 里，别写进代码（发行仓会带出去）。
_DEFAULT_TTS_HOST = "dashscope.aliyuncs.com"


def _tts_endpoint() -> str:
    base = (os.getenv("DASHSCOPE_BASE_URL", "") or "").strip()
    host = ""
    if base:
        try:
            host = urlparse(base).netloc
        except Exception:
            host = ""
    return f"https://{host or _DEFAULT_TTS_HOST}/api/v1/services/audio/tts/SpeechSynthesizer"

# 👇 新增：自动清理老音频的函数
def clean_old_audio(max_keep: int = 10):
    data_dir = Path("data")
    if not data_dir.exists():
        return
    # 匹配所有 wav 文件，按修改时间排序
    wav_files = sorted(data_dir.glob("*.wav"), key=lambda f: f.stat().st_mtime)
    if len(wav_files) > max_keep:
        # 删除多出来的最老的文件
        for f in wav_files[:-max_keep]:
            try:
                f.unlink()
            except Exception as e:
                logger.warning(f"[TTS] 清理旧音频失败: {e}")

class AliyunVoiceTTS:
    @classmethod
    async def _request_tts(cls, text: str, voice_type: str = ALIYUN_VOICE_TYPE, speed_ratio: float = 1.0) -> bytes:
        if not text: return b""
        url = _tts_endpoint()
        headers = {"Authorization": f"Bearer {DASHSCOPE_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": "cosyvoice-v3-flash", "input": {"text": text, "voice": voice_type, "format": "wav", "sample_rate": 16000, "enable_ssml": False}}
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    try:
                        res_json = resp.json()
                        audio_url = res_json.get("output", {}).get("audio", {}).get("url")
                        if audio_url:
                            audio_resp = await client.get(audio_url)
                            if audio_resp.status_code == 200:
                                return audio_resp.content
                    except Exception as e:
                        logger.error(f"[TTS] 解析 JSON 失败: {e}")
                else:
                    logger.error(f"[TTS] 接口异常({resp.status_code}): {resp.text}")
        except Exception as e:
            logger.error(f"[TTS] 请求异常: {e}")
        return b""

    @classmethod
    async def sing_tts(cls, text: str) -> bytes:
        return await cls._request_tts(text, voice_type=ALIYUN_VOICE_TYPE, speed_ratio=1.15)

    @classmethod
    async def text_to_speech(cls, text: str) -> bytes:
        return await cls._request_tts(text, voice_type=ALIYUN_VOICE_TYPE, speed_ratio=0.9)

async def send_voice(bot: Bot, event: MessageEvent, text: str) -> bool:
    logger.info(f"[TTS] 开始合成: {text[:20]}...")
    audio_data = await AliyunVoiceTTS.text_to_speech(text)
    if not audio_data:
        logger.warning("[TTS] 合成失败，返回空")
        return False
    try:
        voice_dir = Path("data")
        voice_dir.mkdir(exist_ok=True)
        
        # 👇 新增：每次存文件前，先执行清理
        clean_old_audio(max_keep=10) 

        voice_path = voice_dir / f"voice_{int(time.time())}_{random.randint(1000,9999)}.wav"
        with open(voice_path, "wb") as f:
            f.write(audio_data)
        abs_path = voice_path.absolute()
        logger.info(f"[TTS] 准备发送本地共享路径: {abs_path}")
        await bot.send(event, MessageSegment.record(file=f"file://{abs_path}"))
        logger.info("[TTS] 语音文件发送成功")
        return True
    except Exception as e:
        logger.error(f"[TTS] 发送语音文件失败: {e}")
        return False