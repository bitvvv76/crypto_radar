def calculate_idea_result_score(checks):
    score = 0
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

        if price_change_percent > flat_threshold:
            score += 25
        elif abs(price_change_percent) <= flat_threshold:
            score += 10

    if score > 100:
        score = 100

    return score


def get_idea_quality(idea_result_score):
    if idea_result_score <= 25:
        return "WEAK (СЛАБАЯ)"

    if idea_result_score <= 50:
        return "NEUTRAL (НЕЙТРАЛЬНАЯ)"

    if idea_result_score <= 75:
        return "GOOD (ХОРОШАЯ)"

    return "STRONG (СИЛЬНАЯ)"


def get_score_accuracy_status(final_score, idea_result_score):
    if final_score is None:
        return "NO_SCORE (НЕТ ОЦЕНКИ)"

    difference = idea_result_score - final_score

    if difference < -10:
        return "OVERESTIMATED (ПЕРЕОЦЕНИЛА)"

    if difference > 10:
        return "UNDERESTIMATED (НЕДООЦЕНИЛА)"

    return "ACCURATE (БЛИЗКО)"


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