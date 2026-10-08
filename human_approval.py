"""
Human Approval v0.7.

Вторая бумажная книга, portfolio_id = 2.
Контрольный портфель 1 этот модуль не покупает и не переоценивает.
Реальных ордеров, ключей и биржевого исполнения здесь нет.
"""

import sqlite3
from datetime import timedelta

import database
from paper_engine import (
    STRATEGY_VERSION,
    calculate_profit_percent,
    format_datetime,
    normalize_price,
    observation_bucket,
    parse_datetime,
    score_cohort,
    utc_now,
)


APPROVAL_PORTFOLIO_ID = 2
DEFAULT_INITIAL_DEPOSIT_USD = 10000.0
DEFAULT_POSITION_PERCENT = 1.0
QUOTE_MAX_AGE_SECONDS = 10
QUOTE_SOURCE = "dexscreener_pair"

STATUS_PENDING = "PENDING"
STATUS_BUY = "BUY"
STATUS_SKIP = "SKIP"
STATUS_EXPIRED = "EXPIRED"

EXPIRY_BASELINE_CLOSED = "BASELINE_CLOSED"
EXPIRY_WINDOW_ENDED = "STRATEGY_WINDOW_ENDED"
EXPIRY_INVALID_WINDOW = "INVALID_STRATEGY_WINDOW"

ACTION_BUY = "buy"
ACTION_SKIP = "skip"

OUTCOME_EXECUTED = "EXECUTED"
OUTCOME_REJECTED = "REJECTED"
OUTCOME_NOOP = "NOOP"

REASON_PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
REASON_QUOTE_STALE = "QUOTE_STALE"
REASON_INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
REASON_BASELINE_CLOSED = "BASELINE_CLOSED"
REASON_BASELINE_MISSING = "BASELINE_MISSING"
REASON_NOT_PENDING = "NOT_PENDING"
REASON_NOT_FOUND = "NOT_FOUND"
REASON_INVALID_SIZE = "INVALID_SIZE"
REASON_ALLOCATION_EXISTS = "ALLOCATION_EXISTS"
REASON_COMMIT_FAILED = "COMMIT_FAILED"

RECOMMENDATION_HUMAN = "HUMAN_APPROVAL"
DECISION_HUMAN_BUY = "HUMAN_BUY"

EVENT_DEPOSIT = "DEPOSIT"
EVENT_BUY = "BUY"
EVENT_SELL = "SELL"

ALLOCATION_OPEN = "OPEN"
ALLOCATION_CLOSED = "CLOSED"
BASELINE_OPEN = "OPEN"
BASELINE_CLOSED = "CLOSED"


def recommend_approval_percent(baseline_position, nav):
    """Доля новой заявки в процентах NAV книги 2. В v0.7 это всегда 1."""
    return DEFAULT_POSITION_PERCENT


def strategy_window(entry_time, max_hold_hours):
    """
    Конец исходного окна baseline.

    Часы берутся из позиции. Константа 168 здесь не используется.
    None означает, что конечное окно посчитать нельзя.
    """
    if max_hold_hours is None or entry_time is None:
        return None
    if isinstance(entry_time, str) and entry_time.strip() == "":
        return None
    try:
        hours = float(max_hold_hours)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    try:
        start = parse_datetime(entry_time)
    except (TypeError, ValueError):
        return None
    return start + timedelta(hours=hours)


def activate_approval_account(now=None, db_path=None):
    """
    Один раз создаёт счёт 2 и один DEPOSIT.

    Повторный вызов не меняет activated_at и не пишет второй депозит.
    Cron этот счёт не создаёт.
    """
    if now is None:
        now = utc_now()
    now_text = format_datetime(now)
    _ensure_portfolio_tables(db_path)
    ensure_approval_tables(db_path)
    connection = _connect(db_path)
    try:
        _begin(connection)
        existing = _select_account(connection)
        if existing is not None:
            _commit(connection)
            return {
                "created": False,
                "activated_at": existing["activated_at"],
                "portfolio_id": APPROVAL_PORTFOLIO_ID,
                "cash_usd": existing["cash_usd"],
            }
        _insert_account_and_deposit(connection, now_text)
        _commit(connection)
        created = _select_account(connection)
        return {
            "created": True,
            "activated_at": created["activated_at"],
            "portfolio_id": APPROVAL_PORTFOLIO_ID,
            "cash_usd": created["cash_usd"],
        }
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def run_approval_maintenance(now=None, db_path=None):
    """
    Журнал и сопровождение уже открытых покупок книги 2.

    Счёт 2 здесь не создаётся. Автоматического BUY нет.
    """
    if now is None:
        now = utc_now()
    stats = _empty_stats()
    if not _account_exists(db_path, APPROVAL_PORTFOLIO_ID):
        stats["account_missing"] = True
        print("HUMAN APPROVAL: счёт не активирован, сопровождение пропущено")
        return stats

    ensure_approval_tables(db_path)
    now_text = format_datetime(now)
    stats["expired"] = _expire_pending(now_text, db_path)
    stats["requests_created"] = _create_requests(now_text, db_path)
    marked, sold = _sync_open_allocations(now_text, db_path)
    stats["mtm_updates"] = marked
    stats["sells"] = sold
    stats["snapshot_inserted"] = _record_snapshot(now_text, db_path)
    _print_maintenance_summary(stats)
    return stats


def list_actionable(now=None, db_path=None):
    """PENDING, baseline OPEN и now строго меньше eligible_until."""
    if now is None:
        now = utc_now()
    if not _table_exists(db_path, "approval_requests"):
        return []
    connection = _connect(db_path)
    try:
        rows = _select_requests_with_baseline(connection, STATUS_PENDING)
    finally:
        connection.close()
    actionable = []
    for row in rows:
        if row.get("baseline_status") != BASELINE_OPEN:
            continue
        if _window_state(row, now) != "open":
            continue
        actionable.append(row)
    return actionable


def decide_buy(request_id, db_path=None, price_fetcher=None, clock=None):
    """
    Свежая DEX-цена снимается до write lock.
    Деньги книги 2 меняются только одним COMMIT.
    """
    clock = clock or utc_now
    if price_fetcher is None:
        price_fetcher = default_approval_price_fetcher

    prepared = _load_request(request_id, db_path)
    if prepared is None:
        return _outcome(request_id, None, OUTCOME_REJECTED, REASON_NOT_FOUND)

    if prepared["status"] != STATUS_PENDING:
        _record_attempt(
            db_path,
            request_id,
            format_datetime(clock()),
            ACTION_BUY,
            OUTCOME_NOOP,
            REASON_NOT_PENDING,
        )
        return _outcome(
            request_id,
            prepared["status"],
            OUTCOME_NOOP,
            REASON_NOT_PENDING,
            allocation_id=prepared.get("allocation_id"),
        )

    execution_price = _fetch_quote(
        prepared.get("chain_id"),
        prepared.get("pair_address"),
        price_fetcher,
    )
    quoted_at = clock()
    quoted_text = format_datetime(quoted_at)
    if execution_price is None:
        _record_attempt(
            db_path,
            request_id,
            quoted_text,
            ACTION_BUY,
            OUTCOME_REJECTED,
            REASON_PRICE_UNAVAILABLE,
        )
        return _outcome(
            request_id,
            STATUS_PENDING,
            OUTCOME_REJECTED,
            REASON_PRICE_UNAVAILABLE,
        )

    connection = _connect(db_path)
    try:
        _begin(connection)
        execution_at = clock()
        execution_text = format_datetime(execution_at)
        request = _select_request(connection, request_id)
        if request is None:
            _rollback(connection)
            return _outcome(request_id, None, OUTCOME_REJECTED, REASON_NOT_FOUND)
        if request["status"] != STATUS_PENDING:
            _rollback(connection)
            connection.close()
            connection = None
            _record_attempt(
                db_path,
                request_id,
                execution_text,
                ACTION_BUY,
                OUTCOME_NOOP,
                REASON_NOT_PENDING,
            )
            return _outcome(
                request_id,
                request["status"],
                OUTCOME_NOOP,
                REASON_NOT_PENDING,
                allocation_id=request.get("allocation_id"),
            )

        window = _window_state(request, execution_at)
        if window == "invalid":
            return _commit_expiry(
                connection,
                request,
                execution_text,
                EXPIRY_INVALID_WINDOW,
                ACTION_BUY,
            )
        if window == "ended":
            return _commit_expiry(
                connection,
                request,
                execution_text,
                EXPIRY_WINDOW_ENDED,
                ACTION_BUY,
            )

        baseline = _select_baseline(connection, request["position_id"])
        if baseline is None or baseline["id"] != request["position_id"]:
            _rollback(connection)
            connection.close()
            connection = None
            _record_attempt(
                db_path,
                request_id,
                execution_text,
                ACTION_BUY,
                OUTCOME_REJECTED,
                REASON_BASELINE_MISSING,
            )
            return _outcome(
                request_id,
                STATUS_PENDING,
                OUTCOME_REJECTED,
                REASON_BASELINE_MISSING,
            )
        if baseline["status"] != BASELINE_OPEN:
            _rollback(connection)
            connection.close()
            connection = None
            _record_attempt(
                db_path,
                request_id,
                execution_text,
                ACTION_BUY,
                OUTCOME_REJECTED,
                REASON_BASELINE_CLOSED,
            )
            return _outcome(
                request_id,
                STATUS_PENDING,
                OUTCOME_REJECTED,
                REASON_BASELINE_CLOSED,
            )
        if not _quote_is_fresh(quoted_at, execution_at):
            _rollback(connection)
            connection.close()
            connection = None
            _record_attempt(
                db_path,
                request_id,
                execution_text,
                ACTION_BUY,
                OUTCOME_REJECTED,
                REASON_QUOTE_STALE,
            )
            return _outcome(
                request_id,
                STATUS_PENDING,
                OUTCOME_REJECTED,
                REASON_QUOTE_STALE,
            )

        recommended_usd = _positive_amount(request.get("recommended_usd"))
        if recommended_usd is None:
            _rollback(connection)
            connection.close()
            connection = None
            _record_attempt(
                db_path,
                request_id,
                execution_text,
                ACTION_BUY,
                OUTCOME_REJECTED,
                REASON_INVALID_SIZE,
            )
            return _outcome(
                request_id,
                STATUS_PENDING,
                OUTCOME_REJECTED,
                REASON_INVALID_SIZE,
            )

        account = _select_account(connection)
        if account is None or float(account["cash_usd"]) < recommended_usd:
            _rollback(connection)
            connection.close()
            connection = None
            _record_attempt(
                db_path,
                request_id,
                execution_text,
                ACTION_BUY,
                OUTCOME_REJECTED,
                REASON_INSUFFICIENT_CASH,
            )
            return _outcome(
                request_id,
                STATUS_PENDING,
                OUTCOME_REJECTED,
                REASON_INSUFFICIENT_CASH,
            )
        if _allocation_exists(connection, request["position_id"]):
            _rollback(connection)
            connection.close()
            connection = None
            _record_attempt(
                db_path,
                request_id,
                execution_text,
                ACTION_BUY,
                OUTCOME_REJECTED,
                REASON_ALLOCATION_EXISTS,
            )
            return _outcome(
                request_id,
                STATUS_PENDING,
                OUTCOME_REJECTED,
                REASON_ALLOCATION_EXISTS,
            )

        allocation_id = _insert_buy_allocation(
            connection,
            request,
            execution_text,
            execution_price,
            float(account["cash_usd"]),
            recommended_usd,
        )
        _mark_bought(
            connection,
            request,
            execution_text,
            quoted_text,
            execution_price,
            allocation_id,
        )
        _insert_attempt(
            connection,
            request_id,
            execution_text,
            ACTION_BUY,
            OUTCOME_EXECUTED,
            None,
        )
        _commit(connection)
        return _outcome(
            request_id,
            STATUS_BUY,
            OUTCOME_EXECUTED,
            None,
            allocation_id=allocation_id,
            execution_price=execution_price,
            quoted_at=quoted_text,
            execution_at=execution_text,
        )
    except Exception as error:
        if connection is not None:
            _rollback(connection)
        return _outcome(
            request_id,
            STATUS_PENDING,
            OUTCOME_REJECTED,
            REASON_COMMIT_FAILED,
            detail=str(error),
        )
    finally:
        if connection is not None:
            connection.close()


def decide_skip(request_id, db_path=None, clock=None):
    """SKIP только пока baseline OPEN и окно снимка ещё не закончилось."""
    clock = clock or utc_now
    connection = _connect(db_path)
    try:
        _begin(connection)
        now = clock()
        now_text = format_datetime(now)
        request = _select_request(connection, request_id)
        if request is None:
            _rollback(connection)
            return _outcome(request_id, None, OUTCOME_REJECTED, REASON_NOT_FOUND)
        if request["status"] != STATUS_PENDING:
            _rollback(connection)
            connection.close()
            connection = None
            _record_attempt(
                db_path,
                request_id,
                now_text,
                ACTION_SKIP,
                OUTCOME_NOOP,
                REASON_NOT_PENDING,
            )
            return _outcome(
                request_id,
                request["status"],
                OUTCOME_NOOP,
                REASON_NOT_PENDING,
            )

        baseline = _select_baseline(connection, request["position_id"])
        baseline_status = None if baseline is None else baseline["status"]
        if baseline is None:
            return _commit_expiry(
                connection,
                request,
                now_text,
                EXPIRY_BASELINE_CLOSED,
                ACTION_SKIP,
            )

        status, reason = _classify_snapshot(baseline_status, request, now)
        if status == STATUS_PENDING:
            _mark_skipped(connection, request, now_text)
            _insert_attempt(
                connection,
                request_id,
                now_text,
                ACTION_SKIP,
                OUTCOME_EXECUTED,
                None,
            )
            _commit(connection)
            return _outcome(request_id, STATUS_SKIP, OUTCOME_EXECUTED, None)
        return _commit_expiry(connection, request, now_text, reason, ACTION_SKIP)
    except Exception as error:
        if connection is not None:
            _rollback(connection)
        return _outcome(
            request_id,
            None,
            OUTCOME_REJECTED,
            REASON_COMMIT_FAILED,
            detail=str(error),
        )
    finally:
        if connection is not None:
            connection.close()


def ensure_approval_tables(db_path=None):
    """Журнал решений. Таблицы контроля и baseline здесь не меняются."""
    connection = _connect(db_path)
    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS approval_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER NOT NULL UNIQUE,
                pair_id INTEGER NOT NULL,
                chain_id TEXT,
                pair_address TEXT,
                strategy_version TEXT,
                final_score INTEGER,
                signal_type TEXT,
                cohort TEXT,
                change_24h REAL,
                recommended_percent REAL,
                recommended_usd REAL,
                reference_price REAL,
                signal_created_at TIMESTAMP,
                position_created_at TIMESTAMP,
                max_hold_hours REAL,
                eligible_until TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                status TEXT NOT NULL,
                expiry_reason TEXT,
                expired_at TIMESTAMP,
                decision_at TIMESTAMP,
                execution_at TIMESTAMP,
                delay_signal_to_decision_seconds INTEGER,
                delay_decision_to_execution_seconds INTEGER,
                execution_price REAL,
                quoted_at TIMESTAMP,
                quote_source TEXT,
                allocation_id INTEGER,
                portfolio_id INTEGER NOT NULL,
                last_reject_reason TEXT,
                last_reject_at TIMESTAMP,
                FOREIGN KEY (position_id) REFERENCES paper_positions (id),
                FOREIGN KEY (portfolio_id) REFERENCES paper_account (id),
                FOREIGN KEY (allocation_id) REFERENCES paper_allocations (id)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS approval_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                attempted_at TIMESTAMP NOT NULL,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT,
                FOREIGN KEY (request_id) REFERENCES approval_requests (id)
            )
        """)
    finally:
        connection.close()


def default_approval_price_fetcher(chain_id, pair_address):
    """Текущая цена пары. В paper_price_marks и baseline она не пишется."""
    from paper_engine import default_price_fetcher

    return default_price_fetcher(chain_id, pair_address)


def _ensure_portfolio_tables(db_path):
    from paper_portfolio import ensure_portfolio_tables

    ensure_portfolio_tables(db_path)


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


def _empty_stats():
    return {
        "account_missing": False,
        "requests_created": 0,
        "expired": 0,
        "mtm_updates": 0,
        "sells": 0,
        "snapshot_inserted": False,
    }


def _account_exists(db_path, portfolio_id):
    if not _table_exists(db_path, "paper_account"):
        return False
    connection = _connect(db_path)
    try:
        row = connection.execute(
            "SELECT id FROM paper_account WHERE id = ? LIMIT 1",
            (portfolio_id,),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


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


def _insert_account_and_deposit(connection, now_text):
    deposit = DEFAULT_INITIAL_DEPOSIT_USD
    connection.execute("""
        INSERT INTO paper_account (
            id,
            currency,
            initial_deposit_usd,
            cash_usd,
            activated_at,
            peak_equity_usd,
            max_drawdown_percent,
            created_at,
            updated_at
        )
        VALUES (?, 'USD', ?, ?, ?, ?, 0, ?, ?)
    """, (
        APPROVAL_PORTFOLIO_ID,
        deposit,
        deposit,
        now_text,
        deposit,
        now_text,
        now_text,
    ))
    connection.execute("""
        INSERT INTO paper_cash_ledger (
            portfolio_id,
            allocation_id,
            event_type,
            amount_usd,
            cash_after_usd,
            created_at
        )
        VALUES (?, NULL, ?, ?, ?, ?)
    """, (
        APPROVAL_PORTFOLIO_ID,
        EVENT_DEPOSIT,
        deposit,
        deposit,
        now_text,
    ))


def _expire_pending(now_text, db_path):
    connection = _connect(db_path)
    expired = 0
    try:
        _begin(connection)
        rows = _select_requests_with_baseline(connection, STATUS_PENDING)
        for request in rows:
            if request.get("baseline_status") is None:
                reason = EXPIRY_BASELINE_CLOSED
            else:
                status, reason = _classify_snapshot(
                    request["baseline_status"],
                    request,
                    now_text,
                )
                if status != STATUS_EXPIRED:
                    continue
            _apply_expiry(connection, request["id"], now_text, reason)
            expired += 1
        _commit(connection)
        return expired
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _create_requests(now_text, db_path):
    connection = _connect(db_path)
    try:
        account = _select_account(connection)
        if account is None:
            return 0
        nav = _nav_from_connection(connection, account)
        baselines = _select_new_baselines(connection, account["activated_at"])
    finally:
        connection.close()

    created = 0
    for baseline in baselines:
        if _insert_request(baseline, now_text, nav, db_path):
            created += 1
    return created


def _insert_request(baseline, now_text, nav, db_path):
    status, reason, eligible = _classify_new(
        baseline.get("status"),
        baseline.get("max_hold_hours"),
        baseline.get("entry_time"),
        now_text,
    )
    equity = float(nav["total_equity_usd"])
    percent = float(recommend_approval_percent(baseline, equity))
    recommended_usd = equity * percent / 100.0
    eligible_text = None if eligible is None else format_datetime(eligible)
    connection = _connect(db_path)
    try:
        _begin(connection)
        if _select_request_by_position(connection, baseline["id"]) is not None:
            _rollback(connection)
            return False
        connection.execute("""
            INSERT INTO approval_requests (
                position_id,
                pair_id,
                chain_id,
                pair_address,
                strategy_version,
                final_score,
                signal_type,
                cohort,
                change_24h,
                recommended_percent,
                recommended_usd,
                reference_price,
                signal_created_at,
                position_created_at,
                max_hold_hours,
                eligible_until,
                created_at,
                status,
                expiry_reason,
                expired_at,
                portfolio_id
            )
            VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?
            )
        """, (
            baseline["id"],
            baseline["pair_id"],
            baseline.get("chain_id"),
            baseline.get("pair_address"),
            baseline.get("strategy_version"),
            baseline.get("final_score"),
            baseline.get("signal_type"),
            score_cohort(baseline.get("final_score")),
            baseline.get("change_24h"),
            percent,
            recommended_usd,
            baseline.get("entry_price"),
            baseline.get("entry_time"),
            baseline.get("created_at"),
            baseline.get("max_hold_hours"),
            eligible_text,
            now_text,
            status,
            reason,
            now_text if status == STATUS_EXPIRED else None,
            APPROVAL_PORTFOLIO_ID,
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


def _classify_new(baseline_status, max_hold_hours, entry_time, now):
    eligible = strategy_window(entry_time, max_hold_hours)
    if eligible is None:
        return STATUS_EXPIRED, EXPIRY_INVALID_WINDOW, None
    moment = parse_datetime(now)
    if baseline_status == BASELINE_CLOSED:
        return STATUS_EXPIRED, EXPIRY_BASELINE_CLOSED, eligible
    if moment >= eligible:
        return STATUS_EXPIRED, EXPIRY_WINDOW_ENDED, eligible
    if baseline_status == BASELINE_OPEN and moment < eligible:
        return STATUS_PENDING, None, eligible
    return STATUS_EXPIRED, EXPIRY_BASELINE_CLOSED, eligible


def _classify_snapshot(baseline_status, request, now):
    if _window_state(request, now) == "invalid":
        return STATUS_EXPIRED, EXPIRY_INVALID_WINDOW
    moment = parse_datetime(now)
    eligible = parse_datetime(request["eligible_until"])
    if baseline_status == BASELINE_CLOSED:
        return STATUS_EXPIRED, EXPIRY_BASELINE_CLOSED
    if moment >= eligible:
        return STATUS_EXPIRED, EXPIRY_WINDOW_ENDED
    if baseline_status == BASELINE_OPEN and moment < eligible:
        return STATUS_PENDING, None
    return STATUS_EXPIRED, EXPIRY_BASELINE_CLOSED


def _window_state(request, moment):
    max_hold_hours = request.get("max_hold_hours")
    eligible_until = request.get("eligible_until")
    try:
        hours = float(max_hold_hours)
    except (TypeError, ValueError):
        return "invalid"
    if hours <= 0 or eligible_until is None:
        return "invalid"
    try:
        deadline = parse_datetime(eligible_until)
        current = parse_datetime(moment)
    except (TypeError, ValueError):
        return "invalid"
    if current >= deadline:
        return "ended"
    return "open"


def _sync_open_allocations(now_text, db_path):
    connection = _connect(db_path)
    try:
        rows = _select_open_allocations(connection)
    finally:
        connection.close()

    marked = 0
    sold = 0
    for allocation in rows:
        if allocation["baseline_status"] == BASELINE_OPEN:
            if _mark_open_allocation(allocation, now_text, db_path):
                marked += 1
            continue
        if allocation["baseline_status"] == BASELINE_CLOSED:
            if _sell_allocation(allocation, now_text, db_path):
                sold += 1
    return marked, sold


def _mark_open_allocation(allocation, now_text, db_path):
    last_price = _positive_price(allocation.get("baseline_last_price"))
    quantity = allocation.get("quantity")
    if last_price is None or quantity is None:
        return False
    market_value = float(quantity) * last_price
    unrealized = market_value - float(allocation["allocated_usd"])
    connection = _connect(db_path)
    try:
        _begin(connection)
        cursor = connection.execute("""
            UPDATE paper_allocations
            SET
                last_price = ?,
                market_value_usd = ?,
                unrealized_pnl_usd = ?,
                updated_at = ?
            WHERE id = ?
              AND portfolio_id = ?
              AND status = 'OPEN'
        """, (
            last_price,
            market_value,
            unrealized,
            now_text,
            allocation["id"],
            APPROVAL_PORTFOLIO_ID,
        ))
        updated = cursor.rowcount == 1
        _commit(connection)
        return updated
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _sell_allocation(allocation, now_text, db_path):
    exit_price = _positive_price(allocation.get("baseline_exit_price"))
    entry_price = _positive_price(allocation.get("entry_price"))
    quantity = allocation.get("quantity")
    if exit_price is None or entry_price is None or quantity is None:
        return False
    proceeds = float(quantity) * exit_price
    realized = proceeds - float(allocation["allocated_usd"])
    result_percent = calculate_profit_percent(entry_price, exit_price)
    connection = _connect(db_path)
    try:
        _begin(connection)
        account = _select_account(connection)
        if account is None:
            _rollback(connection)
            return False
        cursor = connection.execute("""
            UPDATE paper_allocations
            SET
                status = 'CLOSED',
                exit_price = ?,
                exit_time = ?,
                exit_reason = ?,
                result_percent = ?,
                last_price = ?,
                market_value_usd = 0,
                unrealized_pnl_usd = 0,
                realized_pnl_usd = ?,
                updated_at = ?
            WHERE id = ?
              AND portfolio_id = ?
              AND status = 'OPEN'
        """, (
            exit_price,
            allocation.get("baseline_exit_time"),
            allocation.get("baseline_exit_reason"),
            result_percent,
            exit_price,
            realized,
            now_text,
            allocation["id"],
            APPROVAL_PORTFOLIO_ID,
        ))
        if cursor.rowcount != 1:
            _rollback(connection)
            return False
        cash_after = float(account["cash_usd"]) + proceeds
        connection.execute("""
            INSERT INTO paper_cash_ledger (
                portfolio_id,
                allocation_id,
                event_type,
                amount_usd,
                cash_after_usd,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            APPROVAL_PORTFOLIO_ID,
            allocation["id"],
            EVENT_SELL,
            proceeds,
            cash_after,
            now_text,
        ))
        connection.execute("""
            UPDATE paper_account
            SET cash_usd = ?, updated_at = ?
            WHERE id = ?
        """, (
            cash_after,
            now_text,
            APPROVAL_PORTFOLIO_ID,
        ))
        _commit(connection)
        return True
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _record_snapshot(now_text, db_path):
    bucket = observation_bucket(now_text)
    connection = _connect(db_path)
    try:
        _begin(connection)
        account = _select_account(connection)
        if account is None:
            _rollback(connection)
            return False
        nav = _nav_from_connection(connection, account)
        equity = float(nav["total_equity_usd"])
        peak = float(account["peak_equity_usd"])
        if equity > peak:
            peak = equity
        if peak > 0:
            drawdown = (peak - equity) / peak * 100.0
        else:
            drawdown = 0.0
        if drawdown < 0:
            drawdown = 0.0
        max_drawdown = float(account["max_drawdown_percent"])
        if drawdown > max_drawdown:
            max_drawdown = drawdown
        connection.execute("""
            INSERT OR IGNORE INTO paper_nav_snapshots (
                portfolio_id,
                observed_at,
                observation_bucket,
                cash_usd,
                open_market_value_usd,
                total_equity_usd,
                realized_pnl_usd,
                unrealized_pnl_usd,
                drawdown_percent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            APPROVAL_PORTFOLIO_ID,
            now_text,
            bucket,
            nav["cash_usd"],
            nav["open_market_value_usd"],
            equity,
            nav["realized_pnl_usd"],
            nav["unrealized_pnl_usd"],
            drawdown,
        ))
        inserted = connection.execute("SELECT changes()").fetchone()[0] == 1
        if inserted:
            connection.execute("""
                UPDATE paper_account
                SET
                    peak_equity_usd = ?,
                    max_drawdown_percent = ?,
                    updated_at = ?
                WHERE id = ?
            """, (
                peak,
                max_drawdown,
                now_text,
                APPROVAL_PORTFOLIO_ID,
            ))
        _commit(connection)
        return inserted
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _fetch_quote(chain_id, pair_address, price_fetcher):
    if chain_id is None or pair_address is None:
        return None
    if str(chain_id).strip() == "" or str(pair_address).strip() == "":
        return None
    try:
        price = price_fetcher(chain_id, pair_address)
    except Exception as error:
        print("HUMAN APPROVAL: котировка не получена")
        print(error)
        return None
    return normalize_price(price)


def _quote_is_fresh(quoted_at, execution_at):
    delta = (
        parse_datetime(execution_at) - parse_datetime(quoted_at)
    ).total_seconds()
    return 0 <= delta <= QUOTE_MAX_AGE_SECONDS


def _commit_expiry(connection, request, now_text, reason, action):
    _apply_expiry(connection, request["id"], now_text, reason)
    _insert_attempt(
        connection,
        request["id"],
        now_text,
        action,
        OUTCOME_REJECTED,
        reason,
    )
    _commit(connection)
    return _outcome(request["id"], STATUS_EXPIRED, OUTCOME_REJECTED, reason)


def _apply_expiry(connection, request_id, now_text, reason):
    cursor = connection.execute("""
        UPDATE approval_requests
        SET
            status = ?,
            expiry_reason = ?,
            expired_at = ?
        WHERE id = ?
          AND status = ?
    """, (
        STATUS_EXPIRED,
        reason,
        now_text,
        request_id,
        STATUS_PENDING,
    ))
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("заявка уже не PENDING")


def _mark_skipped(connection, request, now_text):
    cursor = connection.execute("""
        UPDATE approval_requests
        SET
            status = ?,
            decision_at = ?,
            delay_signal_to_decision_seconds = ?
        WHERE id = ?
          AND status = ?
    """, (
        STATUS_SKIP,
        now_text,
        _delay_seconds(request.get("signal_created_at"), now_text),
        request["id"],
        STATUS_PENDING,
    ))
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("заявка уже не PENDING")


def _mark_bought(
    connection,
    request,
    execution_text,
    quoted_text,
    execution_price,
    allocation_id,
):
    cursor = connection.execute("""
        UPDATE approval_requests
        SET
            status = ?,
            decision_at = ?,
            execution_at = ?,
            delay_signal_to_decision_seconds = ?,
            delay_decision_to_execution_seconds = ?,
            execution_price = ?,
            quoted_at = ?,
            quote_source = ?,
            allocation_id = ?
        WHERE id = ?
          AND status = ?
    """, (
        STATUS_BUY,
        execution_text,
        execution_text,
        _delay_seconds(request.get("signal_created_at"), execution_text),
        _delay_seconds(execution_text, execution_text),
        execution_price,
        quoted_text,
        QUOTE_SOURCE,
        allocation_id,
        request["id"],
        STATUS_PENDING,
    ))
    if cursor.rowcount != 1:
        raise sqlite3.IntegrityError("заявка уже не PENDING")


def _insert_buy_allocation(
    connection,
    request,
    execution_text,
    execution_price,
    cash_usd,
    recommended_usd,
):
    quantity = recommended_usd / execution_price
    cash_after = cash_usd - recommended_usd
    recommended_percent = request.get("recommended_percent")
    cursor = connection.execute("""
        INSERT INTO paper_allocations (
            portfolio_id,
            position_id,
            pair_id,
            strategy_version,
            status,
            skip_reason,
            recommendation,
            decision,
            decision_time,
            recommended_percent,
            recommended_usd,
            allocated_percent,
            allocated_usd,
            quantity,
            entry_price,
            entry_time,
            signal_type,
            final_score,
            cohort,
            last_price,
            market_value_usd,
            unrealized_pnl_usd,
            realized_pnl_usd,
            created_at,
            updated_at
        )
        VALUES (
            ?, ?, ?, ?, ?,
            NULL, ?, ?, ?,
            ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?, 0, 0,
            ?, ?
        )
    """, (
        APPROVAL_PORTFOLIO_ID,
        request["position_id"],
        request["pair_id"],
        request.get("strategy_version"),
        ALLOCATION_OPEN,
        RECOMMENDATION_HUMAN,
        DECISION_HUMAN_BUY,
        execution_text,
        recommended_percent,
        recommended_usd,
        recommended_percent,
        recommended_usd,
        quantity,
        execution_price,
        execution_text,
        request.get("signal_type"),
        request.get("final_score"),
        request.get("cohort"),
        execution_price,
        recommended_usd,
        execution_text,
        execution_text,
    ))
    allocation_id = cursor.lastrowid
    connection.execute("""
        INSERT INTO paper_cash_ledger (
            portfolio_id,
            allocation_id,
            event_type,
            amount_usd,
            cash_after_usd,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        APPROVAL_PORTFOLIO_ID,
        allocation_id,
        EVENT_BUY,
        -recommended_usd,
        cash_after,
        execution_text,
    ))
    connection.execute("""
        UPDATE paper_account
        SET cash_usd = ?, updated_at = ?
        WHERE id = ?
    """, (
        cash_after,
        execution_text,
        APPROVAL_PORTFOLIO_ID,
    ))
    return allocation_id


def _record_attempt(db_path, request_id, attempted_at, action, outcome, reason):
    connection = _connect(db_path)
    try:
        _begin(connection)
        _insert_attempt(
            connection,
            request_id,
            attempted_at,
            action,
            outcome,
            reason,
        )
        if outcome == OUTCOME_REJECTED:
            connection.execute("""
                UPDATE approval_requests
                SET last_reject_reason = ?, last_reject_at = ?
                WHERE id = ? AND status = ?
            """, (
                reason,
                attempted_at,
                request_id,
                STATUS_PENDING,
            ))
        _commit(connection)
    except Exception:
        _rollback(connection)
        raise
    finally:
        connection.close()


def _insert_attempt(connection, request_id, attempted_at, action, outcome, reason):
    connection.execute("""
        INSERT INTO approval_attempts (
            request_id,
            attempted_at,
            action,
            outcome,
            reason
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        request_id,
        attempted_at,
        action,
        outcome,
        reason,
    ))


def _load_request(request_id, db_path):
    if not _table_exists(db_path, "approval_requests"):
        return None
    connection = _connect(db_path)
    try:
        return _select_request(connection, request_id)
    finally:
        connection.close()


def _select_account(connection):
    row = connection.execute(
        "SELECT * FROM paper_account WHERE id = ? LIMIT 1",
        (APPROVAL_PORTFOLIO_ID,),
    ).fetchone()
    return _row(row)


def _select_request(connection, request_id):
    row = connection.execute(
        "SELECT * FROM approval_requests WHERE id = ? LIMIT 1",
        (request_id,),
    ).fetchone()
    return _row(row)


def _select_request_by_position(connection, position_id):
    row = connection.execute(
        "SELECT id FROM approval_requests WHERE position_id = ? LIMIT 1",
        (position_id,),
    ).fetchone()
    return _row(row)


def _select_baseline(connection, position_id):
    row = connection.execute(
        "SELECT * FROM paper_positions WHERE id = ? LIMIT 1",
        (position_id,),
    ).fetchone()
    return _row(row)


def _allocation_exists(connection, position_id):
    row = connection.execute("""
        SELECT id
        FROM paper_allocations
        WHERE portfolio_id = ? AND position_id = ?
        LIMIT 1
    """, (
        APPROVAL_PORTFOLIO_ID,
        position_id,
    )).fetchone()
    return row is not None


def _select_new_baselines(connection, activated_at):
    rows = connection.execute("""
        SELECT
            pp.id,
            pp.pair_id,
            pp.strategy_version,
            pp.signal_type,
            pp.final_score,
            pp.change_24h,
            pp.status,
            pp.entry_price,
            pp.entry_time,
            pp.created_at,
            pp.max_hold_hours,
            p.chain_id,
            p.pair_address
        FROM paper_positions AS pp
        LEFT JOIN pairs AS p
            ON p.id = pp.pair_id
        WHERE pp.strategy_version = ?
          AND datetime(pp.created_at) >= datetime(?)
          AND NOT EXISTS (
              SELECT 1
              FROM approval_requests AS r
              WHERE r.position_id = pp.id
          )
        ORDER BY pp.id ASC
    """, (
        STRATEGY_VERSION,
        activated_at,
    )).fetchall()
    return [_row(row) for row in rows]


def _select_requests_with_baseline(connection, status):
    rows = connection.execute("""
        SELECT
            r.*,
            pp.status AS baseline_status,
            p.pair_symbol AS pair_symbol
        FROM approval_requests AS r
        LEFT JOIN paper_positions AS pp
            ON pp.id = r.position_id
        LEFT JOIN pairs AS p
            ON p.id = r.pair_id
        WHERE r.status = ?
        ORDER BY r.id ASC
    """, (status,)).fetchall()
    return [_row(row) for row in rows]


def _select_open_allocations(connection):
    rows = connection.execute("""
        SELECT
            a.id,
            a.portfolio_id,
            a.position_id,
            a.quantity,
            a.allocated_usd,
            a.entry_price,
            pp.status AS baseline_status,
            pp.last_price AS baseline_last_price,
            pp.exit_price AS baseline_exit_price,
            pp.exit_time AS baseline_exit_time,
            pp.exit_reason AS baseline_exit_reason,
            pp.result_percent AS baseline_result_percent
        FROM paper_allocations AS a
        JOIN paper_positions AS pp
            ON pp.id = a.position_id
        WHERE a.portfolio_id = ?
          AND a.status = 'OPEN'
        ORDER BY a.id ASC
    """, (APPROVAL_PORTFOLIO_ID,)).fetchall()
    return [_row(row) for row in rows]


def _nav_from_connection(connection, account):
    row = connection.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN status = 'OPEN' THEN market_value_usd ELSE 0 END), 0)
                AS open_market_value_usd,
            COALESCE(SUM(CASE WHEN status = 'OPEN' THEN unrealized_pnl_usd ELSE 0 END), 0)
                AS unrealized_pnl_usd,
            COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN realized_pnl_usd ELSE 0 END), 0)
                AS realized_pnl_usd
        FROM paper_allocations
        WHERE portfolio_id = ?
    """, (account["id"],)).fetchone()
    sums = _row(row)
    cash = float(account["cash_usd"])
    open_market_value = float(sums["open_market_value_usd"])
    return {
        "cash_usd": cash,
        "open_market_value_usd": open_market_value,
        "total_equity_usd": cash + open_market_value,
        "realized_pnl_usd": float(sums["realized_pnl_usd"]),
        "unrealized_pnl_usd": float(sums["unrealized_pnl_usd"]),
    }


def _delay_seconds(start, end):
    if start is None or end is None:
        return None
    try:
        return int((parse_datetime(end) - parse_datetime(start)).total_seconds())
    except (TypeError, ValueError):
        return None


def _positive_price(value):
    return normalize_price(value)


def _positive_amount(value):
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return amount


def _row(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _outcome(
    request_id,
    status,
    outcome,
    reason,
    allocation_id=None,
    execution_price=None,
    quoted_at=None,
    execution_at=None,
    detail=None,
):
    return {
        "request_id": request_id,
        "status": status,
        "outcome": outcome,
        "reason": reason,
        "allocation_id": allocation_id,
        "execution_price": execution_price,
        "quoted_at": quoted_at,
        "execution_at": execution_at,
        "detail": detail,
    }


def _print_maintenance_summary(stats):
    print()
    print("ИТОГ HUMAN APPROVAL")
    print("====================")
    print("Новых заявок:", stats["requests_created"])
    print("EXPIRED:", stats["expired"])
    print("MTM:", stats["mtm_updates"])
    print("SELL:", stats["sells"])
    print("Новый NAV snapshot:", stats["snapshot_inserted"])
