from database import get_all_pairs, get_price_checks_count_for_pair, get_price_checks_for_pair
from analytics import calculate_idea_result_score, get_idea_quality, get_score_accuracy_status

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



def print_summary(pairs):
    total_ideas = len(pairs)

    complete_count = 0
    in_progress_count = 0
    not_checked_count = 0

    high_count = 0
    medium_count = 0
    low_count = 0
    very_low_count = 0

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
        idea_priority = get_idea_priority(final_score)

        if check_status.startswith("COMPLETE"):
            complete_count += 1
        elif check_status.startswith("IN_PROGRESS"):
            in_progress_count += 1
        else:
            not_checked_count += 1

        if idea_priority.startswith("HIGH"):
            high_count += 1
        elif idea_priority.startswith("MEDIUM"):
            medium_count += 1
        elif idea_priority.startswith("LOW"):
            low_count += 1
        elif idea_priority.startswith("VERY_LOW"):
            very_low_count += 1

    print("\nСводка:")
    print("-----------------------------")
    print("Всего идей:", total_ideas)
    print("Проверено полностью:", complete_count)
    print("В процессе проверки:", in_progress_count)
    print("Не проверялись:", not_checked_count)
    print("HIGH priority:", high_count)
    print("MEDIUM priority:", medium_count)
    print("LOW priority:", low_count)
    print("VERY_LOW priority:", very_low_count)


def print_ideas_report():
    pairs = get_all_pairs()

    print("CRYPTO RADAR — СПИСОК ИДЕЙ")
    print("============================")

    if not pairs:
        print("Сохранённых пар пока нет")
        return
    
    print_summary(pairs)

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

        if check_status.startswith("COMPLETE"):
            checks = get_price_checks_for_pair(pair_id)
            idea_result_score = calculate_idea_result_score(checks)
            idea_quality = get_idea_quality(idea_result_score)
            score_accuracy_status = get_score_accuracy_status(
                final_score,
                idea_result_score
            )

            print("Idea result score (оценка результата идеи):", idea_result_score)
            print("Idea quality (качество идеи):", idea_quality)
            print("Score accuracy (точность оценки):", score_accuracy_status)

        print("Дата сохранения:", created_at)

def main():
    print_ideas_report()


if __name__ == "__main__":
    main()