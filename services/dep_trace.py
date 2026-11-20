"""
把 nonebot 依赖解析里被 anyio TaskGroup 吞掉的子异常打出来。

背景（2026-11-13 排 petpet 时踩的坑）：
  nonebot 的 `Dependent.solve()` 是这样写的——

      async with anyio.create_task_group() as tg:
          for param in self.params:
              tg.start_soon(...)

  任何一个依赖抛异常，都会被 anyio 包成
      ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
  而 nonebot 在 `matcher.run()` 里用 exceptiongroup.catch() 处理时又会
      raise unhandled from exc.__cause__
  结果日志里只剩那一行 ExceptionGroup，**连子异常的类型都没有**。
  排查时完全不知道是哪个依赖、哪一行挂的——「指令有反应但不出图」全是这个表象。

这里给 `Dependent.solve` 套一层：出错时把**叶子异常**连同依赖函数名打出来，
再原样抛出，不改变任何行为。内层的那次调用会最先命中，所以日志里最上面那条
就是真正的根因。

⚠️ 必须**递归拆 ExceptionGroup**（2026-11-13 二次踩）：
   loguru 的 formatter 不会展开 ExceptionGroup 的子异常，
   `logger.opt(exception=group)` 打出来还是只有那行
   "unhandled errors in a TaskGroup (1 sub-exception)"。
   真正有用的信息（异常类型、出错的那一行）全在 `group.exceptions` 里，
   必须自己递归取到叶子逐个打。

⚠️ 只捕获 `Exception`，**不能捕 `BaseException`**（2026-11-13 踩过）：
   nonebot 的 FinishedException / SkippedException / PausedException /
   StopPropagation 全部继承自 BaseException（就是为了不被 `except Exception` 接住）。
   matcher.finish() / matcher.skip() 在依赖里是**正常控制流**，
   捕 BaseException 会把每次正常 finish 都打成一条 ERROR + 完整 traceback，
   日志瞬间被刷爆，看起来就像「拿不到消息信息了」。

用法：bot.py 里 `from services.dep_trace import install; install()`，
或设环境变量 `DEP_TRACE=0` 关掉。
"""
import os

from nonebot.dependencies import Dependent
from nonebot.log import logger

_installed = False


def _iter_leaves(exc: BaseException):
    """把 ExceptionGroup 递归拆成叶子异常。叶子自己带着 __traceback__。"""
    sub = getattr(exc, "exceptions", None)
    if isinstance(sub, (list, tuple)) and sub:
        for item in sub:
            yield from _iter_leaves(item)
    else:
        yield exc


def _call_name(dep) -> str:
    call = getattr(dep, "call", None)
    name = (
        getattr(call, "__qualname__", None)
        or getattr(call, "__name__", None)
        or repr(call)
    )
    return f"{getattr(call, '__module__', '?')}.{name}"


def install() -> None:
    global _installed
    if _installed:
        return
    if os.getenv("DEP_TRACE", "1").strip() in ("0", "false", "False"):
        logger.info("[DEP-TRACE] 已按 DEP_TRACE=0 关闭")
        return

    orig_solve = Dependent.solve

    async def solve_with_trace(self, **kwargs):
        try:
            return await orig_solve(self, **kwargs)
        except Exception as e:  # noqa: BLE001 - 只观测，必须原样抛出
            # 同一次异常会从内层一路往外层冒，每层 solve 都会经过这里，
            # 而每层 anyio 都会**新建**一个 ExceptionGroup 把内层包住，
            # 所以外层 group 是不同对象、但叶子是同一批。
            # 给叶子打标记，只让最先命中的那层打日志，否则同一个根因刷 N 遍。
            leaves = [
                leaf
                for leaf in _iter_leaves(e)
                if not getattr(leaf, "_dep_traced", False)
            ]
            if not leaves:
                raise
            for leaf in leaves:
                try:
                    setattr(leaf, "_dep_traced", True)
                except Exception:  # noqa: BLE001
                    pass

            logger.error(
                f"[DEP-TRACE] 依赖解析失败：{_call_name(self)} "
                f"→ {type(e).__name__}（拆出 {len(leaves)} 个叶子异常）"
            )
            for i, leaf in enumerate(leaves, 1):
                logger.opt(exception=leaf).error(
                    f"[DEP-TRACE] 真实根因 #{i}/{len(leaves)}："
                    f"{type(leaf).__name__}: {leaf}"
                )
            raise

    Dependent.solve = solve_with_trace
    _installed = True
    logger.info("[DEP-TRACE] 已挂上 Dependent.solve 观测（依赖报错会拆出真实根因）")
