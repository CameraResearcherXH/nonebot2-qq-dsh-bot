# services/style_vault.py
"""每个人的「说话语料库」+ 自定义白月光风格（用户上传 → 主人特批）。

为什么不是「问答库」：
  · 学**说话方式**要的是原话（句长、标点、口头禅、节奏），不是「问→答」；
  · 问答库适合「知识」，不适合「语气」；
  · 但用户上传的问答样本也可以直接当风格样本用 —— 原样保存，
    注入时明确「学句式，不要照抄答案」。

两层数据：
  1) corpus：自动收集每个人在群里说过的话（**省流模式也收**），
     `data/style_corpus/<gid>/<qq>.jsonl`，一行一条，默认每人保留最近 200 条；
  2) custom：用户上传的风格样本 + 审批状态（pending / approved / rejected），
     `data/style_vault.json`，只有 approved 的才会进 prompt。

和 expression / white_moon 的分工：
  · style_vault = **原话**（可完整回看，能真的模仿句长和标点）；
  · expression = LLM 蒸馏出的短句（<=24 字，学口癖，省 token）；
  · white_moon = 谁最常找谁 + 借谁的风格（总结 + 原话样本）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)

ENABLED = bool(tuning.i("STYLE_VAULT_ENABLED"))
MAX_SAMPLES = tuning.i("STYLE_VAULT_MAX_SAMPLES")
SAMPLE_CHARS = tuning.i("STYLE_VAULT_SAMPLE_CHARS")
MAX_UPLOAD_CHARS = tuning.i("STYLE_VAULT_MAX_UPLOAD_CHARS")

CORPUS_DIR = Path(os.getenv("STYLE_CORPUS_DIR", "data/style_corpus"))
CUSTOM_FILE = Path(os.getenv("STYLE_VAULT_FILE", "data/style_vault.json"))

_LOCK = threading.RLock()
_CUSTOM_CACHE = {"mtime": 0.0, "data": {}}

# 协议残留 / 非正文
_NOISE = re.compile(
    r"〔[^〕]{0,300}〕|<reply:[^>]*>|<at:[^>]*>|\[\[.*?\]\]|⭐|#\d+|"
    r"\[表情[^\]]*\]|\[图片\]|\[语音\]|\[视频\]|\[CQ:[^\]]+\]")
_SKIP_HEAD = ("【", "[", "（这一段", "⚠️", "这是你之前的回复")


def _safe(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(s or ""))[:60] or "x"


def _clean(t: str, limit: int = 0) -> str:
    s = _NOISE.sub("", str(t or ""))
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if limit > 0:
        s = s[:limit]
    return s


def _looks_like_noise(s: str) -> bool:
    s = str(s or "").strip()
    if len(s) < 2:
        return True
    if s.startswith(_SKIP_HEAD):
        return True
    # 指令 / 命令不是说话风格
    if s.startswith(("/", "!", "！")):
        return True
    # 纯符号 / 纯数字 / 纯链接
    if re.fullmatch(r"[\W_]+", s):
        return True
    if re.fullmatch(r"https?://\S+", s):
        return True
    return False


# ============================ ① 自动语料库 ============================
def _corpus_path(gid: str, qq: str) -> Path:
    return CORPUS_DIR / _safe(gid) / f"{_safe(qq)}.jsonl"


def _read_corpus(path: Path) -> list:
    rows = []
    try:
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict) and str(r.get("t") or "").strip():
                    rows.append(r)
    except Exception:
        return []
    return rows


def _compact(path: Path, rows: list) -> None:
    """只留最近 MAX_SAMPLES 条（按时间），并重写。"""
    try:
        rows = sorted(rows, key=lambda r: float(r.get("at") or 0))[-MAX_SAMPLES:]
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(path)
    except Exception as e:
        logger.debug(f"[STYLE] 压缩失败 {path}: {type(e).__name__}: {e}")


def note_sample(gid: str, qq: str, text: str, name: str = "") -> bool:
    """记一条这个人的原话（省流模式也会走到这里）。失败静默。"""
    if not ENABLED:
        return False
    gid, qq = str(gid or "").strip(), str(qq or "").strip()
    if not gid or not qq:
        return False
    s = _clean(text, SAMPLE_CHARS)
    if _looks_like_noise(s):
        return False
    path = _corpus_path(gid, qq)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"t": s, "at": round(time.time(), 1), "n": str(name or "")[:20]}
        with _LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            # 文件太大才压缩，避免每条消息都全量读写
            try:
                if path.stat().st_size > max(200_000, MAX_SAMPLES * 300):
                    _compact(path, _read_corpus(path))
            except Exception:
                pass
        return True
    except Exception as e:
        logger.debug(f"[STYLE] 记样本失败 {gid}/{qq}: {type(e).__name__}: {e}")
        return False


def samples(gid: str, qq: str, limit: int = 3) -> list:
    """取这个人最近的原话（去重、去掉协议残留），最新的在前。"""
    if not ENABLED:
        return []
    rows = _read_corpus(_corpus_path(gid, qq))
    seen, out = set(), []
    for r in reversed(rows):
        t = str(r.get("t") or "").strip()
        if not t or t in seen or _looks_like_noise(t):
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max(1, int(limit or 3)):
            break
    return out


def corpus_count(gid: str, qq: str) -> int:
    return len(_read_corpus(_corpus_path(gid, qq)))


# ============================ ② 自定义风格（上传 / 特批） ============================
def _empty_custom() -> dict:
    return {"groups": {}}


def _load_custom() -> dict:
    try:
        st = CUSTOM_FILE.stat().st_mtime
    except Exception:
        return _empty_custom()
    if st == _CUSTOM_CACHE.get("mtime"):
        return _CUSTOM_CACHE.get("data") or _empty_custom()
    try:
        data = json.loads(CUSTOM_FILE.read_text(encoding="utf-8") or "{}")
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("groups", {})
    _CUSTOM_CACHE["mtime"] = st
    _CUSTOM_CACHE["data"] = data
    return data


def _save_custom(data: dict) -> None:
    try:
        CUSTOM_FILE.parent.mkdir(parents=True, exist_ok=True)
        CUSTOM_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            _CUSTOM_CACHE["mtime"] = CUSTOM_FILE.stat().st_mtime
        except Exception:
            _CUSTOM_CACHE["mtime"] = time.time()
        _CUSTOM_CACHE["data"] = data
    except Exception as e:
        logger.debug(f"[STYLE] 写自定义风格失败: {type(e).__name__}: {e}")


def _group(data: dict, gid: str) -> dict:
    g = (data.setdefault("groups", {})).setdefault(str(gid), {})
    g.setdefault("custom", {})
    return g


def submit(gid: str, qq: str, text: str, by_qq: str, name: str = "") -> dict:
    """用户上传一套说话样本 → 存成 pending，等主人特批。"""
    s = _clean(text, 0)
    if len(s) < 10:
        raise ValueError("样本太短（至少 10 个字）")
    if len(s) > MAX_UPLOAD_CHARS:
        s = s[:MAX_UPLOAD_CHARS]
    rec = {
        "text": s,
        "status": "pending",
        "by": str(by_qq or ""),
        "name": str(name or "")[:20],
        "at": time.time(),
    }
    with _LOCK:
        data = _load_custom()
        _group(data, gid)["custom"][str(qq)] = rec
        _save_custom(data)
    logger.info(f"[STYLE] {gid}/{qq} 收到自定义风格样本 {len(s)} 字（by {by_qq}）")
    return rec


def approve(gid: str, qq: str, owner_qq: str) -> dict:
    with _LOCK:
        data = _load_custom()
        rec = ((_group(data, gid).get("custom") or {}).get(str(qq)) or {})
        if not rec:
            return {}
        rec["status"] = "approved"
        rec["approved_by"] = str(owner_qq or "")
        rec["approved_at"] = time.time()
        _save_custom(data)
    logger.info(f"[STYLE] {gid}/{qq} 自定义风格已特批（by {owner_qq}）")
    return rec


def reject(gid: str, qq: str, owner_qq: str) -> dict:
    with _LOCK:
        data = _load_custom()
        rec = ((_group(data, gid).get("custom") or {}).get(str(qq)) or {})
        if not rec:
            return {}
        rec["status"] = "rejected"
        rec["approved_by"] = str(owner_qq or "")
        rec["approved_at"] = time.time()
        _save_custom(data)
    return rec


def custom_record(gid: str, qq: str) -> dict:
    data = _load_custom()
    return dict((((data.get("groups") or {}).get(str(gid)) or {})
                 .get("custom") or {}).get(str(qq)) or {})


def custom_style(gid: str, qq: str) -> str:
    """只有 approved 的才返回；pending/rejected 一律当没有。"""
    rec = custom_record(gid, qq)
    if str(rec.get("status") or "") != "approved":
        return ""
    return str(rec.get("text") or "").strip()


def pending(gid: str) -> list:
    g = ((_load_custom().get("groups") or {}).get(str(gid)) or {})
    rows = []
    for qq, rec in (g.get("custom") or {}).items():
        if str((rec or {}).get("status") or "") == "pending":
            rows.append({"qq": str(qq), **(rec or {})})
    rows.sort(key=lambda r: -float(r.get("at") or 0))
    return rows


def status_text(gid: str, qq: str = "") -> str:
    lines = [f"🎭 说话样本库：{'开' if ENABLED else '关'}"
             f"｜每人保留最近 {MAX_SAMPLES} 条"]
    if qq:
        rec = custom_record(gid, qq)
        lines.append(f"· {qq}：自动语料 {corpus_count(gid, qq)} 条")
        if rec:
            st = str(rec.get("status") or "")
            label = {"pending": "待主人特批", "approved": "已特批生效",
                     "rejected": "已驳回"}.get(st, st)
            lines.append(f"· 自定义样本：{label}（{len(str(rec.get('text') or ''))} 字，"
                         f"上传者 {rec.get('by') or '?'}）")
        else:
            lines.append("· 自定义样本：没有")
        ex = samples(gid, qq, 3)
        if ex:
            lines.append("· 最近原话：" + " / ".join(f"「{x}」" for x in ex))
        return "\n".join(lines)
    p = pending(gid)
    if p:
        lines.append(f"· 待特批 {len(p)} 个（批准前先看预览，别放过指令类内容）：")
        for r in p[:10]:
            _txt = re.sub(r"\s+", " ", str(r.get("text") or "")).strip()
            lines.append(f"    {r.get('name') or r['qq']}（{r['qq']}）"
                         f" {len(_txt)} 字：{_txt[:100]}…")
    else:
        lines.append("· 待特批：没有")
    return "\n".join(lines)
