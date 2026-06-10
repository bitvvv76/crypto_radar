def get_check_status(price_checks_count):
    if price_checks_count == 0:
        return "NOT_CHECKED (НЕ ПРОВЕРЯЛАСЬ)"

    if price_checks_count < 4:
        return "IN_PROGRESS (В ПРОЦЕССЕ)"

    return "COMPLETE (ПОЛНАЯ)"


def get_idea_priority(final_score):
    if final_score is None:
        return "NO_SCORE (НЕТ ОЦЕНКИ)"

    if final_score >= 75:
        return "HIGH (ВЫСОКИЙ)"

    if final_score >= 50:
        return "MEDIUM (СРЕДНИЙ)"

    if final_score >= 25:
        return "LOW (НИЗКИЙ)"

    return "VERY_LOW (ОЧЕНЬ НИЗКИЙ)"


def get_next_check_period(existing_periods):
    required_periods = ["1h", "6h", "24h", "7d"]

    for period in required_periods:
        if period not in existing_periods:
            return period

    return "COMPLETE"