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

# 运行档位（2026-11-25，和 GROUP_WATCH_MODE 正交）：
#   GROUP_WATCH_MODE 管「回不回」（接话范围）；POWER_MODE 管「算不算」（成本档位）。
#   full = 所有段全开；eco = 关掉花钱的判定类调用（审核/图书馆/主动/表达蒸馏），
#   **L0 永远不省**：硬信号、收发件人结构信号、上下文/白月光/风格语料落盘、安静开关。
POWER_MODE = (os.getenv("POWER_MODE", "full") or "full").strip().lower()
if POWER_MODE not in ("full", "eco"):
    POWER_MODE = "full"
_ECO_OFF = ("audit", "library", "proactive", "expression")


def power_on(stage: str) -> bool:
    """这一档位下，某一段要不要跑。未知 stage 一律放行（默认不误伤）。"""
    if POWER_MODE != "eco":
        return True
    return str(stage or "") not in _ECO_OFF
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
# 2026-11-27：主聊天输出 token 也走 provider 注册表（以前 llm.py 写死 800，
# 用户改 .env 的 GLM_MAX_TOKENS / ROLEPLAY_MAX_TOKENS 都不生效）。
ROLEPLAY_MAX_TOKENS = int(_roleplay.get("max_tokens")
                          or tuning.i("ROLEPLAY_MAX_TOKENS"))
ROLEPLAY_MAX_TOKENS_FIELD = str(
    _roleplay.get("max_tokens_field") or "max_tokens")
# ⚠️ MAX_HISTORY 已废弃删除：历史轮次由 chat 的 MAX_GROUP_RECENT（群聊上下文条数）
#    + prompt_builder 的预算分配（PROMPT_BUDGET / BLOCK_BUDGET）共同控制。
#    .env 里留着 MAX_HISTORY 也不会有任何作用。
# 2026-11-25：MAX_REPLY_CHARS 已删除（从来没有读点；单泡/气泡数由
# OUT_BUBBLE_CHARS / OUT_BUBBLES_MAX 控制）。.env 里留着也不生效。
# ⚠️ 不要在这里写默认密钥：源码会进镜像 / 进 git。
#    没配就是空，调用方自己判断（语音功能会降级，而不是偷偷用作者的 key）。
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
ALIYUN_VOICE_TYPE = "longhuhu_v3"