# scripts/test_stickers.py
"""
测表情三条通路（不依赖 QQ，宿主机直接跑）：

  ① 打标签通路   services.sticker_receiver.classify_sticker_by_vision
                 （GLM 优先 → qwen 兜底 → 关键词映射到 TAG_LIBRARY）
  ② 收表情落库   sticker_lib.add → pick_by_tag → sticker_sender._pick
  ③ 发送通路     sticker_sender._pick / 库存健康度（真实发送用 QQ 里 /发表情 测）

用法（项目根目录）：
    python scripts/test_stickers.py

它会自己读 .env（GLM_API_KEY / DASHSCOPE_API_KEY 等），
这样视觉模型可用时就是真打标签；不可用时也能验证「不抛异常、兜底标签」这条链路。
"""
import asyncio
import base64
import os
import re
import sys
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))


def _load_dotenv(path: Path) -> int:
    """把项目根 .env 读进 os.environ（宿主机直接跑时 shell 里没有这些变量）。"""
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
        v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
        v = v.strip('"').strip("'")
        if k and not os.environ.get(k):
            os.environ[k] = v
            n += 1
    return n


_loaded = _load_dotenv(ROOT / ".env")

from services.sticker_receiver import coerce_tag, classify_sticker_by_vision  # noqa: E402
from services.sticker_library import sticker_lib, TAG_LIBRARY  # noqa: E402
from services import sticker_sender  # noqa: E402


def _tiny_png() -> bytes:
    """造一张 64×64 纯红 PNG（只用标准库，不依赖 Pillow）。"""
    w = h = 64
    rgb = (220, 60, 60)
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _sec(title: str):
    print("\n" + "=" * 56)
    print(title)
    print("=" * 56)


def test_coerce():
    _sec("① 自由词 → 表情库标签（coerce_tag，纯本地）")
    cases = [
        ("笑死", "开心"), ("哈哈哈", "开心"), ("开心", "开心"),
        ("无语", "无语"), ("翻白眼", "无语"), ("委屈", "委屈"),
        ("哭", "哭"), ("狗头", "狗头"), ("doge", "狗头"),
        ("点赞", "点赞"), ("惊讶", "惊讶"), ("吃瓜", "吃瓜"),
        ("完全不搭的词", ""),
    ]
    ok = True
    for word, want in cases:
        got = coerce_tag(word)
        mark = "✅" if got == want else "❌"
        if got != want:
            ok = False
        print(f"  {mark} coerce_tag({word!r}) = {got!r}  期望 {want!r}")
    return ok


async def test_classify():
    _sec("② 用户给图 → 打标签（classify_sticker_by_vision，走视觉模型）")
    png = _tiny_png()
    print(f"  测试图：{len(png)} 字节 PNG（纯红 64×64）")
    print(f"  GLM key：{'有' if os.getenv('GLM_API_KEY') else '无（会走 qwen/兜底）'}")
    try:
        tag, desc = await classify_sticker_by_vision(png)
    except Exception as e:
        print(f"  ❌ 抛异常：{type(e).__name__}: {e}")
        return False
    print(f"  结果：tag={tag!r}  desc={desc[:80]!r}")
    if tag in TAG_LIBRARY:
        print(f"  ✅ tag 在 TAG_LIBRARY 里")
    else:
        print(f"  ⚠️ tag 不在 TAG_LIBRARY（{tag!r}），发表情时可能发不出去")
    return bool(tag)


def test_add_and_pick():
    _sec("③ 收表情落库 + 挑一张能发的（add → pick_by_tag → _pick）")
    png = _tiny_png()
    md5 = sticker_lib.md5_of(png)
    if md5 in sticker_lib.index:
        print(f"  ⚠️ 测试图 md5={md5[:10]} 已在库里，跳过 add（清理后重跑）")
        return False

    r = sticker_lib.add(data=png, tag="吃瓜", chat_id="test_stickers",
                        ext=".png", desc="测试纯红图", quality=0.5)
    if not r.get("ok"):
        print(f"  ❌ add 失败：{r}")
        return False
    print(f"  ✅ add 成功：md5={md5[:10]} path={Path(r['path']).name}")

    pick = sticker_lib.pick_by_tag("吃瓜")
    print(f"  ✅ pick_by_tag('吃瓜') → {pick and pick['md5'][:10]}（tag={pick and pick.get('tag')}）")

    p, tried = sticker_sender._pick("吃瓜")
    print(f"  ✅ sticker_sender._pick('吃瓜') → {p and p['md5'][:10]}  路径={tried}")

    # 清理：别把测试图留在真库
    try:
        sticker_lib.index.pop(md5, None)
        sticker_lib._save()
        Path(str(r.get("path") or "")).unlink(missing_ok=True)
        print("  🧹 已清理测试条目")
    except Exception as e:
        print(f"  ⚠️ 清理失败：{e}")
    return True


def test_health():
    _sec("④ 库存健康度（真实发送前先看这个）")
    h = sticker_lib.health()
    s = sticker_lib.stats()
    print(f"  总条目 {h['total']}（上限 {s['limit']}），能发 {h['usable']}，"
          f"坏文件 {h['broken_file']}，商城 {h['mface']}")
    print(f"  今日新收 {s['created_today']}，累计用过 {sticker_lib.total_used()} 次")
    if h["usable"] == 0:
        print("  ⚠️ 一张都发不出去：/清理表情 或 /导入表情")
    return True


def main():
    print(f"（已从 .env 读入 {_loaded} 个变量）" if _loaded else "（未找到 .env）")

    all_ok = True
    all_ok &= test_coerce()
    all_ok &= asyncio.run(test_classify())
    all_ok &= test_add_and_pick()
    all_ok &= test_health()

    print("\n" + "=" * 56)
    if all_ok:
        print("✅ 三条通路都跑通了。")
    else:
        print("⚠️ 有失败项，看上面对应行。")
    print("\n💡 真机验证（QQ 里）：")
    print("  · 收表情：群/私聊随便发一张自定义表情，看 docker logs 的 [STICKER] 收了一张/暂存")
    print("  · 给图分标签：发图 + /识表情")
    print("  · 发送表情：/发表情 搞怪")
    print("  · 看库存：/表情统计 或 /表情健康")


if __name__ == "__main__":
    main()
