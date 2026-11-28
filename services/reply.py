# services/reply.py
import os
import re
import time
import random
import aiohttp
from pathlib import Path
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment
from .tts import send_voice
from .meme import send_random_fishy

# ⚠️ 这个入口会**绕过 indextts**：命中概率时直接调云端 CosyVoice（龙呼呼）。
#    线上语音统一走 plugins/chat 的 speak_voice（indextts 优先）。
#    chat 里已不再调用本函数，保留仅为兼容旧调用方 —— 别在新代码里用。
RATE_VOICE = 0.30
TEMP_IMG_DIR = Path("data/temp_web_img")
TEMP_IMG_DIR.mkdir(parents=True, exist_ok=True)

# 👇 增加 is_query_image 参数
async def reply_with_multimodal(bot: Bot, event: MessageEvent, text: str, is_query_image: bool = False):
    urls = re.findall(r'https?://[^\s<>"]+', text)
    
    clean_text = text
    for url in urls:
        clean_text = clean_text.replace(url, "")
    clean_text = re.sub(r'(图片链接|图片|链接|URL)[:：]?', '', clean_text).strip()

    if clean_text:
        if random.random() < RATE_VOICE:
            success = await send_voice(bot, event, clean_text)
            if success:
                if random.random() < 0.20 and not is_query_image: # 查图不发随机表情
                    await send_random_fishy(bot, event)
                return
        await bot.send(event, clean_text)
    
    if urls:
        for idx, url in enumerate(urls[:2]):
            if "bilibili.com" in url or "zhihu.com" in url or "baidu.com" in url:
                continue
                
            local_file = TEMP_IMG_DIR / f"temp_{int(time.time())}_{event.user_id}_{idx}.jpg"
            try:
                logger.info(f"[MULTI] 尝试下载图片: {url}")
                timeout = aiohttp.ClientTimeout(total=10.0)
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            with open(local_file, 'wb') as f:
                                f.write(await resp.read())
                        else:
                            continue
                
                if local_file.exists() and local_file.stat().st_size > 0:
                    abs_path = local_file.absolute()
                    await bot.send(event, MessageSegment.image(file=f"file://{abs_path}"))
                    logger.info(f"[MULTI] 图片发送成功")
            except Exception as e:
                logger.error(f"[MULTI] 图片异常: {e}")
            finally:
                if local_file.exists():
                    try: os.remove(local_file)
                    except: pass

    # 👇 核心：如果是查图意图，绝对不发随机表情包
    if random.random() < 0.20 and not is_query_image:
        await send_random_fishy(bot, event)
