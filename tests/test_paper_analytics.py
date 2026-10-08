import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

import database
from database import close_open_baseline, create_tables, ensure_paper_tables, open_baseline_position
from paper_analytics import (
    REASON_MISSING_ACCOUNT,
    REASON_MISSING_FILE,
    REASON_MISSING_TABLES,
    build_report,
    connect_readonly,
    load_portfolio,
)
from paper_engine import STRATEGY_VERSION, format_datetime, observation_bucket
from paper_portfolio import ensure_portfolio, ensure_portfolio_tables, sync_portfolio
from portfolio_report import main, render_report


ENTRY = datetime(2026, 10, 1, 12, 0, 0)


class PaperAnalyticsTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "analytics.db")
        self.original_db_name = database.DB_NAME
        database.DB_NAME = self.db_path
        create_tables()
        ensure_paper_tables(self.db_path)
        ensure_portfolio_tables(self.db_path)

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
        return build_report(load_portfolio(self.db_path))

    def rendered(self, report=None):
        if report is None:
            report = self.report()
        output = StringIO()
        with patch("sys.stdout", output):
            render_report(report, self.db_path)
        return output.getvalue()

    def insert_pair(self, address, symbol="ABC/USDC", final_score=80):
        return self.execute(
            """
            INSERT INTO pairs (
                chain_id, dex_id, pair_address, pair_symbol,
                base_symbol, quote_symbol, price_usd, final_score, created_at
            ) VALUES ('solana', 'raydium', ?, ?, 'ABC', 'USDC', 1, ?, ?)
            """,
            (address, symbol, final_score, self.text(ENTRY)),
        )

    def insert_account(
        self,
        cash=10000,
        initial=10000,
        peak=10000,
        max_drawdown=0,
        activated_at=ENTRY,
    ):
        activated = self.text(activated_at)
        return self.execute(
            """
            INSERT INTO paper_account (
                id, currency, initial_deposit_usd, cash_usd, activated_at,
                peak_equity_usd, max_drawdown_percent, created_at, updated_at
            ) VALUES (1, 'USD', ?, ?, ?, ?, ?, ?, ?)
            """,
            (initial, cash, activated, peak, max_drawdown, activated, activated),
        )

    def insert_position(
        self,
        pair_id,
        created_at,
        status="OPEN",
        strategy=STRATEGY_VERSION,
    ):
        created = self.text(created_at)
        return self.execute(
            """
            INSERT INTO paper_positions (
                pair_id, strategy_version, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (pair_id, strategy, status, created, created),
        )

    def insert_allocation(self, position_id, pair_id, **overrides):
        exit_time = self.text(ENTRY + timedelta(hours=1))
        row = {
            "portfolio_id": 1,
            "position_id": position_id,
            "pair_id": pair_id,
            "strategy_version": STRATEGY_VERSION,
            "status": "CLOSED",
            "skip_reason": None,
            "recommendation": "BASELINE_BUY",
            "decision": "AUTO_PAPER_BUY",
            "decision_time": self.text(ENTRY),
            "recommended_percent": 1,
            "recommended_usd": 100,
            "allocated_percent": 1,
            "allocated_usd": 100,
            "quantity": 100,
            "entry_price": 1,
            "entry_time": self.text(ENTRY),
            "exit_price": 1.2,
            "exit_time": exit_time,
            "exit_reason": "TIME_EXIT",
            "result_percent": 20,
            "signal_type": "CONFIRMED",
            "final_score": 86,
            "cohort": "PRIMARY",
            "last_price": 1.2,
            "market_value_usd": 0,
            "unrealized_pnl_usd": 0,
            "realized_pnl_usd": 20,
            "created_at": self.text(ENTRY),
            "updated_at": self.text(ENTRY),
        }
        row.update(overrides)
        columns = list(row.keys())
        self.execute(
            "INSERT INTO paper_allocations ({columns}) VALUES ({marks})".format(
                columns=", ".join(columns),
                marks=", ".join("?" for _ in columns),
            ),
            [row[column] for column in columns],
        )
        return self.query(
            """
            SELECT id FROM paper_allocations
            WHERE position_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (position_id,),
        )[0]["id"]

    def insert_ledger(
        self,
        event_type,
        amount,
        cash_after,
        created_at,
        allocation_id=None,
    ):
        return self.execute(
            """
            INSERT INTO paper_cash_ledger (
                portfolio_id, allocation_id, event_type,
                amount_usd, cash_after_usd, created_at
            ) VALUES (1, ?, ?, ?, ?, ?)
            """,
            (
                allocation_id,
                event_type,
                amount,
                cash_after,
                self.text(created_at),
            ),
        )

    def insert_snapshot(
        self,
        observed_at,
        equity,
        cash=10000,
        market=0,
        realized=0,
        unrealized=0,
        drawdown=0,
    ):
        return self.execute(
            """
            INSERT INTO paper_nav_snapshots (
                portfolio_id, observed_at, observation_bucket, cash_usd,
                open_market_value_usd, total_equity_usd, realized_pnl_usd,
                unrealized_pnl_usd, drawdown_percent
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.text(observed_at),
                observation_bucket(observed_at),
                cash,
                market,
                equity,
                realized,
                unrealized,
                drawdown,
            ),
        )

    def open_position(self, pair_id, created_at, entry_price=1, final_score=80):
        created = open_baseline_position(
            pair_id=pair_id,
            strategy_version=STRATEGY_VERSION,
            signal_type="CONFIRMED",
            final_score=final_score,
            change_24h=4,
            entry_price=entry_price,
            entry_time=self.text(created_at),
            observation_bucket=observation_bucket(created_at),
            stop_loss_percent=15,
            trailing_start_percent=10,
            trailing_distance_percent=5,
            max_hold_hours=168,
            created_at=self.text(created_at),
            db_path=self.db_path,
        )
        self.assertTrue(created)
        return self.query(
            "SELECT * FROM paper_positions WHERE pair_id = ?",
            (pair_id,),
        )[0]

    def close_baseline(self, position, exit_price, exit_time, result_percent):
        exit_text = self.text(exit_time)
        closed = close_open_baseline(
            position_id=position["id"],
            last_price=exit_price,
            last_checked_at=exit_text,
            max_price=max(position["entry_price"], exit_price),
            max_profit_percent=result_percent if result_percent > 0 else 0,
            drawdown_from_max_percent=0,
            exit_price=exit_price,
            exit_time=exit_text,
            exit_reason="TIME_EXIT",
            result_percent=result_percent,
            updated_at=exit_text,
            db_path=self.db_path,
        )
        self.assertTrue(closed)

    def set_last_price(self, position_id, price):
        self.execute(
            "UPDATE paper_positions SET last_price = ? WHERE id = ?",
            (price, position_id),
        )

    def test_missing_file_account_and_tables_do_not_crash(self):
        missing = os.path.join(self.temp_dir.name, "absent.db")
        missing_report = build_report(load_portfolio(missing))
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(missing_report["source"]["reason"], REASON_MISSING_FILE)
        self.assertIsNone(missing_report["account"]["current_nav_usd"])
        self.assertIn("Файл базы не найден.", self.rendered(missing_report))
        self.assertIn("1. ACCOUNT", self.rendered(missing_report))

        empty_path = os.path.join(self.temp_dir.name, "empty.db")
        sqlite3.connect(empty_path).close()
        output = StringIO()
        with patch("sys.stdout", output):
            code = main(["--db", empty_path])
        text = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Таблицы портфеля не найдены.", text)
        self.assertIn("7. DATA QUALITY", text)
        empty_report = build_report(load_portfolio(empty_path))
        self.assertEqual(empty_report["source"]["reason"], REASON_MISSING_TABLES)

        bare = self.report()
        self.assertEqual(bare["source"]["reason"], REASON_MISSING_ACCOUNT)
        self.assertIn("Счёт портфеля не найден.", self.rendered(bare))
        self.assertNotIn("Post-activation baseline without allocation:", self.rendered(bare))

    def test_deposit_only_account_is_flat(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        report = self.report()
        account = report["account"]

        self.assertEqual(account["initial_capital_usd"], 10000)
        self.assertEqual(account["current_cash_usd"], 10000)
        self.assertEqual(account["current_nav_usd"], 10000)
        self.assertEqual(account["total_pnl_usd"], 0)
        self.assertEqual(account["total_return_percent"], 0)
        self.assertEqual(account["peak_equity_usd"], 10000)
        self.assertEqual(account["max_drawdown_percent"], 0)
        self.assertEqual(account["current_drawdown_percent"], 0)
        self.assertEqual(report["positions"]["open_count"], 0)
        self.assertEqual(report["closed_trades"]["closed_count"], 0)
        self.assertIsNone(report["closed_trades"]["win_rate_percent"])
        self.assertIsNone(report["closed_trades"]["average_result_percent"])
        self.assertIsNone(report["closed_trades"]["median_result_percent"])
        self.assertIsNone(report["closed_trades"]["profit_factor"])
        self.assertFalse(report["data_quality"]["nav_identity_mismatch"])
        self.assertFalse(report["data_quality"]["ledger_cash_mismatch"])

    def test_open_position_nav_includes_market_value(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-open")
        position = self.open_position(pair_id, ENTRY, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.set_last_price(position["id"], 1.25)
        sync_portfolio(ENTRY + timedelta(minutes=15), db_path=self.db_path)
        report = self.report()

        self.assertEqual(report["account"]["current_cash_usd"], 9900)
        self.assertEqual(report["positions"]["open_count"], 1)
        self.assertEqual(report["positions"]["open_value_usd"], 125)
        self.assertEqual(report["positions"]["unrealized_pnl_usd"], 25)
        self.assertEqual(report["positions"]["realized_pnl_usd"], 0)
        self.assertEqual(report["account"]["current_nav_usd"], 10025)
        self.assertEqual(report["account"]["total_pnl_usd"], 25)
        self.assertFalse(report["data_quality"]["nav_identity_mismatch"])

    def test_single_winner_has_no_profit_factor(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-win", "ABC/USDC")
        position = self.open_position(pair_id, ENTRY, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        exit_time = ENTRY + timedelta(minutes=15)
        self.close_baseline(position, 1.5, exit_time, 50)
        sync_portfolio(exit_time, db_path=self.db_path)
        report = self.report()
        closed = report["closed_trades"]

        self.assertEqual(report["account"]["current_cash_usd"], 10050)
        self.assertEqual(report["account"]["current_nav_usd"], 10050)
        self.assertEqual(report["positions"]["realized_pnl_usd"], 50)
        self.assertEqual(report["positions"]["closed_count"], 1)
        self.assertEqual(closed["winners"], 1)
        self.assertEqual(closed["losers"], 0)
        self.assertEqual(closed["win_rate_percent"], 100)
        self.assertEqual(closed["average_result_percent"], 50)
        self.assertEqual(closed["median_result_percent"], 50)
        self.assertEqual(closed["best"]["pair"], "ABC/USDC")
        self.assertEqual(closed["best"]["result_percent"], 50)
        self.assertEqual(closed["worst"]["result_percent"], 50)
        self.assertIsNone(closed["profit_factor"])

    def test_result_percent_drives_win_rate_even_when_other_fields_are_missing(self):
        self.insert_account(cash=10000)
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY)
        rich_pair = self.insert_pair("pair-rich", "RICH/USDC")
        poor_pair = self.insert_pair("pair-poor", "POOR/USDC")
        blank_pair = self.insert_pair("pair-blank", "BLANK/USDC")
        self.insert_position(rich_pair, ENTRY, status="CLOSED")
        self.insert_position(poor_pair, ENTRY, status="CLOSED")
        self.insert_position(blank_pair, ENTRY, status="CLOSED")
        self.insert_allocation(
            1,
            rich_pair,
            result_percent=10,
            realized_pnl_usd=None,
            quantity=None,
            entry_price=None,
            exit_price=None,
            entry_time=None,
            exit_time=None,
            exit_reason=None,
            final_score=90,
            signal_type="CONFIRMED",
            cohort="PRIMARY",
        )
        self.insert_allocation(
            2,
            poor_pair,
            result_percent=-10,
            realized_pnl_usd=-10,
            final_score=70,
            signal_type="WEAK",
            cohort="CONTROL",
            exit_reason="STOP_LOSS",
        )
        self.insert_allocation(
            3,
            blank_pair,
            result_percent=None,
            realized_pnl_usd=4,
            final_score=80,
        )
        report = self.report()
        closed = report["closed_trades"]

        self.assertEqual(closed["closed_count"], 3)
        self.assertEqual(closed["result_count"], 2)
        self.assertEqual(closed["winners"], 1)
        self.assertEqual(closed["losers"], 1)
        self.assertEqual(closed["win_rate_percent"], 50)
        self.assertEqual(closed["average_result_percent"], 0)
        self.assertEqual(closed["median_result_percent"], 0)
        self.assertEqual(closed["best"]["pair"], "RICH/USDC")
        self.assertEqual(closed["best"]["result_percent"], 10)
        self.assertEqual(closed["worst"]["result_percent"], -10)
        self.assertEqual(closed["profit_factor"], 0)
        self.assertEqual(report["positions"]["realized_pnl_usd"], -6)
        self.assertGreaterEqual(report["data_quality"]["incomplete_count"], 1)
        self.assertIn("entry_time", report["data_quality"]["incomplete_fields"])
        self.assertIn("result_percent", report["data_quality"]["incomplete_fields"])
        self.assertEqual(
            [row["key"] for row in report["breakdown"]["final_score"]],
            [90, 70],
        )

    def test_median_win_rate_and_profit_factor(self):
        self.insert_account()
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY)
        specs = [
            (20, 20, 86, "CONFIRMED", "PRIMARY", "TRAILING_STOP", "AAA/USDC"),
            (5, 5.05, 80, "NEUTRAL", "PRIMARY", "TIME_EXIT", "BBB/USDC"),
            (-10, -10, 72, "WATCH", "CONTROL", "STOP_LOSS", "CCC/USDC"),
            (0, 0, None, None, None, None, "DDD/USDC"),
        ]
        for index, spec in enumerate(specs, start=1):
            result, realized, score, signal, cohort, reason, symbol = spec
            pair_id = self.insert_pair("pair-{0}".format(index), symbol, final_score=score or 0)
            self.insert_position(pair_id, ENTRY, status="CLOSED")
            self.insert_allocation(
                index,
                pair_id,
                result_percent=result,
                realized_pnl_usd=realized,
                final_score=score,
                signal_type=signal,
                cohort=cohort,
                exit_reason=reason,
                exit_time=self.text(ENTRY + timedelta(hours=index)),
            )
        report = self.report()
        closed = report["closed_trades"]

        self.assertEqual(closed["result_count"], 4)
        self.assertEqual(closed["winners"], 2)
        self.assertEqual(closed["losers"], 1)
        self.assertEqual(closed["flats"], 1)
        self.assertEqual(closed["win_rate_percent"], 50)
        self.assertEqual(closed["average_result_percent"], 3.75)
        self.assertEqual(closed["median_result_percent"], 2.5)
        self.assertAlmostEqual(closed["average_winner_percent"], 12.5)
        self.assertEqual(closed["average_loser_percent"], -10)
        self.assertAlmostEqual(closed["profit_factor"], 2.505)
        self.assertEqual(closed["best"]["pair"], "AAA/USDC")
        self.assertEqual(closed["worst"]["pair"], "CCC/USDC")

        self.assertEqual(
            [row["key"] for row in report["breakdown"]["final_score"]],
            [86, 80, 72, None],
        )
        self.assertEqual(
            [row["key"] for row in report["breakdown"]["signal_type"]],
            ["NEUTRAL", "CONFIRMED", "WATCH", None],
        )
        self.assertEqual(
            [row["key"] for row in report["breakdown"]["cohort"]],
            ["PRIMARY", "CONTROL", None],
        )
        self.assertEqual(
            [row["key"] for row in report["breakdown"]["exit_reason"]],
            ["STOP_LOSS", "TRAILING_STOP", "TIME_EXIT", None],
        )
        primary = report["breakdown"]["cohort"][0]
        self.assertEqual(primary["key"], "PRIMARY")
        self.assertEqual(primary["n"], 2)
        self.assertEqual(primary["win_rate_percent"], 100)
        self.assertEqual(primary["average_result_percent"], 12.5)
        self.assertAlmostEqual(primary["realized_pnl_usd"], 25.05)

        output = StringIO()
        with patch("sys.stdout", output):
            code = main(["--db", self.db_path])
        self.assertEqual(code, 0)
        self._assert_breakdown_text(output.getvalue())

    def test_odd_median_includes_flat_trade(self):
        self.insert_account()
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY)
        results = [10, -10, 0]
        realized = [10, -5, 0]
        for index, result in enumerate(results, start=1):
            pair_id = self.insert_pair("odd-{0}".format(index))
            self.insert_position(pair_id, ENTRY, status="CLOSED")
            self.insert_allocation(
                index,
                pair_id,
                result_percent=result,
                realized_pnl_usd=realized[index - 1],
                final_score=80,
            )
        closed = self.report()["closed_trades"]
        self.assertAlmostEqual(closed["win_rate_percent"], 100 / 3)
        self.assertEqual(closed["median_result_percent"], 0)
        self.assertEqual(closed["average_result_percent"], 0)
        self.assertEqual(closed["profit_factor"], 2)

    def test_best_trade_tie_uses_smaller_allocation_id(self):
        self.insert_account()
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY)
        for index, result in enumerate((5, 5, 1), start=1):
            pair_id = 90 + index
            self.insert_position(pair_id, ENTRY, status="CLOSED")
            self.insert_allocation(
                index,
                pair_id,
                result_percent=result,
                realized_pnl_usd=result,
                final_score=80,
            )
        best = self.report()["closed_trades"]["best"]
        worst = self.report()["closed_trades"]["worst"]
        self.assertEqual(best["id"], 1)
        self.assertEqual(best["result_percent"], 5)
        self.assertEqual(best["pair"], "pair #91")
        self.assertEqual(worst["id"], 3)
        self.assertEqual(worst["result_percent"], 1)

    def test_skipped_does_not_change_nav_or_win_rate(self):
        self.insert_account()
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY)
        pair_id = self.insert_pair("pair-skip", "SKIP/USDC")
        self.insert_position(pair_id, ENTRY, status="OPEN")
        self.insert_allocation(
            1,
            pair_id,
            status="SKIPPED",
            skip_reason="INSUFFICIENT_CASH",
            decision="SKIPPED",
            allocated_percent=0,
            allocated_usd=0,
            quantity=None,
            exit_price=None,
            exit_time=None,
            exit_reason=None,
            result_percent=None,
            last_price=None,
            market_value_usd=0,
            unrealized_pnl_usd=0,
            realized_pnl_usd=0,
        )
        report = self.report()

        self.assertEqual(report["positions"]["skipped_count"], 1)
        self.assertEqual(report["positions"]["open_count"], 0)
        self.assertEqual(report["positions"]["closed_count"], 0)
        self.assertEqual(report["account"]["current_nav_usd"], 10000)
        self.assertEqual(report["account"]["total_pnl_usd"], 0)
        self.assertEqual(report["closed_trades"]["closed_count"], 0)
        self.assertIsNone(report["closed_trades"]["win_rate_percent"])
        self.assertEqual(report["data_quality"]["skipped_count"], 1)
        self.assertEqual(
            report["data_quality"]["skip_reasons"],
            {"INSUFFICIENT_CASH": 1},
        )
        self.assertEqual(report["data_quality"]["incomplete_count"], 0)
        self.assertIn("INSUFFICIENT_CASH: 1", self.rendered(report))

    def test_overlapping_positions_and_same_timestamp_exit_first(self):
        self.insert_account()
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY)
        first = self.insert_position(1, ENTRY, status="CLOSED")
        second = self.insert_position(2, ENTRY, status="CLOSED")
        first_allocation = self.insert_allocation(
            first,
            1,
            allocated_usd=100,
            result_percent=1,
            realized_pnl_usd=1,
        )
        second_allocation = self.insert_allocation(
            second,
            2,
            allocated_usd=101,
            result_percent=1,
            realized_pnl_usd=1,
        )
        self.insert_ledger("BUY", -100, 9900, ENTRY, first_allocation)
        self.insert_ledger(
            "SELL", 101, 10001, ENTRY + timedelta(hours=2), first_allocation,
        )
        self.insert_ledger(
            "BUY", -101, 9900, ENTRY + timedelta(hours=1), second_allocation,
        )
        self.insert_ledger(
            "SELL", 102, 10002, ENTRY + timedelta(hours=3), second_allocation,
        )
        overlap = self.report()["capital_risk"]
        self.assertEqual(overlap["max_concurrent_positions"], 2)
        self.assertEqual(overlap["max_capital_in_positions_usd"], 201)
        self.assertAlmostEqual(overlap["max_exposure_percent"], 2.01)

        touched = os.path.join(self.temp_dir.name, "touch.db")
        self._seed_touching_intervals(touched)
        touching = build_report(load_portfolio(touched))["capital_risk"]
        self.assertEqual(touching["max_concurrent_positions"], 1)
        self.assertEqual(touching["max_capital_in_positions_usd"], 100)

    def test_current_nav_can_set_max_exposure(self):
        self.insert_account(cash=500, initial=10000, peak=10000)
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY - timedelta(hours=2))
        self.insert_ledger("BUY", -500, 9500, ENTRY, None)
        pair_id = self.insert_pair("pair-exposure", "EXP/USDC")
        position_id = self.insert_position(pair_id, ENTRY, status="OPEN")
        allocation_id = self.insert_allocation(
            position_id,
            pair_id,
            status="OPEN",
            allocated_usd=500,
            quantity=500,
            entry_price=1,
            exit_price=None,
            exit_time=None,
            exit_reason=None,
            result_percent=None,
            market_value_usd=500,
            unrealized_pnl_usd=0,
            realized_pnl_usd=0,
            last_price=1,
        )
        self.execute(
            "UPDATE paper_cash_ledger SET allocation_id = ? WHERE event_type = 'BUY'",
            (allocation_id,),
        )
        self.insert_snapshot(ENTRY - timedelta(hours=1), equity=100000, cash=100000)
        report = self.report()
        self.assertEqual(report["account"]["current_nav_usd"], 1000)
        self.assertAlmostEqual(report["capital_risk"]["max_exposure_percent"], 50)

    def test_live_nav_above_stored_peak_is_not_written_back(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-peak")
        position = self.open_position(pair_id, ENTRY, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.set_last_price(position["id"], 2)
        sync_portfolio(ENTRY + timedelta(minutes=1), db_path=self.db_path)
        before = self.query("SELECT * FROM paper_account")[0]
        report = self.report()
        after = self.query("SELECT * FROM paper_account")[0]

        self.assertEqual(before, after)
        self.assertEqual(before["peak_equity_usd"], 10000)
        self.assertEqual(before["max_drawdown_percent"], 0)
        self.assertEqual(report["account"]["current_nav_usd"], 10100)
        self.assertEqual(report["account"]["peak_equity_usd"], 10100)
        self.assertEqual(report["account"]["current_drawdown_percent"], 0)
        self.assertEqual(report["account"]["max_drawdown_percent"], 0)
        self.assertEqual(
            report["capital_risk"]["current_drawdown_percent"],
            report["account"]["current_drawdown_percent"],
        )

    def test_intra_bucket_drop_raises_reported_drawdown_only(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-drop")
        position = self.open_position(pair_id, ENTRY, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.set_last_price(position["id"], 2)
        sync_portfolio(ENTRY + timedelta(minutes=1), db_path=self.db_path)
        self.set_last_price(position["id"], 0.5)
        sync_portfolio(ENTRY + timedelta(minutes=15), db_path=self.db_path)
        self.set_last_price(position["id"], 2)
        sync_portfolio(ENTRY + timedelta(minutes=30), db_path=self.db_path)
        self.set_last_price(position["id"], 1.95)
        sync_portfolio(ENTRY + timedelta(minutes=45), db_path=self.db_path)
        self.set_last_price(position["id"], 0.5)
        sync_portfolio(ENTRY + timedelta(minutes=46), db_path=self.db_path)

        before = self.query("SELECT * FROM paper_account")[0]
        snapshots_before = self.query("SELECT id FROM paper_nav_snapshots")
        report = self.report()
        after = self.query("SELECT * FROM paper_account")[0]
        snapshots_after = self.query("SELECT id FROM paper_nav_snapshots")
        expected = (10100 - 9950) / 10100 * 100

        self.assertEqual(before, after)
        self.assertEqual(snapshots_before, snapshots_after)
        self.assertEqual(before["peak_equity_usd"], 10100)
        self.assertEqual(before["max_drawdown_percent"], 0.5)
        self.assertEqual(report["account"]["current_nav_usd"], 9950)
        self.assertEqual(report["account"]["peak_equity_usd"], 10100)
        self.assertAlmostEqual(report["account"]["current_drawdown_percent"], expected)
        self.assertAlmostEqual(report["account"]["max_drawdown_percent"], expected)
        self.assertAlmostEqual(
            report["capital_risk"]["max_drawdown_percent"],
            expected,
        )

    def test_open_allocation_remains_unrealized_if_baseline_is_closed(self):
        self.insert_account(cash=9900)
        pair_id = self.insert_pair("pair-lag", "LAG/USDC")
        position_id = self.insert_position(pair_id, ENTRY, status="CLOSED")
        allocation_id = self.insert_allocation(
            position_id,
            pair_id,
            status="OPEN",
            exit_price=None,
            exit_time=None,
            exit_reason=None,
            result_percent=None,
            market_value_usd=125,
            unrealized_pnl_usd=25,
            realized_pnl_usd=0,
            last_price=1.25,
        )
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY)
        self.insert_ledger("BUY", -100, 9900, ENTRY, allocation_id)
        report = self.report()

        self.assertEqual(report["positions"]["open_count"], 1)
        self.assertEqual(report["positions"]["closed_count"], 0)
        self.assertEqual(report["positions"]["unrealized_pnl_usd"], 25)
        self.assertEqual(report["positions"]["realized_pnl_usd"], 0)
        self.assertEqual(report["account"]["current_nav_usd"], 10025)
        self.assertEqual(report["data_quality"]["open_allocation_baseline_closed"], 1)
        self.assertEqual(report["closed_trades"]["closed_count"], 0)
        self.assertFalse(report["data_quality"]["nav_identity_mismatch"])

    def test_recent_trades_keep_latest_ten_and_null_exit_last(self):
        self.insert_account()
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY)
        for index in range(1, 12):
            self.insert_position(index, ENTRY, status="CLOSED")
            self.insert_allocation(
                index,
                index,
                result_percent=1,
                realized_pnl_usd=1,
                exit_time=self.text(ENTRY + timedelta(hours=index)),
            )
        self.insert_position(12, ENTRY, status="CLOSED")
        self.insert_allocation(
            12,
            12,
            result_percent=1,
            realized_pnl_usd=1,
            exit_time=None,
        )
        ids = [trade["id"] for trade in self.report()["recent_trades"]]
        self.assertEqual(ids, [11, 10, 9, 8, 7, 6, 5, 4, 3, 2])

    def test_duration_prefers_ledger_and_falls_back_to_allocation_times(self):
        self.insert_account()
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY)
        ledger_pair = self.insert_pair("pair-ledger", "LED/USDC")
        fallback_pair = self.insert_pair("pair-fallback", "OLD/USDC")
        negative_pair = self.insert_pair("pair-negative", "NEG/USDC")
        self.insert_position(ledger_pair, ENTRY, status="CLOSED")
        self.insert_position(fallback_pair, ENTRY, status="CLOSED")
        self.insert_position(negative_pair, ENTRY, status="CLOSED")
        ledger_id = self.insert_allocation(
            1,
            ledger_pair,
            entry_time=self.text(ENTRY),
            exit_time=self.text(ENTRY + timedelta(hours=10)),
            result_percent=3,
            realized_pnl_usd=3,
        )
        self.insert_ledger("BUY", -100, 9900, ENTRY + timedelta(hours=1), ledger_id)
        self.insert_ledger(
            "BUY", -1, 9899, ENTRY + timedelta(hours=2), ledger_id,
        )
        self.insert_ledger(
            "SELL", 103, 10002, ENTRY + timedelta(hours=4), ledger_id,
        )
        self.insert_allocation(
            2,
            fallback_pair,
            entry_time=self.text(ENTRY),
            exit_time=self.text(ENTRY + timedelta(hours=10)),
            result_percent=4,
            realized_pnl_usd=4,
        )
        self.insert_allocation(
            3,
            negative_pair,
            entry_time=self.text(ENTRY),
            exit_time=self.text(ENTRY - timedelta(hours=1)),
            result_percent=8,
            realized_pnl_usd=8,
        )
        report = self.report()
        by_id = {trade["id"]: trade for trade in report["recent_trades"]}

        self.assertEqual(by_id[ledger_id]["duration_seconds"], 3 * 3600)
        self.assertEqual(by_id[2]["duration_seconds"], 10 * 3600)
        self.assertIsNone(by_id[3]["duration_seconds"])
        self.assertTrue(by_id[3]["duration_negative"])
        self.assertEqual(report["data_quality"]["duplicate_ledger_count"], 1)
        self.assertEqual(report["data_quality"]["negative_duration_count"], 1)
        self.assertEqual(report["closed_trades"]["winners"], 3)
        self.assertIn("3h 0m", self.rendered(report))
        self.assertIn("10h 0m", self.rendered(report))

    def test_pre_activation_baseline_is_ignored_and_post_activation_is_diagnostic(self):
        self.insert_account(activated_at=ENTRY)
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY)
        self.insert_position(1, ENTRY - timedelta(seconds=1), status="OPEN")
        self.insert_position(2, ENTRY - timedelta(hours=2), status="CLOSED")
        self.insert_position(3, ENTRY, status="OPEN")
        self.insert_position(4, ENTRY + timedelta(hours=1), status="CLOSED")
        self.insert_position(
            5,
            ENTRY + timedelta(hours=2),
            status="OPEN",
            strategy="OTHER_V",
        )
        allocated = self.insert_position(6, ENTRY + timedelta(hours=3), status="CLOSED")
        self.insert_allocation(allocated, 6, result_percent=1, realized_pnl_usd=1)

        report = self.report()
        quality = report["data_quality"]
        self.assertEqual(quality["post_activation_without_allocation"], 2)
        self.assertEqual(quality["post_activation_open"], 1)
        self.assertEqual(quality["post_activation_closed"], 1)
        text = self.rendered(report)
        self.assertIn("Post-activation baseline without allocation: 2", text)
        start = text.index("Post-activation baseline without allocation:")
        block = text[start:text.index("Расхождение NAV", start)]
        self.assertNotIn("corruption", block.lower())
        self.assertNotIn("error", block.lower())
        self.assertIn("OPEN:   1", block)
        self.assertIn("CLOSED: 1", block)

    def test_repeated_report_and_text_are_identical(self):
        self.insert_account()
        self.insert_ledger("DEPOSIT", 10000, 10000, ENTRY)
        pair_id = self.insert_pair("pair-repeat", "REP/USDC")
        self.insert_position(pair_id, ENTRY, status="CLOSED")
        allocation_id = self.insert_allocation(1, pair_id, result_percent=7, realized_pnl_usd=7)
        self.insert_ledger("BUY", -100, 9900, ENTRY, allocation_id)
        self.insert_ledger(
            "SELL", 107, 10007, ENTRY + timedelta(hours=2), allocation_id,
        )
        self.insert_snapshot(ENTRY, 10000)
        first = self.report()
        second = self.report()
        self.assertEqual(first, second)
        self.assertEqual(self.rendered(first), self.rendered(second))

    def test_cli_prints_sections_and_does_not_write(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-cli", "CLI/USDC")
        self.open_position(pair_id, ENTRY)
        sync_portfolio(ENTRY, db_path=self.db_path)
        before = self.dump()
        output = StringIO()
        with patch("paper_portfolio.ensure_portfolio") as ensure_mock, \
             patch("paper_portfolio.sync_portfolio") as sync_mock, \
             patch("paper_engine.run_cycle") as cycle_mock, \
             patch("sys.stdout", output):
            code = main(["--db", self.db_path])
        text = output.getvalue()

        self.assertEqual(code, 0)
        for title in (
            "1. ACCOUNT",
            "2. POSITIONS",
            "3. CLOSED TRADES",
            "4. BREAKDOWN",
            "5. CAPITAL / RISK",
            "6. RECENT TRADES",
            "7. DATA QUALITY",
        ):
            self.assertIn(title, text)
        self.assertIn("Режим: read-only. Счёт, NAV, ledger и allocations не изменяются.", text)
        self.assertIn("Нет закрытых сделок с result %.", text)
        self.assertLess(text.index("3. CLOSED TRADES"), text.index("4. BREAKDOWN"))
        self.assertLess(text.index("4. BREAKDOWN"), text.index("5. CAPITAL / RISK"))
        self.assertEqual(self.dump(), before)
        ensure_mock.assert_not_called()
        sync_mock.assert_not_called()
        cycle_mock.assert_not_called()

    def test_analytics_connection_rejects_writes(self):
        self.insert_account()
        before = self.dump()
        connection = connect_readonly(self.db_path)
        try:
            mode = connection.execute("PRAGMA query_only").fetchone()
            self.assertEqual(int(mode[0]), 1)
            statements = (
                "CREATE TABLE should_fail (id INTEGER)",
                """
                INSERT INTO paper_account (
                    id, currency, initial_deposit_usd, cash_usd, activated_at,
                    peak_equity_usd, max_drawdown_percent, created_at, updated_at
                ) VALUES (
                    2, 'USD', 1, 1, '2026-01-01 00:00:00', 1, 0,
                    '2026-01-01 00:00:00', '2026-01-01 00:00:00'
                )
                """,
                "UPDATE paper_account SET cash_usd = 0 WHERE id = 1",
                "DELETE FROM paper_account WHERE id = 1",
            )
            for statement in statements:
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    connection.execute(statement)
                self.assertIn("readonly", str(caught.exception).lower())
        finally:
            connection.close()
        self.assertEqual(self.dump(), before)

    def _assert_breakdown_text(self, text):
        self.assertIn("4. BREAKDOWN", text)
        self.assertLess(text.index("3. CLOSED TRADES"), text.index("4. BREAKDOWN"))
        self.assertLess(text.index("4. BREAKDOWN"), text.index("5. CAPITAL / RISK"))
        section = text.split("4. BREAKDOWN", 1)[1].split("5. CAPITAL / RISK", 1)[0]
        blocks = {}
        current = None
        for line in section.splitlines():
            if line in ("final_score", "signal_type", "cohort", "exit_reason"):
                current = line
                blocks[current] = []
            elif current and line.startswith("  "):
                blocks[current].append(line)

        self.assertEqual(
            list(blocks),
            ["final_score", "signal_type", "cohort", "exit_reason"],
        )
        for lines in blocks.values():
            self.assertTrue(lines)
            for line in lines:
                self.assertIn("n=", line)
                self.assertIn("win rate", line)
                self.assertIn("avg", line)
                self.assertIn("realized", line)
                self.assertIn("USD", line)

        self.assertIn("86  n=1  win rate 100.00%  avg +20.00%  realized +20.00 USD", blocks["final_score"][0])
        self.assertIn("NEUTRAL  n=1  win rate 100.00%  avg +5.00%  realized +5.05 USD", blocks["signal_type"][0])
        self.assertIn("PRIMARY  n=2  win rate 100.00%  avg +12.50%  realized +25.05 USD", blocks["cohort"][0])
        self.assertIn("STOP_LOSS  n=1  win rate 0.00%  avg -10.00%  realized -10.00 USD", blocks["exit_reason"][0])

    def _seed_touching_intervals(self, db_path):
        original = database.DB_NAME
        database.DB_NAME = db_path
        try:
            create_tables()
            ensure_portfolio_tables(db_path)
            connection = sqlite3.connect(db_path)
            activated = self.text(ENTRY)
            later = self.text(ENTRY + timedelta(hours=1))
            after = self.text(ENTRY + timedelta(hours=2))
            connection.execute(
                """
                INSERT INTO paper_account (
                    id, currency, initial_deposit_usd, cash_usd, activated_at,
                    peak_equity_usd, max_drawdown_percent, created_at, updated_at
                ) VALUES (1, 'USD', 10000, 10000, ?, 10000, 0, ?, ?)
                """,
                (activated, activated, activated),
            )
            for position_id in (1, 2):
                connection.execute(
                    """
                    INSERT INTO paper_positions (
                        pair_id, strategy_version, status, created_at, updated_at
                    ) VALUES (?, ?, 'CLOSED', ?, ?)
                    """,
                    (position_id, STRATEGY_VERSION, activated, activated),
                )
                connection.execute(
                    """
                    INSERT INTO paper_allocations (
                        portfolio_id, position_id, pair_id, strategy_version, status,
                        allocated_usd, quantity, entry_price, entry_time,
                        exit_price, exit_time, exit_reason, result_percent,
                        realized_pnl_usd, market_value_usd, unrealized_pnl_usd,
                        final_score, signal_type, cohort, created_at, updated_at
                    ) VALUES (
                        1, ?, ?, ?, 'CLOSED',
                        100, 100, 1, ?,
                        1, ?, 'TIME_EXIT', 0,
                        0, 0, 0,
                        80, 'CONFIRMED', 'PRIMARY', ?, ?
                    )
                    """,
                    (
                        position_id,
                        position_id,
                        STRATEGY_VERSION,
                        activated,
                        later if position_id == 1 else after,
                        activated,
                        activated,
                    ),
                )
            connection.execute(
                """
                INSERT INTO paper_cash_ledger (
                    portfolio_id, allocation_id, event_type, amount_usd, cash_after_usd, created_at
                ) VALUES
                    (1, NULL, 'DEPOSIT', 10000, 10000, ?),
                    (1, 1, 'BUY', -100, 9900, ?),
                    (1, 1, 'SELL', 100, 10000, ?),
                    (1, 2, 'BUY', -100, 9900, ?),
                    (1, 2, 'SELL', 100, 10000, ?)
                """,
                (activated, activated, later, later, after),
            )
            connection.commit()
            connection.close()
        finally:
            database.DB_NAME = original


if __name__ == "__main__":
    unittest.main()
