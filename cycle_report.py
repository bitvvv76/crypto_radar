from database import (
    get_all_pairs,
    get_price_checks_count_for_pair,
    get_price_checks_for_pair,
)
from analytics import (
    calculate_idea_result_score,
    get_idea_quality,
    get_score_accuracy_status,
)


def print_line():
    print("-" * 29)


def main():
    pairs = get_all_pairs()

    completed_pairs = []
    in_progress_pairs = []
    not_checked_pairs = []

    quality_counts = {
        "WEAK (СЛАБАЯ)": 0,
        "NEUTRAL (НЕЙТРАЛЬНАЯ)": 0,
        "GOOD (ХОРОШАЯ)": 0,
        "STRONG (СИЛЬНАЯ)": 0,
    }

    accuracy_counts = {
        "OVERESTIMATED (ПЕРЕОЦЕНИЛА)": 0,
        "ACCURATE (БЛИЗКО)": 0,
        "UNDERESTIMATED (НЕДООЦЕНИЛА)": 0,
        "NO_SCORE (НЕТ ОЦЕНКИ)": 0,
    }

    best_pair = None
    worst_pair = None

    total_final_score = 0
    total_result_score = 0
    total_difference = 0

    for pair in pairs:
        pair_id = pair[0]
        chain = pair[1]
        dex = pair[2]
        symbol = pair[3]
        saved_price = pair[4]
        risk_score = pair[5]
        potential_score = pair[6]
        final_score = pair[7]
        created_at = pair[8]

        checks_count = get_price_checks_count_for_pair(pair_id)

        pair_data = {
            "id": pair_id,
            "chain": chain,
            "dex": dex,
            "symbol": symbol,
            "saved_price": saved_price,
            "risk_score": risk_score,
            "potential_score": potential_score,
            "final_score": final_score,
            "created_at": created_at,
            "checks_count": checks_count,
        }

        if checks_count == 0:
            not_checked_pairs.append(pair_data)
            continue

        if checks_count < 4:
            in_progress_pairs.append(pair_data)
            continue

        completed_pairs.append(pair_data)

        checks = get_price_checks_for_pair(pair_id)
        result_score = calculate_idea_result_score(checks)
        quality = get_idea_quality(result_score)
        accuracy = get_score_accuracy_status(final_score, result_score)

        quality_counts[quality] += 1
        accuracy_counts[accuracy] += 1

        total_final_score += final_score
        total_result_score += result_score
        total_difference += result_score - final_score

        pair_result_data = {
            "id": pair_id,
            "symbol": symbol,
            "chain": chain,
            "dex": dex,
            "final_score": final_score,
            "result_score": result_score,
            "quality": quality,
            "accuracy": accuracy,
        }

        if best_pair is None or result_score > best_pair["result_score"]:
            best_pair = pair_result_data

        if worst_pair is None or result_score < worst_pair["result_score"]:
            worst_pair = pair_result_data

    print("CRYPTO RADAR — ОТЧЁТ ПО ЦИКЛУ")
    print("=" * 32)

    print()
    print("1. Сводка цикла")
    print_line()
    print(f"Всего идей: {len(pairs)}")
    print(f"Проверено полностью: {len(completed_pairs)}")
    print(f"В процессе проверки: {len(in_progress_pairs)}")
    print(f"Не проверялись: {len(not_checked_pairs)}")

    print()
    print("2. Качество проверенных идей")
    print_line()
    for quality, count in quality_counts.items():
        print(f"{quality}: {count}")

    print()
    print("3. Точность scoring-модели")
    print_line()
    for accuracy, count in accuracy_counts.items():
        print(f"{accuracy}: {count}")

    if completed_pairs:
        avg_final_score = round(total_final_score / len(completed_pairs), 2)
        avg_result_score = round(total_result_score / len(completed_pairs), 2)
        avg_difference = round(total_difference / len(completed_pairs), 2)

        print()
        print("4. Средние значения")
        print_line()
        print(f"Средний Final score: {avg_final_score}")
        print(f"Средний Idea result score: {avg_result_score}")
        print(f"Средняя разница: {avg_difference}")

    print()
    print("5. Лучшая идея цикла")
    print_line()
    if best_pair:
        print(f"ID: {best_pair['id']}")
        print(f"Пара: {best_pair['symbol']}")
        print(f"Сеть: {best_pair['chain']}")
        print(f"DEX: {best_pair['dex']}")
        print(f"Final score: {best_pair['final_score']}")
        print(f"Idea result score: {best_pair['result_score']}")
        print(f"Idea quality: {best_pair['quality']}")
        print(f"Score accuracy: {best_pair['accuracy']}")
    else:
        print("Нет полностью проверенных идей.")

    print()
    print("6. Слабейшая идея цикла")
    print_line()
    if worst_pair:
        print(f"ID: {worst_pair['id']}")
        print(f"Пара: {worst_pair['symbol']}")
        print(f"Сеть: {worst_pair['chain']}")
        print(f"DEX: {worst_pair['dex']}")
        print(f"Final score: {worst_pair['final_score']}")
        print(f"Idea result score: {worst_pair['result_score']}")
        print(f"Idea quality: {worst_pair['quality']}")
        print(f"Score accuracy: {worst_pair['accuracy']}")
    else:
        print("Нет полностью проверенных идей.")

    print()
    print("7. Вывод по циклу")
    print_line()

    if not completed_pairs:
        print("Недостаточно данных. Нет полностью проверенных идей.")
    else:
        print("Первый цикл наблюдения завершён.")
        print("Scoring-модель уже даёт полезную фильтрацию, но требует накопления статистики.")
        print("До изменения scoring нужно собрать минимум 20–50 полностью проверенных идей.")
        print("Особая гипотеза: обратные пары типа USDC/SOL нужно анализировать отдельной логикой.")


if __name__ == "__main__":
    main()