from database import (
    get_pairs_for_next_checks,
    get_price_checks_count_for_pair,
    get_existing_check_periods_for_pair
)

from check_utils import get_check_status, get_idea_priority, get_next_check_period
from price_checker import check_pair_price


def get_first_pair_for_next_check():
    pairs = get_pairs_for_next_checks()

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
            created_at
        ) = pair

        price_checks_count = get_price_checks_count_for_pair(pair_id)
        check_status = get_check_status(price_checks_count)

        if check_status.startswith("COMPLETE"):
            continue

        existing_periods = get_existing_check_periods_for_pair(pair_id)
        next_check_period = get_next_check_period(existing_periods)

        return {
            "pair_id": pair_id,
            "chain_id": chain_id,
            "dex_id": dex_id,
            "pair_symbol": pair_symbol,
            "price_usd": price_usd,
            "risk_score": risk_score,
            "potential_score": potential_score,
            "final_score": final_score,
            "created_at": created_at,
            "price_checks_count": price_checks_count,
            "check_status": check_status,
            "next_check_period": next_check_period,
        }

    return None


def main():
    print("CRYPTO RADAR — ЗАПУСК СЛЕДУЮЩЕЙ ПРОВЕРКИ")
    print("=========================================")

    pair_for_check = get_first_pair_for_next_check()

    if pair_for_check is None:
        print("Нет идей для проверки. Все идеи уже полностью проверены.")
        return

    pair_id = pair_for_check["pair_id"]
    pair_symbol = pair_for_check["pair_symbol"]
    final_score = pair_for_check["final_score"]
    next_check_period = pair_for_check["next_check_period"]
    price_checks_count = pair_for_check["price_checks_count"]
    check_status = pair_for_check["check_status"]

    idea_priority = get_idea_priority(final_score)

    print("\nСледующая идея для проверки:")
    print("-----------------------------")
    print("ID:", pair_id)
    print("Пара:", pair_symbol)
    print("Final score (итоговая оценка):", final_score)
    print("Idea priority (приоритет идеи):", idea_priority)
    print("Проверок цены:", price_checks_count)
    print("Статус проверки:", check_status)
    print("Следующая проверка:", next_check_period)

    user_answer = input("\nВыполнить эту проверку? yes/no: ").strip().lower()

    if user_answer != "yes":
        print("Проверка отменена")
        return

    result = check_pair_price(pair_id, next_check_period)

    if result is None:
        print("Проверка не выполнена")
        return

    print("\nПроверка выполнена")
    print("-----------------------------")
    print("Пара:", result["pair_symbol"])
    print("Период проверки:", result["check_period"])
    print("Старая цена:", result["old_price_usd"])
    print("Новая цена:", result["new_price_usd"])
    print("Изменение цены %:", result["price_change_percent"])
    updated_checks_count = get_price_checks_count_for_pair(pair_id)
    updated_check_status = get_check_status(updated_checks_count)
    updated_existing_periods = get_existing_check_periods_for_pair(pair_id)
    updated_next_check_period = get_next_check_period(updated_existing_periods)

    print("\nОбновлённое состояние идеи:")
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