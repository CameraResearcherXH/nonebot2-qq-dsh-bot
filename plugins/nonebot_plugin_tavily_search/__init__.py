from nonebot import get_driver
from nonebot import on_command, require
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.log import logger

from .config import Config
from .search import smart_web_search

# 读取 NoneBot 配置
plugin_config = Config(**get_driver().config.model_dump())

# 注册命令，别名 /搜索
tavily_cmd = on_command("tavily", aliases={"搜索"}, priority=5, block=True)

# 能力开关（/技能 关 搜索）：这个插件独立注册命令，不走工具分发，得单独挂
try:
    from services import skill_switch as _sk
    _sk.gate(tavily_cmd, "web_search", "搜索")
except Exception:
    pass

@tavily_cmd.handle()
async def handle_tavily(args: Message = CommandArg()):
    query = args.extract_plain_text().strip()
    if not query:
        await tavily_cmd.finish("请输入搜索关键词，例如：/tavily DeepSeek 最新进展")

    # 发送“正在搜索”的占位消息
    msg = await tavily_cmd.send(f"🔍 正在搜索：{query} ...")

    try:
        response = await smart_web_search(
            query=query,
            tavily_key=plugin_config.tavily_api_key,
            serper_key=plugin_config.serper_api_key,
            max_results=plugin_config.tavily_max_results,
            search_depth=plugin_config.tavily_search_depth,
        )
    except Exception as e:
        logger.error(f"搜索失败: {e}")
        await tavily_cmd.finish(f"❌ 搜索失败：{e}")

    # 格式化渲染
    lines = [f"📌 来源：{response.source}", f"🔎 查询：{response.query}"]

    if response.answer:
        lines.append(f"\n💡 摘要：{response.answer}")

    if response.results:
        lines.append("\n📋 搜索结果：")
        for i, r in enumerate(response.results[:5], 1):
            lines.append(f"{i}. [{r.title}]({r.url})")
            # 截断过长的摘要，避免消息过长
            snippet = r.content[:120] + "..." if len(r.content) > 120 else r.content
            lines.append(f"   {snippet}")
    else:
        lines.append("\n⚠️ 未找到相关结果")

    # 发送结果（适配 NoneBot 的 markdown 或纯文本）
    await tavily_cmd.finish("\n".join(lines))
