# services/segments.py
"""群聊「分段」：等一段说完再回，而不是一条消息一回。

用户 2026-11-22：
  「我们确实一条一条回消息队列很阻塞，特别是高频率群提问的时候。
   拿这块策略：等一段、看完整段再回。包括三方判心情、判断收发件人，
   也不用固定 20 条，可以按这样一段。」

设计：
  · 每条群消息先进本模块的 buffer（不阻塞）；
  · worker 被触发后，等「安静 SEGMENT_WAIT_SEC 秒」或「攒满 SEGMENT_MAX_SEC 秒」，
    把这一整段一起取走；
  · 这一段同时用于：回复、心情判读、收发件人判断、上下文；
  · 处理过的段单独留一份 `processed`，给「翻历史」和下一轮上下文用，
    不再把 NapCat 原生消息直接塞给模型。
"""
from core import tuning
import time

WAIT_SEC = tuning.f("SEGMENT_WAIT_SEC")
MAX_SEC = tuning.f("SEGMENT_MAX_SEC")
MAX_MSGS = tuning.i("SEGMENT_MAX_MSGS")
HISTORY_KEEP = tuning.i("SEGMENT_HISTORY_KEEP")

_BUF: dict = {}
_PROCESSED: dict = {}


def _key(session_key: str) -> str:
    return str(session_key or "")


# 群聊分段默认把每条消息压到 220 字（防止刷屏把 prompt 撑爆）。
# 但带网页正文的 `【链接内容】` 不能压：压完只剩一个「A」，模型根本看不到题目。
_LINK_MARK = "【链接内容"
_SEG_TEXT_MAX = 600
_SEG_LINK_MAX = 5000


def _clip_text(t, limit: int = _SEG_TEXT_MAX) -> str:
    s = str(t or "")
    return s[:_SEG_LINK_MAX] if _LINK_MARK in s else s[:limit]


def push(session_key: str, ev, text: str, *, name: str = "", uid: str = "",
         mentioned: bool = False, reply_id: str = "", ts: float = 0,
         gid: str = "", mid: str = "", to_kind: str = "", to_name: str = "",
         to_qq: str = "", to_conf: float = 0.0) -> None:
    """记一条群消息（不触发回复，只进当前段）。

    to_kind / to_name / to_qq / to_conf：这条消息**是发给谁的**
    （bot / person / group / unknown，来自 group_scene 的收发件人判断）。
    用户 2026-11-22：「三方 LLM 判心情、以及最终给她的上下文分段，
    都没用上收发件人标签」——所以这一段必须带着「谁 → 谁」。
    """
    key = _key(session_key)
    if not key:
        return
    t = str(text or "").strip()
    # 指令不进聊天段（它们由命令处理器单独处理，混进来会让模型回错东西）
    if t.startswith(("/", "／")):
        return
    if not t and not ev:
        return
    rec = {
        "ev": ev,
        "text": _clip_text(t),
        "name": str(name or ""),
        "uid": str(uid or ""),
        "mentioned": bool(mentioned),
        "reply_id": str(reply_id or ""),
        "ts": float(ts or time.time()),
        "gid": str(gid or ""),
        "mid": str(mid or ""),
        "to_kind": str(to_kind or ""),
        "to_name": str(to_name or ""),
        "to_qq": str(to_qq or ""),
        "to_conf": float(to_conf or 0.0),
    }
    buf = _BUF.setdefault(key, [])
    buf.append(rec)
    if len(buf) > max(1, MAX_MSGS * 2):
        del buf[: len(buf) - MAX_MSGS * 2]


def set_addressee(session_key: str, mid: str, *, to_kind: str = "",
                  to_name: str = "", to_qq: str = "",
                  to_conf: float = 0.0) -> bool:
    """补写一条记录的收发件人。

    为什么要补：分段在 `_early_observe`（priority=1）就入 buffer 了，
    而「谁 → 谁」是后面 `_need_reply` 里 `group_scene.observe()` 才算出来的。
    以前这里没回填 → 分段/心情判读都看不到收发件人（用户 2026-11-22）。
    """
    key = _key(session_key)
    mid = str(mid or "").strip()
    if not key or not mid:
        return False
    for r in reversed(_BUF.get(key) or []):
        if str(r.get("mid") or "") == mid:
            r["to_kind"] = str(to_kind or "")
            r["to_name"] = str(to_name or "")
            r["to_qq"] = str(to_qq or "")
            try:
                r["to_conf"] = float(to_conf or 0.0)
            except Exception:
                r["to_conf"] = 0.0
            return True
    return False


def current(session_key: str) -> list:
    return list(_BUF.get(_key(session_key)) or [])


def take(session_key: str) -> list:
    key = _key(session_key)
    buf = _BUF.pop(key, None) or []
    return list(buf)


def size(session_key: str) -> int:
    return len(_BUF.get(_key(session_key)) or [])


async def wait_close(session_key: str, *, wait_sec: float = 0,
                     max_sec: float = 0, max_msgs: int = 0,
                     poll: float = 0.25) -> list:
    """等这一段说完，返回并清空这一段的记录。

    关闭条件（任一）：
      · 距离最后一条新消息 >= wait_sec（默认 6s）
      · 从第一条约起 >= max_sec（默认 30s）
      · 条数 >= max_msgs（默认 40）
    """
    import asyncio

    key = _key(session_key)
    wait_sec = float(wait_sec or WAIT_SEC)
    max_sec = float(max_sec or MAX_SEC)
    max_msgs = int(max_msgs or MAX_MSGS)
    start = time.monotonic()
    last_len = size(key)
    last_change = time.monotonic()
    while True:
        n = size(key)
        if n != last_len:
            last_len = n
            last_change = time.monotonic()
        if n <= 0:
            return []
        now = time.monotonic()
        if (now - last_change >= wait_sec
                or now - start >= max_sec
                or n >= max_msgs):
            return take(key)
        await asyncio.sleep(max(0.05, poll))


def is_addressed(r: dict) -> bool:
    """这条是不是在叫她（@ 了她 / 喊了她名字）。"""
    if (r or {}).get("mentioned"):
        return True
    return str((r or {}).get("to_kind") or "").strip().lower() == "bot"


def _pick_rows(records: list, limit: int) -> list:
    """截断时**绝不能把「→ 你」的条目截掉**（2026-11-23 真机）。

    事故：一段 5 条，第 4 条是「老大 @她：我的今日小猪呢，刚刚怎么不理我」，
    第 5 条是别人问她「你是啥子」。模型按「默认回最后一条」只答了第 5 条，
    @ 她的那条被整段回复漏掉 → 群友以为她装死。
    所以：被 @ / 点名的条目无论在第几位都保留；其余按最近 limit 条补足。
    """
    rows = list(records or [])
    limit = max(1, int(limit or 20))
    if len(rows) <= limit:
        return rows
    keep = {i for i, r in enumerate(rows) if is_addressed(r)}
    if len(keep) >= limit:
        # @ 的比窗口还多：宁可超一点也不能漏
        return [rows[i] for i in sorted(keep)]
    room = limit - len(keep)
    rest = [i for i in range(len(rows)) if i not in keep]
    keep |= set(rest[-room:])
    return [rows[i] for i in sorted(keep)]


def addressee_of(r: dict) -> str:
    """把一条记录的收发件人渲染成「→ 你 / → 某人 / → 大家」。

    只有置信度够（>=0.5）才敢写成具体的人，否则写「大概某人」/「大家」，
    免得把「其实在跟别人说话」误判成在跟她说话。
    """
    kind = str((r or {}).get("to_kind") or "").strip().lower()
    name = str((r or {}).get("to_name") or "").strip()
    try:
        conf = float((r or {}).get("to_conf") or 0.0)
    except Exception:
        conf = 0.0
    if (r or {}).get("mentioned"):
        return "→ 你"
    if kind == "bot":
        return "→ 你"
    if kind == "person" and name:
        return f"→ {name}" if conf >= 0.5 else f"→ 大概{name}"
    if kind == "unknown" and name:
        return f"→ {name}?"
    return "→ 大家"


def format(records: list, *, limit: int = 20) -> str:
    """把一段记录渲染成给模型看的文本（编号 + 谁 → 谁 + 内容）。

    ⚠️ 用户 2026-11-23：「艾特也不回复」——真机 trace 里一段 5 条，
    第 4 条是 @ 她问事、第 5 条是别人问别的，她只回了第 5 条。
    所以这里做三件事：
      · 被 @ / 点名的行前面打 ⭐，并在段末单独列「必须回应」；
      · 截断时保留 ⭐ 行（见 `_pick_rows`），不让它被挤出窗口；
      · 明确写「每条 ⭐ 都要回应」，不再说「默认只回最后一条」。
    """
    rows = _pick_rows(records, limit)
    if not rows:
        return ""
    lines = []
    addressed = []
    for i, r in enumerate(rows, 1):
        name = str(r.get("name") or r.get("uid") or "?")
        uid = str(r.get("uid") or "")
        who = f"{name}({uid})" if uid and uid != name else name
        _mid = str(r.get("mid") or "")
        _tag = f"#{_mid} " if _mid else ""
        _txt = str(r.get("text") or "（图片/表情）")
        _txt = _txt[:_SEG_LINK_MAX] if _LINK_MARK in _txt else _txt[:220]
        _star = "⭐" if is_addressed(r) else ""
        lines.append(f"[{i}] {_star}{_tag}{who} {addressee_of(r)}：{_txt}")
        if _star:
            addressed.append(f"· #{_mid or '?'} {name}：{_txt[:120]}")
    _last = rows[-1]
    _last_who = str(_last.get("name") or _last.get("uid") or "?")
    _last_star = is_addressed(_last)
    _omitted = max(0, len(list(records or [])) - len(rows))
    _omit_note = (f"，更早的 {_omitted} 条被略（看不懂就 `[[[HISTORY|20]]]` 往上翻）"
                  if _omitted else "")
    head = (
        "（这一段群聊共 %d 条%s，按时间顺序，最后一条最新。"
        "带 ⭐ / 「→ 你」的才是说给你的；「→ 某人 / → 大家」是别人之间的对话，别抢着接。\n"
        "⚠️ **这一段里每一条 ⭐ 都必须回应，但用一条消息全部答完**："
        "「你问的X…；你问的Y…」这样一句话覆盖，**不要一人一条连发**"
        "（群里每分钟有发送上限，连发会被限流/吞掉）；最多 2 条气泡，"
        "只引用最主要的那条（例如 `<reply:%s>`）。\n"
        "带「〔引用 …〕」的是被引用的旧消息，只作指代，"
        "**不要在回复里复述/朗读引用内容**（文字、图片都一样）。\n"
        "不写 `<reply:消息号>` 就默认回最后一条%s（%s）。）\n"
        % (len(rows), _omit_note, str(_last.get("mid") or ""),
           "⭐" if _last_star else "", _last_who)
    )
    tail = ""
    if addressed:
        tail = ("\n\n【必须回应（这一段里在叫你的）】\n"
                + "\n".join(addressed)
                + "\n（上面每一条都要照顾到；答不了就直说「这个我还真不知道」，"
                  "别装没看见。）")
    return head + "\n".join(lines) + tail


def short(records: list, *, n: int = 3, width: int = 80) -> str:
    """压成一句给心情判读/日志用的短摘要。"""
    rows = _pick_rows(records, n)
    return "；".join(
        f"{'⭐' if is_addressed(r) else ''}"
        f"{str(r.get('name') or r.get('uid') or '?')} {addressee_of(r)}："
        f"{str(r.get('text') or '')[:width]}"
        for r in rows if str(r.get("text") or "").strip())


def add_processed(session_key: str, text: str, *, mood: str = "",
                  value: float = 0.0, addressee: str = "") -> None:
    """把处理过的一段存下来（下一轮上下文 / 翻历史用）。"""
    key = _key(session_key)
    t = str(text or "").strip()
    if not key or not t:
        return
    lst = _PROCESSED.setdefault(key, [])
    lst.append({
        "at": time.time(),
        "text": t[:4000],
        "mood": str(mood or "")[:20],
        "value": round(float(value or 0.0), 3),
        "addressee": str(addressee or "")[:60],
    })
    if len(lst) > max(1, HISTORY_KEEP):
        del lst[: len(lst) - HISTORY_KEEP]


def recent_processed(session_key: str, n: int = 5) -> list:
    return list((_PROCESSED.get(_key(session_key)) or [])[-max(1, int(n)):])


def processed_text(session_key: str, n: int = 5) -> str:
    rows = recent_processed(session_key, n)
    if not rows:
        return ""
    out = []
    for r in rows:
        t = time.strftime("%H:%M", time.localtime(float(r.get("at") or 0)))
        mood = f"｜心情 {r['mood']}（{r['value']:+.2f}）" if r.get("mood") else ""
        out.append(f"[{t}]{mood}\n{r.get('text')}")
    return "\n\n".join(out)
