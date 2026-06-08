from database import get_pair_by_id, get_price_checks_for_pair


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


def main():
    pair_id_text = input("Введите ID пары для отчёта: ").strip()

    if not pair_id_text.isdigit():
        print("Ошибка: ID пары должен быть числом")
        return

    pair_id = int(pair_id_text)

    print_pair_report(pair_id)


if __name__ == "__main__":
    main()