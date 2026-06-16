from database import create_tables, get_pairs_count, save_pair
from scanner import filter_valid_pairs, search_pairs
from scoring import (
    calculate_final_score,
    calculate_potential_score,
    calculate_risk_score,
    get_risk_level,
)


SEARCH_QUERIES = [
    "SOL/USDC",
    "ETH/USDC",
    "BTC/USDC",
    "AI/USDC",
    "RWA/USDC",
]

MAX_NEW_IDEAS = 3
MIN_FINAL_SCORE = 70
ALLOWED_QUOTE_TOKENS = {
    "USDC",
    "USDT",
    "DAI",
}

def is_supported_pair(pair):
    base_symbol = (
        pair.get("baseToken", {})
        .get("symbol", "")
        .upper()
    )

    quote_symbol = (
        pair.get("quoteToken", {})
        .get("symbol", "")
        .upper()
    )

    if not base_symbol or not quote_symbol:
        return False

    if base_symbol in ALLOWED_QUOTE_TOKENS:
        return False

    if quote_symbol not in ALLOWED_QUOTE_TOKENS:
        return False

    return True


def collect_unique_pairs():
    all_valid_pairs = []
    seen_pairs = set()
    total_found_pairs = 0

    for query in SEARCH_QUERIES:
        print()
        print("Поисковый запрос:", query)

        data = search_pairs(query)
        pairs = data.get("pairs", [])
        valid_pairs = filter_valid_pairs(pairs)

        print("Найдено пар:", len(pairs))
        print("После фильтра:", len(valid_pairs))

        total_found_pairs += len(pairs)

        for pair in valid_pairs:
            if not is_supported_pair(pair):
                continue
            pair_key = (
                pair.get("chainId"),
                pair.get("pairAddress"),
            )

            if pair_key in seen_pairs:
                continue

            seen_pairs.add(pair_key)
            all_valid_pairs.append(pair)

    return all_valid_pairs, total_found_pairs


def main():
    print("CRYPTO RADAR — АВТОМАТИЧЕСКИЙ ПОИСК НОВЫХ ИДЕЙ")
    print("================================================")

    create_tables()

    pairs_before = get_pairs_count()
    all_valid_pairs, total_found_pairs = collect_unique_pairs()

    sorted_pairs = sorted(
        all_valid_pairs,
        key=calculate_final_score,
        reverse=True,
    )

    saved_count = 0
    existing_count = 0
    below_score_count = 0

    for pair in sorted_pairs:
        if saved_count >= MAX_NEW_IDEAS:
            break

        final_score = calculate_final_score(pair)

        if final_score < MIN_FINAL_SCORE:
            below_score_count += 1
            continue

        risk_score = calculate_risk_score(pair)
        risk_level = get_risk_level(risk_score)
        potential_score = calculate_potential_score(pair)

        is_saved = save_pair(
            pair,
            risk_score,
            risk_level,
            potential_score,
            final_score,
        )

        if not is_saved:
            existing_count += 1
            continue

        saved_count += 1

        base_symbol = pair.get("baseToken", {}).get("symbol")
        quote_symbol = pair.get("quoteToken", {}).get("symbol")

        print()
        print("Сохранена новая идея")
        print("-----------------------------")
        print("Сеть:", pair.get("chainId"))
        print("DEX:", pair.get("dexId"))
        print("Пара:", f"{base_symbol}/{quote_symbol}")
        print("Final score:", final_score)

    pairs_after = get_pairs_count()

    print()
    print("ИТОГ АВТОСКАНИРОВАНИЯ")
    print("======================")
    print("Всего найдено пар:", total_found_pairs)
    print("Уникальных после фильтра:", len(all_valid_pairs))
    print("Новых идей сохранено:", saved_count)
    print("Уже были в базе:", existing_count)
    print("Ниже минимального score:", below_score_count)
    print("Идей в базе до запуска:", pairs_before)
    print("Идей в базе после запуска:", pairs_after)

    if saved_count == 0:
        print("Подходящих новых идей в этом запуске не найдено.")


if __name__ == "__main__":
    main()