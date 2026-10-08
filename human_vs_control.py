"""
Human vs Control / Decision Analytics v0.9.

Модуль только читает SQLite. Он не открывает ордера, не меняет approval,
allocations, ledger, NAV, baseline и не ходит в сеть.

Два уровня не смешиваются:

- Portfolio comparison: фактические книги portfolio_id 1 и 2.
- Decision comparison: решения Human Approval BUY / SKIP против
  автоматического принятия сигнала.

Human PnL берётся только из allocation портфеля 2.
Control PnL берётся только из allocation портфеля 1.
SKIP counterfactual — это baseline trajectory (paper_positions), не сделка Human.
Неоднозначная связь не угадывается и попадает в data quality.
"""

import os
import sqlite3
from pathlib import Path
from statistics import mean, median

from paper_analytics import (
    account_metrics,
    as_float,
    connect_readonly,
    exposure_stats,
    parse_optional,
    position_metrics,
)


CONTROL_PORTFOLIO_ID = 1
HUMAN_PORTFOLIO_ID = 2

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"
STATUS_SKIPPED = "SKIPPED"

DECISION_BUY = "BUY"
DECISION_SKIP = "SKIP"
DECISION_EXPIRED = "EXPIRED"
DECISION_PENDING = "PENDING"
DECISION_STATUSES = (
    DECISION_BUY,
    DECISION_SKIP,
    DECISION_EXPIRED,
    DECISION_PENDING,
)

OUTCOME_COMPLETE = "COMPLETE"
OUTCOME_PENDING = "PENDING_OUTCOME"
OUTCOME_MISSING_BASELINE = "MISSING_BASELINE"
OUTCOME_UNLINKED = "UNLINKED"
OUTCOME_INCOMPLETE = "INCOMPLETE"

PROFIT_FACTOR_OK = "ok"
PROFIT_FACTOR_NO_LOSS = "no_gross_loss"
PROFIT_FACTOR_UNDEFINED = "undefined"

SAMPLE_INSUFFICIENT = "INSUFFICIENT DATA"
SAMPLE_EARLY = "EARLY SAMPLE"
SAMPLE_FORWARD = "FORWARD TEST SAMPLE"
SAMPLE_LARGER = "LARGER SAMPLE"

SAMPLE_NOTE = (
    "Информационный уровень выборки. "
    "Это не статистическая значимость и не вывод, "
    "что стратегия прибыльна или убыточна."
)

RESULT_TOLERANCE = 1e-4

CHANGE_BUCKETS = ("<0", "0..<3", "3..<6", "6..<10", ">=10")
SCORE_BANDS = ("<70", "70-79", ">=80")
EXIT_ORDER = ("STOP_LOSS", "TRAILING_STOP", "TIME_EXIT")
DELAY_BUCKETS = (
    "<1m",
    "1m..<15m",
    "15m..<1h",
    "1h..<6h",
    "6h..<24h",
    ">=24h",
)
SIGNAL_ORDER = (
    "WEAK",
    "NEUTRAL",
    "CONFIRMED",
    "WATCH",
    "OVERHEAT",
)
COHORT_ORDER = ("PRIMARY", "CONTROL")

QUALITY_KEYS = (
    "approval_without_baseline",
    "buy_without_human_allocation",
    "allocation_without_approval_request",
    "duplicate_logical_links",
    "skip_without_completed_baseline",
    "closed_allocation_without_exit_price",
    "missing_reference_price",
    "missing_execution_price",
    "missing_timestamps",
    "unmatched_control_human_signals",
    "baseline_control_result_mismatch",
)

REASON_MISSING_FILE = "missing_file"
REASON_UNREADABLE = "unreadable"


def connect_readonly_db(db_path):
    """URI mode=ro и PRAGMA query_only. Запись через это соединение невозможна."""
    return connect_readonly(db_path)


def sample_label(completed_decisions):
    """Информационная метка размера выборки. Не является значимостью."""
    count = int(completed_decisions or 0)
    if count < 20:
        return SAMPLE_INSUFFICIENT
    if count < 30:
        return SAMPLE_EARLY
    if count < 100:
        return SAMPLE_FORWARD
    return SAMPLE_LARGER


def change_24h_bucket(value):
    """Корзины change_24h: <0, 0..<3, 3..<6, 6..<10, >=10."""
    number = as_float(value)
    if number is None:
        return None
    if number < 0:
        return "<0"
    if number < 3:
        return "0..<3"
    if number < 6:
        return "3..<6"
    if number < 10:
        return "6..<10"
    return ">=10"


def score_band(value):
    """Разрезы 70–79 и >=80. Ниже 70 остаётся отдельной корзиной, без догадки."""
    number = as_float(value)
    if number is None:
        return None
    if number >= 80:
        return ">=80"
    if number >= 70:
        return "70-79"
    return "<70"


def delay_bucket(seconds):
    number = as_float(seconds)
    if number is None:
        return None
    if number < 60:
        return "<1m"
    if number < 15 * 60:
        return "1m..<15m"
    if number < 3600:
        return "15m..<1h"
    if number < 6 * 3600:
        return "1h..<6h"
    if number < 24 * 3600:
        return "6h..<24h"
    return ">=24h"


def slippage_percent(reference_price, execution_price):
    """(execution / reference - 1) * 100. Без цены или при reference <= 0 — None."""
    reference = as_float(reference_price)
    execution = as_float(execution_price)
    if reference is None or reference <= 0:
        return None
    if execution is None or execution <= 0:
        return None
    return (execution / reference - 1.0) * 100.0


def decision_delay_seconds(request):
    """
    Конец — execution_at, иначе decision_at.
    Начало — signal_created_at, иначе created_at заявки.
    Если времён нет, используется сохранённый delay_signal_to_decision_seconds.
    """
    end = parse_optional(request.get("execution_at"))
    if end is None:
        end = parse_optional(request.get("decision_at"))
    start = parse_optional(request.get("signal_created_at"))
    if start is None:
        start = parse_optional(request.get("created_at"))
    if start is not None and end is not None:
        return (end - start).total_seconds()
    return as_float(request.get("delay_signal_to_decision_seconds"))


def notional_pnl(recommended_usd, result_percent):
    """
    USD-результат на номинале заявки: recommended_usd * result_percent / 100.

    recommended_usd — размер 1% NAV на момент заявки, если он записан.
    Нет положительного номинала — None, без подстановки другого капитала.
    """
    notional = as_float(recommended_usd)
    result = as_float(result_percent)
    if notional is None or notional <= 0 or result is None:
        return None
    return notional * result / 100.0


def profit_factor_metrics(allocations):
    """
    gross profit / abs(gross loss) по realized_pnl_usd закрытых сделок.

    Нулевой gross loss не превращается в бесконечность.
    """
    gross_profit = 0.0
    gross_loss = 0.0
    considered = 0
    for allocation in allocations:
        if allocation.get("status") != STATUS_CLOSED:
            continue
        realized = as_float(allocation.get("realized_pnl_usd"))
        if realized is None:
            continue
        considered += 1
        if realized > 0:
            gross_profit += realized
        elif realized < 0:
            gross_loss += abs(realized)

    payload = {
        "gross_profit_usd": gross_profit if considered else None,
        "gross_loss_usd": gross_loss if considered else None,
    }
    if considered == 0 or (gross_profit == 0 and gross_loss == 0):
        payload["value"] = None
        payload["status"] = PROFIT_FACTOR_UNDEFINED
        return payload
    if gross_loss == 0:
        payload["value"] = None
        payload["status"] = PROFIT_FACTOR_NO_LOSS
        return payload
    payload["value"] = gross_profit / gross_loss
    payload["status"] = PROFIT_FACTOR_OK
    return payload


def median_or_none(values):
    if not values:
        return None
    return float(median(values))


def build_report(db_path):
    """Полный read-only отчёт v0.9. Отсутствие базы не роняет процесс."""
    if not db_path or not os.path.exists(db_path):
        return _empty_report(db_path, REASON_MISSING_FILE)

    try:
        connection = connect_readonly_db(db_path)
    except sqlite3.Error:
        return _empty_report(db_path, REASON_UNREADABLE)

    try:
        view = _load_view(connection)
    finally:
        connection.close()

    portfolios = {
        "control": _portfolio_block(
            CONTROL_PORTFOLIO_ID,
            view["accounts"].get(CONTROL_PORTFOLIO_ID),
            _for_portfolio(view["allocations"], CONTROL_PORTFOLIO_ID),
            _for_portfolio(view["ledger"], CONTROL_PORTFOLIO_ID),
            _for_portfolio(view["snapshots"], CONTROL_PORTFOLIO_ID),
        ),
        "human": _portfolio_block(
            HUMAN_PORTFOLIO_ID,
            view["accounts"].get(HUMAN_PORTFOLIO_ID),
            _for_portfolio(view["allocations"], HUMAN_PORTFOLIO_ID),
            _for_portfolio(view["ledger"], HUMAN_PORTFOLIO_ID),
            _for_portfolio(view["snapshots"], HUMAN_PORTFOLIO_ID),
        ),
    }
    decisions = _build_decisions(view)
    quality = decisions["quality"]
    records = decisions["records"]
    buy = _buy_quality(records)
    value = _decision_value(records)
    skip = _skip_quality(records, value)
    execution = _execution_stats(records)
    breakdown = _breakdown(records)
    matched = _matched_block(records)
    sample = _sample_block(records, buy, skip)
    summary = _summary(portfolios, decisions["counts"], value, sample)

    return {
        "source": {
            "available": True,
            "reason": None,
            "db_path": str(db_path),
            "read_only": True,
            "network": False,
        },
        "summary": summary,
        "portfolios": portfolios,
        "decision_counts": decisions["counts"],
        "ignored_non_human_requests": decisions["ignored_non_human_requests"],
        "buy_quality": buy,
        "skip_quality": skip,
        "decision_value": value,
        "execution": execution,
        "breakdown": breakdown,
        "matched": matched,
        "data_quality": quality,
        "sample": sample,
    }


def _empty_report(db_path, reason):
    empty_counts = {status: 0 for status in DECISION_STATUSES}
    buy = _buy_quality([])
    value = _decision_value([])
    skip = _skip_quality([], value)
    sample = _sample_block([], buy, skip)
    portfolios = {
        "control": _empty_portfolio(CONTROL_PORTFOLIO_ID),
        "human": _empty_portfolio(HUMAN_PORTFOLIO_ID),
    }
    return {
        "source": {
            "available": False,
            "reason": reason,
            "db_path": None if db_path is None else str(db_path),
            "read_only": True,
            "network": False,
        },
        "summary": _summary(portfolios, empty_counts, value, sample),
        "portfolios": portfolios,
        "decision_counts": empty_counts,
        "ignored_non_human_requests": 0,
        "buy_quality": buy,
        "skip_quality": skip,
        "decision_value": value,
        "execution": _execution_stats([]),
        "breakdown": _breakdown([]),
        "matched": _matched_block([]),
        "data_quality": _empty_quality(),
        "sample": sample,
    }


def _load_view(connection):
    names = _table_names(connection)
    view = {
        "accounts": {},
        "allocations": [],
        "ledger": [],
        "snapshots": [],
        "baselines": {},
        "requests": [],
        "pairs": {},
    }
    if "paper_account" in names:
        rows = connection.execute(
            "SELECT * FROM paper_account WHERE id IN (?, ?)",
            (CONTROL_PORTFOLIO_ID, HUMAN_PORTFOLIO_ID),
        ).fetchall()
        for row in rows:
            account = _plain(row)
            view["accounts"][account.get("id")] = account
    if "paper_allocations" in names:
        rows = connection.execute(
            """
            SELECT * FROM paper_allocations
            WHERE portfolio_id IN (?, ?)
            ORDER BY id ASC
            """,
            (CONTROL_PORTFOLIO_ID, HUMAN_PORTFOLIO_ID),
        ).fetchall()
        view["allocations"] = [_plain(row) for row in rows]
    if "paper_cash_ledger" in names:
        rows = connection.execute(
            """
            SELECT * FROM paper_cash_ledger
            WHERE portfolio_id IN (?, ?)
            ORDER BY id ASC
            """,
            (CONTROL_PORTFOLIO_ID, HUMAN_PORTFOLIO_ID),
        ).fetchall()
        view["ledger"] = [_plain(row) for row in rows]
    if "paper_nav_snapshots" in names:
        rows = connection.execute(
            """
            SELECT * FROM paper_nav_snapshots
            WHERE portfolio_id IN (?, ?)
            ORDER BY observed_at ASC, id ASC
            """,
            (CONTROL_PORTFOLIO_ID, HUMAN_PORTFOLIO_ID),
        ).fetchall()
        view["snapshots"] = [_plain(row) for row in rows]
    if "paper_positions" in names:
        rows = connection.execute("SELECT * FROM paper_positions").fetchall()
        for row in rows:
            baseline = _plain(row)
            view["baselines"][baseline.get("id")] = baseline
    if "approval_requests" in names:
        rows = connection.execute(
            "SELECT * FROM approval_requests ORDER BY id ASC"
        ).fetchall()
        view["requests"] = [_plain(row) for row in rows]
    if "pairs" in names:
        rows = connection.execute("SELECT id, pair_symbol FROM pairs").fetchall()
        for row in rows:
            plain = _plain(row)
            view["pairs"][plain.get("id")] = plain.get("pair_symbol")
    return view


def _portfolio_block(portfolio_id, account, allocations, ledger, snapshots):
    _attach_events(allocations, ledger)
    if account is None and not allocations:
        return _empty_portfolio(portfolio_id)

    block = _empty_portfolio(portfolio_id)
    account_block = account_metrics(account, allocations, snapshots)
    positions = position_metrics(allocations)
    closed_rows = [
        row for row in allocations if row.get("status") == STATUS_CLOSED
    ]
    open_rows = [
        row for row in allocations if row.get("status") == STATUS_OPEN
    ]
    results = _result_values(closed_rows)
    winners = [value for value in results if value > 0]
    losers = [value for value in results if value < 0]
    factor = profit_factor_metrics(allocations)
    realized = _sum_optional(row.get("realized_pnl_usd") for row in closed_rows)
    unrealized = _sum_optional(row.get("unrealized_pnl_usd") for row in open_rows)
    if not closed_rows:
        realized = None
    if not open_rows:
        unrealized = None

    if account is None:
        exposure = {
            "max_concurrent_positions": None,
            "max_capital_in_positions_usd": None,
            "max_exposure_percent": None,
        }
        available = False
    else:
        exposure = exposure_stats(
            account,
            allocations,
            snapshots,
            account_block.get("current_nav_usd"),
        )
        available = True

    block.update({
        "available": available,
        "initial_deposit_usd": account_block.get("initial_capital_usd"),
        "current_cash_usd": account_block.get("current_cash_usd"),
        "current_nav_usd": account_block.get("current_nav_usd"),
        "total_pnl_usd": account_block.get("total_pnl_usd"),
        "total_return_percent": account_block.get("total_return_percent"),
        "peak_equity_usd": account_block.get("peak_equity_usd"),
        "max_drawdown_percent": account_block.get("max_drawdown_percent"),
        "allocations_total": len(allocations),
        "open_count": positions["open_count"],
        "closed_count": positions["closed_count"],
        "skipped_count": positions["skipped_count"],
        "realized_pnl_usd": realized,
        "unrealized_pnl_usd": unrealized,
        "win_rate_percent": (
            None if not results else len(winners) / len(results) * 100.0
        ),
        "closed_result_count": len(results),
        "average_trade_pnl_percent": mean(results) if results else None,
        "median_trade_pnl_percent": median_or_none(results),
        "best_trade": _extreme_trade(closed_rows, "best"),
        "worst_trade": _extreme_trade(closed_rows, "worst"),
        "average_winner_percent": mean(winners) if winners else None,
        "average_loser_percent": mean(losers) if losers else None,
        "profit_factor": factor["value"],
        "profit_factor_status": factor["status"],
        "gross_profit_usd": factor["gross_profit_usd"],
        "gross_loss_usd": factor["gross_loss_usd"],
        "max_concurrent_open": exposure["max_concurrent_positions"],
        "max_capital_invested_usd": exposure["max_capital_in_positions_usd"],
        "max_exposure_percent": exposure["max_exposure_percent"],
    })
    return block


def _empty_portfolio(portfolio_id):
    return {
        "portfolio_id": portfolio_id,
        "available": False,
        "initial_deposit_usd": None,
        "current_cash_usd": None,
        "current_nav_usd": None,
        "total_pnl_usd": None,
        "total_return_percent": None,
        "peak_equity_usd": None,
        "max_drawdown_percent": None,
        "allocations_total": 0,
        "open_count": 0,
        "closed_count": 0,
        "skipped_count": 0,
        "realized_pnl_usd": None,
        "unrealized_pnl_usd": None,
        "win_rate_percent": None,
        "closed_result_count": 0,
        "average_trade_pnl_percent": None,
        "median_trade_pnl_percent": None,
        "best_trade": None,
        "worst_trade": None,
        "average_winner_percent": None,
        "average_loser_percent": None,
        "profit_factor": None,
        "profit_factor_status": PROFIT_FACTOR_UNDEFINED,
        "gross_profit_usd": None,
        "gross_loss_usd": None,
        "max_concurrent_open": None,
        "max_capital_invested_usd": None,
        "max_exposure_percent": None,
    }


def _build_decisions(view):
    quality = _empty_quality()
    baselines = view["baselines"]
    pairs = view["pairs"]
    human_allocations = _for_portfolio(view["allocations"], HUMAN_PORTFOLIO_ID)
    control_allocations = _for_portfolio(view["allocations"], CONTROL_PORTFOLIO_ID)
    human_by_position = _group_by(human_allocations, "position_id")
    human_by_id = {row.get("id"): row for row in human_allocations}
    control_by_position = _group_by(control_allocations, "position_id")

    human_requests = []
    ignored = 0
    requests_by_position = {}
    requests_by_allocation = {}
    for request in view["requests"]:
        if request.get("portfolio_id") != HUMAN_PORTFOLIO_ID:
            ignored += 1
            continue
        human_requests.append(request)
        requests_by_position.setdefault(request.get("position_id"), []).append(request)
        allocation_id = request.get("allocation_id")
        if allocation_id is not None:
            requests_by_allocation.setdefault(allocation_id, []).append(request)

    shared_positions = {
        position_id
        for position_id, group in requests_by_position.items()
        if len(group) > 1
    }
    shared_allocations = {
        allocation_id
        for allocation_id, group in requests_by_allocation.items()
        if len(group) > 1
    }

    records = []
    for request in human_requests:
        record, issues = _analyze_request(
            request,
            baselines,
            human_by_position,
            human_by_id,
            control_by_position,
            pairs,
            shared_positions,
            shared_allocations,
        )
        for issue in issues:
            quality[issue] += 1
        if record is not None:
            records.append(record)

    for allocation in human_allocations:
        position_id = allocation.get("position_id")
        allocation_id = allocation.get("id")
        position_linked = position_id in requests_by_position
        id_linked = allocation_id in requests_by_allocation
        if not position_linked and not id_linked:
            quality["allocation_without_approval_request"] += 1

    for allocation in view["allocations"]:
        if _missing_exit_price(allocation):
            quality["closed_allocation_without_exit_price"] += 1

    decision_positions = {
        record["position_id"]
        for record in records
        if record["status"] in (DECISION_BUY, DECISION_SKIP)
    }
    unmatched = 0
    for record in records:
        if record["status"] not in (DECISION_BUY, DECISION_SKIP):
            continue
        if not record["matched"]:
            unmatched += 1
    for allocation in control_allocations:
        if allocation.get("status") not in (STATUS_OPEN, STATUS_CLOSED):
            continue
        if allocation.get("position_id") not in decision_positions:
            unmatched += 1
    quality["unmatched_control_human_signals"] = unmatched

    counts = {status: 0 for status in DECISION_STATUSES}
    for record in records:
        status = record["status"]
        if status in counts:
            counts[status] += 1

    return {
        "records": records,
        "counts": counts,
        "quality": quality,
        "ignored_non_human_requests": ignored,
    }


def _analyze_request(
    request,
    baselines,
    human_by_position,
    human_by_id,
    control_by_position,
    pairs,
    shared_positions,
    shared_allocations,
):
    """Возвращает (record, list of quality keys). Не читает baseline result как Human PnL."""
    issues = []
    status = request.get("status")
    if status not in DECISION_STATUSES:
        return None, issues

    position_id = request.get("position_id")
    baseline = baselines.get(position_id)
    if baseline is None:
        issues.append("approval_without_baseline")

    human, human_issue = _resolve_human_allocation(
        request,
        human_by_position,
        human_by_id,
        shared_positions,
        shared_allocations,
    )
    if human_issue == "duplicate":
        issues.append("duplicate_logical_links")
        human = None
    elif human_issue == "missing" and status == DECISION_BUY:
        issues.append("buy_without_human_allocation")

    control_rows = control_by_position.get(position_id, [])
    control = None
    if len(control_rows) > 1:
        issues.append("duplicate_logical_links")
    elif len(control_rows) == 1:
        control = control_rows[0]

    mismatch = _baseline_control_mismatch(baseline, control)
    if mismatch:
        issues.append("baseline_control_result_mismatch")

    buy_outcome = None
    buy_result = None
    buy_realized = None
    human_exit_reason = None
    human_entry = None
    if status == DECISION_BUY:
        buy_outcome, buy_result, buy_realized = _human_buy_result(human)
        if human is not None and buy_outcome in (STATUS_CLOSED, STATUS_OPEN, OUTCOME_INCOMPLETE):
            human_exit_reason = human.get("exit_reason")
            human_entry = as_float(human.get("entry_price"))

    counterfactual_outcome = None
    counterfactual_result = None
    counterfactual_exit = None
    if status == DECISION_SKIP:
        (
            counterfactual_outcome,
            counterfactual_result,
            counterfactual_exit,
        ) = _skip_counterfactual(baseline)
        if counterfactual_outcome == OUTCOME_PENDING:
            issues.append("skip_without_completed_baseline")

    if status == DECISION_BUY:
        reference = as_float(request.get("reference_price"))
        execution = as_float(request.get("execution_price"))
        if reference is None or reference <= 0:
            issues.append("missing_reference_price")
        if execution is None or execution <= 0:
            issues.append("missing_execution_price")

    delay = None
    if status in (DECISION_BUY, DECISION_SKIP):
        delay = decision_delay_seconds(request)
        if delay is None:
            issues.append("missing_timestamps")

    slip = None
    if status == DECISION_BUY:
        slip = slippage_percent(
            request.get("reference_price"),
            request.get("execution_price"),
        )

    matched = _is_matched(
        status,
        baseline,
        control,
        human,
        human_issue,
        mismatch,
    )
    pair_id = request.get("pair_id")
    record = {
        "request_id": request.get("id"),
        "position_id": position_id,
        "pair_id": pair_id,
        "pair_symbol": pairs.get(pair_id),
        "status": status,
        "final_score": _score_key(request.get("final_score")),
        "signal_type": request.get("signal_type"),
        "cohort": request.get("cohort"),
        "change_24h": as_float(request.get("change_24h")),
        "change_24h_bucket": change_24h_bucket(request.get("change_24h")),
        "score_band": score_band(request.get("final_score")),
        "recommended_usd": as_float(request.get("recommended_usd")),
        "recommended_percent": as_float(request.get("recommended_percent")),
        "human_allocation_id": None if human is None else human.get("id"),
        "control_allocation_id": None if control is None else control.get("id"),
        "baseline_id": None if baseline is None else baseline.get("id"),
        "buy_outcome": buy_outcome,
        "buy_result_percent": buy_result,
        "buy_realized_pnl_usd": buy_realized,
        "human_exit_reason": human_exit_reason,
        "human_entry_price": human_entry,
        "counterfactual": status == DECISION_SKIP,
        "counterfactual_outcome": counterfactual_outcome,
        "counterfactual_result_percent": counterfactual_result,
        "counterfactual_exit_reason": counterfactual_exit,
        "delay_seconds": delay,
        "slippage_percent": slip,
        "matched": matched,
        "baseline_control_mismatch": mismatch,
        "control_entry_price": None if control is None else as_float(control.get("entry_price")),
        "control_result_percent": _closed_result(control),
        "control_realized_pnl_usd": _closed_realized(control),
        "control_exit_reason": None if control is None else control.get("exit_reason"),
        "control_status": None if control is None else control.get("status"),
        "baseline_status": None if baseline is None else baseline.get("status"),
    }
    record["return_diff_percent"] = _return_diff(record)
    record["usd_diff"] = _usd_diff(record)
    return record, issues


def _human_buy_result(allocation):
    """
    Фактический результат Human.

    Намеренно принимает только allocation портфеля 2.
    baseline.result_percent сюда не передаётся.
    """
    if allocation is None:
        return OUTCOME_UNLINKED, None, None
    status = allocation.get("status")
    if status == STATUS_OPEN:
        return STATUS_OPEN, None, None
    if status != STATUS_CLOSED:
        return OUTCOME_INCOMPLETE, None, None
    result = as_float(allocation.get("result_percent"))
    if result is None:
        return OUTCOME_INCOMPLETE, None, None
    return STATUS_CLOSED, result, as_float(allocation.get("realized_pnl_usd"))


def _skip_counterfactual(baseline):
    """
    Counterfactual SKIP: результат baseline, не Human allocation.

    OPEN или CLOSED без result_percent — PENDING_OUTCOME / N/A.
    """
    if baseline is None:
        return OUTCOME_MISSING_BASELINE, None, None
    if baseline.get("status") != STATUS_CLOSED:
        return OUTCOME_PENDING, None, None
    result = as_float(baseline.get("result_percent"))
    if result is None:
        return OUTCOME_PENDING, None, None
    return OUTCOME_COMPLETE, result, baseline.get("exit_reason")


def _resolve_human_allocation(
    request,
    human_by_position,
    human_by_id,
    shared_positions,
    shared_allocations,
):
    position_id = request.get("position_id")
    matches = list(human_by_position.get(position_id, []))
    pointed_id = request.get("allocation_id")
    pointed = None if pointed_id is None else human_by_id.get(pointed_id)
    inconsistent = False
    if position_id in shared_positions:
        inconsistent = True
    if pointed_id in shared_allocations:
        inconsistent = True
    if len(matches) > 1:
        inconsistent = True
    if pointed_id is not None:
        if pointed is None:
            inconsistent = True
        elif pointed.get("portfolio_id") != HUMAN_PORTFOLIO_ID:
            inconsistent = True
        elif pointed.get("position_id") != position_id:
            inconsistent = True
        elif matches and pointed.get("id") not in {row.get("id") for row in matches}:
            inconsistent = True
    if inconsistent:
        return None, "duplicate"
    if pointed is not None:
        return pointed, None
    if len(matches) == 1:
        return matches[0], None
    if request.get("status") == DECISION_BUY:
        return None, "missing"
    return None, None


def _baseline_control_mismatch(baseline, control):
    if baseline is None or control is None:
        return False
    if baseline.get("status") != STATUS_CLOSED or control.get("status") != STATUS_CLOSED:
        return False
    baseline_result = as_float(baseline.get("result_percent"))
    control_result = as_float(control.get("result_percent"))
    if baseline_result is None or control_result is None:
        return False
    return abs(baseline_result - control_result) > RESULT_TOLERANCE


def _is_matched(status, baseline, control, human, human_issue, mismatch):
    if status not in (DECISION_BUY, DECISION_SKIP):
        return False
    if human_issue == "duplicate" or mismatch:
        return False
    if baseline is None or control is None:
        return False
    if control.get("status") not in (STATUS_OPEN, STATUS_CLOSED):
        return False
    if baseline.get("status") != control.get("status"):
        return False
    if status == DECISION_BUY and human is None:
        return False
    if baseline.get("status") == STATUS_CLOSED:
        if as_float(baseline.get("result_percent")) is None:
            return False
        if as_float(control.get("result_percent")) is None:
            return False
    return True


def _return_diff(record):
    if not record["matched"] or record["status"] != DECISION_BUY:
        return None
    human_result = record.get("buy_result_percent")
    control_result = record.get("control_result_percent")
    if human_result is None or control_result is None:
        return None
    return human_result - control_result


def _usd_diff(record):
    if not record["matched"] or record["status"] != DECISION_BUY:
        return None
    human_usd = record.get("buy_realized_pnl_usd")
    control_usd = record.get("control_realized_pnl_usd")
    if human_usd is None or control_usd is None:
        return None
    return human_usd - control_usd


def _closed_result(allocation):
    if allocation is None or allocation.get("status") != STATUS_CLOSED:
        return None
    return as_float(allocation.get("result_percent"))


def _closed_realized(allocation):
    if allocation is None or allocation.get("status") != STATUS_CLOSED:
        return None
    return as_float(allocation.get("realized_pnl_usd"))


def _buy_quality(records):
    buys = [record for record in records if record["status"] == DECISION_BUY]
    closed = [
        record for record in buys
        if record["buy_outcome"] == STATUS_CLOSED
    ]
    results = [record["buy_result_percent"] for record in closed]
    winners = [value for value in results if value > 0]
    losers = [value for value in results if value < 0]
    realized_values = [record["buy_realized_pnl_usd"] for record in closed]
    realized_ready = bool(closed) and all(value is not None for value in realized_values)
    return {
        "total_buy": len(buys),
        "closed_buy_outcomes": len(closed),
        "open_buy": sum(1 for record in buys if record["buy_outcome"] == STATUS_OPEN),
        "unlinked_buy": sum(
            1 for record in buys if record["buy_outcome"] == OUTCOME_UNLINKED
        ),
        "incomplete_buy": sum(
            1 for record in buys if record["buy_outcome"] == OUTCOME_INCOMPLETE
        ),
        "profitable_buy": len(winners),
        "losing_buy": len(losers),
        "flat_buy": sum(1 for value in results if value == 0),
        "win_rate_percent": (
            None if not results else len(winners) / len(results) * 100.0
        ),
        "realized_pnl_usd": sum(realized_values) if realized_ready else None,
        "average_return_percent": mean(results) if results else None,
        "median_return_percent": median_or_none(results),
    }


def _decision_value(records):
    buys = [
        record for record in records
        if record["status"] == DECISION_BUY and record["buy_outcome"] == STATUS_CLOSED
    ]
    skips = [
        record for record in records
        if record["status"] == DECISION_SKIP
        and record["counterfactual_outcome"] == OUTCOME_COMPLETE
    ]
    open_buys = sum(
        1 for record in records
        if record["status"] == DECISION_BUY and record["buy_outcome"] == STATUS_OPEN
    )
    pending_skips = sum(
        1 for record in records
        if record["status"] == DECISION_SKIP
        and record["counterfactual_outcome"] == OUTCOME_PENDING
    )

    buy_sum = sum(record["buy_result_percent"] for record in buys)
    skip_sum = sum(record["counterfactual_result_percent"] for record in skips)
    if buys or skips:
        raw_value = buy_sum - skip_sum
        raw_status = "ok"
    else:
        raw_value = None
        raw_status = "na"
        buy_sum = None
        skip_sum = None

    saved_percent = 0.0
    missed_percent = 0.0
    profitable_skipped = 0
    losing_skipped = 0
    flat_skipped = 0
    for record in skips:
        result = record["counterfactual_result_percent"]
        if result > 0:
            profitable_skipped += 1
            missed_percent += result
        elif result < 0:
            losing_skipped += 1
            saved_percent += abs(result)
        else:
            flat_skipped += 1

    skip_notionals = [
        notional_pnl(record.get("recommended_usd"), record["counterfactual_result_percent"])
        for record in skips
    ]
    buy_notionals = [
        notional_pnl(record.get("recommended_usd"), record["buy_result_percent"])
        for record in buys
    ]
    skip_ready = bool(skips) and all(value is not None for value in skip_notionals)
    buy_ready = all(value is not None for value in buy_notionals)
    if skip_ready:
        saved_usd = sum(abs(value) for value in skip_notionals if value < 0)
        missed_usd = sum(value for value in skip_notionals if value > 0)
        net_usd = saved_usd - missed_usd
        skip_usd_status = "ok"
    else:
        saved_usd = None
        missed_usd = None
        net_usd = None
        skip_usd_status = "na"

    normalized_ready = buy_ready and (skip_ready or not skips) and (buys or skips)
    if normalized_ready:
        buy_usd = sum(buy_notionals) if buys else 0.0
        skip_usd = sum(skip_notionals) if skips else 0.0
        normalized_value = buy_usd - skip_usd
        normalized_status = "ok"
    else:
        buy_usd = None
        skip_usd = None
        normalized_value = None
        normalized_status = "na"

    if not skips:
        saved_percent_value = None
        missed_percent_value = None
        net_percent_value = None
    else:
        saved_percent_value = saved_percent
        missed_percent_value = missed_percent
        net_percent_value = saved_percent - missed_percent

    return {
        "completed_buy_outcomes": len(buys),
        "completed_skip_outcomes": len(skips),
        "excluded_open_buys": open_buys,
        "excluded_pending_skips": pending_skips,
        "profitable_skipped": profitable_skipped,
        "losing_skipped": losing_skipped,
        "flat_skipped": flat_skipped,
        "saved_loss_percent": saved_percent_value,
        "missed_profit_percent": missed_percent_value,
        "net_skip_value_percent": net_percent_value,
        "saved_loss_usd": saved_usd,
        "missed_profit_usd": missed_usd,
        "net_skip_value_usd": net_usd,
        "skip_usd_status": skip_usd_status,
        "raw": {
            "unit": "percent_points",
            "value": raw_value,
            "status": raw_status,
            "buy_return_sum_percent": buy_sum,
            "skip_counterfactual_return_sum_percent": skip_sum,
            "note": (
                "Сумма фактических Human BUY result_percent минус сумма "
                "baseline result_percent по завершённым SKIP. "
                "Каждый сигнал весит одинаково. Это не доллары и не доход портфеля."
            ),
        },
        "normalized": {
            "unit": "usd_at_recommended_notional",
            "value": normalized_value,
            "status": normalized_status,
            "buy_pnl_usd": buy_usd,
            "skip_counterfactual_pnl_usd": skip_usd,
            "note": (
                "Тот же набор сигналов на recommended_usd каждой заявки "
                "(номинал 1% NAV, если он записан). "
                "Если номинал восстановить нельзя, значение N/A."
            ),
        },
    }


def _skip_quality(records, value):
    skips = [record for record in records if record["status"] == DECISION_SKIP]
    return {
        "total_skip": len(skips),
        "completed_counterfactual": value["completed_skip_outcomes"],
        "pending_outcome": value["excluded_pending_skips"],
        "missing_baseline": sum(
            1 for record in skips
            if record["counterfactual_outcome"] == OUTCOME_MISSING_BASELINE
        ),
        "profitable_skipped": value["profitable_skipped"],
        "losing_skipped": value["losing_skipped"],
        "flat_skipped": value["flat_skipped"],
        "saved_loss_usd": value["saved_loss_usd"],
        "missed_profit_usd": value["missed_profit_usd"],
        "net_skip_value_usd": value["net_skip_value_usd"],
        "saved_loss_percent": value["saved_loss_percent"],
        "missed_profit_percent": value["missed_profit_percent"],
        "net_skip_value_percent": value["net_skip_value_percent"],
        "counterfactual": True,
    }


def _execution_stats(records):
    delays = [
        record["delay_seconds"]
        for record in records
        if record["status"] == DECISION_BUY and record["delay_seconds"] is not None
    ]
    slips = [
        record["slippage_percent"]
        for record in records
        if record["status"] == DECISION_BUY and record["slippage_percent"] is not None
    ]
    return {
        "delay": _distribution(delays),
        "slippage": _distribution(slips),
        "delay_sample": len(delays),
        "slippage_sample": len(slips),
    }


def _distribution(values):
    if not values:
        return {
            "count": 0,
            "average": None,
            "median": None,
            "min": None,
            "max": None,
            "best": None,
            "worst": None,
        }
    return {
        "count": len(values),
        "average": mean(values),
        "median": float(median(values)),
        "min": min(values),
        "max": max(values),
        "best": min(values),
        "worst": max(values),
    }


def _breakdown(records):
    actionable = [
        record for record in records
        if record["status"] in (DECISION_BUY, DECISION_SKIP)
    ]
    return {
        "by_decision": _group_rows(actionable, lambda record: record["status"], (DECISION_BUY, DECISION_SKIP)),
        "by_final_score": _group_rows(actionable, lambda record: record["final_score"]),
        "by_signal_type": _group_rows(actionable, lambda record: record["signal_type"], SIGNAL_ORDER),
        "by_cohort": _group_rows(actionable, lambda record: record["cohort"], COHORT_ORDER),
        "by_change_24h": _group_rows(
            actionable,
            lambda record: record["change_24h_bucket"],
            CHANGE_BUCKETS,
        ),
        "by_score_band": _group_rows(
            actionable,
            lambda record: record["score_band"],
            SCORE_BANDS,
        ),
        "by_exit_reason": _exit_rows(actionable),
        "by_delay": _delay_rows(actionable),
    }


def _group_rows(records, key_fn, preferred=()):
    grouped = {}
    for record in records:
        grouped.setdefault(key_fn(record), []).append(record)
    keys = list(grouped.keys())
    keys.sort(key=lambda key: _preferred_key(key, preferred))
    rows = []
    for key in keys:
        members = grouped[key]
        returns = []
        counterfactual = 0
        for record in members:
            outcome = _analysis_return(record)
            if outcome is None:
                continue
            returns.append(outcome["return_percent"])
            if outcome["counterfactual"]:
                counterfactual += 1
        winners = [value for value in returns if value > 0]
        rows.append({
            "key": "unknown" if key is None else key,
            "decisions": len(members),
            "completed": len(returns),
            "win_rate_percent": (
                None if not returns else len(winners) / len(returns) * 100.0
            ),
            "average_return_percent": mean(returns) if returns else None,
            "counterfactual_completed": counterfactual,
        })
    return rows


def _exit_rows(records):
    completed = []
    for record in records:
        reason = _completed_exit_reason(record)
        if reason is None:
            continue
        completed.append((record, reason))
    return _group_rows(
        [_with_exit_key(record, reason) for record, reason in completed],
        lambda record: record["_exit_key"],
        EXIT_ORDER,
    )


def _with_exit_key(record, reason):
    cloned = dict(record)
    cloned["_exit_key"] = reason
    return cloned


def _delay_rows(records):
    sampled = [
        record for record in records
        if record.get("delay_seconds") is not None
    ]
    if not sampled:
        return []
    return _group_rows(sampled, lambda record: delay_bucket(record["delay_seconds"]), DELAY_BUCKETS)


def _analysis_return(record):
    if record["status"] == DECISION_BUY and record["buy_outcome"] == STATUS_CLOSED:
        return {
            "return_percent": record["buy_result_percent"],
            "counterfactual": False,
        }
    if (
        record["status"] == DECISION_SKIP
        and record["counterfactual_outcome"] == OUTCOME_COMPLETE
    ):
        return {
            "return_percent": record["counterfactual_result_percent"],
            "counterfactual": True,
        }
    return None


def _completed_exit_reason(record):
    outcome = _analysis_return(record)
    if outcome is None:
        return None
    if outcome["counterfactual"]:
        reason = record.get("counterfactual_exit_reason")
    else:
        reason = record.get("human_exit_reason")
    if reason is None or str(reason).strip() == "":
        return "unknown"
    return reason


def _matched_block(records):
    matched = [record for record in records if record["matched"]]
    buys = [record for record in matched if record["status"] == DECISION_BUY]
    skips = [record for record in matched if record["status"] == DECISION_SKIP]
    return {
        "count": len(matched),
        "buy_count": len(buys),
        "skip_count": len(skips),
        "buys": [_public_matched(record) for record in buys],
        "skips": [_public_matched(record) for record in skips],
    }


def _public_matched(record):
    return {
        "request_id": record["request_id"],
        "position_id": record["position_id"],
        "pair_id": record["pair_id"],
        "pair_symbol": record["pair_symbol"],
        "status": record["status"],
        "counterfactual": record["status"] == DECISION_SKIP,
        "control_entry_price": record["control_entry_price"],
        "human_execution_price": record["human_entry_price"],
        "control_result_percent": record["control_result_percent"],
        "human_result_percent": record["buy_result_percent"],
        "return_diff_percent": record["return_diff_percent"],
        "usd_diff": record["usd_diff"],
        "delay_seconds": record["delay_seconds"],
        "counterfactual_result_percent": record["counterfactual_result_percent"],
        "counterfactual_exit_reason": record["counterfactual_exit_reason"],
        "control_realized_pnl_usd": record["control_realized_pnl_usd"],
        "human_realized_pnl_usd": record["buy_realized_pnl_usd"],
    }


def _sample_block(records, buy, skip):
    eligible = sum(
        1 for record in records
        if record["status"] in (DECISION_BUY, DECISION_SKIP)
    )
    completed_buy = buy["closed_buy_outcomes"]
    completed_skip = skip["completed_counterfactual"]
    completed = completed_buy + completed_skip
    return {
        "eligible_decisions": eligible,
        "completed_buy_outcomes": completed_buy,
        "completed_skip_outcomes": completed_skip,
        "completed_decisions": completed,
        "label": sample_label(completed),
        "note": SAMPLE_NOTE,
    }


def _summary(portfolios, counts, value, sample):
    return {
        "control_nav_usd": portfolios["control"]["current_nav_usd"],
        "human_nav_usd": portfolios["human"]["current_nav_usd"],
        "control_return_percent": portfolios["control"]["total_return_percent"],
        "human_return_percent": portfolios["human"]["total_return_percent"],
        "decision_counts": counts,
        "net_skip_value_usd": value["net_skip_value_usd"],
        "raw_decision_value_percent": value["raw"]["value"],
        "normalized_decision_value_usd": value["normalized"]["value"],
        "sample_label": sample["label"],
    }


def _empty_quality():
    return {key: 0 for key in QUALITY_KEYS}


def _attach_events(allocations, ledger):
    buys = {}
    sells = {}
    for event in ledger:
        allocation_id = event.get("allocation_id")
        if allocation_id is None:
            continue
        if event.get("event_type") == "BUY":
            buys.setdefault(allocation_id, []).append(event)
        elif event.get("event_type") == "SELL":
            sells.setdefault(allocation_id, []).append(event)
    for allocation in allocations:
        allocation_id = allocation.get("id")
        allocation["buy_events"] = list(buys.get(allocation_id, []))
        allocation["sell_events"] = list(sells.get(allocation_id, []))


def _for_portfolio(rows, portfolio_id):
    return [row for row in rows if row.get("portfolio_id") == portfolio_id]


def _group_by(rows, field):
    grouped = {}
    for row in rows:
        grouped.setdefault(row.get(field), []).append(row)
    return grouped


def _result_values(allocations):
    values = []
    for allocation in allocations:
        result = as_float(allocation.get("result_percent"))
        if result is not None:
            values.append(result)
    return values


def _extreme_trade(allocations, pick):
    rows = []
    for allocation in allocations:
        result = as_float(allocation.get("result_percent"))
        if result is None:
            continue
        rows.append((allocation, result))
    if not rows:
        return None
    if pick == "best":
        allocation, result = min(
            rows,
            key=lambda item: (-item[1], item[0].get("id") or 0),
        )
    else:
        allocation, result = min(
            rows,
            key=lambda item: (item[1], item[0].get("id") or 0),
        )
    return {
        "id": allocation.get("id"),
        "pair_id": allocation.get("pair_id"),
        "result_percent": result,
        "realized_pnl_usd": as_float(allocation.get("realized_pnl_usd")),
        "exit_reason": allocation.get("exit_reason"),
        "final_score": allocation.get("final_score"),
    }


def _sum_optional(values):
    total = 0.0
    seen = False
    for value in values:
        number = as_float(value)
        if number is None:
            continue
        total += number
        seen = True
    if not seen:
        return None
    return total


def _missing_exit_price(allocation):
    if allocation.get("status") != STATUS_CLOSED:
        return False
    price = as_float(allocation.get("exit_price"))
    return price is None or price <= 0


def _score_key(value):
    number = as_float(value)
    if number is None:
        return None
    if number == int(number):
        return int(number)
    return number


def _preferred_key(value, preferred):
    if value is None:
        return (2, 0, "")
    try:
        index = preferred.index(value)
    except ValueError:
        return (1, 0, str(value))
    return (0, index, "")


def _table_names(connection):
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row["name"] for row in rows}


def _plain(row):
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def query_only_enabled(connection):
    """Проверка PRAGMA query_only для тестов и диагностики."""
    row = connection.execute("PRAGMA query_only").fetchone()
    if row is None:
        return False
    return int(row[0]) == 1


def readonly_uri(db_path):
    return "{0}?mode=ro".format(Path(db_path).resolve().as_uri())
