#!/usr/bin/env python3
# scripts/plugin_cli.py
"""插件商城 CLI。

用法：
    python3 scripts/plugin_cli.py list
    python3 scripts/plugin_cli.py doctor
    python3 scripts/plugin_cli.py install <本地目录|git-url> [--name 名字] [--yes]
    python3 scripts/plugin_cli.py enable <名字>
    python3 scripts/plugin_cli.py disable <名字>
    python3 scripts/plugin_cli.py remove <名字> --yes

约定：
- 安装目录：plugins/third/<名字>
- 元数据：可选 plugin.toml（声明 tools）+ nonebot __plugin_meta__
- 安装前做静态风险扫描（subprocess/os.system/eval/exec/外联），只报告不阻断
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import plugin_registry as reg  # noqa: E402

THIRD = ROOT / "plugins" / "third"


def cmd_list(_):
    print(reg.status_text())
    return 0


def cmd_doctor(_):
    rows = reg.scan()
    bad = 0
    for e in rows:
        p = THIRD / e["dir"]
        risks = reg.audit_plugin(p)
        flags = []
        if not e["has_upstream"]:
            flags.append("缺 UPSTREAM.md")
        if not e["has_license"]:
            flags.append("缺 LICENSE")
        if risks:
            flags.append("风险 %d 处" % len(risks))
        if flags:
            bad += 1
            print("· %s：%s" % (e["dir"], "；".join(flags)))
            for why, where in risks[:8]:
                print("    - %s（%s）" % (why, where))
    if not bad:
        print("✅ 所有插件都有来源/许可证，未发现高风险调用")
    return 0


def _copy_src(src: Path, dest: Path, yes: bool):
    if dest.exists():
        print("❌ 目标已存在：%s（先 remove 或换 --name）" % dest)
        return False
    if not yes:
        print("将安装到 %s。加 --yes 确认。" % dest)
        return False
    shutil.copytree(src, dest)
    return True


def cmd_install(args):
    src = args.source
    name = args.name
    if src.startswith(("http://", "https://", "git@")):
        tmp = Path(tempfile.mkdtemp(prefix="plugin_"))
        print("git clone %s ..." % src)
        r = subprocess.run(["git", "clone", "--depth", "1", src, str(tmp / "repo")],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("❌ clone 失败：%s" % r.stderr[:300])
            return 1
        src_path = tmp / "repo"
        name = name or src_path.name.replace("nonebot_plugin_", "")
    else:
        src_path = Path(src).expanduser().resolve()
        name = name or src_path.name
    if not src_path.exists():
        print("❌ 源不存在：%s" % src_path)
        return 1
    dest = THIRD / name
    print("源：%s\n目标：%s" % (src_path, dest))
    risks = reg.audit_plugin(src_path)
    if risks:
        print("⚠️ 静态风险（请人工确认后再用）：")
        for why, where in risks[:15]:
            print("    - %s（%s）" % (why, where))
    if not _copy_src(src_path, dest, args.yes):
        return 1
    print("✅ 已安装。记得补 plugins/third/%s/UPSTREAM.md" % name)
    return 0


def cmd_toggle(args, value: bool):
    reg.set_enabled(args.name, value)
    print("%s：%s" % ("已启用" if value else "已禁用", args.name))
    return 0


def cmd_remove(args):
    dest = THIRD / args.name
    if not dest.exists():
        print("❌ 不存在：%s" % dest)
        return 1
    if not args.yes:
        print("将删除 %s。加 --yes 确认。" % dest)
        return 0
    shutil.rmtree(dest)
    print("已删除：%s" % dest)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    p = sub.add_parser("install")
    p.add_argument("source")
    p.add_argument("--name", default="")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_install)
    for name, fn, val in (("enable", cmd_toggle, True), ("disable", cmd_toggle, False)):
        q = sub.add_parser(name)
        q.add_argument("name")
        q.set_defaults(fn=lambda a, f=fn, v=val: f(a, v))
    q = sub.add_parser("remove")
    q.add_argument("name")
    q.add_argument("--yes", action="store_true")
    q.set_defaults(fn=cmd_remove)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
