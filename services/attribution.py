# services/attribution.py
"""写入归因核对（docs/overall-design-20261128.md §1.1）。

about_qq 必须过得去核对：① 不是她自己；② 是说话人 / 被 @ 的人 / 回复对象之一，
或本群现场名册里见过、文本里明确提到；③ 其余一律落空 —— **不猜**。
过不了就置空，标签（tags）照旧保留，检索靠标签兜底。
"""
from . import diag as _diag
from . import identity


def scene_qqs(session_key: str) -> set:
    """本群现场名册里的 QQ（group_scene.names_of 缓存，不拉网络）。"""
    key = str(session_key or "")
    gid = key[6:] if key.startswith("group_") else ""
    if not gid:
        return set()
    try:
        from . import group_scene
        mp = group_scene.names_of(gid, {}) or {}
    except Exception:
        return set()
    return {str(v) for v in mp.values() if str(v).isdigit()}


def verify(qq, *, session_key: str = "", speaker_qq: str = "",
           at_qq: str = "", text: str = "", scene: set | None = None) -> tuple:
    """核对一个 about_qq 候选，返回 (采用值, 依据)。过不了 → ("", 依据)。"""
    q = str(qq or "").strip()
    if not q:
        return "", "empty"
    if not q.isdigit():
        return "", "non_digit"
    if identity.is_self(q):
        return "", "is_bot"
    if q == str(speaker_qq or "").strip():
        return q, "speaker"
    if at_qq and q == str(at_qq):
        return q, "at"
    if q in (scene if scene is not None else scene_qqs(session_key)):
        return q, "scene"
    if q in str(text or ""):
        return q, "in_text"
    return "", "unverified"


def note(src: str, about, kept, why: str, **fields) -> None:
    """记一笔归因核对（可回放肥牛 case）。"""
    _diag.emit("attr", src=src, about=str(about or ""), kept=str(kept or ""),
               why=why, **fields)
