from datetime import datetime, timedelta

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

CHECK_PERIOD_DELAYS = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}


def parse_database_datetime(value):
    if isinstance(value, datetime):
        return value

    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")


def get_check_due_time(created_at, check_period):
    delay = CHECK_PERIOD_DELAYS.get(check_period)

    if delay is None:
        return None

    created_datetime = parse_database_datetime(created_at)

    return created_datetime + delay


def is_check_due(created_at, check_period, current_time=None):
    due_time = get_check_due_time(created_at, check_period)

    if due_time is None:
        return False

    if current_time is None:
        current_time = datetime.utcnow()

    return current_time >= due_time


def get_time_until_check(created_at, check_period, current_time=None):
    due_time = get_check_due_time(created_at, check_period)

    if due_time is None:
        return None

    if current_time is None:
        current_time = datetime.utcnow()

    remaining = due_time - current_time

    if remaining.total_seconds() <= 0:
        return timedelta(0)

    return remaining


def format_remaining_time(remaining):
    if remaining is None:
        return "неизвестно"

    total_seconds = int(remaining.total_seconds())

    if total_seconds <= 0:
        return "проверку уже можно выполнять"

    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []

    if days:
        parts.append(f"{days} дн.")

    if hours:
        parts.append(f"{hours} ч.")

    if minutes or not parts:
        parts.append(f"{minutes} мин.")

    return " ".join(parts)