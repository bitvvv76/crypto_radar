import sqlite3


DB_NAME = "crypto_radar.db"

def get_connection():
    connection = sqlite3.connect(DB_NAME)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def create_tables():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pairs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chain_id TEXT,
            dex_id TEXT,
            pair_address TEXT,
            pair_symbol TEXT,
            base_symbol TEXT,
            base_token_address TEXT,
            quote_symbol TEXT,
            price_usd REAL,
            liquidity_usd REAL,
            volume_24h REAL,
            price_change_24h REAL,
            risk_score INTEGER,
            risk_level TEXT,
            potential_score INTEGER,
            final_score INTEGER,
            url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("PRAGMA table_info(pairs)")
    pair_columns = {
        row[1]
        for row in cursor.fetchall()
    }

    if "base_token_address" not in pair_columns:
        cursor.execute("""
            ALTER TABLE pairs
            ADD COLUMN base_token_address TEXT
        """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair_id INTEGER,
            check_period TEXT,
            old_price_usd REAL,
            new_price_usd REAL,
            price_change_percent REAL,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (pair_id) REFERENCES pairs (id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair_id INTEGER NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'watching',
            note TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (pair_id) REFERENCES pairs (id) ON DELETE CASCADE
        )
    """)

    connection.commit()
    connection.close()

def save_pair(pair, risk_score, risk_level, potential_score, final_score):
    chain_id = pair.get("chainId")
    pair_address = pair.get("pairAddress")

    base_token = pair.get("baseToken", {})
    base_token_address = base_token.get("address")

    if pair_exists(chain_id, pair_address):
        return False

    if base_token_exists(chain_id, base_token_address):
        return False

    connection = get_connection()
    cursor = connection.cursor()
    quote_token = pair.get("quoteToken", {})
    liquidity = pair.get("liquidity", {})
    volume = pair.get("volume", {})
    price_change = pair.get("priceChange", {})

    base_symbol = base_token.get("symbol")
    quote_symbol = quote_token.get("symbol")

    pair_symbol = f"{base_symbol}/{quote_symbol}"

    cursor.execute("""
        INSERT INTO pairs (
            chain_id,
            dex_id,
            pair_address,
            pair_symbol,
            base_symbol,
            base_token_address,
            quote_symbol,
            price_usd,
            liquidity_usd,
            volume_24h,
            price_change_24h,
            risk_score,
            risk_level,
            potential_score,
            final_score,
            url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        pair.get("chainId"),
        pair.get("dexId"),
        pair.get("pairAddress"),
        pair_symbol,
        base_symbol,
        base_token_address,
        quote_symbol,
        pair.get("priceUsd"),
        liquidity.get("usd"),
        volume.get("h24"),
        price_change.get("h24"),
        risk_score,
        risk_level,
        potential_score,
        final_score,
        pair.get("url")
    ))

    connection.commit()
    connection.close()

    return True



def get_pairs_count():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("SELECT COUNT(*) FROM pairs")
    count = cursor.fetchone()[0]

    connection.close()

    return count

def pair_exists(chain_id, pair_address):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT id FROM pairs
        WHERE chain_id = ? AND pair_address = ?
        LIMIT 1
    """, (
        chain_id,
        pair_address
    ))

    result = cursor.fetchone()

    connection.close()

    return result is not None

def get_pair_id(chain_id, pair_address):
    if not chain_id or not pair_address:
        return None

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT id
        FROM pairs
        WHERE chain_id = ?
          AND pair_address = ?
        LIMIT 1
    """, (
        chain_id,
        pair_address,
    ))

    row = cursor.fetchone()

    connection.close()

    if row is None:
        return None

    return row[0]

def base_token_exists(chain_id, base_token_address):
    if not chain_id or not base_token_address:
        return False

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT id
        FROM pairs
        WHERE chain_id = ?
          AND base_token_address = ?
        LIMIT 1
        """,
        (
            chain_id,
            base_token_address,
        ),
    )

    row = cursor.fetchone()
    connection.close()

    return row is not None

def get_last_pairs(limit=5):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            id,
            chain_id,
            dex_id,
            pair_address,
            pair_symbol,
            price_usd,
            risk_score,
            potential_score,
            final_score,
            created_at
        FROM pairs
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()

    connection.close()

    return rows

def calculate_price_change_percent(old_price, new_price):
    if old_price is None:
        return None

    if new_price is None:
        return None

    if old_price == 0:
        return None

    change_percent = ((new_price - old_price) / old_price) * 100

    return round(change_percent, 2)

def save_price_check(pair_id, check_period, old_price_usd, new_price_usd):
    if price_check_exists(pair_id, check_period):
        return None
    
    price_change_percent = calculate_price_change_percent(
        old_price_usd,
        new_price_usd
    )

    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        INSERT INTO price_checks (
            pair_id,
            check_period,
            old_price_usd,
            new_price_usd,
            price_change_percent
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        pair_id,
        check_period,
        old_price_usd,
        new_price_usd,
        price_change_percent
    ))

    connection.commit()
    connection.close()

    return price_change_percent

def get_last_price_checks(limit=5):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            id,
            pair_id,
            check_period,
            old_price_usd,
            new_price_usd,
            price_change_percent,
            checked_at
        FROM price_checks
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))

    rows = cursor.fetchall()

    connection.close()

    return rows

def get_price_checks_count():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("SELECT COUNT(*) FROM price_checks")
    count = cursor.fetchone()[0]

    connection.close()

    return count

def get_pair_by_id(pair_id):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            id,
            chain_id,
            pair_address,
            pair_symbol,
            price_usd,
            final_score
        FROM pairs
        WHERE id = ?
        LIMIT 1
    """, (pair_id,))

    row = cursor.fetchone()

    connection.close()

    return row

def price_check_exists(pair_id, check_period):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT id FROM price_checks
        WHERE pair_id = ? AND check_period = ?
        LIMIT 1
    """, (
        pair_id,
        check_period
    ))

    result = cursor.fetchone()

    connection.close()

    return result is not None

def get_price_checks_for_pair(pair_id):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            check_period,
            old_price_usd,
            new_price_usd,
            price_change_percent,
            checked_at
        FROM price_checks
        WHERE pair_id = ?
        AND check_period IN ('1h', '6h', '24h', '7d')
        ORDER BY
            CASE check_period
                WHEN '1h' THEN 1
                WHEN '6h' THEN 2
                WHEN '24h' THEN 3
                WHEN '7d' THEN 4
                ELSE 5
            END
    """, (pair_id,))

    rows = cursor.fetchall()

    connection.close()

    return rows

def get_all_pairs():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            id,
            chain_id,
            dex_id,
            pair_symbol,
            price_usd,
            risk_score,
            potential_score,
            final_score,
            created_at
        FROM pairs
        ORDER BY final_score DESC, id DESC
    """)

    rows = cursor.fetchall()

    connection.close()

    return rows

def get_price_checks_count_for_pair(pair_id):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT COUNT(*)
        FROM price_checks
        WHERE pair_id = ?
        AND check_period IN ('1h', '6h', '24h', '7d')
    """, (pair_id,))

    count = cursor.fetchone()[0]

    connection.close()

    return count

def get_pairs_for_next_checks():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            id,
            chain_id,
            dex_id,
            pair_symbol,
            price_usd,
            risk_score,
            potential_score,
            final_score,
            created_at
        FROM pairs
        ORDER BY final_score DESC, id DESC
    """)

    rows = cursor.fetchall()

    connection.close()

    return rows

def get_existing_check_periods_for_pair(pair_id):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        SELECT check_period
        FROM price_checks
        WHERE pair_id = ?
        AND check_period IN ('1h', '6h', '24h', '7d')
    """, (pair_id,))

    rows = cursor.fetchall()

    connection.close()

    periods = []

    for row in rows:
        periods.append(row[0])

    return periods

def add_to_watchlist(pair_id, note=None):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        INSERT OR IGNORE INTO watchlist (
            pair_id,
            note
        )
        SELECT ?, ?
        WHERE EXISTS (
            SELECT 1
            FROM pairs
            WHERE id = ?
        )
    """, (
        pair_id,
        note,
        pair_id
    ))

    added = cursor.rowcount > 0

    connection.commit()
    connection.close()

    return added

def get_watchlist(status=None):
    connection = get_connection()
    cursor = connection.cursor()

    query = """
        SELECT
            w.id,
            w.pair_id,
            p.chain_id,
            p.dex_id,
            p.pair_symbol,
            p.price_usd,
            p.risk_score,
            p.risk_level,
            p.potential_score,
            p.final_score,
            w.status,
            w.note,
            w.added_at,
            w.updated_at
        FROM watchlist AS w
        JOIN pairs AS p ON p.id = w.pair_id
    """

    parameters = ()

    if status is not None:
        query += " WHERE w.status = ?"
        parameters = (status,)

    query += " ORDER BY p.final_score DESC, w.id DESC"

    cursor.execute(query, parameters)
    rows = cursor.fetchall()

    connection.close()

    return rows

def update_watchlist_status(pair_id, status, note=None):
    allowed_statuses = {
        "watching",
        "confirmed",
        "rejected",
        "archived",
    }

    if status not in allowed_statuses:
        return False

    connection = get_connection()
    cursor = connection.cursor()

    if note is None:
        cursor.execute("""
            UPDATE watchlist
            SET
                status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE pair_id = ?
        """, (
            status,
            pair_id,
        ))
    else:
        cursor.execute("""
            UPDATE watchlist
            SET
                status = ?,
                note = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE pair_id = ?
        """, (
            status,
            note,
            pair_id,
        ))

    updated = cursor.rowcount > 0

    connection.commit()
    connection.close()

    return updated

def remove_from_watchlist(pair_id):
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        DELETE FROM watchlist
        WHERE pair_id = ?
    """, (pair_id,))

    removed = cursor.rowcount > 0

    connection.commit()
    connection.close()

    return removed

