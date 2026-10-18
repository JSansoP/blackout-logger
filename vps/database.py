#!/usr/bin/env python3
"""
Blackout Logger — Database

SQLite database management for uptime logs and blackout records.
"""

import contextlib
import sqlite3
from datetime import datetime, timedelta

from config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS uptime_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    uptime_seconds REAL,
    boot_time TEXT,
    was_reachable BOOLEAN NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS blackouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_seconds REAL,
    resolved BOOLEAN NOT NULL DEFAULT 0,
    blackout_type TEXT NOT NULL DEFAULT 'unknown',
    last_known_boot_time TEXT
);

CREATE INDEX IF NOT EXISTS idx_uptime_logs_timestamp ON uptime_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_uptime_logs_reachable ON uptime_logs(was_reachable);
CREATE INDEX IF NOT EXISTS idx_blackouts_resolved ON blackouts(resolved);
"""

# Migrations for existing databases
_MIGRATIONS = [
    "ALTER TABLE blackouts ADD COLUMN blackout_type TEXT NOT NULL DEFAULT 'unknown'",
    "ALTER TABLE blackouts ADD COLUMN last_known_boot_time TEXT",
]


def _connect():
    """Create a database connection with WAL mode for better concurrency."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables and indexes if they don't exist. Run migrations for existing DBs."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        _add_missing_columns(conn)
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            _recompute_boot_times_from_vps_clock(conn)
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
    finally:
        conn.close()


def _add_missing_columns(conn):
    """Apply column migrations; SQLite raises OperationalError for columns that already exist."""
    for migration in _MIGRATIONS:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute(migration)
            conn.commit()


def _recompute_boot_times_from_vps_clock(conn):
    """Rewrite stored boot times as poll timestamp minus uptime, both from the VPS clock."""
    rows = conn.execute(
        """SELECT id, timestamp, uptime_seconds FROM uptime_logs
           WHERE was_reachable = 1 AND uptime_seconds IS NOT NULL"""
    ).fetchall()
    for row in rows:
        boot_dt = datetime.fromisoformat(row["timestamp"]) - timedelta(seconds=row["uptime_seconds"])
        conn.execute(
            "UPDATE uptime_logs SET boot_time = ? WHERE id = ?",
            (boot_dt.isoformat(), row["id"]),
        )
    conn.execute(
        """UPDATE blackouts SET last_known_boot_time = (
               SELECT boot_time FROM uptime_logs
               WHERE uptime_logs.timestamp = blackouts.started_at AND was_reachable = 1
               ORDER BY id DESC LIMIT 1)
           WHERE EXISTS (
               SELECT 1 FROM uptime_logs
               WHERE uptime_logs.timestamp = blackouts.started_at AND was_reachable = 1)"""
    )


def log_uptime(timestamp, uptime_seconds, boot_time, was_reachable=True):
    """
    Insert an uptime log entry.

    Args:
        timestamp: ISO 8601 string of when the poll happened
        uptime_seconds: uptime returned by Pi (None if unreachable)
        boot_time: calculated boot time ISO string (None if unreachable)
        was_reachable: whether the Pi responded
    """
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO uptime_logs (timestamp, uptime_seconds, boot_time, was_reachable)
               VALUES (?, ?, ?, ?)""",
            (timestamp, uptime_seconds, boot_time, was_reachable),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_successful_log():
    """
    Get the most recent uptime log where the Pi was reachable.

    Returns:
        sqlite3.Row with (id, timestamp, uptime_seconds, boot_time, was_reachable)
        or None if no successful logs exist.
    """
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT * FROM uptime_logs
               WHERE was_reachable = 1
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        return row
    finally:
        conn.close()


def create_blackout(
    detected_at,
    started_at,
    ended_at=None,
    duration_seconds=None,
    blackout_type="unknown",
    last_known_boot_time=None,
):
    """
    Insert a new blackout record.

    Args:
        detected_at: ISO 8601 string of when the blackout was detected
        started_at: ISO 8601 string of estimated blackout start
        ended_at: ISO 8601 string of when power returned (None if ongoing)
        duration_seconds: estimated duration in seconds (None if ongoing)
        blackout_type: 'power', 'internet', or 'unknown'
        last_known_boot_time: boot_time of Pi when last seen (used to determine type on recovery)

    Returns:
        The ID of the inserted blackout record.
    """
    resolved = ended_at is not None
    conn = _connect()
    try:
        cursor = conn.execute(
            """INSERT INTO blackouts
               (detected_at, started_at, ended_at, duration_seconds, resolved, blackout_type, last_known_boot_time)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (detected_at, started_at, ended_at, duration_seconds, resolved, blackout_type, last_known_boot_time),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_ongoing_blackout():
    """
    Get any unresolved blackout record.

    Returns:
        sqlite3.Row or None.
    """
    conn = _connect()
    try:
        row = conn.execute(
            """SELECT * FROM blackouts
               WHERE resolved = 0
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        return row
    finally:
        conn.close()


def get_latest_blackout():
    """
    Get the most recently created blackout record, resolved or not.

    Returns:
        sqlite3.Row or None.
    """
    conn = _connect()
    try:
        return conn.execute("SELECT * FROM blackouts ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()


def resolve_blackout(blackout_id, ended_at, duration_seconds, blackout_type="unknown"):
    """
    Mark a blackout as resolved.

    Args:
        blackout_id: ID of the blackout to resolve
        ended_at: ISO 8601 string of when power returned
        duration_seconds: total duration in seconds
        blackout_type: 'power', 'internet', or 'unknown'
    """
    conn = _connect()
    try:
        conn.execute(
            """UPDATE blackouts
               SET ended_at = ?, duration_seconds = ?, resolved = 1, blackout_type = ?
               WHERE id = ?""",
            (ended_at, duration_seconds, blackout_type, blackout_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_log():
    """Get the most recent uptime log, reachable or not, or None."""
    conn = _connect()
    try:
        return conn.execute("SELECT * FROM uptime_logs ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()


def get_first_successful_log_after(iso_timestamp):
    """Get the earliest reachable uptime log strictly after a timestamp, or None."""
    conn = _connect()
    try:
        return conn.execute(
            """SELECT * FROM uptime_logs
               WHERE was_reachable = 1 AND timestamp > ?
               ORDER BY timestamp ASC LIMIT 1""",
            (iso_timestamp,),
        ).fetchone()
    finally:
        conn.close()


def get_blackouts_since(iso_timestamp, limit):
    """Get blackouts that started at or after a timestamp, or are still open, newest first."""
    conn = _connect()
    try:
        return conn.execute(
            """SELECT * FROM blackouts
               WHERE started_at >= ? OR resolved = 0
               ORDER BY started_at DESC LIMIT ?""",
            (iso_timestamp, limit),
        ).fetchall()
    finally:
        conn.close()
