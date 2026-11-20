"""
转发卡片 / 外链预览：把「小强转发了一个B站视频」变成结构化文本给 DeepSeek 看。

三层信息，从便宜到贵：
  ① QQ 卡片自带的 json/xml 段（title / desc / jumpUrl / preview）—— 零成本，先拿
  ② B 站公开 API 补全（标题 / UP主 / 简介 / 封面 / 时长 / 播放量）
  ③ 封面图交给视觉模型总结（复用 vision_agent.describe_image_bytes）

最终拼成：
    小强转发了一个哔哩哔哩视频
    · 标题：xxx
    · UP主：xxx
    · 简介：xxx
    · 封面：视觉模型看封面后的总结
"""
import json
import re

import httpx
from nonebot import logger

BILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
BILI_PLAYER_API = "https://api.bilibili.com/x/player/v2"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_SHORT_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+")
_BUVID_RE = re.compile(r"[?&]buvid=([A-Za-z0-9]+)")


def _extract_buvid(url: str) -> str:
    """从 B 站 URL 里抠 buvid（b23.tv 302 后的 URL 会带，B 站 API 风控时要用它当 cookie）。"""
    m = _BUVID_RE.search(url or "")
    return m.group(1) if m else ""


def _bili_headers(buvid: str = "") -> dict:
    """B 站 API 请求头。不带 buvid cookie 很容易被风控返回 412。"""
    h = {"User-Agent": _UA, "Referer": "https://www.bilibili.com/"}
    if buvid:
        h["Cookie"] = f"buvid3={buvid}"
    return h


def card_from_segments(segs) -> dict:
    """从一段消息段（MessageSegment 或原始 dict 都行）里抠出 title/desc/url/cover。"""
    if not segs:
        return {}

    def _st(seg):
        return seg.get("type") if isinstance(seg, dict) else getattr(seg, "type", "")

    def _sd(seg):
        return (seg.get("data") or {}) if isinstance(seg, dict) else (getattr(seg, "data", {}) or {})

    for seg in segs:
        if _st(seg) != "json":
            continue
        raw = str(_sd(seg).get("data") or "")
        try:
            data = json.loads(raw)
        except Exception:
            continue
        meta = data.get("meta") or {}
        for key in ("news", "detail_1", "music", "video", "article"):
            node = meta.get(key)
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or node.get("desc") or "").strip()
            desc = str(node.get("desc") or "").strip()
            url = str(node.get("jumpUrl") or node.get("qqdocurl") or "").strip()
            cover = str(node.get("preview") or node.get("pic") or "").strip()
            if title or url:
                return {"title": title, "desc": desc, "url": url, "cover": cover}

    # 兜底：xml 卡片里用 <title>/<summary> 标签
    for seg in segs:
        if _st(seg) != "xml":
            continue
        raw = str(_sd(seg).get("data") or "")
        if not raw:
            continue
        def _tag(name):
            m = re.search(rf"<{name}>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</{name}>", raw)
            return (m.group(1).strip() if m else "")
        title = _tag("title")
        url = _tag("url") or _tag("jumpUrl")
        if title or url:
            return {"title": title, "desc": _tag("summary"),
                    "url": url, "cover": _tag("picture")}
    return {}


def card_from_message(event) -> dict:
    """从 QQ 卡片消息（json / xml 段）里抠出 title / desc / url / cover。"""
    try:
        return card_from_segments(event.get_message())
    except Exception:
        return {}


async def _expand_short(url: str) -> str:
    """b23.tv 短链 → 真实地址（优先跟 302，不行就从返回 HTML 里抓 BV）。"""
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers={"User-Agent": _UA,
                                              "Referer": "https://www.bilibili.com/"}) as c:
            r = await c.get(url)
            final = str(r.url)
            if _BV_RE.search(final):
                logger.info(f"[LINK] b23.tv 302 成功：{final[:80]}")
                return final
            # 有些 b23.tv 不 302，而是 200 返回一个 JS 跳转页 —— 从 HTML 里抠 BV
            m = _BV_RE.search(r.text or "")
            if m:
                logger.info(f"[LINK] b23.tv 没 302，从 HTML 里抓到 {m.group(0)}")
                return f"https://www.bilibili.com/video/{m.group(0)}"
            logger.warning(
                f"[LINK] b23.tv 解析不到 BV：HTTP {r.status_code} final={final[:100]!r} "
                f"body前80字={(r.text or '')[:80]!r}"
            )
    except Exception as e:
        logger.warning(f"[LINK] 短链展开失败: {type(e).__name__}: {e}")
    return url


async def bili_detail(url: str) -> dict:
    """拿 B 站视频详情。失败返回 {}（不抛）。"""
    try:
        target = url
        if _SHORT_RE.match(url or ""):
            target = await _expand_short(url)
        buvid = _extract_buvid(target)
        m = _BV_RE.search(target or "")
        if not m:
            logger.warning(f"[LINK] 链接里没有 BV 号：{str(target)[:120]!r}")
            return {}
        async with httpx.AsyncClient(timeout=10, headers=_bili_headers(buvid)) as c:
            r = await c.get(BILI_VIEW_API, params={"bvid": m.group(1)})
            try:
                payload = r.json() or {}
            except Exception:
                payload = {}
            data = payload.get("data") or {}
        if not data:
            logger.warning(
                f"[LINK] view 接口没拿到数据：HTTP {r.status_code} "
                f"code={payload.get('code')} msg={str(payload.get('message'))[:80]!r} "
                f"bvid={m.group(1)}"
            )
            return {}
        owner = data.get("owner") or {}
        stat = data.get("stat") or {}
        # cid：单 P 视频在 data.cid；多 P 时 data.cid 可能是第一 P，有些情况只在 pages 里。
        cid = int(data.get("cid") or 0)
        if not cid:
            pages = data.get("pages") or []
            if pages and isinstance(pages[0], dict):
                cid = int(pages[0].get("cid") or 0)
        return {
            "title": str(data.get("title") or "").strip(),
            "desc": str(data.get("desc") or "").strip(),
            "owner": str(owner.get("name") or "").strip(),
            "cover": str(data.get("pic") or "").strip(),
            "duration": int(data.get("duration") or 0),
            "views": int(stat.get("view") or 0),
            "cid": cid,
            "bvid": m.group(1),
            "buvid": buvid,
        }
    except Exception as e:
        logger.warning(f"[LINK] B站详情失败: {type(e).__name__}: {e}")
        return {}


async def bili_cid(bvid: str, buvid: str = "") -> int:
    """
    拿视频第一个分 P 的 cid（x/player/pagelist，比 view 接口更稳定）。
    失败返回 0。
    """
    if not bvid:
        return 0
    try:
        async with httpx.AsyncClient(timeout=10, headers=_bili_headers(buvid)) as c:
            r = await c.get("https://api.bilibili.com/x/player/pagelist",
                            params={"bvid": bvid})
            data = (r.json() or {}).get("data") or []
            if data and isinstance(data[0], dict):
                return int(data[0].get("cid") or 0)
    except Exception as e:
        logger.debug(f"[LINK] pagelist 拿 cid 失败: {type(e).__name__}: {e}")
    return 0


async def _describe_cover(cover_url: str) -> str:
    """封面图 → 视觉模型一句话总结。失败返回空（不影响主流程）。"""
    if not cover_url:
        return ""
    try:
        from plugins.vision_agent import describe_image_bytes, DEFAULT_VISION_PROMPT
    except Exception:
        return ""
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": _UA}) as c:
            r = await c.get(cover_url)
            if r.status_code != 200 or not r.content:
                return ""
        # ⚠️ 必须显式传 prompt：describe_image_bytes 默认是「表情包情绪词表」，
        #    封面图套那个会得到「无语｜画面…」这种完全跑偏的结果。
        desc = await describe_image_bytes(r.content, DEFAULT_VISION_PROMPT)
        return str(desc or "").strip()
    except Exception as e:
        logger.debug(f"[LINK] 封面识别失败: {type(e).__name__}: {e}")
        return ""


async def bili_subtitle_segments(bvid: str, cid: int, buvid: str = "") -> list:
    """
    拿 B 站 CC 字幕（带时间戳），返回 [(from_sec, to_sec, text), ...]。
    没字幕 / 接口变了 → 返回 []（不抛）。

    给视频抽帧用：靠字幕时间戳找「关键位置」，在这些位置多取几张高清帧。
    """
    if not bvid or not cid:
        return []
    try:
        async with httpx.AsyncClient(timeout=12, headers=_bili_headers(buvid)) as c:
            r = await c.get(BILI_PLAYER_API, params={"bvid": bvid, "cid": cid})
            sub = ((r.json() or {}).get("data") or {}).get("subtitle") or {}
            items = sub.get("subtitles") or []
            if not items:
                return []
            url = str(items[0].get("subtitle_url") or "")
            if not url:
                return []
            if url.startswith("//"):
                url = "https:" + url
            r2 = await c.get(url)
            body = (r2.json() or {}).get("body") or []
        segs = []
        for x in body:
            if not isinstance(x, dict):
                continue
            try:
                segs.append((float(x.get("from") or 0),
                             float(x.get("to") or 0),
                             str(x.get("content") or "")))
            except Exception:
                continue
        return segs
    except Exception as e:
        logger.debug(f"[LINK] 字幕获取失败: {type(e).__name__}: {e}")
        return []


async def bili_subtitle(bvid: str, cid: int, max_chars: int = 4000) -> str:
    """
    拿 B 站字幕全文。没字幕 / 需要登录 / 接口变了 → 返回 ""（不抛）。
    ⚠️ 很多视频没有 CC 字幕，这时候只能靠标题+简介+搜索。
    """
    segs = await bili_subtitle_segments(bvid, cid)
    text = "".join(t for _, _, t in segs)
    return text.strip()[:max_chars]


async def _deepseek_summary(title: str, desc: str, subtitle: str,
                            extra: str = "") -> str:
    """
    用 DeepSeek 做「深度思考式」总结：优先用字幕，没有就用简介 + 搜索补充。
    返回 2~3 句中文总结；失败返回 ""。
    """
    try:
        from .llm import XAI_API_KEY, XAI_BASE_URL, XAI_MODEL
    except Exception:
        return ""
    if not XAI_API_KEY:
        return ""

    if subtitle:
        material = f"【视频字幕（节选）】\n{subtitle[:3000]}"
    else:
        material = f"【视频简介】\n{desc[:600]}"
        if extra:
            material += f"\n\n【搜索到的相关资料】\n{extra[:1200]}"

    prompt = (
        f"这是一个B站视频，标题：{title}\n\n{material}\n\n"
        "请用中文总结这个视频讲了什么，2~3 句话，说清楚核心内容，"
        "不要客套话、不要说『这个视频』开头。直接给结论。"
    )
    try:
        async with httpx.AsyncClient(timeout=45) as c:
            r = await c.post(
                f"{XAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {XAI_API_KEY}",
                         "Content-Type": "application/json"},
                json={"model": XAI_MODEL,
                      "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.3, "max_tokens": 300},
            )
            if r.status_code != 200:
                return ""
            _d = r.json()
            try:
                from . import llm_usage as _lu
                _lu.record_from_response(_d, purpose="link_preview")
            except Exception:
                pass
            msg = _d["choices"][0]["message"]
            return str(msg.get("content") or "").strip()[:300]
    except Exception as e:
        logger.debug(f"[LINK] 视频总结失败: {type(e).__name__}: {e}")
        return ""


def _fmt_duration(sec: int) -> str:
    if sec <= 0:
        return ""
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def preview_text(event, *, with_cover: bool = True) -> str:
    """
    把一条转发卡片变成一段结构化文本。不是卡片消息返回 ""。

    输出示例：
        小强转发了一个哔哩哔哩视频
        · 标题：xxxx
        · UP主：xxx ｜ 时长 12:34 ｜ 播放 12.3万
        · 简介：xxxx
        · 封面：画面里是一个……
    """
    card = card_from_message(event)
    if not card:
        return ""

    url = card.get("url") or ""
    detail = await bili_detail(url) if ("bilibili.com" in url or "b23.tv" in url) else {}

    title = detail.get("title") or card.get("title") or ""
    desc = detail.get("desc") or card.get("desc") or ""
    owner = detail.get("owner") or ""
    cover = detail.get("cover") or card.get("cover") or ""

    is_bili = bool(detail) or ("bilibili.com" in url or "b23.tv" in url)
    head = "转发了一个哔哩哔哩视频" if is_bili else "转发了一个链接"

    lines = [head]
    if title:
        lines.append(f"· 标题：{title[:80]}")
    meta = []
    if owner:
        meta.append(f"UP主：{owner}")
    dur = _fmt_duration(detail.get("duration") or 0)
    if dur:
        meta.append(f"时长 {dur}")
    views = detail.get("views") or 0
    if views:
        meta.append(f"播放 {views / 10000:.1f}万" if views >= 10000 else f"播放 {views}")
    if meta:
        lines.append("· " + " ｜ ".join(meta))
    if desc:
        lines.append(f"· 简介：{desc[:160]}")
    # ⚠️ B 站链接也要把 url 留下来：否则她后面被问「能看到这个视频内容吗」时，
    #    上下文里没有链接，没法调 video_understand 去真正看视频。
    if url:
        lines.append(f"· 链接：{url[:120]}")

    if with_cover:
        cover_desc = await _describe_cover(cover)
        if cover_desc:
            lines.append(f"· 封面：{cover_desc[:120]}")

    # 内容总结：有 CC 字幕就用字幕总结；没字幕就别用搜索糊弄——
    # 留给 video_understand 真正下载视频抽帧看（2026-11-17）。
    if is_bili and title:
        sub = await bili_subtitle(detail.get("bvid") or "", detail.get("cid") or 0)
        if sub:
            summary = await _deepseek_summary(title, desc, sub)
            if summary:
                lines.append(f"· 内容总结（据字幕）：{summary}")
        else:
            lines.append("· ⚠️ 没字幕，标题/封面看不出内容 → 想真看就调 video_understand 工具")

    # 只有标题也至少要给出标题，别返回空
    return "\n".join(lines) if len(lines) > 1 else ""
