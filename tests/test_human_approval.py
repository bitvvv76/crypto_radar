import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

import database
from database import close_open_baseline, create_tables, ensure_paper_tables, open_baseline_position
from human_approval import (
    APPROVAL_PORTFOLIO_ID,
    EXPIRY_BASELINE_CLOSED,
    EXPIRY_INVALID_WINDOW,
    EXPIRY_WINDOW_ENDED,
    QUOTE_SOURCE,
    REASON_BASELINE_CLOSED,
    REASON_COMMIT_FAILED,
    REASON_INSUFFICIENT_CASH,
    REASON_NOT_PENDING,
    REASON_PRICE_UNAVAILABLE,
    REASON_QUOTE_STALE,
    STATUS_BUY,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_SKIP,
    activate_approval_account,
    decide_buy,
    decide_skip,
    list_actionable,
    run_approval_maintenance,
)
from paper_analytics import load_portfolio
from paper_engine import STRATEGY_VERSION, format_datetime, observation_bucket
from paper_portfolio import ensure_portfolio, sync_portfolio


ENTRY = datetime(2026, 10, 1, 12, 0, 0)


class HumanApprovalTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "approval.db")
        self.original_db_name = database.DB_NAME
        database.DB_NAME = self.db_path
        create_tables()
        ensure_paper_tables(self.db_path)

    def tearDown(self):
        database.DB_NAME = self.original_db_name
        self.temp_dir.cleanup()

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

    def insert_pair(self, pair_address, chain_id="solana", symbol="ABC/USDC", final_score=80):
        connection = sqlite3.connect(self.db_path)
        cursor = connection.cursor()
        cursor.execute("""
            INSERT INTO pairs (
                chain_id, dex_id, pair_address, pair_symbol,
                base_symbol, quote_symbol, price_usd, final_score, created_at
            )
            VALUES (?, 'raydium', ?, ?, 'ABC', 'USDC', 1, ?, ?)
        """, (
            chain_id,
            pair_address,
            symbol,
            final_score,
            format_datetime(ENTRY),
        ))
        pair_id = cursor.lastrowid
        connection.commit()
        connection.close()
        return pair_id

    def open_position(
        self,
        pair_id,
        created_at=ENTRY,
        entry_time=None,
        entry_price=1,
        final_score=80,
        max_hold_hours=168,
        signal_type="CONFIRMED",
        change_24h=4,
        strategy_version=STRATEGY_VERSION,
    ):
        if entry_time is None:
            entry_time = created_at
        created = open_baseline_position(
            pair_id=pair_id,
            strategy_version=strategy_version,
            signal_type=signal_type,
            final_score=final_score,
            change_24h=change_24h,
            entry_price=entry_price,
            entry_time=format_datetime(entry_time),
            observation_bucket=observation_bucket(entry_time),
            stop_loss_percent=15,
            trailing_start_percent=10,
            trailing_distance_percent=5,
            max_hold_hours=max_hold_hours,
            created_at=format_datetime(created_at),
            db_path=self.db_path,
        )
        self.assertTrue(created)
        return self.position_by_pair(pair_id)

    def position_by_pair(self, pair_id):
        rows = self.query("SELECT * FROM paper_positions WHERE pair_id = ?", (pair_id,))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def close_position(self, position, exit_price, exit_time, result_percent):
        exit_text = format_datetime(exit_time)
        closed = close_open_baseline(
            position_id=position["id"],
            last_price=exit_price,
            last_checked_at=exit_text,
            max_price=max(exit_price, position["entry_price"]),
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

    def activate(self, now=ENTRY):
        return activate_approval_account(now, db_path=self.db_path)

    def maintain(self, now=ENTRY):
        return run_approval_maintenance(now, db_path=self.db_path)

    def requests(self):
        return self.query("SELECT * FROM approval_requests ORDER BY id ASC")

    def request_row(self):
        rows = self.requests()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def attempts(self):
        if not self.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'approval_attempts'"
        ):
            return []
        return self.query("SELECT * FROM approval_attempts ORDER BY id ASC")

    def account(self, portfolio_id):
        rows = self.query("SELECT * FROM paper_account WHERE id = ?", (portfolio_id,))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def allocations(self, portfolio_id=None):
        if portfolio_id is None:
            return self.query("SELECT * FROM paper_allocations ORDER BY id ASC")
        return self.query(
            "SELECT * FROM paper_allocations WHERE portfolio_id = ? ORDER BY id ASC",
            (portfolio_id,),
        )

    def ledger(self, portfolio_id, event_type=None):
        if event_type is None:
            return self.query(
                "SELECT * FROM paper_cash_ledger WHERE portfolio_id = ? ORDER BY id ASC",
                (portfolio_id,),
            )
        return self.query(
            """
            SELECT * FROM paper_cash_ledger
            WHERE portfolio_id = ? AND event_type = ?
            ORDER BY id ASC
            """,
            (portfolio_id, event_type),
        )

    def control_money(self):
        return {
            "account": self.query("SELECT * FROM paper_account WHERE id = 1"),
            "allocations": self.allocations(1),
            "ledger": self.ledger(1),
            "snapshots": self.query(
                "SELECT * FROM paper_nav_snapshots WHERE portfolio_id = 1 ORDER BY id ASC"
            ),
        }

    def marks(self):
        return self.query("SELECT * FROM paper_price_marks ORDER BY id ASC")

    def clock_at(self, *moments):
        pending = list(moments)

        def clock():
            if len(pending) == 1:
                return pending[0]
            return pending.pop(0)

        return clock

    def test_activation_is_idempotent_and_cron_does_not_create_account(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        before_control = self.account(1)
        skipped = self.maintain(ENTRY)
        self.assertTrue(skipped["account_missing"])
        self.assertEqual(
            self.query("SELECT id FROM paper_account WHERE id = ?", (APPROVAL_PORTFOLIO_ID,)),
            [],
        )
        self.assertEqual(
            self.query(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'approval_requests'"
            ),
            [],
        )

        first = self.activate(ENTRY)
        second = self.activate(ENTRY + timedelta(days=1))
        deposits = self.ledger(APPROVAL_PORTFOLIO_ID, "DEPOSIT")
        account = self.account(APPROVAL_PORTFOLIO_ID)

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(account["initial_deposit_usd"], 10000)
        self.assertEqual(account["cash_usd"], 10000)
        self.assertEqual(account["activated_at"], format_datetime(ENTRY))
        self.assertEqual(second["activated_at"], format_datetime(ENTRY))
        self.assertEqual(len(deposits), 1)
        self.assertEqual(deposits[0]["amount_usd"], 10000)
        self.assertIsNone(deposits[0]["allocation_id"])
        self.assertEqual(self.account(1), before_control)
        self.assertEqual(
            self.query("SELECT id FROM paper_account ORDER BY id ASC"),
            [{"id": 1}, {"id": 2}],
        )

    def test_activation_does_not_create_control_account(self):
        result = self.activate(ENTRY)
        self.assertTrue(result["created"])
        self.assertEqual(self.query("SELECT id FROM paper_account"), [{"id": 2}])

    def test_open_baseline_becomes_one_pending_request(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-open")
        position = self.open_position(
            pair_id,
            created_at=ENTRY,
            entry_time=ENTRY,
            max_hold_hours=2,
            final_score=80,
        )
        first = self.maintain(ENTRY)
        second = self.maintain(ENTRY + timedelta(minutes=5))
        request = self.request_row()

        self.assertEqual(first["requests_created"], 1)
        self.assertEqual(second["requests_created"], 0)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(request["status"], STATUS_PENDING)
        self.assertEqual(request["position_id"], position["id"])
        self.assertEqual(request["pair_id"], pair_id)
        self.assertEqual(request["chain_id"], "solana")
        self.assertEqual(request["pair_address"], "pair-open")
        self.assertEqual(request["strategy_version"], STRATEGY_VERSION)
        self.assertEqual(request["final_score"], 80)
        self.assertEqual(request["signal_type"], "CONFIRMED")
        self.assertEqual(request["cohort"], "PRIMARY")
        self.assertEqual(request["change_24h"], 4)
        self.assertEqual(request["recommended_percent"], 1.0)
        self.assertEqual(request["recommended_usd"], 100)
        self.assertEqual(request["reference_price"], 1)
        self.assertEqual(request["signal_created_at"], format_datetime(ENTRY))
        self.assertEqual(request["position_created_at"], format_datetime(ENTRY))
        self.assertEqual(float(request["max_hold_hours"]), 2.0)
        self.assertEqual(request["eligible_until"], format_datetime(ENTRY + timedelta(hours=2)))
        self.assertEqual(request["portfolio_id"], APPROVAL_PORTFOLIO_ID)
        self.assertIsNone(request["expiry_reason"])
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])
        self.assertEqual(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY"), [])

    def test_baseline_before_activation_is_not_imported(self):
        pair_id = self.insert_pair("pair-old")
        self.open_position(pair_id, created_at=ENTRY - timedelta(seconds=1))
        self.activate(ENTRY)
        self.maintain(ENTRY)
        self.assertEqual(self.requests(), [])

    def test_closed_baseline_is_created_expired(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-closed")
        position = self.open_position(pair_id, max_hold_hours=24)
        self.close_position(position, exit_price=1.2, exit_time=ENTRY, result_percent=20)
        self.maintain(ENTRY)
        request = self.request_row()
        self.assertEqual(request["status"], STATUS_EXPIRED)
        self.assertEqual(request["expiry_reason"], EXPIRY_BASELINE_CLOSED)
        self.assertEqual(request["expired_at"], format_datetime(ENTRY))
        self.assertIsNone(request["decision_at"])
        self.assertEqual(list_actionable(ENTRY, db_path=self.db_path), [])
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])

    def test_elapsed_strategy_window_is_expired_even_if_baseline_is_open(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-window")
        self.open_position(
            pair_id,
            created_at=ENTRY,
            entry_time=ENTRY - timedelta(hours=3),
            max_hold_hours=2,
        )
        self.maintain(ENTRY)
        request = self.request_row()
        self.assertEqual(request["status"], STATUS_EXPIRED)
        self.assertEqual(request["expiry_reason"], EXPIRY_WINDOW_ENDED)
        self.assertEqual(
            self.position_by_pair(pair_id)["status"],
            "OPEN",
        )
        self.assertEqual(list_actionable(ENTRY, db_path=self.db_path), [])

    def test_pending_request_expires_when_window_ends(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-maintain")
        self.open_position(pair_id, max_hold_hours=5)
        self.maintain(ENTRY)
        self.assertEqual(self.request_row()["status"], STATUS_PENDING)
        later = ENTRY + timedelta(hours=5)
        stats = self.maintain(later)
        request = self.request_row()
        self.assertEqual(stats["expired"], 1)
        self.assertEqual(request["status"], STATUS_EXPIRED)
        self.assertEqual(request["expiry_reason"], EXPIRY_WINDOW_ENDED)
        self.assertEqual(self.position_by_pair(pair_id)["status"], "OPEN")
        self.assertEqual(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY"), [])

    def test_invalid_max_hold_hours_cannot_become_pending(self):
        self.activate(ENTRY)
        null_pair = self.insert_pair("pair-null")
        zero_pair = self.insert_pair("pair-zero")
        self.open_position(null_pair)
        self.open_position(zero_pair, max_hold_hours=0)
        self.execute(
            "UPDATE paper_positions SET max_hold_hours = NULL WHERE pair_id = ?",
            (null_pair,),
        )
        self.maintain(ENTRY)
        reasons = sorted(row["expiry_reason"] for row in self.requests())
        statuses = {row["status"] for row in self.requests()}
        self.assertEqual(statuses, {STATUS_EXPIRED})
        self.assertEqual(reasons, [EXPIRY_INVALID_WINDOW, EXPIRY_INVALID_WINDOW])
        self.assertEqual(list_actionable(ENTRY, db_path=self.db_path), [])

    def test_negative_max_hold_is_invalid_window(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-negative")
        self.open_position(pair_id, max_hold_hours=-4)
        self.maintain(ENTRY)
        request = self.request_row()
        self.assertEqual(request["status"], STATUS_EXPIRED)
        self.assertEqual(request["expiry_reason"], EXPIRY_INVALID_WINDOW)

    def test_buy_uses_fresh_quote_and_snapshot_pair(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-snapshot", chain_id="solana")
        self.open_position(pair_id, entry_price=1, max_hold_hours=10)
        self.maintain(ENTRY)
        self.execute(
            "UPDATE pairs SET chain_id = ?, pair_address = ? WHERE id = ?",
            ("ethereum", "mutated-address", pair_id),
        )
        seen = []
        blocked = []

        def fetch(chain_id, pair_address):
            seen.append((chain_id, pair_address))
            probe = sqlite3.connect(self.db_path, timeout=0.3)
            probe.isolation_level = None
            try:
                probe.execute("BEGIN IMMEDIATE")
                probe.execute("COMMIT")
                blocked.append(False)
            except sqlite3.OperationalError:
                blocked.append(True)
            finally:
                probe.close()
            return 2

        holder = sqlite3.connect(self.db_path, timeout=1)
        holder.isolation_level = None
        holder.execute("BEGIN IMMEDIATE")
        probe = sqlite3.connect(self.db_path, timeout=0.2)
        probe.isolation_level = None
        with self.assertRaises(sqlite3.OperationalError):
            probe.execute("BEGIN IMMEDIATE")
        probe.close()
        holder.execute("ROLLBACK")
        holder.close()

        marks_before = self.marks()
        position_before = self.position_by_pair(pair_id)
        result = decide_buy(
            self.request_row()["id"],
            db_path=self.db_path,
            price_fetcher=fetch,
            clock=self.clock_at(ENTRY + timedelta(seconds=4)),
        )
        request = self.request_row()
        allocation = self.allocations(APPROVAL_PORTFOLIO_ID)[0]

        self.assertEqual(seen, [("solana", "pair-snapshot")])
        self.assertEqual(blocked, [False])
        self.assertEqual(result["status"], STATUS_BUY)
        self.assertEqual(result["outcome"], "EXECUTED")
        self.assertEqual(request["execution_price"], 2)
        self.assertEqual(request["quote_source"], QUOTE_SOURCE)
        self.assertEqual(request["reference_price"], 1)
        self.assertEqual(request["chain_id"], "solana")
        self.assertEqual(request["pair_address"], "pair-snapshot")
        self.assertEqual(allocation["entry_price"], 2)
        self.assertEqual(allocation["allocated_usd"], 100)
        self.assertEqual(allocation["quantity"], 50)
        self.assertEqual(allocation["portfolio_id"], APPROVAL_PORTFOLIO_ID)
        self.assertEqual(allocation["decision"], "HUMAN_BUY")
        self.assertNotEqual(allocation["entry_price"], request["reference_price"])
        self.assertEqual(self.account(APPROVAL_PORTFOLIO_ID)["cash_usd"], 9900)
        self.assertEqual(len(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY")), 1)
        self.assertEqual(self.marks(), marks_before)
        self.assertEqual(self.position_by_pair(pair_id), position_before)
        self.assertEqual(
            [row["outcome"] for row in self.attempts()],
            ["EXECUTED"],
        )

    def test_missing_quote_does_not_fall_back_to_reference_price(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-no-quote")
        self.open_position(pair_id, entry_price=1.25)
        self.maintain(ENTRY)

        def fetch(chain_id, pair_address):
            return None

        result = decide_buy(
            self.request_row()["id"],
            db_path=self.db_path,
            price_fetcher=fetch,
            clock=self.clock_at(ENTRY),
        )
        request = self.request_row()
        self.assertEqual(result["reason"], REASON_PRICE_UNAVAILABLE)
        self.assertEqual(request["status"], STATUS_PENDING)
        self.assertIsNone(request["execution_price"])
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])
        self.assertEqual(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY"), [])
        self.assertEqual(self.account(APPROVAL_PORTFOLIO_ID)["cash_usd"], 10000)
        self.assertEqual(self.attempts()[0]["reason"], REASON_PRICE_UNAVAILABLE)

    def test_zero_quote_and_fetcher_error_stay_pending(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-bad-quote")
        self.open_position(pair_id, entry_price=4)
        self.maintain(ENTRY)
        request_id = self.request_row()["id"]

        def zero(chain_id, pair_address):
            return 0

        def boom(chain_id, pair_address):
            raise TimeoutError("dex down")

        first = decide_buy(
            request_id,
            db_path=self.db_path,
            price_fetcher=zero,
            clock=self.clock_at(ENTRY),
        )
        second = decide_buy(
            request_id,
            db_path=self.db_path,
            price_fetcher=boom,
            clock=self.clock_at(ENTRY),
        )
        self.assertEqual(first["reason"], REASON_PRICE_UNAVAILABLE)
        self.assertEqual(second["reason"], REASON_PRICE_UNAVAILABLE)
        self.assertEqual(self.request_row()["status"], STATUS_PENDING)
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])

    def test_stale_quote_does_not_buy(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-stale")
        self.open_position(pair_id, max_hold_hours=48)
        self.maintain(ENTRY)
        result = decide_buy(
            self.request_row()["id"],
            db_path=self.db_path,
            price_fetcher=lambda chain_id, pair_address: 2,
            clock=self.clock_at(ENTRY, ENTRY + timedelta(seconds=11)),
        )
        request = self.request_row()
        self.assertEqual(result["reason"], REASON_QUOTE_STALE)
        self.assertEqual(request["status"], STATUS_PENDING)
        self.assertIsNone(request["execution_price"])
        self.assertEqual(self.account(APPROVAL_PORTFOLIO_ID)["cash_usd"], 10000)
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])
        self.assertEqual(self.attempts()[-1]["reason"], REASON_QUOTE_STALE)

    def test_buy_after_window_expires_request_without_money(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-buy-window")
        self.open_position(pair_id, max_hold_hours=1)
        self.maintain(ENTRY)
        deadline = ENTRY + timedelta(hours=1)
        result = decide_buy(
            self.request_row()["id"],
            db_path=self.db_path,
            price_fetcher=lambda chain_id, pair_address: 3,
            clock=self.clock_at(deadline - timedelta(seconds=3), deadline),
        )
        request = self.request_row()
        self.assertEqual(result["status"], STATUS_EXPIRED)
        self.assertEqual(result["reason"], EXPIRY_WINDOW_ENDED)
        self.assertEqual(request["status"], STATUS_EXPIRED)
        self.assertEqual(request["expiry_reason"], EXPIRY_WINDOW_ENDED)
        self.assertIsNone(request["execution_price"])
        self.assertIsNone(request["decision_at"])
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])
        self.assertEqual(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY"), [])
        self.assertEqual(self.account(APPROVAL_PORTFOLIO_ID)["cash_usd"], 10000)
        self.assertEqual(self.position_by_pair(pair_id)["status"], "OPEN")

    def test_buy_while_baseline_closed_inside_window_stays_pending(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-buy-closed")
        position = self.open_position(pair_id, max_hold_hours=24)
        self.maintain(ENTRY)
        self.close_position(
            position,
            exit_price=1.4,
            exit_time=ENTRY + timedelta(minutes=10),
            result_percent=40,
        )
        result = decide_buy(
            self.request_row()["id"],
            db_path=self.db_path,
            price_fetcher=lambda chain_id, pair_address: 2,
            clock=self.clock_at(ENTRY + timedelta(minutes=11)),
        )
        self.assertEqual(result["reason"], REASON_BASELINE_CLOSED)
        self.assertEqual(self.request_row()["status"], STATUS_PENDING)
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])

    def test_crash_before_commit_leaves_no_money(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-crash")
        self.open_position(pair_id)
        self.maintain(ENTRY)

        def crash(connection):
            raise RuntimeError("crash before commit")

        with patch("human_approval._commit", side_effect=crash):
            result = decide_buy(
                self.request_row()["id"],
                db_path=self.db_path,
                price_fetcher=lambda chain_id, pair_address: 2,
                clock=self.clock_at(ENTRY + timedelta(seconds=1)),
            )
        self.assertEqual(result["reason"], REASON_COMMIT_FAILED)
        self.assertEqual(self.request_row()["status"], STATUS_PENDING)
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])
        self.assertEqual(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY"), [])
        self.assertEqual(self.account(APPROVAL_PORTFOLIO_ID)["cash_usd"], 10000)

    def test_repeated_buy_is_idempotent(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-repeat-buy")
        self.open_position(pair_id)
        self.maintain(ENTRY)
        calls = []

        def fetch(chain_id, pair_address):
            calls.append(pair_address)
            return 2

        request_id = self.request_row()["id"]
        first = decide_buy(
            request_id,
            db_path=self.db_path,
            price_fetcher=fetch,
            clock=self.clock_at(ENTRY + timedelta(seconds=2)),
        )
        second = decide_buy(
            request_id,
            db_path=self.db_path,
            price_fetcher=fetch,
            clock=self.clock_at(ENTRY + timedelta(seconds=3)),
        )
        self.assertEqual(first["status"], STATUS_BUY)
        self.assertEqual(second["outcome"], "NOOP")
        self.assertEqual(second["reason"], REASON_NOT_PENDING)
        self.assertEqual(calls, ["pair-repeat-buy"])
        self.assertEqual(len(self.allocations(APPROVAL_PORTFOLIO_ID)), 1)
        self.assertEqual(len(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY")), 1)
        self.assertEqual(self.account(APPROVAL_PORTFOLIO_ID)["cash_usd"], 9900)

    def test_insufficient_cash_does_not_partially_buy(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-cash")
        self.open_position(pair_id)
        self.maintain(ENTRY)
        self.execute(
            "UPDATE paper_account SET cash_usd = 40 WHERE id = ?",
            (APPROVAL_PORTFOLIO_ID,),
        )
        result = decide_buy(
            self.request_row()["id"],
            db_path=self.db_path,
            price_fetcher=lambda chain_id, pair_address: 2,
            clock=self.clock_at(ENTRY),
        )
        self.assertEqual(result["reason"], REASON_INSUFFICIENT_CASH)
        self.assertEqual(self.request_row()["status"], STATUS_PENDING)
        self.assertEqual(self.request_row()["recommended_usd"], 100)
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])
        self.assertEqual(self.account(APPROVAL_PORTFOLIO_ID)["cash_usd"], 40)

    def test_skip_only_while_signal_is_executable(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-skip")
        self.open_position(pair_id, max_hold_hours=6)
        self.maintain(ENTRY)
        result = decide_skip(
            self.request_row()["id"],
            db_path=self.db_path,
            clock=self.clock_at(ENTRY + timedelta(minutes=30)),
        )
        request = self.request_row()
        self.assertEqual(result["status"], STATUS_SKIP)
        self.assertEqual(request["status"], STATUS_SKIP)
        self.assertEqual(request["decision_at"], format_datetime(ENTRY + timedelta(minutes=30)))
        self.assertEqual(request["delay_signal_to_decision_seconds"], 1800)
        self.assertIsNone(request["execution_price"])
        self.assertIsNone(request["expiry_reason"])
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])
        self.assertEqual(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY"), [])
        self.assertEqual(self.account(APPROVAL_PORTFOLIO_ID)["cash_usd"], 10000)
        repeat = decide_skip(
            request["id"],
            db_path=self.db_path,
            clock=self.clock_at(ENTRY + timedelta(hours=2)),
        )
        self.assertEqual(repeat["outcome"], "NOOP")
        self.assertEqual(self.request_row()["status"], STATUS_SKIP)
        self.assertEqual(
            self.request_row()["decision_at"],
            format_datetime(ENTRY + timedelta(minutes=30)),
        )

    def test_skip_after_baseline_closes_becomes_expired(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-skip-closed")
        position = self.open_position(pair_id, max_hold_hours=12)
        self.maintain(ENTRY)
        self.close_position(
            position,
            exit_price=0.8,
            exit_time=ENTRY + timedelta(minutes=5),
            result_percent=-20,
        )
        result = decide_skip(
            self.request_row()["id"],
            db_path=self.db_path,
            clock=self.clock_at(ENTRY + timedelta(minutes=6)),
        )
        request = self.request_row()
        self.assertEqual(result["status"], STATUS_EXPIRED)
        self.assertNotEqual(request["status"], STATUS_SKIP)
        self.assertEqual(request["expiry_reason"], EXPIRY_BASELINE_CLOSED)
        self.assertIsNone(request["decision_at"])
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])

    def test_skip_after_eligible_until_becomes_expired(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-skip-window")
        self.open_position(pair_id, max_hold_hours=2)
        self.maintain(ENTRY)
        result = decide_skip(
            self.request_row()["id"],
            db_path=self.db_path,
            clock=self.clock_at(ENTRY + timedelta(hours=2)),
        )
        request = self.request_row()
        self.assertEqual(result["status"], STATUS_EXPIRED)
        self.assertEqual(request["status"], STATUS_EXPIRED)
        self.assertEqual(request["expiry_reason"], EXPIRY_WINDOW_ENDED)
        self.assertNotEqual(request["status"], STATUS_SKIP)
        self.assertEqual(self.position_by_pair(pair_id)["status"], "OPEN")
        self.assertEqual(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY"), [])

    def test_terminal_buy_and_skip_do_not_flip(self):
        self.activate(ENTRY)
        bought_pair = self.insert_pair("pair-bought")
        skipped_pair = self.insert_pair("pair-skipped")
        self.open_position(bought_pair)
        self.open_position(skipped_pair)
        self.maintain(ENTRY)
        rows = self.requests()
        buy_id = rows[0]["id"]
        skip_id = rows[1]["id"]
        decide_buy(
            buy_id,
            db_path=self.db_path,
            price_fetcher=lambda chain_id, pair_address: 2,
            clock=self.clock_at(ENTRY + timedelta(seconds=1)),
        )
        decide_skip(
            skip_id,
            db_path=self.db_path,
            clock=self.clock_at(ENTRY + timedelta(seconds=1)),
        )
        cash = self.account(APPROVAL_PORTFOLIO_ID)["cash_usd"]
        flipped_buy = decide_skip(
            buy_id,
            db_path=self.db_path,
            clock=self.clock_at(ENTRY + timedelta(hours=1)),
        )
        flipped_skip = decide_buy(
            skip_id,
            db_path=self.db_path,
            price_fetcher=lambda chain_id, pair_address: 9,
            clock=self.clock_at(ENTRY + timedelta(hours=1)),
        )
        statuses = {row["id"]: row["status"] for row in self.requests()}
        self.assertEqual(flipped_buy["outcome"], "NOOP")
        self.assertEqual(flipped_skip["outcome"], "NOOP")
        self.assertEqual(statuses[buy_id], STATUS_BUY)
        self.assertEqual(statuses[skip_id], STATUS_SKIP)
        self.assertEqual(self.account(APPROVAL_PORTFOLIO_ID)["cash_usd"], cash)
        self.assertEqual(len(self.allocations(APPROVAL_PORTFOLIO_ID)), 1)

    def test_human_sell_once_uses_execution_price_and_leaves_control(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-sell")
        position = self.open_position(pair_id, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.activate(ENTRY)
        self.maintain(ENTRY)
        control_before = self.control_money()
        decide_buy(
            self.request_row()["id"],
            db_path=self.db_path,
            price_fetcher=lambda chain_id, pair_address: 2,
            clock=self.clock_at(ENTRY + timedelta(seconds=5)),
        )
        self.close_position(
            position,
            exit_price=3,
            exit_time=ENTRY + timedelta(hours=1),
            result_percent=200,
        )
        first = self.maintain(ENTRY + timedelta(hours=1))
        second = self.maintain(ENTRY + timedelta(hours=2))
        human = self.allocations(APPROVAL_PORTFOLIO_ID)[0]
        sells = self.ledger(APPROVAL_PORTFOLIO_ID, "SELL")

        self.assertEqual(first["sells"], 1)
        self.assertEqual(second["sells"], 0)
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["amount_usd"], 150)
        self.assertEqual(human["status"], "CLOSED")
        self.assertEqual(human["exit_price"], 3)
        self.assertEqual(human["entry_price"], 2)
        self.assertEqual(human["result_percent"], 50)
        self.assertEqual(human["realized_pnl_usd"], 50)
        self.assertNotEqual(human["result_percent"], 200)
        self.assertEqual(self.account(APPROVAL_PORTFOLIO_ID)["cash_usd"], 10050)
        self.assertEqual(self.control_money(), control_before)
        self.assertEqual(self.allocations(1)[0]["status"], "OPEN")
        self.assertEqual(self.account(1)["cash_usd"], 9900)

    def test_control_book_and_v06_ignore_approval_portfolio(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-control")
        self.open_position(pair_id, entry_price=1, final_score=75)
        sync_portfolio(ENTRY, db_path=self.db_path)
        before = self.control_money()
        positions_before = self.query("SELECT * FROM paper_positions ORDER BY id ASC")
        marks_before = self.marks()
        self.activate(ENTRY)
        self.maintain(ENTRY)
        decide_buy(
            self.request_row()["id"],
            db_path=self.db_path,
            price_fetcher=lambda chain_id, pair_address: 2,
            clock=self.clock_at(ENTRY + timedelta(seconds=2)),
        )
        self.maintain(ENTRY + timedelta(minutes=15))
        self.assertEqual(self.control_money(), before)
        self.assertEqual(self.query("SELECT * FROM paper_positions ORDER BY id ASC"), positions_before)
        self.assertEqual(self.marks(), marks_before)

        view = load_portfolio(self.db_path)
        self.assertEqual(view["account"]["id"], 1)
        self.assertEqual(view["account"]["cash_usd"], 9900)
        self.assertEqual(len(view["allocations"]), 1)
        self.assertEqual(view["allocations"][0]["portfolio_id"], 1)
        self.assertEqual(view["allocations"][0]["decision"], "AUTO_PAPER_BUY")
        self.assertTrue(all(row["portfolio_id"] == 1 for row in view["ledger"]))
        self.assertTrue(all(row["portfolio_id"] == 1 for row in view["snapshots"]))

    def test_inbox_lists_only_actionable_pending(self):
        self.activate(ENTRY)
        open_pair = self.insert_pair("pair-inbox-open", symbol="OPEN/USDC")
        closed_pair = self.insert_pair("pair-inbox-closed", symbol="CLOSED/USDC")
        self.open_position(open_pair, max_hold_hours=4)
        closed = self.open_position(closed_pair, max_hold_hours=4)
        self.maintain(ENTRY)
        self.close_position(closed, 1.1, ENTRY + timedelta(minutes=1), 10)
        shown = list_actionable(ENTRY + timedelta(minutes=2), db_path=self.db_path)
        self.assertEqual([row["pair_address"] for row in shown], ["pair-inbox-open"])
        hidden = list_actionable(ENTRY + timedelta(hours=4), db_path=self.db_path)
        self.assertEqual(hidden, [])

    def test_snapshot_survives_later_position_edits(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-frozen")
        position = self.open_position(pair_id, final_score=82, change_24h=4)
        self.maintain(ENTRY)
        self.execute(
            """
            UPDATE paper_positions
            SET final_score = 1, change_24h = 99, max_hold_hours = 1
            WHERE id = ?
            """,
            (position["id"],),
        )
        request = self.request_row()
        self.assertEqual(request["final_score"], 82)
        self.assertEqual(request["change_24h"], 4)
        self.assertEqual(float(request["max_hold_hours"]), 168.0)
        self.assertEqual(
            request["eligible_until"],
            format_datetime(ENTRY + timedelta(hours=168)),
        )

    def test_other_strategy_is_ignored(self):
        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-other")
        self.open_position(pair_id, strategy_version="OTHER")
        self.maintain(ENTRY)
        self.assertEqual(self.requests(), [])

    def test_approval_failure_does_not_break_auto_check(self):
        import auto_check_all

        output = StringIO()

        def boom(now=None, db_path=None):
            raise RuntimeError("approval boom")

        with patch("auto_check_all.get_pairs_for_next_checks", return_value=[]), \
             patch("paper_engine.run_cycle", return_value={}), \
             patch("paper_portfolio.ensure_portfolio"), \
             patch("paper_portfolio.sync_portfolio"), \
             patch("human_approval.run_approval_maintenance", side_effect=boom), \
             patch("sys.stdout", output):
            auto_check_all.main()

        text = output.getvalue()
        self.assertIn("ИТОГ АВТОПРОВЕРКИ", text)
        self.assertIn("HUMAN APPROVAL: ошибка, контроль уже завершён", text)
        self.assertIn("approval boom", text)

    def test_cli_activate_inbox_and_decide(self):
        from approval_activate import main as activate_main
        from approval_decide import main as decide_main
        from approval_inbox import main as inbox_main

        self.activate(ENTRY)
        pair_id = self.insert_pair("pair-cli", symbol="CLI/USDC")
        self.open_position(pair_id, max_hold_hours=8)
        self.maintain(ENTRY)
        request_id = self.request_row()["id"]
        activate_output = StringIO()
        inbox_output = StringIO()
        buy_output = StringIO()
        with patch("sys.stdout", activate_output):
            code = activate_main(["--db", self.db_path])
        self.assertEqual(code, 0)
        self.assertIn("Создан сейчас: нет", activate_output.getvalue())
        self.assertIn(format_datetime(ENTRY), activate_output.getvalue())

        with patch("approval_inbox.utc_now", return_value=ENTRY + timedelta(minutes=1)), \
             patch("sys.stdout", inbox_output):
            inbox_main(["--db", self.db_path])
        self.assertIn("Request: {0}".format(request_id), inbox_output.getvalue())
        self.assertIn("CLI/USDC", inbox_output.getvalue())

        with patch("human_approval.default_approval_price_fetcher", return_value=2), \
             patch("human_approval.utc_now", return_value=ENTRY + timedelta(seconds=2)), \
             patch("sys.stdout", buy_output):
            decide_code = decide_main(["--db", self.db_path, str(request_id), "buy"])
        self.assertEqual(decide_code, 0)
        self.assertIn("Status: BUY", buy_output.getvalue())
        self.assertEqual(self.request_row()["execution_price"], 2)


if __name__ == "__main__":
    unittest.main()
