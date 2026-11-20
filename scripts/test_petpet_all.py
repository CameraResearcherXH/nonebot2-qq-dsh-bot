"""
遍历 petpet 全部模板，检查哪些能出图、哪些会抛异常。

为什么要有这个脚本：
  · 在 QQ 里一个个手测太慢（121 个模板）
  · 更关键的是——nonebot 的依赖解析跑在 anyio TaskGroup 里，模板抛的异常会被
    包成 "ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)"，
    日志里连异常类型都看不到。
    这个脚本**直接调用 meme.func(**kwargs)**，绕开依赖注入，异常原样暴露。

用法（在容器里跑；docker-compose 已挂载 ./scripts:/app/scripts）：
    docker exec qq-grok-bot python scripts/test_petpet_all.py            # 全部
    docker exec qq-grok-bot python scripts/test_petpet_all.py 兑换券      # 只测一个
    docker exec qq-grok-bot python scripts/test_petpet_all.py --fail-only # 只看失败的
"""
import asyncio
import inspect
import os
import sys
import traceback
from io import BytesIO
from pathlib import Path

# 用 `python scripts/xxx.py` 跑时 sys.path[0] 是 /app/scripts，导不到 plugins 包，
# 这里把项目根目录（scripts 的上一级）塞进 sys.path。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
# nonebot.init() 从 cwd 找 .env，切到项目根目录保证配置一致
os.chdir(_ROOT)

import nonebot

# petpet 的 config.py 在 import 时就调 get_driver()，所以必须先 init
nonebot.init()

# 关键：petpet 的 __init__.py 里有 require("nonebot_plugin_imageutils")。
# nonebot 的 require() 要求该模块**尚未被 import**，否则会抛
# "Module ... is not loaded as a plugin! Make sure not to import it before loading."
# 所以这里必须走 load_plugin 先把它注册成插件，再 import 取符号。
nonebot.load_plugin("nonebot_plugin_imageutils")

from nonebot_plugin_imageutils import BuildImage  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

from plugins.third.petpet.data_source import memes  # noqa: E402
from plugins.third.petpet.utils import UserInfo  # noqa: E402


def make_test_avatar(size: int = 640) -> BuildImage:
    """造一张纯色测试头像，避免脚本依赖网络去下真头像。"""
    img = PILImage.new("RGB", (size, size), (120, 160, 200))
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return BuildImage.open(buf)


def make_test_user() -> UserInfo:
    u = UserInfo(qq="10001", group="", name="测试用户", gender="unknown")
    u.img = make_test_avatar()
    return u


def build_kwargs(func, img: BuildImage, user: UserInfo) -> dict:
    """
    按类型注解给模板函数造参数。

    模板的形参默认值是 Depends 对象（UserImg() / Arg() / Args(0,10) …），
    直接调 func() 会拿到 Depends 实例而不是值，所以这里按**注解类型**注入。

    注意几个坑：
      · List[BuildImage]（UserImgs(1,2)）必须给 [img]，给 [] 会在模板里 user_imgs[0] IndexError
      · 很多模板的 arg=NoArg() **没有注解**，注解是 inspect._empty，
        给 None 会在字符串操作里炸，按参数名兜底成 "" / []
      · PIL 的 IMG 注解 str() 出来是 "<class 'PIL.Image.Image'>"，不是 "IMG"
    """
    sig = inspect.signature(func)
    kwargs = {}
    for name, p in sig.parameters.items():
        ann_s = str(p.annotation)

        if "BuildImage" in ann_s:
            # List[BuildImage] / Tuple[BuildImage, ...] 一律给 [img]
            kwargs[name] = [img] if ("List" in ann_s or "list" in ann_s or "Tuple" in ann_s) else img
        elif "UserInfo" in ann_s:
            # users: List[UserInfo] → [user]；user: UserInfo → user
            kwargs[name] = [user] if ("List" in ann_s or "list" in ann_s or "Tuple" in ann_s) else user
        elif "Image.Image" in ann_s or ann_s == "IMG":
            kwargs[name] = img.image
        elif "int" in ann_s and "str" not in ann_s:
            kwargs[name] = 1
        elif "List" in ann_s or "list" in ann_s:
            kwargs[name] = []
        elif "str" in ann_s:
            kwargs[name] = ""
        else:
            # 注解缺失：按参数名猜
            if name in ("args", "texts", "strs"):
                kwargs[name] = []
            elif name in ("users", "user_imgs", "imgs"):
                kwargs[name] = [img] if "img" in name else [user]
            else:
                kwargs[name] = ""
    return kwargs


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    fail_only = "--fail-only" in sys.argv
    only = args[0] if args else ""

    img = make_test_avatar()
    user = make_test_user()

    ok, susp, fail = [], [], []
    for meme in memes:
        if only and only not in meme.name and only not in "/".join(meme.keywords):
            continue
        try:
            kwargs = build_kwargs(meme.func, img, user)
            res = meme.func(**kwargs)
            # dianzhongdian 是 async def（内部要 await 下载图片），
            # 不 await 的话拿到的是 coroutine 对象，会被误判成"成功但 0KB"。
            if inspect.iscoroutine(res):
                res = asyncio.run(res)
            if isinstance(res, str):
                ok.append((meme.name, f"返回文本：{res[:40]}"))
            else:
                data = res.getvalue() if hasattr(res, "getvalue") else b""
                if data:
                    ok.append((meme.name, f"{len(data) // 1024} KB"))
                else:
                    susp.append((meme.name, f"输出为空（{type(res).__name__}）"))
        except Exception as e:  # noqa: BLE001
            fail.append((meme.name, f"{type(e).__name__}: {e}", traceback.format_exc()))

    total = len(ok) + len(susp) + len(fail)
    print(f"\n===== 共 {total} 个模板：{len(ok)} 成功 / {len(susp)} 可疑 / {len(fail)} 失败 =====\n")

    if not fail_only:
        print("--- 成功 ---")
        for n, info in ok:
            print(f"  OK   {n:<26} {info}")

    if susp:
        print("\n--- 可疑（没抛异常但没出图）---")
        for n, msg in susp:
            print(f"  WARN {n:<26} {msg}")

    if fail:
        print("\n--- 失败 ---")
        for n, msg, _ in fail:
            print(f"  FAIL {n:<26} {msg}")
        print("\n--- 每个失败的完整 traceback ---")
        for n, msg, tb in fail:
            print(f"\n### {n}  {msg}\n{tb}")

    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
