from nonebot import get_plugin_config
from pydantic import Extra, BaseModel


class Config(BaseModel, extra=Extra.ignore):
    pixr_local_proxy: str
    pixr_rsshub: str = "https://rsshub.app/"
    pixr_retry: int = 3
    # pixiv 网页版登录 cookie（2026-11-14 新增）
    # 搜图接口 /ajax/search/artworks 需要一个 www.pixiv.net 的会话 cookie。
    # 实测**匿名 PHPSESSID 也能用**（访问一次 pixiv.net 就会发一个），
    # 所以留空也能跑；留空时如果被 403，就按 scripts/pixiv_get_cookie.py 的说明去拿。
    # 只填 PHPSESSID 的值或 `PHPSESSID=xxx` 都行，代码会自动补 name。
    pixr_cookie: str = ""


config = get_plugin_config(Config)

config.pixr_rsshub = config.pixr_rsshub[:-1] if config.pixr_rsshub[-1] == "/" else config.pixr_rsshub