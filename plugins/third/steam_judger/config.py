import os

from nonebot import get_driver
from pydantic import BaseModel, Field


class Config(BaseModel):
    """Steam Talk Plugin Config"""
    steam_api_key: str = Field(default="", description="Steam Web API密钥")
    ai_api_key: str = Field(default="", description="AI接口的API密钥")
    ai_api_base: str = Field(default="", description="AI接口的完整URL")
    ai_model: str = Field(default="deepseek-v3-0324", description="使用的AI模型")


def _pick(name: str, default: str = "") -> str:
    """
    取配置：优先 nonebot 配置（.env 里的自定义字段），再退回真实环境变量。

    为什么要兜一层 os.getenv：
      docker-compose 的 `env_file: .env` 会把变量注入容器环境，
      但 nonebot 的 Config 是否把它们暴露成属性取决于 pydantic 的 extra 设置，
      不同版本行为不一致。两边都读一遍最稳。
      空字符串也当没配（避免 `.env` 里写了 `KEY=` 空值把环境变量盖掉）。
    """
    value = getattr(get_driver().config, name, "") or os.getenv(name.upper(), "") or default
    return value.strip()


plugin_config = Config.model_validate(
    {
        "steam_api_key": _pick("steam_api_key"),
        "ai_api_key": _pick("ai_api_key"),
        "ai_api_base": _pick("ai_api_base"),
        "ai_model": _pick("ai_model", Config().ai_model),
    }
)

# 启动时把关键配置的长度打出来：403/401 到底是「没读到 key」还是「key 无效」，
# 看这一行就知道，不用再去猜。
from nonebot.log import logger  # noqa: E402

_key = plugin_config.steam_api_key
logger.info(
    f"[steam] steam_api_key={'已配置 len=%d %s…' % (len(_key), _key[:6]) if _key else '空'}"
    f"  ai_model={plugin_config.ai_model!r}"
)
