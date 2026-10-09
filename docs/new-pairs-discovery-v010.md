# Crypto Radar v0.10 — новые DEX pools

Ветка разработки: `cursor/new-pairs-discovery-v010`.

База кода — production v0.9.2, commit `dfe0d29` (`feature/signal-monitoring-v092`). На `main` этого коммита ещё нет: `main` остаётся на `d9dedfb`, а v0.9.2 — его прямой потомок.

Scoring, порог 70, Paper Engine, Human Approval, выход из позиции и legacy scanner не менялись. Реальных заявок и приватных ключей нет. Идея после сохранения идёт в обычные `pairs` и `watchlist`.

## Что делает запуск

`python new_pairs_discovery.py` читает новые pools GeckoTerminal, оставляет сети с проверенным mapping и quote USDC, USDT или DAI с каноническим адресом этой сети, подтверждает пару в DexScreener и считает текущий score. За один запуск сохраняется не больше 3 новых идей.

`python new_pairs_discovery.py --dry-run` проходит ту же воронку и ничего не пишет в `pairs`, `watchlist`, `discovery_candidates` и `monitor_job_runs`.

## GeckoTerminal

Базовый URL: `https://api.geckoterminal.com/api/v2`.

Рабочий запуск ходит только в network-specific endpoint:

`GET /networks/{network}/new_pools?page=N&include=base_token,quote_token,dex`

`N` ограничен константой `MAX_PAGES_PER_NETWORK = 3`. Бесконечного обхода и исторического backfill нет. Увеличение лимита само по себе покрытие не закрывает.

Coverage сети полное только если пагинация дошла до cutoff или API вернула пустую страницу. `max_pages` до cutoff, 429, HTTP/network error и битая страница дают `coverage_complete = false` и сохраняют точный `stop_reason`. Если хотя бы одна сеть неполная, у запуска `coverage_complete = false` и статус `partial`.

Следующий cutoff сети считается от `started_at` последнего запуска, где у этой сети `coverage_complete = true`, минус overlap 15 минут. Неполный или `partial` запуск для этой сети reference не становится. Старый запуск без явного флага тоже не считается полным. Если полного покрытия ещё не было, cutoff — текущее время минус initial lookback 30 минут.

Страницы читаются, пока самый старый `pool_created_at` на странице новее cutoff. Страница, на которой возраст пересекает cutoff, входит в результат, следующая уже не запрашивается. Если на странице нет ни одного `pool_created_at`, это не считается достижением cutoff. Пустая страница останавливает только эту сеть и считается полным покрытием.

Повторно найденный pool съедается текущей идемпотентностью `discovery_candidates` и `save_pair`.

Запросы одного процесса проходят через общий limiter HTTP-адаптера (`GECKO_MIN_REQUEST_INTERVAL_SECONDS = 8`). Первый запрос паузу не ждёт, каждый следующий ждёт остаток интервала. Пауза не стоит в пагинации и не меняет cutoff, overlap и stop reason.

429 останавливает остальные страницы и остальные сети. Повторов нет. Timeout, 5xx и прочий HTTP-сбой останавливают только текущую сеть. Битая страница или исключение разбора не выбрасывают pools, уже собранные с предыдущих страниц, и не останавливают другие сети.

Глобальный `GET /networks/new_pools` в адаптере есть, но в production-запуск не входит: активная сеть иначе вытесняет остальные. Список сетей читается через `GET /networks?page=N`.

## Проверенные quote-адреса

Символ USDC, USDT или DAI сам по себе пару не пропускает. Адрес должен совпасть с каноническим контрактом этой сети. Списки лежат в одном модуле `stablecoins.py`. EVM сравнивается без учёта регистра, Solana — с учётом. Несовпадение даёт `unverified_quote_token`.

Адреса сверены 2026-10-09:

- USDC: Circle, [USDC contract addresses](https://developers.circle.com/stablecoins/usdc-contract-addresses). Ethereum, Solana, нативный Arbitrum (не bridged USDC.e), Base, Arc.
- USDT: [Tether supported protocols](https://tether.to/en/supported-protocols). Только Ethereum и Solana. Arbitrum, Base и Arc там не указаны и не принимаются.
- DAI: mainnet deployment [Sky/Maker Arbitrum DAI bridge](https://github.com/sky-ecosystem/arbitrum-dai-bridge). Ethereum `l1Dai` и Arbitrum `l2Dai`. Base, Solana и Arc не указаны и не принимаются.

Если канонический адрес для пары token/network не доказан, он не угадывается.

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

Запись источника `dexscreener_legacy_search` в legacy scanner — best-effort. Если она падает после успешного `save_pair`, пара и watchlist остаются, а запуск scanner не становится failure.

## Перед PR v0.10

Сейчас `main` = `d9dedfb`, production v0.9.2 = `dfe0d29`, а эта ветка основана на `dfe0d29`. Rebase и merge в этом изменении не делались. Перед будущим PR v0.10 нужно по порядку:

1. влить v0.9.2 в `main`;
2. обновить ветку v0.10 от нового `main`;
3. заново прогнать полный test suite;
4. только после этого открывать PR v0.10.
