"""
CLI-отчёт Paper Analytics v0.6.

python portfolio_report.py
python portfolio_report.py --db crypto_radar.db

Только чтение. Счёт, NAV, allocations и ledger не изменяются.
"""

import argparse
import sys

from database import DB_NAME
from paper_analytics import build_report, load_portfolio


def configure_console():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def format_usd(value, signed=False):
    if value is None:
        return "—"
    if signed:
        return "{0:+,.2f}".format(value)
    return "{0:,.2f}".format(value)


def format_percent(value, signed=True):
    if value is None:
        return "—"
    if signed:
        return "{0:+.2f}%".format(value)
    return "{0:.2f}%".format(value)


def format_ratio(value):
    if value is None:
        return "—"
    return "{0:.2f}".format(value)


def format_price(value):
    if value is None:
        return "—"
    return "{0:.4f}".format(value)


def format_score(value):
    if value is None:
        return "—"
    number = float(value)
    if number == int(number):
        return str(int(number))
    return str(value)


def format_duration(seconds):
    if seconds is None:
        return "—"
    total = int(seconds)
    if total < 0:
        return "—"
    minutes = total // 60
    days, remainder = divmod(minutes, 24 * 60)
    hours, mins = divmod(remainder, 60)
    if days > 0:
        return "{0}d {1}h".format(days, hours)
    if hours > 0:
        return "{0}h {1}m".format(hours, mins)
    return "{0}m".format(mins)


def format_key(value):
    if value is None:
        return "—"
    if isinstance(value, float) and value == int(value):
        return str(int(value))
    return str(value)


def render_report(report, db_path):
    print()
    print("Crypto Radar Paper Analytics v0.6")
    print("read-only / paper portfolio journal")
    print("-" * 72)
    print("Режим: read-only. Счёт, NAV, ledger и allocations не изменяются.")
    print("Источник: {0}".format(db_path))
    print("Портфель: 1")
    print()
    _render_account(report["account"])
    _render_positions(report["positions"])
    _render_closed(report["closed_trades"])
    _render_breakdown(report["breakdown"])
    _render_capital(report["capital_risk"])
    _render_recent(report["recent_trades"])
    _render_quality(report["data_quality"])


def main(argv=None):
    configure_console()
    parser = argparse.ArgumentParser(
        description="Read-only отчёт Paper Portfolio v0.6"
    )
    parser.add_argument("--db", dest="db_path", default=None)
    args = parser.parse_args(argv)
    db_path = args.db_path or DB_NAME
    report = build_report(load_portfolio(db_path))
    render_report(report, db_path)
    return 0


def _render_account(account):
    print("1. ACCOUNT")
    print("-" * 72)
    print("Initial capital:   {0} USD".format(
        format_usd(account["initial_capital_usd"])
    ))
    print("Current cash:      {0} USD".format(
        format_usd(account["current_cash_usd"])
    ))
    print("Current NAV:       {0} USD".format(
        format_usd(account["current_nav_usd"])
    ))
    print("Total PnL:         {0} USD".format(
        format_usd(account["total_pnl_usd"], signed=True)
    ))
    print("Total return:      {0}".format(
        format_percent(account["total_return_percent"])
    ))
    print("Peak equity:       {0} USD".format(
        format_usd(account["peak_equity_usd"])
    ))
    print("Max drawdown:      {0}".format(
        format_percent(account["max_drawdown_percent"], signed=False)
    ))
    print()


def _render_positions(positions):
    print("2. POSITIONS")
    print("-" * 72)
    print("OPEN:        {0}".format(positions["open_count"]))
    print("CLOSED:      {0}".format(positions["closed_count"]))
    print("SKIPPED:     {0}".format(positions["skipped_count"]))
    print("Open value:     {0} USD".format(format_usd(positions["open_value_usd"])))
    print("Unrealized:     {0} USD".format(
        format_usd(positions["unrealized_pnl_usd"], signed=True)
    ))
    print("Realized:       {0} USD".format(
        format_usd(positions["realized_pnl_usd"], signed=True)
    ))
    print()


def _render_closed(closed):
    print("3. CLOSED TRADES")
    print("-" * 72)
    print("Закрытых сделок: {0}".format(closed["closed_count"]))
    print("В расчёте:       {0}".format(closed["result_count"]))
    print("Прибыльных:      {0}".format(closed["winners"]))
    print("Убыточных:       {0}".format(closed["losers"]))
    print("Win rate:        {0}".format(
        format_percent(closed["win_rate_percent"], signed=False)
    ))
    print("Average result:  {0}".format(
        format_percent(closed["average_result_percent"])
    ))
    print("Median result:   {0}".format(
        format_percent(closed["median_result_percent"])
    ))
    print("Best trade:      {0}".format(_format_extremum(closed["best"])))
    print("Worst trade:     {0}".format(_format_extremum(closed["worst"])))
    print("Average winner:  {0}".format(
        format_percent(closed["average_winner_percent"])
    ))
    print("Average loser:   {0}".format(
        format_percent(closed["average_loser_percent"])
    ))
    print("Profit factor:   {0}".format(format_ratio(closed["profit_factor"])))
    print()


def _render_breakdown(breakdown):
    print("4. BREAKDOWN")
    print("-" * 72)
    labels = (
        ("final_score", "final_score"),
        ("signal_type", "signal_type"),
        ("cohort", "cohort"),
        ("exit_reason", "exit_reason"),
    )
    printed = False
    for key, label in labels:
        rows = breakdown[key]
        if not rows:
            continue
        printed = True
        print(label)
        for row in rows:
            print(
                "  {0}  n={1}  win rate {2}  avg {3}  realized {4} USD".format(
                    format_key(row["key"]),
                    row["n"],
                    format_percent(row["win_rate_percent"], signed=False),
                    format_percent(row["average_result_percent"]),
                    format_usd(row["realized_pnl_usd"], signed=True),
                )
            )
        print()
    if not printed:
        print("Нет закрытых сделок с result %.")
        print()


def _render_capital(capital):
    print("5. CAPITAL / RISK")
    print("-" * 72)
    print("Max concurrent positions:  {0}".format(
        capital["max_concurrent_positions"]
    ))
    print("Max capital in positions:  {0} USD".format(
        format_usd(capital["max_capital_in_positions_usd"])
    ))
    print("Max portfolio exposure:    {0}".format(
        format_percent(capital["max_exposure_percent"], signed=False)
    ))
    print("Max drawdown:              {0}".format(
        format_percent(capital["max_drawdown_percent"], signed=False)
    ))
    print("Current drawdown:          {0}".format(
        format_percent(capital["current_drawdown_percent"], signed=False)
    ))
    print()


def _render_recent(trades):
    print("6. RECENT TRADES")
    print("-" * 72)
    print("Duration: BUY ledger -> SELL ledger")
    if not trades:
        print("Нет закрытых сделок.")
        print()
        return
    for trade in trades:
        print(
            "{pair}  score {score}  entry {entry}  exit {exit}  {result}  {pnl} USD  {reason}  {duration}".format(
                pair=trade["pair"],
                score=format_score(trade["score"]),
                entry=format_price(trade["entry_price"]),
                exit=format_price(trade["exit_price"]),
                result=format_percent(trade["result_percent"]),
                pnl=format_usd(trade["realized_pnl_usd"], signed=True),
                reason=trade["exit_reason"] or "—",
                duration=format_duration(trade["duration_seconds"]),
            )
        )
    print()


def _render_quality(quality):
    print("7. DATA QUALITY")
    print("-" * 72)
    reason = quality.get("reason")
    if reason == "missing_file":
        print("Файл базы не найден.")
    elif reason == "unreadable":
        print("Файл базы не открывается для чтения.")
    elif reason == "missing_tables":
        print("Таблицы портфеля не найдены.")
    elif reason == "missing_account":
        print("Счёт портфеля не найден.")

    print("Неполных сделок: {0}".format(quality["incomplete_count"]))
    if quality["incomplete_fields"]:
        for field in sorted(quality["incomplete_fields"]):
            print("  {0}: {1}".format(field, quality["incomplete_fields"][field]))
    print("SKIPPED:         {0}".format(quality["skipped_count"]))
    for name, count in sorted(
        quality["skip_reasons"].items(),
        key=lambda item: (-item[1], item[0]),
    ):
        print("  {0}: {1}".format(name, count))
    print("Allocation OPEN при baseline CLOSED: {0}".format(
        quality["open_allocation_baseline_closed"]
    ))
    if reason not in ("missing_file", "unreadable", "missing_tables", "missing_account"):
        print("Post-activation baseline without allocation: {0}".format(
            quality["post_activation_without_allocation"]
        ))
        print("  OPEN:   {0}".format(quality["post_activation_open"]))
        print("  CLOSED: {0}".format(quality["post_activation_closed"]))
        if quality["post_activation_other"]:
            print("  OTHER:  {0}".format(quality["post_activation_other"]))
        if quality["post_activation_unparsed"]:
            print("  UNPARSED: {0}".format(quality["post_activation_unparsed"]))
    print("Расхождение NAV и PnL: {0}".format(
        _yes_no(quality["nav_identity_mismatch"])
    ))
    print("Расхождение ledger и cash: {0}".format(
        _yes_no(quality["ledger_cash_mismatch"])
    ))
    print("Расхождение знака result и PnL: {0}".format(
        quality["sign_mismatch_count"]
    ))
    print("Повторные BUY/SELL: {0}".format(quality["duplicate_ledger_count"]))
    print()


def _format_extremum(trade):
    if trade is None:
        return "—"
    return "{pair}  score {score}  {result}".format(
        pair=trade["pair"],
        score=format_score(trade["score"]),
        result=format_percent(trade["result_percent"]),
    )


def _yes_no(flag):
    if flag:
        return "да"
    return "нет"


if __name__ == "__main__":
    sys.exit(main())
