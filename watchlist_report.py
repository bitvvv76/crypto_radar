import sys
from check_utils import get_next_check_period
from database import get_price_checks_for_pair, get_watchlist


def print_watchlist_report(status=None):
    rows = get_watchlist(status)

    print("CRYPTO RADAR — WATCHLIST")
    print("========================")

    all_rows = get_watchlist()

    status_counts = {
        "watching": 0,
        "confirmed": 0,
        "rejected": 0,
        "archived": 0,
    }

    for watchlist_row in all_rows:
        watchlist_status = watchlist_row[10]

        if watchlist_status in status_counts:
            status_counts[watchlist_status] += 1

    print(f"Всего идей: {len(all_rows)}")
    print(f"Наблюдаем: {status_counts['watching']}")
    print(f"Подтверждено: {status_counts['confirmed']}")
    print(f"Отклонено: {status_counts['rejected']}")
    print(f"Архив: {status_counts['archived']}")

    if not rows:
        if status is None:
            print("Watchlist пуст.")
        else:
            print(f"Идей со статусом '{status}' нет.")
        return

    for row in rows:
        (
            watchlist_id,
            pair_id,
            chain_id,
            dex_id,
            pair_symbol,
            price_usd,
            risk_score,
            risk_level,
            potential_score,
            final_score,
            watchlist_status,
            note,
            added_at,
            updated_at,
        ) = row

        print()
        print(f"Watchlist ID: {watchlist_id}")
        print(f"Pair ID: {pair_id}")
        print(f"Пара: {pair_symbol}")
        print(f"Сеть: {chain_id}")
        print(f"DEX: {dex_id}")
        print(f"Цена: ${price_usd}")
        print(f"Risk: {risk_score} — {risk_level}")
        print(f"Potential score: {potential_score}")
        print(f"Final score: {final_score}")
        print(f"Статус: {watchlist_status}")
        print(f"Заметка: {note or '—'}")
        print(f"Добавлена: {added_at}")
        print(f"Обновлена: {updated_at}")

        price_checks = get_price_checks_for_pair(pair_id)

        print("Проверки цены:")

        checks_by_period = {
            check[0]: check
            for check in price_checks
        }

        required_periods = (
            "1h",
            "6h",
            "24h",
            "7d",
        )

        for check_period in required_periods:
            check = checks_by_period.get(check_period)

            if check is None:
                print(
                    f"  {check_period}: "
                    "проверка ещё не выполнена"
                )
                continue

            (
                _,
                old_price_usd,
                new_price_usd,
                price_change_percent,
                checked_at,
            ) = check

            change_sign = ""

            if (
                price_change_percent is not None
                and price_change_percent > 0
            ):
                change_sign = "+"

            print(
                f"  {check_period}: "
                f"${old_price_usd} → ${new_price_usd} | "
                f"{change_sign}{price_change_percent}% | "
                f"{checked_at}"
            )

        completed_checks = sum(
            1
            for check_period in required_periods
            if check_period in checks_by_period
        )

        print(
            f"Прогресс проверок: "
            f"{completed_checks} из {len(required_periods)}"
        )

        if completed_checks == len(required_periods):
            cycle_status = "завершён"
        else:
            cycle_status = "выполняется"

        print(f"Цикл наблюдения: {cycle_status}")
        existing_periods = list(checks_by_period.keys())
        next_check_period = get_next_check_period(existing_periods)

        if next_check_period is None:
            print("Следующая проверка: цикл завершён")
        else:
            print(f"Следующая проверка: {next_check_period}")

        print("-" * 40)


if __name__ == "__main__":
    allowed_statuses = {
        "watching",
        "confirmed",
        "rejected",
        "archived",
    }

    selected_status = None

    if len(sys.argv) > 1:
        selected_status = sys.argv[1].lower()

        if selected_status not in allowed_statuses:
            print(f"Неизвестный статус: {selected_status}")
            print(
                "Допустимые статусы: "
                "watching, confirmed, rejected, archived"
            )
            raise SystemExit(1)

    print_watchlist_report(selected_status)