import ctypes
import io
import json
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
import urllib.error
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gemini_usage
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
    FONT_FAMILY,
    MUTED,
    TRACK,
    WEEK_TEXT,
    YELLOW,
    UsageWidget,
    _MouseHookData,
    _Rect,
    clamp_position,
    enable_dpi_awareness,
    gradient_color,
    load_bundled_fonts,
    recovery_delay,
    reset_short,
    usage_color,
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
    def test_usage_gradient_has_expected_endpoints(self):
        self.assertEqual(gradient_color(0), "#A5E9C1")
        self.assertEqual(gradient_color(1), "#F6AEAE")
        self.assertEqual(gradient_color(-1), "#A5E9C1")
        self.assertEqual(gradient_color(2), "#F6AEAE")

    def test_widget_position_stays_above_taskbar(self):
        area = (0, 0, 1920, 1040)
        self.assertEqual(clamp_position(1700, 1030, 270, 38, area), (1650, 1002))
        self.assertEqual(clamp_position(-20, -10, 270, 38, area), (0, 0))

    def test_recovery_backoff_is_bounded(self):
        self.assertEqual([recovery_delay(value) for value in range(1, 7)], [15, 30, 60, 120, 300, 300])

    def test_reset_short_formats_compact_ranges(self):
        self.assertEqual(reset_short(None), "—")
        with mock.patch("widget.time.time", return_value=1_000_000.0):
            self.assertEqual(reset_short(1_000_000), "now")
            self.assertEqual(reset_short(1_000_000 + 86_400), "24h 0m")
            self.assertEqual(reset_short(1_000_000 + 90_000), "1d 1h")
            self.assertEqual(reset_short(1_000_000 + 3_660), "1h 1m")
            self.assertEqual(reset_short(1_000_000 + 61), "2m")

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

    def test_connection_issue_keeps_retry_and_uses_muted_dash_when_offline(self):
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
        widget.reset_compact.configure.assert_called_with(text="—", fg=MUTED)

        widget.failure_count = 1
        widget.auth_required = True
        widget._render_connection_issue()
        widget.reset_compact.configure.assert_called_with(text="—", fg=MUTED)

        widget.auth_required = False
        widget.snapshot = None
        widget._render_connection_issue()
        widget.reset_compact.configure.assert_called_with(text="—", fg=MUTED)

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
            widget._build()

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


class MouseGestureTests(unittest.TestCase):
    def setUp(self):
        self.widget = UsageWidget.__new__(UsageWidget)
        self.widget.root = mock.Mock()
        self.widget.root.winfo_x.return_value = 100
        self.widget.root.winfo_y.return_value = 100
        self.widget._mouse_events = queue.SimpleQueue()
        self.widget.drag_origin = None
        self.widget._drag_threshold = (4, 4)
        self.widget._show_menu = mock.Mock()
        self.widget._move_widget = mock.Mock()
        self.widget.movement_locked = mock.Mock()
        self.widget.movement_locked.get.return_value = False

    def gesture(self, *events):
        for event in events:
            self.widget._mouse_events.put(event)
        self.widget._drain_mouse_events()

    def test_right_click_with_small_jitter_opens_menu_on_release(self):
        self.gesture(("down", 110, 110), ("move", 112, 111))
        self.widget._show_menu.assert_not_called()
        self.gesture(("up", 112, 111))
        self.widget._show_menu.assert_called_once_with(112, 111)
        self.widget._move_widget.assert_not_called()

    def test_drag_outside_and_back_does_not_open_menu(self):
        self.gesture(("down", 110, 110), ("move", 600, 500), ("up", 110, 110))
        self.widget._show_menu.assert_not_called()
        self.assertEqual(self.widget._move_widget.call_count, 2)
        self.assertIsNone(self.widget.drag_origin)

    def test_release_position_alone_can_identify_drag(self):
        self.gesture(("down", 110, 110), ("up", 210, 210))
        self.widget._show_menu.assert_not_called()
        self.widget._move_widget.assert_called_once_with(210, 210)

    def test_hook_routes_only_right_gesture_started_inside(self):
        widget = self.widget
        widget._input_api = mock.Mock()
        widget._input_api.CallNextHookEx.return_value = 42
        widget._input_hwnd = 1
        widget._right_captured = False
        widget._menu_open = False

        def rectangle(_hwnd, pointer):
            pointer._obj.left, pointer._obj.top = 100, 100
            pointer._obj.right, pointer._obj.bottom = 370, 138
            return 1

        widget._input_api.GetWindowRect.side_effect = rectangle

        def send(message, x=110, y=110, code=0):
            data = _MouseHookData()
            data.pt.x, data.pt.y = x, y
            return widget._mouse_hook(code, message, ctypes.addressof(data))

        for message in (0x201, 0x202, 0x200, 0x20A):
            self.assertEqual(send(message), 42)
        self.assertEqual(send(0x204, 90, 90), 42)
        self.assertEqual(send(0x205), 42)
        self.assertEqual(send(0x204, code=-1), 42)
        self.assertEqual(send(0x204), 1)
        self.assertEqual(send(0x201), 42)
        self.assertEqual(send(0x200, 600, 500), 42)
        self.assertEqual(send(0x205, 600, 500), 1)
        self.assertFalse(widget._right_captured)
        self.assertEqual(widget._mouse_events.qsize(), 3)
        widget._menu_open = True
        widget._dismiss_menu_outside = mock.Mock()
        for message in (0x201, 0x204, 0x207, 0x20B):
            self.assertEqual(send(message), 42)
        self.assertEqual(widget._dismiss_menu_outside.call_count, 4)

    def test_destroy_unhooks_only_for_root(self):
        self.widget._hook_thread = mock.Mock()
        self.widget._stop_mouse_hook = mock.Mock()
        self.widget._release_mouse_hook(mock.Mock(widget=object()))
        self.widget._stop_mouse_hook.assert_not_called()
        self.widget._release_mouse_hook(mock.Mock(widget=self.widget.root))
        self.widget._stop_mouse_hook.assert_called_once_with()

    def test_stop_wakes_hook_message_loop_and_joins_thread(self):
        self.widget._hook_stop = threading.Event()
        self.widget._hook_thread_id = 123
        self.widget._input_api = mock.Mock()
        self.widget._hook_thread = mock.Mock()
        self.widget._stop_mouse_hook()
        self.assertTrue(self.widget._hook_stop.is_set())
        self.widget._input_api.PostThreadMessageW.assert_called_once_with(123, 0x12, 0, 0)
        self.widget._hook_thread.join.assert_called_once_with(timeout=2)

    @unittest.skipUnless(os.name == "nt", "Requires native Windows mouse hooks")
    def test_native_hook_runs_and_stops_without_tk_message_pump(self):
        widget = UsageWidget.__new__(UsageWidget)
        enable_dpi_awareness()
        widget.root = tk.Tk()
        widget.root.withdraw()
        widget.menu = tk.Menu(widget.root, tearoff=False)
        try:
            widget._enable_click_through()
            self.assertTrue(widget._mouse_hook_handle)
            self.assertNotEqual(widget._hook_thread_id, threading.get_native_id())
            # Tk is deliberately not pumped while the hook thread runs.
            time.sleep(0.1)
            self.assertTrue(widget._hook_thread.is_alive())
            widget._stop_mouse_hook()
            self.assertFalse(widget._hook_thread.is_alive())
            self.assertIsNone(widget._mouse_hook_handle)
            self.assertIsNone(widget._hook_error)
        finally:
            if hasattr(widget, "_hook_thread"):
                widget._stop_mouse_hook()
            for callback_id in widget.root.tk.call("after", "info"):
                widget.root.after_cancel(callback_id)
            widget.root.destroy()

    @unittest.skipUnless(os.name == "nt", "Requires native Windows DPI conversion")
    def test_native_physical_click_matches_window_bounds_and_opens_menu(self):
        enable_dpi_awareness()
        widget = UsageWidget.__new__(UsageWidget)
        widget.root = tk.Tk()
        widget.root.overrideredirect(True)
        widget.root.geometry("270x38+2464+18")
        widget.menu = tk.Menu(widget.root, tearoff=False)
        widget.drag_origin = None
        widget._show_menu = mock.Mock()
        widget._move_widget = mock.Mock()
        try:
            widget._enable_click_through()
            widget.root.update()
            api = widget._input_api
            rect = _Rect()
            self.assertTrue(api.GetWindowRect(widget._input_hwnd, ctypes.byref(rect)))
            self.assertTrue(api.IsWindowVisible(widget._input_hwnd))
            point = ctypes.wintypes.POINT(rect.left + 10, rect.top + 10)
            expected = (point.x, point.y)
            api.LogicalToPhysicalPointForPerMonitorDPI.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.wintypes.POINT)
            ]
            api.LogicalToPhysicalPointForPerMonitorDPI.restype = ctypes.c_int
            self.assertTrue(api.LogicalToPhysicalPointForPerMonitorDPI(
                widget._input_hwnd, ctypes.byref(point)
            ))
            self.assertEqual((point.x, point.y), expected)
            data = _MouseHookData()
            data.pt.x, data.pt.y = point.x, point.y
            for message in (0x204, 0x205):
                self.assertEqual(widget._mouse_hook(0, message, ctypes.addressof(data)), 1)
            widget._drain_mouse_events()
            widget._show_menu.assert_called_once_with(*expected)
            widget._move_widget.assert_not_called()
        finally:
            if hasattr(widget, "_hook_thread"):
                widget._stop_mouse_hook()
            for callback_id in widget.root.tk.call("after", "info"):
                widget.root.after_cancel(callback_id)
            widget.root.destroy()

    def test_outside_click_queues_dismissal_but_inside_click_does_not(self):
        widget = self.widget
        api = widget._input_api = mock.Mock()
        widget._menu_hwnd = 123
        api.WindowFromPoint.return_value = 456
        api.GetAncestor.return_value = 123
        widget._dismiss_menu_outside(110, 110)
        self.assertTrue(widget._mouse_events.empty())
        api.GetAncestor.return_value = 789
        widget._dismiss_menu_outside(600, 500)
        self.assertEqual(widget._mouse_events.get_nowait(), ("dismiss", 600, 500))


if __name__ == "__main__":
    unittest.main()
