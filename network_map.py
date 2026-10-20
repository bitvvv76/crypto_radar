"""
Единый mapping network id → DexScreener chainId.

Имена сетей не считаются одинаковыми, пока один и тот же pool address
не найден в обоих API. Проверка 2026-10-09:

- solana → solana, pool DOOBIE/USDC
- eth → ethereum, pool BTR/USDT
- arbitrum → arbitrum, pool NEOS/USDT
- base → base, pool DOTF/WETH
- arc → arc, pool COLDROOK/USDC

Сети вне этого списка получают статус unsupported_network.

CoinGecko authenticated discovery v0.10.2 берёт сети только из
COINGECKO_DISCOVERY_NETWORKS. Solana в этом списке нет: её new-pools
поток слишком частый для Demo budget. Solana остаётся в MAPPINGS,
legacy scanner, price checks, Paper Engine и Human Approval.
"""

from datetime import timedelta


MAPPINGS = (
    {"gecko_network": "solana", "chain_id": "solana", "evm": False},
    {"gecko_network": "eth", "chain_id": "ethereum", "evm": True},
    {"gecko_network": "arbitrum", "chain_id": "arbitrum", "evm": True},
    {"gecko_network": "base", "chain_id": "base", "evm": True},
    {"gecko_network": "arc", "chain_id": "arc", "evm": True},
)

_BY_NETWORK = {
    item["gecko_network"]: item
    for item in MAPPINGS
}

_EVM_CHAIN_IDS = {
    item["chain_id"]
    for item in MAPPINGS
    if item["evm"]
}


def mapping_for_network(network):
    if not isinstance(network, str):
        return None
    return _BY_NETWORK.get(network)


def chain_is_evm(chain_id):
    return chain_id in _EVM_CHAIN_IDS


def discovery_networks():
    return [item["gecko_network"] for item in MAPPINGS]


# Единственный список сетей authenticated discovery. Интервал — минуты
# с последнего complete coverage. Arbitrum реже, потому что page 1
# покрывает больше двух суток.
COINGECKO_DISCOVERY_NETWORKS = (
    {"network": "eth", "interval_minutes": 20},
    {"network": "base", "interval_minutes": 20},
    {"network": "arc", "interval_minutes": 20},
    {"network": "arbitrum", "interval_minutes": 60},
)

_AUTH_BY_NETWORK = {
    item["network"]: item
    for item in COINGECKO_DISCOVERY_NETWORKS
}


def coingecko_discovery_network_ids():
    return [item["network"] for item in COINGECKO_DISCOVERY_NETWORKS]


def coingecko_discovery_interval(network):
    item = _AUTH_BY_NETWORK.get(network)
    if item is None:
        return None
    return timedelta(minutes=item["interval_minutes"])
