"""
Blackout Logger - heartbeat pings to heartbeat-monitor.

Sending a heartbeat must never break or slow down the checker: short timeout,
every error is caught and only logged, and nothing is sent when HEARTBEAT_URL
is unset.
"""

import logging
import urllib.request

import config

logger = logging.getLogger(__name__)

TIMEOUT = 5


def send(suffix=""):
    """GET HEARTBEAT_URL, or HEARTBEAT_URL/<suffix> ("start", "fail"). Returns True on HTTP 200."""
    if not config.HEARTBEAT_URL:
        return False
    url = config.HEARTBEAT_URL.rstrip("/") + (f"/{suffix}" if suffix else "")
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return response.status == 200
    except Exception as e:  # noqa: BLE001
        logger.warning("Heartbeat %s failed: %s", suffix or "success", e)
        return False
