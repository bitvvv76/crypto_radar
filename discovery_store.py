"""
Метаданные discovery: источник идеи, повторные встречи и решение воронки.

Торговые таблицы здесь не изменяются. Уникальность кандидата:
source + source_network + pool_address.

Отдельно хранятся watermark authenticated discovery и локальный счётчик
CoinGecko Demo credits. API key в эти таблицы не пишется.
"""

import os
import sqlite3
from datetime import datetime
from pathlib import Path

import database
from monitor_store import utc_now_text
from network_map import chain_is_evm


SOURCE_GECKO = "geckoterminal_new_pools"
SOURCE_LEGACY = "dexscreener_legacy_search"
SOURCE_COINGECKO = "coingecko_authenticated_new_pools"
API_PROVIDER_COINGECKO = "coingecko"


def ensure_discovery_tables(db_path=None):
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS discovery_candidates (
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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_discovery_candidates_identity
            ON discovery_candidates (source, source_network, pool_address)
        """)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def upsert_candidate(fields, now=None, db_path=None):
    source = fields.get("source")
    source_network = fields.get("source_network")
    pool_address = fields.get("pool_address")
    if not source or not source_network or not pool_address:
        raise ValueError("discovery candidate identity is incomplete")

    ensure_discovery_tables(db_path)
    moment = utc_now_text(now)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        existing = connection.execute("""
            SELECT id, seen_count, first_seen_at, pair_id,
                   risk_score, potential_score, final_score
            FROM discovery_candidates
            WHERE source = ?
              AND source_network = ?
              AND pool_address = ?
        """, (source, source_network, pool_address)).fetchone()

        if existing is None:
            connection.execute("""
                INSERT INTO discovery_candidates (
                    source,
                    source_network,
                    pool_address,
                    chain_id,
                    pair_address,
                    base_token_address,
                    quote_token_address,
                    base_symbol,
                    quote_symbol,
                    pool_created_at,
                    first_seen_at,
                    last_seen_at,
                    seen_count,
                    status,
                    reason,
                    risk_score,
                    potential_score,
                    final_score,
                    pair_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                source,
                source_network,
                pool_address,
                fields.get("chain_id"),
                fields.get("pair_address"),
                fields.get("base_token_address"),
                fields.get("quote_token_address"),
                fields.get("base_symbol"),
                fields.get("quote_symbol"),
                fields.get("pool_created_at"),
                moment,
                moment,
                1,
                fields.get("status"),
                fields.get("reason"),
                fields.get("risk_score"),
                fields.get("potential_score"),
                fields.get("final_score"),
                fields.get("pair_id"),
            ))
        else:
            pair_id = fields.get("pair_id")
            if pair_id is None:
                pair_id = existing["pair_id"]
            connection.execute("""
                UPDATE discovery_candidates
                SET chain_id = ?,
                    pair_address = ?,
                    base_token_address = ?,
                    quote_token_address = ?,
                    base_symbol = ?,
                    quote_symbol = ?,
                    pool_created_at = ?,
                    last_seen_at = ?,
                    seen_count = ?,
                    status = ?,
                    reason = ?,
                    risk_score = ?,
                    potential_score = ?,
                    final_score = ?,
                    pair_id = ?
                WHERE id = ?
            """, (
                fields.get("chain_id"),
                fields.get("pair_address"),
                fields.get("base_token_address"),
                fields.get("quote_token_address"),
                fields.get("base_symbol"),
                fields.get("quote_symbol"),
                fields.get("pool_created_at"),
                moment,
                existing["seen_count"] + 1,
                fields.get("status"),
                fields.get("reason"),
                _keep_score(fields.get("risk_score"), existing["risk_score"]),
                _keep_score(fields.get("potential_score"), existing["potential_score"]),
                _keep_score(fields.get("final_score"), existing["final_score"]),
                pair_id,
                existing["id"],
            ))
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()

    return get_candidate(source, source_network, pool_address, db_path=db_path)


def get_candidate(source, source_network, pool_address, db_path=None):
    if not _table_ready(db_path):
        return None
    connection = _connect(db_path)
    try:
        row = connection.execute("""
            SELECT *
            FROM discovery_candidates
            WHERE source = ?
              AND source_network = ?
              AND pool_address = ?
        """, (source, source_network, pool_address)).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}
    finally:
        connection.close()


def record_legacy_idea(
    pair,
    risk_score,
    potential_score,
    final_score,
    pair_id,
    now=None,
    db_path=None,
):
    chain_id = pair.get("chainId")
    pair_address = pair.get("pairAddress")
    if not chain_id or not pair_address:
        return None
    base_token = pair.get("baseToken") or {}
    quote_token = pair.get("quoteToken") or {}
    pool_key = pair_address.lower() if chain_is_evm(chain_id) else pair_address
    return upsert_candidate({
        "source": SOURCE_LEGACY,
        "source_network": chain_id,
        "pool_address": pool_key,
        "chain_id": chain_id,
        "pair_address": pair_address,
        "base_token_address": base_token.get("address"),
        "quote_token_address": quote_token.get("address"),
        "base_symbol": base_token.get("symbol"),
        "quote_symbol": quote_token.get("symbol"),
        "pool_created_at": None,
        "status": "saved",
        "reason": None,
        "risk_score": risk_score,
        "potential_score": potential_score,
        "final_score": final_score,
        "pair_id": pair_id,
    }, now=now, db_path=db_path)


def ensure_discovery_runtime_tables(db_path=None):
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS discovery_network_state (
                network TEXT PRIMARY KEY,
                initial_target_cutoff TEXT,
                last_complete_reference TEXT,
                updated_at TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS discovery_api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                network TEXT,
                endpoint TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                http_status INTEGER,
                billable INTEGER NOT NULL,
                job_run_id INTEGER
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_discovery_api_usage_month
            ON discovery_api_usage (provider, billable, requested_at)
        """)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def get_network_state(network, db_path=None):
    connection = _connect_readonly(db_path)
    if connection is None:
        return None
    try:
        row = connection.execute("""
            SELECT network, initial_target_cutoff, last_complete_reference, updated_at
            FROM discovery_network_state
            WHERE network = ?
        """, (network,)).fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def save_initial_target_cutoff(network, cutoff_text, now=None, db_path=None):
    """
    Пишет initial_target_cutoff один раз. Повторный incomplete run
    уже сохранённое значение не сдвигает. Complete reference тоже не трогает.
    """
    ensure_discovery_runtime_tables(db_path)
    moment = utc_now_text(now)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        existing = connection.execute("""
            SELECT initial_target_cutoff, last_complete_reference
            FROM discovery_network_state
            WHERE network = ?
        """, (network,)).fetchone()
        if existing is None:
            connection.execute("""
                INSERT INTO discovery_network_state (
                    network,
                    initial_target_cutoff,
                    last_complete_reference,
                    updated_at
                ) VALUES (?, ?, NULL, ?)
            """, (network, cutoff_text, moment))
        elif existing["last_complete_reference"] is None and not existing["initial_target_cutoff"]:
            connection.execute("""
                UPDATE discovery_network_state
                SET initial_target_cutoff = ?,
                    updated_at = ?
                WHERE network = ?
                  AND last_complete_reference IS NULL
                  AND (initial_target_cutoff IS NULL OR initial_target_cutoff = '')
            """, (cutoff_text, moment, network))
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return get_network_state(network, db_path=db_path)


def save_complete_reference(network, reference_text, now=None, db_path=None):
    ensure_discovery_runtime_tables(db_path)
    moment = utc_now_text(now)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        existing = connection.execute("""
            SELECT network
            FROM discovery_network_state
            WHERE network = ?
        """, (network,)).fetchone()
        if existing is None:
            connection.execute("""
                INSERT INTO discovery_network_state (
                    network,
                    initial_target_cutoff,
                    last_complete_reference,
                    updated_at
                ) VALUES (?, NULL, ?, ?)
            """, (network, reference_text, moment))
        else:
            connection.execute("""
                UPDATE discovery_network_state
                SET last_complete_reference = ?,
                    updated_at = ?
                WHERE network = ?
            """, (reference_text, moment, network))
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    return get_network_state(network, db_path=db_path)


def record_api_usage(
    provider,
    network,
    endpoint,
    requested_at,
    http_status,
    billable,
    job_run_id=None,
    db_path=None,
):
    ensure_discovery_runtime_tables(db_path)
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        cursor = connection.execute("""
            INSERT INTO discovery_api_usage (
                provider,
                network,
                endpoint,
                requested_at,
                http_status,
                billable,
                job_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            provider,
            network,
            endpoint,
            requested_at,
            http_status,
            1 if billable else 0,
            job_run_id,
        ))
        row_id = cursor.lastrowid
        connection.execute("COMMIT")
        return row_id
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def attach_usage_job_run(row_ids, job_run_id, db_path=None):
    if not row_ids or job_run_id is None:
        return
    if not _table_exists(db_path, "discovery_api_usage"):
        return
    connection = _connect(db_path)
    try:
        connection.execute("BEGIN")
        for row_id in row_ids:
            connection.execute("""
                UPDATE discovery_api_usage
                SET job_run_id = ?
                WHERE id = ?
                  AND job_run_id IS NULL
            """, (job_run_id, row_id))
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def monthly_billable_count(now=None, db_path=None, provider=API_PROVIDER_COINGECKO):
    connection = _connect_readonly(db_path)
    if connection is None:
        return 0
    start, end = _month_bounds(now)
    try:
        row = connection.execute("""
            SELECT COUNT(*)
            FROM discovery_api_usage
            WHERE provider = ?
              AND billable = 1
              AND requested_at >= ?
              AND requested_at < ?
        """, (provider, start, end)).fetchone()
        if row is None:
            return 0
        return int(row[0])
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def _month_bounds(now):
    moment = _month_moment(now)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return (
        start.strftime("%Y-%m-%d %H:%M:%S"),
        end.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _month_moment(now):
    if isinstance(now, datetime):
        return now.replace(tzinfo=None, microsecond=0)
    text = utc_now_text(now)
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")


def _db_path(db_path):
    return db_path or database.DB_NAME


def _connect_readonly(db_path):
    path = _db_path(db_path)
    if not path or not os.path.exists(path):
        return None
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(db_path, name):
    connection = _connect_readonly(db_path)
    if connection is None:
        return False
    try:
        found = connection.execute("""
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            LIMIT 1
        """, (name,)).fetchone()
        return found is not None
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def _keep_score(new_value, previous_value):
    if new_value is None:
        return previous_value
    return new_value


def _table_ready(db_path):
    connection = sqlite3.connect(db_path or database.DB_NAME)
    try:
        found = connection.execute("""
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = 'discovery_candidates'
            LIMIT 1
        """).fetchone()
        return found is not None
    finally:
        connection.close()


def _connect(db_path):
    connection = sqlite3.connect(db_path or database.DB_NAME)
    connection.row_factory = sqlite3.Row
    connection.isolation_level = None
    connection.execute("PRAGMA foreign_keys = ON")
    return connection
