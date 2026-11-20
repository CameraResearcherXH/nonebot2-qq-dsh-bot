import random
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment

FEATURED_IDS = ["123", "010", "022", "035", "037", "040", "044", "050", "052", "056", "058", "070", "071", "077", "081", "083", "085", "097"]
ALL_POOL_IDS = [f"{i:03d}" for i in range(1, 201)]
CDN_BASE_URL = "https://img.aigengtu.com/0_preview/meme/{}.webp"
RATE_RANDOM_POOL = 0.10

async def send_random_fishy(bot: Bot, event: MessageEvent) -> bool:
    return
    if not FEATURED_IDS and not ALL_POOL_IDS: return False
    if random.random() < RATE_RANDOM_POOL:
        random_id = random.choice(ALL_POOL_IDS)
    else:
        random_id = random.choice(FEATURED_IDS)
    img_url = CDN_BASE_URL.format(random_id)
    try:
        await bot.send(event, MessageSegment.image(file=img_url))
        return True
    except Exception as e:
        logger.error(f"[IMGN] 发送失败: {e}")
        return False
