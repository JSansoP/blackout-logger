#!/usr/bin/env python3
"""
Unit tests for blackout detection and timestamp calculations.
"""

import contextlib
import os
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from unittest.mock import patch

# Add vps dir to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set test environment
os.environ["PI_ADDRESS"] = "100.64.0.1"
os.environ["TELEGRAM_BOT_TOKEN"] = "fake_token"
os.environ["TELEGRAM_CHAT_ID"] = "fake_chat"

import blackout_checker
import database


def _use_temp_db():
    """Point the database module at a new empty temporary file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    database.DB_PATH = path
    return path


def _remove_temp_db(path):
    with contextlib.suppress(OSError):
        os.remove(path)


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_db = _use_temp_db()
        database.init_db()

    def tearDown(self):
        _remove_temp_db(self.temp_db)

    def _fetch_all(self, table):
        conn = database._connect()
        try:
            return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY id")]
        finally:
            conn.close()

    def _create_ongoing_blackout(self, started_at, last_known_boot_time):
        database.create_blackout(
            detected_at=started_at,
            started_at=started_at,
            last_known_boot_time=last_known_boot_time,
        )
        return database.get_ongoing_blackout()


class TestBlackoutLogic(DatabaseTestCase):
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
            
            blackout_checker.handle_reachable(uptime2)

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
             patch("blackout_checker.poll_pi", return_value=uptime_recover), \
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
             patch("blackout_checker.poll_pi", return_value=uptime_recover), \
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
            self.assertEqual(duration, 840.0)

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
            blackout_checker.handle_reachable(uptime2)
            mock_notify.assert_not_called()

    def test_ongoing_blackout_resolution_in_handle_reachable(self):
        """Test ongoing blackout resolution when VPS restarted/recovered."""
        # Setup ongoing blackout
        t_start = "2026-08-25T10:00:00+00:00"
        last_boot = "2026-08-20T00:00:00+00:00"
        database.create_blackout(
            detected_at="2026-08-25T10:10:00+00:00",
            started_at=t_start,
            last_known_boot_time=last_boot,
        )

        # Pi now reachable with new boot time (power outage)
        t_reach = "2026-08-25T10:20:00+00:00"
        uptime = 300.0  # booted at 10:15:00

        with patch("blackout_checker._now_iso", return_value=t_reach), \
             patch("notifications.notify_blackout_resolved") as mock_notify:
            blackout_checker.handle_reachable(uptime)

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
        database.create_blackout(
            detected_at="2026-08-25T10:10:00+00:00",
            started_at=t_start,
            last_known_boot_time=last_boot,
        )

        # Pi reachable again, uptime is 5 days + 10 hours (same boot time, internet was down)
        t_reach = "2026-08-25T10:20:00+00:00"
        uptime = 469200.0  # ~5.43 days, matches last_boot!

        with patch("blackout_checker._now_iso", return_value=t_reach), \
             patch("notifications.notify_blackout_resolved") as mock_notify:
            blackout_checker.handle_reachable(uptime)

            mock_notify.assert_called_once()
            args = mock_notify.call_args[0]
            started_at, ended_at, duration, blackout_type = args

            self.assertEqual(blackout_type, "internet")
            self.assertEqual(started_at, t_start)
            self.assertEqual(ended_at, t_reach)  # ended_at must be recovery time!
            self.assertEqual(duration, 1200.0)   # 20 min


class TestPiClockMigration(unittest.TestCase):
    def setUp(self):
        self.temp_db = _use_temp_db()

    def tearDown(self):
        _remove_temp_db(self.temp_db)

    def _create_legacy_db_with_pi_clock_rows(self):
        conn = database._connect()
        try:
            conn.executescript(database._SCHEMA)
            database._add_missing_columns(conn)
            conn.execute(
                "INSERT INTO uptime_logs (timestamp, uptime_seconds, boot_time, was_reachable) VALUES (?, ?, ?, 1)",
                ("2026-08-25T10:00:00+00:00", 3600.0, "2026-08-25T07:00:00+00:00"),
            )
            conn.execute(
                "INSERT INTO uptime_logs (timestamp, uptime_seconds, boot_time, was_reachable) VALUES (?, NULL, NULL, 0)",
                ("2026-08-25T10:10:00+00:00",),
            )
            conn.execute(
                """INSERT INTO blackouts (detected_at, started_at, resolved, last_known_boot_time)
                   VALUES (?, ?, 0, ?)""",
                ("2026-08-25T10:10:00+00:00", "2026-08-25T10:00:00+00:00", "2026-08-25T07:00:00+00:00"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_migration_recomputes_boot_times_from_vps_clock(self):
        self._create_legacy_db_with_pi_clock_rows()

        database.init_db()

        log = database.get_last_successful_log()
        self.assertEqual(log["boot_time"], "2026-08-25T09:00:00+00:00")
        self.assertEqual(database.get_ongoing_blackout()["last_known_boot_time"], "2026-08-25T09:00:00+00:00")

    def test_migration_runs_once(self):
        self._create_legacy_db_with_pi_clock_rows()
        database.init_db()
        conn = database._connect()
        try:
            conn.execute("UPDATE uptime_logs SET boot_time = 'sentinel' WHERE was_reachable = 1")
            conn.commit()
        finally:
            conn.close()

        database.init_db()

        self.assertEqual(database.get_last_successful_log()["boot_time"], "sentinel")

    def test_no_false_power_outage_after_migrating_pi_clock_rows(self):
        self._create_legacy_db_with_pi_clock_rows()
        database.init_db()

        with patch("blackout_checker._now_iso", return_value="2026-08-25T10:20:00+00:00"), \
             patch("notifications.notify_blackout_resolved") as mock_resolved:
            blackout_checker.handle_reachable(4800.0)

        _, ended_at, duration, blackout_type = mock_resolved.call_args[0]
        self.assertEqual(blackout_type, "internet")
        self.assertEqual(ended_at, "2026-08-25T10:20:00+00:00")
        self.assertEqual(duration, 1200.0)


class TestOutageEnd(unittest.TestCase):
    def test_power_outage_ends_at_boot_time(self):
        ended_at, duration = blackout_checker._outage_end(
            "2026-08-25T10:00:00+00:00", "power",
            "2026-08-25T10:05:00+00:00", "2026-08-25T10:10:00+00:00",
        )
        self.assertEqual(ended_at, "2026-08-25T10:05:00+00:00")
        self.assertEqual(duration, 300.0)

    def test_boot_time_before_start_falls_back_to_poll_time_with_warning(self):
        with self.assertLogs("blackout_checker", level="WARNING"):
            ended_at, duration = blackout_checker._outage_end(
                "2026-08-25T10:00:00+00:00", "power",
                "2026-08-25T09:59:00+00:00", "2026-08-25T10:10:00+00:00",
            )
        self.assertEqual(ended_at, "2026-08-25T10:10:00+00:00")
        self.assertEqual(duration, 600.0)

    def test_poll_time_before_start_clamps_to_zero(self):
        with self.assertLogs("blackout_checker", level="WARNING"):
            _, duration = blackout_checker._outage_end(
                "2026-08-25T10:00:00+00:00", "internet",
                None, "2026-08-25T09:59:00+00:00",
            )
        self.assertEqual(duration, 0.0)


class TestRebootDetection(unittest.TestCase):
    def test_threshold(self):
        prev = "2026-08-25T10:00:00+00:00"
        self.assertFalse(blackout_checker._pi_rebooted(prev, "2026-08-25T10:00:30+00:00"))
        self.assertTrue(blackout_checker._pi_rebooted(prev, "2026-08-25T10:00:31+00:00"))

    def test_missing_or_bad_boot_times(self):
        self.assertIsNone(blackout_checker._pi_rebooted(None, "2026-08-25T10:00:00+00:00"))
        self.assertIsNone(blackout_checker._pi_rebooted("garbage", "2026-08-25T10:00:00+00:00"))
        self.assertEqual(blackout_checker._determine_blackout_type("garbage", "2026-08-25T10:00:00+00:00"), "unknown")

    def test_small_uptime_drop_is_not_a_reboot_between_crons(self):
        temp_db = _use_temp_db()
        try:
            database.init_db()
            t1 = "2026-08-25T10:00:00+00:00"
            database.log_uptime(t1, 1000.0, blackout_checker._calculate_boot_time(t1, 1000.0))
            with patch("blackout_checker._now_iso", return_value="2026-08-25T10:00:10+00:00"), \
                 patch("notifications.notify_blackout_detected_resolved") as mock_notify:
                blackout_checker.handle_reachable(990.0)
            mock_notify.assert_not_called()
        finally:
            _remove_temp_db(temp_db)


class TestPollPi(unittest.TestCase):
    def _fake_response(self, body):
        response = unittest.mock.MagicMock()
        response.read.return_value = body
        response.__enter__.return_value = response
        return response

    def test_valid_response_returns_uptime(self):
        with patch("urllib.request.urlopen", return_value=self._fake_response(b'{"uptime_seconds": 12.5}')):
            self.assertEqual(blackout_checker.poll_pi(), 12.5)

    def test_connection_error_is_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            self.assertIsNone(blackout_checker.poll_pi())

    def test_timeout_is_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            self.assertIsNone(blackout_checker.poll_pi())

    def test_http_error_is_invalid_response(self):
        error = urllib.error.HTTPError("http://pi/uptime", 500, "boom", {}, None)
        with patch("urllib.request.urlopen", side_effect=error), \
             self.assertRaises(blackout_checker.InvalidPiResponse):
            blackout_checker.poll_pi()

    def test_bad_bodies_are_invalid_responses(self):
        bodies = [
            b"not json",
            b"\xff\xfe",
            b"[]",
            b"{}",
            b'{"uptime_seconds": null}',
            b'{"uptime_seconds": "12"}',
            b'{"uptime_seconds": true}',
            b'{"uptime_seconds": -1}',
            b'{"uptime_seconds": NaN}',
        ]
        for body in bodies:
            with self.subTest(body=body), \
                 patch("urllib.request.urlopen", return_value=self._fake_response(body)), \
                 self.assertRaises(blackout_checker.InvalidPiResponse):
                blackout_checker.poll_pi()


class TestInvalidResponseHandling(DatabaseTestCase):
    def test_invalid_response_resolves_ongoing_blackout_as_unknown(self):
        self._create_ongoing_blackout("2026-08-25T10:00:00+00:00", "2026-08-20T00:00:00+00:00")

        with patch("blackout_checker._now_iso", return_value="2026-08-25T10:20:00+00:00"), \
             patch("notifications.notify_blackout_resolved") as mock_resolved:
            blackout_checker.handle_invalid_response(blackout_checker.InvalidPiResponse("HTTP 500"))

        mock_resolved.assert_called_once_with(
            "2026-08-25T10:00:00+00:00", "2026-08-25T10:20:00+00:00", 1200.0, "unknown"
        )
        self.assertIsNone(database.get_ongoing_blackout())

    def test_invalid_response_without_ongoing_blackout_records_nothing(self):
        with patch("notifications.notify_blackout_resolved") as mock_resolved:
            blackout_checker.handle_invalid_response(blackout_checker.InvalidPiResponse("HTTP 500"))

        mock_resolved.assert_not_called()
        self.assertEqual(self._fetch_all("blackouts"), [])
        self.assertEqual(self._fetch_all("uptime_logs"), [])

    def test_invalid_response_in_retry_loop_resolves_as_unknown(self):
        t1 = "2026-08-25T10:00:00+00:00"
        database.log_uptime(t1, 1000.0, blackout_checker._calculate_boot_time(t1, 1000.0))

        with patch("blackout_checker._now_iso", side_effect=["2026-08-25T10:10:00+00:00", "2026-08-25T10:11:00+00:00"]), \
             patch("blackout_checker.poll_pi", side_effect=blackout_checker.InvalidPiResponse("HTTP 500")), \
             patch("time.sleep", return_value=None), \
             patch("notifications.notify_blackout_in_progress"), \
             patch("notifications.notify_blackout_resolved") as mock_resolved:
            blackout_checker.handle_unreachable()

        mock_resolved.assert_called_once_with(t1, "2026-08-25T10:11:00+00:00", 660.0, "unknown")

    def test_main_routes_invalid_response_without_opening_blackout(self):
        with patch("blackout_checker.poll_pi", side_effect=blackout_checker.InvalidPiResponse("HTTP 500")), \
             patch("blackout_checker.handle_unreachable") as mock_unreachable, \
             patch("blackout_checker.handle_invalid_response") as mock_invalid, \
             patch("blackout_checker.acquire_lock", return_value=True), \
             patch("blackout_checker.release_lock"), \
             patch("signal.signal"):
            blackout_checker.main()

        mock_unreachable.assert_not_called()
        mock_invalid.assert_called_once()


if __name__ == "__main__":
    unittest.main()
