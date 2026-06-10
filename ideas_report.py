from database import (
    get_all_pairs,
    get_price_checks_count_for_pair,
    get_price_checks_for_pair,
    get_existing_check_periods_for_pair
)
from analytics import calculate_idea_result_score, get_idea_quality, get_score_accuracy_status
from check_utils import get_check_status, get_idea_priority, get_next_check_period


def print_summary(pairs):
    total_ideas = len(pairs)

    complete_count = 0
    in_progress_count = 0
    not_checked_count = 0

    high_count = 0
    medium_count = 0
    low_count = 0
    very_low_count = 0
    weak_quality_count = 0
    neutral_quality_count = 0
    good_quality_count = 0
    strong_quality_count = 0

    overestimated_count = 0
    accurate_count = 0
    underestimated_count = 0
    no_score_count = 0
    checked_final_score_sum = 0
    checked_idea_result_score_sum = 0
    checked_difference_sum = 0
    checked_score_count = 0

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

            checks = get_price_checks_for_pair(pair_id)
            idea_result_score = calculate_idea_result_score(checks)
            idea_quality = get_idea_quality(idea_result_score)
            score_accuracy_status = get_score_accuracy_status(
                final_score,
                idea_result_score
            )
            if final_score is not None:
                difference = idea_result_score - final_score

                checked_final_score_sum += final_score
                checked_idea_result_score_sum += idea_result_score
                checked_difference_sum += difference
                checked_score_count += 1

            if idea_quality.startswith("WEAK"):
                weak_quality_count += 1
            elif idea_quality.startswith("NEUTRAL"):
                neutral_quality_count += 1
            elif idea_quality.startswith("GOOD"):
                good_quality_count += 1
            elif idea_quality.startswith("STRONG"):
                strong_quality_count += 1

            if score_accuracy_status.startswith("OVERESTIMATED"):
                overestimated_count += 1
            elif score_accuracy_status.startswith("ACCURATE"):
                accurate_count += 1
            elif score_accuracy_status.startswith("UNDERESTIMATED"):
                underestimated_count += 1
            elif score_accuracy_status.startswith("NO_SCORE"):
                no_score_count += 1

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
    print("\nКачество проверенных идей:")
    print("-----------------------------")
    print("WEAK (СЛАБАЯ):", weak_quality_count)
    print("NEUTRAL (НЕЙТРАЛЬНАЯ):", neutral_quality_count)
    print("GOOD (ХОРОШАЯ):", good_quality_count)
    print("STRONG (СИЛЬНАЯ):", strong_quality_count)

    print("\nТочность scoring-модели:")
    print("-----------------------------")
    print("OVERESTIMATED (ПЕРЕОЦЕНИЛА):", overestimated_count)
    print("ACCURATE (БЛИЗКО):", accurate_count)
    print("UNDERESTIMATED (НЕДООЦЕНИЛА):", underestimated_count)
    print("NO_SCORE (НЕТ ОЦЕНКИ):", no_score_count)

    if checked_score_count > 0:
        average_final_score = checked_final_score_sum / checked_score_count
        average_idea_result_score = checked_idea_result_score_sum / checked_score_count
        average_difference = checked_difference_sum / checked_score_count
    else:
        average_final_score = 0
        average_idea_result_score = 0
        average_difference = 0

    print("\nСредние значения по проверенным идеям:")
    print("-----------------------------")
    print("Средний Final score проверенных идей:", round(average_final_score, 2))
    print("Средний Idea result score проверенных идей:", round(average_idea_result_score, 2))
    print("Средняя разница:", round(average_difference, 2))


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

        existing_periods = get_existing_check_periods_for_pair(pair_id)
        next_check_period = get_next_check_period(existing_periods)
        print("Следующая проверка:", next_check_period)

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