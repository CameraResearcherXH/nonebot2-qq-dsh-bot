# services/usage_chart.py
"""用量图表：横向排行 + 扇形占比，渲染成 PNG 后由 /用量 发出去。

为什么用 image-charts：
  Google Image Charts（chart.googleapis.com/chart）2024 年已经下线，直接请求是 404。
  image-charts.com 是**兼容同一套 cht/chd/chl 参数**的替代服务，改 BASE_URL 就能换回
  任何同协议的图床；图表参数仍然按 Google Image Charts 的写法组织。

设计：
  · 数据只从 llm_usage.snapshot() 取，文本报表和图表口径一致；
  · 先下载 PNG 到 data/temp/，再发 file://（NapCat 传外链容易超时）；
  · 下载失败只记日志，不影响文字报表。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import quote

import httpx

try:  # 离线单测没有 nonebot
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

from services import llm_usage

# Google Image Charts 已停用；这个服务兼容同样的参数。
BASE_URL = os.getenv("USAGE_CHART_BASE", "https://image-charts.com/chart")
OUT_DIR = Path(os.getenv("USAGE_CHART_DIR", "data/temp"))

# 最多画几项（太长的尾巴并成 other）
TOP_N = 8
# 饼图切片太多小标签会叠在一起；只画前 4 + other，保证能看清。
_PIE_TOP = 5
_COLORS = (
    "4E79A7", "F28E2B", "E15759", "76B7B2", "59A14F",
    "EDC948", "B07AA1", "FF9DA7", "9C755F", "BAB0AC",
)

# 图表里用中文，别让主人对着一串 purpose 英文猜。
_PURPOSE_CN = {
    "chat": "主对话", "preflight": "预检", "judge": "心情判读",
    "audit": "审核", "tool_loop": "工具轮", "conv_state": "对话状态",
    "expression": "表达学习", "profile_miner": "档案挖掘",
    "vision": "识图", "link_preview": "链接预览", "sticker": "表情",
    "song": "唱歌", "other": "其他",
}


def _cn(purpose: str) -> str:
    return _PURPOSE_CN.get(str(purpose), str(purpose))


def _pct_items(snap: dict, top_n: int = TOP_N) -> list:
    """[(label, percent)]，超过 top_n 的并成 other。"""
    total = float(snap.get("total") or 1)
    order = snap.get("order") or []
    items = []
    for purpose, d in order:
        tok = float((d.get("p") or 0) + (d.get("c") or 0))
        if tok <= 0:
            continue
        items.append((str(purpose), tok / total * 100.0))
    if len(items) > top_n:
        head = items[:top_n - 1]
        tail = sum(v for _, v in items[top_n - 1:])
        head.append(("other", tail))
        items = head
    return items


def _url(params: dict) -> str:
    # 只编码中文/空格/%；| : , 这些分隔符保留，Google 协议认。
    qs = "&".join(
        f"{k}={quote(str(v), safe=':|,')}" for k, v in params.items())
    return f"{BASE_URL}?{qs}"


def build_charts(items: list, title: str, *, top_n: int = TOP_N,
                 pie_top: int = _PIE_TOP) -> list:
    """通用：[(label, percent)] → [横向柱状图 URL, 扇形图 URL]。"""
    items = [(str(k), float(v)) for k, v in (items or []) if float(v) > 0]
    if not items:
        return []
    if len(items) > top_n:
        head = items[:top_n - 1]
        tail = sum(v for _, v in items[top_n - 1:])
        head.append(("其他", tail))
        items = head
    labels = [str(x[0]) for x in items]
    vals = [x[1] for x in items]
    colors = ",".join(_COLORS[i % len(_COLORS)] for i in range(len(items)))

    # 横向柱状图（cht=bhg）
    bar = _url({
        "cht": "bhg", "chs": "760x420",
        "chd": "t:" + ",".join(f"{v:.1f}" for v in vals),
        "chds": "0,%.1f" % max(10.0, max(vals) * 1.15),
        "chxt": "x,y",
        # ⚠️ cht=bhg 的 y 轴标签是「从下往上」排的，数据是「从上往下」；
        #    不反转标签会出现「最大项贴到最小项名字上」。
        "chxl": "1:|" + "|".join(reversed(labels)),
        "chco": colors,
        "chbh": "28",
        "chtt": title,
        "chts": "333333,18",
        "chg": "20,0",
    })

    # 扇形图（cht=p3），饼图项少一点，标签带百分比
    pie_items = items[:max(1, int(pie_top))]
    pie_labels = [f"{k} {v:.1f}%" for k, v in pie_items]
    pie_colors = ",".join(_COLORS[i % len(_COLORS)] for i in range(len(pie_items)))
    pie = _url({
        "cht": "p3", "chs": "760x420",
        "chd": "t:" + ",".join(f"{v:.1f}" for _, v in pie_items),
        # 切片标签在 6 项以上会叠在一起；用右侧图例，百分比也写在图例里。
        "chdl": "|".join(pie_labels),
        "chco": pie_colors,
        "chtt": title,
        "chts": "333333,18",
    })
    return [bar, pie]


def chart_urls(days: int = 1) -> list:
    """返回 [横向排行图 URL, 扇形占比图 URL]；没数据返回 []。"""
    snap = llm_usage.snapshot(days)
    items = [(_cn(k), v) for k, v in _pct_items(snap, TOP_N)]
    return build_charts(items, f"Token 用量 · 最近 {days} 天")


async def download_urls(urls: list, prefix: str = "chart",
                        timeout: float = 25.0) -> list:
    """把任意图表 URL 下载到 data/temp/，返回本地路径列表（失败项跳过）。"""
    if not urls:
        return []
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    stamp = int(time.time() * 1000)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        for i, url in enumerate(urls):
            try:
                r = await c.get(url)
                ok = r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
                if not ok:
                    logger.warning(
                        f"[CHART] 图表 {i} 渲染失败：HTTP {r.status_code}, "
                        f"{len(r.content)} bytes")
                    continue
                p = OUT_DIR / f"{prefix}_{stamp}_{i}.png"
                p.write_bytes(r.content)
                out.append(str(p))
            except Exception as e:
                logger.warning(f"[CHART] 图表 {i} 下载失败：{type(e).__name__}: {e}")
    return out


async def download_charts(days: int = 1, timeout: float = 25.0) -> list:
    """用量图表：下载到 data/temp/，返回本地路径列表。"""
    return await download_urls(chart_urls(days), prefix=f"usage_{days}d",
                               timeout=timeout)
