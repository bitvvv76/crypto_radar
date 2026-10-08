"""
Telegram-сопровождение уже закрытой и открытой книги Human Approval.

Модуль только читает portfolio_id = 2 и отправляет текст.
SELL, BUY и состояние аллокации здесь не исполняются.
"""

import sqlite3

import database
from human_approval import (
    ALLOCATION_CLOSED,
    ALLOCATION_OPEN,
    APPROVAL_PORTFOLIO_ID,
)
from paper_engine import (
    calculate_profit_percent,
    format_datetime,
    normalize_price,
    parse_datetime,
    utc_now,
)
from position_notification_store import (
    DELIVERY_DELIVERED,
    DELIVERY_DISPATCHING,
    DELIVERY_RESERVED,
    EVENT_CLOSED,
    attach_position_message,
    claim_position_notification,
    get_position_notification,
    mark_position_dispatching,
    record_position_delivered,
    release_unsent_position,
)


class NotificationDeliveryError(Exception):
    pass


def deliver_closed_positions(db_path, client, chat_id, clock=None):
    """
    Доставляет итог CLOSED аллокаций книги 2.

    reserved — Telegram ещё не вызывался, отправку можно повторить.
    dispatching — вызов уже начат; новый цикл сообщение не шлёт.
    delivered — message_id записан.
    Ошибка Telegram до принятия сообщения снимает резерв и не меняет книги.
    """
    clock = clock or utc_now
    now_text = format_datetime(clock())
    sent_ids = []
    errors = []
    try:
        rows = list_closed_allocations(db_path)
    except Exception as error:
        return {
            "sent": 0,
            "errors": [{"allocation_id": None, "error": str(error)}],
            "allocation_ids": [],
        }

    for row in rows:
        allocation_id = row["id"]
        sent_message = None
        dispatch_started = False
        try:
            existing = _notification_for_delivery(
                db_path,
                allocation_id,
                chat_id,
                now_text,
            )
            if existing is None or _delivery_blocks_send(existing):
                continue
            if not mark_position_dispatching(
                db_path,
                allocation_id,
                EVENT_CLOSED,
                now_text,
            ):
                continue
            dispatch_started = True
            sent_message = client.send_message(
                chat_id,
                render_closed_position(build_closed_view(row)),
            )
            message_id = _message_id_of(sent_message)
            if message_id is None:
                raise NotificationDeliveryError("telegram request failed")
            _finish_delivery(db_path, allocation_id, message_id, now_text)
            sent_ids.append(allocation_id)
        except Exception as error:
            if _message_id_of(sent_message) is not None:
                _persist_sent_message(db_path, allocation_id, sent_message, now_text)
            elif dispatch_started:
                release_unsent_position(db_path, allocation_id, EVENT_CLOSED)
            errors.append({
                "allocation_id": allocation_id,
                "error": str(error),
            })
    return {"sent": len(sent_ids), "errors": errors, "allocation_ids": sent_ids}


def handle_positions_message(update, settings, client, db_path):
    """
    Read-only команда /positions.

    Чужой user_id и чужой chat_id не получают текст и не меняют книги.
    """
    message = update.get("message") if isinstance(update, dict) else None
    if not isinstance(message, dict):
        return {"acted": False, "reason": "ignored", "command": None}
    if not parse_positions_command(message.get("text")):
        return {"acted": False, "reason": "ignored", "command": None}

    user_id = _user_id(message)
    chat_id = _chat_id(message)
    if user_id != settings.allowed_user_id or chat_id != settings.chat_id:
        return {"acted": False, "reason": "forbidden", "command": "positions"}

    client.send_message(
        chat_id,
        render_open_positions(list_open_position_views(db_path)),
    )
    return {"acted": True, "reason": None, "command": "positions"}


def parse_positions_command(text):
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if stripped == "":
        return False
    token = stripped.split()[0]
    name = token.split("@", 1)[0]
    return name.lower() == "/positions"


def list_closed_allocations(db_path):
    if not _table_exists(db_path, "paper_allocations"):
        return []
    connection = _connect(db_path)
    try:
        rows = connection.execute("""
            SELECT
                a.id,
                a.portfolio_id,
                a.position_id,
                a.pair_id,
                a.status,
                a.quantity,
                a.allocated_usd,
                a.entry_price,
                a.entry_time,
                a.exit_price,
                a.exit_time,
                a.exit_reason,
                a.market_value_usd,
                pp.exit_reason AS baseline_exit_reason,
                p.pair_symbol
            FROM paper_allocations AS a
            LEFT JOIN paper_positions AS pp
                ON pp.id = a.position_id
            LEFT JOIN pairs AS p
                ON p.id = a.pair_id
            WHERE a.portfolio_id = ?
              AND a.status = ?
            ORDER BY a.id ASC
        """, (
            APPROVAL_PORTFOLIO_ID,
            ALLOCATION_CLOSED,
        )).fetchall()
        return [_row(row) for row in rows]
    finally:
        connection.close()


def list_open_position_views(db_path):
    return [build_open_view(row) for row in list_open_allocations(db_path)]


def list_open_allocations(db_path):
    if not _table_exists(db_path, "paper_allocations"):
        return []
    connection = _connect(db_path)
    try:
        rows = connection.execute("""
            SELECT
                a.id,
                a.portfolio_id,
                a.status,
                a.quantity,
                a.allocated_usd,
                a.entry_price,
                a.last_price,
                a.market_value_usd,
                a.unrealized_pnl_usd,
                p.pair_symbol
            FROM paper_allocations AS a
            LEFT JOIN pairs AS p
                ON p.id = a.pair_id
            WHERE a.portfolio_id = ?
              AND a.status = ?
            ORDER BY a.id ASC
        """, (
            APPROVAL_PORTFOLIO_ID,
            ALLOCATION_OPEN,
        )).fetchall()
        return [_row(row) for row in rows]
    finally:
        connection.close()


def build_closed_view(row):
    """
    Итог сделки от цены исполнения книги 2.

    result_percent baseline и result_percent аллокации не используются:
    доходность считается из entry_price и exit_price самой аллокации.
    """
    entry_price = _positive_price(row.get("entry_price"))
    exit_price = _positive_price(row.get("exit_price"))
    quantity = _number(row.get("quantity"))
    invested = _number(row.get("allocated_usd"))
    received = None
    if quantity is not None and exit_price is not None:
        received = quantity * exit_price
    pnl = None
    if received is not None and invested is not None:
        pnl = received - invested
    reason = _text(row.get("exit_reason"))
    if reason is None:
        reason = _text(row.get("baseline_exit_reason"))
    remaining_quantity = None
    remaining_value = None
    if row.get("status") == ALLOCATION_CLOSED:
        # Полного выхода v0.8.1 хватает, чтобы остаток количества был 0.
        # Остаток в USD берётся из уже записанного market_value_usd.
        remaining_quantity = 0
        remaining_value = _number(row.get("market_value_usd"))
    return {
        "allocation_id": row.get("id"),
        "portfolio_id": row.get("portfolio_id"),
        "pair_label": format_pair(row.get("pair_symbol")),
        "reason": reason,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "invested_usd": invested,
        "received_usd": received,
        "pnl_usd": pnl,
        "pnl_percent": calculate_profit_percent(entry_price, exit_price),
        "quantity": quantity,
        "remaining_quantity": remaining_quantity,
        "remaining_value_usd": remaining_value,
        "hold_label": format_hold(row.get("entry_time"), row.get("exit_time")),
    }


def build_open_view(row):
    """
    Открытая позиция из уже записанных MTM-полей аллокации.

    Если текущей цены нет, стоимость и PnL не вычисляются.
    """
    last_price = _positive_price(row.get("last_price"))
    entry_price = _positive_price(row.get("entry_price"))
    market_value = _number(row.get("market_value_usd"))
    unrealized = _number(row.get("unrealized_pnl_usd"))
    current_value = None
    pnl = None
    pnl_percent = None
    if last_price is not None and market_value is not None:
        current_value = market_value
    if last_price is not None and unrealized is not None and entry_price is not None:
        pnl = unrealized
        pnl_percent = calculate_profit_percent(entry_price, last_price)
    return {
        "allocation_id": row.get("id"),
        "portfolio_id": row.get("portfolio_id"),
        "pair_label": format_pair(row.get("pair_symbol")),
        "invested_usd": _number(row.get("allocated_usd")),
        "market_value_usd": current_value,
        "pnl_usd": pnl,
        "pnl_percent": pnl_percent,
        "quantity": _number(row.get("quantity")),
    }


def render_closed_position(view):
    portfolio_id = view.get("portfolio_id")
    if portfolio_id is None:
        portfolio_id = APPROVAL_PORTFOLIO_ID
    lines = [
        "🔴 CRYPTO RADAR — POSITION CLOSED",
        "",
        "Пара: {0}".format(_or_missing(view.get("pair_label"))),
        "Причина: {0}".format(_or_missing(view.get("reason"))),
        "",
        "Цена входа:  {0}".format(format_price(view.get("entry_price"))),
        "Цена выхода: {0}".format(format_price(view.get("exit_price"))),
        "",
        "Вложено:  {0}".format(format_usd(view.get("invested_usd"))),
        "Получено: {0}".format(format_usd(view.get("received_usd"))),
        "",
        "Результат: {0}".format(format_usd(view.get("pnl_usd"), signed=True)),
        "Доходность: {0}".format(format_percent(view.get("pnl_percent"))),
        "",
        "Количество куплено: {0}".format(format_quantity(view.get("quantity"))),
        "Осталось монет: {0}".format(format_quantity(view.get("remaining_quantity"))),
        "Осталось в позиции: {0}".format(format_usd(view.get("remaining_value_usd"))),
        "",
        "Время в позиции: {0}".format(_or_missing(view.get("hold_label"))),
        "",
        "Portfolio: Human Approval #{0}".format(portfolio_id),
    ]
    return "\n".join(lines)


def render_open_positions(views):
    lines = ["🟡 OPEN POSITIONS", ""]
    if not views:
        lines.append("Открытых позиций нет.")
        return "\n".join(lines)
    blocks = []
    for view in views:
        blocks.append("\n".join([
            _or_missing(view.get("pair_label")),
            "Вложено: {0}".format(format_usd(view.get("invested_usd"))),
            "Текущая стоимость: {0}".format(format_usd(view.get("market_value_usd"))),
            "PnL: {0}".format(format_open_pnl(view)),
            "Количество: {0}".format(format_quantity(view.get("quantity"))),
        ]))
    lines.append("\n\n".join(blocks))
    return "\n".join(lines)


def format_open_pnl(view):
    if view.get("pnl_usd") is None or view.get("pnl_percent") is None:
        return "нет данных"
    return "{0} / {1}".format(
        format_usd(view.get("pnl_usd"), signed=True),
        format_percent(view.get("pnl_percent")),
    )


def format_pair(symbol):
    text = _text(symbol)
    if text is None:
        return None
    if "/" not in text:
        return text
    base, quote = text.split("/", 1)
    return "{0} / {1}".format(base.strip(), quote.strip())


def format_price(value):
    number = _positive_price(value)
    if number is None:
        return "нет данных"
    return "${0:.6f}".format(number)


def format_usd(amount, signed=False):
    number = _number(amount)
    if number is None:
        return "нет данных"
    number = round(number, 2)
    if number == 0:
        number = 0.0
    body = "${0:.2f}".format(abs(number))
    if not signed:
        if number < 0:
            return "-{0}".format(body)
        return body
    if number > 0:
        return "+{0}".format(body)
    if number < 0:
        return "-{0}".format(body)
    return "+{0}".format(body)


def format_percent(value):
    number = _number(value)
    if number is None:
        return "нет данных"
    number = round(number, 2)
    if number == 0:
        number = 0.0
    if number > 0:
        return "+{0:.2f}%".format(number)
    if number < 0:
        return "-{0:.2f}%".format(abs(number))
    return "+0.00%"


def format_quantity(value):
    number = _number(value)
    if number is None:
        return "нет данных"
    if abs(number - round(number)) < 1e-6:
        whole = int(round(number))
        sign = "-" if whole < 0 else ""
        return sign + "{0:,}".format(abs(whole)).replace(",", " ")
    sign = "-" if number < 0 else ""
    text = "{0:,.4f}".format(abs(number)).replace(",", " ")
    text = text.rstrip("0").rstrip(".")
    return sign + text


def format_hold(entry_time, exit_time):
    if entry_time is None or exit_time is None:
        return None
    try:
        start = parse_datetime(entry_time)
        end = parse_datetime(exit_time)
    except (TypeError, ValueError):
        return None
    seconds = (end - start).total_seconds()
    if seconds < 0:
        return None
    total_minutes = int(seconds // 60)
    days = total_minutes // (24 * 60)
    hours = (total_minutes % (24 * 60)) // 60
    minutes = total_minutes % 60
    parts = []
    if days:
        parts.append("{0} дн.".format(days))
    if hours or days:
        parts.append("{0} ч.".format(hours))
    if minutes:
        parts.append("{0} мин.".format(minutes))
    if not parts:
        return "0 ч."
    return " ".join(parts)


def _finish_delivery(db_path, allocation_id, message_id, now_text):
    try:
        stored = attach_position_message(
            db_path,
            allocation_id,
            EVENT_CLOSED,
            message_id,
            now_text,
        )
    except Exception:
        stored = False
    if stored:
        return
    if not record_position_delivered(
        db_path,
        allocation_id,
        EVENT_CLOSED,
        message_id,
        now_text,
    ):
        raise NotificationDeliveryError("telegram request failed")


def _persist_sent_message(db_path, allocation_id, sent_message, now_text):
    message_id = _message_id_of(sent_message)
    if message_id is None:
        return False
    try:
        return record_position_delivered(
            db_path,
            allocation_id,
            EVENT_CLOSED,
            message_id,
            now_text,
        )
    except Exception:
        return False


def _notification_for_delivery(db_path, allocation_id, chat_id, now_text):
    existing = get_position_notification(db_path, allocation_id, EVENT_CLOSED)
    if existing is not None:
        return existing
    if not claim_position_notification(
        db_path,
        allocation_id,
        EVENT_CLOSED,
        chat_id,
        now_text,
    ):
        return get_position_notification(db_path, allocation_id, EVENT_CLOSED)
    return get_position_notification(db_path, allocation_id, EVENT_CLOSED)


def _delivery_blocks_send(existing):
    if existing.get("message_id") is not None:
        return True
    state = existing.get("delivery_state")
    if state == DELIVERY_DELIVERED:
        return True
    if state == DELIVERY_DISPATCHING:
        return True
    return state not in (DELIVERY_RESERVED, None)


def _message_id_of(sent_message):
    if not isinstance(sent_message, dict):
        return None
    message_id = sent_message.get("message_id")
    if message_id is None:
        return None
    return int(message_id)


def _connect(db_path=None):
    connection = sqlite3.connect(db_path or database.DB_NAME)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _table_exists(db_path, table_name):
    connection = sqlite3.connect(db_path or database.DB_NAME)
    try:
        row = connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            LIMIT 1
            """,
            (table_name,),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _user_id(message):
    sender = message.get("from")
    if not isinstance(sender, dict):
        return None
    try:
        return int(sender.get("id"))
    except (TypeError, ValueError):
        return None


def _chat_id(message):
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return None
    try:
        return int(chat.get("id"))
    except (TypeError, ValueError):
        return None


def _positive_price(value):
    return normalize_price(value)


def _number(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return text


def _or_missing(value):
    if value is None or value == "":
        return "нет данных"
    return value


def _row(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
