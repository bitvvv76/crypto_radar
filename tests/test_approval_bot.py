import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import approval_bot
import database
from approval_bot import (
    LABEL_BUY_EXECUTED,
    LABEL_BUY_NOT_EXECUTED,
    LABEL_EXPIRED,
    LABEL_SKIPPED,
    TelegramApiError,
    TelegramClient,
    approval_keyboard,
    deliver_pending,
    handle_callback,
    load_settings,
    parse_callback_data,
    refresh_notifications,
    run_cycle,
)
from approval_bot_store import (
    DELIVERY_DELIVERED,
    DELIVERY_DISPATCHING,
    DELIVERY_RESERVED,
    claim_notification,
    list_notifications,
)
from database import create_tables, ensure_paper_tables, open_baseline_position
from human_approval import (
    APPROVAL_PORTFOLIO_ID,
    STATUS_BUY,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_SKIP,
    activate_approval_account,
    decide_buy,
    run_approval_maintenance,
)
from paper_engine import STRATEGY_VERSION, format_datetime, observation_bucket
from paper_portfolio import ensure_portfolio, sync_portfolio


ENTRY = datetime(2026, 10, 1, 12, 0, 0)


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.answers = []
        self.fail_send = False
        self.fail_updates = False
        self._message_id = 500

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
            "reply_markup": reply_markup,
        })
        return {"message_id": message_id, "text": text}

    def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append({
            "callback_query_id": callback_query_id,
            "text": text,
        })
        return True

    def get_updates(self, offset=None, timeout=0):
        if self.fail_updates:
            raise TelegramApiError("telegram request failed")
        return []


class RaisingSession:
    def __init__(self, token):
        self.token = token

    def post(self, url, json, timeout):
        raise RuntimeError("network down {0}".format(url))


class ApprovalBotTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "approval.db")
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
        entry_price=1,
        final_score=80,
        max_hold_hours=168,
        change_24h=4,
    ):
        created = open_baseline_position(
            pair_id=pair_id,
            strategy_version=STRATEGY_VERSION,
            signal_type="CONFIRMED",
            final_score=final_score,
            change_24h=change_24h,
            entry_price=entry_price,
            entry_time=format_datetime(created_at),
            observation_bucket=observation_bucket(created_at),
            stop_loss_percent=15,
            trailing_start_percent=10,
            trailing_distance_percent=5,
            max_hold_hours=max_hold_hours,
            created_at=format_datetime(created_at),
            db_path=self.db_path,
        )
        self.assertTrue(created)
        rows = self.query("SELECT * FROM paper_positions WHERE pair_id = ?", (pair_id,))
        self.assertEqual(len(rows), 1)
        return rows[0]

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

    def notifications(self):
        return list_notifications(self.db_path)

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

    def attempts(self):
        return self.query("SELECT * FROM approval_attempts ORDER BY id ASC")

    def clock_at(self, moment):
        def clock():
            return moment
        return clock

    def seed_pending(self, address="pair-1", symbol="ABC/USDC", max_hold_hours=24, final_score=80):
        self.activate(ENTRY)
        pair_id = self.insert_pair(address, symbol=symbol, final_score=final_score)
        self.open_position(
            pair_id,
            final_score=final_score,
            max_hold_hours=max_hold_hours,
        )
        self.maintain(ENTRY)
        return self.request_row()

    def deliver(self, client, moment=ENTRY):
        return deliver_pending(
            self.db_path,
            client,
            self.chat_id,
            clock=self.clock_at(moment),
        )

    def callback(self, request_id, action, user_id=None, message_id=501, update_id=1, data=None):
        if user_id is None:
            user_id = self.user_id
        if data is None:
            data = "{0}:{1}".format(action, request_id)
        return {
            "update_id": update_id,
            "callback_query": {
                "id": "cb-{0}".format(update_id),
                "from": {"id": user_id},
                "message": {
                    "message_id": message_id,
                    "chat": {"id": self.chat_id},
                    "text": "Pair: ABC/USDC",
                },
                "data": data,
            },
        }

    def press(self, client, request_id, action, moment, user_id=None, price_fetcher=None, data=None, update_id=1):
        notes = self.notifications()
        message_id = notes[0]["message_id"] if notes else 501
        return handle_callback(
            self.callback(
                request_id,
                action,
                user_id=user_id,
                message_id=message_id,
                update_id=update_id,
                data=data,
            ),
            self.settings,
            client,
            self.db_path,
            clock=self.clock_at(moment),
            price_fetcher=price_fetcher,
        )

    def core_snapshot(self):
        return {
            "accounts": self.query("SELECT * FROM paper_account ORDER BY id ASC"),
            "allocations": self.allocations(),
            "ledger": self.query("SELECT * FROM paper_cash_ledger ORDER BY id ASC"),
            "positions": self.query("SELECT * FROM paper_positions ORDER BY id ASC"),
            "requests": self.requests(),
            "snapshots": self.query("SELECT * FROM paper_nav_snapshots ORDER BY id ASC"),
            "marks": self.query("SELECT * FROM paper_price_marks ORDER BY id ASC"),
        }

    def callback_data(self, markup):
        data = []
        for row in markup["inline_keyboard"]:
            for button in row:
                data.append(button["callback_data"])
        return data

    def test_one_request_produces_one_notification(self):
        request = self.seed_pending(symbol="ONE/USDC")
        client = FakeTelegram()
        first = self.deliver(client)
        second = self.deliver(client)
        notes = self.notifications()
        text = client.sent[0]["text"]

        self.assertEqual(first["sent"], 1)
        self.assertEqual(first["request_ids"], [request["id"]])
        self.assertEqual(second["sent"], 0)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["request_id"], request["id"])
        self.assertEqual(notes[0]["chat_id"], self.chat_id)
        self.assertEqual(notes[0]["message_id"], client.sent[0]["message_id"])
        self.assertEqual(notes[0]["last_status"], STATUS_PENDING)
        self.assertIsNotNone(notes[0]["sent_at"])
        self.assertIn("ONE/USDC", text)
        self.assertIn(str(request["final_score"]), text)
        self.assertIn(str(request["change_24h"]), text)
        self.assertIn(request["cohort"], text)
        self.assertIn(str(request["reference_price"]), text)
        self.assertIn(str(request["recommended_usd"]), text)
        self.assertIn(request["eligible_until"], text)
        self.assertEqual(
            self.callback_data(client.sent[0]["reply_markup"]),
            ["buy:{0}".format(request["id"]), "skip:{0}".format(request["id"])],
        )
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])
        self.assertEqual(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY"), [])

    def test_restart_does_not_duplicate_notification(self):
        request = self.seed_pending(symbol="RESTART/USDC")
        first_client = FakeTelegram()
        self.deliver(first_client)
        restarted = FakeTelegram()
        report = self.deliver(restarted)

        self.assertEqual(len(first_client.sent), 1)
        self.assertEqual(report["sent"], 0)
        self.assertEqual(restarted.sent, [])
        self.assertEqual(len(self.notifications()), 1)
        self.assertEqual(self.notifications()[0]["request_id"], request["id"])

    def test_attach_failure_does_not_send_duplicate(self):
        request = self.seed_pending(symbol="ATTACH/USDC")
        client = FakeTelegram()
        with patch("approval_bot.attach_message", side_effect=RuntimeError("attach failed")):
            report = self.deliver(client)
        restarted = FakeTelegram()
        again = self.deliver(restarted)
        note = self.notifications()[0]

        self.assertEqual(report["sent"], 1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(again["sent"], 0)
        self.assertEqual(restarted.sent, [])
        self.assertEqual(note["request_id"], request["id"])
        self.assertEqual(note["message_id"], client.sent[0]["message_id"])
        self.assertEqual(note["delivery_state"], DELIVERY_DELIVERED)
        self.assertEqual(len(self.notifications()), 1)

    def test_restart_after_send_before_attach_does_not_duplicate(self):
        request = self.seed_pending(symbol="CRASH/USDC", max_hold_hours=48)
        client = FakeTelegram()
        with patch("approval_bot.attach_message", side_effect=SystemExit("crash before attach")):
            with self.assertRaises(SystemExit):
                self.deliver(client)
        note = self.notifications()[0]
        restarted = FakeTelegram()
        again = self.deliver(restarted)

        self.assertEqual(len(client.sent), 1)
        self.assertEqual(note["request_id"], request["id"])
        self.assertIsNone(note["message_id"])
        self.assertEqual(note["delivery_state"], DELIVERY_DISPATCHING)
        self.assertEqual(again["sent"], 0)
        self.assertEqual(restarted.sent, [])
        self.assertEqual(len(self.notifications()), 1)

        pair_id = self.insert_pair("pair-later", symbol="LATER/USDC")
        self.open_position(pair_id, max_hold_hours=48)
        self.maintain(ENTRY + timedelta(minutes=1))
        recovered = self.deliver(restarted, moment=ENTRY + timedelta(minutes=1))
        sent_ids = [item["request_id"] for item in self.notifications()]

        self.assertEqual(recovered["sent"], 1)
        self.assertEqual(len(restarted.sent), 1)
        self.assertIn("LATER/USDC", restarted.sent[0]["text"])
        self.assertNotIn(request["id"], recovered["request_ids"])
        self.assertEqual(len(self.notifications()), 2)
        self.assertIn(request["id"], sent_ids)

    def test_unsent_reservation_is_delivered_later(self):
        request = self.seed_pending(symbol="LATER-SEND/USDC")
        claimed = claim_notification(
            self.db_path,
            request["id"],
            self.chat_id,
            format_datetime(ENTRY),
            STATUS_PENDING,
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
        self.assertEqual(delivered["request_ids"], [request["id"]])
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(self.notifications()[0]["delivery_state"], DELIVERY_DELIVERED)
        self.assertEqual(self.notifications()[0]["message_id"], client.sent[0]["message_id"])
        self.assertEqual(self.deliver(client)["sent"], 0)
        self.assertEqual(len(client.sent), 1)

    def test_recovery_sends_missed_actionable_pending(self):
        self.activate(ENTRY)
        short_pair = self.insert_pair("pair-short", symbol="SHORT/USDC")
        long_pair = self.insert_pair("pair-long", symbol="LONG/USDC")
        self.open_position(short_pair, max_hold_hours=1)
        self.open_position(long_pair, max_hold_hours=48)
        self.maintain(ENTRY)
        self.assertEqual(self.notifications(), [])
        rows = self.requests()
        short_id = rows[0]["id"]
        long_id = rows[1]["id"]
        client = FakeTelegram()
        report = self.deliver(client, moment=ENTRY + timedelta(hours=2))

        self.assertEqual(report["request_ids"], [long_id])
        self.assertEqual(len(client.sent), 1)
        self.assertIn("LONG/USDC", client.sent[0]["text"])
        self.assertEqual(
            [note["request_id"] for note in self.notifications()],
            [long_id],
        )
        self.assertNotIn(short_id, report["request_ids"])
        self.assertEqual(self.requests()[0]["status"], STATUS_PENDING)

    def test_buy_uses_existing_human_approval_core(self):
        self.assertIs(decide_buy, approval_bot.decide_buy)
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-buy", symbol="BUY/USDC")
        self.open_position(pair_id, entry_price=1)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.activate(ENTRY)
        self.maintain(ENTRY)
        request = self.request_row()
        control_before = self.query("SELECT * FROM paper_account WHERE id = 1")
        control_allocations = self.allocations(1)
        client = FakeTelegram()
        self.deliver(client)
        calls = []

        def spy(request_id, db_path=None, price_fetcher=None, clock=None):
            calls.append({
                "request_id": request_id,
                "price_fetcher": price_fetcher,
                "clock": clock,
            })
            return decide_buy(
                request_id,
                db_path=db_path,
                price_fetcher=price_fetcher,
                clock=clock,
            )

        with patch("approval_bot.decide_buy", side_effect=spy):
            outcome = self.press(
                client,
                request["id"],
                "buy",
                ENTRY + timedelta(seconds=2),
                price_fetcher=lambda chain_id, pair_address: 2.5,
            )

        bought = self.allocations(APPROVAL_PORTFOLIO_ID)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["request_id"], request["id"])
        self.assertEqual(calls[0]["price_fetcher"]("solana", "pair-buy"), 2.5)
        self.assertNotIn("price", calls[0])
        self.assertEqual(outcome["result"]["status"], STATUS_BUY)
        self.assertEqual(outcome["result"]["execution_price"], 2.5)
        self.assertEqual(outcome["label"], LABEL_BUY_EXECUTED)
        self.assertEqual(len(bought), 1)
        self.assertEqual(bought[0]["portfolio_id"], APPROVAL_PORTFOLIO_ID)
        self.assertEqual(bought[0]["entry_price"], 2.5)
        self.assertNotEqual(bought[0]["entry_price"], request["reference_price"])
        self.assertEqual(self.query("SELECT cash_usd FROM paper_account WHERE id = 2")[0]["cash_usd"], 9900)
        self.assertEqual(self.query("SELECT * FROM paper_account WHERE id = 1"), control_before)
        self.assertEqual(self.allocations(1), control_allocations)
        self.assertNotIn("2.5", client.sent[0]["text"])
        self.assertTrue(client.edits[0]["text"].startswith(LABEL_BUY_EXECUTED))
        self.assertEqual(client.edits[0]["reply_markup"]["inline_keyboard"], [])
        self.assertEqual(self.notifications()[0]["last_status"], STATUS_BUY)

    def test_double_buy_does_not_create_second_buy(self):
        request = self.seed_pending(symbol="DBL/USDC")
        client = FakeTelegram()
        self.deliver(client)
        moment = ENTRY + timedelta(seconds=2)
        fetcher = lambda chain_id, pair_address: 2
        first = self.press(client, request["id"], "buy", moment, price_fetcher=fetcher, update_id=1)
        second = self.press(client, request["id"], "buy", moment, price_fetcher=fetcher, update_id=2)

        self.assertEqual(first["result"]["outcome"], "EXECUTED")
        self.assertEqual(second["result"]["outcome"], "NOOP")
        self.assertEqual(second["result"]["status"], STATUS_BUY)
        self.assertEqual(len(self.allocations(APPROVAL_PORTFOLIO_ID)), 1)
        self.assertEqual(len(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY")), 1)
        self.assertEqual(self.query("SELECT cash_usd FROM paper_account WHERE id = 2")[0]["cash_usd"], 9900)
        self.assertEqual(self.request_row()["status"], STATUS_BUY)

    def test_skip_is_idempotent(self):
        request = self.seed_pending(symbol="SKIP/USDC")
        client = FakeTelegram()
        self.deliver(client)
        first = self.press(
            client,
            request["id"],
            "skip",
            ENTRY + timedelta(minutes=30),
            update_id=1,
        )
        decision_at = self.request_row()["decision_at"]
        second = self.press(
            client,
            request["id"],
            "skip",
            ENTRY + timedelta(hours=1),
            update_id=2,
        )

        self.assertEqual(first["result"]["status"], STATUS_SKIP)
        self.assertEqual(first["label"], LABEL_SKIPPED)
        self.assertEqual(second["result"]["outcome"], "NOOP")
        self.assertEqual(second["result"]["status"], STATUS_SKIP)
        self.assertEqual(second["label"], LABEL_SKIPPED)
        self.assertEqual(self.request_row()["status"], STATUS_SKIP)
        self.assertEqual(self.request_row()["decision_at"], decision_at)
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])
        self.assertEqual(self.ledger(APPROVAL_PORTFOLIO_ID, "BUY"), [])
        self.assertEqual(self.query("SELECT cash_usd FROM paper_account WHERE id = 2")[0]["cash_usd"], 10000)
        self.assertTrue(client.edits[-1]["text"].startswith(LABEL_SKIPPED))

    def test_closed_window_becomes_expired_not_skip(self):
        request = self.seed_pending(symbol="LATE/USDC", max_hold_hours=2)
        client = FakeTelegram()
        self.deliver(client)
        outcome = self.press(
            client,
            request["id"],
            "skip",
            ENTRY + timedelta(hours=2),
        )
        row = self.request_row()

        self.assertEqual(outcome["result"]["status"], STATUS_EXPIRED)
        self.assertEqual(outcome["label"], LABEL_EXPIRED)
        self.assertEqual(row["status"], STATUS_EXPIRED)
        self.assertNotEqual(row["status"], STATUS_SKIP)
        self.assertEqual(row["expiry_reason"], "STRATEGY_WINDOW_ENDED")
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])
        self.assertTrue(client.edits[0]["text"].startswith(LABEL_EXPIRED))
        self.assertEqual(client.edits[0]["reply_markup"]["inline_keyboard"], [])
        self.assertEqual(self.notifications()[0]["last_status"], STATUS_EXPIRED)

    def test_foreign_telegram_user_cannot_act(self):
        request = self.seed_pending(symbol="NOPE/USDC")
        client = FakeTelegram()
        self.deliver(client)
        before = self.core_snapshot()
        with patch("approval_bot.decide_buy") as buy, patch("approval_bot.decide_skip") as skip:
            outcome = self.press(
                client,
                request["id"],
                "buy",
                ENTRY + timedelta(seconds=2),
                user_id=999,
                price_fetcher=lambda chain_id, pair_address: 9,
            )
        buy.assert_not_called()
        skip.assert_not_called()

        self.assertFalse(outcome["acted"])
        self.assertEqual(outcome["reason"], "forbidden")
        self.assertEqual(self.core_snapshot(), before)
        self.assertEqual(self.attempts(), [])
        self.assertEqual(client.edits, [])
        self.assertEqual(self.notifications()[0]["last_status"], STATUS_PENDING)
        self.assertEqual(self.request_row()["status"], STATUS_PENDING)

    def test_telegram_api_failure_does_not_break_core(self):
        ensure_portfolio(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("pair-fail", symbol="FAIL/USDC")
        self.open_position(pair_id)
        sync_portfolio(ENTRY, db_path=self.db_path)
        self.activate(ENTRY)
        self.maintain(ENTRY)
        before = self.core_snapshot()
        client = FakeTelegram()
        client.fail_send = True
        report = self.deliver(client)
        client.fail_updates = True
        cycle = run_cycle(
            self.settings,
            client,
            self.db_path,
            clock=self.clock_at(ENTRY + timedelta(minutes=1)),
        )
        secret = "123456:SECRET-TOKEN"
        with self.assertRaises(TelegramApiError) as caught:
            TelegramClient(secret, session=RaisingSession(secret)).send_message(1, "hi")

        self.assertEqual(report["sent"], 0)
        self.assertTrue(report["errors"])
        self.assertEqual(self.notifications(), [])
        self.assertTrue(cycle["errors"])
        self.assertEqual(cycle["handled"], [])
        self.assertEqual(self.core_snapshot(), before)
        self.assertEqual(self.requests()[0]["status"], STATUS_PENDING)
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("telegram request failed", str(caught.exception))

    def test_callback_carries_only_request_and_action(self):
        request = self.seed_pending()
        markup = approval_keyboard(request["id"])
        self.assertEqual(
            self.callback_data(markup),
            ["buy:{0}".format(request["id"]), "skip:{0}".format(request["id"])],
        )
        self.assertEqual(parse_callback_data("buy:{0}".format(request["id"])), ("buy", request["id"]))
        self.assertEqual(parse_callback_data("skip:{0}".format(request["id"])), ("skip", request["id"]))
        self.assertIsNone(parse_callback_data("buy:{0}:2.5".format(request["id"])))
        self.assertIsNone(parse_callback_data("buy:{0}:100".format(request["id"])))
        client = FakeTelegram()
        self.deliver(client)
        before = self.core_snapshot()
        outcome = self.press(
            client,
            request["id"],
            "buy",
            ENTRY + timedelta(seconds=1),
            data="buy:{0}:2.5:100".format(request["id"]),
            price_fetcher=lambda chain_id, pair_address: 9,
        )
        self.assertFalse(outcome["acted"])
        self.assertEqual(outcome["reason"], "bad_callback")
        self.assertEqual(self.core_snapshot(), before)

    def test_rejected_buy_stays_pending(self):
        request = self.seed_pending(symbol="MISS/USDC")
        client = FakeTelegram()
        self.deliver(client)
        rejected = self.press(
            client,
            request["id"],
            "buy",
            ENTRY + timedelta(seconds=1),
            price_fetcher=lambda chain_id, pair_address: None,
            update_id=1,
        )
        self.assertEqual(rejected["label"], LABEL_BUY_NOT_EXECUTED)
        self.assertEqual(rejected["result"]["status"], STATUS_PENDING)
        self.assertEqual(self.request_row()["status"], STATUS_PENDING)
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])
        self.assertEqual(
            self.callback_data(client.edits[0]["reply_markup"]),
            ["buy:{0}".format(request["id"]), "skip:{0}".format(request["id"])],
        )
        bought = self.press(
            client,
            request["id"],
            "buy",
            ENTRY + timedelta(seconds=2),
            price_fetcher=lambda chain_id, pair_address: 2,
            update_id=2,
        )
        self.assertEqual(bought["result"]["status"], STATUS_BUY)
        self.assertEqual(len(self.allocations(APPROVAL_PORTFOLIO_ID)), 1)

    def test_refresh_expired_edits_without_second_message(self):
        request = self.seed_pending(symbol="SYNC/USDC", max_hold_hours=2)
        client = FakeTelegram()
        self.deliver(client)
        self.maintain(ENTRY + timedelta(hours=2))
        refreshed = refresh_notifications(self.db_path, client)
        again = self.deliver(client, moment=ENTRY + timedelta(hours=2))

        self.assertEqual(refreshed, [request["id"]])
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(again["sent"], 0)
        self.assertEqual(self.request_row()["status"], STATUS_EXPIRED)
        self.assertTrue(client.edits[0]["text"].startswith(LABEL_EXPIRED))
        self.assertEqual(self.notifications()[0]["last_status"], STATUS_EXPIRED)
        self.assertEqual(self.allocations(APPROVAL_PORTFOLIO_ID), [])

    def test_settings_come_from_environment(self):
        with self.assertRaises(RuntimeError):
            load_settings({})
        with self.assertRaises(RuntimeError):
            load_settings({"TELEGRAM_BOT_TOKEN": "only-token"})
        settings = load_settings({
            "TELEGRAM_BOT_TOKEN": "env-token",
            "TELEGRAM_ALLOWED_USER_ID": "7",
            "TELEGRAM_CHAT_ID": "8",
        })
        self.assertEqual(settings.token, "env-token")
        self.assertEqual(settings.allowed_user_id, 7)
        self.assertEqual(settings.chat_id, 8)
        service_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "deploy",
            "systemd",
            "crypto-approval-bot.service",
        )
        with open(service_path, encoding="utf-8") as handle:
            service = handle.read()
        self.assertIn("EnvironmentFile=/opt/crypto_radar/.env", service)
        self.assertIn("approval_bot.py", service)
        self.assertNotIn("env-token", service)
        self.assertNotIn("TELEGRAM_BOT_TOKEN=", service)


if __name__ == "__main__":
    unittest.main()
