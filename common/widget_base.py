"""Shared UI, Win32 helpers, popup menu, and base widget for usage widgets."""

from __future__ import annotations

import atexit
import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winreg
from ctypes import wintypes
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox
from typing import Callable


BG = "#222127"
TEXT = "#FFFFFF"
MUTED = "#C6C3CE"
TRACK = "#424149"
YELLOW = "#F9E29C"
GRADIENT_STOPS = (
    (0.00, "#A5E9C1"),
    (0.28, "#A3E1F7"),
    (0.55, "#F9E29C"),
    (0.78, "#F7C697"),
    (1.00, "#F6AEAE"),
)
WINDOW_WIDTH = 270
WINDOW_HEIGHT = 38
FONT_FAMILY = "NEXON Football Gothic L"
BOLD_FONT_FAMILY = "NEXON Football Gothic B"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RECOVERY_DELAYS = (15, 30, 60, 120, 300)

DEFAULT_FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


def load_bundled_fonts(font_directory: Path | None = None) -> None:
    """Register bundled Nexon Football Gothic fonts privately for this process."""
    target_dir = font_directory or DEFAULT_FONT_DIR
    gdi32 = ctypes.WinDLL("gdi32")
    gdi32.AddFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    gdi32.AddFontResourceExW.restype = ctypes.c_int
    gdi32.RemoveFontResourceExW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    gdi32.RemoveFontResourceExW.restype = wintypes.BOOL
    private_font = 0x10  # FR_PRIVATE: available only to this process.
    for filename in ("NEXONFootballGothic-Light.otf", "NEXONFootballGothic-Bold.otf"):
        path = str(target_dir / filename)
        if gdi32.AddFontResourceExW(path, private_font, None):
            atexit.register(gdi32.RemoveFontResourceExW, path, private_font, None)


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


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_ulong),
    ]


def clamp_position(
    x: int,
    y: int,
    width: int,
    height: int,
    area: tuple[int, int, int, int],
) -> tuple[int, int]:
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


def reset_short(timestamp: int | None, *, weekly: bool = False) -> str:
    if timestamp is None:
        return "—"
    seconds = max(0, timestamp - int(time.time()))
    if seconds == 0:
        return "now"
    # Round up to minutes so a positive remainder never appears as zero.
    total_minutes = (seconds + 59) // 60
    days, remainder = divmod(total_minutes, 1440)
    hours, minutes = divmod(remainder, 60)
    if weekly:
        return f"{days}d {hours}h"
    if total_minutes == 1440:
        return "24h 0m"
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def usage_color(used_percent: int) -> str:
    return gradient_color(max(0.0, min(1.0, used_percent / 100)))


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


def pythonw_command_for(script_path: Path) -> str:
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    selected = pythonw if pythonw.exists() else interpreter
    return f'"{selected}" "{script_path.resolve()}"'


def autostart_enabled_for(run_value: str, script_path: Path) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, run_value)
            return value == pythonw_command_for(script_path)
    except FileNotFoundError:
        return False


def set_autostart_for(run_value: str, script_path: Path, enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, run_value, 0, winreg.REG_SZ, pythonw_command_for(script_path))
        else:
            try:
                winreg.DeleteValue(key, run_value)
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


def run_single_instance(
    mutex_name: str,
    app_title: str,
    create_and_run: Callable[[], None],
) -> None:
    """Ensure only one instance per named Windows mutex runs at a time."""
    if os.name != "nt":
        raise SystemExit("이 위젯은 Windows에서만 실행할 수 있습니다.")

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    error_already_exists = 183
    ctypes.set_last_error(0)
    mutex = kernel.CreateMutexW(None, False, mutex_name)
    error = ctypes.get_last_error()
    if not mutex:
        raise ctypes.WinError(error)

    if error == error_already_exists:
        kernel.CloseHandle(mutex)
        messagebox.showinfo(app_title, "위젯이 이미 실행 중입니다.")
        return

    try:
        create_and_run()
    finally:
        kernel.CloseHandle(mutex)


class PopupMenu:
    """Tk popup with inert information labels and selectable action rows."""

    def __init__(
        self,
        root: tk.Tk,
        on_close: Callable[[], None],
        *,
        reserve_indicator_space: bool = False,
    ) -> None:
        self.root = root
        self.on_close = on_close
        self.reserve_indicator_space = reserve_indicator_space
        self.font = tkfont.nametofont("TkMenuFont", root=root).copy()
        self.font.configure(family=FONT_FAMILY)
        self.background = "#2A3032"
        self.active_background = "#3A4345"
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.body = tk.Frame(
            self.window,
            bg=self.background,
            highlightbackground="#56525F",
            highlightthickness=1,
        )
        self.body.pack(fill="both", expand=True)
        self.entries: list[dict] = []
        self.selected: int | None = None
        self.open = False
        self.window.bind("<Escape>", self._escape)
        self.window.bind("<Up>", lambda event: self._navigate(-1))
        self.window.bind("<Down>", lambda event: self._navigate(1))
        self.window.bind("<Return>", self._activate_selected)
        self.window.bind("<space>", self._activate_selected)
        self.window.bind("<Left>", lambda event: self._adjust_scale(-1))
        self.window.bind("<Right>", lambda event: self._adjust_scale(1))
        self.window.bind("<Home>", lambda event: self._adjust_scale(endpoint=0))
        self.window.bind("<End>", lambda event: self._adjust_scale(endpoint=100))
        self.window.bind("<FocusOut>", self._focus_out)

    def _add_row(
        self,
        kind: str,
        label: str,
        state: str = "normal",
        command=None,
        variable: tk.BooleanVar | None = None,
        right_text: str | None = None,
    ) -> None:
        index = len(self.entries)
        row = tk.Frame(self.body, bg=self.background, bd=0)
        row.pack(fill="x")
        indicator = None
        if self.reserve_indicator_space or kind == "check":
            indicator = tk.Canvas(
                row,
                width=18,
                height=18,
                bg=self.background,
                bd=0,
                highlightthickness=0,
            )
            indicator.pack(side="left", padx=(12, 6))
        right_widget = None
        if right_text is not None:
            right_widget = tk.Label(
                row,
                anchor="e",
                padx=0,
                pady=6,
                bg=self.background,
                bd=0,
                font=self.font,
            )
            right_widget.pack(side="right", padx=(0, 12))
        text_widget = tk.Label(
            row,
            anchor="w",
            padx=0,
            pady=6,
            bg=self.background,
            bd=0,
            font=self.font,
        )
        text_widget.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(0 if indicator else 12, 12),
        )
        self.entries.append(
            dict(
                kind=kind,
                label=label,
                state=state,
                command=command,
                variable=variable,
                widget=row,
                text_widget=text_widget,
                indicator=indicator,
                right_widget=right_widget,
                right_text=right_text,
            )
        )
        if kind != "info":
            for target in (row, indicator, text_widget):
                if target is not None:
                    target.bind("<Enter>", lambda event, i=index: self._select(i))
                    target.bind("<Leave>", lambda event: self._select(None))
                    target.bind("<ButtonRelease-1>", lambda event, i=index: self._invoke(i))
        # Information labels have no mouse, keyboard, or command bindings.
        self._render_row(index)

    def add_command(self, *, label: str, command, state: str = "normal") -> None:
        self._add_row("command", label, state, command)

    def add_info(self, *, label: str, right_text: str | None = None) -> None:
        self._add_row("info", label, "disabled", right_text=right_text)

    def add_checkbutton(self, *, label: str, variable: tk.BooleanVar, command) -> None:
        self._add_row("check", label, command=command, variable=variable)

    def add_scale(self, *, label: str, variable: tk.IntVar, command) -> None:
        index = len(self.entries)
        row = tk.Frame(self.body, bg=self.background)
        row.pack(fill="x", padx=12, pady=4)
        caption = tk.Frame(row, bg=self.background)
        caption.pack(fill="x")
        text_widget = tk.Label(
            caption,
            text=label,
            anchor="w",
            bg=self.background,
            fg=TEXT,
            font=self.font,
        )
        text_widget.pack(side="left")
        font = self.font
        value_box = tk.Frame(
            caption,
            bg=self.background,
            width=font.measure("100%") + 4,
            height=font.metrics("linespace") + 4,
        )
        value_box.pack(side="right")
        value_box.pack_propagate(False)
        value_widget = tk.Label(
            value_box,
            text=f"{variable.get()}%",
            anchor="e",
            bg=self.background,
            fg=TEXT,
            font=font,
        )
        value_widget.pack(fill="both", expand=True)
        scale = tk.Scale(
            row,
            from_=0,
            to=100,
            resolution=1,
            orient="horizontal",
            variable=variable,
            showvalue=False,
            length=210,
            width=12,
            sliderlength=16,
            bd=0,
            highlightthickness=0,
            takefocus=True,
            bg=self.background,
            troughcolor="#424149",
            activebackground="#8CD9F5",
            command=lambda value, i=index: self._scale_changed(i, value),
        )
        scale.pack(fill="x")
        self.entries.append(
            dict(
                kind="scale",
                label=label,
                state="normal",
                command=command,
                variable=variable,
                widget=row,
                text_widget=text_widget,
                value_widget=value_widget,
                scale=scale,
                font=font,
            )
        )
        for target in (row, caption, text_widget, value_widget, scale):
            target.bind("<Enter>", lambda event, i=index: self._select(i))
        scale.bind("<ButtonPress-1>", lambda event, i=index: self._select(i), add="+")
        scale.bind("<FocusIn>", lambda event, i=index: self._select(i))
        scale.bind("<Up>", lambda event: self._navigate(-1))
        scale.bind("<Down>", lambda event: self._navigate(1))
        scale.bind("<Left>", lambda event: self._adjust_scale(-1))
        scale.bind("<Right>", lambda event: self._adjust_scale(1))
        scale.bind("<Home>", lambda event: self._adjust_scale(endpoint=0))
        scale.bind("<End>", lambda event: self._adjust_scale(endpoint=100))
        scale.bind("<Return>", self._activate_selected)
        scale.bind("<space>", self._activate_selected)

    def _scale_changed(self, index: int, value: str) -> None:
        entry = self.entries[index]
        number = max(0, min(100, round(float(value))))
        # Keep the track fixed while dragging, including 9 -> 10 -> 100%.
        entry["value_widget"].configure(text=f"{number}%")
        entry["command"](number)

    def _adjust_scale(self, delta: int = 0, *, endpoint: int | None = None) -> str:
        if self.selected is not None:
            entry = self.entries[self.selected]
            if entry["kind"] == "scale":
                value = endpoint if endpoint is not None else entry["variable"].get() + delta
                entry["scale"].set(max(0, min(100, value)))
        return "break"

    def add_separator(self) -> None:
        line = tk.Frame(self.body, bg="#56525F", height=1)
        line.pack(fill="x", padx=8, pady=4)
        self.entries.append(dict(kind="separator", widget=line))

    def index(self, index: str) -> int:
        if index != "end":
            raise ValueError("Only the end index is supported")
        return len(self.entries) - 1

    def entryconfigure(self, index: int, **options) -> None:
        self.entries[index].update(options)
        if self.selected == index and not self._enabled(index):
            self.selected = None
        self._render_row(index)
        if self.open:
            self._fit_window(self.window.winfo_x(), self.window.winfo_y())

    def _enabled(self, index: int) -> bool:
        entry = self.entries[index]
        return entry["kind"] in ("command", "check", "scale") and entry["state"] == "normal"

    def _render_row(self, index: int) -> None:
        entry = self.entries[index]
        if entry["kind"] == "separator":
            return
        if entry["kind"] == "scale":
            entry["text_widget"].configure(fg=TEXT if self.selected == index else MUTED)
            return
        enabled = self._enabled(index)
        foreground = TEXT if enabled else MUTED
        background = self.active_background if enabled and self.selected == index else self.background
        for target in (entry["widget"], entry["text_widget"], entry["indicator"]):
            if target is not None:
                target.configure(bg=background, cursor="hand2" if enabled else "arrow")
        entry["text_widget"].configure(text=entry["label"], fg=foreground)
        if entry["right_widget"] is not None:
            entry["right_widget"].configure(
                text=entry["right_text"],
                fg=foreground,
                bg=background,
            )
        indicator = entry["indicator"]
        if indicator is not None:
            indicator.delete("all")
            if entry["kind"] == "check":
                # Draw the check instead of depending on a font's Unicode glyph.
                indicator.create_rectangle(2, 2, 15, 15, outline=foreground)
                if entry["variable"].get():
                    indicator.create_line(4, 8, 7, 11, 13, 5, fill=foreground, width=2)

    def _select(self, index: int | None) -> None:
        previous = self.selected
        self.selected = index if index is not None and self._enabled(index) else None
        for candidate in (previous, self.selected):
            if candidate is not None:
                self._render_row(candidate)

    def _navigate(self, step: int) -> str:
        choices = [i for i in range(len(self.entries)) if self._enabled(i)]
        if choices:
            if self.selected in choices:
                index = (choices.index(self.selected) + step) % len(choices)
            else:
                index = 0 if step > 0 else -1
            self._select(choices[index])
        return "break"

    def _activate_selected(self, event: tk.Event) -> str:
        if self.selected is not None:
            self._invoke(self.selected)
        return "break"

    def _invoke(self, index: int) -> None:
        if not self.open or not self._enabled(index):
            return
        entry = self.entries[index]
        if entry["kind"] == "scale":
            entry["scale"].focus_set()
            return
        self.close()
        if entry["kind"] == "check":
            entry["variable"].set(not entry["variable"].get())
        entry["command"]()

    def show(self, x: int, y: int) -> None:
        self._select(None)
        for index in range(len(self.entries)):
            self._render_row(index)
        self._fit_window(x, y)
        self.window.deiconify()
        self.window.lift()
        self.open = True
        self.window.focus_force()

    def _fit_window(self, x: int, y: int) -> None:
        self.window.update_idletasks()
        width, height = self.window.winfo_reqwidth(), self.window.winfo_reqheight()
        area = monitor_work_area(x, y, width, height)
        x, y = clamp_position(x, y, width, height, area)
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    def close(self) -> None:
        if not self.open:
            return
        self.open = False
        self.window.withdraw()
        self._select(None)
        self.on_close()

    def _escape(self, event: tk.Event) -> str:
        self.close()
        return "break"

    def _focus_out(self, event: tk.Event) -> None:
        self.window.after_idle(self._close_if_unfocused)

    def _close_if_unfocused(self) -> None:
        if self.open:
            focused = self.window.focus_get()
            if focused is None or focused.winfo_toplevel() is not self.window:
                self.close()


class BaseUsageWidget:
    """Common windowing, hook, transparency, and layout behavior for usage widgets."""

    APP_TITLE = "Usage"
    RUN_VALUE = ""

    def _init_base(self, *, script_path: Path) -> None:
        enable_dpi_awareness()
        load_bundled_fonts()
        self._script_path = script_path
        self.settings = self._load_widget_settings()
        self._settings_after_id: str | None = None
        self._settings_ready = False
        self._closing = False
        self.root = tk.Tk()
        self.root.title(self.APP_TITLE)
        self.root.configure(bg=BG)
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.resizable(False, False)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.transparency = tk.IntVar(master=self.root, value=self.settings.transparency)
        self.movement_locked = tk.BooleanVar(master=self.root, value=self.settings.movement_locked)
        self.hover_transparency_enabled = tk.BooleanVar(
            master=self.root,
            value=self.settings.hover_transparency_enabled,
        )
        self._hovered = False
        self.root.attributes("-alpha", 1.0 - self.transparency.get() / 100)
        self.snapshot = None
        self.drag_origin: tuple[int, int] | None = None
        self.results: queue.Queue[tuple[int, str, object]] = queue.Queue()
        self.refreshing = False
        self.fetch_generation = 0
        self.failure_count = 0
        self.recovery_after_id: str | None = None
        self.connection_failed = False
        self.last_error = ""
        self.auth_required = False
        self.always_on_top = tk.BooleanVar(value=True)
        self.autostart = tk.BooleanVar(value=autostart_enabled_for(self.RUN_VALUE, self._script_path))

    def _finalize_init(self) -> None:
        self._restore_position()
        self._apply_rounded_corners()
        self._bind_actions()
        self._enable_click_through()
        self._settings_ready = True
        self._update_hover()
        self.refresh()
        self.root.after(500, self._drain_results)
        self._start_extra_timers()
        self.root.after(60_000, self._update_clock)
        self.root.after(60_000, self._scheduled_refresh)

    # Subclass extension points (with safe defaults for partial test instances)
    def _load_widget_settings(self):
        raise NotImplementedError

    def _save_widget_settings(self, settings) -> None:
        raise NotImplementedError

    def _start_extra_timers(self) -> None:
        pass

    def _can_start_refresh(self) -> bool:
        return not self.refreshing

    def _on_refresh_state_changed(self) -> None:
        pass

    def _query_usage(self, timeout: float = 18.0):
        raise NotImplementedError

    def _is_non_retryable_fetch_error(self, exc: Exception) -> bool:
        return False

    def _open_app(self) -> None:
        pass

    def _render(self, snapshot) -> None:
        raise NotImplementedError

    def refresh(self) -> None:
        if not self._can_start_refresh():
            return
        self.refreshing = True
        self.fetch_generation += 1
        generation = self.fetch_generation
        self.menu.entryconfigure(0, state="disabled")
        self._update_connection_status()
        self._on_refresh_state_changed()
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
                self.results.put((generation, "ok", self._query_usage(timeout=18.0)))
                return
            except Exception as exc:
                if self._is_non_retryable_fetch_error(exc):
                    self.results.put((generation, "error", exc))
                    return
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
        if kind == "ok" and payload is not None:
            if self.recovery_after_id is not None:
                self.root.after_cancel(self.recovery_after_id)
                self.recovery_after_id = None
            self.connection_failed = False
            self.last_error = ""
            self.auth_required = False
            self.failure_count = 0
            self.snapshot = payload
            self._update_connection_status()
            self._render(payload)
        else:
            auth_required = isinstance(payload, Exception) and self._is_non_retryable_fetch_error(payload)
            message = str(payload) if payload else "알 수 없는 오류"
            self._handle_connection_error(message, auth_required=auth_required)
        self.root.after(500, self._drain_results)

    def _handle_connection_error(self, message: str = "", *, auth_required: bool = False) -> None:
        self.connection_failed = True
        self.last_error = message or "connection failed"
        self.auth_required = auth_required
        if auth_required and self.recovery_after_id is not None:
            self.root.after_cancel(self.recovery_after_id)
            self.recovery_after_id = None
        self._update_connection_status()
        self._on_refresh_state_changed()
        self.failure_count += 1
        self._render_connection_issue()
        if not auth_required and self.recovery_after_id is None:
            delay = recovery_delay(self.failure_count)
            self.recovery_after_id = self.root.after(delay * 1000, self._recovery_refresh)

    def _render_connection_issue(self) -> None:
        if not getattr(self, "connection_failed", False) and not getattr(self, "last_error", ""):
            return
        if (
            self.snapshot is not None
            and self.failure_count < 3
            and not getattr(self, "auth_required", False)
        ):
            self.reset_compact.configure(text="retry", fg=YELLOW)
        else:
            self.reset_compact.configure(text="offline", fg=MUTED)

    def _update_connection_status(self) -> None:
        """Display cached query state without starting a connection check."""
        auth_clickable = False
        if self.refreshing:
            status = "조회 중"
        elif getattr(self, "auth_required", False):
            status = "로그인 필요"
            auth_clickable = True
        elif getattr(self, "connection_failed", False) or getattr(self, "last_error", ""):
            status = "재연결 중"
        elif self.snapshot is not None:
            status = "정상"
        else:
            status = "확인 전"
        self.menu.entryconfigure(
            self.connection_status_index,
            label=f"연결 상태: {status}",
            state="normal" if auth_clickable else "disabled",
        )

    def _build_header_shell(
        self,
        *,
        logo_path: Path,
        five_fg: str,
        week_fg: str,
    ) -> None:
        outer = tk.Frame(self.root, bg=BG, highlightbackground="#56525F", highlightthickness=1)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG, height=36, cursor="fleur")
        header.pack(fill="both", expand=True, padx=7)
        header.pack_propagate(True)
        self.header = header

        try:
            self._logo_image = tk.PhotoImage(master=self.root, file=str(logo_path))
            tk.Label(header, image=self._logo_image, bg=BG, bd=0).pack(side="left", padx=(2, 4))
        except tk.TclError:
            pass

        percent_font = tkfont.Font(self.root, family=BOLD_FONT_FAMILY, size=9)
        reset_font = tkfont.Font(self.root, family=FONT_FAMILY, size=9)

        def add_right_aligned_label(
            text: str,
            max_text: str,
            color: str,
            font: tkfont.Font,
            padx: tuple[int, int] = (0, 0),
        ) -> tk.Label:
            # Measure with Tk's active font metrics, which already reflect DPI scaling.
            column = tk.Frame(header, width=font.measure(max_text), height=18, bg=BG)
            column.pack_propagate(False)
            label = tk.Label(
                column,
                text=text,
                anchor="e",
                padx=0,
                pady=0,
                bd=0,
                highlightthickness=0,
                fg=color,
                bg=BG,
                font=font,
            )
            label.pack(fill="both", expand=True)
            column.pack(side="left", padx=padx)
            return label

        def add_metric_labels(
            title: str,
            value: str,
            max_text: str,
            color: str,
            font: tkfont.Font,
            padx: tuple[int, int] = (1, 0),
        ) -> tuple[tk.Label, tk.Label]:
            column = tk.Frame(header, width=font.measure(max_text), height=18, bg=BG)
            column.pack_propagate(False)
            title_label = tk.Label(
                column,
                text=title,
                anchor="w",
                padx=0,
                pady=0,
                bd=0,
                highlightthickness=0,
                fg=color,
                bg=BG,
                font=font,
            )
            title_label.pack(side="left", fill="y")
            value_label = tk.Label(
                column,
                text=value,
                anchor="e",
                padx=0,
                pady=0,
                bd=0,
                highlightthickness=0,
                fg=color,
                bg=BG,
                font=font,
            )
            value_label.pack(side="left", fill="both", expand=True)
            column.pack(side="left", padx=padx)
            return title_label, value_label

        self.five_title, self.five_label = add_metric_labels(
            "5H",
            "—%",
            "5H 100%",
            five_fg,
            percent_font,
        )
        self.five_meter = tk.Canvas(header, width=23, height=10, bg=BG, bd=0, highlightthickness=0)
        self.five_meter.pack(side="left", padx=(3, 5))
        self.reset_compact = add_right_aligned_label(
            "—",
            "5h 59m",
            MUTED,
            reset_font,
            padx=(0, 8),
        )

        tk.Frame(header, bg="#56525F", width=1, height=18).pack(side="left", padx=(0, 5))

        self.week_title, self.week_label = add_metric_labels(
            "WK",
            "—%",
            "WK 100%",
            week_fg,
            percent_font,
        )
        self.week_meter = tk.Canvas(header, width=23, height=10, bg=BG, bd=0, highlightthickness=0)
        self.week_meter.pack(side="left", padx=(3, 5))
        self.week_reset = add_right_aligned_label(
            "—",
            "7d 23h",
            MUTED,
            reset_font,
        )

    def _add_common_preference_menu_items(self, menu: PopupMenu) -> None:
        menu.add_scale(label="투명도", variable=self.transparency, command=self._set_transparency)
        menu.add_checkbutton(
            label="마우스를 올리면 투명도 80%",
            variable=self.hover_transparency_enabled,
            command=self._toggle_hover_transparency,
        )
        menu.add_checkbutton(
            label="이동 잠금",
            variable=self.movement_locked,
            command=self._schedule_settings_save,
        )
        menu.add_separator()
        menu.add_checkbutton(
            label="항상 맨 위에 표시",
            variable=self.always_on_top,
            command=self._toggle_topmost,
        )
        menu.add_checkbutton(
            label="Windows 시작 시 자동 실행",
            variable=self.autostart,
            command=self._toggle_autostart,
        )

    def _fit_content_width(self) -> None:
        """Fit the window to fixed pixel columns measured at the current DPI."""
        self.root.update_idletasks()
        # Label columns have fixed pixel widths, so content changes cannot resize it.
        # Tk font metrics are DPI-aware because awareness is enabled before Tk starts.
        width = max(WINDOW_WIDTH, self.header.winfo_reqwidth() + 16)
        x, y = self.root.winfo_x(), self.root.winfo_y()
        area = monitor_work_area(x, y, width, WINDOW_HEIGHT)
        x, y = clamp_position(x, y, width, WINDOW_HEIGHT, area)
        self.root.geometry(f"{width}x{WINDOW_HEIGHT}+{x}+{y}")

    def _bind_actions(self) -> None:
        self.root.bind("<Escape>", lambda _event: self._close())
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Destroy>", self._release_mouse_hook, add="+")
        self.root.bind("<Configure>", self._position_changed, add="+")

    def _restore_position(self) -> None:
        if self.settings.x is None or self.settings.y is None:
            self._position_top_right()
            return
        self.root.update_idletasks()
        width = self.root.winfo_width()
        x, y = self.settings.x, self.settings.y
        area = monitor_work_area(x, y, width, WINDOW_HEIGHT)
        x, y = clamp_position(x, y, width, WINDOW_HEIGHT, area)
        self.root.geometry(f"+{x}+{y}")

    def _position_changed(self, event: tk.Event) -> None:
        if event.widget is self.root:
            self._schedule_settings_save()

    def _schedule_settings_save(self) -> None:
        if not self._settings_ready or self._closing:
            return
        if self._settings_after_id is not None:
            self.root.after_cancel(self._settings_after_id)
        self._settings_after_id = self.root.after(350, self._save_preferences)

    def _save_preferences(self) -> None:
        self._settings_after_id = None
        # Read the Scale variable directly: its command can still be pending
        # when the user closes the widget immediately after the last adjustment.
        self.settings.transparency = self.transparency.get()
        self.settings.movement_locked = self.movement_locked.get()
        self.settings.hover_transparency_enabled = self.hover_transparency_enabled.get()
        self.settings.x, self.settings.y = self.root.winfo_x(), self.root.winfo_y()
        try:
            self._save_widget_settings(self.settings)
        except OSError:
            pass

    def _close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._settings_after_id is not None:
            self.root.after_cancel(self._settings_after_id)
        self._save_preferences()
        self.root.destroy()

    def _position_top_right(self) -> None:
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        width = self.root.winfo_width()
        area = monitor_work_area(screen_width - 1, 0, width, WINDOW_HEIGHT)
        x, y = clamp_position(area[2] - width - 18, area[1] + 18, width, WINDOW_HEIGHT, area)
        self.root.geometry(f"{width}x{WINDOW_HEIGHT}+{x}+{y}")

    def _apply_rounded_corners(self) -> None:
        """Ask Windows 11 DWM for iOS-like rounded top-level corners."""
        try:
            dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
            dwmapi.DwmSetWindowAttribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
            preference = ctypes.c_int(2)  # DWMWCP_ROUND
            dwmapi.DwmSetWindowAttribute(
                self.root.winfo_id(),
                33,
                ctypes.byref(preference),
                ctypes.sizeof(preference),
            )
        except (AttributeError, OSError):
            pass

    def _enable_click_through(self) -> None:
        """Keep the body transparent; intercept only right gestures starting on it."""
        self._input_api = ctypes.WinDLL("user32", use_last_error=True)
        api = self._input_api
        self._hook_type = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            ctypes.c_int,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        signatures = {
            "GetAncestor": ([ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p),
            "GetWindowLongW": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_long),
            "SetWindowLongW": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_long], ctypes.c_long),
            "GetWindowRect": ([ctypes.c_void_p, ctypes.POINTER(_Rect)], ctypes.c_int),
            "IsWindowVisible": ([ctypes.c_void_p], ctypes.c_int),
            "WindowFromPoint": ([wintypes.POINT], wintypes.HWND),
            "SetWindowsHookExW": (
                [ctypes.c_int, self._hook_type, ctypes.c_void_p, ctypes.c_ulong],
                ctypes.c_void_p,
            ),
            "CallNextHookEx": (
                [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t],
                ctypes.c_ssize_t,
            ),
            "UnhookWindowsHookEx": ([ctypes.c_void_p], ctypes.c_int),
            "PeekMessageW": (
                [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint],
                ctypes.c_int,
            ),
            "GetMessageW": (
                [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint],
                ctypes.c_int,
            ),
            "PostThreadMessageW": (
                [ctypes.c_ulong, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t],
                ctypes.c_int,
            ),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = arguments, result
        self.root.update_idletasks()
        self._input_hwnd = api.GetAncestor(self.root.winfo_id(), 2)  # GA_ROOT
        self._mouse_events = queue.SimpleQueue()
        self._right_captured = False
        self._menu_open = False
        self._menu_hwnd = None
        self._dragged = False
        self._drag_threshold = (
            max(1, api.GetSystemMetrics(68)),
            max(1, api.GetSystemMetrics(69)),
        )  # SM_CX/YDRAG

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
                14,
                self._mouse_callback,
                kernel.GetModuleHandleW(None),
                0,
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
            if message == 0x204:  # WM_RBUTTONDOWN
                rect = _Rect()
                if (
                    self._input_api.IsWindowVisible(self._input_hwnd)
                    and self._input_api.GetWindowRect(self._input_hwnd, ctypes.byref(rect))
                    and rect.left <= x < rect.right
                    and rect.top <= y < rect.bottom
                ):
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
        """Queue dismissal while leaving the outside click to its target app."""
        api = self._input_api
        hwnd = api.WindowFromPoint(wintypes.POINT(x, y))
        if self._menu_hwnd and api.GetAncestor(hwnd, 2) == self._menu_hwnd:
            return
        self._mouse_events.put(("dismiss", x, y))

    def _drain_mouse_events(self) -> None:
        while not self._mouse_events.empty():
            action, x, y = self._mouse_events.get_nowait()
            if action == "dismiss":
                self.menu.close()
            elif action == "down":
                self._right_start = (x, y)
                self.drag_origin = (x - self.root.winfo_x(), y - self.root.winfo_y())
                self._dragged = False
            elif self.drag_origin is not None:
                dx, dy = abs(x - self._right_start[0]), abs(y - self._right_start[1])
                if dx >= self._drag_threshold[0] or dy >= self._drag_threshold[1]:
                    self._dragged = True
                if self._dragged and not self.movement_locked.get():
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
        self._update_connection_status()
        self.menu.show(x, y)
        self._menu_hwnd = self._input_api.GetAncestor(self.menu.window.winfo_id(), 2)
        self._menu_open = True

    def _menu_closed(self) -> None:
        self._menu_open = False
        self._menu_hwnd = None
        self._ensure_topmost()

    def _release_mouse_hook(self, event: tk.Event) -> None:
        if event.widget is self.root and hasattr(self, "_hook_thread"):
            self._stop_mouse_hook()

    def _toggle_topmost(self) -> None:
        self.root.attributes("-topmost", self.always_on_top.get())

    def _ensure_topmost(self) -> None:
        """Restore native WS_EX_TOPMOST if enabled and unexpectedly cleared."""
        if self._closing or not self.always_on_top.get():
            return
        api = getattr(self, "_input_api", None)
        hwnd = getattr(self, "_input_hwnd", None)
        if api is None or not hwnd:
            return
        try:
            style = api.GetWindowLongW(hwnd, -20)
            if style and not (style & 0x8):
                self.root.attributes("-topmost", True)
                self.root.lift()
        except (OSError, tk.TclError):
            pass

    def _set_transparency(self, value: int) -> None:
        self._apply_transparency(value)
        self._schedule_settings_save()

    def _apply_transparency(self, value: int) -> None:
        alpha = 1.0 - max(0, min(100, value)) / 100
        if self._hovered and self.hover_transparency_enabled.get():
            alpha = 0.2
        self.root.attributes("-alpha", alpha)

    def _toggle_hover_transparency(self) -> None:
        self._apply_transparency(self.transparency.get())
        self._schedule_settings_save()

    def _update_hover(self) -> None:
        """Poll coordinates because the click-through window receives no enter events."""
        if self._closing:
            return
        x, y = self.root.winfo_pointerxy()
        left, top = self.root.winfo_rootx(), self.root.winfo_rooty()
        hovered = (
            left <= x < left + self.root.winfo_width()
            and top <= y < top + self.root.winfo_height()
        )
        if hovered != self._hovered:
            self._hovered = hovered
            self._apply_transparency(self.transparency.get())
        self.root.after(50, self._update_hover)

    def _toggle_autostart(self) -> None:
        try:
            set_autostart_for(self.RUN_VALUE, self._script_path, self.autostart.get())
        except OSError as exc:
            self.autostart.set(autostart_enabled_for(self.RUN_VALUE, self._script_path))
            messagebox.showerror(
                self.APP_TITLE,
                f"Windows 시작 시 자동 실행 설정을 변경하지 못했습니다.\n{exc}",
            )

    def _draw_segments(self, canvas: tk.Canvas, used_percent: int | None) -> None:
        canvas.delete("all")
        count = 5
        segment_width = 3
        gap = 2
        active = 0 if used_percent is None else round(count * used_percent / 100)
        color = MUTED if used_percent is None else usage_color(used_percent)
        for index in range(count):
            x = index * (segment_width + gap)
            fill = color if index < active else TRACK
            canvas.create_rectangle(x, 2, x + segment_width, 8, fill=fill, outline="")

    def _recovery_refresh(self) -> None:
        self.recovery_after_id = None
        self.refresh()

    def _update_clock(self) -> None:
        self._ensure_topmost()
        if self.snapshot:
            self._render(self.snapshot)
        self._render_connection_issue()
        self.root.after(60_000, self._update_clock)

    def _scheduled_refresh(self) -> None:
        self.refresh()
        self.root.after(60_000, self._scheduled_refresh)

    def run(self) -> None:
        self.root.mainloop()
