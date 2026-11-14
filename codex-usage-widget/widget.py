"""Small always-on-top Windows widget for ChatGPT Work/Codex usage."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from tkinter import messagebox

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
from codex_usage import UsageSnapshot, consume_reset_credit, find_codex_executables, query_usage
from widget_settings import (
    PendingReset,
    load_pending_reset,
    load_settings,
    save_pending_reset,
    save_settings,
)

try:
    from diagnostics.error_log import record_error
except ModuleNotFoundError as exc:
    if exc.name not in ("diagnostics", "diagnostics.error_log"):
        raise

    def record_error(event: str) -> None:
        pass

try:
    from diagnostics.topmost import TopmostDiagnostics
except ModuleNotFoundError as exc:
    if exc.name not in ("diagnostics", "diagnostics.topmost"):
        raise
    TopmostDiagnostics = None


RUN_VALUE = "ChatGPTUsageWidget"


def pythonw_command() -> str:
    return pythonw_command_for(Path(__file__).resolve())


def autostart_enabled() -> bool:
    return autostart_enabled_for(RUN_VALUE, Path(__file__).resolve())


def set_autostart(enabled: bool) -> None:
    set_autostart_for(RUN_VALUE, Path(__file__).resolve(), enabled)


class UsageWidget(BaseUsageWidget):
    APP_TITLE = "ChatGPT Usage"
    RUN_VALUE = RUN_VALUE

    def __init__(self) -> None:
        self._diagnostics = None
        self._init_base(script_path=Path(__file__).resolve())
        self.resetting = False
        self.reset_state_unreadable = False
        try:
            self.pending_reset = load_pending_reset()
        except (OSError, ValueError, RecursionError):
            self.pending_reset = None
            self.reset_state_unreadable = True
            record_error("pending_reset_load_failed")
        self.reset_results: queue.Queue[tuple[str, object]] = queue.Queue()
        self.connection_failed = False
        self._build()
        self._finalize_init()

    def _on_font_load_error(self, _path: str) -> None:
        record_error("bundled_font_load_failed")

    def _on_settings_save_error(self, _exc: OSError) -> None:
        record_error("settings_save_failed")

    def _on_mouse_hook_error(self, _exc: Exception) -> None:
        record_error("mouse_hook_failed")

    def _log_topmost_action(self, message: str, *args: object) -> None:
        diagnostics = getattr(self, "_diagnostics", None)
        if diagnostics is not None:
            diagnostics.log.info(message, *args)

    def _load_widget_settings(self):
        return load_settings()

    def _save_widget_settings(self, settings) -> None:
        save_settings(settings)

    def _after_window_ready(self) -> None:
        if TopmostDiagnostics is not None:
            self._diagnostics = TopmostDiagnostics(self)
            self._diagnostics.check()

    def _start_extra_timers(self) -> None:
        self.root.after(500, self._drain_reset_results)

    def _build(self) -> None:
        logo_path = Path(__file__).resolve().parent / "assets" / "codex-logo.png"
        self._build_header_shell(
            logo_path=logo_path,
            on_logo_error=lambda: record_error("codex_logo_load_failed"),
            five_fg=TEXT,
            week_fg=TEXT,
        )

        self.menu = PopupMenu(self.root, self._menu_closed)
        self.menu.add_command(label="지금 새로고침", command=self.refresh)
        self.menu.add_info(label="연결 상태: 확인 전")
        self.connection_status_index = self.menu.index("end")
        self.menu.add_separator()
        self.menu.add_info(label="계정: —")
        self.account_index = self.menu.index("end")
        self.menu.add_info(label="요금제: —", right_text="크레딧: —")
        self.plan_index = self.menu.index("end")
        self.menu.add_info(label="사용량 리셋권: —", right_text="")
        self.reset_count_index = self.menu.index("end")
        self.menu.add_command(label="리셋권 사용하기", state="disabled", command=self._use_reset_credit)
        self.reset_use_index = self.menu.index("end")
        self.menu.add_separator()
        self._add_common_preference_menu_items(self.menu)
        self.menu.add_separator()
        self.menu.add_command(label="ChatGPT 데스크톱 열기", command=self._open_chatgpt)
        self.menu.add_command(label="종료", command=self._close)
        self._fit_content_width()

    def _open_chatgpt(self) -> None:
        try:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            executables = find_codex_executables()
            if not executables:
                raise OSError("Codex executable not found")
            subprocess.Popen([executables[0], "app"], creationflags=creation_flags)
        except OSError:
            webbrowser.open("https://chatgpt.com/")

    def refresh(self) -> None:
        if self.refreshing or self.resetting:
            return
        self.refreshing = True
        self.fetch_generation += 1
        generation = self.fetch_generation
        self.menu.entryconfigure(0, state="disabled")
        self._update_connection_status()
        self._update_reset_menu()
        if self.snapshot is None:
            self.reset_compact.configure(text="…", fg=MUTED)
        threading.Thread(target=self._fetch_worker, args=(generation,), daemon=True).start()
        self.root.after(50_000, lambda: self._fetch_watchdog(generation))

    def _fetch_worker(self, generation: int) -> None:
        for delay in (0.0, 2.0):
            if delay:
                time.sleep(delay)
            try:
                self.results.put((generation, "ok", query_usage(timeout=18.0)))
                return
            except Exception:
                continue
        self.results.put((generation, "error", None))

    def _fetch_watchdog(self, generation: int) -> None:
        if not self.refreshing or generation != self.fetch_generation:
            return
        self.fetch_generation += 1  # Ignore a late result from the stuck worker.
        self.refreshing = False
        self.menu.entryconfigure(0, state="normal")
        self._handle_connection_error()

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
            self.connection_failed = False
            self.failure_count = 0
            self.snapshot = payload
            self._update_connection_status()
            self._render(payload)
        else:
            self._handle_connection_error()
        self.root.after(500, self._drain_results)

    def _handle_connection_error(self) -> None:
        self.connection_failed = True
        self._update_connection_status()
        self._update_reset_menu()
        self.failure_count += 1
        record_error("usage_refresh_failed")
        self._render_connection_issue()
        if self.recovery_after_id is None:
            delay = recovery_delay(self.failure_count)
            self.recovery_after_id = self.root.after(delay * 1000, self._recovery_refresh)

    def _render_connection_issue(self) -> None:
        if not self.connection_failed:
            return
        if self.snapshot is not None and self.failure_count < 3:
            self.reset_compact.configure(text="retry", fg=YELLOW)
        else:
            self.reset_compact.configure(text="offline", fg=MUTED)
        self._fit_content_width()

    def _update_connection_status(self) -> None:
        """Display cached query state without starting a connection check."""
        if self.refreshing:
            status = "조회 중"
        elif self.connection_failed:
            status = "재연결 중"
        elif self.snapshot is not None:
            status = "정상"
        else:
            status = "확인 전"
        self.menu.entryconfigure(self.connection_status_index, label=f"연결 상태: {status}")

    def _update_reset_menu(self) -> None:
        count = self.snapshot.reset_credit_count if self.snapshot else None
        expires_at = self.snapshot.reset_credit_expires_at if self.snapshot else None
        expiry = ""
        if count:
            try:
                expiry = (
                    f"~{datetime.fromtimestamp(expires_at):%m.%d.}"
                    if expires_at is not None
                    else "만료일 조회 불가"
                )
            except (OverflowError, OSError, ValueError):
                expiry = "만료일 조회 불가"
        self.menu.entryconfigure(
            self.reset_count_index,
            label=f"사용량 리셋권: {count}개" if count is not None else "사용량 리셋권: 조회 불가",
            right_text=expiry,
        )
        retry = self.pending_reset is not None
        can_start = (
            not self.connection_failed
            and self.snapshot is not None
            and self.snapshot.reset_credit_id is not None
            and self.snapshot.executable is not None
        )
        enabled = (
            not self.resetting
            and not self.refreshing
            and not self.reset_state_unreadable
            and (retry or can_start)
        )
        label = "리셋권 사용 결과 재확인" if retry else "리셋권 사용하기"
        if self.resetting:
            label = "리셋권 사용 처리 중…"
        elif self.reset_state_unreadable:
            label = "리셋권 사용 불가 (요청 기록 확인 필요)"
        elif count and not retry and not can_start and not self.connection_failed:
            label = "리셋권 사용 불가 (대상 확인 필요)"
        self.menu.entryconfigure(
            self.reset_use_index,
            label=label,
            state="normal" if enabled else "disabled",
        )

    def _use_reset_credit(self) -> None:
        if self.resetting or self.refreshing or self.reset_state_unreadable:
            return
        request = self.pending_reset
        snapshot = self.snapshot
        if request is None:
            if (
                self.connection_failed
                or snapshot is None
                or not snapshot.reset_credit_id
                or not snapshot.executable
            ):
                return
            request = PendingReset(str(uuid.uuid4()), snapshot.executable, snapshot.reset_credit_id)
        retry = self.pending_reset is not None
        prompt = (
            "이전 리셋권 사용 요청의 결과를 같은 요청 키로 다시 확인합니다.\n계속하시겠습니까?"
            if retry
            else "사용량 리셋권 1개를 사용하시겠습니까? 만료가 있는 권리부터 사용합니다.\n\n"
            "초기화 대상 기간은 서버가 결정하며, 주간 사용량만 지정할 수는 없습니다."
        )
        if not messagebox.askyesno("사용량 리셋권", prompt, parent=self.root):
            return
        # Tk modal dialogs keep timers running, including scheduled refreshes.
        if self.resetting or self.refreshing:
            messagebox.showinfo("사용량 리셋권", "조회가 끝난 뒤 다시 선택해 주세요.", parent=self.root)
            return
        if not retry and (
            self.connection_failed or self.snapshot is not snapshot or self.pending_reset is not None
        ):
            return
        if not retry:
            try:
                save_pending_reset(request)
            except OSError:
                record_error("pending_reset_save_failed")
                messagebox.showwarning("사용량 리셋권", "요청 기록을 저장하지 못했습니다.", parent=self.root)
                return
            self.pending_reset = request
        elif self.pending_reset != request:
            return
        self.resetting = True
        self.menu.entryconfigure(0, state="disabled")
        self._update_reset_menu()
        threading.Thread(target=self._reset_worker, args=(request,), daemon=True).start()

    def _reset_worker(self, request: PendingReset) -> None:
        try:
            self.reset_results.put(
                ("ok", consume_reset_credit(request.key, request.executable, request.credit_id))
            )
        except Exception:
            record_error("reset_request_failed")
            self.reset_results.put(("error", ""))

    def _drain_reset_results(self) -> None:
        try:
            kind, payload = self.reset_results.get_nowait()
        except queue.Empty:
            self.root.after(500, self._drain_reset_results)
            return
        self.resetting = False
        self.menu.entryconfigure(0, state="normal")
        if kind == "ok":
            try:
                save_pending_reset(None)
            except OSError:
                record_error("pending_reset_clear_failed")
            else:
                self.pending_reset = None
            messages = {
                "reset": "리셋권 1개를 사용했습니다. 사용량과 남은 개수를 다시 조회합니다.",
                "alreadyRedeemed": "이 요청은 이미 처리되었습니다. 사용량과 남은 개수를 다시 조회합니다.",
                "nothingToReset": "현재 초기화할 수 있는 사용량 기간이 없습니다.",
                "noCredit": "사용 가능한 리셋권이 없습니다.",
            }
            self.refresh()
            messagebox.showinfo("사용량 리셋권", messages[str(payload)], parent=self.root)
        else:
            self._update_reset_menu()
            messagebox.showwarning(
                "사용량 리셋권",
                "리셋권 사용 결과를 확인하지 못했습니다.\n\n"
                "‘리셋권 사용 결과 재확인’에서 같은 리셋권과 요청 키로 확인할 수 있습니다.",
                parent=self.root,
            )
        self.root.after(500, self._drain_reset_results)

    def _render(self, snapshot: UsageSnapshot) -> None:
        primary = snapshot.primary
        if primary:
            color = usage_color(primary.used_percent)
            self.five_title.configure(fg=color)
            self.five_label.configure(text=f"{primary.used_percent}%", fg=color)
            self.reset_compact.configure(text=reset_short(primary.resets_at), fg=MUTED)
            self.root.after_idle(
                lambda: self._draw_segments(self.five_meter, primary.used_percent)
            )
        else:
            self.five_title.configure(fg=TEXT)
            self.five_label.configure(text="—%", fg=TEXT)
            self.reset_compact.configure(text="—", fg=MUTED)
            self._draw_segments(self.five_meter, None)

        secondary = snapshot.secondary
        self.week_reset.configure(
            text=reset_short(secondary.resets_at if secondary else None, weekly=True)
        )
        if secondary:
            secondary_color = usage_color(secondary.used_percent)
            self.week_title.configure(fg=secondary_color)
            self.week_label.configure(text=f"{secondary.used_percent}%", fg=secondary_color)
            self.root.after_idle(
                lambda: self._draw_segments(self.week_meter, secondary.used_percent)
            )
        else:
            self.week_title.configure(fg=TEXT)
            self.week_label.configure(text="—%", fg=TEXT)
            self._draw_segments(self.week_meter, None)

        self.menu.entryconfigure(self.account_index, label=f"계정: {snapshot.account or '—'}")
        plan_text = snapshot.plan if snapshot.plan.lower() != "unknown" else "—"
        if snapshot.unlimited_credits:
            credit_text = "무제한"
        elif snapshot.credit_balance is not None:
            credit_text = f"{snapshot.credit_balance.quantize(Decimal('0.01'))}"
        else:
            credit_text = "—"
        self.menu.entryconfigure(
            self.plan_index,
            label=f"요금제: {plan_text}",
            right_text=f"크레딧: {credit_text}",
        )
        self._update_reset_menu()
        self._fit_content_width()


def main() -> None:
    run_single_instance(
        r"Local\CodexUsageMiniWidget",
        "Codex 사용량 위젯",
        lambda: UsageWidget().run(),
    )


if __name__ == "__main__":
    main()
