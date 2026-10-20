"""
HTTP-адаптер CoinGecko Demo Onchain API.

Адаптер только читает new pools выбранной сети, разбирает JSON:API
и помечает, съел ли ответ месячный credit. Он не считает score,
не пишет pairs и не открывает позиции.

Production discovery ходит только в
GET /onchain/networks/{network}/new_pools.
Глобальный /onchain/networks/new_pools здесь намеренно отсутствует.

Ключ читается из COINGECKO_DEMO_API_KEY и уходит только заголовком
x-cg-demo-api-key. В query string, ответ и текст ошибки он не попадает.
"""

import os
import re

import requests

from discovery_store import SOURCE_COINGECKO


COINGECKO_DEMO_BASE_URL = "https://api.coingecko.com/api/v3"
COINGECKO_DEMO_API_KEY_ENV = "COINGECKO_DEMO_API_KEY"
COINGECKO_DEMO_API_KEY_HEADER = "x-cg-demo-api-key"
COINGECKO_MONTHLY_BUDGET = 9000
DEFAULT_INCLUDE = "base_token,quote_token,dex"
DEFAULT_TIMEOUT = 20
USER_AGENT = "crypto-radar-discovery/0.10.2"
_NETWORK_RE = re.compile(r"[a-z0-9_]{1,64}")


def demo_api_key():
    value = os.environ.get(COINGECKO_DEMO_API_KEY_ENV)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text


def monthly_budget_limit(override=None):
    if isinstance(override, int) and not isinstance(override, bool) and override >= 0:
        return override
    raw = os.environ.get("COINGECKO_MONTHLY_BUDGET")
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = int(raw.strip())
        except ValueError:
            parsed = None
        if isinstance(parsed, int) and parsed >= 0:
            return parsed
    return COINGECKO_MONTHLY_BUDGET


def new_pools_path(network):
    safe_network = _safe_network(network)
    if safe_network is None:
        return None
    return "/onchain/networks/{0}/new_pools".format(safe_network)


def fetch_network_new_pools(
    network,
    page=1,
    include=DEFAULT_INCLUDE,
    timeout=DEFAULT_TIMEOUT,
    getter=None,
):
    path = new_pools_path(network)
    if path is None:
        return _failure("invalid_network")
    key = demo_api_key()
    if key is None:
        return _failure("missing_api_key")

    url = COINGECKO_DEMO_BASE_URL + path
    params = {"page": page, "include": include}
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        COINGECKO_DEMO_API_KEY_HEADER: key,
    }
    try:
        response = _request(url, params, timeout, headers, getter)
    except requests.exceptions.Timeout:
        return _failure("timeout")
    except requests.exceptions.RequestException:
        return _failure("request_error")
    except Exception:
        return _failure("request_error")

    status_code = getattr(response, "status_code", None)
    billable = status_code == 200
    if status_code == 401:
        return _failure("auth_error", status_code, billable=False)
    if status_code == 429:
        return _failure("rate_limited", status_code, billable=False)
    if status_code == 403:
        return _failure("forbidden", status_code, billable=False)
    if isinstance(status_code, int) and status_code >= 500:
        return _failure("server_error", status_code, billable=False)
    if status_code != 200:
        return _failure("http_error", status_code, billable=False)

    try:
        payload = response.json()
    except ValueError:
        return _failure("malformed_json", status_code, billable=True)
    except Exception:
        return _failure("malformed_json", status_code, billable=True)

    if not isinstance(payload, dict):
        return _failure("malformed_json", status_code, billable=True)

    pools = payload.get("data", [])
    included = payload.get("included", [])
    if pools is None:
        pools = []
    if included is None:
        included = []
    if not isinstance(pools, list) or not isinstance(included, list):
        return _failure("malformed_json", status_code, billable=True)

    return {
        "ok": True,
        "pools": pools,
        "included": included,
        "error_kind": None,
        "status_code": status_code,
        "billable": True,
        "endpoint": path,
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

    record["pool_address"] = _text(attributes.get("address")) or None
    record["pool_created_at"] = _text(attributes.get("pool_created_at")) or None

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


def _request(url, params, timeout, headers, getter):
    if getter is not None:
        return getter(url, params=params, timeout=timeout, headers=headers)
    return requests.get(
        url,
        params=params,
        timeout=timeout,
        headers=headers,
    )


def _safe_network(network):
    if not isinstance(network, str):
        return None
    if _NETWORK_RE.fullmatch(network) is None:
        return None
    return network


def _failure(kind, status_code=None, billable=False):
    return {
        "ok": False,
        "pools": [],
        "included": [],
        "error_kind": kind,
        "status_code": status_code,
        "billable": billable,
        "endpoint": None,
    }


def _blank_record(source_network):
    return {
        "ok": False,
        "reason": None,
        "source": SOURCE_COINGECKO,
        "source_network": _text(source_network) or None,
        "pool_address": None,
        "pool_created_at": None,
        "dex": None,
        "base_token_address": None,
        "base_symbol": None,
        "quote_token_address": None,
        "quote_symbol": None,
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
    if value is None or not isinstance(value, str):
        return ""
    return value.strip()
