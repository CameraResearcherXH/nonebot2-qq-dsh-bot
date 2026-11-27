# plugins/self_tools/__init__.py
"""
【她自己的工具工厂】—— 让 DeepSeek 能自己写工具、跑工具、看日志、存爬来的数据。

用户 2026-11-16 要的闭环：
  1. 她写一份代码、起个名，调 `self_tool_write` → 代码存到 tools/，并且立刻以
     `mytool_<名字>` 的形式挂进 function calling（名字带 mytool_ 前缀，一眼看出是
     「她自己造的工具」，跟系统给的 draw/pixiv/rollpig 那些分开）。
  2. 她调 `self_tool_run`（或直接调 `mytool_<名字>`）→ 脚本在子进程里跑，
     执行结果同时返回给她 + 追加写进 runs/<名字>.log。
  3. 她用 `self_tool_file` 读自己写的代码、读执行 log、把爬到的数据写进 data/。
  4. 跑挂了就 self_tool_write 改代码再跑，反复到能用为止。

三个目录（都在 SELF_TOOL_DIR 下，默认 data/self_tools/）：
  tools/  —— 她自己写的工具源码（一个 <名字>.py 一个工具，带同名 .meta.json）
  runs/   —— 每次执行的运行结果 log
  data/   —— 她爬来的数据 / 想留存的文件

安全约定（这机器就是私人 bot，但代码是她写的，能炸的只有她自己）：
  · 工具脚本在独立子进程里跑，带超时，卡死就 kill，不会把事件循环带崩。
  · 文件工具只能碰 SELF_TOOL_DIR 之内，`..` 和绝对路径一律拦。
  · 写工具 / 跑工具默认只有主人能触发（SELF_TOOL_OWNER_ONLY=true）。

环境变量：
  SELF_TOOL_DIR       默认 data/self_tools
  SELF_TOOL_OWNER_ONLY 默认 true
  SELF_TOOL_TIMEOUT    脚本超时秒数，默认 60
  SELF_TOOL_READ_MAX   读文件最多返回多少字，默认 1500（跟工具结果上限对齐）
  SELF_TOOL_SCOPE      挂哪些会话：private / private,group，默认 private
"""
from core import tuning
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

from nonebot import logger

from services import llm as llm_mod
from services.access import is_owner

ROOT = Path(__file__).resolve().parents[2]          # 项目根目录
SELF_TOOL_DIR = Path(os.getenv("SELF_TOOL_DIR", "data/self_tools"))
if not SELF_TOOL_DIR.is_absolute():
    SELF_TOOL_DIR = ROOT / SELF_TOOL_DIR
TOOLS_DIR = SELF_TOOL_DIR / "tools"
RUNS_DIR = SELF_TOOL_DIR / "runs"
DATA_DIR = SELF_TOOL_DIR / "data"
for _d in (TOOLS_DIR, RUNS_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SELF_TOOL_OWNER_ONLY = os.getenv("SELF_TOOL_OWNER_ONLY", "true").lower() == "true"
SELF_TOOL_TIMEOUT = tuning.f("SELF_TOOL_TIMEOUT")
SELF_TOOL_READ_MAX = tuning.i("SELF_TOOL_READ_MAX")
# 2026-11-17：把「写工具 / 跑测试」的过程直接打印到聊天窗口，让主人看得见她在干嘛。
SELF_TOOL_ECHO = os.getenv("SELF_TOOL_ECHO", "true").lower() == "true"

# 2026-11-17：默认 private,group —— 群聊里 @ 她让写工具也能调（owner_only 仍挡着非主人）。
_scope_raw = (os.getenv("SELF_TOOL_SCOPE", "private,group") or "private,group").lower()
SCOPE = tuple(s.strip() for s in _scope_raw.split(",") if s.strip()) or ("private",)

# 2026-11-26（audit 待办 #4）：**写工具 / 跑工具只允许私聊**。
# 群聊里只留 self_tool_file（看代码 / 看 log / 存数据）。
# 为什么：`SELF_TOOL_OWNER_ONLY` 只挡「谁」，挡不住「群友诱导主人让她跑代码」，
# 而群里跑代码的后果（刷屏、卡住、被截图）是不可逆的。
# 想放开：SELF_TOOL_EXEC_SCOPE=private,group
_EXEC_SCOPE_RAW = (os.getenv("SELF_TOOL_EXEC_SCOPE", "private") or "private").lower()
EXEC_SCOPE = tuple(s.strip() for s in _EXEC_SCOPE_RAW.split(",") if s.strip()) or ("private",)

# 工具名规则：小写字母开头，后面只能小写字母/数字/下划线。函数名用 mytool_<name>。
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

# ============================================================
# 危险操作黑名单（2026-11-17 用户要求：别给她开「重启容器」的口子）
# 自造工具改完重跑就生效，根本不需要重启 bot；所以她写的代码里一旦出现
# 重启容器/杀进程/关机这类操作，write 直接拒绝保存、run 直接拒绝执行。
# ============================================================
_DANGEROUS_RE = [
    re.compile(r"docker(?:-compose|\s+compose)?\s+(?:restart|stop|down|kill|rm)", re.I),
    re.compile(r"\bkill\s+(?:-\d+\s+)?1\b", re.I),          # kill 1 / kill -9 1（容器主进程）
    re.compile(r"os\.kill\s*\(\s*1\b", re.I),
    re.compile(r"\b(killall|pkill)\b", re.I),
    re.compile(r"\breboot\b", re.I),
    re.compile(r"\bshutdown\b", re.I),
    re.compile(r"\bpoweroff\b", re.I),
    re.compile(r"\bhalt\b", re.I),
    re.compile(r"systemctl\s+(?:restart|stop|kill)", re.I),
    re.compile(r"service\s+\S+\s+(?:restart|stop)", re.I),
]


def _dangerous_hit(code: str) -> str:
    """返回命中的危险片段（空串=安全）。"""
    for _r in _DANGEROUS_RE:
        _m = _r.search(str(code or ""))
        if _m:
            return _m.group(0)
    return ""


_DANGEROUS_NOTE = (
    "改自造工具重跑就生效，**不需要重启 bot 容器**。"
    "别写重启容器 / 杀进程 / 关机 / 停服务这类代码；真要重启，先测好再告诉主人，由主人决定。"
)


def _tool_name_for(name: str) -> str:
    return f"mytool_{name}"


# ============================================================
# 子进程隔离
# ============================================================
# ⚠️ 以前是 `env = dict(os.environ)`：模型写的工具里只要 `print(os.environ)`
#    就能把 XAI_API_KEY / TAVILY_API_KEY / DASHSCOPE_API_KEY 全打出来发到群里。
#    路径限制只挡住了 self_tool_file，挡不住脚本自己读写文件。
#    现在只给「跑 Python 必需」的变量 + 代理（代理不是密钥），密钥一律不给。
_ENV_ALLOW = {
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "HOME", "TMPDIR",
    "SYSTEMROOT", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
}


def _safe_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 保留项目根目录，让她写的工具还能 import services/httpx；
    # 密钥已从环境里拿掉，import 也读不到。
    env["PYTHONPATH"] = str(ROOT)
    return env


def _limit_launcher(tool_path: str, args_json: str) -> list:
    """
    用一段引导脚本设好 rlimit，再 os.execv 成真正的工具进程。

    为什么不用 preexec_fn：Python 官方明确说它在多线程进程里不安全
    （本项目有调度器/图片处理线程），可能死锁。引导脚本是独立进程，没这个问题。
    """
    mem = tuning.i("SELF_TOOL_MEM_MB") * 1024 * 1024
    cpu = int(SELF_TOOL_TIMEOUT) + 5
    fsize = tuning.i("SELF_TOOL_FSIZE_MB") * 1024 * 1024
    code = (
        "import os, resource, sys\n"
        "for _name, _val in ((\"RLIMIT_AS\", %d), (\"RLIMIT_CPU\", %d), (\"RLIMIT_FSIZE\", %d)):\n"
        "    try:\n"
        "        _r = getattr(resource, _name)\n"
        "        resource.setrlimit(_r, (_val, _val))\n"
        "    except Exception:\n"
        "        pass\n"
        "os.execv(sys.executable, [sys.executable, \"-u\", sys.argv[1], sys.argv[2]])\n"
    ) % (mem, cpu, fsize)
    return [sys.executable, "-c", code, str(tool_path), args_json]


# ============================================================
# 执行一个她自己写的工具脚本（子进程，带超时）
# ============================================================
async def _run_script(name: str, args: dict) -> str:
    """在子进程里跑 tools/<name>.py，stdout 当结果返回，并追加写 runs/<name>.log。"""
    tool_path = TOOLS_DIR / f"{name}.py"
    if not tool_path.exists():
        return f"没有 {name} 这个自造工具（tools/{name}.py 不存在）。先 self_tool_write 写一个。"

    # 执行前再扫一遍危险操作（write 已拦，这里防的是旧文件/手改文件绕过）
    try:
        _src = tool_path.read_text(encoding="utf-8", errors="replace")
        _hit = _dangerous_hit(_src)
        if _hit:
            return (f"拒绝执行 {name}：代码里有危险操作「{_hit}」。{_DANGEROUS_NOTE}")
    except Exception as e:
        logger.warning(f"[SELFTOOL] 读 {name} 源码做安全检查失败: {type(e).__name__}: {e}")

    args = args if isinstance(args, dict) else {}
    args_json = json.dumps(args, ensure_ascii=False)
    env = _safe_env()

    started = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *_limit_launcher(str(tool_path), args_json),
            cwd=str(SELF_TOOL_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except Exception as e:
        return f"启动 {name} 失败：{type(e).__name__}: {e}"

    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=SELF_TOOL_TIMEOUT
        )
    except asyncio.TimeoutError:
        # ⚠️ wait_for 取消的是 communicate 协程，不能再调一次 communicate；
        #    直接 kill 进程、读管道收尾，再用 wait() 拿退出码。
        timed_out = True
        try:
            proc.kill()
        except Exception:
            pass
        stdout_b, stderr_b = b"", b""
        try:
            _so, _se = b"", b""
            if proc.stdout is not None:
                _so = await asyncio.wait_for(proc.stdout.read(), timeout=5)
            if proc.stderr is not None:
                _se = await asyncio.wait_for(proc.stderr.read(), timeout=5)
            await asyncio.wait_for(proc.wait(), timeout=5)
            stdout_b, stderr_b = _so, _se
        except Exception:
            pass

    stdout = (stdout_b or b"").decode("utf-8", "replace").strip()
    stderr = (stderr_b or b"").decode("utf-8", "replace").strip()
    code = proc.returncode
    cost = time.time() - started

    # 追加写运行 log（她随时能用 self_tool_file 翻）
    try:
        log_path = RUNS_DIR / f"{name}.log"
        with log_path.open("a", encoding="utf-8") as f:
            f.write("\n" + "=" * 70 + "\n")
            f.write(f"时间 {time.strftime('%Y-%m-%d %H:%M:%S')} | "
                    f"耗时 {cost:.1f}s | exit={code}" +
                    (" | 超时被杀" if timed_out else "") + "\n")
            f.write(f"args {args_json[:2000]}\n")
            f.write("-" * 70 + "\n")
            if stdout:
                f.write(stdout[:20000] + "\n")
            else:
                f.write("(无 stdout 输出)\n")
            if stderr:
                f.write("-" * 70 + "\nstderr:\n" + stderr[:20000] + "\n")
    except Exception as e:
        logger.warning(f"[SELFTOOL] 写运行 log 失败: {type(e).__name__}: {e}")

    tail = "（完整输出在 runs/{}.log，用 self_tool_file 看）".format(name)
    if timed_out:
        return f"{name} 跑了 {SELF_TOOL_TIMEOUT:.0f} 秒还没结束，被强制杀掉。{tail}"
    if code == 0:
        body = stdout or "（执行成功，但没有打印任何内容）"
        return f"{body}\n{tail}"
    detail = stdout or ""
    if stderr:
        detail += ("\n" if detail else "") + stderr
    if not detail:
        detail = "（没有输出）"
    return (f"{name} 执行失败（exit={code}）：\n{detail}\n"
            f"{tail}。改好代码用 self_tool_write 重写，再 self_tool_run 重试。")


# ============================================================
# 动态注册 / 恢复她自己写的工具
# ============================================================
def _make_handler(name: str):
    async def _handler(args: dict) -> str:
        # 自造工具跑的是她自己写的代码，同样只对主人开放（和 write/run 一致）。
        if not _speaker_is_owner():
            return "这个能力只有主人能用。"
        result = await _run_script(name, args or {})
        await _echo(f"🧪 mytool_{name} 测试：{_brief(result)}")
        return result
    return _handler


def _load_meta(name: str) -> dict:
    try:
        p = TOOLS_DIR / f"{name}.meta.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_meta(name: str, description: str, parameters: dict) -> None:
    meta = {
        "name": name,
        "description": description,
        "parameters": parameters or {
            "type": "object",
            "properties": {
                "arg": {"type": "string", "description": "传给脚本的参数（JSON 里随便放）"},
            },
            "required": [],
        },
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (TOOLS_DIR / f"{name}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _register_dynamic(name: str, description: str, parameters: dict) -> None:
    """把一个自造工具挂进 function calling（名字带 mytool_ 前缀以示区别）。"""
    desc = str(description or "").strip() or f"我自己写的工具 {name}。"
    params = parameters if isinstance(parameters, dict) and parameters else {
        "type": "object",
        "properties": {
            "arg": {"type": "string", "description": "传给脚本的参数"},
        },
        "required": [],
    }
    spec = {
        "type": "function",
        "function": {
            "name": _tool_name_for(name),
            "description": f"【我自己写的工具 · {name}】{desc}",
            "parameters": params,
        },
    }
    # 自造工具也是「跑代码」→ 跟 self_tool_run 一样只给私聊
    llm_mod.register_tool(spec, _make_handler(name), scope=EXEC_SCOPE)
    logger.info(f"[SELFTOOL] 已注册自造工具 mytool_{name}")


def _scan_and_restore() -> int:
    """启动时把 tools/ 里已有的工具重新挂上（重启不丢）。"""
    n = 0
    try:
        for py in sorted(TOOLS_DIR.glob("*.py")):
            name = py.stem
            if not _NAME_RE.match(name):
                continue
            meta = _load_meta(name)
            _register_dynamic(name, meta.get("description", ""), meta.get("parameters"))
            n += 1
    except Exception as e:
        logger.warning(f"[SELFTOOL] 恢复自造工具失败: {type(e).__name__}: {e}")
    return n


def _speaker_is_owner() -> bool:
    if not SELF_TOOL_OWNER_ONLY:
        return True
    qq = str(llm_mod.TOOL_CTX.get("speaker_qq") or "")
    return bool(qq) and is_owner(qq)


async def _echo(text: str) -> None:
    """把「写工具 / 跑测试」的过程直接打印到聊天窗口（主人可见），失败静默不打断主流程。"""
    if not SELF_TOOL_ECHO:
        return
    try:
        bot = llm_mod.TOOL_CTX.get("bot")
        ev = llm_mod.TOOL_CTX.get("event")
        if bot and ev:
            await bot.send(ev, str(text)[:400])
    except Exception as e:
        logger.debug(f"[SELFTOOL] 过程播报失败: {type(e).__name__}: {e}")


def _brief(result: str, limit: int = 120) -> str:
    """把工具执行结果压成一行摘要（给聊天窗口播报用，完整结果照旧返回给模型）。"""
    s = str(result or "").strip().replace("\n", " ")
    return s[:limit] + ("…" if len(s) > limit else "")


# ============================================================
# 工具 1：写工具（写入工具）
# ============================================================
async def _tool_write(args: dict) -> str:
    if not _speaker_is_owner():
        return "这个能力只有主人能用。"
    name = str(args.get("name") or "").strip().lower().replace("-", "_")
    if not _NAME_RE.match(name):
        return ("工具名不合法：要小写字母开头，只能含小写字母/数字/下划线，"
                "最多 32 个字符。例如 read_pdf。")
    code = str(args.get("code") or "").strip()
    if not code:
        return "code 不能为空。"
    description = str(args.get("description") or "").strip()
    if not description:
        return "description 不能为空（一句话说清这工具干嘛、什么时候用）。"

    # 保存前先做语法检查，别把一个明显写坏的脚本挂上去
    try:
        compile(code, f"mytool_{name}.py", "exec")
    except SyntaxError as e:
        return f"代码语法有错，没保存：{type(e).__name__}: {e}"

    # 危险操作拦截：不许写重启容器/杀进程/关机这类代码（重跑即生效，不需要重启）
    _hit = _dangerous_hit(code)
    if _hit:
        return f"没保存：代码里有危险操作「{_hit}」。{_DANGEROUS_NOTE}"

    parameters = args.get("parameters")
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except Exception:
            parameters = None
    if not isinstance(parameters, dict):
        parameters = None

    try:
        (TOOLS_DIR / f"{name}.py").write_text(code, encoding="utf-8")
        _save_meta(name, description, parameters)
        _register_dynamic(name, description, parameters)
    except Exception as e:
        return f"保存失败：{type(e).__name__}: {e}"

    await _echo(f"✍️ 写了个工具 mytool_{name}：{_brief(description, 60)}")
    return (
        f"已保存并挂上工具 mytool_{name}（这是你自己的工具）。\n"
        f"说明：{description}\n"
        f"接下来调 self_tool_run(name='{name}', args={{...}}) 跑一次试试，"
        f"跑挂了看返回里的报错，再用 self_tool_write 改代码重试。"
    )


# ============================================================
# 工具 2：跑工具（执行 + 落 log）
# ============================================================
async def _tool_run(args: dict) -> str:
    if not _speaker_is_owner():
        return "这个能力只有主人能用。"
    name = str(args.get("name") or "").strip().lower().replace("-", "_")
    name = name.removeprefix("mytool_")
    if not _NAME_RE.match(name):
        return "name 不合法。"
    run_args = args.get("args")
    if not isinstance(run_args, dict):
        run_args = {}
    result = await _run_script(name, run_args)
    await _echo(f"🧪 mytool_{name} 测试：{_brief(result)}")
    return result


# ============================================================
# 工具 3：文件工具（看代码 / 看 log / 存爬来的 data）
# ============================================================
def _resolve_path(raw: str, for_write: bool = False):
    """
    把模型给的 path 安全地解析到 SELF_TOOL_DIR 之内。
    支持前缀 tools: / runs: / data:，也支持 tools/xxx.py 这种相对写法。
    返回 (Path, err)。err 非空表示不合法。
    """
    raw = (raw or "").strip()
    if not raw:
        return SELF_TOOL_DIR, ""
    if raw.startswith(("/", "\\", "~")):
        return None, "只允许相对路径（在 tools/ runs/ data/ 三个目录里），不支持绝对路径。"

    p = raw
    for prefix, sub in (("tools:", TOOLS_DIR), ("runs:", RUNS_DIR), ("data:", DATA_DIR)):
        if raw.lower().startswith(prefix):
            p = raw[len(prefix):].strip()
            base = sub
            break
    else:
        base = SELF_TOOL_DIR

    p = p.lstrip("/")
    if not p:
        return base, ""

    cand = (base / p).resolve()
    root = SELF_TOOL_DIR.resolve()
    try:
        cand.relative_to(root)
    except ValueError:
        return None, f"路径越界了：只允许在 {root} 里面。"
    if for_write:
        tools_root = TOOLS_DIR.resolve()
        runs_root = RUNS_DIR.resolve()
        try:
            cand.relative_to(tools_root)
            return None, ("要改工具代码请用 self_tool_write（直接写 tools/ 不会重新注册、"
                          "也可能把工具改坏）。")
        except ValueError:
            pass
        try:
            cand.relative_to(runs_root)
            return None, "runs/ 是执行日志目录，系统自己写，不要手动改。"
        except ValueError:
            pass
    return cand, ""


async def _tool_file(args: dict) -> str:
    if not _speaker_is_owner():
        return "这个能力只有主人能用。"
    action = str(args.get("action") or "read").strip().lower()
    raw_path = str(args.get("path") or "").strip()
    content = args.get("content")

    if action == "list":
        target, err = _resolve_path(raw_path)
        if err:
            return err
        try:
            if target.is_file():
                st = target.stat()
                return f"{target.relative_to(SELF_TOOL_DIR)} ｜ {st.st_size} 字节"
            entries = sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name))
            if not entries:
                return f"{target.relative_to(SELF_TOOL_DIR)} 是空的。"
            lines = []
            for e in entries:
                if e.is_dir():
                    lines.append(f"[目录] {e.name}/")
                else:
                    try:
                        size = e.stat().st_size
                    except Exception:
                        size = -1
                    lines.append(f"{e.name} ｜ {size} 字节")
            return "\n".join(lines)
        except Exception as e:
            return f"列目录失败：{type(e).__name__}: {e}"

    if action == "read":
        target, err = _resolve_path(raw_path)
        if err:
            return err
        if not target.exists():
            return f"文件不存在：{target.relative_to(SELF_TOOL_DIR)}"
        if target.is_dir():
            return f"这是个目录，用 action=list 看内容：{target.relative_to(SELF_TOOL_DIR)}"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"读文件失败：{type(e).__name__}: {e}"
        total = len(text)
        if total <= SELF_TOOL_READ_MAX:
            return text
        # 太长：默认看开头；log 或显式 tail=true 时看末尾（最新的那次执行）
        _rel = str(target.relative_to(SELF_TOOL_DIR))
        _is_log = _rel == "runs" or _rel.startswith("runs" + os.sep)
        want_tail = bool(args.get("tail")) or _is_log
        if want_tail:
            return (f"（文件共 {total} 字，显示末尾最新 {SELF_TOOL_READ_MAX} 字）\n"
                    f"{text[-SELF_TOOL_READ_MAX:]}")
        return (f"（文件共 {total} 字，只显示开头 {SELF_TOOL_READ_MAX} 字；"
                f"要看末尾请加 tail=true）\n{text[:SELF_TOOL_READ_MAX]}")

    if action == "write":
        target, err = _resolve_path(raw_path, for_write=True)
        if err:
            return err
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except Exception as e:
            return f"写文件失败：{type(e).__name__}: {e}"
        return f"已写入 {target.relative_to(SELF_TOOL_DIR)}（{len(text)} 字）"

    return ("action 只支持 list / read / write。"
            "看自己写的代码：read + tools/<名字>.py；"
            "看执行 log：read + runs/<名字>.log；"
            "存爬来的数据：write + data/<文件名>。")


# ============================================================
# 注册三个元工具 + 恢复已有自造工具
# ============================================================
_OWNER_NOTE = ("（只有主人能触发）" if SELF_TOOL_OWNER_ONLY else "")

llm_mod.register_tool(
    {
        "type": "function",
        "function": {
            "name": "self_tool_write",
            "description": (
                f"写/改一个【你自己的工具】。把 Python 代码存进 tools/ 目录并立刻以 "
                f"mytool_<名字> 挂进你的 function calling（带 mytool_ 前缀 = 你自己造的，"
                f"跟系统给的 draw_image/pixiv_search/rollpig/petpet/napcat_call 等区分开）。"
                f"⚠️ 只有在现有工具（我给你的那批 + 你自己已写的）确实干不了这件事时，"
                f"才自己写；已有能力优先用现成的。写代码前先想清楚：脚本会以 "
                f"sys.argv[1]（JSON 字符串）收到参数，用 print 把结果打到 stdout，"
                f"stdout 就是返回给你的结果。执行结果也会自动存进 runs/<名字>.log。"
                f"同一个名字再次写 = 覆盖更新（比如今天写了 read_pdf，改天来了 doc 文件，"
                f"就重写 read_pdf 给它补上 doc 能力）。覆盖前先 self_tool_file read tools/<名字>.py "
                f"把现有代码读回来，在旧能力基础上加新的，别把原来能干的删了。"
                f"⚠️ {_DANGEROUS_NOTE}{_OWNER_NOTE}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "工具名，小写字母开头，只含小写字母/数字/下划线，如 read_pdf",
                    },
                    "code": {
                        "type": "string",
                        "description": (
                            "完整 Python 代码。约定：用 sys.argv[1] 收 JSON 参数（可缺省），"
                            "干活，最后 print 结果。cwd 是 SELF_TOOL_DIR，data/ 子目录可存爬来的数据。"
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "一句话说清这工具干嘛、什么时候该调它（会写进 function 说明）",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "可选，这个工具的 JSON Schema 参数表；不填就默认一个 arg 参数",
                    },
                },
                "required": ["name", "code", "description"],
            },
        },
    },
    _tool_write,
    scope=EXEC_SCOPE,
)

llm_mod.register_tool(
    {
        "type": "function",
        "function": {
            "name": "self_tool_run",
            "description": (
                f"运行一个你自己写的工具（tools/<名字>.py），并把它这次执行的输出追加进 "
                f"runs/<名字>.log。写工具 → 跑一次 → 看报错 → 改代码，反复到能用。"
                f"{_OWNER_NOTE}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "要跑的工具名（不带 mytool_ 前缀），如 read_pdf",
                    },
                    "args": {
                        "type": "object",
                        "description": "传给脚本的参数对象，会序列化成 JSON 放到 sys.argv[1]",
                    },
                },
                "required": ["name"],
            },
        },
    },
    _tool_run,
    scope=EXEC_SCOPE,
)

llm_mod.register_tool(
    {
        "type": "function",
        "function": {
            "name": "self_tool_file",
            "description": (
                f"在你的三个目录（tools/ 工具源码、runs/ 执行 log、data/ 爬来的数据）里"
                f"看文件 / 列目录 / 写数据文件。"
                f"看自己写的代码：action=read, path=tools/<名字>.py；"
                f"看执行 log：action=read, path=runs/<名字>.log；"
                f"存爬来的数据：action=write, path=data/<文件名>, content=内容。"
                f"{_OWNER_NOTE}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "write"],
                        "description": "list=列目录，read=读文件，write=写数据文件",
                    },
                    "path": {
                        "type": "string",
                        "description": (
                            "相对路径，可带前缀 tools:/runs:/data:，也可写 tools/xx.py 这种。"
                            "list 不填 = 看三个目录的根。"
                        ),
                    },
                    "tail": {
                        "type": "boolean",
                        "description": "读太长文件时，true=看末尾（log 默认就是末尾），false=看开头",
                    },
                    "content": {
                        "type": "string",
                        "description": "action=write 时要写入的内容",
                    },
                },
                "required": ["action"],
            },
        },
    },
    _tool_file,
    scope=SCOPE,
)

_restored = _scan_and_restore()
if _restored:
    logger.info(f"[SELFTOOL] 启动时恢复了 {_restored} 个自造工具")
logger.info(
    f"[SELFTOOL] 自造工具工厂已就绪：dir={SELF_TOOL_DIR} | owner_only={SELF_TOOL_OWNER_ONLY} "
    f"| timeout={SELF_TOOL_TIMEOUT:.0f}s | scope={SCOPE}"
)
