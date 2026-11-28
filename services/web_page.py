# services/web_page.py
"""普通网页读取：把 URL 的正文抓下来，喂给模型回答/做事。

为什么需要：
  以前只有 B 站卡片预览（services/link_preview.py），普通网页链接在消息里
  就只是一串 URL → 模型只能回「我点不开」。用户 2026-11-23 发 AI 自测题
  （静态 HTML）让她做，她直接说点不开。

设计：
  · 只抓 http/https，限制大小和超时；
  · 去掉 script/style/标签，保留段落换行（题目/选项还能读）；
  · 私聊 / 叫了她 / 明确说「看/读/打开/做一下」才自动抓，避免群里每个链接都请求；
  · 基础 SSRF 防护：拒绝 localhost / 内网 IP 字面量。
"""
from __future__ import annotations

import html as _html
import ipaddress
import re
from urllib.parse import urlparse

import httpx

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_URL_RE = re.compile(r"https?://[^\s<>\"'）)】〕，。！？、]+", re.I)
_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
             ".mp4", ".mp3", ".wav", ".zip", ".rar", ".7z", ".exe",
             ".apk", ".dmg", ".iso")
_SKIP_HOSTS = ("bilibili.com", "b23.tv", "multimedia.nt.qq.com.cn",
               "q1.qlogo.cn", "q2.qlogo.cn", "q3.qlogo.cn", "q4.qlogo.cn")
_MAX_HTML = 500_000
_MAX_TEXT = 4000


def find_urls(text: str) -> list:
    out = []
    for m in _URL_RE.finditer(str(text or "")):
        u = m.group(0).rstrip(".,;:!?)]}，。；：！？）】〕")
        if u and u not in out:
            out.append(u)
    return out


def should_skip(url: str) -> bool:
    """B 站有专门链路；图片/压缩包不抓；内网地址不抓。"""
    try:
        p = urlparse(url)
    except Exception:
        return True
    if p.scheme not in ("http", "https") or not p.hostname:
        return True
    host = p.hostname.lower()
    if any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS):
        return True
    if p.path.lower().endswith(_SKIP_EXT):
        return True
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    except ValueError:
        pass
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    return False


def html_to_text(raw: str) -> str:
    s = str(raw or "")
    s = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(
        r"(?i)</(?:p|div|section|article|li|ul|ol|h[1-6]|tr|td|th|dt|dd|"
        r"table|main|header|footer|blockquote|pre)>", "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n[ \t]*\n+", "\n", s)
    return s.strip()


def _title_of(raw: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", str(raw or ""))
    return html_to_text(m.group(1))[:120] if m else ""


async def fetch_page(url: str, timeout: float = 18.0) -> dict:
    """抓一个网页，返回 {url,title,text,error}。"""
    if should_skip(url):
        return {"url": url, "title": "", "text": "", "error": "不抓这类链接"}
    try:
        async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True,
                headers={"User-Agent": _UA,
                         "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5"}) as c:
            r = await c.get(url)
        final = str(r.url)
        if should_skip(final):
            return {"url": final, "title": "", "text": "", "error": "跳转到了不抓的地址"}
        ctype = str(r.headers.get("content-type") or "").lower()
        if r.status_code != 200:
            return {"url": final, "title": "", "text": "",
                    "error": f"HTTP {r.status_code}"}
        if ctype and not any(x in ctype for x in ("html", "text/plain", "xml")):
            return {"url": final, "title": "", "text": "",
                    "error": f"不是网页（{ctype[:40]}）"}
        raw = r.text[:_MAX_HTML]
        text = html_to_text(raw)
        title = _title_of(raw)
        if not text:
            return {"url": final, "title": title, "text": "", "error": "页面没有可读正文"}
        return {"url": final, "title": title, "text": text[:_MAX_TEXT], "error": ""}
    except Exception as e:
        logger.debug(f"[WEB] 抓取失败 {url[:80]}: {type(e).__name__}: {e}")
        return {"url": url, "title": "", "text": "",
                "error": f"{type(e).__name__}: {e}"}
