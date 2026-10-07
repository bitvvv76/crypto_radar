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
        "data_missing",
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


def get_paper_connection(db_path=None):
    connection = sqlite3.connect(db_path or DB_NAME)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _paper_row(row):
    if row is None:
        return None

    return {key: row[key] for key in row.keys()}


def ensure_paper_tables(db_path=None):
    """
    Create paper tables on a clean database.

    paper_positions matches the existing VPS table. CREATE TABLE IF NOT EXISTS
    leaves that table untouched: no ALTER and no migration.
    """
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paper_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair_id INTEGER UNIQUE,
            strategy_version TEXT,
            signal_type TEXT,
            final_score INTEGER,
            change_24h REAL,
            status TEXT,
            entry_price REAL,
            entry_time TIMESTAMP,
            last_price REAL,
            last_checked_at TIMESTAMP,
            max_price REAL,
            max_profit_percent REAL,
            drawdown_from_max_percent REAL,
            stop_loss_percent REAL,
            trailing_start_percent REAL,
            trailing_distance_percent REAL,
            max_hold_hours INTEGER,
            exit_price REAL,
            exit_time TIMESTAMP,
            exit_reason TEXT,
            result_percent REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS paper_price_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pair_id INTEGER NOT NULL,
            price_usd REAL NOT NULL,
            observed_at TIMESTAMP NOT NULL,
            profit_percent_from_entry REAL,
            observation_bucket TEXT NOT NULL,
            UNIQUE (pair_id, observation_bucket),
            FOREIGN KEY (pair_id) REFERENCES paper_positions (pair_id)
        )
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_paper_price_marks_pair_observed
        ON paper_price_marks (pair_id, observed_at)
    """)

    connection.commit()
    connection.close()


def get_fresh_24h_paper_candidates(
    min_final_score,
    checked_from,
    checked_to,
    db_path=None,
):
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            p.id AS pair_id,
            p.final_score,
            p.chain_id,
            p.pair_address,
            p.pair_symbol,
            pc.price_change_percent AS change_24h,
            pc.new_price_usd AS entry_price,
            pc.checked_at AS entry_time
        FROM pairs AS p
        JOIN price_checks AS pc
            ON pc.pair_id = p.id
           AND pc.check_period = '24h'
        LEFT JOIN paper_positions AS pp
            ON pp.pair_id = p.id
        WHERE pp.id IS NULL
          AND p.final_score >= ?
          AND pc.price_change_percent IS NOT NULL
          AND pc.new_price_usd > 0
          AND pc.checked_at >= ?
          AND pc.checked_at <= ?
          AND pc.id = (
              SELECT pc2.id
              FROM price_checks AS pc2
              WHERE pc2.pair_id = p.id
                AND pc2.check_period = '24h'
              ORDER BY pc2.checked_at ASC, pc2.id ASC
              LIMIT 1
          )
        ORDER BY p.id ASC
    """, (
        min_final_score,
        checked_from,
        checked_to,
    ))

    rows = [_paper_row(row) for row in cursor.fetchall()]
    connection.close()

    return rows


def open_baseline_position(
    pair_id,
    strategy_version,
    signal_type,
    final_score,
    change_24h,
    entry_price,
    entry_time,
    observation_bucket,
    stop_loss_percent,
    trailing_start_percent,
    trailing_distance_percent,
    max_hold_hours,
    created_at,
    db_path=None,
):
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    try:
        cursor.execute("""
            INSERT INTO paper_positions (
                pair_id,
                strategy_version,
                signal_type,
                final_score,
                change_24h,
                status,
                entry_price,
                entry_time,
                last_price,
                last_checked_at,
                max_price,
                max_profit_percent,
                drawdown_from_max_percent,
                stop_loss_percent,
                trailing_start_percent,
                trailing_distance_percent,
                max_hold_hours,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?)
        """, (
            pair_id,
            strategy_version,
            signal_type,
            final_score,
            change_24h,
            entry_price,
            entry_time,
            entry_price,
            entry_time,
            entry_price,
            stop_loss_percent,
            trailing_start_percent,
            trailing_distance_percent,
            max_hold_hours,
            created_at,
            created_at,
        ))

        cursor.execute("""
            INSERT INTO paper_price_marks (
                pair_id,
                price_usd,
                observed_at,
                profit_percent_from_entry,
                observation_bucket
            )
            VALUES (?, ?, ?, 0, ?)
        """, (
            pair_id,
            entry_price,
            entry_time,
            observation_bucket,
        ))
    except sqlite3.IntegrityError:
        connection.rollback()
        connection.close()
        return False

    connection.commit()
    connection.close()

    return True


def get_paper_position(pair_id, db_path=None):
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM paper_positions
        WHERE pair_id = ?
        LIMIT 1
    """, (pair_id,))

    row = _paper_row(cursor.fetchone())
    connection.close()

    return row


def get_active_tracking_positions(now_text, db_path=None):
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            pp.id,
            pp.pair_id,
            pp.strategy_version,
            pp.signal_type,
            pp.final_score,
            pp.change_24h,
            pp.status,
            pp.entry_price,
            pp.entry_time,
            pp.last_price,
            pp.last_checked_at,
            pp.max_price,
            pp.max_profit_percent,
            pp.drawdown_from_max_percent,
            pp.stop_loss_percent,
            pp.trailing_start_percent,
            pp.trailing_distance_percent,
            pp.max_hold_hours,
            pp.exit_price,
            pp.exit_time,
            pp.exit_reason,
            pp.result_percent,
            p.chain_id,
            p.pair_address,
            p.pair_symbol
        FROM paper_positions AS pp
        JOIN pairs AS p ON p.id = pp.pair_id
        WHERE datetime(pp.entry_time, '+' || pp.max_hold_hours || ' hours')
              >= datetime(?)
        ORDER BY pp.id ASC
    """, (now_text,))

    rows = [_paper_row(row) for row in cursor.fetchall()]
    connection.close()

    return rows


def get_expired_open_positions(now_text, db_path=None):
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT *
        FROM paper_positions
        WHERE status = 'OPEN'
          AND datetime(entry_time, '+' || max_hold_hours || ' hours')
              < datetime(?)
        ORDER BY id ASC
    """, (now_text,))

    rows = [_paper_row(row) for row in cursor.fetchall()]
    connection.close()

    return rows


def mark_exists(pair_id, observation_bucket, db_path=None):
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT id
        FROM paper_price_marks
        WHERE pair_id = ?
          AND observation_bucket = ?
        LIMIT 1
    """, (
        pair_id,
        observation_bucket,
    ))

    row = cursor.fetchone()
    connection.close()

    return row is not None


def insert_price_mark(
    pair_id,
    price_usd,
    observed_at,
    profit_percent_from_entry,
    observation_bucket,
    baseline_update=None,
    db_path=None,
):
    """
    Insert one observation.

    baseline_update is applied in the same transaction only when the insert
    creates a new row and the baseline position is still OPEN.
    """
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    try:
        cursor.execute("""
            INSERT OR IGNORE INTO paper_price_marks (
                pair_id,
                price_usd,
                observed_at,
                profit_percent_from_entry,
                observation_bucket
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            pair_id,
            price_usd,
            observed_at,
            profit_percent_from_entry,
            observation_bucket,
        ))

        inserted = connection.execute("SELECT changes()").fetchone()[0] == 1

        if inserted and baseline_update is not None:
            _apply_baseline_update(cursor, baseline_update)
    except sqlite3.IntegrityError:
        connection.rollback()
        connection.close()
        return False

    connection.commit()
    connection.close()

    return inserted


def _apply_baseline_update(cursor, baseline_update):
    action = baseline_update["action"]
    position_id = baseline_update["position_id"]

    if action == "update":
        cursor.execute("""
            UPDATE paper_positions
            SET
                last_price = ?,
                last_checked_at = ?,
                max_price = ?,
                max_profit_percent = ?,
                drawdown_from_max_percent = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'OPEN'
        """, (
            baseline_update["last_price"],
            baseline_update["last_checked_at"],
            baseline_update["max_price"],
            baseline_update["max_profit_percent"],
            baseline_update["drawdown_from_max_percent"],
            baseline_update["updated_at"],
            position_id,
        ))
        return

    if action == "close":
        cursor.execute("""
            UPDATE paper_positions
            SET
                status = 'CLOSED',
                last_price = ?,
                last_checked_at = ?,
                max_price = ?,
                max_profit_percent = ?,
                drawdown_from_max_percent = ?,
                exit_price = ?,
                exit_time = ?,
                exit_reason = ?,
                result_percent = ?,
                updated_at = ?
            WHERE id = ?
              AND status = 'OPEN'
        """, (
            baseline_update["last_price"],
            baseline_update["last_checked_at"],
            baseline_update["max_price"],
            baseline_update["max_profit_percent"],
            baseline_update["drawdown_from_max_percent"],
            baseline_update["exit_price"],
            baseline_update["exit_time"],
            baseline_update["exit_reason"],
            baseline_update["result_percent"],
            baseline_update["updated_at"],
            position_id,
        ))


def close_open_baseline(
    position_id,
    last_price,
    last_checked_at,
    max_price,
    max_profit_percent,
    drawdown_from_max_percent,
    exit_price,
    exit_time,
    exit_reason,
    result_percent,
    updated_at,
    db_path=None,
):
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    cursor.execute("""
        UPDATE paper_positions
        SET
            status = 'CLOSED',
            last_price = ?,
            last_checked_at = ?,
            max_price = ?,
            max_profit_percent = ?,
            drawdown_from_max_percent = ?,
            exit_price = ?,
            exit_time = ?,
            exit_reason = ?,
            result_percent = ?,
            updated_at = ?
        WHERE id = ?
          AND status = 'OPEN'
    """, (
        last_price,
        last_checked_at,
        max_price,
        max_profit_percent,
        drawdown_from_max_percent,
        exit_price,
        exit_time,
        exit_reason,
        result_percent,
        updated_at,
        position_id,
    ))

    closed = cursor.rowcount > 0
    connection.commit()
    connection.close()

    return closed


def get_price_marks(pair_id, db_path=None):
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            id,
            pair_id,
            price_usd,
            observed_at,
            profit_percent_from_entry,
            observation_bucket
        FROM paper_price_marks
        WHERE pair_id = ?
        ORDER BY observed_at ASC, id ASC
    """, (pair_id,))

    rows = [_paper_row(row) for row in cursor.fetchall()]
    connection.close()

    return rows


def get_last_mark_within_window(pair_id, deadline_text, db_path=None):
    connection = get_paper_connection(db_path)
    cursor = connection.cursor()

    cursor.execute("""
        SELECT
            id,
            pair_id,
            price_usd,
            observed_at,
            profit_percent_from_entry,
            observation_bucket
        FROM paper_price_marks
        WHERE pair_id = ?
          AND observed_at <= ?
        ORDER BY observed_at DESC, id DESC
        LIMIT 1
    """, (
        pair_id,
        deadline_text,
    ))

    row = _paper_row(cursor.fetchone())
    connection.close()

    return row

