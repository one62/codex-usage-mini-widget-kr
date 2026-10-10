"""Small always-on-top Windows widget for ChatGPT Work/Codex usage."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
import winreg
import ctypes
from ctypes import wintypes
import logging
from logging.handlers import RotatingFileHandler
from decimal import Decimal
from pathlib import Path
from tkinter import messagebox

from codex_usage import UsageSnapshot, find_codex_executables, query_usage


BG = "#222127"
PANEL = BG
TEXT = "#FBF9FD"
MUTED = "#B8B4C2"
TRACK = "#424149"
GREEN = "#8FE3B2"
YELLOW = "#F7DB83"
RED = "#F49A9A"
GRADIENT_STOPS = (
    (0.00, "#8FE3B2"),
    (0.28, "#8CD9F5"),
    (0.55, "#F7DB83"),
    (0.78, "#F5B87D"),
    (1.00, "#F49A9A"),
)
WINDOW_WIDTH = 270
WINDOW_HEIGHT = 38
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ChatGPTUsageWidget"
RECOVERY_DELAYS = (15, 30, 60, 120, 300)


def diagnostic_log_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CodexUsageMini"
    return base / "widget.log"


def _configure_logger() -> logging.Logger:
    logger = logging.getLogger("codex-usage-mini-widget")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    try:
        path = diagnostic_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=262_144, backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    except OSError:
        logger.addHandler(logging.NullHandler())
    return logger


LOGGER = _configure_logger()


def recovery_delay(failure_count: int) -> int:
    """Return a bounded backoff while continuing to retry forever."""
    return RECOVERY_DELAYS[min(max(1, failure_count) - 1, len(RECOVERY_DELAYS) - 1)]


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MouseHookData(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", _Rect),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_ulong),
    ]


def clamp_position(x: int, y: int, width: int, height: int,
                   area: tuple[int, int, int, int]) -> tuple[int, int]:
    """Keep the entire widget inside a monitor's usable area."""
    left, top, right, bottom = area
    return (
        max(left, min(x, right - width)),
        max(top, min(y, bottom - height)),
    )


def monitor_work_area(x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
    """Return the nearest monitor work area, excluding the Windows taskbar."""
    if os.name != "nt":
        return (0, 0, max(width, x + width), max(height, y + height))
    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromRect.argtypes = [ctypes.POINTER(_Rect), ctypes.c_ulong]
        user32.MonitorFromRect.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]
        user32.GetMonitorInfoW.restype = ctypes.c_int
        rect = _Rect(x, y, x + width, y + height)
        monitor = user32.MonitorFromRect(ctypes.byref(rect), 2)  # nearest monitor
        info = _MonitorInfo()
        info.cbSize = ctypes.sizeof(_MonitorInfo)
        if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            work = info.rcWork
            return (work.left, work.top, work.right, work.bottom)
    except (AttributeError, OSError):
        pass
    return (0, 0, ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1))


def reset_short(timestamp: int | None) -> str:
    if timestamp is None:
        return "↻ —"
    seconds = max(0, timestamp - int(time.time()))
    if seconds == 0:
        return "↻ now"
    # Round up to minutes so a positive remainder never appears as zero.
    total_minutes = (seconds + 59) // 60
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if days:
        return f"↻ {days}d {hours}h {minutes}m"
    if hours:
        return f"↻ {hours}h {minutes}m"
    return f"↻ {minutes}m"


def usage_color(used_percent: int) -> str:
    return gradient_color(max(0.0, min(1.0, used_percent / 100)))


def usage_emoji(used_percent: int | None) -> str:
    if used_percent is None:
        return "✨"
    if used_percent < 25:
        return "🌱"
    if used_percent < 50:
        return "🫧"
    if used_percent < 70:
        return "🌼"
    if used_percent < 90:
        return "🍊"
    return "❤️"


def _rgb(hex_color: str) -> tuple[int, int, int]:
    value = hex_color.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def gradient_color(position: float) -> str:
    """Return a restrained iOS-style usage color along the configured gradient."""
    position = max(0.0, min(1.0, position))
    for index in range(1, len(GRADIENT_STOPS)):
        left_at, left_color = GRADIENT_STOPS[index - 1]
        right_at, right_color = GRADIENT_STOPS[index]
        if position <= right_at:
            span = right_at - left_at
            mix = 0.0 if span == 0 else (position - left_at) / span
            left = _rgb(left_color)
            right = _rgb(right_color)
            channels = tuple(round(a + ((b - a) * mix)) for a, b in zip(left, right))
            return "#{:02X}{:02X}{:02X}".format(*channels)
    return GRADIENT_STOPS[-1][1]


def pythonw_command() -> str:
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    selected = pythonw if pythonw.exists() else interpreter
    script = Path(__file__).resolve()
    return f'"{selected}" "{script}"'


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
            return value == pythonw_command()
    except FileNotFoundError:
        return False


def set_autostart(enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, pythonw_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def enable_dpi_awareness() -> None:
    """Use physical pixels for Tk, monitor bounds, and low-level hook points."""
    if os.name != "nt":
        return
    api = ctypes.WinDLL("user32", use_last_error=True)
    try:
        api.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        api.SetProcessDpiAwarenessContext.restype = ctypes.c_int
        api.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        api.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        for context in (-4, -3):  # PER_MONITOR_AWARE_V2, then V1
            api.SetProcessDpiAwarenessContext(ctypes.c_void_p(context))
            # Also covers an executable whose manifest already set a default.
            if api.SetThreadDpiAwarenessContext(ctypes.c_void_p(context)):
                return
    except AttributeError:
        # Early Windows 10 releases lack the context APIs.
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.SetProcessDpiAwareness(2) == 0:
            return
    raise RuntimeError("Could not enable per-monitor DPI awareness")


class UsageWidget:
    def __init__(self) -> None:
        enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title("ChatGPT Usage")
        self.root.configure(bg=BG)
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.resizable(False, False)
        if os.environ.get("CHATGPT_USAGE_WIDGET_VISUAL_QA") != "1":
            self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.7)
        self.snapshot: UsageSnapshot | None = None
        self.drag_origin: tuple[int, int] | None = None
        self.results: queue.Queue[tuple[int, str, object]] = queue.Queue()
        self.refreshing = False
        self.fetch_generation = 0
        self.failure_count = 0
        self.recovery_after_id: str | None = None
        self.last_error = ""
        self.always_on_top = tk.BooleanVar(value=True)
        self.autostart = tk.BooleanVar(value=autostart_enabled())
        self._build()
        self._position_top_right()
        self._apply_rounded_corners()
        self._bind_actions()
        self._enable_click_through()
        self.refresh()
        self.root.after(500, self._drain_results)
        self.root.after(60_000, self._update_clock)
        self.root.after(60_000, self._scheduled_refresh)

    def _build(self) -> None:
        outer = tk.Frame(self.root, bg=BG, highlightbackground="#56525F", highlightthickness=1)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG, height=36, cursor="fleur")
        header.pack(fill="both", expand=True, padx=7)
        header.pack_propagate(True)
        self.header = header
        # Reserve the right edge before packing the usage labels.
        self.reset_compact = tk.Label(header, text="↻ —", fg=MUTED, bg=BG, font=("Segoe UI", 8))
        self.reset_compact.pack(side="right", padx=(5, 0))
        self.five_label = tk.Label(header, text="5H —%", fg=TEXT, bg=BG, font=("Segoe UI Semibold", 8))
        self.five_label.pack(side="left")
        self.five_meter = tk.Canvas(header, width=28, height=10, bg=BG, bd=0, highlightthickness=0)
        self.five_meter.pack(side="left", padx=(3, 5))

        tk.Frame(header, bg="#56525F", width=1, height=18).pack(side="left", padx=(0, 5))

        self.week_label = tk.Label(header, text="WK —%", fg=TEXT, bg=BG, font=("Segoe UI Semibold", 8))
        self.week_label.pack(side="left")
        self.week_meter = tk.Canvas(header, width=28, height=10, bg=BG, bd=0, highlightthickness=0)
        self.week_meter.pack(side="left", padx=(3, 5))

        self.menu = tk.Menu(self.root, tearoff=False, bg="#202628", fg=TEXT,
                            activebackground="#30393B", activeforeground=TEXT)
        self.menu.add_command(label="지금 새로고침", command=self.refresh)
        self.menu.add_command(label="크레딧 —", state="disabled")
        self.menu.add_command(label="연결 상태", command=self._show_connection_status)
        self.menu.add_separator()
        self.menu.add_checkbutton(label="항상 맨 위에 표시", variable=self.always_on_top, command=self._toggle_topmost)
        self.menu.add_checkbutton(label="Windows 시작 시 자동 실행", variable=self.autostart, command=self._toggle_autostart)
        self.menu.add_separator()
        self.menu.add_command(label="ChatGPT 데스크톱 열기", command=self._open_chatgpt)
        self.menu.add_command(label="종료", command=self.root.destroy)
        self._fit_content_width()

    def _fit_content_width(self) -> None:
        """Keep the reset countdown visible as fonts and percentages change width."""
        self.root.update_idletasks()
        # Header horizontal padding (7 each side) plus the outer border (1 each side).
        width = max(WINDOW_WIDTH, self.header.winfo_reqwidth() + 16)
        x, y = self.root.winfo_x(), self.root.winfo_y()
        area = monitor_work_area(x, y, width, WINDOW_HEIGHT)
        x, y = clamp_position(x, y, width, WINDOW_HEIGHT, area)
        self.root.geometry(f"{width}x{WINDOW_HEIGHT}+{x}+{y}")

    def _bind_actions(self) -> None:
        self.root.bind("<Escape>", lambda _event: self.root.destroy())
        self.root.bind("<Destroy>", self._release_mouse_hook, add="+")

    def _position_top_right(self) -> None:
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        width = self.root.winfo_width()
        area = monitor_work_area(screen_width - 1, 0, width, WINDOW_HEIGHT)
        x, y = clamp_position(area[2] - width - 18, area[1] + 18,
                              width, WINDOW_HEIGHT, area)
        self.root.geometry(f"{width}x{WINDOW_HEIGHT}+{x}+{y}")

    def _apply_rounded_corners(self) -> None:
        """Ask Windows 11 DWM for iOS-like rounded top-level corners."""
        try:
            preference = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                self.root.winfo_id(), 33, ctypes.byref(preference), ctypes.sizeof(preference)
            )
        except (AttributeError, OSError):
            pass

    def _enable_click_through(self) -> None:
        """Keep the body transparent; intercept only right gestures starting on it."""
        self._input_api = ctypes.WinDLL("user32", use_last_error=True)
        api = self._input_api
        self._hook_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                            ctypes.c_size_t, ctypes.c_ssize_t)
        signatures = {
            "GetAncestor": ([ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p),
            "GetWindowLongW": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_long),
            "SetWindowLongW": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_long], ctypes.c_long),
            "GetWindowRect": ([ctypes.c_void_p, ctypes.POINTER(_Rect)], ctypes.c_int),
            "IsWindowVisible": ([ctypes.c_void_p], ctypes.c_int),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                                          wintypes.DWORD),
            "GetGUIThreadInfo": ([wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo)], ctypes.c_int),
            "WindowFromPoint": ([wintypes.POINT], wintypes.HWND),
            "GetClassNameW": ([wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int], ctypes.c_int),
            "PostMessageW": ([wintypes.HWND, ctypes.c_uint, ctypes.c_size_t,
                              ctypes.c_ssize_t], ctypes.c_int),
            "SetWindowsHookExW": ([ctypes.c_int, self._hook_type, ctypes.c_void_p,
                                    ctypes.c_ulong], ctypes.c_void_p),
            "CallNextHookEx": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t,
                                ctypes.c_ssize_t], ctypes.c_ssize_t),
            "UnhookWindowsHookEx": ([ctypes.c_void_p], ctypes.c_int),
            "PeekMessageW": ([ctypes.POINTER(wintypes.MSG), ctypes.c_void_p,
                              ctypes.c_uint, ctypes.c_uint, ctypes.c_uint], ctypes.c_int),
            "GetMessageW": ([ctypes.POINTER(wintypes.MSG), ctypes.c_void_p,
                             ctypes.c_uint, ctypes.c_uint], ctypes.c_int),
            "PostThreadMessageW": ([ctypes.c_ulong, ctypes.c_uint, ctypes.c_size_t,
                                    ctypes.c_ssize_t], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = arguments, result
        self.root.update_idletasks()
        self._input_hwnd = api.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
        self._input_thread_id = api.GetWindowThreadProcessId(self._input_hwnd, None)
        self._mouse_events = queue.SimpleQueue()
        self._right_captured = False
        self._menu_open = False
        self._dragged = False
        self._drag_threshold = (max(1, api.GetSystemMetrics(68)),
                                max(1, api.GetSystemMetrics(69)))  # SM_CX/YDRAG

        # Retain the callback until unhooked so ctypes cannot collect it.
        self._mouse_callback = self._hook_type(self._mouse_hook)
        self._mouse_hook_handle = None
        self._hook_thread_id = None
        self._hook_error = None
        self._hook_ready = threading.Event()
        self._hook_stop = threading.Event()
        self._hook_thread = threading.Thread(target=self._run_mouse_hook, daemon=True)
        self._hook_thread.start()
        if not self._hook_ready.wait(5):
            self._stop_mouse_hook()
            raise RuntimeError("Mouse hook thread did not start")
        if self._hook_error is not None:
            self._stop_mouse_hook()
            raise self._hook_error
        try:
            ctypes.set_last_error(0)
            style = api.GetWindowLongW(self._input_hwnd, -20)
            if not style and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
            ctypes.set_last_error(0)
            previous = api.SetWindowLongW(self._input_hwnd, -20, style | 0x80000 | 0x20)
            if not previous and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError:
            self._stop_mouse_hook()
            raise
        self.root.after(10, self._drain_mouse_events)

    def _run_mouse_hook(self) -> None:
        """Pump hook messages independently of Tk redraws and modal menus."""
        api = self._input_api
        try:
            enable_dpi_awareness()
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentThreadId.argtypes = []
            kernel.GetCurrentThreadId.restype = ctypes.c_ulong
            kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
            kernel.GetModuleHandleW.restype = ctypes.c_void_p
            self._hook_thread_id = kernel.GetCurrentThreadId()
            message = wintypes.MSG()
            # Create the thread queue before publishing readiness or posting WM_QUIT.
            api.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self._mouse_hook_handle = api.SetWindowsHookExW(
                14, self._mouse_callback, kernel.GetModuleHandleW(None), 0
            )
            if not self._mouse_hook_handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self._hook_ready.set()
            while not self._hook_stop.is_set():
                result = api.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                if result == 0:
                    break
        except Exception as exc:
            self._hook_error = exc
            LOGGER.error("Mouse hook failed: %s", exc)
        finally:
            if self._mouse_hook_handle:
                api.UnhookWindowsHookEx(self._mouse_hook_handle)
                self._mouse_hook_handle = None
            self._hook_ready.set()

    def _stop_mouse_hook(self) -> None:
        self._hook_stop.set()
        if self._hook_thread_id is not None:
            self._input_api.PostThreadMessageW(self._hook_thread_id, 0x12, 0, 0)  # WM_QUIT
        self._hook_thread.join(timeout=2)

    def _mouse_hook(self, code: int, message: int, data: int) -> int:
        # No Tk calls or popup work in the hook: Windows requires a quick return.
        if code >= 0 and message in (0x200, 0x201, 0x204, 0x205, 0x207, 0x20B):
            point = ctypes.cast(data, ctypes.POINTER(_MouseHookData)).contents.pt
            x, y = point.x, point.y
            if self._menu_open:
                if message in (0x201, 0x204, 0x207, 0x20B):
                    self._dismiss_menu_outside(x, y)
                return self._input_api.CallNextHookEx(None, code, message, data)
            if message == 0x204 and not self._menu_open:  # WM_RBUTTONDOWN
                rect = _Rect()
                if (self._input_api.IsWindowVisible(self._input_hwnd)
                        and self._input_api.GetWindowRect(self._input_hwnd, ctypes.byref(rect))
                        and rect.left <= x < rect.right and rect.top <= y < rect.bottom):
                    self._right_captured = True
                    self._mouse_events.put(("down", x, y))
                    return 1
            elif self._right_captured:
                if message == 0x205:  # WM_RBUTTONUP, even outside the widget
                    self._right_captured = False
                    self._mouse_events.put(("up", x, y))
                    return 1
                if message == 0x200:
                    self._mouse_events.put(("move", x, y))
        return self._input_api.CallNextHookEx(None, code, message, data)

    def _dismiss_menu_outside(self, x: int, y: int) -> None:
        """Cancel the native popup from the hook without calling Tk across threads."""
        api = self._input_api
        info = _GuiThreadInfo()
        info.cbSize = ctypes.sizeof(info)
        if not api.GetGUIThreadInfo(self._input_thread_id, ctypes.byref(info)):
            return
        if not info.hwndMenuOwner:
            return
        hwnd = api.WindowFromPoint(wintypes.POINT(x, y))
        name = ctypes.create_unicode_buffer(256)
        api.GetClassNameW(hwnd, name, len(name))
        if (name.value == "#32768"
                and api.GetWindowThreadProcessId(hwnd, None) == self._input_thread_id):
            return  # Leave clicks on this thread's native menu to Windows.
        # Tk's popup uses a separate native owner; its modal loop pauses Tk timers.
        api.PostMessageW(info.hwndMenuOwner, 0x1F, 0, 0)  # WM_CANCELMODE

    def _drain_mouse_events(self) -> None:
        while not self._mouse_events.empty():
            action, x, y = self._mouse_events.get_nowait()
            if action == "down":
                self._right_start = (x, y)
                self.drag_origin = (x - self.root.winfo_x(), y - self.root.winfo_y())
                self._dragged = False
            elif self.drag_origin is not None:
                dx, dy = abs(x - self._right_start[0]), abs(y - self._right_start[1])
                if dx >= self._drag_threshold[0] or dy >= self._drag_threshold[1]:
                    self._dragged = True
                if self._dragged:
                    self._move_widget(x, y)
                if action == "up":
                    self.drag_origin = None
                    if not self._dragged:
                        self._show_menu(x, y)
        self.root.after(10, self._drain_mouse_events)

    def _move_widget(self, pointer_x: int, pointer_y: int) -> None:
        x = pointer_x - self.drag_origin[0]
        y = pointer_y - self.drag_origin[1]
        width = self.root.winfo_width()
        area = monitor_work_area(x, y, width, WINDOW_HEIGHT)
        x, y = clamp_position(x, y, width, WINDOW_HEIGHT, area)
        # '+-N' specifies an absolute negative coordinate in Tk geometry syntax.
        self.root.geometry(f"+{x}+{y}")

    def _show_menu(self, x: int, y: int) -> None:
        self._menu_open = True
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()
            self._menu_open = False

    def _release_mouse_hook(self, event: tk.Event) -> None:
        if event.widget is self.root and hasattr(self, "_hook_thread"):
            self._stop_mouse_hook()

    def _toggle_topmost(self) -> None:
        self.root.attributes("-topmost", self.always_on_top.get())

    def _toggle_autostart(self) -> None:
        try:
            set_autostart(self.autostart.get())
        except OSError as exc:
            self.autostart.set(autostart_enabled())
            messagebox.showerror("ChatGPT Usage", f"Windows 시작 시 자동 실행 설정을 변경하지 못했습니다.\n{exc}")

    def _open_chatgpt(self) -> None:
        try:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            executables = find_codex_executables()
            if not executables:
                raise OSError("Codex executable not found")
            subprocess.Popen([executables[0], "app"], creationflags=creation_flags)
        except OSError:
            webbrowser.open("https://chatgpt.com/")

    def _draw_segments(self, canvas: tk.Canvas, remaining_percent: int | None,
                       used_percent: int | None) -> None:
        canvas.delete("all")
        count = 6
        segment_width = 3
        gap = 2
        active = 0 if remaining_percent is None else round(count * remaining_percent / 100)
        color = MUTED if used_percent is None else usage_color(used_percent)
        for index in range(count):
            x = index * (segment_width + gap)
            fill = color if index < active else TRACK
            canvas.create_rectangle(x, 2, x + segment_width, 8, fill=fill, outline="")

    def refresh(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.fetch_generation += 1
        generation = self.fetch_generation
        self.menu.entryconfigure(0, state="disabled")
        if self.snapshot is None:
            self.reset_compact.configure(text="…", fg=MUTED)
        threading.Thread(target=self._fetch_worker, args=(generation,), daemon=True).start()
        self.root.after(50_000, lambda: self._fetch_watchdog(generation))

    def _fetch_worker(self, generation: int) -> None:
        last_error: Exception | None = None
        for delay in (0.0, 2.0):
            if delay:
                time.sleep(delay)
            try:
                self.results.put((generation, "ok", query_usage(timeout=18.0)))
                return
            except Exception as exc:
                last_error = exc
        self.results.put(
            (generation, "error", last_error or RuntimeError("알 수 없는 오류"))
        )

    def _fetch_watchdog(self, generation: int) -> None:
        if not self.refreshing or generation != self.fetch_generation:
            return
        self.fetch_generation += 1  # Ignore a late result from the stuck worker.
        self.refreshing = False
        self.menu.entryconfigure(0, state="normal")
        self._handle_connection_error("사용량 조회가 50초를 초과했습니다.")

    def _drain_results(self) -> None:
        try:
            generation, kind, payload = self.results.get_nowait()
        except queue.Empty:
            self.root.after(500, self._drain_results)
            return
        if generation != self.fetch_generation:
            self.root.after(500, self._drain_results)
            return
        self.refreshing = False
        self.menu.entryconfigure(0, state="normal")
        if kind == "ok" and isinstance(payload, UsageSnapshot):
            if self.recovery_after_id is not None:
                self.root.after_cancel(self.recovery_after_id)
                self.recovery_after_id = None
            self.last_error = ""
            self.failure_count = 0
            self.reset_compact.unbind("<Button-1>")
            self.snapshot = payload
            self._render(payload)
            LOGGER.info("Usage refreshed successfully")
        else:
            message = str(payload) if payload else "알 수 없는 오류"
            self._handle_connection_error(message)
        self.root.after(500, self._drain_results)

    def _handle_connection_error(self, message: str) -> None:
        self.last_error = message
        self.failure_count += 1
        LOGGER.warning("Refresh failed (%s): %s", self.failure_count, message)
        self._render_connection_issue()
        if self.recovery_after_id is None:
            delay = recovery_delay(self.failure_count)
            self.recovery_after_id = self.root.after(delay * 1000, self._recovery_refresh)

    def _render_connection_issue(self) -> None:
        if not self.last_error:
            return
        if self.snapshot is not None and self.failure_count < 3:
            self.reset_compact.configure(text="retry", fg=YELLOW)
        else:
            self.reset_compact.configure(text="offline", fg=RED)
        self.reset_compact.bind("<Button-1>", lambda _event: self.refresh())
        self._fit_content_width()

    def _show_connection_status(self) -> None:
        if self.last_error:
            messagebox.showwarning(
                "Codex Usage",
                f"자동으로 다시 연결을 시도하고 있습니다.\n\n{self.last_error}\n\n로그: {diagnostic_log_path()}",
            )
        else:
            messagebox.showinfo("Codex Usage", "Codex에 정상적으로 연결되어 있습니다.")

    def _recovery_refresh(self) -> None:
        self.recovery_after_id = None
        self.refresh()

    def _render(self, snapshot: UsageSnapshot) -> None:
        primary = snapshot.primary
        if primary:
            color = usage_color(primary.used_percent)
            self.five_label.configure(text=f"5H {primary.used_percent}%", fg=color)
            self.reset_compact.configure(text=reset_short(primary.resets_at), fg=MUTED)
            self.root.after_idle(
                lambda: self._draw_segments(
                    self.five_meter, primary.used_percent, primary.used_percent
                )
            )
        else:
            self.five_label.configure(text="5H —%", fg=TEXT)
            self.reset_compact.configure(text="↻ —", fg=MUTED)
            self._draw_segments(self.five_meter, None, None)

        secondary = snapshot.secondary
        if secondary:
            secondary_color = usage_color(secondary.used_percent)
            self.week_label.configure(text=f"WK {secondary.used_percent}%", fg=secondary_color)
            self.root.after_idle(
                lambda: self._draw_segments(
                    self.week_meter, secondary.used_percent, secondary.used_percent
                )
            )
        else:
            self.week_label.configure(text="WK —%", fg=TEXT)
            self._draw_segments(self.week_meter, None, None)

        if snapshot.unlimited_credits:
            credit_text = "무제한"
        elif snapshot.credit_balance is not None:
            credit_text = f"{snapshot.credit_balance.quantize(Decimal('0.01'))}"
        else:
            credit_text = "—"
        self.menu.entryconfigure(1, label=f"크레딧 {credit_text}")
        self._fit_content_width()

    def _update_clock(self) -> None:
        if self.snapshot:
            self._render(self.snapshot)
        self._render_connection_issue()
        self.root.after(60_000, self._update_clock)

    def _scheduled_refresh(self) -> None:
        self.refresh()
        self.root.after(60_000, self._scheduled_refresh)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("이 위젯은 Windows에서만 실행할 수 있습니다.")
    UsageWidget().run()
