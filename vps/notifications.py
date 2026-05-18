#!/usr/bin/env python3
"""
Blackout Logger — Telegram Notifications

Sends formatted Telegram messages for blackout events.
Uses urllib (stdlib) — no dependencies required.
"""

import json
import urllib.request
import urllib.error

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def _send_telegram(message):
    """
    Send a message via Telegram Bot API.

    Args:
        message: The message text (supports Markdown formatting)

    Returns:
        True if sent successfully, False otherwise.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram not configured, skipping notification")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            if response.status == 200:
                print("[INFO] Telegram notification sent")
                return True
            else:
                print(f"[WARN] Telegram API returned status {response.status}")
                return False
    except urllib.error.URLError as e:
        print(f"[ERROR] Failed to send Telegram notification: {e}")
        return False
    except Exception as e:
        print(f"[ERROR] Unexpected error sending Telegram notification: {e}")
        return False


def _format_duration(seconds):
    """Format a duration in seconds to a human-readable string."""
    if seconds is None:
        return "unknown"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} seconds"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        if secs == 0:
            return f"{minutes} minutes"
        return f"{minutes} min {secs} sec"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if minutes == 0:
            return f"{hours} hours"
        return f"{hours} hr {minutes} min"


def _format_time(iso_string):
    """Format an ISO 8601 timestamp to a more readable format."""
    if not iso_string:
        return "unknown"
    # Simple formatting: replace T with space, remove microseconds
    formatted = iso_string.replace("T", " ")
    if "." in formatted:
        formatted = formatted.split(".")[0] + " UTC"
    elif "+" in formatted:
        formatted = formatted.split("+")[0] + " UTC"
    return formatted


def notify_blackout_detected_resolved(started_at, ended_at, duration_seconds):
    """
    Notification type 1: Blackout happened between cron runs but Pi is already back up.

    This means we detected a reboot (uptime reset) but the Pi is currently reachable.
    """
    message = (
        "⚡ *Power Outage Detected*\n"
        "\n"
        "A blackout occurred while monitoring was idle.\n"
        "\n"
        f"📅 Started: ~{_format_time(started_at)}\n"
        f"📅 Ended: ~{_format_time(ended_at)}\n"
        f"⏱ Duration: ~{_format_duration(duration_seconds)}\n"
        "\n"
        "✅ Power has been restored."
    )
    return _send_telegram(message)


def notify_blackout_in_progress(last_seen):
    """
    Notification type 2: Pi is currently unreachable — blackout is happening now.
    """
    message = (
        "🔴 *Ongoing Power Outage*\n"
        "\n"
        "The Raspberry Pi is unreachable.\n"
        "\n"
        f"📅 Last seen online: {_format_time(last_seen)}\n"
        "⏱ Monitoring every 60 seconds..."
    )
    return _send_telegram(message)


def notify_blackout_resolved(started_at, ended_at, duration_seconds):
    """
    Notification type 3: Pi came back online after being unreachable (exiting retry loop).
    """
    message = (
        "🟢 *Power Restored*\n"
        "\n"
        "The Raspberry Pi is back online.\n"
        "\n"
        f"📅 Started: ~{_format_time(started_at)}\n"
        f"📅 Ended: ~{_format_time(ended_at)}\n"
        f"⏱ Duration: ~{_format_duration(duration_seconds)}"
    )
    return _send_telegram(message)
