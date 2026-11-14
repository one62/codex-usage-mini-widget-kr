import os
import ctypes
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from codex_usage import UsageError, find_codex_executables, parse_rate_limits
from widget import FONT_FAMILY, UsageWidget, _MouseHookData, _Rect, enable_dpi_awareness, clamp_position, gradient_color, load_bundled_fonts, recovery_delay


class ParseRateLimitsTests(unittest.TestCase):
    def test_parses_codex_bucket(self):
        snapshot = parse_rate_limits(
            {
                "rateLimits": {},
                "rateLimitsByLimitId": {
                    "codex": {
                        "planType": "plus",
                        "primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 123},
                        "secondary": {"usedPercent": 17, "windowDurationMins": 10080, "resetsAt": 456},
                        "credits": {"unlimited": False, "balance": "83.02622"},
                        "rateLimitReachedType": None,
                    }
                },
            }
        )
        self.assertEqual(snapshot.plan, "plus")
        self.assertEqual(snapshot.primary.remaining_percent, 93)
        self.assertEqual(snapshot.secondary.remaining_percent, 83)
        self.assertEqual(snapshot.credit_balance, Decimal("83.02622"))

    def test_clamps_percentages(self):
        snapshot = parse_rate_limits({"rateLimits": {"primary": {"usedPercent": 120}}})
        self.assertEqual(snapshot.primary.used_percent, 100)
        self.assertEqual(snapshot.primary.remaining_percent, 0)

    def test_rejects_missing_limits(self):
        with self.assertRaises(UsageError):
            parse_rate_limits({})

    def test_accepts_a_future_bucket_identifier(self):
        snapshot = parse_rate_limits(
            {
                "rateLimitsByLimitId": {
                    "codex-next": {
                        "primary": {"usedPercent": "12.8", "windowDurationMins": 300}
                    }
                }
            }
        )
        self.assertEqual(snapshot.primary.used_percent, 12)

    def test_finds_versioned_desktop_codex_when_path_is_stale(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "OpenAI" / "Codex" / "bin" / "new" / "codex.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": temp}, clear=True):
                with mock.patch("codex_usage.shutil.which", return_value=None):
                    self.assertEqual(find_codex_executables(), [str(executable.resolve())])

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

    @unittest.skipUnless(os.name == "nt", "Requires Windows Tk font metrics")
    def test_fixed_titles_and_zero_border_right_aligned_labels(self):
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
            # At 125% this used to turn 2474,28 into 3093,35, outside the rect.
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
