from scanner import search_pairs, filter_valid_pairs, get_pair_by_address
from scoring import calculate_risk_score, get_risk_level, calculate_potential_score, calculate_final_score
from database import create_tables, save_pair, get_pairs_count, get_last_pairs, get_last_price_checks, get_price_checks_count



def show_pair(pair, number):
    base_token = pair.get("baseToken", {})
    quote_token = pair.get("quoteToken", {})
    liquidity = pair.get("liquidity", {})

    print(f"\nПара #{number}")
    print("Сеть:", pair.get("chainId"))
    print("DEX:", pair.get("dexId"))
    print("Пара:", base_token.get("symbol"), "/", quote_token.get("symbol"))
    print("Pair address (адрес пары):", pair.get("pairAddress"))
    print("Цена USD:", pair.get("priceUsd"))
    print("Ликвидность USD:", liquidity.get("usd"))
    print("Объём 24ч:", pair.get("volume", {}).get("h24"))
    print("Изменение 24ч %:", pair.get("priceChange", {}).get("h24"))
    risk_score = calculate_risk_score(pair)
    risk_level = get_risk_level(risk_score)
    potential_score = calculate_potential_score(pair)
    final_score = calculate_final_score(pair)

    print("Risk score (оценка риска):", risk_score)
    print("Risk level (уровень риска):", risk_level)
    print("Potential score (оценка потенциала):", potential_score)
    print("Final score (итоговая оценка):", final_score)

    is_saved = save_pair(pair, risk_score, risk_level, potential_score, final_score)

    if is_saved:
        print("Сохранено в базу данных")
    else:
        print("Уже есть в базе данных")

    return is_saved
        
def main():
    print("Crypto Radar запущен")
    create_tables()
    print("База данных готова")

    data = search_pairs("SOL/USDC")
    pairs = data.get("pairs", [])
    valid_pairs = filter_valid_pairs(pairs)

    print(f"Найдено пар всего: {len(pairs)}")
    print(f"После фильтра: {len(valid_pairs)}")

    sorted_pairs = sorted(valid_pairs, key=calculate_final_score, reverse=True)

    new_pairs_count = 0
    existing_pairs_count = 0

    for index, pair in enumerate(sorted_pairs[:5], start=1):
        is_saved = show_pair(pair, index)

        if is_saved:
            new_pairs_count += 1
        else:
            existing_pairs_count += 1

    total_saved = get_pairs_count()

    print("\nСтатистика запуска:")
    print(f"Новых пар сохранено: {new_pairs_count}")
    print(f"Уже были в базе: {existing_pairs_count}")
    print(f"Всего записей в базе данных: {total_saved}")

    last_pairs = get_last_pairs()

    print("\nПоследние записи в базе:")

    for row in last_pairs:
        (
            pair_id,
            chain_id,
            dex_id,
            pair_address,
            pair_symbol,
            price_usd,
            risk_score,
            potential_score,
            final_score,
            created_at
        ) = row

        print("\n-----------------------------")
        print("ID:", pair_id)
        print("Сеть:", chain_id)
        print("DEX:", dex_id)
        print("Пара:", pair_symbol)
        print("Pair address (адрес пары):", pair_address)
        print("Цена при сохранении:", price_usd)
        print("Risk score (оценка риска):", risk_score)
        print("Potential score (оценка потенциала):", potential_score)
        print("Final score (итоговая оценка):", final_score)
        print("Дата сохранения:", created_at)

    
    last_checks = get_last_price_checks()

    total_checks = get_price_checks_count()
    print(f"\nВсего проверок цены в базе: {total_checks}")

    print("\nПоследние проверки цены:")

    for check in last_checks:
        (
            check_id,
            pair_id,
            check_period,
            old_price_usd,
            new_price_usd,
            price_change_percent,
            checked_at
        ) = check

        print("\n-----------------------------")
        print("ID проверки:", check_id)
        print("ID пары:", pair_id)
        print("Период проверки:", check_period)
        print("Старая цена:", old_price_usd)
        print("Новая цена:", new_price_usd)
        print("Изменение цены %:", price_change_percent)
        print("Дата проверки:", checked_at)

   
    

if __name__ == "__main__":
    
    main()

    