#!/usr/bin/env python3
"""
Blackout Logger — Database

SQLite database management for uptime logs and blackout records.
"""

import sqlite3
from datetime import datetime, timezone

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
    resolved BOOLEAN NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_uptime_logs_timestamp ON uptime_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_uptime_logs_reachable ON uptime_logs(was_reachable);
CREATE INDEX IF NOT EXISTS idx_blackouts_resolved ON blackouts(resolved);
"""


def _connect():
    """Create a database connection with WAL mode for better concurrency."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables and indexes if they don't exist."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


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


def create_blackout(detected_at, started_at, ended_at=None, duration_seconds=None):
    """
    Insert a new blackout record.

    Args:
        detected_at: ISO 8601 string of when the blackout was detected
        started_at: ISO 8601 string of estimated blackout start
        ended_at: ISO 8601 string of when power returned (None if ongoing)
        duration_seconds: estimated duration in seconds (None if ongoing)

    Returns:
        The ID of the inserted blackout record.
    """
    resolved = ended_at is not None
    conn = _connect()
    try:
        cursor = conn.execute(
            """INSERT INTO blackouts (detected_at, started_at, ended_at, duration_seconds, resolved)
               VALUES (?, ?, ?, ?, ?)""",
            (detected_at, started_at, ended_at, duration_seconds, resolved),
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


def resolve_blackout(blackout_id, ended_at, duration_seconds):
    """
    Mark a blackout as resolved.

    Args:
        blackout_id: ID of the blackout to resolve
        ended_at: ISO 8601 string of when power returned
        duration_seconds: total duration in seconds
    """
    conn = _connect()
    try:
        conn.execute(
            """UPDATE blackouts
               SET ended_at = ?, duration_seconds = ?, resolved = 1
               WHERE id = ?""",
            (ended_at, duration_seconds, blackout_id),
        )
        conn.commit()
    finally:
        conn.close()
