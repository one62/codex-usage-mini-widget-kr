"""Small always-on-top Windows widget for Gemini / Antigravity usage."""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

_WIDGET_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WIDGET_DIR.parent
for _path in (str(_REPO_ROOT), str(_WIDGET_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from common.widget_base import (
    BG,
    FONT_FAMILY,
    MUTED,
    PopupMenu,
    RED,
    RUN_KEY,
    TEXT,
    TRACK,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
    YELLOW,
    BaseUsageWidget,
    _MonitorInfo,
    _MouseHookData,
    _Rect,
    autostart_enabled_for,
    clamp_position,
    enable_dpi_awareness,
    gradient_color,
    load_bundled_fonts,
    monitor_work_area,
    pythonw_command_for,
    recovery_delay,
    reset_short,
    run_single_instance,
    set_autostart_for,
    usage_color,
)
from gemini_usage import AuthRequiredError, UsageSnapshot, find_antigravity_executables, query_usage
from widget_settings import load_settings, save_settings


FIVE_HOUR_TEXT = "#A5E9C1"
WEEK_TEXT = "#D2B8FF"
RUN_VALUE = "GeminiUsageWidget"


def diagnostic_log_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "GeminiUsageMini"
    return base / "widget.log"


def _configure_logger() -> logging.Logger:
    logger = logging.getLogger("gemini-usage-mini-widget")
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


def pythonw_command() -> str:
    return pythonw_command_for(Path(__file__).resolve())


def autostart_enabled() -> bool:
    return autostart_enabled_for(RUN_VALUE, Path(__file__).resolve())


def set_autostart(enabled: bool) -> None:
    set_autostart_for(RUN_VALUE, Path(__file__).resolve(), enabled)


class UsageWidget(BaseUsageWidget):
    APP_TITLE = "Gemini Usage"
    RUN_VALUE = RUN_VALUE

    def __init__(self) -> None:
        self._init_base(script_path=Path(__file__).resolve())
        self.last_error = ""
        self.auth_required = False
        self._topmost_diagnostic_state = None
        self._build()
        self._finalize_init()

    def _on_font_load_error(self, path: str) -> None:
        LOGGER.warning("Could not load bundled font: %s", path)

    def _on_settings_save_error(self, exc: OSError) -> None:
        LOGGER.warning("Could not save widget settings: %s", exc)

    def _on_mouse_hook_error(self, exc: Exception) -> None:
        LOGGER.error("Mouse hook failed: %s", exc)

    def _log_topmost_action(self, message: str, *args: object) -> None:
        LOGGER.info(message, *args)

    def _load_widget_settings(self):
        return load_settings()

    def _save_widget_settings(self, settings) -> None:
        save_settings(settings)

    def _after_window_ready(self) -> None:
        self._check_topmost_diagnostics()

    def _build(self) -> None:
        logo_path = Path(__file__).resolve().parent / "assets" / "gemini-logo.png"
        self._build_header_shell(
            logo_path=logo_path,
            on_logo_error=lambda: LOGGER.warning("Could not load Gemini logo: %s", logo_path),
            five_fg=FIVE_HOUR_TEXT,
            week_fg=WEEK_TEXT,
        )

        self.menu = PopupMenu(self.root, self._menu_closed, reserve_indicator_space=True)
        self.menu.add_command(label="지금 새로고침", command=self.refresh)
        self.menu.add_info(label="연결 상태: 확인 전")
        self.connection_status_index = self.menu.index("end")
        self.menu.add_separator()
        self.menu.add_info(label="계정: —")
        self.account_index = self.menu.index("end")
        self.menu.add_info(label="요금제: —", right_text="")
        self.plan_index = self.menu.index("end")
        self.menu.add_info(label="Claude·GPT 5시간: —", right_text="")
        self.claude_5h_index = self.menu.index("end")
        self.menu.add_info(label="Claude·GPT 주간: —", right_text="")
        self.claude_7d_index = self.menu.index("end")
        self.menu.add_separator()
        self._add_common_preference_menu_items(self.menu)
        self.menu.add_separator()
        self.menu.add_command(label="Antigravity 열기", command=self._open_antigravity)
        self.menu.add_command(label="종료", command=self._close)
        self._fit_content_width()

    def _check_topmost_diagnostics(self) -> None:
        """Record native state changes without changing window order or focus."""
        if self._closing:
            return
        try:
            api = self._input_api
            hwnd = api.GetAncestor(self.root.winfo_id(), 2)
            ctypes.set_last_error(0)
            style = api.GetWindowLongW(hwnd, -20)
            if not style and ctypes.get_last_error():
                raise ctypes.WinError(ctypes.get_last_error())
            state = (
                hwnd,
                style,
                self.always_on_top.get(),
                bool(self.root.attributes("-topmost")),
            )
            if state != self._topmost_diagnostic_state:
                LOGGER.info(
                    "Topmost state: previous=%s hwnd=%s input_hwnd=%s "
                    "exstyle=%#x native_topmost=%s option=%s tk_topmost=%s "
                    "foreground=%s window_above=%s hovered=%s menu_open=%s",
                    self._topmost_diagnostic_state,
                    hwnd,
                    self._input_hwnd,
                    style,
                    bool(style & 0x8),
                    state[2],
                    state[3],
                    api.GetForegroundWindow(),
                    api.GetWindow(hwnd, 3),
                    self._hovered,
                    self._menu_open,
                )
                self._topmost_diagnostic_state = state
        except (OSError, tk.TclError):
            LOGGER.exception("Could not read topmost diagnostic state")
        finally:
            self.root.after(1000, self._check_topmost_diagnostics)

    def _open_antigravity(self) -> None:
        try:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            executables = find_antigravity_executables()
            if not executables:
                raise OSError("Antigravity executable not found")
            subprocess.Popen([executables[0]], creationflags=creation_flags)
        except OSError:
            webbrowser.open("https://gemini.google.com/")

    def refresh(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.fetch_generation += 1
        generation = self.fetch_generation
        self.menu.entryconfigure(0, state="disabled")
        self._update_connection_status()
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
            except AuthRequiredError as exc:
                self.results.put((generation, "error", exc))
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
            self.auth_required = False
            self.failure_count = 0
            self.snapshot = payload
            self._update_connection_status()
            self._render(payload)
        else:
            auth_required = isinstance(payload, AuthRequiredError)
            message = str(payload) if payload else "알 수 없는 오류"
            self._handle_connection_error(message, auth_required=auth_required)
        self.root.after(500, self._drain_results)

    def _handle_connection_error(self, message: str, auth_required: bool = False) -> None:
        self.last_error = message
        self.auth_required = auth_required
        self._update_connection_status()
        self.failure_count += 1
        LOGGER.warning("Refresh failed (%s): %s", self.failure_count, message)
        self._render_connection_issue()
        if self.recovery_after_id is None:
            delay = recovery_delay(self.failure_count)
            self.recovery_after_id = self.root.after(delay * 1000, self._recovery_refresh)

    def _render_connection_issue(self) -> None:
        if not self.last_error:
            return
        if self.snapshot is not None and self.failure_count < 3 and not self.auth_required:
            self.reset_compact.configure(text="retry", fg=YELLOW)
        else:
            self.reset_compact.configure(text="offline", fg=MUTED)
        self._fit_content_width()

    def _update_connection_status(self) -> None:
        """Display cached query state without starting a connection check."""
        if self.refreshing:
            status = "조회 중"
        elif self.auth_required:
            status = "로그인 필요"
        elif self.last_error:
            status = "재연결 중"
        elif self.snapshot is not None:
            status = "정상"
        else:
            status = "확인 전"
        self.menu.entryconfigure(self.connection_status_index, label=f"연결 상태: {status}")

    def _render(self, snapshot: UsageSnapshot) -> None:
        five = snapshot.five_hour
        if five:
            self.five_label.configure(text=f"{five.used_percent}%", fg=FIVE_HOUR_TEXT)
            self.reset_compact.configure(text=reset_short(five.resets_at), fg=MUTED)
            self.root.after_idle(
                lambda: self._draw_segments(self.five_meter, five.used_percent)
            )
        else:
            self.five_label.configure(text="—%", fg=FIVE_HOUR_TEXT)
            self.reset_compact.configure(text="—", fg=MUTED)
            self._draw_segments(self.five_meter, None)

        week = snapshot.seven_day
        self.week_reset.configure(
            text=reset_short(week.resets_at if week else None, weekly=True)
        )
        if week:
            self.week_label.configure(text=f"{week.used_percent}%", fg=WEEK_TEXT)
            self.root.after_idle(
                lambda: self._draw_segments(self.week_meter, week.used_percent)
            )
        else:
            self.week_label.configure(text="—%", fg=WEEK_TEXT)
            self._draw_segments(self.week_meter, None)

        account_text = snapshot.account_email or "—"
        self.menu.entryconfigure(self.account_index, label=f"계정: {account_text}")
        credit_text = f"크레딧 {snapshot.credit_balance}" if snapshot.credit_balance is not None else ""
        self.menu.entryconfigure(
            self.plan_index,
            label=f"요금제: {snapshot.plan}",
            right_text=credit_text,
        )

        c5 = snapshot.claude_gpt_five_hour
        if c5 is not None:
            self.menu.entryconfigure(
                self.claude_5h_index,
                label=f"Claude·GPT 5시간: {c5.used_percent}%",
                right_text=reset_short(c5.resets_at) if c5.resets_at is not None else "",
            )
        else:
            self.menu.entryconfigure(
                self.claude_5h_index,
                label="Claude·GPT 5시간: —",
                right_text="",
            )

        c7 = snapshot.claude_gpt_seven_day
        if c7 is not None:
            self.menu.entryconfigure(
                self.claude_7d_index,
                label=f"Claude·GPT 주간: {c7.used_percent}%",
                right_text=reset_short(c7.resets_at, weekly=True) if c7.resets_at is not None else "",
            )
        else:
            self.menu.entryconfigure(
                self.claude_7d_index,
                label="Claude·GPT 주간: —",
                right_text="",
            )
        self._fit_content_width()


def main() -> None:
    run_single_instance(
        r"Local\GeminiUsageMiniWidget",
        "Gemini 사용량 위젯",
        lambda: UsageWidget().run(),
    )


if __name__ == "__main__":
    main()
