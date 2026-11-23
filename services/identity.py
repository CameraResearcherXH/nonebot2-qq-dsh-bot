# services/identity.py
"""她是谁 —— 一个和登录 QQ 解耦的稳定身份。

背景（用户 2026-11-23）：
    100000018 被冻结，换 100000010 之后，对话日志里她自己的 qq 变成了新号。
    记忆一旦跟着号走，封一个号就断一次 —— 但记忆是存在本地的，不该随 QQ 走。
    用户原话：「我们珍贵的记忆一切在本地，而不是随着 qq 走的。」

约定
----
· **人格 ID（self_id）**：第一次运行时取 `NAPCAT_ACCOUNT`，落盘 `data/self_id.json`，
  此后永不变。写对话日志 / 记忆 / 「发给她的」判定，全部用这个 ID。
· **accounts**：实际登录过的每个号都记进来（换号后自动追加），
  判「这是不是她自己」时**所有别名都算**，所以旧号的历史消息不会被当成陌生人。
· 换号：只改 `.env` 的 `NAPCAT_ACCOUNT` → 重启 → 新号自动进 accounts，记忆原样复用。

env
---
    SELF_ID        手动指定人格 ID（优先于落盘值；测试/迁移用）
    SELF_ALIASES   她以前登录过的号，逗号分隔（如 100000018）。
                   旧号的发言/记忆都算「她」，写在这里最稳（不依赖日志还在不在）
    SELF_ID_FILE   落盘路径（默认 data/self_id.json）
    SELF_ID_SCAN   false = 首次建文件时不扫对话日志（测试用）
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_STATE: dict | None = None
_SCANNED = False          # 本进程是否已扫过对话日志（只扫一次）


def _path() -> Path:
    return Path(os.getenv("SELF_ID_FILE", "") or "data/self_id.json")


def _env_account() -> str:
    return str(os.getenv("SELF_ID", "")
               or os.getenv("NAPCAT_ACCOUNT", "")
               or os.getenv("SELF_QQ", "") or "").strip()


def _env_aliases() -> set:
    """`.env` 里显式列出的旧号（SELF_ALIASES=100000018,123456）。"""
    raw = str(os.getenv("SELF_ALIASES", "") or "")
    out = set()
    for part in re.split(r"[,，;；\s]+", raw):
        q = part.strip()
        if q.isdigit() and 5 <= len(q) <= 12:
            out.add(q)
    return out


def _scan_bot_accounts(limit_files: int = 90) -> set:
    """从对话日志里捞出「她自己说过话」的 qq（kind=bot）。

    只读不改，用来把换号前的旧号补进 accounts —— 不然旧号的历史发言
    会被当成普通群友（建档案、算白月光、判收件人全歪）。
    """
    out: set = set()
    try:
        files = sorted(Path("data/dialogue").glob("*.jsonl"))[-limit_files:]
    except Exception:
        return out
    for fp in files:
        try:
            with fp.open("r", encoding="utf-8") as f:
                for line in f:
                    if '"kind": "bot"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    qq = str(rec.get("qq") or "").strip()
                    # 真 QQ 至少 5 位；过滤掉测试假号（999/123 这种）
                    if qq.isdigit() and 5 <= len(qq) <= 12:
                        out.add(qq)
        except Exception:
            continue
    return out


def _load() -> dict:
    global _STATE
    with _LOCK:
        if _STATE is not None:
            return _STATE
        env = _env_account()
        data: dict = {}
        fp = _path()
        try:
            if fp.exists():
                data = json.loads(fp.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.debug(f"[IDENTITY] 读 {fp} 失败: {type(e).__name__}: {e}")
            data = {}

        stored = str(data.get("self_id") or "").strip()
        self_id = stored or env
        accounts = {str(x).strip() for x in (data.get("accounts") or [])
                    if str(x).strip().isdigit() and 5 <= len(str(x).strip()) <= 12}
        if env:
            accounts.add(env)
        # 显式配置的旧号：最稳的一路（日志过期删了也还认得出「这是她以前的号」）
        accounts |= _env_aliases()
        # 每次进程启动扫一遍对话日志（只扫一次，结果并进 accounts）：
        #   · 第一次运行时靠它把「换号前的旧号」找出来；
        #   · 之后即使 self_id 已经落盘，也能补上历史里出现过的其他登录号。
        global _SCANNED
        if not _SCANNED and os.getenv("SELF_ID_SCAN", "true").lower() in (
                "1", "true", "yes", "on"):
            _SCANNED = True
            accounts |= _scan_bot_accounts()
        if self_id:
            accounts.add(self_id)

        _STATE = {"self_id": self_id, "accounts": sorted(accounts),
                  "updated_at": float(data.get("updated_at") or time.time())}
        # 没定出人格 ID 时先不落盘（测试/没配 NAPCAT_ACCOUNT 的临时进程）
        if self_id and (not data or set(data.get("accounts") or []) != accounts):
            _save(_STATE)
        if self_id:
            logger.info(f"[IDENTITY] 人格 ID = {self_id}｜登录过的号 = "
                        f"{', '.join(_STATE['accounts']) or '—'}")
        return _STATE


def _save(state: dict) -> None:
    try:
        fp = _path()
        fp.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = time.time()
        tmp = fp.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(fp)
    except Exception as e:
        logger.debug(f"[IDENTITY] 写 {_path()} 失败: {type(e).__name__}: {e}")


def reset() -> None:
    """清缓存（测试/换 env 后重读用）。"""
    global _STATE, _SCANNED
    with _LOCK:
        _STATE = None
        _SCANNED = False


# ============================== 对外 API ============================== #
def canonical(event_self_id: str = "") -> str:
    """她的人格 ID；没配 NAPCAT_ACCOUNT 时退回事件里的真实 self_id。"""
    sid = _load().get("self_id") or ""
    return str(sid or event_self_id or "")


def accounts() -> set:
    """所有人格别名（含真实登录过的号）。"""
    return set(_load().get("accounts") or [])


def is_self(qq, event_self_id: str = "") -> bool:
    """这个 QQ 是不是她自己（任何登录过的号都算）。"""
    q = str(qq or "").strip()
    if not q:
        return False
    if event_self_id and q == str(event_self_id):
        return True
    return q in accounts()


def note_account(qq) -> str:
    """登记一个真实登录号（新号第一次说话时调用），返回人格 ID。"""
    q = str(qq or "").strip()
    with _LOCK:
        st = _load()
        if q and q not in (st.get("accounts") or []):
            st["accounts"] = sorted(set(st.get("accounts") or []) | {q})
            _save(st)
            logger.info(f"[IDENTITY] 新登录号 {q} 并入人格 "
                        f"{st.get('self_id') or '—'}（记忆继续复用）")
        return str(st.get("self_id") or q)


def canon(qq) -> str:
    """别名 → 人格 ID（不是她自己的原样返回）。"""
    q = str(qq or "").strip()
    if not q:
        return ""
    return canonical() if is_self(q) else q


def info() -> dict:
    st = _load()
    return {"self_id": st.get("self_id") or "",
            "accounts": list(st.get("accounts") or []),
            "file": str(_path())}
