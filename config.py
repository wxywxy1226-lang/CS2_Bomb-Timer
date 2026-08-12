"""全局配置：所有可调参数集中于此。"""

# --- GSI 服务 ---
HOST = "127.0.0.1"
PORT = 31983
GSI_CONFIG_FILE_NAME = "gamestate_integration_c4_timer.cfg"
HEARTBEAT_TIMEOUT = 8.0  # 秒；超过该时长未收到 GSI 数据视为未连接
DIAG_LOG_FILE = "gsi_dump.log"  # 收到的 GSI 关键字段日志（限频写入）

# --- 炸弹计时 ---
C4_TIMER = 40.0  # 从安放成功到爆炸的总秒数（CS2 标准值）
SHOW_DECIMAL = True  # 倒计时显示小数位（True 显示 27.4s，False 显示 27s）
# 已安放但炸弹字段连续超过该秒数未再推送 → 视为对局已提前结束、需复位隐藏。
# 正常对局中 CS2 约每 6 秒重推一次 planted，取 20 秒（3 倍余量）不会误伤。
BOMB_STALE_TIMEOUT = 20.0

# --- 拆弹判定阈值 ---
DEFUSE_TIME = 10.0  # 无拆弹钳徒手拆除需要 10 秒
KIT_TIME = 5.0  # 有拆弹钳需要 5 秒

# 三档状态颜色
COLOR_SAFE = "#2ecc71"  # 剩余 > 10s
COLOR_WARN = "#f1c40f"  # 剩余 5s ~ 10s（含）
COLOR_DANGER = "#e74c3c"  # 剩余 < 5s
COLOR_IDLE = "#8a97a5"  # 未安放/等待状态
COLOR_BG = "#14181f"  # 面板/卡片窗口背景
COLOR_CARD = "#1b2230"  # 悬浮卡片圆角填充（略亮于背景，形成层次）
COLOR_FG = "#f4f7fb"  # 主要文字

# --- 窗口位置 ---
PANEL_X = 40
PANEL_Y = 40
CARD_X = 60
CARD_Y = 160

# --- 字体（Windows） ---
FONT_TIMER = ("Consolas", 34, "bold")
FONT_STATUS = ("Segoe UI", 14, "bold")
FONT_LABEL = ("Microsoft YaHei UI", 9)