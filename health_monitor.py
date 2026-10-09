"""
Контроль исправности сканера, проверок цены, API и доставки Telegram.

Повторное уведомление уходит только при новом состоянии.
"""

from datetime import datetime, timedelta

from monitor_store import (
    JOB_AUTO_CHECK,
    JOB_SCANNER,
    latest_job_run,
    list_health_state,
    redact_text,
)


SCAN_STALE_AFTER = timedelta(hours=26)
PRICE_CHECK_STALE_AFTER = timedelta(minutes=45)

ALERT_SCANNER = "scanner"
ALERT_PRICE_CHECK = "price_check"
ALERT_API = "api"
ALERT_TELEGRAM = "telegram"

STATE_OK = "ok"
STATE_NEVER_RAN = "never_ran"
STATE_STALE = "stale"
STATE_ERROR = "error"


def evaluate_health(
    db_path,
    now,
    telegram_errors=None,
    scan_stale_after=SCAN_STALE_AFTER,
    price_stale_after=PRICE_CHECK_STALE_AFTER,
):
    moment = _as_datetime(now)
    previous = list_health_state(db_path)
    desired = {
        ALERT_SCANNER: _job_state(
            latest_job_run(db_path, JOB_SCANNER),
            moment,
            scan_stale_after,
        ),
        ALERT_PRICE_CHECK: _job_state(
            latest_job_run(db_path, JOB_AUTO_CHECK),
            moment,
            price_stale_after,
        ),
        ALERT_API: _api_state(db_path),
        ALERT_TELEGRAM: _telegram_state(telegram_errors),
    }
    changes = []
    for key in (ALERT_SCANNER, ALERT_PRICE_CHECK, ALERT_API, ALERT_TELEGRAM):
        current = desired[key]
        stored = previous.get(key)
        stored_state = None if stored is None else stored.get("state")
        if current["state"] == STATE_OK and stored_state is None:
            continue
        if current["state"] == stored_state:
            continue
        changes.append({
            "alert_key": key,
            "state": current["state"],
            "detail": current.get("detail"),
            "previous_state": stored_state,
        })
    return {
        "changes": changes,
        "states": {
            item["alert_key"]: {
                "state": item["state"],
                "detail": item.get("detail"),
            }
            for item in changes
        },
    }


def render_health_alert(changes, now):
    lines = [
        "CRYPTO RADAR — контроль исправности",
        "Время UTC: {0}".format(_as_text(now)),
    ]
    for item in changes:
        lines.append(_change_line(item))
    return "\n".join(lines)


def _job_state(run, now, stale_after):
    if run is None:
        return {"state": STATE_NEVER_RAN, "detail": "запусков нет"}
    moment = _parse_time(run.get("finished_at") or run.get("started_at"))
    if moment is None:
        return {"state": STATE_STALE, "detail": "время последнего запуска не читается"}
    age = now - moment
    if age > stale_after:
        return {
            "state": STATE_STALE,
            "detail": "последний запуск {0}, статус {1}".format(
                run.get("finished_at") or run.get("started_at"),
                run.get("status"),
            ),
        }
    return {
        "state": STATE_OK,
        "detail": "последний запуск {0}, статус {1}".format(
            run.get("finished_at") or run.get("started_at"),
            run.get("status"),
        ),
    }


def _api_state(db_path):
    kinds = []
    for job_name in (JOB_SCANNER, JOB_AUTO_CHECK):
        run = latest_job_run(db_path, job_name)
        summary = {} if run is None or run.get("summary") is None else run["summary"]
        for item in summary.get("api_errors") or []:
            kind = item.get("kind")
            source = item.get("source") or job_name
            if kind:
                kinds.append("{0}:{1}".format(source, kind))
    if not kinds:
        return {"state": STATE_OK, "detail": "ошибок API в последних запусках нет"}
    fingerprint = ",".join(sorted(set(kinds)))
    return {
        "state": "{0}:{1}".format(STATE_ERROR, fingerprint),
        "detail": fingerprint,
    }


def _telegram_state(telegram_errors):
    public = []
    for item in telegram_errors or []:
        text = redact_text(item).strip()
        if text:
            public.append(text)
    if not public:
        return {"state": STATE_OK, "detail": "ошибок доставки нет"}
    fingerprint = " | ".join(sorted(set(public)))
    return {
        "state": "{0}:{1}".format(STATE_ERROR, fingerprint),
        "detail": fingerprint,
    }


def _change_line(item):
    key = item["alert_key"]
    state = item["state"]
    detail = item.get("detail") or ""
    previous = item.get("previous_state")
    if state == STATE_OK or (isinstance(state, str) and state == STATE_OK):
        title = _ok_title(key)
        return "{0}. {1}".format(title, detail)
    if previous not in (None, STATE_OK) and _is_problem(state):
        title = _problem_title(key, state)
        return "{0}. Состояние изменилось. {1}".format(title, detail)
    title = _problem_title(key, state)
    return "{0}. {1}".format(title, detail)


def _ok_title(key):
    if key == ALERT_SCANNER:
        return "Плановое сканирование снова выполняется"
    if key == ALERT_PRICE_CHECK:
        return "Проверки цены снова выполняются"
    if key == ALERT_API:
        return "Ошибки API в последнем запуске отсутствуют"
    return "Доставка Telegram снова проходит"


def _problem_title(key, state):
    if key == ALERT_SCANNER and state == STATE_NEVER_RAN:
        return "Плановое сканирование не выполнялось"
    if key == ALERT_SCANNER and state == STATE_STALE:
        return "Плановое сканирование не выполняется"
    if key == ALERT_PRICE_CHECK and state == STATE_NEVER_RAN:
        return "Проверки цены не выполнялись"
    if key == ALERT_PRICE_CHECK and state == STATE_STALE:
        return "Проверки цены не выполняются"
    if key == ALERT_API:
        return "Ошибка API"
    return "Ошибка доставки Telegram"


def _is_problem(state):
    return state != STATE_OK


def _as_datetime(now):
    if isinstance(now, datetime):
        return now.replace(tzinfo=None, microsecond=0)
    return _parse_time(now)


def _as_text(now):
    return _as_datetime(now).strftime("%Y-%m-%d %H:%M:%S")


def _parse_time(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    if value is None:
        return None
    text = str(value).strip()
    for time_format in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text, time_format)
        except ValueError:
            continue
    return None
