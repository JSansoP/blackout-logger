#!/usr/bin/env python3
"""
Tests for the dashboard API: status derivation, blackout listing and HTTP routing.
"""

import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("PI_ADDRESS", "100.64.0.1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake_token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "fake_chat")

import database
import web_server
from test_blackout_logic import DatabaseTestCase

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


def _iso(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _log_reachable(minutes_ago, uptime_seconds):
    timestamp = _iso(minutes_ago)
    boot = (NOW - timedelta(minutes=minutes_ago, seconds=uptime_seconds)).isoformat()
    database.log_uptime(timestamp, uptime_seconds, boot, was_reachable=True)
    return boot


def _log_unreachable(minutes_ago):
    database.log_uptime(_iso(minutes_ago), None, None, was_reachable=False)


class BuildStatusTests(DatabaseTestCase):
    def test_no_data(self):
        status = web_server.build_status(NOW)
        self.assertEqual(status["state"], "no_data")
        self.assertIsNone(status["since"])
        self.assertFalse(status["stale"])
        self.assertIsNone(status["latest_blackout"])

    def test_online_since_boot_time(self):
        boot = _log_reachable(5, 3600)
        status = web_server.build_status(NOW)
        self.assertEqual(status["state"], "online")
        self.assertEqual(status["since"], boot)
        self.assertFalse(status["stale"])
        self.assertTrue(status["last_poll"]["reachable"])

    def test_offline_with_ongoing_blackout(self):
        _log_reachable(30, 3600)
        _log_unreachable(20)
        database.create_blackout(detected_at=_iso(20), started_at=_iso(30))
        status = web_server.build_status(NOW)
        self.assertEqual(status["state"], "offline")
        self.assertEqual(status["since"], _iso(30))
        self.assertFalse(status["ongoing"]["resolved"])

    def test_offline_without_blackout_uses_last_success(self):
        _log_reachable(30, 3600)
        _log_unreachable(20)
        status = web_server.build_status(NOW)
        self.assertEqual(status["state"], "offline")
        self.assertEqual(status["since"], _iso(30))

    def test_stuck_until_pi_is_seen_again(self):
        _log_reachable(600, 3600)
        database.create_blackout(_iso(590), _iso(600), blackout_type="stuck")
        conn = database._connect()
        conn.execute("UPDATE blackouts SET resolved = 1")
        conn.commit()
        conn.close()
        _log_unreachable(5)
        status = web_server.build_status(NOW)
        self.assertEqual(status["state"], "stuck")
        self.assertEqual(status["since"], _iso(600))
        self.assertIsNone(status["latest_blackout"]["seen_again_at"])

        _log_reachable(1, 60)
        status = web_server.build_status(NOW)
        self.assertEqual(status["state"], "online")
        self.assertEqual(status["latest_blackout"]["seen_again_at"], _iso(1))

    def test_stale_when_last_poll_is_old(self):
        _log_reachable(web_server.STALE_AFTER_SECONDS // 60 + 1, 3600)
        self.assertTrue(web_server.build_status(NOW)["stale"])


class ListBlackoutsTests(DatabaseTestCase):
    def test_filters_by_start_and_keeps_open_ones(self):
        old = (NOW - timedelta(days=40)).isoformat()
        database.create_blackout(old, old, old, 60.0, blackout_type="power")
        database.create_blackout(_iso(100), _iso(110), _iso(105), 300.0, blackout_type="internet")
        database.create_blackout(detected_at=old, started_at=old)

        result = web_server.list_blackouts(30, NOW)

        self.assertEqual([b["type"] for b in result], ["internet", "unknown"])
        self.assertFalse(result[1]["resolved"])
        self.assertEqual(result[0]["duration_seconds"], 300.0)

    def test_newest_first(self):
        database.create_blackout(_iso(200), _iso(210), _iso(205), 300.0, blackout_type="power")
        database.create_blackout(_iso(100), _iso(110), _iso(105), 300.0, blackout_type="internet")
        result = web_server.list_blackouts(1, NOW)
        self.assertEqual([b["started_at"] for b in result], [_iso(110), _iso(210)])


class HttpTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.server = web_server.ThreadingHTTPServer(("127.0.0.1", 0), web_server.DashboardHandler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_index_and_static(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"Blackout Logger", body)
        for path, content_type in [
            ("/static/app.css", "text/css"),
            ("/static/app.js", "text/javascript"),
            ("/static/vendor/archivo-latin-wdth.woff2", "font/woff2"),
        ]:
            status, headers, _ = self._get(path)
            self.assertEqual(status, 200, path)
            self.assertIn(content_type, headers["Content-Type"])

    def test_static_rejects_traversal(self):
        status, _, _ = self._get("/static/../web_server.py")
        self.assertEqual(status, 404)
        status, _, _ = self._get("/static/%2e%2e/web_server.py")
        self.assertEqual(status, 404)

    def test_api_status_and_blackouts(self):
        _log_reachable(5, 3600)
        status, _, body = self._get("/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "online")

        status, _, body = self._get("/api/blackouts?days=7")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"days": 7, "blackouts": []})

    def test_api_blackouts_rejects_bad_days(self):
        for query in ("days=0", "days=abc", f"days={web_server.MAX_DAYS + 1}"):
            status, _, body = self._get(f"/api/blackouts?{query}")
            self.assertEqual(status, 400, query)
            self.assertIn("error", json.loads(body))

    def test_unknown_path(self):
        status, _, _ = self._get("/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
