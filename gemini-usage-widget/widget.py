"""Small always-on-top Windows widget for Gemini / Antigravity usage."""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path

_WIDGET_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _WIDGET_DIR.parent
for _path in (str(_REPO_ROOT), str(_WIDGET_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from common.widget_base import (
    MUTED,
    PopupMenu,
    BaseUsageWidget,
    reset_short,
    run_single_instance,
)
from gemini_usage import AuthRequiredError, UsageSnapshot, find_antigravity_executables, query_usage
from widget_settings import load_settings, save_settings


FIVE_HOUR_TEXT = "#A5E9C1"
WEEK_TEXT = "#D2B8FF"
RUN_VALUE = "GeminiUsageWidget"


class UsageWidget(BaseUsageWidget):
    APP_TITLE = "Gemini Usage"
    RUN_VALUE = RUN_VALUE

    def __init__(self) -> None:
        self._init_base(script_path=Path(__file__).resolve())
        self._build()
        self._finalize_init()

    def _load_widget_settings(self):
        return load_settings()

    def _save_widget_settings(self, settings) -> None:
        save_settings(settings)

    def _query_usage(self, timeout: float = 18.0) -> UsageSnapshot:
        return query_usage(timeout=timeout)

    def _is_non_retryable_fetch_error(self, exc: Exception) -> bool:
        return isinstance(exc, AuthRequiredError)

    def _build(self) -> None:
        logo_path = Path(__file__).resolve().parent / "assets" / "gemini-logo.png"
        self._build_header_shell(
            logo_path=logo_path,
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

    def _open_antigravity(self) -> None:
        try:
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            executables = find_antigravity_executables()
            if not executables:
                raise OSError("Antigravity executable not found")
            subprocess.Popen([executables[0]], creationflags=creation_flags)
        except OSError:
            webbrowser.open("https://gemini.google.com/")

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
