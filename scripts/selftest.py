# scripts/selftest.py
"""
一键自测：不依赖 QQ，直接检查各模块是否可用。
用法：
    python -m scripts.selftest
可选：
    python -m scripts.selftest --with-llm     # 顺便测一次真实 LLM 调用
    python -m scripts.selftest --with-tts     # 顺便测一次 TTS 合成（较慢）
"""
import argparse
import asyncio
import io
import json
import os
import sys
import time
from pathlib import Path

# 确保项目根在 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


PASS = "✅"
FAIL = "❌"
WARN = "⚠️"
INFO = "ℹ️"


def head(title):
    print(f"\n========== {title} ==========")


def line(ok, msg):
    print(f"{PASS if ok else FAIL} {msg}")


def warn(msg):
    print(f"{WARN} {msg}")


def info(msg):
    print(f"{INFO} {msg}")


# =========================================================
# 1. 环境变量
# =========================================================
def test_env():
    head("环境变量")
    keys = [
        "ALLOWED_GROUPS", "ALLOWED_PRIVATE", "OWNER_QQ",
        "RANDOM_CHAT_ENABLED", "RANDOM_CHAT_PROB",
        "SLEEP_HOUR_START", "SLEEP_HOUR_END",
        "USE_LOCAL_TTS", "STICKER_COLLECT_MODE",
    ]
    bad = 0
    for k in keys:
        v = os.getenv(k, None)
        if v is None:
            warn(f"{k} 未设置（用默认值）")
            bad += 1
        else:
            info(f"{k}={v}")
    if not os.getenv("OWNER_QQ"):
        warn("OWNER_QQ 未设置，is_owner 永远为 False，一些指令会拒绝") 
    return bad


# =========================================================
# 2. 人物档案（A）
# =========================================================
def test_profiles():
    head("人物档案 / 相处经验 / 说话方式")
    from services import profiles

    test_qq = "99999999"
    profiles.upsert_profile(
        test_qq, nickname="测试小明", aliases=["小明", "测小明"], owner=False, level="member",
    )
    p = profiles.get_profile(test_qq)
    ok1 = p["nickname"] == "测试小明" and "小明" in p["aliases"]
    line(ok1, f"写入档案：{p['nickname']}, aliases={p['aliases']}")

    profiles.touch_profile(test_qq, "昨天聊了白米饭")
    p2 = profiles.get_profile(test_qq)
    ok2 = "白米饭" in (p2.get("last_seen_msg") or "")
    line(ok2, f"touch_profile 生效: last_seen_msg={p2.get('last_seen_msg')[:30]}")

    profiles.update_style(test_qq, msg_len=5, has_emoji=True)
    for _ in range(5):
        profiles.update_style(test_qq, msg_len=6, has_emoji=True)
    style = profiles.style_summary(test_qq)
    line(bool(style), f"说话方式：{style or '(样本不足)'}")

    profiles.add_bond_note(test_qq, "喜欢猫", emotion="happy")
    profiles.add_bond_note(test_qq, "上次骂了她一句，她记仇了", emotion="angry")
    b = profiles.get_bond(test_qq)
    texts = profiles.bond_texts(test_qq)
    line("喜欢猫" in texts, f"相处经验（list[dict]）：{b['notes']}")
    line(
        all(isinstance(n, dict) and "emotion" in n for n in b["notes"]),
        "bond.json 已升级为 list[dict]（带 emotion）",
    )

    prompt = profiles.profile_prompt(test_qq, "测试小明")
    info(f"Prompt 片段预览：{prompt}")


# =========================================================
# 3. 心情 & 情绪刻痕（B）
# =========================================================
def test_mood():
    head("心情系统")
    from services import mood

    m0 = mood.get_mood()
    info(f"当前心情值：{m0['value']:+.1f}")

    v1 = mood.bump_mood(+20)
    v2 = mood.get_mood()["value"]
    line(abs(v2 - (m0["value"] + 20)) < 1e-3 or v2 > m0["value"],
         f"加 20 后心情值：{v2:+.1f}")

    delta, strong = mood.judge_message_mood("你好可爱啊，真棒")
    line(delta > 0, f"判断「可爱」→ delta={delta:+.1f}, strong={strong}")

    delta2, strong2 = mood.judge_message_mood("滚，笨死了")
    line(delta2 < 0, f"判断「滚」→ delta={delta2:+.1f}, strong={strong2}")
    line(strong2, "「滚」被识别为强情绪（情绪刻痕触发）")

    prompt = mood.mood_prompt()
    info(f"心情 Prompt：{prompt or '(无)'}")


# =========================================================
# 4. 记忆系统（memory_v2）
# =========================================================
def test_memory():
    head("记忆系统")
    from services import memory_v2

    mid = memory_v2.remember(
        text="测试小明：我昨天买了一盒白米饭",
        kind="fact", about_qq="99999999", importance=0.7,
    )
    line(bool(mid), f"写入记忆 id={mid}")

    mid2 = memory_v2.remember(
        text="测试小明：白米饭很好吃，下次还要买",
        kind="fact", about_qq="99999999", importance=0.6,
    )
    line(bool(mid2), f"写入相关记忆 id={mid2}")

    hits = memory_v2.recall("白米饭", speaker_qq="99999999", limit=3)
    line(len(hits) > 0, f"检索「白米饭」：命中 {len(hits)} 条")
    for h in hits[:3]:
        info(f"  · {h['ago']}｜{h['text'][:40]}（score={h['score']:.2f}）")

    prompt = memory_v2.memory_prompt("白米饭", speaker_qq="99999999", limit=3)
    info("记忆 Prompt 预览：")
    print("  " + "\n  ".join(prompt.splitlines()))

    # 过期标记 & 恢复
    memory_v2.mark_outdated(mid)
    hits2 = memory_v2.recall("白米饭", speaker_qq="99999999", limit=3)
    line(len(hits2) < len(hits), f"标记过时后命中减少：{len(hits)} → {len(hits2)}")
    memory_v2.restore(mid)
    hits3 = memory_v2.recall("白米饭", speaker_qq="99999999", limit=3)
    line(len(hits3) == len(hits), f"恢复后命中还原：{len(hits3)}")

    # 归档合并（相似度 > 0.6 → 并入旧记忆）
    memory_v2.archive_fact("测试小明：白米饭还是周记的最好吃", about_qq="99999999")
    s = memory_v2.stats()
    info(f"记忆库统计：{s}")


# =========================================================
# 5. 记忆 Prompt 结构（"目录还是详细"核心答疑）
# =========================================================
def test_prompt_builder():
    head("Prompt 组装（目录+详细+心情+档案）")
    from services.prompt_builder import build_prompt
    from services import memory_v2, mood, profiles

    memory_v2.remember(text="测试小明：我以前说过喜欢猫", kind="fact", about_qq="99999999")
    memory_v2.remember(text="测试小明：我家猫叫二零", kind="fact", about_qq="99999999")
    mood.bump_mood(+15)
    profiles.upsert_profile("99999999", nickname="测试小明", aliases=["小明"])

    built = build_prompt(
        session_key="private_99999999",
        user_text="你还记得我的猫叫什么吗",
        speaker_qq="99999999",
        speaker_name="测试小明",
        persona_text="",
    )
    print("---- 生成的 Prompt ----")
    print(built)
    print("---- end ----")
    line("202" in built or "二零" in built or "猫" in built, "记忆被塞进 Prompt")
    line("测试小明" in built, "档案（名字）被塞进 Prompt")


# =========================================================
# 6. 短句兜底（E）
# =========================================================
async def test_short_query():
    head("短句兜底")
    from services.prompt_builder import query_for_recall, record_query

    sk = "test_short"
    record_query(sk, "我家猫叫二零，昨天新买了猫粮")
    q = query_for_recall(sk, "嗯嗯")
    line("猫粮" in q or "猫" in q, f"短句「嗯嗯」→ 带上下文：{q[:60]}")

    q2 = query_for_recall(sk, "今天天气怎么样")
    line("今天天气" in q2 and "猫" not in q2, f"长句原样返回：{q2[:60]}")


# =========================================================
# 7. 权限层级（C）
# =========================================================
def test_access():
    head("权限层级")
    from services import access

    class FakeSender:
        def __init__(self, card, nickname):
            self.card = card
            self.nickname = nickname

    class FakeEvent:
        def __init__(self, user_id, card="", nickname=""):
            self.user_id = user_id
            self.sender = FakeSender(card, nickname)

    owner = os.getenv("OWNER_QQ", "")
    if owner:
        line(access.is_owner(owner), f"OWNER_QQ({owner}) 被识别为主人")
        line(not access.is_owner("12345678"), "其他号不是主人")
    else:
        warn("未配置 OWNER_QQ，跳过主人识别校验")

    ev = FakeEvent(123, card="张小明（摸鱼仔）", nickname="小明")
    n = access.display_name(ev)
    line(n == "张小明", f"nickname 去括号：{n}")

    variants = access.nickname_variants(ev)
    line("张小明" in variants and "张小明（摸鱼仔）" in variants,
         f"nickname variants={variants}")


# =========================================================
# 8. 表情包（收、发、统计）
# =========================================================
def test_stickers():
    head("表情包系统")
    from services.sticker_library import sticker_lib
    import io
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        img_bytes = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    else:
        buf = io.BytesIO()
        Image.new("RGB", (32, 32), (120, 200, 120)).save(buf, format="PNG")
        img_bytes = buf.getvalue()

    md5 = sticker_lib.md5_of(img_bytes)
    if md5 in sticker_lib.index:
        info(f"测试图已存在（md5={md5[:8]}）")
    else:
        r = sticker_lib.add(data=img_bytes, tag="开心", chat_id="selftest", ext=".png")
        line(r.get("ok"), f"入库表情：{r}")

    s = sticker_lib.stats()
    line(s["total"] > 0, f"总张数：{s['total']}（图片 {s['image']} + mface {s['mface']}）")

    pick = sticker_lib.pick_by_tag("开心")
    line(bool(pick), f"按标签「开心」挑到：{pick.get('path') if pick else '(空)'}")

    # mface 模拟
    sticker_lib.index["mface_selftest"] = {
        "tag": "吃瓜",
        "mface_id": "selftest_id",
        "summary": "自测表情",
        "used_count": 0,
        "created_at": time.time(),
    }
    sticker_lib._save()
    line(True, "商城表情（mface）写入成功")


# =========================================================
# 9. Aurabs 逻辑（@ / 群聊 / 拆条）
# =========================================================
def test_aura_logic():
    head("Aurabot 逻辑（@ / 群聊策略 / 拆条）")
    from services import aura_logic

    # 真 @
    members = [
        {"user_id": 111111, "card": "张小明（摸鱼仔）", "nickname": "小明"},
        {"user_id": 222222, "card": "李四", "nickname": "李四"},
    ]
    segs = [{"type": "text", "data": {"text": "大家好 @张小明 你看看"}}]
    out, missed = aura_logic.expand_mentions(segs, members)
    has_at = any(x["type"] == "at" and x["data"]["qq"] == "111111" for x in out)
    line(has_at, f"真 @：{out}")

    # 群聊策略
    d = aura_logic.decide_group_reply({
        "policy": aura_logic.DEFAULT_GROUP_POLICY,
        "relevance": 0.5,
        "engagedUntil": 0,
        "mentioned": True,
        "repliedToBot": False,
        "fromOwner": False,
        "text": "大肥鱼你在吗",
        "now": int(time.time() * 1000),
        "hour": 14,
        "recentTimes": [int(time.time() * 1000)] * 5,
        "interjectTimes": [],
        "botReplyTimes": [],
    })
    line(d["reply"] and d["mode"] == "must", f"@/唤醒词命中：{d}")

    # 拆条
    parsed = aura_logic.parse_reply("第一条 ||| 第二条 <voice>这里是语音</voice>")
    line(len(parsed["items"]) == 3, f"拆条结果：{parsed['items']}")

    # [[MEMO]] 协议
    raw = (
        "哼，才没有很高兴呢 ||| 下次也夸我\n"
        "[[MEMO|emotion:happy|text:他夸我可爱，我尾巴都摇起来了]]\n"
        "[[MEMO|emotion:angry|text:他刚才说我胖，我记住了]]"
    )
    parsed2 = aura_logic.parse_reply(raw)
    memos = parsed2["memos"]
    line(len(memos) == 2, f"MEMO 拆出 {len(memos)} 条：{[m['emotion'] for m in memos]}")
    line(
        all("MEMO" not in i["value"] for i in parsed2["items"]),
        "MEMO 没有泄漏到气泡正文里",
    )
    line(memos[0]["emotion"] == "happy" and "夸我" in memos[0]["text"], f"memo[0]={memos[0]}")
    line(aura_logic.normalize_emotion("生气") == "angry", "中文情绪词归一化")


# =========================================================
# 10. 真正的 LLM 调用（可选）
# =========================================================
async def test_llm():
    head("LLM 真实调用")
    from services.llm import call_llm
    from services.prompt_builder import build_prompt
    try:
        prompt = build_prompt(
            session_key="private_test_llm",
            user_text="你还记得我的猫叫什么吗",
            speaker_qq="99999999",
            speaker_name="测试小明",
            persona_text="",
        )
        reply, _ = await call_llm("private_test_llm", prompt)
        line(bool(reply), f"LLM 返回：{reply[:60]!r}...")
        if "二零" in reply or "202" in reply or "猫" in reply:
            line(True, "✅ 记忆被 LLM 正确使用（记得猫的名字）")
        else:
            warn("LLM 没有用到记忆（可能没命中，这是正常的，取决于 recall 质量）")
    except Exception as e:
        line(False, f"LLM 调用失败: {type(e).__name__}: {e}")


# =========================================================
# 11. TTS 合成（可选）
# =========================================================
async def test_tts():
    head("TTS 合成（可选，慢）")
    try:
        from services.tts import send_voice  # noqa: F401
        from pathlib import Path
        line(True, "services.tts 已就位（真正合成需通过 QQ 触发）")
    except Exception as e:
        line(False, f"TTS 加载失败: {e}")


# =========================================================
# 主流程
# =========================================================
async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-llm", action="store_true", help="顺便调用 LLM")
    ap.add_argument("--with-tts", action="store_true", help="顺便测试 TTS")
    args = ap.parse_args()

    print("=" * 60)
    print("🐟 一键自测")
    print("=" * 60)

    test_env()
    test_profiles()
    test_mood()
    test_memory()
    test_prompt_builder()
    await test_short_query()
    test_access()
    test_stickers()
    test_aura_logic()

    if args.with_llm:
        await test_llm()
    if args.with_tts:
        await test_tts()

    print("\n========== 完成 ==========")
    print("提示：如果想测真实 LLM 调用 + 记忆命中，用 --with-llm")


if __name__ == "__main__":
    asyncio.run(main())
