from database import (
    get_pairs_for_next_checks,
    get_price_checks_count_for_pair,
    get_existing_check_periods_for_pair,
)
from price_checker import check_pair_price
from check_utils import (
    get_check_status,
    get_next_check_period,
    is_check_due,
    get_time_until_check,
    format_remaining_time,
)


def main():
    print("CRYPTO RADAR — АВТОПРОВЕРКА ВСЕЙ ОЧЕРЕДИ")
    print("==========================================")

    pairs = get_pairs_for_next_checks()

    completed_count = 0
    checked_count = 0
    waiting_count = 0
    data_not_found_count = 0
    failed_count = 0

    for pair in pairs:
        (
            pair_id,
            chain_id,
            dex_id,
            pair_symbol,
            price_usd,
            risk_score,
            potential_score,
            final_score,
            created_at,
        ) = pair

        price_checks_count = get_price_checks_count_for_pair(pair_id)
        check_status = get_check_status(price_checks_count)

        if check_status.startswith("COMPLETE"):
            completed_count += 1
            continue

        existing_periods = get_existing_check_periods_for_pair(pair_id)
        next_check_period = get_next_check_period(existing_periods)

        print()
        print("-----------------------------")
        print("ID:", pair_id)
        print("Пара:", pair_symbol)
        print("Следующая проверка:", next_check_period)

        if not is_check_due(created_at, next_check_period):
            remaining = get_time_until_check(created_at, next_check_period)

            print("Статус: ЕЩЁ РАНО")
            print("До проверки осталось:", format_remaining_time(remaining))

            waiting_count += 1
            continue

        result = check_pair_price(
            pair_id,
            next_check_period,
            return_error=True,
        )

        if result is None:
            print("Статус: ОШИБКА ПРОВЕРКИ")
            failed_count += 1
            continue

        if result.get("status") == "DATA_NOT_FOUND":
            print("Статус: DATA_NOT_FOUND")
            data_not_found_count += 1
            continue

        if result.get("status") == "PRICE_NOT_FOUND":
            print("Статус: PRICE_NOT_FOUND")
            failed_count += 1
            continue

        if result.get("status") == "PAIR_NOT_FOUND":
            print("Статус: PAIR_NOT_FOUND")
            failed_count += 1
            continue

        print("Статус: ПРОВЕРКА ВЫПОЛНЕНА")
        print("Период:", result["check_period"])
        print("Старая цена:", result["old_price_usd"])
        print("Новая цена:", result["new_price_usd"])
        print("Изменение цены %:", result["price_change_percent"])

        checked_count += 1

    print()
    print("ИТОГ АВТОПРОВЕРКИ")
    print("==================")
    print("Всего идей в базе:", len(pairs))
    print("Уже полностью проверены:", completed_count)
    print("Проверок выполнено сейчас:", checked_count)
    print("Ещё не наступил срок:", waiting_count)
    print("Пары без свежих данных:", data_not_found_count)
    print("Ошибок проверки:", failed_count)


if __name__ == "__main__":
    main()