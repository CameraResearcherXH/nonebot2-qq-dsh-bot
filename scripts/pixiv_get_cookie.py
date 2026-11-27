"""
在容器里用 headless Chromium 登录 pixiv 网页版，把 PHPSESSID 打出来。

为什么需要这个：
  /p站搜图 走的是 pixiv 网页版搜索接口 /ajax/search/artworks/xxx，
  匿名访问直接 403（pixiv 把搜索划进登录墙了）。
  而 App API 那条 refresh_token 的路已经全断（password 授权 1508、
  PKCE 的 /start 接口 404、RSSHub 不支持 cookie），所以只能走网页版 cookie。

  PHPSESSID 是 HttpOnly，`javascript:document.cookie` 读不到，
  手机上没有 F12 也看不到 —— 那就让容器里的 Chromium 替你登录一次。

用法（在服务器上）：

    # 让容器自己登录并打印 cookie
    sudo docker compose exec -it bot python scripts/pixiv_get_cookie.py \\
        --user 你的pixiv邮箱或ID --pass 你的密码

    # 已经有 cookie 了，只验证它能不能搜（不登录）
    sudo docker compose exec -it bot python scripts/pixiv_get_cookie.py \\
        --test "PHPSESSID=xxxxxxxx"

    # 浏览器不是 headless 跑（调试用，容器里没显示器，一般不用）
    ... --no-headless

拿到之后写进 .env：
    PIXR_COOKIE=PHPSESSID=xxxxxxxx
然后 `sudo docker compose up -d --force-recreate bot`（env_file 只在创建容器时读）。

⚠️ 已知会被挡的情况：
  1. 账号开了两步验证 / 新设备邮箱验证码 → 脚本停在验证码页，需要手动过
  2. pixiv 登录页有 reCAPTCHA → headless 环境下大概率过不去
  这两种情况脚本会明确告诉你，改走「Kiwi Browser 看 DevTools」或「手机抓包」。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import httpx

DRIVER_PATH = "/usr/bin/chromedriver"
CHROME_CANDIDATES = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
)

LOGIN_URL = "https://accounts.pixiv.net/login?return_to=https%3A%2F%2Fwww.pixiv.net%2F"
SEARCH_URL = "https://www.pixiv.net/ajax/search/artworks/{keyword}"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.1 Safari/537.36"
)


def _chrome_options():
    from selenium.webdriver.chrome.options import Options

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")            # 容器里是 root，不加必崩
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,900")
    options.add_argument(f"--user-agent={UA}")
    for cand in CHROME_CANDIDATES:
        if os.path.exists(cand):
            options.binary_location = cand
            break
    return options


def _dump_cookies(driver) -> str:
    """从浏览器里取 www.pixiv.net 的 PHPSESSID。"""
    for c in driver.get_cookies():
        if c.get("name") == "PHPSESSID" and "pixiv.net" in c.get("domain", ""):
            return c.get("value", "")
    return ""


def login_and_get_cookie(user: str, password: str, headless: bool = True) -> str:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    if not headless:
        # 容器里没有 X server，这条只是为了在本机调试时不报错
        pass
    options = _chrome_options()
    if not headless:
        options.arguments = [a for a in options.arguments if not a.startswith("--headless")]

    driver = webdriver.Chrome(service=Service(DRIVER_PATH), options=options)
    driver.set_page_load_timeout(60)
    try:
        print(f"[1/4] 打开登录页: {LOGIN_URL}")
        driver.get(LOGIN_URL)
        time.sleep(2)

        # 万一这个浏览器配置里已经有登录态（复用 user-data-dir 时）
        existing = _dump_cookies(driver)
        if existing:
            print("[!] 已经是登录态，直接复用现有 cookie")
            return existing

        # reCAPTCHA 存在就提前告警（headless 基本过不去）
        if driver.find_elements(By.CSS_SELECTOR, 'iframe[src*="recaptcha"]'):
            print("[!] 登录页出现 reCAPTCHA，headless 很可能过不去")

        print("[2/4] 填账号密码")
        user_box = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, 'input[autocomplete="username"], input[name="pixiv_id"]')
            )
        )
        user_box.clear()
        user_box.send_keys(user)

        pwd_box = driver.find_element(
            By.CSS_SELECTOR, 'input[autocomplete="current-password"], input[type="password"]'
        )
        pwd_box.clear()
        pwd_box.send_keys(password)

        # pixiv 的登录按钮没有稳定 id，按 type=submit 找
        submit = driver.find_element(By.CSS_SELECTOR, 'button[type="submit"]')
        submit.click()

        print("[3/4] 等待登录结果（最多 60 秒）")
        deadline = time.time() + 60
        while time.time() < deadline:
            cookie = _dump_cookies(driver)
            if cookie:
                print("[OK] 登录成功")
                return cookie
            time.sleep(2)

        # 没拿到 cookie —— 把当前页面状态打出来，便于判断卡在哪
        print("[FAIL] 60 秒内没拿到 PHPSESSID")
        print(f"    当前 URL : {driver.current_url}")
        print(f"    页面标题 : {driver.title}")
        for sel, tip in (
            ('iframe[src*="recaptcha"]', "reCAPTCHA 验证"),
            ('input[autocomplete="one-time-code"]', "两步验证验证码"),
            ('input[name="otp"]', "两步验证验证码"),
            (".error-message", "账号或密码错误"),
        ):
            if driver.find_elements(By.CSS_SELECTOR, sel):
                print(f"    ⛔ 检测到：{tip}")
        return ""
    finally:
        try:
            driver.quit()
        except Exception:  # noqa: BLE001
            pass


def test_cookie(cookie: str, keyword: str = "初音ミク") -> bool:
    """用 cookie 打一次搜索接口，确认真的能搜。"""
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.pixiv.net/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8",
        "Cookie": cookie,
    }
    params = {
        "word": keyword,
        "order": "date_d",
        "mode": "all",
        "p": 1,
        "s_mode": "s_tag_full",
        "type": "all",
    }
    url = SEARCH_URL.format(keyword=keyword)
    print(f"[test] GET {url}  keyword={keyword}")
    try:
        r = httpx.get(url, params=params, headers=headers, timeout=15, follow_redirects=True)
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] 请求异常: {e}")
        return False

    print(f"[test] HTTP {r.status_code}")
    if r.status_code != 200:
        print(f"[FAIL] 非 200，cookie 多半无效。body 前 200 字: {r.text[:200]}")
        return False

    try:
        data = r.json()
    except Exception:  # noqa: BLE001
        print(f"[FAIL] 返回不是 JSON: {r.text[:200]}")
        return False

    if data.get("error"):
        print(f"[FAIL] 接口报错: {json.dumps(data, ensure_ascii=False)[:300]}")
        return False

    body = data.get("body") or {}
    illust = (body.get("illustManga") or {}).get("data") or []
    print(f"[OK] 搜到 {len(illust)} 条（total={(body.get('illustManga') or {}).get('total')}）")
    for item in illust[:3]:
        print(f"     id={item.get('id')}  {str(item.get('title'))[:40]}")
    return bool(illust)


def main() -> int:
    ap = argparse.ArgumentParser(description="获取/验证 pixiv 网页版 PHPSESSID")
    ap.add_argument("--user", help="pixiv 邮箱 / pixiv ID")
    ap.add_argument("--pass", dest="password", help="pixiv 密码")
    ap.add_argument("--test", metavar="COOKIE", help="只验证 cookie 能不能搜图，不登录")
    ap.add_argument("--keyword", default="初音ミク", help="--test 时用的关键词")
    ap.add_argument("--no-headless", action="store_true", help="不启用 headless（需显示器）")
    args = ap.parse_args()

    if args.test:
        ok = test_cookie(args.test.strip(), args.keyword)
        if ok:
            print("\n把这一行写进 .env：")
            print(f"PIXR_COOKIE={args.test.strip()}")
        return 0 if ok else 1

    if not args.user or not args.password:
        ap.error("要么给 --user/--pass 登录，要么用 --test 验证已有 cookie")

    cookie = login_and_get_cookie(args.user, args.password, headless=not args.no_headless)
    if not cookie:
        print(
            "\n登录失败。可选替代方案：\n"
            "  1) Android 装 Kiwi Browser → Developer tools → Application → Cookies 取 PHPSESSID\n"
            "  2) 手机装 Reqable/Stream 抓包，看 www.pixiv.net 请求头里的 Cookie\n"
            "  3) 借一台电脑 F12\n"
            "拿到后先跑：python scripts/pixiv_get_cookie.py --test \"PHPSESSID=xxx\" 验证"
        )
        return 1

    print("\n===== 拿到 cookie =====")
    print(f"PHPSESSID={cookie}")
    print("\n验证一下能不能搜图：")
    ok = test_cookie(f"PHPSESSID={cookie}", args.keyword)
    if ok:
        print("\n把这一行写进 .env：")
        print(f"PIXR_COOKIE=PHPSESSID={cookie}")
        print("然后：sudo docker compose up -d --force-recreate bot")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
