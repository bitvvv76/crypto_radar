from database import get_pairs_for_next_checks, get_price_checks_count_for_pair
from ideas_report import get_check_status, get_idea_priority


def print_next_checks():
    pairs = get_pairs_for_next_checks()

    print("CRYPTO RADAR — ИДЕИ ДЛЯ ПРОВЕРКИ")
    print("=================================")

    if not pairs:
        print("Сохранённых идей пока нет")
        return

    found_pairs = 0

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

        idea_priority = get_idea_priority(final_score)

        found_pairs += 1

        print("\n-----------------------------")
        print("ID:", pair_id)
        print("Сеть:", chain_id)
        print("DEX:", dex_id)
        print("Пара:", pair_symbol)
        print("Цена при сохранении:", price_usd)
        print("Final score (итоговая оценка):", final_score)
        print("Idea priority (приоритет идеи):", idea_priority)
        print("Проверок цены:", price_checks_count)
        print("Статус проверки:", check_status)
        print("Дата сохранения:", created_at)

    if found_pairs == 0:
        print("\nВсе идеи уже полностью проверены")


def main():
    print_next_checks()


if __name__ == "__main__":
    main()