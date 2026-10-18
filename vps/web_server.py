#!/usr/bin/env python3
"""
Blackout Logger — Web Dashboard

Read-only HTTP server for the dashboard: serves the static frontend in web/
and a small JSON API over the blackout database.

Usage:
    python3 web_server.py [--host 127.0.0.1] [--port 8090]

Endpoints:
    GET /                 dashboard
    GET /static/<file>    frontend assets
    GET /api/status       current state of the Pi
    GET /api/blackouts    blackouts of the last ?days=N days (default 30)
"""

import argparse
import json
import logging
import mimetypes
import os
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import database
from blackout_checker import STUCK_TYPE, _is_unanswered_stuck_blackout, _seconds_between

logger = logging.getLogger(__name__)

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
STALE_AFTER_SECONDS = 30 * 60
DEFAULT_DAYS = 30
MAX_DAYS = 3650
MAX_BLACKOUTS = 1000

mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("text/javascript", ".js")


def _now():
    return datetime.now(UTC)


def _log_dict(row):
    if row is None:
        return None
    return {
        "timestamp": row["timestamp"],
        "reachable": bool(row["was_reachable"]),
        "uptime_seconds": row["uptime_seconds"],
        "boot_time": row["boot_time"],
    }


def _blackout_dict(row):
    result = {
        "id": row["id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "duration_seconds": row["duration_seconds"],
        "type": row["blackout_type"],
        "resolved": bool(row["resolved"]),
        "seen_again_at": None,
    }
    if row["blackout_type"] == STUCK_TYPE:
        seen = database.get_first_successful_log_after(row["started_at"])
        if seen is not None:
            result["seen_again_at"] = seen["timestamp"]
    return result


def build_status(now=None):
    """
    Summarize the current state of the Pi.

    state is one of:
        'online'   — the latest poll reached the Pi
        'offline'  — the Pi is unreachable (since = start of the outage)
        'stuck'    — an outage was closed as stuck and the Pi has not been seen since
        'no_data'  — nothing has been logged yet
    """
    now_iso = (now or _now()).isoformat()
    last_log = database.get_last_log()
    last_success = database.get_last_successful_log()
    ongoing = database.get_ongoing_blackout()
    latest_blackout = database.get_latest_blackout()

    if last_log is None:
        state, since = "no_data", None
    elif ongoing is not None:
        state, since = "offline", ongoing["started_at"]
    elif _is_unanswered_stuck_blackout(latest_blackout, last_success):
        state, since = "stuck", latest_blackout["started_at"]
    elif last_log["was_reachable"]:
        state, since = "online", last_log["boot_time"]
    else:
        state, since = "offline", last_success["timestamp"] if last_success else last_log["timestamp"]

    stale = last_log is not None and _seconds_between(last_log["timestamp"], now_iso) > STALE_AFTER_SECONDS

    return {
        "now": now_iso,
        "state": state,
        "since": since,
        "stale": stale,
        "last_poll": _log_dict(last_log),
        "last_success": _log_dict(last_success),
        "ongoing": _blackout_dict(ongoing) if ongoing is not None else None,
        "latest_blackout": _blackout_dict(latest_blackout) if latest_blackout is not None else None,
    }


def list_blackouts(days, now=None):
    """Return blackouts that started in the last `days` days, plus any open one, newest first."""
    since = ((now or _now()) - timedelta(days=days)).isoformat()
    return [_blackout_dict(row) for row in database.get_blackouts_since(since, MAX_BLACKOUTS)]


def _parse_days(query):
    values = parse_qs(query).get("days")
    if not values:
        return DEFAULT_DAYS
    days = int(values[0])
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    return days


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "BlackoutLogger"

    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                self._send_file(os.path.join(WEB_DIR, "index.html"))
            elif path.startswith("/static/"):
                self._send_static(path.removeprefix("/static/"))
            elif path == "/api/status":
                self._send_json(build_status())
            elif path == "/api/blackouts":
                try:
                    days = _parse_days(url.query)
                except ValueError as e:
                    self._send_json({"error": str(e)}, 400)
                    return
                self._send_json({"days": days, "blackouts": list_blackouts(days)})
            else:
                self._send_json({"error": "Not found"}, 404)
        except Exception:
            logger.exception("Error handling %s", self.path)
            self._send_json({"error": "Internal server error"}, 500)

    def _send_static(self, relative):
        root = os.path.realpath(WEB_DIR)
        target = os.path.realpath(os.path.join(root, relative))
        if not target.startswith(root + os.sep) or not os.path.isfile(target):
            self._send_json({"error": "Not found"}, 404)
            return
        self._send_file(target, cache=True)

    def _send_file(self, file_path, cache=False):
        with open(file_path, "rb") as f:
            body = f.read()
        content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        if content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=300" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        logger.debug("%s - %s", self.address_string(), format % args)


def main():
    parser = argparse.ArgumentParser(description="Blackout Logger dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    database.init_db()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    logger.info("Dashboard listening on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
