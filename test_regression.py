"""回归测试：跨对局残留状态、差量推送、提前结束复位。

覆盖用户实测数据对应的场景：
- 对局在 C4 爆炸前提前结束且 GSI 未下发终结信号（如 13:30:23.790 planted
  → 13:30:23.797 推送 round.bomb=null）→ 显式置空立即复位；
- 炸弹字段长期消失（对局结束但连 null 都不推）→ 超时兜底复位；
- 残留计时不得污染下一局：新 planted 必须重新从 40s 起算；
- planted 期间周期性重推（约每 6 秒）不得重置倒计时；
- 快照累积合并：phase 缺省时保留上一帧值。
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

import config
from bomb_tracker import BombTracker, defuse_status
from gsi import GameSnapshot


class DefuseStatusBoundary(unittest.TestCase):
    def test_green_above_10(self):
        self.assertEqual(defuse_status(10.5), (config.COLOR_SAFE, "Defuse Available"))

    def test_yellow_at_10(self):
        self.assertEqual(defuse_status(10.0), (config.COLOR_WARN, "Defuse Available by Kit"))

    def test_yellow_between(self):
        self.assertEqual(defuse_status(7.3), (config.COLOR_WARN, "Defuse Available by Kit"))

    def test_yellow_at_5(self):
        self.assertEqual(defuse_status(5.0), (config.COLOR_WARN, "Defuse Available by Kit"))

    def test_red_below_5(self):
        self.assertEqual(defuse_status(4.9), (config.COLOR_DANGER, "Defuse Unavailable"))


class SnapshotMerge(unittest.TestCase):
    def test_delta_merge_keeps_absent_fields(self):
        snap = GameSnapshot()
        snap.merge({"round": {"bomb": "planted", "phase": "live"}}, 100.0)
        cleared = snap.merge({"round": {"bomb": None}}, 101.0)  # 显式置空
        self.assertTrue(cleared)
        self.assertEqual(snap.round_phase(), "live")  # phase 应保留
        self.assertIsNone(snap.bomb_state())
        self.assertEqual(snap.bomb_seen_at, 100.0)  # null 不算"炸弹在场"

    def test_plain_delta_without_bomb_keeps_state_in_live(self):
        """差量帧不带 bomb（live 回合内）：状态跟随最近来源的累积值。"""
        snap = GameSnapshot()
        snap.merge({"round": {"bomb": "planted", "phase": "live"}}, 100.0)
        snap.merge({"round": {"phase": "live"}}, 101.0)  # 只推 phase
        self.assertEqual(snap.bomb_state(), "planted")
        self.assertEqual(snap.round_phase(), "live")

    def test_round_boundary_invalidates_bomb_state(self):
        """回合边界（over/freezetime）且无 bomb 推送 → 炸弹状态作废。"""
        snap = GameSnapshot()
        snap.merge({"round": {"bomb": "planted", "phase": "live"}}, 100.0)
        snap.merge({"round": {"phase": "over"}}, 101.0)  # 回合结束，无 bomb
        self.assertIsNone(snap.bomb_state())
        self.assertEqual(snap.round_phase(), "over")

    def test_nested_merge(self):
        snap = GameSnapshot()
        snap.merge({"round": {"bomb": "carried"}}, 1.0)
        snap.merge({"round": {"phase": "live"}}, 2.0)
        self.assertEqual(snap.get("round", "bomb"), "carried")
        self.assertEqual(snap.round_phase(), "live")

    def test_planted_after_prev_null_cleared_not_triggered(self):
        """核心回归：上一局 bomb.state=null 残留，新局只推 round.bomb=planted。

        旧实现在 merge 里读累积快照的 bomb.state（残留 null）→ 误报置空，
        导致状态机立即复位、卡片不显示。现在 cleared 只取决于本次 payload。
        """
        snap = GameSnapshot()
        snap.merge({"bomb": {"state": None}}, 100.0)  # 上一局结束
        cleared = snap.merge({"round": {"bomb": "planted"}}, 101.0)  # 新局安放
        self.assertFalse(cleared)
        self.assertEqual(snap.bomb_state(), "planted")
        self.assertEqual(snap.bomb_seen_at, 101.0)

    def test_planted_after_prev_round_null_follows_latest_source(self):
        """反向串扰：上一局 round.bomb=null 残留，新局只推 bomb.state=planted。"""
        snap = GameSnapshot()
        snap.merge({"round": {"bomb": None}}, 100.0)
        cleared = snap.merge({"bomb": {"state": "planted"}}, 101.0)
        self.assertFalse(cleared)
        self.assertEqual(snap.bomb_state(), "planted")
        self.assertEqual(snap.bomb_seen_at, 101.0)

    def test_delta_without_bomb_keeps_bomb_state(self):
        """差量帧不带 bomb：状态跟随最近来源的累积值。"""
        snap = GameSnapshot()
        snap.merge({"round": {"bomb": "planted"}}, 100.0)
        snap.merge({"round": {"phase": "live"}}, 101.0)  # 只推 phase
        self.assertEqual(snap.bomb_state(), "planted")

    def test_defused_round_boundary_clears_sticky_state(self):
        """核心回归（用户实测 17:11 序列）：拆除后 bomb 字段消失，
        炸弹状态必须随回合边界失效，否则面板永远显示 defused。"""
        snap = GameSnapshot()
        # 17:11:01 安放
        snap.merge({"round": {"bomb": "planted", "phase": "live"}}, 100.0)
        self.assertEqual(snap.bomb_state(), "planted")
        # 周期重推 planted
        snap.merge({"round": {"bomb": "planted", "phase": "live"}}, 106.0)
        self.assertEqual(snap.bomb_state(), "planted")
        # 拆除帧：bomb=defused + phase=over，状态仍可读（供状态机复位）
        snap.merge({"round": {"bomb": "defused", "phase": "over"}}, 107.0)
        self.assertEqual(snap.bomb_state(), "defused")
        # 拆除后 bomb 字段消失，phase 仍在 over → 炸弹状态作废
        snap.merge({"round": {"phase": "over"}}, 113.0)
        self.assertIsNone(snap.bomb_state())
        # 新回合
        snap.merge({"round": {"phase": "freezetime"}}, 120.0)
        self.assertIsNone(snap.bomb_state())
        snap.merge({"round": {"phase": "live"}}, 130.0)
        self.assertIsNone(snap.bomb_state())
        # 下一局重新安放
        snap.merge({"round": {"bomb": "planted", "phase": "live"}}, 140.0)
        self.assertEqual(snap.bomb_state(), "planted")

    def test_sticky_planted_survives_absent_frames_in_live_round(self):
        """粘性倒计时：live 回合内 bomb 字段消失（CS2 差量）不得失效。"""
        snap = GameSnapshot()
        snap.merge({"round": {"bomb": "planted", "phase": "live"}}, 100.0)
        snap.merge({"round": {"phase": "live"}}, 106.0)  # 无 bomb 的差量帧
        self.assertEqual(snap.bomb_state(), "planted")

    def test_direct_countdown_stale_ignored(self):
        snap = GameSnapshot()
        with mock.patch("gsi.time.time", return_value=100.0):
            snap.merge({"bomb": {"countdown": 30.0}}, 95.0)  # 5 秒前推送的旧值
            self.assertIsNone(snap.direct_countdown())
            snap.merge({"bomb": {"countdown": 12.0}}, 99.5)  # 新鲜值
            self.assertEqual(snap.direct_countdown(), 12.0)
            snap.merge({"round": {"phase": "over"}}, 100.0)  # 差量不带 countdown
            self.assertEqual(snap.direct_countdown(), 12.0)  # 仍在 2s 窗口内可用
            with mock.patch("gsi.time.time", return_value=102.0):
                self.assertIsNone(snap.direct_countdown())  # 超窗失效


class TrackerCrossRound(unittest.TestCase):
    def setUp(self):
        self.tracker = BombTracker()
        self.now = 10_000.0
        patcher = mock.patch("bomb_tracker.time.monotonic", side_effect=lambda: self.now)
        self.mono = patcher.start()
        self.addCleanup(patcher.stop)

    def _feed(self, state, phase="live", bomb_seen_ago=0.0, bomb_cleared=False):
        """喂一帧。bomb_seen_at 用真实 time.time() 模拟，避免与 mock 的 monotonic 冲突。"""
        return self.tracker.update(
            state,
            None,
            phase,
            bomb_seen_at=time.time() - bomb_seen_ago,
            bomb_cleared=bomb_cleared,
        )

    def test_plant_then_periodic_repush_keeps_countdown(self):
        """周期重推 planted（约每 6 秒）不得重置计时。"""
        self._feed("planted", bomb_seen_ago=0.0)
        self.assertEqual(self._feed(None, "live", bomb_seen_ago=0.0)[0] is not None, True)
        self.now += 6.0
        remaining, decision = self._feed("planted", "live", bomb_seen_ago=0.0)
        self.assertAlmostEqual(remaining, config.C4_TIMER - 6.0, delta=0.1)
        self.assertEqual(decision[0], config.COLOR_SAFE)

    def test_sticky_countdown_when_bomb_field_absent(self):
        self._feed("planted", bomb_seen_ago=0.0)
        self.now += 15.0
        remaining, _ = self._feed(None, "live", bomb_seen_ago=0.0)  # 差量推送缺 bomb
        self.assertAlmostEqual(remaining, config.C4_TIMER - 15.0, delta=0.1)

    def test_round_ends_early_clears_immediately_on_null(self):
        """用户实测：planted 后 7ms 推 round.bomb=null → 立即复位。"""
        self._feed("planted", bomb_seen_ago=0.0)
        self.now += 5.0
        self.assertIsNone(self._feed(None, "live", bomb_seen_ago=0.0, bomb_cleared=True)[0])
        self.assertFalse(self.tracker.armed)

    def test_round_ends_early_stale_timeout_resets(self):
        """炸弹字段长期消失（连 null 都不推）→ 超时兜底复位。"""
        self._feed("planted", bomb_seen_ago=0.0)
        self.now += 10.0
        remaining, _ = self._feed(None, "live", bomb_seen_ago=5.0)  # 5s 前还在推 → 粘性计时
        self.assertAlmostEqual(remaining, config.C4_TIMER - 10.0, delta=0.1)
        self.now += 16.0  # 距上次推送共 21s > 20s
        self.assertIsNone(self._feed(None, "live", bomb_seen_ago=21.0)[0])
        self.assertFalse(self.tracker.armed)

    def test_next_round_replants_restarts_full_timer(self):
        """回归用户场景：上一局提前结束 → 下一局 planted 必须重新 40s 起算。"""
        self._feed("planted", bomb_seen_ago=0.0)
        self.now += 8.0
        # 上一局提前结束：显式置空
        self.assertIsNone(self._feed(None, "live", bomb_seen_ago=0.0, bomb_cleared=True)[0])
        self.assertFalse(self.tracker.armed)
        # 新一局又安放 → 重新 40s
        self._feed("planted", bomb_seen_ago=0.0)
        self.now += 3.0
        remaining, _ = self._feed("planted", "live", bomb_seen_ago=0.0)
        self.assertAlmostEqual(remaining, config.C4_TIMER - 3.0, delta=0.1)

    def test_defused_clears(self):
        self._feed("planted", bomb_seen_ago=0.0)
        self.now += 5.0
        self.assertIsNone(self._feed("defused", "over", bomb_seen_ago=0.0)[0])
        self.assertFalse(self.tracker.armed)

    def test_over_phase_clears_while_armed(self):
        self._feed("planted", bomb_seen_ago=0.0)
        self.now += 5.0
        self.assertIsNone(self._feed(None, "over", bomb_seen_ago=0.0)[0])
        self.assertFalse(self.tracker.armed)

    def test_planting_does_not_start_timer(self):
        self.assertIsNone(self._feed("planting", "live", bomb_seen_ago=0.0)[0])
        self.assertFalse(self.tracker.armed)


if __name__ == "__main__":
    unittest.main()