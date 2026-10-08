"""
Журнал Telegram-уведомлений о жизненном цикле позиции Human Approval.

Одна аллокация и один event_type — одна запись.
Денежные таблицы и статус сделки здесь не меняются.

Состояния доставки те же, что у заявок v0.8:
reserved → dispatching → delivered.

dispatching без message_id повторно не отправляется: Telegram уже мог
принять сообщение, а процесс упасть до записи message_id.
reserved без вызова Telegram можно доставить следующим циклом.
"""

import sqlite3

import database


DELIVERY_RESERVED = "reserved"
DELIVERY_DISPATCHING = "dispatching"
DELIVERY_DELIVERED = "delivered"

EVENT_CLOSED = "CLOSED"


def ensure_position_notification_table(db_path=None):
    connection = _connect(db_path)
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS position_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                allocation_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                delivery_state TEXT NOT NULL CHECK (
                    delivery_state IN ('reserved', 'dispatching', 'delivered')
                ),
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                sent_at TIMESTAMP,
                UNIQUE (allocation_id, event_type),
                FOREIGN KEY (allocation_id) REFERENCES paper_allocations (id)
            )
        """)
    finally:
        connection.close()


def get_position_notification(db_path, allocation_id, event_type):
    ensure_position_notification_table(db_path)
    connection = _connect(db_path)
    try:
        row = connection.execute(
            """
            SELECT *
            FROM position_notifications
            WHERE allocation_id = ?
              AND event_type = ?
            LIMIT 1
            """,
            (allocation_id, event_type),
        ).fetchone()
        return _row(row)
    finally:
        connection.close()


def list_position_notifications(db_path):
    ensure_position_notification_table(db_path)
    connection = _connect(db_path)
    try:
        rows = connection.execute("""
            SELECT *
            FROM position_notifications
            ORDER BY id ASC
        """).fetchall()
        return [_row(row) for row in rows]
    finally:
        connection.close()


def claim_position_notification(
    db_path,
    allocation_id,
    event_type,
    chat_id,
    now_text,
):
    """
    Резервирует доставку.

    False означает, что запись этой аллокации и event_type уже есть.
    """
    ensure_position_notification_table(db_path)
    connection = _connect(db_path)
    try:
        _begin(connection)
        existing = connection.execute(
            """
            SELECT id
            FROM position_notifications
            WHERE allocation_id = ?
              AND event_type = ?
            LIMIT 1
            """,
            (allocation_id, event_type),
        ).fetchone()
        if existing is not None:
            _commit(connection)
            return False
        connection.execute("""
            INSERT INTO position_notifications (
                allocation_id,
                event_type,
                chat_id,
                message_id,
                delivery_state,
                created_at,
                updated_at,
                sent_at
            )
            VALUES (?, ?, ?, NULL, ?, ?, ?, NULL)
        """, (
            allocation_id,
            event_type,
            chat_id,
            DELIVERY_RESERVED,
            now_text,
            now_text,
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


def mark_position_dispatching(db_path, allocation_id, event_type, now_text):
    """
    Фиксирует, что вызов Telegram начат.

    Повторный вызов не переводит строку снова и не даёт вторую отправку.
    """
    ensure_position_notification_table(db_path)
    connection = _connect(db_path)
    try:
        _begin(connection)
        cursor = connection.execute("""
            UPDATE position_notifications
            SET delivery_state = ?, updated_at = ?
            WHERE allocation_id = ?
              AND event_type = ?
              AND message_id IS NULL
              AND delivery_state = ?
        """, (
            DELIVERY_DISPATCHING,
            now_text,
            allocation_id,
            event_type,
            DELIVERY_RESERVED,
        ))
        _commit(connection)
        return cursor.rowcount == 1
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def record_position_delivered(
    db_path,
    allocation_id,
    event_type,
    message_id,
    now_text,
):
    """Записывает message_id. Повтор с тем же id остаётся успешным."""
    ensure_position_notification_table(db_path)
    message_id = int(message_id)
    connection = _connect(db_path)
    try:
        _begin(connection)
        cursor = connection.execute("""
            UPDATE position_notifications
            SET
                message_id = ?,
                delivery_state = ?,
                sent_at = ?,
                updated_at = ?
            WHERE allocation_id = ?
              AND event_type = ?
              AND (message_id IS NULL OR message_id = ?)
        """, (
            message_id,
            DELIVERY_DELIVERED,
            now_text,
            now_text,
            allocation_id,
            event_type,
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
    current = get_position_notification(db_path, allocation_id, event_type)
    return current is not None and current.get("message_id") == message_id


def attach_position_message(
    db_path,
    allocation_id,
    event_type,
    message_id,
    now_text,
):
    return record_position_delivered(
        db_path,
        allocation_id,
        event_type,
        message_id,
        now_text,
    )


def release_unsent_position(db_path, allocation_id, event_type):
    """Снимает резерв, если Telegram так и не принял сообщение."""
    ensure_position_notification_table(db_path)
    connection = _connect(db_path)
    try:
        _begin(connection)
        connection.execute("""
            DELETE FROM position_notifications
            WHERE allocation_id = ?
              AND event_type = ?
              AND message_id IS NULL
        """, (
            allocation_id,
            event_type,
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
