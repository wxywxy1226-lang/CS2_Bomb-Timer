"""控制面板与置顶悬浮卡片。

控制面板：状态显示、退出；标题栏默认隐藏、悬停显示，可拖动。
悬浮卡片：overrideredirect + -topmost 置顶，内容用 GDI 直接渲染到
32 位 ARGB 位图并经 UpdateLayeredWindow 合成上屏——只有圆角卡片与
文字不透明，其余区域 per-pixel 全透明，不再出现整窗深色/黑色块。
（不用 -transparentcolor：色键透明依赖 DWM 合成，在部分机器上失效
会退化成不可移动的黑块；per-pixel alpha 由系统统一合成，各版本
Windows 均可靠。）仅炸弹安放时显示，位置固定（改 config.py 的
CARD_X/CARD_Y）；鼠标点击穿透不挡游戏操作；周期 SetWindowPos 刷新
置顶层级，窗口化全屏（无边框）游戏下保持覆盖。
首次启动：必须完成 GSI 配置安装（自动定位 CS2 cfg 目录或手动选择）
才能进入主程序，安装标记持久化到用户目录。
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

try:  # ctypes.wintypes 仅在 Windows 上存在
    import ctypes.wintypes as wintypes
except ImportError:  # pragma: no cover
    wintypes = None

import config

# Win32 扩展样式
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOOLWINDOW = 0x80
GWL_EXSTYLE = -20

# Layered window / GDI 渲染常量
LWA_ALPHA = 0x2
ULW_ALPHA = 0x2
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0
BI_RGB = 0
TRANSPARENT_BKGND = 1  # SetBkMode
ANTIALIASED_QUALITY = 4
DEFAULT_CHARSET = 1
FW_BOLD = 700
FW_NORMAL = 400
DT_CENTER = 0x1
DT_VCENTER = 0x4
DT_SINGLELINE = 0x20
DT_NOCLIP = 0x100
LOGPIXELSY = 90
SWP_NOSIZE = 0x1
SWP_NOMOVE = 0x2
SWP_NOACTIVATE = 0x10


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER)]


def _setup_win32() -> None:
    """声明 Win32 函数原型：不声明时 ctypes 会把 64 位指针参数按 32 位截断。"""
    if os.name != "nt":
        return
    u32 = ctypes.windll.user32
    g32 = ctypes.windll.gdi32

    u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    u32.SetWindowPos.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    u32.SetWindowPos.restype = wintypes.BOOL
    u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
    u32.GetWindowRect.restype = wintypes.BOOL
    u32.UpdateLayeredWindow.argtypes = [
        wintypes.HWND, wintypes.HDC, ctypes.POINTER(_POINT), ctypes.POINTER(_SIZE),
        wintypes.HDC, ctypes.POINTER(_POINT), wintypes.COLORREF,
        ctypes.POINTER(_BLENDFUNCTION), ctypes.c_uint,
    ]
    u32.UpdateLayeredWindow.restype = wintypes.BOOL
    u32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
                                               ctypes.c_ubyte, ctypes.c_uint]
    u32.SetLayeredWindowAttributes.restype = wintypes.BOOL

    g32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g32.CreateCompatibleDC.restype = wintypes.HDC
    g32.DeleteDC.argtypes = [wintypes.HDC]
    g32.DeleteDC.restype = wintypes.BOOL
    g32.CreateDIBSection.argtypes = [
        wintypes.HDC, ctypes.POINTER(_BITMAPINFO), ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_uint,
    ]
    g32.CreateDIBSection.restype = wintypes.HBITMAP
    g32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g32.SelectObject.restype = wintypes.HGDIOBJ
    g32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g32.DeleteObject.restype = wintypes.BOOL
    g32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
    g32.GetDeviceCaps.restype = ctypes.c_int
    g32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
    g32.CreateRoundRectRgn.restype = wintypes.HRGN
    g32.SelectClipRgn.argtypes = [wintypes.HDC, wintypes.HRGN]
    g32.SelectClipRgn.restype = ctypes.c_int
    g32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    g32.CreateSolidBrush.restype = wintypes.HBRUSH
    u32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(_RECT), wintypes.HBRUSH]
    u32.FillRect.restype = ctypes.c_int
    g32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    g32.SetBkMode.restype = ctypes.c_int
    g32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    g32.SetTextColor.restype = wintypes.COLORREF
    g32.CreateFontW.argtypes = [ctypes.c_int] * 7 + [ctypes.c_uint] * 6 + [wintypes.LPCWSTR]
    g32.CreateFontW.restype = wintypes.HFONT
    u32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                              ctypes.POINTER(_RECT), ctypes.c_uint]
    u32.DrawTextW.restype = ctypes.c_int
    g32.GdiFlush.restype = wintypes.BOOL


_setup_win32()


def _colorref(hex_color: str) -> int:
    """'#rrggbb' → GDI COLORREF（0x00bbggrr）。"""
    rgb = hex_color.lstrip("#")
    r = int(rgb[0:2], 16)
    g = int(rgb[2:4], 16)
    b = int(rgb[4:6], 16)
    return (b << 16) | (g << 8) | r


def set_click_through(hwnd: int, enabled: bool) -> None:
    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    ex_style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if enabled:
        ex_style |= WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW
    else:
        ex_style &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex_style)
    # 立即应用样式变更，否则可能延迟到下次窗口操作才生效
    SWP_NOSIZE = 0x1
    SWP_NOMOVE = 0x2
    SWP_NOZORDER = 0x4
    SWP_FRAMECHANGED = 0x20
    user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_FRAMECHANGED)


class _GdiCard:
    """GDI 渲染 32 位 ARGB 位图并经 UpdateLayeredWindow 合成上屏的悬浮卡片。

    GDI 绘制只写 RGB、不写 alpha 通道：全部绘制完成后扫描一遍位图，
    把 RGB 非零的像素 alpha 置 255（圆角卡片与文字不透明），其余保持
    0（全透明）。per-pixel 合成由系统统一完成，不依赖 DWM 色键，
    任何 Windows 版本都不会退化成整窗黑块。

    布局以逻辑尺寸（320×110）为基准，按窗口实际物理尺寸等比缩放，
    高分屏/系统缩放下依然清晰。
    """

    def __init__(self, hwnd: int, logical_w: int, logical_h: int) -> None:
        self.hwnd = hwnd
        u32 = ctypes.windll.user32
        g32 = ctypes.windll.gdi32

        rect = _RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        self.w = rect.right - rect.left
        self.h = rect.bottom - rect.top
        if self.w <= 0 or self.h <= 0:  # 兜底：窗口尚未映射时按逻辑尺寸
            self.w, self.h = logical_w, logical_h
        self._scale = min(self.w / logical_w, self.h / logical_h)

        self.memdc = g32.CreateCompatibleDC(None)
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.w
        bmi.bmiHeader.biHeight = -self.h  # top-down：行序与屏幕一致
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        self.bits = ctypes.c_void_p()
        self.hbm = g32.CreateDIBSection(self.memdc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                        ctypes.byref(self.bits), None, 0)
        g32.SelectObject(self.memdc, self.hbm)
        self._pixels = (ctypes.c_ubyte * (self.w * self.h * 4)).from_address(self.bits.value)

        dpi = g32.GetDeviceCaps(self.memdc, LOGPIXELSY) or 96
        self._pt2px = dpi / 72.0

        s = self._scale
        self._pad = round(8 * s)
        self._radius = max(2, round(14 * s))
        self._stroke = max(1, round(2 * s))
        # 文字垂直中心对齐原布局：时间 46、状态 90（逻辑坐标）
        self._timer_rect = _RECT(0, round(8 * s), self.w, round(84 * s))
        self._status_rect = _RECT(0, round(76 * s), self.w, round(104 * s))

    def _font(self, face: str, point: int, bold: bool) -> int:
        g32 = ctypes.windll.gdi32
        height = -max(8, round(point * self._pt2px * self._scale))
        return g32.CreateFontW(
            height, 0, 0, 0, FW_BOLD if bold else FW_NORMAL, 0, 0, 0,
            DEFAULT_CHARSET, 0, 0, ANTIALIASED_QUALITY, 0, face,
        )

    def draw(self, timer_text: str, status_text: str, color: str) -> None:
        u32 = ctypes.windll.user32
        g32 = ctypes.windll.gdi32
        w, h = self.w, self.h

        # 1) 全透明底
        ctypes.memset(self.bits.value, 0, w * h * 4)

        # 2) 圆角描边：外圈填状态色、内圈填卡片色
        p, r, st = self._pad, self._radius, self._stroke
        full = _RECT(0, 0, w, h)
        rgn_outer = g32.CreateRoundRectRgn(p, p, w - p, h - p, r, r)
        rgn_inner = g32.CreateRoundRectRgn(p + st, p + st, w - p - st, h - p - st,
                                           max(2, r - st), max(2, r - st))
        brush = g32.CreateSolidBrush(_colorref(color))
        g32.SelectClipRgn(self.memdc, rgn_outer)
        u32.FillRect(self.memdc, ctypes.byref(full), brush)
        g32.DeleteObject(brush)
        brush = g32.CreateSolidBrush(_colorref(config.COLOR_CARD))
        g32.SelectClipRgn(self.memdc, rgn_inner)
        u32.FillRect(self.memdc, ctypes.byref(full), brush)
        g32.DeleteObject(brush)
        g32.SelectClipRgn(self.memdc, None)
        g32.DeleteObject(rgn_outer)
        g32.DeleteObject(rgn_inner)

        # 3) 时间 + 状态文字
        g32.SetBkMode(self.memdc, TRANSPARENT_BKGND)
        g32.SetTextColor(self.memdc, _colorref(color))
        face, point, style = config.FONT_TIMER
        font = self._font(face, point, style.lower() == "bold")
        g32.SelectObject(self.memdc, font)
        u32.DrawTextW(self.memdc, timer_text, -1, ctypes.byref(self._timer_rect),
                      DT_CENTER | DT_SINGLELINE | DT_VCENTER | DT_NOCLIP)
        face, point, style = config.FONT_STATUS
        font2 = self._font(face, point, style.lower() == "bold")
        g32.SelectObject(self.memdc, font2)
        u32.DrawTextW(self.memdc, status_text, -1, ctypes.byref(self._status_rect),
                      DT_CENTER | DT_SINGLELINE | DT_VCENTER | DT_NOCLIP)
        g32.DeleteObject(font)
        g32.DeleteObject(font2)

        # 4) 已绘制的像素置为不透明（GDI 不写 alpha 通道）
        g32.GdiFlush()
        px = self._pixels
        for i in range(0, len(px), 4):
            if px[i] or px[i + 1] or px[i + 2]:
                px[i + 3] = 255

        # 5) per-pixel alpha 合成上屏
        win = _RECT()
        u32.GetWindowRect(self.hwnd, ctypes.byref(win))
        pt_dst = _POINT(win.left, win.top)
        size = _SIZE(w, h)
        blend = _BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        u32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(pt_dst), ctypes.byref(size),
                                self.memdc, ctypes.byref(_POINT(0, 0)), 0,
                                ctypes.byref(blend), ULW_ALPHA)

    def hide(self) -> None:
        """切回统一 alpha 模式并置 0：窗口整体全透明（与 ULW 互斥切换）。"""
        ctypes.windll.user32.SetLayeredWindowAttributes(self.hwnd, 0, 0, LWA_ALPHA)

    def close(self) -> None:
        g32 = ctypes.windll.gdi32
        g32.DeleteObject(self.hbm)
        g32.DeleteDC(self.memdc)


class OverlayCard(tk.Toplevel):
    """炸弹倒计时悬浮卡片：per-pixel alpha 透明圆角卡片，置顶显示。

    内容由 _GdiCard 渲染，圆角与文字之外全透明，不再出现整窗黑块；
    显隐通过 layered 渲染模式切换实现，窗口始终映射（避免
    overrideredirect 窗口 withdraw/deiconify 后不恢复显示的 Tk 问题）。
    位置由 config.CARD_X/CARD_Y 决定；鼠标点击穿透不挡游戏操作；
    周期 SetWindowPos(HWND_TOPMOST) 保持置顶，覆盖窗口化全屏游戏。
    """

    CARD_W, CARD_H = 320, 110

    def __init__(self, root: tk.Misc) -> None:
        super().__init__(root)

        self.overrideredirect(True)
        self.configure(bg=config.COLOR_BG)
        self.geometry(f"{self.CARD_W}x{self.CARD_H}+{config.CARD_X}+{config.CARD_Y}")

        self.update_idletasks()
        self.attributes("-topmost", True)

        self._hidden = True
        self._set_click_through(True)
        self._gdi = _GdiCard(self.winfo_id(), self.CARD_W, self.CARD_H)
        self._gdi.hide()  # 初始完全透明
        self._draw_key: tuple | None = None
        self._keep_topmost()

    def _set_click_through(self, enabled: bool) -> None:
        if os.name == "nt":
            set_click_through(self.winfo_id(), enabled)

    def _keep_topmost(self) -> None:
        """周期刷新置顶层级；窗口化全屏（无边框）游戏下保持覆盖。"""
        if os.name == "nt":
            ctypes.windll.user32.SetWindowPos(
                self.winfo_id(), ctypes.c_void_p(-1), 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        self.after(1000, self._keep_topmost)

    def show(self, remaining: float, status_color: str, status_text: str) -> None:
        text = f"{remaining:.1f}s" if config.SHOW_DECIMAL else f"{int(remaining)}s"
        key = (text, status_color, status_text)
        if self._draw_key == key and not self._hidden:
            return  # 内容未变化，不重复渲染
        self._draw_key = key
        self._gdi.draw(text, status_text, status_color)
        if self._hidden:
            self._hidden = False
            self.lift()

    def hide(self) -> None:
        if self._hidden:
            return
        self._hidden = True
        self._draw_key = None
        self._gdi.hide()


class ControlPanel(tk.Tk):
    """常驻控制面板：状态显示、退出。

    标题栏默认隐藏（窗口只显示主体一行），鼠标悬停到窗口时显示标题栏，
    可拖动面板。
    """

    def __init__(self, on_close) -> None:
        super().__init__()
        self._on_close = on_close
        self.status_text = tk.StringVar(value="启动中")
        self.countdown_text = tk.StringVar(value="")
        self.dot = tk.Canvas(self, width=14, height=14, highlightthickness=0, bg="#10141a")
        self.drag_start = (0, 0)
        self._title_visible = False

        self.configure_window()
        self.build_ui()
        self._keep_topmost()

    def _keep_topmost(self) -> None:
        """周期刷新置顶层级，保证游戏全屏（窗口化）时面板也在最上层。"""
        if os.name == "nt":
            ctypes.windll.user32.SetWindowPos(
                self.winfo_id(), ctypes.c_void_p(-1), 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
        self.after(1500, self._keep_topmost)

    def configure_window(self) -> None:
        self.geometry(f"300x46+{config.PANEL_X}+{config.PANEL_Y}")
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg="#10141a")

    def build_ui(self) -> None:
        self.title_bar = tk.Frame(self, bg="#1a212b", height=30)
        # 默认隐藏，鼠标悬停到窗口时显示
        self.title_bar.pack_forget()
        self.title_bar.bind("<ButtonPress-1>", self.start_drag)
        self.title_bar.bind("<B1-Motion>", self.on_drag)

        tk.Label(
            self.title_bar,
            text="CS C4 Bomb Timer",
            bg="#1a212b",
            fg="#f4f7fb",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left", padx=(12, 6), pady=5)

        tk.Button(
            self.title_bar,
            text="×",
            command=self._on_close,
            bg="#1a212b",
            fg="#f4f7fb",
            bd=0,
            activebackground="#333c48",
            activeforeground="#ffffff",
            font=("Segoe UI", 13),
            width=3,
        ).pack(side="right", padx=(0, 4))

        self.body = tk.Frame(self, bg="#10141a")
        self.body.pack(fill="both", expand=True, padx=10, pady=(8, 8))

        self.dot.pack(side="left", pady=6, padx=(0, 6))
        self._dot_color = config.COLOR_IDLE
        self._draw_dot()

        tk.Label(
            self.body,
            textvariable=self.status_text,
            bg="#10141a",
            fg="#8ea4b8",
            font=("Microsoft YaHei UI", 9),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)

        self.countdown_label = tk.Label(
            self.body,
            textvariable=self.countdown_text,
            bg="#10141a",
            fg="#f4f7fb",
            font=("Consolas", 12, "bold"),
        )
        self.countdown_label.pack(side="left", padx=(0, 10))

        self._bind_hover_all()

    def _draw_dot(self) -> None:
        self.dot.delete("all")
        self.dot.create_oval(3, 3, 11, 11, fill=self._dot_color, outline="")

    def set_dot_color(self, color: str) -> None:
        self._dot_color = color
        self._draw_dot()

    def set_status(self, text: str) -> None:
        self.status_text.set(text)

    def set_countdown(self, text: str, color: str) -> None:
        """面板上的倒计时数字（悬浮卡被遮挡时的兜底显示）；空文本表示隐藏。"""
        self.countdown_text.set(text)
        if text:
            self.countdown_label.configure(fg=color)

    # --- 标题栏悬停显隐 ---

    def _bind_hover_all(self) -> None:
        for w in (self, *self._walk(self)):
            w.bind("<Enter>", self._on_hover, add="+")
            w.bind("<Leave>", self._on_hover, add="+")

    @staticmethod
    def _walk(root: tk.Misc):
        for child in root.winfo_children():
            yield child
            yield from ControlPanel._walk(child)

    def _on_hover(self, event) -> None:
        w = self.winfo_containing(event.x_root, event.y_root)
        inside = w is not None and w.winfo_toplevel() is self
        self._set_title_visible(inside)

    def _set_title_visible(self, visible: bool) -> None:
        if visible == self._title_visible:
            return
        self._title_visible = visible
        x, y = self.winfo_x(), self.winfo_y()
        if visible:
            self.title_bar.pack(fill="x", before=self.body)
            self.geometry(f"300x76+{x}+{y}")
        else:
            self.title_bar.pack_forget()
            self.geometry(f"300x46+{x}+{y}")

    def start_drag(self, event) -> None:
        self.drag_start = (event.x, event.y)

    def on_drag(self, event) -> None:
        x = self.winfo_x() + event.x - self.drag_start[0]
        y = self.winfo_y() + event.y - self.drag_start[1]
        self.geometry(f"+{x}+{y}")


# --- 安装 GSI 配置（首次启动强制流程） ---

# 安装标记：记录最后一次成功安装的 cfg 目录，位于用户目录 %APPDATA%/CS_C4/
INSTALL_MARKER_NAME = "cs_c4_installed.txt"


def _install_marker_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "CS_C4" / INSTALL_MARKER_NAME


def _record_install(cfg_dir: Path) -> None:
    marker = _install_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(cfg_dir), encoding="utf-8")


def is_config_installed() -> bool:
    """标记存在且记录的 cfg 文件仍在磁盘上 → 视为已安装。"""
    marker = _install_marker_path()
    try:
        if not marker.exists():
            return False
        cfg_dir = Path(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return (cfg_dir / config.GSI_CONFIG_FILE_NAME).is_file()


def _read_registry(root, subkey: str, name: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(root, subkey) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


def _parse_steam_library_paths(vdf_text: str) -> list[Path]:
    paths = []
    for match in re.finditer(r'"path"\s+"([^"]+)"', vdf_text):
        paths.append(Path(match.group(1).replace("\\\\", "\\")))
    return paths


def find_cs2_cfg_dirs() -> list[Path]:
    candidates: list[Path] = []
    if os.name == "nt":
        import winreg

        steam_path = (
            _read_registry(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath")
            or _read_registry(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath")
        )
        if steam_path:
            candidates.append(Path(steam_path))
            library_file = Path(steam_path) / "steamapps" / "libraryfolders.vdf"
            if library_file.exists():
                try:
                    candidates.extend(_parse_steam_library_paths(library_file.read_text(encoding="utf-8", errors="ignore")))
                except OSError:
                    pass

    candidates.extend(
        [
            Path(r"C:\Program Files (x86)\Steam"),
            Path(r"C:\Program Files\Steam"),
        ]
    )

    cfg_dirs = []
    seen = set()
    for steam_root in candidates:
        cfg = steam_root / "steamapps" / "common" / "Counter-Strike Global Offensive" / "game" / "csgo" / "cfg"
        key = str(cfg).lower()
        if key not in seen and cfg.exists():
            cfg_dirs.append(cfg)
            seen.add(key)
    return cfg_dirs


def _write_gsi_config(target_dir: Path, parent: tk.Misc) -> bool:
    """把配置文件写入目标目录并记录安装标记；失败弹错并返回 False。"""
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        # PyInstaller -F 单文件打包后：内置资源在 sys._MEIPASS 临时解压目录，
        # __file__ 也在那里；源码运行时 __file__ 旁边就是 cfg，两者兼容。
        source = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / config.GSI_CONFIG_FILE_NAME
        target = target_dir / config.GSI_CONFIG_FILE_NAME
        target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        messagebox.showerror(
            "安装失败",
            f"写入失败：{exc}\n请检查权限或换一个目录。",
            parent=parent,
        )
        return False
    _record_install(target_dir)
    messagebox.showinfo(
        "安装完成",
        f"已写入：\n{target}\n\n请重启 CS2 或在控制台执行 exec {config.GSI_CONFIG_FILE_NAME}",
        parent=parent,
    )
    return True


def install_gsi_config(parent: tk.Misc) -> Path | None:
    """自动定位 CS2 cfg 目录并写入配置；成功返回该目录，取消/失败返回 None。

    找不到 CS2 时引导用户手动选择 cfg 文件夹。
    """
    cfg_dirs = find_cs2_cfg_dirs()
    if cfg_dirs and _write_gsi_config(cfg_dirs[0], parent):
        return cfg_dirs[0]
    messagebox.showinfo(
        "选择 CS2 cfg 文件夹",
        "没有自动找到 CS2，请选择 Counter-Strike Global Offensive\\game\\csgo\\cfg 文件夹。",
        parent=parent,
    )
    selected = filedialog.askdirectory(parent=parent, title="选择 CS2 cfg 文件夹")
    if not selected:
        return None
    if _write_gsi_config(Path(selected), parent):
        return Path(selected)
    return None


class _InstallDialog(tk.Toplevel):
    """首次启动的强制安装对话框：只有安装成功才能关闭并进入主程序。"""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.installed = False
        self.title("CS C4 · 首次安装")
        self.resizable(False, False)
        self.configure(bg="#10141a")
        self.protocol("WM_DELETE_WINDOW", self.destroy)

        tk.Label(
            self,
            text=(
                "使用 CS C4 前必须先安装 GSI 配置文件，\n"
                "否则程序无法接收 CS2 的炸弹数据。\n\n"
                "点击「自动安装」将自动定位 CS2 的 cfg 文件夹；\n"
                "找不到时请选择「手动选择」指定该文件夹。\n\n"
                "安装后请重启 CS2（或在控制台执行 "
                f"exec {config.GSI_CONFIG_FILE_NAME}）。"
            ),
            bg="#10141a",
            fg="#c6d3e0",
            justify="left",
            font=("Microsoft YaHei UI", 10),
        ).pack(padx=24, pady=(20, 16))

        btns = tk.Frame(self, bg="#10141a")
        btns.pack(pady=(0, 18))
        tk.Button(
            btns,
            text="自动安装",
            command=self._auto_install,
            bg="#2d4057",
            fg="#ffffff",
            bd=0,
            activebackground="#3a5473",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 10),
            padx=16,
            pady=4,
        ).pack(side="left", padx=6)
        tk.Button(
            btns,
            text="手动选择",
            command=self._manual_install,
            bg="#223246",
            fg="#f4f7fb",
            bd=0,
            activebackground="#2d4057",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 10),
            padx=12,
            pady=4,
        ).pack(side="left", padx=6)
        tk.Button(
            btns,
            text="退出",
            command=self.destroy,
            bg="#223246",
            fg="#f4f7fb",
            bd=0,
            activebackground="#2d4057",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 10),
            padx=12,
            pady=4,
        ).pack(side="left", padx=6)

        self.grab_set()
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _auto_install(self) -> None:
        if install_gsi_config(self) is not None:
            self.installed = True
            self.destroy()

    def _manual_install(self) -> None:
        messagebox.showinfo(
            "选择 CS2 cfg 文件夹",
            "请选择 Counter-Strike Global Offensive\\game\\csgo\\cfg 文件夹。",
            parent=self,
        )
        selected = filedialog.askdirectory(parent=self, title="选择 CS2 cfg 文件夹")
        if not selected:
            return
        if _write_gsi_config(Path(selected), self):
            self.installed = True
            self.destroy()


def ensure_config_installed(parent: tk.Misc) -> bool:
    """首次启动强制安装检查：已安装直接返回 True；
    未安装则弹出对话框，安装成功返回 True，退出返回 False。"""
    if is_config_installed():
        return True
    dialog = _InstallDialog(parent)
    parent.wait_window(dialog)
    return dialog.installed