# Crypto Radar v0.9.2 — наблюдение сигналов и ежедневный отчёт

Ветка: `feature/signal-monitoring-v092`.

Торговые правила, scoring, Stop-loss, Take-profit, trailing-stop и решения BUY/SKIP не менялись. Реальных биржевых операций нет. Исторические строки торговых таблиц эта версия не переписывает.

## Что появляется в работе

- `auto_check_all.py` в конце цикла печатает диагностику: новые события 24h, SQL-кандидаты, созданные baseline, причины отклонения, заявки Human Approval, доставленные и недоставленные Telegram-уведомления этих заявок. Отсутствие новых событий 24h и ошибка обработки — разные исходы.
- В paper engine по-прежнему попадают только id новых проверок 24h текущего цикла. Старая проверка 24h позицию не открывает.
- Работающий `approval_bot.py` тем же клиентом отправляет один отчёт за завершённые сутки UTC и оповещения исправности. Второй `getUpdates` не запускается.
- `python daily_report.py --dry-run` печатает отчёт локально. `python daily_report.py --send` вызывает только `sendMessage`.

Пороги контроля исправности:

- проверки цены считаются пропущенными, если последний `auto_check` старше 45 минут;
- сканер считается пропущенным, если последний `auto_scan` старше 26 часов;
- одинаковое состояние повторно в Telegram не отправляется.

Отсутствующий показатель в отчёте пишется как `н/д`. Ноль остаётся нулём только после реального запроса к существующей таблице.

## База данных

Новые таблицы, создаются через `CREATE TABLE IF NOT EXISTS`:

- `monitor_job_runs`
- `monitor_daily_reports`
- `monitor_health_state`

Индекс: `idx_monitor_job_runs_name_id`.

Таблицы `pairs`, `price_checks`, `watchlist`, `paper_positions`, `paper_price_marks`, `paper_account`, `paper_allocations`, `paper_cash_ledger`, `paper_nav_snapshots`, `approval_requests`, `approval_attempts`, `approval_notifications` не изменяются.

## Развёртывание на VPS

Команды ниже выполняет оператор. Этот комплект сам службы не перезапускает и unit-файлы не подменяет.

```bash
cd /opt/crypto_radar
sudo -u root cp crypto_radar.db "crypto_radar.db.bak-$(date -u +%Y%m%dT%H%M%SZ)"
git fetch origin feature/signal-monitoring-v092
git checkout feature/signal-monitoring-v092
./venv/bin/python -m unittest discover -s tests
./venv/bin/python daily_report.py --dry-run
sudo systemctl restart crypto-approval-bot
```

`crypto-radar-check.service`, `crypto-radar-check.timer`, `crypto-approval-bot.service` и служба резервного копирования остаются прежними. Следующий запуск таймера проверок уже запишет диагностику. Сканер записывает запуск, когда оператор запускает `auto_scan.py` тем же способом, что и раньше.

Повторный процесс `approval_bot.py` запускать не нужно: он снова займёт polling Telegram. Ручная отправка, если бот остановлен:

```bash
cd /opt/crypto_radar
./venv/bin/python daily_report.py --send
```

Токен остаётся в `/opt/crypto_radar/.env`. В журнал попадают статус и тип ошибки, не значение `TELEGRAM_BOT_TOKEN`.

Проверка после первого цикла бота: в чате одно сообщение отчёта за вчерашние сутки UTC. Повторный цикл то же сообщение не дублирует. Если Telegram отклонил отправку, следующая попытка повторяет тот же `report_date`.

## Откат

```bash
cd /opt/crypto_radar
git checkout d9dedfb
sudo systemctl restart crypto-approval-bot
```

Новые таблицы можно оставить: предыдущий код их не читает. Удаление только служебных таблиц, если они больше не нужны:

```sql
DROP TABLE IF EXISTS monitor_health_state;
DROP TABLE IF EXISTS monitor_daily_reports;
DROP TABLE IF EXISTS monitor_job_runs;
```

`crypto_radar.db.bak-*` возвращается на место только если оператор хочет вернуть и файл базы. Торговые строки этой версией не переписывались, поэтому откат кода их не требует.
