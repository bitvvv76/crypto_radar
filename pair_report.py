from database import get_pair_by_id, get_price_checks_for_pair


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

    print("\nИтог по паре:")
    print("-----------------------------")
    print("Максимальный результат:", best_check[0], "→", best_check[3], "%")
    print("Минимальный результат:", worst_check[0], "→", worst_check[3], "%")

    if best_check[3] > 0 and worst_check[3] >= 0:
        print("Общий вывод: по всем проверкам пара показала положительную динамику")
    elif best_check[3] > 0 and worst_check[3] < 0:
        print("Общий вывод: динамика смешанная, были и рост, и просадка")
    elif best_check[3] <= 0:
        print("Общий вывод: по текущим проверкам роста нет, идея ушла в минус")
    else:
        print("Общий вывод: данных недостаточно для уверенного вывода")


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