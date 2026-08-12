"""炸弹安放状态机与拆弹可行性判定。

核心逻辑：CS2 的 GSI 是差量推送——bomb 字段只在状态变化时携带，
且已安放期间会周期性重推（实测约每 6 秒一次），不提供精确倒计时。
因此：

1. 检测到 planted（安装成功）瞬间记录本地时间戳，按 C4_TIMER 推算剩余秒数；
2. 安放完成后即使某次推送缺失 bomb 字段也持续倒计时（粘性计时），
   直到 defused/exploded、回合阶段结束，或炸弹字段长期消失（对局已提前
   结束但 CS2 未下发终结信号）才复位隐藏；
3. 每次收到 planted 都无条件以当前时刻重新起算，覆盖"对局提前结束
   导致上一局计时残留、污染下一局"的场景；
4. planting（安装中）不启动计时，避免安装被中断导致误报。
"""

from __future__ import annotations

import time

import config


def defuse_status(remaining: float) -> tuple[str, str]:
    """根据剩余秒数返回 (卡片颜色, 状态文字)。边界：>10 绿 / 10~5 黄 / <5 红。"""
    if remaining > config.DEFUSE_TIME:
        return config.COLOR_SAFE, "Defuse Available"
    if remaining >= config.KIT_TIME:
        return config.COLOR_WARN, "Defuse Available by Kit"
    return config.COLOR_DANGER, "Defuse Unavailable"


ARMED_STATES = {"planted", "defusing"}  # 炸弹已安放完成
CLEAR_STATES = {"defused", "exploded"}  # 回合终结信号
ROUND_OVER_PHASES = {"over", "gameover"}
ZERO_HIDE_AFTER = 5.0  # 剩余归零后若迟迟收不到爆炸/拆除信号，5 秒后自动隐藏


class BombTracker:
    def __init__(self) -> None:
        self.planted_at: float | None = None  # time.monotonic()
        self.armed = False
        self.zero_since: float | None = None

    def reset(self) -> None:
        self.planted_at = None
        self.armed = False
        self.zero_since = None

    def _remaining(self) -> float:
        return max(0.0, config.C4_TIMER - (time.monotonic() - self.planted_at))

    def update(
        self,
        state: str | None,
        direct_seconds: float | None,
        round_phase: str | None = None,
        bomb_seen_at: float | None = None,
        bomb_cleared: bool = False,
    ) -> tuple[float | None, tuple[str, str] | None]:
        """接收最新快照，返回 (剩余秒数或 None, 判定或 None)。

        剩余秒数为 None 表示卡片应隐藏；否则总是返回有效判定。
        bomb_seen_at 为最近一次推送携带非空炸弹状态的时刻（time.time()），
        用于识别"对局提前结束但未收到终结信号"。
        bomb_cleared 为 True 表示最近一次推送显式把炸弹置空
        （round.bomb=null），是 CS2 的终结信号，立即复位。
        """
        now = time.monotonic()

        # 1. 明确的回合终结信号：defused/exploded、炸弹被显式置空
        #    （round.bomb=null）、回合阶段已结束 → 复位
        if (
            state in CLEAR_STATES
            or bomb_cleared
            or (self.armed and round_phase in ROUND_OVER_PHASES)
        ):
            self.reset()
            return None, None

        # 2. 已安放但炸弹字段长期未出现 → 对局已被 CS2 提前结束
        #    （正常对局中 planted 约每 6 秒重推一次，超时阈值取 20 秒，
        #    不会误伤；同时避免残留状态污染下一局）
        if (
            self.armed
            and bomb_seen_at is not None
            and time.time() - bomb_seen_at > config.BOMB_STALE_TIMEOUT
        ):
            self.reset()
            return None, None

        if state is None:
            # 本次推送未携带炸弹字段：未安放则隐藏；已安放则粘性计时继续
            if not self.armed:
                return None, None
        elif state == "planting":
            # 安装中：不启动计时；顺带清除上一局可能残留的计时
            self.reset()
            return None, None
        elif state in ARMED_STATES:
            # 首次安放时记录时间戳；已安放期间 CS2 约每 6 秒重推一次
            # planted，此时不能重置，否则倒计时永远停在满值
            if not self.armed:
                self.armed = True
                self.planted_at = now
                self.zero_since = None
        else:
            # carried/dropped：炸弹被捡起/掉落，复位
            self.reset()
            return None, None

        if not self.armed:
            return None, None

        remaining = self._remaining()
        if direct_seconds is not None and 0.0 <= direct_seconds <= config.C4_TIMER:
            # GSI 直供倒计时（通常为 -1，个别时刻有效）可信度更高时采纳
            remaining = min(remaining, direct_seconds)

        if remaining <= 0.0:
            if self.zero_since is None:
                self.zero_since = now
            elif now - self.zero_since > ZERO_HIDE_AFTER:
                self.reset()
                return None, None
            remaining = 0.0
        else:
            self.zero_since = None

        return remaining, defuse_status(remaining)