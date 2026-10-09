"""
Ежедневный отчёт Crypto Radar v0.9.2.

Отчёт строится по строкам SQLite. Отсутствующий показатель остаётся
недоступным и в текст попадает как «н/д». Отправка — одно сообщение
sendMessage в уже настроенный чат. Второй polling не запускается.
"""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

import database
from approval_bot import TelegramRejectedError
from health_monitor import evaluate_health, render_health_alert
from monitor_store import (
    DISPATCH_LEASE,
    JOB_AUTO_CHECK,
    JOB_SCANNER,
    REPORT_DELIVERED,
    REPORT_FAILED,
    REPORT_UNKNOWN,
    claim_daily_report,
    get_daily_report,
    job_runs_between,
    latest_job_run,
    list_unknown_daily_reports,
    mark_daily_report_delivered,
    mark_daily_report_failed,
    mark_daily_report_unknown,
    note_uncertain_daily_delivery,
    redact_text,
    release_daily_report_before_send,
    resolve_unknown_delivery,
    save_health_states,
    utc_now_text,
)
from signal_diagnostics import (
    build_signal_diagnostics,
    format_signal_diagnostics,
    telegram_delivery_counts,
)


CONTROL_PORTFOLIO_ID = 1
APPROVAL_PORTFOLIO_ID = 2
APPROVAL_STATUSES = ("PENDING", "BUY", "SKIP", "EXPIRED")
MISSING = "н/д"


def completed_report_window(now):
    moment = _as_datetime(now)
    end = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)
    return start, end


def build_daily_report(db_path, now):
    start, end = completed_report_window(now)
    start_text = _text(start)
    end_text = _text(end)
    runs = job_runs_between(db_path, start_text, end_text)
    return {
        "generated_at": _text(_as_datetime(now)),
        "report_date": start.strftime("%Y-%m-%d"),
        "window_start": start_text,
        "window_end": end_text,
        "new_ideas": _count_new_ideas(db_path, start_text, end_text),
        "successful_24h_checks": _count_successful_24h(db_path, start_text, end_text),
        "paper_positions_opened": _count_positions_opened(db_path, start_text, end_text),
        "paper_positions_closed": _count_positions_closed(db_path, start_text, end_text),
        "approval_by_status": _approval_counts(db_path, start_text, end_text),
        "portfolios": {
            "control": _read_portfolio(db_path, CONTROL_PORTFOLIO_ID),
            "approval": _read_portfolio(db_path, APPROVAL_PORTFOLIO_ID),
        },
        "market_data_errors": _market_data_errors(runs),
        "unknown_deliveries": list_unknown_daily_reports(db_path),
        "last_scanner_run": latest_job_run(db_path, JOB_SCANNER),
        "last_auto_check_run": latest_job_run(db_path, JOB_AUTO_CHECK),
    }


def render_daily_report(report):
    lines = [
        "CRYPTO RADAR v0.9.2 — ежедневный отчёт",
        "Время UTC: {0}".format(report.get("generated_at") or MISSING),
        "Период UTC: {0} — {1}".format(
            report.get("window_start") or MISSING,
            report.get("window_end") or MISSING,
        ),
        "",
        "Новые идеи: {0}".format(_show_count(report.get("new_ideas"))),
        "Успешные проверки 24h: {0}".format(
            _show_count(report.get("successful_24h_checks"))
        ),
        "Paper-позиции, новые: {0}".format(
            _show_count(report.get("paper_positions_opened"))
        ),
        "Paper-позиции, закрытые: {0}".format(
            _show_count(report.get("paper_positions_closed"))
        ),
        "Заявки Human Approval:",
    ]
    approval = report.get("approval_by_status")
    if approval is None:
        lines.append("  {0}".format(MISSING))
    else:
        for status in APPROVAL_STATUSES:
            lines.append("  {0}: {1}".format(status, approval.get(status, 0)))
        extra = sorted(set(approval) - set(APPROVAL_STATUSES))
        for status in extra:
            lines.append("  {0}: {1}".format(status, approval[status]))
    lines.append("")
    lines.extend(_portfolio_lines("Портфель 1, контроль", _portfolio(report, "control")))
    lines.extend(_portfolio_lines(
        "Портфель 2, Human Approval",
        _portfolio(report, "approval"),
    ))
    lines.append("")
    lines.append("Ошибки проверки рыночных данных: {0}".format(
        _show_count(report.get("market_data_errors"))
    ))
    lines.append("Неопределённая доставка: {0}".format(
        _show_unknown(report.get("unknown_deliveries"))
    ))
    lines.append("Сканер: {0}".format(_run_text(report.get("last_scanner_run"))))
    lines.append("Автопроверки: {0}".format(_run_text(report.get("last_auto_check_run"))))
    return "\n".join(lines)


def send_due_daily_report(
    db_path,
    client,
    chat_id,
    now=None,
    before_send=None,
    lease=DISPATCH_LEASE,
):
    """Одна попытка отправить отчёт завершённых суток UTC.

    Ошибка до вызова Telegram становится failed и может повториться.
    После вызова failed остаётся только при ответе Telegram ok=false.
    Timeout, обрыв и ответ без message_id остаются delivery_unknown.
    """
    moment = _as_datetime(now) if now is not None else datetime.now(timezone.utc).replace(tzinfo=None)
    start, _end = completed_report_window(moment)
    report_date = start.strftime("%Y-%m-%d")
    attempted_at = utc_now_text(moment)
    decision, claimed = claim_daily_report(
        db_path,
        report_date,
        attempted_at,
        lease=lease,
    )
    if decision != "send":
        return _skipped_delivery(db_path, report_date)

    claim_stamp = None if claimed is None else claimed.get("attempted_at")
    try:
        text = render_daily_report(build_daily_report(db_path, moment))
        if before_send is not None:
            before_send()
    except Exception as error:
        release_daily_report_before_send(
            db_path,
            report_date,
            claim_stamp,
            error,
            utc_now_text(moment),
        )
        return {
            "sent": False,
            "report_date": report_date,
            "status": REPORT_FAILED,
            "error": redact_text(error),
        }

    if not mark_daily_report_unknown(db_path, report_date, claim_stamp):
        return _skipped_delivery(db_path, report_date)

    try:
        sent = client.send_message(chat_id, text)
    except TelegramRejectedError as error:
        return _confirmed_rejection(
            db_path,
            report_date,
            error,
            utc_now_text(moment),
        )
    except Exception as error:
        return _uncertain_delivery(db_path, report_date, error)

    message_id = _message_id_of(sent)
    if message_id is None:
        return _uncertain_delivery(
            db_path,
            report_date,
            "telegram response has no message_id",
        )
    mark_daily_report_delivered(
        db_path,
        report_date,
        message_id,
        utc_now_text(moment),
    )
    return {
        "sent": True,
        "report_date": report_date,
        "status": REPORT_DELIVERED,
        "error": None,
        "message_id": message_id,
    }


def deliver_monitoring(db_path, client, chat_id, now=None, telegram_errors=None):
    """
    Отчёт и оповещения исправности через уже созданный Telegram-клиент.

    Второй polling из этого модуля не запускается.
    """
    moment = _as_datetime(now) if now is not None else datetime.now(timezone.utc).replace(tzinfo=None)
    errors = [redact_text(item) for item in (telegram_errors or [])]
    result = {
        "daily": None,
        "health_sent": False,
        "errors": [],
    }
    try:
        result["daily"] = send_due_daily_report(db_path, client, chat_id, now=moment)
    except Exception as error:
        public = redact_text(error)
        result["daily"] = {
            "sent": False,
            "status": "failed",
            "error": public,
        }
        errors.append(public)
    daily_error = (result["daily"] or {}).get("error")
    if daily_error:
        errors.append(daily_error)

    try:
        health = evaluate_health(db_path, moment, telegram_errors=errors)
        changes = health["changes"]
        if changes:
            client.send_message(chat_id, render_health_alert(changes, moment))
            save_health_states(db_path, health["states"], utc_now_text(moment))
            result["health_sent"] = True
    except Exception as error:
        result["errors"].append(redact_text(error))
    result["errors"].extend(
        item for item in errors
        if item and item not in result["errors"]
    )
    return result


def observe_auto_check(
    db_path,
    started_at,
    new_24h_pair_ids,
    price_check_errors,
    paper_stats,
    paper_error,
    approval_stats,
    approval_error,
    finished_at=None,
    api_errors=None,
    approval_created_at=None,
):
    """Пишет диагностику завершившегося auto_check. Позиции не открывает."""
    from monitor_store import record_job_run

    finished = finished_at or utc_now_text()
    approval_created = None
    account_missing = False
    if approval_error is None and approval_stats is not None:
        approval_created = approval_stats.get("requests_created")
        account_missing = bool(approval_stats.get("account_missing"))
    if approval_error is not None or approval_stats is None:
        delivered, undelivered = None, None
    elif account_missing or approval_created == 0:
        delivered, undelivered = 0, 0
    else:
        delivered, undelivered = telegram_delivery_counts(
            db_path,
            approval_created_at,
        )

    if paper_stats is None:
        sql_candidates = None
        baseline_opened = None
        rejections = []
        entry_errors = []
    else:
        sql_candidates = paper_stats.get("sql_candidates")
        baseline_opened = paper_stats.get("opened")
        rejections = list(paper_stats.get("rejections") or [])
        entry_errors = list(paper_stats.get("entry_errors") or [])
    if paper_error is not None:
        entry_errors.append({
            "pair_id": None,
            "reason": "paper_cycle_error",
            "error_type": type(paper_error).__name__,
        })

    diagnostics = build_signal_diagnostics(
        new_24h_events=len(new_24h_pair_ids or []),
        sql_candidates=sql_candidates,
        baseline_opened=baseline_opened,
        rejections=rejections,
        entry_errors=entry_errors,
        price_check_errors=price_check_errors or [],
        approval_requests_created=approval_created,
        approval_account_missing=account_missing,
        approval_error=approval_error,
        telegram_delivered=delivered,
        telegram_undelivered=undelivered,
    )
    status = "ok"
    error_text = None
    if paper_error is not None or approval_error is not None:
        status = "error"
        parts = []
        if paper_error is not None:
            parts.append(type(paper_error).__name__)
        if approval_error is not None:
            parts.append(type(approval_error).__name__)
        error_text = ", ".join(parts)
    record_job_run(
        db_path,
        JOB_AUTO_CHECK,
        started_at,
        status,
        summary={
            "diagnostics": diagnostics,
            "price_check_errors": list(price_check_errors or []),
            "api_errors": list(api_errors or []),
        },
        error_text=error_text,
        finished_at=finished,
    )
    print(format_signal_diagnostics(diagnostics))
    return diagnostics


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Ежедневный отчёт Crypto Radar без polling Telegram",
    )
    parser.add_argument("--db", dest="db_path", default=None)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resolve-unknown", metavar="YYYY-MM-DD")
    parser.add_argument("--as", dest="resolution", choices=(REPORT_DELIVERED, REPORT_FAILED))
    parser.add_argument("--message-id", type=int)
    args = parser.parse_args(argv)
    db_path = args.db_path or database.DB_NAME
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    if args.resolve_unknown:
        if args.resolution is None:
            print("Для неопределённой доставки нужно --as delivered или --as failed")
            return 2
        result = resolve_unknown_delivery(
            db_path,
            args.resolve_unknown,
            args.resolution,
            message_id=args.message_id,
            resolved_at=utc_now_text(now),
        )
        print("Разрешение {0}: {1}".format(
            args.resolve_unknown,
            result.get("status") or result.get("reason"),
        ))
        return 0 if result.get("resolved") else 1
    if args.dry_run or not args.send:
        print(render_daily_report(build_daily_report(db_path, now)))
        return 0
    from approval_bot import TelegramClient, load_settings

    settings = load_settings()
    client = TelegramClient(settings.token)
    result = deliver_monitoring(db_path, client, settings.chat_id, now=now)
    daily = result.get("daily") or {}
    print("Ежедневный отчёт: {0}".format(daily.get("status")))
    if result.get("health_sent"):
        print("Контроль исправности: уведомление отправлено")
    if result.get("errors"):
        print("Ошибки мониторинга: {0}".format(len(result["errors"])))
        return 1
    if daily.get("status") == "failed":
        return 1
    return 0


def _count_new_ideas(db_path, start_text, end_text):
    if not _table_exists(db_path, "pairs"):
        return None
    return _count(
        db_path,
        """
        SELECT COUNT(*)
        FROM pairs
        WHERE created_at IS NOT NULL
          AND datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
        """,
        (start_text, end_text),
    )


def _count_successful_24h(db_path, start_text, end_text):
    if not _table_exists(db_path, "price_checks"):
        return None
    return _count(
        db_path,
        """
        SELECT COUNT(*)
        FROM price_checks
        WHERE check_period = '24h'
          AND price_change_percent IS NOT NULL
          AND new_price_usd > 0
          AND datetime(checked_at) >= datetime(?)
          AND datetime(checked_at) < datetime(?)
        """,
        (start_text, end_text),
    )


def _count_positions_opened(db_path, start_text, end_text):
    if not _table_exists(db_path, "paper_positions"):
        return None
    return _count(
        db_path,
        """
        SELECT COUNT(*)
        FROM paper_positions
        WHERE datetime(created_at) >= datetime(?)
          AND datetime(created_at) < datetime(?)
        """,
        (start_text, end_text),
    )


def _count_positions_closed(db_path, start_text, end_text):
    if not _table_exists(db_path, "paper_positions"):
        return None
    return _count(
        db_path,
        """
        SELECT COUNT(*)
        FROM paper_positions
        WHERE status = 'CLOSED'
          AND exit_time IS NOT NULL
          AND datetime(exit_time) >= datetime(?)
          AND datetime(exit_time) < datetime(?)
        """,
        (start_text, end_text),
    )


def _approval_counts(db_path, start_text, end_text):
    if not _table_exists(db_path, "approval_requests"):
        return None
    connection = _connect(db_path)
    try:
        rows = connection.execute("""
            SELECT status, COUNT(*) AS total
            FROM approval_requests
            WHERE datetime(created_at) >= datetime(?)
              AND datetime(created_at) < datetime(?)
            GROUP BY status
        """, (start_text, end_text)).fetchall()
    finally:
        connection.close()
    counts = {status: 0 for status in APPROVAL_STATUSES}
    for row in rows:
        counts[row["status"]] = row["total"]
    return counts


def _read_portfolio(db_path, portfolio_id):
    if not _table_exists(db_path, "paper_account"):
        return None
    connection = _connect(db_path)
    try:
        account = connection.execute("""
            SELECT cash_usd
            FROM paper_account
            WHERE id = ?
            LIMIT 1
        """, (portfolio_id,)).fetchone()
        if account is None:
            return None
        cash = account["cash_usd"]
        if not _table_exists(db_path, "paper_allocations"):
            return {
                "cash_usd": cash,
                "nav": None,
                "realized_pnl_usd": None,
                "unrealized_pnl_usd": None,
            }
        sums = connection.execute("""
            SELECT
                COALESCE(SUM(CASE WHEN status = 'OPEN' THEN market_value_usd ELSE 0 END), 0)
                    AS open_market_value_usd,
                COALESCE(SUM(CASE WHEN status = 'OPEN' THEN unrealized_pnl_usd ELSE 0 END), 0)
                    AS unrealized_pnl_usd,
                COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN realized_pnl_usd ELSE 0 END), 0)
                    AS realized_pnl_usd
            FROM paper_allocations
            WHERE portfolio_id = ?
        """, (portfolio_id,)).fetchone()
    finally:
        connection.close()
    open_market_value = float(sums["open_market_value_usd"])
    return {
        "cash_usd": float(cash),
        "nav": float(cash) + open_market_value,
        "realized_pnl_usd": float(sums["realized_pnl_usd"]),
        "unrealized_pnl_usd": float(sums["unrealized_pnl_usd"]),
    }


def _market_data_errors(runs):
    if runs is None:
        return None
    if not runs:
        return None
    total = 0
    found = False
    for run in runs:
        if run.get("job_name") != JOB_AUTO_CHECK:
            continue
        found = True
        summary = run.get("summary") or {}
        total += len(summary.get("price_check_errors") or [])
    if not found:
        return None
    return total


def _portfolio(report, name):
    portfolios = report.get("portfolios") or {}
    return portfolios.get(name)


def _portfolio_lines(title, portfolio):
    if portfolio is None:
        return ["{0}: {1}".format(title, MISSING)]
    return [
        "{0}:".format(title),
        "  NAV: {0}".format(_show_money(portfolio.get("nav"))),
        "  Свободные средства: {0}".format(_show_money(portfolio.get("cash_usd"))),
        "  Реализованный PnL: {0}".format(_show_money(portfolio.get("realized_pnl_usd"))),
        "  Нереализованный PnL: {0}".format(
            _show_money(portfolio.get("unrealized_pnl_usd"))
        ),
    ]


def _run_text(run):
    if run is None:
        return MISSING
    finished = run.get("finished_at") or run.get("started_at")
    status = run.get("status") or MISSING
    text = "{0}, {1}".format(status, finished or MISSING)
    if run.get("error_text"):
        text = "{0}, {1}".format(text, redact_text(run.get("error_text")))
    return text


def _show_count(value):
    if value is None:
        return MISSING
    return str(value)


def _show_unknown(value):
    if value is None:
        return MISSING
    if not value:
        return "нет"
    return ", ".join(str(item) for item in value)


def _confirmed_rejection(db_path, report_date, error, failed_at):
    public = redact_text(error)
    mark_daily_report_failed(db_path, report_date, public, failed_at)
    return {
        "sent": False,
        "report_date": report_date,
        "status": REPORT_FAILED,
        "error": public,
    }


def _uncertain_delivery(db_path, report_date, error):
    public = redact_text(error)
    note_uncertain_daily_delivery(db_path, report_date, public)
    return {
        "sent": False,
        "report_date": report_date,
        "status": REPORT_UNKNOWN,
        "error": public,
    }


def _skipped_delivery(db_path, report_date):
    stored = get_daily_report(db_path, report_date)
    status = None if stored is None else stored.get("status")
    return {
        "sent": False,
        "report_date": report_date,
        "status": status or "skipped",
        "error": None,
    }


def _show_money(value):
    if value is None:
        return MISSING
    return "{0:.2f}".format(float(value))


def _count(db_path, sql, params):
    connection = _connect(db_path)
    try:
        row = connection.execute(sql, params).fetchone()
        return int(row[0])
    finally:
        connection.close()


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


def _connect(db_path):
    connection = sqlite3.connect(db_path or database.DB_NAME)
    connection.row_factory = sqlite3.Row
    return connection


def _message_id_of(sent):
    if not isinstance(sent, dict):
        return None
    message_id = sent.get("message_id")
    if isinstance(message_id, int) and message_id > 0:
        return message_id
    return None


def _as_datetime(now):
    if isinstance(now, datetime):
        if now.tzinfo is not None:
            return now.replace(tzinfo=None, microsecond=0)
        return now.replace(microsecond=0)
    text = str(now).strip()
    for time_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, time_format)
        except ValueError:
            continue
    raise ValueError("Не удалось разобрать дату")


def _text(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    raise SystemExit(main())
