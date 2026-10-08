"""
Read-only расчёты Paper Analytics v0.6.

Модуль только читает SQLite. Он не создаёт таблицы, не вызывает
ensure/sync/run_cycle и не меняет счёт, NAV, allocations или ledger.
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path
from statistics import mean, median

from paper_engine import (
    COHORT_CONTROL,
    COHORT_PRIMARY,
    EXIT_STOP_LOSS,
    EXIT_TIME,
    EXIT_TRAILING_STOP,
    SIGNAL_CONFIRMED,
    SIGNAL_NEUTRAL,
    SIGNAL_OVERHEAT,
    SIGNAL_WATCH,
    SIGNAL_WEAK,
    STRATEGY_VERSION,
    parse_datetime,
)


PORTFOLIO_ID = 1

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"
STATUS_SKIPPED = "SKIPPED"

EVENT_BUY = "BUY"
EVENT_SELL = "SELL"

REASON_MISSING_FILE = "missing_file"
REASON_UNREADABLE = "unreadable"
REASON_MISSING_TABLES = "missing_tables"
REASON_MISSING_ACCOUNT = "missing_account"

REQUIRED_TABLES = (
    "pairs",
    "paper_account",
    "paper_allocations",
    "paper_cash_ledger",
    "paper_nav_snapshots",
    "paper_positions",
)

SIGNAL_ORDER = (
    SIGNAL_WEAK,
    SIGNAL_NEUTRAL,
    SIGNAL_CONFIRMED,
    SIGNAL_WATCH,
    SIGNAL_OVERHEAT,
)
COHORT_ORDER = (
    COHORT_PRIMARY,
    COHORT_CONTROL,
)
EXIT_ORDER = (
    EXIT_STOP_LOSS,
    EXIT_TRAILING_STOP,
    EXIT_TIME,
)

OPEN_FIELDS = (
    "entry_price",
    "entry_time",
    "quantity",
    "allocated_usd",
    "market_value_usd",
    "unrealized_pnl_usd",
    "last_price",
)
CLOSED_FIELDS = (
    "result_percent",
    "realized_pnl_usd",
    "allocated_usd",
    "quantity",
    "entry_price",
    "exit_price",
    "entry_time",
    "exit_time",
    "exit_reason",
)

MONEY_TOLERANCE_USD = 0.01


def connect_readonly(db_path):
    """Открывает базу только на чтение: URI mode=ro и PRAGMA query_only."""
    uri = "{0}?mode=ro".format(Path(db_path).resolve().as_uri())
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def load_portfolio(db_path):
    """
    Читает портфель 1.

    Отсутствие файла, таблиц или счёта возвращает пустое представление.
    """
    if not os.path.exists(db_path):
        return _empty_view(REASON_MISSING_FILE)

    try:
        connection = connect_readonly(db_path)
    except sqlite3.Error:
        return _empty_view(REASON_UNREADABLE)

    try:
        present = _table_names(connection)
        if any(table not in present for table in REQUIRED_TABLES):
            return _empty_view(REASON_MISSING_TABLES)

        account = _select_account(connection)
        allocations = _select_allocations(connection)
        ledger = _select_ledger(connection)
        snapshots = _select_snapshots(connection)
        baselines = _select_baselines_without_allocation(connection)
    finally:
        connection.close()

    _attach_ledger(allocations, ledger)

    if account is None:
        view = _empty_view(REASON_MISSING_ACCOUNT)
        view["baselines_without_allocation"] = baselines
        return view

    return {
        "available": True,
        "reason": None,
        "account": account,
        "allocations": allocations,
        "ledger": ledger,
        "snapshots": snapshots,
        "baselines_without_allocation": baselines,
    }


def build_report(view):
    """Собирает все блоки отчёта из уже загруженных строк."""
    account = view.get("account")
    allocations = view.get("allocations") or []
    ledger = view.get("ledger") or []
    snapshots = view.get("snapshots") or []
    baselines = view.get("baselines_without_allocation") or []

    account_block = account_metrics(account, allocations, snapshots)
    positions = position_metrics(allocations)
    closed = closed_trade_metrics(allocations)
    nav = account_block.get("current_nav_usd")

    return {
        "source": {
            "available": bool(view.get("available")),
            "reason": view.get("reason"),
        },
        "account": account_block,
        "positions": positions,
        "closed_trades": closed,
        "breakdown": breakdown_metrics(allocations),
        "capital_risk": capital_risk_metrics(
            account,
            allocations,
            snapshots,
            nav,
        ),
        "recent_trades": recent_trades(allocations, limit=10),
        "data_quality": data_quality(
            view,
            account_block,
            positions,
        ),
    }


def account_metrics(account, allocations, snapshots):
    if account is None:
        return {
            "initial_capital_usd": None,
            "current_cash_usd": None,
            "current_nav_usd": None,
            "total_pnl_usd": None,
            "total_return_percent": None,
            "peak_equity_usd": None,
            "max_drawdown_percent": None,
            "current_drawdown_percent": None,
        }

    initial = as_float(account.get("initial_deposit_usd"))
    cash = as_float(account.get("cash_usd"))
    open_value = _sum_money(
        allocation.get("market_value_usd")
        for allocation in allocations
        if allocation.get("status") == STATUS_OPEN
    )
    nav = None
    if cash is not None:
        nav = cash + open_value

    total_pnl = None
    total_return = None
    if nav is not None and initial is not None:
        total_pnl = nav - initial
        if initial > 0:
            total_return = total_pnl / initial * 100.0

    equity = equity_stats(account, snapshots, nav)
    return {
        "initial_capital_usd": initial,
        "current_cash_usd": cash,
        "current_nav_usd": nav,
        "total_pnl_usd": total_pnl,
        "total_return_percent": total_return,
        "peak_equity_usd": equity["peak_equity_usd"],
        "max_drawdown_percent": equity["max_drawdown_percent"],
        "current_drawdown_percent": equity["current_drawdown_percent"],
    }


def equity_stats(account, snapshots, current_nav):
    """Пик и просадка по сохранённым точкам плюс текущий NAV. В базу не пишется."""
    stored_peak = as_float(account.get("peak_equity_usd")) if account else None
    stored_drawdown = as_float(account.get("max_drawdown_percent")) if account else None
    current = as_float(current_nav)

    candidates = []
    if stored_peak is not None:
        candidates.append(stored_peak)
    if current is not None:
        candidates.append(current)

    curve = []
    for snapshot in snapshots:
        equity = as_float(snapshot.get("total_equity_usd"))
        if equity is None:
            continue
        candidates.append(equity)
        observed = parse_optional(snapshot.get("observed_at"))
        if observed is not None:
            curve.append((observed, int(snapshot.get("id") or 0), equity))

    curve.sort()
    ordered = [point[2] for point in curve]
    if current is not None:
        ordered.append(current)

    reconstructed = 0.0
    saw_curve = False
    running_peak = None
    for equity in ordered:
        saw_curve = True
        if running_peak is None or equity > running_peak:
            running_peak = equity
        if running_peak > 0:
            drawdown = (running_peak - equity) / running_peak * 100.0
            if drawdown < 0:
                drawdown = 0.0
            if drawdown > reconstructed:
                reconstructed = drawdown

    peak = max(candidates) if candidates else None
    if stored_drawdown is None and not saw_curve:
        max_drawdown = None
    elif stored_drawdown is None:
        max_drawdown = reconstructed
    else:
        max_drawdown = max(stored_drawdown, reconstructed)

    current_drawdown = None
    if peak is not None and peak > 0 and current is not None:
        current_drawdown = (peak - current) / peak * 100.0
        if current_drawdown < 0:
            current_drawdown = 0.0

    if current_drawdown is not None:
        if max_drawdown is None or current_drawdown > max_drawdown:
            max_drawdown = current_drawdown

    return {
        "peak_equity_usd": peak,
        "max_drawdown_percent": max_drawdown,
        "current_drawdown_percent": current_drawdown,
    }


def position_metrics(allocations):
    open_rows = _with_status(allocations, STATUS_OPEN)
    closed_rows = _with_status(allocations, STATUS_CLOSED)
    skipped_rows = _with_status(allocations, STATUS_SKIPPED)
    return {
        "open_count": len(open_rows),
        "closed_count": len(closed_rows),
        "skipped_count": len(skipped_rows),
        "open_value_usd": _sum_money(
            row.get("market_value_usd") for row in open_rows
        ),
        "unrealized_pnl_usd": _sum_money(
            row.get("unrealized_pnl_usd") for row in open_rows
        ),
        "realized_pnl_usd": _sum_money(
            row.get("realized_pnl_usd") for row in closed_rows
        ),
    }


def closed_trade_metrics(allocations):
    closed_rows = _with_status(allocations, STATUS_CLOSED)
    with_result = []
    for allocation in closed_rows:
        result = as_float(allocation.get("result_percent"))
        if result is None:
            continue
        with_result.append((allocation, result))

    winners = [(row, result) for row, result in with_result if result > 0]
    losers = [(row, result) for row, result in with_result if result < 0]
    flats = [result for _, result in with_result if result == 0]
    result_values = [result for _, result in with_result]

    best = None
    worst = None
    if with_result:
        best_row, best_result = min(
            with_result,
            key=lambda item: (-item[1], item[0]["id"]),
        )
        worst_row, worst_result = min(
            with_result,
            key=lambda item: (item[1], item[0]["id"]),
        )
        best = _trade_extremum(best_row, best_result)
        worst = _trade_extremum(worst_row, worst_result)

    return {
        "closed_count": len(closed_rows),
        "result_count": len(with_result),
        "winners": len(winners),
        "losers": len(losers),
        "flats": len(flats),
        "win_rate_percent": (
            len(winners) / len(with_result) * 100.0 if with_result else None
        ),
        "average_result_percent": mean(result_values) if result_values else None,
        "median_result_percent": median(result_values) if result_values else None,
        "best": best,
        "worst": worst,
        "average_winner_percent": (
            mean([result for _, result in winners]) if winners else None
        ),
        "average_loser_percent": (
            mean([result for _, result in losers]) if losers else None
        ),
        "profit_factor": _profit_factor(with_result),
    }


def breakdown_metrics(allocations):
    trades = []
    for allocation in _with_status(allocations, STATUS_CLOSED):
        result = as_float(allocation.get("result_percent"))
        if result is None:
            continue
        trades.append((allocation, result))

    return {
        "final_score": _breakdown_groups(
            trades,
            "final_score",
            _score_sort_key,
        ),
        "signal_type": _breakdown_groups(
            trades,
            "signal_type",
            lambda value: _preferred_sort_key(value, SIGNAL_ORDER),
        ),
        "cohort": _breakdown_groups(
            trades,
            "cohort",
            lambda value: _preferred_sort_key(value, COHORT_ORDER),
        ),
        "exit_reason": _breakdown_groups(
            trades,
            "exit_reason",
            lambda value: _preferred_sort_key(value, EXIT_ORDER),
        ),
    }


def capital_risk_metrics(account, allocations, snapshots, current_nav):
    exposure = exposure_stats(account, allocations, snapshots, current_nav)
    equity = equity_stats(account, snapshots, current_nav) if account else {
        "max_drawdown_percent": None,
        "current_drawdown_percent": None,
    }
    return {
        "max_concurrent_positions": exposure["max_concurrent_positions"],
        "max_capital_in_positions_usd": exposure["max_capital_in_positions_usd"],
        "max_exposure_percent": exposure["max_exposure_percent"],
        "max_drawdown_percent": equity["max_drawdown_percent"],
        "current_drawdown_percent": equity["current_drawdown_percent"],
    }


def exposure_stats(account, allocations, snapshots, current_nav):
    """
    Вложенный капитал по интервалам ledger.

    В одну и ту же секунду сначала учитывается SELL, затем BUY.
    """
    empty = {
        "max_concurrent_positions": 0,
        "max_capital_in_positions_usd": 0.0,
        "max_exposure_percent": None if account is None else 0.0,
    }
    if account is None:
        return empty

    events = []
    open_capital = 0.0
    for allocation in allocations:
        if allocation.get("status") not in (STATUS_OPEN, STATUS_CLOSED):
            continue
        capital = as_float(allocation.get("allocated_usd"))
        if capital is None:
            continue
        buy = earliest_event(allocation.get("buy_events") or [])
        if buy is None:
            continue
        start = parse_optional(buy.get("created_at"))
        if start is None:
            continue
        sell = earliest_event(allocation.get("sell_events") or [])
        end = parse_optional(sell.get("created_at")) if sell is not None else None
        if end is not None:
            events.append((start, 1, 1, capital))
            events.append((end, 0, -1, -capital))
            continue
        if allocation.get("status") == STATUS_OPEN:
            events.append((start, 1, 1, capital))
            open_capital += capital

    events.sort()
    equity_points = _equity_points(snapshots)
    initial = as_float(account.get("initial_deposit_usd"))
    current_count = 0
    current_capital = 0.0
    max_count = 0
    max_capital = 0.0
    max_exposure = 0.0
    undefined_exposure = False

    def note_exposure(capital, equity):
        nonlocal max_exposure, undefined_exposure
        if capital > 0 and equity is not None and equity <= 0:
            undefined_exposure = True
            return
        if capital > 0 and equity is not None and equity > 0:
            exposure = capital / equity * 100.0
            if exposure > max_exposure:
                max_exposure = exposure

    for moment, _order, delta_count, delta_capital in events:
        current_count += delta_count
        current_capital += delta_capital
        if current_count > max_count:
            max_count = current_count
        if current_capital > max_capital:
            max_capital = current_capital
        note_exposure(current_capital, _equity_at(moment, equity_points, initial))

    current = as_float(current_nav)
    if current is not None:
        note_exposure(open_capital, current)

    return {
        "max_concurrent_positions": max_count,
        "max_capital_in_positions_usd": max_capital,
        "max_exposure_percent": None if undefined_exposure else max_exposure,
    }


def recent_trades(allocations, limit=10):
    closed_rows = _with_status(allocations, STATUS_CLOSED)
    ordered = sorted(closed_rows, key=_recent_sort_key)
    trades = []
    for allocation in ordered[:limit]:
        duration, negative = holding_duration(allocation)
        trades.append({
            "id": allocation.get("id"),
            "pair": pair_label(allocation),
            "score": allocation.get("final_score"),
            "entry_price": as_float(allocation.get("entry_price")),
            "exit_price": as_float(allocation.get("exit_price")),
            "result_percent": as_float(allocation.get("result_percent")),
            "realized_pnl_usd": as_float(allocation.get("realized_pnl_usd")),
            "exit_reason": allocation.get("exit_reason"),
            "duration_seconds": duration,
            "duration_negative": negative,
        })
    return trades


def holding_duration(allocation):
    """
    Длительность удержания капитала.

    Основной интервал: самый ранний BUY.created_at -> самый ранний SELL.created_at.
    Если события нет, соответствующий конец берётся из entry_time или exit_time.
    Отрицательный интервал не считается длительностью.
    """
    buy = earliest_event(allocation.get("buy_events") or [])
    sell = earliest_event(allocation.get("sell_events") or [])
    if buy is not None:
        start = parse_optional(buy.get("created_at"))
    else:
        start = parse_optional(allocation.get("entry_time"))
    if sell is not None:
        end = parse_optional(sell.get("created_at"))
    else:
        end = parse_optional(allocation.get("exit_time"))

    if start is None or end is None:
        return None, False
    if end < start:
        return None, True
    return int(round((end - start).total_seconds())), False


def data_quality(view, account_block, positions):
    account = view.get("account")
    allocations = view.get("allocations") or []
    ledger = view.get("ledger") or []
    baselines = view.get("baselines_without_allocation") or []

    incomplete_ids = set()
    field_counts = {}
    negative_duration = 0
    sign_mismatch = 0

    for allocation in allocations:
        status = allocation.get("status")
        if status == STATUS_OPEN:
            fields = OPEN_FIELDS
        elif status == STATUS_CLOSED:
            fields = CLOSED_FIELDS
        else:
            fields = ()

        missing = [
            field
            for field in fields
            if _field_missing(allocation.get(field))
        ]
        if missing:
            incomplete_ids.add(allocation.get("id"))
            for field in missing:
                field_counts[field] = field_counts.get(field, 0) + 1

        if status in (STATUS_OPEN, STATUS_CLOSED):
            _duration, negative = holding_duration(allocation)
            if negative:
                negative_duration += 1
                incomplete_ids.add(allocation.get("id"))
                field_counts["duration"] = field_counts.get("duration", 0) + 1

        if status == STATUS_CLOSED:
            result = as_float(allocation.get("result_percent"))
            realized = as_float(allocation.get("realized_pnl_usd"))
            if result is not None and realized is not None and _sign_mismatch(result, realized):
                sign_mismatch += 1

    skip_reasons = {}
    for allocation in _with_status(allocations, STATUS_SKIPPED):
        reason = allocation.get("skip_reason")
        if reason is None or str(reason).strip() == "":
            reason = "(none)"
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    post_activation = _post_activation_baseline(account, baselines)
    return {
        "incomplete_count": len(incomplete_ids),
        "incomplete_fields": field_counts,
        "skipped_count": positions["skipped_count"],
        "skip_reasons": skip_reasons,
        "open_allocation_baseline_closed": _open_but_baseline_closed(allocations),
        "post_activation_without_allocation": post_activation["total"],
        "post_activation_open": post_activation["open"],
        "post_activation_closed": post_activation["closed"],
        "post_activation_other": post_activation["other"],
        "post_activation_unparsed": post_activation["unparsed"],
        "nav_identity_mismatch": _nav_identity_mismatch(account_block, positions),
        "ledger_cash_mismatch": _ledger_cash_mismatch(account, ledger),
        "sign_mismatch_count": sign_mismatch,
        "duplicate_ledger_count": _duplicate_ledger_count(allocations),
        "negative_duration_count": negative_duration,
        "reason": view.get("reason"),
    }


def pair_label(allocation):
    symbol = allocation.get("pair_symbol")
    if symbol is not None and str(symbol).strip() != "":
        return str(symbol)
    return "pair #{0}".format(allocation.get("pair_id"))


def earliest_event(events):
    parsed = []
    for event in events:
        moment = parse_optional(event.get("created_at"))
        if moment is None:
            continue
        parsed.append((moment, int(event.get("id") or 0), event))
    if not parsed:
        return None
    parsed.sort()
    return parsed[0][2]


def as_float(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def parse_optional(value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        return parse_datetime(text)
    except (TypeError, ValueError):
        return None


def _empty_view(reason):
    return {
        "available": False,
        "reason": reason,
        "account": None,
        "allocations": [],
        "ledger": [],
        "snapshots": [],
        "baselines_without_allocation": [],
    }


def _table_names(connection):
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row["name"] for row in rows}


def _select_account(connection):
    row = connection.execute(
        """
        SELECT *
        FROM paper_account
        WHERE id = ?
        LIMIT 1
        """,
        (PORTFOLIO_ID,),
    ).fetchone()
    return _plain_row(row)


def _select_allocations(connection):
    rows = connection.execute(
        """
        SELECT
            a.*,
            p.pair_symbol AS pair_symbol,
            pp.status AS baseline_status
        FROM paper_allocations AS a
        LEFT JOIN pairs AS p
            ON p.id = a.pair_id
        LEFT JOIN paper_positions AS pp
            ON pp.id = a.position_id
        WHERE a.portfolio_id = ?
        ORDER BY a.id ASC
        """,
        (PORTFOLIO_ID,),
    ).fetchall()
    return [_plain_row(row) for row in rows]


def _select_ledger(connection):
    rows = connection.execute(
        """
        SELECT *
        FROM paper_cash_ledger
        WHERE portfolio_id = ?
        ORDER BY id ASC
        """,
        (PORTFOLIO_ID,),
    ).fetchall()
    return [_plain_row(row) for row in rows]


def _select_snapshots(connection):
    rows = connection.execute(
        """
        SELECT *
        FROM paper_nav_snapshots
        WHERE portfolio_id = ?
        ORDER BY observed_at ASC, id ASC
        """,
        (PORTFOLIO_ID,),
    ).fetchall()
    return [_plain_row(row) for row in rows]


def _select_baselines_without_allocation(connection):
    rows = connection.execute(
        """
        SELECT
            pp.id,
            pp.pair_id,
            pp.strategy_version,
            pp.status,
            pp.created_at
        FROM paper_positions AS pp
        WHERE pp.strategy_version = ?
          AND NOT EXISTS (
              SELECT 1
              FROM paper_allocations AS a
              WHERE a.portfolio_id = ?
                AND a.position_id = pp.id
          )
        ORDER BY pp.id ASC
        """,
        (STRATEGY_VERSION, PORTFOLIO_ID),
    ).fetchall()
    return [_plain_row(row) for row in rows]


def _attach_ledger(allocations, ledger):
    buys = {}
    sells = {}
    for event in ledger:
        allocation_id = event.get("allocation_id")
        if allocation_id is None:
            continue
        if event.get("event_type") == EVENT_BUY:
            buys.setdefault(allocation_id, []).append(event)
        elif event.get("event_type") == EVENT_SELL:
            sells.setdefault(allocation_id, []).append(event)

    for allocation in allocations:
        allocation_id = allocation.get("id")
        allocation["buy_events"] = list(buys.get(allocation_id, []))
        allocation["sell_events"] = list(sells.get(allocation_id, []))


def _plain_row(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _with_status(allocations, status):
    return [
        allocation
        for allocation in allocations
        if allocation.get("status") == status
    ]


def _sum_money(values):
    total = 0.0
    for value in values:
        number = as_float(value)
        if number is not None:
            total += number
    return total


def _trade_extremum(allocation, result):
    return {
        "id": allocation.get("id"),
        "pair": pair_label(allocation),
        "score": allocation.get("final_score"),
        "result_percent": result,
    }


def _profit_factor(with_result):
    loser_realized = []
    winner_realized = []
    for allocation, result in with_result:
        realized = as_float(allocation.get("realized_pnl_usd"))
        if realized is None:
            continue
        if result > 0:
            winner_realized.append(realized)
        elif result < 0:
            loser_realized.append(realized)

    if not loser_realized:
        return None
    gross_loss = sum(loser_realized)
    if gross_loss >= 0:
        return None
    gross_profit = sum(winner_realized)
    return gross_profit / abs(gross_loss)


def _breakdown_groups(trades, field, sort_key):
    grouped = {}
    for allocation, result in trades:
        key = allocation.get(field)
        if field == "final_score":
            key = as_float(key)
            if key is not None and key == int(key):
                key = int(key)
        grouped.setdefault(key, []).append((allocation, result))

    rows = []
    for key in sorted(grouped, key=sort_key):
        members = grouped[key]
        results = [result for _, result in members]
        winners = [result for result in results if result > 0]
        realized_values = [
            as_float(allocation.get("realized_pnl_usd"))
            for allocation, _result in members
        ]
        realized_values = [value for value in realized_values if value is not None]
        rows.append({
            "key": key,
            "n": len(members),
            "win_rate_percent": len(winners) / len(members) * 100.0,
            "average_result_percent": mean(results),
            "realized_pnl_usd": sum(realized_values) if realized_values else 0.0,
        })
    return rows


def _score_sort_key(value):
    if value is None:
        return (1, 0)
    return (0, -float(value))


def _preferred_sort_key(value, preferred):
    if value is None:
        return (2, 0, "")
    try:
        index = preferred.index(value)
    except ValueError:
        return (1, 0, str(value))
    return (0, index, "")


def _equity_points(snapshots):
    points = []
    for snapshot in snapshots:
        moment = parse_optional(snapshot.get("observed_at"))
        equity = as_float(snapshot.get("total_equity_usd"))
        if moment is None or equity is None:
            continue
        points.append((moment, int(snapshot.get("id") or 0), equity))
    points.sort()
    return points


def _equity_at(moment, equity_points, initial):
    basis = initial
    for observed, _snapshot_id, equity in equity_points:
        if observed <= moment:
            basis = equity
        else:
            break
    return basis


def _field_missing(value):
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def _sign_mismatch(result, realized):
    if result > 0 and realized < -MONEY_TOLERANCE_USD:
        return True
    if result < 0 and realized > MONEY_TOLERANCE_USD:
        return True
    if result == 0 and abs(realized) > MONEY_TOLERANCE_USD:
        return True
    return False


def _open_but_baseline_closed(allocations):
    count = 0
    for allocation in allocations:
        if allocation.get("status") != STATUS_OPEN:
            continue
        if allocation.get("baseline_status") == STATUS_CLOSED:
            count += 1
    return count


def _post_activation_baseline(account, baselines):
    empty = {
        "total": 0,
        "open": 0,
        "closed": 0,
        "other": 0,
        "unparsed": 0,
    }
    if account is None:
        return empty
    activated = parse_optional(account.get("activated_at"))
    if activated is None:
        return empty

    counts = dict(empty)
    for position in baselines:
        if position.get("strategy_version") != STRATEGY_VERSION:
            continue
        created = parse_optional(position.get("created_at"))
        if created is None:
            counts["unparsed"] += 1
            continue
        if created < activated:
            continue
        status = position.get("status")
        if status == STATUS_OPEN:
            counts["open"] += 1
        elif status == STATUS_CLOSED:
            counts["closed"] += 1
        else:
            counts["other"] += 1
    counts["total"] = counts["open"] + counts["closed"] + counts["other"]
    return counts


def _nav_identity_mismatch(account_block, positions):
    if account_block.get("total_pnl_usd") is None:
        return False
    component = (
        positions["realized_pnl_usd"] + positions["unrealized_pnl_usd"]
    )
    return abs(account_block["total_pnl_usd"] - component) > MONEY_TOLERANCE_USD


def _ledger_cash_mismatch(account, ledger):
    if account is None:
        return False
    cash = as_float(account.get("cash_usd"))
    if cash is None:
        return True
    amounts = []
    for event in ledger:
        amount = as_float(event.get("amount_usd"))
        if amount is None:
            return True
        amounts.append(amount)
    if abs(sum(amounts) - cash) > MONEY_TOLERANCE_USD:
        return True
    if ledger:
        last = max(ledger, key=lambda event: int(event.get("id") or 0))
        cash_after = as_float(last.get("cash_after_usd"))
        if cash_after is None or abs(cash_after - cash) > MONEY_TOLERANCE_USD:
            return True
    return False


def _duplicate_ledger_count(allocations):
    extras = 0
    for allocation in allocations:
        buys = allocation.get("buy_events") or []
        sells = allocation.get("sell_events") or []
        if len(buys) > 1:
            extras += len(buys) - 1
        if len(sells) > 1:
            extras += len(sells) - 1
    return extras


def _recent_sort_key(allocation):
    moment = parse_optional(allocation.get("exit_time"))
    allocation_id = int(allocation.get("id") or 0)
    if moment is None:
        return (1, datetime.min, -allocation_id)
    return (0, datetime.max - moment, -allocation_id)
