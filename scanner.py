import requests

from config import DEX_SCREENER_BASE_URL, MIN_LIQUIDITY_USD


_api_errors = []


def consume_api_errors():
    found = list(_api_errors)
    _api_errors.clear()
    return found


def _note_api_error(source, kind):
    _api_errors.append({
        "source": source,
        "kind": kind,
    })
    if len(_api_errors) > 50:
        del _api_errors[:-50]


def search_pairs(query):
    url = f"{DEX_SCREENER_BASE_URL}/search"

    params = {
        "q": query
    }

    try:
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()

        data = response.json()
        return data

    except requests.exceptions.Timeout:
        _note_api_error("search", "timeout")
        print("Ошибка: DEX Screener не ответил вовремя. Попробуйте запустить позже.")
        return {"pairs": []}

    except requests.exceptions.HTTPError:
        _note_api_error("search", "http_error")
        print("Ошибка запроса к DEX Screener: HTTP")
        return {"pairs": []}

    except requests.exceptions.RequestException:
        _note_api_error("search", "request_error")
        print("Ошибка запроса к DEX Screener")
        return {"pairs": []}

def filter_valid_pairs(pairs):
    valid_pairs = []

    for pair in pairs:
        price_usd = pair.get("priceUsd")
        liquidity_usd = pair.get("liquidity", {}).get("usd")

        if price_usd is None:
            continue

        if liquidity_usd is None:
            continue

        if liquidity_usd < MIN_LIQUIDITY_USD:
            continue

        valid_pairs.append(pair)

    return valid_pairs

def get_pair_by_address(chain_id, pair_address):
    url = f"{DEX_SCREENER_BASE_URL}/pairs/{chain_id}/{pair_address}"

    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()

        data = response.json()
        pairs = data.get("pairs", [])

        if not pairs:
            return None

        return pairs[0]

    except requests.exceptions.Timeout:
        _note_api_error("pair", "timeout")
        print("Ошибка: DEX Screener не ответил вовремя при получении пары.")
        return None

    except requests.exceptions.HTTPError:
        _note_api_error("pair", "http_error")
        print("Ошибка запроса пары к DEX Screener: HTTP")
        return None

    except requests.exceptions.RequestException:
        _note_api_error("pair", "request_error")
        print("Ошибка запроса пары к DEX Screener")
        return None