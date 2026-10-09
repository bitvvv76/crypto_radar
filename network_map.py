"""
Единый mapping GeckoTerminal network id → DexScreener chainId.

Имена сетей не считаются одинаковыми, пока один и тот же pool address
не найден в обоих API. Проверка 2026-10-09:

- solana → solana, pool DOOBIE/USDC
- eth → ethereum, pool BTR/USDT
- arbitrum → arbitrum, pool NEOS/USDT
- base → base, pool DOTF/WETH
- arc → arc, pool COLDROOK/USDC

Сети вне этого списка получают статус unsupported_network.
"""


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
