#!/usr/bin/env python3
"""
Blackout Logger — Raspberry Pi Uptime Server

A zero-dependency, lightweight HTTP server that exposes the system's
uptime via a single JSON endpoint. Designed to run on a Raspberry Pi
with minimal memory footprint (~5-8 MB).

Usage:
    python3 uptime_server.py [--port PORT] [--bind ADDRESS]
"""

import json
import socket
import argparse
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler


def get_uptime_seconds():
    """Read system uptime from /proc/uptime (Linux only)."""
    with open("/proc/uptime", "r") as f:
        return float(f.read().split()[0])


def get_boot_time(uptime_seconds):
    """Calculate the boot time from the current time and uptime."""
    now = datetime.now(timezone.utc)
    boot_time = now - timedelta(seconds=uptime_seconds)
    return boot_time.isoformat()


class UptimeHandler(BaseHTTPRequestHandler):
    """HTTP request handler with a single /uptime endpoint."""

    # Suppress default stderr logging for each request to reduce noise.
    # Override log_message to use a simpler format.
    def log_message(self, format, *args):
        print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}] {args[0]}")

    def do_GET(self):
        if self.path == "/uptime" or self.path == "/uptime/":
            self._handle_uptime()
        elif self.path == "/health" or self.path == "/health/":
            self._handle_health()
        else:
            self._send_json({"error": "Not found"}, 404)

    def _handle_uptime(self):
        try:
            uptime_seconds = get_uptime_seconds()
            response = {
                "uptime_seconds": round(uptime_seconds, 2),
                "boot_time": get_boot_time(uptime_seconds),
                "hostname": socket.gethostname(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            self._send_json(response, 200)
        except FileNotFoundError:
            self._send_json(
                {"error": "/proc/uptime not available (not running on Linux?)"},
                500,
            )
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def _handle_health(self):
        """Simple health check endpoint."""
        self._send_json({"status": "ok"}, 200)

    def _send_json(self, data, status_code):
        response = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def main():
    parser = argparse.ArgumentParser(description="Blackout Logger — Uptime Server")
    parser.add_argument(
        "--port", type=int, default=8080, help="Port to listen on (default: 8080)"
    )
    parser.add_argument(
        "--bind",
        type=str,
        default="0.0.0.0",
        help="Address to bind to (default: 0.0.0.0)",
    )
    args = parser.parse_args()

    server = HTTPServer((args.bind, args.port), UptimeHandler)
    print(f"Uptime server running on {args.bind}:{args.port}")
    print(f"Hostname: {socket.gethostname()}")
    print(f"Endpoints: GET /uptime, GET /health")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
