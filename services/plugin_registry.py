# services/plugin_registry.py
"""插件商城：扫描 plugins/third，读元数据，把声明的能力注册成 function calling skill。

双轨元数据：
1. nonebot 原生 `__plugin_meta__`（name / description / usage）—— 所有插件都能有。
2. 可选 `plugin.toml` —— 声明 [[tools]]（name/description/level/handler/scope），
   有才注册工具；没有就只当普通 nonebot 插件，不暴露 skill。

安全：
- 工具默认 level=safe；confirm/owner 复用 services.napcat_tools 的分级语义。
- 安装前静态扫描（subprocess/os.system/eval/exec/外联）由 CLI 做，不在这里执行。
"""
import ast
import importlib
import json
import os
import re
import time
from pathlib import Path

try:
    import tomllib
except Exception:  # Python < 3.11
    tomllib = None

ROOT = Path(__file__).resolve().parent.parent
THIRD_DIR = ROOT / "plugins" / "third"
STATE_FILE = Path(os.getenv("PLUGIN_STATE_FILE", "data/plugins.json"))


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}
    return {}


def _save_state(d: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def enabled_map() -> dict:
    return dict(_load_state().get("enabled") or {})


def is_enabled(name: str) -> bool:
    return bool(enabled_map().get(str(name), True))


def set_enabled(name: str, value: bool) -> None:
    d = _load_state()
    en = dict(d.get("enabled") or {})
    en[str(name)] = bool(value)
    d["enabled"] = en
    d["updated_at"] = time.time()
    _save_state(d)


def _meta_from_init(path: Path) -> dict:
    """不 import 插件，从 AST 里读 __plugin_meta__ 的字面量字段。"""
    init = path / "__init__.py"
    if not init.exists():
        return {}
    try:
        tree = ast.parse(init.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id == "__plugin_meta__":
                try:
                    return ast.literal_eval(node.value)
                except Exception:
                    return {}
    return {}


def _read_toml(path: Path) -> dict:
    f = path / "plugin.toml"
    if not f.exists() or tomllib is None:
        return {}
    try:
        return tomllib.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}


def scan(root: Path = None) -> list:
    root = Path(root or THIRD_DIR)
    out = []
    if not root.exists():
        return out
    for p in sorted(root.iterdir()):
        if not p.is_dir() or p.name.startswith(("_", ".")):
            continue
        meta = _meta_from_init(p)
        conf = _read_toml(p)
        pl = conf.get("plugin") or {}
        entry = {
            "dir": p.name,
            "name": str(pl.get("name") or meta.get("name") or p.name),
            "version": str(pl.get("version") or meta.get("version") or ""),
            "description": str(pl.get("description") or meta.get("description") or ""),
            "enabled": is_enabled(p.name),
            "tools": list(conf.get("tools") or []),
            "has_upstream": (p / "UPSTREAM.md").exists(),
            "has_license": any((p / x).exists() for x in ("LICENSE", "COPYING", "LICENSE.md")),
        }
        out.append(entry)
    return out


def _resolve(handler: str):
    """'module:func' 或 'pkg.module:func' → callable。"""
    if ":" not in str(handler or ""):
        return None
    mod_name, fn_name = str(handler).split(":", 1)
    try:
        mod = importlib.import_module(mod_name)
        return getattr(mod, fn_name)
    except Exception:
        return None


def register_all() -> dict:
    """把启用插件里声明的 tools 注册到 llm.register_tool。返回统计。"""
    stats = {"plugins": 0, "tools": 0, "skipped": []}
    try:
        from . import llm
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}
    for e in scan():
        stats["plugins"] += 1
        if not e["enabled"]:
            stats["skipped"].append((e["dir"], "disabled"))
            continue
        for t in e["tools"]:
            name = str(t.get("name") or "").strip()
            handler = _resolve(t.get("handler") or "")
            if not name or handler is None:
                stats["skipped"].append((e["dir"], "tool 缺少 name/handler: %s" % name))
                continue
            spec = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(t.get("description") or e["description"])[:200],
                    "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                },
            }
            try:
                llm.register_tool(spec, handler,
                                  scope=tuple(t.get("scope") or ("private", "group")),
                                  hold=bool(t.get("hold", True)))
                stats["tools"] += 1
            except Exception as ex:
                stats["skipped"].append((e["dir"], "%s: %s" % (type(ex).__name__, ex)))
    return stats


RISK_PATTERNS = (
    (r"\bsubprocess\b", "调用 subprocess"),
    (r"\bos\.system\b", "调用 os.system"),
    (r"\beval\s*\(", "使用 eval"),
    (r"\bexec\s*\(", "使用 exec"),
    (r"https?://[^\s\"']+", "外联地址"),
)


def audit_plugin(path: Path) -> list:
    """安装前静态扫描，返回 [(风险, 文件:行)]。"""
    out = []
    for f in Path(path).rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for i, line in enumerate(lines, 1):
            for pat, why in RISK_PATTERNS:
                if re.search(pat, line):
                    out.append((why, "%s:%d" % (f.name, i)))
    return out


def status_text() -> str:
    rows = scan()
    lines = ["▍插件商城（plugins/third）共 %d 个" % len(rows)]
    for e in rows:
        flag = "开" if e["enabled"] else "关"
        tools = len(e["tools"])
        lines.append("· [%s] %-26s %s｜tools=%d%s%s" % (
            flag, e["dir"], e["name"][:18], tools,
            "" if e["has_upstream"] else "｜⚠️无UPSTREAM",
            "" if e["has_license"] else "｜⚠️无LICENSE"))
    return "\n".join(lines)
