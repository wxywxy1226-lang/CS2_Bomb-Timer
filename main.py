"""CS_C4 入口：装配 GSI 服务、状态机与悬浮窗，驱动主循环。

支持 --demo 参数：不启动 CS2 也能模拟"安放 → 40 秒倒计时 → 拆除/爆炸"流程。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path

import config
from bomb_tracker import BombTracker, defuse_status
from gsi import GameSnapshot, spawn_gsi_server
from overlay import ControlPanel, OverlayCard, ensure_config_installed

TICK_MS = 50  # 20Hz
DIAG_LOG_INTERVAL = 5.0  # 日志写入间隔（秒），配合状态变化即写


def acquire_single_instance() -> socket.socket | None:
    """占用 端口+1 作为单实例锁；失败说明已有实例在运行。"""
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind((config.HOST, config.PORT + 1))
        lock.listen(1)
        return lock
    except OSError:
        return None


class DiagLogger:
    """把收到的 GSI 关键字段写入 JSONL 日志，用于定位数据链路断点。

    日志写在程序/ exe 所在目录（PyInstaller -F 下 __file__ 指向临时
    解压目录，不能用作日志位置）。
    """

    def __init__(self) -> None:
        base = Path(getattr(sys, "_MEIPASS", None)).parent if getattr(sys, "_MEIPASS", None) else Path(__file__).parent
        self.path = base / config.DIAG_LOG_FILE
        self.last_state: tuple[str | None, str | None] | None = None
        self.last_write = 0.0

    def log(self, payload: dict) -> None:
        now = time.time()
        snap = GameSnapshot(payload, now)
        state = (snap.bomb_state(), snap.round_phase())
        changed = state != self.last_state
        self.last_state = state
        if not changed and now - self.last_write < DIAG_LOG_INTERVAL:
            return
        self.last_write = now

        round_patch = payload.get("round") or {}
        bomb_patch = payload.get("bomb") or {}
        entry = {
            "t": datetime.now().isoformat(timespec="milliseconds"),
            "bomb_state": state[0],
            "round_phase": state[1],
            "bomb_countdown": snap.get("bomb", "countdown", default=None),
            "phase_ends_in": snap.get("phase_countdowns", "phase_ends_in", default=None),
            "connected": snap.connected,
            # 原始差量 payload 是否携带 bomb 键（区分"缺字段"与"值为 null"）
            "pb_round_bomb": round_patch.get("bomb", "<absent>"),
            "pb_bomb_state": bomb_patch.get("state", "<absent>"),
        }
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


class DemoDriver:
    """演示模式：注入模拟的炸弹状态序列。"""

    def __init__(self) -> None:
        self.phase: int = 0  # 0=未安放 1=倒计时 2=拆除 3=结束
        self.started_at: float = time.monotonic()
        self.next_state = "carried"

    def payload(self) -> dict:
        now = time.monotonic()
        elapsed = now - self.started_at
        state = self.next_state
        if self.phase == 0:
            if elapsed > 3.0:
                self.phase = 1
                self.started_at = now
                state = "planted"
        elif self.phase == 1:
            state = "planted"
            if elapsed > 40.0:
                self.phase = 3
                state = "exploded"
        elif self.phase == 2:
            state = "defusing"
            if elapsed > 6.0:
                self.phase = 3
                state = "defused"
        elif self.phase == 3:
            if elapsed > 4.0:
                self.phase = 0
                self.started_at = now
                self.next_state = "carried"
                state = "carried"

        return {"round": {"bomb": state, "phase": "live"}, "bomb": {"state": state}}


def _describe_frame(state: str | None, phase: str | None) -> str:
    """把 (炸弹状态, 回合阶段) 转成面板状态文字。"""
    if state:
        return f"炸弹: {state}"
    if phase in ("over", "gameover"):
        return "回合结束"
    if phase == "freezetime":
        return "冻结时间"
    if phase == "live":
        return "对局中，炸弹未安放"
    return "等待 CS2 推送炸弹数据"


class App:
    def __init__(self, demo: bool = False) -> None:
        self.demo = demo
        self.snapshot = GameSnapshot()
        self.snapshot_queue: queue.Queue = queue.Queue()
        self.status_queue: queue.Queue = queue.Queue()
        self.tracker = BombTracker()
        self.demo_driver = DemoDriver() if demo else None

        self.diag = DiagLogger()
        self.last_frame: tuple[str | None, str | None] | None = None

        self.panel = ControlPanel(on_close=self.close)
        self.card = OverlayCard(self.panel)
        self.panel.protocol("WM_DELETE_WINDOW", self.close)

        # 首次使用：必须先完成 GSI 配置安装才能使用（demo 模式跳过）
        self.aborted = False
        if not demo and not ensure_config_installed(self.panel):
            self.aborted = True
            self.panel.destroy()
            return

        if not demo:
            spawn_gsi_server(self.snapshot_queue, self.status_queue)

        self.panel.after(TICK_MS, self.tick)
        self.panel.mainloop()

    def close(self) -> None:
        self.panel.destroy()
        sys.exit(0)

    def _drain_queue(self) -> None:
        cleared = False
        while True:
            try:
                payload = self.snapshot_queue.get_nowait()
            except queue.Empty:
                break
            # 只保留最新一帧的置空信号：同一 tick 内旧帧的 null
            # 不应覆盖更新帧的 planted
            cleared = self.snapshot.merge(payload, time.time())
            if not self.demo:
                self.diag.log(payload)

        while True:
            try:
                status = self.status_queue.get_nowait()
            except queue.Empty:
                break
            self.panel.set_status(status)
        return cleared

    def tick(self) -> None:
        cleared = self._drain_queue()
        connected = self.snapshot.connected
        self.panel.set_dot_color(config.COLOR_SAFE if connected else config.COLOR_IDLE)

        if self.demo_driver is not None:
            payload = self.demo_driver.payload()
            cleared = self.snapshot.merge(payload, time.time())

        state = self.snapshot.bomb_state()
        phase = self.snapshot.round_phase()

        if not connected:
            # 超过心跳超时未收到任何推送 → CS2 未运行/配置未加载
            if self.last_frame is not None:
                self.last_frame = None
                self.panel.set_status("等待 CS2")
        elif (state, phase) != self.last_frame:
            self.last_frame = (state, phase)
            self.panel.set_status(_describe_frame(state, phase))

        remaining, decision = self.tracker.update(
            state,
            self.snapshot.direct_countdown(),
            phase,
            self.snapshot.bomb_seen_at,
            cleared,
        )

        if remaining is None or decision is None:
            self.card.hide()
            self.panel.set_countdown("", "")
        else:
            color, label = decision
            self.card.show(remaining, color, label)
            text = f"{remaining:.1f}s" if config.SHOW_DECIMAL else f"{int(remaining)}s"
            self.panel.set_countdown(text, color)

        self.panel.after(TICK_MS, self.tick)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CS2 炸弹倒计时悬浮窗")
    parser.add_argument("--demo", action="store_true", help="演示模式：模拟安放/拆除流程，无需 CS2")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    if args.demo:
        app = App(demo=True)
        return 0

    lock = acquire_single_instance()
    if lock is None:
        print("CS_C4 已经在运行。")
        return 1

    app = App()
    lock.close()
    # 未完成首次安装直接退出（用户点"退出"）→ 返回 2
    return 2 if app.aborted else 0


if __name__ == "__main__":
    sys.exit(main())