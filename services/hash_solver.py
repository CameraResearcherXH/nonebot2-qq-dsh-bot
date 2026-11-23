# services/hash_solver.py
"""哈希反查：群友发一串 32/40/64 位十六进制，让你猜原文。

为什么需要：
  2026-11-23 群友用「我爱你」做 SHA-256：
  c0ad5411b19cfcba9d674d21411a970159f6ae4e180831ddd6a91797be547752
  有的模型能“认出来”，其实不是解密，是猜常见短语 / 查哈希库。

边界（必须说清）：
  · 哈希是单向的，**不能解密**；只能拿候选原文逐个哈希比对。
  · 本地字典 + 4 位数字暴力，覆盖常见短语/口令；加盐/长口令查不到。
  · 不做外部请求，稳定、免费、不泄露原文。
"""
from __future__ import annotations

import hashlib
import re

# 32=MD5, 40=SHA1, 64=SHA256
_ALGO_BY_LEN = {32: "md5", 40: "sha1", 64: "sha256"}
_HASH_RE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32}|[0-9a-fA-F]{40}|[0-9a-fA-F]{64})(?![0-9a-fA-F])")

# 常见短语/口令。不是穷举，只覆盖“群里会拿来玩”的那一类。
_WORDS = """
我爱你
我喜欢你
爱你
我想你
我想你了
想你
喜欢你
爱你哟
永远爱你
我喜欢你很久了
做我女朋友吧
做我男朋友吧
你是我的
我的
老婆
老公
宝宝
宝贝
亲爱的
小可爱
笨蛋
傻瓜
猪
猫
喵
喵喵
喵喵喵
嘤嘤嘤
呜呜呜
呜呜
好耶
太棒了
厉害
绝了
离谱
无语
救命
完了
寄
摆烂
摸鱼
上班
下班
吃饭
睡觉
打游戏
原神
启动
王者
英雄联盟
二次元
猫娘
女仆
主人
主人大人
小鲸鱼
大肥鱼
白饭
阿明
你好
您好
谢谢
谢谢你
再见
晚安
早安
生日快乐
对不起
没关系
哈哈哈
哈哈哈哈
笑死
草
牛逼
傻逼
加油
抱抱
贴贴
亲亲
你好世界
世界
我爱你中国
中华人民共和国
中国共产党
社会主义核心价值观
hello
world
hello world
i love you
love you
love
iloveyou
i love you too
520
1314
5201314
1314520
5201314520
123456
123456789
12345678
111111
000000
password
admin
root
qwerty
abc123
letmein
secret
test
test123
1234
0000
""".split()


def detect(text: str):
    """返回 (hash, algo) 或 (None, None)。"""
    m = _HASH_RE.search(str(text or ""))
    if not m:
        return None, None
    h = m.group(1).lower()
    return h, _ALGO_BY_LEN[len(h)]


def _candidates():
    seen = set()

    def emit(x):
        x = str(x)
        if x and x not in seen:
            seen.add(x)
            return x
        return None

    for w in _WORDS:
        variants = {w, w.lower(), w.upper(), w.capitalize()}
        for v in variants:
            for suffix in ("", "。", "！", "!", "~", "～", " ", "\n"):
                c = emit(v + suffix)
                if c:
                    yield c
            c = emit(" " + w + " ")
            if c:
                yield c
    # 4 位数字口令（0000-9999），成本很低
    for i in range(10000):
        c = emit(f"{i:04d}")
        if c:
            yield c


_CANDIDATES = None


def _candidate_list():
    global _CANDIDATES
    if _CANDIDATES is None:
        _CANDIDATES = list(_candidates())
    return _CANDIDATES


def solve(text: str) -> dict:
    """解一个哈希。返回 {hash, algo, plain, tried}；plain 为空 = 没查到。"""
    h, algo = detect(text)
    if not h:
        return {"hash": "", "algo": "", "plain": "", "tried": 0}
    cands = _candidate_list()
    for c in cands:
        if hashlib.new(algo, c.encode("utf-8")).hexdigest() == h:
            return {"hash": h, "algo": algo, "plain": c, "tried": len(cands)}
    return {"hash": h, "algo": algo, "plain": "", "tried": len(cands)}
