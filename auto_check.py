from run_next_check import get_first_pair_for_next_check
from price_checker import check_pair_price
from database import (
    get_price_checks_count_for_pair,
    get_existing_check_periods_for_pair,
)
from check_utils import get_check_status, get_next_check_period


def main():
    print("CRYPTO RADAR — АВТОПРОВЕРКА СЛЕДУЮЩЕЙ ИДЕИ")
    print("===========================================")

    pair_for_check = get_first_pair_for_next_check()

    if pair_for_check is None:
        print()
        print("Нет идей для проверки.")
        print("Все идеи уже полностью проверены.")
        return

    pair_id = pair_for_check["pair_id"]
    pair_symbol = pair_for_check["pair_symbol"]
    final_score = pair_for_check["final_score"]
    next_check_period = pair_for_check["next_check_period"]
    price_checks_count = pair_for_check["price_checks_count"]
    check_status = pair_for_check["check_status"]

    print()
    print("Следующая идея для автопроверки:")
    print("-----------------------------")
    print("ID:", pair_id)
    print("Пара:", pair_symbol)
    print("Final score:", final_score)
    print("Проверок цены:", price_checks_count)
    print("Статус проверки:", check_status)
    print("Следующая проверка:", next_check_period)

    if next_check_period == "COMPLETE":
        print()
        print("Идея уже полностью проверена. Автопроверка не требуется.")
        return

    result = check_pair_price(pair_id, next_check_period)

    if result is None:
        print()
        print("Проверка не выполнена.")
        print("Нужно посмотреть логику price_checker.py или доступность данных.")
        return

    updated_checks_count = get_price_checks_count_for_pair(pair_id)
    updated_existing_periods = get_existing_check_periods_for_pair(pair_id)
    updated_check_status = get_check_status(updated_checks_count)
    updated_next_check_period = get_next_check_period(updated_existing_periods)

    print()
    print("Автопроверка выполнена")
    print("-----------------------------")
    print("Пара:", result["pair_symbol"])
    print("Период проверки:", result["check_period"])
    print("Старая цена:", result["old_price_usd"])
    print("Новая цена:", result["new_price_usd"])
    print("Изменение цены %:", result["price_change_percent"])

    print()
    print("Обновлённое состояние идеи:")
    print("-----------------------------")
    print("ID:", pair_id)
    print("Пара:", pair_symbol)
    print("Проверок цены:", updated_checks_count)
    print("Статус проверки:", updated_check_status)
    print("Следующая проверка:", updated_next_check_period)

    if updated_next_check_period == "COMPLETE":
        print("Идея полностью проверена по периодам 1h / 6h / 24h / 7d.")
    else:
        print("Следующий шаг: продолжить проверку периода", updated_next_check_period)


if __name__ == "__main__":
    main()