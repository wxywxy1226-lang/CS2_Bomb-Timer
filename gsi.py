"""GSI 接收服务器与游戏快照解析。

通过 ThreadingHTTPServer 监听本地端口，接收 CS2 通过 GSI 配置推送的
HTTP POST JSON 数据，并对外提供结构化的 GameSnapshot。

CS2 的 GSI 是差量推送：payload 只包含相对上一帧有变化的字段，因此
快照采用"逐层累积合并"——字段缺席时保留上一次的值，而不是清空。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config

# 直接倒计时（bomb.countdown / phase_countdowns.phase_ends_in）仅在
# 部分时刻推送，且常为 -1；超过该秒数的旧值视为失效，避免跨越对局复用。
DIRECT_SECONDS_STALE = 2.0

# 用于区分"字段缺失"与"字段值为 null"（CS2 会显式推 round.bomb=null）
_BOMB_MISSING = object()


def _deep_merge(base: dict, patch: dict) -> dict:
    """把差量 patch 递归合并进 base（原位修改并返回 base）。"""
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


@dataclass
class GameSnapshot:
    raw: dict = field(default_factory=dict)
    updated_at: float = 0.0
    bomb_seen_at: float = 0.0  # 最近一次推送携带非空炸弹状态的时刻（time.time()）
    # 最近一次携带炸弹字段的来源（"round"/"bomb"/None），读取状态时跟随该来源，
    # 避免另一表示的残留 null 遮蔽新状态
    bomb_source: str | None = None
    # 直供倒计时的原始值与推送时刻（仅最近一次推送携带时刷新）
    countdown_raw: float | None = None
    countdown_at: float = 0.0
    phase_ends_in_raw: float | None = None
    phase_ends_in_at: float = 0.0

    def merge(self, payload: dict, now: float) -> bool:
        """合并一次差量推送；记录炸弹字段最近出现时间。

        返回 True 表示该推送显式把炸弹置空（round.bomb=null / bomb.state=null），
        这是 CS2 的回合终结信号（如对局提前结束、炸弹被拆除/爆炸）。
        实测 CS2 拆除/爆炸后并不推 null，而是直接让 bomb 字段消失，
        因此该信号仅作防御；主要终结路径依赖回合边界失效（见下方）。

        注意：置空判定与 bomb_seen_at 都**只基于本次 payload 实际携带的字段**。
        上一局结束时推过的值会残留在累积快照里，新一局安放若只推
        round.bomb（不带 bomb.state），读取累积值会误判为置空。
        """
        self.raw = _deep_merge(self.raw, payload)
        self.updated_at = now

        cleared = False
        round_patch = payload.get("round")
        if isinstance(round_patch, dict) and "bomb" in round_patch:
            self.bomb_source = "round"
            value = round_patch["bomb"]
            if value is None:
                cleared = True
            else:
                self.bomb_seen_at = now

        bomb_patch = payload.get("bomb")
        if isinstance(bomb_patch, dict) and "state" in bomb_patch:
            self.bomb_source = "bomb"
            value = bomb_patch["state"]
            if value is None:
                cleared = True
            else:
                self.bomb_seen_at = now

        # 回合边界失效：炸弹状态只属于当前回合。phase 离开 live（回合结束/
        # 新回合准备）且本次推送未携带炸弹字段时，炸弹状态作废——
        # CS2 拆除后 bomb 字段直接消失，若不失效，上回合残留（如 defused）
        # 会永远粘在面板上，新回合的对局状态无法显示。
        # 携带炸弹字段的终结帧（如 defused + phase=over）不受影响。
        if (
            isinstance(round_patch, dict)
            and "phase" in round_patch
            and round_patch["phase"] not in (None, "live")
            and "bomb" not in round_patch
            and not (isinstance(bomb_patch, dict) and "state" in bomb_patch)
        ):
            self.bomb_source = None

        # 直供倒计时：只有本次推送携带才刷新，避免旧值在累积快照中滞留
        if "countdown" in payload.get("bomb", {}):
            self.countdown_raw = payload["bomb"]["countdown"]
            self.countdown_at = now
        if "phase_ends_in" in payload.get("phase_countdowns", {}):
            self.phase_ends_in_raw = payload["phase_countdowns"]["phase_ends_in"]
            self.phase_ends_in_at = now
        return cleared

    @property
    def connected(self) -> bool:
        return time.time() - self.updated_at < config.HEARTBEAT_TIMEOUT

    def get(self, *path, default=None):
        current = self.raw
        for item in path:
            if not isinstance(current, dict) or item not in current:
                return default
            current = current[item]
        return current

    def bomb_state(self) -> str | None:
        """返回炸弹状态，跟随最近一次携带炸弹字段的来源。"""
        if self.bomb_source == "round":
            value = self.raw.get("round", {}).get("bomb", _BOMB_MISSING)
        elif self.bomb_source == "bomb":
            value = self.raw.get("bomb", {}).get("state", _BOMB_MISSING)
        else:
            return None
        return str(value) if value else None

    def round_phase(self) -> str | None:
        phase = self.get("round", "phase", default=None)
        return str(phase) if phase else None

    def direct_countdown(self) -> float | None:
        """GSI 直接提供的倒计时秒数（存在但常为 -1，仅作防御性读取）。

        只信任"最近一次推送携带"的值：旧值会在对局间滞留，
        因此先按推送时刻判断是否仍新鲜，再检查取值区间。
        """
        for pushed_at, value in (
            (self.countdown_at, self.countdown_raw),
            (self.phase_ends_in_at, self.phase_ends_in_raw),
        ):
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                continue
            if time.time() - pushed_at > DIRECT_SECONDS_STALE:
                continue
            if 0.0 <= seconds <= config.C4_TIMER:
                return seconds
        return None

    def in_active_round(self) -> bool:
        phase = self.round_phase()
        return phase is not None and phase not in {"freezetime", "over", "warmup", "gameover"}


class _GsiHandler(BaseHTTPRequestHandler):
    snapshot_queue: queue.Queue | None = None

    def do_POST(self):  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if _GsiHandler.snapshot_queue is not None:
                _GsiHandler.snapshot_queue.put(payload)
            self.send_response(200)
        except Exception:
            self.send_response(400)
        self.end_headers()

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"CS_C4 bomb timer is running.")

    def log_message(self, format, *args):  # noqa: A002
        return


def start_gsi_server(snapshot_queue: queue.Queue, status_queue: queue.Queue) -> None:
    _GsiHandler.snapshot_queue = snapshot_queue
    try:
        server = ThreadingHTTPServer((config.HOST, config.PORT), _GsiHandler)
    except OSError as exc:
        status_queue.put(f"端口 {config.PORT} 无法使用：{exc}")
        return
    status_queue.put(f"监听 {config.HOST}:{config.PORT}")
    server.serve_forever()


def spawn_gsi_server(snapshot_queue: queue.Queue, status_queue: queue.Queue) -> threading.Thread:
    thread = threading.Thread(
        target=start_gsi_server,
        args=(snapshot_queue, status_queue),
        daemon=True,
    )
    thread.start()
    return thread