#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
外部翻唱 API 客户端（调试用，只用标准库，任何机器直接跑）

用法：
    # 1. 先测认证通不通（秒回）
    python cover_client.py --ping

    # 2. 翻唱（人声 + 伴奏），默认存到 ./晴天.mp3
    python cover_client.py --query "晴天"

    # 3. 清唱（只要人声）
    python cover_client.py --query "晴天" --mode acapella

    # 4. 直接甩网易云链接
    python cover_client.py --query "https://music.163.com/song?id=186016"

    # 5. 强制性别 / 强制音高
    python cover_client.py --query "晴天" --gender male --pitch 11

    # 6. 完整参数
    python cover_client.py --url http://1.2.3.4:8080 --key your_key \\
        --query "晴天" --mode cover --out test.mp3 --debug

环境变量（省得每次都敲）：
    COVER_API_URL   默认 http://127.0.0.1:8080
    COVER_API_KEY   必填，和服务器 .env 里的 COVER_API_KEY 一致

⚠️ 翻唱是同步长请求，正常 2~5 分钟。这个脚本的 socket 超时设了 900 秒，
   用 httpx/requests 自己写客户端的话记得也设长（httpx 默认 5 秒必失败）。
"""
import argparse
import json
import os
import socket
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_TIMEOUT = 900


def _p(msg: str):
    print(msg, flush=True)


def _spin(stop_event: threading.Event, started: float):
    """后台打点，让长请求看起来不是卡死。"""
    dots = 0
    while not stop_event.wait(10):
        dots += 1
        _p(f"  ... 已等待 {int(time.time() - started)}s" + "." * (dots % 4))


def stats(url: str, key: str, debug: bool = False) -> bool:
    """查各 key 的调用统计（审计 / 计费）。"""
    target = url.rstrip("/") + "/api/cover/stats"
    req = urllib.request.Request(target, headers={"X-Api-Key": key})
    if debug:
        _p(f"[debug] GET {target}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
            _p(f"✅ stats OK  你是 {data.get('you')}，共 {data.get('keys_total')} 个 key")
            q = data.get("queue") or {}
            if q:
                _p(f"   并发占用 {q.get('running')}/{q.get('max_concurrent')}，"
                   f"空闲 {q.get('free')} 个槽位")
            for u in data.get("usage") or []:
                last = u.get("last") or 0
                when = time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "-"
                _p(f"   {u.get('owner'):<12} 调用 {u.get('calls'):>3} 次  "
                   f"失败 {u.get('fails'):>2} 次  共 {u.get('mb')} MB  最后 {when}")
            never = data.get("never_used") or []
            if never:
                _p(f"   从未使用: {', '.join(never)}")
            return True
    except urllib.error.HTTPError as e:
        _p(f"❌ stats 失败  HTTP {e.code}")
        _p(f"   {e.read().decode('utf-8', 'replace')}")
        return False
    except Exception as e:
        _p(f"❌ stats 异常: {type(e).__name__}: {e}")
        return False


def ping(url: str, key: str, debug: bool = False) -> bool:
    target = url.rstrip("/") + "/api/cover/ping"
    req = urllib.request.Request(target, headers={"X-Api-Key": key})
    if debug:
        _p(f"[debug] GET {target}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode("utf-8", "replace")
            _p(f"✅ ping OK  HTTP {r.status}")
            _p(f"   {body}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        _p(f"❌ ping 失败  HTTP {e.code}")
        _p(f"   {body}")
        if e.code == 401:
            _p("   → key 不对，检查 --key 和服务器 .env 的 COVER_API_KEY")
        elif e.code == 503:
            _p("   → 服务器没配 COVER_API_KEY")
        elif e.code == 404:
            _p("   → 路径不对，或者 cover_api 插件没加载（看 docker logs）")
        return False
    except Exception as e:
        _p(f"❌ ping 异常: {type(e).__name__}: {e}")
        _p("   → 检查 URL 对不对、防火墙、服务器是否在跑")
        return False


def request_cover(url: str, key: str, payload: dict, out: str, debug: bool = False) -> bool:
    target = url.rstrip("/") + "/api/cover"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        target, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Api-Key": key},
    )
    _p(f"→ POST {target}")
    _p(f"  {json.dumps(payload, ensure_ascii=False)}")
    if debug:
        _p(f"[debug] 超时 {DEFAULT_TIMEOUT}s，翻唱正常要 2~5 分钟，请耐心等")

    started = time.time()
    stop = threading.Event()
    t = threading.Thread(target=_spin, args=(stop, started), daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as r:
            cost = time.time() - started
            meta = {k: v for k, v in r.headers.items() if k.lower().startswith("x-cover")}
            title = urllib.parse.unquote(meta.get("X-Cover-Title", ""))
            blob = r.read()
            if not blob:
                _p("❌ 服务器返回空内容")
                return False
            with open(out, "wb") as f:
                f.write(blob)
            _p(f"\n✅ 成功  用时 {cost:.1f}s（服务端 {meta.get('X-Cover-Cost', '?')}s）")
            _p(f"   身份   {urllib.parse.unquote(meta.get('X-Cover-Owner', '?'))}")
            _p(f"   歌名   {title}")
            _p(f"   模式   {meta.get('X-Cover-Mode', '?')}")
            _p(f"   性别   {meta.get('X-Cover-Gender', '?')}")
            _p(f"   音高   {meta.get('X-Cover-Pitch', '?')} 半音")
            _p(f"   大小   {len(blob) / 1048576:.2f} MB → {os.path.abspath(out)}")
            if debug:
                _p(f"[debug] 响应头 {json.dumps(meta, ensure_ascii=False)}")
            return True
    except urllib.error.HTTPError as e:
        stop.set()
        body = e.read().decode("utf-8", "replace")
        _p(f"\n❌ 失败  HTTP {e.code}")
        _p(f"   {body}")
        if e.code == 401:
            _p("   → key 不对")
        elif e.code == 400:
            _p("   → 参数有问题（query 为空？）")
        elif e.code == 500:
            _p("   → 服务端处理失败，看上面 error 字段和服务器日志 [SONG]/[COVER-API]")
        return False
    except socket.timeout:
        stop.set()
        _p(f"\n❌ 超时（{DEFAULT_TIMEOUT}s）—— 可能算力机没在线，或者任务卡住了")
        return False
    except Exception as e:
        stop.set()
        _p(f"\n❌ 异常: {type(e).__name__}: {e}")
        return False
    finally:
        stop.set()


def main():
    ap = argparse.ArgumentParser(
        description="外部翻唱 API 客户端",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--url", default=os.getenv("COVER_API_URL", "http://127.0.0.1:8080"),
                    help="服务器地址（默认取 COVER_API_URL）")
    ap.add_argument("--key", default=os.getenv("COVER_API_KEY", ""),
                    help="API key（默认取 COVER_API_KEY）")
    ap.add_argument("--query", help="歌名 / 网易云链接 / 歌曲 id")
    ap.add_argument("--mode", default="cover", choices=["cover", "acapella"],
                    help="cover=人声+伴奏（默认），acapella=清唱")
    ap.add_argument("--gender", default="", choices=["", "male", "female"],
                    help="强制覆盖原唱性别判断")
    ap.add_argument("--pitch", type=int, default=None,
                    help="强制覆盖音高（半音，比如 11）")
    ap.add_argument("--out", help="输出文件（默认 <歌名>.mp3）")
    ap.add_argument("--ping", action="store_true", help="只测认证，不翻唱")
    ap.add_argument("--stats", action="store_true", help="查各 key 的调用统计")
    ap.add_argument("--debug", action="store_true", help="打印详细日志")
    args = ap.parse_args()

    if not args.key:
        _p("❌ 缺少 key：用 --key 传，或者设环境变量 COVER_API_KEY")
        sys.exit(2)

    if args.ping:
        sys.exit(0 if ping(args.url, args.key, args.debug) else 1)

    if args.stats:
        sys.exit(0 if stats(args.url, args.key, args.debug) else 1)

    if not args.query:
        _p("❌ 缺少 --query（歌名 / 网易云链接 / 歌曲 id）")
        sys.exit(2)

    payload = {"query": args.query, "mode": args.mode}
    if args.gender:
        payload["gender"] = args.gender
    if args.pitch is not None:
        payload["pitch"] = args.pitch

    out = args.out
    if not out:
        base = args.query
        if base.startswith("http") or base.isdigit():
            base = "cover"
        base = "".join(c for c in base if c not in '\\/:*?"<>|').strip()[:40] or "cover"
        out = f"{base}.mp3"

    ok = request_cover(args.url, args.key, payload, out, args.debug)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
