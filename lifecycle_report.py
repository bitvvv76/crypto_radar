
# -*- coding: utf-8 -*-

import sqlite3
import sys
from statistics import mean


DB_PATH = "crypto_radar.db"
TRACKED_PERIODS = ("1h", "6h", "24h", "7d")


def configure_console():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def format_percent(value):
    if value is None:
        return "—"
    return f"{value:+.2f}%"


def print_line():
    print("-" * 72)


def get_lifecycle_status(checks):
    ch6 = checks.get("6h")
    ch24 = checks.get("24h")

    if ch24 is None:
        return "WAITING_24H"

    if ch24 <= 0:
        return "WEAK_24H"

    if ch6 is not None and ch6 > 30:
        return "PUMP_RISK"

    if ch24 > 15:
        return "PUMP_RISK"

    if ch6 is not None and ch6 > 0 and ch24 > 0:
        return "CONFIRMED_STABLE"

    if ch24 > 0:
        return "RECOVERED_24H"

    return "UNKNOWN"


def get_lifecycle_meaning(status):
    meanings = {
        "CONFIRMED_STABLE": "stable_confirmation / стабильное подтверждение",
        "PUMP_RISK": "high_momentum_risk / риск пампа",
        "RECOVERED_24H": "cautious_watch / восстановление после слабости",
        "WEAK_24H": "weak_candidate / early_reject / слабая идея",
        "WAITING_24H": "ожидается 24h проверка",
        "WAITING_7D": "ожидается 7d проверка",
        "UNKNOWN": "неопределённый статус",
    }
    return meanings.get(status, "неопределённый статус")


def load_lifecycle_rows():
    """
    Read-only lifecycle report.

    Важно:
    - база открывается в mode=ro;
    - используются только SELECT-запросы;
    - scoring.py не меняется;
    - auto_scan.py не меняется;
    - database.py не меняется;
    - структура БД не меняется;
    - записи в БД нет.
    """

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH ranked_checks AS (
                SELECT
                    id,
                    pair_id,
                    check_period,
                    price_change_percent,
                    checked_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY pair_id, check_period
                        ORDER BY checked_at DESC, id DESC
                    ) AS rn
                FROM price_checks
                WHERE check_period IN ('1h', '6h', '24h', '7d')
            )
            SELECT
                p.id,
                p.pair_symbol,
                p.chain_id,
                p.dex_id,
                p.final_score,
                p.risk_score,
                p.potential_score,
                p.price_change_24h,
                p.created_at,
                rc.check_period,
                rc.price_change_percent,
                rc.checked_at
            FROM pairs p
            LEFT JOIN ranked_checks rc
                ON rc.pair_id = p.id
               AND rc.rn = 1
            WHERE EXISTS (
                SELECT 1
                FROM price_checks pc
                WHERE pc.pair_id = p.id
                  AND pc.check_period IN ('1h', '6h', '24h', '7d')
            )
            ORDER BY p.id ASC, rc.check_period ASC
            """
        )

        rows = cur.fetchall()
    finally:
        conn.close()

    ideas = {}

    for row in rows:
        pair_id = row["id"]

        if pair_id not in ideas:
            ideas[pair_id] = {
                "id": row["id"],
                "pair_symbol": row["pair_symbol"],
                "chain_id": row["chain_id"],
                "dex_id": row["dex_id"],
                "final_score": row["final_score"],
                "risk_score": row["risk_score"],
                "potential_score": row["potential_score"],
                "price_change_24h": row["price_change_24h"],
                "created_at": row["created_at"],
                "checks": {},
                "checked_at": {},
            }

        period = row["check_period"]

        if period in TRACKED_PERIODS:
            ideas[pair_id]["checks"][period] = row["price_change_percent"]
            ideas[pair_id]["checked_at"][period] = row["checked_at"]

    return list(ideas.values())


def build_group_stats(ideas):
    groups = {}

    for idea in ideas:
        status = idea["lifecycle_status"]
        change_7d = idea["checks"].get("7d")

        if status not in groups:
            groups[status] = {
                "ideas": [],
                "done_7d": [],
                "waiting_7d": [],
            }

        groups[status]["ideas"].append(idea)

        if change_7d is None and idea["checks"].get("24h") is not None:
            groups[status]["waiting_7d"].append(idea)
        elif change_7d is not None:
            groups[status]["done_7d"].append(change_7d)

    return groups


def print_group_summary(groups):
    print("1. Lifecycle-группы")
    print_line()

    for status in sorted(groups.keys()):
        group = groups[status]
        done_values = group["done_7d"]

        print(f"{status}")
        print(f"  смысл: {get_lifecycle_meaning(status)}")
        print(f"  всего идей: {len(group['ideas'])}")
        print(f"  DONE_7D: {len(done_values)}")
        print(f"  WAITING_7D: {len(group['waiting_7d'])}")

        if done_values:
            print(f"  средний 7d: {format_percent(mean(done_values))}")
            print(f"  худший 7d: {format_percent(min(done_values))}")
            print(f"  лучший 7d: {format_percent(max(done_values))}")
        else:
            print("  7d статистика: пока нет завершённых 7d")

        print()


def print_done_7d(ideas):
    print("2. DONE_7D")
    print_line()

    done = [idea for idea in ideas if idea["checks"].get("7d") is not None]

    if not done:
        print("Нет идей с завершённой 7d проверкой.")
        print()
        return

    for idea in done:
        checks = idea["checks"]

        print(
            f"#{idea['id']} {idea['pair_symbol']} | "
            f"score={idea['final_score']} | "
            f"lifecycle={idea['lifecycle_status']} | "
            f"1h={format_percent(checks.get('1h'))} | "
            f"6h={format_percent(checks.get('6h'))} | "
            f"24h={format_percent(checks.get('24h'))} | "
            f"7d={format_percent(checks.get('7d'))}"
        )

    print()


def print_waiting_7d(ideas):
    print("3. WAITING_7D")
    print_line()

    waiting = [
        idea
        for idea in ideas
        if idea["checks"].get("24h") is not None
        and idea["checks"].get("7d") is None
    ]

    if not waiting:
        print("Нет идей, ожидающих 7d проверку.")
        print()
        return

    for idea in waiting:
        checks = idea["checks"]

        print(
            f"#{idea['id']} {idea['pair_symbol']} | "
            f"score={idea['final_score']} | "
            f"lifecycle={idea['lifecycle_status']} | "
            f"1h={format_percent(checks.get('1h'))} | "
            f"6h={format_percent(checks.get('6h'))} | "
            f"24h={format_percent(checks.get('24h'))} | "
            f"7d=ждём"
        )

    print()


def print_notes():
    print("4. Ограничения v0.3 draft")
    print_line()
    print("- read-only отчёт")
    print("- база открывается через SQLite mode=ro")
    print("- используются только SELECT-запросы")
    print("- scoring.py не меняется")
    print("- auto_scan.py не меняется")
    print("- database.py не меняется")
    print("- price_checker.py не меняется")
    print("- структура БД не меняется")
    print("- записи в БД нет")
    print("- VPS/systemd не трогаются")
    print("- автоторговля и реальные деньги не подключаются")
    print()


def main():
    configure_console()

    ideas = load_lifecycle_rows()

    for idea in ideas:
        idea["lifecycle_status"] = get_lifecycle_status(idea["checks"])

    groups = build_group_stats(ideas)

    print()
    print("Crypto Radar Lifecycle Report v0.3 draft")
    print("Confirmation layer / read-only")
    print_line()
    print(f"Источник: {DB_PATH}")
    print(f"Периоды: {', '.join(TRACKED_PERIODS)}")
    print(f"Идей с lifecycle-проверками: {len(ideas)}")
    print()

    print_group_summary(groups)
    print_done_7d(ideas)
    print_waiting_7d(ideas)
    print_notes()


if __name__ == "__main__":
    main()
