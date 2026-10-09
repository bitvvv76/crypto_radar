from datetime import datetime, timedelta

from database import (
    close_open_baseline,
    ensure_paper_tables,
    get_active_tracking_positions,
    get_expired_open_positions,
    get_24h_paper_candidates_for_pairs,
    get_last_mark_within_window,
    insert_price_mark,
    mark_exists,
    open_baseline_position,
)


STRATEGY_VERSION = "BASELINE_V04"
STOP_LOSS_PERCENT = 15
TRAILING_START_PERCENT = 10
TRAILING_DISTANCE_PERCENT = 5
MAX_HOLD_HOURS = 168
MARK_INTERVAL_MINUTES = 15
ENTRY_GRACE_MINUTES = 30
MIN_FINAL_SCORE = 70
PRIMARY_FINAL_SCORE = 80

SIGNAL_WEAK = "WEAK"
SIGNAL_NEUTRAL = "NEUTRAL"
SIGNAL_CONFIRMED = "CONFIRMED"
SIGNAL_WATCH = "WATCH"
SIGNAL_OVERHEAT = "OVERHEAT"

COHORT_PRIMARY = "PRIMARY"
COHORT_CONTROL = "CONTROL"

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"

EXIT_STOP_LOSS = "STOP_LOSS"
EXIT_TRAILING_STOP = "TRAILING_STOP"
EXIT_TIME = "TIME_EXIT"


def parse_datetime(value):
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.replace(tzinfo=None)

        return value

    text = str(value).strip()

    for time_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, time_format)
        except ValueError:
            continue

    raise ValueError("Не удалось разобрать дату: {0}".format(value))


def format_datetime(value):
    return parse_datetime(value).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def utc_now():
    return datetime.utcnow().replace(microsecond=0)


def tracking_deadline(entry_time, max_hold_hours):
    return parse_datetime(entry_time) + timedelta(hours=float(max_hold_hours))


def is_active_tracking(entry_time, now, max_hold_hours):
    return parse_datetime(now) <= tracking_deadline(entry_time, max_hold_hours)


def is_expired_window(entry_time, now, max_hold_hours):
    return parse_datetime(now) > tracking_deadline(entry_time, max_hold_hours)


def elapsed_hours(entry_time, observed_at):
    delta = parse_datetime(observed_at) - parse_datetime(entry_time)
    return delta.total_seconds() / 3600


def is_fresh_24h(checked_at, now, grace_minutes=ENTRY_GRACE_MINUTES):
    age = parse_datetime(now) - parse_datetime(checked_at)
    return timedelta(0) <= age <= timedelta(minutes=grace_minutes)


def score_cohort(final_score):
    if final_score is None or final_score < MIN_FINAL_SCORE:
        return None

    if final_score >= PRIMARY_FINAL_SCORE:
        return COHORT_PRIMARY

    return COHORT_CONTROL


def classify_signal_type(change_24h):
    if change_24h is None:
        return None

    change = float(change_24h)

    if change < 0:
        return SIGNAL_WEAK

    if change < 3:
        return SIGNAL_NEUTRAL

    if change < 6:
        return SIGNAL_CONFIRMED

    if change < 10:
        return SIGNAL_WATCH

    return SIGNAL_OVERHEAT


def normalize_price(value):
    if value is None:
        return None

    try:
        price = float(value)
    except (TypeError, ValueError):
        return None

    if price <= 0:
        return None

    return price


def calculate_profit_percent(entry_price, price):
    entry = normalize_price(entry_price)
    current = normalize_price(price)

    if entry is None or current is None:
        return None

    return round(((current - entry) / entry) * 100, 2)


def calculate_drawdown_from_max_percent(max_price, last_price):
    peak = normalize_price(max_price)
    current = normalize_price(last_price)

    if peak is None or current is None:
        return None

    drawdown = ((peak - current) / peak) * 100

    if drawdown < 0:
        drawdown = 0

    return round(drawdown, 2)


def observation_bucket(observed_at, interval_minutes=MARK_INTERVAL_MINUTES):
    moment = parse_datetime(observed_at).replace(microsecond=0)
    bucket_minute = (moment.minute // interval_minutes) * interval_minutes
    bucket = moment.replace(minute=bucket_minute, second=0)
    return format_datetime(bucket)


def decide_baseline_exit(
    profit_percent,
    max_profit_percent,
    drawdown_from_max_percent,
    elapsed_hours_value,
    stop_loss_percent,
    trailing_start_percent,
    trailing_distance_percent,
    max_hold_hours,
):
    if (
        profit_percent is not None
        and stop_loss_percent is not None
        and profit_percent <= -float(stop_loss_percent)
    ):
        return EXIT_STOP_LOSS

    trailing_armed = (
        max_profit_percent is not None
        and trailing_start_percent is not None
        and max_profit_percent >= float(trailing_start_percent)
    )

    if (
        trailing_armed
        and drawdown_from_max_percent is not None
        and trailing_distance_percent is not None
        and drawdown_from_max_percent >= float(trailing_distance_percent)
    ):
        return EXIT_TRAILING_STOP

    if (
        elapsed_hours_value is not None
        and max_hold_hours is not None
        and elapsed_hours_value >= float(max_hold_hours)
    ):
        return EXIT_TIME

    return None


def project_baseline(position, price, observed_at):
    entry_price = position["entry_price"]
    current_max = normalize_price(position.get("max_price"))
    observed_price = normalize_price(price)

    if current_max is None or observed_price > current_max:
        max_price = observed_price
    else:
        max_price = current_max

    max_profit_percent = calculate_profit_percent(entry_price, max_price)
    drawdown_from_max_percent = calculate_drawdown_from_max_percent(
        max_price,
        observed_price,
    )
    profit_percent = calculate_profit_percent(entry_price, observed_price)
    observed_text = format_datetime(observed_at)
    exit_reason = decide_baseline_exit(
        profit_percent=profit_percent,
        max_profit_percent=max_profit_percent,
        drawdown_from_max_percent=drawdown_from_max_percent,
        elapsed_hours_value=elapsed_hours(position["entry_time"], observed_text),
        stop_loss_percent=position["stop_loss_percent"],
        trailing_start_percent=position["trailing_start_percent"],
        trailing_distance_percent=position["trailing_distance_percent"],
        max_hold_hours=position["max_hold_hours"],
    )

    projected = {
        "last_price": observed_price,
        "last_checked_at": observed_text,
        "max_price": max_price,
        "max_profit_percent": max_profit_percent,
        "drawdown_from_max_percent": drawdown_from_max_percent,
        "profit_percent": profit_percent,
        "exit_reason": exit_reason,
    }

    if exit_reason is None:
        return projected

    projected["exit_price"] = observed_price
    projected["exit_time"] = observed_text
    projected["result_percent"] = profit_percent
    return projected


def baseline_update_from_projection(position, projected, updated_at):
    update = {
        "position_id": position["id"],
        "last_price": projected["last_price"],
        "last_checked_at": projected["last_checked_at"],
        "max_price": projected["max_price"],
        "max_profit_percent": projected["max_profit_percent"],
        "drawdown_from_max_percent": projected["drawdown_from_max_percent"],
        "updated_at": format_datetime(updated_at),
    }

    if projected["exit_reason"] is None:
        update["action"] = "update"
        return update

    update["action"] = "close"
    update["exit_price"] = projected["exit_price"]
    update["exit_time"] = projected["exit_time"]
    update["exit_reason"] = projected["exit_reason"]
    update["result_percent"] = projected["result_percent"]
    return update


def empty_cycle_stats():
    return {
        "opened": 0,
        "marks_inserted": 0,
        "baseline_updates": 0,
        "baseline_closes": 0,
        "expired_closed": 0,
        "expired_without_mark": 0,
        "skipped_existing_bucket": 0,
        "price_missing": 0,
        "new_24h_events": 0,
        "sql_candidates": 0,
        "rejections": [],
        "entry_errors": [],
    }


def default_price_fetcher(chain_id, pair_address):
    from scanner import get_pair_by_address

    fresh_pair = get_pair_by_address(chain_id, pair_address)

    if fresh_pair is None:
        return None

    return normalize_price(fresh_pair.get("priceUsd"))


def candidate_rejection_reason(candidate):
    """Та же последовательность проверок, что и у допуска кандидата."""
    if score_cohort(candidate.get("final_score")) is None:
        return "score_outside_cohort"

    if classify_signal_type(candidate.get("change_24h")) is None:
        return "change_24h_unclassified"

    if normalize_price(candidate.get("entry_price")) is None:
        return "entry_price_invalid"

    return None


def _candidate_allowed(candidate):
    return candidate_rejection_reason(candidate) is None


def _open_new_positions(now, db_path, stats, new_24h_pair_ids):
    now_text = format_datetime(now)
    events = list(new_24h_pair_ids or [])
    stats["new_24h_events"] = len(events)
    stats["rejections"] = []
    stats["entry_errors"] = []

    if not events:
        stats["sql_candidates"] = 0
        return set()

    candidates = get_24h_paper_candidates_for_pairs(
        pair_ids=events,
        min_final_score=MIN_FINAL_SCORE,
        db_path=db_path,
    )
    stats["sql_candidates"] = len(candidates)
    _record_sql_gaps(stats, events, candidates, db_path)
    opened_pair_ids = set()

    for candidate in candidates:
        pair_id = candidate["pair_id"]

        try:
            rejection = candidate_rejection_reason(candidate)
            if rejection is not None:
                stats["rejections"].append({
                    "pair_id": pair_id,
                    "reason": rejection,
                })
                continue

            entry_price = normalize_price(candidate["entry_price"])
            entry_time = format_datetime(candidate["entry_time"])
            created = open_baseline_position(
                pair_id=pair_id,
                strategy_version=STRATEGY_VERSION,
                signal_type=classify_signal_type(candidate["change_24h"]),
                final_score=candidate["final_score"],
                change_24h=candidate["change_24h"],
                entry_price=entry_price,
                entry_time=entry_time,
                observation_bucket=observation_bucket(entry_time),
                stop_loss_percent=STOP_LOSS_PERCENT,
                trailing_start_percent=TRAILING_START_PERCENT,
                trailing_distance_percent=TRAILING_DISTANCE_PERCENT,
                max_hold_hours=MAX_HOLD_HOURS,
                created_at=now_text,
                db_path=db_path,
            )
        except Exception as error:
            from monitor_store import redact_text

            print("PAPER ENGINE: ошибка входа, Pair ID:", pair_id)
            print(redact_text(error))
            stats["entry_errors"].append({
                "pair_id": pair_id,
                "reason": "entry_error",
                "error_type": type(error).__name__,
            })
            continue

        if not created:
            stats["rejections"].append({
                "pair_id": pair_id,
                "reason": "baseline_already_exists",
            })
            continue

        opened_pair_ids.add(pair_id)
        stats["opened"] += 1
        stats["marks_inserted"] += 1
        print("PAPER ENGINE: открыта baseline-позиция, Pair ID:", pair_id)

    return opened_pair_ids


def _track_active_positions(now, price_fetcher, db_path, opened_pair_ids, stats):
    now_text = format_datetime(now)
    current_bucket = observation_bucket(now_text)
    positions = get_active_tracking_positions(now_text, db_path=db_path)

    for position in positions:
        pair_id = position["pair_id"]

        try:
            if pair_id in opened_pair_ids:
                continue

            if not is_active_tracking(
                position["entry_time"],
                now_text,
                position["max_hold_hours"],
            ):
                continue

            if mark_exists(pair_id, current_bucket, db_path=db_path):
                stats["skipped_existing_bucket"] += 1
                continue

            price = _fetch_price(price_fetcher, position)

            if price is None:
                stats["price_missing"] += 1
                continue

            profit_percent = calculate_profit_percent(position["entry_price"], price)

            if profit_percent is None:
                stats["price_missing"] += 1
                continue

            baseline_update = None

            if position["status"] == STATUS_OPEN:
                projected = project_baseline(position, price, now_text)
                baseline_update = baseline_update_from_projection(
                    position,
                    projected,
                    now_text,
                )

            inserted = insert_price_mark(
                pair_id=pair_id,
                price_usd=price,
                observed_at=now_text,
                profit_percent_from_entry=profit_percent,
                observation_bucket=current_bucket,
                baseline_update=baseline_update,
                db_path=db_path,
            )
        except Exception as error:
            print("PAPER ENGINE: ошибка наблюдения, Pair ID:", pair_id)
            print(error)
            continue

        if not inserted:
            stats["skipped_existing_bucket"] += 1
            continue

        stats["marks_inserted"] += 1

        if baseline_update is None:
            continue

        if baseline_update["action"] == "close":
            stats["baseline_closes"] += 1
            print(
                "PAPER ENGINE: baseline закрыта,",
                baseline_update["exit_reason"],
                "Pair ID:",
                pair_id,
            )
            continue

        stats["baseline_updates"] += 1


def _fetch_price(price_fetcher, position):
    chain_id = position.get("chain_id")
    pair_address = position.get("pair_address")

    if not chain_id or not pair_address:
        return None

    try:
        return normalize_price(price_fetcher(chain_id, pair_address))
    except Exception as error:
        print("PAPER ENGINE: цена не получена, Pair ID:", position.get("pair_id"))
        print(error)
        return None


def _close_expired_open_positions(now, db_path, stats):
    now_text = format_datetime(now)
    positions = get_expired_open_positions(now_text, db_path=db_path)

    for position in positions:
        pair_id = position["pair_id"]

        try:
            if position["status"] != STATUS_OPEN:
                continue

            if not is_expired_window(
                position["entry_time"],
                now_text,
                position["max_hold_hours"],
            ):
                continue

            deadline = format_datetime(
                tracking_deadline(position["entry_time"], position["max_hold_hours"])
            )
            mark = get_last_mark_within_window(
                pair_id,
                deadline,
                db_path=db_path,
            )

            if mark is None:
                stats["expired_without_mark"] += 1
                print(
                    "PAPER ENGINE: нет mark внутри окна, TIME_EXIT пропущен, Pair ID:",
                    pair_id,
                )
                continue

            exit_price = normalize_price(mark["price_usd"])

            if exit_price is None:
                stats["expired_without_mark"] += 1
                print(
                    "PAPER ENGINE: mark без цены, TIME_EXIT пропущен, Pair ID:",
                    pair_id,
                )
                continue

            max_price = normalize_price(position.get("max_price")) or exit_price
            max_profit_percent = position.get("max_profit_percent")

            if max_profit_percent is None:
                max_profit_percent = calculate_profit_percent(
                    position["entry_price"],
                    max_price,
                )

            result_percent = mark.get("profit_percent_from_entry")

            if result_percent is None:
                result_percent = calculate_profit_percent(
                    position["entry_price"],
                    exit_price,
                )

            closed = close_open_baseline(
                position_id=position["id"],
                last_price=exit_price,
                last_checked_at=format_datetime(mark["observed_at"]),
                max_price=max_price,
                max_profit_percent=max_profit_percent,
                drawdown_from_max_percent=calculate_drawdown_from_max_percent(
                    max_price,
                    exit_price,
                ),
                exit_price=exit_price,
                exit_time=format_datetime(mark["observed_at"]),
                exit_reason=EXIT_TIME,
                result_percent=result_percent,
                updated_at=now_text,
                db_path=db_path,
            )
        except Exception as error:
            print("PAPER ENGINE: ошибка TIME_EXIT, Pair ID:", pair_id)
            print(error)
            continue

        if not closed:
            continue

        stats["expired_closed"] += 1
        print("PAPER ENGINE: TIME_EXIT по последнему mark, Pair ID:", pair_id)


def _record_sql_gaps(stats, events, candidates, db_path):
    try:
        from signal_diagnostics import explain_sql_gaps

        stats["rejections"].extend(explain_sql_gaps(
            events,
            [candidate["pair_id"] for candidate in candidates],
            MIN_FINAL_SCORE,
            db_path=db_path,
        ))
    except Exception as error:
        stats["entry_errors"].append({
            "pair_id": None,
            "reason": "entry_error",
            "error_type": type(error).__name__,
        })


def _print_cycle_summary(stats):
    print()
    print("ИТОГ PAPER ENGINE")
    print("=================")
    print("Новых событий 24h:", stats["new_24h_events"])
    print("Кандидатов после SQL:", stats["sql_candidates"])
    print("Открыто baseline:", stats["opened"])
    print("Отклонено кандидатов:", len(stats["rejections"]))
    print("Новых marks:", stats["marks_inserted"])
    print("Обновлено OPEN:", stats["baseline_updates"])
    print("Закрыто по наблюдению:", stats["baseline_closes"])
    print("TIME_EXIT после окна:", stats["expired_closed"])
    print("Окно кончилось без mark:", stats["expired_without_mark"])
    print("Пропущено, bucket уже есть:", stats["skipped_existing_bucket"])
    print("Нет свежей цены:", stats["price_missing"])


def run_cycle(now=None, price_fetcher=None, db_path=None, new_24h_pair_ids=None):
    if now is None:
        now = utc_now()

    if price_fetcher is None:
        price_fetcher = default_price_fetcher

    if new_24h_pair_ids is None:
        new_24h_pair_ids = []

    stats = empty_cycle_stats()

    print("CRYPTO RADAR — PAPER ENGINE")
    print("============================")

    ensure_paper_tables(db_path)
    opened_pair_ids = _open_new_positions(now, db_path, stats, new_24h_pair_ids)
    _track_active_positions(
        now,
        price_fetcher,
        db_path,
        opened_pair_ids,
        stats,
    )
    _close_expired_open_positions(now, db_path, stats)
    _print_cycle_summary(stats)

    return stats


if __name__ == "__main__":
    from paper_portfolio import run_engine_with_portfolio

    cycle_now = utc_now()
    run_engine_with_portfolio(
        cycle_now,
        lambda: run_cycle(now=cycle_now),
    )
