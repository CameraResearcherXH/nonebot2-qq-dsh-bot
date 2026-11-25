# services/attention.py
"""
注意力漂移（attention drift）——把「走神/联想」做成可调的风格，而不是靠运气。

参考 MaiBot 的 attention_drift（只借思路，文案自己写）。

为什么需要它（用户 2026-11-19）：
  我们现在的约束全是**负向**的：「只接一个线程」「别总结」「别替别人说话」。
  模型被管得很老实，结果是：要么正面回答，要么完全不接 —— 缺少真人那种
  「被一个细节勾走一下，然后拐回来」的活气。

三个维度（每个 3~4 档）：
  · drift    漂移强度：subtle / active / scattered / wild
  · anchor   回钩策略：strict / balanced / loose
  · reaction 短反应风格：reserved / natural / lively

硬约束（写在每一档里，防止变成"胡说八道"）：
  · 每次漂移必须能在最近消息里找到触发点；
  · 漂移 ≠ 换话题：先抓住那个点，再拐回来；
  · 每轮最多一次明显跳跃；
  · 不能无视明确提问；
  · 不要自称分心 / 不要提 ADHD。

配置（.env，改了要重启）：
  ATTENTION_DRIFT=active        # subtle|active|scattered|wild|off
  ATTENTION_ANCHOR=balanced     # strict|balanced|loose
  ATTENTION_REACTION=natural    # reserved|natural|lively
"""

import os

from . import ctx_budget

_DRIFT = {
    "subtle": "漂移档位：轻微。只在最近消息里有**非常自然**的触发点时才轻轻联想一句，大多数时候继续当前话题。",
    "active": "漂移档位：活跃。可以主动抓住新鲜、好笑、反差强或熟悉的细节接话，但回复仍要清楚、短、能被最近消息解释。",
    "scattered": "漂移档位：明显发散。可以被支线、关键词、熟人语气或反差点勾走，先接住那个点再回到正题；一轮里允许一次可理解的突然拐弯。",
    "wild": "漂移档位：强烈跳跃。可以先被最有趣的细节劫走一下，出现短插话、突然联想或半路拐弯；但每轮最多一次明显跳跃。",
}

_ANCHOR = {
    "strict": "回钩策略：严格。联想或短反应之后，立刻回到当前正在聊的主题或被回复的那个人。",
    "balanced": "回钩策略：自然。可以短暂沿着支线说一句，但结尾或主要意思要回到当前聊天。",
    "loose": "回钩策略：宽松。可以保留更自由的相关联想，但不能凭空换话题，也不能无视明确提问。",
}

_REACTION = {
    "reserved": "短反应：少量。只有特别适合接话时，才用一句很短的反应开头。",
    "natural": "短反应：自然。可以偶尔先用短句、吐槽或语气词接住话题，再继续正常回复。",
    "lively": "短反应：活泼。更容易先用短促反应开头，但别把回复拆得太碎，也别每次都这样。",
}

DRIFT_LEVEL = (os.getenv("ATTENTION_DRIFT", "active") or "active").strip().lower()
ANCHOR_POLICY = (os.getenv("ATTENTION_ANCHOR", "balanced") or "balanced").strip().lower()
REACTION_STYLE = (os.getenv("ATTENTION_REACTION", "natural") or "natural").strip().lower()

ENABLED = DRIFT_LEVEL not in ("off", "none", "0", "false", "")


def render(level: str = "", anchor: str = "", reaction: str = "") -> str:
    """渲染成一段可直接塞进 prompt 的文本；关闭时返回空串。"""
    lv = (level or DRIFT_LEVEL).strip().lower()
    if lv in ("off", "none", "0", "false", ""):
        return ""
    an = (anchor or ANCHOR_POLICY).strip().lower()
    rc = (reaction or REACTION_STYLE).strip().lower()
    # ⚠️ 不要自带 `▍标题`：调用方（_behaviour_protocol 的「02d 注意力风格」）
    #    已经给了标题，再带一层就会出现「▍02d 注意力风格」下面又一行「▍注意力风格…」，
    #    模型看到两个同级标题，反而不知道哪个是主。
    text = "\n".join([
        "· 可以被新鲜/好笑/反差强/熟人的细节短暂吸引，但必须有触发点；"
        "先接住再拐回正题，每轮最多一次明显跳跃。",
        "· 别人明确在问你什么就先答那个；不要自称分心、不要提 ADHD、不要说「我走神了」。",
        "· " + _DRIFT.get(lv, _DRIFT["active"]),
        "· " + _ANCHOR.get(an, _ANCHOR["balanced"]),
        "· " + _REACTION.get(rc, _REACTION["natural"]),
    ])
    return ctx_budget.observe("rhythm", text, source=lv)


def status() -> str:
    """给 /测函数 用：现在是什么档位。"""
    if not ENABLED:
        return f"注意力漂移：关闭（ATTENTION_DRIFT={DRIFT_LEVEL}）"
    return (f"注意力漂移：{DRIFT_LEVEL}｜回钩 {ANCHOR_POLICY}"
            f"｜短反应 {REACTION_STYLE}")

