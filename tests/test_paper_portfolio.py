import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

import database
from database import close_open_baseline, create_tables, ensure_paper_tables, open_baseline_position
from paper_engine import (
    STRATEGY_VERSION,
    format_datetime,
    observation_bucket,
)
from paper_portfolio import (
    DECISION_AUTO_PAPER_BUY,
    DECISION_SKIPPED,
    DEFAULT_INITIAL_DEPOSIT_USD,
    DEFAULT_POSITION_PERCENT,
    RECOMMENDATION_BASELINE_BUY,
    SKIP_INSUFFICIENT_CASH,
    ensure_portfolio,
    sync_portfolio,
)


ENTRY = datetime(2026, 10, 1, 12, 0, 0)
PAPER_POSITION_COLUMNS = [
    "id",
    "pair_id",
    "strategy_version",
    "signal_type",
    "final_score",
    "change_24h",
    "status",
    "entry_price",
    "entry_time",
    "last_price",
    "last_checked_at",
    "max_price",
    "max_profit_percent",
    "drawdown_from_max_percent",
    "stop_loss_percent",
    "trailing_start_percent",
    "trailing_distance_percent",
    "max_hold_hours",
    "exit_price",
    "exit_time",
    "exit_reason",
    "result_percent",
    "created_at",
    "updated_at",
]
PAPER_MARK_COLUMNS = [
    "id",
    "pair_id",
    "price_usd",
    "observed_at",
    "profit_percent_from_entry",
    "observation_bucket",
]


class PaperPortfolioTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "portfolio.db")
        self.original_db_name = database.DB_NAME
        database.DB_NAME = self.db_path
        create_tables()
        ensure_paper_tables(self.db_path)

    def tearDown(self):
        database.DB_NAME = self.original_db_name
        self.temp_dir.cleanup()

    def columns(self, table_name):
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "PRAGMA table_info({0})".format(table_name)
        ).fetchall()
        connection.close()
        return [row[1] for row in rows]

    def query(self, sql, params=()):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(sql, params).fetchall()
        connection.close()
        return [dict(row) for row in rows]

    def execute(self, sql, params=()):
        connection = sqlite3.connect(self.db_path)
        connection.execute(sql, params)
        connection.commit()
        connection.close()

    def insert_pair(self, pair_address, final_score=80, created_at=ENTRY):
        connection = sqlite3.connect(self.db_path)
        cursor = connection.cursor()
        cursor.execute("""
            INSERT INTO pairs (
                chain_id,
                dex_id,
                pair_address,
                pair_symbol,
                base_symbol,
                quote_symbol,
                price_usd,
                final_score,
                created_at
            )
            VALUES ('solana', 'raydium', ?, 'ABC/USDC', 'ABC', 'USDC', 1, ?, ?)
        """, (
            pair_address,
            final_score,
            format_datetime(created_at),
        ))
        pair_id = cursor.lastrowid
        connection.commit()
        connection.close()
        return pair_id

    def open_position(self, pair_id, created_at, entry_price=1, final_score=80):
        created = open_baseline_position(
            pair_id=pair_id,
            strategy_version=STRATEGY_VERSION,
            signal_type="CONFIRMED",
            final_score=final_score,
            change_24h=4,
            entry_price=entry_price,
            entry_time=format_datetime(created_at),
            observation_bucket=observation_bucket(created_at),
            stop_loss_percent=15,
            trailing_start_percent=10,
            trailing_distance_percent=5,
            max_hold_hours=168,
            created_at=format_datetime(created_at),
            db_path=self.db_path,
        )
        self.assertTrue(created)
        return self.position_by_pair(pair_id)

    def position_by_pair(self, pair_id):
        rows = self.query(
            "SELECT * FROM paper_positions WHERE pair_id = ?",
            (pair_id,),
        )
        self.assertEqual(len(rows), 1)
        return rows[0]

    def account(self):
        rows = self.query("SELECT * FROM paper_account")
        self.assertEqual(len(rows), 1)
        return rows[0]

    def allocations(self):
        return self.query("SELECT * FROM paper_allocations ORDER BY position_id ASC, id ASC")

    def allocation_for(self, position_id):
        rows = self.query(
            "SELECT * FROM paper_allocations WHERE position_id = ?",
            (position_id,),
        )
        self.assertEqual(len(rows), 1)
        return rows[0]

    def ledger(self, event_type=None):
        if event_type is None:
            return self.query("SELECT * FROM paper_cash_ledger ORDER BY id ASC")

        return self.query(
            """
            SELECT *
            FROM paper_cash_ledger
            WHERE event_type = ?
            ORDER BY id ASC
            """,
            (event_type,),
        )

    def snapshots(self):
        return self.query("SELECT * FROM paper_nav_snapshots ORDER BY id ASC")

    def set_last_price(self, position_id, price):
        self.execute(
            "UPDATE paper_positions SET last_price = ? WHERE id = ?",
            (price, position_id),
        )

    def close_baseline(self, position, exit_price, exit_time, result_percent):
        exit_text = format_datetime(exit_time)
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

    def test_account_starts_with_deposit_and_ensure_is_idempotent(self):
        first = ensure_portfolio(ENTRY, db_path=self.db_path)
        second = ensure_portfolio(ENTRY + timedelta(hours=3), db_path=self.db_path)
        account = self.account()
        deposits = self.ledger("DEPOSIT")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(account["initial_deposit_usd"], DEFAULT_INITIAL_DEPOSIT_USD)
        self.assertEqual(account["cash_usd"], 10000)
        self.assertEqual(account["peak_equity_usd"], 10000)
        self.assertEqual(account["max_drawdown_percent"], 0)
        self.assertEqual(account["activated_at"], format_datetime(ENTRY))
        self.assertEqual(second["activated_at"], format_datetime(ENTRY))
        self.assertEqual(len(deposits), 1)
        self.assertEqual(deposits[0]["amount_usd"], 10000)
        self.assertEqual(deposits[0]["cash_after_usd"], 10000)
        self.assertIsNone(deposits[0]["allocation_id"])

    def test_existing_research_tables_keep_their_columns(self):
        before = {
            "pairs": self.columns("pairs"),
            "price_checks": self.columns("price_checks"),
            "watchlist": self.columns("watchlist"),
            "paper_positions": self.columns("paper_positions"),
            "paper_price_marks": self.columns("paper_price_marks"),
        }

        ensure_portfolio(ENTRY, db_path=self.db_path)
        sync_portfolio(ENTRY, db_path=self.db_path)

        self.assertEqual(self.columns("pairs"), before["pairs"])
        self.assertEqual(self.columns("price_checks"), before["price_checks"])
        self.assertEqual(self.columns("watchlist"), before["watchlist"])
        self.assertEqual(self.columns("paper_positions"), before["paper_positions"])
        self.assertEqual(self.columns("paper_price_marks"), before["paper_price_marks"])
        self.assertEqual(before["paper_positions"], PAPER_POSITION_COLUMNS)
        self.assertEqual(before["paper_price_marks"], PAPER_MARK_COLUMNS)
        self.assertIn("recommendation", self.columns("paper_allocations"))
        self.assertIn("decision", self.columns("paper_allocations"))
        self.assertIn("decision_time", self.columns("paper_allocations"))
        self.assertIn("recommended_usd", self.columns("paper_allocations"))
        allocation_sql = self.query(
            "SELECT sql FROM sqlite_master WHERE name = 'paper_allocations'"
        )[0]["sql"]
        self.assertIn("UNIQUE (portfolio_id, position_id)", allocation_sql)

    def test_position_opened_before_activation_is_not_imported(self):
        pair_id = self.insert_pair("pair-old")
        self.open_position(pair_id, ENTRY - timedelta(seconds=1))

        sync_portfolio(ENTRY, db_path=self.db_path)
        self.assertEqual(self.query("SELECT id FROM paper_account"), [])

        ensure_portfolio(ENTRY, db_path=self.db_path)
        sync_portfolio(ENTRY, db_path=self.db_path)

        self.assertEqual(self.allocations(), [])
        self.assertEqual(self.ledger("BUY"), [])
        self.assertEqual(self.account()["cash_usd"], 10000)

    def test_new_position_buys_one_percent_of_nav(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-new")
        position = self.open_position(pair_id, ENTRY, entry_price=1)

        first = sync_portfolio(ENTRY, db_path=self.db_path)
        second = sync_portfolio(ENTRY + timedelta(minutes=1), db_path=self.db_path)
        allocation = self.allocation_for(position["id"])
        buys = self.ledger("BUY")

        self.assertEqual(first["buys"], 1)
        self.assertEqual(second["buys"], 0)
        self.assertEqual(DEFAULT_POSITION_PERCENT, 1.0)
        self.assertEqual(allocation["recommendation"], RECOMMENDATION_BASELINE_BUY)
        self.assertEqual(allocation["decision"], DECISION_AUTO_PAPER_BUY)
        self.assertEqual(allocation["decision_time"], format_datetime(ENTRY))
        self.assertEqual(allocation["recommended_percent"], 1.0)
        self.assertEqual(allocation["recommended_usd"], 100)
        self.assertEqual(allocation["allocated_percent"], 1.0)
        self.assertEqual(allocation["allocated_usd"], 100)
        self.assertEqual(allocation["quantity"], 100)
        self.assertEqual(allocation["status"], "OPEN")
        self.assertEqual(self.account()["cash_usd"], 9900)
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]["amount_usd"], -100)
        self.assertEqual(buys[0]["cash_after_usd"], 9900)
        self.assertEqual(len(self.allocations()), 1)
        self.assertEqual(first["nav"], 10000)
        self.assertFalse(second["snapshot_inserted"])

    def test_open_position_marks_unrealized_pnl_to_market(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-mtm")
        position = self.open_position(pair_id, ENTRY, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.set_last_price(position["id"], 1.25)

        observed = ENTRY + timedelta(minutes=15)
        stats = sync_portfolio(observed, db_path=self.db_path)
        allocation = self.allocation_for(position["id"])

        self.assertEqual(stats["mtm_updates"], 1)
        self.assertEqual(stats["buys"], 0)
        self.assertEqual(allocation["last_price"], 1.25)
        self.assertEqual(allocation["market_value_usd"], 125)
        self.assertEqual(allocation["unrealized_pnl_usd"], 25)
        self.assertEqual(allocation["status"], "OPEN")
        self.assertEqual(self.account()["cash_usd"], 9900)
        self.assertEqual(stats["nav"], 10025)

    def test_closed_baseline_sells_once_and_realizes_pnl(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-sell")
        position = self.open_position(pair_id, ENTRY, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        exit_time = ENTRY + timedelta(minutes=15)
        self.close_baseline(position, exit_price=1.5, exit_time=exit_time, result_percent=50)

        first = sync_portfolio(exit_time, db_path=self.db_path)
        self.set_last_price(position["id"], 9)
        second = sync_portfolio(exit_time + timedelta(minutes=15), db_path=self.db_path)
        allocation = self.allocation_for(position["id"])
        sells = self.ledger("SELL")

        self.assertEqual(first["sells"], 1)
        self.assertEqual(second["sells"], 0)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["amount_usd"], 150)
        self.assertEqual(sells[0]["cash_after_usd"], 10050)
        self.assertEqual(allocation["status"], "CLOSED")
        self.assertEqual(allocation["exit_price"], 1.5)
        self.assertEqual(allocation["result_percent"], 50)
        self.assertEqual(allocation["realized_pnl_usd"], 50)
        self.assertEqual(allocation["unrealized_pnl_usd"], 0)
        self.assertEqual(allocation["market_value_usd"], 0)
        self.assertEqual(self.account()["cash_usd"], 10050)
        self.assertEqual(second["nav"], 10050)
        self.assertEqual(len(self.ledger("BUY")), 1)

    def test_insufficient_cash_skips_without_partial_buy(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        held_pair = self.insert_pair("pair-held")
        held = self.open_position(held_pair, ENTRY, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.set_last_price(held["id"], 98.5)
        self.execute(
            "UPDATE paper_account SET cash_usd = ? WHERE id = 1",
            (150,),
        )
        earlier_pair = self.insert_pair("pair-earlier")
        later_pair = self.insert_pair("pair-later")
        earlier = self.open_position(earlier_pair, ENTRY, entry_price=1)
        later = self.open_position(later_pair, ENTRY, entry_price=1)
        self.assertLess(earlier["id"], later["id"])

        stats = sync_portfolio(ENTRY + timedelta(minutes=15), db_path=self.db_path)
        earlier_allocation = self.allocation_for(earlier["id"])
        later_allocation = self.allocation_for(later["id"])
        buy_position_ids = [
            row["position_id"]
            for row in self.query("""
                SELECT a.position_id
                FROM paper_cash_ledger AS ledger
                JOIN paper_allocations AS a ON a.id = ledger.allocation_id
                WHERE ledger.event_type = 'BUY'
                ORDER BY ledger.id ASC
            """)
        ]

        self.assertEqual(stats["buys"], 1)
        self.assertEqual(stats["skips"], 1)
        self.assertEqual(earlier_allocation["status"], "OPEN")
        self.assertEqual(earlier_allocation["decision"], DECISION_AUTO_PAPER_BUY)
        self.assertEqual(earlier_allocation["allocated_usd"], 100)
        self.assertEqual(earlier_allocation["allocated_percent"], 1.0)
        self.assertEqual(later_allocation["status"], "SKIPPED")
        self.assertEqual(later_allocation["decision"], DECISION_SKIPPED)
        self.assertEqual(later_allocation["recommendation"], RECOMMENDATION_BASELINE_BUY)
        self.assertEqual(later_allocation["skip_reason"], SKIP_INSUFFICIENT_CASH)
        self.assertEqual(later_allocation["recommended_percent"], 1.0)
        self.assertEqual(later_allocation["recommended_usd"], 100)
        self.assertEqual(later_allocation["allocated_percent"], 0)
        self.assertEqual(later_allocation["allocated_usd"], 0)
        self.assertIsNone(later_allocation["quantity"])
        self.assertEqual(buy_position_ids, [held["id"], earlier["id"]])
        self.assertEqual(self.account()["cash_usd"], 50)
        self.assertEqual(len(self.ledger("BUY")), 2)

    def test_repeat_sync_does_not_buy_skipped_position_later(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        held_pair = self.insert_pair("pair-held-skip")
        held = self.open_position(held_pair, ENTRY, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.set_last_price(held["id"], 10000)
        skipped_pair = self.insert_pair("pair-skip")
        skipped = self.open_position(skipped_pair, ENTRY, entry_price=1)

        first = sync_portfolio(ENTRY + timedelta(minutes=15), db_path=self.db_path)
        self.execute("UPDATE paper_account SET cash_usd = 10000 WHERE id = 1")
        second = sync_portfolio(ENTRY + timedelta(minutes=30), db_path=self.db_path)
        allocation = self.allocation_for(skipped["id"])

        self.assertEqual(first["skips"], 1)
        self.assertEqual(first["buys"], 0)
        self.assertEqual(second["buys"], 0)
        self.assertEqual(second["skips"], 0)
        self.assertEqual(allocation["status"], "SKIPPED")
        self.assertEqual(allocation["allocated_usd"], 0)
        self.assertEqual(len(self.ledger("BUY")), 1)

    def test_peak_drawdown_and_one_snapshot_per_bucket(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-dd")
        position = self.open_position(pair_id, ENTRY, entry_price=1)
        opened = sync_portfolio(ENTRY, db_path=self.db_path)
        account = self.account()

        self.assertTrue(opened["snapshot_inserted"])
        self.assertEqual(account["peak_equity_usd"], 10000)
        self.assertEqual(account["max_drawdown_percent"], 0)
        self.assertEqual(self.snapshots()[0]["total_equity_usd"], 10000)
        self.assertEqual(self.snapshots()[0]["drawdown_percent"], 0)

        self.set_last_price(position["id"], 2)
        same_bucket = sync_portfolio(ENTRY + timedelta(minutes=1), db_path=self.db_path)
        account = self.account()
        marked = self.allocation_for(position["id"])

        self.assertFalse(same_bucket["snapshot_inserted"])
        self.assertEqual(len(self.snapshots()), 1)
        self.assertEqual(marked["market_value_usd"], 200)
        self.assertEqual(marked["unrealized_pnl_usd"], 100)
        self.assertEqual(same_bucket["nav"], 10100)
        self.assertEqual(account["peak_equity_usd"], 10000)
        self.assertEqual(account["max_drawdown_percent"], 0)

        self.set_last_price(position["id"], 0.5)
        dropped = sync_portfolio(ENTRY + timedelta(minutes=15), db_path=self.db_path)
        account = self.account()
        drop_snapshot = self.snapshots()[-1]

        self.assertTrue(dropped["snapshot_inserted"])
        self.assertEqual(dropped["nav"], 9950)
        self.assertEqual(drop_snapshot["cash_usd"], 9900)
        self.assertEqual(drop_snapshot["open_market_value_usd"], 50)
        self.assertEqual(drop_snapshot["unrealized_pnl_usd"], -50)
        self.assertEqual(drop_snapshot["realized_pnl_usd"], 0)
        self.assertEqual(drop_snapshot["drawdown_percent"], 0.5)
        self.assertEqual(account["peak_equity_usd"], 10000)
        self.assertEqual(account["max_drawdown_percent"], 0.5)

        self.set_last_price(position["id"], 2)
        recovered = sync_portfolio(ENTRY + timedelta(minutes=30), db_path=self.db_path)
        account = self.account()

        self.assertEqual(recovered["nav"], 10100)
        self.assertEqual(account["peak_equity_usd"], 10100)
        self.assertEqual(account["max_drawdown_percent"], 0.5)
        self.assertEqual(self.snapshots()[-1]["drawdown_percent"], 0)

        self.set_last_price(position["id"], 1.95)
        eased = sync_portfolio(ENTRY + timedelta(minutes=45), db_path=self.db_path)
        account = self.account()

        self.assertAlmostEqual(eased["nav"], 10095)
        self.assertEqual(account["peak_equity_usd"], 10100)
        self.assertEqual(account["max_drawdown_percent"], 0.5)
        self.assertLess(self.snapshots()[-1]["drawdown_percent"], 0.5)
        self.assertEqual(len(self.snapshots()), 4)

    def test_nav_identity_after_realized_and_unrealized(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        winner_pair = self.insert_pair("pair-winner")
        loser_pair = self.insert_pair("pair-loser")
        winner = self.open_position(winner_pair, ENTRY, entry_price=2)
        loser = self.open_position(loser_pair, ENTRY, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.set_last_price(loser["id"], 1.25)
        self.close_baseline(
            winner,
            exit_price=3,
            exit_time=ENTRY + timedelta(minutes=15),
            result_percent=50,
        )

        observed = ENTRY + timedelta(minutes=15)
        stats = sync_portfolio(observed, db_path=self.db_path)
        snapshot = self.snapshots()[-1]
        nav = snapshot["cash_usd"] + snapshot["open_market_value_usd"]
        identity = (
            DEFAULT_INITIAL_DEPOSIT_USD
            + snapshot["realized_pnl_usd"]
            + snapshot["unrealized_pnl_usd"]
        )

        self.assertEqual(self.allocation_for(winner["id"])["realized_pnl_usd"], 50)
        self.assertEqual(self.allocation_for(loser["id"])["unrealized_pnl_usd"], 25)
        self.assertEqual(snapshot["realized_pnl_usd"], 50)
        self.assertEqual(snapshot["unrealized_pnl_usd"], 25)
        self.assertEqual(nav, 10075)
        self.assertEqual(identity, nav)
        self.assertEqual(stats["nav"], nav)

    def test_portfolio_errors_do_not_break_paper_engine(self):
        pair_id = self.insert_pair(
            "pair-guard",
            created_at=datetime.utcnow() - timedelta(days=3),
        )
        self._insert_period(pair_id, "1h", datetime.utcnow() - timedelta(days=3))
        self._insert_period(pair_id, "6h", datetime.utcnow() - timedelta(days=2, hours=18))

        def fake_check(checked_pair_id, check_period, return_error=False):
            self._insert_period(checked_pair_id, check_period, datetime.utcnow())
            return {
                "pair_id": checked_pair_id,
                "pair_symbol": "ABC/USDC",
                "check_period": check_period,
                "old_price_usd": 1,
                "new_price_usd": 1.04,
                "price_change_percent": 4,
                "already_checked": False,
            }

        import auto_check_all

        output = StringIO()

        def fail_sync(now=None, db_path=None):
            raise RuntimeError("portfolio sync boom")

        with patch("auto_check_all.check_pair_price", side_effect=fake_check), \
             patch("paper_portfolio.sync_portfolio", side_effect=fail_sync), \
             patch("sys.stdout", output):
            auto_check_all.main()

        text = output.getvalue()
        position_rows = self.query("SELECT * FROM paper_positions WHERE pair_id = ?", (pair_id,))

        self.assertIn("ИТОГ PAPER ENGINE", text)
        self.assertIn("Открыто baseline: 1", text)
        self.assertIn("PAPER PORTFOLIO: ошибка синхронизации, paper engine уже завершён", text)
        self.assertIn("portfolio sync boom", text)
        self.assertEqual(len(position_rows), 1)
        self.assertEqual(position_rows[0]["status"], "OPEN")
        self.assertEqual(position_rows[0]["strategy_version"], STRATEGY_VERSION)

        output = StringIO()

        def fail_ensure(now=None, db_path=None):
            raise RuntimeError("portfolio init boom")

        with patch("auto_check_all.check_pair_price", side_effect=fake_check), \
             patch("paper_portfolio.ensure_portfolio", side_effect=fail_ensure), \
             patch("paper_portfolio.sync_portfolio", side_effect=fail_sync), \
             patch("sys.stdout", output):
            auto_check_all.main()

        text = output.getvalue()
        self.assertIn("PAPER PORTFOLIO: ошибка инициализации, paper engine продолжит работу", text)
        self.assertIn("portfolio init boom", text)
        self.assertIn("ИТОГ PAPER ENGINE", text)
        self.assertNotIn("PAPER ENGINE: ошибка, проверки цены уже завершены", text)

    def _insert_period(self, pair_id, check_period, checked_at):
        connection = sqlite3.connect(self.db_path)
        connection.execute("""
            INSERT INTO price_checks (
                pair_id,
                check_period,
                old_price_usd,
                new_price_usd,
                price_change_percent,
                checked_at
            )
            VALUES (?, ?, 1, 1.04, 4, ?)
        """, (
            pair_id,
            check_period,
            format_datetime(checked_at),
        ))
        connection.commit()
        connection.close()


if __name__ == "__main__":
    unittest.main()
