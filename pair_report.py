from database import get_pair_by_id, get_price_checks_for_pair
from analytics import (
    calculate_idea_result_score,
    get_idea_quality,
    get_score_accuracy_status,
    get_result_status
)




def get_decision_note(result_status, idea_quality, score_accuracy_status):
    if result_status.startswith("POSITIVE") and idea_quality.startswith(("GOOD", "STRONG")):
        return "Идея подтверждается текущими проверками. Нужно продолжать наблюдение и проверять результат на большем количестве похожих случаев."

    if result_status.startswith("NEGATIVE") and score_accuracy_status.startswith("OVERESTIMATED"):
        return "Идея не подтверждена текущими проверками. Первичная оценка была завышена. Нужна доработка scoring-модели на большем количестве наблюдений."

    if result_status.startswith("MIXED"):
        return "Динамика неоднозначная. Идею нельзя считать подтверждённой или проваленной без дополнительных наблюдений."

    if result_status.startswith("FLAT"):
        return "Сильного движения по идее пока нет. Нужно больше времени или дополнительные фильтры для оценки."

    return "Недостаточно данных для уверенного аналитического вывода."

def print_score_comparison(final_score, idea_result_score, result_status, idea_quality):
    if final_score is None:
        print("\n4. Сравнение scoring-модели")
        print("Final score при нахождении отсутствует")
        return

    difference = idea_result_score - final_score
    score_accuracy_status = get_score_accuracy_status(
        final_score,
        idea_result_score
    )

    decision_note = get_decision_note(
        result_status,
        idea_quality,
        score_accuracy_status
    )

    print("\n4. Сравнение scoring-модели")
    print("-----------------------------")
    print("Final score при нахождении:", final_score)
    print("Idea result score после проверок:", idea_result_score)
    print("Разница:", difference)
    print("Score accuracy (точность оценки):", score_accuracy_status)

    print("\n5. Decision note (аналитическая заметка)")
    print(decision_note)

    if difference >= 20:
        print("Вывод по scoring: фактический результат оказался лучше первичной оценки")
    elif difference <= -20:
        print("Вывод по scoring: первичная оценка была выше фактического результата. Scoring-модель нужно будет улучшать на статистике")
    else:
        print("Вывод по scoring: первичная оценка близка к фактическому результату")





def print_checks_summary(checks, final_score):
    if not checks:
        return

    best_check = None
    worst_check = None

    for check in checks:
        (
            check_period,
            old_price_usd,
            new_price_usd,
            price_change_percent,
            checked_at
        ) = check

        if price_change_percent is None:
            continue

        if best_check is None or price_change_percent > best_check[3]:
            best_check = check

        if worst_check is None or price_change_percent < worst_check[3]:
            worst_check = check

    if best_check is None or worst_check is None:
        print("\n3. Итог по идее")
        print("Недостаточно данных для анализа")
        return

    result_status, result_description = get_result_status(checks)
    idea_result_score = calculate_idea_result_score(checks)
    idea_quality = get_idea_quality(idea_result_score)

    print("\n3. Итог по идее")
    print("-----------------------------")
    print("Максимальный результат:", best_check[0], "→", best_check[3], "%")
    print("Минимальный результат:", worst_check[0], "→", worst_check[3], "%")
    print("Result status (статус результата):", result_status)
    print("Idea result score (оценка результата идеи):", idea_result_score)
    print("Idea quality (качество идеи):", idea_quality)
    print("Вывод:", result_description)
    print_score_comparison(
        final_score,
        idea_result_score,
        result_status,
        idea_quality
    )

def print_pair_report(pair_id):
    pair = get_pair_by_id(pair_id)

    if pair is None:
        print("Пара не найдена в базе данных")
        return

    (
        saved_pair_id,
        chain_id,
        pair_address,
        pair_symbol,
        price_usd,
        final_score
    ) = pair

    checks = get_price_checks_for_pair(pair_id)

    print("CRYPTO RADAR — ОТЧЁТ ПО ПАРЕ")
    print("=============================")

    print("\n1. Основная информация")
    print("-----------------------------")
    print("ID пары:", saved_pair_id)
    print("Сеть:", chain_id)
    print("Пара:", pair_symbol)
    print("Pair address (адрес пары):", pair_address)
    print("Цена при сохранении:", price_usd)
    print("Final score при нахождении:", final_score)

    if not checks:
        print("\nПроверок цены пока нет")
        return

    print("\n2. Проверки цены")

    for check in checks:
        (
            check_period,
            old_price_usd,
            new_price_usd,
            price_change_percent,
            checked_at
        ) = check

        print("\n-----------------------------")
        print("Период:", check_period)
        print("Старая цена:", old_price_usd)
        print("Новая цена:", new_price_usd)
        print("Изменение цены %:", price_change_percent)
        print("Дата проверки:", checked_at)

    print_checks_summary(checks, final_score)


def main():
    pair_id_text = input("Введите ID пары для отчёта: ").strip()

    if not pair_id_text.isdigit():
        print("Ошибка: ID пары должен быть числом")
        return

    pair_id = int(pair_id_text)

    print_pair_report(pair_id)


if __name__ == "__main__":
    main()