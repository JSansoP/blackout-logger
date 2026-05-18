#!/usr/bin/env python3
"""
Blackout Logger — Blackout Checker

Main entry point for the VPS cron job. Polls the Raspberry Pi's uptime API,
detects power outages, and sends Telegram notifications.

Usage:
    python3 blackout_checker.py

Designed to be run as a cron job every 5 minutes:
    */5 * * * * cd /path/to/vps && python3 blackout_checker.py >> /var/log/blackout_checker.log 2>&1
"""

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
                print(f"[INFO] Another instance is running (PID {old_pid}), exiting")
                return False
            else:
                print(f"[WARN] Stale lock file found (PID {old_pid} is dead), cleaning up")
                os.remove(lock_file)
        except (ValueError, IOError):
            # Corrupted lock file, remove it
            print("[WARN] Corrupted lock file, cleaning up")
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
        print(f"[WARN] Pi unreachable: {e}")
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

    # Log the uptime
    database.log_uptime(now, uptime_seconds, boot_time, was_reachable=True)
    print(f"[INFO] Pi reachable — uptime: {uptime_seconds:.0f}s, boot: {boot_time}")

    # Check for ongoing blackout (from a previous crashed retry loop)
    ongoing = database.get_ongoing_blackout()
    if ongoing:
        duration = _seconds_between(ongoing["started_at"], boot_time)
        database.resolve_blackout(ongoing["id"], boot_time, duration)
        notifications.notify_blackout_resolved(
            ongoing["started_at"], boot_time, duration
        )
        print(f"[INFO] Resolved ongoing blackout #{ongoing['id']} (duration: {duration:.0f}s)")
        return

    # Check for reboot between cron runs
    last_log = database.get_last_successful_log()

    if last_log is None:
        # First run ever, nothing to compare against
        print("[INFO] First run — no previous data to compare")
        return

    previous_boot_time = last_log["boot_time"]

    if previous_boot_time is None:
        # Previous log didn't have boot_time (shouldn't happen, but be safe)
        print("[WARN] Previous log has no boot_time, skipping comparison")
        return

    # Compare boot times — if they differ by more than 30 seconds, a reboot happened
    try:
        prev_boot = _parse_iso(previous_boot_time)
        curr_boot = _parse_iso(boot_time)
        boot_diff = abs((curr_boot - prev_boot).total_seconds())
    except (ValueError, TypeError) as e:
        print(f"[WARN] Could not compare boot times: {e}")
        return

    if boot_diff > 30:
        # Reboot detected — a blackout happened between cron runs
        started_at = last_log["timestamp"]  # last time we saw the Pi alive
        ended_at = boot_time  # when the Pi booted back up
        duration = _seconds_between(started_at, ended_at)

        # Duration could be negative if clocks are slightly off; clamp to 0
        if duration < 0:
            duration = 0

        database.create_blackout(now, started_at, ended_at, duration)
        notifications.notify_blackout_detected_resolved(started_at, ended_at, duration)
        print(f"[INFO] Blackout detected between crons! ~{duration:.0f}s (boot_diff: {boot_diff:.0f}s)")


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

    if ongoing is None:
        # New blackout — create record and notify
        blackout_id = database.create_blackout(
            detected_at=now,
            started_at=started_at,
        )
        notifications.notify_blackout_in_progress(started_at)
        print(f"[INFO] Blackout detected! Pi unreachable. Created blackout #{blackout_id}")
    else:
        blackout_id = ongoing["id"]
        started_at = ongoing["started_at"]
        print(f"[INFO] Continuing to monitor ongoing blackout #{blackout_id}")

    # Enter retry loop
    retry_start = time.time()

    while True:
        elapsed = time.time() - retry_start
        if elapsed >= config.MAX_RETRY_DURATION:
            print(f"[WARN] Max retry duration ({config.MAX_RETRY_DURATION}s) reached, exiting")
            print("[INFO] Next cron run will pick up monitoring")
            break

        print(f"[INFO] Retrying in {config.RETRY_INTERVAL}s... (elapsed: {elapsed:.0f}s)")
        time.sleep(config.RETRY_INTERVAL)

        pi_data = poll_pi()
        retry_now = _now_iso()

        if pi_data is not None:
            # Pi is back!
            boot_time = pi_data["boot_time"]
            uptime_seconds = pi_data["uptime_seconds"]

            database.log_uptime(retry_now, uptime_seconds, boot_time, was_reachable=True)

            duration = _seconds_between(started_at, boot_time)
            if duration < 0:
                duration = 0

            database.resolve_blackout(blackout_id, boot_time, duration)
            notifications.notify_blackout_resolved(started_at, boot_time, duration)
            print(f"[INFO] Power restored! Blackout #{blackout_id} resolved (duration: {duration:.0f}s)")
            break
        else:
            # Still down, log the attempt
            database.log_uptime(retry_now, None, None, was_reachable=False)
            print(f"[INFO] Pi still unreachable...")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\n{'='*60}")
    print(f"[{_now_iso()}] Blackout Checker starting")
    print(f"{'='*60}")

    # Validate configuration
    try:
        config.validate()
    except ValueError as e:
        print(f"[FATAL] {e}")
        sys.exit(1)

    # Initialize database
    database.init_db()

    # Acquire lock
    if not acquire_lock():
        sys.exit(0)

    # Handle signals for clean exit
    def signal_handler(signum, frame):
        print(f"\n[INFO] Received signal {signum}, cleaning up...")
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
        print(f"[FATAL] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        release_lock()
        print(f"[{_now_iso()}] Blackout Checker finished")


if __name__ == "__main__":
    main()
