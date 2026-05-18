#!/usr/bin/env python3
"""
Blackout Logger — Telegram Notifications

Sends formatted Telegram messages for blackout events.
Uses urllib (stdlib) — no dependencies required.
"""

import json
import logging
import urllib.request
import urllib.error

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


def _send_telegram(message):
    """
    Send a message via Telegram Bot API.

    Args:
        message: The message text (supports Markdown formatting)

    Returns:
        True if sent successfully, False otherwise.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram not configured, skipping notification")
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
                logger.info("Telegram notification sent")
                return True
            else:
                logger.warning("Telegram API returned status %d", response.status)
                return False
    except urllib.error.URLError as e:
        logger.error("Failed to send Telegram notification: %s", e)
        return False
    except Exception as e:
        logger.error("Unexpected error sending Telegram notification: %s", e)
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
    # Simple formatting: replace T with space, remove microseconds/timezone
    formatted = iso_string.replace("T", " ")
    if "." in formatted:
        formatted = formatted.split(".")[0] + " UTC"
    elif "+" in formatted:
        formatted = formatted.split("+")[0] + " UTC"
    return formatted


def notify_blackout_detected_resolved(started_at, ended_at, duration_seconds):
    """
    Notification type 1: Blackout happened between cron runs, Pi already back up.

    This is always a power grid outage — we know because the Pi's boot time changed.
    """
    message = (
        "⚡ *Power Grid Outage Detected*\n"
        "\n"
        "The Pi rebooted while monitoring was idle.\n"
        "\n"
        f"🔴 Outage started: ~{_format_time(started_at)}\n"
        f"🟢 Power restored: ~{_format_time(ended_at)}\n"
        f"⏱ Duration: ~{_format_duration(duration_seconds)}\n"
        "\n"
        "✅ Everything is back to normal."
    )
    return _send_telegram(message)


def notify_blackout_in_progress(last_seen):
    """
    Notification type 2: Pi is currently unreachable — outage is happening now.

    The type (power vs internet) is not yet known.
    """
    message = (
        "🔴 *Ongoing Outage Detected*\n"
        "\n"
        "The Raspberry Pi is unreachable.\n"
        "\n"
        f"🕐 Last seen online: {_format_time(last_seen)}\n"
        "⏱ Monitoring every 60 seconds..."
    )
    return _send_telegram(message)


def notify_blackout_resolved(started_at, ended_at, duration_seconds, blackout_type="unknown"):
    """
    Notification type 3: Pi came back online after being unreachable (retry loop resolved).

    blackout_type determines the tone and content of the message:
      - 'power':    Pi rebooted — actual power outage.
      - 'internet': Pi uptime unchanged — connectivity/internet disruption.
      - 'unknown':  Could not determine type.
    """
    if blackout_type == "power":
        header = "🟢 *Power Restored*"
        explanation = "The Pi rebooted — this was a *power grid outage*."
        start_label = "🔴 Outage started"
        end_label = "🟢 Power restored"
    elif blackout_type == "internet":
        header = "🌐 *Connectivity Restored*"
        explanation = "The Pi never rebooted — this was a *network/internet disruption*."
        start_label = "🔴 Disruption started"
        end_label = "🟢 Connectivity restored"
    else:
        header = "🟢 *Outage Resolved*"
        explanation = "The Raspberry Pi is back online."
        start_label = "🔴 Outage started"
        end_label = "🟢 Resolved"

    message = (
        f"{header}\n"
        "\n"
        f"{explanation}\n"
        "\n"
        f"{start_label}: ~{_format_time(started_at)}\n"
        f"{end_label}: ~{_format_time(ended_at)}\n"
        f"⏱ Duration: ~{_format_duration(duration_seconds)}"
    )
    return _send_telegram(message)
