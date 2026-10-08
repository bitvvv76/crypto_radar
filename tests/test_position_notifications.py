import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import database
from approval_bot import TelegramApiError, load_settings, run_cycle
from database import close_open_baseline, create_tables, ensure_paper_tables, open_baseline_position
from human_approval import (
    APPROVAL_PORTFOLIO_ID,
    STATUS_BUY,
    activate_approval_account,
    decide_buy,
    run_approval_maintenance,
)
from paper_engine import (
    EXIT_STOP_LOSS,
    EXIT_TIME,
    EXIT_TRAILING_STOP,
    STRATEGY_VERSION,
    format_datetime,
    observation_bucket,
)
from paper_portfolio import ensure_portfolio, sync_portfolio
from position_notification_store import (
    DELIVERY_DELIVERED,
    DELIVERY_DISPATCHING,
    DELIVERY_RESERVED,
    EVENT_CLOSED,
    claim_position_notification,
    list_position_notifications,
)
from position_notifications import (
    deliver_closed_positions,
    handle_positions_message,
    render_closed_position,
)


ENTRY = datetime(2026, 10, 1, 12, 0, 0)
HUMAN_ENTRY = 0.0125
HUMAN_EXIT = 0.0141
BASELINE_ENTRY = 0.01
BASELINE_RESULT = 77.77


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.answers = []
        self.fail_send = False
        self.fail_updates = False
        self._message_id = 800

    def send_message(self, chat_id, text, reply_markup=None):
        if self.fail_send:
            raise TelegramApiError("telegram request failed")
        self._message_id += 1
        item = {
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
            "message_id": self._message_id,
        }
        self.sent.append(item)
        return {
            "message_id": self._message_id,
            "chat": {"id": chat_id},
            "text": text,
        }

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append({
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        })
        return {"message_id": message_id, "text": text}

    def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append(callback_query_id)
        return True

    def get_updates(self, offset=None, timeout=0):
        if self.fail_updates:
            raise TelegramApiError("telegram request failed")
        return []


class PositionNotificationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "positions.db")
        self.original_db_name = database.DB_NAME
        database.DB_NAME = self.db_path
        create_tables()
        ensure_paper_tables(self.db_path)
        self.chat_id = 777
        self.user_id = 42
        self.settings = load_settings({
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_ALLOWED_USER_ID": str(self.user_id),
            "TELEGRAM_CHAT_ID": str(self.chat_id),
        })

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
        base, quote = symbol.split("/", 1)
        cursor.execute("""
            INSERT INTO pairs (
                chain_id, dex_id, pair_address, pair_symbol,
                base_symbol, quote_symbol, price_usd, final_score, created_at
            )
            VALUES (?, 'raydium', ?, ?, ?, ?, 1, ?, ?)
        """, (
            chain_id,
            pair_address,
            symbol,
            base,
            quote,
            final_score,
            format_datetime(ENTRY),
        ))
        pair_id = cursor.lastrowid
        connection.commit()
        connection.close()
        return pair_id

    def open_position(self, pair_id, entry_price=1, final_score=80, max_hold_hours=240):
        created = open_baseline_position(
            pair_id=pair_id,
            strategy_version=STRATEGY_VERSION,
            signal_type="CONFIRMED",
            final_score=final_score,
            change_24h=4,
            entry_price=entry_price,
            entry_time=format_datetime(ENTRY),
            observation_bucket=observation_bucket(ENTRY),
            stop_loss_percent=15,
            trailing_start_percent=10,
            trailing_distance_percent=5,
            max_hold_hours=max_hold_hours,
            created_at=format_datetime(ENTRY),
            db_path=self.db_path,
        )
        self.assertTrue(created)
        rows = self.query("SELECT * FROM paper_positions WHERE pair_id = ?", (pair_id,))
        self.assertEqual(len(rows), 1)
        return rows[0]

    def close_position(self, position, exit_price, exit_time, result_percent, exit_reason):
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
            exit_reason=exit_reason,
            result_percent=result_percent,
            updated_at=exit_text,
            db_path=self.db_path,
        )
        self.assertTrue(closed)

    def activate(self, now=ENTRY):
        return activate_approval_account(now, db_path=self.db_path)

    def maintain(self, now=ENTRY):
        return run_approval_maintenance(now, db_path=self.db_path)

    def clock_at(self, moment):
        def clock():
            return moment
        return clock

    def allocations(self, portfolio_id):
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

    def notifications(self):
        return list_position_notifications(self.db_path)

    def core_snapshot(self):
        return {
            "accounts": self.query("SELECT * FROM paper_account ORDER BY id ASC"),
            "allocations": self.query("SELECT * FROM paper_allocations ORDER BY id ASC"),
            "ledger": self.query("SELECT * FROM paper_cash_ledger ORDER BY id ASC"),
            "positions": self.query("SELECT * FROM paper_positions ORDER BY id ASC"),
            "requests": self.query("SELECT * FROM approval_requests ORDER BY id ASC"),
            "attempts": self.query("SELECT * FROM approval_attempts ORDER BY id ASC"),
            "snapshots": self.query("SELECT * FROM paper_nav_snapshots ORDER BY id ASC"),
            "marks": self.query("SELECT * FROM paper_price_marks ORDER BY id ASC"),
        }

    def close_human(
        self,
        symbol,
        human_price,
        exit_price,
        exit_reason,
        baseline_entry=BASELINE_ENTRY,
        baseline_result=BASELINE_RESULT,
        hold=timedelta(hours=55),
        address=None,
    ):
        self.activate(ENTRY)
        pair_id = self.insert_pair(address or symbol.lower(), symbol=symbol)
        position = self.open_position(pair_id, entry_price=baseline_entry)
        self.maintain(ENTRY)
        request = self.query(
            "SELECT * FROM approval_requests WHERE position_id = ?",
            (position["id"],),
        )[0]
        bought_at = ENTRY + timedelta(seconds=5)
        bought = decide_buy(
            request["id"],
            db_path=self.db_path,
            price_fetcher=lambda chain_id, pair_address: human_price,
            clock=self.clock_at(bought_at),
        )
        self.assertEqual(bought["status"], STATUS_BUY)
        exit_time = bought_at + hold
        self.close_position(position, exit_price, exit_time, baseline_result, exit_reason)
        stats = self.maintain(exit_time)
        self.assertGreaterEqual(stats["sells"], 1)
        rows = self.allocations(APPROVAL_PORTFOLIO_ID)
        matched = [row for row in rows if row["position_id"] == position["id"]]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["status"], "CLOSED")
        self.assertEqual(matched[0]["entry_price"], human_price)
        return matched[0]

    def open_human(self, symbol, human_price, baseline_entry=1, address=None):
        self.activate(ENTRY)
        pair_id = self.insert_pair(address or symbol.lower(), symbol=symbol)
        position = self.open_position(pair_id, entry_price=baseline_entry)
        self.maintain(ENTRY)
        request = self.query(
            "SELECT * FROM approval_requests WHERE position_id = ?",
            (position["id"],),
        )[0]
        bought = decide_buy(
            request["id"],
            db_path=self.db_path,
            price_fetcher=lambda chain_id, pair_address: human_price,
            clock=self.clock_at(ENTRY + timedelta(seconds=5)),
        )
        self.assertEqual(bought["status"], STATUS_BUY)
        rows = [
            row for row in self.allocations(APPROVAL_PORTFOLIO_ID)
            if row["position_id"] == position["id"]
        ]
        self.assertEqual(rows[0]["status"], "OPEN")
        return position, rows[0]

    def deliver(self, client, moment=None):
        if moment is None:
            moment = ENTRY + timedelta(hours=56)
        return deliver_closed_positions(
            self.db_path,
            client,
            self.chat_id,
            clock=self.clock_at(moment),
        )

    def positions_update(self, user_id=None, chat_id=None, text="/positions", update_id=1):
        if user_id is None:
            user_id = self.user_id
        if chat_id is None:
            chat_id = self.chat_id
        return {
            "update_id": update_id,
            "message": {
                "message_id": 10 + update_id,
                "text": text,
                "from": {"id": user_id},
                "chat": {"id": chat_id},
            },
        }

    def test_profitable_closed_reports_human_pnl(self):
        allocation = self.close_human(
            "ABC/USDC",
            HUMAN_ENTRY,
            HUMAN_EXIT,
            EXIT_TRAILING_STOP,
            address="pair-profit",
        )
        client = FakeTelegram()
        report = self.deliver(client)
        text = client.sent[0]["text"]

        self.assertEqual(report["sent"], 1)
        self.assertEqual(report["allocation_ids"], [allocation["id"]])
        self.assertIn("🔴 CRYPTO RADAR — POSITION CLOSED", text)
        self.assertIn("Пара: ABC / USDC", text)
        self.assertIn("Причина: TRAILING_STOP", text)
        self.assertIn("Цена входа:  $0.012500", text)
        self.assertIn("Цена выхода: $0.014100", text)
        self.assertIn("Вложено:  $100.00", text)
        self.assertIn("Получено: $112.80", text)
        self.assertIn("Результат: +$12.80", text)
        self.assertIn("Доходность: +12.80%", text)
        self.assertIn("Количество куплено: 8 000", text)
        self.assertIn("Время в позиции: 2 дн. 7 ч.", text)
        self.assertIn("Portfolio: Human Approval #2", text)
        self.assertNotEqual(allocation["entry_price"], BASELINE_ENTRY)
        self.assertNotEqual(allocation["result_percent"], BASELINE_RESULT)

    def test_losing_closed_reports_negative_pnl(self):
        self.close_human(
            "DOWN/USDC",
            0.02,
            0.015,
            EXIT_STOP_LOSS,
            baseline_entry=0.03,
            baseline_result=-50,
            address="pair-loss",
        )
        client = FakeTelegram()
        self.deliver(client)
        text = client.sent[0]["text"]

        self.assertIn("Пара: DOWN / USDC", text)
        self.assertIn("Цена входа:  $0.020000", text)
        self.assertIn("Цена выхода: $0.015000", text)
        self.assertIn("Вложено:  $100.00", text)
        self.assertIn("Получено: $75.00", text)
        self.assertIn("Результат: -$25.00", text)
        self.assertIn("Доходность: -25.00%", text)
        self.assertNotIn("+$", text.split("Результат:", 1)[1].split("Доходность:", 1)[0])

    def test_closed_reason_stop_loss(self):
        allocation = self.close_human(
            "SL/USDC",
            0.02,
            0.015,
            EXIT_STOP_LOSS,
            address="pair-sl",
        )
        client = FakeTelegram()
        self.deliver(client)
        self.assertEqual(allocation["exit_reason"], EXIT_STOP_LOSS)
        self.assertIn("Причина: STOP_LOSS", client.sent[0]["text"])

        self.execute(
            "UPDATE paper_allocations SET exit_reason = NULL WHERE id = ?",
            (allocation["id"],),
        )
        self.execute("DELETE FROM position_notifications")
        fallback = FakeTelegram()
        self.deliver(fallback)
        baseline = self.query(
            "SELECT exit_reason FROM paper_positions WHERE id = ?",
            (allocation["position_id"],),
        )[0]
        self.assertEqual(baseline["exit_reason"], EXIT_STOP_LOSS)
        self.assertIsNone(self.query(
            "SELECT exit_reason FROM paper_allocations WHERE id = ?",
            (allocation["id"],),
        )[0]["exit_reason"])
        self.assertIn("Причина: STOP_LOSS", fallback.sent[0]["text"])

    def test_closed_reason_trailing_stop(self):
        allocation = self.close_human(
            "TS/USDC",
            HUMAN_ENTRY,
            HUMAN_EXIT,
            EXIT_TRAILING_STOP,
            address="pair-ts",
        )
        client = FakeTelegram()
        self.deliver(client)
        self.assertEqual(allocation["exit_reason"], EXIT_TRAILING_STOP)
        self.assertIn("Причина: TRAILING_STOP", client.sent[0]["text"])

    def test_closed_reason_time_exit(self):
        allocation = self.close_human(
            "TIME/USDC",
            1,
            1.1,
            EXIT_TIME,
            baseline_entry=1,
            baseline_result=10,
            address="pair-time",
        )
        client = FakeTelegram()
        self.deliver(client)
        self.assertEqual(allocation["exit_reason"], EXIT_TIME)
        self.assertIn("Причина: TIME_EXIT", client.sent[0]["text"])

    def test_pnl_uses_human_entry_not_baseline_result(self):
        allocation = self.close_human(
            "HUMAN/USDC",
            HUMAN_ENTRY,
            HUMAN_EXIT,
            EXIT_TRAILING_STOP,
            baseline_entry=BASELINE_ENTRY,
            baseline_result=BASELINE_RESULT,
            address="pair-human",
        )
        self.execute("""
            UPDATE paper_allocations
            SET result_percent = ?, realized_pnl_usd = ?
            WHERE id = ?
        """, (
            BASELINE_RESULT,
            999,
            allocation["id"],
        ))
        stored = self.query(
            "SELECT entry_price, result_percent, realized_pnl_usd FROM paper_allocations WHERE id = ?",
            (allocation["id"],),
        )[0]
        baseline = self.query(
            "SELECT entry_price, result_percent FROM paper_positions WHERE id = ?",
            (allocation["position_id"],),
        )[0]
        client = FakeTelegram()
        self.deliver(client)
        text = client.sent[0]["text"]

        self.assertEqual(stored["entry_price"], HUMAN_ENTRY)
        self.assertEqual(stored["result_percent"], BASELINE_RESULT)
        self.assertEqual(stored["realized_pnl_usd"], 999)
        self.assertEqual(baseline["entry_price"], BASELINE_ENTRY)
        self.assertEqual(baseline["result_percent"], BASELINE_RESULT)
        self.assertIn("Цена входа:  $0.012500", text)
        self.assertIn("Результат: +$12.80", text)
        self.assertIn("Доходность: +12.80%", text)
        self.assertNotIn("$0.010000", text)
        self.assertNotIn("77.77", text)
        self.assertNotIn("999", text)

    def test_full_close_remaining_quantity_is_zero(self):
        self.close_human(
            "QTY/USDC",
            HUMAN_ENTRY,
            HUMAN_EXIT,
            EXIT_TRAILING_STOP,
            address="pair-qty",
        )
        client = FakeTelegram()
        self.deliver(client)
        text = client.sent[0]["text"]
        self.assertIn("Осталось монет: 0", text)
        self.assertNotIn("Осталось монет: 8 000", text)

        flexible = render_closed_position({
            "pair_label": "ABC / USDC",
            "reason": EXIT_TRAILING_STOP,
            "entry_price": HUMAN_ENTRY,
            "exit_price": HUMAN_EXIT,
            "invested_usd": 100,
            "received_usd": 112.8,
            "pnl_usd": 12.8,
            "pnl_percent": 12.8,
            "quantity": 8000,
            "remaining_quantity": 3,
            "remaining_value_usd": 12.5,
            "hold_label": "2 дн. 7 ч.",
            "portfolio_id": APPROVAL_PORTFOLIO_ID,
        })
        self.assertIn("Осталось монет: 3", flexible)
        self.assertIn("Осталось в позиции: $12.50", flexible)

    def test_full_close_remaining_value_is_zero(self):
        allocation = self.close_human(
            "VAL/USDC",
            HUMAN_ENTRY,
            HUMAN_EXIT,
            EXIT_TIME,
            address="pair-val",
        )
        client = FakeTelegram()
        self.deliver(client)
        self.assertEqual(allocation["market_value_usd"], 0)
        self.assertIn("Осталось в позиции: $0.00", client.sent[0]["text"])

    def test_one_closed_allocation_one_notification(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        control_pair = self.insert_pair("pair-ctrl", symbol="CTRL/USDC")
        self.open_position(control_pair, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.execute(
            "UPDATE paper_allocations SET status = 'CLOSED' WHERE portfolio_id = 1"
        )
        human = self.close_human(
            "ONE/USDC",
            HUMAN_ENTRY,
            HUMAN_EXIT,
            EXIT_TRAILING_STOP,
            address="pair-one",
        )
        client = FakeTelegram()
        first = self.deliver(client)
        second = self.deliver(client)
        notes = self.notifications()

        self.assertEqual(first["sent"], 1)
        self.assertEqual(first["allocation_ids"], [human["id"]])
        self.assertEqual(second["sent"], 0)
        self.assertEqual(len(client.sent), 1)
        self.assertIn("ONE / USDC", client.sent[0]["text"])
        self.assertNotIn("CTRL", client.sent[0]["text"])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["allocation_id"], human["id"])
        self.assertEqual(notes[0]["event_type"], EVENT_CLOSED)
        self.assertEqual(notes[0]["chat_id"], self.chat_id)
        self.assertEqual(notes[0]["message_id"], client.sent[0]["message_id"])
        self.assertEqual(notes[0]["delivery_state"], DELIVERY_DELIVERED)
        self.assertIsNotNone(notes[0]["created_at"])
        self.assertIsNotNone(notes[0]["sent_at"])
        self.assertFalse(claim_position_notification(
            self.db_path,
            human["id"],
            EVENT_CLOSED,
            self.chat_id,
            format_datetime(ENTRY),
        ))
        self.assertEqual(len(self.ledger(APPROVAL_PORTFOLIO_ID, "SELL")), 1)
        self.assertEqual(self.allocations(1)[0]["status"], "CLOSED")

    def test_restart_does_not_duplicate_closed_notification(self):
        human = self.close_human(
            "RESTART/USDC",
            HUMAN_ENTRY,
            HUMAN_EXIT,
            EXIT_TIME,
            address="pair-restart",
        )
        first_client = FakeTelegram()
        self.deliver(first_client)
        restarted = FakeTelegram()
        report = run_cycle(
            self.settings,
            restarted,
            self.db_path,
            clock=self.clock_at(ENTRY + timedelta(hours=80)),
            incoming_updates=[],
        )

        self.assertEqual(len(first_client.sent), 1)
        self.assertEqual(report["closed"]["sent"], 0)
        self.assertEqual(restarted.sent, [])
        self.assertEqual(len(self.notifications()), 1)
        self.assertEqual(self.notifications()[0]["allocation_id"], human["id"])
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID)[0]["status"], "CLOSED")

    def test_send_success_then_attach_failure_does_not_duplicate(self):
        crashed = self.close_human(
            "CRASH/USDC",
            HUMAN_ENTRY,
            HUMAN_EXIT,
            EXIT_TRAILING_STOP,
            address="pair-crash",
        )
        client = FakeTelegram()
        with patch(
            "position_notifications.attach_position_message",
            side_effect=SystemExit("crash before attach"),
        ):
            with self.assertRaises(SystemExit):
                self.deliver(client)
        note = self.notifications()[0]
        restarted = FakeTelegram()
        again = self.deliver(restarted)

        self.assertEqual(len(client.sent), 1)
        self.assertEqual(note["allocation_id"], crashed["id"])
        self.assertIsNone(note["message_id"])
        self.assertEqual(note["delivery_state"], DELIVERY_DISPATCHING)
        self.assertEqual(again["sent"], 0)
        self.assertEqual(restarted.sent, [])
        self.assertEqual(len(self.notifications()), 1)
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID)[0]["status"], "CLOSED")

        attached = self.close_human(
            "ATTACH/USDC",
            0.02,
            0.015,
            EXIT_STOP_LOSS,
            address="pair-attach",
        )
        attach_client = FakeTelegram()
        with patch(
            "position_notifications.attach_position_message",
            side_effect=RuntimeError("attach failed"),
        ):
            attached_report = self.deliver(attach_client)
        later = FakeTelegram()
        later_report = self.deliver(later)
        notes = {
            item["allocation_id"]: item
            for item in self.notifications()
        }

        self.assertEqual(attached_report["sent"], 1)
        self.assertEqual(attached_report["allocation_ids"], [attached["id"]])
        self.assertEqual(attached_report["errors"], [])
        self.assertEqual(len(attach_client.sent), 1)
        self.assertEqual(later_report["sent"], 0)
        self.assertEqual(later.sent, [])
        self.assertEqual(notes[attached["id"]]["delivery_state"], DELIVERY_DELIVERED)
        self.assertEqual(
            notes[attached["id"]]["message_id"],
            attach_client.sent[0]["message_id"],
        )
        self.assertEqual(notes[crashed["id"]]["delivery_state"], DELIVERY_DISPATCHING)
        self.assertEqual(len(self.notifications()), 2)

    def test_unsent_reserved_closed_notification_is_delivered_later(self):
        human = self.close_human(
            "LATER/USDC",
            HUMAN_ENTRY,
            HUMAN_EXIT,
            EXIT_TIME,
            address="pair-later",
        )
        claimed = claim_position_notification(
            self.db_path,
            human["id"],
            EVENT_CLOSED,
            self.chat_id,
            format_datetime(ENTRY),
        )
        self.assertTrue(claimed)
        self.assertEqual(self.notifications()[0]["delivery_state"], DELIVERY_RESERVED)
        self.assertIsNone(self.notifications()[0]["message_id"])

        failed = FakeTelegram()
        failed.fail_send = True
        blocked = self.deliver(failed)
        self.assertEqual(blocked["sent"], 0)
        self.assertTrue(blocked["errors"])
        self.assertEqual(failed.sent, [])
        self.assertEqual(self.notifications(), [])

        client = FakeTelegram()
        delivered = self.deliver(client)
        self.assertEqual(delivered["allocation_ids"], [human["id"]])
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(self.notifications()[0]["delivery_state"], DELIVERY_DELIVERED)
        self.assertEqual(
            self.notifications()[0]["message_id"],
            client.sent[0]["message_id"],
        )
        self.assertEqual(self.deliver(client)["sent"], 0)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(len(self.ledger(APPROVAL_PORTFOLIO_ID, "SELL")), 1)

    def test_telegram_failure_does_not_change_core_data(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        control_pair = self.insert_pair("pair-core", symbol="CORE/USDC")
        self.open_position(control_pair, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.close_human(
            "FAIL/USDC",
            HUMAN_ENTRY,
            HUMAN_EXIT,
            EXIT_STOP_LOSS,
            address="pair-fail",
        )
        before = self.core_snapshot()
        sells_before = len(self.ledger(APPROVAL_PORTFOLIO_ID, "SELL"))
        client = FakeTelegram()
        client.fail_send = True
        report = self.deliver(client)
        client.fail_updates = True
        cycle = run_cycle(
            self.settings,
            client,
            self.db_path,
            clock=self.clock_at(ENTRY + timedelta(hours=80)),
            incoming_updates=[],
        )

        self.assertEqual(report["sent"], 0)
        self.assertTrue(report["errors"])
        self.assertTrue(cycle["errors"])
        self.assertEqual(self.core_snapshot(), before)
        self.assertEqual(len(self.ledger(APPROVAL_PORTFOLIO_ID, "SELL")), sells_before)
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID)[0]["status"], "CLOSED")
        self.assertEqual(self.allocations(1)[0]["status"], "OPEN")
        self.assertEqual(client.sent, [])

    def test_positions_shows_only_open_human_approval(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        control_pair = self.insert_pair("pair-ctrl-open", symbol="CTRL/USDC")
        self.open_position(control_pair, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)

        position, allocation = self.open_human(
            "SHOW/USDC",
            HUMAN_ENTRY,
            baseline_entry=0.01,
            address="pair-show",
        )
        self.execute(
            "UPDATE paper_positions SET last_price = ? WHERE id = ?",
            (0.0133, position["id"]),
        )
        self.maintain(ENTRY + timedelta(minutes=20))
        marked = self.query(
            "SELECT * FROM paper_allocations WHERE id = ?",
            (allocation["id"],),
        )[0]
        self.close_human(
            "HIDE/USDC",
            HUMAN_ENTRY,
            HUMAN_EXIT,
            EXIT_TIME,
            address="pair-hide",
        )
        client = FakeTelegram()
        report = run_cycle(
            self.settings,
            client,
            self.db_path,
            clock=self.clock_at(ENTRY + timedelta(hours=80)),
            incoming_updates=[self.positions_update()],
        )
        open_texts = [
            item["text"] for item in client.sent
            if item["text"].startswith("🟡 OPEN POSITIONS")
        ]
        text = open_texts[0]

        self.assertAlmostEqual(marked["market_value_usd"], 106.4, places=2)
        self.assertEqual(report["handled"][0]["command"], "positions")
        self.assertTrue(report["handled"][0]["acted"])
        self.assertEqual(len(open_texts), 1)
        self.assertIn("SHOW / USDC", text)
        self.assertIn("Вложено: $100.00", text)
        self.assertIn("Текущая стоимость: $106.40", text)
        self.assertIn("PnL: +$6.40 / +6.40%", text)
        self.assertIn("Количество: 8 000", text)
        self.assertNotIn("CTRL", text)
        self.assertNotIn("HIDE", text)
        self.assertEqual(
            [row["status"] for row in self.allocations(APPROVAL_PORTFOLIO_ID) if row["id"] == allocation["id"]],
            ["OPEN"],
        )

    def test_positions_command_is_read_only(self):
        position, allocation = self.open_human(
            "READ/USDC",
            HUMAN_ENTRY,
            address="pair-read",
        )
        self.execute(
            "UPDATE paper_positions SET last_price = ? WHERE id = ?",
            (0.0133, position["id"]),
        )
        self.maintain(ENTRY + timedelta(minutes=20))
        before = self.core_snapshot()
        client = FakeTelegram()
        outcome = handle_positions_message(
            self.positions_update(),
            self.settings,
            client,
            self.db_path,
        )
        cycle_client = FakeTelegram()
        run_cycle(
            self.settings,
            cycle_client,
            self.db_path,
            clock=self.clock_at(ENTRY + timedelta(minutes=30)),
            incoming_updates=[self.positions_update(update_id=2)],
        )

        self.assertTrue(outcome["acted"])
        self.assertEqual(outcome["command"], "positions")
        self.assertEqual(self.core_snapshot(), before)
        self.assertEqual(self.ledger(APPROVAL_PORTFOLIO_ID, "SELL"), [])
        self.assertEqual(
            [row["status"] for row in self.allocations(APPROVAL_PORTFOLIO_ID)],
            ["OPEN"],
        )
        self.assertEqual(len(client.sent), 1)
        self.assertTrue(client.sent[0]["text"].startswith("🟡 OPEN POSITIONS"))
        self.assertEqual(len(cycle_client.sent), 1)

    def test_positions_rejects_foreign_user(self):
        self.open_human("SECRET/USDC", HUMAN_ENTRY, address="pair-secret")
        before = self.core_snapshot()
        client = FakeTelegram()
        foreign = run_cycle(
            self.settings,
            client,
            self.db_path,
            clock=self.clock_at(ENTRY + timedelta(minutes=10)),
            incoming_updates=[self.positions_update(user_id=999)],
        )
        wrong_chat = handle_positions_message(
            self.positions_update(chat_id=12345, update_id=2),
            self.settings,
            client,
            self.db_path,
        )

        self.assertEqual(foreign["handled"][0]["reason"], "forbidden")
        self.assertFalse(foreign["handled"][0]["acted"])
        self.assertEqual(wrong_chat["reason"], "forbidden")
        self.assertFalse(wrong_chat["acted"])
        self.assertEqual(client.sent, [])
        self.assertEqual(self.core_snapshot(), before)
        self.assertNotIn("SECRET", " ".join(item["text"] for item in client.sent))

    def test_positions_missing_price_has_no_invented_pnl(self):
        position, allocation = self.open_human(
            "NOPRICE/USDC",
            HUMAN_ENTRY,
            baseline_entry=9.99,
            address="pair-noprice",
        )
        self.execute(
            "UPDATE paper_positions SET last_price = ? WHERE id = ?",
            (9.99, position["id"]),
        )
        self.execute("""
            UPDATE paper_allocations
            SET last_price = NULL, market_value_usd = NULL, unrealized_pnl_usd = NULL
            WHERE id = ?
        """, (allocation["id"],))
        client = FakeTelegram()
        outcome = handle_positions_message(
            self.positions_update(),
            self.settings,
            client,
            self.db_path,
        )
        text = client.sent[0]["text"]

        self.assertTrue(outcome["acted"])
        self.assertIn("NOPRICE / USDC", text)
        self.assertIn("Вложено: $100.00", text)
        self.assertIn("Текущая стоимость: нет данных", text)
        self.assertIn("PnL: нет данных", text)
        self.assertIn("Количество: 8 000", text)
        self.assertNotIn("%", text)
        self.assertNotIn("9.99", text)
        self.assertNotIn("$106.40", text)
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID)[0]["status"], "OPEN")
        self.assertEqual(self.ledger(APPROVAL_PORTFOLIO_ID, "SELL"), [])


if __name__ == "__main__":
    unittest.main()
