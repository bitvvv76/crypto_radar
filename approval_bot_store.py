"""
Журнал доставки заявок Human Approval в Telegram.

Одна заявка — одна запись. Денежные таблицы здесь не меняются.
"""

import sqlite3

import database


DELIVERY_RESERVED = "reserved"
DELIVERY_DISPATCHING = "dispatching"
DELIVERY_DELIVERED = "delivered"


def ensure_notification_table(db_path=None):
    connection = _connect(db_path)
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS approval_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL UNIQUE,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                sent_at TIMESTAMP NOT NULL,
                last_status TEXT NOT NULL,
                delivery_state TEXT NOT NULL CHECK (
                    delivery_state IN ('reserved', 'dispatching', 'delivered')
                ),
                FOREIGN KEY (request_id) REFERENCES approval_requests (id)
            )
        """)
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(approval_notifications)")
        }
        if "delivery_state" not in columns:
            connection.execute("""
                ALTER TABLE approval_notifications
                ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'reserved'
            """)
            connection.execute("""
                UPDATE approval_notifications
                SET delivery_state = ?
                WHERE message_id IS NOT NULL
            """, (DELIVERY_DELIVERED,))
    finally:
        connection.close()


def get_notification(db_path, request_id):
    ensure_notification_table(db_path)
    connection = _connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT *
            FROM approval_notifications
            WHERE request_id = ?
            LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        return _row(row)
    finally:
        connection.close()


def list_notifications(db_path):
    ensure_notification_table(db_path)
    connection = _connect(db_path)
    try:
        rows = connection.execute("""
            SELECT *
            FROM approval_notifications
            ORDER BY id ASC
        """).fetchall()
        return [_row(row) for row in rows]
    finally:
        connection.close()


def claim_notification(db_path, request_id, chat_id, sent_at, last_status):
    """
    Резервирует доставку.

    False означает, что запись этой заявки уже есть.
    """
    ensure_notification_table(db_path)
    connection = _connect(db_path)
    try:
        _begin(connection)
        existing = connection.execute(
            """
            SELECT id
            FROM approval_notifications
            WHERE request_id = ?
            LIMIT 1
            """,
            (request_id,),
        ).fetchone()
        if existing is not None:
            _commit(connection)
            return False
        connection.execute("""
            INSERT INTO approval_notifications (
                request_id,
                chat_id,
                message_id,
                sent_at,
                last_status,
                delivery_state
            )
            VALUES (?, ?, NULL, ?, ?, ?)
        """, (
            request_id,
            chat_id,
            sent_at,
            last_status,
            DELIVERY_RESERVED,
        ))
        _commit(connection)
        return True
    except sqlite3.IntegrityError:
        _rollback(connection)
        return False
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def mark_dispatching(db_path, request_id):
    """
    Фиксирует, что вызов Telegram начат.

    Повторный вызов не переводит строку снова и не даёт вторую отправку.
    """
    ensure_notification_table(db_path)
    connection = _connect(db_path)
    try:
        _begin(connection)
        cursor = connection.execute("""
            UPDATE approval_notifications
            SET delivery_state = ?
            WHERE request_id = ?
              AND message_id IS NULL
              AND delivery_state = ?
        """, (
            DELIVERY_DISPATCHING,
            request_id,
            DELIVERY_RESERVED,
        ))
        _commit(connection)
        return cursor.rowcount == 1
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def record_delivered_message(db_path, request_id, message_id):
    """Записывает message_id. Повтор с тем же id остаётся успешным."""
    ensure_notification_table(db_path)
    message_id = int(message_id)
    connection = _connect(db_path)
    try:
        _begin(connection)
        cursor = connection.execute("""
            UPDATE approval_notifications
            SET message_id = ?, delivery_state = ?
            WHERE request_id = ?
              AND (message_id IS NULL OR message_id = ?)
        """, (
            message_id,
            DELIVERY_DELIVERED,
            request_id,
            message_id,
        ))
        _commit(connection)
        stored = cursor.rowcount == 1
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()
    if stored:
        return True
    current = get_notification(db_path, request_id)
    return current is not None and current.get("message_id") == message_id


def attach_message(db_path, request_id, message_id):
    return record_delivered_message(db_path, request_id, message_id)


def release_unsent(db_path, request_id):
    """Снимает резерв, если Telegram так и не принял сообщение."""
    ensure_notification_table(db_path)
    connection = _connect(db_path)
    try:
        _begin(connection)
        connection.execute("""
            DELETE FROM approval_notifications
            WHERE request_id = ? AND message_id IS NULL
        """, (request_id,))
        _commit(connection)
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def update_last_status(db_path, request_id, last_status):
    ensure_notification_table(db_path)
    connection = _connect(db_path)
    try:
        _begin(connection)
        connection.execute("""
            UPDATE approval_notifications
            SET last_status = ?
            WHERE request_id = ?
        """, (
            last_status,
            request_id,
        ))
        _commit(connection)
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _connect(db_path=None):
    connection = sqlite3.connect(db_path or database.DB_NAME)
    connection.row_factory = sqlite3.Row
    connection.isolation_level = None
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _begin(connection):
    connection.execute("BEGIN IMMEDIATE")


def _commit(connection):
    connection.execute("COMMIT")


def _rollback(connection):
    try:
        connection.execute("ROLLBACK")
    except sqlite3.OperationalError:
        return


def _row(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
