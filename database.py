import sqlite3


DB_NAME = "crypto_radar.db"


def get_connection():
    connection = sqlite3.connect(DB_NAME)
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

    connection.commit()
    connection.close()

def save_pair(pair, risk_score, risk_level, potential_score, final_score):
    chain_id = pair.get("chainId")
    pair_address = pair.get("pairAddress")

    if pair_exists(chain_id, pair_address):
        return False

    connection = get_connection()
    cursor = connection.cursor()

    base_token = pair.get("baseToken", {})
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        pair.get("chainId"),
        pair.get("dexId"),
        pair.get("pairAddress"),
        pair_symbol,
        base_symbol,
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

def get_main_price_checks_for_pair(pair_id):
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