# Crypto Radar v0.10.2 — CoinGecko authenticated discovery

Ветка: `cursor/coingecko-auth-discovery-v0102`.

База: `main` `9a54c8c` (v0.10). Ветка v0.10.1 не использовалась.

Публичный GeckoTerminal остаётся в `gecko_discovery.py` как deprecated adapter и **не является production-источником**. `python new_pairs_discovery.py` ходит в CoinGecko Demo Onchain API.

Scoring, порог 70, лимит 3 идей, Paper Engine, Human Approval, выходы, legacy `auto_scan.py` и Solana вне нового discovery не менялись. Реальных заявок нет.

## Источник

`coingecko_authenticated_new_pools`

```
GET https://api.coingecko.com/api/v3/onchain/networks/{network}/new_pools
    ?page=N&include=base_token,quote_token,dex
Header: x-cg-demo-api-key
```

Ключ только из `COINGECKO_DEMO_API_KEY`. В git, fixture, query string и логи он не пишется. Если ключа нет, запуск сообщает `missing_api_key` и не переключается на публичный GeckoTerminal.

Глобальный `/onchain/networks/new_pools` production runner не вызывает.

Сети задаются одним списком `COINGECKO_DISCOVERY_NETWORKS`:

| Сеть | Интервал после complete coverage |
| --- | --- |
| eth | 20 минут |
| base | 20 минут |
| arc | 20 минут |
| arbitrum | 60 минут |

Solana в этот список не входит. Она по-прежнему доступна через `dexscreener_legacy_search`, проверки цены, Paper Engine и Human Approval.

## Пагинация, cutoff, fairness

На каждую due-сеть не больше `MAX_PAGES_PER_NETWORK = 3` страниц, до 20 pools на страницу. Сначала page 1 всех due-сетей, затем page 2, затем page 3.

`coverage_complete` сети — только `cutoff` или пустая страница. `max_pages`, 429, timeout, HTTP error, битый JSON, отсутствие timestamps и остановка по бюджету оставляют покрытие неполным и не двигают watermark.

Первый cutoff сети фиксируется в `discovery_network_state.initial_target_cutoff` (момент старта минус 30 минут) и повторяется, пока не будет первого complete. Дальше cutoff = `last_complete_reference` минус overlap 15 минут. Incomplete run reference не обновляет.

Dry-run эти поля не пишет.

## Бюджет

`COINGECKO_MONTHLY_BUDGET = 9000`. Успешный HTTP 200 discovery пишется в `discovery_api_usage` с `billable = 1`. Не-200 в месячный счёт не входят. На границе бюджета следующий запрос не выполняется (`monthly_budget_exhausted`), покрытие остаётся неполным, watermark не двигается. Счёт обнуляется по календарному месяцу UTC. Dry-run строки usage не пишет.

429 останавливает оставшиеся запросы этого запуска. Повторов нет.

## Дальше по воронке

Нормализация берёт pool address, `pool_created_at`, dex, base и quote из `relationships` и `included`. Quote по-прежнему только канонические USDC, USDT и DAI. Дальше точный DexScreener pair lookup, текущие `filter_valid_pairs` и scoring. Идеи со score >= 70, не больше 3 за запуск, идут в существующие `pairs` и `watchlist`.

## Запуск

```bash
python new_pairs_discovery.py --dry-run
python new_pairs_discovery.py
```

`--dry-run` может вызвать CoinGecko, если ключ задан, но не пишет `pairs`, `watchlist`, `discovery_candidates`, `monitor_job_runs`, watermark и API usage.

Таймер в `deploy/systemd/crypto-radar-discovery.timer` — шаблон на 20 минут. Arbitrum дополнительно режется логикой 60 минут. Unit читает `/opt/crypto_radar/.env`. На этом шаге timer не устанавливается и `systemctl` не запускается.

`crypto-auto-scan.timer` не менялся.
