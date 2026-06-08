from database import get_all_pairs, get_price_checks_count_for_pair

def get_check_status(price_checks_count):
    if price_checks_count == 0:
        return "NOT_CHECKED (НЕ ПРОВЕРЯЛАСЬ)"

    if price_checks_count < 4:
        return "IN_PROGRESS (В ПРОЦЕССЕ)"

    return "COMPLETE (ПОЛНАЯ)"

def get_idea_priority(final_score):
    if final_score is None:
        return "NO_SCORE (НЕТ ОЦЕНКИ)"

    if final_score >= 75:
        return "HIGH (ВЫСОКИЙ)"

    if final_score >= 50:
        return "MEDIUM (СРЕДНИЙ)"

    if final_score >= 25:
        return "LOW (НИЗКИЙ)"

    return "VERY_LOW (ОЧЕНЬ НИЗКИЙ)"


def print_ideas_report():
    pairs = get_all_pairs()

    print("CRYPTO RADAR — СПИСОК ИДЕЙ")
    print("============================")

    if not pairs:
        print("Сохранённых пар пока нет")
        return

    print("\nСохранённые пары:")
    print("-----------------------------")

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

        print("\n-----------------------------")
        print("ID:", pair_id)
        print("Сеть:", chain_id)
        print("DEX:", dex_id)
        print("Пара:", pair_symbol)
        print("Цена при сохранении:", price_usd)
        print("Risk score (оценка риска):", risk_score)
        print("Potential score (оценка потенциала):", potential_score)
        print("Final score (итоговая оценка):", final_score)

        idea_priority = get_idea_priority(final_score)
        print("Idea priority (приоритет идеи):", idea_priority)

        price_checks_count = get_price_checks_count_for_pair(pair_id)
        check_status = get_check_status(price_checks_count)

        print("Проверок цены:", price_checks_count)
        print("Статус проверки:", check_status)

        print("Дата сохранения:", created_at)

def main():
    print_ideas_report()


if __name__ == "__main__":
    main()