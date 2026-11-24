# scripts/probe_glm.py
"""
探一下你的 GLM key 到底能用哪个模型（文本 + 视觉各测一遍）。

用法（项目根目录）：
    set GLM_API_KEY=你的key        # Windows
    export GLM_API_KEY=你的key     # Linux
    python scripts/probe_glm.py

它会按降级链把每个模型名都试一次，告诉你：
  · 哪个模型文本可用
  · 哪个模型视觉可用（能不能读图）
  · 你账号有没有 thinking 权限
然后把结论写进 data/glm_model.txt，代码下次直接用这个。
"""
import base64
import io
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_dotenv(path: Path) -> int:
    """
    把项目根的 .env 读进 os.environ。

    为什么必须自己读：`.env` 只在 docker-compose 里被注入成容器环境变量，
    你在宿主机直接 `python scripts/xxx.py` 时 shell 里是没有这些变量的
    —— 这就是「.env 明明写了 GLM_API_KEY，脚本却说没设置」的原因。
    """
    if not path.exists():
        return 0
    n = 0
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        v = re.split(r"\s+#", v, maxsplit=1)[0].strip()   # 去掉行内注释
        v = v.strip('"').strip("'")
        if k and not os.environ.get(k):
            os.environ[k] = v
            n += 1
    return n


_loaded = _load_dotenv(ROOT / ".env")

CANDIDATES = [
    "glm-5.3-flash", "glm-4-flash", "glm-4v-flash", "glm-4-air",
    "glm-4-flash-250414", "glm-4v-plus", "glm-4-plus",
]
URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"


def _post(key: str, payload: dict):
    """
    只用标准库 urllib。

    为什么不用 httpx：这是运维脚本，你在**宿主机**上跑，而 httpx 只装在 docker
    容器里 —— 直接用 httpx 会报 `ModuleNotFoundError: No module named 'httpx'`，
    看起来像「key 不对」，其实只是依赖不在。
    返回一个带 .status_code / .text / .json() 的小对象（和 httpx 的响应一样用）。
    网络异常抛出去，由调用方兜。
    """
    import json as _json
    import urllib.error
    import urllib.request

    data = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        URL, data=data, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return _Resp(r.status, r.read().decode("utf-8", errors="ignore"))
    except urllib.error.HTTPError as e:
        return _Resp(e.code, e.read().decode("utf-8", errors="ignore"))


class _Resp:
    """把 (status, text) 包成和 httpx 一样的 .status_code / .text / .json()"""

    def __init__(self, status: int, text: str):
        self.status_code = status
        self.text = text

    def json(self):
        import json as _json
        return _json.loads(self.text)


def _tiny_png_b64() -> str:
    """
    造一张 64×64 纯红 PNG，**只用标准库 zlib/struct**，不依赖 Pillow。
    宿主机上通常没装 Pillow，用 PIL 的话视觉这一项会被静默跳过。
    """
    try:
        import struct
        import zlib

        w = h = 64
        rgb = (220, 60, 60)
        raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

        def chunk(tag: bytes, data: bytes) -> bytes:
            body = tag + data
            return (struct.pack(">I", len(data)) + body
                    + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", ihdr)
               + chunk(b"IDAT", zlib.compress(raw, 9))
               + chunk(b"IEND", b""))
        return base64.b64encode(png).decode()
    except Exception:
        return ""


def main():
    key = os.getenv("GLM_API_KEY", "").strip()
    if _loaded:
        print(f"（已从 .env 读入 {_loaded} 个变量）")
    if not key:
        print("❌ 没设置 GLM_API_KEY")
        print(f"   找过：环境变量 + {ROOT / '.env'}")
        print("   检查 .env 里是不是写成了 `GLM_API_KEY = xxx`（等号两边有空格也能读，但键名别拼错）")
        return

    text_ok, vision_ok, thinking_ok = [], [], []
    img = _tiny_png_b64()

    print(f"探测 {len(CANDIDATES)} 个模型…\n")
    for m in CANDIDATES:
        # ---- 文本 ----
        try:
            # max_tokens 给 200：5.3 这类思考型模型会先用掉一部分，给 20 会返回空
            r = _post(key, {"model": m,
                            "messages": [{"role": "user", "content": "只回复两个字：可用"}],
                            "temperature": 0.1, "max_tokens": 200})
            if r.status_code == 200:
                msg = r.json()["choices"][0]["message"]
                txt = str(msg.get("content") or "").strip()
                rc = str(msg.get("reasoning_content") or "").strip()
                if not txt and rc:
                    print(f"  ⚠️  文本 {m} → content 空，reasoning_content 有 "
                          f"{len(rc)} 字（thinking 吃了 token，调大 max_tokens）")
                    text_ok.append(m)
                elif not txt:
                    print(f"  ⚠️  文本 {m} → HTTP 200 但返回空内容（不可用）")
                else:
                    print(f"  ✅ 文本 {m} → {txt[:20]!r}")
                    text_ok.append(m)
            else:
                print(f"  ✗  文本 {m} → HTTP {r.status_code} {r.text[:90]}")
        except Exception as e:
            print(f"  ✗  文本 {m} → {type(e).__name__}: {e}")

        # ---- 视觉 ----
        if not img:
            continue
        try:
            r = _post(key, {"model": m, "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": img}},
                    {"type": "text", "text": "这张图是什么颜色？只回复颜色。"},
                ]}], "max_tokens": 200})
            if r.status_code == 200:
                msg = r.json()["choices"][0]["message"]
                txt = str(msg.get("content") or "").strip()
                rc = str(msg.get("reasoning_content") or "").strip()
                if not txt and rc:
                    print(f"  ⚠️  视觉 {m} → content 空，reasoning 有 {len(rc)} 字"
                          f"（thinking 吃了 token，调大 max_tokens）")
                    vision_ok.append(m)
                elif not txt:
                    print(f"  ⚠️  视觉 {m} → HTTP 200 但返回空内容（不可用）")
                else:
                    print(f"  ✅ 视觉 {m} → {txt[:20]!r}")
                    vision_ok.append(m)
            else:
                print(f"  ✗  视觉 {m} → HTTP {r.status_code} {r.text[:90]}")
        except Exception as e:
            print(f"  ✗  视觉 {m} → {type(e).__name__}: {e}")

    # ---- thinking ----
    if text_ok:
        try:
            r = _post(key, {"model": text_ok[0],
                            "messages": [{"role": "user", "content": "1+1=?"}],
                            "max_tokens": 50,
                            "thinking": {"type": "enabled"}})
            if r.status_code == 200:
                thinking_ok.append(text_ok[0])
                print(f"\n  ✅ thinking 可用（{text_ok[0]}）")
            else:
                print(f"\n  ✗  thinking 不可用：HTTP {r.status_code} {r.text[:90]}")
        except Exception as e:
            print(f"\n  ✗  thinking 异常：{e}")

    print("\n" + "=" * 50)
    print(f"文本可用：{text_ok or '（无）'}")
    print(f"视觉可用：{vision_ok or '（无）'}")
    if not text_ok:
        print("\n❌ 一个都没通。检查：key 是否正确、账号是否开通了该模型、是否欠费。")
        return

    # 文本首选：优先 5.3（新、免费额度大），但它带思考、偶尔空响应；
    # 想要「快且稳」就选 glm-4-flash。
    pick_text = text_ok[0]
    pick_vision = vision_ok[0] if vision_ok else ""

    out = ROOT / "data" / "glm_model.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(pick_text, encoding="utf-8")

    print(f"\n已记住首选文本模型：{pick_text}（写入 {out}）")
    print("\n建议写进 .env：")
    print(f"  GLM_MODEL={pick_text}")
    if pick_vision:
        print(f"  GLM_VISION_MODEL={pick_vision}")
    print(f"  GLM_THINKING={'true' if thinking_ok else 'false'}")
    if thinking_ok:
        print("  # 开 thinking 的话记得调大 max_tokens，否则 content 会空：")
        print("  GLM_MAX_TOKENS=1500")

    if "glm-5.3-flash" in text_ok and "glm-4-flash" in text_ok:
        print("\n💡 想「快而稳」可以把 GLM_MODEL 设成 glm-4-flash（不思考、不吃 token）；")
        print("   想「更准但慢」就留 glm-5.3-flash + GLM_THINKING=true + GLM_MAX_TOKENS=1500。")


if __name__ == "__main__":
    main()

