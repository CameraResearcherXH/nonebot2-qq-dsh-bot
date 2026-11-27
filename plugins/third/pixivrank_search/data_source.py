import asyncio
import platform
import random
import re
import time
from asyncio.exceptions import TimeoutError
from pathlib import Path
from urllib.parse import quote

import aiohttp
import aiofiles
import feedparser
from bs4 import BeautifulSoup
from nonebot.log import logger
from nonebot import require, get_driver

from .config import config

require("nonebot_plugin_localstore")

from nonebot_plugin_alconna import UniMessage

if platform.system() == "Windows":

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

headers = {
    # ⚠️ i.pximg.net 会同时校验 UA 和 Referer（2026-11-13 实测）：
    #    原作者这里写的是 "Mozilla/4.0 (compatible; MSIE 7.0; ...TencentTraveler 4.0)"
    #    —— 2010 年代的 IE7 UA，pixiv 直接判非浏览器，图一张都下不下来，
    #    表现是「网页排行取到 50 条，但图片全部下载失败」。
    #    换成桌面 Chrome 的 UA，Referer 也必须带（防盗链）。
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.1 Safari/537.36"
    ),
    "Referer": "https://www.pixiv.net/",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8",
}

# pixiv 网页版接口用的头：必须是像浏览器的 UA，且带 Referer / Accept
WEB_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.1 Safari/537.36"
    ),
    "Referer": "https://www.pixiv.net/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8",
}

# ⚠️ 图片必须写到 **NapCat 也能读到的目录**（2026-11-13 实测）：
#   NapCat 是独立容器，只挂了 ./data:/app/data 和 ./plugins:/app/plugins。
#   原来用 localstore 的 get_cache_dir() → /root/.cache/nonebot2/pixivrank_search，
#   那个路径只存在于 bot 容器里，NapCat 打开时报
#     ENOENT: no such file or directory, open '/app/file:///root/.cache/nonebot2/...'
#   改到 data/temp/pixiv（和 petpet 用 data/temp/petpet 是同一个道理），
#   两个容器看到的绝对路径都是 /app/data/temp/pixiv/...
IMAGE_PATH = (Path("data") / "temp" / "pixiv").resolve()

# 一次调用最多发几张图（多页作品可能十几张，全发既慢又容易被风控）
MAX_IMAGES_PER_CALL = 3

# 本插件内部用的 mode 名 → pixiv 网页版 ranking.php 的 mode
RANK_MODE_MAP = {
    "day": "daily",
    "week": "weekly",
    "month": "monthly",
    "week_original": "original",
    "week_rookie": "rookie",
    "day_r18": "daily_r18",
    "week_r18": "weekly_r18",
    "day_male_r18": "male_r18",
    "week_r18g": "r18g",
}

RANKING_URL = "https://www.pixiv.net/ranking.php"

# 网页版搜索 / 作品详情（2026-11-14 新增，用来替掉需要 refresh_token 的 RSSHub）
SEARCH_URL = "https://www.pixiv.net/ajax/search/artworks/{keyword}"
ILLUST_URL = "https://www.pixiv.net/ajax/illust/{illust_id}"

# 搜图只发 1 张，所以从搜索结果的前 N 条里随机抽（用户要求，2026-11-14）
# 演变：前 10 → 前 3（觉得太少）→ 前 5，现在是 5
SEARCH_POOL_SIZE = 5
# 抽中的作品详情拿不到原图（被删/仅关注可见/小说）时，最多再换几条试
SEARCH_MAX_TRIES = 5
# 搜索一条都没有时的兜底：从日榜前 N 名里随机推一张（用户要求，2026-11-14）
SEARCH_FALLBACK_POOL = 20

driver = get_driver()
session: aiohttp.ClientSession


@driver.on_startup
async def _():
    global session
    session = aiohttp.ClientSession()


@driver.on_shutdown
async def _():
    await session.close()


def _to_original_url(thumb_url: str, page: int = 0) -> str:
    """
    缩略图地址 → 原图地址。

    ranking.php 返回的 url 是带 `/c/<宽>x<高>/img-master/` 前缀的缩略图，
    而且**尺寸前缀不固定**（实测 2026-11-13 是 `/c/480x960/`，别处可能是
    `/c/240x480/` 或 `/c/540x540/`），所以必须用正则匹配任意尺寸：

        https://i.pximg.net/c/480x960/img-master/img/2026/11/11/00/00/37/151917034_p0.jpg
                                  ↓
        https://i.pximg.net/img-original/img/2026/11/11/00/00/37/151917034_p0.jpg

    只写死 `/c/240x480/` 会导致 replace 不生效，地址原样留着 `/c/480x960/`，
    于是 404 —— 表现就是「排行取到 50 条，图片一张都下不来」。

    另外兼容更老的 `xxx_p0_master1200.jpg` 形式（去掉 `_master1200`）。
    """
    url = re.sub(r"/c/\d+x\d+/img-master/", "/img-original/", thumb_url)
    url = re.sub(r"_master1200(\.\w+)$", r"\1", url)
    # 指定要第几页（多页作品）
    url = re.sub(r"_p\d+(\.\w+)$", rf"_p{page}\1", url)
    return url


async def _rank_from_web(mode: str, num: int, date: str = ""):
    """
    走 pixiv 网页版的公开排行接口，**不需要登录态**。

    为什么换这条：
      RSSHub 的 /pixiv/* 需要 PIXIV_REFRESH_TOKEN，而拿 token 的三条路都断了
      （账号密码授权被 pixiv 关闭 code 1508；PKCE 的 /start 接口 404；
        RSSHub 不支持 cookie）。这个网页接口是公开的，只要 UA/Referer 像浏览器就行。
    """
    web_mode = RANK_MODE_MAP.get(mode, mode)
    params = {"mode": web_mode, "format": "json", "p": 1}
    if date:
        # 网页接口要 YYYYMMDD，插件这边传进来的是 YYYY-MM-DD
        params["date"] = date.replace("-", "")

    async with session.get(
        RANKING_URL,
        params=params,
        headers=WEB_HEADERS,
        proxy=config.pixr_local_proxy,
        timeout=10,
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"ranking.php HTTP {resp.status}")
        data = await resp.json(content_type=None)

    contents = data.get("contents") or []
    if not contents:
        logger.warning(
            f"[pixivrank] ranking.php mode={web_mode} 返回 0 条，顶层键={list(data)[:8]}"
        )
        return ["没有搜索到喔"], [], 997
    logger.info(
        f"[pixivrank] 网页排行 mode={web_mode} 取到 {len(contents)} 条，"
        f"首条={contents[0].get('title', '')[:24]!r}"
    )
    if num > len(contents):
        num = len(contents)

    text_list, urls = [], []
    for item in contents[:num]:
        title = item.get("title", "")
        user_name = item.get("user_name", "")
        text_list.append(f"标题：{title}\n{user_name}\n")

        thumb = item.get("url", "")
        try:
            page_count = int(item.get("illust_page_count") or 1)
        except (TypeError, ValueError):
            page_count = 1
        # 多页作品最多取前 4 张，避免一次刷屏
        imgs = [_to_original_url(thumb, p) for p in range(min(page_count, 4))]
        urls.append([u for u in imgs if u])
    return text_list, urls, 200


async def parser_data(url: str, num: int) -> tuple[list[str], list[list[str]], int]:
    """RSSHub 解析（备用路线，需要 PIXIV_REFRESH_TOKEN 才能用）。"""
    text_list = []
    urls = []
    for _ in range(config.pixr_retry):
        try:
            async with session.get(url, proxy=config.pixr_local_proxy, timeout=5) as response:
                resp = feedparser.parse(await response.text())["entries"]
                break
        except TimeoutError:
            logger.warning("网络超时, 重试")
    else:
        return ["网络不太好，也许过一会就好了"], [], 998
    try:
        if len(resp) == 0:
            return ["没有搜索到喔"], [], 997
        if num > len(resp):
            num = len(resp)
        for data in resp[:num]:
            soup = BeautifulSoup(data["summary"], "lxml")
            title = f"标题：{data['title']}"
            pl = soup.find_all("p")
            author = pl[0].text.split("-")[0].strip()
            imgs = []
            text_list.append(f"{title}\n{author}\n")
            for p in pl[1:]:
                imgs.append(p.find("img").get("src"))
            urls.append(imgs)
    except ValueError:
        return ["是网站坏了啊，也许过一会就好"], [], 999
    return text_list, urls, 200


async def get_pixiv_urls(mode: str, num: int = 5, date: str = ""):
    """
    排行：优先 pixiv 网页版公开接口（无需登录），失败再退回 RSSHub。
    """
    try:
        result = await _rank_from_web(mode, num, date)
        if result[2] == 200:
            return result
        logger.info(f"[pixivrank] 网页接口返回 {result[2]}，尝试 RSSHub")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[pixivrank] 网页接口失败：{type(e).__name__}: {e}")

    url = f"{config.pixr_rsshub}/pixiv/ranking/{mode}"
    if date:
        url += f"/{date}"
    return await parser_data(url, num)


async def search_pixiv_urls(keyword: str, num: int, order: str, r18: int):
    """
    搜图：优先 pixiv 网页版搜索接口，失败再退回 RSSHub。

    历史：原来只走 RSSHub，而 RSSHub 的 /pixiv/search 要 PIXIV_REFRESH_TOKEN，
    拿 token 的三条路全断（password 授权 1508 / PKCE /start 404 / 不支持 cookie），
    所以 /p站搜图 一直是死的。改成直连网页接口后，带一个会话 cookie 就能搜。

    ⚠️ 输入是作品 id（或作品链接）时**直接走详情接口**：
       搜索接口只按标签/标题匹配，`/搜图 146165548` 这种必然搜不到。
    """
    illust_id = _extract_illust_id(keyword)
    if illust_id:
        return await _by_illust_id(illust_id)

    try:
        result = await _search_from_web(keyword, num, order, r18)
        if result[2] == 200:
            return result
        logger.info(f"[pixivrank] 网页搜索返回 {result[2]}，尝试 RSSHub")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[pixivrank] 网页搜索失败：{type(e).__name__}: {e}")
        if "403" in str(e):
            logger.warning(
                "[pixivrank] 403 说明需要 pixiv 会话 cookie："
                "跑 scripts/pixiv_get_cookie.py 拿 PHPSESSID 填进 .env 的 PIXR_COOKIE"
            )

    url = f"{config.pixr_rsshub}/pixiv/search/{keyword}/{order}/{r18}"
    return await parser_data(url, num)


def _web_headers(with_cookie: bool = True) -> dict:
    """WEB_HEADERS + 可选的 pixiv 会话 cookie。"""
    h = dict(WEB_HEADERS)
    if with_cookie:
        cookie = (config.pixr_cookie or "").strip()
        if cookie:
            # 允许只填 PHPSESSID 的值，自动补上 name
            if "=" not in cookie:
                cookie = f"PHPSESSID={cookie}"
            h["Cookie"] = cookie
    return h


async def _illust_detail(illust_id: str, headers: dict) -> dict:
    """
    作品详情接口 → {title, user_name, original}，失败返回 {}。

    为什么需要它：
      排行接口给的是带 `/c/480x960/img-master/` 的缩略图，能靠正则换成原图；
      但搜索接口**只返回作品 id，连缩略图都不给**，原图的日期目录
      （`img/2026/11/11/00/00/37/`）无从推断，只能再查一次详情接口。
      顺带这里也能拿到标题和作者 —— 用户直接发作品 id 时就靠它定位。
    """
    try:
        async with session.get(
            ILLUST_URL.format(illust_id=illust_id),
            headers=headers,
            proxy=config.pixr_local_proxy,
            timeout=10,
        ) as resp:
            if resp.status != 200:
                logger.warning(f"[pixivrank] illust/{illust_id} 详情 HTTP {resp.status}")
                return {}
            data = await resp.json(content_type=None)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[pixivrank] illust/{illust_id} 详情失败：{type(e).__name__}: {e}")
        return {}

    if data.get("error"):
        logger.warning(f"[pixivrank] illust/{illust_id} 详情报错：{str(data.get('message'))[:100]}")
        return {}

    body = data.get("body") or {}
    # illustType: 0=插画 1=漫画 2=小说（小说没有图，直接跳过）
    try:
        if int(body.get("illustType") or 0) == 2:
            return {}
    except (TypeError, ValueError):
        pass
    original = ((body.get("urls") or {}).get("original")) or ""
    if not original:
        return {}
    return {
        "title": body.get("illustTitle") or body.get("title") or "",
        "user_name": body.get("userName") or "",
        "original": original,
    }


async def _illust_original_url(illust_id: str, headers: dict) -> str:
    """只要原图直链的简化版（搜索流程用）。"""
    return (await _illust_detail(illust_id, headers)).get("original", "")


# ---- 直接按作品 id 定位（2026-11-14 用户要求）----
# 搜索接口只按标签/标题匹配，**永远搜不到 id**（`/搜图 146165548` 会返回空）。
# 但作品 id 恰恰是能精确定位的：走详情接口一步到位。
_ID_PATTERNS = (
    re.compile(r"^\s*(\d{5,10})\s*$"),                        # 纯数字
    re.compile(r"pixiv\.net/(?:[a-z]{2}/)?artworks/(\d{5,10})", re.I),
    re.compile(r"pixiv\.net/i/(\d{5,10})", re.I),              # 小说页
    re.compile(r"[?&]illust_id=(\d{5,10})", re.I),
)


def _extract_illust_id(text: str) -> str:
    """从用户输入里抠出作品 id：纯数字 / 作品链接 / illust_id=xxx 都认。"""
    t = (text or "").strip()
    if not t:
        return ""
    for pat in _ID_PATTERNS:
        m = pat.search(t)
        if m:
            return m.group(1)
    return ""


async def _by_illust_id(illust_id: str):
    """直接按 id 取那一张。"""
    detail = await _illust_detail(illust_id, _web_headers())
    if not detail:
        logger.warning(f"[pixivrank] 作品 id={illust_id} 拿不到详情")
        return [
            f"没有找到作品 {illust_id}（可能已删除、仅关注可见，或者它是小说）"
        ], [], 997
    logger.info(f"[pixivrank] 🎯 按 id 直达 id={illust_id} {detail['title'][:20]!r}")
    return (
        [f"标题：{detail['title']}\n{detail['user_name']}\n"],
        [[detail["original"]]],
        200,
    )


async def _random_from_ranking(pool: int = SEARCH_FALLBACK_POOL):
    """从日榜前 pool 名里随机挑一个，返回 (caption, 原图url)；失败返回 None。"""
    try:
        text_list, urls, code = await _rank_from_web("day", pool)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[pixivrank] 兜底取日榜失败：{type(e).__name__}: {e}")
        return None
    if code != 200 or not text_list or not urls:
        return None
    idx = random.randrange(len(text_list))
    picks = urls[idx] or []
    if not picks:
        return None
    return text_list[idx], picks[0]


async def _search_fallback(keyword: str):
    """
    搜索一条都没有时的兜底：随机推一张日榜。

    什么时候会真的搜不到（不是 bug）：
      · 关键词是纯符号 / 乱码 / 超长串
      · 命中结果**全是小说**（illustType=2）—— 我们把小说滤掉了
      · 命中结果**全是广告位**（isAd）
      · 关键词被 pixiv 判敏感，接口直接给空
    所以「pixiv 理论上不会空」这个假设不成立，兜底是必要的。
    """
    got = await _random_from_ranking()
    if not got:
        return ["没有搜索到喔"], [], 997
    caption, url = got
    logger.info(f"[pixivrank] 搜索 {keyword!r} 没结果，兜底随机推一张日榜")
    return [caption + "（这个关键词没搜到，随机推一张日榜）\n"], [[url]], 200


async def _search_from_web(keyword: str, num: int, order: str, r18: int):
    """
    走 pixiv 网页版搜索接口，不需要 App API 的 refresh_token。

    接口：GET /ajax/search/artworks/{keyword}
          ?word=&order=&mode=&p=1&s_mode=s_tag_full&type=all
    实测（2026-11-14）：带一个**匿名** PHPSESSID 就返回 HTTP 200 + 60 条
    （total 六十多万），所以不必真的登录账号，只要像个浏览器。

    order: popular → popular_d，date → date_d
    r18:   1 → mode=all，2 → mode=r18（受账号「閲覧制限」设置影响，匿名基本搜不到）
    """
    headers = _web_headers()
    params = {
        "word": keyword,
        "order": "popular_d" if order == "popular" else "date_d",
        "mode": "r18" if r18 == 2 else "all",
        "p": 1,
        "s_mode": "s_tag_full",
        "type": "all",
    }
    async with session.get(
        SEARCH_URL.format(keyword=quote(keyword, safe="")),
        params=params,
        headers=headers,
        proxy=config.pixr_local_proxy,
        timeout=15,
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"search HTTP {resp.status}")
        data = await resp.json(content_type=None)

    if data.get("error"):
        raise RuntimeError(f"search 接口报错: {str(data.get('message'))[:120]}")

    body = data.get("body") or {}
    items = ((body.get("illustManga") or {}).get("data")) or []
    # 搜索结果里会混广告位（isAd）和小说（illustType=2），都要剔掉
    picked = [
        it
        for it in items
        if it.get("id") and not it.get("isAd") and str(it.get("illustType", 0)) != "2"
    ]
    if not picked:
        logger.warning(f"[pixivrank] 网页搜索 keyword={keyword!r} 返回 0 条，顶层键={list(body)[:6]}")
        return await _search_fallback(keyword)

    logger.info(
        f"[pixivrank] 网页搜索 keyword={keyword!r} order={params['order']} "
        f"取到 {len(picked)} 条，首条={str(picked[0].get('title', ''))[:24]!r}"
    )

    # ---- 从搜索结果前 N 条里随机抽 1 条（2026-11-14 用户要求）----
    # 原因：搜图目前只支持单发（一条消息一张图），而一次搜索动辄几十条结果，
    # 全发既刷屏又慢。抽签还能让同一个关键词每次出不同的图。
    pool = picked[:SEARCH_POOL_SIZE]
    indexes = list(range(len(pool)))
    random.shuffle(indexes)

    # 详情接口偶发失败（作品被删/仅关注可见）时，换下一条继续试，不让整条命令失败
    for rank_idx in indexes[:SEARCH_MAX_TRIES]:
        item = pool[rank_idx]
        illust_id = str(item.get("id"))
        original = await _illust_original_url(illust_id, headers)
        if not original:
            continue
        title = item.get("title") or ""
        user_name = item.get("userName") or ""
        logger.info(
            f"[pixivrank] 🎲 从前 {len(pool)} 条里抽中第 {rank_idx + 1} 条 "
            f"id={illust_id} {title[:20]!r}"
        )
        return (
            [f"标题：{title}\n{user_name}\n"],
            [[original]],
            200,
        )

    # 抽中的几条详情都拿不到原图（作品被删 / 仅关注可见 / 全部是小说）
    logger.warning(f"[pixivrank] 抽了 {min(SEARCH_MAX_TRIES, len(pool))} 条都没拿到原图")
    return await _search_fallback(keyword)


def _ext_candidates(url: str) -> list[str]:
    """
    原图扩展名候选。

    ranking.php 给的缩略图基本是 .jpg，但原图可能是 .png / .gif
    （透明背景的作品几乎都是 png）。所以 .jpg 拿不到就换 .png / .gif 再试，
    否则会白白报「这张图下载失败了」。
    """
    base, _, ext = url.rpartition(".")
    if not base:
        return [url]
    exts = [ext] + [e for e in ("png", "gif", "jpg") if e != ext]
    return [f"{base}.{e}" for e in exts]


_tmp_cleaned = False


def _cleanup_temp(max_age_hours: int = 6) -> None:
    """清掉 data/temp/pixiv 里的旧图。每次调用写 5 张 2MB 的图，不清会把盘吃满。"""
    global _tmp_cleaned
    if _tmp_cleaned:
        return
    _tmp_cleaned = True
    try:
        if not IMAGE_PATH.exists():
            return
        deadline = time.time() - max_age_hours * 3600
        removed = 0
        for f in IMAGE_PATH.iterdir():
            if f.is_file() and f.stat().st_mtime < deadline:
                f.unlink(missing_ok=True)
                removed += 1
        if removed:
            logger.info(f"[pixivrank] 🧹 清理临时图 {removed} 个")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[pixivrank] 清理临时图失败（忽略）：{type(e).__name__}: {e}")


async def download_pixiv_imgs(urls: list, user_id: str) -> list[Path]:
    """
    把图片下载到本地，返回**本地文件路径列表**（最多 MAX_IMAGES_PER_CALL 张）。

    为什么返回路径而不是 UniMessage（2026-11-13 改）：
      alconna 的 `UniMessage.image(url="file://...")` 导出成 OneBot 段时，
      file:// 这个本地 URI 不一定落在 image 段的 `file` 字段上，
      结果就是「下载成功、发送也成功，但用户收不到图」——日志里一条错都没有。
      改成由调用方自己拼 `MessageSegment.image(f"file://{path}")`，
      和 petpet 发图完全一致，这条链路是验证过的。

    另外限制总张数：一页排行默认只取 1 个作品，但多页作品可能有十几张，
    全发出去既慢又容易被风控，所以封顶 3 张。
    """
    IMAGE_PATH.mkdir(parents=True, exist_ok=True)
    _cleanup_temp()

    saved: list[Path] = []
    for index, img in enumerate(urls):
        if len(saved) >= MAX_IMAGES_PER_CALL:
            logger.info(
                f"[pixivrank] 图片超过 {MAX_IMAGES_PER_CALL} 张，剩余 {len(urls) - index} 张跳过"
            )
            break
        for candidate in _ext_candidates(img):
            ok = False
            for _ in range(config.pixr_retry):
                try:
                    async with session.get(
                        candidate,
                        proxy=config.pixr_local_proxy,
                        timeout=10,
                        headers=headers,
                    ) as response:
                        if response.status != 200:
                            raise RuntimeError(f"HTTP {response.status}")
                        raw = await response.read()
                        ext = candidate.rpartition(".")[2].lower()
                        path = (IMAGE_PATH / f"{user_id}_{index}_pixiv.{ext}").resolve()
                        async with aiofiles.open(path, "wb") as f:
                            await f.write(raw)
                        logger.info(
                            f"[pixivrank] 💾 已保存 {path.name}（{len(raw) // 1024} KB）"
                        )
                        saved.append(path)
                        ok = True
                        break
                except TimeoutError:
                    logger.warning("网络超时, 重试")
                except Exception as e:  # noqa: BLE001
                    # 用 WARNING 而不是 debug：默认 LOG_LEVEL=INFO 时 debug 看不见，
                    # 「排行取到了但一张图都没有」就完全无从下手。
                    logger.warning(f"[pixivrank] 图片下载失败 {candidate}：{e}")
                    break
            if ok:
                break
        else:
            logger.warning(f"[pixivrank] 第 {index} 张图所有扩展名都下载失败")
    return saved