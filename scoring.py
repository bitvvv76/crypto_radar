def calculate_risk_score(pair):
    risk_score = 0

    liquidity_usd = pair.get("liquidity", {}).get("usd") or 0
    volume_24h = pair.get("volume", {}).get("h24") or 0
    price_change_24h = pair.get("priceChange", {}).get("h24") or 0

    # 1. Риск по ликвидности
    if liquidity_usd < 20_000:
        risk_score += 40
    elif liquidity_usd < 50_000:
        risk_score += 25
    elif liquidity_usd < 100_000:
        risk_score += 10

    # 2. Риск по объёму торгов
    if volume_24h < 1_000:
        risk_score += 30
    elif volume_24h < 10_000:
        risk_score += 15

    # 3. Риск по резкому росту цены
    if price_change_24h > 200:
        risk_score += 30
    elif price_change_24h > 100:
        risk_score += 20
    elif price_change_24h > 50:
        risk_score += 10

    if risk_score > 100:
        risk_score = 100

    return risk_score

def get_risk_level(risk_score):
    if risk_score <= 20:
        return "LOW (НИЗКИЙ)"

    if risk_score <= 50:
        return "MEDIUM (СРЕДНИЙ)"

    return "HIGH (ВЫСОКИЙ)"

def calculate_potential_score(pair):
    potential_score = 0

    liquidity_usd = pair.get("liquidity", {}).get("usd") or 0
    volume_24h = pair.get("volume", {}).get("h24") or 0
    price_change_24h = pair.get("priceChange", {}).get("h24") or 0

    # 1. Потенциал по ликвидности
    if liquidity_usd >= 50_000:
        potential_score += 20
    elif liquidity_usd >= 20_000:
        potential_score += 10

    # 2. Потенциал по объёму торгов
    if volume_24h >= 100_000:
        potential_score += 30
    elif volume_24h >= 10_000:
        potential_score += 20
    elif volume_24h >= 1_000:
        potential_score += 10

    # 3. Потенциал по росту цены
    if 5 <= price_change_24h <= 50:
        potential_score += 30
    elif 0 < price_change_24h < 5:
        potential_score += 15

    # 4. Штраф за слишком сильный памп
    if price_change_24h > 100:
        potential_score -= 30
    elif price_change_24h > 50:
        potential_score -= 15

    if potential_score < 0:
        potential_score = 0

    if potential_score > 100:
        potential_score = 100

    return potential_score

def calculate_final_score(pair):
    risk_score = calculate_risk_score(pair)
    potential_score = calculate_potential_score(pair)

    final_score = potential_score - risk_score

    if final_score < 0:
        final_score = 0

    if final_score > 100:
        final_score = 100

    return final_score