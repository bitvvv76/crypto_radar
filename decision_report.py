"""
CLI-отчёт Human vs Control / Decision Analytics v0.9.

python decision_report.py
python decision_report.py --db crypto_radar.db

Только чтение. Заявки, сделки, NAV и ledger не изменяются.
Telegram и торговый цикл здесь не вызываются.
"""

import argparse
import sys

from database import DB_NAME
from human_vs_control import (
    PROFIT_FACTOR_NO_LOSS,
    build_report,
)


SECTION_RULE = "-" * 72


def configure_console():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def format_usd(value, signed=False):
    if value is None:
        return "N/A"
    if signed:
        return "{0:+,.2f}".format(value)
    return "{0:,.2f}".format(value)


def format_percent(value, signed=True):
    if value is None:
        return "N/A"
    if signed:
        return "{0:+.2f}%".format(value)
    return "{0:.2f}%".format(value)


def format_number(value, digits=2):
    if value is None:
        return "N/A"
    return "{0:.{1}f}".format(value, digits)


def format_count(value):
    if value is None:
        return "N/A"
    return str(int(value))


def format_delay(seconds):
    if seconds is None:
        return "N/A"
    total = int(round(seconds))
    sign = "-" if total < 0 else ""
    total = abs(total)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "{0}{1}h {2}m {3}s".format(sign, hours, minutes, secs)
    if minutes:
        return "{0}{1}m {2}s".format(sign, minutes, secs)
    return "{0}{1}s".format(sign, secs)


def format_price(value):
    if value is None:
        return "N/A"
    return "{0:.6f}".format(value)


def format_key(value):
    if value is None:
        return "unknown"
    return str(value)


def format_profit_factor(portfolio):
    status = portfolio.get("profit_factor_status")
    if status == PROFIT_FACTOR_NO_LOSS:
        return "N/A (no losing trades)"
    return format_number(portfolio.get("profit_factor"))


def render_report(report):
    source = report["source"]
    print()
    print("Crypto Radar Human vs Control v0.9")
    print("read-only decision analytics")
    print(SECTION_RULE)
    print("Режим: read-only. Сделки, approval, NAV и ledger не изменяются.")
    print("Сеть, DEX и Telegram не используются.")
    print("Источник: {0}".format(source.get("db_path") or "N/A"))
    if source.get("reason"):
        print("Состояние источника: {0}".format(source["reason"]))
    print("Portfolio comparison и Decision comparison показаны раздельно.")
    print()
    _render_summary(report)
    _render_portfolios(report["portfolios"])
    _render_decisions(report)
    _render_buy(report["buy_quality"])
    _render_skip(report["skip_quality"])
    _render_value(report["decision_value"])
    _render_execution(report["execution"])
    _render_breakdown(report["breakdown"])
    _render_matched(report["matched"], report["data_quality"])
    _render_quality(report["data_quality"])
    _render_sample(report["sample"])


def main(argv=None):
    configure_console()
    parser = argparse.ArgumentParser(
        description="Read-only отчёт Human vs Control v0.9"
    )
    parser.add_argument("--db", dest="db_path", default=None)
    args = parser.parse_args(argv)
    db_path = args.db_path or DB_NAME
    render_report(build_report(db_path))
    return 0


def _render_summary(report):
    summary = report["summary"]
    counts = summary["decision_counts"]
    print("1. HUMAN vs CONTROL — SUMMARY")
    print(SECTION_RULE)
    print("Control NAV:     {0} USD".format(format_usd(summary["control_nav_usd"])))
    print("Control return:  {0}".format(format_percent(summary["control_return_percent"])))
    print("Human NAV:       {0} USD".format(format_usd(summary["human_nav_usd"])))
    print("Human return:    {0}".format(format_percent(summary["human_return_percent"])))
    print("Decisions BUY:   {0}".format(counts.get("BUY", 0)))
    print("Decisions SKIP:  {0}".format(counts.get("SKIP", 0)))
    print("Expired:         {0}".format(counts.get("EXPIRED", 0)))
    print("Pending:         {0}".format(counts.get("PENDING", 0)))
    print("Net Skip Value:  {0} USD".format(
        format_usd(summary["net_skip_value_usd"], signed=True)
    ))
    print("Sample:          {0}".format(summary["sample_label"]))
    print("Два вопроса не смешиваются: факт книг 1 и 2, и ценность решений BUY/SKIP.")
    print()


def _render_portfolios(portfolios):
    print("2. PORTFOLIO COMPARISON")
    print(SECTION_RULE)
    print("CONTROL = portfolio 1. HUMAN = portfolio 2.")
    print("{0:<28} {1:>18} {2:>18}".format("Metric", "Control", "Human"))
    rows = (
        ("Initial deposit USD", "initial_deposit_usd", "usd"),
        ("Current cash USD", "current_cash_usd", "usd"),
        ("Current NAV USD", "current_nav_usd", "usd"),
        ("Total PnL USD", "total_pnl_usd", "usd_signed"),
        ("Total return %", "total_return_percent", "percent"),
        ("Peak equity USD", "peak_equity_usd", "usd"),
        ("Max drawdown %", "max_drawdown_percent", "percent_plain"),
        ("Allocations total", "allocations_total", "count"),
        ("OPEN", "open_count", "count"),
        ("CLOSED", "closed_count", "count"),
        ("SKIPPED", "skipped_count", "count"),
        ("Realized PnL USD", "realized_pnl_usd", "usd_signed"),
        ("Unrealized PnL USD", "unrealized_pnl_usd", "usd_signed"),
        ("Win rate CLOSED %", "win_rate_percent", "percent_plain"),
        ("Average trade PnL %", "average_trade_pnl_percent", "percent"),
        ("Median trade PnL %", "median_trade_pnl_percent", "percent"),
        ("Average winner %", "average_winner_percent", "percent"),
        ("Average loser %", "average_loser_percent", "percent"),
        ("Max concurrent OPEN", "max_concurrent_open", "count"),
        ("Max capital invested USD", "max_capital_invested_usd", "usd"),
        ("Max exposure %", "max_exposure_percent", "percent_plain"),
    )
    control = portfolios["control"]
    human = portfolios["human"]
    for label, key, kind in rows:
        print("{0:<28} {1:>18} {2:>18}".format(
            label,
            _format_metric(control.get(key), kind),
            _format_metric(human.get(key), kind),
        ))
    print("{0:<28} {1:>18} {2:>18}".format(
        "Profit factor",
        format_profit_factor(control),
        format_profit_factor(human),
    ))
    _render_extreme("Best trade", control.get("best_trade"), human.get("best_trade"))
    _render_extreme("Worst trade", control.get("worst_trade"), human.get("worst_trade"))
    print("Win rate считается только по CLOSED с result_percent. OPEN не входит.")
    print("Пустая выборка показывает N/A, а не 0% win rate.")
    print()


def _render_extreme(label, control_trade, human_trade):
    print("{0:<28} {1:>18} {2:>18}".format(
        label,
        _format_trade(control_trade),
        _format_trade(human_trade),
    ))


def _format_trade(trade):
    if not trade:
        return "N/A"
    return "{0} #{1}".format(
        format_percent(trade.get("result_percent")),
        trade.get("id"),
    )


def _render_decisions(report):
    counts = report["decision_counts"]
    print("3. HUMAN DECISIONS")
    print(SECTION_RULE)
    print("Учитываются только approval_requests портфеля 2.")
    print("BUY:     {0}".format(counts.get("BUY", 0)))
    print("SKIP:    {0}".format(counts.get("SKIP", 0)))
    print("EXPIRED: {0}".format(counts.get("EXPIRED", 0)))
    print("PENDING: {0}".format(counts.get("PENDING", 0)))
    ignored = report.get("ignored_non_human_requests") or 0
    if ignored:
        print("Заявки вне портфеля 2, не включены: {0}".format(ignored))
    print("EXPIRED и PENDING не являются завершёнными решениями.")
    print()


def _render_buy(buy):
    print("4. BUY QUALITY")
    print(SECTION_RULE)
    print("Источник результата: Human allocation portfolio 2.")
    print("baseline.result_percent здесь не используется.")
    print("Total BUY decisions:   {0}".format(buy["total_buy"]))
    print("Closed BUY outcomes:   {0}".format(buy["closed_buy_outcomes"]))
    print("OPEN BUY:              {0}".format(buy["open_buy"]))
    print("Profitable BUY:        {0}".format(buy["profitable_buy"]))
    print("Losing BUY:            {0}".format(buy["losing_buy"]))
    print("Flat BUY:              {0}".format(buy["flat_buy"]))
    print("Win rate:              {0}".format(
        format_percent(buy["win_rate_percent"], signed=False)
    ))
    print("Realized PnL:          {0} USD".format(
        format_usd(buy["realized_pnl_usd"], signed=True)
    ))
    print("Average return:        {0}".format(format_percent(buy["average_return_percent"])))
    print("Median return:         {0}".format(format_percent(buy["median_return_percent"])))
    print("Unlinked BUY:          {0}".format(buy["unlinked_buy"]))
    print("Incomplete BUY:        {0}".format(buy["incomplete_buy"]))
    print()


def _render_skip(skip):
    print("5. SKIP QUALITY")
    print(SECTION_RULE)
    print("SKIP counterfactual — это не реальная Human сделка.")
    print("Траектория: baseline paper_positions того же position_id.")
    print("Незавершённый baseline: PENDING_OUTCOME / N/A.")
    print("Total SKIP decisions:              {0}".format(skip["total_skip"]))
    print("Completed counterfactual outcomes: {0}".format(skip["completed_counterfactual"]))
    print("Pending outcome:                   {0}".format(skip["pending_outcome"]))
    print("Missing baseline:                  {0}".format(skip["missing_baseline"]))
    print("Profitable skipped signals:        {0}".format(skip["profitable_skipped"]))
    print("Losing skipped signals:            {0}".format(skip["losing_skipped"]))
    print("Saved Loss:                        {0} USD".format(
        format_usd(skip["saved_loss_usd"])
    ))
    print("Missed Profit:                     {0} USD".format(
        format_usd(skip["missed_profit_usd"])
    ))
    print("Net Skip Value:                    {0} USD".format(
        format_usd(skip["net_skip_value_usd"], signed=True)
    ))
    print("Saved Loss percent points:         {0}".format(
        format_number(skip["saved_loss_percent"])
    ))
    print("Missed Profit percent points:      {0}".format(
        format_number(skip["missed_profit_percent"])
    ))
    print("Net Skip Value percent points:     {0}".format(
        format_number(skip["net_skip_value_percent"], digits=2)
        if skip["net_skip_value_percent"] is not None else "N/A"
    ))
    print("Saved Loss = сумма |убытка| пропущенных сигналов, которые в counterfactual убыточны.")
    print("Missed Profit = сумма прибыли пропущенных сигналов, которые в counterfactual прибыльны.")
    print("Net Skip Value = Saved Loss - Missed Profit.")
    print("USD считается на recommended_usd заявки. Нет номинала — N/A.")
    print()


def _render_value(value):
    raw = value["raw"]
    normalized = value["normalized"]
    print("6. DECISION VALUE")
    print(SECTION_RULE)
    print("Сравнение завершённых BUY и SKIP. OPEN и PENDING_OUTCOME не подставляются нулём.")
    print("Excluded OPEN BUY:            {0}".format(value["excluded_open_buys"]))
    print("Excluded pending SKIP:        {0}".format(value["excluded_pending_skips"]))
    print("RAW DECISION VALUE:           {0} percent points".format(
        format_number(raw["value"])
    ))
    print("  status:                     {0}".format(raw["status"]))
    print("  BUY return sum:             {0}".format(
        format_number(raw["buy_return_sum_percent"])
    ))
    print("  SKIP counterfactual sum:    {0}".format(
        format_number(raw["skip_counterfactual_return_sum_percent"])
    ))
    print("  {0}".format(raw["note"]))
    print("NORMALIZED DECISION VALUE:   {0} USD".format(
        format_usd(normalized["value"], signed=True)
    ))
    print("  status:                     {0}".format(normalized["status"]))
    print("  BUY notional PnL:           {0} USD".format(
        format_usd(normalized["buy_pnl_usd"], signed=True)
    ))
    print("  SKIP counterfactual PnL:    {0} USD".format(
        format_usd(normalized["skip_counterfactual_pnl_usd"], signed=True)
    ))
    print("  {0}".format(normalized["note"]))
    print()


def _render_execution(execution):
    delay = execution["delay"]
    slip = execution["slippage"]
    print("7. EXECUTION DELAY / SLIPPAGE")
    print(SECTION_RULE)
    print("Delay = decision/execution time - signal/request creation time. Только BUY.")
    print("Delay sample:   {0}".format(delay["count"]))
    print("Average delay:  {0}".format(format_delay(delay["average"])))
    print("Median delay:   {0}".format(format_delay(delay["median"])))
    print("Min delay:      {0}".format(format_delay(delay["min"])))
    print("Max delay:      {0}".format(format_delay(delay["max"])))
    print("Slippage = (execution_price / reference_price - 1) * 100. Сетевых цен нет.")
    print("Slippage sample: {0}".format(slip["count"]))
    print("Average slippage: {0}".format(format_percent(slip["average"])))
    print("Median slippage:  {0}".format(format_percent(slip["median"])))
    print("Best slippage:    {0}".format(format_percent(slip["best"])))
    print("Worst slippage:   {0}".format(format_percent(slip["worst"])))
    print()


def _render_breakdown(breakdown):
    print("8. BREAKDOWN")
    print(SECTION_RULE)
    print("BUY: фактический Human result. SKIP: counterfactual baseline. Смешение помечено.")
    print("Win rate в разрезе — доля положительных исходов только по completed. Иначе N/A.")
    _render_table("BUY / SKIP", breakdown["by_decision"])
    _render_table("final_score", breakdown["by_final_score"])
    _render_table("signal_type", breakdown["by_signal_type"])
    _render_table("cohort", breakdown["by_cohort"])
    _render_table("change_24h", breakdown["by_change_24h"])
    _render_table("score band", breakdown["by_score_band"])
    _render_table("exit_reason", breakdown["by_exit_reason"])
    if breakdown["by_delay"]:
        _render_table("decision delay", breakdown["by_delay"])
    else:
        print("decision delay: N/A (no delay sample)")
        print()


def _render_table(title, rows):
    print(title)
    if not rows:
        print("  N/A")
        print()
        return
    print("  {0:<16} {1:>6} {2:>10} {3:>10} {4:>10} {5:>8}".format(
        "key", "n", "completed", "positive%", "avg %", "cf"
    ))
    for row in rows:
        print("  {0:<16} {1:>6} {2:>10} {3:>10} {4:>10} {5:>8}".format(
            format_key(row["key"]),
            row["decisions"],
            row["completed"],
            format_percent(row["win_rate_percent"], signed=False),
            format_percent(row["average_return_percent"]),
            row["counterfactual_completed"],
        ))
    print()


def _render_matched(matched, quality):
    print("9. MATCHED SIGNALS")
    print(SECTION_RULE)
    print("Только сигналы одной baseline trajectory, сопоставленные между книгами.")
    print("Несвязанные сделки в сравнение не входят.")
    print("Matched: {0} (BUY {1}, SKIP {2})".format(
        matched["count"],
        matched["buy_count"],
        matched["skip_count"],
    ))
    print("Unmatched control/human signals: {0}".format(
        quality["unmatched_control_human_signals"]
    ))
    if not matched["buys"] and not matched["skips"]:
        print("N/A (no matched signals)")
        print()
        return
    for row in matched["buys"]:
        print("BUY request #{0} position #{1}".format(
            row["request_id"], row["position_id"]
        ))
        print("  control entry:     {0}".format(format_price(row["control_entry_price"])))
        print("  human execution:   {0}".format(format_price(row["human_execution_price"])))
        print("  control result:    {0}".format(format_percent(row["control_result_percent"])))
        print("  human result:      {0}".format(format_percent(row["human_result_percent"])))
        print("  return diff:       {0}".format(format_percent(row["return_diff_percent"])))
        print("  USD diff:          {0} USD".format(format_usd(row["usd_diff"], signed=True)))
        print("  delay:             {0}".format(format_delay(row["delay_seconds"])))
    for row in matched["skips"]:
        print("SKIP request #{0} position #{1} [counterfactual, not a human trade]".format(
            row["request_id"], row["position_id"]
        ))
        print("  baseline/control:  {0}".format(
            format_percent(row["counterfactual_result_percent"])
        ))
        exit_reason = row["counterfactual_exit_reason"]
        print("  exit reason:       {0}".format(
            "N/A" if exit_reason is None else exit_reason
        ))
        print("  delay:             {0}".format(format_delay(row["delay_seconds"])))
    print()


def _render_quality(quality):
    print("10. DATA QUALITY")
    print(SECTION_RULE)
    print("Ничего автоматически не исправляется.")
    labels = (
        ("approval_without_baseline", "Approval request без baseline"),
        ("buy_without_human_allocation", "BUY request без Human allocation"),
        ("allocation_without_approval_request", "Human allocation без approval request"),
        ("duplicate_logical_links", "Duplicate logical links"),
        ("skip_without_completed_baseline", "SKIP без завершённого baseline"),
        ("closed_allocation_without_exit_price", "CLOSED allocation без exit price"),
        ("missing_reference_price", "Missing reference price"),
        ("missing_execution_price", "Missing execution price"),
        ("missing_timestamps", "Missing timestamps"),
        ("unmatched_control_human_signals", "Unmatched control/human signals"),
        ("baseline_control_result_mismatch", "Baseline/control result mismatch"),
    )
    for key, label in labels:
        print("{0:<44} {1}".format(label, quality.get(key, 0)))
    print()


def _render_sample(sample):
    print("11. SAMPLE SIZE / CONFIDENCE WARNING")
    print(SECTION_RULE)
    print("Total eligible decisions:            {0}".format(sample["eligible_decisions"]))
    print("Completed BUY outcomes:              {0}".format(sample["completed_buy_outcomes"]))
    print("Completed SKIP counterfactual:       {0}".format(sample["completed_skip_outcomes"]))
    print("Completed decisions:                 {0}".format(sample["completed_decisions"]))
    print("Sample status:                       {0}".format(sample["label"]))
    print(sample["note"])
    print("v0.9 не делает вывод о прибыльности стратегии по этой выборке.")
    print()


def _format_metric(value, kind):
    if kind == "usd":
        return format_usd(value)
    if kind == "usd_signed":
        return format_usd(value, signed=True)
    if kind == "percent":
        return format_percent(value)
    if kind == "percent_plain":
        return format_percent(value, signed=False)
    if kind == "count":
        return format_count(value)
    return format_number(value)


if __name__ == "__main__":
    sys.exit(main())
