import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

import auto_scan
import database
import human_approval
import new_pairs_discovery
import paper_engine
import scoring
from coingecko_discovery import COINGECKO_DEMO_API_KEY_ENV, COINGECKO_MONTHLY_BUDGET
from config import MIN_LIQUIDITY_USD
from discovery_store import (
    SOURCE_COINGECKO,
    SOURCE_GECKO,
    SOURCE_LEGACY,
    _keep_score,
    append_market_snapshot,
    ensure_discovery_tables,
    get_candidate,
    get_market_snapshots,
    get_network_state,
    upsert_candidate,
)
from monitor_store import latest_job_run
from network_map import (
    COINGECKO_DISCOVERY_NETWORKS,
    coingecko_discovery_network_ids,
    discovery_networks,
    mapping_for_network,
)
from new_pairs_discovery import (
    DISCOVERY_OVERLAP,
    INITIAL_LOOKBACK,
    JOB_NEW_PAIRS_DISCOVERY,
    MAX_PAGES_PER_NETWORK,
    empty_summary,
    format_auth_report,
    format_report,
    run_authenticated_discovery,
    run_discovery,
)
from scoring import (
    calculate_final_score,
    calculate_potential_score,
    calculate_risk_score,
)
from stablecoins import canonical_quote_address
from test_coingecko_auth_discovery import cg_pool, dex_pair, evm_address
from test_new_pairs_discovery import gecko_pool, sol_address


CREATED = "2026-10-09T11:45:00Z"
NOW = datetime(2026, 10, 9, 12, 0, 0)
LATER = datetime(2026, 10, 9, 12, 20, 0)
SECRET = "cg-demo-observation-secret"
SNAPSHOT_COLUMNS = {
    "id",
    "candidate_id",
    "observed_at",
    "price_usd",
    "liquidity_usd",
    "volume_24h",
    "price_change_24h",
    "risk_score",
    "potential_score",
    "final_score",
    "status",
    "reason",
}


class SnapshotSchemaTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "snapshots.db")
        self.original_db_name = database.DB_NAME
        database.DB_NAME = self.db_path

    def tearDown(self):
        database.DB_NAME = self.original_db_name
        self.temp_dir.cleanup()

    def test_table_index_and_repeated_ensure_are_idempotent(self):
        ensure_discovery_tables(self.db_path)
        ensure_discovery_tables(self.db_path)

        connection = sqlite3.connect(self.db_path)
        try:
            tables = connection.execute("""
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'discovery_market_snapshots'
            """).fetchall()
            index = connection.execute("""
                SELECT sql FROM sqlite_master
                WHERE type = 'index'
                  AND name = 'idx_discovery_market_snapshots_candidate_observed'
            """).fetchone()
            triggers = connection.execute("""
                SELECT name FROM sqlite_master
                WHERE type = 'trigger' AND tbl_name = 'discovery_market_snapshots'
                ORDER BY name
            """).fetchall()
            columns = [
                row[1]
                for row in connection.execute("PRAGMA table_info(discovery_market_snapshots)")
            ]
        finally:
            connection.close()

        self.assertEqual(len(tables), 1)
        self.assertIsNotNone(index)
        self.assertIn("UNIQUE", index[0])
        self.assertIn("candidate_id", index[0])
        self.assertIn("observed_at", index[0])
        self.assertEqual(columns, [
            "id",
            "candidate_id",
            "observed_at",
            "price_usd",
            "liquidity_usd",
            "volume_24h",
            "price_change_24h",
            "risk_score",
            "potential_score",
            "final_score",
            "status",
            "reason",
        ])
        self.assertEqual(
            [row[0] for row in triggers],
            [
                "trg_discovery_market_snapshots_no_delete",
                "trg_discovery_market_snapshots_no_update",
            ],
        )
        source = inspect.getsource(ensure_discovery_tables)
        self.assertNotIn("DROP TABLE", source)
        writer = inspect.getsource(append_market_snapshot)
        self.assertIn("INSERT INTO discovery_market_snapshots", writer)
        self.assertNotIn("UPDATE", writer)
        self.assertNotIn("DELETE", writer)
        self.assertEqual(get_market_snapshots(1, db_path=self.db_path), [])

    def test_old_candidates_survive_and_snapshots_are_append_only(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute("""
            CREATE TABLE discovery_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                source_network TEXT NOT NULL,
                pool_address TEXT NOT NULL,
                chain_id TEXT,
                pair_address TEXT,
                base_token_address TEXT,
                quote_token_address TEXT,
                base_symbol TEXT,
                quote_symbol TEXT,
                pool_created_at TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                seen_count INTEGER NOT NULL,
                status TEXT,
                reason TEXT,
                risk_score INTEGER,
                potential_score INTEGER,
                final_score INTEGER,
                pair_id INTEGER
            )
        """)
        connection.execute("""
            INSERT INTO discovery_candidates (
                id, source, source_network, pool_address,
                first_seen_at, last_seen_at, seen_count,
                status, reason, risk_score, potential_score, final_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            7,
            SOURCE_COINGECKO,
            "eth",
            evm_address(7),
            "2026-10-08 01:00:00",
            "2026-10-08 01:00:00",
            4,
            "BELOW_SCORE",
            "below_score",
            25,
            45,
            20,
        ))
        connection.commit()
        original_sql = connection.execute("""
            SELECT sql FROM sqlite_master WHERE name = 'discovery_candidates'
        """).fetchone()[0]
        connection.close()

        ensure_discovery_tables(self.db_path)
        ensure_discovery_tables(self.db_path)

        preserved = get_candidate(SOURCE_COINGECKO, "eth", evm_address(7), db_path=self.db_path)
        self.assertEqual(preserved["id"], 7)
        self.assertEqual(preserved["seen_count"], 4)
        self.assertEqual(preserved["first_seen_at"], "2026-10-08 01:00:00")
        self.assertEqual(preserved["final_score"], 20)
        self.assertEqual(preserved["status"], "BELOW_SCORE")
        self.assertEqual(get_market_snapshots(7, db_path=self.db_path), [])

        connection = sqlite3.connect(self.db_path)
        try:
            current_sql = connection.execute("""
                SELECT sql FROM sqlite_master WHERE name = 'discovery_candidates'
            """).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(current_sql, original_sql)

        append_market_snapshot(
            7,
            "2026-10-09 12:00:00",
            1.25,
            22000.0,
            900.0,
            0.12,
            25,
            45,
            20,
            "BELOW_SCORE",
            "below_score",
            db_path=self.db_path,
        )
        with self.assertRaises(sqlite3.IntegrityError):
            append_market_snapshot(
                7,
                "2026-10-09 12:00:00",
                9.0,
                1.0,
                1.0,
                1.0,
                1,
                1,
                1,
                "saved",
                None,
                db_path=self.db_path,
            )
        with self.assertRaises(sqlite3.IntegrityError):
            append_market_snapshot(
                999999,
                "2026-10-09 12:05:00",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "saved",
                None,
                db_path=self.db_path,
            )
        with self.assertRaises(ValueError):
            append_market_snapshot(
                7,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "saved",
                None,
                db_path=self.db_path,
            )

        before = get_candidate(SOURCE_COINGECKO, "eth", evm_address(7), db_path=self.db_path)
        append_market_snapshot(
            7,
            "2026-10-09 12:20:00",
            None,
            0.05,
            None,
            None,
            None,
            None,
            None,
            "invalid_liquidity",
            "invalid_liquidity",
            db_path=self.db_path,
        )
        after = get_candidate(SOURCE_COINGECKO, "eth", evm_address(7), db_path=self.db_path)
        self.assertEqual(after, before)

        rows = get_market_snapshots(7, db_path=self.db_path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["liquidity_usd"], 22000.0)
        self.assertEqual(rows[0]["final_score"], 20)
        self.assertAlmostEqual(rows[1]["liquidity_usd"], 0.05)
        self.assertIsNone(rows[1]["final_score"])

        connection = sqlite3.connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("""
                    UPDATE discovery_market_snapshots
                    SET liquidity_usd = 1
                    WHERE id = ?
                """, (rows[0]["id"],))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM discovery_market_snapshots")
        finally:
            connection.close()

        ensure_discovery_tables(self.db_path)
        untouched = get_market_snapshots(7, db_path=self.db_path)
        self.assertEqual(untouched[0], rows[0])
        self.assertEqual(untouched[1]["status"], "invalid_liquidity")
        self.assertEqual(
            get_candidate(SOURCE_COINGECKO, "eth", evm_address(7), db_path=self.db_path)["seen_count"],
            4,
        )


class FreshPoolObservationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "observation.db")
        self.original_db_name = database.DB_NAME
        database.DB_NAME = self.db_path
        database.create_tables()
        self.calls = []
        self.enrich_calls = []

    def tearDown(self):
        database.DB_NAME = self.original_db_name
        self.temp_dir.cleanup()

    def test_valid_matched_pool_stores_market_fields_scores_and_run_moment(self):
        spec = self.spec(20)
        pair = self.build_pair(spec)
        with patch.dict(os.environ, {COINGECKO_DEMO_API_KEY_ENV: SECRET}):
            summary = self.observe([spec, dict(spec)], now=NOW)

        rows = self.snapshots(spec)
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["snapshots_written"], 1)
        self.assertEqual(summary["snapshot_errors"], 0)
        self.assertEqual(summary["saved_new_ideas"], 1)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        candidate = self.candidate(spec)
        self.assertEqual(set(row), SNAPSHOT_COLUMNS)
        self.assertEqual(row["candidate_id"], candidate["id"])
        self.assertEqual(row["observed_at"], "2026-10-09 12:00:00")
        self.assertEqual(row["price_usd"], 1.25)
        self.assertEqual(row["liquidity_usd"], 150000.0)
        self.assertEqual(row["volume_24h"], 150000.0)
        self.assertEqual(row["price_change_24h"], 10.0)
        self.assertEqual(row["risk_score"], 0)
        self.assertIsNotNone(row["risk_score"])
        self.assertEqual(row["potential_score"], 80)
        self.assertEqual(row["final_score"], 80)
        self.assertEqual(row["final_score"], calculate_final_score(pair))
        self.assertEqual(row["risk_score"], calculate_risk_score(pair))
        self.assertEqual(row["potential_score"], calculate_potential_score(pair))
        self.assertEqual(row["status"], "saved")
        self.assertIsNone(row["reason"])
        self.assertEqual(candidate["seen_count"], 1)
        self.assertEqual(candidate["final_score"], 80)
        self.assertEqual(candidate["status"], "saved")
        self.assertNotIn(SECRET, json.dumps(rows))
        self.assertNotIn(SECRET, format_auth_report(summary))
        self.assertEqual(self.pair_count(), 1)
        self.assertEqual(self.table_count("watchlist"), 1)
        self.assertIsNone(self.table_count("paper_positions"))

    def test_invalid_liquidity_keeps_observation_and_does_not_copy_score(self):
        spec = self.spec(30)
        self.observe([spec], now=NOW)
        kept = dict(self.candidate(spec))
        first = dict(self.snapshots(spec)[0])
        degraded = dict(spec)
        degraded["liquidity_usd"] = 0.05
        summary = self.observe([degraded], now=LATER)

        rows = self.snapshots(spec)
        current = self.candidate(spec)
        self.assertEqual(summary["invalid_liquidity"], 1)
        self.assertEqual(summary["snapshots_written"], 1)
        self.assertEqual(summary["dex_enriched"], 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], first)
        self.assertEqual(rows[1]["status"], "invalid_liquidity")
        self.assertEqual(rows[1]["reason"], "invalid_liquidity")
        self.assertAlmostEqual(rows[1]["liquidity_usd"], 0.05)
        self.assertEqual(rows[1]["price_usd"], 1.25)
        self.assertIsNone(rows[1]["risk_score"])
        self.assertIsNone(rows[1]["potential_score"])
        self.assertIsNone(rows[1]["final_score"])
        self.assertEqual(current["final_score"], kept["final_score"])
        self.assertEqual(current["risk_score"], kept["risk_score"])
        self.assertEqual(current["potential_score"], kept["potential_score"])
        self.assertEqual(current["final_score"], 80)
        self.assertEqual(current["status"], "invalid_liquidity")
        self.assertEqual(current["seen_count"], 2)
        self.assertEqual(self.pair_count(), 1)
        self.assertEqual(rows[1]["observed_at"], "2026-10-09 12:20:00")

    def test_invalid_price_stores_null_price_and_null_scores(self):
        fresh = self.spec(40, dex="bad_price")
        summary = self.observe([fresh], now=NOW)
        row = self.snapshots(fresh)[0]
        self.assertEqual(summary["invalid_price"], 1)
        self.assertEqual(summary["snapshots_written"], 1)
        self.assertEqual(summary["saved_new_ideas"], 0)
        self.assertIsNone(row["price_usd"])
        self.assertEqual(row["status"], "invalid_price")
        self.assertEqual(row["reason"], "invalid_price")
        self.assertIsNone(row["risk_score"])
        self.assertIsNone(row["potential_score"])
        self.assertIsNone(row["final_score"])
        self.assertEqual(self.pair_count(), 0)
        self.assertIsNone(self.candidate(fresh)["final_score"])

        seeded = self.spec(41)
        self.observe([seeded], now=NOW)
        kept = dict(self.candidate(seeded))
        missing_price = dict(seeded)
        missing_price["dex"] = "bad_price"
        self.observe([missing_price], now=LATER)
        history = self.snapshots(seeded)
        self.assertEqual(history[0]["final_score"], 80)
        self.assertIsNone(history[1]["price_usd"])
        self.assertEqual(history[1]["status"], "invalid_price")
        self.assertIsNone(history[1]["final_score"])
        self.assertIsNone(history[1]["risk_score"])
        self.assertIsNone(history[1]["potential_score"])
        self.assertEqual(self.candidate(seeded)["final_score"], kept["final_score"])

    def test_repeated_observation_appends_history_without_rewriting_the_first_row(self):
        spec = self.spec(50)
        self.observe([spec], now=NOW)
        first = dict(self.snapshots(spec)[0])
        again = dict(spec)
        again["liquidity_usd"] = 22000
        summary = self.observe([again], now=LATER)

        rows = self.snapshots(spec)
        pair = self.build_pair(again)
        candidate = self.candidate(spec)
        self.assertEqual(summary["snapshots_written"], 1)
        self.assertEqual(summary["below_score"], 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], first)
        self.assertEqual(rows[0]["liquidity_usd"], 150000.0)
        self.assertEqual(rows[0]["final_score"], 80)
        self.assertEqual(rows[0]["status"], "saved")
        self.assertEqual(rows[1]["liquidity_usd"], 22000.0)
        self.assertEqual(rows[1]["risk_score"], 25)
        self.assertEqual(rows[1]["potential_score"], 70)
        self.assertEqual(rows[1]["final_score"], 45)
        self.assertEqual(rows[1]["final_score"], calculate_final_score(pair))
        self.assertEqual(rows[1]["status"], "BELOW_SCORE")
        self.assertEqual(rows[0]["observed_at"], "2026-10-09 12:00:00")
        self.assertEqual(rows[1]["observed_at"], "2026-10-09 12:20:00")
        self.assertNotEqual(rows[0]["id"], rows[1]["id"])
        self.assertEqual(candidate["seen_count"], 2)
        self.assertEqual(candidate["first_seen_at"], "2026-10-09 12:00:00")
        self.assertEqual(candidate["last_seen_at"], "2026-10-09 12:20:00")
        self.assertEqual(candidate["final_score"], 45)

    def test_each_candidate_has_its_own_history(self):
        first = self.spec(60)
        second = self.spec(61, liquidity_usd=22000)
        summary = self.observe([first, second], now=NOW)
        left = self.candidate(first)
        right = self.candidate(second)
        left_rows = self.snapshots(first)
        right_rows = self.snapshots(second)

        self.assertEqual(summary["snapshots_written"], 2)
        self.assertNotEqual(left["id"], right["id"])
        self.assertEqual(len(left_rows), 1)
        self.assertEqual(len(right_rows), 1)
        self.assertEqual(left_rows[0]["candidate_id"], left["id"])
        self.assertEqual(right_rows[0]["candidate_id"], right["id"])
        self.assertEqual(left_rows[0]["liquidity_usd"], 150000.0)
        self.assertEqual(right_rows[0]["liquidity_usd"], 22000.0)
        self.assertEqual(left_rows[0]["final_score"], 80)
        self.assertEqual(right_rows[0]["final_score"], 45)
        self.assertEqual(get_market_snapshots(left["id"]), left_rows)
        self.assertEqual(get_market_snapshots(right["id"]), right_rows)

    def test_absent_market_fields_stay_null_and_real_zero_stays_zero(self):
        missing = self.spec(70, drop_volume=True, drop_change=True)
        explicit_zero = self.spec(71, volume_h24=0, price_change=0)
        zero_liquidity = self.spec(72, liquidity_usd=0)
        absent_liquidity = self.spec(73, liquidity_usd=None)
        text_liquidity = self.spec(74, liquidity_usd="30012.50")
        summary = self.observe([
            missing,
            explicit_zero,
            zero_liquidity,
            absent_liquidity,
            text_liquidity,
        ], now=NOW)

        missing_row = self.snapshots(missing)[0]
        zero_row = self.snapshots(explicit_zero)[0]
        zero_liq_row = self.snapshots(zero_liquidity)[0]
        absent_row = self.snapshots(absent_liquidity)[0]
        text_row = self.snapshots(text_liquidity)[0]

        self.assertEqual(summary["snapshots_written"], 5)
        self.assertEqual(summary["snapshot_errors"], 0)
        self.assertIsNone(missing_row["volume_24h"])
        self.assertIsNone(missing_row["price_change_24h"])
        self.assertEqual(missing_row["price_usd"], 1.25)
        self.assertEqual(missing_row["final_score"], 0)
        self.assertIsNotNone(missing_row["final_score"])
        self.assertEqual(missing_row["risk_score"], calculate_risk_score(self.build_pair(missing)))
        self.assertEqual(zero_row["volume_24h"], 0.0)
        self.assertEqual(zero_row["price_change_24h"], 0.0)
        self.assertIsNotNone(zero_row["final_score"])
        self.assertEqual(zero_liq_row["status"], "invalid_liquidity")
        self.assertEqual(zero_liq_row["liquidity_usd"], 0.0)
        self.assertIsNone(zero_liq_row["final_score"])
        self.assertEqual(absent_row["status"], "invalid_liquidity")
        self.assertIsNone(absent_row["liquidity_usd"])
        self.assertIsNone(absent_row["final_score"])
        self.assertEqual(text_row["status"], "invalid_liquidity")
        self.assertEqual(text_row["liquidity_usd"], 30012.5)
        self.assertIsNone(text_row["final_score"])
        self.assertIsNone(text_row["risk_score"])
        self.assertIsNone(text_row["potential_score"])

    def test_downstream_statuses_snapshot_without_trading_side_effects(self):
        early = self.spec(80, created="2026-10-09T10:00:00Z")
        middle = self.spec(81, created="2026-10-09T11:00:00Z")
        late = self.spec(82, created="2026-10-09T12:00:00Z")
        held = self.spec(83, created="2026-10-09T13:00:00Z")
        low = self.spec(84, profile="65", created="2026-10-09T09:00:00Z")
        existing = self.spec(85)
        self.insert_pair(existing["address"], existing["base_addr"])

        with patch("paper_engine.run_cycle", side_effect=AssertionError("paper")), \
             patch("human_approval.decide_buy", side_effect=AssertionError("buy")), \
             patch("human_approval.decide_skip", side_effect=AssertionError("skip")):
            summary = self.observe([early, middle, late, held, low, existing], now=NOW)

        self.assertEqual(summary["saved_new_ideas"], 3)
        self.assertEqual(summary["eligible_score"], 5)
        self.assertEqual(summary["existing_pairs"], 1)
        self.assertEqual(summary["below_score"], 1)
        self.assertEqual(summary["snapshots_written"], 6)
        self.assertEqual(summary["snapshot_errors"], 0)
        self.assertEqual(self.snapshots(early)[0]["status"], "saved")
        self.assertEqual(self.snapshots(middle)[0]["status"], "saved")
        self.assertEqual(self.snapshots(late)[0]["status"], "saved")
        self.assertEqual(self.snapshots(early)[0]["final_score"], 80)
        held_row = self.snapshots(held)[0]
        self.assertEqual(held_row["status"], "eligible_not_saved")
        self.assertEqual(held_row["reason"], "max_new_ideas")
        self.assertEqual(held_row["final_score"], 80)
        low_row = self.snapshots(low)[0]
        self.assertEqual(low_row["status"], "BELOW_SCORE")
        self.assertEqual(low_row["reason"], "below_score")
        self.assertEqual(low_row["final_score"], 65)
        existing_row = self.snapshots(existing)[0]
        self.assertEqual(existing_row["status"], "existing_pair")
        self.assertEqual(existing_row["final_score"], 80)
        self.assertEqual(self.pair_count(), 4)
        self.assertEqual(self.table_count("watchlist"), 3)
        self.assertIsNone(self.table_count("paper_positions"))
        self.assertIsNone(self.table_count("approval_requests"))
        self.assertNotIn(held["address"], [row[0] for row in self.pair_rows()])

    def test_rejections_without_a_matched_market_do_not_snapshot(self):
        unsupported = self.spec(90, quote="WETH")
        unverified = self.spec(91, quote_addr=evm_address(7777))
        missing = self.spec(92, dex="missing")
        mismatched = self.spec(93, dex="mismatch")
        quote_rejected = self.spec(94, dex="quote_mismatch")
        dex_unverified = self.spec(95, dex="bad_quote_address")
        blank = self.spec(96, address="")
        summary = self.observe([
            unsupported,
            unverified,
            missing,
            mismatched,
            quote_rejected,
            dex_unverified,
            blank,
        ], now=NOW)

        self.assertEqual(summary["snapshots_written"], 0)
        self.assertEqual(summary["snapshot_errors"], 0)
        self.assertEqual(summary["unsupported_quote"], 1)
        self.assertEqual(summary["unverified_quote"], 1)
        self.assertEqual(summary["dex_not_found"], 4)
        self.assertEqual(summary["saved_new_ideas"], 0)
        self.assertEqual(self.all_snapshots(), [])
        self.assertEqual(self.pair_count(), 0)
        self.assertEqual(self.candidate(unsupported)["status"], "unsupported_quote")
        self.assertEqual(self.candidate(unverified)["reason"], "unverified_quote_token")
        self.assertEqual(self.candidate(missing)["status"], "DEX_PAIR_NOT_FOUND")
        self.assertEqual(self.candidate(mismatched)["reason"], "dex_pair_not_found")
        self.assertEqual(self.candidate(quote_rejected)["reason"], "dex_quote_rejected")
        self.assertEqual(self.candidate(dex_unverified)["status"], "unverified_quote_token")
        self.assertIsNone(self.candidate(blank))
        enriched = {address for _chain, address in self.enrich_calls}
        self.assertNotIn(unsupported["address"], enriched)
        self.assertNotIn(unverified["address"], enriched)
        self.assertNotIn("", enriched)
        self.assertIn(missing["address"], enriched)
        self.assertIn(mismatched["address"], enriched)
        self.assertIn(quote_rejected["address"], enriched)
        self.assertIn(dex_unverified["address"], enriched)

        bitcoin = self.spec(97, network="bitcoin", quote="USDC")

        def boom(chain, address):
            raise AssertionError("enrichment")

        legacy = self.observe_legacy(
            [bitcoin],
            networks=["bitcoin"],
            now=NOW,
            enrich_pair=boom,
        )
        self.assertEqual(legacy["unsupported_network"], 1)
        self.assertEqual(legacy["snapshots_written"], 0)
        self.assertEqual(self.candidate(bitcoin, SOURCE_GECKO)["status"], "unsupported_network")
        self.assertEqual(get_market_snapshots(self.candidate(bitcoin, SOURCE_GECKO)["id"]), [])

    def test_dry_run_does_not_create_or_insert_snapshots(self):
        spec = self.spec(110)
        before = self.db_snapshot()
        dry = self.observe([spec], dry_run=True, now=NOW)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["eligible_score"], 1)
        self.assertEqual(dry["snapshots_written"], 0)
        self.assertEqual(dry["snapshot_errors"], 0)
        self.assertEqual(dry["saved_new_ideas"], 0)
        self.assertEqual(self.db_snapshot(), before)
        self.assertIsNone(self.all_snapshots())
        self.assertIsNone(self.candidate(spec))
        self.assertIsNone(get_network_state("eth"))
        self.assertEqual(self.usage_count(), 0)
        report = format_auth_report(dry)
        self.assertIn("Market snapshots written: 0", report)
        self.assertIn("Snapshot errors: 0", report)
        self.assertIn("Режим: dry-run. База не изменяется.", report)

        legacy_spec = self.spec(111, network="solana")
        legacy_before = self.db_snapshot()
        legacy = self.observe_legacy([legacy_spec], dry_run=True, now=NOW)
        self.assertTrue(legacy["dry_run"])
        self.assertEqual(legacy["snapshots_written"], 0)
        self.assertEqual(self.db_snapshot(), legacy_before)
        self.assertIn("Market snapshots written:", format_report(legacy))
        self.assertIn("Snapshot errors:", format_report(legacy))

        live = self.observe([spec], now=NOW)
        self.assertEqual(live["snapshots_written"], 1)
        frozen = self.db_snapshot()
        second = self.observe([spec], dry_run=True, now=LATER, reset_schedule=False)
        self.assertEqual(second["snapshots_written"], 0)
        self.assertEqual(self.db_snapshot(), frozen)
        self.assertEqual(len(self.snapshots(spec)), 1)
        self.assertEqual(self.candidate(spec)["seen_count"], 1)

    def test_snapshot_failure_is_counted_and_does_not_stop_the_run(self):
        spec = self.spec(120)
        with patch(
            "new_pairs_discovery.append_market_snapshot",
            side_effect=RuntimeError("snapshot failed"),
        ):
            summary = self.observe([spec], now=NOW)

        self.assertEqual(summary["snapshot_errors"], 1)
        self.assertEqual(summary["snapshots_written"], 0)
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(summary["saved_new_ideas"], 1)
        self.assertTrue(summary["coverage_complete"])
        self.assertEqual(summary["pagination"]["eth"]["stop_reason"], "empty_page")
        self.assertEqual(summary["pagination"]["eth"]["pages_fetched"], 2)
        self.assertEqual(self.calls, [("eth", 1), ("eth", 2)])
        self.assertEqual(self.usage_count(), 2)
        self.assertEqual(self.candidate(spec)["status"], "saved")
        self.assertEqual(self.candidate(spec)["final_score"], 80)
        self.assertEqual(self.candidate(spec)["seen_count"], 1)
        self.assertEqual(self.all_snapshots(), [])
        self.assertEqual(self.pair_count(), 1)
        self.assertIsNone(self.table_count("paper_positions"))
        state = get_network_state("eth")
        self.assertEqual(state["last_complete_reference"], "2026-10-09 12:00:00")
        run = latest_job_run(self.db_path, JOB_NEW_PAIRS_DISCOVERY)
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["summary"]["snapshot_errors"], 1)
        self.assertEqual(run["summary"]["snapshots_written"], 0)
        self.assertIn("Snapshot errors: 1", format_auth_report(summary))
        self.assertIn("Market snapshots written: 0", format_auth_report(summary))

    def test_report_counts_snapshots_written_and_snapshot_errors(self):
        blank = empty_summary()
        self.assertEqual(blank["snapshots_written"], 0)
        self.assertEqual(blank["snapshot_errors"], 0)
        first = self.spec(130)
        second = self.spec(131)
        summary = self.observe([first, second], now=NOW)
        report = format_auth_report(summary)
        self.assertEqual(summary["snapshots_written"], 2)
        self.assertEqual(summary["snapshot_errors"], 0)
        self.assertEqual(len(self.all_snapshots()), 2)
        self.assertIn("Market snapshots written: 2", report)
        self.assertIn("Snapshot errors: 0", report)
        run = latest_job_run(self.db_path, JOB_NEW_PAIRS_DISCOVERY)
        self.assertEqual(run["summary"]["snapshots_written"], 2)
        self.assertEqual(run["summary"]["snapshot_errors"], 0)
        self.assertEqual(self.usage_count(), 2)

    def test_shared_runner_snapshots_solana_pool_and_auto_scan_does_not(self):
        spec = self.spec(140, network="solana")
        summary = self.observe_legacy([spec], now=NOW)
        row = self.snapshots(spec, SOURCE_GECKO)[0]
        self.assertEqual(summary["source"], SOURCE_GECKO)
        self.assertEqual(summary["snapshots_written"], 1)
        self.assertEqual(summary["saved_new_ideas"], 1)
        self.assertEqual(row["status"], "saved")
        self.assertEqual(row["final_score"], 80)
        self.assertEqual(row["candidate_id"], self.candidate(spec, SOURCE_GECKO)["id"])

        pair = dex_pair(
            "solana",
            sol_address(180),
            sol_address(181),
            "SOLTEST",
            canonical_quote_address("solana", "USDC"),
            "USDC",
            "80",
        )
        with patch("auto_scan.search_pairs", return_value={"pairs": [pair]}), \
             patch(
                 "discovery_store.append_market_snapshot",
                 side_effect=AssertionError("snapshot"),
             ), \
             patch("sys.stdout", new_callable=StringIO):
            auto_scan._scan_once()
        connection = sqlite3.connect(self.db_path)
        try:
            sources = connection.execute("""
                SELECT source FROM discovery_candidates
                WHERE pool_address = ?
            """, (pair["pairAddress"],)).fetchall()
        finally:
            connection.close()
        self.assertEqual(sources, [(SOURCE_LEGACY,)])
        legacy = get_candidate(SOURCE_LEGACY, "solana", pair["pairAddress"])
        self.assertEqual(get_market_snapshots(legacy["id"]), [])
        self.assertNotIn("append_market_snapshot", inspect.getsource(auto_scan))
        self.assertNotIn("discovery_market_snapshots", inspect.getsource(auto_scan))

    def test_scoring_candidate_thresholds_and_keep_score_stay(self):
        self.assertIn(
            "final_score = potential_score - risk_score",
            inspect.getsource(scoring.calculate_final_score),
        )
        self.assertNotIn("discovery_market", inspect.getsource(scoring))
        self.assertNotIn("coingecko", inspect.getsource(scoring))
        self.assertEqual(auto_scan.MIN_FINAL_SCORE, 70)
        self.assertEqual(auto_scan.MAX_NEW_IDEAS, 3)
        self.assertEqual(paper_engine.MIN_FINAL_SCORE, 70)
        self.assertEqual(MIN_LIQUIDITY_USD, 10000)
        self.assertIsNone(_keep_score(None, None))
        self.assertEqual(_keep_score(None, 80), 80)
        self.assertEqual(_keep_score(45, 80), 45)
        self.assertEqual(_keep_score(0, 80), 0)
        keep_source = inspect.getsource(_keep_score)
        self.assertIn("if new_value is None", keep_source)
        self.assertIn("return previous_value", keep_source)
        self.assertIn("return new_value", keep_source)
        self.assertNotIn(
            "calculate_final_score",
            inspect.getsource(new_pairs_discovery._record_market_snapshot),
        )
        self.assertNotIn("calculate_risk_score", inspect.getsource(append_market_snapshot))

    def test_paper_approval_exit_budget_cadence_and_pagination_stay(self):
        self.assertEqual(paper_engine.STOP_LOSS_PERCENT, 15)
        self.assertEqual(paper_engine.TRAILING_START_PERCENT, 10)
        self.assertEqual(paper_engine.TRAILING_DISTANCE_PERCENT, 5)
        self.assertEqual(paper_engine.MAX_HOLD_HOURS, 168)
        self.assertEqual(paper_engine.EXIT_TIME, "TIME_EXIT")
        self.assertEqual(human_approval.STATUS_BUY, "BUY")
        self.assertEqual(human_approval.STATUS_SKIP, "SKIP")
        self.assertNotIn("paper_engine", inspect.getsource(new_pairs_discovery))
        self.assertNotIn("open_baseline_position", inspect.getsource(new_pairs_discovery))
        self.assertNotIn("decide_buy", inspect.getsource(new_pairs_discovery))
        self.assertNotIn("discovery_market", inspect.getsource(paper_engine))
        self.assertNotIn("discovery_market", inspect.getsource(human_approval))
        self.assertEqual(COINGECKO_MONTHLY_BUDGET, 9000)
        self.assertEqual(
            [(item["network"], item["interval_minutes"]) for item in COINGECKO_DISCOVERY_NETWORKS],
            [("eth", 20), ("base", 20), ("arc", 20), ("arbitrum", 60)],
        )
        self.assertNotIn("solana", coingecko_discovery_network_ids())
        self.assertIn("solana", discovery_networks())
        self.assertEqual(MAX_PAGES_PER_NETWORK, 3)
        self.assertEqual(DISCOVERY_OVERLAP, timedelta(minutes=15))
        self.assertEqual(INITIAL_LOOKBACK, timedelta(minutes=30))
        self.assertNotIn("append_market_snapshot", inspect.getsource(new_pairs_discovery._collect_fair))
        self.assertNotIn("append_market_snapshot", inspect.getsource(new_pairs_discovery._auth_cutoff))
        self.assertNotIn("discovery_market_snapshots", inspect.getsource(new_pairs_discovery._collect_fair))
        self.assertEqual(auto_scan.SEARCH_QUERIES, [
            "SOL/USDC",
            "ETH/USDC",
            "BTC/USDC",
            "AI/USDC",
            "RWA/USDC",
        ])

    def spec(
        self,
        seed,
        quote="USDC",
        profile="80",
        network="eth",
        address=None,
        created=CREATED,
        dex="found",
        quote_addr=None,
        **extra
    ):
        if address is None:
            address = sol_address(seed) if network == "solana" else evm_address(1000 + seed)
        if quote_addr is None:
            quote_addr = canonical_quote_address(network, quote)
        if not quote_addr:
            quote_addr = sol_address(5000 + seed) if network == "solana" else evm_address(5000 + seed)
        base_addr = sol_address(3000 + seed) if network == "solana" else evm_address(3000 + seed)
        data = {
            "network": network,
            "address": address,
            "base_symbol": "AAA{0}".format(seed),
            "quote_symbol": quote,
            "base_addr": base_addr,
            "quote_addr": quote_addr,
            "created": created,
            "profile": profile,
            "dex": dex,
        }
        data.update(extra)
        return data

    def build_pair(self, spec):
        mapping = mapping_for_network(spec["network"])
        if mapping is None or spec["dex"] == "missing":
            return None
        base = spec["base_addr"]
        quote_symbol = spec["quote_symbol"]
        quote_addr = spec["quote_addr"]
        if spec["dex"] == "mismatch":
            base = sol_address(99999) if spec["network"] == "solana" else evm_address(9)
        if spec["dex"] == "quote_mismatch":
            quote_symbol = "SOL"
        if spec["dex"] == "bad_quote_address":
            quote_addr = sol_address(4242) if spec["network"] == "solana" else evm_address(42)
        built = dex_pair(
            mapping["chain_id"],
            spec["address"],
            base,
            spec["base_symbol"],
            quote_addr,
            quote_symbol,
            spec["profile"],
            price=spec.get("price", "1.25"),
        )
        if spec["dex"] == "bad_price":
            built.pop("priceUsd", None)
        if "liquidity_usd" in spec:
            if spec["liquidity_usd"] is None:
                built.pop("liquidity", None)
            else:
                built["liquidity"] = {"usd": spec["liquidity_usd"]}
        if spec.get("drop_volume"):
            built.pop("volume", None)
        if "volume_h24" in spec:
            built["volume"] = {"h24": spec["volume_h24"]}
        if spec.get("drop_change"):
            built.pop("priceChange", None)
        if "price_change" in spec:
            built["priceChange"] = {"h24": spec["price_change"]}
        return built

    def observe(self, specs, now=None, dry_run=False, networks=None, reset_schedule=True):
        selected = []
        for spec in specs:
            if spec["network"] not in selected:
                selected.append(spec["network"])
        if networks is not None:
            selected = list(networks)
        if reset_schedule:
            self.clear_schedule(selected)
        grouped = {}
        included = {}
        pairs = {}
        for spec in specs:
            pool, pool_included = cg_pool(
                spec["network"],
                spec["address"],
                spec["base_addr"],
                spec["base_symbol"],
                spec["quote_addr"],
                spec["quote_symbol"],
                spec["created"],
            )
            grouped.setdefault(spec["network"], []).append(pool)
            included.setdefault(spec["network"], []).extend(pool_included)
            built = self.build_pair(spec)
            if built is None:
                continue
            pairs[spec["address"]] = built
            if mapping_for_network(spec["network"])["evm"]:
                pairs[spec["address"].lower()] = built

        self.calls = []
        self.enrich_calls = []

        def fetch(network, page):
            self.calls.append((network, page))
            if page != 1:
                return {
                    "ok": True,
                    "pools": [],
                    "included": [],
                    "status_code": 200,
                    "billable": True,
                }
            return {
                "ok": True,
                "pools": grouped.get(network, []),
                "included": included.get(network, []),
                "status_code": 200,
                "billable": True,
            }

        def lookup(chain, address):
            self.enrich_calls.append((chain, address))
            direct = pairs.get(address)
            if direct is not None and direct.get("chainId") == chain:
                return direct
            folded = pairs.get(str(address).lower())
            if folded is not None and folded.get("chainId") == chain:
                return folded
            return None

        return run_authenticated_discovery(
            dry_run=dry_run,
            networks=selected,
            fetch_pools=fetch,
            enrich_pair=lookup,
            now=now or NOW,
            budget_limit=9000,
        )

    def observe_legacy(self, specs, now=None, dry_run=False, networks=None, enrich_pair=None):
        selected = []
        for spec in specs:
            if spec["network"] not in selected:
                selected.append(spec["network"])
        if networks is not None:
            selected = list(networks)
        grouped_pools = {}
        grouped_included = {}
        pairs = {}
        for spec in specs:
            pool, pool_included = gecko_pool(
                spec["network"],
                spec["address"],
                spec["base_addr"],
                spec["base_symbol"],
                spec["quote_addr"],
                spec["quote_symbol"],
                spec["created"],
            )
            grouped_pools.setdefault(spec["network"], []).append(pool)
            grouped_included.setdefault(spec["network"], []).extend(pool_included)
            built = self.build_pair(spec)
            if built is not None:
                pairs[spec["address"]] = built

        def fetch(network, page):
            if page != 1:
                return {"ok": True, "pools": [], "included": []}
            return {
                "ok": True,
                "pools": grouped_pools.get(network, []),
                "included": grouped_included.get(network, []),
            }

        def lookup(chain, address):
            direct = pairs.get(address)
            if direct is not None and direct.get("chainId") == chain:
                return direct
            return None

        return run_discovery(
            dry_run=dry_run,
            networks=selected,
            fetch_pools=fetch,
            enrich_pair=enrich_pair or lookup,
            now=now or NOW,
        )

    def candidate(self, spec, source=SOURCE_COINGECKO):
        address = spec["address"]
        mapping = mapping_for_network(spec["network"])
        if mapping is not None and mapping["evm"]:
            address = address.lower()
        return get_candidate(source, spec["network"], address)

    def snapshots(self, spec, source=SOURCE_COINGECKO):
        row = self.candidate(spec, source)
        if row is None:
            return []
        return get_market_snapshots(row["id"])

    def all_snapshots(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            found = connection.execute("""
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'discovery_market_snapshots'
            """).fetchone()
            if found is None:
                return None
            rows = connection.execute("""
                SELECT * FROM discovery_market_snapshots ORDER BY id
            """).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def clear_schedule(self, networks):
        connection = sqlite3.connect(self.db_path)
        try:
            found = connection.execute("""
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'discovery_network_state'
            """).fetchone()
            if found is None:
                return
            for network in networks:
                connection.execute(
                    "DELETE FROM discovery_network_state WHERE network = ?",
                    (network,),
                )
            connection.commit()
        finally:
            connection.close()

    def insert_pair(self, pair_address, base_address, chain_id="ethereum"):
        connection = sqlite3.connect(self.db_path)
        connection.execute("""
            INSERT INTO pairs (
                chain_id, dex_id, pair_address, pair_symbol,
                base_symbol, base_token_address, quote_symbol,
                price_usd, liquidity_usd, volume_24h, price_change_24h,
                risk_score, risk_level, potential_score, final_score, url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chain_id, "uniswap", pair_address, "OLD/USDC",
            "OLD", base_address, "USDC",
            1, 20000, 20000, 10,
            0, "LOW (НИЗКИЙ)", 80, 80, "https://dexscreener.com",
        ))
        connection.commit()
        connection.close()

    def pair_rows(self):
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                "SELECT pair_address FROM pairs ORDER BY pair_address"
            ).fetchall()
        finally:
            connection.close()

    def pair_count(self):
        return len(self.pair_rows())

    def usage_count(self):
        connection = sqlite3.connect(self.db_path)
        try:
            found = connection.execute("""
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'discovery_api_usage'
            """).fetchone()
            if found is None:
                return 0
            return connection.execute("SELECT COUNT(*) FROM discovery_api_usage").fetchone()[0]
        finally:
            connection.close()

    def table_count(self, name):
        connection = sqlite3.connect(self.db_path)
        try:
            found = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ).fetchone()
            if found is None:
                return None
            return connection.execute("SELECT COUNT(*) FROM {0}".format(name)).fetchone()[0]
        finally:
            connection.close()

    def db_snapshot(self):
        connection = sqlite3.connect(self.db_path)
        try:
            tables = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
            data = {}
            for name in tables:
                data[name] = connection.execute(
                    "SELECT * FROM {0}".format(name)
                ).fetchall()
            return tables, data
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
