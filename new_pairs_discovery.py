"""
Поиск новых DEX pools и передача их в существующий pipeline Crypto Radar.

Production-источник v0.10.2 — CoinGecko Demo Onchain API,
coingecko_authenticated_new_pools. Публичный GeckoTerminal из CLI
не вызывается. _run_legacy_discovery оставлен только для контракта
тестов v0.10.

После save_pair идея идёт обычным путём: watchlist, проверки цены,
paper engine. Отдельного paper-контура здесь нет.

С v0.10.3 каждое matched DexScreener наблюдение дописывается в
discovery_market_snapshots. Scoring, пороги и состояние кандидата
не меняются. Новый score по истории не считается.
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import database
from auto_scan import ALLOWED_QUOTE_TOKENS, MAX_NEW_IDEAS, MIN_FINAL_SCORE
from coingecko_discovery import (
    demo_api_key,
    fetch_network_new_pools,
    monthly_budget_limit,
    new_pools_path,
    parse_new_pool as parse_coingecko_pool,
)
from config import MIN_LIQUIDITY_USD
from database import (
    add_to_watchlist,
    base_token_exists,
    create_tables,
    get_connection,
    get_pair_id,
    pair_exists,
    save_pair,
)
from discovery_store import (
    API_PROVIDER_COINGECKO,
    SOURCE_COINGECKO,
    SOURCE_GECKO,
    append_market_snapshot,
    attach_usage_job_run,
    ensure_discovery_runtime_tables,
    get_network_state,
    monthly_billable_count,
    record_api_usage,
    save_complete_reference,
    save_initial_target_cutoff,
    upsert_candidate,
)
from monitor_store import record_job_run, utc_now_text
from network_map import (
    coingecko_discovery_interval,
    coingecko_discovery_network_ids,
    discovery_networks,
    mapping_for_network,
)
from scanner import filter_valid_pairs, get_pair_by_address
from scoring import (
    calculate_final_score,
    calculate_potential_score,
    calculate_risk_score,
    get_risk_level,
)
from stablecoins import quote_token_reason


JOB_NEW_PAIRS_DISCOVERY = "new_pairs_discovery"
MAX_PAGES_PER_NETWORK = 3
COVERAGE_HISTORY_LIMIT = 100
DISCOVERY_OVERLAP = timedelta(minutes=15)
INITIAL_LOOKBACK = timedelta(minutes=30)
_COMPLETE_STOPS = frozenset(("cutoff", "empty_page"))
_SNAPSHOT_BLOCK_STATUSES = frozenset((
    "unsupported_network",
    "unsupported_quote",
    "unverified_quote_token",
    "missing_pool_address",
    "missing_network",
    "invalid_pool_address",
    "missing_base_token",
    "missing_quote_token",
    "reverse_pair",
    "DEX_PAIR_NOT_FOUND",
))
_SNAPSHOT_BLOCK_REASONS = frozenset((
    "unsupported_network",
    "unsupported_quote",
    "unverified_quote_token",
    "missing_pool_address",
    "dex_pair_not_found",
    "dex_quote_rejected",
))
_EVM_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}|0x[0-9a-fA-F]{64}")
_SOLANA_ADDRESS_RE = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    unknown = [arg for arg in args if arg != "--dry-run"]
    if unknown:
        print("Неизвестный аргумент. Допустимо: --dry-run")
        return 2

    dry_run = "--dry-run" in args
    try:
        summary = run_discovery(dry_run=dry_run)
    except Exception as error:
        if not dry_run:
            _record_failed_run(error)
        print("CRYPTO RADAR — COINGECKO AUTH DISCOVERY")
        print("Запуск завершился с ошибкой.")
        return 1

    if summary.get("source") == SOURCE_COINGECKO:
        report = format_auth_report(summary)
    else:
        report = format_report(summary)
    print(_redact_secret(report))
    if summary.get("missing_api_key"):
        return 1
    return 0


def run_discovery(
    dry_run=False,
    networks=None,
    pages=MAX_PAGES_PER_NETWORK,
    fetch_pools=None,
    enrich_pair=None,
    now=None,
):
    # Явный fetch_pools оставляет прежний GeckoTerminal runner: так
    # зафиксирован контракт тестов v0.10. CLI его не передаёт.
    if fetch_pools is not None:
        return _run_legacy_discovery(
            dry_run=dry_run,
            networks=networks,
            pages=pages,
            fetch_pools=fetch_pools,
            enrich_pair=enrich_pair,
            now=now,
        )
    return run_authenticated_discovery(
        dry_run=dry_run,
        networks=networks,
        pages=pages,
        enrich_pair=enrich_pair,
        now=now,
    )


def _run_legacy_discovery(
    dry_run=False,
    networks=None,
    pages=MAX_PAGES_PER_NETWORK,
    fetch_pools=None,
    enrich_pair=None,
    now=None,
):
    summary = empty_summary(dry_run=dry_run)
    started_at = utc_now_text(now)
    if fetch_pools is None:
        fetch_pools = _default_fetch
    if enrich_pair is None:
        enrich_pair = get_pair_by_address
    if networks is None:
        networks = discovery_networks()

    cutoffs = {
        network: _network_cutoff(network, now)
        for network in networks
    }
    page_cap = _page_cap(pages)
    summary["max_pages"] = page_cap
    received, api_error_details, rate_limited, pagination = _collect_pools(
        networks,
        page_cap,
        fetch_pools,
        cutoffs,
    )
    summary["api_pools_received"] = len(received)
    summary["api_errors"] = len(api_error_details)
    summary["api_error_kinds"] = api_error_details
    summary["rate_limited"] = rate_limited
    summary["pagination"] = pagination
    _apply_coverage(summary, networks)
    if not dry_run:
        create_tables()

    prepared = []
    seen_keys = set()
    for source_network, pool, included in received:
        try:
            parsed = _legacy_parse_new_pool(
                pool,
                included,
                source_network=source_network,
            )
        except Exception:
            summary["errors"] += 1
            continue

        if not parsed.get("ok"):
            summary["errors"] += 1
            continue

        pool_key = _dedup_key(parsed)
        if pool_key is not None:
            if pool_key in seen_keys:
                summary["duplicates"] += 1
                continue
            seen_keys.add(pool_key)
        prepared.append((parsed, pool_key))

    summary["unique_pools"] = len(prepared)
    decided = []
    for parsed, pool_key in prepared:
        try:
            decision = _decide_pool(parsed, pool_key, enrich_pair, summary)
        except Exception:
            summary["errors"] += 1
            continue
        if decision is not None:
            decided.append(decision)

    _apply_save_cap(decided)
    if not dry_run:
        _persist(decided, now, summary)
    else:
        for item in decided:
            if item["will_save"]:
                item["status"] = "dry_run"
                item["reason"] = "dry_run"

    summary["saved_new_ideas"] = _count_saved(decided, dry_run)
    summary["preview"] = [_preview(item) for item in decided if item["score_ready"]]
    summary["score_counts"] = _score_counts(decided)

    if not dry_run:
        run_status = "ok" if summary["coverage_complete"] else "partial"
        _record_run(started_at, run_status, summary, None)
    return summary


def empty_summary(dry_run=False):
    return {
        "source": SOURCE_GECKO,
        "dry_run": dry_run,
        "api_pools_received": 0,
        "unique_pools": 0,
        "supported_network": 0,
        "unsupported_network": 0,
        "supported_quote": 0,
        "unsupported_quote": 0,
        "unverified_quote": 0,
        "cutoff": None,
        "cutoff_basis": None,
        "max_pages": MAX_PAGES_PER_NETWORK,
        "pagination": {},
        "coverage_complete": False,
        "networks_complete": 0,
        "networks_incomplete": 0,
        "duplicates": 0,
        "dex_enriched": 0,
        "dex_not_found": 0,
        "invalid_price": 0,
        "invalid_liquidity": 0,
        "below_score": 0,
        "eligible_score": 0,
        "saved_new_ideas": 0,
        "existing_pairs": 0,
        "snapshots_written": 0,
        "snapshot_errors": 0,
        "errors": 0,
        "api_errors": 0,
        "api_error_kinds": [],
        "rate_limited": False,
        "preview": [],
        "score_counts": {},
    }


def format_report(summary):
    lines = [
        "CRYPTO RADAR — NEW PAIRS DISCOVERY",
        "==================================",
        "",
    ]
    if summary.get("dry_run"):
        lines.append("Режим: dry-run. pairs и watchlist не изменяются.")
        lines.append("")

    rows = [
        ("GeckoTerminal pools received", summary["api_pools_received"]),
        ("Unique pools", summary["unique_pools"]),
        ("Supported networks", summary["supported_network"]),
        ("Supported quote pairs", summary["supported_quote"]),
        ("DexScreener confirmed", summary["dex_enriched"]),
        ("Below score 70", summary["below_score"]),
        ("Eligible score >=70", summary["eligible_score"]),
        ("Existing ideas", summary["existing_pairs"]),
        ("New ideas saved", summary["saved_new_ideas"]),
        ("Market snapshots written", summary.get("snapshots_written", 0)),
        ("Snapshot errors", summary.get("snapshot_errors", 0)),
        ("Errors", summary["errors"]),
        ("Unsupported networks", summary["unsupported_network"]),
        ("Unsupported quote", summary["unsupported_quote"]),
        ("Unverified quote", summary.get("unverified_quote", 0)),
        ("Duplicates", summary["duplicates"]),
        ("DexScreener not found", summary["dex_not_found"]),
        ("Invalid price", summary["invalid_price"]),
        ("Invalid liquidity", summary["invalid_liquidity"]),
        ("API errors", summary["api_errors"]),
    ]
    label_width = max(len(label) for label, _value in rows)
    for label, value in rows:
        lines.append("{0} {1}".format((label + ":").ljust(label_width + 2), value))

    lines.append("")
    lines.append("Источник:")
    lines.append(summary.get("source") or SOURCE_GECKO)
    lines.append("")
    lines.append("coverage_complete: {0}".format(
        "yes" if summary.get("coverage_complete") else "no",
    ))
    lines.append("networks_complete: {0}".format(summary.get("networks_complete", 0)))
    lines.append("networks_incomplete: {0}".format(summary.get("networks_incomplete", 0)))
    pagination = summary.get("pagination") or {}
    if not pagination:
        lines.append("Pages per network: нет запрошенных страниц")
    for network, info in pagination.items():
        lines.append("")
        lines.append("{0}:".format(network))
        lines.append("pages_fetched: {0}".format(info.get("pages_fetched", 0)))
        lines.append("oldest_pool_created_at: {0}".format(
            info.get("oldest_pool_created_at") or "-",
        ))
        lines.append("cutoff: {0}".format(info.get("cutoff") or "-"))
        lines.append("stop_reason: {0}".format(info.get("stop_reason") or "-"))
        lines.append("coverage_complete: {0}".format(
            "yes" if info.get("coverage_complete") else "no",
        ))

    if summary.get("rate_limited"):
        lines.append("")
        lines.append("GeckoTerminal вернул 429. Повторных запросов не было.")

    preview = [
        item for item in (summary.get("preview") or [])
        if item.get("final_score") is not None and item["final_score"] >= MIN_FINAL_SCORE
    ]
    if preview:
        lines.append("")
        lines.append("Кандидаты со score >= 70:")
        for item in preview:
            lines.append(
                "- {0}/{1} {2} score {3} {4}".format(
                    item["base_symbol"],
                    item["quote_symbol"],
                    item["chain_id"],
                    item["final_score"],
                    item["action"],
                )
            )

    counts = summary.get("score_counts") or {}
    if counts:
        lines.append("")
        lines.append("Распределение final_score:")
        for score in sorted(counts, key=int):
            lines.append("- {0}: {1}".format(score, counts[score]))

    return "\n".join(lines)


def intake_reason(parsed):
    network = parsed.get("source_network")
    if not network:
        return "missing_network"
    mapping = mapping_for_network(network)
    if mapping is None:
        return "unsupported_network"

    address = parsed.get("pool_address")
    if not address:
        return "missing_pool_address"
    if not _address_is_valid(mapping, address):
        return "invalid_pool_address"

    base_symbol = parsed.get("base_symbol") or ""
    quote_symbol = parsed.get("quote_symbol") or ""
    base_address = parsed.get("base_token_address") or ""
    quote_address = parsed.get("quote_token_address") or ""
    if not base_symbol or not base_address:
        return "missing_base_token"
    if not quote_symbol or not quote_address:
        return "missing_quote_token"

    evm = mapping["evm"]
    if _same_address(base_address, quote_address, evm) or base_symbol.upper() == quote_symbol.upper():
        return "reverse_pair"
    if base_symbol.upper() in ALLOWED_QUOTE_TOKENS:
        return "reverse_pair"
    if quote_symbol.upper() not in ALLOWED_QUOTE_TOKENS:
        return "unsupported_quote"
    return quote_token_reason(network, quote_symbol, quote_address)


def find_existing_pair(chain_id, pair_address, base_token_address, evm):
    if not chain_id:
        return None
    try:
        if pair_address and pair_exists(chain_id, pair_address):
            return "pair_address"
        if base_token_address and base_token_exists(chain_id, base_token_address):
            return "base_token_address"
        if not evm:
            return None
        return _find_existing_evm_case(chain_id, pair_address, base_token_address)
    except sqlite3.OperationalError:
        return None


def _find_existing_evm_case(chain_id, pair_address, base_token_address):
    connection = get_connection()
    try:
        if pair_address:
            row = connection.execute("""
                SELECT id
                FROM pairs
                WHERE chain_id = ?
                  AND pair_address IS NOT NULL
                  AND lower(pair_address) = lower(?)
                LIMIT 1
            """, (chain_id, pair_address)).fetchone()
            if row is not None:
                return "pair_address"
        if base_token_address:
            row = connection.execute("""
                SELECT id
                FROM pairs
                WHERE chain_id = ?
                  AND base_token_address IS NOT NULL
                  AND lower(base_token_address) = lower(?)
                LIMIT 1
            """, (chain_id, base_token_address)).fetchone()
            if row is not None:
                return "base_token_address"
        return None
    finally:
        connection.close()


def _decide_pool(parsed, pool_key, enrich_pair, summary):
    reason = intake_reason(parsed)
    mapping = mapping_for_network(parsed.get("source_network"))
    decision = _decision_shell(parsed, pool_key, mapping)

    if reason == "unsupported_network":
        summary["unsupported_network"] += 1
        decision["status"] = "unsupported_network"
        decision["reason"] = "unsupported_network"
        return decision
    if reason == "missing_network":
        summary["errors"] += 1
        decision["status"] = "missing_network"
        decision["reason"] = "missing_network"
        return decision
    if reason is not None:
        summary["supported_network"] += 1
        _count_intake(summary, reason)
        decision["status"] = reason
        decision["reason"] = reason
        return decision

    summary["supported_network"] += 1
    summary["supported_quote"] += 1

    enriched = enrich_pair(mapping["chain_id"], parsed.get("pool_address"))
    if not enriched:
        summary["dex_not_found"] += 1
        decision["status"] = "DEX_PAIR_NOT_FOUND"
        decision["reason"] = "dex_pair_not_found"
        return decision
    if not _same_market(parsed, enriched, mapping):
        summary["dex_not_found"] += 1
        decision["status"] = "DEX_PAIR_NOT_FOUND"
        decision["reason"] = "dex_pair_not_found"
        return decision

    if isinstance(enriched, dict):
        decision["enriched"] = enriched
        decision["market_matched"] = True

    market_reason = _market_reason(enriched)
    if market_reason == "invalid_price":
        summary["invalid_price"] += 1
        decision["status"] = "invalid_price"
        decision["reason"] = "invalid_price"
        return decision
    if market_reason == "invalid_liquidity":
        summary["invalid_liquidity"] += 1
        decision["status"] = "invalid_liquidity"
        decision["reason"] = "invalid_liquidity"
        return decision

    dex_quote_reason = _dex_quote_reason(enriched, mapping["chain_id"])
    if dex_quote_reason == "dex_quote_rejected":
        summary["dex_not_found"] += 1
        decision["status"] = "DEX_PAIR_NOT_FOUND"
        decision["reason"] = "dex_quote_rejected"
        return decision
    if dex_quote_reason is not None:
        summary["dex_not_found"] += 1
        decision["status"] = dex_quote_reason
        decision["reason"] = dex_quote_reason
        return decision

    summary["dex_enriched"] += 1
    risk_score = calculate_risk_score(enriched)
    potential_score = calculate_potential_score(enriched)
    final_score = calculate_final_score(enriched)
    decision["enriched"] = enriched
    decision["risk_score"] = risk_score
    decision["potential_score"] = potential_score
    decision["final_score"] = final_score
    decision["score_ready"] = True
    decision["pair_address"] = enriched.get("pairAddress")
    decision["chain_id"] = enriched.get("chainId")

    existing = find_existing_pair(
        enriched.get("chainId"),
        enriched.get("pairAddress"),
        (enriched.get("baseToken") or {}).get("address"),
        mapping["evm"],
    )
    if final_score < MIN_FINAL_SCORE:
        summary["below_score"] += 1
        decision["status"] = "BELOW_SCORE"
        decision["reason"] = "below_score"
        return decision

    summary["eligible_score"] += 1
    if existing:
        summary["existing_pairs"] += 1
        decision["status"] = "existing_pair"
        decision["reason"] = existing
        decision["existing"] = True
        return decision

    decision["status"] = "eligible"
    decision["reason"] = None
    return decision


def _apply_save_cap(decided):
    eligible = [
        item for item in decided
        if item["status"] == "eligible"
    ]
    eligible.sort(key=_save_sort_key)
    for index, item in enumerate(eligible):
        if index < MAX_NEW_IDEAS:
            item["will_save"] = True
        else:
            item["will_save"] = False
            item["status"] = "eligible_not_saved"
            item["reason"] = "max_new_ideas"


def _persist(decided, now, summary):
    observed_at = utc_now_text(now)
    for item in decided:
        candidate = None
        try:
            if item["will_save"]:
                outcome = _save_idea(item)
                if outcome == "existing":
                    summary["existing_pairs"] += 1
            if item.get("pool_key") is None:
                continue
            candidate = upsert_candidate(_candidate_fields(item), now=now)
        except Exception:
            summary["errors"] += 1
            continue
        _record_market_snapshot(item, candidate, observed_at, summary)


def _record_market_snapshot(item, candidate, observed_at, summary):
    try:
        if not _snapshot_allowed(item, candidate):
            return
        enriched = item.get("enriched")
        append_market_snapshot(
            candidate.get("id"),
            observed_at,
            _pair_number(enriched, "priceUsd"),
            _pair_number(enriched, "liquidity", "usd"),
            _pair_number(enriched, "volume", "h24"),
            _pair_number(enriched, "priceChange", "h24"),
            _observation_score(item, "risk_score"),
            _observation_score(item, "potential_score"),
            _observation_score(item, "final_score"),
            item.get("status"),
            item.get("reason"),
        )
    except Exception:
        summary["snapshot_errors"] += 1
        return
    summary["snapshots_written"] += 1


def _snapshot_allowed(item, candidate):
    if not isinstance(candidate, dict) or candidate.get("id") is None:
        return False
    if not item.get("market_matched"):
        return False
    if not isinstance(item.get("enriched"), dict):
        return False
    if item.get("status") in _SNAPSHOT_BLOCK_STATUSES:
        return False
    if item.get("reason") in _SNAPSHOT_BLOCK_REASONS:
        return False
    return True


def _observation_score(item, name):
    if not item.get("score_ready"):
        return None
    value = item.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _pair_number(pair, *path):
    current = pair
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return _optional_real(current)


def _optional_real(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _save_idea(item):
    pair = item.get("enriched")
    if not isinstance(pair, dict):
        item["will_save"] = False
        item["status"] = "error"
        item["reason"] = "missing_enrichment"
        return "error"

    saved = save_pair(
        pair,
        item["risk_score"],
        get_risk_level(item["risk_score"]),
        item["potential_score"],
        item["final_score"],
    )
    if not saved:
        item["will_save"] = False
        item["status"] = "existing_pair"
        item["reason"] = "save_pair_rejected"
        return "existing"

    pair_id = get_pair_id(pair.get("chainId"), pair.get("pairAddress"))
    item["pair_id"] = pair_id
    item["status"] = "saved"
    item["reason"] = None
    if pair_id is not None:
        add_to_watchlist(
            pair_id,
            "Автоматически добавлена после new_pairs_discovery: Final score {0}".format(
                item["final_score"]
            ),
        )
    return "saved"


def _candidate_fields(item):
    return {
        "source": item.get("source") or SOURCE_GECKO,
        "source_network": item["source_network"],
        "pool_address": item["pool_key"][1],
        "chain_id": item.get("chain_id"),
        "pair_address": item.get("pair_address"),
        "base_token_address": item.get("base_token_address"),
        "quote_token_address": item.get("quote_token_address"),
        "base_symbol": item.get("base_symbol"),
        "quote_symbol": item.get("quote_symbol"),
        "pool_created_at": item.get("pool_created_at"),
        "status": item.get("status"),
        "reason": item.get("reason"),
        "risk_score": item.get("risk_score"),
        "potential_score": item.get("potential_score"),
        "final_score": item.get("final_score"),
        "pair_id": item.get("pair_id"),
    }


def _collect_pools(networks, pages, fetch_pools, cutoffs):
    received = []
    errors = []
    pagination = {}
    rate_limited = False
    for network in networks:
        if rate_limited:
            break
        cutoff, cutoff_basis = cutoffs[network]
        (
            fetched,
            stop_reason,
            network_received,
            network_errors,
            rate_limited,
            oldest,
        ) = _collect_network(network, pages, fetch_pools, cutoff)
        received.extend(network_received)
        errors.extend(network_errors)
        pagination[network] = _coverage_record(
            cutoff,
            cutoff_basis,
            fetched,
            stop_reason,
            oldest,
        )
    for network in networks:
        if network in pagination:
            continue
        cutoff, cutoff_basis = cutoffs[network]
        pagination[network] = _coverage_record(
            cutoff,
            cutoff_basis,
            0,
            "rate_limited",
            None,
        )
    return received, errors, rate_limited, pagination


def _collect_network(network, pages, fetch_pools, cutoff):
    received = []
    errors = []
    fetched = 0
    oldest = None
    stop_reason = "max_pages"

    def finish(reason, limited):
        return fetched, reason, received, errors, limited, oldest

    for page in range(1, pages + 1):
        try:
            result = fetch_pools(network, page)
        except Exception:
            errors.append(_api_error(network, page, "malformed_page"))
            return finish("malformed_page", False)

        if not isinstance(result, dict):
            errors.append(_api_error(network, page, "malformed_page"))
            return finish("malformed_page", False)

        if not result.get("ok"):
            kind = result.get("error_kind") or "http_error"
            errors.append(_api_error(network, page, kind))
            if kind == "rate_limited":
                return finish("rate_limited", True)
            if kind in {"malformed_json", "malformed_page"}:
                return finish("malformed_page", False)
            return finish(kind, False)

        pools = result.get("pools")
        included = result.get("included")
        if not isinstance(pools, list) or (included is not None and not isinstance(included, list)):
            errors.append(_api_error(network, page, "malformed_page"))
            return finish("malformed_page", False)
        if included is None:
            included = []

        fetched += 1
        if not pools:
            return finish("empty_page", False)

        for pool in pools:
            received.append((network, pool, included))

        page_oldest = _oldest_pool_created_at(pools)
        if page_oldest is not None and (oldest is None or page_oldest < oldest):
            oldest = page_oldest
        if page_oldest is not None and page_oldest <= cutoff:
            return finish("cutoff", False)
        if page == pages:
            stop_reason = "max_pages"
    return finish(stop_reason, False)


def _api_error(network, page, kind):
    return {
        "source": "geckoterminal",
        "network": network,
        "page": page,
        "kind": kind,
    }


def _count_intake(summary, reason):
    if reason == "unsupported_quote":
        summary["unsupported_quote"] += 1
    elif reason == "unverified_quote_token":
        summary["unverified_quote"] += 1


def _market_reason(pair):
    if not isinstance(pair, dict):
        return "invalid_price"
    if pair.get("priceUsd") is None:
        return "invalid_price"
    liquidity = pair.get("liquidity")
    liquidity_usd = None
    if isinstance(liquidity, dict):
        liquidity_usd = liquidity.get("usd")
    if not isinstance(liquidity_usd, (int, float)) or isinstance(liquidity_usd, bool):
        return "invalid_liquidity"
    if liquidity_usd < MIN_LIQUIDITY_USD:
        return "invalid_liquidity"
    if not filter_valid_pairs([pair]):
        return "invalid_liquidity"
    return None


def _dex_quote_reason(pair, chain_id):
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    base_symbol = (base.get("symbol") or "").upper()
    quote_symbol = (quote.get("symbol") or "").upper()
    if not base_symbol or not quote_symbol:
        return "dex_quote_rejected"
    if base_symbol in ALLOWED_QUOTE_TOKENS:
        return "dex_quote_rejected"
    if quote_symbol not in ALLOWED_QUOTE_TOKENS:
        return "dex_quote_rejected"
    return quote_token_reason(chain_id, quote_symbol, quote.get("address"))


def _same_market(parsed, enriched, mapping):
    if enriched.get("chainId") != mapping["chain_id"]:
        return False
    if not _same_address(
        parsed.get("pool_address"),
        enriched.get("pairAddress"),
        mapping["evm"],
    ):
        return False
    dex_base = (enriched.get("baseToken") or {}).get("address")
    if not _same_address(parsed.get("base_token_address"), dex_base, mapping["evm"]):
        return False
    return True


def _decision_shell(parsed, pool_key, mapping):
    return {
        "source_network": parsed.get("source_network"),
        "pool_key": pool_key,
        "pool_created_at": parsed.get("pool_created_at"),
        "base_symbol": parsed.get("base_symbol"),
        "quote_symbol": parsed.get("quote_symbol"),
        "base_token_address": parsed.get("base_token_address"),
        "quote_token_address": parsed.get("quote_token_address"),
        "chain_id": None if mapping is None else mapping["chain_id"],
        "pair_address": parsed.get("pool_address"),
        "status": None,
        "reason": None,
        "risk_score": None,
        "potential_score": None,
        "final_score": None,
        "pair_id": None,
        "enriched": None,
        "market_matched": False,
        "existing": False,
        "will_save": False,
        "score_ready": False,
    }


def _dedup_key(parsed):
    address = parsed.get("pool_address")
    network = parsed.get("source_network")
    if not address or not network:
        return None
    mapping = mapping_for_network(network)
    evm = bool(mapping and mapping["evm"])
    normalized = address.lower() if evm else address
    return (network, normalized)


def _address_is_valid(mapping, address):
    if mapping["evm"]:
        return _EVM_ADDRESS_RE.fullmatch(address) is not None
    if mapping["chain_id"] == "solana":
        return _SOLANA_ADDRESS_RE.fullmatch(address) is not None
    return False


def _same_address(left, right, evm):
    if not left or not right:
        return False
    if evm:
        return left.lower() == right.lower()
    return left == right


def _save_sort_key(item):
    created = item.get("pool_created_at") or "9999-12-31T23:59:59Z"
    pool_key = item.get("pool_key") or ("", "")
    return (-int(item["final_score"]), created, pool_key[1])


def _count_saved(decided, dry_run):
    if dry_run:
        return 0
    return sum(1 for item in decided if item["status"] == "saved")


def _preview(item):
    if item["final_score"] is None:
        action = item["status"]
    elif item["existing"]:
        action = "existing"
    elif item["will_save"] and item["status"] == "dry_run":
        action = "dry-run"
    elif item["status"] == "saved":
        action = "saved"
    elif item["reason"] == "max_new_ideas":
        action = "over-max"
    elif item["status"] == "BELOW_SCORE":
        action = "below-score"
    else:
        action = item["status"]
    return {
        "base_symbol": item.get("base_symbol"),
        "quote_symbol": item.get("quote_symbol"),
        "chain_id": item.get("chain_id"),
        "final_score": item.get("final_score"),
        "action": action,
        "pool_created_at": item.get("pool_created_at"),
    }


def _score_counts(decided):
    counts = {}
    for item in decided:
        if item.get("final_score") is None:
            continue
        score = str(item["final_score"])
        counts[score] = counts.get(score, 0) + 1
    return counts


def _legacy_parse_new_pool(pool, included=None, source_network=None):
    from gecko_discovery import parse_new_pool
    return parse_new_pool(pool, included, source_network=source_network)


def _default_fetch(network, page):
    # Публичный GeckoTerminal из этого модуля больше не вызывается.
    return {
        "ok": False,
        "pools": [],
        "included": [],
        "error_kind": "deprecated_source",
        "status_code": None,
        "billable": False,
    }


def _page_cap(pages):
    if isinstance(pages, int) and 1 <= pages <= MAX_PAGES_PER_NETWORK:
        return pages
    return MAX_PAGES_PER_NETWORK


def _apply_coverage(summary, networks):
    pagination = summary.get("pagination") or {}
    complete = 0
    for network in networks:
        info = pagination.get(network) or {}
        if info.get("coverage_complete") is True:
            complete += 1
    summary["networks_complete"] = complete
    summary["networks_incomplete"] = len(networks) - complete
    summary["coverage_complete"] = summary["networks_incomplete"] == 0
    cutoffs = []
    bases = []
    for network in networks:
        info = pagination.get(network) or {}
        cutoffs.append(info.get("cutoff"))
        bases.append(info.get("cutoff_basis"))
    if cutoffs and len(set(cutoffs)) == 1 and len(set(bases)) == 1:
        summary["cutoff"] = cutoffs[0]
        summary["cutoff_basis"] = bases[0]
    elif cutoffs:
        summary["cutoff"] = None
        summary["cutoff_basis"] = "per_network"


def _coverage_record(cutoff, cutoff_basis, pages_fetched, stop_reason, oldest):
    return {
        "pages_fetched": pages_fetched,
        "oldest_pool_created_at": None if oldest is None else _format_cutoff(oldest),
        "cutoff": _format_cutoff(cutoff),
        "cutoff_basis": cutoff_basis,
        "stop_reason": stop_reason,
        "coverage_complete": stop_reason in _COMPLETE_STOPS,
    }


def _network_cutoff(network, now):
    moment = _parse_time(utc_now_text(now))
    previous = _last_complete_started_at(network)
    if previous is None:
        return moment - INITIAL_LOOKBACK, "initial_lookback"
    return previous - DISCOVERY_OVERLAP, "overlap"


def _last_complete_started_at(network):
    for started_at, summary_json in _recent_discovery_runs():
        parsed = _coverage_from_summary(summary_json, network)
        if parsed is not True:
            continue
        moment = _parse_time(started_at)
        if moment is not None:
            return moment
    return None


def _coverage_from_summary(summary_json, network):
    if not summary_json:
        return False
    try:
        summary = json.loads(summary_json)
    except ValueError:
        return False
    if not isinstance(summary, dict):
        return False
    pagination = summary.get("pagination")
    if not isinstance(pagination, dict):
        return False
    info = pagination.get(network)
    if not isinstance(info, dict):
        return False
    return info.get("coverage_complete") is True


def _recent_discovery_runs():
    path = database.DB_NAME
    if not path or not os.path.exists(path):
        return []
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    try:
        return connection.execute("""
            SELECT started_at, summary_json
            FROM monitor_job_runs
            WHERE job_name = ?
            ORDER BY id DESC
            LIMIT ?
        """, (JOB_NEW_PAIRS_DISCOVERY, COVERAGE_HISTORY_LIMIT)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _oldest_pool_created_at(pools):
    oldest = None
    for pool in pools:
        if not isinstance(pool, dict):
            continue
        attributes = pool.get("attributes")
        if not isinstance(attributes, dict):
            continue
        created = _parse_time(attributes.get("pool_created_at"))
        if created is None:
            continue
        if oldest is None or created < oldest:
            oldest = created
    return oldest


def _parse_time(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    if "." in text:
        text = text.split(".", 1)[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _format_cutoff(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_run(started_at, status, summary, error):
    public_summary = {
        key: value
        for key, value in summary.items()
        if key not in {"preview"}
    }
    try:
        record_job_run(
            None,
            JOB_NEW_PAIRS_DISCOVERY,
            started_at,
            status,
            summary=public_summary,
            error_text=None if error is None else type(error).__name__,
        )
    except Exception:
        print("DISCOVERY: журнал запуска не записан")


def _record_failed_run(error):
    try:
        record_job_run(
            None,
            JOB_NEW_PAIRS_DISCOVERY,
            utc_now_text(),
            "error",
            summary={"source": SOURCE_COINGECKO, "errors": 1},
            error_text=type(error).__name__,
        )
    except Exception:
        print("DISCOVERY: журнал запуска не записан")


def run_authenticated_discovery(
    dry_run=False,
    networks=None,
    pages=MAX_PAGES_PER_NETWORK,
    fetch_pools=None,
    enrich_pair=None,
    now=None,
    budget_limit=None,
):
    summary = empty_auth_summary(dry_run=dry_run)
    started_at = utc_now_text(now)
    moment = _parse_time(started_at)
    limit = monthly_budget_limit(budget_limit)
    summary["monthly_budget_limit"] = limit
    chosen = _auth_networks(networks)
    due, skipped = _split_due_networks(chosen, moment)
    summary["due_networks"] = due
    summary["skipped_not_due_networks"] = skipped
    summary["monthly_budget_used"] = monthly_billable_count(now)
    summary["monthly_budget_remaining"] = max(0, limit - summary["monthly_budget_used"])

    if fetch_pools is None and demo_api_key() is None:
        summary["missing_api_key"] = True
        summary["coverage_complete"] = False
        summary["networks_complete"] = 0
        summary["networks_incomplete"] = len(due)
        for network in due:
            summary["pagination"][network] = _open_coverage("missing_api_key")
        if not dry_run:
            _record_auth_run(started_at, "error", summary)
        return summary

    if not dry_run:
        create_tables()
        ensure_discovery_runtime_tables()

    page_cap = _page_cap(pages)
    summary["max_pages"] = page_cap
    cutoffs = {
        network: _auth_cutoff(network, moment, dry_run, now)
        for network in due
    }
    if fetch_pools is None:
        fetch_pools = _default_auth_fetch
    if enrich_pair is None:
        enrich_pair = get_pair_by_address

    received, api_error_details, pagination, stats = _collect_fair(
        due,
        page_cap,
        fetch_pools,
        cutoffs,
        now,
        dry_run,
        limit,
    )
    summary["api_pools_received"] = len(received)
    summary["pools_received"] = len(received)
    summary["api_errors"] = stats["api_errors"]
    summary["api_error_kinds"] = api_error_details
    summary["api_requests"] = stats["api_requests"]
    summary["api_200"] = stats["api_200"]
    summary["rate_limited"] = stats["halt"] == "rate_limited"
    summary["pagination"] = pagination
    summary["unrecorded_billable"] = stats["local_billable"] if dry_run else 0
    used = stats["baseline"] if dry_run else stats["baseline"] + stats["local_billable"]
    summary["monthly_budget_used"] = used
    summary["monthly_budget_remaining"] = max(0, limit - used)
    _apply_auth_coverage(summary, due)

    prepared = []
    seen_keys = set()
    for source_network, pool, included in received:
        try:
            parsed = parse_coingecko_pool(
                pool,
                included,
                source_network=source_network,
            )
        except Exception:
            summary["errors"] += 1
            continue
        if not parsed.get("ok"):
            summary["errors"] += 1
            continue
        pool_key = _dedup_key(parsed)
        if pool_key is not None:
            if pool_key in seen_keys:
                summary["duplicates"] += 1
                continue
            seen_keys.add(pool_key)
        prepared.append((parsed, pool_key))

    summary["unique_pools"] = len(prepared)
    decided = []
    for parsed, pool_key in prepared:
        try:
            decision = _decide_pool(parsed, pool_key, enrich_pair, summary)
        except Exception:
            summary["errors"] += 1
            continue
        if decision is None:
            continue
        decision["source"] = SOURCE_COINGECKO
        decided.append(decision)

    _apply_save_cap(decided)
    if not dry_run:
        _persist(decided, now, summary)
        for network in due:
            info = pagination.get(network) or {}
            if info.get("coverage_complete") is True:
                save_complete_reference(network, started_at, now=now)
    else:
        for item in decided:
            if item["will_save"]:
                item["status"] = "dry_run"
                item["reason"] = "dry_run"

    summary["saved_new_ideas"] = _count_saved(decided, dry_run)
    summary["dex_confirmed"] = summary["dex_enriched"]
    summary["pools_received"] = summary["api_pools_received"]
    summary["preview"] = [_preview(item) for item in decided if item["score_ready"]]
    summary["score_counts"] = _score_counts(decided)

    if not dry_run:
        status = "ok" if summary["coverage_complete"] else "partial"
        run_id = _record_auth_run(started_at, status, summary)
        attach_usage_job_run(stats["usage_ids"], run_id)
    return summary


def empty_auth_summary(dry_run=False):
    summary = empty_summary(dry_run=dry_run)
    summary["source"] = SOURCE_COINGECKO
    summary["due_networks"] = []
    summary["skipped_not_due_networks"] = []
    summary["api_requests"] = 0
    summary["api_200"] = 0
    summary["pools_received"] = 0
    summary["dex_confirmed"] = 0
    summary["monthly_budget_used"] = 0
    summary["monthly_budget_remaining"] = 0
    summary["monthly_budget_limit"] = monthly_budget_limit()
    summary["missing_api_key"] = False
    summary["unrecorded_billable"] = 0
    return summary


def format_auth_report(summary):
    lines = [
        "CRYPTO RADAR — COINGECKO AUTH DISCOVERY",
        "=======================================",
        "",
    ]
    if summary.get("dry_run"):
        lines.append("Режим: dry-run. База не изменяется.")
        lines.append("")
    if summary.get("missing_api_key"):
        lines.append("COINGECKO_DEMO_API_KEY is not set.")
        lines.append("Discovery did not call CoinGecko.")
        lines.append("")

    lines.append("Source:")
    lines.append(summary.get("source") or SOURCE_COINGECKO)
    lines.append("")
    lines.append("Due networks:")
    due = summary.get("due_networks") or []
    if due:
        for network in due:
            lines.append(network.upper())
    else:
        lines.append("none")
    lines.append("")
    lines.append("Skipped not due:")
    skipped = summary.get("skipped_not_due_networks") or []
    if skipped:
        for network in skipped:
            lines.append(network.upper())
    else:
        lines.append("none")
    lines.append("")
    lines.append("API:")
    lines.append("Requests: {0}".format(summary.get("api_requests", 0)))
    lines.append("HTTP 200: {0}".format(summary.get("api_200", 0)))
    lines.append("Errors: {0}".format(summary.get("api_errors", 0)))
    lines.append("")
    limit = summary.get("monthly_budget_limit", 0)
    used = summary.get("monthly_budget_used", 0)
    remaining = summary.get("monthly_budget_remaining", 0)
    lines.append("Monthly budget:")
    lines.append("Used: {0} / {1}".format(used, limit))
    lines.append("Remaining: {0}".format(remaining))
    if summary.get("dry_run"):
        lines.append("API usage was not recorded.")
    lines.append("")
    lines.append("Pools received: {0}".format(summary.get("pools_received", 0)))
    lines.append("Unique: {0}".format(summary.get("unique_pools", 0)))
    lines.append("Verified quote: {0}".format(summary.get("supported_quote", 0)))
    lines.append("Unsupported quote: {0}".format(summary.get("unsupported_quote", 0)))
    lines.append("Unverified quote: {0}".format(summary.get("unverified_quote", 0)))
    lines.append("DexScreener confirmed: {0}".format(summary.get("dex_confirmed", 0)))
    lines.append("DexScreener not found: {0}".format(summary.get("dex_not_found", 0)))
    lines.append("Invalid price: {0}".format(summary.get("invalid_price", 0)))
    lines.append("Invalid liquidity: {0}".format(summary.get("invalid_liquidity", 0)))
    lines.append("Below score: {0}".format(summary.get("below_score", 0)))
    lines.append("Eligible: {0}".format(summary.get("eligible_score", 0)))
    lines.append("Existing: {0}".format(summary.get("existing_pairs", 0)))
    lines.append("Saved: {0}".format(summary.get("saved_new_ideas", 0)))
    lines.append("Market snapshots written: {0}".format(summary.get("snapshots_written", 0)))
    lines.append("Snapshot errors: {0}".format(summary.get("snapshot_errors", 0)))
    lines.append("")
    lines.append("NETWORK COVERAGE")
    pagination = summary.get("pagination") or {}
    if not pagination:
        lines.append("none")
    for network, info in pagination.items():
        lines.append("")
        lines.append("{0}:".format(network.upper()))
        lines.append("pages: {0}".format(info.get("pages_fetched", 0)))
        lines.append("cutoff: {0}".format(info.get("cutoff") or "-"))
        lines.append("oldest: {0}".format(info.get("oldest_pool_created_at") or "-"))
        lines.append("stop: {0}".format(info.get("stop_reason") or "-"))
        lines.append("complete: {0}".format(
            "yes" if info.get("coverage_complete") else "no",
        ))
    return "\n".join(lines)


def _auth_networks(requested):
    allowed = coingecko_discovery_network_ids()
    if requested is None:
        return list(allowed)
    selected = set(requested)
    return [network for network in allowed if network in selected]


def _split_due_networks(networks, moment):
    due = []
    skipped = []
    for network in networks:
        if _auth_network_due(network, moment):
            due.append(network)
        else:
            skipped.append(network)
    return due, skipped


def _auth_network_due(network, moment):
    interval = coingecko_discovery_interval(network)
    if interval is None or moment is None:
        return False
    state = get_network_state(network)
    if state is None or not state.get("last_complete_reference"):
        return True
    reference = _parse_time(state.get("last_complete_reference"))
    if reference is None:
        return True
    return moment - reference >= interval


def _auth_cutoff(network, moment, dry_run, now):
    state = get_network_state(network)
    if state is not None and state.get("last_complete_reference"):
        reference = _parse_time(state.get("last_complete_reference"))
        if reference is not None:
            return reference - DISCOVERY_OVERLAP, "overlap"
    if state is not None and state.get("initial_target_cutoff"):
        stored = _parse_time(state.get("initial_target_cutoff"))
        if stored is not None:
            return stored, "initial_target_cutoff"
    cutoff = moment - INITIAL_LOOKBACK
    if not dry_run:
        save_initial_target_cutoff(network, _format_cutoff(cutoff), now=now)
    return cutoff, "initial_target_cutoff"


def _collect_fair(due, pages, fetch_pools, cutoffs, now, dry_run, budget_limit):
    baseline = monthly_billable_count(now)
    local = 0
    usage_ids = []
    requested_at = utc_now_text(now)
    errors = []
    states = {}
    for network in due:
        states[network] = {
            "active": True,
            "pages_fetched": 0,
            "oldest": None,
            "stop_reason": None,
            "received": [],
        }
    halt = None
    api_requests = 0
    api_200 = 0
    api_errors = 0

    for page in range(1, pages + 1):
        for network in due:
            state = states[network]
            if not state["active"]:
                continue
            if halt is not None:
                state["active"] = False
                state["stop_reason"] = halt
                continue
            if baseline + local >= budget_limit:
                halt = "monthly_budget_exhausted"
                state["active"] = False
                state["stop_reason"] = halt
                continue

            try:
                result = fetch_pools(network, page)
            except Exception:
                result = None
            if not isinstance(result, dict):
                result = {
                    "ok": False,
                    "error_kind": "malformed_page",
                    "pools": [],
                    "included": [],
                    "billable": False,
                    "status_code": None,
                }

            api_requests += 1
            billable = _result_billable(result)
            if billable:
                local += 1
                api_200 += 1
            endpoint = result.get("endpoint") or new_pools_path(network)
            if not endpoint:
                endpoint = "/onchain/networks/{0}/new_pools".format(network)
            if not dry_run:
                usage_ids.append(record_api_usage(
                    API_PROVIDER_COINGECKO,
                    network,
                    endpoint,
                    requested_at,
                    result.get("status_code"),
                    billable,
                ))

            if not result.get("ok"):
                api_errors += 1
                kind = result.get("error_kind") or "http_error"
                errors.append(_auth_api_error(network, page, kind))
                state["active"] = False
                state["stop_reason"] = kind
                if kind in {"rate_limited", "auth_error", "missing_api_key"}:
                    halt = kind
                continue

            pools = result.get("pools")
            included = result.get("included")
            if not isinstance(pools, list) or (
                included is not None and not isinstance(included, list)
            ):
                api_errors += 1
                errors.append(_auth_api_error(network, page, "malformed_page"))
                state["active"] = False
                state["stop_reason"] = "malformed_page"
                continue
            if included is None:
                included = []

            state["pages_fetched"] += 1
            if not pools:
                state["active"] = False
                state["stop_reason"] = "empty_page"
                continue
            for pool in pools:
                state["received"].append((network, pool, included))
            page_oldest = _oldest_pool_created_at(pools)
            if page_oldest is not None and (
                state["oldest"] is None or page_oldest < state["oldest"]
            ):
                state["oldest"] = page_oldest
            cutoff, _basis = cutoffs[network]
            if page_oldest is not None and page_oldest <= cutoff:
                state["active"] = False
                state["stop_reason"] = "cutoff"
                continue
            if page_oldest is None and page == pages:
                state["active"] = False
                state["stop_reason"] = "missing_timestamps"
                continue
            if page == pages:
                state["active"] = False
                state["stop_reason"] = "max_pages"

    received = []
    pagination = {}
    for network in due:
        state = states[network]
        if state["stop_reason"] is None:
            state["stop_reason"] = "max_pages"
        cutoff, basis = cutoffs[network]
        received.extend(state["received"])
        pagination[network] = _coverage_record(
            cutoff,
            basis,
            state["pages_fetched"],
            state["stop_reason"],
            state["oldest"],
        )
    return received, errors, pagination, {
        "api_requests": api_requests,
        "api_200": api_200,
        "api_errors": api_errors,
        "local_billable": local,
        "baseline": baseline,
        "usage_ids": usage_ids,
        "halt": halt,
    }


def _result_billable(result):
    if not isinstance(result, dict):
        return False
    if "billable" in result:
        return bool(result["billable"])
    status = result.get("status_code")
    if status == 200:
        return True
    return bool(result.get("ok")) and status in (None, 200)


def _auth_api_error(network, page, kind):
    return {
        "source": SOURCE_COINGECKO,
        "network": network,
        "page": page,
        "kind": kind,
    }


def _open_coverage(stop_reason):
    return {
        "pages_fetched": 0,
        "oldest_pool_created_at": None,
        "cutoff": None,
        "cutoff_basis": None,
        "stop_reason": stop_reason,
        "coverage_complete": False,
    }


def _apply_auth_coverage(summary, due):
    pagination = summary.get("pagination") or {}
    complete = 0
    for network in due:
        info = pagination.get(network) or {}
        if info.get("coverage_complete") is True:
            complete += 1
    summary["networks_complete"] = complete
    summary["networks_incomplete"] = len(due) - complete
    summary["coverage_complete"] = summary["networks_incomplete"] == 0


def _default_auth_fetch(network, page):
    return fetch_network_new_pools(network, page=page)


def _record_auth_run(started_at, status, summary):
    hidden = {"preview", "unrecorded_billable"}
    public_summary = _scrub_value({
        key: value
        for key, value in summary.items()
        if key not in hidden
    })
    try:
        return record_job_run(
            None,
            JOB_NEW_PAIRS_DISCOVERY,
            started_at,
            status,
            summary=public_summary,
            error_text=None,
        )
    except Exception:
        print("DISCOVERY: журнал запуска не записан")
        return None


def _redact_secret(text):
    return _scrub_value(text)


def _scrub_value(value):
    secret = demo_api_key()
    if not secret or len(secret) < 8:
        return value
    return _replace_secret(value, secret)


def _replace_secret(value, secret):
    if isinstance(value, str):
        return value.replace(secret, "[redacted]")
    if isinstance(value, dict):
        return {
            key: _replace_secret(item, secret)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_secret(item, secret) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
