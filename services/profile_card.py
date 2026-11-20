# services/profile_card.py
"""
把人物档案渲染成一张图（用户 2026-11-18：/档案 直接发一份档案图片）。

设计原则：
  · 头像三级来源（2026-11-19 用户：「没有档案卡片渲染 NapCat 获得用户头像的内容」）：
      ① data/avatars 缓存 → ② NapCat（QQ 客户端，见 plugins/chat._ensure_avatar）
      → ③ 本文件 _avatar_download 直连 qlogo（多源 + 代理兜底）
    都拿不到才退回「名字首字」圆 —— 绝不因为取头像把 /档案 拖挂；
  · 模板固定：人 / 简介 / 事情 / 关系 / 语气 / 注意，和 prompt 里的文字卡一致；
  · 失败绝不抛：渲染失败返回 ""，调用方退回文字版。
"""
from core import tuning

import hashlib
import json
import os
import re
import time
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = ImageDraw = ImageFont = None

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

from . import profiles

CARD_DIR = Path("data/profile_cards")
CARD_DIR.mkdir(parents=True, exist_ok=True)

W = 760
PAD = 34
# 卡片版本：改视觉/取头像逻辑就 +1。
# 它进缓存键（旧图不会被当成新图复用），也画在脚注 —— 用户 2026-11-19
# 「改了没看到变更」踩过太多次，图上带版本号，一眼就知道服务器跑的是哪一版。
CARD_VER = "v3"
BG_TOP = (12, 19, 31)
BG_BOTTOM = (18, 31, 48)
CARD_BG = (22, 33, 47)
ACCENT = (86, 166, 255)
TEXT = (232, 238, 246)
MUTED = (143, 163, 186)
LINE = (42, 58, 78)

_FONT_CACHE: dict = {}

# 图里不画 emoji：中文字体没有对应字形，会变成「☒」。
# 情绪标记在文字卡里保留，图里去掉更干净。
_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]"
)


def _clean(s) -> str:
    t = _EMOJI_RE.sub(" ", str(s or ""))
    # 图里不画 markdown 标记
    t = t.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", t).strip()


def _font_candidates(bold: bool = False):
    name = "msyhbd.ttc" if bold else "msyh.ttc"
    return [
        Path.cwd() / "fonts" / name,
        Path.cwd() / "fonts" / "MicrosoftYaHei" / "Microsoft Yahei.ttf",
        Path("/app/fonts") / name,
        Path("/app/fonts/MicrosoftYaHei/Microsoft Yahei.ttf"),
        Path("/root/.fonts") / name,
        Path("/root/.fonts/MicrosoftYaHei/Microsoft Yahei.ttf"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold
             else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf" if bold
             else "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
        Path("/usr/share/fonts/truetype/arphic/uming.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]


def _font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for p in _font_candidates(bold):
        try:
            if p.is_file():
                f = ImageFont.truetype(str(p), size=size)
                _FONT_CACHE[key] = f
                return f
        except Exception:
            continue
    f = ImageFont.load_default()
    _FONT_CACHE[key] = f
    return f


def _tw(draw, text: str, font) -> int:
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        return len(text) * 12


# ASCII 单词（deepseek-chat / v2026-11-19e / llm:xxx）折行时不能从中间断开，
# 否则卡片上会出现 "mod / el=deepseek-chat"、"un / known" 这种断词。
_ASCII_WORD = re.compile(r"[A-Za-z0-9_\-./:=+%#@]+")


def _tokens(s: str):
    """把一行切成「ASCII 单词（含后面的空格）」或「单个汉字/符号」。"""
    i, n = 0, len(s)
    while i < n:
        m = _ASCII_WORD.match(s, i)
        if m:
            j = m.end()
            tok = m.group(0)
            if j < n and s[j] == " ":
                tok += " "
                j += 1
            yield tok
            i = j
        else:
            yield s[i]
            i += 1


def _wrap(draw, text: str, font, max_w: int) -> list:
    out, cur = [], ""
    for para in str(text or "").split("\n"):
        if not para:
            if cur:
                out.append(cur.strip())
            cur = ""
            continue
        for tok in _tokens(para):
            if _tw(draw, cur + tok, font) <= max_w:
                cur += tok
                continue
            if cur.strip():
                out.append(cur.strip())
            cur = tok.lstrip()
            # 单个 token 本身就超宽（超长网址）→ 只能硬切
            while _tw(draw, cur, font) > max_w and len(cur) > 1:
                k = len(cur)
                while k > 1 and _tw(draw, cur[:k], font) > max_w:
                    k -= 1
                out.append(cur[:k])
                cur = cur[k:]
    if cur.strip():
        out.append(cur.strip())
    return out or [""]


def _monogram(name: str) -> str:
    s = str(name or "?").strip()
    if not s:
        return "?"
    # 去掉表情/符号，取第一个汉字或字母
    for ch in s:
        if re.match(r"[\u4e00-\u9fa5A-Za-z0-9]", ch):
            return ch.upper()
    return "?"


AVATAR_DIR = Path(os.getenv("AVATAR_DIR", "data/avatars"))
AVATAR_TTL = tuning.f("AVATAR_TTL_DAYS") * 86400
AVATAR_PX = tuning.i("AVATAR_PX")


_AVATAR_WARNED: set = set()
_AVATAR_OK_LOGGED: set = set()
# 最近一次取头像的结果：qq -> (ok, 说明, 时间)。/测函数 头像 会打出来。
_AV_LAST: dict = {}

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"

# 只认图片魔数：qlogo 会「HTTP 200 + 风控页 HTML」，以前只看长度会把网页当头像存下来。
_IMG_MAGIC = (b"\xff\xd8", b"\x89PNG", b"GIF8", b"RIFF", b"BM")


def _looks_image(data: bytes) -> bool:
    return bool(data) and any(data.startswith(m) for m in _IMG_MAGIC)


def _proxy_env() -> str:
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
              "ALL_PROXY", "all_proxy"):
        v = str(os.getenv(k) or "").strip()
        if v:
            return v
    return ""


def _avatar_urls(qq: str) -> tuple:
    """
    头像源，按「最可能成功」排序。

    为什么 s=640 放第一：qlogo 只认 0/40/100/140/640 这几档，传别的档位
    可能给回一张很小的图；640 是明确有效的，反正后面要缩到 88px。
    最后一条故意用 http —— petpet 一直用 http 拉 qlogo 且能跑通，
    万一容器里 https 握手有问题（缺 CA/被中间设备拦），这条是保命的。
    """
    return (f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640",
            f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s={AVATAR_PX}",
            f"https://q.qlogo.cn/headimg_dl?dst_uin={qq}&spec=640",
            f"http://q1.qlogo.cn/g?b=qq&nk={qq}&s=640")


def _try_urls(qq: str, f: Path, trust_env: bool, errs: list) -> bool:
    """按 _avatar_urls 逐个试；成功就落盘。trust_env=走容器里的 *_PROXY。"""
    try:
        import httpx
    except Exception as e:
        errs.append(f"没装 httpx（{type(e).__name__}）")
        return False
    tag = "代理" if trust_env else "直连"
    try:
        client = httpx.Client(timeout=6, follow_redirects=True,
                              trust_env=trust_env,
                              headers={"User-Agent": _UA})
    except Exception as e:
        errs.append(f"{tag} 建 client 失败（{type(e).__name__}）")
        return False
    try:
        for url in _avatar_urls(qq):
            host = url.split("/")[2]
            for attempt in (1, 2):
                try:
                    r = client.get(url)
                except Exception as e:
                    if attempt == 2:
                        errs.append(f"{tag} {host} {type(e).__name__}")
                    continue
                if r.status_code == 200 and _looks_image(r.content):
                    f.write_bytes(r.content)
                    return True
                if attempt == 2:
                    kind = "图片" if _looks_image(r.content) else "非图片"
                    errs.append(f"{tag} {host} HTTP{r.status_code}/{len(r.content)}B/{kind}")
                break
    finally:
        try:
            client.close()
        except Exception:
            pass
    return False


def _avatar_download(qq: str) -> tuple:
    """
    下载头像到 data/avatars/<qq>.jpg。返回 (ok, 说明)。

    ⚠️ 这是**最后一道**：优先应该走 plugins/chat 的 _ensure_avatar()，
       它会先让 NapCat（QQ 客户端）去拿 —— 见 services/profile_card 文档。
       这里的直连只在 NapCat 也给不出图时兜底。
    """
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return False, f"{qq or '?'} 不是 QQ 号"
    try:
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return False, f"缓存目录建不了（{type(e).__name__}: {e}）"
    f = AVATAR_DIR / f"{qq}.jpg"
    errs = []
    # ⚠️ trust_env=False：容器里可能有 HTTP_PROXY/ALL_PROXY，httpx 默认会走它 →
    #    连 qlogo 直接失败（项目里其它出网调用也都是 trust_env=False）。
    #    但反过来也有环境只有代理能出网，所以直连失败后再用代理试一轮。
    if _try_urls(qq, f, False, errs):
        why = f"直连成功 {f.stat().st_size / 1024:.1f}KB"
        _AV_LAST[qq] = (True, why, time.time())
        return True, why
    if _proxy_env() and _try_urls(qq, f, True, errs):
        why = f"走代理成功 {f.stat().st_size / 1024:.1f}KB"
        _AV_LAST[qq] = (True, why, time.time())
        return True, why
    why = "下载失败：" + "；".join(errs[:6] or ["没有可用源"])
    _AV_LAST[qq] = (False, why, time.time())
    return False, why


def avatar_fresh(qq: str) -> bool:
    """缓存里的头像还能用吗（存在 + 不是空壳 + 没过期）。"""
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return False
    f = AVATAR_DIR / f"{qq}.jpg"
    try:
        return (f.exists() and f.stat().st_size > 200
                and (time.time() - f.stat().st_mtime) < AVATAR_TTL)
    except Exception:
        return False


def store_avatar(qq: str, data: bytes) -> tuple:
    """
    把外部（NapCat）拿到的头像字节写进缓存。返回 (ok, 说明)。

    为什么要有这个入口：NapCat 是真正的 QQ 客户端，它拿头像的成功率远高于
    容器直连 qlogo（容器可能没网/被风控/走不通代理）。拿到字节后写进同一个
    缓存文件，后面 render_profile_image 就零改动地能用上。
    """
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return False, f"{qq or '?'} 不是 QQ 号"
    if not data or len(data) < 200:
        return False, f"数据太小（{len(data or b'')}B）"
    if not _looks_image(data):
        return False, "不是图片（可能是风控页/HTML）"
    try:
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        f = AVATAR_DIR / f"{qq}.jpg"
        f.write_bytes(data)
        if Image is not None:
            with Image.open(f) as im:      # verify() 会读整块，坏了就抛
                im.verify()
    except Exception as e:
        return False, f"写缓存失败 {type(e).__name__}: {e}"
    why = f"已存 {len(data) / 1024:.1f}KB"
    _AV_LAST[qq] = (True, why, time.time())
    return True, why


def avatar_probe(qq: str) -> str:
    """
    /测函数 头像 <QQ>：把每个源的 HTTP 状态/字节数/是不是图片/耗时全打出来。
    用户 2026-11-19「档案左上角还是不显示头像」——不能只回一句「没头像」。
    """
    qq = str(qq or "").strip()
    head = f"🖼 头像自检 QQ {qq or '?'}"
    if not qq.isdigit():
        return head + "\n· 不是 QQ 号（头像只认数字 QQ）"
    if Image is None:
        return head + "\n· 环境没有 Pillow，画不了图"
    lines = [head]
    f = AVATAR_DIR / f"{qq}.jpg"
    if f.exists():
        age_h = (time.time() - f.stat().st_mtime) / 3600
        try:
            sz = Image.open(f).size
        except Exception:
            sz = "坏文件"
        lines.append(f"· 缓存：{f.stat().st_size / 1024:.1f}KB｜{sz}｜{age_h:.1f}h 前")
    else:
        lines.append("· 缓存：没有")
    px = _proxy_env()
    lines.append(f"· 环境代理：{px.split('//')[-1][:36] if px else '无'}")
    last = _AV_LAST.get(qq)
    if last:
        lines.append(f"· 上次：{'成功' if last[0] else '失败'}｜{last[1][:70]}")
    try:
        import httpx
    except Exception as e:
        lines.append(f"· httpx：没装（{type(e).__name__}）")
        return "\n".join(lines)
    try:
        client = httpx.Client(timeout=8, follow_redirects=True, trust_env=False,
                              headers={"User-Agent": _UA})
    except Exception as e:
        lines.append(f"· 建 client 失败（{type(e).__name__}: {e}）")
        return "\n".join(lines)
    try:
        for url in _avatar_urls(qq):
            host = url.split("/")[2] + ("(http)" if url.startswith("http:") else "")
            t0 = time.time()
            try:
                r = client.get(url)
                ms = int((time.time() - t0) * 1000)
                kind = "图片" if _looks_image(r.content) else "非图片"
                lines.append(f"· {host} HTTP{r.status_code} {len(r.content)}B {kind} {ms}ms")
            except Exception as e:
                ms = int((time.time() - t0) * 1000)
                lines.append(f"· {host} 失败 {type(e).__name__} {ms}ms")
    finally:
        try:
            client.close()
        except Exception:
            pass
    lines.append("· 真机 /档案 时会先让 NapCat 取，再退到这里直连")
    return "\n".join(lines)


def _avatar_image(qq: str):
    """
    取 QQ 头像（PIL Image / None），缓存到 data/avatars/<qq>.jpg，AVATAR_TTL_DAYS 天更新。

    用户 2026-11-19：「档案左上角可以刚好获取 qq 头像标记到卡片上去。」
    拿不到就退回「名字首字」圆（老行为）—— 但**必须留日志**，
    不然真机上只能看到「还是没头像」，不知道是网络、UA 还是缓存坏了。
    """
    if Image is None:
        return None
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return None
    try:
        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        f = AVATAR_DIR / f"{qq}.jpg"
        fresh = f.exists() and (time.time() - f.stat().st_mtime) < AVATAR_TTL
        if not fresh:
            ok, why = _avatar_download(qq)
            if not ok and qq not in _AVATAR_WARNED:
                _AVATAR_WARNED.add(qq)
                logger.warning(f"[PROFILE-CARD] {qq} 头像拿不到：{why}")
        if not f.exists():
            return None
        try:
            im = Image.open(f).convert("RGB")
        except Exception:
            # 缓存坏了（0 字节/半截图）→ 删掉重下一次
            try:
                f.unlink()
            except Exception:
                pass
            ok, why = _avatar_download(qq)
            if not ok:
                if qq not in _AVATAR_WARNED:
                    _AVATAR_WARNED.add(qq)
                    logger.warning(f"[PROFILE-CARD] {qq} 头像缓存坏了，重下失败：{why}")
                return None
            im = Image.open(f).convert("RGB")
        im.thumbnail((AVATAR_PX * 2, AVATAR_PX * 2))
        if qq not in _AVATAR_OK_LOGGED:      # 只打一次，别每张卡刷一行
            _AVATAR_OK_LOGGED.add(qq)
            logger.info(f"[PROFILE-CARD] {qq} 头像就绪 "
                        f"{f.stat().st_size / 1024:.1f}KB｜{im.size}")
        return im
    except Exception as e:
        if qq not in _AVATAR_WARNED:
            _AVATAR_WARNED.add(qq)
            logger.warning(f"[PROFILE-CARD] 头像异常 {qq}: {type(e).__name__}: {e}")
        return None


def avatar_status(qq: str) -> str:
    """给 /测函数 档案 用：头像到底能不能用、为什么不能用。"""
    qq = str(qq or "").strip()
    if not qq.isdigit():
        return f"头像：{qq or '?'} 不是 QQ 号"
    if Image is None:
        return "头像：环境没有 Pillow，渲染不了"
    f = AVATAR_DIR / f"{qq}.jpg"
    if f.exists():
        age_h = (time.time() - f.stat().st_mtime) / 3600
        try:
            size = Image.open(f).size
        except Exception:
            size = "坏文件"
        return f"头像：已缓存 {f.stat().st_size / 1024:.1f}KB｜{size}｜{age_h:.1f}h 前"
    last = _AV_LAST.get(qq)
    if last:
        return f"头像：没有缓存｜上次{'成功' if last[0] else '失败'}：{last[1][:80]}"
    ok, why = _avatar_download(qq)
    return f"头像：{'刚下载成功' if ok else why}"


def _rel_text(qq: str) -> str:
    try:
        v = profiles.intimacy(qq)
        if v >= 0.05:
            return f"{profiles.intimacy_label(v)} {v:.2f}"
    except Exception:
        pass
    return ""


def _blocks(qq: str, gid: str) -> tuple:
    """返回 (head_info, [(label, [lines])...])。"""
    p = profiles.get_profile(qq)
    g = profiles.group_of(qq, gid)
    name = profiles.call_of(qq, "", gid) or str(qq)
    nick = str(p.get("nickname") or "").strip()
    tags = []
    if p.get("is_owner"):
        tags.append("主人")
    role = str(g.get("role") or "").strip()
    if role and role not in ("member", "owner"):
        tags.append(role)
    if g.get("muted") or p.get("muted"):
        tags.append("静音")

    identity = str(g.get("identity") or p.get("identity") or p.get("long_term_impression") or "").strip()
    personality = str(p.get("personality") or "").strip()
    scenario = str(p.get("scenario") or "").strip()
    note = str(g.get("note") or p.get("depth_note") or "").strip()

    try:
        notes = profiles.get_bond(qq).get("notes") or []
    except Exception:
        notes = []
    events = []
    # 档案图上的「事情」：最近 10 条（用户 2026-11-19：4 条太少）
    _ev_n = tuning.i("PROFILE_CARD_EVENTS")
    for n in notes[-_ev_n:]:
        mark = profiles.EMOTION_MARK.get(str(n.get("emotion") or ""), "")
        txt = str(n.get("text") or "").strip()
        if txt:
            events.append(f"{mark}{txt}")

    examples = [str(x or "").strip() for x in (p.get("mes_example") or []) if str(x or "").strip()]
    aliases = [str(x or "").strip() for x in (p.get("aliases") or []) if str(x or "").strip()]
    aliases = [a for a in aliases if a != qq and a != nick][:5]

    out = []
    if identity:
        out.append(("简介", [identity]))
    if personality or scenario:
        out.append(("性格", [x for x in (personality, scenario) if x]))
    if events:
        out.append(("事情", events))
    rel = _rel_text(qq)
    rel_lines = []
    if rel:
        rel_lines.append(f"你俩：{rel}")
    rel_lines.append(f"你叫他「{name}」" + (f"，他叫你「{p['bot_alias']}」" if p.get("bot_alias") else ""))
    if aliases:
        rel_lines.append("别名：" + "、".join(aliases))
    out.append(("关系", rel_lines))
    if examples:
        ex_lines = []
        for ex in examples[:2]:
            ex_lines.extend([ln.strip() for ln in str(ex).splitlines() if ln.strip()][:2])
        if ex_lines:
            out.append(("语气", ex_lines))
    if note:
        out.append(("注意", [note]))

    # 清理 emoji（字体没有字形会变方框）
    out = [(label, [_clean(x) for x in lines if _clean(x)]) for label, lines in out]
    out = [(label, lines) for label, lines in out if lines]
    head = {"name": _clean(name), "qq": str(qq), "nick": _clean(nick),
            "tags": [_clean(t) for t in tags if _clean(t)]}
    return head, out


def render_profile_image(qq: str, gid: str = "") -> str:
    """渲染档案图，返回文件路径；失败返回 ""。"""
    if Image is None:
        return ""
    qq = str(qq or "").strip()
    if not qq:
        return ""
    try:
        head, blocks = _blocks(qq, gid)
    except Exception as e:
        logger.warning(f"[PROFILE-CARD] 取档案失败 {qq}: {type(e).__name__}: {e}")
        return ""

    # 头像先取（有缓存就零成本），版本进缓存键：换了头像要重画
    avatar = _avatar_image(qq)
    try:
        _av_ver = int((AVATAR_DIR / f"{qq}.jpg").stat().st_mtime)
    except Exception:
        _av_ver = 0
    # ⚠️ 缓存键必须带「这次到底有没有贴上头像」(ok1/ok0)：
    #    以前只有 av 版本号 —— 头像一直下载失败时 av=0 恒定，那张「只有名字首字」
    #    的图会被永远复用，后面就算头像下成功了也看不见（用户 2026-11-19 踩过）。
    _av_ok = 1 if avatar is not None else 0
    sig = hashlib.md5(
        (str(head) + str(blocks) + f"{CARD_VER}|av{_av_ver}|ok{_av_ok}").encode("utf-8")
    ).hexdigest()[:10]
    fname = CARD_DIR / f"{re.sub(r'[^0-9A-Za-z_-]', '_', gid or 'private')}_{qq}_{sig}.png"
    if fname.exists():
        return str(fname)

    try:
        f_title = _font(36, bold=True)
        f_name = _font(24, bold=True)
        f_label = _font(19, bold=True)
        f_body = _font(22)
        f_small = _font(18)

        # 先量高度
        tmp = Image.new("RGB", (W, 10))
        d = ImageDraw.Draw(tmp)
        body_w = W - PAD * 2 - 26
        prepared = []
        h = PAD + 118  # header
        for label, lines in blocks:
            wrapped = []
            for ln in lines:
                wrapped.extend(_wrap(d, ln, f_body, body_w))
            bh = 12 + 30 + len(wrapped) * 32 + 10
            prepared.append((label, wrapped, bh))
            h += bh
        h += 96  # 底部留白 + footer
        H = max(320, h)

        # 背景
        img = Image.new("RGB", (W, H), BG_TOP)
        grad = Image.new("RGB", (1, 2))
        grad.putpixel((0, 0), BG_TOP)
        grad.putpixel((0, 1), BG_BOTTOM)
        img.paste(grad.resize((W, H)), (0, 0))
        draw = ImageDraw.Draw(img)

        # 卡片底
        draw.rounded_rectangle(
            (PAD - 14, PAD - 14, W - PAD + 14, H - PAD + 14),
            radius=22, fill=CARD_BG, outline=LINE, width=1,
        )

        y = PAD + 6
        # 头像圆：优先真头像，拿不到才用名字首字
        cx, cy, r = PAD + 44, y + 44, 44
        _drawn_avatar = False
        if avatar is not None:
            try:
                _side = r * 2
                _av = avatar.resize((_side, _side))
                _mask = Image.new("L", (_side, _side), 0)
                ImageDraw.Draw(_mask).ellipse((0, 0, _side, _side), fill=255)
                img.paste(_av, (cx - r, cy - r), _mask)
                draw.ellipse((cx - r, cy - r, cx + r, cy + r),
                             outline=ACCENT, width=2)
                _drawn_avatar = True
            except Exception as e:
                logger.debug(f"[PROFILE-CARD] 贴头像失败: {type(e).__name__}: {e}")
        if not _drawn_avatar:
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(30, 52, 82),
                         outline=ACCENT, width=2)
            mono = _monogram(head["name"])
            mf = _font(40, bold=True)
            mw = _tw(draw, mono, mf)
            draw.text((cx - mw / 2, cy - 28), mono, font=mf, fill=TEXT)

        # 名字 / QQ / 标签
        tx = cx + r + 22
        draw.text((tx, y + 10), head["name"], font=f_title, fill=TEXT)
        sub = f"QQ {head['qq']}"
        if head["nick"] and head["nick"] != head["name"]:
            sub += f"  ·  群名片 {head['nick']}"
        draw.text((tx, y + 58), sub, font=f_small, fill=MUTED)
        tag_x = tx
        for tag in head["tags"][:4]:
            tw = _tw(draw, tag, f_small) + 18
            draw.rounded_rectangle((tag_x, y + 84, tag_x + tw, y + 108), radius=9,
                                   fill=(28, 56, 92))
            draw.text((tag_x + 9, y + 87), tag, font=f_small, fill=ACCENT)
            tag_x += tw + 8

        y += 118
        draw.line((PAD, y, W - PAD, y), fill=LINE, width=1)
        y += 14

        for label, lines, bh in prepared:
            # 标签
            lw = _tw(draw, label, f_label) + 20
            draw.rounded_rectangle((PAD, y + 4, PAD + lw, y + 32), radius=8, fill=(26, 46, 70))
            draw.text((PAD + 10, y + 7), label, font=f_label, fill=ACCENT)
            yy = y + 42
            for ln in lines:
                draw.text((PAD + 18, yy), ln, font=f_body, fill=TEXT)
                yy += 32
            y += bh

        foot = time.strftime("小鲸鱼档案 · %Y-%m-%d %H:%M", time.localtime())
        foot += f" · {CARD_VER}"          # 图上带版本：一眼看出服务器跑的是哪一版
        draw.text((PAD, H - PAD - 8), foot, font=f_small, fill=MUTED)

        img.save(fname, format="PNG", optimize=True)
        # 只留最近 200 张，避免磁盘无限涨
        files = sorted(CARD_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[200:]:
            try:
                old.unlink()
            except Exception:
                pass
        return str(fname)
    except Exception as e:
        logger.warning(f"[PROFILE-CARD] 渲染失败 {qq}: {type(e).__name__}: {e}")
        return ""


# ================================ 词条卡片 ================================ #
def render_lore_image(scope: str, *, title: str = "本群词条", limit: int = 12) -> str:
    """
    把 lorebook 的词条列表渲染成一张卡片图（用户 2026-11-19）。

    和人物卡同一套视觉：深色卡、蓝色标签、固定宽度；超过 limit 条只画前 N 条，
    底部提示「还有 N 条，/词条 文字 看全部」。
    """
    if Image is None:
        return ""
    scope = str(scope or "").strip()
    if not scope:
        return ""
    try:
        from . import lorebook
        items = lorebook.list_entries(scope, limit=500)
    except Exception as e:
        logger.warning(f"[LORE-CARD] 取词条失败 {scope}: {type(e).__name__}: {e}")
        return ""
    if not items:
        return ""

    total = len(items)
    shown = items[:max(1, int(limit))]
    sig_src = json.dumps(
        [[e.get("id"), e.get("comment"), e.get("keys"), e.get("content"),
          e.get("position"), e.get("enabled")] for e in shown],
        ensure_ascii=False, sort_keys=True,
    ) + f"|{total}|v1"
    sig = hashlib.md5(sig_src.encode("utf-8")).hexdigest()[:10]
    fname = CARD_DIR / f"lore_{re.sub(r'[^0-9A-Za-z_-]', '_', scope)}_{sig}.png"
    if fname.exists():
        return str(fname)

    try:
        f_title = _font(36, bold=True)
        f_name = _font(24, bold=True)
        f_body = _font(22)
        f_small = _font(18)

        tmp = Image.new("RGB", (W, 10))
        d = ImageDraw.Draw(tmp)
        body_w = W - PAD * 2 - 26

        prepared = []
        h = PAD + 112
        for e in shown:
            head = _clean(e.get("comment") or "词条")
            keys = "、".join(_clean(k) for k in (e.get("keys") or [])[:6])
            content = _clean(e.get("content") or "")
            clines = _wrap(d, content, f_body, body_w)[:3]
            bh = 46 + (28 if keys else 0) + len(clines) * 30 + 16
            prepared.append((head, keys, clines, bh))
            h += bh
        h += 84
        H = max(320, h)

        img = Image.new("RGB", (W, H), BG_TOP)
        grad = Image.new("RGB", (1, 2))
        grad.putpixel((0, 0), BG_TOP)
        grad.putpixel((0, 1), BG_BOTTOM)
        img.paste(grad.resize((W, H)), (0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (PAD - 14, PAD - 14, W - PAD + 14, H - PAD + 14),
            radius=22, fill=CARD_BG, outline=LINE, width=1,
        )

        y = PAD + 8
        draw.text((PAD, y), title, font=f_title, fill=TEXT)
        draw.text((PAD, y + 46), f"共 {total} 条 · 聊到触发词才会注入",
                  font=f_small, fill=MUTED)
        y += 86
        draw.line((PAD, y, W - PAD, y), fill=LINE, width=1)
        y += 16

        for head, keys, clines, bh in prepared:
            # 标题行：评论名 + 左侧小色条
            draw.rounded_rectangle((PAD, y + 6, PAD + 6, y + 36), radius=3, fill=ACCENT)
            draw.text((PAD + 18, y + 6), head[:24], font=f_name, fill=TEXT)
            if keys:
                draw.text((PAD + 18, y + 40), "触发：" + keys, font=f_small, fill=MUTED)
                yy = y + 66
            else:
                yy = y + 44
            for ln in clines:
                draw.text((PAD + 18, yy), ln, font=f_body, fill=TEXT)
                yy += 30
            y += bh

        if total > len(shown):
            draw.text((PAD, H - PAD - 34), f"还有 {total - len(shown)} 条，/词条 文字 看全部",
                      font=f_small, fill=MUTED)
        foot = time.strftime("小鲸鱼词条 · %Y-%m-%d %H:%M", time.localtime())
        draw.text((PAD, H - PAD - 8), foot, font=f_small, fill=MUTED)

        img.save(fname, format="PNG", optimize=True)
        files = sorted(CARD_DIR.glob("lore_*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[200:]:
            try:
                old.unlink()
            except Exception:
                pass
        return str(fname)
    except Exception as e:
        logger.warning(f"[LORE-CARD] 渲染失败 {scope}: {type(e).__name__}: {e}")
        return ""


# ================================ 群聊现场卡片 ================================ #
def render_scene_image(gid: str, *, bot_qq: str = "", bot_name: str = "",
                       limit_threads: int = 3) -> str:
    """
    /测函数 现场 的图片版（用户 2026-11-19：群消息打印太长，log 会被反感）。
    和人物卡/词条卡同一套视觉：深色卡、蓝色标签、760 宽。
    """
    if Image is None:
        return ""
    gid = str(gid or "").strip()
    if not gid:
        return ""
    try:
        from . import group_scene as gs
        from . import scene_auditor as sa
    except Exception:
        return ""

    try:
        snap = gs.snapshot(gid, bot_qq=bot_qq, my_names=[bot_name] if bot_name else [])
        addr = gs.addressee_line(gid, bot_qq)
        prev = snap.get("prev") or {}
        sm = snap.get("summary") or {}
        threads = snap.get("threads") or []
    except Exception as e:
        logger.warning(f"[SCENE-CARD] 取现场失败 {gid}: {type(e).__name__}: {e}")
        return ""

    blocks = []
    if addr:
        blocks.append(("这条消息", [x.strip("· ").strip() for x in addr.splitlines() if x.strip()]))
    for i, t in enumerate(threads[:max(1, int(limit_threads))], 1):
        _p = "、".join(t.get("participants") or [])
        if len(_p) > 22:      # 别切在半个名字上，宁可加省略号
            _p = _p[:22] + "…"
        head = f"{t.get('ago')}｜{_p}｜话题：{t.get('topic') or '（不明）'}"
        lines = [head]
        # 线程 1 给足 20 句（和审核窗口一致），卡片会变高 —— 用户要的就是能看清
        # 「这条到底在接哪句」（2026-11-19：只给五六条判不出来）。
        for ln in (t.get("lines") or [])[:max(3, tuning.i("SCENE_CARD_LINES"))]:
            lines.append(str(ln).strip())
        blocks.append((f"线程{i}", lines))
    if prev:
        blocks.append(("上一句", [f"{prev.get('name')}：{prev.get('text')}"]))
    if sm.get("text"):
        blocks.append(("最近在聊", [str(sm.get("text"))]))

    # 审核：状态 + 最近 6 条硬/审对照
    audit_lines = []
    try:
        audit_lines.extend(
            [x for x in sa.status(brief=True, gid=gid).splitlines() if x.strip()][:5])
        for j, m in enumerate(gs.recent_messages(gid, 8), 1):
            who = "你" if m.get("is_bot") else str(m.get("name") or m.get("qq") or "?")
            def _cut(s, n=22):
                s = str(s or "-")
                return s if len(s) <= n else s[:n] + "…"

            def _kind(kind, name=""):
                # 「某人」看不出到底判给了谁 —— 用户 2026-11-19 就是靠这个
                # 发现「找打了」被判给了 OTAKU咩（其实是对肥鱼说的）。
                s = gs.kind_cn(kind or "?")
                if str(kind) == "person" and name:
                    s += "→" + _cut(name, 6)
                return s

            _head = f"{j}. {who}：{str(m.get('text') or '')[:18]}"
            if m.get("is_bot"):
                audit_lines.append(f"{_head}｜她自己说的")
                continue
            # ⚠️ f-string 里的表达式**不能跨行**：容器是 python 3.11，
            #    3.12 才允许（PEP 701）。写成跨行 → SyntaxError: unterminated
            #    string literal，整个档案图/现场图都渲染不出来（2026-11-19 真机踩过）。
            _hk = _kind(m.get("hard_kind") or m.get("to_kind") or "?",
                        m.get("to_name") or "")
            _hr = _cut(gs.reason_cn(m.get("hard_reason") or m.get("to_reason")), 18)
            hard = f"{_hk}/{_hr}"
            if m.get("audited"):
                aud = (f"{_kind(m.get('to_kind') or '?', m.get('to_name') or '')}/"
                       f"{_cut(gs.reason_cn(m.get('to_reason')), 18)}")
            else:
                _skip = gs.audit_skip_reason(m)
                aud = f"不审（{_cut(_skip, 16)}）" if _skip else "未审核"
            audit_lines.append(f"{_head}｜硬:{hard}｜审:{aud}")
    except Exception:
        pass
    if audit_lines:
        blocks.append(("审核", audit_lines))

    # 缓存键
    sig_src = (str(blocks) + f"|{len(threads)}|{snap.get('last_is_bot')}"
               f"|{snap.get('seeded')}|v2")
    sig = hashlib.md5(sig_src.encode("utf-8")).hexdigest()[:10]
    # JPEG 而不是 PNG：现场卡经常 760x1500，PNG 三四百 KB，
    # NapCat 上传会超时（实测 12 秒没传完就 ActionFailed，图两分钟后才出来）。
    # JPEG 85 只有它的 1/4，上传快很多，字也不会糊。
    fname = CARD_DIR / f"scene_{re.sub(r'[^0-9A-Za-z_-]', '_', gid)}_{sig}.jpg"
    if fname.exists():
        return str(fname)

    try:
        f_title = _font(36, bold=True)
        f_name = _font(22, bold=True)
        f_label = _font(19, bold=True)
        f_body = _font(21)
        f_small = _font(17)

        tmp = Image.new("RGB", (W, 10))
        d = ImageDraw.Draw(tmp)
        body_w = W - PAD * 2 - 26
        prepared = []
        h = PAD + 112
        for label, lines in blocks:
            wrapped = []
            for ln in lines:
                wrapped.extend(_wrap(d, _clean(ln), f_body, body_w))
            bh = 12 + 30 + len(wrapped) * 29 + 10
            prepared.append((label, wrapped, bh))
            h += bh
        h += 84
        H = max(320, h)

        img = Image.new("RGB", (W, H), BG_TOP)
        grad = Image.new("RGB", (1, 2))
        grad.putpixel((0, 0), BG_TOP)
        grad.putpixel((0, 1), BG_BOTTOM)
        img.paste(grad.resize((W, H)), (0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (PAD - 14, PAD - 14, W - PAD + 14, H - PAD + 14),
            radius=22, fill=CARD_BG, outline=LINE, width=1,
        )

        y = PAD + 8
        draw.text((PAD, y), "群聊现场", font=f_title, fill=TEXT)
        sub = (f"线程 {len(threads)} · 在叫你 {len(snap.get('to_bot') or [])}"
               f" · 最后是她 {'是' if snap.get('last_is_bot') else '否'}")
        if snap.get("seeded"):    # 重启后从对话日志补回来的，明说，别让人以为现场只有 1 条
            sub += f" · 日志补 {snap['seeded']} 条"
        if "#replay" in gid:      # 回放/样例数据，别让人当成真实现场
            sub += " · 回放"
        draw.text((PAD, y + 46), sub, font=f_small, fill=MUTED)
        y += 86
        draw.line((PAD, y, W - PAD, y), fill=LINE, width=1)
        y += 16

        for label, lines, bh in prepared:
            lw = _tw(draw, label, f_label) + 20
            draw.rounded_rectangle((PAD, y + 4, PAD + lw, y + 32), radius=8, fill=(26, 46, 70))
            draw.text((PAD + 10, y + 7), label, font=f_label, fill=ACCENT)
            yy = y + 42
            for ln in lines:
                draw.text((PAD + 18, yy), ln, font=f_body, fill=TEXT)
                yy += 29
            y += bh

        foot = time.strftime("小鲸鱼现场 · %Y-%m-%d %H:%M", time.localtime())
        draw.text((PAD, H - PAD - 8), foot, font=f_small, fill=MUTED)
        img.save(fname, format="JPEG", quality=85, optimize=True)
        files = sorted(CARD_DIR.glob("scene_*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[200:]:
            try:
                old.unlink()
            except Exception:
                pass
        return str(fname)
    except Exception as e:
        logger.warning(f"[SCENE-CARD] 渲染失败 {gid}: {type(e).__name__}: {e}")
        return ""
