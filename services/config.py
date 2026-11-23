from core import tuning
import os

BOT_MODE = os.getenv("BOT_MODE", "mock").lower()
MOCK_REPLY = os.getenv("MOCK_REPLY", "我是测试机器人~")
# ⚠️ XAI_* 现在只给「三方判读 / 审核 / 参考 / 表达学习」等辅助调用用，
#    默认仍是 DeepSeek（deepseek-chat）。
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
XAI_BASE_URL = os.getenv("XAI_BASE_URL", "https://api.x.ai/v1")
XAI_MODEL = os.getenv("XAI_MODEL", "grok-3-mini")
PROXY = os.getenv("XAI_PROXY", "")

# 角色扮演（主聊天）单独一套 provider（用户 2026-11-23：
# 「角色扮演用 deepseek 4.1-flash（API 模型名 deepseek-flash），
#   三方判读用 deepseek-chat；GLM 只做视觉」）。
ROLEPLAY_API_KEY = (os.getenv("ROLEPLAY_API_KEY", "")
                    or os.getenv("XAI_API_KEY", "")
                    or os.getenv("GLM_API_KEY", ""))
ROLEPLAY_BASE_URL = os.getenv(
    "ROLEPLAY_BASE_URL", "https://api.deepseek.com").rstrip("/")
ROLEPLAY_MODEL = os.getenv("ROLEPLAY_MODEL", "deepseek-flash")
# ⚠️ MAX_HISTORY 已废弃删除：历史轮次由 chat 的 MAX_GROUP_RECENT（群聊上下文条数）
#    + prompt_builder 的预算分配（PROMPT_BUDGET / BLOCK_BUDGET）共同控制。
#    .env 里留着 MAX_HISTORY 也不会有任何作用。
MAX_REPLY_CHARS = tuning.i("MAX_REPLY_CHARS")
# ⚠️ 不要在这里写默认密钥：源码会进镜像 / 进 git。
#    没配就是空，调用方自己判断（语音功能会降级，而不是偷偷用作者的 key）。
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
ALIYUN_VOICE_TYPE = "longhuhu_v3"