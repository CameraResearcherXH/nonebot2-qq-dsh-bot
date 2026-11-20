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
