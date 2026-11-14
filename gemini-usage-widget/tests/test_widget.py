import io
import json
import os
import sys
import tempfile
import time
import tkinter as tk
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


_WIDGET_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _WIDGET_DIR.parent
for _path in (str(_REPO_ROOT), str(_WIDGET_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import gemini_usage
from common.widget_base import (
    FONT_FAMILY,
    MUTED,
    TRACK,
    YELLOW,
    enable_dpi_awareness,
    load_bundled_fonts,
    usage_color,
)
from gemini_usage import (
    LimitWindow,
    UsageError,
    UsageSnapshot,
    _LanguageServerProcess,
    _call_language_server_rpc,
    _discover_daemon_candidates,
    _parse_reset_time,
    find_antigravity_executables,
    parse_command_line_ports,
    parse_language_server_command_line,
    parse_user_quota,
    query_usage,
)
from widget import (
    FIVE_HOUR_TEXT,
    WEEK_TEXT,
    UsageWidget,
)


class ParseUserQuotaTests(unittest.TestCase):
    def test_parses_retrieve_user_quota_summary_and_user_status(self):
        snapshot = parse_user_quota(
            {
                "userStatus": {
                    "email": "user@example.com",
                    "userSettings": {"useAiCredits": True},
                    "userTier": {
                        "id": "g1-pro-tier",
                        "name": "Google AI Pro",
                        "availableCredits": [{"creditType": "GOOGLE_ONE_AI", "creditAmount": "250"}],
                    },
                }
            },
            summary_result={
                "groups": [
                    {
                        "groupId": "gemini-models",
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-5h",
                                "displayName": "Gemini · 5-hour",
                                "remaining": {"remainingFraction": 0.85},
                                "resetTime": "2026-11-14T04:30:00Z",
                            },
                            {
                                "bucketId": "gemini-weekly",
                                "displayName": "Gemini · Weekly",
                                "remaining": {"remainingFraction": 0.72},
                                "resetTime": "2026-11-20T09:00:00Z",
                            },
                        ],
                    },
                    {
                        "groupId": "third-party-models",
                        "displayName": "Claude & GPT (shared)",
                        "buckets": [
                            {
                                "bucketId": "3p-5h",
                                "displayName": "Claude & GPT (shared) · 5-hour",
                                "remaining": {"remainingFraction": 0.90},
                                "resetTime": "2026-11-14T03:00:00Z",
                            },
                            {
                                "bucketId": "3p-weekly",
                                "displayName": "Claude & GPT (shared) · Weekly",
                                "remaining": {"remainingFraction": 0.60},
                                "resetTime": "2026-11-20T09:00:00Z",
                            },
                        ],
                    },
                ]
            },
        )
        self.assertEqual(snapshot.plan, "Google AI Pro")
        self.assertEqual(snapshot.account_email, "user@example.com")
        self.assertEqual(snapshot.credit_balance, 250)
        self.assertFalse(hasattr(snapshot, "overage_enabled"))
        self.assertIsNotNone(snapshot.five_hour)
        self.assertEqual(snapshot.five_hour.used_percent, 15)
        self.assertIsNotNone(snapshot.seven_day)
        self.assertEqual(snapshot.seven_day.used_percent, 28)
        self.assertIsNotNone(snapshot.claude_gpt_five_hour)
        self.assertEqual(snapshot.claude_gpt_five_hour.used_percent, 10)
        self.assertIsNotNone(snapshot.claude_gpt_seven_day)
        self.assertEqual(snapshot.claude_gpt_seven_day.used_percent, 40)

    def test_parses_client_model_configs_with_weekly_and_proto3_omission(self):
        snapshot = parse_user_quota(
            {
                "userStatus": {
                    "email": "user@example.com",
                    "useG1Credits": False,
                    "planStatus": {
                        "planInfo": {"planName": "Ultra"},
                        "availablePromptCredits": 500,
                    },
                    "cascadeModelConfigData": {
                        "clientModelConfigs": [
                            {
                                "label": "Gemini 3.1 Pro (High)",
                                "quotaInfo": {
                                    "remainingFraction": 0.80,
                                    "resetTime": "2026-11-14T04:00:00Z",
                                },
                                "weeklyQuotaInfo": {
                                    "remainingFraction": 0.65,
                                    "resetTime": "2026-11-20T00:00:00Z",
                                },
                            },
                            {
                                "label": "Claude Sonnet 4.5",
                                "quotaInfo": {
                                    "resetTime": "2026-11-14T04:00:00Z",
                                },
                                "weeklyQuotaInfo": {
                                    "remainingFraction": 0.50,
                                    "resetTime": "2026-11-20T00:00:00Z",
                                },
                            },
                        ]
                    },
                }
            }
        )
        self.assertEqual(snapshot.plan, "Ultra")
        self.assertEqual(snapshot.credit_balance, 500)
        self.assertEqual(snapshot.five_hour.used_percent, 20)
        self.assertEqual(snapshot.seven_day.used_percent, 35)
        self.assertEqual(snapshot.claude_gpt_five_hour.used_percent, 100)
        self.assertEqual(snapshot.claude_gpt_seven_day.used_percent, 50)

    def test_preserves_zero_credit_balance(self):
        snapshot = parse_user_quota(
            {
                "userStatus": {
                    "userTier": {
                        "name": "Google AI Pro",
                        "availableCredits": [{"creditType": "GOOGLE_ONE_AI", "creditAmount": "0"}],
                    },
                    "buckets": [
                        {"modelId": "gemini-2.5-flash", "remainingFraction": 0.5},
                    ],
                }
            }
        )
        self.assertEqual(snapshot.credit_balance, 0)

    def test_parses_language_server_command_line(self):
        cmd = (
            r'"C:\Users\ONEMO\AppData\Local\Programs\Antigravity\resources\app\extensions'
            r'\antigravity\bin\language_server_windows_x64.exe" '
            r"--enable_lsp --csrf_token 1234-abcd-5678 --extension_server_port 54321"
        )
        self.assertEqual(
            parse_language_server_command_line(cmd),
            ("1234-abcd-5678", 54321),
        )
        self.assertEqual(
            parse_language_server_command_line('--csrf_token="quoted-token" --extension_server_port=41234'),
            ("quoted-token", 41234),
        )

    def test_clamps_percentages_and_rejects_missing_buckets(self):
        snapshot = parse_user_quota(
            {
                "buckets": [
                    {"modelId": "gemini-2.5-flash-lite", "remainingFraction": 1.4},
                    {"modelId": "claude-3-7-sonnet", "remainingFraction": -0.2},
                ]
            },
        )
        self.assertEqual(snapshot.five_hour.used_percent, 0)
        self.assertEqual(snapshot.claude_gpt_five_hour.used_percent, 100)
        with self.assertRaises(UsageError):
            parse_user_quota({})
        with self.assertRaises(UsageError):
            parse_user_quota({"buckets": [{"modelId": "text-embedding-004", "remainingFraction": 0.8}]})

    def test_parses_retrieve_user_quota_summary_response_envelope_and_window_field(self):
        summary_payload = {
            "response": {
                "groups": [
                    {
                        "bucketId": "gemini-models",
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "bucketId": "gemini-5h",
                                "window": "5h",
                                "remainingFraction": 0.85,
                                "resetTime": "2026-11-14T04:30:00.123456789Z",
                            },
                            {
                                "bucketId": "gemini-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.72,
                                "resetTime": "2026-11-20T09:00:00.123456789Z",
                            },
                        ],
                    },
                    {
                        "bucketId": "3p-models",
                        "displayName": "Claude and GPT models",
                        "buckets": [
                            {
                                "bucketId": "3p-5h",
                                "window": "5h",
                                "remainingFraction": 1,
                                "resetTime": "2026-11-14T03:00:00Z",
                            },
                            {
                                "bucketId": "3p-weekly",
                                "window": "weekly",
                                "remainingFraction": 0.45,
                                "resetTime": "2026-11-20T09:00:00Z",
                            },
                        ],
                    },
                ]
            }
        }
        snapshot = parse_user_quota({}, summary_result=summary_payload)
        self.assertFalse(hasattr(snapshot, "overage_enabled"))
        self.assertIsNotNone(snapshot.five_hour)
        self.assertEqual(snapshot.five_hour.used_percent, 15)
        self.assertIsNotNone(snapshot.five_hour.resets_at)
        self.assertIsNotNone(snapshot.seven_day)
        self.assertEqual(snapshot.seven_day.used_percent, 28)
        self.assertIsNotNone(snapshot.seven_day.resets_at)
        self.assertIsNotNone(snapshot.claude_gpt_five_hour)
        self.assertEqual(snapshot.claude_gpt_five_hour.used_percent, 0)
        self.assertIsNotNone(snapshot.claude_gpt_five_hour.resets_at)
        self.assertIsNotNone(snapshot.claude_gpt_seven_day)
        self.assertEqual(snapshot.claude_gpt_seven_day.used_percent, 55)
        self.assertIsNotNone(snapshot.claude_gpt_seven_day.resets_at)

    def test_parse_reset_time_supports_iso_nanos_epoch_strings_ms_and_proto_timestamp(self):
        self.assertIsInstance(_parse_reset_time("2026-11-20T09:00:00.123456789Z"), int)
        self.assertEqual(_parse_reset_time("1763629200"), 1763629200)
        self.assertEqual(_parse_reset_time(1763629200000), 1763629200)
        self.assertEqual(_parse_reset_time({"seconds": "1763629200", "nanos": 0}), 1763629200)

    def test_rpc_dispatches_empty_body_to_summary_and_metadata_to_status(self):
        captured: list[tuple[str, bytes]] = []

        class _FakeResponse:
            def __init__(self, payload: dict):
                self._data = json.dumps(payload).encode("utf-8")

            def read(self) -> bytes:
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        class _FakeOpener:
            def open(self, request, timeout=None):
                captured.append((request.full_url, request.data))
                if request.full_url.endswith("/RetrieveUserQuotaSummary") and len(captured) == 1:
                    return _FakeResponse({"response": {"groups": []}})
                if len(captured) == 2:
                    return _FakeResponse({"userStatus": {}})
                # Third call simulates HTTP 400 fallback retry
                if request.data != b"{}":
                    raise urllib.error.HTTPError(
                        request.full_url, 400, "Bad Request", {}, io.BytesIO(b"")
                    )
                return _FakeResponse({"response": {"groups": []}})

        with mock.patch("gemini_usage.urllib.request.build_opener", return_value=_FakeOpener()):
            _call_language_server_rpc(
                "https", 50001, "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary", "tok", 2.0
            )
            _call_language_server_rpc(
                "https", 50001, "/exa.language_server_pb.LanguageServerService/GetUserStatus", "tok", 2.0
            )
            _call_language_server_rpc(
                "https", 50001, "/exa.language_server_pb.LanguageServerService/GetUserStatus", "tok", 2.0
            )

        self.assertEqual(captured[0][1], b"{}")
        self.assertIn(b'"metadata"', captured[1][1])
        self.assertIn(b'"metadata"', captured[2][1])
        self.assertEqual(captured[3][1], b"{}")

    def test_query_usage_continues_past_5h_only_process_and_merges_weekly_summary(self):
        with gemini_usage._CACHE_LOCK:
            gemini_usage._ENDPOINT_CACHE.clear()
        try:
            now = int(time.time())
            reset_5h = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3600))
            reset_weekly = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 5 * 86400))

            proc_ide = _LanguageServerProcess(
                pid=101,
                name="language_server_windows_x64.exe",
                command_line="",
                csrf_token="tok-ide",
                server_ports=(51001,),
                priority=1,
            )
            proc_hub = _LanguageServerProcess(
                pid=202,
                name="agy.exe",
                command_line="--hub --hub-port=51002 --csrf_token=tok-hub",
                csrf_token="tok-hub",
                server_ports=(51002,),
                priority=0,
            )

            def fake_rpc(scheme: str, port: int, path: str, csrf_token: str, timeout: float):
                if port == 51001:
                    if path.endswith("/RetrieveUserQuotaSummary"):
                        raise urllib.error.HTTPError(path, 404, "Not Found", {}, io.BytesIO(b""))
                    return {
                        "userStatus": {
                            "email": "merged@example.com",
                            "userTier": {
                                "name": "Google AI Pro",
                                "availableCredits": [{"creditAmount": "320"}],
                            },
                            "cascadeModelConfigData": {
                                "clientModelConfigs": [
                                    {
                                        "label": "Gemini 3.1 Pro",
                                        "quotaInfo": {
                                            "remainingFraction": 0.9,
                                            "resetTime": reset_5h,
                                        },
                                    },
                                    {
                                        "label": "Claude Sonnet 4.5",
                                        "quotaInfo": {
                                            "remainingFraction": 1.0,
                                            "resetTime": reset_5h,
                                        },
                                    },
                                ]
                            },
                        }
                    }
                if port == 51002:
                    if path.endswith("/GetUserStatus"):
                        raise urllib.error.HTTPError(path, 404, "Not Found", {}, io.BytesIO(b""))
                    return {
                        "response": {
                            "groups": [
                                {
                                    "bucketId": "gemini-models",
                                    "buckets": [
                                        {
                                            "bucketId": "gemini-5h",
                                            "window": "5h",
                                            "remainingFraction": 0.85,
                                            "resetTime": reset_5h,
                                        },
                                        {
                                            "bucketId": "gemini-weekly",
                                            "window": "weekly",
                                            "remainingFraction": 0.7,
                                            "resetTime": reset_weekly,
                                        },
                                    ],
                                },
                                {
                                    "bucketId": "3p-models",
                                    "buckets": [
                                        {
                                            "bucketId": "3p-5h",
                                            "window": "5h",
                                            "remainingFraction": 0.95,
                                            "resetTime": reset_5h,
                                        },
                                        {
                                            "bucketId": "3p-weekly",
                                            "window": "weekly",
                                            "remainingFraction": 0.5,
                                            "resetTime": reset_weekly,
                                        },
                                    ],
                                },
                            ]
                        }
                    }
                raise AssertionError(f"Unexpected port {port}")

            with mock.patch("gemini_usage._discover_language_server_processes", return_value=[proc_ide, proc_hub]):
                with mock.patch("gemini_usage._candidate_ports_for_process", side_effect=lambda p: list(p.server_ports)):
                    with mock.patch("gemini_usage._call_language_server_rpc", side_effect=fake_rpc):
                        snapshot = query_usage(timeout=5.0)

            self.assertEqual(snapshot.account_email, "merged@example.com")
            self.assertEqual(snapshot.plan, "Google AI Pro")
            self.assertEqual(snapshot.credit_balance, 320)
            self.assertEqual(snapshot.five_hour.used_percent, 15)
            self.assertEqual(snapshot.seven_day.used_percent, 30)
            self.assertIsNotNone(snapshot.seven_day.resets_at)
            self.assertEqual(snapshot.claude_gpt_five_hour.used_percent, 5)
            self.assertEqual(snapshot.claude_gpt_seven_day.used_percent, 50)
            self.assertIsNotNone(snapshot.claude_gpt_seven_day.resets_at)
        finally:
            with gemini_usage._CACHE_LOCK:
                gemini_usage._ENDPOINT_CACHE.clear()

    def test_daemon_file_and_command_line_port_discovery(self):
        cmd = (
            'agy.exe --hub --hub-port=55123 --https_server_port 55124 '
            '--extension_server_port=55125 --csrf-token "abc-123"'
        )
        token, ext = parse_language_server_command_line(cmd)
        server_ports, parsed_ext = parse_command_line_ports(cmd)
        self.assertEqual(token, "abc-123")
        self.assertEqual(ext, 55125)
        self.assertEqual(parsed_ext, 55125)
        self.assertEqual(server_ports, [55123, 55124])

        with tempfile.TemporaryDirectory() as temp:
            daemon_dir = Path(temp)
            (daemon_dir / "ls_1234.json").write_text(
                json.dumps({"pid": 4321, "httpsPort": 60123, "csrfToken": "daemon-tok"}),
                encoding="utf-8",
            )
            found = _discover_daemon_candidates(daemon_dir)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].pid, 4321)
            self.assertEqual(found[0].csrf_token, "daemon-tok")
            self.assertEqual(found[0].server_ports, (60123,))

    def test_finds_antigravity_executable(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "Programs" / "Antigravity" / "Antigravity.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temp}, clear=True):
                with mock.patch("gemini_usage.shutil.which", return_value=None):
                    self.assertEqual(find_antigravity_executables(), [str(executable.resolve())])


class WidgetHelpersTests(unittest.TestCase):
    def test_render_uses_wk_label_distinct_font_colors_and_gradient_meters(self):
        self.assertEqual(FIVE_HOUR_TEXT, "#A5E9C1")
        self.assertEqual(WEEK_TEXT, "#D2B8FF")
        self.assertNotEqual(FIVE_HOUR_TEXT, WEEK_TEXT)

        widget = UsageWidget.__new__(UsageWidget)
        widget.root = mock.Mock()
        widget.root.after_idle.side_effect = lambda callback: callback()
        widget.five_label = mock.Mock()
        widget.reset_compact = mock.Mock()
        widget.five_meter = mock.Mock()
        widget.week_label = mock.Mock()
        widget.week_reset = mock.Mock()
        widget.week_meter = mock.Mock()
        widget.menu = mock.Mock()
        widget.account_index = 1
        widget.plan_index = 2
        widget.claude_5h_index = 3
        widget.claude_7d_index = 4
        widget._fit_content_width = mock.Mock()

        snapshot = UsageSnapshot(
            plan="Google AI Pro",
            account_email="user@example.com",
            five_hour=LimitWindow(40, None),
            seven_day=LimitWindow(80, None),
            claude_gpt_five_hour=None,
            claude_gpt_seven_day=None,
            credit_balance=None,
            fetched_at=1_000_000.0,
        )
        widget._render(snapshot)

        widget.five_label.configure.assert_called_with(text="40%", fg=FIVE_HOUR_TEXT)
        widget.week_label.configure.assert_called_with(text="80%", fg=WEEK_TEXT)
        five_fills = [
            call.kwargs["fill"] for call in widget.five_meter.create_rectangle.call_args_list
        ]
        week_fills = [
            call.kwargs["fill"] for call in widget.week_meter.create_rectangle.call_args_list
        ]
        self.assertEqual(five_fills, [usage_color(40)] * 2 + [TRACK] * 3)
        self.assertEqual(week_fills, [usage_color(80)] * 4 + [TRACK])

        widget.five_meter.reset_mock()
        widget.week_meter.reset_mock()
        widget._render(
            UsageSnapshot(
                plan="Google AI Pro",
                account_email=None,
                five_hour=None,
                seven_day=None,
                claude_gpt_five_hour=None,
                claude_gpt_seven_day=None,
                credit_balance=None,
                fetched_at=1_000_000.0,
            )
        )
        widget.five_label.configure.assert_called_with(text="—%", fg=FIVE_HOUR_TEXT)
        widget.week_label.configure.assert_called_with(text="—%", fg=WEEK_TEXT)
        self.assertEqual(
            [call.kwargs["fill"] for call in widget.five_meter.create_rectangle.call_args_list],
            [TRACK] * 5,
        )
        self.assertEqual(
            [call.kwargs["fill"] for call in widget.week_meter.create_rectangle.call_args_list],
            [TRACK] * 5,
        )

    def test_connection_issue_keeps_retry_and_shows_offline_when_unavailable(self):
        widget = UsageWidget.__new__(UsageWidget)
        widget.reset_compact = mock.Mock()
        widget._fit_content_width = mock.Mock()
        widget.last_error = "connection failed"
        widget.auth_required = False
        widget.snapshot = mock.Mock()

        widget.failure_count = 2
        widget._render_connection_issue()
        widget.reset_compact.configure.assert_called_with(text="retry", fg=YELLOW)

        widget.failure_count = 3
        widget._render_connection_issue()
        widget.reset_compact.configure.assert_called_with(text="offline", fg=MUTED)

        widget.failure_count = 1
        widget.auth_required = True
        widget._render_connection_issue()
        widget.reset_compact.configure.assert_called_with(text="offline", fg=MUTED)

        widget.auth_required = False
        widget.snapshot = None
        widget._render_connection_issue()
        widget.reset_compact.configure.assert_called_with(text="offline", fg=MUTED)

    @unittest.skipUnless(os.name == "nt", "Requires Windows Tk font metrics")
    def test_right_aligned_columns_match_codex_5h_width_and_do_not_clip_labels(self):
        enable_dpi_awareness()
        load_bundled_fonts()
        widget = UsageWidget.__new__(UsageWidget)
        widget.root = tk.Tk()
        widget.root.withdraw()
        try:
            widget.transparency = tk.IntVar(master=widget.root, value=30)
            widget.movement_locked = tk.BooleanVar(master=widget.root, value=False)
            widget.hover_transparency_enabled = tk.BooleanVar(master=widget.root, value=True)
            widget.always_on_top = tk.BooleanVar(master=widget.root, value=True)
            widget.autostart = tk.BooleanVar(master=widget.root, value=False)
            widget._closing = False
            widget._open_antigravity = mock.Mock()
            widget._build()

            status_entry = widget.menu.entries[widget.connection_status_index]
            self.assertEqual(status_entry["kind"], "command")
            self.assertEqual(status_entry["state"], "disabled")

            widget.refreshing = False
            widget.auth_required = True
            widget._update_connection_status()
            self.assertEqual(status_entry["label"], "연결 상태: 로그인 필요")
            self.assertEqual(status_entry["state"], "normal")
            widget.menu.open = True
            widget.menu._invoke(widget.connection_status_index)
            widget._open_antigravity.assert_called_once_with()

            reset_font = tk.font.Font(widget.root, family=FONT_FAMILY, size=9)
            self.assertEqual(
                int(widget.reset_compact.master.cget("width")),
                reset_font.measure("5h 59m"),
            )
            self.assertEqual(int(widget.five_meter.cget("width")), 23)
            self.assertEqual(int(widget.week_meter.cget("width")), 23)

            for title, label, expected_title in (
                (widget.five_title, widget.five_label, "5H"),
                (widget.week_title, widget.week_label, "WK"),
            ):
                self.assertEqual(label.master.pack_info()["padx"], (1, 0))
                self.assertEqual(title.cget("text"), expected_title)
                self.assertEqual(title.cget("anchor"), "w")
                self.assertEqual(int(title.cget("bd")), 0)
                self.assertEqual(int(title.cget("highlightthickness")), 0)
                self.assertEqual(label.cget("anchor"), "e")
                self.assertEqual(int(label.cget("bd")), 0)
                self.assertEqual(int(label.cget("highlightthickness")), 0)
                label.configure(text="100%")
                widget.root.update_idletasks()
                self.assertLessEqual(
                    title.winfo_reqwidth() + label.winfo_reqwidth(),
                    int(label.master.cget("width")),
                )

            for label, sample in (
                (widget.reset_compact, "4h 58m"),
                (widget.week_reset, "6d 23h"),
            ):
                self.assertEqual(int(label.cget("bd")), 0)
                self.assertEqual(int(label.cget("highlightthickness")), 0)
                label.configure(text=sample)
                widget.root.update_idletasks()
                self.assertLessEqual(
                    label.winfo_reqwidth(),
                    int(label.master.cget("width")),
                )
        finally:
            widget.root.destroy()


if __name__ == "__main__":
    unittest.main()
