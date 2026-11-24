# services/view_frequency.py
"""
「查看频率」——决定她这段时间**愿不愿意开口**的惯性模型。

问题（用户 2026-11-19，参考 MaiBot 的 view_frequency）：
  我们原来是「冷却 + 固定概率」：到点了就掷一次骰子，掷中就说。
  结果是要么一整天不说话，要么连着插三句 —— 没有惯性，不像人。

人是怎么样的：
  · 刚聊过一阵 → 接下来会自然安静一会儿（不需要谁来关她嘴）；
  · 安静久了 → 再有人说话时她更愿意接；
  · 如果一段对话她连着说了好几轮 → 后面会明显收敛。

做法（和 MaiBot 同构，但按我们的量级重写）：
  · 外部频率 external：配置/心情/关系算出来的基础概率；
  · 内部频率 internal：真正决定放行概率的那个值；
  · activate()：一旦真的开始思考，internal 从 external 起步，进入 decay；
  · record_run()：每跑完一轮扣 1/16，扣到 0 进 recovery；
  · recovery：每次判定 +1/16，16 次回到 external；期间又 activate 会重新开始 decay。

外部 0.35 时，大约连说 5~6 轮就自然收敛；外部 1.0 时能连说 16 轮。
"""

DECAY_STEP = 1.0 / 16
RECOVERY_ROUNDS = 16


class ViewFrequency:
    """一个会话一个实例。纯内存、无 IO、可随时重建。"""

    __slots__ = ("phase", "internal_frequency", "recovery_rounds")

    def __init__(self) -> None:
        self.phase = "idle"          # idle / decay / recovery
        self.internal_frequency = 0.0
        self.recovery_rounds = 0

    # ---------------- 判定 ----------------
    def probability(self, external: float) -> float:
        """当前这一刻的放行概率。"""
        ext = min(1.0, max(0.0, float(external or 0.0)))
        if ext <= 0:
            return 0.0
        if self.phase == "decay":
            # 衰减期内已经「热身」过了，后面每一轮都直接放行
            return 1.0
        if self.phase == "recovery":
            return ext * self.recovery_rounds / RECOVERY_ROUNDS
        return ext

    def decide(self, external: float, draw: float) -> bool:
        """
        掷一次骰子。draw 由调用方传入（0~1 的随机数），方便测试。

        每个「机会」只判一次；recovery 期内即使没通过也推进恢复进度。
        """
        ext = min(1.0, max(0.0, float(external or 0.0)))
        if self.phase == "idle":
            self.internal_frequency = ext
        accepted = float(draw) < self.probability(ext)
        if self.phase == "recovery":
            self.recovery_rounds += 1
            self.internal_frequency = ext * self.recovery_rounds / RECOVERY_ROUNDS
            if self.recovery_rounds >= RECOVERY_ROUNDS:
                self.phase = "idle"
                self.internal_frequency = ext
        return accepted

    # ---------------- 状态推进 ----------------
    def activate(self, external: float) -> None:
        """真的开始思考了（进入 LLM 调用）。decay 期内重复 activate 不重置。"""
        if self.phase != "decay":
            self.phase = "decay"
            self.internal_frequency = min(1.0, max(0.0, float(external or 0.0)))
            self.recovery_rounds = 0

    def record_run(self) -> bool:
        """完整跑完一轮后扣预算；返回是否刚好耗尽（进入 recovery）。"""
        if self.phase != "decay":
            return False
        self.internal_frequency = max(0.0, self.internal_frequency - DECAY_STEP)
        if self.internal_frequency > 0:
            return False
        self.phase = "recovery"
        self.recovery_rounds = 0
        return True

    # ---------------- 调试 ----------------
    def describe(self) -> str:
        return (f"{self.phase}｜internal={self.internal_frequency:.3f}"
                f"｜recovery={self.recovery_rounds}/{RECOVERY_ROUNDS}")


def mean_interval(times, *, limit: int = 12, min_gap: float = 0.5):
    """
    最近若干条消息的**平均到达间隔**（秒）。不够 2 个不同时刻就返回 None。

    用途：静默唤醒 —— 预测「下一条消息大概什么时候该来」，超过这个时间还没来，
    说明这段对话停下来了，可以给她一次额外开口机会（用户 2026-11-19）。
    """
    vals = []
    for t in list(times or [])[-limit:]:
        try:
            vals.append(float(t))
        except Exception:
            continue
    vals = sorted(set(vals))
    if len(vals) < 2:
        return None
    gaps = [b - a for a, b in zip(vals, vals[1:]) if b - a >= min_gap]
    if not gaps:
        return None
    return sum(gaps) / len(gaps)


def predict_interval(current, previous=None):
    """当前窗口和上一轮窗口都有间隔时取均值（更稳，不被一次刷屏带偏）。"""
    vals = [x for x in (current, previous) if x]
    if not vals:
        return None
    return sum(vals) / len(vals)

