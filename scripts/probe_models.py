#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
模型探测：搞清楚你的 workspace 到底有哪些模型可用。

为什么需要它：
  `qwen-vl-max` 在部分阿里云 MaaS workspace 上没上架，直接返回
  404 {"code":"model_not_found"}，视觉就静默失效了。
  这个脚本会列出 /models 里真实存在的模型，并逐个用一张 1x1 图试一遍。

用法：
    python scripts/probe_models.py
    python scripts/probe_models.py --chat          # 顺便探测对话模型
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 1x1 透明 PNG
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AGP"
    "e8y0AAAAAElFTkSuQmCC"
)


def load_env():
    """手写 .env 解析，不依赖 python-dotenv，也不会覆盖已有环境变量。"""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


def get_models(base: str, key: str, timeout: float = 20):
    import httpx
    url = f"{base.rstrip('/')}/models"
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as c:
            r = c.get(url, headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        ids = [m.get("id") for m in (data.get("data") or data.get("models") or []) if isinstance(m, dict)]
        return ids, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def try_vision(base: str, key: str, model: str, timeout: float = 30):
    import httpx
    url = f"{base.rstrip('/')}/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," +
                                                base64.b64encode(PNG_1PX).decode()}},
            {"type": "text", "text": "这是什么颜色？两个字以内"},
        ]}],
        "max_tokens": 20,
    }
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as c:
            r = c.post(url, headers={"Authorization": f"Bearer {key}",
                                     "Content-Type": "application/json"}, json=payload)
        if r.status_code == 200:
            d = r.json()
            msg = d["choices"][0]["message"]
            return True, str(msg.get("content") or "")[:60]
        body = r.text[:160]
        low = body.lower()
        if r.status_code == 404 or "model_not_found" in low or "model not exist" in low:
            return False, "模型不存在"
        return False, f"HTTP {r.status_code}: {body}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def try_chat(base: str, key: str, model: str, timeout: float = 40):
    import httpx
    url = f"{base.rstrip('/')}/chat/completions"
    payload = {"model": model, "messages": [{"role": "user", "content": "回复一个字：好"}],
               "max_tokens": 50}
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as c:
            r = c.post(url, headers={"Authorization": f"Bearer {key}",
                                     "Content-Type": "application/json"}, json=payload)
        if r.status_code == 200:
            msg = r.json()["choices"][0]["message"]
            has_reason = bool(msg.get("reasoning_content"))
            return True, (str(msg.get("content") or "")[:40], has_reason)
        return False, r.text[:160]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


VISION_HINTS = ("vl", "vision", "omni", "visual")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chat", action="store_true", help="顺便探测对话/推理模型")
    args = ap.parse_args()
    load_env()

    key = os.getenv("DASHSCOPE_API_KEY", "")
    base = (os.getenv("DASHSCOPE_BASE_URL", "") or
            "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")

    print("=" * 68)
    print("  阿里云 MaaS / DashScope 模型探测")
    print("=" * 68)
    print(f"  端点：{base}")
    print(f"  Key ：{key[:10]}…{key[-4:] if len(key) > 14 else ''}（长度 {len(key)}）")
    if not key:
        print("\n  ❌ DASHSCOPE_API_KEY 为空，先配 .env")
        return 1

    print("\n[1] 拉取 /models 列表…")
    ids, err = get_models(base, key)
    if err:
        print(f"  ⚠️  拉不到列表：{err}")
        print("      （有些 workspace 不开放 /models，下面直接逐个试候选）")
        ids = []
    else:
        print(f"  ✅ 共 {len(ids)} 个模型")
        for i in ids:
            mark = "👁️ " if any(h in str(i).lower() for h in VISION_HINTS) else "   "
            print(f"     {mark}{i}")

    # 候选：列表里的视觉模型 + 常见名字兜底
    cands = [i for i in ids if any(h in str(i).lower() for h in VISION_HINTS)]
    for m in ("Qwen3.5-Flash", "qwen3.5-flash", "qwen-vl-max", "qwen-vl-max-latest",
              "qwen-vl-plus", "qwen3-vl-plus", "qwen2.5-vl-72b-instruct",
              "qwen2.5-vl-32b-instruct", "qwen2.5-vl-7b-instruct"):
        if m not in cands:
            cands.append(m)

    # 两个端点都试：workspace 专属端点 + 百炼公网端点
    endpoints = []
    for b in (base, "https://dashscope.aliyuncs.com/compatible-mode/v1"):
        if b and b.rstrip("/") not in [e.rstrip("/") for e in endpoints]:
            endpoints.append(b)

    good = []
    for ep in endpoints:
        print(f"\n[2] 端点 {ep}")
        print(f"    逐个测试视觉能力（共 {len(cands)} 个候选）…")
        for m in cands:
            if m in good:
                continue
            ok, info = try_vision(ep, key, m)
            if ok:
                print(f"  ✅ {m}  →  {info!r}")
                good.append(m)
            else:
                print(f"  ❌ {m}  →  {info}")

    print("\n" + "-" * 68)
    if good:
        print(f"  🎯 可用的视觉模型：{good}")
        print(f"     在 .env 里加一行：VISION_MODEL={good[0]}")
        print(f"     （或者 VISION_MODEL_CANDIDATES={','.join(good[:3])}）")
    else:
        print("  ❌ 这个 workspace 没有任何可用视觉模型。")
        print("     两个办法：")
        print("     1) 去阿里云百炼控制台给这个 workspace 开通 qwen-vl 系列模型")
        print("     2) 把 DASHSCOPE_BASE_URL 换成公网端点，用公网 key：")
        print("        DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1")

    # ---- 对话 / 推理模型 ----
    if args.chat:
        xbase = (os.getenv("XAI_BASE_URL", "") or "").rstrip("/")
        xkey = os.getenv("XAI_API_KEY", "")
        xmodel = os.getenv("XAI_MODEL", "")
        print("\n" + "=" * 68)
        print("  对话 / 推理模型探测")
        print("=" * 68)
        print(f"  端点：{xbase}")
        print(f"  当前：{xmodel}")
        if xbase and xkey:
            cids, cerr = get_models(xbase, xkey)
            if cerr:
                print(f"  ⚠️  拉不到列表：{cerr}")
                cids = [xmodel] if xmodel else []
            else:
                print(f"  ✅ 共 {len(cids)} 个：{cids[:20]}")
            for m in [xmodel] + [c for c in cids if c != xmodel][:5]:
                if not m:
                    continue
                ok, info = try_chat(xbase, xkey, m)
                if ok:
                    content, has_reason = info
                    flag = "🧠 有思维链(reasoning_content)" if has_reason else "  无思维链"
                    print(f"  ✅ {m}  →  {content!r}  {flag}")
                else:
                    print(f"  ❌ {m}  →  {info}")
        else:
            print("  ⚠️  XAI_BASE_URL / XAI_API_KEY 没配，跳过")

    print("\n" + "=" * 68)
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
