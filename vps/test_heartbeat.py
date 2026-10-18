#!/usr/bin/env python3
"""
Unit tests for heartbeat pings to heartbeat-monitor.
"""

import os
import sys
import unittest
import urllib.error
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("PI_ADDRESS", "100.64.0.1")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake_token")
os.environ.setdefault("TELEGRAM_CHAT_ID", "fake_chat")

import blackout_checker
import config
import heartbeat

URL = "http://127.0.0.1:8091/ping/blackout-checker"


class TestSend(unittest.TestCase):
    def test_disabled_without_url(self):
        with patch.object(config, "HEARTBEAT_URL", ""), patch("urllib.request.urlopen") as urlopen:
            self.assertFalse(heartbeat.send())
        urlopen.assert_not_called()

    def test_urls_and_timeout(self):
        response = MagicMock(status=200)
        response.__enter__.return_value = response
        with patch.object(config, "HEARTBEAT_URL", URL + "/"), patch("urllib.request.urlopen", return_value=response) as urlopen:
            self.assertTrue(heartbeat.send())
            self.assertTrue(heartbeat.send("start"))
        self.assertEqual(urlopen.call_args_list, [call(URL, timeout=5), call(URL + "/start", timeout=5)])

    def test_errors_are_logged_not_raised(self):
        for error in (urllib.error.URLError("refused"), TimeoutError("timed out"), ValueError("bad url")):
            with self.subTest(error=error), patch.object(config, "HEARTBEAT_URL", URL), \
                 patch("urllib.request.urlopen", side_effect=error), \
                 self.assertLogs("heartbeat", "WARNING"):
                self.assertFalse(heartbeat.send("fail"))


class TestMainPings(unittest.TestCase):
    def run_main(self, lock=True, poll=None, poll_error=None, handler_error=None):
        with patch("heartbeat.send") as send, \
             patch("blackout_checker.config.validate"), \
             patch("blackout_checker.database.init_db"), \
             patch("blackout_checker.acquire_lock", return_value=lock), \
             patch("blackout_checker.release_lock"), \
             patch("blackout_checker.poll_pi", return_value=poll, side_effect=poll_error), \
             patch("blackout_checker.handle_reachable", side_effect=handler_error), \
             patch("blackout_checker.handle_unreachable"), \
             patch("blackout_checker.handle_invalid_response"), \
             patch("signal.signal"):
            try:
                blackout_checker.main()
            except SystemExit:
                pass
        return [c.args for c in send.call_args_list]

    def test_normal_run_pings_start_then_success(self):
        self.assertEqual(self.run_main(poll=1000.0), [("start",), ()])

    def test_unreachable_run_pings_success_after_the_retry_loop(self):
        self.assertEqual(self.run_main(poll=None), [("start",), ()])

    def test_invalid_response_run_still_pings_success(self):
        self.assertEqual(self.run_main(poll_error=blackout_checker.InvalidPiResponse("HTTP 500")), [("start",), ()])

    def test_lock_held_run_pings_start_only(self):
        self.assertEqual(self.run_main(lock=False), [("start",)])

    def test_unexpected_error_pings_fail(self):
        with self.assertLogs("blackout_checker", "CRITICAL"):
            self.assertEqual(self.run_main(poll=1000.0, handler_error=RuntimeError("boom")), [("start",), ("fail",)])

    def test_config_error_pings_fail(self):
        with patch("heartbeat.send") as send, \
             patch("blackout_checker.config.validate", side_effect=ValueError("PI_ADDRESS is required")), \
             self.assertRaises(SystemExit):
            blackout_checker.main()
        self.assertEqual([c.args for c in send.call_args_list], [("start",), ("fail",)])


if __name__ == "__main__":
    unittest.main()
