import html
import re

_HAN = re.compile(r"[\u4e00-\u9fff]")

# <br> / <br/> / </br> → 换行
_BR_TAG = re.compile(r"<\s*/?\s*br\s*/?\s*>", re.I)
# 其余常见 HTML 标签 → 空格（白名单式，不误伤正文里的 < 和 >）
_HTML_TAG = re.compile(
    r"<\s*/?\s*(?:p|div|span|li|ul|ol|h[1-6]|b|i|u|s|strong|em|a|img|hr|"
    r"table|thead|tbody|tr|td|th|pre|code|blockquote|font|sup|sub|"
    r"small|big|center|section|article|header|footer|nav|figure|figcaption)\b[^<>]{0,300}>",
    re.I,
)


def _char_w(ch: str) -> float:
    """显示宽度权重：汉字/全角 = 1，ASCII/半角 = 0.5，emoji ≈ 1。

    2026-11-20 审计 G5：以前只数汉字，一条 500 字符的英文回复
    hanzi_count=0 → 既不截断也不走合并转发。改成加权计数后，
    「20 字」对中英混排仍然成立，纯英文也不会无限长。
    """
    if _HAN.match(ch):
        return 1.0
    o = ord(ch)
    # CJK 标点 / 全角字符
    if 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF:
        return 1.0
    # emoji 及补充平面
    if o >= 0x1F000:
        return 1.0
    return 0.5


def hanzi_count(text: str) -> float:
    """按显示宽度计数（汉字 1，半角 0.5），跟 limit_hanzi 的口径一致。"""
    return sum(_char_w(ch) for ch in str(text or ""))


# 允许断句的标点（截断时优先切在这些字符后面）
_SOFT_BREAK = "，。！？；：、,.!?;:~～…—）)」』"


def limit_hanzi(text: str, limit: int) -> str:
    """
    按汉字数截断。

    ⚠️ 以前是硬切 + 补「…」，经常把话切在句子中间
    （「那你要不要试试这…」）。现在先往回找最近的标点断句，
    实在找不到标点才补省略号 —— 群聊 20 字限制下，断句是否自然很影响观感。
    """
    if limit <= 0:
        return text
    count = 0.0
    out = []
    for ch in text:
        out.append(ch)
        count += _char_w(ch)
        if count >= limit:
            break
    result = "".join(out)
    if count >= limit and len(result) < len(text):
        # 只在最后 10 个字符里找断点，避免为了断句砍掉太多内容
        tail = max(0, len(result) - 10)
        cut = -1
        for i in range(len(result) - 1, tail - 1, -1):
            if result[i] in _SOFT_BREAK:
                cut = i
                break
        if cut > 0:
            result = result[:cut + 1]
        else:
            result = result.rstrip() + "…"
    return result


def strip_html(text: str) -> str:
    """
    清掉网页/搜索结果的 HTML 标签和实体。

    为什么需要：Tavily 的 answer 字段和网页 snippet 里经常带 <br>、<b> 之类，
    原样塞进 prompt，模型会照着抄进回复 —— 真机出现过一条回复里
    每句话后面都跟一个 <br>，用户直接看到标签。

    顺序很重要：先删真标签，再反转义实体。
    这样「正文里本来就有的 &lt;」反转义成 < 后不会再被当标签删掉。
    """
    if not text:
        return ""
    t = str(text)
    t = _BR_TAG.sub("\n", t)
    t = _HTML_TAG.sub(" ", t)
    t = html.unescape(t)
    # 反转义后可能又冒出 <br>（双重转义的搜索结果），再清一次
    t = _BR_TAG.sub("\n", t)
    t = t.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()

# ============================================================
# 昵称清洗 / 渲染（2026-11-27）
# ============================================================
# 零宽 / 格式字符：QQ 昵称里常见（U+2067、U+2069、U+115F…），人看着一样、模型容易串
_ZW_CHARS = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff\u115f\u1160\u3164\uffa0]")
# 人称代词：昵称里带这些字，直接写成 `名字 → 大家: 内容` 会被模型当成句子
_PRONOUN = re.compile(r"(你|我|他|她|咱|大家|我们|你们|他们)")


def clean_name(name) -> str:
    """去掉昵称里的零宽/格式字符，压缩空白。正常中文与 emoji 不动。"""
    s = _ZW_CHARS.sub("", str(name or ""))
    return re.sub(r"\s+", " ", s).strip()


def render_name(name) -> str:
    """渲染发言者名字：含人称代词的昵称加「」，避免被当成句子。

    2026-11-27 上帝视角复盘（docs/reply-audit-20261127.md）：
      群友昵称 `你已被管理员下个路口见喵禁言6小时`、`我的床会长猫`、
      `请给我很多很多token` —— 直接渲染成 `你已被管理员… → 大家: 哪有`，
      模型会把开头的「你」当成被指代的人，人称直接错。
    只给**长度 ≥3 且含代词**的名字加引号（`你` / `大家` 这种正常词不包）。
    """
    n = clean_name(name)
    if not n:
        return ""
    if len(n) >= 3 and _PRONOUN.search(n):
        return f"「{n}」"
    return n
