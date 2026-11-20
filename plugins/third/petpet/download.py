import asyncio
import hashlib
import json
from pathlib import Path

import httpx
from nonebot import get_driver
from nonebot.log import logger
from nonebot_plugin_imageutils import BuildImage
from nonebot_plugin_imageutils.fonts import Font, add_font

from .config import petpet_config

data_path = Path() / "data" / "petpet"


def load_image(path: str) -> BuildImage:
    return BuildImage.open(data_path / "images" / path).convert("RGBA")


async def download_url(url: str) -> bytes:
    async with httpx.AsyncClient() as client:
        for i in range(3):
            try:
                resp = await client.get(url, timeout=20)
                resp.raise_for_status()
                return resp.content
            except Exception as e:
                logger.warning(f"Error downloading {url}, retry {i}/3: {e}")
                await asyncio.sleep(3)
    raise Exception(f"{url} 下载失败！")


async def download_avatar(user_id: str) -> bytes:
    url = f"http://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
    data = await download_url(url)
    if hashlib.md5(data).hexdigest() == "acef72340ac0e914090bd35799f5594e":
        url = f"http://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
        data = await download_url(url)
    return data


def resource_url(path: str) -> str:
    return f"{petpet_config.petpet_resource_url}/{path}"


async def download_resource(path: str) -> bytes:
    return await download_url(resource_url(path))


async def check_font(family: str, fontname: str):
    try:
        Font.find(family)
    except ValueError:
        try:
            await add_font(fontname, resource_url(f"fonts/{fontname}"))
        except Exception as e:
            logger.warning(f"字体 {fontname} 加载失败（离线？）：{e}")


async def check_resources():
    # 本地有清单就直接用，**不要联网**。
    # petpet_resource_url 默认指向 ghproxy.com，它现在 301 跳到 ghfast.top，
    # httpx 会当异常处理 → 每次启动重试 3 次、间隔 3 秒，白等约 10 秒，
    # 日志里刷三条 "Redirect response '301 Moved Permanently'"。
    # 素材已经确认齐全（data/petpet/images/ 1091 个文件），没必要每次校验。
    # 真要更新素材：删掉 data/petpet/resource_list.json 再重启即可。
    local_list = data_path / "resource_list.json"
    if local_list.exists():
        logger.info(f"资源清单直接用本地：{local_list}")
        resource_list = json.loads(local_list.read_text(encoding="utf-8"))
    else:
        try:
            resource_list = json.loads(
                (await download_resource("resource_list.json")).decode("utf-8")
            )
            # 首次拉到的清单存本地，下次启动就不联网了
            local_list.parent.mkdir(parents=True, exist_ok=True)
            local_list.write_text(
                json.dumps(resource_list, ensure_ascii=False), encoding="utf-8"
            )
            logger.info(f"资源清单已缓存到本地：{local_list}")
        except Exception as e:
            logger.warning(f"资源清单拉取失败，本地也没有：{e}")
            return
    for resource in resource_list:
        file_name = str(resource["path"])
        file_path = data_path / file_name
        file_hash = str(resource["hash"])
        if (
            file_path.exists()
            and hashlib.md5(file_path.read_bytes()).hexdigest() == file_hash
        ):
            continue
        logger.debug(f"Downloading {file_name} ...")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = await download_resource(file_name)
            with file_path.open("wb") as f:
                f.write(data)
        except Exception as e:
            logger.warning(str(e))
    await check_font("Consolas", "consola.ttf")


driver = get_driver()


@driver.on_startup
async def _():
    logger.info("正在检查资源文件...")
    asyncio.create_task(check_resources())
