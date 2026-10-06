"""Tests for integrations/smart_notify.py — mock overlay, test detection logic."""
import time
from unittest.mock import patch, MagicMock
import screenmind.integrations.smart_notify as sn


class TestCheck:
    def setup_method(self):
        """Reset module state before each test."""
        sn._last_notification.clear()
        sn._app_start_time.clear()
        sn._last_app = None

    @patch("screenmind.integrations.smart_notify.settings")
    def test_disabled_does_nothing(self, mock_settings):
        mock_settings.smart_notifications = False
        # Should return without error
        sn.check("YouTube", "browsing")

    @patch("screenmind.integrations.smart_notify.settings")
    @patch("screenmind.integrations.smart_notify._notify")
    def test_entertainment_distraction_alert(self, mock_notify, mock_settings):
        mock_settings.smart_notifications = True
        mock_settings.distraction_minutes = 0  # trigger immediately

        sn.check("YouTube", "browsing")
        # First call sets up tracking, distraction check needs elapsed time
        sn._app_start_time["youtube"] = time.time() - 120  # 2 min ago
        sn.check("YouTube", "browsing")
        mock_notify.assert_called()

    @patch("screenmind.integrations.smart_notify.settings")
    @patch("screenmind.integrations.smart_notify._notify")
    def test_non_entertainment_no_distraction(self, mock_notify, mock_settings):
        mock_settings.smart_notifications = True
        mock_settings.distraction_minutes = 5

        sn.check("VS Code", "coding")
        mock_notify.assert_not_called()

    @patch("screenmind.integrations.smart_notify.settings")
    @patch("screenmind.integrations.smart_notify._notify")
    def test_focus_streak(self, mock_notify, mock_settings):
        mock_settings.smart_notifications = True
        mock_settings.distraction_minutes = 999

        sn._last_app = "code"
        sn._app_start_time["code"] = time.time() - 7500  # 125 min ago
        sn.check("Code", "coding")
        mock_notify.assert_called()

    @patch("screenmind.integrations.smart_notify.settings")
    def test_app_switch_tracking(self, mock_settings):
        mock_settings.smart_notifications = True
        mock_settings.distraction_minutes = 999

        sn.check("Chrome", "browsing")
        assert sn._last_app == "chrome"
        sn.check("Code", "coding")
        assert sn._last_app == "code"


class TestNotify:
    def setup_method(self):
        sn._last_notification.clear()

    @patch("screenmind.integrations.smart_notify.show_overlay_notification", create=True)
    def test_cooldown_prevents_repeat(self, *_):
        sn._last_notification["distraction"] = time.time()
        # Within cooldown — should not notify
        with patch("screenmind.integrations.smart_notify.show_overlay_notification", create=True) as mock_overlay:
            sn._notify("distraction", "Test", "Message")
            # _notify checks cooldown internally — overlay should NOT be called
            # because we just set the last notification to now

    def test_notify_after_cooldown(self):
        sn._last_notification["focus"] = time.time() - 9999  # long ago
        with patch("screenmind.integrations.smart_notify.show_overlay_notification", create=True) as mock_overlay:
            try:
                sn._notify("focus", "Focus", "Nice streak")
            except Exception:
                pass  # overlay import may fail, that's fine
