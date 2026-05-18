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

import logging
import os
import sys
import json
import time
import signal
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

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
        except (ValueError, IOError):
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
    except IOError:
        pass


# ---------------------------------------------------------------------------
# Pi API communication
# ---------------------------------------------------------------------------

def poll_pi():
    """
    Poll the Raspberry Pi's uptime API.

    Returns:
        dict with uptime data if reachable, None if unreachable.
    """
    url = f"http://{config.PI_ADDRESS}:{config.PI_PORT}/uptime"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=config.POLL_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as e:
        logger.warning("Pi unreachable: %s", e)
        return None


# ---------------------------------------------------------------------------
# Blackout detection logic
# ---------------------------------------------------------------------------

def _now_iso():
    """Get current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(iso_string):
    """Parse an ISO 8601 string to a datetime object."""
    # Handle both with and without timezone info
    if iso_string.endswith("Z"):
        iso_string = iso_string[:-1] + "+00:00"
    return datetime.fromisoformat(iso_string)


def _seconds_between(iso_start, iso_end):
    """Calculate seconds between two ISO 8601 timestamps."""
    start = _parse_iso(iso_start)
    end = _parse_iso(iso_end)
    return (end - start).total_seconds()


def handle_reachable(pi_data):
    """
    Handle the case where the Pi is reachable.

    Logs the uptime, checks for a reboot (blackout between crons),
    and resolves any ongoing blackout from a previous crashed retry loop.
    """
    now = _now_iso()
    uptime_seconds = pi_data["uptime_seconds"]
    boot_time = pi_data["boot_time"]

    # Fetch previous state BEFORE writing current entry — order matters.
    # get_last_successful_log() must run first, otherwise it would return
    # the record we're about to insert and boot_diff would always be 0.
    ongoing = database.get_ongoing_blackout()
    last_log = database.get_last_successful_log()

    # Log the current uptime
    database.log_uptime(now, uptime_seconds, boot_time, was_reachable=True)
    logger.info("Pi reachable — uptime: %.0fs, boot: %s", uptime_seconds, boot_time)

    # Check for ongoing blackout (from a previous crashed retry loop)
    if ongoing:
        duration = _seconds_between(ongoing["started_at"], boot_time)
        if duration < 0:
            duration = 0

        # Determine type: compare current boot_time with the one stored at outage start
        blackout_type = _determine_blackout_type(
            ongoing["last_known_boot_time"], boot_time
        )

        database.resolve_blackout(ongoing["id"], boot_time, duration, blackout_type)
        notifications.notify_blackout_resolved(
            ongoing["started_at"], boot_time, duration, blackout_type
        )
        logger.info(
            "Resolved ongoing blackout #%d (type: %s, duration: %.0fs)",
            ongoing["id"], blackout_type, duration,
        )
        return

    # Check for reboot between cron runs
    if last_log is None:
        # First run ever, nothing to compare against
        logger.info("First run — no previous data to compare")
        return

    previous_boot_time = last_log["boot_time"]

    if previous_boot_time is None:
        # Previous log didn't have boot_time (shouldn't happen, but be safe)
        logger.warning("Previous log has no boot_time, skipping comparison")
        return

    # Compare boot times — if they differ by more than 30 seconds, a reboot happened
    try:
        prev_boot = _parse_iso(previous_boot_time)
        curr_boot = _parse_iso(boot_time)
        boot_diff = abs((curr_boot - prev_boot).total_seconds())
    except (ValueError, TypeError) as e:
        logger.warning("Could not compare boot times: %s", e)
        return

    if boot_diff > 30:
        # Reboot detected — a blackout happened between cron runs (always power)
        started_at = last_log["timestamp"]  # last time we saw the Pi alive
        ended_at = boot_time                 # when the Pi booted back up
        duration = _seconds_between(started_at, ended_at)

        # Duration could be negative if clocks are slightly off; clamp to 0
        if duration < 0:
            duration = 0

        database.create_blackout(
            now, started_at, ended_at, duration,
            blackout_type="power",
            last_known_boot_time=previous_boot_time,
        )
        notifications.notify_blackout_detected_resolved(started_at, ended_at, duration)
        logger.info("Power outage detected between crons! ~%.0fs (boot_diff: %.0fs)", duration, boot_diff)


def _determine_blackout_type(last_known_boot_time, current_boot_time):
    """
    Compare boot times to determine whether the outage was a power cut or internet disruption.

    Returns:
        'power'    — boot time changed, Pi rebooted (power grid outage)
        'internet' — boot time unchanged, Pi never went down (network disruption)
        'unknown'  — could not compare (missing data)
    """
    if not last_known_boot_time or not current_boot_time:
        return "unknown"
    try:
        prev = _parse_iso(last_known_boot_time)
        curr = _parse_iso(current_boot_time)
        boot_diff = abs((curr - prev).total_seconds())
        return "power" if boot_diff > 30 else "internet"
    except (ValueError, TypeError):
        return "unknown"


def handle_unreachable():
    """
    Handle the case where the Pi is unreachable.

    Creates a blackout record (if none ongoing), sends a notification,
    and enters a retry loop until the Pi comes back or MAX_RETRY_DURATION is exceeded.
    """
    now = _now_iso()

    # Log the failed attempt
    database.log_uptime(now, None, None, was_reachable=False)

    # Check if there's already an ongoing blackout
    ongoing = database.get_ongoing_blackout()
    last_successful = database.get_last_successful_log()
    started_at = last_successful["timestamp"] if last_successful else now
    last_known_boot_time = last_successful["boot_time"] if last_successful else None

    if ongoing is None:
        # New blackout — create record and notify
        blackout_id = database.create_blackout(
            detected_at=now,
            started_at=started_at,
            last_known_boot_time=last_known_boot_time,
        )
        notifications.notify_blackout_in_progress(started_at)
        logger.info("Outage detected! Pi unreachable. Created blackout #%d", blackout_id)
    else:
        blackout_id = ongoing["id"]
        started_at = ongoing["started_at"]
        last_known_boot_time = ongoing["last_known_boot_time"]
        logger.info("Continuing to monitor ongoing blackout #%d", blackout_id)

    # Enter retry loop
    retry_start = time.time()

    while True:
        elapsed = time.time() - retry_start
        if elapsed >= config.MAX_RETRY_DURATION:
            logger.warning("Max retry duration (%ds) reached, exiting", config.MAX_RETRY_DURATION)
            logger.info("Next cron run will pick up monitoring")
            break

        logger.info("Retrying in %ds... (elapsed: %.0fs)", config.RETRY_INTERVAL, elapsed)
        time.sleep(config.RETRY_INTERVAL)

        pi_data = poll_pi()
        retry_now = _now_iso()

        if pi_data is not None:
            # Pi is back!
            boot_time = pi_data["boot_time"]
            uptime_seconds = pi_data["uptime_seconds"]

            database.log_uptime(retry_now, uptime_seconds, boot_time, was_reachable=True)

            # Determine outage type: compare boot times
            blackout_type = _determine_blackout_type(last_known_boot_time, boot_time)

            duration = _seconds_between(started_at, boot_time)
            if duration < 0:
                duration = 0

            database.resolve_blackout(blackout_id, boot_time, duration, blackout_type)
            notifications.notify_blackout_resolved(started_at, boot_time, duration, blackout_type)
            logger.info(
                "Outage resolved! Blackout #%d (type: %s, duration: %.0fs)",
                blackout_id, blackout_type, duration,
            )
            break
        else:
            # Still down, log the attempt
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
        pi_data = poll_pi()

        if pi_data is not None:
            handle_reachable(pi_data)
        else:
            handle_unreachable()
    except Exception as e:
        logger.critical("Unexpected error: %s", e, exc_info=True)
    finally:
        release_lock()
        logger.info("Blackout Checker finished")


if __name__ == "__main__":
    main()
