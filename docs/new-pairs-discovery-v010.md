# Crypto Radar v0.10 — новые DEX pools

Ветка разработки: `cursor/new-pairs-discovery-v010`.

База кода — production v0.9.2, commit `dfe0d29` (`feature/signal-monitoring-v092`). На `main` этого коммита ещё нет: `main` остаётся на `d9dedfb`, а v0.9.2 — его прямой потомок.

Scoring, порог 70, Paper Engine, Human Approval, выход из позиции и legacy scanner не менялись. Реальных заявок и приватных ключей нет. Идея после сохранения идёт в обычные `pairs` и `watchlist`.

## Что делает запуск

`python new_pairs_discovery.py` читает новые pools GeckoTerminal, оставляет сети с проверенным mapping и quote USDC, USDT или DAI, подтверждает пару в DexScreener и считает текущий score. За один запуск сохраняется не больше 3 новых идей.

`python new_pairs_discovery.py --dry-run` проходит ту же воронку и ничего не пишет в `pairs`, `watchlist`, `discovery_candidates` и `monitor_job_runs`.

## GeckoTerminal

Базовый URL: `https://api.geckoterminal.com/api/v2`.

Рабочий запуск ходит только в network-specific endpoint:

`GET /networks/{network}/new_pools?page=1&include=base_token,quote_token,dex`

Глобальный `GET /networks/new_pools` в адаптере есть, но в production-запуск не входит: активная сеть иначе вытесняет остальные. Список сетей читается через `GET /networks?page=N`. На 429 повторных запросов нет, в сводке стоит `rate_limited`.

## Проверенный mapping

Один и тот же pool address найден в GeckoTerminal и DexScreener 2026-10-09:

| GeckoTerminal | DexScreener chainId | Сравнение адреса |
| --- | --- | --- |
| solana | solana | как есть |
| eth | ethereum | EVM, без учёта регистра |
| arbitrum | arbitrum | EVM, без учёта регистра |
| base | base | EVM, без учёта регистра |
| arc | arc | EVM, без учёта регистра |

`arc` записан только после этой проверки. Остальные сети получают `unsupported_network`.

## Таблица discovery_candidates

Создаётся через `CREATE TABLE IF NOT EXISTS`. Уникальность: `source + source_network + pool_address`. Повторный pool обновляет `last_seen_at`, `seen_count` и последнее решение, а не создаёт вторую строку.

Поля: `id`, `source`, `source_network`, `pool_address`, `chain_id`, `pair_address`, `base_token_address`, `quote_token_address`, `base_symbol`, `quote_symbol`, `pool_created_at`, `first_seen_at`, `last_seen_at`, `seen_count`, `status`, `reason`, `risk_score`, `potential_score`, `final_score`, `pair_id`.

`pool_created_at` хранится для наблюдения. Возраст pool торговлю не фильтрует.

Источники: `geckoterminal_new_pools` и, для идей старого сканера, `dexscreener_legacy_search`. Колонка в `pairs` не добавлялась.

Обычный запуск пишет строку в `monitor_job_runs` с `job_name = new_pairs_discovery`. Оповещения исправности v0.9.2 не расширялись.

## Установка таймера

Команды ниже — для отдельного controlled deploy. На этапе разработки их не выполнять: unit-файлы не копировать в `/etc/systemd/system` и `systemctl` не запускать.

```bash
cd /opt/crypto_radar
sudo -u root cp crypto_radar.db "crypto_radar.db.bak-$(date -u +%Y%m%dT%H%M%SZ)"
./venv/bin/python -m unittest discover -s tests
./venv/bin/python new_pairs_discovery.py --dry-run
sudo cp deploy/systemd/crypto-radar-discovery.service /etc/systemd/system/
sudo cp deploy/systemd/crypto-radar-discovery.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-radar-discovery.timer
```

`crypto-auto-scan.timer` остаётся прежним и по-прежнему запускается примерно раз в 6 часов. `crypto-radar-check.timer` и Telegram-бот этот комплект не перезапускает.
