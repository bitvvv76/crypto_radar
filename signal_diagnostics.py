"""
Диагностика формирования сигналов по уже прошедшему циклу.

Модуль читает факты цикла и базу. Заявки и позиции он не создаёт.
"""

from collections import Counter

from database import get_paper_connection


OUTCOME_NO_NEW_EVENTS = "no_new_events"
OUTCOME_PROCESSING_ERROR = "processing_error"
OUTCOME_FILTERED = "filtered"
OUTCOME_SIGNALS_CREATED = "signals_created"
OUTCOME_PROCESSED_WITH_ERRORS = "processed_with_errors"

REASON_LABELS = {
    "score_below_sql_minimum": "final score ниже порога SQL",
    "score_outside_cohort": "score вне допустимой когорты",
    "change_24h_unclassified": "изменение 24h не раскладывается в тип сигнала",
    "entry_price_invalid": "цена входа некорректна",
    "entry_price_not_positive": "цена проверки 24h не положительная",
    "price_change_missing": "у проверки 24h нет изменения цены",
    "no_24h_check": "проверка 24h отсутствует",
    "baseline_already_exists": "baseline по паре уже есть",
    "pair_missing": "пара отсутствует в базе",
    "first_24h_check_ineligible": "первая проверка 24h не проходит SQL-отбор",
    "entry_error": "ошибка записи baseline",
    "paper_cycle_error": "сбой paper engine",
}

OUTCOME_TEXT = {
    OUTCOME_NO_NEW_EVENTS: "Новых событий 24h нет. Ошибок обработки нет.",
    OUTCOME_PROCESSING_ERROR: "Новых событий 24h нет. Есть ошибки обработки.",
    OUTCOME_FILTERED: "События 24h были. Baseline не создана: кандидаты отклонены.",
    OUTCOME_SIGNALS_CREATED: "По новым событиям 24h создана baseline.",
    OUTCOME_PROCESSED_WITH_ERRORS: "События 24h обработаны. Часть шагов завершилась ошибкой.",
}


def explain_sql_gaps(pair_ids, selected_pair_ids, min_final_score, db_path=None):
    selected = set(selected_pair_ids or [])
    gaps = []
    if not pair_ids:
        return gaps
    connection = get_paper_connection(db_path)
    try:
        for pair_id in pair_ids:
            if pair_id in selected:
                continue
            gaps.append({
                "pair_id": pair_id,
                "reason": _gap_reason(connection, pair_id, min_final_score),
            })
    finally:
        connection.close()
    return gaps


def telegram_delivery_counts(db_path, created_at_text):
    """
    Доставка заявок, созданных в этом цикле.

    None означает, что журнал заявок недоступен и ноль подставлять нельзя.
    """
    if created_at_text is None:
        return None, None
    if not _table_exists(db_path, "approval_requests"):
        return None, None

    notifications_exist = _table_exists(db_path, "approval_notifications")
    connection = get_paper_connection(db_path)
    try:
        try:
            if notifications_exist:
                rows = connection.execute("""
                    SELECT
                        r.id AS request_id,
                        n.delivery_state AS delivery_state,
                        n.message_id AS message_id
                    FROM approval_requests AS r
                    LEFT JOIN approval_notifications AS n
                        ON n.request_id = r.id
                    WHERE r.created_at = ?
                    ORDER BY r.id ASC
                """, (created_at_text,)).fetchall()
            else:
                rows = connection.execute("""
                    SELECT id AS request_id
                    FROM approval_requests
                    WHERE created_at = ?
                    ORDER BY id ASC
                """, (created_at_text,)).fetchall()
        except Exception:
            return None, None
    finally:
        connection.close()

    delivered = 0
    undelivered = 0
    for row in rows:
        state = row["delivery_state"] if notifications_exist else None
        message_id = row["message_id"] if notifications_exist else None
        if state == "delivered" or message_id is not None:
            delivered += 1
        else:
            undelivered += 1
    return delivered, undelivered


def build_signal_diagnostics(
    new_24h_events,
    sql_candidates,
    baseline_opened,
    rejections,
    entry_errors,
    price_check_errors,
    approval_requests_created,
    approval_account_missing,
    approval_error,
    telegram_delivered,
    telegram_undelivered,
):
    rejection_rows = list(rejections or [])
    processing_errors = []
    for item in list(entry_errors or []):
        processing_errors.append(dict(item))
    for item in list(price_check_errors or []):
        processing_errors.append(dict(item))
    if approval_error:
        processing_errors.append({
            "pair_id": None,
            "reason": "approval_error",
            "error_type": type(approval_error).__name__
            if isinstance(approval_error, BaseException)
            else "approval_error",
        })

    counts = Counter(item.get("reason") for item in rejection_rows)
    outcome = _outcome(
        new_24h_events,
        sql_candidates,
        baseline_opened,
        processing_errors,
    )
    return {
        "new_24h_events": new_24h_events,
        "sql_candidates": sql_candidates,
        "baseline_opened": baseline_opened,
        "rejections": rejection_rows,
        "rejection_counts": dict(counts),
        "approval_requests_created": approval_requests_created,
        "approval_account_missing": bool(approval_account_missing),
        "telegram_delivered": telegram_delivered,
        "telegram_undelivered": telegram_undelivered,
        "processing_errors": processing_errors,
        "outcome": outcome,
    }


def format_signal_diagnostics(diagnostics):
    lines = [
        "",
        "ДИАГНОСТИКА СИГНАЛОВ",
        "====================",
        "Исход: {0}".format(OUTCOME_TEXT.get(
            diagnostics.get("outcome"),
            diagnostics.get("outcome"),
        )),
        "Новых событий 24h: {0}".format(_show(diagnostics.get("new_24h_events"))),
        "Кандидатов после SQL: {0}".format(_show(diagnostics.get("sql_candidates"))),
        "Создано baseline: {0}".format(_show(diagnostics.get("baseline_opened"))),
        "Заявок Human Approval: {0}".format(
            _approval_text(diagnostics)
        ),
        "Telegram доставлено: {0}".format(_show(diagnostics.get("telegram_delivered"))),
        "Telegram не доставлено: {0}".format(
            _show(diagnostics.get("telegram_undelivered"))
        ),
    ]
    counts = diagnostics.get("rejection_counts") or {}
    if counts:
        lines.append("Причины отклонения:")
        for reason in sorted(counts):
            lines.append("  {0}: {1}".format(
                REASON_LABELS.get(reason, reason),
                counts[reason],
            ))
    else:
        lines.append("Причины отклонения: нет")
    errors = diagnostics.get("processing_errors") or []
    if errors:
        lines.append("Ошибки обработки: {0}".format(len(errors)))
        for item in errors:
            kind = item.get("kind") or item.get("reason") or item.get("error_type")
            lines.append("  {0} pair_id={1}".format(kind, item.get("pair_id")))
    else:
        lines.append("Ошибки обработки: нет")
    return "\n".join(lines)


def _outcome(new_24h_events, sql_candidates, baseline_opened, processing_errors):
    has_errors = bool(processing_errors)
    events = new_24h_events or 0
    if events == 0 and has_errors:
        return OUTCOME_PROCESSING_ERROR
    if events == 0:
        return OUTCOME_NO_NEW_EVENTS
    if sql_candidates is None or baseline_opened is None:
        return OUTCOME_PROCESSING_ERROR
    if has_errors:
        return OUTCOME_PROCESSED_WITH_ERRORS
    if baseline_opened > 0:
        return OUTCOME_SIGNALS_CREATED
    return OUTCOME_FILTERED


def _approval_text(diagnostics):
    if diagnostics.get("approval_requests_created") is None:
        return "н/д"
    text = str(diagnostics.get("approval_requests_created"))
    if diagnostics.get("approval_account_missing"):
        return "{0}; счёт 2 не активирован".format(text)
    return text


def _show(value):
    if value is None:
        return "н/д"
    return str(value)


def _gap_reason(connection, pair_id, min_final_score):
    pair = connection.execute("""
        SELECT id, final_score
        FROM pairs
        WHERE id = ?
        LIMIT 1
    """, (pair_id,)).fetchone()
    if pair is None:
        return "pair_missing"

    position = connection.execute("""
        SELECT id
        FROM paper_positions
        WHERE pair_id = ?
        LIMIT 1
    """, (pair_id,)).fetchone()
    if position is not None:
        return "baseline_already_exists"

    score = pair["final_score"]
    if score is None or score < min_final_score:
        return "score_below_sql_minimum"

    check = connection.execute("""
        SELECT id, price_change_percent, new_price_usd
        FROM price_checks
        WHERE pair_id = ?
          AND check_period = '24h'
        ORDER BY checked_at ASC, id ASC
        LIMIT 1
    """, (pair_id,)).fetchone()
    if check is None:
        return "no_24h_check"
    if check["price_change_percent"] is None:
        return "price_change_missing"
    price = check["new_price_usd"]
    if price is None or price <= 0:
        return "entry_price_not_positive"
    return "first_24h_check_ineligible"


def _table_exists(db_path, table_name):
    connection = get_paper_connection(db_path)
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
