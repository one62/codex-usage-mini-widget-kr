import ctypes
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock


_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.settings import (
    WidgetSettings,
    load_settings_from_path,
    save_settings_to_path,
    validate_settings,
)
from common.widget_base import (
    BaseUsageWidget,
    PopupMenu,
    _MouseHookData,
    _Rect,
    clamp_position,
    enable_dpi_awareness,
    gradient_color,
    recovery_delay,
    reset_short,
)


class CommonHelpersTests(unittest.TestCase):
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
        with mock.patch("common.widget_base.time.time", return_value=1_000_000.0):
            self.assertEqual(reset_short(1_000_000), "now")
            self.assertEqual(reset_short(1_000_000 + 86_400), "24h 0m")
            self.assertEqual(reset_short(1_000_000 + 90_000), "1d 1h")
            self.assertEqual(reset_short(1_000_000 + 3_660), "1h 1m")
            self.assertEqual(reset_short(1_000_000 + 61), "2m")

    def test_settings_validation_and_atomic_persistence(self):
        validated = validate_settings(
            {
                "transparency": True,
                "x": 120,
                "y": -10,
                "movement_locked": True,
                "hover_transparency_enabled": False,
            }
        )
        self.assertEqual(validated.transparency, 30)
        self.assertEqual((validated.x, validated.y), (120, -10))
        self.assertTrue(validated.movement_locked)
        self.assertFalse(validated.hover_transparency_enabled)

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            self.assertEqual(load_settings_from_path(path), WidgetSettings())
            expected = WidgetSettings(
                transparency=55,
                x=300,
                y=40,
                movement_locked=True,
                hover_transparency_enabled=False,
            )
            save_settings_to_path(expected, path)
            self.assertEqual(load_settings_from_path(path), expected)

    def test_auth_required_enables_clickable_status_and_skips_fast_recovery_timer(self):
        widget = BaseUsageWidget.__new__(BaseUsageWidget)
        widget.root = mock.Mock()
        widget.menu = mock.Mock()
        widget.reset_compact = mock.Mock()
        widget.connection_status_index = 1
        widget.refreshing = False
        widget.snapshot = None
        widget.failure_count = 0
        widget.recovery_after_id = "timer-1"

        widget._handle_connection_error("auth error", auth_required=True)

        widget.root.after_cancel.assert_called_once_with("timer-1")
        self.assertIsNone(widget.recovery_after_id)
        widget.root.after.assert_not_called()
        widget.menu.entryconfigure.assert_called_with(
            1,
            label="연결 상태: 로그인 필요",
            state="normal",
        )
        widget.reset_compact.configure.assert_called_with(text="offline", fg="#C6C3CE")

        widget.refreshing = False
        widget.auth_required = False
        widget.connection_failed = False
        widget.last_error = ""
        widget.snapshot = object()
        widget._update_connection_status()
        widget.menu.entryconfigure.assert_called_with(
            1,
            label="연결 상태: 정상",
            state="disabled",
        )

    @unittest.skipUnless(os.name == "nt", "Requires Windows Tk")
    def test_clicking_login_required_status_invokes_open_app(self):
        enable_dpi_awareness()
        root = tk.Tk()
        root.withdraw()
        try:
            opened = mock.Mock()
            menu = PopupMenu(root, lambda: None)
            menu.add_command(label="연결 상태: 확인 전", command=opened, state="disabled")
            menu.open = True

            menu._invoke(0)
            opened.assert_not_called()

            menu.entryconfigure(0, label="연결 상태: 로그인 필요", state="normal")
            menu._invoke(0)
            opened.assert_called_once_with()
        finally:
            root.destroy()


class MouseGestureTests(unittest.TestCase):
    def setUp(self):
        self.widget = BaseUsageWidget.__new__(BaseUsageWidget)
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
        widget = BaseUsageWidget.__new__(BaseUsageWidget)
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
        widget = BaseUsageWidget.__new__(BaseUsageWidget)
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
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.wintypes.POINT),
            ]
            api.LogicalToPhysicalPointForPerMonitorDPI.restype = ctypes.c_int
            self.assertTrue(
                api.LogicalToPhysicalPointForPerMonitorDPI(widget._input_hwnd, ctypes.byref(point))
            )
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

    def test_ensure_topmost_restores_when_native_flag_cleared(self):
        widget = self.widget
        widget._closing = False
        widget.always_on_top = mock.Mock()
        widget.always_on_top.get.return_value = True
        widget._input_hwnd = 100
        api = widget._input_api = mock.Mock()

        api.GetWindowLongW.return_value = 0x80028  # WS_EX_TOPMOST (0x8) already set
        widget._ensure_topmost()
        widget.root.attributes.assert_not_called()

        api.GetWindowLongW.return_value = 0x80020  # WS_EX_TOPMOST (0x8) cleared
        widget._ensure_topmost()
        widget.root.attributes.assert_called_once_with("-topmost", True)
        widget.root.lift.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
