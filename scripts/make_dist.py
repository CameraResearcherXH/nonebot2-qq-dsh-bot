#!/usr/bin/env python3
# scripts/make_dist.py
"""生成开源发行仓 ~/hzy/qq-dsh-bot（不带 data / 不带密钥 / 带文档）。

用法：
    python3 scripts/make_dist.py                 # 生成到 ~/hzy/qq-dsh-bot
    python3 scripts/make_dist.py --dest /tmp/x    # 指定目录
    python3 scripts/make_dist.py --force          # 覆盖已存在目录
    python3 scripts/make_dist.py --allow          # 脱敏命中时不中止（仍写报告）

规则：
- 白名单复制；data/、.env、node.py、napcat/、plugins_backup/、venv/ 等一律不复制。
- 复制后扫描密钥/私人号码，命中即中止（除非 --allow）。
- 生成 .env.example、personality.example.txt、DIST_REPORT.md。
- 最后跑 compileall + 依赖方向检查；失败写进报告。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {
    "data", "napcat", "plugins_backup", "venv", ".venv", "env",
    "__pycache__", ".git", ".idea", ".vscode", ".workbuddy", ".codebuddy",
    "_deploy", "_deploy_server",
    # xray 本地运行时（34MB Windows 二进制，2026-11-24 发现误入过发行仓）
    ".xray_runtime",
    # 本地合入用的补丁（含 .env 原始 diff，绝不能进发行仓）
    "patches",
    # 私人人格卡：发行仓只放通用模板（personality.example.txt）
    "Personality",
}
EXCLUDE_FILES = {
    # node.py 现在随发行仓提供（算力机客户端）；node.env 只生成 .example
    ".env", "node.env", "deploy_manifest.txt", "bot.py.working",
    "test.py", "jy.tex", "help.txt",
    # NapCat 登录二维码（私人）
    "napcat_login_qr.png",
    # 与本项目无关的私人笔记 / 抓取的第三方文档
    "3a_zh.txt", "d3.txt", "e1.txt", "v1.txt",
    "t_api_zh.txt", "t_hdr_zh.txt", "t_iqtuning_zh.txt",
}
EXCLUDE_GLOBS = ("*.working", "*.bak", "*.old", "*.orig", "*.log", "*.pyc",
                 ".env.*", "docs/review-*.md",
                 "scripts/_*.py",
                 "scripts/xray_proxy.py")

SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9._-]{10,}"), "API key (sk-)"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"), "Bearer token"),
    # 只匹配「带引号的字面量」，避免把 default_token=xxx 这种变量名吃掉
    (re.compile(r"(?i)(cookie|token|secret)\s*[:=]\s*['\"][A-Za-z0-9._-]{20,}['\"]"),
     "cookie/token"),
]

# 私人号码：从 .env 的 ALLOWED_GROUPS / ALLOWED_PRIVATE / OWNER_QQ 里取真实值
PRIVATE_NUMBERS = set()


def load_private_numbers():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(
            r"\s*(ALLOWED_GROUPS|ALLOWED_PRIVATE|OWNER_QQ|SUPERUSERS"
            r"|NAPCAT_ACCOUNT|SELF_ID|SELF_ALIASES)\s*=\s*(.+)", line)
        if not m:
            continue
        for x in re.split(r"[,，\s]+", m.group(2)):
            x = x.strip().strip("\"'")
            if x.isdigit() and len(x) >= 5:
                PRIVATE_NUMBERS.add(x)


def _num_placeholder(num: str) -> str:
    """把真实 QQ / 群号换成稳定的假号（同一个号在所有文件里一致）。"""
    try:
        idx = sorted(PRIVATE_NUMBERS).index(str(num)) + 1
    except ValueError:
        idx = 0
    return str(100000000 + idx)


def is_excluded(rel: Path) -> bool:
    parts = rel.parts
    for i, p in enumerate(parts):
        if p not in EXCLUDE_DIRS:
            continue
        # 只排除仓库根目录的 patches/（里面有 .env diff，绝不能发）；
        # plugins/third/**/patches/*.patch 是第三方插件自带的补丁，要保留。
        if p == "patches" and i > 0:
            continue
        return True
    if rel.name in EXCLUDE_FILES:
        return True
    s = rel.as_posix()
    for g in EXCLUDE_GLOBS:
        if Path(s).match(g) or rel.match(g):
            return True
    return False


def copy_tree(dest: Path) -> list:
    copied = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT)
        if is_excluded(rel):
            continue
        target = dest / rel
        if p.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        copied.append(str(rel))
    return copied


BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp3", ".wav",
              ".db", ".zip", ".ttf", ".otf", ".woff", ".woff2")


def env_secret_values() -> list:
    """从 .env / node.env 收集真实密钥值（发行仓必须清掉）。"""
    out = []
    for name in (".env", "node.env"):
        p = ROOT / name
        if not p.exists():
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for line in lines:
            m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S.*)$", line)
            if not m:
                continue
            key, val = m.group(1), m.group(2).strip().strip("'\"")
            if "#" in val:
                val = val.split("#", 1)[0].strip()
            if not any(t in key for t in ("SECRET", "KEY", "TOKEN", "COOKIE", "PASS")):
                continue
            if len(val) >= 8 and not val.startswith(("http", "sk-xxx", "your")):
                out.append(val)
    return sorted(set(out), key=len, reverse=True)


# 私人服务器地址：发行仓统一换成 127.0.0.1
PRIVATE_HOSTS = ("127.0.0.1",)


# 群友昵称：文档/注释/测试里出现过的真实群友称呼，统一换成通用占位名
NICKNAME_MAP = {
    "阿明": "阿明",
    "阿明": "阿明",
    "小红": "小红",
    "老王": "老王",
}


def redact_nicknames(dest: Path) -> list:
    hits = []
    for p in dest.rglob("*"):
        if not p.is_file() or p.suffix.lower() in BINARY_EXT:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        new = text
        for a, b in NICKNAME_MAP.items():
            if a in new:
                # ⚠️ 报告里不能写原昵称：DIST_REPORT.md 本身也在发行仓里，
                #    写出来等于把刚脱敏掉的名字又漏一遍。
                hits.append((p.relative_to(dest).as_posix(), "群友昵称", b))
                new = new.replace(a, b)
        if new != text:
            try:
                p.write_text(new, encoding="utf-8")
            except Exception:
                pass
    return hits


def redact_hosts(dest: Path) -> list:
    hits = []
    for p in dest.rglob("*"):
        if not p.is_file() or p.suffix.lower() in BINARY_EXT:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        new = text
        for h in PRIVATE_HOSTS:
            if h in new:
                hits.append((p.relative_to(dest).as_posix(), "私人服务器 IP", "[REDACTED]"))
                new = new.replace(h, "127.0.0.1")
        if new != text:
            try:
                p.write_text(new, encoding="utf-8")
            except Exception:
                pass
    return hits


def redact_secrets(dest: Path) -> list:
    """在发行仓副本里就地脱敏（密钥/私人号码），返回命中清单。

    注意：只改副本，源仓不动。二进制文件跳过。
    """
    hits = []
    secrets = env_secret_values()
    for p in dest.rglob("*"):
        if not p.is_file() or p.suffix.lower() in BINARY_EXT:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        rel = p.relative_to(dest).as_posix()
        new = text
        # ① 精确替换 .env 里的真实密钥值（连文档里只写了一半的也一起清掉）
        for val in secrets:
            if val and val in new:
                hits.append((rel, "env 密钥值", "[REDACTED]"))
                new = new.replace(val, "[REDACTED_SECRET]")
        # ② 通用模式
        for pat, why in SECRET_PATTERNS:
            for _m in pat.finditer(new):
                hits.append((rel, why, "[REDACTED]"))
            new = pat.sub("[REDACTED_SECRET]", new)
        # 私人号码：.py 里可能是 int 字面量，必须换成**数字**占位符，不能换成 [REDACTED]
        for num in sorted(PRIVATE_NUMBERS):
            if num in new:
                hits.append((rel, "私人号码", "[REDACTED]"))
                new = new.replace(num, _num_placeholder(num))
        if new != text:
            try:
                p.write_text(new, encoding="utf-8")
            except Exception:
                pass
    return hits


# .env.example 里必须清空的键（密钥 / 私人号码 / 私人名单）
_ENV_CLEAR_KEYS = {
    "SUPERUSERS", "ALLOWED_GROUPS", "ALLOWED_PRIVATE", "OWNER_QQ",
    "NAPCAT_ACCOUNT", "SELF_ID", "SELF_ALIASES",
}

# 私人基础设施地址 → 公开默认值（2026-11-24：阿里云专属工作区 / 自建站不能进发行仓）
_ENV_REWRITE = {
    "DASHSCOPE_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "BT_BASE_URL": "",
}


def gen_env_example(dest: Path) -> int:
    """生成 .env.example：密钥/私人名单清空，**普通参数保留当前值**当参考。"""
    src = ROOT / ".env"
    if not src.exists():
        return 0
    out = ["# 自动生成：密钥与私人名单已清空；普通参数保留当前值，按需修改。", ""]
    n = 0
    for line in src.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            if line.strip().startswith("#"):
                out.append(line)
            continue
        key, val = m.group(1), m.group(2)
        comment = ""
        if "#" in val:
            val, comment = val.split("#", 1)
            comment = "#" + comment
        val = val.strip()
        secret = (key in _ENV_CLEAR_KEYS
                  or any(t in key for t in ("SECRET", "KEY", "TOKEN", "COOKIE", "PASS")))
        if secret:
            val = ""
        elif key in _ENV_REWRITE:
            val = _ENV_REWRITE[key]
        else:
            for num in sorted(PRIVATE_NUMBERS):
                if num in val:
                    val = val.replace(num, _num_placeholder(num))
        out.append("%s=%s" % (key, val + ((" " + comment.strip()) if comment.strip() else "")))
        n += 1
    (dest / ".env.example").write_text("\n".join(out) + "\n", encoding="utf-8")
    return n


def gen_node_env_example(dest: Path) -> int:
    """node.env → node.env.example：保留注释和键名，密钥/值清空。"""
    src = ROOT / "node.env"
    if not src.exists():
        return 0
    out = ["# 自动生成：算力机配置模板（node.py 读取）。填好后改名为 node.env", ""]
    n = 0
    for line in src.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            out.append(line)
            continue
        key, val = m.group(1), m.group(2)
        comment = ""
        if "#" in val:
            val, comment = val.split("#", 1)
            comment = "#" + comment
        # 密钥类一律留空；普通参数保留原值当示例
        if (any(t in key for t in ("SECRET", "KEY", "TOKEN", "COOKIE", "PASS"))
                or key in ("DASHSCOPE_BASE_URL",)):
            val = ""
        out.append("%s=%s" % (key, val.rstrip() + (" " + comment.strip() if comment.strip() else "")))
        n += 1
    (dest / "node.env.example").write_text("\n".join(out) + "\n", encoding="utf-8")
    return n


PERSONA_TEMPLATE = """# 通用人格模板（请替换成你自己的角色）
#
# 用法：
#   1. 复制成 services/Personality/<你的角色名>.txt
#   2. .env 里 DEFAULT_PERSONA=<你的角色名>
#   3. 聊天里 /persona list 查看，/persona <名字> 切换
#
# 字段参考（对齐 SillyTavern character card v2，但我们用纯文本分段）：
#   description / personality / scenario / mes_example / system_prompt
#   对应下面的一、二、三、四节。详见 docs/persona-card.md。

一、你是谁（description）
────────────────────────────────────────
你是<角色名>，<身份/来历>。你清楚自己是 AI 还是人类（按你的设定写），
在 QQ 群里以<什么姿态>存在。别人叫你<称呼>时，你会<反应>。

二、性格与说话方式（personality）
────────────────────────────────────────
- 表层：<别人能看到的性格>
- 内里：<不轻易露出来的部分>
- 说话：<句子长短、口头禅、标点习惯、用不用表情>
- 不会：<明确不做的行为，例如不复述设定、不写旁白动作>

三、场景与关系（scenario）
────────────────────────────────────────
- 在群里：<她通常怎么参与聊天>
- 在私聊：<一对一时的状态>
- 对主人：<特殊关系/称呼>
- 对陌生人：<边界>

四、语气样例（mes_example）
────────────────────────────────────────
用户：在吗
角色：<一句符合人设的回复>

用户：今天好累
角色：<一句符合人设的回复>

五、最高原则（system_prompt）
────────────────────────────────────────
- 先像真人，再谈人设。
- 不解释自己是 AI，不输出 Markdown、列表、括号动作。
- 不把设定原文复述给用户。
- <你这个角色独有的硬规则>

六、禁止（negative）
────────────────────────────────────────
- 不碰政治敏感、色情、违法内容。
- 不承认自己是语言模型/客服。
- 不替用户做决定，不输出「希望对你有帮助」这类客套话。
"""


def gen_persona_example(dest: Path) -> bool:
    """发行仓只放通用模板，不复制私人人格卡。"""
    (dest / "personality.example.txt").write_text(
        PERSONA_TEMPLATE, encoding="utf-8")
    return True


def _py_for_compile() -> str:
    """挑一个 >=3.9 的解释器做 compileall。

    仓库里有 3.9+ 语法（`@get_driver().on_shutdown` 这类装饰器里套调用）。
    宿主只有 3.8 时 compileall 会把**正确的**代码报成 SyntaxError，
    所以要么换解释器，要么明确 SKIP（到 3.11 容器里再验）。
    """
    # 需要 >=3.10：三方插件里有 `match` 语句（VideoZoomer 的 remote_code），
    # 3.9 也编译不过。容器镜像是 3.11。
    for cand in ("python3.12", "python3.11", "python3.10"):
        p = shutil.which(cand)
        if p:
            return p
    if sys.version_info >= (3, 10):
        return sys.executable
    return ""


def run_checks(dest: Path) -> dict:
    checks = {}
    py = sys.executable
    cpy = _py_for_compile()
    if cpy:
        r = subprocess.run([cpy, "-m", "compileall", "-q", str(dest)],
                           capture_output=True, text=True)
        checks["compileall"] = (r.returncode == 0,
                                r.stdout[-500:] + r.stderr[-500:])
    else:
        checks["compileall"] = (
            True,
            "SKIP: 宿主 python %d.%d < 3.10（仓库含 match / 3.9+ 装饰器语法）；"
            "请在 3.11 容器内跑 `python -m compileall -q .` 复验"
            % sys.version_info[:2],
        )
    dep = dest / "scripts" / "check_deps.py"
    if dep.exists():
        r = subprocess.run([py, str(dep)], capture_output=True, text=True, cwd=str(dest))
        checks["deps"] = (r.returncode == 0, (r.stdout or "")[-800:])
    return checks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", default=str(Path.home() / "hzy" / "qq-dsh-bot"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--allow", action="store_true")
    args = ap.parse_args()
    dest = Path(args.dest).expanduser().resolve()

    load_private_numbers()
    if dest.exists():
        if not args.force:
            print("❌ 目标已存在：%s（加 --force 覆盖）" % dest)
            return 1
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    copied = copy_tree(dest)
    env_n = gen_env_example(dest)
    node_env_n = gen_node_env_example(dest)
    persona = gen_persona_example(dest)
    hits = redact_secrets(dest)
    hits += redact_nicknames(dest)
    hits += redact_hosts(dest)
    checks = run_checks(dest)

    report = [
        "# DIST_REPORT",
        "",
        "- 生成时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "- 目标：%s" % dest,
        "- 复制文件：%d" % len(copied),
        "- .env.example 配置项：%d" % env_n,
        "- node.env.example 配置项：%d" % node_env_n,
        "- 人格模板：%s" % ("已生成" if persona else "无"),
        "- 脱敏命中：%d" % len(hits),
        "",
        "## 校验",
    ]
    for k, (ok, msg) in checks.items():
        status = ("SKIP" if (msg or "").startswith("SKIP")
                  else ("PASS" if ok else "FAIL"))
        report.append("- %s：%s" % (k, status))
        if status == "SKIP":
            report.append("  %s" % msg.strip())
        elif not ok and msg:
            report.append("```\n%s\n```" % msg.strip())
    if hits:
        report += ["", "## 脱敏命中（必须处理）"]
        report += ["- %s：%s（%s）" % h for h in hits[:80]]
    (dest / "DIST_REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    # 校验时在副本里跑过 compileall，清掉它产生的缓存（发行仓不该带 __pycache__）
    import shutil as _sh
    for _d in dest.rglob("__pycache__"):
        _sh.rmtree(_d, ignore_errors=True)

    print("已生成：%s" % dest)
    _cok, _cmsg = checks.get("compileall", (False, ""))
    _cst = "SKIP" if (_cmsg or "").startswith("SKIP") else ("PASS" if _cok else "FAIL")
    print("文件 %d｜脱敏命中 %d｜compileall %s"
          % (len(copied), len(hits), _cst))
    if hits:
        print("已脱敏 %d 处（密钥/私人号码，仅改副本）。详见 DIST_REPORT.md" % len(hits))
    if not all(ok for ok, _ in checks.values()):
        print("⚠️ 校验未全通过，详见 DIST_REPORT.md")
        return 1
    print("✅ 发行仓可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
