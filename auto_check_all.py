from database import (
    get_pairs_for_next_checks,
    get_price_checks_count_for_pair,
    get_existing_check_periods_for_pair,
)
from price_checker import check_pair_price
from check_utils import (
    get_check_status,
    get_next_check_period,
    is_check_due,
    get_time_until_check,
    format_remaining_time,
)
from daily_report import observe_auto_check
from monitor_store import redact_text, utc_now_text
from scanner import consume_api_errors


def _is_newly_created_24h_check(result):
    if not isinstance(result, dict):
        return False

    if result.get("status"):
        return False

    if result.get("check_period") != "24h":
        return False

    if result.get("already_checked"):
        return False

    if result.get("price_change_percent") is None:
        return False

    return True


def main():
    print("CRYPTO RADAR — АВТОПРОВЕРКА ВСЕЙ ОЧЕРЕДИ")
    print("==========================================")

    started_at = utc_now_text()
    consume_api_errors()
    price_check_errors = []
    new_24h_pair_ids = []
    paper_stats = None
    paper_error = None
    approval_stats = None
    approval_error = None
    cycle_now = None
    fatal_error = None

    try:
        pairs = get_pairs_for_next_checks()

        completed_count = 0
        checked_count = 0
        waiting_count = 0
        data_not_found_count = 0
        failed_count = 0

        for pair in pairs:
            (
                pair_id,
                chain_id,
                dex_id,
                pair_symbol,
                price_usd,
                risk_score,
                potential_score,
                final_score,
                created_at,
            ) = pair

            price_checks_count = get_price_checks_count_for_pair(pair_id)
            check_status = get_check_status(price_checks_count)

            if check_status.startswith("COMPLETE"):
                completed_count += 1
                continue

            existing_periods = get_existing_check_periods_for_pair(pair_id)
            next_check_period = get_next_check_period(existing_periods)

            print()
            print("-----------------------------")
            print("ID:", pair_id)
            print("Пара:", pair_symbol)
            print("Следующая проверка:", next_check_period)

            if not is_check_due(created_at, next_check_period):
                remaining = get_time_until_check(created_at, next_check_period)

                print("Статус: ЕЩЁ РАНО")
                print("До проверки осталось:", format_remaining_time(remaining))

                waiting_count += 1
                continue

            result = check_pair_price(
                pair_id,
                next_check_period,
                return_error=True,
            )

            if result is None:
                print("Статус: ОШИБКА ПРОВЕРКИ")
                failed_count += 1
                price_check_errors.append({
                    "kind": "CHECK_FAILED",
                    "pair_id": pair_id,
                })
                continue

            if result.get("status") == "DATA_NOT_FOUND":
                print("Статус: DATA_NOT_FOUND")
                data_not_found_count += 1
                price_check_errors.append({
                    "kind": "DATA_NOT_FOUND",
                    "pair_id": pair_id,
                })
                continue

            if result.get("status") == "PRICE_NOT_FOUND":
                print("Статус: PRICE_NOT_FOUND")
                failed_count += 1
                price_check_errors.append({
                    "kind": "PRICE_NOT_FOUND",
                    "pair_id": pair_id,
                })
                continue

            if result.get("status") == "PAIR_NOT_FOUND":
                print("Статус: PAIR_NOT_FOUND")
                failed_count += 1
                price_check_errors.append({
                    "kind": "PAIR_NOT_FOUND",
                    "pair_id": pair_id,
                })
                continue

            print("Статус: ПРОВЕРКА ВЫПОЛНЕНА")
            print("Период:", result["check_period"])
            print("Старая цена:", result["old_price_usd"])
            print("Новая цена:", result["new_price_usd"])
            print("Изменение цены %:", result["price_change_percent"])

            if _is_newly_created_24h_check(result):
                new_24h_pair_ids.append(pair_id)

            checked_count += 1

        print()
        print("ИТОГ АВТОПРОВЕРКИ")
        print("==================")
        print("Всего идей в базе:", len(pairs))
        print("Уже полностью проверены:", completed_count)
        print("Проверок выполнено сейчас:", checked_count)
        print("Ещё не наступил срок:", waiting_count)
        print("Пары без свежих данных:", data_not_found_count)
        print("Ошибок проверки:", failed_count)

        try:
            from paper_engine import run_cycle, utc_now
            from paper_portfolio import run_engine_with_portfolio

            cycle_now = utc_now()

            def _run_paper():
                nonlocal paper_stats
                paper_stats = run_cycle(
                    now=cycle_now,
                    new_24h_pair_ids=new_24h_pair_ids,
                )
                return paper_stats

            def _guarded_paper():
                nonlocal paper_error
                try:
                    return _run_paper()
                except Exception as error:
                    paper_error = error
                    raise

            run_engine_with_portfolio(
                cycle_now,
                _guarded_paper,
            )
        except Exception as error:
            if paper_error is None:
                paper_error = error
            print()
            print("PAPER ENGINE: ошибка, проверки цены уже завершены")
            print(redact_text(error))

        try:
            from human_approval import run_approval_maintenance
            if cycle_now is None:
                from paper_engine import utc_now
                cycle_now = utc_now()
            approval_stats = run_approval_maintenance(cycle_now)
        except Exception as error:
            approval_error = error
            print()
            print("HUMAN APPROVAL: ошибка, контроль уже завершён")
            print(redact_text(error))
    except Exception as error:
        fatal_error = error
        raise
    finally:
        created_at = None if cycle_now is None else utc_now_text(cycle_now)
        observed_errors = list(price_check_errors)
        if fatal_error is not None:
            observed_errors.append({
                "kind": "CYCLE_FAILED",
                "pair_id": None,
            })
        try:
            observe_auto_check(
                None,
                started_at,
                new_24h_pair_ids,
                observed_errors,
                paper_stats,
                paper_error,
                approval_stats,
                approval_error,
                api_errors=consume_api_errors(),
                approval_created_at=created_at,
            )
        except Exception as error:
            print("ДИАГНОСТИКА: запись не выполнена")
            print(type(error).__name__)


if __name__ == "__main__":
    main()
