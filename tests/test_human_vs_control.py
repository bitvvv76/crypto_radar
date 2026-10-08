import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import database
from database import create_tables, ensure_paper_tables
from decision_report import main, render_report
from human_approval import ensure_approval_tables
from human_vs_control import (
    CONTROL_PORTFOLIO_ID,
    HUMAN_PORTFOLIO_ID,
    SAMPLE_EARLY,
    SAMPLE_FORWARD,
    SAMPLE_INSUFFICIENT,
    SAMPLE_LARGER,
    build_report,
    change_24h_bucket,
    connect_readonly_db,
    decision_delay_seconds,
    median_or_none,
    profit_factor_metrics,
    query_only_enabled,
    readonly_uri,
    sample_label,
    score_band,
    slippage_percent,
)
from paper_engine import format_datetime
from paper_portfolio import ensure_portfolio_tables


ENTRY = datetime(2026, 10, 1, 12, 0, 0)
ROOT = Path(__file__).resolve().parents[1]


class HumanVsControlTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "decision.db")
        self.original_db_name = database.DB_NAME
        database.DB_NAME = self.db_path
        create_tables()
        ensure_paper_tables(self.db_path)
        ensure_portfolio_tables(self.db_path)
        ensure_approval_tables(self.db_path)
        self.sequence = 0

    def tearDown(self):
        database.DB_NAME = self.original_db_name
        self.temp_dir.cleanup()

    def text(self, value):
        if isinstance(value, str):
            return value
        return format_datetime(value)

    def execute(self, sql, params=()):
        connection = sqlite3.connect(self.db_path)
        cursor = connection.cursor()
        cursor.execute(sql, params)
        last_id = cursor.lastrowid
        connection.commit()
        connection.close()
        return last_id

    def query(self, sql, params=()):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(sql, params).fetchall()
        connection.close()
        return [dict(row) for row in rows]

    def insert_row(self, table, row):
        columns = list(row.keys())
        sql = "INSERT INTO {table} ({columns}) VALUES ({marks})".format(
            table=table,
            columns=", ".join(columns),
            marks=", ".join("?" for _ in columns),
        )
        return self.execute(sql, [row[column] for column in columns])

    def dump(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        tables = [
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]
        data = {}
        for table in tables:
            rows = connection.execute(
                "SELECT * FROM {0} ORDER BY rowid".format(table)
            ).fetchall()
            data[table] = [tuple(row) for row in rows]
        connection.close()
        return data

    def report(self):
        return build_report(self.db_path)

    def rendered(self, report=None):
        if report is None:
            report = self.report()
        output = StringIO()
        with patch("sys.stdout", output):
            render_report(report)
        return output.getvalue()

    def insert_account(
        self,
        portfolio_id,
        cash,
        initial=None,
        peak=None,
        max_drawdown=0,
        activated_at=ENTRY,
    ):
        if initial is None:
            initial = cash
        if peak is None:
            peak = initial
        activated = self.text(activated_at)
        return self.insert_row("paper_account", {
            "id": portfolio_id,
            "currency": "USD",
            "initial_deposit_usd": initial,
            "cash_usd": cash,
            "activated_at": activated,
            "peak_equity_usd": peak,
            "max_drawdown_percent": max_drawdown,
            "created_at": activated,
            "updated_at": activated,
        })

    def insert_pair(self, address):
        return self.insert_row("pairs", {
            "chain_id": "solana",
            "dex_id": "raydium",
            "pair_address": address,
            "pair_symbol": address,
            "base_symbol": "ABC",
            "quote_symbol": "USDC",
            "price_usd": 1,
            "final_score": 80,
            "created_at": self.text(ENTRY),
        })

    def insert_position(
        self,
        pair_id,
        status="CLOSED",
        result_percent=None,
        exit_reason="TIME_EXIT",
        exit_price=1.2,
        entry_price=1,
        change_24h=4,
        final_score=80,
        signal_type="CONFIRMED",
    ):
        created = self.text(ENTRY)
        return self.insert_row("paper_positions", {
            "pair_id": pair_id,
            "strategy_version": "BASELINE_V04",
            "signal_type": signal_type,
            "final_score": final_score,
            "change_24h": change_24h,
            "status": status,
            "entry_price": entry_price,
            "entry_time": created,
            "last_price": exit_price,
            "exit_price": exit_price,
            "exit_time": self.text(ENTRY + timedelta(hours=2)),
            "exit_reason": exit_reason,
            "result_percent": result_percent,
            "created_at": created,
            "updated_at": created,
        })

    def insert_allocation(self, portfolio_id, position_id, pair_id, **overrides):
        created = self.text(ENTRY)
        row = {
            "portfolio_id": portfolio_id,
            "position_id": position_id,
            "pair_id": pair_id,
            "strategy_version": "BASELINE_V04",
            "status": "CLOSED",
            "skip_reason": None,
            "recommendation": "BASELINE_BUY",
            "decision": "AUTO_PAPER_BUY",
            "decision_time": created,
            "recommended_percent": 1,
            "recommended_usd": 100,
            "allocated_percent": 1,
            "allocated_usd": 100,
            "quantity": 100,
            "entry_price": 1,
            "entry_time": created,
            "exit_price": 1.2,
            "exit_time": self.text(ENTRY + timedelta(hours=2)),
            "exit_reason": "TIME_EXIT",
            "result_percent": 0,
            "signal_type": "CONFIRMED",
            "final_score": 80,
            "cohort": "PRIMARY",
            "last_price": 1,
            "market_value_usd": 0,
            "unrealized_pnl_usd": 0,
            "realized_pnl_usd": 0,
            "created_at": created,
            "updated_at": created,
        }
        row.update(overrides)
        return self.insert_row("paper_allocations", row)

    def insert_ledger(
        self,
        portfolio_id,
        allocation_id,
        event_type,
        amount,
        cash_after,
        created_at,
    ):
        return self.insert_row("paper_cash_ledger", {
            "portfolio_id": portfolio_id,
            "allocation_id": allocation_id,
            "event_type": event_type,
            "amount_usd": amount,
            "cash_after_usd": cash_after,
            "created_at": self.text(created_at),
        })

    def insert_request(self, position_id, pair_id, **overrides):
        created = self.text(ENTRY)
        row = {
            "position_id": position_id,
            "pair_id": pair_id,
            "strategy_version": "BASELINE_V04",
            "final_score": 80,
            "signal_type": "CONFIRMED",
            "cohort": "PRIMARY",
            "change_24h": 4,
            "recommended_percent": 1,
            "recommended_usd": 100,
            "reference_price": 1,
            "signal_created_at": created,
            "position_created_at": created,
            "created_at": created,
            "status": "PENDING",
            "portfolio_id": HUMAN_PORTFOLIO_ID,
            "decision_at": None,
            "execution_at": None,
            "execution_price": None,
            "allocation_id": None,
            "delay_signal_to_decision_seconds": None,
        }
        row.update(overrides)
        return self.insert_row("approval_requests", row)

    def seed_control_book(self):
        self.insert_account(
            CONTROL_PORTFOLIO_ID,
            cash=9750,
            initial=10000,
            peak=10200,
            max_drawdown=5,
        )
        pair_a = self.insert_pair("control-a")
        pair_b = self.insert_pair("control-b")
        pair_c = self.insert_pair("control-c")
        pair_d = self.insert_pair("control-d")
        position_a = self.insert_position(pair_a, result_percent=20, final_score=86)
        position_b = self.insert_position(
            pair_b,
            result_percent=-10,
            exit_reason="STOP_LOSS",
            final_score=74,
            signal_type="WEAK",
        )
        position_c = self.insert_position(pair_c, status="OPEN", result_percent=None)
        position_d = self.insert_position(pair_d, status="OPEN", result_percent=None)
        winner = self.insert_allocation(
            CONTROL_PORTFOLIO_ID,
            position_a,
            pair_a,
            result_percent=20,
            realized_pnl_usd=20,
            exit_price=1.2,
            final_score=86,
            cohort="PRIMARY",
            signal_type="CONFIRMED",
            exit_reason="TIME_EXIT",
            allocated_usd=100,
        )
        loser = self.insert_allocation(
            CONTROL_PORTFOLIO_ID,
            position_b,
            pair_b,
            result_percent=-10,
            realized_pnl_usd=-10,
            exit_price=0.9,
            final_score=74,
            cohort="CONTROL",
            signal_type="WEAK",
            exit_reason="STOP_LOSS",
            allocated_usd=100,
        )
        opened = self.insert_allocation(
            CONTROL_PORTFOLIO_ID,
            position_c,
            pair_c,
            status="OPEN",
            result_percent=None,
            realized_pnl_usd=None,
            exit_price=None,
            exit_time=None,
            exit_reason=None,
            allocated_usd=150,
            market_value_usd=180,
            unrealized_pnl_usd=30,
            last_price=1.2,
            entry_price=1,
        )
        self.insert_allocation(
            CONTROL_PORTFOLIO_ID,
            position_d,
            pair_d,
            status="SKIPPED",
            skip_reason="INSUFFICIENT_CASH",
            decision="SKIPPED",
            result_percent=None,
            realized_pnl_usd=None,
            allocated_usd=None,
            quantity=None,
            exit_price=None,
            exit_time=None,
            exit_reason=None,
            market_value_usd=None,
            unrealized_pnl_usd=None,
        )
        buy_a = ENTRY
        buy_b = ENTRY + timedelta(hours=1)
        sell_a = ENTRY + timedelta(hours=2)
        sell_b = ENTRY + timedelta(hours=3)
        self.insert_ledger(1, winner, "BUY", -100, 9900, buy_a)
        self.insert_ledger(1, winner, "SELL", 120, 10020, sell_a)
        self.insert_ledger(1, loser, "BUY", -100, 9920, buy_b)
        self.insert_ledger(1, loser, "SELL", 90, 10010, sell_b)
        self.insert_ledger(1, opened, "BUY", -150, 9750, buy_b)
        return {
            "winner": winner,
            "loser": loser,
            "opened": opened,
        }

    def seed_human_book(self):
        self.insert_account(
            HUMAN_PORTFOLIO_ID,
            cash=4800,
            initial=5000,
            peak=5200,
            max_drawdown=4,
        )
        pair_closed = self.insert_pair("human-closed")
        pair_open = self.insert_pair("human-open")
        pair_skip = self.insert_pair("human-skip")
        position_closed = self.insert_position(pair_closed, result_percent=-10)
        position_open = self.insert_position(pair_open, status="OPEN", result_percent=None)
        position_skip = self.insert_position(pair_skip, status="OPEN", result_percent=None)
        closed = self.insert_allocation(
            HUMAN_PORTFOLIO_ID,
            position_closed,
            pair_closed,
            result_percent=-10,
            realized_pnl_usd=-20,
            allocated_usd=200,
            exit_reason="STOP_LOSS",
            final_score=74,
            cohort="CONTROL",
        )
        opened = self.insert_allocation(
            HUMAN_PORTFOLIO_ID,
            position_open,
            pair_open,
            status="OPEN",
            result_percent=100,
            realized_pnl_usd=None,
            exit_price=None,
            exit_time=None,
            exit_reason=None,
            allocated_usd=250,
            market_value_usd=300,
            unrealized_pnl_usd=50,
        )
        self.insert_allocation(
            HUMAN_PORTFOLIO_ID,
            position_skip,
            pair_skip,
            status="SKIPPED",
            skip_reason="INSUFFICIENT_CASH",
            decision="SKIPPED",
            result_percent=None,
            realized_pnl_usd=None,
            allocated_usd=None,
            quantity=None,
            exit_price=None,
            exit_time=None,
            exit_reason=None,
        )
        self.insert_ledger(2, closed, "BUY", -200, 4800, ENTRY)
        self.insert_ledger(2, closed, "SELL", 180, 4980, ENTRY + timedelta(hours=2))
        self.insert_ledger(2, opened, "BUY", -250, 4800, ENTRY + timedelta(hours=1))
        return closed

    def add_decision(
        self,
        status,
        baseline_result=None,
        baseline_status="CLOSED",
        baseline_exit_reason="TIME_EXIT",
        baseline_entry=1,
        control=None,
        human=None,
        recommended_usd=100,
        reference_price=1,
        execution_price=None,
        signal_at=None,
        decision_at=None,
        final_score=80,
        signal_type="CONFIRMED",
        cohort="PRIMARY",
        change_24h=4,
        allocation_link="auto",
        portfolio_id=HUMAN_PORTFOLIO_ID,
        with_baseline=True,
        store_delay=None,
    ):
        """
        control/human — словари полей allocation или None.
        allocation_link:
        - auto: проставить allocation_id найденной human allocation
        - none: оставить allocation_id пустым
        - wrong: указать allocation другой позиции
        """
        self.sequence += 1
        pair_id = self.insert_pair("signal-{0}".format(self.sequence))
        position_id = None
        if with_baseline:
            position_id = self.insert_position(
                pair_id,
                status=baseline_status,
                result_percent=baseline_result,
                exit_reason=baseline_exit_reason,
                entry_price=baseline_entry,
                change_24h=change_24h,
                final_score=final_score,
                signal_type=signal_type,
                exit_price=None if baseline_status != "CLOSED" else 1,
            )
        else:
            position_id = 100000 + self.sequence
        control_id = None
        if control is not None:
            payload = {
                "status": "CLOSED",
                "result_percent": baseline_result,
                "realized_pnl_usd": baseline_result,
                "entry_price": 1,
                "exit_price": 1,
                "exit_reason": baseline_exit_reason,
            }
            payload.update(control)
            control_id = self.insert_allocation(
                CONTROL_PORTFOLIO_ID,
                position_id,
                pair_id,
                **payload,
            )
        human_id = None
        if human is not None:
            payload = {
                "status": "CLOSED",
                "result_percent": 0,
                "realized_pnl_usd": 0,
                "entry_price": 1,
                "exit_price": 1,
                "exit_reason": "TIME_EXIT",
                "recommended_usd": recommended_usd,
                "decision": "HUMAN_BUY",
                "recommendation": "HUMAN_APPROVAL",
            }
            payload.update(human)
            human_id = self.insert_allocation(
                HUMAN_PORTFOLIO_ID,
                position_id,
                pair_id,
                **payload,
            )
        if signal_at is None:
            signal_at = ENTRY
        if decision_at is None:
            decision_at = ENTRY + timedelta(seconds=60)
        execution_at = decision_at if status == "BUY" else None
        if execution_price is None and status == "BUY":
            execution_price = reference_price
        request_allocation_id = None
        if allocation_link == "auto":
            request_allocation_id = human_id
        elif allocation_link == "wrong":
            other_pair = self.insert_pair("wrong-{0}".format(self.sequence))
            other_position = self.insert_position(other_pair, result_percent=1)
            request_allocation_id = self.insert_allocation(
                HUMAN_PORTFOLIO_ID,
                other_position,
                other_pair,
                result_percent=99,
                realized_pnl_usd=99,
            )
        created = self.text(signal_at)
        decision_text = None if status in ("PENDING", "EXPIRED") else self.text(decision_at)
        execution_text = None
        if status == "BUY" and decision_text is not None:
            execution_text = self.text(decision_at)
        request_id = self.insert_request(
            position_id,
            pair_id,
            status=status,
            portfolio_id=portfolio_id,
            final_score=final_score,
            signal_type=signal_type,
            cohort=cohort,
            change_24h=change_24h,
            recommended_usd=recommended_usd,
            recommended_percent=1,
            reference_price=reference_price,
            execution_price=execution_price if status == "BUY" else None,
            signal_created_at=created,
            created_at=created,
            decision_at=decision_text,
            execution_at=execution_text,
            allocation_id=request_allocation_id,
            delay_signal_to_decision_seconds=store_delay,
        )
        return {
            "request_id": request_id,
            "position_id": position_id,
            "pair_id": pair_id,
            "human_id": human_id,
            "control_id": control_id,
        }

    def test_portfolio_1_metrics(self):
        ids = self.seed_control_book()
        self.seed_human_book()
        control = self.report()["portfolios"]["control"]

        self.assertTrue(control["available"])
        self.assertEqual(control["portfolio_id"], 1)
        self.assertEqual(control["initial_deposit_usd"], 10000)
        self.assertEqual(control["current_cash_usd"], 9750)
        self.assertAlmostEqual(control["current_nav_usd"], 9930)
        self.assertAlmostEqual(control["total_pnl_usd"], -70)
        self.assertAlmostEqual(control["total_return_percent"], -0.7)
        self.assertEqual(control["peak_equity_usd"], 10200)
        self.assertAlmostEqual(control["max_drawdown_percent"], 5)
        self.assertEqual(control["allocations_total"], 4)
        self.assertEqual(control["open_count"], 1)
        self.assertEqual(control["closed_count"], 2)
        self.assertEqual(control["skipped_count"], 1)
        self.assertAlmostEqual(control["realized_pnl_usd"], 10)
        self.assertAlmostEqual(control["unrealized_pnl_usd"], 30)
        self.assertAlmostEqual(control["win_rate_percent"], 50)
        self.assertAlmostEqual(control["average_trade_pnl_percent"], 5)
        self.assertAlmostEqual(control["median_trade_pnl_percent"], 5)
        self.assertAlmostEqual(control["best_trade"]["result_percent"], 20)
        self.assertEqual(control["best_trade"]["id"], ids["winner"])
        self.assertAlmostEqual(control["worst_trade"]["result_percent"], -10)
        self.assertAlmostEqual(control["average_winner_percent"], 20)
        self.assertAlmostEqual(control["average_loser_percent"], -10)
        self.assertAlmostEqual(control["profit_factor"], 2)
        self.assertEqual(control["profit_factor_status"], "ok")
        self.assertEqual(control["max_concurrent_open"], 3)
        self.assertAlmostEqual(control["max_capital_invested_usd"], 350)
        self.assertAlmostEqual(control["max_exposure_percent"], 3.5)

    def test_portfolio_2_metrics(self):
        self.seed_control_book()
        self.seed_human_book()
        report = self.report()
        human = report["portfolios"]["human"]
        control = report["portfolios"]["control"]

        self.assertTrue(human["available"])
        self.assertEqual(human["portfolio_id"], 2)
        self.assertEqual(human["initial_deposit_usd"], 5000)
        self.assertEqual(human["current_cash_usd"], 4800)
        self.assertAlmostEqual(human["current_nav_usd"], 5100)
        self.assertAlmostEqual(human["total_pnl_usd"], 100)
        self.assertAlmostEqual(human["total_return_percent"], 2)
        self.assertEqual(human["allocations_total"], 3)
        self.assertEqual(human["open_count"], 1)
        self.assertEqual(human["closed_count"], 1)
        self.assertEqual(human["skipped_count"], 1)
        self.assertAlmostEqual(human["realized_pnl_usd"], -20)
        self.assertAlmostEqual(human["unrealized_pnl_usd"], 50)
        self.assertAlmostEqual(human["win_rate_percent"], 0)
        self.assertAlmostEqual(human["closed_result_count"], 1)
        self.assertNotAlmostEqual(human["current_nav_usd"], control["current_nav_usd"])
        self.assertAlmostEqual(control["current_nav_usd"], 9930)

    def test_human_buy_uses_human_allocation(self):
        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        self.add_decision(
            "BUY",
            baseline_result=90,
            control={"result_percent": 40, "realized_pnl_usd": 40, "allocated_usd": 100},
            human={"result_percent": 12, "realized_pnl_usd": 12, "allocated_usd": 100},
        )
        report = self.report()
        buy = report["buy_quality"]
        self.assertEqual(buy["closed_buy_outcomes"], 1)
        self.assertAlmostEqual(buy["average_return_percent"], 12)
        self.assertAlmostEqual(buy["realized_pnl_usd"], 12)
        self.assertAlmostEqual(report["portfolios"]["human"]["realized_pnl_usd"], 12)
        self.assertAlmostEqual(report["portfolios"]["control"]["realized_pnl_usd"], 40)

    def test_human_buy_ignores_baseline_result_percent(self):
        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        self.add_decision(
            "BUY",
            baseline_result=80,
            control={"result_percent": 80, "realized_pnl_usd": 80},
            human={"result_percent": -15, "realized_pnl_usd": -15},
        )
        buy = self.report()["buy_quality"]
        self.assertAlmostEqual(buy["average_return_percent"], -15)
        self.assertAlmostEqual(buy["realized_pnl_usd"], -15)
        self.assertNotAlmostEqual(buy["average_return_percent"], 80)

    def test_skip_counterfactual_uses_matched_baseline(self):
        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        self.add_decision(
            "SKIP",
            baseline_result=-12,
            baseline_exit_reason="STOP_LOSS",
            control={"result_percent": -12, "realized_pnl_usd": -12},
            human={"result_percent": 77, "realized_pnl_usd": 77, "status": "CLOSED"},
        )
        self.add_decision(
            "SKIP",
            baseline_result=5,
            control={"result_percent": 5, "realized_pnl_usd": 5},
        )
        skip = self.report()["skip_quality"]
        self.assertEqual(skip["completed_counterfactual"], 2)
        self.assertTrue(skip["counterfactual"])
        self.assertEqual(skip["losing_skipped"], 1)
        self.assertEqual(skip["profitable_skipped"], 1)
        self.assertAlmostEqual(skip["saved_loss_percent"], 12)
        self.assertAlmostEqual(skip["missed_profit_percent"], 5)
        self.assertNotAlmostEqual(skip["saved_loss_percent"], 77)

    def test_skip_unfinished_baseline_is_na(self):
        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        self.add_decision(
            "SKIP",
            baseline_status="OPEN",
            baseline_result=15,
            control={"status": "OPEN", "result_percent": None, "realized_pnl_usd": None, "exit_price": None},
        )
        report = self.report()
        skip = report["skip_quality"]
        self.assertEqual(skip["total_skip"], 1)
        self.assertEqual(skip["completed_counterfactual"], 0)
        self.assertEqual(skip["pending_outcome"], 1)
        self.assertIsNone(skip["saved_loss_usd"])
        self.assertIsNone(skip["missed_profit_usd"])
        self.assertIsNone(skip["net_skip_value_usd"])
        self.assertIsNone(report["decision_value"]["raw"]["value"])
        self.assertEqual(report["data_quality"]["skip_without_completed_baseline"], 1)
        text = self.rendered(report)
        self.assertIn("PENDING_OUTCOME", text)
        self.assertIn("[counterfactual, not a human trade]", text)

    def test_saved_loss(self):
        self._seed_value_case()
        skip = self.report()["skip_quality"]
        self.assertAlmostEqual(skip["saved_loss_usd"], 10)
        self.assertAlmostEqual(skip["saved_loss_percent"], 10)

    def test_missed_profit(self):
        self._seed_value_case()
        skip = self.report()["skip_quality"]
        self.assertAlmostEqual(skip["missed_profit_usd"], 30)
        self.assertAlmostEqual(skip["missed_profit_percent"], 30)

    def test_net_skip_value(self):
        self._seed_value_case()
        value = self.report()["decision_value"]
        self.assertAlmostEqual(value["net_skip_value_usd"], -20)
        self.assertAlmostEqual(value["net_skip_value_percent"], -20)
        self.assertAlmostEqual(value["saved_loss_usd"] - value["missed_profit_usd"], -20)

    def test_profitable_buy(self):
        self._seed_value_case()
        buy = self.report()["buy_quality"]
        self.assertEqual(buy["profitable_buy"], 1)
        self.assertAlmostEqual(buy["win_rate_percent"], 50)

    def test_losing_buy(self):
        self._seed_value_case()
        buy = self.report()["buy_quality"]
        self.assertEqual(buy["losing_buy"], 1)
        self.assertEqual(buy["closed_buy_outcomes"], 2)
        self.assertAlmostEqual(buy["realized_pnl_usd"], -10)

    def test_open_excluded_from_win_rate(self):
        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        self.add_decision(
            "BUY",
            baseline_result=1,
            human={
                "status": "OPEN",
                "result_percent": 100,
                "realized_pnl_usd": None,
                "exit_price": None,
                "exit_reason": None,
                "market_value_usd": 150,
                "unrealized_pnl_usd": 50,
            },
            control={"status": "OPEN", "result_percent": None, "realized_pnl_usd": None, "exit_price": None},
        )
        only_open = self.report()
        self.assertIsNone(only_open["buy_quality"]["win_rate_percent"])
        self.assertEqual(only_open["buy_quality"]["open_buy"], 1)
        self.assertEqual(only_open["buy_quality"]["closed_buy_outcomes"], 0)
        self.assertIsNone(only_open["portfolios"]["human"]["win_rate_percent"])

        self.add_decision(
            "BUY",
            baseline_result=-10,
            human={"result_percent": -10, "realized_pnl_usd": -10},
            control={"result_percent": -10, "realized_pnl_usd": -10},
        )
        mixed = self.report()
        self.assertEqual(mixed["buy_quality"]["closed_buy_outcomes"], 1)
        self.assertAlmostEqual(mixed["buy_quality"]["win_rate_percent"], 0)
        self.assertEqual(mixed["portfolios"]["human"]["closed_result_count"], 1)
        self.assertAlmostEqual(mixed["portfolios"]["human"]["win_rate_percent"], 0)

    def test_profit_factor(self):
        self.seed_control_book()
        factor = profit_factor_metrics([])
        self.assertIsNone(factor["value"])
        control = self.report()["portfolios"]["control"]
        self.assertAlmostEqual(control["profit_factor"], 2)
        self.assertAlmostEqual(control["gross_profit_usd"], 20)
        self.assertAlmostEqual(control["gross_loss_usd"], 10)

    def test_profit_factor_zero_loss(self):
        self.insert_account(1, 10000)
        pair_a = self.insert_pair("pf-a")
        pair_b = self.insert_pair("pf-b")
        position_a = self.insert_position(pair_a, result_percent=10)
        position_b = self.insert_position(pair_b, result_percent=40)
        self.insert_allocation(
            1, position_a, pair_a, result_percent=10, realized_pnl_usd=10,
        )
        self.insert_allocation(
            1, position_b, pair_b, result_percent=40, realized_pnl_usd=40,
        )
        direct = profit_factor_metrics([
            {"status": "CLOSED", "realized_pnl_usd": 10},
            {"status": "CLOSED", "realized_pnl_usd": 40},
        ])
        self.assertIsNone(direct["value"])
        self.assertNotEqual(direct["value"], float("inf"))
        self.assertEqual(direct["status"], "no_gross_loss")
        control = self.report()["portfolios"]["control"]
        self.assertIsNone(control["profit_factor"])
        self.assertEqual(control["profit_factor_status"], "no_gross_loss")
        rendered = self.rendered()
        self.assertIn("N/A (no losing trades)", rendered)
        self.assertNotRegex(rendered.lower(), r"(?<![a-z])inf(?![a-z])")

    def test_median(self):
        self.assertEqual(median_or_none([1, 2, 3, 4]), 2.5)
        self.assertEqual(median_or_none([10, -20, 5, 1]), 3)
        self.assertIsNone(median_or_none([]))
        self.insert_account(1, 10000)
        for result in (10, -20, 5, 1):
            pair_id = self.insert_pair("med-{0}".format(result))
            position_id = self.insert_position(pair_id, result_percent=result)
            self.insert_allocation(
                1,
                position_id,
                pair_id,
                result_percent=result,
                realized_pnl_usd=result,
            )
        control = self.report()["portfolios"]["control"]
        self.assertAlmostEqual(control["median_trade_pnl_percent"], 3)
        self.assertAlmostEqual(control["average_trade_pnl_percent"], -1)

    def test_execution_delay(self):
        self.insert_account(2, 10000)
        self.add_decision(
            "BUY",
            baseline_result=1,
            human={"result_percent": 1, "realized_pnl_usd": 1},
            decision_at=ENTRY + timedelta(seconds=30),
            signal_at=ENTRY,
        )
        self.add_decision(
            "BUY",
            baseline_result=1,
            human={"result_percent": 1, "realized_pnl_usd": 1},
            decision_at=ENTRY + timedelta(seconds=90),
            signal_at=ENTRY,
        )
        delay = self.report()["execution"]["delay"]
        self.assertEqual(delay["count"], 2)
        self.assertAlmostEqual(delay["average"], 60)
        self.assertAlmostEqual(delay["median"], 60)
        self.assertAlmostEqual(delay["min"], 30)
        self.assertAlmostEqual(delay["max"], 90)
        measured = decision_delay_seconds({
            "signal_created_at": self.text(ENTRY),
            "execution_at": self.text(ENTRY + timedelta(seconds=90)),
            "decision_at": self.text(ENTRY + timedelta(seconds=90)),
        })
        self.assertAlmostEqual(measured, 90)

    def test_execution_slippage(self):
        self.insert_account(2, 10000)
        self.add_decision(
            "BUY",
            baseline_result=1,
            human={"result_percent": 1, "realized_pnl_usd": 1, "entry_price": 1.02},
            reference_price=1,
            execution_price=1.02,
        )
        self.add_decision(
            "BUY",
            baseline_result=1,
            human={"result_percent": 1, "realized_pnl_usd": 1, "entry_price": 1.9},
            reference_price=2,
            execution_price=1.9,
        )
        slip = self.report()["execution"]["slippage"]
        self.assertAlmostEqual(slippage_percent(1, 1.02), 2)
        self.assertAlmostEqual(slippage_percent(2, 1.9), -5)
        self.assertIsNone(slippage_percent(0, 1))
        self.assertIsNone(slippage_percent(None, 1))
        self.assertEqual(slip["count"], 2)
        self.assertAlmostEqual(slip["average"], -1.5)
        self.assertAlmostEqual(slip["median"], -1.5)
        self.assertAlmostEqual(slip["best"], -5)
        self.assertAlmostEqual(slip["worst"], 2)

    def test_matched_signal_comparison(self):
        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        matched = self.add_decision(
            "BUY",
            baseline_result=20,
            baseline_entry=0.5,
            control={
                "result_percent": 20,
                "realized_pnl_usd": 20,
                "entry_price": 1.0,
                "exit_reason": "TIME_EXIT",
            },
            human={
                "result_percent": 10,
                "realized_pnl_usd": 10,
                "entry_price": 1.02,
                "exit_reason": "TRAILING_STOP",
            },
            decision_at=ENTRY + timedelta(seconds=90),
            reference_price=1,
            execution_price=1.02,
        )
        skipped = self.add_decision(
            "SKIP",
            baseline_result=-10,
            baseline_exit_reason="STOP_LOSS",
            control={"result_percent": -10, "realized_pnl_usd": -10, "entry_price": 1},
            decision_at=ENTRY + timedelta(seconds=15),
        )
        block = self.report()["matched"]
        self.assertEqual(block["buy_count"], 1)
        self.assertEqual(block["skip_count"], 1)
        buy = block["buys"][0]
        self.assertEqual(buy["request_id"], matched["request_id"])
        self.assertAlmostEqual(buy["control_entry_price"], 1.0)
        self.assertAlmostEqual(buy["human_execution_price"], 1.02)
        self.assertNotAlmostEqual(buy["control_entry_price"], 0.5)
        self.assertAlmostEqual(buy["control_result_percent"], 20)
        self.assertAlmostEqual(buy["human_result_percent"], 10)
        self.assertAlmostEqual(buy["return_diff_percent"], -10)
        self.assertAlmostEqual(buy["usd_diff"], -10)
        self.assertAlmostEqual(buy["delay_seconds"], 90)
        skip = block["skips"][0]
        self.assertEqual(skip["request_id"], skipped["request_id"])
        self.assertTrue(skip["counterfactual"])
        self.assertIsNone(skip["human_result_percent"])
        self.assertAlmostEqual(skip["counterfactual_result_percent"], -10)
        self.assertEqual(skip["counterfactual_exit_reason"], "STOP_LOSS")

    def test_unmatched_signal_excluded(self):
        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        linked = self.add_decision(
            "BUY",
            baseline_result=5,
            control={"result_percent": 5, "realized_pnl_usd": 5},
            human={"result_percent": 4, "realized_pnl_usd": 4},
        )
        loose = self.add_decision(
            "BUY",
            baseline_result=9,
            control=None,
            human={"result_percent": 9, "realized_pnl_usd": 9},
        )
        block = self.report()["matched"]
        matched_ids = {row["request_id"] for row in block["buys"]}
        self.assertIn(linked["request_id"], matched_ids)
        self.assertNotIn(loose["request_id"], matched_ids)
        self.assertGreaterEqual(
            self.report()["data_quality"]["unmatched_control_human_signals"],
            1,
        )

    def test_score_breakdown(self):
        self._seed_breakdown()
        rows = {
            row["key"]: row
            for row in self.report()["breakdown"]["by_score_band"]
        }
        self.assertEqual(rows["70-79"]["decisions"], 3)
        self.assertEqual(rows["70-79"]["completed"], 3)
        self.assertAlmostEqual(rows["70-79"]["average_return_percent"], -16 / 3)
        self.assertEqual(rows[">=80"]["decisions"], 2)
        self.assertAlmostEqual(rows[">=80"]["average_return_percent"], 9)
        self.assertEqual(score_band(70), "70-79")
        self.assertEqual(score_band(79.9), "70-79")
        self.assertEqual(score_band(80), ">=80")
        self.assertEqual(score_band(69), "<70")
        scores = {
            row["key"]: row["decisions"]
            for row in self.report()["breakdown"]["by_final_score"]
        }
        self.assertEqual(scores[74], 1)
        self.assertEqual(scores[86], 1)

    def test_cohort_breakdown(self):
        self._seed_breakdown()
        rows = {
            row["key"]: row
            for row in self.report()["breakdown"]["by_cohort"]
        }
        self.assertEqual(rows["CONTROL"]["decisions"], 3)
        self.assertEqual(rows["PRIMARY"]["decisions"], 2)

    def test_change_24h_buckets(self):
        self.assertEqual(change_24h_bucket(-0.01), "<0")
        self.assertEqual(change_24h_bucket(0), "0..<3")
        self.assertEqual(change_24h_bucket(2.999), "0..<3")
        self.assertEqual(change_24h_bucket(3), "3..<6")
        self.assertEqual(change_24h_bucket(5.999), "3..<6")
        self.assertEqual(change_24h_bucket(6), "6..<10")
        self.assertEqual(change_24h_bucket(9.999), "6..<10")
        self.assertEqual(change_24h_bucket(10), ">=10")
        self.assertIsNone(change_24h_bucket(None))
        self._seed_breakdown()
        rows = {
            row["key"]: row["decisions"]
            for row in self.report()["breakdown"]["by_change_24h"]
        }
        self.assertEqual(rows["<0"], 1)
        self.assertEqual(rows["0..<3"], 1)
        self.assertEqual(rows["3..<6"], 1)
        self.assertEqual(rows["6..<10"], 1)
        self.assertEqual(rows[">=10"], 1)

    def test_exit_reason_breakdown(self):
        self._seed_breakdown()
        rows = {
            row["key"]: row["completed"]
            for row in self.report()["breakdown"]["by_exit_reason"]
        }
        self.assertEqual(rows["STOP_LOSS"], 1)
        self.assertEqual(rows["TRAILING_STOP"], 1)
        self.assertEqual(rows["TIME_EXIT"], 2)
        self.assertEqual(rows["OTHER_EXIT"], 1)

    def test_data_quality_counters(self):
        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        self._seed_quality_issues()
        quality = self.report()["data_quality"]
        self.assertEqual(quality["approval_without_baseline"], 1)
        self.assertEqual(quality["buy_without_human_allocation"], 1)
        self.assertEqual(quality["allocation_without_approval_request"], 1)
        self.assertEqual(quality["duplicate_logical_links"], 1)
        self.assertEqual(quality["skip_without_completed_baseline"], 1)
        self.assertEqual(quality["closed_allocation_without_exit_price"], 2)
        self.assertEqual(quality["missing_reference_price"], 1)
        self.assertEqual(quality["missing_execution_price"], 1)
        self.assertEqual(quality["missing_timestamps"], 1)
        self.assertEqual(quality["unmatched_control_human_signals"], 7)
        self.assertEqual(quality["baseline_control_result_mismatch"], 1)

    def test_sample_size_labels(self):
        self.assertEqual(sample_label(0), SAMPLE_INSUFFICIENT)
        self.assertEqual(sample_label(19), SAMPLE_INSUFFICIENT)
        self.assertEqual(sample_label(20), SAMPLE_EARLY)
        self.assertEqual(sample_label(29), SAMPLE_EARLY)
        self.assertEqual(sample_label(30), SAMPLE_FORWARD)
        self.assertEqual(sample_label(99), SAMPLE_FORWARD)
        self.assertEqual(sample_label(100), SAMPLE_LARGER)
        self.insert_account(2, 10000)
        empty = self.report()["sample"]
        self.assertEqual(empty["label"], SAMPLE_INSUFFICIENT)
        self.assertEqual(empty["completed_decisions"], 0)
        for index in range(20):
            self.add_decision(
                "SKIP",
                baseline_result=-1,
                recommended_usd=100,
                change_24h=index,
            )
        sample = self.report()["sample"]
        self.assertEqual(sample["completed_skip_outcomes"], 20)
        self.assertEqual(sample["completed_buy_outcomes"], 0)
        self.assertEqual(sample["eligible_decisions"], 20)
        self.assertEqual(sample["label"], SAMPLE_EARLY)
        self.assertIn("не статистическая значимость", sample["note"])

    def test_readonly_sqlite_connection(self):
        self.insert_account(1, 10000)
        uri = readonly_uri(self.db_path)
        self.assertIn("mode=ro", uri)
        connection = connect_readonly_db(self.db_path)
        try:
            self.assertTrue(query_only_enabled(connection))
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "UPDATE paper_account SET cash_usd = 0 WHERE id = 1"
                )
        finally:
            connection.close()

    def test_report_does_not_change_db(self):
        self._seed_value_case()
        before = self.dump()
        exit_code = main(["--db", self.db_path])
        after = self.dump()
        self.assertEqual(exit_code, 0)
        self.assertEqual(before, after)
        self.report()
        self.assertEqual(self.dump(), before)

    def test_no_network_or_api_call(self):
        analytics = (ROOT / "human_vs_control.py").read_text(encoding="utf-8")
        report_source = (ROOT / "decision_report.py").read_text(encoding="utf-8")
        combined = analytics + "\n" + report_source
        for forbidden in (
            "run_cycle",
            "sync_portfolio",
            "decide_buy",
            "decide_skip",
            "import requests",
            "urllib",
            "dexscreener",
            "import scanner",
            "approval_bot",
            "human_approval",
            "paper_portfolio",
            "paper_engine",
            "socket",
        ):
            self.assertNotIn(forbidden, combined)

        def fail_network(*_args, **_kwargs):
            raise AssertionError("network call")

        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        with patch("socket.socket", fail_network), patch(
            "socket.create_connection",
            fail_network,
        ):
            report = build_report(self.db_path)
            self.assertFalse(report["source"]["network"])
            self.assertTrue(report["source"]["read_only"])
            main(["--db", self.db_path])

    def test_empty_production_state(self):
        missing = build_report(os.path.join(self.temp_dir.name, "missing.db"))
        self.assertEqual(missing["sample"]["label"], SAMPLE_INSUFFICIENT)
        self.assertIsNone(missing["portfolios"]["human"]["win_rate_percent"])
        self.assertIsNone(missing["buy_quality"]["win_rate_percent"])
        self.assertIsNone(missing["decision_value"]["raw"]["value"])
        self.assertIsNone(missing["decision_value"]["normalized"]["value"])

        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        report = self.report()
        self.assertEqual(report["sample"]["label"], SAMPLE_INSUFFICIENT)
        self.assertEqual(report["sample"]["eligible_decisions"], 0)
        self.assertEqual(report["sample"]["completed_buy_outcomes"], 0)
        self.assertEqual(report["sample"]["completed_skip_outcomes"], 0)
        self.assertIsNone(report["portfolios"]["control"]["win_rate_percent"])
        self.assertIsNone(report["portfolios"]["human"]["win_rate_percent"])
        self.assertIsNone(report["portfolios"]["human"]["realized_pnl_usd"])
        self.assertEqual(report["portfolios"]["human"]["open_count"], 0)
        self.assertEqual(report["decision_counts"]["BUY"], 0)
        text = self.rendered(report)
        self.assertIn("1. HUMAN vs CONTROL — SUMMARY", text)
        self.assertIn("11. SAMPLE SIZE / CONFIDENCE WARNING", text)
        self.assertIn("INSUFFICIENT DATA", text)
        self.assertIn("Win rate:              N/A", text)
        self.assertNotIn("Win rate:              0.00%", text)
        self.assertNotRegex(text.lower(), r"(?<![a-z])inf(?![a-z])")
        self.assertIn("не делает вывод", text)

    def test_only_portfolios_1_and_2(self):
        self.seed_control_book()
        self.seed_human_book()
        self.insert_account(3, cash=1, initial=1, peak=999999)
        pair_id = self.insert_pair("foreign-book")
        position_id = self.insert_position(pair_id, result_percent=500)
        self.insert_allocation(
            3,
            position_id,
            pair_id,
            result_percent=500,
            realized_pnl_usd=99999,
            allocated_usd=1,
            market_value_usd=0,
        )
        report = self.report()
        self.assertEqual(set(report["portfolios"]), {"control", "human"})
        self.assertAlmostEqual(report["portfolios"]["control"]["current_nav_usd"], 9930)
        self.assertAlmostEqual(report["portfolios"]["human"]["current_nav_usd"], 5100)
        self.assertAlmostEqual(report["portfolios"]["control"]["realized_pnl_usd"], 10)
        self.assertNotIn(99999, (
            report["portfolios"]["control"]["realized_pnl_usd"],
            report["portfolios"]["human"]["realized_pnl_usd"],
        ))

    def test_decision_value_contributions(self):
        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        nominal = 100
        worse = self.add_decision(
            "BUY",
            baseline_result=50,
            control={
                "result_percent": 50,
                "realized_pnl_usd": 999,
                "entry_price": 1,
            },
            human={
                "result_percent": 10,
                "realized_pnl_usd": 1,
                "entry_price": 1.02,
            },
            recommended_usd=nominal,
        )
        better = self.add_decision(
            "BUY",
            baseline_result=10,
            control={"result_percent": 10, "realized_pnl_usd": 10, "entry_price": 1},
            human={"result_percent": 20, "realized_pnl_usd": 20, "entry_price": 1},
            recommended_usd=nominal,
        )
        saved = self.add_decision(
            "SKIP",
            baseline_result=-10,
            control={"result_percent": -10, "realized_pnl_usd": -10},
            recommended_usd=nominal,
        )
        missed = self.add_decision(
            "SKIP",
            baseline_result=30,
            control={"result_percent": 30, "realized_pnl_usd": 30},
            recommended_usd=nominal,
        )
        loose = self.add_decision(
            "BUY",
            baseline_result=80,
            control=None,
            human={"result_percent": 99, "realized_pnl_usd": 99},
            recommended_usd=nominal,
        )
        value = self.report()["decision_value"]
        by_request = {
            item["request_id"]: item
            for item in value["raw"]["contributions"]
        }

        self.assertAlmostEqual(by_request[worse["request_id"]]["contribution_percent"], -40)
        self.assertAlmostEqual(by_request[better["request_id"]]["contribution_percent"], 10)
        self.assertAlmostEqual(by_request[saved["request_id"]]["contribution_percent"], 10)
        self.assertAlmostEqual(by_request[missed["request_id"]]["contribution_percent"], -30)
        self.assertAlmostEqual(value["raw"]["value"], -50)
        self.assertNotIn(loose["request_id"], by_request)
        self.assertIn(loose["request_id"], value["excluded_unmatched_buy_ids"])
        self.assertEqual(value["excluded_unmatched_buy_count"], 1)

        for item in value["raw"]["contributions"]:
            self.assertEqual(item["nominal_usd"], nominal)
            self.assertAlmostEqual(item["human_nominal_usd"], item["auto_nominal_usd"] + item["contribution_usd"])
            self.assertAlmostEqual(
                item["auto_nominal_usd"],
                nominal * item["auto_result_percent"] / 100.0,
            )
            self.assertAlmostEqual(
                item["contribution_usd"],
                nominal * item["contribution_percent"] / 100.0,
            )
        worse_row = by_request[worse["request_id"]]
        self.assertAlmostEqual(worse_row["human_nominal_usd"], 10)
        self.assertAlmostEqual(worse_row["auto_nominal_usd"], 50)
        self.assertAlmostEqual(worse_row["contribution_usd"], -40)
        self.assertAlmostEqual(value["normalized"]["value"], -50)
        self.assertEqual(value["normalized"]["status"], "ok")

        self.add_decision(
            "BUY",
            baseline_result=5,
            control={"result_percent": 5, "realized_pnl_usd": 5},
            human={"result_percent": 5, "realized_pnl_usd": 5},
            recommended_usd=None,
        )
        without_nominal = self.report()["decision_value"]
        self.assertEqual(without_nominal["normalized"]["status"], "na")
        self.assertIsNone(without_nominal["normalized"]["value"])
        self.assertAlmostEqual(without_nominal["raw"]["value"], -50)

    def test_raw_and_normalized_decision_value(self):
        self._seed_value_case()
        value = self.report()["decision_value"]
        self.assertAlmostEqual(value["raw"]["value"], -60)
        self.assertEqual(value["raw"]["status"], "ok")
        self.assertEqual(value["raw"]["unit"], "percent_points")
        self.assertEqual(value["excluded_unmatched_buy_count"], 1)
        self.assertAlmostEqual(value["raw"]["buy_contribution_percent"], -40)
        self.assertAlmostEqual(value["raw"]["skip_contribution_percent"], -20)
        self.assertAlmostEqual(value["normalized"]["value"], -60)
        self.assertEqual(value["normalized"]["status"], "ok")
        self.assertAlmostEqual(value["normalized"]["buy_contribution_usd"], -40)
        self.assertAlmostEqual(value["normalized"]["skip_contribution_usd"], -20)

        self.add_decision(
            "SKIP",
            baseline_result=-10,
            recommended_usd=None,
        )
        partial = self.report()["decision_value"]
        self.assertIsNone(partial["saved_loss_usd"])
        self.assertIsNotNone(partial["raw"]["value"])
        self.assertEqual(partial["normalized"]["status"], "ok")

    def test_delay_buckets(self):
        self._seed_breakdown()
        rows = {
            row["key"]: row["decisions"]
            for row in self.report()["breakdown"]["by_delay"]
        }
        self.assertEqual(rows["<1m"], 2)
        self.assertEqual(rows["1m..<15m"], 2)
        self.assertEqual(rows["15m..<1h"], 1)

    def test_report_sections_mark_counterfactual(self):
        self._seed_value_case()
        text = self.rendered()
        for title in (
            "1. HUMAN vs CONTROL — SUMMARY",
            "2. PORTFOLIO COMPARISON",
            "3. HUMAN DECISIONS",
            "4. BUY QUALITY",
            "5. SKIP QUALITY",
            "6. DECISION VALUE",
            "7. EXECUTION DELAY / SLIPPAGE",
            "8. BREAKDOWN",
            "9. MATCHED SIGNALS",
            "10. DATA QUALITY",
            "11. SAMPLE SIZE / CONFIDENCE WARNING",
        ):
            self.assertIn(title, text)
        self.assertIn("counterfactual", text)
        self.assertIn("not a human trade", text)
        self.assertIn("baseline.result_percent здесь не используется", text)

    def _seed_value_case(self):
        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        self.add_decision(
            "BUY",
            baseline_result=50,
            control={"result_percent": 50, "realized_pnl_usd": 50, "entry_price": 1},
            human={"result_percent": 10, "realized_pnl_usd": 10, "entry_price": 1.02},
            recommended_usd=100,
            reference_price=1,
            execution_price=1.02,
            decision_at=ENTRY + timedelta(seconds=30),
            final_score=86,
            cohort="PRIMARY",
            signal_type="OVERHEAT",
            change_24h=12,
        )
        self.add_decision(
            "BUY",
            baseline_result=-20,
            control=None,
            human={
                "result_percent": -20,
                "realized_pnl_usd": -20,
                "exit_reason": "STOP_LOSS",
            },
            recommended_usd=100,
            reference_price=2,
            execution_price=1.9,
            decision_at=ENTRY + timedelta(seconds=90),
            final_score=74,
            cohort="CONTROL",
            signal_type="WEAK",
            change_24h=1,
        )
        self.add_decision(
            "SKIP",
            baseline_result=-10,
            baseline_exit_reason="STOP_LOSS",
            control={"result_percent": -10, "realized_pnl_usd": -10},
            recommended_usd=100,
            decision_at=ENTRY + timedelta(seconds=20),
        )
        self.add_decision(
            "SKIP",
            baseline_result=30,
            baseline_exit_reason="TIME_EXIT",
            control={"result_percent": 30, "realized_pnl_usd": 30},
            recommended_usd=100,
            decision_at=ENTRY + timedelta(minutes=20),
        )
        self.add_decision(
            "SKIP",
            baseline_status="OPEN",
            baseline_result=None,
            control={"status": "OPEN", "result_percent": None, "realized_pnl_usd": None, "exit_price": None},
            recommended_usd=100,
        )
        self.add_decision("EXPIRED", baseline_status="CLOSED", baseline_result=7)
        self.add_decision("PENDING", baseline_status="OPEN", baseline_result=None)

    def _seed_breakdown(self):
        self.insert_account(1, 10000)
        self.insert_account(2, 10000)
        specs = (
            ("BUY", 74, "CONTROL", "WEAK", 1.0, -15, "STOP_LOSS", 30),
            ("BUY", 86, "PRIMARY", "OVERHEAT", 12.0, 10, "TRAILING_STOP", 90),
            ("SKIP", 72, "CONTROL", "CONFIRMED", 4.0, -5, "TIME_EXIT", 1200),
            ("SKIP", 90, "PRIMARY", "WATCH", 8.0, 8, "TIME_EXIT", 60),
            ("BUY", 75, "CONTROL", "NEUTRAL", -1.0, 4, "OTHER_EXIT", 10),
        )
        for status, score, cohort, signal, change, result, reason, delay in specs:
            human = None
            control = {
                "result_percent": result,
                "realized_pnl_usd": result,
                "exit_reason": reason,
            }
            if status == "BUY":
                human = {
                    "result_percent": result,
                    "realized_pnl_usd": result,
                    "exit_reason": reason,
                    "final_score": score,
                    "cohort": cohort,
                    "signal_type": signal,
                }
            self.add_decision(
                status,
                baseline_result=result,
                baseline_exit_reason=reason,
                baseline_status="CLOSED",
                control=control,
                human=human,
                final_score=score,
                cohort=cohort,
                signal_type=signal,
                change_24h=change,
                decision_at=ENTRY + timedelta(seconds=delay),
                recommended_usd=100,
            )

    def _seed_quality_issues(self):
        signal_at = ENTRY
        decision_at = ENTRY + timedelta(seconds=30)

        self.add_decision(
            "SKIP",
            with_baseline=False,
            signal_at=signal_at,
            decision_at=decision_at,
        )

        buy_without = self.add_decision(
            "BUY",
            baseline_result=1,
            human=None,
            control={
                "result_percent": 1,
                "realized_pnl_usd": 1,
                "exit_price": None,
            },
            allocation_link="none",
            reference_price=1,
            execution_price=1,
            signal_at=signal_at,
            decision_at=decision_at,
        )
        self.assertIsNotNone(buy_without["control_id"])

        pair_orphan = self.insert_pair("orphan-human")
        position_orphan = self.insert_position(pair_orphan, result_percent=1)
        self.insert_allocation(
            HUMAN_PORTFOLIO_ID,
            position_orphan,
            pair_orphan,
            status="CLOSED",
            exit_price=None,
            result_percent=1,
            realized_pnl_usd=1,
        )
        self.insert_allocation(
            CONTROL_PORTFOLIO_ID,
            position_orphan,
            pair_orphan,
            status="OPEN",
            result_percent=None,
            realized_pnl_usd=None,
            exit_price=None,
            exit_reason=None,
        )

        self.add_decision(
            "BUY",
            baseline_result=2,
            human={"result_percent": 2, "realized_pnl_usd": 2},
            allocation_link="wrong",
            reference_price=1,
            execution_price=1,
            signal_at=signal_at,
            decision_at=decision_at,
        )

        self.add_decision(
            "SKIP",
            baseline_status="OPEN",
            baseline_result=None,
            control=None,
            signal_at=signal_at,
            decision_at=decision_at,
        )

        self.add_decision(
            "BUY",
            baseline_result=5,
            control={"result_percent": 5, "realized_pnl_usd": 5, "entry_price": 1},
            human={"result_percent": 5, "realized_pnl_usd": 5, "entry_price": 1},
            reference_price=None,
            execution_price=None,
            signal_at=None,
            decision_at=None,
            store_delay=None,
        )
        request_id = self.query(
            "SELECT id FROM approval_requests ORDER BY id DESC LIMIT 1"
        )[0]["id"]
        self.execute(
            """
            UPDATE approval_requests
            SET
                signal_created_at = NULL,
                decision_at = NULL,
                execution_at = NULL,
                reference_price = NULL,
                execution_price = NULL,
                delay_signal_to_decision_seconds = NULL
            WHERE id = ?
            """,
            (request_id,),
        )

        self.add_decision(
            "BUY",
            baseline_result=9,
            control=None,
            human={"result_percent": 9, "realized_pnl_usd": 9},
            reference_price=1,
            execution_price=1.1,
            signal_at=signal_at,
            decision_at=decision_at,
        )

        self.add_decision(
            "SKIP",
            baseline_result=-5,
            control={"result_percent": 50, "realized_pnl_usd": 50},
            signal_at=signal_at,
            decision_at=decision_at,
        )


if __name__ == "__main__":
    unittest.main()
