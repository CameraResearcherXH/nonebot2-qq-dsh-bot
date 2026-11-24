# scripts/test_self_tools.py
"""
直接验证 plugins/self_tools 的「写工具 → 跑工具 → 读日志 → 读代码 → 写数据」闭环。

不依赖 LLM，也不依赖 QQ 收发消息 —— 在 bot 容器里直接调 _tool_write / _tool_run /
_tool_file 三个函数，并检查自造工具有没有被注册进 function calling。

用法（服务器上，项目目录 /app 内）：
    docker exec -it qq-grok-bot python scripts/test_self_tools.py

测试完会自动清理测试工具（t_smoke）和它产生的文件、注册项，不会污染她真正的工具库。
"""
import asyncio
import os
import sys
from pathlib import Path

# 项目根目录加进 sys.path：直接 `python scripts/test_self_tools.py` 时 sys.path[0]
# 是 scripts/ 而不是项目根，会导致 `import services` / `import plugins` 找不到。
# 同时把 cwd 切到项目根，保证 nonebot.init() 能读到 /app/.env。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# 先初始化 nonebot，让 services.access 能读到 .env 里的 SUPERUSERS（主人 QQ）。
# 顺序很重要：必须先 init，再 import services.access / plugins.self_tools。
import nonebot
nonebot.init()


async def main() -> int:
    from services import llm as llm_mod
    from services.access import OWNER_QQ
    from plugins.self_tools import (
        SELF_TOOL_OWNER_ONLY,
        TOOLS_DIR,
        RUNS_DIR,
        DATA_DIR,
        _tool_write,
        _tool_run,
        _tool_file,
        _tool_name_for,
    )

    name = "t_smoke"
    code = (
        "import sys, json\n"
        "args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}\n"
        "print('ok', args.get('x', 'none'))\n"
    )
    fn_name = _tool_name_for(name)

    # 0) 三个目录建好了吗
    assert TOOLS_DIR.is_dir() and RUNS_DIR.is_dir() and DATA_DIR.is_dir()
    print(f"✅ 目录就绪：{TOOLS_DIR.parent}")

    # 1) 权限：非主人应被拦（只有开了 owner_only 才测这条）
    llm_mod.set_tool_context(speaker_qq="999999", bot=None, event=None)
    if SELF_TOOL_OWNER_ONLY:
        r = await _tool_write({"name": name, "code": code, "description": "测试"})
        assert "只有主人" in r, f"非主人应该被拦，实际返回：{r}"
        print("✅ 非主人被正确拦截")

    # 2) 主人写工具 → 应注册进 function calling
    llm_mod.set_tool_context(speaker_qq=OWNER_QQ, bot=None, event=None)
    r = await _tool_write({
        "name": name,
        "code": code,
        "description": "测试用：打印 ok 和参数",
    })
    print(f"[write] {r}")
    assert fn_name in llm_mod.TOOL_HANDLERS, "工具没注册进 TOOL_HANDLERS"
    assert any(s.get("function", {}).get("name") == fn_name for s in llm_mod.TOOLS_SPEC), \
        "工具没进 TOOLS_SPEC"
    print(f"✅ 已注册 {fn_name}（带 mytool_ 前缀 = 她自己造的工具）")

    # 3) 跑一次 → stdout 应该带 ok 42，且结果落 log
    r = await _tool_run({"name": name, "args": {"x": 42}})
    print(f"[run] {r}")
    assert "ok 42" in r, f"运行结果不对：{r}"

    # 4) 读执行 log（应该能看到 exit=0 和 args）
    r = await _tool_file({"action": "read", "path": f"runs/{name}.log"})
    print(f"[read-log 前80字] {r[:80]!r}")
    assert "exit=0" in r, "log 里没有 exit=0"

    # 5) 读自己写的代码
    r = await _tool_file({"action": "read", "path": f"tools/{name}.py"})
    assert "sys.argv" in r, "读到的代码不对"
    print("✅ 能读回自己写的代码")

    # 6) 写爬来的 data
    r = await _tool_file({"action": "write", "path": "data/t_smoke.txt", "content": "hello"})
    print(f"[write-data] {r}")
    assert (DATA_DIR / "t_smoke.txt").exists()

    # 7) 路径越界要拦
    r = await _tool_file({"action": "read", "path": "../../.env"})
    assert "越界" in r or "只允许" in r, f"路径越界没拦住：{r}"
    print("✅ 路径越界被拦截")

    # 清理测试痕迹
    for p in (TOOLS_DIR / f"{name}.py", TOOLS_DIR / f"{name}.meta.json",
              RUNS_DIR / f"{name}.log", DATA_DIR / "t_smoke.txt"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
    llm_mod.TOOLS_SPEC[:] = [s for s in llm_mod.TOOLS_SPEC
                             if s.get("function", {}).get("name") != fn_name]
    llm_mod.TOOL_HANDLERS.pop(fn_name, None)
    llm_mod.TOOL_SCOPES.pop(fn_name, None)
    print("✅ 全部通过（测试工具 t_smoke 已清理）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

