#!/usr/bin/env python3
"""
Blackout Logger — Recompute resolved blackouts

Recomputes type, end time and duration of resolved blackouts from the stored
uptime logs, using the same rules as the live checker.

Usage:
    python3 recompute_blackouts.py           # dry run, prints the changes
    python3 recompute_blackouts.py --apply   # writes the changes
"""

import argparse
import logging

import blackout_checker
import database


def _first_reachable_log_after(conn, timestamp):
    return conn.execute(
        """SELECT * FROM uptime_logs
           WHERE was_reachable = 1 AND uptime_seconds IS NOT NULL AND timestamp > ?
           ORDER BY timestamp, id LIMIT 1""",
        (timestamp,),
    ).fetchone()


def compute_changes(conn):
    """Return a list of (blackout_row, new_values) for resolved blackouts that would change."""
    changes = []
    blackouts = conn.execute(
        "SELECT * FROM blackouts WHERE resolved = 1 AND blackout_type != ? ORDER BY id",
        (blackout_checker.STUCK_TYPE,),
    ).fetchall()
    for blackout in blackouts:
        recovery = _first_reachable_log_after(conn, blackout["started_at"])
        if recovery is None:
            continue
        blackout_type = blackout_checker._determine_blackout_type(
            blackout["last_known_boot_time"], recovery["boot_time"]
        )
        ended_at, duration = blackout_checker._outage_end(
            blackout["started_at"], blackout_type, recovery["boot_time"], recovery["timestamp"]
        )
        new_values = {"ended_at": ended_at, "duration_seconds": duration, "blackout_type": blackout_type}
        if any(blackout[key] != value for key, value in new_values.items()):
            changes.append((blackout, new_values))
    return changes


def apply_changes(conn, changes):
    for blackout, new_values in changes:
        conn.execute(
            """UPDATE blackouts SET ended_at = ?, duration_seconds = ?, blackout_type = ?
               WHERE id = ?""",
            (new_values["ended_at"], new_values["duration_seconds"], new_values["blackout_type"], blackout["id"]),
        )
    conn.commit()


def _describe(blackout, new_values):
    return (
        f"#{blackout['id']} start {blackout['started_at']}\n"
        f"    type     {blackout['blackout_type']} -> {new_values['blackout_type']}\n"
        f"    ended_at {blackout['ended_at']} -> {new_values['ended_at']}\n"
        f"    duration {blackout['duration_seconds']} -> {new_values['duration_seconds']:.0f}s"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes to the database")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    database.init_db()
    conn = database._connect()
    try:
        changes = compute_changes(conn)
        for blackout, new_values in changes:
            print(_describe(blackout, new_values))
        print(f"{len(changes)} blackout(s) to update")
        if args.apply and changes:
            apply_changes(conn, changes)
            print("Applied")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
