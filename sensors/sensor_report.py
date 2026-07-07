import argparse
import sqlite3

from sensors.scam_risk_sensor import ScamRiskSensor


DB_PATH = "crypto_radar.db"


def run_case(title: str, idea: dict):
    print("=" * 60)
    print(title)
    print("=" * 60)

    sensor = ScamRiskSensor()
    result = sensor.analyze(idea)

    print(result.to_text())
    print()


def load_latest_idea_from_db(db_path: str = DB_PATH) -> dict:
    """
    Загружает последнюю идею из локальной SQLite-базы.

    Важно:
    - только чтение
    - без записи в БД
    - без изменения структуры БД
    - без подключения к auto_scan.py
    """

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                id,
                pair_symbol,
                chain_id,
                dex_id,
                final_score,
                risk_score,
                liquidity_usd,
                volume_24h,
                price_change_24h
            FROM pairs
            ORDER BY id DESC
            LIMIT 1
            """
        )

        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def run_latest_from_db():
    idea = load_latest_idea_from_db()

    if not idea:
        print("Нет идей в локальной базе.")
        return

    print("DB IDEA:")
    print(idea)
    print()

    sensor = ScamRiskSensor()
    result = sensor.analyze(idea)

    print(result.to_text())


def run_test_cases():
    """
    Ручной тест Sensors Core v0.1.

    Этот режим не подключен к боевому автосканеру.
    Используется только для проверки логики сенсора.
    """

    ok_idea = {
        "pair_symbol": "AI/USDC",
        "final_score": 80,
        "risk_score": 20,
        "liquidity_usd": 150_000,
        "volume_24h": 50_000,
        "price_change_24h": 12.5,
    }

    warning_idea = {
        "pair_symbol": "HOT/USDC",
        "final_score": 80,
        "risk_score": 20,
        "liquidity_usd": 120_000,
        "volume_24h": 8_000,
        "price_change_24h": 45.0,
    }

    reject_idea = {
        "pair_symbol": "USDC/SCAM",
        "final_score": 55,
        "risk_score": 85,
        "liquidity_usd": 5_000,
        "volume_24h": 0,
        "price_change_24h": 120.0,
    }

    run_case("CASE 1: нормальная идея", ok_idea)
    run_case("CASE 2: перегретая идея / warning", warning_idea)
    run_case("CASE 3: слабая или мусорная идея / reject", reject_idea)


def main():
    parser = argparse.ArgumentParser(
        description="Manual sensor report for Crypto Radar."
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Run Scam/Risk Sensor for the latest idea from local crypto_radar.db",
    )

    args = parser.parse_args()

    if args.latest:
        run_latest_from_db()
    else:
        run_test_cases()


if __name__ == "__main__":
    main()