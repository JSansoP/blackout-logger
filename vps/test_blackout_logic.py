#!/usr/bin/env python3
"""
Unit tests for blackout detection and timestamp calculations.
"""

import os
import sys
import unittest
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Add vps dir to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set test environment
os.environ["PI_ADDRESS"] = "100.64.0.1"
os.environ["TELEGRAM_BOT_TOKEN"] = "fake_token"
os.environ["TELEGRAM_CHAT_ID"] = "fake_chat"

import config
import database
import blackout_checker


class TestBlackoutLogic(unittest.TestCase):
    def setUp(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False)
        self.temp_db.close()
        database.DB_PATH = self.temp_db.name
        database.init_db()

    def tearDown(self):
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except OSError:
            pass

    def test_boot_time_calculation(self):
        now_iso = "2026-08-25T12:00:00+00:00"
        uptime_seconds = 300.0  # 5 minutes
        boot_iso = blackout_checker._calculate_boot_time(now_iso, uptime_seconds)
        self.assertEqual(boot_iso, "2026-08-25T11:55:00+00:00")

    def test_idle_reboot_timestamps_and_duration(self):
        """
        Test reboot between cron polls:
        Run 1 at 10:00:00, uptime = 50000s
        Run 2 at 10:10:00, uptime = 180s (Pi rebooted at 10:07:00)
        """
        # Run 1: Pi online
        t1 = "2026-08-25T10:00:00+00:00"
        uptime1 = 50000.0
        boot1 = blackout_checker._calculate_boot_time(t1, uptime1)
        database.log_uptime(t1, uptime1, boot1, was_reachable=True)

        # Run 2: Pi online after reboot
        t2 = "2026-08-25T10:10:00+00:00"
        uptime2 = 180.0  # 3 minutes uptime

        with patch("blackout_checker._now_iso", return_value=t2), \
             patch("notifications.notify_blackout_detected_resolved") as mock_notify:
            
            blackout_checker.handle_reachable({"uptime_seconds": uptime2})

            # Check that notification was sent with correct positive duration and ordered timestamps
            mock_notify.assert_called_once()
            args = mock_notify.call_args[0]
            started_at, ended_at, duration = args

            self.assertEqual(started_at, t1)
            self.assertEqual(ended_at, "2026-08-25T10:07:00+00:00")
            self.assertEqual(duration, 420.0)  # 7 minutes between 10:00 and 10:07

            # Verify in DB
            conn = database._connect()
            try:
                row = conn.execute("SELECT * FROM blackouts ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(row["started_at"], t1)
                self.assertEqual(row["ended_at"], "2026-08-25T10:07:00+00:00")
                self.assertEqual(row["duration_seconds"], 420.0)
                self.assertEqual(row["blackout_type"], "power")
                self.assertEqual(row["resolved"], 1)
            finally:
                conn.close()

    def test_internet_disruption_uses_recovery_time_as_ended_at(self):
        """
        Test network disruption (Pi never rebooted):
        Run 1: Pi online at 10:00:00, uptime = 100000s (boot time days ago)
        Run 2: Pi unreachable at 10:10:00
        Retry: Pi comes back online at 10:15:00, uptime = 100900s (no reboot!)
        """
        t1 = "2026-08-25T10:00:00+00:00"
        uptime1 = 100000.0
        boot1 = blackout_checker._calculate_boot_time(t1, uptime1)
        database.log_uptime(t1, uptime1, boot1, was_reachable=True)

        t_fail = "2026-08-25T10:10:00+00:00"
        t_recover = "2026-08-25T10:15:00+00:00"
        uptime_recover = 100900.0  # Pi kept running

        # Mock poll_pi to return reachable Pi on first retry
        with patch("blackout_checker._now_iso", side_effect=[t_fail, t_recover]), \
             patch("blackout_checker.poll_pi", return_value={"uptime_seconds": uptime_recover}), \
             patch("time.sleep", return_value=None), \
             patch("notifications.notify_blackout_in_progress") as mock_in_prog, \
             patch("notifications.notify_blackout_resolved") as mock_resolved:

            blackout_checker.handle_unreachable()

            mock_in_prog.assert_called_once_with(t1)
            mock_resolved.assert_called_once()
            args = mock_resolved.call_args[0]
            started_at, ended_at, duration, blackout_type = args

            self.assertEqual(blackout_type, "internet")
            self.assertEqual(started_at, t1)
            self.assertEqual(ended_at, t_recover)  # Must be recovery time, NOT boot time from days ago!
            self.assertEqual(duration, 900.0)      # 15 minutes (10:00 to 10:15)

    def test_power_outage_during_retry_loop(self):
        """
        Test power outage during retry loop (Pi rebooted):
        Run 1: Pi online at 10:00:00, uptime = 100000s
        Run 2: Pi unreachable at 10:10:00
        Retry: Pi comes back online at 10:15:00 with uptime = 60s (booted at 10:14:00)
        """
        t1 = "2026-08-25T10:00:00+00:00"
        uptime1 = 100000.0
        boot1 = blackout_checker._calculate_boot_time(t1, uptime1)
        database.log_uptime(t1, uptime1, boot1, was_reachable=True)

        t_fail = "2026-08-25T10:10:00+00:00"
        t_recover = "2026-08-25T10:15:00+00:00"
        uptime_recover = 60.0  # Just rebooted 1 min ago!

        with patch("blackout_checker._now_iso", side_effect=[t_fail, t_recover]), \
             patch("blackout_checker.poll_pi", return_value={"uptime_seconds": uptime_recover}), \
             patch("time.sleep", return_value=None), \
             patch("notifications.notify_blackout_in_progress"), \
             patch("notifications.notify_blackout_resolved") as mock_resolved:

            blackout_checker.handle_unreachable()

            mock_resolved.assert_called_once()
            args = mock_resolved.call_args[0]
            started_at, ended_at, duration, blackout_type = args

            self.assertEqual(blackout_type, "power")
            self.assertEqual(started_at, t1)
            self.assertEqual(ended_at, "2026-08-25T10:14:00+00:00")
    def test_normal_poll_no_false_positive(self):
        """Test normal cron polls with no reboot: uptime increments as expected."""
        t1 = "2026-08-25T10:00:00+00:00"
        uptime1 = 50000.0
        boot1 = blackout_checker._calculate_boot_time(t1, uptime1)
        database.log_uptime(t1, uptime1, boot1, was_reachable=True)

        t2 = "2026-08-25T10:10:00+00:00"
        uptime2 = 50600.0  # +600s

        with patch("blackout_checker._now_iso", return_value=t2), \
             patch("notifications.notify_blackout_detected_resolved") as mock_notify:
            blackout_checker.handle_reachable({"uptime_seconds": uptime2})
            mock_notify.assert_not_called()

    def test_ongoing_blackout_resolution_in_handle_reachable(self):
        """Test ongoing blackout resolution when VPS restarted/recovered."""
        # Setup ongoing blackout
        t_start = "2026-08-25T10:00:00+00:00"
        last_boot = "2026-08-20T00:00:00+00:00"
        blackout_id = database.create_blackout(
            detected_at="2026-08-25T10:10:00+00:00",
            started_at=t_start,
            last_known_boot_time=last_boot,
        )

        # Pi now reachable with new boot time (power outage)
        t_reach = "2026-08-25T10:20:00+00:00"
        uptime = 300.0  # booted at 10:15:00

        with patch("blackout_checker._now_iso", return_value=t_reach), \
             patch("notifications.notify_blackout_resolved") as mock_notify:
            blackout_checker.handle_reachable({"uptime_seconds": uptime})

            mock_notify.assert_called_once()
            args = mock_notify.call_args[0]
            started_at, ended_at, duration, blackout_type = args

            self.assertEqual(blackout_type, "power")
            self.assertEqual(started_at, t_start)
            self.assertEqual(ended_at, "2026-08-25T10:15:00+00:00")
            self.assertEqual(duration, 900.0)  # 15 min

    def test_ongoing_internet_outage_resolution_in_handle_reachable(self):
        """Test ongoing internet disruption resolution in handle_reachable."""
        t_start = "2026-08-25T10:00:00+00:00"
        # Last known boot time 5 days ago:
        last_boot = "2026-08-20T00:00:00+00:00"
        blackout_id = database.create_blackout(
            detected_at="2026-08-25T10:10:00+00:00",
            started_at=t_start,
            last_known_boot_time=last_boot,
        )

        # Pi reachable again, uptime is 5 days + 10 hours (same boot time, internet was down)
        t_reach = "2026-08-25T10:20:00+00:00"
        uptime = 469200.0  # ~5.43 days, matches last_boot!

        with patch("blackout_checker._now_iso", return_value=t_reach), \
             patch("notifications.notify_blackout_resolved") as mock_notify:
            blackout_checker.handle_reachable({"uptime_seconds": uptime})

            mock_notify.assert_called_once()
            args = mock_notify.call_args[0]
            started_at, ended_at, duration, blackout_type = args

            self.assertEqual(blackout_type, "internet")
            self.assertEqual(started_at, t_start)
            self.assertEqual(ended_at, t_reach)  # ended_at must be recovery time!
            self.assertEqual(duration, 1200.0)   # 20 min


if __name__ == "__main__":
    unittest.main()
