import sqlite3

from scanner import get_pair_by_address


DATABASE_NAME = "crypto_radar.db"


def get_pairs_without_token_address(connection):
    cursor = connection.cursor()

    cursor.execute(
        """
        SELECT
            id,
            chain_id,
            pair_address,
            pair_symbol
        FROM pairs
        WHERE base_token_address IS NULL
           OR base_token_address = ''
        ORDER BY id
        """
    )

    return cursor.fetchall()


def update_token_address(connection, pair_id, base_token_address):
    cursor = connection.cursor()

    cursor.execute(
        """
        UPDATE pairs
        SET base_token_address = ?
        WHERE id = ?
        """,
        (
            base_token_address,
            pair_id,
        ),
    )

    connection.commit()


def main():
    connection = sqlite3.connect(DATABASE_NAME)

    pairs = get_pairs_without_token_address(connection)

    print("CRYPTO RADAR — ЗАПОЛНЕНИЕ АДРЕСОВ ТОКЕНОВ")
    print("==========================================")
    print("Записей для обработки:", len(pairs))

    updated_count = 0
    skipped_count = 0
    error_count = 0

    for pair_id, chain_id, pair_address, pair_symbol in pairs:
        print()
        print("-----------------------------")
        print("ID:", pair_id)
        print("Пара:", pair_symbol)
        print("Сеть:", chain_id)

        pair_data = get_pair_by_address(chain_id, pair_address)

        if pair_data is None:
            print("Статус: данные пары не получены")
            error_count += 1
            continue

        base_token = pair_data.get("baseToken", {})
        base_token_address = base_token.get("address")

        if not base_token_address:
            print("Статус: адрес базового токена отсутствует")
            skipped_count += 1
            continue

        update_token_address(
            connection,
            pair_id,
            base_token_address,
        )

        print("Статус: адрес токена сохранён")
        print("Адрес токена:", base_token_address)

        updated_count += 1

    connection.close()

    print()
    print("ИТОГ МИГРАЦИИ")
    print("==============")
    print("Обновлено записей:", updated_count)
    print("Пропущено записей:", skipped_count)
    print("Ошибок получения данных:", error_count)


if __name__ == "__main__":
    main()