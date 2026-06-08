from database import get_pair_by_id, save_price_check
from scanner import get_pair_by_address


def check_pair_price(pair_id, check_period):
    saved_pair = get_pair_by_id(pair_id)

    if saved_pair is None:
        print("Пара не найдена в базе данных")
        return None

    (
        pair_id,
        chain_id,
        pair_address,
        pair_symbol,
        old_price_usd,
        final_score
    ) = saved_pair

    fresh_pair = get_pair_by_address(chain_id, pair_address)

    if fresh_pair is None:
        print("Свежие данные по паре получить не удалось")
        return None

    fresh_price_usd = fresh_pair.get("priceUsd")

    if fresh_price_usd is None:
        print("У свежей пары нет цены")
        return None

    new_price_usd = float(fresh_price_usd)

    price_change_percent = save_price_check(
        pair_id=pair_id,
        check_period=check_period,
        old_price_usd=old_price_usd,
        new_price_usd=new_price_usd
    )

    if price_change_percent is None:
        result = {
            "pair_id": pair_id,
            "pair_symbol": pair_symbol,
            "check_period": check_period,
            "old_price_usd": old_price_usd,
            "new_price_usd": new_price_usd,
            "price_change_percent": None,
            "already_checked": True
        }

        return result

    result = {
        "pair_id": pair_id,
        "pair_symbol": pair_symbol,
        "check_period": check_period,
        "old_price_usd": old_price_usd,
        "new_price_usd": new_price_usd,
        "price_change_percent": price_change_percent,
        "already_checked": False
    }

    return result