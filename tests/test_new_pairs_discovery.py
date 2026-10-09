import inspect
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from io import StringIO
from unittest.mock import patch

import requests

import auto_scan
import database
import gecko_discovery
import new_pairs_discovery
import paper_engine
import scoring
from config import MIN_LIQUIDITY_USD
from discovery_store import SOURCE_GECKO, SOURCE_LEGACY, get_candidate
from gecko_discovery import (
    fetch_global_new_pools,
    fetch_new_pools,
    fetch_supported_networks,
    parse_new_pool,
)
from monitor_store import latest_job_run
from network_map import mapping_for_network
from new_pairs_discovery import (
    JOB_NEW_PAIRS_DISCOVERY,
    empty_summary,
    find_existing_pair,
    format_report,
    intake_reason,
    main,
    run_discovery,
)
from scoring import (
    calculate_final_score,
    calculate_potential_score,
    calculate_risk_score,
)


FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "geckoterminal_new_pools.json",
)
FIRST_SEEN = datetime(2026, 10, 9, 12, 0, 0)
SECOND_SEEN = datetime(2026, 10, 9, 12, 10, 0)


def load_fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def sol_address(seed):
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    chars = []
    value = seed + 97
    for _index in range(44):
        chars.append(alphabet[value % len(alphabet)])
        value = (value // 7) + 13 + len(chars)
    return "".join(chars)


def evm_address(seed, upper=False):
    text = "{0:040x}".format(seed)
    if upper:
        text = text.upper()
    return "0x" + text


def market_numbers(profile):
    if profile == "80":
        return 150000, 150000, 10
    if profile == "70":
        return 60000, 150000, 10
    if profile == "65":
        return 150000, 150000, 1
    if profile == "thin":
        return 5000, 150000, 10
    raise AssertionError(profile)


def dex_pair(chain_id, pair_address, base_address, base_symbol, quote_address, quote_symbol, profile, price="1.25"):
    liquidity, volume, change = market_numbers(profile)
    return {
        "chainId": chain_id,
        "dexId": "raydium",
        "pairAddress": pair_address,
        "baseToken": {"address": base_address, "symbol": base_symbol},
        "quoteToken": {"address": quote_address, "symbol": quote_symbol},
        "priceUsd": price,
        "liquidity": {"usd": liquidity},
        "volume": {"h24": volume},
        "priceChange": {"h24": change},
        "url": "https://dexscreener.com/{0}/{1}".format(chain_id, pair_address),
    }


def gecko_pool(network, address, base_address, base_symbol, quote_address, quote_symbol, created):
    base_id = "{0}_{1}".format(network, base_address)
    quote_id = "{0}_{1}".format(network, quote_address)
    pool = {
        "id": "{0}_{1}".format(network, address),
        "type": "pool",
        "attributes": {
            "address": address,
            "name": "{0} / {1}".format(base_symbol, quote_symbol),
            "pool_created_at": created,
            "base_token_price_usd": "1.5",
            "reserve_in_usd": "22000",
            "volume_usd": {"h24": "900"},
        },
        "relationships": {
            "base_token": {"data": {"id": base_id, "type": "token"}},
            "quote_token": {"data": {"id": quote_id, "type": "token"}},
            "dex": {"data": {"id": "raydium", "type": "dex"}},
        },
    }
    included = [
        {
            "id": base_id,
            "type": "token",
            "attributes": {
                "address": base_address,
                "symbol": base_symbol,
                "name": base_symbol,
            },
        },
        {
            "id": quote_id,
            "type": "token",
            "attributes": {
                "address": quote_address,
                "symbol": quote_symbol,
                "name": quote_symbol,
            },
        },
        {"id": "raydium", "type": "dex", "attributes": {"name": "Raydium"}},
    ]
    return pool, included


class Response:
    def __init__(self, status_code, payload=None, broken=False):
        self.status_code = status_code
        self.payload = payload
        self.broken = broken

    def json(self):
        if self.broken:
            raise ValueError("malformed")
        return self.payload


class GeckoAdapterTest(unittest.TestCase):
    def test_parse_valid_gecko_pool_from_fixture(self):
        payload = load_fixture()
        pool = payload["data"][1]
        parsed = parse_new_pool(pool, payload["included"], source_network="solana")

        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["source"], SOURCE_GECKO)
        self.assertEqual(parsed["source_network"], "solana")
        self.assertEqual(parsed["pool_address"], "2sMgNrBVgni1x7kEJ98QuqWXDBhmZ1tz7C39mCodpqy8")
        self.assertEqual(parsed["pool_created_at"], "2026-10-09T15:38:51Z")
        self.assertEqual(parsed["dex"], "Pump.fun")
        self.assertEqual(parsed["base_symbol"], "DOOBIE")
        self.assertEqual(parsed["quote_symbol"], "USDC")
        self.assertEqual(parsed["base_token_address"], "9udd47zzccdC5Pr97fHGZtvge7KuUW9frCFKSbkPpump")
        self.assertEqual(parsed["quote_token_address"], "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v")
        self.assertAlmostEqual(parsed["reserve_usd"], 2451.4084)
        self.assertIsNotNone(parsed["price_usd"])
        self.assertAlmostEqual(parsed["volume_h24"], 8.9894794027)

    def test_malformed_pool_does_not_raise(self):
        parsed = parse_new_pool(["not", "a", "pool"])
        self.assertFalse(parsed["ok"])
        self.assertEqual(parsed["reason"], "malformed_pool")
        self.assertFalse(parse_new_pool({"attributes": None})["ok"])

    def test_missing_pool_address(self):
        parsed = parse_new_pool(
            {"attributes": {"name": "A / USDC"}, "relationships": {}},
            source_network="solana",
        )
        self.assertTrue(parsed["ok"])
        self.assertIsNone(parsed["pool_address"])
        self.assertEqual(intake_reason(parsed), "missing_pool_address")

    def test_missing_network(self):
        parsed = parse_new_pool({
            "attributes": {"address": sol_address(1)},
            "relationships": {},
        })
        self.assertIsNone(parsed["source_network"])
        self.assertEqual(intake_reason(parsed), "missing_network")

    def test_network_mapping_uses_proven_ids_only(self):
        self.assertEqual(mapping_for_network("solana")["chain_id"], "solana")
        self.assertFalse(mapping_for_network("solana")["evm"])
        self.assertEqual(mapping_for_network("eth")["chain_id"], "ethereum")
        self.assertEqual(mapping_for_network("arbitrum")["chain_id"], "arbitrum")
        self.assertEqual(mapping_for_network("base")["chain_id"], "base")
        self.assertEqual(mapping_for_network("arc")["chain_id"], "arc")
        self.assertTrue(mapping_for_network("arc")["evm"])
        self.assertIsNone(mapping_for_network("ethereum"))
        self.assertIsNone(mapping_for_network("polygon"))
        self.assertEqual(intake_reason({
            "source_network": "polygon",
            "pool_address": evm_address(1),
            "base_symbol": "ABC",
            "base_token_address": evm_address(2),
            "quote_symbol": "USDC",
            "quote_token_address": evm_address(3),
        }), "unsupported_network")

    def test_stable_quotes_and_rejections(self):
        self.assertIsNone(self.reason("ABC", "USDC"))
        self.assertIsNone(self.reason("ABC", "USDT"))
        self.assertIsNone(self.reason("ABC", "DAI"))
        self.assertEqual(self.reason("ABC", "SOL"), "unsupported_quote")
        self.assertEqual(self.reason("USDC", "USDT"), "reverse_pair")
        self.assertEqual(self.reason("usdc", "dai"), "reverse_pair")

    def test_fetch_new_pools_uses_network_endpoint_and_page(self):
        captured = {}

        def getter(url, params, timeout):
            captured["url"] = url
            captured["params"] = params
            captured["timeout"] = timeout
            return Response(200, {"data": [], "included": []})

        result = fetch_new_pools("eth", page=2, timeout=7, getter=getter)
        self.assertTrue(result["ok"])
        self.assertIn("/networks/eth/new_pools", captured["url"])
        self.assertNotIn("/networks/ethereum/", captured["url"])
        self.assertEqual(captured["params"]["page"], 2)
        self.assertEqual(captured["params"]["include"], "base_token,quote_token,dex")
        self.assertEqual(captured["timeout"], 7)

    def test_global_endpoint_is_available_but_separate(self):
        captured = {}

        def getter(url, params, timeout):
            captured["url"] = url
            return Response(200, {"data": [], "included": []})

        result = fetch_global_new_pools(getter=getter)
        self.assertTrue(result["ok"])
        self.assertTrue(captured["url"].endswith("/networks/new_pools"))

    def test_fetch_supported_networks_reads_pages(self):
        def getter(url, params, timeout):
            page = params["page"]
            if page == 1:
                return Response(200, {"data": [{
                    "id": "eth",
                    "type": "network",
                    "attributes": {
                        "name": "Ethereum",
                        "coingecko_asset_platform_id": "ethereum",
                    },
                }]})
            return Response(200, {"data": []})

        result = fetch_supported_networks(getter=getter)
        self.assertTrue(result["ok"])
        self.assertEqual(result["networks"][0]["id"], "eth")
        self.assertEqual(result["networks"][0]["coingecko_asset_platform_id"], "ethereum")

    def test_timeout_http_429_and_malformed_json(self):
        def timeout_getter(url, params, timeout):
            raise requests.exceptions.Timeout("slow")

        self.assertEqual(
            fetch_new_pools("solana", getter=timeout_getter)["error_kind"],
            "timeout",
        )
        self.assertEqual(
            fetch_new_pools(
                "solana",
                getter=lambda url, params, timeout: Response(503, broken=True),
            )["error_kind"],
            "server_error",
        )
        self.assertEqual(
            fetch_new_pools(
                "solana",
                getter=lambda url, params, timeout: Response(404, broken=True),
            )["error_kind"],
            "http_error",
        )
        limited = fetch_new_pools(
            "solana",
            getter=lambda url, params, timeout: Response(429, broken=True),
        )
        self.assertEqual(limited["error_kind"], "rate_limited")
        self.assertEqual(limited["status_code"], 429)
        self.assertEqual(
            fetch_new_pools(
                "solana",
                getter=lambda url, params, timeout: Response(200, broken=True),
            )["error_kind"],
            "malformed_json",
        )

    def test_empty_payload_is_success(self):
        result = fetch_new_pools(
            "base",
            getter=lambda url, params, timeout: Response(200, {"data": []}),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["pools"], [])

    def reason(self, base_symbol, quote_symbol):
        return intake_reason({
            "source_network": "solana",
            "pool_address": sol_address(3),
            "base_symbol": base_symbol,
            "base_token_address": sol_address(4),
            "quote_symbol": quote_symbol,
            "quote_token_address": sol_address(5),
        })


class DiscoveryRunTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "discovery.db")
        self.original_db_name = database.DB_NAME
        database.DB_NAME = self.db_path
        database.create_tables()

    def tearDown(self):
        database.DB_NAME = self.original_db_name
        self.temp_dir.cleanup()

    def test_quote_acceptance_reaches_enrichment_only_for_stables(self):
        specs = [
            self.spec(1, "USDC", "80"),
            self.spec(2, "USDT", "80"),
            self.spec(3, "DAI", "80"),
            self.spec(4, "SOL", "80"),
        ]
        summary = self.run_specs(specs)
        self.assertEqual(summary["supported_quote"], 3)
        self.assertEqual(summary["unsupported_quote"], 1)
        self.assertEqual(summary["saved_new_ideas"], 3)
        self.assertEqual(len(self.pair_rows()), 3)
        quotes = sorted(row[1] for row in self.pair_rows_raw())
        self.assertEqual(quotes, ["DAI", "USDC", "USDT"])

    def test_duplicate_same_pool_is_stored_once(self):
        spec = self.spec(10, "USDC", "80")
        summary = self.run_specs([spec, dict(spec)])
        self.assertEqual(summary["api_pools_received"], 2)
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["unique_pools"], 1)
        self.assertEqual(summary["saved_new_ideas"], 1)
        self.assertEqual(len(self.pair_rows()), 1)

    def test_duplicate_evm_address_ignores_case(self):
        lower = evm_address(11)
        upper = evm_address(11, upper=True)
        first = self.spec(11, "USDC", "80", network="eth", address=lower)
        second = self.spec(11, "USDC", "80", network="eth", address=upper)
        second["base_addr"] = first["base_addr"]
        second["quote_addr"] = first["quote_addr"]
        summary = self.run_specs([first, second], networks=["eth"])
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["saved_new_ideas"], 1)
        self.assertEqual(len(self.pair_rows()), 1)

    def test_non_evm_address_keeps_case(self):
        lower = "1" + ("A" * 43)
        upper = "1" + ("a" * 43)
        first = self.spec(12, "USDC", "80", address=lower)
        second = self.spec(13, "USDC", "80", address=upper)
        summary = self.run_specs([first, second])
        self.assertEqual(summary["duplicates"], 0)
        self.assertEqual(summary["saved_new_ideas"], 2)
        stored = sorted(row[0] for row in self.pair_rows())
        self.assertEqual(stored, sorted([lower, upper]))

    def test_enrichment_success_uses_dexscreener_object_for_scoring(self):
        spec = self.spec(20, "USDC", "80")
        with patch(
            "new_pairs_discovery.calculate_final_score",
            wraps=calculate_final_score,
        ) as final_score, patch(
            "new_pairs_discovery.calculate_risk_score",
            wraps=calculate_risk_score,
        ) as risk_score, patch(
            "new_pairs_discovery.calculate_potential_score",
            wraps=calculate_potential_score,
        ) as potential_score:
            summary = self.run_specs([spec])

        enriched = final_score.call_args.args[0]
        self.assertEqual(enriched["chainId"], "solana")
        self.assertEqual(enriched["pairAddress"], spec["address"])
        self.assertEqual(enriched, risk_score.call_args.args[0])
        self.assertEqual(enriched, potential_score.call_args.args[0])
        self.assertEqual(calculate_final_score(enriched), 80)
        self.assertEqual(summary["dex_enriched"], 1)
        self.assertEqual(summary["eligible_score"], 1)
        self.assertEqual(summary["saved_new_ideas"], 1)
        candidate = get_candidate(SOURCE_GECKO, "solana", spec["address"])
        self.assertEqual(candidate["source"], SOURCE_GECKO)
        self.assertEqual(candidate["final_score"], 80)
        self.assertEqual(candidate["status"], "saved")
        self.assertIsNotNone(candidate["pair_id"])

    def test_dexscreener_pair_not_found(self):
        spec = self.spec(21, "USDC", "80", dex="missing")
        summary = self.run_specs([spec])
        self.assertEqual(summary["dex_not_found"], 1)
        self.assertEqual(summary["saved_new_ideas"], 0)
        candidate = get_candidate(SOURCE_GECKO, "solana", spec["address"])
        self.assertEqual(candidate["status"], "DEX_PAIR_NOT_FOUND")
        self.assertEqual(self.pair_rows(), [])

    def test_dex_quote_disagreement_stays_out_of_the_intake_counter(self):
        spec = self.spec(22, "USDC", "80", dex="quote_mismatch")
        summary = self.run_specs([spec])
        self.assertEqual(summary["supported_quote"], 1)
        self.assertEqual(summary["unsupported_quote"], 0)
        self.assertEqual(summary["dex_not_found"], 1)
        self.assertEqual(summary["dex_enriched"], 0)
        self.assertEqual(summary["saved_new_ideas"], 0)
        candidate = get_candidate(SOURCE_GECKO, "solana", spec["address"])
        self.assertEqual(candidate["status"], "DEX_PAIR_NOT_FOUND")
        self.assertEqual(candidate["reason"], "dex_quote_rejected")

    def test_invalid_price_and_liquidity_are_not_saved(self):
        missing_price = self.spec(22, "USDC", "80", dex="bad_price")
        thin = self.spec(23, "USDC", "thin")
        summary = self.run_specs([missing_price, thin])
        self.assertEqual(summary["invalid_price"], 1)
        self.assertEqual(summary["invalid_liquidity"], 1)
        self.assertEqual(summary["dex_enriched"], 0)
        self.assertEqual(self.pair_rows(), [])

    def test_score_69_is_rejected_without_changing_formula(self):
        spec = self.spec(24, "USDC", "80")
        with patch("new_pairs_discovery.calculate_final_score", return_value=69):
            summary = self.run_specs([spec])
        self.assertEqual(summary["below_score"], 1)
        self.assertEqual(summary["eligible_score"], 0)
        self.assertEqual(summary["saved_new_ideas"], 0)
        self.assertEqual(self.pair_rows(), [])
        self.assertIn(
            "final_score = potential_score - risk_score",
            inspect.getsource(scoring.calculate_final_score),
        )

    def test_real_scores_70_and_80_follow_current_formula(self):
        high = self.spec(25, "USDC", "80", created="2026-10-09T10:00:00Z")
        border = self.spec(26, "USDC", "70", created="2026-10-09T11:00:00Z")
        low = self.spec(27, "USDC", "65", created="2026-10-09T09:00:00Z")
        summary = self.run_specs([low, border, high])
        self.assertEqual(calculate_final_score(dex_pair(
            "solana", high["address"], high["base_addr"], "AAA",
            high["quote_addr"], "USDC", "80",
        )), 80)
        self.assertEqual(calculate_final_score(dex_pair(
            "solana", border["address"], border["base_addr"], "BBB",
            border["quote_addr"], "USDC", "70",
        )), 70)
        self.assertEqual(summary["below_score"], 1)
        self.assertEqual(summary["eligible_score"], 2)
        self.assertEqual(summary["saved_new_ideas"], 2)
        stored = {row[0]: row[2] for row in self.pair_rows_raw()}
        self.assertEqual(stored[high["address"]], 80)
        self.assertEqual(stored[border["address"]], 70)
        self.assertNotIn(low["address"], stored)
        rejected = get_candidate(SOURCE_GECKO, "solana", low["address"])
        self.assertEqual(rejected["status"], "BELOW_SCORE")

    def test_max_new_ideas_and_deterministic_order(self):
        specs = [
            self.spec(31, "USDC", "80", created="2026-10-09T18:00:00Z", address="4" + ("1" * 43)),
            self.spec(32, "USDC", "80", created="2026-10-09T10:00:00Z", address="1" + ("1" * 43)),
            self.spec(33, "USDC", "80", created="2026-10-09T14:00:00Z", address="3" + ("1" * 43)),
            self.spec(34, "USDC", "80", created="2026-10-09T12:00:00Z", address="2" + ("1" * 43)),
        ]
        summary = self.run_specs(specs)
        self.assertEqual(auto_scan.MAX_NEW_IDEAS, 3)
        self.assertEqual(summary["eligible_score"], 4)
        self.assertEqual(summary["saved_new_ideas"], 3)
        self.assertEqual(
            [row[0] for row in self.pair_rows()],
            ["1" + ("1" * 43), "2" + ("1" * 43), "3" + ("1" * 43)],
        )
        held = get_candidate(SOURCE_GECKO, "solana", "4" + ("1" * 43))
        self.assertEqual(held["reason"], "max_new_ideas")

        same_time = [
            self.spec(41, "USDC", "80", created="2026-10-09T10:00:00Z", address="4" + ("2" * 43)),
            self.spec(42, "USDC", "80", created="2026-10-09T10:00:00Z", address="1" + ("2" * 43)),
            self.spec(43, "USDC", "80", created="2026-10-09T10:00:00Z", address="3" + ("2" * 43)),
            self.spec(44, "USDC", "80", created="2026-10-09T10:00:00Z", address="2" + ("2" * 43)),
        ]
        self.run_specs(same_time)
        stored = [row[0] for row in self.pair_rows() if row[0].endswith("2" * 43)]
        self.assertEqual(stored, [
            "1" + ("2" * 43),
            "2" + ("2" * 43),
            "3" + ("2" * 43),
        ])

    def test_existing_pair_and_base_token_are_not_duplicated(self):
        existing = self.spec(50, "USDC", "80")
        self.insert_pair(existing["address"], existing["base_addr"])
        summary = self.run_specs([existing])
        self.assertEqual(summary["existing_pairs"], 1)
        self.assertEqual(summary["saved_new_ideas"], 0)
        self.assertEqual(len(self.pair_rows()), 1)

        other = self.spec(51, "USDC", "80")
        self.insert_pair(sol_address(900), other["base_addr"], pair_symbol="OLD/USDC")
        with patch(
            "new_pairs_discovery.base_token_exists",
            wraps=database.base_token_exists,
        ) as guard:
            second = self.run_specs([other])
        self.assertTrue(guard.called)
        self.assertEqual(second["saved_new_ideas"], 0)
        self.assertEqual(second["existing_pairs"], 1)
        self.assertEqual(len(self.pair_rows()), 2)

    def test_evm_case_difference_does_not_rewrite_stored_address(self):
        stored = evm_address(60)
        discovered = evm_address(60, upper=True)
        spec = self.spec(60, "USDC", "80", network="eth", address=discovered)
        self.insert_pair(stored, evm_address(4242), chain_id="ethereum")
        summary = self.run_specs([spec], networks=["eth"])
        self.assertEqual(summary["saved_new_ideas"], 0)
        self.assertEqual(summary["existing_pairs"], 1)
        rows = self.pair_rows()
        self.assertEqual(rows, [(stored,)])
        self.assertEqual(
            find_existing_pair("ethereum", discovered, spec["base_addr"], True),
            "pair_address",
        )

    def test_source_first_seen_and_seen_count(self):
        spec = self.spec(70, "SOL", "80")
        self.run_specs([spec], now=FIRST_SEEN)
        self.run_specs([spec], now=SECOND_SEEN)
        candidate = get_candidate(SOURCE_GECKO, "solana", spec["address"])
        self.assertEqual(candidate["source"], SOURCE_GECKO)
        self.assertEqual(candidate["seen_count"], 2)
        self.assertEqual(candidate["first_seen_at"], "2026-10-09 12:00:00")
        self.assertEqual(candidate["last_seen_at"], "2026-10-09 12:10:00")
        self.assertEqual(candidate["status"], "unsupported_quote")
        self.assertEqual(self.pair_rows(), [])

    def test_dry_run_does_not_write_production_tables(self):
        spec = self.spec(80, "USDC", "80")
        self.insert_pair(sol_address(880), sol_address(881))
        before = self.snapshot()
        summary = self.run_specs([spec], dry_run=True)
        after = self.snapshot()
        self.assertEqual(before, after)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["eligible_score"], 1)
        self.assertEqual(summary["saved_new_ideas"], 0)
        self.assertIsNone(latest_job_run(self.db_path, JOB_NEW_PAIRS_DISCOVERY))
        self.assertNotIn("discovery_candidates", before[0])
        report = format_report(summary)
        self.assertIn("Режим: dry-run. pairs и watchlist не изменяются.", report)
        self.assertIn("dry-run", report)

    def test_normal_run_saves_pair_watchlist_and_monitor_funnel(self):
        spec = self.spec(90, "USDC", "80")
        summary = self.run_specs([spec])
        self.assertEqual(summary["saved_new_ideas"], 1)
        self.assertEqual(self.table_count("watchlist"), 1)
        connection = sqlite3.connect(self.db_path)
        note = connection.execute("SELECT note FROM watchlist").fetchone()[0]
        connection.close()
        self.assertIn("new_pairs_discovery", note)
        self.assertIn("80", note)
        run = latest_job_run(self.db_path, JOB_NEW_PAIRS_DISCOVERY)
        self.assertEqual(run["status"], "ok")
        self.assertEqual(run["summary"]["source"], SOURCE_GECKO)
        self.assertEqual(run["summary"]["api_pools_received"], 1)
        self.assertEqual(run["summary"]["unique_pools"], 1)
        self.assertEqual(run["summary"]["supported_network"], 1)
        self.assertEqual(run["summary"]["supported_quote"], 1)
        self.assertEqual(run["summary"]["dex_enriched"], 1)
        self.assertEqual(run["summary"]["eligible_score"], 1)
        self.assertEqual(run["summary"]["saved_new_ideas"], 1)
        self.assertEqual(run["summary"]["errors"], 0)
        self.assertEqual(run["summary"]["api_errors"], 0)
        self.assertNotIn("preview", run["summary"])
        report = format_report(summary)
        self.assertIn("CRYPTO RADAR — NEW PAIRS DISCOVERY", report)
        self.assertIn("GeckoTerminal pools received:", report)
        self.assertIn("New ideas saved:", report)
        self.assertIn(SOURCE_GECKO, report)

    def test_repeated_discovery_keeps_one_production_pair(self):
        spec = self.spec(91, "USDC", "80")
        self.run_specs([spec], now=FIRST_SEEN)
        self.run_specs([spec], now=SECOND_SEEN)
        self.assertEqual(len(self.pair_rows()), 1)
        self.assertEqual(self.table_count("watchlist"), 1)
        candidate = get_candidate(SOURCE_GECKO, "solana", spec["address"])
        self.assertEqual(candidate["seen_count"], 2)
        self.assertEqual(candidate["first_seen_at"], "2026-10-09 12:00:00")
        self.assertEqual(candidate["last_seen_at"], "2026-10-09 12:10:00")
        self.assertEqual(candidate["status"], "existing_pair")
        self.assertIsNotNone(candidate["pair_id"])

    def test_api_failures_and_empty_response_finish_cleanly(self):
        calls = []

        def fetch(network, page):
            calls.append(network)
            if network == "solana":
                return {"ok": False, "error_kind": "rate_limited", "pools": [], "included": []}
            return {"ok": True, "pools": [self.pool_only(self.spec(100, "USDC", "80"))], "included": []}

        summary = run_discovery(
            networks=["solana", "eth"],
            fetch_pools=fetch,
            enrich_pair=lambda chain, address: None,
            now=FIRST_SEEN,
        )
        self.assertEqual(calls, ["solana"])
        self.assertTrue(summary["rate_limited"])
        self.assertEqual(summary["api_errors"], 1)
        self.assertEqual(summary["saved_new_ideas"], 0)

        timeout = run_discovery(
            networks=["solana"],
            fetch_pools=lambda network, page: {
                "ok": False,
                "error_kind": "timeout",
                "pools": [],
                "included": [],
            },
            enrich_pair=lambda chain, address: None,
            now=FIRST_SEEN,
        )
        self.assertEqual(timeout["api_errors"], 1)
        self.assertEqual(timeout["api_error_kinds"][0]["kind"], "timeout")

        http_error = run_discovery(
            networks=["base"],
            fetch_pools=lambda network, page: {
                "ok": False,
                "error_kind": "server_error",
                "pools": [],
                "included": [],
            },
            enrich_pair=lambda chain, address: None,
            now=FIRST_SEEN,
        )
        self.assertEqual(http_error["api_error_kinds"][0]["kind"], "server_error")

        empty = run_discovery(
            networks=["solana"],
            fetch_pools=lambda network, page: {
                "ok": True,
                "pools": [],
                "included": [],
            },
            enrich_pair=lambda chain, address: None,
            now=FIRST_SEEN,
        )
        self.assertEqual(empty["api_pools_received"], 0)
        self.assertEqual(empty["unique_pools"], 0)
        self.assertEqual(empty["errors"], 0)

    def test_one_bad_pool_does_not_break_the_run(self):
        bad = self.spec(110, "USDC", "80")
        good = self.spec(111, "USDC", "80")
        pools, included, _pairs = self.materialize([bad, good])

        def enrich(chain_id, pair_address):
            if pair_address == bad["address"]:
                raise RuntimeError("bad pool")
            return _pairs[pair_address]

        summary = run_discovery(
            networks=["solana"],
            fetch_pools=lambda network, page: {
                "ok": True,
                "pools": pools,
                "included": included,
            },
            enrich_pair=enrich,
            now=FIRST_SEEN,
        )
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["saved_new_ideas"], 1)
        self.assertEqual([row[0] for row in self.pair_rows()], [good["address"]])

    def test_no_network_call_in_unit_run(self):
        spec = self.spec(120, "USDC", "80")
        pools, included, pairs = self.materialize([spec])
        with patch("requests.get", side_effect=AssertionError("network")), patch(
            "gecko_discovery.requests.get",
            side_effect=AssertionError("network"),
        ):
            summary = run_discovery(
                networks=["solana"],
                fetch_pools=lambda network, page: {
                    "ok": True,
                    "pools": pools,
                    "included": included,
                },
                enrich_pair=lambda chain, address: pairs[address],
                now=FIRST_SEEN,
            )
        self.assertEqual(summary["saved_new_ideas"], 1)

    def test_synthetic_path_saves_watchlist_and_rejects_low_score(self):
        accepted = self.spec(130, "USDC", "80")
        rejected = self.spec(131, "USDC", "65")
        summary = self.run_specs([accepted, rejected])
        self.assertEqual(summary["saved_new_ideas"], 1)
        self.assertEqual(summary["below_score"], 1)
        self.assertEqual(self.table_count("watchlist"), 1)
        self.assertEqual(self.table_count("paper_positions"), None)
        self.assertNotIn("paper_engine", inspect.getsource(new_pairs_discovery))
        self.assertNotIn("open_baseline_position", inspect.getsource(new_pairs_discovery))

    def test_legacy_scanner_semantics_stay_in_place(self):
        self.assertEqual(auto_scan.SEARCH_QUERIES, [
            "SOL/USDC",
            "ETH/USDC",
            "BTC/USDC",
            "AI/USDC",
            "RWA/USDC",
        ])
        self.assertEqual(auto_scan.MIN_FINAL_SCORE, 70)
        self.assertEqual(auto_scan.MAX_NEW_IDEAS, 3)
        self.assertEqual(auto_scan.ALLOWED_QUOTE_TOKENS, {"USDC", "USDT", "DAI"})
        self.assertEqual(paper_engine.MIN_FINAL_SCORE, 70)
        self.assertEqual(paper_engine.STOP_LOSS_PERCENT, 15)
        self.assertEqual(paper_engine.TRAILING_START_PERCENT, 10)
        self.assertEqual(paper_engine.TRAILING_DISTANCE_PERCENT, 5)
        self.assertEqual(paper_engine.MAX_HOLD_HOURS, 168)
        self.assertEqual(MIN_LIQUIDITY_USD, 10000)

        low = dex_pair("solana", sol_address(140), sol_address(141), "LOW", sol_address(142), "USDC", "65")
        with patch("auto_scan.search_pairs", return_value={"pairs": [low]}), patch(
            "sys.stdout",
            new_callable=StringIO,
        ):
            auto_scan._scan_once()
        self.assertEqual(self.pair_rows(), [])

        highs = [
            dex_pair("solana", sol_address(150 + index), sol_address(160 + index), "T{0}".format(index), sol_address(170), "USDC", "80")
            for index in range(4)
        ]
        with patch("auto_scan.search_pairs", return_value={"pairs": highs}), patch(
            "sys.stdout",
            new_callable=StringIO,
        ):
            auto_scan._scan_once()
        self.assertEqual(len(self.pair_rows()), 3)
        connection = sqlite3.connect(self.db_path)
        sources = connection.execute(
            "SELECT source FROM discovery_candidates ORDER BY id"
        ).fetchall()
        connection.close()
        self.assertEqual(sources, [(SOURCE_LEGACY,)] * 3)

        with patch("discovery_store.record_legacy_idea", side_effect=RuntimeError("boom")), patch(
            "auto_scan.search_pairs",
            return_value={"pairs": [dex_pair(
                "solana", sol_address(180), sol_address(181), "MORE", sol_address(182), "USDC", "80",
            )]},
        ), patch("sys.stdout", new_callable=StringIO) as output:
            auto_scan._scan_once()
        self.assertEqual(len(self.pair_rows()), 4)
        self.assertIn("источник идеи не записан", output.getvalue())

    def test_cli_dry_run_flag_does_not_imply_a_live_call(self):
        with patch("sys.stdout", new_callable=StringIO):
            with patch("new_pairs_discovery.run_discovery", return_value=empty_summary(True)) as run:
                code = main(["--dry-run"])
            self.assertEqual(code, 0)
            run.assert_called_once_with(dry_run=True)
            with patch("new_pairs_discovery.run_discovery", return_value=empty_summary(False)) as run:
                code = main([])
            run.assert_called_once_with(dry_run=False)
            self.assertEqual(main(["--write"]), 2)

    def spec(self, seed, quote, profile, network="solana", address=None, created="2026-10-09T12:00:00Z", dex="found"):
        if address is None:
            address = evm_address(seed) if network != "solana" else sol_address(seed)
        return {
            "network": network,
            "address": address,
            "base_symbol": "AAA{0}".format(seed),
            "quote_symbol": quote,
            "base_addr": evm_address(1000 + seed) if network != "solana" else sol_address(1000 + seed),
            "quote_addr": evm_address(2000 + seed) if network != "solana" else sol_address(2000 + seed),
            "created": created,
            "profile": profile,
            "dex": dex,
        }

    def run_specs(self, specs, networks=None, dry_run=False, now=None):
        if networks is None:
            networks = ["solana"]
        pools, included, pairs = self.materialize(specs)
        return run_discovery(
            dry_run=dry_run,
            networks=networks,
            fetch_pools=lambda network, page: {
                "ok": True,
                "pools": pools,
                "included": included,
            },
            enrich_pair=lambda chain, address: self.lookup_dex(pairs, chain, address),
            now=now or FIRST_SEEN,
        )

    def materialize(self, specs):
        pools = []
        included = []
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
            pools.append(pool)
            included.extend(pool_included)
            if spec["dex"] == "missing":
                continue
            chain_id = mapping_for_network(spec["network"])["chain_id"]
            built = dex_pair(
                chain_id,
                spec["address"],
                spec["base_addr"],
                spec["base_symbol"],
                spec["quote_addr"],
                spec["quote_symbol"],
                spec["profile"],
            )
            if spec["dex"] == "bad_price":
                built["priceUsd"] = None
            if spec["dex"] == "quote_mismatch":
                built["quoteToken"]["symbol"] = "SOL"
            if spec["dex"] == "mismatch":
                built["baseToken"]["address"] = sol_address(99999)
            pairs[spec["address"]] = built
        return pools, included, pairs

    def lookup_dex(self, pairs, chain_id, address):
        direct = pairs.get(address)
        if direct is not None and direct.get("chainId") == chain_id:
            return direct
        if chain_id == "solana":
            return None
        for pair in pairs.values():
            if pair.get("chainId") == chain_id and str(pair.get("pairAddress")).lower() == str(address).lower():
                return pair
        return None

    def pool_only(self, spec):
        pool, _included = gecko_pool(
            spec["network"],
            spec["address"],
            spec["base_addr"],
            spec["base_symbol"],
            spec["quote_addr"],
            spec["quote_symbol"],
            spec["created"],
        )
        return pool

    def insert_pair(self, pair_address, base_address, chain_id="solana", pair_symbol="OLD/USDC"):
        connection = sqlite3.connect(self.db_path)
        connection.execute("""
            INSERT INTO pairs (
                chain_id, dex_id, pair_address, pair_symbol,
                base_symbol, base_token_address, quote_symbol,
                price_usd, liquidity_usd, volume_24h, price_change_24h,
                risk_score, risk_level, potential_score, final_score, url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chain_id, "raydium", pair_address, pair_symbol,
            "OLD", base_address, "USDC",
            1, 20000, 20000, 10,
            0, "LOW (НИЗКИЙ)", 80, 80, "https://dexscreener.com",
        ))
        connection.commit()
        connection.close()

    def pair_rows(self):
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT pair_address FROM pairs ORDER BY pair_address"
        ).fetchall()
        connection.close()
        return rows

    def pair_rows_raw(self):
        connection = sqlite3.connect(self.db_path)
        rows = connection.execute(
            "SELECT pair_address, quote_symbol, final_score FROM pairs ORDER BY pair_address"
        ).fetchall()
        connection.close()
        return rows

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

    def snapshot(self):
        connection = sqlite3.connect(self.db_path)
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
        connection.close()
        return tables, data


if __name__ == "__main__":
    unittest.main()
