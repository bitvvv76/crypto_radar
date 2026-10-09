"""
Проверенные адреса quote-токенов для new-pool discovery.

Политика прежняя: только USDC, USDT и DAI. Совпадения символа недостаточно.
Адрес должен быть каноническим контрактом этого токена в этой сети.
Сверка 2026-10-09. Если издатель не публикует адрес для пары token/network,
адрес не угадывается и пара не принимается.

USDC, таблица Circle mainnet
https://developers.circle.com/stablecoins/usdc-contract-addresses
- ethereum 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48
- solana EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
- arbitrum native 0xaf88d065e77c8cC2239327C5EDb3A432268e5831
  bridged USDC.e 0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8 не используется
- base 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
- arc 0x3600000000000000000000000000000000000000

USDT, Tether supported protocols
https://tether.to/en/supported-protocols
- ethereum 0xdAC17F958D2ee523a2206206994597C13D831ec7
- solana Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB
Arbitrum, Base и Arc на этой странице нет, поэтому USDT там не принимается.

DAI, mainnet deployments Sky/Maker Arbitrum DAI bridge
https://github.com/sky-ecosystem/arbitrum-dai-bridge
- ethereum l1Dai 0x6B175474E89094C44Da98b954EedeAC495271d0F
- arbitrum l2Dai 0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1
Base, Solana и Arc в этом deployment нет, поэтому DAI там не принимается.
"""


UNVERIFIED_QUOTE_TOKEN = "unverified_quote_token"

_CANONICAL = {
    "ethereum": {
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    },
    "solana": {
        "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    },
    "arbitrum": {
        "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "DAI": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
    },
    "base": {
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
    "arc": {
        "USDC": "0x3600000000000000000000000000000000000000",
    },
}

_CHAIN_KEYS = {
    "eth": "ethereum",
    "ethereum": "ethereum",
    "solana": "solana",
    "arbitrum": "arbitrum",
    "base": "base",
    "arc": "arc",
}

_EVM_CHAINS = {"ethereum", "arbitrum", "base", "arc"}


def canonical_quote_address(network, symbol):
    chain = _CHAIN_KEYS.get(network)
    if chain is None or not isinstance(symbol, str):
        return None
    return _CANONICAL.get(chain, {}).get(symbol.upper())


def quote_token_reason(network, symbol, address):
    """
    None, если quote разрешён и адрес доказан.
    unverified_quote_token, если символ разрешён, но адрес для этой сети нет.
    Для символа вне USDC/USDT/DAI возвращает None: это проверяет другая политика.
    """
    if not isinstance(symbol, str):
        return UNVERIFIED_QUOTE_TOKEN
    token = symbol.upper()
    if token not in {"USDC", "USDT", "DAI"}:
        return None
    canonical = canonical_quote_address(network, token)
    if canonical is None or not isinstance(address, str) or not address.strip():
        return UNVERIFIED_QUOTE_TOKEN
    if _addresses_match(network, address.strip(), canonical):
        return None
    return UNVERIFIED_QUOTE_TOKEN


def _addresses_match(network, left, right):
    chain = _CHAIN_KEYS.get(network)
    if chain in _EVM_CHAINS:
        return left.lower() == right.lower()
    return left == right
