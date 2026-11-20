"""
第三方插件「数据层」体检：一次性扒出「指令有反应但不出结果」的根因。

为什么要有这个脚本：
  · 在 QQ 里测一个功能要 @人、等队列、看日志，而且失败常常被 matcher 静默吃掉
    （`Matcher running complete` 之后什么都没发出去）。
  · 三方插件的失败点几乎全在**数据层**：站点改版、API key 没配、上游 403、
    解析选择器对不上、缺重依赖（selenium / htmlrender）。
  · 这个脚本绕过 QQ，直接调各插件自己的数据层函数，把根因原样打出来。

用法（容器内，docker-compose 已挂载 ./scripts:/app/scripts）：
    docker exec qq-grok-bot python scripts/test_third_plugins.py
    docker exec qq-grok-bot python scripts/test_third_plugins.py bt      # 只测 bt
    docker exec qq-grok-bot python scripts/test_third_plugins.py pixiv steam

退出码：有失败 → 1，全过 → 0。
"""
import asyncio
import os
import sys
import traceback
from pathlib import Path

# 用 `python scripts/xxx.py` 跑时 sys.path[0] 是 /app/scripts，导不到 plugins 包
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
# nonebot.init() 从 cwd 找 .env
os.chdir(_ROOT)

import nonebot  # noqa: E402

nonebot.init()

# ⚠️ 三方插件里大量 `require("xxx")`，而 require() 要求目标插件**尚未被 import**。
#    这里先把它们注册成插件，否则 import 插件包时会抛
#    "Module xxx is not loaded as a plugin! Make sure not to import it before loading."
for _p in (
    "nonebot_plugin_localstore",
    "nonebot_plugin_alconna",
    "nonebot_plugin_apscheduler",
):
    try:
        nonebot.load_plugin(_p)
    except Exception as _e:  # noqa: BLE001
        print(f"[WARN] 预加载 {_p} 失败：{type(_e).__name__}: {_e}")

import httpx  # noqa: E402

RESULTS = []  # (group, item, ok, detail)


def _ok(group: str, item: str, detail: str = "") -> None:
    RESULTS.append((group, item, True, detail))
    print(f"  [OK]   {item}" + (f"  {detail}" if detail else ""))


def _bad(group: str, item: str, detail: str = "") -> None:
    RESULTS.append((group, item, False, detail))
    print(f"  [FAIL] {item}" + (f"  {detail}" if detail else ""))


def _section(title: str) -> None:
    print(f"\n{'=' * 70}\n=== {title}\n{'=' * 70}")


# ---------------------------------------------------------------- bittorrents
async def test_bittorrents(keyword: str = "纳西妲") -> None:
    G = "bt (bittorrents)"
    _section(G)
    try:
        from plugins.third.bittorrents.main import (
            MagnetConfig,
            MagnetSearchService,
            MagnetUtils,
            magnet_headers,
        )
    except Exception as e:  # noqa: BLE001
        _bad(G, "import 插件", f"{type(e).__name__}: {e}")
        return

    cfg = MagnetConfig()
    _ok(G, "配置", f"base={cfg.base_url} path={cfg.search_path} 候选={cfg.candidates()}")
    print(f"         UA = {magnet_headers.get('User-Agent', '')[:80]}")

    # 第一步：裸探每个站点，看清到底是「连不上」「状态码不对」还是「页面结构变了」
    # ⚠️ 必须用插件自己的 headers：httpx 默认 UA 是 "python-httpx/x.y"，
    #    这类站对非浏览器 UA 直接返回屏蔽页，探出来的结果会误导人。
    word = MagnetUtils.encode_search_word(keyword)
    async with httpx.AsyncClient(
        headers=magnet_headers,
        timeout=cfg.request_timeout,
        follow_redirects=True,
        verify=False,
    ) as client:
        for base in cfg.candidates():
            url = f"{base}{cfg.search_path}?word={word}&sort=rele"
            try:
                r = await client.get(url)
            except Exception as e:  # noqa: BLE001
                _bad(G, f"连通 {base}", f"{type(e).__name__}: {e}")
                continue
            has_atob = "window.atob" in r.text
            has_plain = "SearchListTitle_result_title" in r.text
            hops = " → ".join(str(h.url) for h in r.history) or "无"
            _ok(
                G,
                f"连通 {base}",
                f"HTTP {r.status_code} type={r.headers.get('content-type', '?')[:30]} "
                f"len={len(r.text)} atob={'有' if has_atob else '无'} "
                f"明文结果块={'有' if has_plain else '无'}",
            )
            if r.history:
                print(f"         重定向：{hops} → {r.url}")
            if not has_atob and not has_plain:
                _probe_page(G, base, r)

    # 第二步：跑插件自己的完整搜索流程（同样的 headers，跟线上一致）
    async with httpx.AsyncClient(
        headers=magnet_headers,
        timeout=cfg.request_timeout,
        follow_redirects=True,
        verify=False,
    ) as client:
        svc = MagnetSearchService(MagnetConfig(), client)
        try:
            res = await svc.search(keyword, "rele")
        except Exception as e:  # noqa: BLE001
            _bad(G, "完整搜索", f"{type(e).__name__}: {e}")
            return
    if not res:
        _bad(G, "完整搜索", "返回 0 条（站点改版 / 全挂）")
    elif res[0].get("error"):
        _bad(G, "完整搜索", res[0]["error"])
    else:
        got_magnet = sum(1 for x in res if x.get("magnet_link"))
        _ok(G, "完整搜索", f"{len(res)} 条结果，其中 {got_magnet} 条拿到磁力链接")
        for x in res[:3]:
            print(
                f"         · {x['title'][:40]}  {x['size']}  "
                f"magnet={'有' if x.get('magnet_link') else '无'}"
            )


def _probe_page(G: str, base: str, r) -> None:
    """
    页面既没有 atob 块也没有明文结果块时，把「它到底是什么页」挖出来。

    只打一句「结构变了」没有意义——要区分：
      · 拦截/验证页（Cloudflare / 网站自己的屏蔽提醒弹窗）
      · 搜索表单换了 method 或 path（GET /search → POST /s/xxx 之类）
      · 真的空结果
    所以这里打印：关键标记 + 所有 <form> 的 action/method/字段 + 页面开头，
    并且把整页落盘到 data/temp/bt_dump.html，方便直接翻。
    """
    from bs4 import BeautifulSoup

    text = r.text
    markers = {
        "window.atob": "atob 加密块",
        "SearchListTitle_result_title": "明文结果标题",
        "Search_list_wrapper": "明文结果容器",
        "网址屏蔽提醒": "站点自己的屏蔽提醒弹窗",
        "Just a moment": "Cloudflare 挑战页",
        "cf-browser-verification": "Cloudflare 挑战页",
        "challenge-platform": "Cloudflare 挑战页",
        "captcha": "验证码",
        "请输入验证码": "验证码",
        "访问过于频繁": "限流",
    }
    hit = [desc for k, desc in markers.items() if k in text]
    _bad(G, f"解析 {base}", f"没有结果块。命中标记：{hit or '无'}")

    soup = BeautifulSoup(text, "lxml")
    forms = soup.find_all("form")
    if forms:
        print(f"         页面里的 <form> 共 {len(forms)} 个：")
        for f in forms:
            action = f.get("action", "")
            method = (f.get("method") or "get").upper()
            fields = [
                i.get("name") for i in f.find_all(["input", "select"]) if i.get("name")
            ]
            print(f"           · method={method} action={action!r} 字段={fields}")
    else:
        print("         页面里没有任何 <form>（搜索可能走 JS/AJAX，需要看接口）")

    # 找找页面里有没有藏着接口路径
    import re as _re

    apis = sorted(set(_re.findall(r'["\'](/[a-z0-9_\-/]{2,40})["\']', text)))[:15]
    if apis:
        print(f"         页面里出现的路径片段（前 15 个）：{apis}")

    head = " ".join(text[:400].split())
    print(f"         页面开头：{head[:300]}")

    dump = Path("data") / "temp" / "bt_dump.html"
    try:
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(text, encoding="utf-8", errors="ignore")
        print(f"         整页已保存：{dump.resolve()}（{len(text)} 字符，可以直接 less 翻）")
    except Exception as e:  # noqa: BLE001
        print(f"         保存页面失败：{e}")


# ------------------------------------------------------------------- pixivrank
async def test_pixiv() -> None:
    G = "pixiv (p站排行)"
    _section(G)
    try:
        from plugins.third.pixivrank_search.config import config
        from plugins.third.pixivrank_search.data_source import (
            WEB_HEADERS,
            _to_original_url,
        )
    except Exception as e:  # noqa: BLE001
        _bad(G, "import 插件", f"{type(e).__name__}: {e}")
        return

    _ok(
        G,
        "配置",
        f"rsshub={config.pixr_rsshub!r} proxy={config.pixr_local_proxy!r} retry={config.pixr_retry}",
    )

    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        # ---- 主路线：pixiv 网页版公开排行接口（不需要登录态）----
        try:
            r = await client.get(
                "https://www.pixiv.net/ranking.php",
                params={"mode": "daily", "format": "json", "p": 1},
                headers=WEB_HEADERS,
            )
            if r.status_code != 200:
                _bad(G, "网页排行接口", f"HTTP {r.status_code}，body 开头：{' '.join(r.text[:200].split())}")
            else:
                data = r.json()
                contents = data.get("contents") or []
                if not contents:
                    _bad(G, "网页排行接口", f"HTTP 200 但 contents 为空，顶层键：{list(data)[:8]}")
                else:
                    _ok(
                        G,
                        "网页排行接口",
                        f"HTTP 200，{len(contents)} 条，首条：{contents[0].get('title', '')[:30]}"
                        f" / {contents[0].get('user_name', '')}",
                    )
                    thumb = contents[0].get("url", "")
                    _ok(G, "缩略图地址", thumb[:80])

                    # 缩略图 → 原图，再带 Referer 下载（i.pximg 有防盗链）
                    orig = _to_original_url(thumb, 0)
                    _ok(G, "原图地址", orig[:80])
                    r2 = await client.get(
                        orig,
                        headers={
                            "User-Agent": WEB_HEADERS["User-Agent"],
                            "Referer": "https://www.pixiv.net/",
                        },
                    )
                    if r2.status_code == 200 and len(r2.content) > 1000:
                        _ok(G, "原图下载", f"HTTP 200，{len(r2.content) // 1024} KB")
                    else:
                        _bad(
                            G,
                            "原图下载",
                            f"HTTP {r2.status_code}，{len(r2.content)} bytes"
                            "（可能是该作品原图是 png/gif，或地址变换规则要调）",
                        )
        except Exception as e:  # noqa: BLE001
            _bad(G, "网页排行接口", f"{type(e).__name__}: {e}")

        # ---- 备用路线：RSSHub（2026-11-13 已从 docker-compose 移除，仅作信息展示）----
        # 这里**不计入失败**：主路线（pixiv 网页公开接口）能用就够了。
        try:
            r = await client.get(f"{config.pixr_rsshub}/pixiv/ranking/day", timeout=5)
            if r.status_code == 200:
                print(f"  [INFO] RSSHub（备用）  HTTP 200 len={len(r.text)}")
            else:
                print(
                    f"  [INFO] RSSHub（备用）  HTTP {r.status_code}"
                    "（缺 PIXIV_REFRESH_TOKEN，主路线不受影响）"
                )
        except Exception as e:  # noqa: BLE001
            print(f"  [INFO] RSSHub（备用）  未运行（{type(e).__name__}）—— 已主动移除，忽略")


# ----------------------------------------------------------------- steam_judger
async def test_steam() -> None:
    G = "steam (steam分析)"
    _section(G)
    try:
        from plugins.third import steam_judger as sj
    except Exception as e:  # noqa: BLE001
        _bad(G, "import 插件", f"{type(e).__name__}: {e}")
        return

    cfg = sj.plugin_config
    key = cfg.steam_api_key or ""
    _ok(
        G,
        "配置",
        f"ai_base={cfg.ai_api_base!r} model={cfg.ai_model!r} "
        f"steam_key={('已配 len=%d %s…' % (len(key), key[:6])) if key else '空'} "
        f"ai_key={'已配' if cfg.ai_api_key else '空'}",
    )

    # 1. Steam Web API
    if not key:
        # 直接看进程环境变量：能区分「.env 文件里没写」和「容器环境变量里的空值把它盖了」
        env_raw = os.getenv("STEAM_API_KEY", None)
        if env_raw is None:
            hint = "环境变量里也没有 → .env 文件没被读到（检查挂载 ./env）"
        elif env_raw == "":
            hint = (
                "环境变量是**空字符串** → 容器创建时 .env 里还是 `STEAM_API_KEY=`，\n"
                "           而 python-dotenv 默认不覆盖已存在的环境变量（override=False），\n"
                "           空值把 .env 里的新值盖住了。\n"
                "           修法：sudo docker compose up -d --force-recreate bot\n"
                "           （docker restart / compose restart 都不会重读 env_file）"
            )
        else:
            hint = f"环境变量有值 len={len(env_raw)} 但插件没读到，检查 config.py 的 _pick"
        _bad(
            G,
            "Steam API",
            f"STEAM_API_KEY 为空 → api.steampowered.com 一律 401/403。\n           {hint}",
        )
    else:
        sample = "76561197960287930"  # Valve 官方示例账号（Gabe Newell，库公开）
        # 先裸打一次：401 的 body 里才有真正的原因，光看状态码分不清
        # 「key 无效」「key 未激活」「steamid 格式不对」。
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                r = await client.get(
                    "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
                    params={"key": key, "steamids": sample},
                )
            _ok(G, "Steam 原始响应", f"HTTP {r.status_code} body={r.text[:220]}")
            if r.status_code != 200:
                _bad(
                    G,
                    "Steam API",
                    f"HTTP {r.status_code} —— key 本身无效/未激活（不是 steamid 的问题）",
                )
            else:
                players = (r.json().get("response") or {}).get("players") or []
                if players:
                    _ok(G, "Steam API", f"取到用户：{players[0].get('personaname', '?')}")
                else:
                    _bad(G, "Steam API", "HTTP 200 但 players 为空（steamid 不存在？）")
        except Exception as e:  # noqa: BLE001
            _bad(G, "Steam API", f"{type(e).__name__}: {e}")

    # 2. AI 接口（出分析文字用）
    try:
        text = await sj.test_ai_api()
        _ok(G, "AI 接口", str(text)[:80].replace("\n", " "))
    except Exception as e:  # noqa: BLE001
        _bad(G, "AI 接口", f"{type(e).__name__}: {e}")

    # 3. 出图链路：selenium + chromedriver
    if sj._SELENIUM_ERR is not None:
        _bad(G, "selenium", f"没装：{sj._SELENIUM_ERR}")
    elif not os.path.exists(sj.DRIVER_PATH):
        _bad(G, "chromedriver", f"路径不存在：{sj.DRIVER_PATH}（可用 STEAM_CHROMEDRIVER 覆盖）")
    else:
        _ok(G, "chromedriver", sj.DRIVER_PATH)


# --------------------------------------------------------------------- girls_2d
async def test_girls2d() -> None:
    G = "girls_2d (来点美少女)"
    _section(G)
    url = "https://www.dmoe.cc/random.php"
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        ctype = r.headers.get("content-type", "")
        if r.status_code == 200 and ctype.startswith("image") and len(r.content) > 1000:
            _ok(G, "随机图 API", f"HTTP 200 {ctype} {len(r.content) // 1024} KB")
        else:
            _bad(G, "随机图 API", f"HTTP {r.status_code} type={ctype} len={len(r.content)}")
    except Exception as e:  # noqa: BLE001
        _bad(G, "随机图 API", f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------- gspanel
async def test_gspanel(uid: str = "100000001") -> None:
    G = "gspanel (原神面板)"
    _section(G)
    # 出图靠 htmlrender + playwright，缺了它数据能拿、图出不来
    try:
        import nonebot_plugin_htmlrender  # noqa: F401

        _ok(G, "htmlrender", "已安装")
    except Exception as e:  # noqa: BLE001
        _bad(G, "htmlrender", f"没装：{type(e).__name__}: {e} → 面板能查但出不了图")

    try:
        from plugins.third.gspanel.data_source import queryPanelApi

        data = await queryPanelApi(uid)
        if isinstance(data, dict) and data.get("error"):
            _bad(G, "Enka API", str(data["error"])[:120])
        else:
            ok_flag = "playerInfo" in str(data) or "avatarInfoList" in str(data)
            (_ok if ok_flag else _bad)(G, "Enka API", f"uid={uid} 返回字段：{list(data)[:6]}")
    except Exception as e:  # noqa: BLE001
        _bad(G, "Enka API", f"{type(e).__name__}: {e}")


TESTS = {
    "bt": test_bittorrents,
    "pixiv": test_pixiv,
    "steam": test_steam,
    "girls": test_girls2d,
    "gspanel": test_gspanel,
}


async def main() -> int:
    picked = [a for a in sys.argv[1:] if not a.startswith("-")]
    names = picked or list(TESTS)
    for name in names:
        fn = TESTS.get(name)
        if not fn:
            print(f"[WARN] 没有这个测试：{name}（可选：{', '.join(TESTS)}）")
            continue
        try:
            await fn()
        except Exception as e:  # noqa: BLE001
            _bad(name, "未捕获异常", f"{type(e).__name__}: {e}")
            print(traceback.format_exc())

    print(f"\n{'=' * 70}\n=== 汇总\n{'=' * 70}")
    fails = [r for r in RESULTS if not r[2]]
    for group, item, ok_flag, detail in RESULTS:
        if not ok_flag:
            print(f"  [FAIL] {group:<24} {item}  {detail}")
    print(
        f"\n共 {len(RESULTS)} 项：{len(RESULTS) - len(fails)} 通过 / {len(fails)} 失败"
    )
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
