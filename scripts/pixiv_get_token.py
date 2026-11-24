"""
用 pixiv 官方 App 的 OAuth PKCE 流程，换一个 refresh_token 给 RSSHub 用。

为什么要这个：
  RSSHub 的 /pixiv/* 路由现在基本都要登录态（pixiv 关掉了匿名 App API），
  没配 PIXIV_REFRESH_TOKEN 时路由直接抛错，RSSHub 返回 503
  （页面就是 "Welcome to RSSHub!" 那个通用错误页），
  表现就是 /p站排行 永远「没有搜索到喔」。

为什么不用账号密码：
  RSSHub 早期支持 PIXIV_USERNAME / PIXIV_PASSWORD，现在已不推荐；
  refresh_token 更稳（不会因为改密码/开两步验证就失效，也不需要存明文密码）。

用法（必须在能直连 pixiv 的机器上跑，我们这台香港服务器可以）：

    docker exec -it qq-grok-bot python scripts/pixiv_get_token.py

它会打印一条 pixiv 登录链接：
  1. 在你自己的浏览器里打开（需要能访问 pixiv），用 pixiv 账号登录
  2. 登录后浏览器会跳到 https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=&code=xxxx
     （或者页面提示「请复制链接」）——把**整条 URL** 粘回终端回车
  3. 脚本换出 refresh_token，打印出来

然后把 token 填进 .env：
    PIXIV_REFRESH_TOKEN=xxxxxxxx
再重建 rsshub：
    sudo docker compose up -d --force-recreate rsshub

也可以非交互传参（脚本在外面跑、结果拷进来）：
    python scripts/pixiv_get_token.py --code "粘贴的整条回调URL"
"""
import argparse
import base64
import hashlib
import re
import secrets
import sys
from typing import Any

import httpx

# pixiv Android App 的公开 client（pixivpy / pixiv_auth 等工具通用）
# ⚠️ client_id 是 32 位，**少一位都会被判非法客户端**：
#    写成 MOBrBDS8blbauoSck0Zf（20 位）→ HTTP 403 + code 918「不正なOAuthクライアントです」。
#    2026-11-13 实际踩过，浪费了一轮排查。
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_[REDACTED_SECRET]
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"

HEADERS = {
    "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
    "App-OS": "android",
    "App-OS-Version": "11",
    "App-Version": "5.0.234",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def build_login_url() -> tuple[str, str]:
    """生成 PKCE 的 code_verifier / code_challenge 和登录链接。"""
    # token_urlsafe(64) ≈ 86 字符，和 pixiv-token / pixiv_auth 的实现保持一致
    # （PKCE 规范只要求 43~128 字符，但没必要在这里标新立异）
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    url = (
        f"{LOGIN_URL}?code_challenge={challenge}"
        f"&code_challenge_method=S256&client=pixiv-android"
    )
    return url, verifier


def extract_code(raw: str) -> str:
    """从粘贴内容里取出 code：整条回调 URL / `code=xxx` / 裸 code 都支持。"""
    raw = raw.strip()
    m = re.search(r"[?&]code=([^&\s#]+)", raw)
    if m:
        return m.group(1)
    return raw


def exchange(code: str, verifier: str) -> dict[str, Any]:
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
        "include_policy": "true",
        "get_secure_url": "true",
    }
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        r = client.post(TOKEN_URL, data=data)
    if r.status_code != 200:
        raise RuntimeError(f"换 token 失败：HTTP {r.status_code}\n{r.text[:800]}")
    return r.json()


def password_login(username: str, password: str) -> dict[str, Any]:
    """
    备用方案：账号密码直换 refresh_token（pixivpy 用的同一个 grant_type=password）。

    为什么需要这条备用路：
      标准做法是浏览器走 PKCE（app-api.pixiv.net/web/v1/login → .../start → callback?code=），
      但如果 app-api.pixiv.net 在你的网络里打不开（国内直连被墙 / 代理规则没覆盖），
      整条跳转链就断了，`start` 会显示「此站点不存在」。
      这个接口只需要能访问 oauth.secure.pixiv.net，服务器（香港）直连就行。

    限制：账号开了两步验证（2FA）时这里会失败，只能用 PKCE 那条路。
    """
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "get_secure_url": "1",
        "include_policy": "true",
        "grant_type": "password",
        "username": username,
        "password": password,
    }
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        r = client.post(TOKEN_URL, data=data)
    if r.status_code != 200:
        raise RuntimeError(f"登录失败：HTTP {r.status_code}\n{r.text[:800]}")
    return r.json()


def selftest() -> int:
    """
    自检：确认「client 凭据有效」和「PKCE 各接口还活着」，把排查从猜变成看。

    分别测：
      1. /web/v1/login          —— 期望 302 跳 accounts.pixiv.net；404 说明入口没了
      2. .../auth/pixiv/start   —— 期望 302 跳 callback；404 说明这一步被下线了
      3. auth/token (假 token)  —— 用假 refresh_token 探 client 凭据：
                                   code 918 = client_id/secret 不对
                                   其它错误   = client 有效，只是 token 假
    """
    print("=" * 72)
    print("pixiv 登录链路自检")
    print("=" * 72)

    probe = "A" * 43  # 形状合法的假 challenge

    checks = [
        (
            "登录入口 /web/v1/login",
            f"{LOGIN_URL}?code_challenge={probe}"
            f"&code_challenge_method=S256&client=pixiv-android",
        ),
        (
            "签发 code 的 start 接口",
            "https://app-api.pixiv.net/web/v1/users/auth/pixiv/start"
            f"?code_challenge={probe}"
            "&code_challenge_method=S256&client=pixiv-android&via=login",
        ),
    ]
    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=False) as client:
        for name, url in checks:
            try:
                r = client.get(url)
            except Exception as e:  # noqa: BLE001
                print(f"\n[?] {name}\n    请求失败：{type(e).__name__}: {e}")
                continue
            loc = r.headers.get("location", "")
            print(f"\n[{'OK' if r.status_code in (200, 302) else '??'}] {name}")
            print(f"    HTTP {r.status_code}  location={loc[:140]}")
            if r.status_code == 404:
                print(f"    body={r.text[:200]}")
                print("    → 这个接口不存在了，PKCE 这条路走不通")

        # 3. 用假 refresh_token 探 client 凭据
        print("\n[...] 探 client 凭据（假 refresh_token）")
        try:
            r = client.post(
                TOKEN_URL,
                data={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "get_secure_url": "1",
                    "include_policy": "true",
                    "grant_type": "refresh_token",
                    "refresh_token": "bogus_refresh_token_for_probe",
                },
            )
            body = r.text[:300]
            print(f"    HTTP {r.status_code}  body={body}")
            if '"code":918' in body or '"code": 918' in body:
                print("    → client_id/secret 无效（918 非法客户端）")
            elif "invalid_grant" in body:
                print("    → client 凭据有效，只是 refresh_token 是假的（符合预期）")
        except Exception as e:  # noqa: BLE001
            print(f"    请求失败：{type(e).__name__}: {e}")

    print("\n" + "=" * 72)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="获取 pixiv refresh_token")
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="只做链路自检，不登录",
    )
    parser.add_argument(
        "--code",
        default="",
        help="回调 URL 或 code（不传则交互式输入）",
    )
    parser.add_argument(
        "--verifier",
        default="",
        help="上一次运行打印出来的 code_verifier（脚本被关掉后补做兑换用）",
    )
    parser.add_argument(
        "--user",
        default="",
        help="备用方案：pixiv 账号（邮箱或 pixiv id），配合 --pass 使用",
    )
    parser.add_argument(
        "--pass",
        dest="password",
        default="",
        help="备用方案：pixiv 密码（注意会留在 shell history，用完记得清）",
    )
    args = parser.parse_args()

    if args.selftest:
        return selftest()

    # 备用方案优先：账号密码直换，不需要浏览器跳转
    if args.user and args.password:
        print(f"用账号密码直换 refresh_token（账号：{args.user}）…")
        try:
            result = password_login(args.user, args.password)
        except Exception as e:  # noqa: BLE001
            print(f"\n{e}")
            print(
                "\n常见原因：\n"
                "  · 账号开了两步验证（2FA）→ 这条路走不通，只能用 PKCE 那条\n"
                "  · 密码错 / 触发了 pixiv 的风控验证码\n"
                "  · 邮箱登录要用邮箱地址，不是 pixiv id"
            )
            return 1
        return _report(result)

    if args.verifier:
        verifier = args.verifier
        login_url = ""
        print("用你提供的 code_verifier 继续兑换（跳过登录步骤）\n")
    else:
        login_url, verifier = build_login_url()

        print("=" * 72)
        print("第 1 步：在浏览器里打开下面这条链接，用 pixiv 账号登录")
        print("         （浏览器必须能访问 pixiv；国内网络需要自备代理）")
        print("=" * 72)
        print()
        print(login_url)
        print()
        print("=" * 72)
        print("第 2 步：登录成功后，浏览器会跳到")
        print("         https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=&code=xxxx")
        print("         把**整条 URL** 复制下来粘到下面（页面白屏也没关系，看地址栏）")
        print()
        print("         中途可能被「安全设置提醒」页拦住（提示设两步验证/Passkey），")
        print("         点「跳过 / 稍后再说」或右上角 × 关掉，它会自动继续跳转。")
        print("         ⚠️ 不要关掉这个脚本、也不要重跑：重跑会生成新的 challenge，")
        print("            刚才那条链接就作废了。")
        print("=" * 72)
        print()
        print("如果终端被意外关掉，用下面这行补做兑换（不用重新登录）：")
        print(f"  python scripts/pixiv_get_token.py --verifier {verifier}")
        print()
        print("=" * 72)

    raw = args.code
    if not raw:
        try:
            raw = input("\n粘贴回调 URL（或只粘 code）> ")
        except EOFError:
            print("\n没有输入，退出。注意要用 docker exec -it（带 -it 才能交互）")
            return 2

    code = extract_code(raw)
    if not code:
        print("没解析出 code，退出")
        return 2
    print(f"\n解析到 code：{code[:12]}...（长度 {len(code)}）")

    try:
        result = exchange(code, verifier)
    except Exception as e:  # noqa: BLE001
        print(f"\n{e}")
        return 1

    return _report(result)


def _report(result: dict[str, Any]) -> int:
    """统一打印 refresh_token 和后续步骤。"""
    token = result.get("refresh_token")
    if not token:
        print(f"\n响应里没有 refresh_token，完整返回：\n{result}")
        return 1

    user = result.get("user", {}) or {}
    print("\n" + "=" * 72)
    print(f"登录账号：{user.get('name', '?')}（pixiv id {user.get('account', '?')}）")
    print("=" * 72)
    print("\nPIXIV_REFRESH_TOKEN=" + token)
    print()
    print("下一步：")
    print("  1) 把上面这行写进项目根的 .env")
    print("  2) sudo docker compose up -d --force-recreate rsshub")
    print("  3) sudo docker exec qq-grok-bot python scripts/test_third_plugins.py pixiv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
