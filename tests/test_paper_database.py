import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

import database
from database import (
    close_open_baseline,
    create_tables,
    ensure_paper_tables,
    get_paper_position,
    get_price_marks,
    insert_price_mark,
    open_baseline_position,
)
from paper_engine import (
    EXIT_STOP_LOSS,
    EXIT_TIME,
    EXIT_TRAILING_STOP,
    MAX_HOLD_HOURS,
    STOP_LOSS_PERCENT,
    STRATEGY_VERSION,
    TRAILING_DISTANCE_PERCENT,
    TRAILING_START_PERCENT,
    calculate_drawdown_from_max_percent,
    calculate_profit_percent,
    decide_baseline_exit,
    format_datetime,
    observation_bucket,
    run_cycle,
    tracking_deadline,
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


class PaperDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "paper.db")
        self.original_db_name = database.DB_NAME
        database.DB_NAME = self.db_path
        create_tables()
        ensure_paper_tables(self.db_path)

    def tearDown(self):
        database.DB_NAME = self.original_db_name
        self.temp_dir.cleanup()

    def table_names(self):
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        connection.close()
        return [row[0] for row in rows]

    def columns(self, table_name):
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "PRAGMA table_info({0})".format(table_name)
        ).fetchall()
        connection.close()
        return [row[1] for row in rows]

    def dump(self, table_name):
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT * FROM {0} ORDER BY id".format(table_name)
        ).fetchall()
        connection.close()
        return rows

    def insert_pair(self, pair_address, final_score, created_at, price_usd=1):
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
            VALUES ('solana', 'raydium', ?, 'ABC/USDC', 'ABC', 'USDC', ?, ?, ?)
        """, (
            pair_address,
            price_usd,
            final_score,
            format_datetime(created_at),
        ))
        pair_id = cursor.lastrowid
        connection.commit()
        connection.close()
        return pair_id

    def insert_24h(self, pair_id, new_price, change_percent, checked_at, old_price=1):
        connection = sqlite3.connect(self.db_path)
        cursor = connection.cursor()
        cursor.execute("""
            INSERT INTO price_checks (
                pair_id,
                check_period,
                old_price_usd,
                new_price_usd,
                price_change_percent,
                checked_at
            )
            VALUES (?, '24h', ?, ?, ?, ?)
        """, (
            pair_id,
            old_price,
            new_price,
            change_percent,
            format_datetime(checked_at),
        ))
        connection.commit()
        connection.close()

    def open_at(self, pair_id, entry_time, entry_price=1, change_24h=4, final_score=80):
        created = open_baseline_position(
            pair_id=pair_id,
            strategy_version=STRATEGY_VERSION,
            signal_type="CONFIRMED",
            final_score=final_score,
            change_24h=change_24h,
            entry_price=entry_price,
            entry_time=format_datetime(entry_time),
            observation_bucket=observation_bucket(entry_time),
            stop_loss_percent=STOP_LOSS_PERCENT,
            trailing_start_percent=TRAILING_START_PERCENT,
            trailing_distance_percent=TRAILING_DISTANCE_PERCENT,
            max_hold_hours=MAX_HOLD_HOURS,
            created_at=format_datetime(entry_time),
            db_path=self.db_path,
        )
        self.assertTrue(created)
        return get_paper_position(pair_id, db_path=self.db_path)

    def test_create_tables_does_not_create_paper_tables(self):
        bare_dir = tempfile.TemporaryDirectory()
        bare_path = os.path.join(bare_dir.name, "bare.db")
        database.DB_NAME = bare_path
        create_tables()
        connection = sqlite3.connect(bare_path)
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        ]
        pair_columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(pairs)")
        ]
        connection.close()
        database.DB_NAME = self.db_path

        self.assertNotIn("paper_positions", names)
        self.assertNotIn("paper_price_marks", names)

        ensure_paper_tables(bare_path)
        ensure_paper_tables(bare_path)
        connection = sqlite3.connect(bare_path)
        paper_columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(paper_positions)")
        ]
        mark_columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(paper_price_marks)")
        ]
        pair_columns_after = [
            row[1]
            for row in connection.execute("PRAGMA table_info(pairs)")
        ]
        connection.close()
        bare_dir.cleanup()

        self.assertEqual(paper_columns, PAPER_POSITION_COLUMNS)
        self.assertEqual(mark_columns, PAPER_MARK_COLUMNS)
        self.assertEqual(pair_columns_after, pair_columns)
        self.assertEqual(self.columns("pairs"), pair_columns_after)

    def test_fresh_24h_opens_baseline_and_entry_mark_without_price_fetch(self):
        checked_at = ENTRY
        pair_id = self.insert_pair("pair-open", 80, checked_at - timedelta(hours=24))
        self.insert_24h(pair_id, new_price=1.04, change_percent=4, checked_at=checked_at)
        ledger = {
            "pairs": self.dump("pairs"),
            "price_checks": self.dump("price_checks"),
            "watchlist": self.dump("watchlist"),
        }

        def fetcher(chain_id, pair_address):
            raise AssertionError("вход не запрашивает DEX Screener")

        stats = run_cycle(
            now=checked_at + timedelta(minutes=10),
            price_fetcher=fetcher,
            db_path=self.db_path,
        )

        position = get_paper_position(pair_id, db_path=self.db_path)
        marks = get_price_marks(pair_id, db_path=self.db_path)

        self.assertEqual(stats["opened"], 1)
        self.assertEqual(position["strategy_version"], STRATEGY_VERSION)
        self.assertEqual(position["signal_type"], "CONFIRMED")
        self.assertEqual(position["final_score"], 80)
        self.assertEqual(position["change_24h"], 4)
        self.assertEqual(position["status"], "OPEN")
        self.assertEqual(position["entry_price"], 1.04)
        self.assertEqual(position["entry_time"], format_datetime(checked_at))
        self.assertEqual(position["stop_loss_percent"], STOP_LOSS_PERCENT)
        self.assertEqual(position["trailing_start_percent"], TRAILING_START_PERCENT)
        self.assertEqual(position["trailing_distance_percent"], TRAILING_DISTANCE_PERCENT)
        self.assertEqual(position["max_hold_hours"], MAX_HOLD_HOURS)
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["price_usd"], 1.04)
        self.assertEqual(marks[0]["profit_percent_from_entry"], 0)
        self.assertEqual(marks[0]["observed_at"], format_datetime(checked_at))
        self.assertEqual(self.dump("pairs"), ledger["pairs"])
        self.assertEqual(self.dump("price_checks"), ledger["price_checks"])
        self.assertEqual(self.dump("watchlist"), ledger["watchlist"])

    def test_low_score_and_stale_24h_do_not_open(self):
        now = ENTRY + timedelta(minutes=10)
        low_score_id = self.insert_pair("pair-low", 69, ENTRY - timedelta(hours=24))
        self.insert_24h(low_score_id, 1.1, 10, ENTRY)
        stale_id = self.insert_pair("pair-stale", 90, ENTRY - timedelta(hours=30))
        self.insert_24h(
            stale_id,
            1.1,
            10,
            now - timedelta(minutes=30, seconds=1),
        )

        stats = run_cycle(
            now=now,
            price_fetcher=lambda chain_id, pair_address: 2,
            db_path=self.db_path,
        )

        self.assertEqual(stats["opened"], 0)
        self.assertIsNone(get_paper_position(low_score_id, db_path=self.db_path))
        self.assertIsNone(get_paper_position(stale_id, db_path=self.db_path))

    def test_control_score_opens_once(self):
        pair_id = self.insert_pair("pair-control", 70, ENTRY - timedelta(hours=24))
        self.insert_24h(pair_id, 1.02, 2, ENTRY)

        first = run_cycle(now=ENTRY, price_fetcher=lambda *args: 5, db_path=self.db_path)
        second = run_cycle(now=ENTRY, price_fetcher=lambda *args: 5, db_path=self.db_path)
        position = get_paper_position(pair_id, db_path=self.db_path)

        self.assertEqual(first["opened"], 1)
        self.assertEqual(second["opened"], 0)
        self.assertEqual(position["final_score"], 70)
        self.assertEqual(position["signal_type"], "NEUTRAL")
        self.assertEqual(len(get_price_marks(pair_id, db_path=self.db_path)), 1)

    def test_repeat_cycle_in_same_bucket_ignores_new_mock_price(self):
        pair_id = self.insert_pair("pair-bucket", 85, ENTRY - timedelta(hours=24))
        self.insert_24h(pair_id, 1, 4, ENTRY)
        run_cycle(now=ENTRY, price_fetcher=lambda *args: 9, db_path=self.db_path)

        observed_at = ENTRY + timedelta(minutes=15)
        calls = []

        def first_price(chain_id, pair_address):
            calls.append(1.2)
            return 1.2

        first = run_cycle(
            now=observed_at,
            price_fetcher=first_price,
            db_path=self.db_path,
        )
        after_first = get_paper_position(pair_id, db_path=self.db_path)
        marks_after_first = get_price_marks(pair_id, db_path=self.db_path)

        def second_price(chain_id, pair_address):
            calls.append(0.5)
            return 0.5

        second = run_cycle(
            now=observed_at,
            price_fetcher=second_price,
            db_path=self.db_path,
        )
        after_second = get_paper_position(pair_id, db_path=self.db_path)

        self.assertEqual(first["marks_inserted"], 1)
        self.assertEqual(calls, [1.2])
        self.assertEqual(second["marks_inserted"], 0)
        self.assertEqual(second["baseline_closes"], 0)
        self.assertEqual(second["baseline_updates"], 0)
        self.assertEqual(len(get_price_marks(pair_id, db_path=self.db_path)), len(marks_after_first))
        self.assertEqual(after_second["status"], "OPEN")
        self.assertEqual(after_second["last_price"], after_first["last_price"])
        self.assertEqual(after_second["max_price"], after_first["max_price"])
        self.assertEqual(after_second["exit_reason"], after_first["exit_reason"])
        self.assertEqual(after_second["last_price"], 1.2)

    def test_duplicate_mark_does_not_apply_unsaved_baseline_update(self):
        pair_id = self.insert_pair("pair-direct", 80, ENTRY)
        self.open_at(pair_id, ENTRY, entry_price=1)
        position = get_paper_position(pair_id, db_path=self.db_path)
        observed_at = format_datetime(ENTRY + timedelta(minutes=15))
        bucket = observation_bucket(observed_at)

        inserted = insert_price_mark(
            pair_id=pair_id,
            price_usd=1.2,
            observed_at=observed_at,
            profit_percent_from_entry=20,
            observation_bucket=bucket,
            baseline_update={
                "action": "update",
                "position_id": position["id"],
                "last_price": 1.2,
                "last_checked_at": observed_at,
                "max_price": 1.2,
                "max_profit_percent": 20,
                "drawdown_from_max_percent": 0,
                "updated_at": observed_at,
            },
            db_path=self.db_path,
        )
        duplicate = insert_price_mark(
            pair_id=pair_id,
            price_usd=0.5,
            observed_at=observed_at,
            profit_percent_from_entry=-50,
            observation_bucket=bucket,
            baseline_update={
                "action": "close",
                "position_id": position["id"],
                "last_price": 0.5,
                "last_checked_at": observed_at,
                "max_price": 1.2,
                "max_profit_percent": 20,
                "drawdown_from_max_percent": 50,
                "exit_price": 0.5,
                "exit_time": observed_at,
                "exit_reason": EXIT_STOP_LOSS,
                "result_percent": -50,
                "updated_at": observed_at,
            },
            db_path=self.db_path,
        )
        stored = get_paper_position(pair_id, db_path=self.db_path)
        marks = get_price_marks(pair_id, db_path=self.db_path)

        self.assertTrue(inserted)
        self.assertFalse(duplicate)
        self.assertEqual(len(marks), 2)
        self.assertEqual(marks[-1]["price_usd"], 1.2)
        self.assertEqual(stored["status"], "OPEN")
        self.assertEqual(stored["last_price"], 1.2)
        self.assertIsNone(stored["exit_reason"])

    def test_missing_price_does_not_create_mark(self):
        pair_id = self.insert_pair("pair-missing", 80, ENTRY - timedelta(hours=24))
        self.insert_24h(pair_id, 1, 4, ENTRY)
        run_cycle(now=ENTRY, price_fetcher=lambda *args: None, db_path=self.db_path)
        before = get_paper_position(pair_id, db_path=self.db_path)

        stats = run_cycle(
            now=ENTRY + timedelta(minutes=15),
            price_fetcher=lambda *args: None,
            db_path=self.db_path,
        )
        after = get_paper_position(pair_id, db_path=self.db_path)

        self.assertEqual(stats["price_missing"], 1)
        self.assertEqual(stats["marks_inserted"], 0)
        self.assertEqual(len(get_price_marks(pair_id, db_path=self.db_path)), 1)
        self.assertEqual(after["last_price"], before["last_price"])
        self.assertEqual(after["status"], "OPEN")

    def test_closed_baseline_keeps_receiving_marks_until_window_end(self):
        pair_id = self.insert_pair("pair-closed", 88, ENTRY - timedelta(hours=24))
        self.insert_24h(pair_id, 1, 4, ENTRY)
        run_cycle(now=ENTRY, price_fetcher=lambda *args: 1, db_path=self.db_path)

        stop_at = ENTRY + timedelta(minutes=15)
        run_cycle(now=stop_at, price_fetcher=lambda *args: 0.8, db_path=self.db_path)
        closed = get_paper_position(pair_id, db_path=self.db_path)
        price_checks_before = self.dump("price_checks")

        later = ENTRY + timedelta(minutes=30)
        stats = run_cycle(now=later, price_fetcher=lambda *args: 2, db_path=self.db_path)
        after = get_paper_position(pair_id, db_path=self.db_path)
        marks = get_price_marks(pair_id, db_path=self.db_path)

        self.assertEqual(closed["status"], "CLOSED")
        self.assertEqual(closed["exit_reason"], EXIT_STOP_LOSS)
        self.assertEqual(closed["exit_price"], 0.8)
        self.assertEqual(closed["result_percent"], -20)
        self.assertEqual(stats["marks_inserted"], 1)
        self.assertEqual(stats["baseline_updates"], 0)
        self.assertEqual(stats["baseline_closes"], 0)
        self.assertEqual(len(marks), 3)
        self.assertEqual(marks[-1]["price_usd"], 2)
        self.assertEqual(marks[-1]["profit_percent_from_entry"], 100)
        self.assertEqual(after["exit_price"], closed["exit_price"])
        self.assertEqual(after["exit_reason"], closed["exit_reason"])
        self.assertEqual(after["result_percent"], closed["result_percent"])
        self.assertEqual(after["max_price"], closed["max_price"])
        self.assertEqual(self.dump("price_checks"), price_checks_before)

    def test_marks_stop_after_168_hours(self):
        pair_id = self.insert_pair("pair-window", 90, ENTRY - timedelta(days=8))
        self.open_at(pair_id, ENTRY, entry_price=1)
        inside = tracking_deadline(ENTRY, MAX_HOLD_HOURS)
        calls = []

        def fetch(chain_id, pair_address):
            calls.append(pair_address)
            return 1.1

        inside_stats = run_cycle(now=inside, price_fetcher=fetch, db_path=self.db_path)
        inside_position = get_paper_position(pair_id, db_path=self.db_path)
        inside_marks = get_price_marks(pair_id, db_path=self.db_path)

        after = inside + timedelta(seconds=1)
        after_stats = run_cycle(now=after, price_fetcher=fetch, db_path=self.db_path)

        self.assertEqual(inside_stats["marks_inserted"], 1)
        self.assertEqual(inside_position["status"], "CLOSED")
        self.assertEqual(inside_position["exit_reason"], EXIT_TIME)
        self.assertEqual(inside_position["exit_price"], 1.1)
        self.assertEqual(len(inside_marks), 2)
        self.assertEqual(after_stats["marks_inserted"], 0)
        self.assertEqual(len(get_price_marks(pair_id, db_path=self.db_path)), 2)
        self.assertEqual(calls, ["pair-window"])

    def test_expired_open_closes_on_last_in_window_mark_without_fetch(self):
        pair_id = self.insert_pair("pair-expired", 91, ENTRY - timedelta(days=8))
        self.open_at(pair_id, ENTRY, entry_price=1)
        position = get_paper_position(pair_id, db_path=self.db_path)
        late_mark_time = format_datetime(
            tracking_deadline(ENTRY, MAX_HOLD_HOURS) + timedelta(minutes=20)
        )
        insert_price_mark(
            pair_id=pair_id,
            price_usd=5,
            observed_at=late_mark_time,
            profit_percent_from_entry=400,
            observation_bucket=observation_bucket(late_mark_time),
            baseline_update=None,
            db_path=self.db_path,
        )

        def fetcher(chain_id, pair_address):
            raise AssertionError("EXPIRED OPEN не вызывает DEX Screener")

        now = tracking_deadline(ENTRY, MAX_HOLD_HOURS) + timedelta(minutes=15)
        stats = run_cycle(now=now, price_fetcher=fetcher, db_path=self.db_path)
        closed = get_paper_position(pair_id, db_path=self.db_path)
        marks = get_price_marks(pair_id, db_path=self.db_path)

        self.assertEqual(stats["expired_closed"], 1)
        self.assertEqual(stats["marks_inserted"], 0)
        self.assertEqual(closed["status"], "CLOSED")
        self.assertEqual(closed["exit_reason"], EXIT_TIME)
        self.assertEqual(closed["exit_price"], 1)
        self.assertEqual(closed["exit_time"], format_datetime(ENTRY))
        self.assertEqual(closed["result_percent"], 0)
        self.assertEqual(len(marks), 2)
        self.assertEqual(marks[0]["price_usd"], 1)
        self.assertNotEqual(position["status"], "CLOSED")

    def test_expired_open_without_mark_stays_open(self):
        pair_id = self.insert_pair("pair-empty", 92, ENTRY - timedelta(days=8))
        connection = sqlite3.connect(self.db_path)
        connection.execute("""
            INSERT INTO paper_positions (
                pair_id,
                strategy_version,
                signal_type,
                final_score,
                change_24h,
                status,
                entry_price,
                entry_time,
                last_price,
                last_checked_at,
                max_price,
                max_profit_percent,
                drawdown_from_max_percent,
                stop_loss_percent,
                trailing_start_percent,
                trailing_distance_percent,
                max_hold_hours,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'CONFIRMED', 92, 4, 'OPEN', 1, ?, 1, ?, 1, 0, 0, ?, ?, ?, ?, ?, ?)
        """, (
            pair_id,
            STRATEGY_VERSION,
            format_datetime(ENTRY),
            format_datetime(ENTRY),
            STOP_LOSS_PERCENT,
            TRAILING_START_PERCENT,
            TRAILING_DISTANCE_PERCENT,
            MAX_HOLD_HOURS,
            format_datetime(ENTRY),
            format_datetime(ENTRY),
        ))
        connection.commit()
        connection.close()

        def fetcher(chain_id, pair_address):
            raise AssertionError("EXPIRED OPEN без mark не вызывает DEX Screener")

        now = tracking_deadline(ENTRY, MAX_HOLD_HOURS) + timedelta(hours=1)
        stats = run_cycle(now=now, price_fetcher=fetcher, db_path=self.db_path)
        position = get_paper_position(pair_id, db_path=self.db_path)

        self.assertEqual(stats["expired_without_mark"], 1)
        self.assertEqual(stats["expired_closed"], 0)
        self.assertEqual(position["status"], "OPEN")
        self.assertIsNone(position["exit_price"])
        self.assertIsNone(position["exit_reason"])
        self.assertEqual(get_price_marks(pair_id, db_path=self.db_path), [])

    def test_baseline_exit_matches_mark_replay(self):
        pair_id = self.insert_pair("pair-replay", 95, ENTRY - timedelta(hours=24))
        self.insert_24h(pair_id, 1, 5, ENTRY)
        prices = (
            (ENTRY, None),
            (ENTRY + timedelta(minutes=15), 1.12),
            (ENTRY + timedelta(minutes=30), 1.05),
            (ENTRY + timedelta(minutes=45), 1.4),
        )
        price_by_time = {
            format_datetime(moment): price
            for moment, price in prices
            if price is not None
        }

        def fetcher(chain_id, pair_address):
            return None

        for moment, price in prices:
            current_price = price

            def fetch_at_moment(chain_id, pair_address, current_price=current_price):
                return current_price

            if moment == ENTRY:
                run_cycle(now=moment, price_fetcher=fetcher, db_path=self.db_path)
            else:
                run_cycle(
                    now=moment,
                    price_fetcher=fetch_at_moment,
                    db_path=self.db_path,
                )

        position = get_paper_position(pair_id, db_path=self.db_path)
        marks = get_price_marks(pair_id, db_path=self.db_path)
        replay_reason = None
        replay_result = None
        max_price = position["entry_price"]

        for mark in marks:
            if mark["price_usd"] > max_price:
                max_price = mark["price_usd"]
            max_profit = calculate_profit_percent(position["entry_price"], max_price)
            drawdown = calculate_drawdown_from_max_percent(max_price, mark["price_usd"])
            profit = calculate_profit_percent(position["entry_price"], mark["price_usd"])
            reason = decide_baseline_exit(
                profit,
                max_profit,
                drawdown,
                (
                    datetime.strptime(mark["observed_at"], "%Y-%m-%d %H:%M:%S")
                    - datetime.strptime(position["entry_time"], "%Y-%m-%d %H:%M:%S")
                ).total_seconds() / 3600,
                position["stop_loss_percent"],
                position["trailing_start_percent"],
                position["trailing_distance_percent"],
                position["max_hold_hours"],
            )
            if reason is not None and replay_reason is None:
                replay_reason = reason
                replay_result = profit

        self.assertEqual(position["exit_reason"], EXIT_TRAILING_STOP)
        self.assertEqual(position["result_percent"], 5)
        self.assertEqual(replay_reason, position["exit_reason"])
        self.assertEqual(replay_result, position["result_percent"])
        self.assertEqual(marks[-1]["price_usd"], 1.4)
        self.assertEqual(len(price_by_time), 3)
        self.assertEqual(self.dump("price_checks")[0][2], "24h")

    def test_close_open_baseline_does_not_reopen(self):
        pair_id = self.insert_pair("pair-reopen", 80, ENTRY)
        position = self.open_at(pair_id, ENTRY)
        closed = close_open_baseline(
            position_id=position["id"],
            last_price=0.8,
            last_checked_at=format_datetime(ENTRY),
            max_price=1,
            max_profit_percent=0,
            drawdown_from_max_percent=20,
            exit_price=0.8,
            exit_time=format_datetime(ENTRY),
            exit_reason=EXIT_STOP_LOSS,
            result_percent=-20,
            updated_at=format_datetime(ENTRY),
            db_path=self.db_path,
        )
        again = close_open_baseline(
            position_id=position["id"],
            last_price=0.1,
            last_checked_at=format_datetime(ENTRY),
            max_price=1,
            max_profit_percent=0,
            drawdown_from_max_percent=90,
            exit_price=0.1,
            exit_time=format_datetime(ENTRY),
            exit_reason=EXIT_TIME,
            result_percent=-90,
            updated_at=format_datetime(ENTRY),
            db_path=self.db_path,
        )
        stored = get_paper_position(pair_id, db_path=self.db_path)

        self.assertTrue(closed)
        self.assertFalse(again)
        self.assertEqual(stored["status"], "CLOSED")
        self.assertEqual(stored["exit_reason"], EXIT_STOP_LOSS)
        self.assertEqual(stored["exit_price"], 0.8)


class AutoCheckIsolationTest(unittest.TestCase):
    def test_paper_exception_does_not_hide_check_summary(self):
        import auto_check_all

        output = StringIO()

        def fail_cycle(*args, **kwargs):
            raise RuntimeError("paper boom")

        with patch("auto_check_all.get_pairs_for_next_checks", return_value=[]), \
             patch("paper_engine.run_cycle", fail_cycle), \
             patch("sys.stdout", output):
            auto_check_all.main()

        text = output.getvalue()
        self.assertIn("ИТОГ АВТОПРОВЕРКИ", text)
        self.assertIn("PAPER ENGINE: ошибка, проверки цены уже завершены", text)
        self.assertIn("paper boom", text)


if __name__ == "__main__":
    unittest.main()
