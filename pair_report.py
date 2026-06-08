from database import get_pair_by_id, get_price_checks_for_pair


def get_result_status(checks):
    positive_count = 0
    negative_count = 0
    flat_count = 0

    flat_threshold = 0.2

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

        if abs(price_change_percent) <= flat_threshold:
            flat_count += 1
        elif price_change_percent > 0:
            positive_count += 1
        else:
            negative_count += 1

    total_count = positive_count + negative_count + flat_count

    if total_count == 0:
        return (
            "NO_DATA (НЕТ ДАННЫХ)",
            "Недостаточно данных для анализа"
        )

    if flat_count == total_count:
        return (
            "FLAT (ФЛЭТ)",
            "По текущим проверкам сильного движения нет"
        )

    if positive_count > negative_count:
        return (
            "POSITIVE (ПОЗИТИВНО)",
            "По текущим проверкам положительная динамика преобладает"
        )

    if negative_count > positive_count:
        return (
            "NEGATIVE (НЕГАТИВНО)",
            "По текущим проверкам отрицательная динамика преобладает"
        )

    return (
        "MIXED (СМЕШАННО)",
        "По текущим проверкам динамика неоднозначная"
    )


def print_checks_summary(checks):
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
        print("\nИтог по паре:")
        print("Недостаточно данных для анализа")
        return

    result_status, result_description = get_result_status(checks)

    print("\nИтог по паре:")
    print("-----------------------------")
    print("Максимальный результат:", best_check[0], "→", best_check[3], "%")
    print("Минимальный результат:", worst_check[0], "→", worst_check[3], "%")
    print("Result status (статус результата):", result_status)
    print("Вывод:", result_description)


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
        price_usd
    ) = pair

    checks = get_price_checks_for_pair(pair_id)

    print("Отчёт по паре")
    print("-----------------------------")
    print("ID пары:", saved_pair_id)
    print("Сеть:", chain_id)
    print("Пара:", pair_symbol)
    print("Pair address (адрес пары):", pair_address)
    print("Цена при сохранении:", price_usd)

    if not checks:
        print("\nПроверок цены пока нет")
        return

    print("\nПроверки цены:")

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

    print_checks_summary(checks)


def main():
    pair_id_text = input("Введите ID пары для отчёта: ").strip()

    if not pair_id_text.isdigit():
        print("Ошибка: ID пары должен быть числом")
        return

    pair_id = int(pair_id_text)

    print_pair_report(pair_id)


if __name__ == "__main__":
    main()