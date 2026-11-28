# services/person_facts.py
"""人物查询的事实行注入（docs/overall-design-20261128.md §1.2）。

「X是谁 / 把X@出来 / X的档案」这类问句，先用**本群现场名册 + 档案别名**硬查；
查到就注入事实行（权威措辞），查不到就注入「没找到」—— 防编造。
档案是检索的第二道墙，不是替代。
"""
import json
import re

from . import diag

_PATTERNS = (
    re.compile(r"([^\s，。,.!?！？、：:\"“”]{1,10})(?:是谁|是哪位|是哪个|是谁啊|是谁呀|是啥人)"),
    re.compile(r"(?:把|叫|让|@)([^\s，。,.!?！？、：:\"“”]{1,10})(?:@?出来|出来一下|过来)"),
    re.compile(r"(?:查一下|看看|搜一下)([^\s，。,.!?！？、：:\"“”]{1,10})(?:是谁|的档案|的资料)"),
)
_SKIP = {"你", "我", "他", "她", "它", "大家", "谁", "这个", "那个", "什么",
         "这", "那", "哪", "哪位", "你们", "我们", "他们", "她们", "有人"}


def candidate(text: str) -> str:
    """从一句问话里取「被问的人」。取不到返回空串。"""
    t = str(text or "").strip()
    if not t or len(t) > 120:
        return ""
    for rgx in _PATTERNS:
        m = rgx.search(t)
        if not m:
            continue
        nm = m.group(1).strip("「」『』“”\"'：:@ ")
        if nm and nm not in _SKIP and len(nm) <= 10:
            return nm
    return ""


def _profile_by_name(name: str, gid: str = "") -> str:
    """档案里按昵称/称呼/别名找人；只有**唯一**命中才返回 QQ。"""
    from . import profiles
    nm = str(name or "").strip()
    if not nm:
        return ""
    hits = set()
    for f in profiles.PROFILE_DIR.glob("*_profile.json"):
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        qq = str(p.get("qq") or f.name.split("_")[0])
        if not qq.isdigit():
            continue
        cand = [str(p.get("nickname") or ""), str(p.get("call_name") or "")]
        cand += [str(a) for a in (p.get("aliases") or [])]
        g = (p.get("groups") or {}).get(str(gid)) if gid else None
        if isinstance(g, dict):
            cand.append(str(g.get("call_name") or ""))
        if nm in cand:
            hits.add(qq)
    return hits.pop() if len(hits) == 1 else ""


def _resolve(name: str, gid: str = "") -> tuple:
    """名字 → (QQ, 依据)。名册精确 > 名册宽松 > 档案唯一命中。"""
    try:
        from . import group_scene
        mp = group_scene.names_of(str(gid or ""), {}) or {}
    except Exception:
        mp = {}
    qq = str(mp.get(name) or "")
    if qq.isdigit():
        return qq, "scene"
    for k, v in mp.items():
        if str(v).isdigit() and (name in str(k) or str(k) in name):
            return str(v), "scene_lax"
    qq = _profile_by_name(name, gid)
    return (qq, "profile") if qq else ("", "")


def render(text: str, *, gid: str = "", speaker_qq: str = "") -> str:
    """给流水线的「人物事实」块；不是人物问句就返回空串。"""
    nm = candidate(text)
    if not nm:
        return ""
    qq, how = _resolve(nm, gid)
    diag.emit("person", q=text, name=nm, qq=qq, hit=bool(qq),
              src=how, speaker=speaker_qq, gid=gid)
    if not qq:
        return (f"· 本群名册和档案里都**没有**「{nm}」这个人。"
                f"直接说没找到/不认识，**不要编**。")
    from . import profiles
    try:
        p = profiles.get_profile(qq)
    except Exception:
        p = {}
    lines = [f"· 「{nm}」= QQ {qq}（本群叫「{p.get('nickname') or nm}」）"]
    call = ""
    try:
        call = profiles.call_of(qq, "", gid) or str(p.get("call_name") or "")
    except Exception:
        pass
    if call:
        lines.append(f"· 你平时叫他：{call}")
    for k, label in (("identity", "他是谁"), ("personality", "性格"),
                     ("depth_note", "相处注意")):
        v = str(p.get(k) or "").strip()
        if v:
            lines.append(f"· {label}：{v[:60]}")
    try:
        from . import memory_v2
        for h in memory_v2.recent_about(qq, gid=gid, limit=2) or []:
            lines.append(f"· 关于他的记忆（{h.get('ago') or ''}）："
                         f"{str(h.get('summary') or '')[:40]}")
    except Exception:
        pass
    lines.append("· 回答「是谁 / 在哪 / 叫什么」先以这几行为准；"
                 "档案里没有的就说不知道，别猜。")
    return "\n".join(lines)
