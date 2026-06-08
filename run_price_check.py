from price_checker import check_pair_price


ALLOWED_PERIODS = ["MANUAL_RUN", "1h", "6h", "24h", "7d"]


def main():
    print("Запуск ручной проверки цены")

    pair_id_text = input("Введите ID пары для проверки: ").strip()

    if not pair_id_text.isdigit():
        print("Ошибка: ID пары должен быть числом")
        return

    pair_id = int(pair_id_text)

    print("Доступные периоды проверки:")
    print("MANUAL_RUN — ручная проверка")
    print("1h — проверка через 1 час")
    print("6h — проверка через 6 часов")
    print("24h — проверка через 24 часа")
    print("7d — проверка через 7 дней")

    check_period = input("Введите период проверки: ").strip()

    if check_period not in ALLOWED_PERIODS:
        print("Ошибка: неизвестный период проверки")
        return

    result = check_pair_price(
        pair_id=pair_id,
        check_period=check_period
    )

    if result is None:
        print("Проверку цены выполнить не удалось")
        return

    if result.get("already_checked"):
        print("Такая проверка уже есть в базе")
        print("Пара:", result["pair_symbol"])
        print("Период проверки:", result["check_period"])
        return

    print("Проверка цены выполнена")
    print("Пара:", result["pair_symbol"])
    print("Период проверки:", result["check_period"])
    print("Старая цена:", result["old_price_usd"])
    print("Новая цена:", result["new_price_usd"])
    print("Изменение цены %:", result["price_change_percent"])

if __name__ == "__main__":
    main()