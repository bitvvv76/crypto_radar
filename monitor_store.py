"""
Новые служебные таблицы наблюдения.

Торговые таблицы здесь не изменяются: только CREATE TABLE IF NOT EXISTS
для журнала запусков, ежедневного отчёта и состояния оповещений.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone

import database


JOB_SCANNER = "scanner"
JOB_AUTO_CHECK = "auto_check"

REPORT_FAILED = "failed"
REPORT_DISPATCHING = "dispatching"
REPORT_DELIVERED = "delivered"

_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_SECRET_ASSIGN_RE = re.compile(
    r"((?:TELEGRAM_BOT_TOKEN|TELEGRAM_ALLOWED_USER_ID|TELEGRAM_CHAT_ID)\s*=\s*)\S+",
    re.IGNORECASE,
)


def redact_text(value):
    text = "" if value is None else str(value)
    text = _TOKEN_RE.sub("[redacted]", text)
    text = _SECRET_ASSIGN_RE.sub(r"\1[redacted]", text)
    return text


def utc_now_text(now=None):
    if now is None:
        moment = datetime.now(timezone.utc).replace(tzinfo=None)
    elif isinstance(now, datetime):
        moment = now
    else:
        moment = datetime.strptime(str(now).strip(), "%Y-%m-%d %H:%M:%S")
    return moment.replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def ensure_monitor_tables(db_path=None):
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS monitor_job_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                summary_json TEXT,
                error_text TEXT
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_monitor_job_runs_name_id
            ON monitor_job_runs (job_name, id)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS monitor_daily_reports (
                report_date TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                message_id INTEGER,
                attempted_at TEXT,
                delivered_at TEXT,
                error_text TEXT
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS monitor_health_state (
                alert_key TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                detail TEXT,
                notified_at TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def record_job_run(
    db_path,
    job_name,
    started_at,
    status,
    summary=None,
    error_text=None,
    finished_at=None,
):
    ensure_monitor_tables(db_path)
    payload = None
    if summary is not None:
        payload = json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        cursor = connection.execute("""
            INSERT INTO monitor_job_runs (
                job_name,
                started_at,
                finished_at,
                status,
                summary_json,
                error_text
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            job_name,
            started_at,
            finished_at or utc_now_text(),
            status,
            payload,
            None if error_text is None else redact_text(error_text),
        ))
        run_id = cursor.lastrowid
        connection.execute("COMMIT")
        return run_id
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def latest_job_run(db_path, job_name):
    if not _table_exists(db_path, "monitor_job_runs"):
        return None
    connection = _connect(db_path)
    try:
        row = connection.execute("""
            SELECT *
            FROM monitor_job_runs
            WHERE job_name = ?
            ORDER BY id DESC
            LIMIT 1
        """, (job_name,)).fetchone()
        return _with_summary(_row(row))
    finally:
        connection.close()


def job_runs_between(db_path, start_text, end_text):
    if not _table_exists(db_path, "monitor_job_runs"):
        return None
    connection = _connect(db_path)
    try:
        rows = connection.execute("""
            SELECT *
            FROM monitor_job_runs
            WHERE finished_at IS NOT NULL
              AND datetime(finished_at) >= datetime(?)
              AND datetime(finished_at) < datetime(?)
            ORDER BY id ASC
        """, (start_text, end_text)).fetchall()
        return [_with_summary(_row(row)) for row in rows]
    finally:
        connection.close()


def get_daily_report(db_path, report_date):
    ensure_monitor_tables(db_path)
    connection = _connect(db_path)
    try:
        row = connection.execute("""
            SELECT *
            FROM monitor_daily_reports
            WHERE report_date = ?
            LIMIT 1
        """, (report_date,)).fetchone()
        return _row(row)
    finally:
        connection.close()


def claim_daily_report(db_path, report_date, attempted_at):
    """
    Резервирует одну отправку отчёта за сутки.

    delivered и dispatching повторно не отправляются.
    failed можно взять снова.
    """
    ensure_monitor_tables(db_path)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("""
            SELECT *
            FROM monitor_daily_reports
            WHERE report_date = ?
            LIMIT 1
        """, (report_date,)).fetchone()
        current = _row(row)
        if current is not None and current["status"] in (
            REPORT_DELIVERED,
            REPORT_DISPATCHING,
        ):
            connection.execute("COMMIT")
            return "skip", current
        if current is None:
            connection.execute("""
                INSERT INTO monitor_daily_reports (
                    report_date,
                    status,
                    message_id,
                    attempted_at,
                    delivered_at,
                    error_text
                )
                VALUES (?, ?, NULL, ?, NULL, NULL)
            """, (report_date, REPORT_DISPATCHING, attempted_at))
        else:
            cursor = connection.execute("""
                UPDATE monitor_daily_reports
                SET status = ?,
                    attempted_at = ?,
                    error_text = NULL
                WHERE report_date = ?
                  AND status = ?
            """, (
                REPORT_DISPATCHING,
                attempted_at,
                report_date,
                REPORT_FAILED,
            ))
            if cursor.rowcount != 1:
                connection.execute("COMMIT")
                fresh = connection.execute("""
                    SELECT *
                    FROM monitor_daily_reports
                    WHERE report_date = ?
                    LIMIT 1
                """, (report_date,)).fetchone()
                return "skip", _row(fresh)
        connection.execute("COMMIT")
        return "send", get_daily_report(db_path, report_date)
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def mark_daily_report_delivered(db_path, report_date, message_id, delivered_at):
    ensure_monitor_tables(db_path)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            UPDATE monitor_daily_reports
            SET status = ?,
                message_id = ?,
                delivered_at = ?,
                error_text = NULL
            WHERE report_date = ?
              AND status = ?
        """, (
            REPORT_DELIVERED,
            message_id,
            delivered_at,
            report_date,
            REPORT_DISPATCHING,
        ))
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def mark_daily_report_failed(db_path, report_date, error_text, failed_at):
    ensure_monitor_tables(db_path)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("""
            UPDATE monitor_daily_reports
            SET status = ?,
                attempted_at = ?,
                error_text = ?
            WHERE report_date = ?
              AND status = ?
        """, (
            REPORT_FAILED,
            failed_at,
            redact_text(error_text),
            report_date,
            REPORT_DISPATCHING,
        ))
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def list_health_state(db_path):
    if not _table_exists(db_path, "monitor_health_state"):
        return {}
    connection = _connect(db_path)
    try:
        rows = connection.execute("""
            SELECT *
            FROM monitor_health_state
            ORDER BY alert_key ASC
        """).fetchall()
        return {row["alert_key"]: _row(row) for row in rows}
    finally:
        connection.close()


def save_health_states(db_path, states, notified_at):
    ensure_monitor_tables(db_path)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for alert_key, payload in states.items():
            connection.execute("""
                INSERT INTO monitor_health_state (
                    alert_key,
                    state,
                    detail,
                    notified_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(alert_key) DO UPDATE SET
                    state = excluded.state,
                    detail = excluded.detail,
                    notified_at = excluded.notified_at,
                    updated_at = excluded.updated_at
            """, (
                alert_key,
                payload["state"],
                payload.get("detail"),
                notified_at,
                notified_at,
            ))
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _with_summary(row):
    if row is None:
        return None
    raw = row.get("summary_json")
    summary = None
    if raw:
        try:
            summary = json.loads(raw)
        except (TypeError, ValueError):
            summary = None
    row["summary"] = summary
    return row


def _table_exists(db_path, table_name):
    connection = sqlite3.connect(db_path or database.DB_NAME)
    try:
        found = connection.execute("""
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            LIMIT 1
        """, (table_name,)).fetchone()
        return found is not None
    finally:
        connection.close()


def _connect(db_path=None):
    connection = sqlite3.connect(db_path or database.DB_NAME)
    connection.row_factory = sqlite3.Row
    connection.isolation_level = None
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _row(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
