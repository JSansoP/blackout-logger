#!/usr/bin/env python3
"""
Blackout Logger — Blackout Checker

Main entry point for the VPS cron job. Polls the Raspberry Pi's uptime API,
detects power outages, and sends Telegram notifications.

Usage:
    python3 blackout_checker.py

Designed to be run as a cron job periodically (default every 10 minutes):
    */10 * * * * cd /path/to/vps && python3 blackout_checker.py >> /var/log/blackout_checker.log 2>&1
"""

import json
import logging
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import config
import database
import notifications

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lock file management
# ---------------------------------------------------------------------------

def _is_process_alive(pid):
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock():
    """
    Acquire the PID-based lock file.

    Returns:
        True if lock was acquired, False if another instance is running.
    """
    lock_file = config.LOCK_FILE

    if os.path.exists(lock_file):
        try:
            with open(lock_file, "r") as f:
                old_pid = int(f.read().strip())

            if _is_process_alive(old_pid):
                logger.info("Another instance is running (PID %d), exiting", old_pid)
                return False
            else:
                logger.warning("Stale lock file found (PID %d is dead), cleaning up", old_pid)
                os.remove(lock_file)
        except (OSError, ValueError):
            # Corrupted lock file, remove it
            logger.warning("Corrupted lock file, cleaning up")
            os.remove(lock_file)

    # Write our PID
    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))

    return True


def release_lock():
    """Remove the lock file."""
    try:
        if os.path.exists(config.LOCK_FILE):
            os.remove(config.LOCK_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Pi API communication
# ---------------------------------------------------------------------------

REBOOT_THRESHOLD_SECONDS = 30


class InvalidPiResponse(Exception):
    """The Pi answered, but the response cannot be used to read its uptime."""


def _parse_uptime(data):
    """Extract a valid uptime in seconds from the Pi's JSON payload."""
    if not isinstance(data, dict):
        raise InvalidPiResponse(f"unexpected payload: {data!r}")
    uptime = data.get("uptime_seconds")
    if isinstance(uptime, bool) or not isinstance(uptime, (int, float)):
        raise InvalidPiResponse(f"invalid uptime_seconds: {uptime!r}")
    if not math.isfinite(uptime) or uptime < 0:
        raise InvalidPiResponse(f"invalid uptime_seconds: {uptime!r}")
    return float(uptime)


def poll_pi():
    """
    Poll the Raspberry Pi's uptime API.

    Returns:
        Uptime in seconds if the Pi answered correctly, None if it is unreachable.

    Raises:
        InvalidPiResponse: the Pi answered with an HTTP error or an unusable body.
    """
    url = f"http://{config.PI_ADDRESS}:{config.PI_PORT}/uptime"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=config.POLL_TIMEOUT) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise InvalidPiResponse(f"HTTP {e.code}") from e
    except (urllib.error.URLError, OSError) as e:
        logger.warning("Pi unreachable: %s", e)
        return None
    except UnicodeDecodeError as e:
        raise InvalidPiResponse(f"undecodable body: {e}") from e
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise InvalidPiResponse(f"malformed JSON: {e}") from e
    return _parse_uptime(data)


# ---------------------------------------------------------------------------
# Blackout detection logic
# ---------------------------------------------------------------------------

def _now_iso():
    """Get current UTC time as ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _parse_iso(iso_string):
    """Parse an ISO 8601 string to a datetime object."""
    if iso_string.endswith("Z"):
        iso_string = iso_string[:-1] + "+00:00"
    return datetime.fromisoformat(iso_string)


def _seconds_between(iso_start, iso_end):
    """Calculate seconds between two ISO 8601 timestamps."""
    return (_parse_iso(iso_end) - _parse_iso(iso_start)).total_seconds()


def _calculate_boot_time(now_iso, uptime_seconds):
    """
    Calculate the Pi's boot time as VPS poll time minus Pi uptime.

    All stored timestamps come from the VPS clock, so they are comparable with
    each other regardless of the Pi's (battery-less) system clock.
    """
    boot_dt = _parse_iso(now_iso) - timedelta(seconds=uptime_seconds)
    return boot_dt.isoformat()


def _pi_rebooted(previous_boot_time, current_boot_time):
    """
    Return True if the boot time moved by more than the threshold, False if not,
    or None if the boot times are missing or unparsable.
    """
    if not previous_boot_time or not current_boot_time:
        return None
    try:
        boot_diff = abs(_seconds_between(previous_boot_time, current_boot_time))
    except (ValueError, TypeError) as e:
        logger.warning("Could not compare boot times: %s", e)
        return None
    return boot_diff > REBOOT_THRESHOLD_SECONDS


def _determine_blackout_type(last_known_boot_time, current_boot_time):
    """
    Classify an outage from the boot times before and after it.

    Returns:
        'power'    — the Pi rebooted (power grid outage)
        'internet' — the Pi kept running (network disruption)
        'unknown'  — could not compare (missing data)
    """
    rebooted = _pi_rebooted(last_known_boot_time, current_boot_time)
    if rebooted is None:
        return "unknown"
    return "power" if rebooted else "internet"


def _outage_end(started_at, blackout_type, boot_time, now):
    """
    Return (ended_at, duration_seconds) for an outage.

    Power outages end at the Pi's boot time, other outages when the Pi is seen again.
    If the boot time precedes the start, the poll time is used as an upper bound.
    """
    ended_at = boot_time if blackout_type == "power" else now
    duration = _seconds_between(started_at, ended_at)
    if duration < 0:
        logger.warning(
            "Outage end %s precedes start %s; using poll time %s as end",
            ended_at, started_at, now,
        )
        ended_at = now
        duration = max(_seconds_between(started_at, now), 0.0)
    return ended_at, duration


def _resolve_blackout(blackout, ended_at, duration, blackout_type):
    """Mark a blackout as resolved and notify."""
    database.resolve_blackout(blackout["id"], ended_at, duration, blackout_type)
    notifications.notify_blackout_resolved(blackout["started_at"], ended_at, duration, blackout_type)
    logger.info(
        "Resolved blackout #%d (type: %s, duration: %.0fs)",
        blackout["id"], blackout_type, duration,
    )


def _resolve_with_uptime(blackout, uptime_seconds, now):
    """Resolve a blackout using the uptime read when the Pi came back."""
    boot_time = _calculate_boot_time(now, uptime_seconds)
    blackout_type = _determine_blackout_type(blackout["last_known_boot_time"], boot_time)
    ended_at, duration = _outage_end(blackout["started_at"], blackout_type, boot_time, now)
    _resolve_blackout(blackout, ended_at, duration, blackout_type)


def _resolve_without_uptime(blackout, now):
    """Resolve a blackout when the Pi answers but its uptime is unavailable."""
    duration = max(_seconds_between(blackout["started_at"], now), 0.0)
    _resolve_blackout(blackout, now, duration, "unknown")


def handle_reachable(uptime_seconds):
    """
    Handle the case where the Pi is reachable.

    Logs the uptime, checks for a reboot (blackout between crons),
    and resolves any ongoing blackout from a previous crashed retry loop.
    """
    now = _now_iso()
    boot_time = _calculate_boot_time(now, uptime_seconds)

    ongoing = database.get_ongoing_blackout()
    last_log = database.get_last_successful_log()

    database.log_uptime(now, uptime_seconds, boot_time, was_reachable=True)
    logger.info("Pi reachable — uptime: %.0fs, boot: %s", uptime_seconds, boot_time)

    if ongoing:
        _resolve_with_uptime(ongoing, uptime_seconds, now)
        return

    if last_log is None:
        logger.info("First run — no previous data to compare")
        return

    previous_boot_time = last_log["boot_time"]
    if not _pi_rebooted(previous_boot_time, boot_time):
        return

    started_at = last_log["timestamp"]
    ended_at, duration = _outage_end(started_at, "power", boot_time, now)
    database.create_blackout(
        now, started_at, ended_at, duration,
        blackout_type="power",
        last_known_boot_time=previous_boot_time,
    )
    notifications.notify_blackout_detected_resolved(started_at, ended_at, duration)
    logger.info("Power outage detected between crons! ~%.0fs", duration)


def handle_invalid_response(error):
    """
    Handle a Pi that answers with an unusable response.

    The Pi is online, so an ongoing blackout is resolved with an unknown type.
    """
    now = _now_iso()
    logger.error("Pi answered with an invalid response: %s", error)
    ongoing = database.get_ongoing_blackout()
    if ongoing:
        _resolve_without_uptime(ongoing, now)


def handle_unreachable():
    """
    Handle the case where the Pi is unreachable.

    Creates a blackout record (if none ongoing), sends a notification,
    and enters a retry loop until the Pi comes back or MAX_RETRY_DURATION is exceeded.
    """
    now = _now_iso()
    database.log_uptime(now, None, None, was_reachable=False)

    ongoing = database.get_ongoing_blackout()
    if ongoing is None:
        last_successful = database.get_last_successful_log()
        started_at = last_successful["timestamp"] if last_successful else now
        last_known_boot_time = last_successful["boot_time"] if last_successful else None
        blackout_id = database.create_blackout(
            detected_at=now,
            started_at=started_at,
            last_known_boot_time=last_known_boot_time,
        )
        notifications.notify_blackout_in_progress(started_at)
        logger.info("Outage detected! Pi unreachable. Created blackout #%d", blackout_id)
        ongoing = database.get_ongoing_blackout()
    else:
        logger.info("Continuing to monitor ongoing blackout #%d", ongoing["id"])

    retry_start = time.time()

    while True:
        elapsed = time.time() - retry_start
        if elapsed >= config.MAX_RETRY_DURATION:
            logger.warning("Max retry duration (%ds) reached, exiting", config.MAX_RETRY_DURATION)
            logger.info("Next cron run will pick up monitoring")
            break

        logger.info("Retrying in %ds... (elapsed: %.0fs)", config.RETRY_INTERVAL, elapsed)
        time.sleep(config.RETRY_INTERVAL)

        try:
            uptime_seconds = poll_pi()
        except InvalidPiResponse as e:
            logger.error("Pi answered with an invalid response: %s", e)
            _resolve_without_uptime(ongoing, _now_iso())
            break
        retry_now = _now_iso()

        if uptime_seconds is not None:
            boot_time = _calculate_boot_time(retry_now, uptime_seconds)
            database.log_uptime(retry_now, uptime_seconds, boot_time, was_reachable=True)
            _resolve_with_uptime(ongoing, uptime_seconds, retry_now)
            break

        database.log_uptime(retry_now, None, None, was_reachable=False)
        logger.info("Pi still unreachable...")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("Blackout Checker starting")

    # Validate configuration
    try:
        config.validate()
    except ValueError as e:
        logger.critical("Configuration error: %s", e)
        sys.exit(1)

    # Initialize database
    database.init_db()

    # Acquire lock
    if not acquire_lock():
        sys.exit(0)

    # Handle signals for clean exit
    def signal_handler(signum, frame):
        logger.info("Received signal %d, cleaning up...", signum)
        release_lock()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        # Poll the Pi
        try:
            uptime_seconds = poll_pi()
        except InvalidPiResponse as e:
            handle_invalid_response(e)
            return

        if uptime_seconds is not None:
            handle_reachable(uptime_seconds)
        else:
            handle_unreachable()
    except Exception as e:
        logger.critical("Unexpected error: %s", e, exc_info=True)
    finally:
        release_lock()
        logger.info("Blackout Checker finished")


if __name__ == "__main__":
    main()
