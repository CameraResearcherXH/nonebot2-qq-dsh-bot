# services/harness.py
"""QQ ⇄ DeepSeek Harness（DSH）会话桥。

2026-11-24 用户需求：
    「用户反馈那个自己写代码的不好用，想加个 aurabot 里的那种 deepseek harness
      回话链接 QQ 会话……可以直接 QQ 或者 harness 聊天直接修改代码。」

这个模块只做**桥**：
    QQ 消息（主人） → 组装上下文（人格 / 心情 / 好感 / 记忆） → 调 DSH → 结果回 QQ

DSH 的调用方式做成可配置的两条后端（因为我们不绑定某个 DSH 版本）：
    HARNESS_BACKEND=cli   —— 跑一个本地命令（默认）
    HARNESS_BACKEND=http  —— POST 到一个 HTTP 接口

配置（`.env`）：
    CODE_AGENT=harness        打开桥，同时关掉 self_tool
    HARNESS_BACKEND=cli
    HARNESS_CLI=dsh run --cwd {cwd} --session {session} --prompt {prompt}
    HARNESS_URL=http://127.0.0.1:8787/api/chat
    HARNESS_TOKEN=
    HARNESS_CWD=.             工作目录（相对项目根或绝对路径）
    HARNESS_CONTEXT=1         是否带人格/心情/好感/记忆
    HARNESS_OWNER_ONLY=1      只有主人能用
    HARNESS_TIMEOUT=300       单次最长秒数
    HARNESS_MAX_OUTPUT=3000   回给 QQ 的最大字数

安全：
  · 只有主人能触发（`HARNESS_OWNER_ONLY=1`）；
  · 输出会 redact 掉 .env 里的密钥值；
  · 超时 kill 子进程；输出截断。
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
import uuid
from pathlib import Path

import httpx

from core import tuning

try:
    from nonebot import logger
except Exception:  # pragma: no cover
    import logging
    logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = Path(os.getenv("HARNESS_STATE", "data/harness.json"))


def _env(key: str, default: str = "") -> str:
    return str(os.getenv(key, default) or "").strip()


def _cwd() -> Path:
    raw = _env("HARNESS_CWD", ".")
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / p
    try:
        return p.resolve()
    except Exception:
        return ROOT


def owner_only() -> bool:
    """HARNESS_OWNER_ONLY（默认 1）：关掉就允许所有人用（不推荐，能改代码）。"""
    return _env("HARNESS_OWNER_ONLY", "1").lower() not in ("0", "false", "off", "no")


def _timeout() -> float:
    return float(tuning.f("HARNESS_TIMEOUT") or 300.0)


def _max_output() -> int:
    return int(tuning.i("HARNESS_MAX_OUTPUT") or 3000)


def _load() -> dict:
    try:
        if STATE_FILE.exists():
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _save(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as e:
        logger.debug(f"[HARNESS] 保存会话失败: {type(e).__name__}: {e}")


def session_of(key: str) -> dict:
    d = _load()
    s = (d.get("sessions") or {}).get(str(key))
    if not isinstance(s, dict):
        s = {"id": f"qq-{uuid.uuid4().hex[:12]}", "created": time.time(), "cwd": str(_cwd())}
        d.setdefault("sessions", {})[str(key)] = s
        _save(d)
    s.setdefault("cwd", str(_cwd()))
    return s


def reset(key: str) -> dict:
    d = _load()
    s = {"id": f"qq-{uuid.uuid4().hex[:12]}", "created": time.time(), "cwd": str(_cwd())}
    d.setdefault("sessions", {})[str(key)] = s
    _save(d)
    return s


def _secret_values() -> list:
    out = []
    for k, v in os.environ.items():
        if not v or len(v) < 8:
            continue
        if any(t in k.upper() for t in ("KEY", "TOKEN", "SECRET", "COOKIE", "PASS")):
            out.append(v)
    return sorted(set(out), key=len, reverse=True)


def redact(text: str) -> str:
    out = str(text or "")
    for v in _secret_values():
        if v in out:
            out = out.replace(v, "[REDACTED]")
    return out


def build_context(session_key: str, speaker_qq: str = "", speaker_name: str = "",
                  gid: str = "") -> str:
    """给 harness 的一小段背景：人格 / 心情 / 好感 / 白月光 / 相关记忆。"""
    if _env("HARNESS_CONTEXT", "1").lower() in ("0", "false", "off", "no"):
        return ""
    lines = []
    try:
        from . import persona, mood, profiles
        nm = persona.persona_name(session_key)
        if nm:
            lines.append(f"【角色】{nm}")
        try:
            m = mood.get_mood(session_key) or {}
            if m.get("label"):
                lines.append(f"【当前心情】{m['label']}")
        except Exception:
            pass
        if speaker_qq:
            try:
                aw = profiles.get_bond_word(speaker_qq)
                if aw:
                    lines.append(f"【你对{speaker_name or speaker_qq}的好感】{aw}")
            except Exception:
                pass
        if gid:
            try:
                from . import white_moon
                wm = white_moon.render(gid, speaker_qq, speaker_name)
                if wm:
                    lines.append(wm.strip()[:300])
            except Exception:
                pass
        if speaker_qq:
            try:
                from . import memory_v2
                hits = memory_v2.recall("代码 需求 修改", limit=3,
                                        speaker_qq=speaker_qq, gid=gid)
                if hits:
                    lines.append("【相关记忆】" + "；".join(
                        str(h.get("summary") or "")[:60] for h in hits if h.get("summary")))
            except Exception:
                pass
    except Exception:
        pass
    return "\n".join(lines)


async def _call_cli(prompt: str, sess: dict) -> str:
    tmpl = _env("HARNESS_CLI")
    if not tmpl:
        return ("还没配置 HARNESS_CLI。\n"
                "在 .env 里写：HARNESS_CLI=dsh run --cwd {cwd} --session {session} --prompt {prompt}\n"
                "（{prompt} / {cwd} / {session} 会被替换；也可以改成你们自己的 dsh 命令）")
    argv = shlex.split(tmpl)
    argv = [a.replace("{prompt}", prompt).replace("{cwd}", sess.get("cwd") or str(_cwd()))
               .replace("{session}", sess.get("id") or "") for a in argv]

    def _run():
        import subprocess
        return subprocess.run(argv, cwd=sess.get("cwd") or str(_cwd()),
                              capture_output=True, text=True, timeout=_timeout())

    try:
        r = await asyncio.to_thread(_run)
    except Exception as e:
        # ⚠️ subprocess 超时抛的是 TimeoutExpired，不是 asyncio.TimeoutError
        import subprocess as _sp
        if isinstance(e, _sp.TimeoutExpired):
            return f"harness 超时（>{_timeout():.0f}s），已终止。"
        return f"harness 启动失败：{type(e).__name__}: {e}"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode != 0 and not out:
        return f"harness 退出码 {r.returncode}：{(err or '(无输出)')[:800]}"
    return out or err or "（harness 没有输出）"


async def _call_http(prompt: str, sess: dict) -> str:
    url = _env("HARNESS_URL")
    if not url:
        return "还没配置 HARNESS_URL（或者把 HARNESS_BACKEND 改成 cli）。"
    headers = {"Content-Type": "application/json"}
    token = _env("HARNESS_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {"prompt": prompt, "session_id": sess.get("id"), "cwd": sess.get("cwd")}
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as c:
            r = await c.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            return f"harness HTTP {r.status_code}：{r.text[:400]}"
        try:
            d = r.json()
        except Exception:
            return r.text
        if isinstance(d, dict):
            for k in ("reply", "output", "text", "content", "message"):
                if d.get(k):
                    return str(d[k])
            return json.dumps(d, ensure_ascii=False)[:1000]
        return str(d)
    except Exception as e:
        return f"harness 请求失败：{type(e).__name__}: {e}"


async def ask(session_key: str, prompt: str, *, speaker_qq: str = "",
              speaker_name: str = "", gid: str = "") -> str:
    """把一条需求交给 harness，返回可以直接发到 QQ 的文本。"""
    p = str(prompt or "").strip()
    if not p:
        return "要改什么？把需求写清楚一点。"
    sess = session_of(session_key)
    ctx = build_context(session_key, speaker_qq, speaker_name, gid)
    full = (f"{ctx}\n\n【任务】{p}" if ctx else p)
    backend = _env("HARNESS_BACKEND", "cli").lower()
    if backend == "http":
        out = await _call_http(full, sess)
    else:
        out = await _call_cli(full, sess)
    out = redact(out).strip()
    sess["last"] = time.time()
    d = _load()
    d.setdefault("sessions", {})[str(session_key)] = sess
    _save(d)
    if len(out) > _max_output():
        out = out[: _max_output()] + f"\n…（输出太长，截到 {_max_output()} 字）"
    return out or "（harness 没有输出）"


def status(session_key: str) -> str:
    sess = session_of(session_key)
    return (
        "🧰 Harness 桥\n"
        f"· 后端：{_env('HARNESS_BACKEND', 'cli')}\n"
        f"· 会话：{sess.get('id')}\n"
        f"· 工作目录：{sess.get('cwd')}\n"
        f"· 超时：{_timeout():.0f}s｜最大输出：{_max_output()} 字\n"
        f"· 上下文：{'开' if _env('HARNESS_CONTEXT', '1') not in ('0','false','off','no') else '关'}\n"
        f"· 仅主人：{'是' if _env('HARNESS_OWNER_ONLY', '1') not in ('0','false','off','no') else '否'}"
    )
