"""
HTTP-адаптер публичного GeckoTerminal API V2.

Адаптер только получает и разбирает new pools. Он не пишет pairs,
не считает score и не открывает позиции.

Все HTTP-вызовы одного процесса идут через один limiter: не чаще
одного запроса примерно каждые GECKO_MIN_REQUEST_INTERVAL_SECONDS.
Первый запрос не ждёт. Ответ 429 не повторяется.
"""

import re
import threading
import time

import requests


GECKO_TERMINAL_BASE_URL = "https://api.geckoterminal.com/api/v2"
GECKO_MIN_REQUEST_INTERVAL_SECONDS = 8.0
DEFAULT_INCLUDE = "base_token,quote_token,dex"
DEFAULT_TIMEOUT = 20
USER_AGENT = "crypto-radar-discovery/0.10.1"
_NETWORK_RE = re.compile(r"[a-z0-9_]{1,64}")


class GeckoRequestLimiter:
    """Пауза между стартами соседних HTTP-запросов GeckoTerminal.

    clock и sleeper подменяются в тестах. По умолчанию это
    time.monotonic и time.sleep. Limiter не читает ответ и не
    повторяет запрос.
    """

    def __init__(self, interval_seconds=None, clock=None, sleeper=None):
        if interval_seconds is None:
            interval_seconds = GECKO_MIN_REQUEST_INTERVAL_SECONDS
        self.interval_seconds = float(interval_seconds)
        self.clock = time.monotonic if clock is None else clock
        self.sleeper = time.sleep if sleeper is None else sleeper
        self.request_count = 0
        self._last_request_at = None
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = self.clock()
            self.request_count += 1
            if self._last_request_at is None:
                self._last_request_at = now
                return 0.0
            remaining = self.interval_seconds - (now - self._last_request_at)
            if remaining > 0:
                self.sleeper(remaining)
                now = self.clock()
                scheduled = self._last_request_at + self.interval_seconds
                if now < scheduled:
                    now = scheduled
            self._last_request_at = now
            if remaining > 0:
                return remaining
            return 0.0


_REQUEST_LIMITER = GeckoRequestLimiter()


def get_request_limiter():
    return _REQUEST_LIMITER


def set_request_limiter(limiter):
    global _REQUEST_LIMITER
    previous = _REQUEST_LIMITER
    _REQUEST_LIMITER = limiter
    return previous


def fetch_new_pools(
    network,
    page=1,
    include=DEFAULT_INCLUDE,
    timeout=DEFAULT_TIMEOUT,
    getter=None,
):
    safe_network = _safe_network(network)
    if safe_network is None:
        return _failure("invalid_network")
    return _get_collection(
        "/networks/{0}/new_pools".format(safe_network),
        {"page": page, "include": include},
        timeout,
        getter,
    )


def fetch_global_new_pools(
    page=1,
    include="base_token,quote_token,dex,network",
    timeout=DEFAULT_TIMEOUT,
    getter=None,
):
    return _get_collection(
        "/networks/new_pools",
        {"page": page, "include": include},
        timeout,
        getter,
    )


def fetch_supported_networks(timeout=DEFAULT_TIMEOUT, getter=None, max_pages=5):
    networks = []
    page = 1
    while page <= max_pages:
        result = _get_collection(
            "/networks",
            {"page": page},
            timeout,
            getter,
        )
        if not result["ok"]:
            result["networks"] = networks
            return result
        for item in result["pools"]:
            if not isinstance(item, dict):
                continue
            attributes = item.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}
            networks.append({
                "id": item.get("id"),
                "name": attributes.get("name"),
                "coingecko_asset_platform_id": attributes.get(
                    "coingecko_asset_platform_id"
                ),
            })
        if len(result["pools"]) == 0:
            break
        page += 1
    return {
        "ok": True,
        "pools": [],
        "included": [],
        "networks": networks,
        "error_kind": None,
        "status_code": None,
    }


def parse_new_pool(pool, included=None, source_network=None):
    record = _blank_record(source_network)
    if not isinstance(pool, dict):
        record["ok"] = False
        record["reason"] = "malformed_pool"
        return record

    attributes = pool.get("attributes")
    if not isinstance(attributes, dict):
        record["ok"] = False
        record["reason"] = "malformed_pool"
        return record

    if not record["source_network"]:
        network_ref = _relation(pool, "network")
        if network_ref is not None:
            record["source_network"] = network_ref.get("id")

    record["pool_address"] = _text(attributes.get("address")) or None
    record["pool_created_at"] = _text(attributes.get("pool_created_at")) or None
    record["reserve_usd"] = _optional_float(attributes.get("reserve_in_usd"))
    record["price_usd"] = _optional_float(attributes.get("base_token_price_usd"))
    volume = attributes.get("volume_usd")
    if isinstance(volume, dict):
        record["volume_h24"] = _optional_float(volume.get("h24"))

    index = _included_index(included)
    base = _token(pool, index, "base_token")
    quote = _token(pool, index, "quote_token")
    record["base_token_address"] = base.get("address")
    record["base_symbol"] = base.get("symbol")
    record["quote_token_address"] = quote.get("address")
    record["quote_symbol"] = quote.get("symbol")
    record["dex"] = _dex_name(pool, index)
    record["ok"] = True
    return record


def _get_collection(path, params, timeout, getter):
    url = GECKO_TERMINAL_BASE_URL + path
    try:
        response = _request(url, params, timeout, getter)
    except requests.exceptions.Timeout:
        return _failure("timeout")
    except requests.exceptions.RequestException:
        return _failure("request_error")

    status_code = getattr(response, "status_code", None)
    if status_code == 429:
        return _failure("rate_limited", status_code)
    if isinstance(status_code, int) and status_code >= 500:
        return _failure("server_error", status_code)
    if status_code != 200:
        return _failure("http_error", status_code)

    try:
        payload = response.json()
    except ValueError:
        return _failure("malformed_json", status_code)

    if not isinstance(payload, dict):
        return _failure("malformed_json", status_code)

    pools = payload.get("data", [])
    included = payload.get("included", [])
    if pools is None:
        pools = []
    if included is None:
        included = []
    if not isinstance(pools, list) or not isinstance(included, list):
        return _failure("malformed_json", status_code)

    return {
        "ok": True,
        "pools": pools,
        "included": included,
        "networks": [],
        "error_kind": None,
        "status_code": status_code,
    }


def _request(url, params, timeout, getter):
    get_request_limiter().acquire()
    if getter is not None:
        return getter(url, params=params, timeout=timeout)
    return requests.get(
        url,
        params=params,
        timeout=timeout,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )


def _safe_network(network):
    if not isinstance(network, str):
        return None
    if _NETWORK_RE.fullmatch(network) is None:
        return None
    return network


def _failure(kind, status_code=None):
    return {
        "ok": False,
        "pools": [],
        "included": [],
        "networks": [],
        "error_kind": kind,
        "status_code": status_code,
    }


def _blank_record(source_network):
    return {
        "ok": False,
        "reason": None,
        "source": "geckoterminal_new_pools",
        "source_network": _text(source_network) or None,
        "pool_address": None,
        "pool_created_at": None,
        "dex": None,
        "base_token_address": None,
        "base_symbol": None,
        "quote_token_address": None,
        "quote_symbol": None,
        "reserve_usd": None,
        "volume_h24": None,
        "price_usd": None,
    }


def _included_index(included):
    index = {}
    if not isinstance(included, list):
        return index
    for item in included:
        if not isinstance(item, dict):
            continue
        index[(item.get("type"), item.get("id"))] = item
    return index


def _relation(pool, name):
    relationships = pool.get("relationships")
    if not isinstance(relationships, dict):
        return None
    relation = relationships.get(name)
    if not isinstance(relation, dict):
        return None
    data = relation.get("data")
    if not isinstance(data, dict):
        return None
    return data


def _token(pool, index, name):
    ref = _relation(pool, name)
    if ref is None:
        return {"address": None, "symbol": None}
    item = index.get((ref.get("type") or "token", ref.get("id")))
    if not isinstance(item, dict):
        return {"address": None, "symbol": None}
    attributes = item.get("attributes")
    if not isinstance(attributes, dict):
        return {"address": None, "symbol": None}
    return {
        "address": _text(attributes.get("address")) or None,
        "symbol": _text(attributes.get("symbol")) or None,
    }


def _dex_name(pool, index):
    ref = _relation(pool, "dex")
    if ref is None:
        return None
    item = index.get((ref.get("type") or "dex", ref.get("id")))
    if isinstance(item, dict) and isinstance(item.get("attributes"), dict):
        name = _text(item["attributes"].get("name"))
        if name:
            return name
    return _text(ref.get("id")) or None


def _text(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _optional_float(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None
