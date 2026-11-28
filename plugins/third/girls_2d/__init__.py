r"""
来点美少女 —— 从 dmoe.cc 随机取一张二次元图。

原版（nonebot-plugin-2dgirls）的问题，已全部修掉：
1. 硬编码 `C:\Users\ASUS\Desktop\bot_test\...` 绝对路径（写了两处）→ docker 里必崩
2. API 失败时 `image_data` 没定义就往下用 → UnboundLocalError
3. `requests.get` 同步阻塞，会卡住整个事件循环 → 换 httpx 异步
4. 每次都写同一个文件名，两个人同时用会互相覆盖 → 用 uuid 命名，发完就删
"""
import uuid
from pathlib import Path

import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.log import logger

# 图片落到项目根的 data/ 下（和 petpet / gspanel 一样归拢）
DATA_DIR = Path() / "data" / "girls_2d"

API_URL = "https://www.dmoe.cc/random.php"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

command = on_command("来点美少女", aliases={"来点二次元"}, priority=15, block=True)


@command.handle()
async def send():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / f"{uuid.uuid4().hex}.jpg"

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(API_URL, headers=HEADERS)
        if resp.status_code != 200 or len(resp.content) < 1024:
            await command.finish(f"api 寄了没找到捏（HTTP {resp.status_code}）")
        tmp.write_bytes(resp.content)
    except Exception as e:
        logger.warning(f"[2dgirls] 取图失败: {type(e).__name__}: {e}")
        await command.finish("api 寄了没找到捏")

    try:
        await command.send(MessageSegment.image(file=tmp.read_bytes()))
    finally:
        # 发完就删，别在 data 里堆几百张图
        tmp.unlink(missing_ok=True)
