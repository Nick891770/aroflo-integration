"""
SQLite database for tracking processed tasks and poll run history.

Prevents re-processing tasks that have already been proofread and marked
Ready to Invoice. Also stores a log of every poll run for debugging.
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional


DB_PATH = os.path.join(os.path.dirname(__file__), "job_monitor.db")


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Get a database connection, creating tables if needed."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection):
    """Create tables if they don't exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS processed_tasks (
            task_id         TEXT PRIMARY KEY,
            task_name       TEXT,
            job_number      TEXT,
            processed_at    TEXT NOT NULL,
            description_fixed   INTEGER DEFAULT 0,
            notes_needing_fix   INTEGER DEFAULT 0,
            errors_found        INTEGER DEFAULT 0,
            marked_ready        INTEGER DEFAULT 0,
            note_added          INTEGER DEFAULT 0,
            run_id              INTEGER
        );

        CREATE TABLE IF NOT EXISTS poll_runs (
            run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TEXT NOT NULL,
            completed_at    TEXT,
            tasks_found     INTEGER DEFAULT 0,
            tasks_new       INTEGER DEFAULT 0,
            tasks_processed INTEGER DEFAULT 0,
            tasks_skipped   INTEGER DEFAULT 0,
            errors          TEXT,
            mode            TEXT DEFAULT 'one-shot'
        );

        CREATE INDEX IF NOT EXISTS idx_processed_at
            ON processed_tasks(processed_at);
        CREATE INDEX IF NOT EXISTS idx_job_number
            ON processed_tasks(job_number);
    """)
    conn.commit()


def is_processed(conn: sqlite3.Connection, task_id: str) -> bool:
    """Check if a task has already been processed."""
    row = conn.execute(
        "SELECT 1 FROM processed_tasks WHERE task_id = ?", (task_id,)
    ).fetchone()
    return row is not None


def mark_processed(
    conn: sqlite3.Connection,
    task_id: str,
    task_name: str,
    job_number: str,
    description_fixed: bool = False,
    notes_needing_fix: int = 0,
    errors_found: int = 0,
    marked_ready: bool = False,
    note_added: bool = False,
    run_id: Optional[int] = None,
):
    """Record a task as processed."""
    conn.execute(
        """INSERT OR REPLACE INTO processed_tasks
           (task_id, task_name, job_number, processed_at,
            description_fixed, notes_needing_fix, errors_found,
            marked_ready, note_added, run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            task_id,
            task_name,
            job_number,
            datetime.now(timezone.utc).isoformat(),
            int(description_fixed),
            notes_needing_fix,
            errors_found,
            int(marked_ready),
            int(note_added),
            run_id,
        ),
    )
    conn.commit()


def start_run(conn: sqlite3.Connection, mode: str = "one-shot") -> int:
    """Start a new poll run and return its ID."""
    cursor = conn.execute(
        "INSERT INTO poll_runs (started_at, mode) VALUES (?, ?)",
        (datetime.now(timezone.utc).isoformat(), mode),
    )
    conn.commit()
    return cursor.lastrowid


def complete_run(
    conn: sqlite3.Connection,
    run_id: int,
    tasks_found: int = 0,
    tasks_new: int = 0,
    tasks_processed: int = 0,
    tasks_skipped: int = 0,
    errors: Optional[str] = None,
):
    """Mark a poll run as complete."""
    conn.execute(
        """UPDATE poll_runs
           SET completed_at = ?, tasks_found = ?, tasks_new = ?,
               tasks_processed = ?, tasks_skipped = ?, errors = ?
           WHERE run_id = ?""",
        (
            datetime.now(timezone.utc).isoformat(),
            tasks_found,
            tasks_new,
            tasks_processed,
            tasks_skipped,
            errors,
            run_id,
        ),
    )
    conn.commit()


def get_processed_count(conn: sqlite3.Connection) -> int:
    """Get total number of processed tasks."""
    row = conn.execute("SELECT COUNT(*) FROM processed_tasks").fetchone()
    return row[0]


def get_recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Get recent poll runs."""
    rows = conn.execute(
        "SELECT * FROM poll_runs ORDER BY run_id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def get_stats(conn: sqlite3.Connection) -> dict:
    """Get summary statistics."""
    processed = conn.execute("SELECT COUNT(*) FROM processed_tasks").fetchone()[0]
    with_errors = conn.execute(
        "SELECT COUNT(*) FROM processed_tasks WHERE errors_found > 0"
    ).fetchone()[0]
    desc_fixed = conn.execute(
        "SELECT COUNT(*) FROM processed_tasks WHERE description_fixed = 1"
    ).fetchone()[0]
    notes_pending = conn.execute(
        "SELECT SUM(notes_needing_fix) FROM processed_tasks"
    ).fetchone()[0] or 0
    total_runs = conn.execute("SELECT COUNT(*) FROM poll_runs").fetchone()[0]

    return {
        "total_processed": processed,
        "with_errors": with_errors,
        "descriptions_fixed": desc_fixed,
        "timesheet_notes_pending": notes_pending,
        "total_runs": total_runs,
    }
