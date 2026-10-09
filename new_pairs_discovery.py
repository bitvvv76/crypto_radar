"""
Поиск новых DEX pools через GeckoTerminal и передача их
в существующий pipeline Crypto Radar.

После save_pair идея идёт обычным путём: watchlist, проверки цены,
paper engine. Отдельного paper-контура здесь нет.
"""

import re
import sqlite3
import sys

from auto_scan import ALLOWED_QUOTE_TOKENS, MAX_NEW_IDEAS, MIN_FINAL_SCORE
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
from discovery_store import SOURCE_GECKO, upsert_candidate
from gecko_discovery import fetch_new_pools, parse_new_pool
from monitor_store import record_job_run, utc_now_text
from network_map import discovery_networks, mapping_for_network
from scanner import filter_valid_pairs, get_pair_by_address
from scoring import (
    calculate_final_score,
    calculate_potential_score,
    calculate_risk_score,
    get_risk_level,
)


JOB_NEW_PAIRS_DISCOVERY = "new_pairs_discovery"
PAGES_PER_NETWORK = 1
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
        _record_failed_run(error)
        print("CRYPTO RADAR — NEW PAIRS DISCOVERY")
        print("Запуск завершился с ошибкой.")
        return 1

    print(format_report(summary))
    return 0


def run_discovery(
    dry_run=False,
    networks=None,
    pages=PAGES_PER_NETWORK,
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

    received, api_error_details, rate_limited = _collect_pools(
        networks,
        pages,
        fetch_pools,
    )
    summary["api_pools_received"] = len(received)
    summary["api_errors"] = len(api_error_details)
    summary["api_error_kinds"] = api_error_details
    summary["rate_limited"] = rate_limited
    if not dry_run:
        create_tables()

    prepared = []
    seen_keys = set()
    for source_network, pool, included in received:
        try:
            parsed = parse_new_pool(
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
        _record_run(started_at, "ok", summary, None)
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
        "duplicates": 0,
        "dex_enriched": 0,
        "dex_not_found": 0,
        "invalid_price": 0,
        "invalid_liquidity": 0,
        "below_score": 0,
        "eligible_score": 0,
        "saved_new_ideas": 0,
        "existing_pairs": 0,
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
        ("Errors", summary["errors"]),
        ("Unsupported networks", summary["unsupported_network"]),
        ("Unsupported quote", summary["unsupported_quote"]),
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

    if summary.get("rate_limited"):
        lines.append("")
        lines.append("GeckoTerminal вернул 429. Повторных запросов не было.")

    preview = summary.get("preview") or []
    if preview:
        lines.append("")
        lines.append("Кандидаты со score >= 70:")
        for item in preview:
            if item["final_score"] < MIN_FINAL_SCORE:
                continue
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
    return None


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

    if not _quote_still_allowed(enriched):
        summary["unsupported_quote"] += 1
        decision["status"] = "unsupported_quote"
        decision["reason"] = "unsupported_quote"
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
    for item in decided:
        try:
            if item["will_save"]:
                outcome = _save_idea(item)
                if outcome == "existing":
                    summary["existing_pairs"] += 1
            if item.get("pool_key") is None:
                continue
            upsert_candidate(_candidate_fields(item), now=now)
        except Exception:
            summary["errors"] += 1


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
        "source": SOURCE_GECKO,
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


def _collect_pools(networks, pages, fetch_pools):
    received = []
    errors = []
    rate_limited = False
    for network in networks:
        if rate_limited:
            break
        for page in range(1, pages + 1):
            result = fetch_pools(network, page)
            if not result.get("ok"):
                errors.append({
                    "source": "geckoterminal",
                    "network": network,
                    "page": page,
                    "kind": result.get("error_kind"),
                })
                if result.get("error_kind") == "rate_limited":
                    rate_limited = True
                break
            pools = result.get("pools") or []
            if not pools:
                break
            included = result.get("included") or []
            for pool in pools:
                received.append((network, pool, included))
    return received, errors, rate_limited


def _count_intake(summary, reason):
    if reason == "unsupported_quote":
        summary["unsupported_quote"] += 1


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


def _quote_still_allowed(pair):
    base_symbol = ((pair.get("baseToken") or {}).get("symbol") or "").upper()
    quote_symbol = ((pair.get("quoteToken") or {}).get("symbol") or "").upper()
    if not base_symbol or not quote_symbol:
        return False
    if base_symbol in ALLOWED_QUOTE_TOKENS:
        return False
    if quote_symbol not in ALLOWED_QUOTE_TOKENS:
        return False
    return True


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


def _default_fetch(network, page):
    return fetch_new_pools(network, page=page)


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
            summary={"source": SOURCE_GECKO, "errors": 1},
            error_text=type(error).__name__,
        )
    except Exception:
        print("DISCOVERY: журнал запуска не записан")


if __name__ == "__main__":
    raise SystemExit(main())
