from core import tuning
import os

BOT_MODE = os.getenv("BOT_MODE", "mock").lower()
MOCK_REPLY = os.getenv("MOCK_REPLY", "我是测试机器人~")

# 算力机总开关（2026-11-24 用户要求）：
#   1 = 走 node.py（画图/翻唱/语音/视频理解都排任务给算力机）
#   0 = 不加载本项目的 draw / song_cover，也不注册这些 function 工具；
#       画图/唱歌请把现成的 nonebot 插件放进 plugins/third（本机跑）。
NODE_COMPUTE = os.getenv("NODE_COMPUTE", "1").strip().lower() not in (
    "0", "false", "off", "no")

# 改代码的方式（2026-11-24 用户要求）：
#   self_tool = 原来的「她自己写 Python 工具」（self_tool_write/run/file）
#   harness   = 接 DeepSeek Harness 会话（可以真的改仓库代码），self_tool 自动关闭
#   off       = 两个都不开
CODE_AGENT = os.getenv("CODE_AGENT", "self_tool").strip().lower()

# 群聊观察范围（2026-11-24 用户要求）：
#   full = 现状：所有群消息都进判定（关注全部群聊）
#   lite = 省流：只观察/记录，只有「硬信号」才入队回（@她 / 引用她 / 喊名字 / 收件人是她）；
#          接话时只拉附近 8 条上下文 + 记忆。私聊不受影响。
GROUP_WATCH_MODE = (os.getenv("GROUP_WATCH_MODE", "full") or "full").strip().lower()
if GROUP_WATCH_MODE not in ("full", "lite"):
    GROUP_WATCH_MODE = "full"
HARNESS_ENABLED = CODE_AGENT == "harness"
# ⚠️ XAI_* 现在只给「三方判读 / 审核 / 参考 / 表达学习」等辅助调用用，
#    默认仍是 DeepSeek（deepseek-chat）。
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_BASE_URL = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-3-mini")
PROXY = os.getenv("XAI_PROXY", "")

# 角色扮演（主聊天）单独一套 provider（用户 2026-11-23：
# 「角色扮演用 deepseek 4.1-flash（API 模型名 deepseek-flash），
#   三方判读用 deepseek-chat；GLM 只做视觉」）。
# 2026-11-24：改成统一走 providers 注册表 —— 用户可以在 .env 里把主聊天
# 换成 glm / qwen / 自己的中转站，不用改代码。
from .providers import resolve as _resolve_provider  # noqa: E402

_roleplay = _resolve_provider("roleplay")
ROLEPLAY_API_KEY = _roleplay["api_key"]
ROLEPLAY_BASE_URL = _roleplay["base_url"]
ROLEPLAY_MODEL = _roleplay["model"]
# ⚠️ MAX_HISTORY 已废弃删除：历史轮次由 chat 的 MAX_GROUP_RECENT（群聊上下文条数）
#    + prompt_builder 的预算分配（PROMPT_BUDGET / BLOCK_BUDGET）共同控制。
#    .env 里留着 MAX_HISTORY 也不会有任何作用。
MAX_REPLY_CHARS = tuning.i("MAX_REPLY_CHARS")
# ⚠️ 不要在这里写默认密钥：源码会进镜像 / 进 git。
#    没配就是空，调用方自己判断（语音功能会降级，而不是偷偷用作者的 key）。
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
ALIYUN_VOICE_TYPE = "longhuhu_v3"