import inspect
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

import requests

import approval_bot
import auto_scan
import database
import daily_report
import scanner
from daily_report import (
    build_daily_report,
    deliver_monitoring,
    observe_auto_check,
    render_daily_report,
    send_due_daily_report,
)
from database import create_tables, ensure_paper_tables, get_paper_position, open_baseline_position
from health_monitor import evaluate_health
from human_approval import activate_approval_account, run_approval_maintenance
from monitor_store import (
    JOB_AUTO_CHECK,
    JOB_SCANNER,
    get_daily_report,
    latest_job_run,
    list_health_state,
    record_job_run,
    save_health_states,
)
from paper_engine import (
    STRATEGY_VERSION,
    candidate_rejection_reason,
    format_datetime,
    run_cycle,
)
from paper_portfolio import ensure_portfolio_tables
from signal_diagnostics import (
    OUTCOME_FILTERED,
    OUTCOME_NO_NEW_EVENTS,
    OUTCOME_PROCESSED_WITH_ERRORS,
    OUTCOME_PROCESSING_ERROR,
    OUTCOME_SIGNALS_CREATED,
    build_signal_diagnostics,
    explain_sql_gaps,
    telegram_delivery_counts,
)


NOW = datetime(2026, 10, 9, 8, 0, 0)
ENTRY = datetime(2026, 10, 1, 12, 0, 0)
TOKEN = "123456789:AAAAAAAAAAABBBBBBBBBBBB"


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.fail_times = 0
        self.crash = None
        self.empty_result = False

    def send_message(self, chat_id, text, reply_markup=None):
        if self.crash is not None:
            raise self.crash
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("telegram request failed {0}".format(TOKEN))
        if self.empty_result:
            return {"ok": True}
        self.sent.append({
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
        })
        return {"message_id": len(self.sent), "text": text}

    def get_updates(self, offset=None, timeout=0):
        raise AssertionError("polling")


class SignalMonitoringTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "monitor.db")
        self.original_db_name = database.DB_NAME
        database.DB_NAME = self.db_path
        scanner.consume_api_errors()

    def tearDown(self):
        database.DB_NAME = self.original_db_name
        scanner.consume_api_errors()
        self.temp_dir.cleanup()

    def test_report_uses_real_window_counts_and_portfolio_numbers(self):
        self.seed_trading_day()
        record_job_run(
            self.db_path,
            JOB_AUTO_CHECK,
            "2026-10-08 10:00:00",
            "ok",
            summary={
                "price_check_errors": [
                    {"kind": "DATA_NOT_FOUND", "pair_id": 1},
                    {"kind": "PRICE_NOT_FOUND", "pair_id": 2},
                ],
                "api_errors": [],
            },
            finished_at="2026-10-08 10:00:05",
        )
        record_job_run(
            self.db_path,
            JOB_SCANNER,
            "2026-10-08 06:00:00",
            "ok",
            summary={"saved_count": 1, "api_errors": []},
            finished_at="2026-10-08 06:05:00",
        )
        before = self.dump("pairs")

        report = build_daily_report(self.db_path, NOW)
        text = render_daily_report(report)

        self.assertEqual(report["report_date"], "2026-10-08")
        self.assertEqual(report["new_ideas"], 1)
        self.assertEqual(report["successful_24h_checks"], 1)
        self.assertEqual(report["paper_positions_opened"], 1)
        self.assertEqual(report["paper_positions_closed"], 1)
        self.assertEqual(report["approval_by_status"]["PENDING"], 1)
        self.assertEqual(report["approval_by_status"]["BUY"], 1)
        self.assertEqual(report["approval_by_status"]["SKIP"], 0)
        self.assertEqual(report["approval_by_status"]["EXPIRED"], 0)
        self.assertEqual(report["portfolios"]["control"]["nav"], 9125)
        self.assertEqual(report["portfolios"]["control"]["cash_usd"], 9000)
        self.assertEqual(report["portfolios"]["control"]["realized_pnl_usd"], 40)
        self.assertEqual(report["portfolios"]["control"]["unrealized_pnl_usd"], 25)
        self.assertEqual(report["portfolios"]["approval"]["nav"], 10110)
        self.assertEqual(report["market_data_errors"], 2)
        self.assertIn("Время UTC: 2026-10-09 08:00:00", text)
        self.assertIn("Период UTC: 2026-10-08 00:00:00 — 2026-10-09 00:00:00", text)
        self.assertIn("Новые идеи: 1", text)
        self.assertIn("Успешные проверки 24h: 1", text)
        self.assertIn("Paper-позиции, новые: 1", text)
        self.assertIn("Paper-позиции, закрытые: 1", text)
        self.assertIn("PENDING: 1", text)
        self.assertIn("BUY: 1", text)
        self.assertIn("NAV: 9125.00", text)
        self.assertIn("Свободные средства: 9000.00", text)
        self.assertIn("Реализованный PnL: 40.00", text)
        self.assertIn("Нереализованный PnL: 25.00", text)
        self.assertIn("NAV: 10110.00", text)
        self.assertIn("Ошибки проверки рыночных данных: 2", text)
        self.assertIn("Сканер: ok, 2026-10-08 06:05:00", text)
        self.assertIn("Автопроверки: ok, 2026-10-08 10:00:05", text)
        self.assertNotIn(TOKEN, text)
        self.assertEqual(self.dump("pairs"), before)
        self.assertEqual(self.dump("price_checks"), self.price_checks)

    def test_missing_tables_stay_unavailable(self):
        text = render_daily_report(build_daily_report(self.db_path, NOW))

        self.assertIn("Новые идеи: н/д", text)
        self.assertIn("Успешные проверки 24h: н/д", text)
        self.assertIn("Paper-позиции, новые: н/д", text)
        self.assertIn("Paper-позиции, закрытые: н/д", text)
        self.assertIn("Заявки Human Approval:\n  н/д", text)
        self.assertIn("Портфель 1, контроль: н/д", text)
        self.assertIn("Портфель 2, Human Approval: н/д", text)
        self.assertIn("Ошибки проверки рыночных данных: н/д", text)
        self.assertIn("Сканер: н/д", text)
        self.assertIn("Автопроверки: н/д", text)
        self.assertNotIn("Новые идеи: 0", text)
        self.assertNotIn("NAV: 0.00", text)
        self.assertNotIn("PENDING: 0", text)

    def test_present_tables_with_no_rows_keep_real_zero_counts(self):
        create_tables()
        ensure_paper_tables(self.db_path)
        ensure_portfolio_tables(self.db_path)
        from human_approval import ensure_approval_tables
        ensure_approval_tables(self.db_path)
        record_job_run(
            self.db_path,
            JOB_AUTO_CHECK,
            "2026-10-08 11:00:00",
            "ok",
            summary={"price_check_errors": [], "api_errors": []},
            finished_at="2026-10-08 11:00:01",
        )

        report = build_daily_report(self.db_path, NOW)
        text = render_daily_report(report)

        self.assertEqual(report["new_ideas"], 0)
        self.assertEqual(report["successful_24h_checks"], 0)
        self.assertEqual(report["paper_positions_opened"], 0)
        self.assertEqual(report["paper_positions_closed"], 0)
        self.assertEqual(report["approval_by_status"]["PENDING"], 0)
        self.assertIsNone(report["portfolios"]["control"])
        self.assertEqual(report["market_data_errors"], 0)
        self.assertIn("Новые идеи: 0", text)
        self.assertIn("Портфель 1, контроль: н/д", text)
        self.assertIn("Ошибки проверки рыночных данных: 0", text)

    def test_resend_is_idempotent_and_failure_retries_once(self):
        client = FakeTelegram()
        client.fail_times = 1

        failed = send_due_daily_report(self.db_path, client, 77, now=NOW)
        stored = get_daily_report(self.db_path, "2026-10-08")
        delivered = send_due_daily_report(self.db_path, client, 77, now=NOW)
        repeated = send_due_daily_report(self.db_path, client, 77, now=NOW)
        next_day = send_due_daily_report(
            self.db_path,
            client,
            77,
            now=NOW + timedelta(days=1),
        )

        self.assertFalse(failed["sent"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(stored["status"], "failed")
        self.assertIsNone(stored["message_id"])
        self.assertNotIn(TOKEN, stored["error_text"])
        self.assertNotIn(TOKEN, failed["error"])
        self.assertIn("[redacted]", stored["error_text"])
        self.assertTrue(delivered["sent"])
        self.assertEqual(delivered["message_id"], 1)
        self.assertEqual(get_daily_report(self.db_path, "2026-10-08")["status"], "delivered")
        self.assertFalse(repeated["sent"])
        self.assertEqual(repeated["status"], "delivered")
        self.assertTrue(next_day["sent"])
        self.assertEqual(next_day["report_date"], "2026-10-09")
        self.assertEqual(len(client.sent), 2)
        self.assertNotIn(TOKEN, client.sent[0]["text"])

    def test_crash_during_send_does_not_send_a_second_copy(self):
        crashing = FakeTelegram()
        crashing.crash = SystemExit("crash before result")
        with self.assertRaises(SystemExit):
            send_due_daily_report(self.db_path, crashing, 77, now=NOW)
        self.assertEqual(get_daily_report(self.db_path, "2026-10-08")["status"], "dispatching")

        client = FakeTelegram()
        skipped = send_due_daily_report(self.db_path, client, 77, now=NOW)

        self.assertFalse(skipped["sent"])
        self.assertEqual(skipped["status"], "dispatching")
        self.assertEqual(client.sent, [])

    def test_empty_telegram_result_can_be_retried(self):
        empty = FakeTelegram()
        empty.empty_result = True
        first = send_due_daily_report(self.db_path, empty, 77, now=NOW)
        empty.empty_result = False
        second = send_due_daily_report(self.db_path, empty, 77, now=NOW)

        self.assertEqual(first["status"], "failed")
        self.assertTrue(second["sent"])
        self.assertEqual(len(empty.sent), 1)

    def test_monitoring_delivery_does_not_poll(self):
        client = FakeTelegram()
        result = deliver_monitoring(self.db_path, client, 77, now=NOW)
        source = inspect.getsource(daily_report)
        hook = inspect.getsource(approval_bot._deliver_monitoring_pass)

        self.assertTrue(result["daily"]["sent"])
        self.assertTrue(result["health_sent"])
        self.assertNotIn(".get_updates(", source)
        self.assertNotIn("getUpdates", source)
        self.assertNotIn("get_updates", hook)
        self.assertNotIn("TelegramClient(", hook)
        self.assertIn("_deliver_monitoring_pass", inspect.getsource(approval_bot.main))
        joined = "\n".join(item["text"] for item in client.sent)
        self.assertIn("ежедневный отчёт", joined)
        self.assertIn("Плановое сканирование не выполнялось", joined)
        self.assertIn("Проверки цены не выполнялись", joined)

    def test_health_alert_is_sent_once_until_state_changes(self):
        moment = NOW
        save_health_states(self.db_path, {
            "scanner": {"state": "ok", "detail": "есть"},
            "api": {"state": "ok", "detail": "нет"},
        }, "2026-10-09 07:00:00")
        record_job_run(
            self.db_path,
            JOB_SCANNER,
            "2026-10-09 07:30:00",
            "ok",
            summary={"api_errors": [{"source": "search", "kind": "timeout"}]},
            finished_at="2026-10-09 07:40:00",
        )
        record_job_run(
            self.db_path,
            JOB_AUTO_CHECK,
            "2026-10-09 07:50:00",
            "ok",
            summary={"api_errors": [], "price_check_errors": []},
            finished_at="2026-10-09 07:50:00",
        )

        first = evaluate_health(self.db_path, moment, telegram_errors=["telegram request failed"])
        second = evaluate_health(self.db_path, moment, telegram_errors=["telegram request failed"])
        save_health_states(self.db_path, first["states"], "2026-10-09 08:00:00")
        third = evaluate_health(self.db_path, moment, telegram_errors=["telegram request failed"])
        record_job_run(
            self.db_path,
            JOB_SCANNER,
            "2026-10-09 07:55:00",
            "ok",
            summary={"api_errors": []},
            finished_at="2026-10-09 07:55:00",
        )
        recovered = evaluate_health(self.db_path, moment, telegram_errors=[])

        keys = [item["alert_key"] for item in first["changes"]]
        self.assertEqual(keys, ["api", "telegram"])
        self.assertEqual(
            [item["alert_key"] for item in second["changes"]],
            keys,
        )
        self.assertEqual(third["changes"], [])
        self.assertEqual(
            [item["alert_key"] for item in recovered["changes"]],
            ["api", "telegram"],
        )
        self.assertTrue(all(item["state"] == "ok" for item in recovered["changes"]))

    def test_stale_jobs_alert_once_and_price_check_recovers(self):
        record_job_run(
            self.db_path,
            JOB_SCANNER,
            "2026-10-07 00:00:00",
            "ok",
            summary={"api_errors": []},
            finished_at="2026-10-07 00:10:00",
        )
        record_job_run(
            self.db_path,
            JOB_AUTO_CHECK,
            "2026-10-09 06:00:00",
            "ok",
            summary={"api_errors": [], "price_check_errors": []},
            finished_at="2026-10-09 06:00:00",
        )

        stale = evaluate_health(self.db_path, NOW)
        save_health_states(self.db_path, stale["states"], "2026-10-09 08:00:00")
        again = evaluate_health(self.db_path, NOW)
        record_job_run(
            self.db_path,
            JOB_AUTO_CHECK,
            "2026-10-09 07:50:00",
            "ok",
            summary={"api_errors": []},
            finished_at="2026-10-09 07:50:00",
        )
        fresh = evaluate_health(self.db_path, NOW)

        self.assertEqual(
            [item["alert_key"] for item in stale["changes"]],
            ["scanner", "price_check"],
        )
        self.assertEqual(stale["changes"][0]["state"], "stale")
        self.assertEqual(stale["changes"][1]["state"], "stale")
        self.assertEqual(again["changes"], [])
        self.assertEqual([item["alert_key"] for item in fresh["changes"]], ["price_check"])
        self.assertEqual(fresh["changes"][0]["state"], "ok")

    def test_failed_health_delivery_is_retried(self):
        client = FakeTelegram()
        client.fail_times = 1
        send_due_daily_report(self.db_path, FakeTelegram(), 77, now=NOW)
        self.assertEqual(get_daily_report(self.db_path, "2026-10-08")["status"], "delivered")

        client.fail_times = 1
        failed = deliver_monitoring(
            self.db_path,
            client,
            77,
            now=NOW,
            telegram_errors=["telegram request failed"],
        )
        self.assertFalse(failed["health_sent"])
        self.assertTrue(failed["errors"])
        self.assertNotIn(TOKEN, " ".join(failed["errors"]))
        self.assertEqual(list_health_state(self.db_path), {})

        retried = deliver_monitoring(
            self.db_path,
            client,
            77,
            now=NOW,
            telegram_errors=["telegram request failed"],
        )
        quiet = deliver_monitoring(
            self.db_path,
            client,
            77,
            now=NOW,
            telegram_errors=["telegram request failed"],
        )

        self.assertTrue(retried["health_sent"])
        self.assertIn("telegram", list_health_state(self.db_path))
        self.assertFalse(quiet["health_sent"])
        health_messages = [
            item["text"] for item in client.sent
            if item["text"].startswith("CRYPTO RADAR — контроль")
        ]
        self.assertEqual(len(health_messages), 1)

    def test_no_new_events_is_distinct_from_processing_errors(self):
        quiet = build_signal_diagnostics(
            new_24h_events=0,
            sql_candidates=0,
            baseline_opened=0,
            rejections=[],
            entry_errors=[],
            price_check_errors=[],
            approval_requests_created=0,
            approval_account_missing=False,
            approval_error=None,
            telegram_delivered=0,
            telegram_undelivered=0,
        )
        broken = build_signal_diagnostics(
            new_24h_events=0,
            sql_candidates=0,
            baseline_opened=0,
            rejections=[],
            entry_errors=[],
            price_check_errors=[{"kind": "DATA_NOT_FOUND", "pair_id": 4}],
            approval_requests_created=0,
            approval_account_missing=False,
            approval_error=None,
            telegram_delivered=0,
            telegram_undelivered=0,
        )
        mixed = build_signal_diagnostics(
            new_24h_events=1,
            sql_candidates=1,
            baseline_opened=1,
            rejections=[],
            entry_errors=[],
            price_check_errors=[{"kind": "PRICE_NOT_FOUND", "pair_id": 9}],
            approval_requests_created=1,
            approval_account_missing=False,
            approval_error=None,
            telegram_delivered=0,
            telegram_undelivered=1,
        )

        self.assertEqual(quiet["outcome"], OUTCOME_NO_NEW_EVENTS)
        self.assertEqual(broken["outcome"], OUTCOME_PROCESSING_ERROR)
        self.assertEqual(mixed["outcome"], OUTCOME_PROCESSED_WITH_ERRORS)
        self.assertEqual(broken["new_24h_events"], 0)
        self.assertEqual(len(broken["processing_errors"]), 1)

    def test_rejections_do_not_open_positions_or_old_24h_checks(self):
        create_tables()
        ensure_paper_tables(self.db_path)
        low_id = self.insert_pair("low", 69, ENTRY)
        self.insert_24h(low_id, 1.1, 10, ENTRY)
        stale_id = self.insert_pair("stale", 90, ENTRY)
        self.insert_24h(stale_id, 1.1, 4, ENTRY)
        missing_change_id = self.insert_pair("missing-change", 88, ENTRY)
        self.insert_24h(missing_change_id, 1.1, None, ENTRY)
        fresh_id = self.insert_pair("fresh", 84, ENTRY)
        self.insert_24h(fresh_id, 1.04, 4, ENTRY)
        pairs_before = self.dump("pairs")
        checks_before = self.dump("price_checks")

        rejected = run_cycle(
            now=ENTRY,
            price_fetcher=lambda *args: (_ for _ in ()).throw(AssertionError("цена не нужна")),
            db_path=self.db_path,
            new_24h_pair_ids=[low_id, missing_change_id],
        )
        self.assertEqual(rejected["opened"], 0)
        self.assertEqual(rejected["new_24h_events"], 2)
        self.assertEqual(rejected["sql_candidates"], 0)
        self.assertEqual(build_signal_diagnostics(
            new_24h_events=rejected["new_24h_events"],
            sql_candidates=rejected["sql_candidates"],
            baseline_opened=rejected["opened"],
            rejections=rejected["rejections"],
            entry_errors=rejected["entry_errors"],
            price_check_errors=[],
            approval_requests_created=0,
            approval_account_missing=False,
            approval_error=None,
            telegram_delivered=0,
            telegram_undelivered=0,
        )["outcome"], OUTCOME_FILTERED)
        reasons = {item["pair_id"]: item["reason"] for item in rejected["rejections"]}
        self.assertEqual(reasons[low_id], "score_below_sql_minimum")
        self.assertEqual(reasons[missing_change_id], "price_change_missing")
        self.assertNotIn(stale_id, reasons)
        self.assertIsNone(get_paper_position(low_id, db_path=self.db_path))
        self.assertIsNone(get_paper_position(stale_id, db_path=self.db_path))
        self.assertIsNone(get_paper_position(fresh_id, db_path=self.db_path))

        opened = run_cycle(
            now=ENTRY,
            price_fetcher=lambda *args: (_ for _ in ()).throw(AssertionError("цена не нужна")),
            db_path=self.db_path,
            new_24h_pair_ids=[fresh_id],
        )
        diagnostics = build_signal_diagnostics(
            new_24h_events=opened["new_24h_events"],
            sql_candidates=opened["sql_candidates"],
            baseline_opened=opened["opened"],
            rejections=opened["rejections"],
            entry_errors=opened["entry_errors"],
            price_check_errors=[],
            approval_requests_created=0,
            approval_account_missing=True,
            approval_error=None,
            telegram_delivered=0,
            telegram_undelivered=0,
        )

        self.assertEqual(opened["opened"], 1)
        self.assertEqual(opened["sql_candidates"], 1)
        self.assertEqual(diagnostics["outcome"], OUTCOME_SIGNALS_CREATED)
        self.assertEqual(get_paper_position(fresh_id, db_path=self.db_path)["status"], "OPEN")
        self.assertEqual(self.dump("pairs"), pairs_before)
        self.assertEqual(self.dump("price_checks"), checks_before)
        self.assertIsNone(candidate_rejection_reason({
            "final_score": 80,
            "change_24h": 4,
            "entry_price": 1,
        }))
        self.assertEqual(
            candidate_rejection_reason({"final_score": 80, "change_24h": None, "entry_price": 1}),
            "change_24h_unclassified",
        )

    def test_sql_gap_read_does_not_create_rows(self):
        create_tables()
        ensure_paper_tables(self.db_path)
        pair_id = self.insert_pair("gap", 90, ENTRY)
        before_positions = self.dump("paper_positions")
        before_pairs = self.dump("pairs")

        gaps = explain_sql_gaps([pair_id], [], 70, db_path=self.db_path)

        self.assertEqual(gaps, [{"pair_id": pair_id, "reason": "no_24h_check"}])
        self.assertEqual(self.dump("paper_positions"), before_positions)
        self.assertEqual(self.dump("pairs"), before_pairs)

    def test_entry_error_is_recorded_without_a_position(self):
        create_tables()
        ensure_paper_tables(self.db_path)
        pair_id = self.insert_pair("boom", 90, ENTRY)
        self.insert_24h(pair_id, 1.04, 4, ENTRY)

        with patch("paper_engine.open_baseline_position", side_effect=RuntimeError("db")):
            stats = run_cycle(
                now=ENTRY,
                price_fetcher=lambda *args: 1,
                db_path=self.db_path,
                new_24h_pair_ids=[pair_id],
            )

        self.assertEqual(stats["opened"], 0)
        self.assertEqual(stats["sql_candidates"], 1)
        self.assertEqual(stats["entry_errors"][0]["error_type"], "RuntimeError")
        self.assertEqual(stats["entry_errors"][0]["reason"], "entry_error")
        self.assertIsNone(get_paper_position(pair_id, db_path=self.db_path))
        filtered = build_signal_diagnostics(
            new_24h_events=stats["new_24h_events"],
            sql_candidates=stats["sql_candidates"],
            baseline_opened=stats["opened"],
            rejections=stats["rejections"],
            entry_errors=stats["entry_errors"],
            price_check_errors=[],
            approval_requests_created=None,
            approval_account_missing=False,
            approval_error=None,
            telegram_delivered=None,
            telegram_undelivered=None,
        )
        self.assertEqual(filtered["outcome"], OUTCOME_PROCESSED_WITH_ERRORS)

    def test_observation_does_not_create_approval_requests(self):
        create_tables()
        ensure_paper_tables(self.db_path)
        activate_approval_account(ENTRY, db_path=self.db_path)
        pair_id = self.insert_pair("approval", 90, ENTRY)
        open_baseline_position(
            pair_id=pair_id,
            strategy_version=STRATEGY_VERSION,
            signal_type="CONFIRMED",
            final_score=90,
            change_24h=4,
            entry_price=1,
            entry_time=format_datetime(ENTRY),
            observation_bucket=format_datetime(ENTRY),
            stop_loss_percent=15,
            trailing_start_percent=10,
            trailing_distance_percent=5,
            max_hold_hours=168,
            created_at=format_datetime(ENTRY),
            db_path=self.db_path,
        )
        approval = run_approval_maintenance(ENTRY, db_path=self.db_path)
        before = self.query("SELECT * FROM approval_requests ORDER BY id")
        requests_before = len(before)

        diagnostics = observe_auto_check(
            self.db_path,
            format_datetime(ENTRY),
            [pair_id],
            [],
            {
                "opened": 1,
                "sql_candidates": 1,
                "rejections": [],
                "entry_errors": [],
            },
            None,
            approval,
            None,
            approval_created_at=format_datetime(ENTRY),
        )
        delivered, undelivered = telegram_delivery_counts(
            self.db_path,
            format_datetime(ENTRY),
        )

        self.assertGreaterEqual(approval["requests_created"], 1)
        self.assertEqual(len(self.query("SELECT id FROM approval_requests")), requests_before)
        self.assertEqual(diagnostics["approval_requests_created"], approval["requests_created"])
        self.assertEqual(diagnostics["telegram_delivered"], delivered)
        self.assertEqual(diagnostics["telegram_undelivered"], undelivered)
        self.assertEqual(diagnostics["telegram_delivered"], 0)
        self.assertEqual(diagnostics["telegram_undelivered"], approval["requests_created"])
        self.assertEqual(self.query("SELECT id FROM paper_positions"), self.query(
            "SELECT id FROM paper_positions"
        ))

    def test_unavailable_approval_journal_is_not_reported_as_zero(self):
        delivered, undelivered = telegram_delivery_counts(self.db_path, format_datetime(ENTRY))
        diagnostics = observe_auto_check(
            self.db_path,
            format_datetime(ENTRY),
            [],
            [{"kind": "DATA_NOT_FOUND", "pair_id": 3}],
            {"opened": 0, "sql_candidates": 0, "rejections": [], "entry_errors": []},
            None,
            None,
            RuntimeError("approval"),
        )

        self.assertEqual((delivered, undelivered), (None, None))
        self.assertIsNone(diagnostics["approval_requests_created"])
        self.assertIsNone(diagnostics["telegram_delivered"])
        self.assertIsNone(diagnostics["telegram_undelivered"])
        self.assertEqual(diagnostics["outcome"], OUTCOME_PROCESSING_ERROR)
        run = latest_job_run(self.db_path, JOB_AUTO_CHECK)
        self.assertEqual(run["status"], "error")
        self.assertEqual(run["summary"]["diagnostics"]["outcome"], OUTCOME_PROCESSING_ERROR)
        self.assertNotIn(TOKEN, run["error_text"] or "")

    def test_scanner_api_error_is_counted_without_the_response_text(self):
        def fail(*args, **kwargs):
            raise requests.exceptions.RequestException("body {0}".format(TOKEN))

        with patch("scanner.requests.get", side_effect=fail):
            with patch("sys.stdout", new_callable=StringIO) as output:
                result = scanner.search_pairs("SOL/USDC")
                printed = output.getvalue()

        errors = scanner.consume_api_errors()
        self.assertEqual(result, {"pairs": []})
        self.assertEqual(errors, [{"source": "search", "kind": "request_error"}])
        self.assertNotIn(TOKEN, printed)

        with patch("auto_scan._scan_once", return_value={"saved_count": 0}):
            auto_scan.main()
        run = latest_job_run(self.db_path, JOB_SCANNER)
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["summary"]["saved_count"], 0)

    def test_monitor_tables_do_not_change_existing_schema(self):
        create_tables()
        ensure_paper_tables(self.db_path)
        before = {
            name: self.columns(name)
            for name in ("pairs", "price_checks", "watchlist", "paper_positions")
        }
        record_job_run(
            self.db_path,
            JOB_SCANNER,
            "2026-10-08 00:00:00",
            "ok",
            summary={"api_errors": []},
        )
        after = {
            name: self.columns(name)
            for name in before
        }
        self.assertEqual(after, before)
        self.assertIn("monitor_job_runs", self.table_names())
        self.assertIn("monitor_daily_reports", self.table_names())
        self.assertIn("monitor_health_state", self.table_names())

    def seed_trading_day(self):
        create_tables()
        ensure_paper_tables(self.db_path)
        ensure_portfolio_tables(self.db_path)
        from human_approval import ensure_approval_tables
        ensure_approval_tables(self.db_path)
        inside = datetime(2026, 10, 8, 12, 0, 0)
        outside = datetime(2026, 10, 9, 1, 0, 0)
        idea_id = self.insert_pair("idea", 80, inside)
        self.insert_pair("tomorrow", 80, outside)
        self.insert_24h(idea_id, 1.04, 4, inside)
        self.insert_24h(idea_id, 1.10, 10, outside, period="1h")
        opened_id = self.insert_position(idea_id, inside, "OPEN", None)
        closed_pair = self.insert_pair("closed", 80, datetime(2026, 10, 7, 12, 0, 0))
        closed_id = self.insert_position(
            closed_pair,
            datetime(2026, 10, 7, 12, 0, 0),
            "CLOSED",
            inside,
        )
        self.insert_account(1, 9000)
        self.insert_account(2, 10000)
        self.insert_allocation(1, opened_id, idea_id, "OPEN", 125, 25, 0)
        self.insert_allocation(1, closed_id, closed_pair, "CLOSED", 0, 0, 40)
        self.insert_allocation(2, opened_id, idea_id, "OPEN", 110, 10, 0)
        self.insert_request(opened_id, idea_id, "PENDING", inside)
        self.insert_request(closed_id, closed_pair, "BUY", inside)
        self.price_checks = self.dump("price_checks")

    def insert_pair(self, address, final_score, created_at):
        connection = sqlite3.connect(self.db_path)
        cursor = connection.cursor()
        cursor.execute("""
            INSERT INTO pairs (
                chain_id, dex_id, pair_address, pair_symbol,
                base_symbol, quote_symbol, price_usd, final_score, created_at
            )
            VALUES ('solana', 'raydium', ?, 'ABC/USDC', 'ABC', 'USDC', 1, ?, ?)
        """, (address, final_score, format_datetime(created_at)))
        pair_id = cursor.lastrowid
        connection.commit()
        connection.close()
        return pair_id

    def insert_24h(self, pair_id, new_price, change_percent, checked_at, period="24h"):
        connection = sqlite3.connect(self.db_path)
        connection.execute("""
            INSERT INTO price_checks (
                pair_id, check_period, old_price_usd, new_price_usd,
                price_change_percent, checked_at
            )
            VALUES (?, ?, 1, ?, ?, ?)
        """, (pair_id, period, new_price, change_percent, format_datetime(checked_at)))
        connection.commit()
        connection.close()

    def insert_position(self, pair_id, created_at, status, exit_time):
        connection = sqlite3.connect(self.db_path)
        cursor = connection.cursor()
        cursor.execute("""
            INSERT INTO paper_positions (
                pair_id, strategy_version, status, entry_price, entry_time,
                created_at, updated_at, exit_time
            )
            VALUES (?, ?, ?, 1, ?, ?, ?, ?)
        """, (
            pair_id,
            STRATEGY_VERSION,
            status,
            format_datetime(created_at),
            format_datetime(created_at),
            format_datetime(created_at),
            None if exit_time is None else format_datetime(exit_time),
        ))
        position_id = cursor.lastrowid
        connection.commit()
        connection.close()
        return position_id

    def insert_account(self, portfolio_id, cash):
        connection = sqlite3.connect(self.db_path)
        moment = "2026-10-01 00:00:00"
        connection.execute("""
            INSERT INTO paper_account (
                id, currency, initial_deposit_usd, cash_usd, activated_at,
                peak_equity_usd, max_drawdown_percent, created_at, updated_at
            )
            VALUES (?, 'USD', ?, ?, ?, ?, 0, ?, ?)
        """, (portfolio_id, cash, cash, moment, cash, moment, moment))
        connection.commit()
        connection.close()

    def insert_allocation(
        self,
        portfolio_id,
        position_id,
        pair_id,
        status,
        market_value,
        unrealized,
        realized,
    ):
        connection = sqlite3.connect(self.db_path)
        moment = "2026-10-08 12:00:00"
        connection.execute("""
            INSERT INTO paper_allocations (
                portfolio_id, position_id, pair_id, status,
                market_value_usd, unrealized_pnl_usd, realized_pnl_usd,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            portfolio_id,
            position_id,
            pair_id,
            status,
            market_value,
            unrealized,
            realized,
            moment,
            moment,
        ))
        connection.commit()
        connection.close()

    def insert_request(self, position_id, pair_id, status, created_at, allow_duplicate=True):
        if not allow_duplicate:
            return
        connection = sqlite3.connect(self.db_path)
        connection.execute("""
            INSERT INTO approval_requests (
                position_id, pair_id, portfolio_id, status, created_at
            )
            VALUES (?, ?, 2, ?, ?)
        """, (position_id, pair_id, status, format_datetime(created_at)))
        connection.commit()
        connection.close()

    def dump(self, table_name):
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT * FROM {0} ORDER BY id".format(table_name)
        ).fetchall()
        connection.close()
        return rows

    def columns(self, table_name):
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "PRAGMA table_info({0})".format(table_name)
        ).fetchall()
        connection.close()
        return [row[1] for row in rows]

    def table_names(self):
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        connection.close()
        return [row[0] for row in rows]

    def query(self, sql):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        rows = connection.execute(sql).fetchall()
        connection.close()
        return [dict(row) for row in rows]


if __name__ == "__main__":
    unittest.main()
