from database import ensure_paper_tables, get_paper_connection
from paper_engine import (
    STRATEGY_VERSION,
    format_datetime,
    observation_bucket,
    score_cohort,
    utc_now,
)


DEFAULT_INITIAL_DEPOSIT_USD = 10000.0
DEFAULT_POSITION_PERCENT = 1.0
PORTFOLIO_ID = 1

RECOMMENDATION_BASELINE_BUY = "BASELINE_BUY"
DECISION_AUTO_PAPER_BUY = "AUTO_PAPER_BUY"
DECISION_SKIPPED = "SKIPPED"

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"
STATUS_SKIPPED = "SKIPPED"

SKIP_INSUFFICIENT_CASH = "INSUFFICIENT_CASH"

EVENT_DEPOSIT = "DEPOSIT"
EVENT_BUY = "BUY"
EVENT_SELL = "SELL"


def recommend_position_percent(baseline_position, nav):
    """
    Размер новой сделки в процентах NAV.

    v0.5 всегда возвращает DEFAULT_POSITION_PERCENT.
    Позже Risk Engine сможет заменить эту функцию, не меняя таблицы.
    """
    return DEFAULT_POSITION_PERCENT


def ensure_portfolio_tables(db_path=None):
    """
    Создаёт только таблицы портфеля.

    paper_positions и paper_price_marks создаются прежним
    ensure_paper_tables и здесь не изменяются.
    """
    ensure_paper_tables(db_path)
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paper_account (
            id INTEGER PRIMARY KEY,
            currency TEXT NOT NULL DEFAULT 'USD',
            initial_deposit_usd REAL NOT NULL,
            cash_usd REAL NOT NULL,
            activated_at TIMESTAMP NOT NULL,
            peak_equity_usd REAL NOT NULL,
            max_drawdown_percent REAL NOT NULL,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paper_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id INTEGER NOT NULL,
            position_id INTEGER NOT NULL,
            pair_id INTEGER NOT NULL,
            strategy_version TEXT,
            status TEXT NOT NULL,
            skip_reason TEXT,
            recommendation TEXT,
            decision TEXT,
            decision_time TIMESTAMP,
            recommended_percent REAL,
            recommended_usd REAL,
            allocated_percent REAL,
            allocated_usd REAL,
            quantity REAL,
            entry_price REAL,
            entry_time TIMESTAMP,
            exit_price REAL,
            exit_time TIMESTAMP,
            exit_reason TEXT,
            result_percent REAL,
            signal_type TEXT,
            final_score INTEGER,
            cohort TEXT,
            last_price REAL,
            market_value_usd REAL,
            unrealized_pnl_usd REAL,
            realized_pnl_usd REAL,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            UNIQUE (portfolio_id, position_id),
            FOREIGN KEY (portfolio_id) REFERENCES paper_account (id),
            FOREIGN KEY (position_id) REFERENCES paper_positions (id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paper_cash_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id INTEGER NOT NULL,
            allocation_id INTEGER,
            event_type TEXT NOT NULL,
            amount_usd REAL NOT NULL,
            cash_after_usd REAL NOT NULL,
            created_at TIMESTAMP NOT NULL,
            FOREIGN KEY (portfolio_id) REFERENCES paper_account (id),
            FOREIGN KEY (allocation_id) REFERENCES paper_allocations (id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paper_nav_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id INTEGER NOT NULL,
            observed_at TIMESTAMP NOT NULL,
            observation_bucket TEXT NOT NULL,
            cash_usd REAL NOT NULL,
            open_market_value_usd REAL NOT NULL,
            total_equity_usd REAL NOT NULL,
            realized_pnl_usd REAL NOT NULL,
            unrealized_pnl_usd REAL NOT NULL,
            drawdown_percent REAL NOT NULL,
            UNIQUE (portfolio_id, observation_bucket),
            FOREIGN KEY (portfolio_id) REFERENCES paper_account (id)
        )
    """)

    connection.commit()
    connection.close()


def ensure_portfolio(now=None, db_path=None):
    """
    Создаёт единственный счёт v0.5 до run_cycle.

    Повторный вызов не меняет activated_at и не пишет второй DEPOSIT.
    """
    if now is None:
        now = utc_now()

    ensure_portfolio_tables(db_path)
    now_text = format_datetime(now)
    connection = get_paper_connection(db_path)

    try:
        existing = _select_account(connection)

        if existing is not None:
            return {
                "created": False,
                "activated_at": existing["activated_at"],
            }

        _insert_account_and_deposit(connection, now_text)
        connection.commit()
        created = _select_account(connection)
        return {
            "created": True,
            "activated_at": created["activated_at"],
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def sync_portfolio(now=None, db_path=None):
    """
    Переводит уже записанный baseline в денежный счёт.

    Порядок фиксирован:
    существующие аллокации, затем NAV, затем новые входы,
    затем один снимок на 15-минутную корзину.
    Счёт здесь не создаётся.
    """
    if now is None:
        now = utc_now()

    ensure_portfolio_tables(db_path)
    stats = _empty_sync_stats()
    now_text = format_datetime(now)
    connection = get_paper_connection(db_path)

    try:
        account = _select_account(connection)
    finally:
        connection.close()

    if account is None:
        stats["account_missing"] = True
        print("PAPER PORTFOLIO: счёт не активирован, синхронизация пропущена")
        _print_sync_summary(stats)
        return stats

    _sync_existing_allocations(now_text, db_path, stats)
    _open_new_allocations(now_text, db_path, account["activated_at"], stats)
    stats["snapshot_inserted"] = _record_snapshot(now_text, db_path)
    stats["nav"] = _read_nav(db_path)
    _print_sync_summary(stats)
    return stats


def _empty_sync_stats():
    return {
        "account_missing": False,
        "mtm_updates": 0,
        "sells": 0,
        "buys": 0,
        "skips": 0,
        "snapshot_inserted": False,
        "nav": None,
    }


def _sync_existing_allocations(now_text, db_path, stats):
    connection = get_paper_connection(db_path)

    try:
        rows = _select_allocations(connection, PORTFOLIO_ID)
    finally:
        connection.close()

    for allocation in rows:
        if allocation["status"] != STATUS_OPEN:
            continue

        position_id = allocation["position_id"]

        try:
            if allocation["baseline_status"] == STATUS_OPEN:
                updated = _mark_open_allocation(allocation, now_text, db_path)

                if updated:
                    stats["mtm_updates"] += 1

                continue

            if allocation["baseline_status"] == STATUS_CLOSED:
                sold = _sell_allocation(allocation, now_text, db_path)

                if sold:
                    stats["sells"] += 1
        except Exception as error:
            print("PAPER PORTFOLIO: ошибка сопровождения, Position ID:", position_id)
            print(error)


def _open_new_allocations(now_text, db_path, activated_at, stats):
    connection = get_paper_connection(db_path)

    try:
        positions = _select_new_positions(connection, PORTFOLIO_ID, activated_at)
    finally:
        connection.close()

    for position in positions:
        try:
            outcome = _allocate_new_position(position, now_text, db_path)
        except Exception as error:
            print("PAPER PORTFOLIO: ошибка входа, Position ID:", position["id"])
            print(error)
            continue

        if outcome == EVENT_BUY:
            stats["buys"] += 1
            continue

        if outcome == DECISION_SKIPPED:
            stats["skips"] += 1


def _insert_account_and_deposit(connection, now_text):
    cursor = connection.cursor()
    deposit = DEFAULT_INITIAL_DEPOSIT_USD

    cursor.execute("""
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
        PORTFOLIO_ID,
        deposit,
        deposit,
        now_text,
        deposit,
        now_text,
        now_text,
    ))

    cursor.execute("""
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
        PORTFOLIO_ID,
        EVENT_DEPOSIT,
        deposit,
        deposit,
        now_text,
    ))


def _allocate_new_position(position, now_text, db_path):
    entry_price = _positive_price(position.get("entry_price"))

    if entry_price is None:
        print("PAPER PORTFOLIO: нет цены входа, Position ID:", position["id"])
        return None

    connection = get_paper_connection(db_path)

    try:
        account = _select_account(connection)

        if account is None:
            return None

        nav = _nav_from_connection(connection, account)
        recommended_percent = float(recommend_position_percent(position, nav["total_equity_usd"]))
        recommended_usd = nav["total_equity_usd"] * recommended_percent / 100.0

        if recommended_usd > 0 and account["cash_usd"] >= recommended_usd:
            _insert_buy(
                connection,
                position,
                now_text,
                account["cash_usd"],
                entry_price,
                recommended_percent,
                recommended_usd,
            )
            connection.commit()
            return EVENT_BUY

        _insert_skip(
            connection,
            position,
            now_text,
            entry_price,
            recommended_percent,
            recommended_usd,
        )
        connection.commit()
        return DECISION_SKIPPED
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _insert_buy(
    connection,
    position,
    now_text,
    cash_usd,
    entry_price,
    recommended_percent,
    recommended_usd,
):
    quantity = recommended_usd / entry_price
    cash_after = cash_usd - recommended_usd
    cursor = connection.cursor()

    cursor.execute("""
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
        PORTFOLIO_ID,
        position["id"],
        position["pair_id"],
        position.get("strategy_version"),
        STATUS_OPEN,
        RECOMMENDATION_BASELINE_BUY,
        DECISION_AUTO_PAPER_BUY,
        now_text,
        recommended_percent,
        recommended_usd,
        recommended_percent,
        recommended_usd,
        quantity,
        entry_price,
        position.get("entry_time"),
        position.get("signal_type"),
        position.get("final_score"),
        score_cohort(position.get("final_score")),
        entry_price,
        recommended_usd,
        now_text,
        now_text,
    ))

    allocation_id = cursor.lastrowid

    cursor.execute("""
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
        PORTFOLIO_ID,
        allocation_id,
        EVENT_BUY,
        -recommended_usd,
        cash_after,
        now_text,
    ))

    cursor.execute("""
        UPDATE paper_account
        SET
            cash_usd = ?,
            updated_at = ?
        WHERE id = ?
    """, (
        cash_after,
        now_text,
        PORTFOLIO_ID,
    ))


def _insert_skip(
    connection,
    position,
    now_text,
    entry_price,
    recommended_percent,
    recommended_usd,
):
    cursor = connection.cursor()

    cursor.execute("""
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
            ?, ?, ?, ?,
            ?, ?, 0, 0,
            NULL, ?, ?,
            ?, ?, ?,
            NULL, 0, 0, 0,
            ?, ?
        )
    """, (
        PORTFOLIO_ID,
        position["id"],
        position["pair_id"],
        position.get("strategy_version"),
        STATUS_SKIPPED,
        SKIP_INSUFFICIENT_CASH,
        RECOMMENDATION_BASELINE_BUY,
        DECISION_SKIPPED,
        now_text,
        recommended_percent,
        recommended_usd,
        entry_price,
        position.get("entry_time"),
        position.get("signal_type"),
        position.get("final_score"),
        score_cohort(position.get("final_score")),
        now_text,
        now_text,
    ))


def _mark_open_allocation(allocation, now_text, db_path):
    last_price = _positive_price(allocation.get("baseline_last_price"))
    quantity = allocation.get("quantity")

    if last_price is None or quantity is None:
        print("PAPER PORTFOLIO: MTM пропущен, Position ID:", allocation["position_id"])
        return False

    market_value = float(quantity) * last_price
    unrealized = market_value - float(allocation["allocated_usd"])
    connection = get_paper_connection(db_path)

    try:
        cursor = connection.cursor()
        cursor.execute("""
            UPDATE paper_allocations
            SET
                last_price = ?,
                market_value_usd = ?,
                unrealized_pnl_usd = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'OPEN'
        """, (
            last_price,
            market_value,
            unrealized,
            now_text,
            allocation["id"],
        ))
        updated = cursor.rowcount == 1
        connection.commit()
        return updated
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _sell_allocation(allocation, now_text, db_path):
    exit_price = _positive_price(allocation.get("baseline_exit_price"))
    quantity = allocation.get("quantity")

    if exit_price is None or quantity is None:
        print("PAPER PORTFOLIO: SELL пропущен, нет цены выхода, Position ID:", allocation["position_id"])
        return False

    proceeds = float(quantity) * exit_price
    realized = proceeds - float(allocation["allocated_usd"])
    connection = get_paper_connection(db_path)

    try:
        account = _select_account(connection)

        if account is None:
            return False

        cursor = connection.cursor()
        cursor.execute("""
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
              AND status = 'OPEN'
        """, (
            exit_price,
            allocation.get("baseline_exit_time"),
            allocation.get("baseline_exit_reason"),
            allocation.get("baseline_result_percent"),
            exit_price,
            realized,
            now_text,
            allocation["id"],
        ))

        if cursor.rowcount != 1:
            connection.rollback()
            return False

        cash_after = float(account["cash_usd"]) + proceeds

        cursor.execute("""
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
            PORTFOLIO_ID,
            allocation["id"],
            EVENT_SELL,
            proceeds,
            cash_after,
            now_text,
        ))

        cursor.execute("""
            UPDATE paper_account
            SET
                cash_usd = ?,
                updated_at = ?
            WHERE id = ?
        """, (
            cash_after,
            now_text,
            PORTFOLIO_ID,
        ))
        connection.commit()
        return True
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _record_snapshot(now_text, db_path):
    bucket = observation_bucket(now_text)
    connection = get_paper_connection(db_path)

    try:
        account = _select_account(connection)

        if account is None:
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

        cursor = connection.cursor()
        cursor.execute("""
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
            PORTFOLIO_ID,
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
            cursor.execute("""
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
                PORTFOLIO_ID,
            ))

        connection.commit()
        return inserted
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _read_nav(db_path):
    connection = get_paper_connection(db_path)

    try:
        account = _select_account(connection)

        if account is None:
            return None

        return _nav_from_connection(connection, account)["total_equity_usd"]
    finally:
        connection.close()


def _nav_from_connection(connection, account):
    cursor = connection.cursor()
    cursor.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN status = 'OPEN' THEN market_value_usd ELSE 0 END), 0)
                AS open_market_value_usd,
            COALESCE(SUM(CASE WHEN status = 'OPEN' THEN unrealized_pnl_usd ELSE 0 END), 0)
                AS unrealized_pnl_usd,
            COALESCE(SUM(CASE WHEN status = 'CLOSED' THEN realized_pnl_usd ELSE 0 END), 0)
                AS realized_pnl_usd
        FROM paper_allocations
        WHERE portfolio_id = ?
    """, (account["id"],))
    sums = _row(cursor.fetchone())
    cash = float(account["cash_usd"])
    open_market_value = float(sums["open_market_value_usd"])

    return {
        "cash_usd": cash,
        "open_market_value_usd": open_market_value,
        "total_equity_usd": cash + open_market_value,
        "realized_pnl_usd": float(sums["realized_pnl_usd"]),
        "unrealized_pnl_usd": float(sums["unrealized_pnl_usd"]),
    }


def _select_account(connection):
    cursor = connection.cursor()
    cursor.execute("""
        SELECT *
        FROM paper_account
        WHERE id = ?
        LIMIT 1
    """, (PORTFOLIO_ID,))
    return _row(cursor.fetchone())


def _select_allocations(connection, portfolio_id):
    cursor = connection.cursor()
    cursor.execute("""
        SELECT
            a.id,
            a.portfolio_id,
            a.position_id,
            a.status,
            a.quantity,
            a.allocated_usd,
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
        ORDER BY a.id ASC
    """, (portfolio_id,))
    return [_row(row) for row in cursor.fetchall()]


def _select_new_positions(connection, portfolio_id, activated_at):
    cursor = connection.cursor()
    cursor.execute("""
        SELECT
            pp.id,
            pp.pair_id,
            pp.strategy_version,
            pp.signal_type,
            pp.final_score,
            pp.entry_price,
            pp.entry_time,
            pp.created_at
        FROM paper_positions AS pp
        WHERE pp.strategy_version = ?
          AND pp.created_at >= ?
          AND NOT EXISTS (
              SELECT 1
              FROM paper_allocations AS a
              WHERE a.portfolio_id = ?
                AND a.position_id = pp.id
          )
        ORDER BY pp.id ASC
    """, (
        STRATEGY_VERSION,
        activated_at,
        portfolio_id,
    ))
    return [_row(row) for row in cursor.fetchall()]


def _positive_price(value):
    if value is None:
        return None

    try:
        price = float(value)
    except (TypeError, ValueError):
        return None

    if price <= 0:
        return None

    return price


def _row(row):
    if row is None:
        return None

    return {key: row[key] for key in row.keys()}


def _print_sync_summary(stats):
    print()
    print("ИТОГ PAPER PORTFOLIO")
    print("====================")
    print("MTM:", stats["mtm_updates"])
    print("SELL:", stats["sells"])
    print("BUY:", stats["buys"])
    print("SKIPPED:", stats["skips"])
    print("Новый NAV snapshot:", stats["snapshot_inserted"])
    print("NAV:", stats["nav"])


if __name__ == "__main__":
    from paper_engine import run_cycle

    current_time = utc_now()

    try:
        ensure_portfolio(current_time)
    except Exception as error:
        print("PAPER PORTFOLIO: ошибка инициализации, paper engine продолжит работу")
        print(error)

    try:
        run_cycle(now=current_time)
    except Exception as error:
        print("PAPER ENGINE: ошибка, проверки цены уже завершены")
        print(error)

    try:
        sync_portfolio(current_time)
    except Exception as error:
        print("PAPER PORTFOLIO: ошибка синхронизации, paper engine уже завершён")
        print(error)
