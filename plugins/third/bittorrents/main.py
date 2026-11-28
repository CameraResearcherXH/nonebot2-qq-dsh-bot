import os
import re
import base64
import json
import urllib.parse
from pathlib import Path
from typing import List
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
from nonebot import on_command, logger, get_driver, get_bot
from nonebot.adapters import Event, Message
from nonebot.params import CommandArg

try:
    from pydantic import BaseModel
    from nonebot import get_plugin_config
except ImportError:
    from pydantic import BaseSettings as BaseModel

try:
    from nonebot.adapters.onebot.v11 import Bot, MessageSegment
    from nonebot.adapters.onebot.v11 import Message as OB11Message
except ImportError:
    Bot = None
    MessageSegment = None
    OB11Message = None

# ========== 1. 配置映射类（从配置文件读取参数） ==========
@dataclass
class MagnetConfig:
    """磁力搜索配置类（全部可用环境变量覆盖，换站不用改代码）"""
    base_url: str = os.getenv("BT_BASE_URL", "https://clg38.xyz")   # 主站
    search_path: str = os.getenv("BT_SEARCH_PATH", "/search")       # 搜索接口路径
    max_results: int = int(os.getenv("BT_MAX_RESULTS", "3"))        # 最大返回结果数
    request_timeout: int = int(os.getenv("BT_TIMEOUT", "15"))       # 请求超时（秒）

    def __post_init__(self):
        # 处理base_url结尾的/（统一格式：不带结尾/）
        if self.base_url.endswith("/"):
            self.base_url = self.base_url.rstrip("/")
        # 处理search_path开头的/（统一格式：带开头/）
        if not self.search_path.startswith("/"):
            self.search_path = f"/{self.search_path}"

    def candidates(self) -> List[str]:
        """
        主站 + 备用镜像，按顺序试。

        为什么需要：这类站域名换得很勤，写死一个地址就等于写死一个失效时间。
        BT_MIRRORS 用逗号分隔，主站挂了自动切下一个。
        """
        out = [self.base_url]
        for m in os.getenv("BT_MIRRORS", "").split(","):
            m = m.strip().rstrip("/")
            if m and m not in out:
                out.append(m)
        return out

# ========== 2. 核心工具类 ==========
class MagnetUtils:
    @staticmethod
    def decrypt_base64(encrypted_str: str) -> str:
        """Base64解密"""
        try:
            encrypted_str = encrypted_str.ljust(len(encrypted_str) + (4 - len(encrypted_str) % 4) % 4, '=')
            decoded = base64.b64decode(encrypted_str).decode('utf-8', errors='ignore')
            return urllib.parse.unquote(decoded)
        except Exception as e:
            logger.warning(f"Base64解密失败：{str(e)}")
            return ""

    @staticmethod
    def get_full_url(base_url: str, relative_url: str) -> str:
        """拼接完整URL"""
        if relative_url.startswith("http"):
            return relative_url
        if relative_url.startswith("./"):
            return f"{base_url}/{relative_url[2:]}"
        if relative_url.startswith("/"):
            return f"{base_url}{relative_url}"
        return f"{base_url}/{relative_url}"

    @staticmethod
    def encode_search_word(keyword: str) -> str:
        """搜索关键词匹配站点加密逻辑"""
        return base64.urlsafe_b64encode(keyword.encode("utf-8")).decode("ascii").rstrip("=")

    @staticmethod
    def extract_atob_blob(html: str) -> str | None:
        """提取页面中 window.atob("...") 加密内容"""
        m = re.search(r'window\.atob\(\s*["\']([^"\']+)["\']', html)
        return m.group(1) if m else None

    @staticmethod
    def get_sort_param(sort_keyword: str) -> str:
        """
        排序关键词
        """
        sort_mapping = {
            "相关度": "rele",
            "大小": "length",
            "文件大小": "length",
            "热门": "hits",
            "热门程度": "hits",
            "时间": "time",
            "最新": "time",
        }
        # 提高鲁棒性
        sort_keyword = sort_keyword.strip().lower()
        for key, value in sort_mapping.items():
            if key.lower() == sort_keyword:
                return value
        # 匹配不到则回退默认
        return "rele"

# ========== 3. whatslink.info 预览服务 ==========
class WhatsLinkService:
    """whatslink.info 预览服务"""

    API_URL = "https://whatslink.info/api/v1/link"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def get_preview(self, magnet_url: str) -> dict | None:
        """查询磁链预览信息，失败时返回 None"""
        try:
            resp = await self.client.get(self.API_URL, params={"url": magnet_url})
            if resp.status_code != 200:
                logger.warning(f"whatslink API 返回 {resp.status_code}")
                return None
            return resp.json()
        except Exception as e:
            logger.warning(f"whatslink API 请求失败：{e}")
            return None


# ========== 4. 工具函数 ==========
def _format_size(size_bytes) -> str:
    """将字节数格式化为可读大小"""
    if not size_bytes or size_bytes <= 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


# ========== 5. 核心搜索服务 ==========
class MagnetSearchService:
    def __init__(self, config: MagnetConfig, client: httpx.AsyncClient):
        self.config = config
        self.client = client

    async def search(self, keyword: str, sort_param: str = "rele") -> List[dict]:
        """搜索逻辑：返回包含原始数据的 dict 列表"""
        results = []

        try:
            # ========== 构造搜索URL（主站 + 备用镜像轮询） ==========
            encoded_word = MagnetUtils.encode_search_word(keyword)
            blob = None
            decrypted_html = ""
            for _base in self.config.candidates():
                search_url = f"{_base}{self.config.search_path}?word={encoded_word}&sort={sort_param}"
                logger.debug(f"GET请求：{search_url}")
                try:
                    response = await self.client.get(search_url)
                except Exception as e:
                    logger.warning(f"[BT] {_base} 连不上：{e}")
                    continue
                logger.debug(
                    f"响应状态码：{response.status_code} 最终URL：{response.url}"
                )
                if response.status_code != 200:
                    continue

                blob = MagnetUtils.extract_atob_blob(response.text)
                if blob:
                    decrypted_html = MagnetUtils.decrypt_base64(blob)
                elif "SearchListTitle_result_title" in response.text:
                    # 站点改版：结果不再塞进 window.atob，直接是明文 HTML。
                    # 后面的解析逻辑完全一样，只是少一步 base64 解码。
                    logger.info(f"[BT] {_base} 没有 atob 块，按明文 HTML 解析")
                    decrypted_html = response.text
                else:
                    logger.warning(f"[BT] {_base} 没有结果块（结构变了/被拦），试下一个")
                    continue
                self.config.base_url = _base      # 详情页也用这个站来拼
                break

            # ========== 提取并解密搜索结果块（window.atob + decodeURIComponent） ==========
            if not decrypted_html:
                logger.error("主站和镜像都拿不到结果（全挂了，或页面结构变了）")
                return []

            soup = BeautifulSoup(decrypted_html, "lxml")
            result_container = soup.find("ul", id="Search_list_wrapper")
            if not result_container:
                logger.error("网站不可用")
                return []

            detail_links = []
            # 遍历结果：最多取配置的max_results条（只取直接子 li，排除分页）
            for idx, li in enumerate(result_container.find_all("li", recursive=False)):
                if idx >= self.config.max_results:
                    break

                title_a = li.find("a", class_="SearchListTitle_result_title")
                if not title_a:
                    continue
                href = title_a.get("href", "").strip()
                if not href:
                    continue
                title = title_a.get_text(strip=True) or f"搜索结果{idx+1}"

                info = li.find("div", class_="Search_list_info")
                info_text = info.get_text(" ", strip=True) if info else ""
                size = re.search(r"文件大小：\s*([0-9.]+\s*[GMK]B)", info_text)
                size = size.group(1).strip() if size else "未知大小"
                create_time = re.search(r"创建时间：\s*(\d{4}-\d{2}-\d{2})", info_text)
                create_time = create_time.group(1).strip() if create_time else "未知时间"

                detail_links.append({
                    "url": MagnetUtils.get_full_url(self.config.base_url, href),
                    "title": title,
                    "size": size,
                    "create_time": create_time
                })

            if not detail_links:
                return []

            # ========== 解析详情页提取磁力链接 ==========
            for link in detail_links:
                try:
                    detail_resp = await self.client.get(link["url"])
                    magnet_link = None

                    # 详情页同样可能带 atob，也可能已经是明文（站点改版）
                    detail_blob = MagnetUtils.extract_atob_blob(detail_resp.text)
                    detail_html = (
                        MagnetUtils.decrypt_base64(detail_blob)
                        if detail_blob
                        else detail_resp.text
                    )
                    detail_soup = BeautifulSoup(detail_html, "lxml")
                    magnet_a = detail_soup.find("a", class_="Information_magnet")
                    if magnet_a:
                        magnet_link = magnet_a.get("href", "").strip() or None
                    if not magnet_link:
                        magnet_match = re.search(
                            r"magnet:\?xt=urn:btih:[a-fA-F0-9]{40,}[^\"']*", detail_html
                        )
                        if magnet_match:
                            magnet_link = magnet_match.group().strip()

                    results.append({
                        "title": link["title"],
                        "magnet_link": magnet_link,
                        "size": link["size"],
                        "create_time": link["create_time"],
                    })
                except Exception as e:
                    results.append({
                        "title": link["title"],
                        "magnet_link": None,
                        "size": link["size"],
                        "create_time": "",
                        "error": str(e)[:30],
                    })

        except Exception as e:
            logger.error(f"搜索异常：{str(e)}")
            results = [{"error": f"搜索失败：{str(e)[:50]}"}]

        return results

# ========== 5.5 BT 总开关（bt on / bt off，主人/管理员专属） ==========
# 状态落盘 data/bittorrents/switch.json，重启不丢。
_SWITCH_FILE = Path("data") / "bittorrents" / "switch.json"
_SWITCH_ON_WORDS = {"on", "开", "开启", "恢复", "启用", "1", "true"}
_SWITCH_OFF_WORDS = {"off", "关", "关闭", "停", "暂停", "禁用", "0", "false"}


def _load_switch() -> bool:
    try:
        if _SWITCH_FILE.exists():
            d = json.loads(_SWITCH_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return bool(d.get("on", True))
    except Exception:
        pass
    return True


def _save_switch(on: bool) -> None:
    try:
        _SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SWITCH_FILE.write_text(
            json.dumps({"on": bool(on)}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"[BT] 开关状态写入失败: {type(e).__name__}: {e}")


BT_ENABLED = _load_switch()


def _is_privileged(event) -> bool:
    """开关只有主人/管理员能动。services.access 不可用就退回「仅超级用户」。"""
    try:
        from services import access
        return access.level_of(getattr(event, "user_id", "")) != access.LEVEL_MEMBER
    except Exception:
        try:
            return str(getattr(event, "user_id", "")) in (get_driver().config.superusers or set())
        except Exception:
            return False


def _toggle_switch(on: bool) -> None:
    global BT_ENABLED
    BT_ENABLED = bool(on)
    _save_switch(BT_ENABLED)
    logger.info(f"[BT] 磁力搜索总开关 → {'on' if BT_ENABLED else 'off'}")


# ========== 6. 配置类定义 ==========
class Config(BaseModel):
    magnet_base_url: str = "https://clg38.xyz"
    magnet_search_path: str = "/search"
    magnet_max_results: int = 3
    magnet_request_timeout: int = 15
    magnet_enable_preview: bool = True

try:
    plugin_config = get_plugin_config(Config)
except:
    # 兜底从全局配置读取
    driver = get_driver()
    global_config = driver.config
    plugin_config = Config(**global_config.dict())

# 初始化配置类
magnet_config = MagnetConfig(
    base_url=plugin_config.magnet_base_url,
    search_path=plugin_config.magnet_search_path,
    max_results=plugin_config.magnet_max_results,
    request_timeout=plugin_config.magnet_request_timeout
)
enable_preview = plugin_config.magnet_enable_preview

# 统一创建 HTTP 客户端
magnet_headers = {
    # 原来的 UA 是「Android 2.2 / 2010 年代」，这类站现在普遍按 UA 分流：
    # 老 UA 会被丢到一个「网址屏蔽提醒 / 请用导航站」的提示页，页面里没有结果块。
    # 换成桌面 Chrome 的 UA，拿到的才是真正的搜索页。
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Origin": magnet_config.base_url,
    "Referer": magnet_config.base_url,
}
_magnet_client = httpx.AsyncClient(
    headers=magnet_headers,
    timeout=magnet_config.request_timeout,
    # ⚠️ 必须跟随重定向（2026-11-13 修）：
    #    这类站换域名很勤，主站常常 301/302 跳到新域名。
    #    follow_redirects=False 时拿到的只是一个 30x 空 body，
    #    于是 extract_atob_blob 返回 None → 日志「没有加密结果块（结构变了？）」
    #    → 看起来像站点挂了，其实人家只是跳转了。
    follow_redirects=True,
    verify=False,
)
_preview_client = httpx.AsyncClient(
    timeout=magnet_config.request_timeout,
    follow_redirects=True
)

search_service = MagnetSearchService(magnet_config, _magnet_client)
whatslink_service = WhatsLinkService(_preview_client)

logger.info(f"磁力搜索插件初始化完成，使用站点：{magnet_config.base_url}{magnet_config.search_path}")

# 注册关闭钩子，释放 httpx 客户端
@get_driver().on_shutdown
async def _cleanup():
    await _magnet_client.aclose()
    await _preview_client.aclose()


# ========== 7. 注册命令处理器 ==========
def _format_result(idx: int, res: dict) -> str:
    """格式化单条搜索结果"""
    if "error" in res and not res.get("magnet_link"):
        title = res.get("title", "未知")
        size = res.get("size", "未知")
        return f"\n===== 结果 {idx} =====\n标题：{title}\n解析失败：{res['error']}\n文件大小：{size}"

    return (
        f"\n===== 结果 {idx} =====\n"
        f"标题：{res['title']}\n"
        f"磁力链接：{res['magnet_link'] or '未提取到'}\n"
        f"文件大小：{res['size']}\n"
        f"收录时间：{res['create_time']}"
    )


def _get_sender_info(event: Event):
    """
    合并转发节点用的「显示身份」。

    ⚠️ 不要用发送者本人的 QQ 号 / 昵称（2026-11-13 用户要求）：
      转发消息会把节点署名渲染成那个 QQ 的头像和昵称，等于把用户信息
      跟磁力链接一起挂在转发卡片上 —— 既暴露隐私，也更容易被关联风控。
      所以默认用 uin=0 + 中性昵称，QQ 客户端会画成默认/匿名头像。

    想换成别的显示名，用环境变量覆盖：
      BT_FORWARD_UIN=0
      BT_FORWARD_NAME=磁力搜索
    """
    uin = (os.getenv("BT_FORWARD_UIN", "0") or "0").strip() or "0"
    name = (os.getenv("BT_FORWARD_NAME", "磁力搜索") or "磁力搜索").strip() or "磁力搜索"
    return name, uin


def _build_nodes(nodes: List[dict]):
    """把 dict 列表转成 OneBot 的 node 段列表。"""
    return [
        MessageSegment.node_custom(
            user_id=n["uin"],
            nickname=n["name"],
            content=n["content"],
        )
        for n in nodes
    ]


async def _send_forward(bot, event: Event, segments) -> bool:
    """发一条合并转发（群聊/私聊自适应）。成功返回 True。"""
    if Bot is None or not segments or not isinstance(bot, Bot):
        return False
    try:
        group_id = getattr(event, "group_id", None)
        if group_id:
            await bot.send_group_forward_msg(
                group_id=group_id, messages=OB11Message(segments)
            )
        else:
            await bot.send_private_forward_msg(
                user_id=event.get_user_id(), messages=OB11Message(segments)
            )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"合并转发发送失败：{type(e).__name__}: {e}")
        return False


async def _send_nested_forward(bot, event: Event, nodes: List[dict]) -> bool:
    """
    两层合并转发：外层一条 → 点进去是一条转发 → 再点进去才是结果。

    为什么要套两层（2026-11-13 用户要求）：
      磁力链接是明文 `magnet:?xt=urn:btih:...`，直接作为消息正文发出去，
      腾讯侧会做链接风控（轻则折叠/不提醒，重则整条消息被吞）。
      合并转发的正文不参与常规的链接扫描，套两层后外层连「转发」内容都被包住，
      实测能稳定送达。所以结果**不再单独发一条纯文本**，全部塞进转发里。

    结构：
      outer_forward
        └─ node(发送者)
             └─ inner_forward          ← 点第一下看到这个
                  ├─ node 结果 1
                  ├─ node 结果 2
                  └─ node 预览图 ...
    """
    if not nodes:
        return False

    inner = _build_nodes(nodes)
    outer = MessageSegment.node_custom(
        user_id=nodes[0]["uin"],
        nickname=nodes[0]["name"],
        content=OB11Message(inner),  # 内容本身又是一个 forward
    )

    if await _send_forward(bot, event, [outer]):
        return True

    # 有些 NapCat 版本 / 客户端不支持嵌套 forward，退回单层
    logger.warning("[BT] 嵌套转发不被支持，退回单层转发")
    if await _send_forward(bot, event, inner):
        return True

    logger.error("[BT] 合并转发全部失败，用户收不到结果")
    return False


bt_cmd = on_command("bt", aliases=set(), priority=10, block=True)

@bt_cmd.handle()
async def handle_bt_command(event: Event, args: Message = CommandArg()):
    message = str(args).strip()

    # ---- 总开关：bt on / bt off（主人/管理员专属，状态落盘重启不丢）----
    _low = message.strip().lower()
    if _low in _SWITCH_ON_WORDS or _low in _SWITCH_OFF_WORDS:
        if not _is_privileged(event):
            await bt_cmd.finish("这个开关只有主人或管理员能动。")
        _on = _low in _SWITCH_ON_WORDS
        _toggle_switch(_on)
        await bt_cmd.finish(f"✅ BT 磁力搜索已{'开启' if _on else '关闭'}。")

    if not BT_ENABLED:
        await bt_cmd.finish("🛑 BT 磁力搜索现在是关着的。要用了让主人/管理员发 bt on。")

    args_list = message.split()

    # 检查参数是否足够
    if not message or len(args_list) < 1:
        await bt_cmd.finish("用法：bt （排序方式） [关键词]\n示例：bt 热门 安达与岛村")

    # 解析排序参数和关键词
    sort_keyword = ""
    keyword = ""
    if len(args_list) == 1:
        keyword = args_list[0]
    else:
        sort_keyword = args_list[0]
        keyword = " ".join(args_list[1:])

    sort_param = MagnetUtils.get_sort_param(sort_keyword)
    results = await search_service.search(keyword, sort_param)

    if not results:
        await bt_cmd.finish("未找到相关磁力链接，网站失效或网络问题")
    elif len(results) == 1 and results[0].get("error") and not results[0].get("title"):
        # 搜索整体失败
        await bt_cmd.finish(results[0]["error"])
    else:
        nickname, uid = _get_sender_info(event)
        bot = get_bot()

        # 每条结果一个 node；预览图各自一个 node 跟在对应结果后面。
        # ⚠️ 不再单独发一条纯文本结果（那条就是被风控的目标），全部塞进转发里。
        inner_nodes: List[dict] = []
        inner_nodes.append(
            dict(
                uin=uid,
                name=nickname,
                content=OB11Message(
                    MessageSegment.text(f"共找到 {len(results)} 条有效结果")
                ),
            )
        )

        for idx, res in enumerate(results, 1):
            part = _format_result(idx, res).lstrip("\n")

            # 预览信息
            if enable_preview and res.get("magnet_link"):
                preview = await whatslink_service.get_preview(res["magnet_link"])
                if preview:
                    part += (
                        f"\n--- 预览 ---\n"
                        f"类型：{preview.get('file_type') or '未知'}\n"
                        f"文件数：{preview.get('count') or '未知'}"
                    )
                    if preview.get("screenshots"):
                        screenshot_url = preview["screenshots"][0].get("screenshot")
                        if screenshot_url and MessageSegment:
                            inner_nodes.append(
                                dict(
                                    uin=uid,
                                    name=nickname,
                                    content=OB11Message(
                                        MessageSegment.image(screenshot_url)
                                    ),
                                )
                            )

            inner_nodes.append(
                dict(
                    uin=uid,
                    name=nickname,
                    content=OB11Message(MessageSegment.text(part)),
                )
            )

        if enable_preview:
            inner_nodes.append(
                dict(
                    uin=uid,
                    name=nickname,
                    content=OB11Message(
                        MessageSegment.text("预览数据来源：whatslink.info")
                    ),
                )
            )

        # 两层合并转发：外层 → 内层转发 → 结果
        sent = await _send_nested_forward(bot, event, inner_nodes)
        if not sent:
            # 转发全挂时也不退回明文（明文里的 magnet 链接正是要被规避的东西）
            await bt_cmd.finish("结果发送失败（转发消息被拒绝），请稍后重试")

        await bt_cmd.finish()


btp_cmd = on_command("btp", aliases=set(), priority=10, block=True)

@btp_cmd.handle()
async def handle_btp_command(args: Message = CommandArg()):
    """
    磁链预览指令
    使用方式：btp [磁力链接]
    示例：btp magnet:?xt=urn:btih:xxxx
    """
    message = str(args).strip()

    # 总开关：bt 关了，btp 预览也跟着关
    if not BT_ENABLED:
        await btp_cmd.finish("🛑 BT 磁力搜索现在是关着的。")

    if not message:
        await btp_cmd.finish("用法：btp [磁力链接]\n示例：btp magnet:?xt=urn:btih:xxxx")

    magnet_url = message
    if not magnet_url.startswith("magnet:"):
        await btp_cmd.finish("请输入有效的磁力链接（以 magnet: 开头）")

    preview = await whatslink_service.get_preview(magnet_url)
    if not preview or not preview.get("name"):
        await btp_cmd.finish("未查询到预览信息，链接可能无效或 API 暂不可用")

    text = (
        f"文件名：{preview.get('name', '未知')}\n"
        f"类型：{preview.get('file_type') or '未知'}\n"
        f"总大小：{_format_size(preview.get('size', 0))}\n"
        f"文件数：{preview.get('count') or '未知'}\n"
        f"\n预览数据来源：whatslink.info"
    )
    await btp_cmd.send(text)

    if preview.get("screenshots"):
        screenshot_url = preview["screenshots"][0].get("screenshot")
        if screenshot_url and MessageSegment:
            await btp_cmd.finish(MessageSegment.image(screenshot_url))