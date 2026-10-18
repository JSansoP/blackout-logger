#!/usr/bin/env python3
"""
Unit tests for recomputing resolved blackouts.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("PI_ADDRESS", "100.64.0.1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake_token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "fake_chat")

import blackout_checker
import database
import recompute_blackouts
from test_blackout_logic import DatabaseTestCase


def _log(timestamp, uptime):
    database.log_uptime(timestamp, uptime, blackout_checker._calculate_boot_time(timestamp, uptime))


def _resolved_blackout(started_at, ended_at, duration, blackout_type):
    last_log = database.get_last_successful_log()
    blackout_id = database.create_blackout(
        detected_at=started_at,
        started_at=started_at,
        last_known_boot_time=last_log["boot_time"],
    )
    database.resolve_blackout(blackout_id, ended_at, duration, blackout_type)
    return blackout_id


class TestRecomputeBlackouts(DatabaseTestCase):
    def _changes(self):
        conn = database._connect()
        try:
            return recompute_blackouts.compute_changes(conn)
        finally:
            conn.close()

    def _apply(self):
        conn = database._connect()
        try:
            changes = recompute_blackouts.compute_changes(conn)
            recompute_blackouts.apply_changes(conn, changes)
        finally:
            conn.close()

    def test_zero_second_power_outage_gets_boot_time_end(self):
        _log("2026-08-25T10:00:00+00:00", 50000.0)
        blackout_id = _resolved_blackout(
            "2026-08-25T10:00:00+00:00", "2026-08-25T09:30:00+00:00", 0, "power"
        )
        database.log_uptime("2026-08-25T10:05:00+00:00", None, None, was_reachable=False)
        _log("2026-08-25T10:20:00+00:00", 300.0)

        self._apply()

        row = self._fetch_all("blackouts")[0]
        self.assertEqual(row["id"], blackout_id)
        self.assertEqual(row["blackout_type"], "power")
        self.assertEqual(row["ended_at"], "2026-08-25T10:15:00+00:00")
        self.assertEqual(row["duration_seconds"], 900.0)

    def test_misclassified_power_outage_becomes_internet(self):
        _log("2026-08-25T10:00:00+00:00", 50000.0)
        _resolved_blackout("2026-08-25T10:00:00+00:00", "2026-08-25T10:01:00+00:00", 60, "power")
        _log("2026-08-25T10:30:00+00:00", 51800.0)

        self._apply()

        row = self._fetch_all("blackouts")[0]
        self.assertEqual(row["blackout_type"], "internet")
        self.assertEqual(row["ended_at"], "2026-08-25T10:30:00+00:00")
        self.assertEqual(row["duration_seconds"], 1800.0)

    def test_correct_blackout_is_unchanged(self):
        _log("2026-08-25T10:00:00+00:00", 50000.0)
        _resolved_blackout("2026-08-25T10:00:00+00:00", "2026-08-25T10:30:00+00:00", 1800.0, "internet")
        _log("2026-08-25T10:30:00+00:00", 51800.0)

        self.assertEqual(self._changes(), [])

    def test_blackout_without_recovery_log_is_skipped(self):
        _log("2026-08-25T10:00:00+00:00", 50000.0)
        _resolved_blackout("2026-08-25T10:00:00+00:00", "2026-08-25T10:30:00+00:00", 0, "power")

        self.assertEqual(self._changes(), [])

    def test_ongoing_blackout_is_not_touched(self):
        _log("2026-08-25T10:00:00+00:00", 50000.0)
        database.create_blackout(detected_at="2026-08-25T10:10:00+00:00", started_at="2026-08-25T10:00:00+00:00")
        _log("2026-08-25T10:30:00+00:00", 300.0)

        self.assertEqual(self._changes(), [])


if __name__ == "__main__":
    unittest.main()
