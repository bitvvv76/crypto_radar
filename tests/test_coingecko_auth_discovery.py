import inspect
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

import requests

import auto_scan
import database
import gecko_discovery
import human_approval
import new_pairs_discovery
import paper_engine
import scoring
from coingecko_discovery import (
    COINGECKO_DEMO_API_KEY_ENV,
    COINGECKO_DEMO_API_KEY_HEADER,
    COINGECKO_DEMO_BASE_URL,
    COINGECKO_MONTHLY_BUDGET,
    fetch_network_new_pools,
    monthly_budget_limit,
    new_pools_path,
    parse_new_pool,
)
from discovery_store import (
    SOURCE_COINGECKO,
    SOURCE_LEGACY,
    ensure_discovery_runtime_tables,
    get_candidate,
    get_network_state,
    monthly_billable_count,
    record_api_usage,
    save_complete_reference,
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
    JOB_NEW_PAIRS_DISCOVERY,
    MAX_PAGES_PER_NETWORK,
    format_auth_report,
    main,
    run_authenticated_discovery,
    run_discovery,
)
from stablecoins import canonical_quote_address


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "coingecko_onchain_new_pools.json",
)
NOW = datetime(2026, 10, 9, 12, 0, 0)
LATER = datetime(2026, 10, 9, 12, 10, 0)
SECRET = "cg-demo-super-secret-key"


def load_fixture():
    with open(FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


class Response:
    def __init__(self, status_code, payload=None, broken=False):
        self.status_code = status_code
        self.payload = payload
        self.broken = broken

    def json(self):
        if self.broken:
            raise ValueError("malformed")
        return self.payload


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
        "dexId": "uniswap",
        "pairAddress": pair_address,
        "baseToken": {"address": base_address, "symbol": base_symbol},
        "quoteToken": {"address": quote_address, "symbol": quote_symbol},
        "priceUsd": price,
        "liquidity": {"usd": liquidity},
        "volume": {"h24": volume},
        "priceChange": {"h24": change},
        "url": "https://dexscreener.com/{0}/{1}".format(chain_id, pair_address),
    }


def cg_pool(network, address, base_address, base_symbol, quote_address, quote_symbol, created):
    base_id = "{0}_{1}".format(network, base_address)
    quote_id = "{0}_{1}".format(network, quote_address)
    pool = {
        "id": "{0}_{1}".format(network, address),
        "type": "pool",
        "attributes": {
            "address": address,
            "name": "{0} / {1}".format(base_symbol, quote_symbol),
            "pool_created_at": created,
            "base_token_price_usd": "1.25",
            "reserve_in_usd": "22000",
            "volume_usd": {"h24": "900"},
        },
        "relationships": {
            "base_token": {"data": {"id": base_id, "type": "token"}},
            "quote_token": {"data": {"id": quote_id, "type": "token"}},
            "dex": {"data": {"id": "uniswap-v3", "type": "dex"}},
        },
    }
    included = [
        {
            "id": base_id,
            "type": "token",
            "attributes": {"address": base_address, "symbol": base_symbol, "name": base_symbol},
        },
        {
            "id": quote_id,
            "type": "token",
            "attributes": {"address": quote_address, "symbol": quote_symbol, "name": quote_symbol},
        },
        {"id": "uniswap-v3", "type": "dex", "attributes": {"name": "Uniswap V3"}},
    ]
    return pool, included


class CoinGeckoClientTest(unittest.TestCase):
    def test_api_key_is_read_from_env_header(self):
        captured = {}

        def getter(url, params, timeout, headers):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return Response(200, {"data": [], "included": []})

        with patch.dict(os.environ, {COINGECKO_DEMO_API_KEY_ENV: SECRET}):
            result = fetch_network_new_pools("eth", page=2, timeout=7, getter=getter)

        self.assertTrue(result["ok"])
        self.assertEqual(captured["headers"][COINGECKO_DEMO_API_KEY_HEADER], SECRET)
        self.assertEqual(captured["headers"]["x-cg-demo-api-key"], SECRET)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(COINGECKO_DEMO_API_KEY_HEADER, captured["params"])
        self.assertNotIn("x_cg_demo_api_key", captured["params"])
        self.assertNotIn(SECRET, captured["url"])
        self.assertEqual(list(captured["params"]), ["page", "include"])

    def test_missing_api_key_is_controlled_failure(self):
        def getter(url, params, timeout, headers):
            raise AssertionError("request without a key")

        with patch.dict(os.environ, {COINGECKO_DEMO_API_KEY_ENV: "   "}, clear=False):
            os.environ.pop(COINGECKO_DEMO_API_KEY_ENV, None)
            result = fetch_network_new_pools("eth", getter=getter)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_kind"], "missing_api_key")
        self.assertIsNone(result["status_code"])
        self.assertFalse(result["billable"])
        self.assertNotIn("Traceback", json.dumps(result))

    def test_key_never_appears_in_errors(self):
        def getter(url, params, timeout, headers):
            raise requests.exceptions.RequestException("boom " + SECRET)

        with patch.dict(os.environ, {COINGECKO_DEMO_API_KEY_ENV: SECRET}):
            result = fetch_network_new_pools("eth", getter=getter)

        self.assertEqual(result["error_kind"], "request_error")
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(SECRET, result["error_kind"])

    def test_demo_base_url_and_network_path(self):
        captured = {}

        def getter(url, params, timeout, headers):
            captured["url"] = url
            captured["params"] = params
            return Response(200, {"data": [], "included": []})

        with patch.dict(os.environ, {COINGECKO_DEMO_API_KEY_ENV: SECRET}):
            fetch_network_new_pools("base", page=1, getter=getter)

        self.assertTrue(captured["url"].startswith(COINGECKO_DEMO_BASE_URL))
        self.assertEqual(
            captured["url"],
            "https://api.coingecko.com/api/v3/onchain/networks/base/new_pools",
        )
        self.assertEqual(new_pools_path("base"), "/onchain/networks/base/new_pools")
        self.assertEqual(captured["params"]["include"], "base_token,quote_token,dex")
        self.assertEqual(captured["params"]["page"], 1)
        self.assertFalse(captured["url"].endswith("/onchain/networks/new_pools"))

    def test_global_feed_is_not_a_client_route(self):
        source = inspect.getsource(fetch_network_new_pools)
        path_source = inspect.getsource(new_pools_path)
        self.assertNotIn("/onchain/networks/new_pools", source)
        self.assertNotIn("/onchain/networks/new_pools", path_source)
        self.assertIsNone(getattr(gecko_discovery, "fetch_network_new_pools", None))

    def test_timeout_401_429_5xx_and_malformed_json(self):
        def timeout_getter(url, params, timeout, headers):
            raise requests.exceptions.Timeout("slow")

        with patch.dict(os.environ, {COINGECKO_DEMO_API_KEY_ENV: SECRET}):
            self.assertEqual(
                fetch_network_new_pools("eth", getter=timeout_getter)["error_kind"],
                "timeout",
            )
            self.assertFalse(
                fetch_network_new_pools("eth", getter=timeout_getter)["billable"]
            )
            auth = fetch_network_new_pools(
                "eth",
                getter=lambda url, params, timeout, headers: Response(401, {"status": SECRET}),
            )
            self.assertEqual(auth["error_kind"], "auth_error")
            self.assertNotIn(SECRET, json.dumps(auth))
            limited = fetch_network_new_pools(
                "eth",
                getter=lambda url, params, timeout, headers: Response(429),
            )
            self.assertEqual(limited["error_kind"], "rate_limited")
            self.assertFalse(limited["billable"])
            server = fetch_network_new_pools(
                "eth",
                getter=lambda url, params, timeout, headers: Response(503),
            )
            self.assertEqual(server["error_kind"], "server_error")
            self.assertFalse(server["billable"])
            broken = fetch_network_new_pools(
                "eth",
                getter=lambda url, params, timeout, headers: Response(200, broken=True),
            )
            self.assertEqual(broken["error_kind"], "malformed_json")
            self.assertTrue(broken["billable"])
            self.assertEqual(
                fetch_network_new_pools(
                    "eth",
                    getter=lambda url, params, timeout, headers: Response(400),
                )["error_kind"],
                "http_error",
            )
            self.assertEqual(
                fetch_network_new_pools(
                    "eth",
                    getter=lambda url, params, timeout, headers: Response(403),
                )["error_kind"],
                "forbidden",
            )

    def test_429_does_not_retry_inside_the_client(self):
        calls = []

        def getter(url, params, timeout, headers):
            calls.append(url)
            return Response(429)

        with patch.dict(os.environ, {COINGECKO_DEMO_API_KEY_ENV: SECRET}):
            fetch_network_new_pools("eth", getter=getter)
        self.assertEqual(len(calls), 1)

    def test_fixture_parses_pool_base_quote_and_dex(self):
        payload = load_fixture()
        self.assertNotIn("x-cg-demo-api-key", json.dumps(payload))
        self.assertNotIn(COINGECKO_DEMO_API_KEY_ENV, json.dumps(payload))
        pool = payload["data"][0]
        parsed = parse_new_pool(pool, payload["included"], source_network="eth")
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["source"], SOURCE_COINGECKO)
        self.assertEqual(parsed["source_network"], "eth")
        self.assertEqual(
            parsed["pool_address"],
            "0x4b025567ef3e47e7c6f091ba2e224d250eaeedadfddcee292c5281f632fbea07",
        )
        self.assertNotEqual(
            parsed["pool_address"],
            pool["id"],
        )
        self.assertEqual(parsed["pool_created_at"], "2026-05-27T15:05:59Z")
        self.assertEqual(parsed["dex"], "Uniswap V4 (Ethereum)")
        self.assertEqual(parsed["base_symbol"], "GOSLINGS")
        self.assertEqual(
            parsed["base_token_address"],
            "0xd4c24d1172813702439ab03b914a7c6e1c65b110",
        )
        self.assertNotIn("eth_", parsed["base_token_address"])
        self.assertEqual(parsed["quote_symbol"], "ETH")
        self.assertEqual(
            parsed["quote_token_address"],
            "0x0000000000000000000000000000000000000000",
        )

    def test_fixture_usdc_uses_included_address(self):
        payload = load_fixture()
        parsed = parse_new_pool(payload["data"][1], payload["included"], source_network="eth")
        self.assertEqual(parsed["quote_symbol"], "USDC")
        self.assertEqual(
            parsed["quote_token_address"],
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        )
        self.assertEqual(parsed["dex"], "Uniswap V3 (Ethereum)")

    def test_public_adapter_is_marked_deprecated(self):
        self.assertIn("NOT A PRODUCTION SOURCE", gecko_discovery.__doc__)
        self.assertIn("DEPRECATED", gecko_discovery.__doc__)


class AuthDiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "discovery.db")
        self.original_db_name = database.DB_NAME
        database.DB_NAME = self.db_path
        database.create_tables()

    def tearDown(self):
        database.DB_NAME = self.original_db_name
        self.temp_dir.cleanup()

    def test_configured_networks_exclude_solana(self):
        self.assertEqual(
            [item["network"] for item in COINGECKO_DISCOVERY_NETWORKS],
            ["eth", "base", "arc", "arbitrum"],
        )
        self.assertEqual(
            [item["interval_minutes"] for item in COINGECKO_DISCOVERY_NETWORKS],
            [20, 20, 20, 60],
        )
        self.assertEqual(coingecko_discovery_network_ids(), ["eth", "base", "arc", "arbitrum"])
        self.assertNotIn("solana", coingecko_discovery_network_ids())
        self.assertIn("solana", discovery_networks())
        self.assertEqual(mapping_for_network("solana")["chain_id"], "solana")

    def test_eth_is_due_at_20_minutes(self):
        self.seed_recent(except_network="eth", age_minutes=20)
        calls = self.due_calls(NOW)
        self.assertEqual(calls, [("eth", 1)])

    def test_base_is_due_at_20_minutes(self):
        self.seed_recent(except_network="base", age_minutes=20)
        self.assertEqual(self.due_calls(NOW), [("base", 1)])

    def test_arc_is_due_at_20_minutes(self):
        self.seed_recent(except_network="arc", age_minutes=20)
        self.assertEqual(self.due_calls(NOW), [("arc", 1)])

    def test_arbitrum_is_due_at_60_minutes(self):
        self.seed_recent(except_network="arbitrum", age_minutes=60, other_age=1)
        self.assertEqual(self.due_calls(NOW), [("arbitrum", 1)])

    def test_network_not_due_makes_no_request(self):
        self.seed_recent(except_network=None, age_minutes=1, other_age=19)
        self.assertEqual(self.due_calls(NOW), [])
        self.seed_complete("arbitrum", NOW - timedelta(minutes=59))
        self.assertNotIn(
            "arbitrum",
            [network for network, _page in self.due_calls(NOW)],
        )

    def test_page1_reaches_cutoff(self):
        spec = self.spec(1, created="2026-10-09T11:00:00Z")
        calls = []
        summary = self.run_pages(
            {("eth", 1): [spec]},
            calls,
            networks=["eth"],
        )
        self.assertEqual(calls, [("eth", 1)])
        self.assertEqual(summary["pagination"]["eth"]["stop_reason"], "cutoff")
        self.assertEqual(summary["pagination"]["eth"]["pages_fetched"], 1)
        self.assertTrue(summary["pagination"]["eth"]["coverage_complete"])
        self.assertTrue(summary["coverage_complete"])

    def test_page2_is_required_when_page1_is_newer_than_cutoff(self):
        fresh = self.spec(2, created="2026-10-09T12:00:00Z")
        older = self.spec(3, created="2026-10-09T11:00:00Z")
        calls = []
        summary = self.run_pages(
            {("eth", 1): [fresh], ("eth", 2): [older]},
            calls,
            networks=["eth"],
        )
        self.assertEqual(calls, [("eth", 1), ("eth", 2)])
        self.assertEqual(summary["pagination"]["eth"]["pages_fetched"], 2)
        self.assertEqual(summary["pagination"]["eth"]["stop_reason"], "cutoff")
        self.assertEqual(summary["api_pools_received"], 2)

    def test_pagination_is_bounded_at_three_pages(self):
        pages = {}
        for page in (1, 2, 3, 4):
            pages[("eth", page)] = [self.spec(10 + page, created="2026-10-09T12:05:00Z")]
        calls = []
        summary = self.run_pages(pages, calls, networks=["eth"])
        self.assertEqual(MAX_PAGES_PER_NETWORK, 3)
        self.assertEqual(calls, [("eth", 1), ("eth", 2), ("eth", 3)])
        self.assertEqual(summary["pagination"]["eth"]["stop_reason"], "max_pages")
        self.assertFalse(summary["pagination"]["eth"]["coverage_complete"])

    def test_empty_page_is_complete(self):
        calls = []
        summary = self.run_pages({}, calls, networks=["eth"])
        self.assertEqual(calls, [("eth", 1)])
        self.assertEqual(summary["pagination"]["eth"]["stop_reason"], "empty_page")
        self.assertTrue(summary["pagination"]["eth"]["coverage_complete"])
        self.assertTrue(summary["coverage_complete"])

    def test_max_pages_before_cutoff_is_incomplete(self):
        pages = {
            ("eth", page): [self.spec(20 + page, created="2026-10-09T12:05:00Z")]
            for page in (1, 2, 3)
        }
        summary = self.run_pages(pages, [], networks=["eth"])
        info = summary["pagination"]["eth"]
        self.assertEqual(info["stop_reason"], "max_pages")
        self.assertEqual(info["cutoff"], "2026-10-09T11:30:00Z")
        self.assertEqual(info["oldest_pool_created_at"], "2026-10-09T12:05:00Z")
        self.assertFalse(info["coverage_complete"])
        self.assertFalse(summary["coverage_complete"])
        self.assertEqual(summary["networks_incomplete"], 1)

    def test_missing_timestamps_do_not_complete_coverage(self):
        spec = self.spec(220, created="2026-10-09T11:00:00Z")
        pools, included, _pairs = self.materialize([spec])
        pools[0]["attributes"].pop("pool_created_at")
        calls = []

        def fetch(network, page):
            calls.append(page)
            return {
                "ok": True,
                "pools": pools,
                "included": included,
                "status_code": 200,
                "billable": True,
            }

        summary = run_authenticated_discovery(
            networks=["eth"],
            fetch_pools=fetch,
            enrich_pair=lambda chain, address: None,
            now=NOW,
            budget_limit=9000,
        )
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(summary["pagination"]["eth"]["stop_reason"], "missing_timestamps")
        self.assertFalse(summary["pagination"]["eth"]["coverage_complete"])
        self.assertIsNone(get_network_state("eth")["last_complete_reference"])

    def test_complete_run_advances_reference(self):
        spec = self.spec(30, created="2026-10-09T11:00:00Z")
        first = self.run_pages({("eth", 1): [spec]}, [], networks=["eth"], now=NOW)
        self.assertTrue(first["coverage_complete"])
        state = get_network_state("eth")
        self.assertEqual(state["last_complete_reference"], "2026-10-09 12:00:00")
        second = self.run_pages({}, [], networks=["eth"], now=NOW + timedelta(minutes=20))
        self.assertEqual(second["pagination"]["eth"]["cutoff_basis"], "overlap")
        self.assertEqual(second["pagination"]["eth"]["cutoff"], "2026-10-09T11:45:00Z")

    def test_incomplete_run_does_not_advance_reference(self):
        pages = {
            ("eth", page): [self.spec(40 + page, created="2026-10-09T12:05:00Z")]
            for page in (1, 2, 3)
        }
        first = self.run_pages(pages, [], networks=["eth"], now=NOW)
        self.assertFalse(first["coverage_complete"])
        state = get_network_state("eth")
        self.assertIsNone(state["last_complete_reference"])
        second = self.run_pages(pages, [], networks=["eth"], now=LATER)
        self.assertEqual(second["pagination"]["eth"]["cutoff_basis"], "initial_target_cutoff")
        self.assertFalse(second["coverage_complete"])
        self.assertIsNone(get_network_state("eth")["last_complete_reference"])

    def test_initial_cutoff_stays_stable_across_incomplete_runs(self):
        pages = {
            ("eth", page): [self.spec(50 + page, created="2026-10-09T12:05:00Z")]
            for page in (1, 2, 3)
        }
        first = self.run_pages(pages, [], networks=["eth"], now=NOW)
        second = self.run_pages(pages, [], networks=["eth"], now=datetime(2026, 10, 9, 13, 0, 0))
        self.assertEqual(first["pagination"]["eth"]["cutoff"], "2026-10-09T11:30:00Z")
        self.assertEqual(second["pagination"]["eth"]["cutoff"], "2026-10-09T11:30:00Z")
        self.assertNotEqual(second["pagination"]["eth"]["cutoff"], "2026-10-09T12:30:00Z")
        self.assertEqual(
            get_network_state("eth")["initial_target_cutoff"],
            "2026-10-09T11:30:00Z",
        )

    def test_first_complete_switches_to_normal_watermark(self):
        fresh_pages = {
            ("eth", page): [self.spec(60 + page, created="2026-10-09T12:05:00Z")]
            for page in (1, 2, 3)
        }
        incomplete = self.run_pages(fresh_pages, [], networks=["eth"], now=NOW)
        self.assertEqual(incomplete["pagination"]["eth"]["cutoff_basis"], "initial_target_cutoff")
        complete = self.run_pages(
            {("eth", 1): [self.spec(70, created="2026-10-09T11:00:00Z")]},
            [],
            networks=["eth"],
            now=LATER,
        )
        self.assertEqual(complete["pagination"]["eth"]["cutoff"], "2026-10-09T11:30:00Z")
        self.assertTrue(complete["coverage_complete"])
        self.assertEqual(get_network_state("eth")["last_complete_reference"], "2026-10-09 12:10:00")
        third = self.run_pages(
            {},
            [],
            networks=["eth"],
            now=datetime(2026, 10, 9, 12, 30, 0),
        )
        self.assertEqual(third["pagination"]["eth"]["cutoff_basis"], "overlap")
        self.assertEqual(third["pagination"]["eth"]["cutoff"], "2026-10-09T11:55:00Z")

    def test_overlap_is_fifteen_minutes(self):
        self.assertEqual(DISCOVERY_OVERLAP, timedelta(minutes=15))
        spec = self.spec(80, created="2026-10-09T11:00:00Z")
        self.run_pages({("eth", 1): [spec]}, [], networks=["eth"], now=NOW)
        second = self.run_pages({}, [], networks=["eth"], now=NOW + timedelta(minutes=20))
        self.assertEqual(second["pagination"]["eth"]["cutoff"], "2026-10-09T11:45:00Z")

    def test_all_due_networks_receive_page1_before_page2(self):
        pages = {}
        for network in ("eth", "base", "arc", "arbitrum"):
            pages[(network, 1)] = [self.spec(90, network=network, created="2026-10-09T12:00:00Z")]
            pages[(network, 2)] = [self.spec(91, network=network, created="2026-10-09T11:00:00Z")]
        calls = []
        self.run_pages(pages, calls)
        self.assertEqual(calls, [
            ("eth", 1),
            ("base", 1),
            ("arc", 1),
            ("arbitrum", 1),
            ("eth", 2),
            ("base", 2),
            ("arc", 2),
            ("arbitrum", 2),
        ])

    def test_active_network_cannot_starve_another(self):
        pages = {}
        for page in (1, 2, 3, 4):
            pages[("eth", page)] = [self.spec(100 + page, created="2026-10-09T12:05:00Z")]
        pages[("base", 1)] = [self.spec(110, network="base", created="2026-10-09T11:00:00Z")]
        calls = []
        self.run_pages(pages, calls, networks=["eth", "base"])
        self.assertEqual(calls, [
            ("eth", 1),
            ("base", 1),
            ("eth", 2),
            ("eth", 3),
        ])
        self.assertLess(calls.index(("base", 1)), calls.index(("eth", 2)))

    def test_http_200_is_billable_and_non_200_is_not(self):
        self.assertEqual(COINGECKO_MONTHLY_BUDGET, 9000)
        with patch.dict(os.environ, {"COINGECKO_MONTHLY_BUDGET": ""}, clear=False):
            os.environ.pop("COINGECKO_MONTHLY_BUDGET", None)
            self.assertEqual(monthly_budget_limit(), 9000)
        summary = self.run_pages({}, [], networks=["eth"], budget_limit=9000)
        self.assertEqual(summary["api_200"], 1)
        self.assertEqual(summary["monthly_budget_used"], 1)
        self.assertEqual(summary["monthly_budget_remaining"], 8999)
        rows = self.usage_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "coingecko")
        self.assertEqual(rows[0][1], "eth")
        self.assertEqual(rows[0][2], "/onchain/networks/eth/new_pools")
        self.assertNotIn("?", rows[0][2])
        self.assertEqual(rows[0][4], 1)
        self.assertIsNotNone(rows[0][5])

        denied = self.run_pages(
            {},
            [],
            networks=["base"],
            budget_limit=9000,
            fetch_result={"ok": False, "error_kind": "rate_limited", "status_code": 429, "billable": False},
            fail_networks={"base"},
        )
        self.assertEqual(denied["api_200"], 0)
        self.assertEqual(monthly_billable_count(NOW), 1)
        statuses = [row[4] for row in self.usage_rows() if row[1] == "base"]
        self.assertEqual(statuses, [0])

    def test_monthly_cap_blocks_the_next_request(self):
        calls = []
        summary = self.run_pages(
            {
                ("eth", 1): [self.spec(120, created="2026-10-09T12:00:00Z")],
                ("base", 1): [self.spec(121, network="base", created="2026-10-09T12:00:00Z")],
            },
            calls,
            networks=["eth", "base"],
            budget_limit=1,
        )
        self.assertEqual(calls, [("eth", 1)])
        self.assertEqual(summary["api_requests"], 1)
        self.assertEqual(summary["pagination"]["base"]["stop_reason"], "monthly_budget_exhausted")
        self.assertEqual(summary["pagination"]["base"]["pages_fetched"], 0)
        self.assertEqual(summary["monthly_budget_used"], 1)
        self.assertEqual(summary["monthly_budget_remaining"], 0)

    def test_monthly_cap_leaves_coverage_incomplete_and_watermark_stable(self):
        calls = []
        first = self.run_pages({}, calls, networks=["eth"], now=NOW, budget_limit=0)
        self.assertEqual(calls, [])
        self.assertFalse(first["coverage_complete"])
        self.assertEqual(first["pagination"]["eth"]["stop_reason"], "monthly_budget_exhausted")
        self.assertIsNone(get_network_state("eth")["last_complete_reference"])
        self.assertEqual(first["pagination"]["eth"]["cutoff"], "2026-10-09T11:30:00Z")
        second = self.run_pages(
            {},
            [],
            networks=["eth"],
            now=datetime(2026, 10, 9, 13, 0, 0),
            budget_limit=0,
        )
        self.assertEqual(second["pagination"]["eth"]["cutoff"], "2026-10-09T11:30:00Z")
        self.assertIsNone(get_network_state("eth")["last_complete_reference"])

    def test_budget_resets_on_calendar_month(self):
        record_api_usage(
            "coingecko",
            "eth",
            "/onchain/networks/eth/new_pools",
            "2026-09-30 23:59:59",
            200,
            True,
        )
        record_api_usage(
            "coingecko",
            "eth",
            "/onchain/networks/eth/new_pools",
            "2026-10-01 00:00:00",
            200,
            True,
        )
        self.assertEqual(monthly_billable_count(NOW), 1)
        self.assertEqual(monthly_billable_count(datetime(2026, 9, 15, 0, 0, 0)), 1)

    def test_timeout_401_429_and_5xx_stop_safely(self):
        self.clear_schedule(["eth", "base", "arc", "arbitrum"])
        timeout_calls = []
        timeout = self.run_pages(
            {},
            timeout_calls,
            networks=["eth", "base"],
            fetch_result={"ok": False, "error_kind": "timeout", "status_code": None, "billable": False},
            fail_networks={"eth"},
        )
        self.assertEqual(timeout_calls, [("eth", 1), ("base", 1)])
        self.assertEqual(timeout["pagination"]["eth"]["stop_reason"], "timeout")
        self.assertEqual(timeout["pagination"]["base"]["stop_reason"], "empty_page")
        self.assertFalse(timeout["pagination"]["eth"]["coverage_complete"])
        self.assertTrue(timeout["pagination"]["base"]["coverage_complete"])

        self.clear_schedule(["eth", "base", "arc", "arbitrum"])
        auth_calls = []
        auth = self.run_pages(
            {},
            auth_calls,
            networks=["eth", "base"],
            fetch_result={"ok": False, "error_kind": "auth_error", "status_code": 401, "billable": False},
            fail_networks={"eth"},
        )
        self.assertEqual(auth_calls, [("eth", 1)])
        self.assertEqual(auth["pagination"]["eth"]["stop_reason"], "auth_error")
        self.assertEqual(auth["pagination"]["base"]["stop_reason"], "auth_error")
        self.assertNotIn(SECRET, json.dumps(auth["api_error_kinds"]))

        self.clear_schedule(["eth", "base", "arc", "arbitrum"])
        limited_calls = []
        limited = self.run_pages(
            {},
            limited_calls,
            networks=["eth", "base"],
            fetch_result={"ok": False, "error_kind": "rate_limited", "status_code": 429, "billable": False},
            fail_networks={"eth"},
        )
        self.assertEqual(limited_calls, [("eth", 1)])
        self.assertTrue(limited["rate_limited"])
        self.assertFalse(limited["coverage_complete"])

        self.clear_schedule(["eth", "base", "arc", "arbitrum"])
        server_calls = []
        server = self.run_pages(
            {},
            server_calls,
            networks=["eth", "base"],
            fetch_result={"ok": False, "error_kind": "server_error", "status_code": 502, "billable": False},
            fail_networks={"eth"},
        )
        self.assertEqual(server_calls, [("eth", 1), ("base", 1)])
        self.assertEqual(server["pagination"]["eth"]["stop_reason"], "server_error")
        self.assertTrue(server["pagination"]["base"]["coverage_complete"])

    def test_malformed_later_page_keeps_prior_results(self):
        kept = self.spec(130, created="2026-10-09T12:00:00Z")
        calls = []

        def fetch(network, page):
            calls.append((network, page))
            if network == "eth" and page == 1:
                pool, included, _pairs = self.materialize([kept])
                return {"ok": True, "pools": pool, "included": included, "status_code": 200, "billable": True}
            if network == "eth" and page == 2:
                raise RuntimeError("malformed " + SECRET)
            if network == "base" and page == 1:
                return {"ok": True, "pools": [], "included": [], "status_code": 200, "billable": True}
            raise AssertionError((network, page))

        summary = run_authenticated_discovery(
            networks=["eth", "base"],
            fetch_pools=fetch,
            enrich_pair=self.enrich_for([kept]),
            now=NOW,
            budget_limit=9000,
        )
        self.assertEqual(calls, [("eth", 1), ("base", 1), ("eth", 2)])
        self.assertEqual(summary["saved_new_ideas"], 1)
        self.assertEqual([row[0] for row in self.pair_rows()], [kept["address"]])
        self.assertTrue(summary["pagination"]["base"]["coverage_complete"])
        self.assertFalse(summary["pagination"]["eth"]["coverage_complete"])
        self.assertEqual(summary["pagination"]["eth"]["pages_fetched"], 1)
        self.assertNotIn(SECRET, json.dumps(summary["api_error_kinds"]))
        report = new_pairs_discovery._redact_secret(format_auth_report(summary))
        self.assertNotIn(SECRET, report)

    def test_canonical_usdc_is_accepted_and_fake_usdc_is_rejected(self):
        real = self.spec(140, "USDC")
        fake = self.spec(141, "USDC", quote_addr=evm_address(1410))
        accepted = self.run_specs([real])
        self.assertEqual(accepted["supported_quote"], 1)
        self.assertEqual(accepted["unverified_quote"], 0)
        self.assertEqual(accepted["saved_new_ideas"], 1)
        rejected = self.run_specs([fake])
        self.assertEqual(rejected["unverified_quote"], 1)
        self.assertEqual(rejected["supported_quote"], 0)
        self.assertEqual(rejected["saved_new_ideas"], 0)
        candidate = get_candidate(SOURCE_COINGECKO, "eth", fake["address"].lower())
        self.assertEqual(candidate["reason"], "unverified_quote_token")

    def test_usdt_and_dai_policy_is_preserved(self):
        eth_usdt = self.run_specs([self.spec(150, "USDT", network="eth")])
        base_usdt = self.run_specs([self.spec(151, "USDT", network="base")], networks=["base"])
        self.assertEqual(eth_usdt["saved_new_ideas"], 1)
        self.assertEqual(base_usdt["unverified_quote"], 1)
        self.assertEqual(base_usdt["saved_new_ideas"], 0)

        eth_dai = self.run_specs([self.spec(152, "DAI", network="eth")])
        base_dai = self.run_specs([self.spec(153, "DAI", network="base")], networks=["base"])
        arb_dai = self.run_specs([self.spec(154, "DAI", network="arbitrum")], networks=["arbitrum"])
        self.assertEqual(eth_dai["saved_new_ideas"], 1)
        self.assertEqual(base_dai["unverified_quote"], 1)
        self.assertEqual(base_dai["saved_new_ideas"], 0)
        self.assertEqual(arb_dai["saved_new_ideas"], 1)

    def test_unsupported_quote_from_fixture_is_rejected(self):
        payload = load_fixture()
        calls = []

        def fetch(network, page):
            calls.append((network, page))
            if page == 1:
                return {
                    "ok": True,
                    "pools": payload["data"],
                    "included": payload["included"],
                    "status_code": 200,
                    "billable": True,
                }
            return {"ok": True, "pools": [], "included": [], "status_code": 200, "billable": True}

        summary = run_authenticated_discovery(
            networks=["eth"],
            fetch_pools=fetch,
            enrich_pair=lambda chain, address: None,
            now=NOW,
            budget_limit=9000,
        )
        self.assertEqual(summary["unsupported_quote"], 1)
        self.assertEqual(summary["saved_new_ideas"], 0)
        self.assertEqual(calls, [("eth", 1)])

    def test_exact_dexscreener_lookup_missing_pair_and_invalid_market(self):
        spec = self.spec(160)
        with patch("scanner.search_pairs", side_effect=AssertionError("search")), patch(
            "new_pairs_discovery.get_pair_by_address",
            return_value=self.materialize([spec])[2][spec["address"]],
        ) as lookup:
            summary = self.run_specs([spec], enrich=None)
        lookup.assert_called_once_with("ethereum", spec["address"])
        self.assertIn("/pairs/", inspect.getsource(new_pairs_discovery.get_pair_by_address))
        self.assertEqual(summary["dex_confirmed"], 1)
        self.assertEqual(summary["saved_new_ideas"], 1)

        missing = self.run_specs([self.spec(161, dex="missing")])
        self.assertEqual(missing["dex_not_found"], 1)
        self.assertEqual(missing["saved_new_ideas"], 0)

        price = self.run_specs([self.spec(162, dex="bad_price")])
        thin = self.run_specs([self.spec(163, profile="thin")])
        self.assertEqual(price["invalid_price"], 1)
        self.assertEqual(thin["invalid_liquidity"], 1)
        self.assertEqual(price["saved_new_ideas"], 0)
        self.assertEqual(thin["saved_new_ideas"], 0)

    def test_scoring_threshold_and_max_three_ideas_are_unchanged(self):
        self.assertIn(
            "final_score = potential_score - risk_score",
            inspect.getsource(scoring.calculate_final_score),
        )
        self.assertNotIn("coingecko", inspect.getsource(scoring))
        self.assertEqual(auto_scan.MIN_FINAL_SCORE, 70)
        self.assertEqual(auto_scan.MAX_NEW_IDEAS, 3)
        high = self.spec(170, profile="80", created="2026-10-09T10:00:00Z")
        border = self.spec(171, profile="70", created="2026-10-09T10:30:00Z")
        low = self.spec(172, profile="65", created="2026-10-09T10:45:00Z")
        summary = self.run_specs([low, border, high])
        self.assertEqual(summary["below_score"], 1)
        self.assertEqual(summary["eligible_score"], 2)
        self.assertEqual(summary["saved_new_ideas"], 2)
        stored = {row[0]: row[2] for row in self.pair_rows_raw()}
        self.assertEqual(stored[high["address"]], 80)
        self.assertEqual(stored[border["address"]], 70)

        rejected = self.spec(173, profile="80")
        with patch("new_pairs_discovery.calculate_final_score", return_value=69):
            blocked = self.run_specs([rejected])
        self.assertEqual(blocked["below_score"], 1)
        self.assertEqual(blocked["eligible_score"], 0)
        self.assertEqual(blocked["saved_new_ideas"], 0)

        specs = [
            self.spec(181, created="2026-10-09T10:00:00Z", address=evm_address(181)),
            self.spec(182, created="2026-10-09T11:00:00Z", address=evm_address(182)),
            self.spec(183, created="2026-10-09T11:30:00Z", address=evm_address(183)),
            self.spec(184, created="2026-10-09T11:40:00Z", address=evm_address(184)),
        ]
        capped = self.run_specs(specs)
        self.assertEqual(capped["eligible_score"], 4)
        self.assertEqual(capped["saved_new_ideas"], 3)
        self.assertEqual(
            [row[0] for row in self.pair_rows() if row[0] in {item["address"] for item in specs}],
            [evm_address(181), evm_address(182), evm_address(183)],
        )
        held = get_candidate(SOURCE_COINGECKO, "eth", evm_address(184))
        self.assertEqual(held["reason"], "max_new_ideas")

    def test_repeated_pool_existing_pair_base_token_and_evm_case(self):
        spec = self.spec(190)
        duplicate = dict(spec)
        once = self.run_specs([spec, duplicate])
        self.assertEqual(once["duplicates"], 1)
        self.assertEqual(once["unique_pools"], 1)
        self.assertEqual(once["saved_new_ideas"], 1)
        candidate = get_candidate(SOURCE_COINGECKO, "eth", spec["address"].lower())
        self.assertEqual(candidate["source"], SOURCE_COINGECKO)
        self.assertNotEqual(candidate["source"], "geckoterminal_new_pools")
        again = self.run_specs([spec], now=NOW + timedelta(minutes=20))
        self.assertEqual(again["existing_pairs"], 1)
        self.assertEqual(again["saved_new_ideas"], 0)
        self.assertEqual(
            get_candidate(SOURCE_COINGECKO, "eth", spec["address"].lower())["seen_count"],
            2,
        )

        other = self.spec(191)
        self.insert_pair(evm_address(880), other["base_addr"])
        base_dup = self.run_specs([other])
        self.assertEqual(base_dup["existing_pairs"], 1)
        self.assertEqual(base_dup["saved_new_ideas"], 0)

        stored = evm_address(192)
        discovered = evm_address(192, upper=True)
        case_spec = self.spec(192, address=discovered)
        self.insert_pair(stored, evm_address(4242))
        case_run = self.run_specs([
            self.spec(193, address=stored),
            self.spec(193, address=discovered),
        ])
        self.assertEqual(case_run["duplicates"], 1)
        existing = self.run_specs([case_spec])
        self.assertEqual(existing["existing_pairs"], 1)
        self.assertEqual(existing["saved_new_ideas"], 0)
        self.assertIn((stored,), self.pair_rows())
        self.assertNotIn((discovered,), self.pair_rows())

    def test_dry_run_does_not_write_production_state_or_usage(self):
        ensure_discovery_runtime_tables(self.db_path)
        before_rows = len(self.usage_rows())
        before = self.snapshot()
        spec = self.spec(200)
        summary = self.run_specs([spec], dry_run=True)
        self.assertTrue(summary["dry_run"])
        self.assertEqual(summary["eligible_score"], 1)
        self.assertEqual(summary["saved_new_ideas"], 0)
        self.assertEqual(summary["unrecorded_billable"], 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.usage_rows()), before_rows)
        self.assertIsNone(get_network_state("eth"))
        self.assertIsNone(latest_job_run(self.db_path, JOB_NEW_PAIRS_DISCOVERY))
        self.assertIsNone(get_candidate(SOURCE_COINGECKO, "eth", spec["address"].lower()))

    def test_production_path_does_not_call_public_gecko_or_global_feed(self):
        calls = []

        def fetch(network, page=1, **kwargs):
            calls.append(network)
            self.assertNotEqual(network, "solana")
            self.assertNotEqual(new_pools_path(network), "/onchain/networks/new_pools")
            return {"ok": True, "pools": [], "included": [], "status_code": 200, "billable": True}

        auth_source = "\n".join([
            inspect.getsource(new_pairs_discovery.run_authenticated_discovery),
            inspect.getsource(new_pairs_discovery._default_auth_fetch),
            inspect.getsource(new_pairs_discovery._collect_fair),
        ])
        self.assertNotIn("geckoterminal", auth_source)
        self.assertNotIn("fetch_new_pools", auth_source)
        self.assertNotIn("/onchain/networks/new_pools", auth_source)

        with patch.dict(os.environ, {COINGECKO_DEMO_API_KEY_ENV: SECRET}), patch(
            "new_pairs_discovery.fetch_network_new_pools",
            side_effect=fetch,
        ), patch(
            "gecko_discovery.fetch_new_pools",
            side_effect=AssertionError("public gecko"),
        ):
            summary = run_discovery(dry_run=True, now=NOW)
        self.assertEqual(summary["source"], SOURCE_COINGECKO)
        self.assertEqual(calls, ["eth", "base", "arc", "arbitrum"])
        self.assertNotIn(SECRET, format_auth_report(summary))

    def test_legacy_solana_scanner_paper_approval_and_exit_stay(self):
        self.assertEqual(auto_scan.SEARCH_QUERIES, [
            "SOL/USDC",
            "ETH/USDC",
            "BTC/USDC",
            "AI/USDC",
            "RWA/USDC",
        ])
        self.assertEqual(auto_scan.MIN_FINAL_SCORE, 70)
        self.assertEqual(auto_scan.MAX_NEW_IDEAS, 3)
        self.assertEqual(paper_engine.MIN_FINAL_SCORE, 70)
        self.assertEqual(paper_engine.STOP_LOSS_PERCENT, 15)
        self.assertEqual(paper_engine.TRAILING_START_PERCENT, 10)
        self.assertEqual(paper_engine.TRAILING_DISTANCE_PERCENT, 5)
        self.assertEqual(paper_engine.MAX_HOLD_HOURS, 168)
        self.assertEqual(paper_engine.EXIT_TIME, "TIME_EXIT")
        self.assertNotIn("coingecko", inspect.getsource(paper_engine))
        self.assertEqual(human_approval.STATUS_BUY, "BUY")
        self.assertEqual(human_approval.STATUS_SKIP, "SKIP")
        self.assertNotIn("coingecko", inspect.getsource(human_approval))
        self.assertNotIn("paper_engine", inspect.getsource(new_pairs_discovery))
        self.assertNotIn("open_baseline_position", inspect.getsource(new_pairs_discovery))

        pair = dex_pair(
            "solana",
            "1" + ("A" * 43),
            "2" + ("B" * 43),
            "SOLTEST",
            canonical_quote_address("solana", "USDC"),
            "USDC",
            "80",
        )
        with patch("auto_scan.search_pairs", return_value={"pairs": [pair]}), patch(
            "sys.stdout",
            new_callable=StringIO,
        ):
            auto_scan._scan_once()
        self.assertEqual(len(self.pair_rows()), 1)
        connection = sqlite3.connect(self.db_path)
        source = connection.execute("SELECT source FROM discovery_candidates").fetchone()[0]
        connection.close()
        self.assertEqual(source, SOURCE_LEGACY)

    def test_cli_report_and_missing_key_message(self):
        self.seed_complete("arbitrum", NOW - timedelta(minutes=10))
        summary = self.run_pages({}, [], now=NOW, budget_limit=9000)
        report = format_auth_report(summary)
        self.assertIn("CRYPTO RADAR — COINGECKO AUTH DISCOVERY", report)
        self.assertIn("coingecko_authenticated_new_pools", report)
        self.assertIn("Due networks:\nETH\nBASE\nARC", report)
        self.assertIn("Skipped not due:\nARBITRUM", report)
        self.assertIn("HTTP 200: 3", report)
        self.assertIn("Used: 3 / 9000", report)
        self.assertIn("Remaining: 8997", report)
        self.assertIn("NETWORK COVERAGE", report)
        self.assertIn("stop: empty_page", report)
        self.assertNotIn("ARBITRUM:\n", report)

        before = self.snapshot()
        with patch.dict(os.environ, {COINGECKO_DEMO_API_KEY_ENV: ""}, clear=False):
            os.environ.pop(COINGECKO_DEMO_API_KEY_ENV, None)
            with patch("sys.stdout", new_callable=StringIO) as output:
                code = main(["--dry-run"])
        text = output.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("COINGECKO_DEMO_API_KEY is not set.", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn(SECRET, text)
        self.assertEqual(self.snapshot(), before)

    def test_systemd_template_and_gitignore(self):
        service = Path(ROOT, "deploy/systemd/crypto-radar-discovery.service").read_text(encoding="utf-8")
        timer = Path(ROOT, "deploy/systemd/crypto-radar-discovery.timer").read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=/opt/crypto_radar/.env", service)
        self.assertIn("new_pairs_discovery.py", service)
        self.assertNotIn("x-cg-demo-api-key", service)
        self.assertNotIn(SECRET, service)
        self.assertNotIn("systemctl", service)
        self.assertIn("OnUnitActiveSec=20min", timer)
        self.assertNotIn("systemctl", timer)
        tracked = subprocess.run(
            ["git", "ls-files", ".env"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(tracked.stdout.strip(), "")
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", ".env"],
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(ignored.returncode, 0)

    def seed_complete(self, network, moment):
        save_complete_reference(network, moment.strftime("%Y-%m-%d %H:%M:%S"), now=moment)

    def seed_recent(self, except_network, age_minutes, other_age=1):
        for network in coingecko_discovery_network_ids():
            age = age_minutes if network == except_network else other_age
            if except_network is None:
                age = age_minutes if network != "eth" else other_age
            self.seed_complete(network, NOW - timedelta(minutes=age))

    def due_calls(self, now):
        calls = []

        def fetch(network, page):
            calls.append((network, page))
            return {"ok": True, "pools": [], "included": [], "status_code": 200, "billable": True}

        run_authenticated_discovery(
            fetch_pools=fetch,
            enrich_pair=lambda chain, address: None,
            now=now,
            dry_run=True,
            budget_limit=9000,
        )
        return calls

    def spec(
        self,
        seed,
        quote="USDC",
        profile="80",
        network="eth",
        address=None,
        created="2026-10-09T11:00:00Z",
        dex="found",
        quote_addr=None,
    ):
        if address is None:
            address = evm_address(1000 + seed)
        if quote_addr is None:
            quote_addr = canonical_quote_address(network, quote)
        if not quote_addr:
            quote_addr = evm_address(5000 + seed)
        return {
            "network": network,
            "address": address,
            "base_symbol": "AAA{0}".format(seed),
            "quote_symbol": quote,
            "base_addr": evm_address(3000 + seed),
            "quote_addr": quote_addr,
            "created": created,
            "profile": profile,
            "dex": dex,
        }

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

    def run_specs(self, specs, networks=None, dry_run=False, now=None, enrich="auto"):
        selected = []
        for spec in specs:
            if spec["network"] not in selected:
                selected.append(spec["network"])
        if networks is not None:
            selected = list(networks)
        self.clear_schedule(selected)
        grouped = {}
        included = {}
        pairs = {}
        for spec in specs:
            pool, pool_included, built = self.materialize([spec])
            grouped.setdefault(spec["network"], []).extend(pool)
            included.setdefault(spec["network"], []).extend(pool_included)
            pairs.update(built)

        def fetch(network, page):
            if page != 1:
                return {"ok": True, "pools": [], "included": [], "status_code": 200, "billable": True}
            return {
                "ok": True,
                "pools": grouped.get(network, []),
                "included": included.get(network, []),
                "status_code": 200,
                "billable": True,
            }

        if enrich == "auto":
            enrich_pair = lambda chain, address: self.lookup_dex(pairs, chain, address)
        else:
            enrich_pair = enrich
        return run_authenticated_discovery(
            dry_run=dry_run,
            networks=networks or [spec["network"] for spec in specs] or ["eth"],
            fetch_pools=fetch,
            enrich_pair=enrich_pair,
            now=now or NOW,
            budget_limit=9000,
        )

    def run_pages(self, pages, calls, networks=None, now=None, budget_limit=9000, fetch_result=None, fail_networks=None):
        pairs = {}

        def fetch(network, page):
            calls.append((network, page))
            if fail_networks and network in fail_networks and page == 1:
                return dict(fetch_result)
            specs = pages.get((network, page))
            if not specs:
                return {"ok": True, "pools": [], "included": [], "status_code": 200, "billable": True}
            pools, included, built = self.materialize(specs)
            pairs.update(built)
            return {"ok": True, "pools": pools, "included": included, "status_code": 200, "billable": True}

        return run_authenticated_discovery(
            networks=networks,
            fetch_pools=fetch,
            enrich_pair=lambda chain, address: self.lookup_dex(pairs, chain, address),
            now=now or NOW,
            budget_limit=budget_limit,
        )

    def enrich_for(self, specs):
        _pools, _included, pairs = self.materialize(specs)
        return lambda chain, address: self.lookup_dex(pairs, chain, address)

    def materialize(self, specs):
        pools = []
        included = []
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
            pairs[spec["address"]] = built
            pairs[spec["address"].lower()] = built
        return pools, included, pairs

    def lookup_dex(self, pairs, chain_id, address):
        direct = pairs.get(address)
        if direct is not None and direct.get("chainId") == chain_id:
            return direct
        folded = pairs.get(str(address).lower())
        if folded is not None and folded.get("chainId") == chain_id:
            return folded
        return None

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

    def usage_rows(self):
        connection = sqlite3.connect(self.db_path)
        try:
            found = connection.execute("""
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'discovery_api_usage'
            """).fetchone()
            if found is None:
                return []
            return connection.execute("""
                SELECT provider, network, endpoint, http_status, billable, job_run_id
                FROM discovery_api_usage
                ORDER BY id
            """).fetchall()
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
            data[name] = connection.execute("SELECT * FROM {0}".format(name)).fetchall()
        connection.close()
        return tables, data


if __name__ == "__main__":
    unittest.main()
