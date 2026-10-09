"""
Telegram UI для Human Approval v0.7 и сопровождение v0.8.1.

Бот показывает заявки, передаёт BUY или SKIP в ядро и сообщает
уже закрытую позицию книги 2. Цену и SELL он не исполняет.
Реальных ордеров и ключей биржи здесь нет.
"""

import argparse
import os
import re
import sys
import time

import requests

import database
from approval_bot_store import (
    DELIVERY_DELIVERED,
    DELIVERY_DISPATCHING,
    DELIVERY_RESERVED,
    attach_message,
    claim_notification,
    get_notification,
    list_notifications,
    mark_dispatching,
    record_delivered_message,
    release_unsent,
    update_last_status,
)
from human_approval import (
    ACTION_BUY,
    ACTION_SKIP,
    OUTCOME_REJECTED,
    STATUS_BUY,
    STATUS_EXPIRED,
    STATUS_PENDING,
    STATUS_SKIP,
    decide_buy,
    decide_skip,
    get_request_view,
    list_actionable,
)
from paper_engine import format_datetime, utc_now
from position_notifications import deliver_closed_positions, handle_positions_message
from monitor_store import redact_text


LABEL_BUY_EXECUTED = "✅ BUY EXECUTED"
LABEL_SKIPPED = "❌ SKIPPED"
LABEL_EXPIRED = "⌛ EXPIRED"
LABEL_BUY_NOT_EXECUTED = "⚠️ BUY NOT EXECUTED"

_CALLBACK_DATA = re.compile(r"^(buy|skip):([1-9][0-9]*)$")
_EMPTY_KEYBOARD = {"inline_keyboard": []}


class TelegramApiError(Exception):
    pass


class TelegramRejectedError(TelegramApiError):
    """Telegram вернул ok=false: сообщение не принято."""


def _response_body(response):
    try:
        return response.json()
    except Exception:
        return None


def _rejection_text(body):
    parts = ["telegram rejected the message"]
    code = body.get("error_code")
    description = body.get("description")
    if isinstance(code, int):
        parts.append("error_code={0}".format(code))
    if isinstance(description, str) and description.strip():
        parts.append(description.strip())
    return "; ".join(parts)


class BotSettings:
    def __init__(self, token, allowed_user_id, chat_id):
        self.token = token
        self.allowed_user_id = int(allowed_user_id)
        self.chat_id = int(chat_id)


class TelegramClient:
    def __init__(self, token, session=None, base_url="https://api.telegram.org"):
        self.token = token
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")

    def send_message(self, chat_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._post("sendMessage", payload)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._post("editMessageText", payload)

    def answer_callback_query(self, callback_query_id, text=None):
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self._post("answerCallbackQuery", payload)

    def get_updates(self, offset=None, timeout=0):
        payload = {
            "timeout": timeout,
            "allowed_updates": ["callback_query", "message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._post("getUpdates", payload, timeout=timeout + 10)
        return result or []

    def _post(self, method, payload, timeout=40):
        url = "{0}/bot{1}/{2}".format(self.base_url, self.token, method)
        try:
            response = self.session.post(url, json=payload, timeout=timeout)
        except Exception:
            raise TelegramApiError("telegram request failed")
        body = _response_body(response)
        if isinstance(body, dict) and body.get("ok") is False:
            raise TelegramRejectedError(_rejection_text(body))
        try:
            response.raise_for_status()
        except Exception:
            raise TelegramApiError("telegram request failed")
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramApiError("telegram request failed")
        return body.get("result")


def load_settings(environ=None):
    if environ is None:
        from dotenv import load_dotenv

        load_dotenv()
        environ = os.environ
    token = str(environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if token == "":
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    try:
        allowed_user_id = int(environ["TELEGRAM_ALLOWED_USER_ID"])
        chat_id = int(environ["TELEGRAM_CHAT_ID"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Telegram approval settings are incomplete") from error
    return BotSettings(token, allowed_user_id, chat_id)


def approval_keyboard(request_id):
    return {
        "inline_keyboard": [[
            {"text": "✅ BUY", "callback_data": "buy:{0}".format(request_id)},
            {"text": "❌ SKIP", "callback_data": "skip:{0}".format(request_id)},
        ]]
    }


def parse_callback_data(data):
    if not isinstance(data, str):
        return None
    match = _CALLBACK_DATA.fullmatch(data.strip())
    if match is None:
        return None
    return match.group(1), int(match.group(2))


def render_request(row):
    pair = row.get("pair_symbol") or row.get("pair_id")
    lines = [
        "Pair: {0}".format(pair),
        "Score: {0}".format(row.get("final_score")),
        "24h: {0}".format(row.get("change_24h")),
        "Cohort: {0}".format(row.get("cohort")),
        "Reference price: {0}".format(row.get("reference_price")),
        "Recommended USD: {0}".format(row.get("recommended_usd")),
        "Eligible until: {0}".format(row.get("eligible_until")),
    ]
    return "\n".join(lines)


def render_with_label(row, label):
    body = render_request(row)
    if not label:
        return body
    return "{0}\n\n{1}".format(label, body)


def label_for_result(action, result):
    status = result.get("status")
    if status == STATUS_EXPIRED:
        return LABEL_EXPIRED
    if status == STATUS_BUY:
        return LABEL_BUY_EXECUTED
    if status == STATUS_SKIP:
        return LABEL_SKIPPED
    if action == ACTION_BUY and result.get("outcome") == OUTCOME_REJECTED:
        return LABEL_BUY_NOT_EXECUTED
    return None


def label_for_status(status):
    if status == STATUS_BUY:
        return LABEL_BUY_EXECUTED
    if status == STATUS_SKIP:
        return LABEL_SKIPPED
    if status == STATUS_EXPIRED:
        return LABEL_EXPIRED
    return None


def markup_for_status(request_id, status):
    if status == STATUS_PENDING:
        return approval_keyboard(request_id)
    return _EMPTY_KEYBOARD


def deliver_pending(db_path, client, chat_id, clock=None):
    """
    Досылает актуальные PENDING без второй копии уже отправленного сообщения.

    reserved — Telegram ещё не вызывался, отправку можно повторить.
    dispatching — вызов уже начат; новый цикл сообщение не шлёт.
    delivered — message_id записан.
    Ошибка Telegram до принятия сообщения снимает резерв и не меняет книги.
    """
    clock = clock or utc_now
    now = clock()
    try:
        rows = list_actionable(now, db_path=db_path)
    except Exception as error:
        return {"sent": 0, "errors": [{"request_id": None, "error": str(error)}], "request_ids": []}

    sent_ids = []
    errors = []
    for row in rows:
        request_id = row["id"]
        sent_message = None
        dispatch_started = False
        try:
            existing = _notification_for_delivery(
                db_path,
                request_id,
                chat_id,
                format_datetime(now),
            )
            if existing is None or _delivery_blocks_send(existing):
                continue
            if not mark_dispatching(db_path, request_id):
                continue
            dispatch_started = True
            sent_message = client.send_message(
                chat_id,
                render_request(row),
                approval_keyboard(request_id),
            )
            message_id = _message_id_of(sent_message)
            if message_id is None:
                raise TelegramApiError("telegram request failed")
            _finish_delivery(db_path, request_id, message_id)
            sent_ids.append(request_id)
        except Exception as error:
            if _message_id_of(sent_message) is not None:
                _persist_sent_message(db_path, request_id, sent_message)
            elif dispatch_started:
                release_unsent(db_path, request_id)
            errors.append({
                "request_id": request_id,
                "error": str(error),
            })
    return {"sent": len(sent_ids), "errors": errors, "request_ids": sent_ids}


def refresh_notifications(db_path, client):
    """Подтягивает уже доставленное сообщение к статусу заявки в ядре."""
    refreshed = []
    for note in list_notifications(db_path):
        if note.get("message_id") is None:
            continue
        view = get_request_view(note["request_id"], db_path=db_path)
        if view is None or view.get("status") == note.get("last_status"):
            continue
        status = view.get("status")
        try:
            client.edit_message_text(
                note["chat_id"],
                note["message_id"],
                render_with_label(view, label_for_status(status)),
                markup_for_status(note["request_id"], status),
            )
        except Exception:
            continue
        update_last_status(db_path, note["request_id"], status)
        refreshed.append(note["request_id"])
    return refreshed


def handle_callback(update, settings, client, db_path, clock=None, price_fetcher=None):
    callback = None if not isinstance(update, dict) else update.get("callback_query")
    if not isinstance(callback, dict):
        return {"acted": False, "reason": "ignored", "result": None, "label": None}

    user_id = _user_id(callback)
    if user_id != settings.allowed_user_id:
        _safe_answer(client, callback.get("id"))
        return {"acted": False, "reason": "forbidden", "result": None, "label": None}

    parsed = parse_callback_data(callback.get("data"))
    if parsed is None:
        _safe_answer(client, callback.get("id"))
        return {"acted": False, "reason": "bad_callback", "result": None, "label": None}

    action, request_id = parsed
    if action == ACTION_BUY:
        result = decide_buy(
            request_id,
            db_path=db_path,
            price_fetcher=price_fetcher,
            clock=clock,
        )
    else:
        result = decide_skip(
            request_id,
            db_path=db_path,
            clock=clock,
        )
    label = label_for_result(action, result)
    _edit_decision(db_path, client, callback, request_id, result, label)
    _safe_answer(client, callback.get("id"))
    return {"acted": True, "reason": None, "result": result, "label": label}


def run_cycle(
    settings,
    client,
    db_path,
    offset=None,
    clock=None,
    price_fetcher=None,
    incoming_updates=None,
    poll_timeout=0,
):
    clock = clock or utc_now
    report = {
        "delivered": {"sent": 0, "errors": [], "request_ids": []},
        "closed": {"sent": 0, "errors": [], "allocation_ids": []},
        "refreshed": [],
        "handled": [],
        "errors": [],
        "offset": offset,
    }
    try:
        report["delivered"] = deliver_pending(
            db_path,
            client,
            settings.chat_id,
            clock=clock,
        )
    except Exception as error:
        report["errors"].append(str(error))
    for item in report["delivered"].get("errors", []):
        report["errors"].append(item.get("error") or str(item))
    try:
        report["closed"] = deliver_closed_positions(
            db_path,
            client,
            settings.chat_id,
            clock=clock,
        )
    except Exception as error:
        report["errors"].append(str(error))
    for item in report["closed"].get("errors", []):
        report["errors"].append(item.get("error") or str(item))
    try:
        report["refreshed"] = refresh_notifications(db_path, client)
    except Exception as error:
        report["errors"].append(str(error))

    updates = incoming_updates
    if updates is None:
        try:
            updates = client.get_updates(offset=offset, timeout=poll_timeout)
        except Exception as error:
            report["errors"].append(str(error))
            updates = []
    next_offset = offset
    for update in updates:
        try:
            if _is_message_update(update):
                report["handled"].append(handle_positions_message(
                    update,
                    settings,
                    client,
                    db_path,
                ))
            else:
                report["handled"].append(handle_callback(
                    update,
                    settings,
                    client,
                    db_path,
                    clock=clock,
                    price_fetcher=price_fetcher,
                ))
        except Exception as error:
            report["errors"].append(str(error))
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if isinstance(update_id, int):
            next_offset = update_id + 1
    report["offset"] = next_offset
    return report


def main(argv=None):
    configure_console()
    parser = argparse.ArgumentParser(description="Telegram UI для Human Approval")
    parser.add_argument("--db", dest="db_path", default=None)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    settings = load_settings()
    client = TelegramClient(settings.token)
    db_path = args.db_path or database.DB_NAME
    offset = None
    while True:
        try:
            report = run_cycle(
                settings,
                client,
                db_path,
                offset=offset,
                poll_timeout=30,
            )
            offset = report["offset"]
            _print_cycle(report)
            _deliver_monitoring_pass(db_path, client, settings, report)
            if report["errors"]:
                time.sleep(3)
        except Exception as error:
            print("TELEGRAM APPROVAL: сбой цикла UI")
            print(redact_text(error))
            time.sleep(5)
        if args.once:
            return 0


def configure_console():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def _edit_decision(db_path, client, callback, request_id, result, label):
    view = get_request_view(request_id, db_path=db_path)
    if view is None:
        return
    note = get_notification(db_path, request_id)
    message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_id = note.get("chat_id") if note is not None else chat.get("id")
    message_id = None
    if note is not None and note.get("message_id") is not None:
        message_id = note.get("message_id")
    else:
        message_id = message.get("message_id")
        if note is not None and message_id is not None:
            try:
                record_delivered_message(db_path, request_id, int(message_id))
            except Exception:
                pass
    if chat_id is None or message_id is None:
        return
    status = result.get("status") or view.get("status")
    try:
        client.edit_message_text(
            chat_id,
            message_id,
            render_with_label(view, label),
            markup_for_status(request_id, status),
        )
    except Exception:
        return
    if note is not None and status is not None:
        update_last_status(db_path, request_id, status)


def _notification_for_delivery(db_path, request_id, chat_id, sent_at):
    existing = get_notification(db_path, request_id)
    if existing is not None:
        return existing
    if not claim_notification(db_path, request_id, chat_id, sent_at, STATUS_PENDING):
        return get_notification(db_path, request_id)
    return get_notification(db_path, request_id)


def _delivery_blocks_send(existing):
    if existing.get("message_id") is not None:
        return True
    state = existing.get("delivery_state")
    if state == DELIVERY_DELIVERED:
        return True
    if state == DELIVERY_DISPATCHING:
        return True
    return state not in (DELIVERY_RESERVED, None)


def _message_id_of(sent_message):
    if not isinstance(sent_message, dict):
        return None
    message_id = sent_message.get("message_id")
    if message_id is None:
        return None
    return int(message_id)


def _finish_delivery(db_path, request_id, message_id):
    try:
        stored = attach_message(db_path, request_id, message_id)
    except Exception:
        stored = False
    if stored:
        return
    if not record_delivered_message(db_path, request_id, message_id):
        raise TelegramApiError("telegram request failed")


def _persist_sent_message(db_path, request_id, sent_message):
    message_id = _message_id_of(sent_message)
    if message_id is None:
        return False
    try:
        return record_delivered_message(db_path, request_id, message_id)
    except Exception:
        return False


def _safe_answer(client, callback_query_id):
    if not callback_query_id:
        return
    try:
        client.answer_callback_query(callback_query_id)
    except Exception:
        return


def _is_message_update(update):
    if not isinstance(update, dict):
        return False
    if update.get("callback_query") is not None:
        return False
    return isinstance(update.get("message"), dict)


def _user_id(callback):
    sender = callback.get("from")
    if not isinstance(sender, dict):
        return None
    try:
        return int(sender.get("id"))
    except (TypeError, ValueError):
        return None


def _print_cycle(report):
    delivered = report.get("delivered") or {}
    if delivered.get("sent"):
        print("TELEGRAM APPROVAL: отправлено", delivered["sent"])
    closed = report.get("closed") or {}
    if closed.get("sent"):
        print("TELEGRAM APPROVAL: закрытых позиций", closed["sent"])
    if report.get("refreshed"):
        print("TELEGRAM APPROVAL: обновлено", len(report["refreshed"]))
    acted = [
        item for item in report.get("handled") or []
        if item.get("acted")
    ]
    if acted:
        print("TELEGRAM APPROVAL: решений", len(acted))
    if report.get("errors"):
        print("TELEGRAM APPROVAL: ошибка Telegram API")


def _deliver_monitoring_pass(db_path, client, settings, report):
    """Тот же процесс и тот же клиент. Второй polling не запускается."""
    from daily_report import deliver_monitoring

    try:
        monitoring = deliver_monitoring(
            db_path,
            client,
            settings.chat_id,
            telegram_errors=report.get("errors") or [],
        )
    except Exception as error:
        print("TELEGRAM: сбой мониторинга")
        print(redact_text(error))
        return
    daily = monitoring.get("daily") or {}
    if daily.get("sent"):
        print("TELEGRAM: ежедневный отчёт отправлен")
    elif daily.get("status") == "delivery_unknown":
        print("TELEGRAM: доставка ежедневного отчёта не подтверждена")
    elif daily.get("status") == "failed":
        print("TELEGRAM: ежедневный отчёт не доставлен")
    if monitoring.get("health_sent"):
        print("TELEGRAM: уведомление контроля исправности отправлено")


if __name__ == "__main__":
    sys.exit(main())
