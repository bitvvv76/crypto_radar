import html
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


DB_PATH = "crypto_radar.db"
HOST = "127.0.0.1"
PORT = 8080

TEXTS = {
    "ru": {
        "title": "Crypto Radar Cockpit v0.1",
        "subtitle": "Read-only панель / только просмотр данных",
        "language_ru": "Русский",
        "language_en": "English",
        "total_ideas": "Всего идей",
        "strong_ideas": "Score ≥ 70",
        "weak_ideas": "Score < 70",
        "idea_card": "Карточка идеи",
        "paper_portfolio": "Paper Portfolio v0.1",
        "paper_preview": "Модельный портфель / Preview",
        "paper_entry_price": "Условная цена входа",
        "paper_latest_price": "Последняя доступная цена",
        "paper_result": "Модельный результат",
        "paper_status": "Статус позиции",
        "paper_status_preview": "Preview only / без реальной сделки",
        "paper_note": "Это модельный read-only расчёт без реальных денег, без биржевых API и без записи в БД.",
        "paper_no_checks": "Для этой идеи пока нет проверок цены.",
        "ai_dossier": "AI Investment Dossier v0.1",
        "dossier_summary": "Краткое досье",
        "dossier_signal": "Сигнал",
        "dossier_risk": "Риск",
        "dossier_liquidity": "Ликвидность",
        "dossier_activity": "Активность",
        "dossier_note": "Это read-only досье на базе уже имеющихся данных Cockpit. OpenAI API не используется.",
        "dossier_signal_strong": "Идея выглядит сильной по итоговой оценке.",
        "dossier_signal_medium": "Идея выглядит умеренной, требуется ручная проверка.",
        "dossier_signal_weak": "Идея слабая или требует осторожности.",
        "dossier_risk_low": "Риск по базовым полям выглядит низким.",
        "dossier_risk_medium": "Есть признаки повышенного риска.",
        "dossier_risk_high": "Риск высокий, нужна осторожность.",
        "dossier_liquidity_good": "Ликвидность выглядит достаточной для наблюдения.",
        "dossier_liquidity_weak": "Ликвидность слабая, возможны проскальзывание и шум.",
        "dossier_activity_good": "Объём торгов выглядит активным.",
        "dossier_activity_weak": "Объём торгов слабый, сигнал менее надёжен.",
        "last_ideas": "Последние идеи",
        "id": "ID",
        "pair": "Пара",
        "chain": "Сеть",
        "dex": "DEX",
        "final_score": "Итоговая оценка",
        "risk_score": "Риск",
        "liquidity_usd": "Ликвидность USD",
        "volume_24h": "Объём 24ч",
        "change_24h": "Изменение 24ч",
        "sensor_status": "Статус сенсора",
        "checks_statistics": "Проверки / Статистика",
        "total_checks": "Всего проверок",
        "positive_checks": "Положительные",
        "negative_checks": "Отрицательные",
        "avg_change": "Среднее изменение",
        "best_change": "Лучший результат",
        "worst_change": "Худший результат",
        "checks_by_period": "Проверки по периодам",
        "latest_checks": "Последние проверки",
        "period": "Период",
        "count": "Количество",
        "old_price": "Старая цена",
        "new_price": "Новая цена",
        "change_percent": "Изменение %",
        "checked_at": "Время проверки",
        "data_source": "Источник данных",
        "source_type": "Тип источника",
        "database_path": "Путь к базе",
        "last_idea": "Последняя идея",
        "last_check": "Последняя проверка",
        "freshness_warning": "Внимание: Cockpit читает локальную SQLite-базу. Данные могут отличаться от боевой VPS-базы.",
        "freshness_note": "Старые даты в локальном Cockpit не являются ошибкой, если локальная база не синхронизирована с VPS.",
        "watchlist": "Watchlist / Список наблюдения",
        "watchlist_unavailable": "Источник watchlist не найден в локальной БД",
        "watchlist_unavailable_note": "Это нормально для локальной разработки. На VPS/боевой базе watchlist может существовать.",
        "watchlist_total": "Всего в watchlist",
        "watchlist_watching": "Наблюдаем",
        "watchlist_confirmed": "Подтверждено",
        "watchlist_rejected": "Отклонено",
        "watchlist_archived": "Архив",
        "watchlist_data_missing": "Нет свежих данных",
        "latest_watchlist": "Последние элементы watchlist",
        "watchlist_id": "Watchlist ID",
        "note_column": "Заметка",
        "added_at": "Добавлена",
        "updated_at": "Обновлена",
        "note": "v0.1: только чтение из SQLite. Без записи в БД, без OpenAI API, без автоторговли, без реальных денег.",
        "ok": "норма",
        "warning": "предупреждение",
        "reject": "отказ",
    },
    "en": {
        "title": "Crypto Radar Cockpit v0.1",
        "subtitle": "Read-only panel / data view only",
        "language_ru": "Русский",
        "language_en": "English",
        "total_ideas": "Total Ideas",
        "strong_ideas": "Score ≥ 70",
        "weak_ideas": "Score < 70",
        "idea_card": "Idea Card",
        "paper_portfolio": "Paper Portfolio v0.1",
        "paper_preview": "Paper Portfolio Preview",
        "paper_entry_price": "Model Entry Price",
        "paper_latest_price": "Latest Available Price",
        "paper_result": "Model Result",
        "paper_status": "Position Status",
        "paper_status_preview": "Preview only / no real trade",
        "paper_note": "This is a read-only paper calculation with no real money, no exchange APIs, and no DB writes.",
        "paper_no_checks": "There are no price checks for this idea yet.",
        "ai_dossier": "AI Investment Dossier v0.1",
        "dossier_summary": "Brief Dossier",
        "dossier_signal": "Signal",
        "dossier_risk": "Risk",
        "dossier_liquidity": "Liquidity",
        "dossier_activity": "Activity",
        "dossier_note": "This is a read-only dossier based on existing Cockpit data. OpenAI API is not used.",
        "dossier_signal_strong": "The idea looks strong based on the final score.",
        "dossier_signal_medium": "The idea looks moderate and needs manual review.",
        "dossier_signal_weak": "The idea is weak or requires caution.",
        "dossier_risk_low": "Risk looks low based on basic fields.",
        "dossier_risk_medium": "There are signs of elevated risk.",
        "dossier_risk_high": "Risk is high, caution is required.",
        "dossier_liquidity_good": "Liquidity looks sufficient for monitoring.",
        "dossier_liquidity_weak": "Liquidity is weak; slippage and noise are possible.",
        "dossier_activity_good": "Trading volume looks active.",
        "dossier_activity_weak": "Trading volume is weak, so the signal is less reliable.",
        "last_ideas": "Last Ideas",
        "id": "ID",
        "pair": "Pair",
        "chain": "Chain",
        "dex": "DEX",
        "final_score": "Final Score",
        "risk_score": "Risk Score",
        "liquidity_usd": "Liquidity USD",
        "volume_24h": "Volume 24h",
        "change_24h": "Change 24h",
        "sensor_status": "Sensor Status",
        "checks_statistics": "Checks / Statistics",
        "total_checks": "Total Checks",
        "positive_checks": "Positive",
        "negative_checks": "Negative",
        "avg_change": "Average Change",
        "best_change": "Best Change",
        "worst_change": "Worst Change",
        "checks_by_period": "Checks by Period",
        "latest_checks": "Latest Checks",
        "period": "Period",
        "count": "Count",
        "old_price": "Old Price",
        "new_price": "New Price",
        "change_percent": "Change %",
        "checked_at": "Checked At",
        "data_source": "Data Source",
        "source_type": "Source Type",
        "database_path": "Database Path",
        "last_idea": "Last Idea",
        "last_check": "Last Check",
        "freshness_warning": "Warning: Cockpit reads the local SQLite database. Data may differ from the production VPS database.",
        "freshness_note": "Old dates in the local Cockpit are not an error if the local database is not synchronized with VPS.",
        "watchlist": "Watchlist",
        "watchlist_unavailable": "Watchlist source not found in local DB",
        "watchlist_unavailable_note": "This is normal for local development. The VPS/production DB may contain watchlist data.",
        "watchlist_total": "Total Watchlist",
        "watchlist_watching": "Watching",
        "watchlist_confirmed": "Confirmed",
        "watchlist_rejected": "Rejected",
        "watchlist_archived": "Archived",
        "watchlist_data_missing": "Data Missing",
        "latest_watchlist": "Latest Watchlist Items",
        "watchlist_id": "Watchlist ID",
        "note_column": "Note",
        "added_at": "Added At",
        "updated_at": "Updated At",
        "note": "v0.1: read-only SQLite mode. No DB writes, no OpenAI API, no autotrading, no real money.",
        "ok": "ok",
        "warning": "warning",
        "reject": "reject",
    },
}

def load_data_freshness() -> dict:
    """
    Загружает read-only информацию об источнике и свежести данных.

    Только чтение.
    Без записи в БД.
    Без изменения структуры БД.
    """

    conn = sqlite3.connect(DB_PATH)

    try:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM pairs")
        total_pairs = cur.fetchone()[0]

        cur.execute("SELECT MAX(created_at) FROM pairs")
        last_pair_created_at = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM price_checks")
        total_checks = cur.fetchone()[0]

        cur.execute("SELECT MAX(checked_at) FROM price_checks")
        last_check_checked_at = cur.fetchone()[0]

        return {
            "data_source": "Local SQLite",
            "database_path": DB_PATH,
            "total_pairs": total_pairs,
            "last_pair_created_at": last_pair_created_at,
            "total_checks": total_checks,
            "last_check_checked_at": last_check_checked_at,
            "is_local": True,
        }
    finally:
        conn.close()


def load_dashboard_stats() -> dict:
    """
    Загружает базовую сводку для Cockpit v0.1.

    Только чтение.
    Без записи в БД.
    Без изменения структуры БД.
    """

    conn = sqlite3.connect(DB_PATH)

    try:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM pairs")
        total_ideas = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM pairs WHERE final_score >= 70")
        strong_ideas = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM pairs WHERE final_score < 70")
        weak_ideas = cur.fetchone()[0]

        return {
            "total_ideas": total_ideas,
            "strong_ideas": strong_ideas,
            "weak_ideas": weak_ideas,
        }
    finally:
        conn.close()


def load_last_ideas(limit: int = 20) -> list[dict]:
    """
    Загружает последние идеи для списка Last Ideas.

    Только чтение.
    """

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                id,
                pair_symbol,
                chain_id,
                dex_id,
                final_score,
                risk_score,
                liquidity_usd,
                volume_24h,
                price_change_24h
            FROM pairs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )

        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def load_idea(idea_id: int) -> dict:
    """
    Загружает одну идею для карточки Idea Card.

    Только чтение.
    """

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                id,
                pair_symbol,
                chain_id,
                dex_id,
                final_score,
                risk_score,
                liquidity_usd,
                volume_24h,
                price_change_24h
            FROM pairs
            WHERE id = ?
            LIMIT 1
            """,
            (idea_id,),
        )

        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()

def load_checks_stats() -> dict:
    """
    Загружает read-only статистику проверок price_checks.

    Только чтение.
    Без записи в БД.
    Без изменения структуры БД.
    """

    conn = sqlite3.connect(DB_PATH)

    try:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM price_checks")
        total_checks = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*)
            FROM price_checks
            WHERE price_change_percent > 0
        """)
        positive_checks = cur.fetchone()[0]

        cur.execute("""
            SELECT COUNT(*)
            FROM price_checks
            WHERE price_change_percent < 0
        """)
        negative_checks = cur.fetchone()[0]

        cur.execute("""
            SELECT AVG(price_change_percent)
            FROM price_checks
            WHERE price_change_percent IS NOT NULL
        """)
        avg_change = cur.fetchone()[0]

        cur.execute("""
            SELECT MAX(price_change_percent)
            FROM price_checks
            WHERE price_change_percent IS NOT NULL
        """)
        best_change = cur.fetchone()[0]

        cur.execute("""
            SELECT MIN(price_change_percent)
            FROM price_checks
            WHERE price_change_percent IS NOT NULL
        """)
        worst_change = cur.fetchone()[0]

        cur.execute("""
            SELECT check_period, COUNT(*)
            FROM price_checks
            GROUP BY check_period
            ORDER BY check_period
        """)
        by_period = cur.fetchall()

        return {
            "total_checks": total_checks,
            "positive_checks": positive_checks,
            "negative_checks": negative_checks,
            "avg_change": avg_change,
            "best_change": best_change,
            "worst_change": worst_change,
            "by_period": by_period,
        }
    finally:
        conn.close()


def load_latest_checks(limit: int = 10) -> list[dict]:
    """
    Загружает последние проверки для Cockpit.

    Только чтение.
    """

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                pc.id,
                pc.pair_id,
                p.pair_symbol,
                pc.check_period,
                pc.old_price_usd,
                pc.new_price_usd,
                pc.price_change_percent,
                pc.checked_at
            FROM price_checks pc
            LEFT JOIN pairs p ON p.id = pc.pair_id
            ORDER BY pc.id DESC
            LIMIT ?
            """,
            (limit,),
        )

        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()

def load_latest_check_for_idea(pair_id: int) -> dict:
    """
    Загружает последнюю проверку цены для конкретной идеи.

    Только чтение.
    Без записи в БД.
    Без изменения структуры БД.
    """

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                id,
                pair_id,
                check_period,
                old_price_usd,
                new_price_usd,
                price_change_percent,
                checked_at
            FROM price_checks
            WHERE pair_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (pair_id,),
        )

        row = cur.fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()

def table_exists(table_name: str) -> bool:
    """
    Проверяет наличие таблицы в локальной SQLite-базе.

    Только чтение.
    """

    conn = sqlite3.connect(DB_PATH)

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name = ?
            LIMIT 1
            """,
            (table_name,),
        )

        return cur.fetchone() is not None
    finally:
        conn.close()


def load_watchlist_stats() -> dict:
    """
    Загружает read-only статистику watchlist.

    Если таблицы watchlist нет в локальной БД,
    возвращает статус unavailable.
    """

    if not table_exists("watchlist"):
        return {
            "available": False,
            "total": 0,
            "watching": 0,
            "confirmed": 0,
            "rejected": 0,
            "archived": 0,
            "data_missing": 0,
        }

    conn = sqlite3.connect(DB_PATH)

    try:
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM watchlist")
        total = cur.fetchone()[0]

        status_counts = {
            "watching": 0,
            "confirmed": 0,
            "rejected": 0,
            "archived": 0,
            "data_missing": 0,
        }

        cur.execute("""
            SELECT status, COUNT(*)
            FROM watchlist
            GROUP BY status
        """)

        for status, count in cur.fetchall():
            if status in status_counts:
                status_counts[status] = count

        return {
            "available": True,
            "total": total,
            **status_counts,
        }
    finally:
        conn.close()


def load_watchlist_items(limit: int = 10) -> list[dict]:
    """
    Загружает последние элементы watchlist.

    Только чтение.
    Если таблицы watchlist нет, возвращает пустой список.
    """

    if not table_exists("watchlist"):
        return []

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                w.id AS watchlist_id,
                w.pair_id,
                p.pair_symbol,
                p.chain_id,
                p.dex_id,
                p.final_score,
                p.risk_score,
                w.status,
                w.note,
                w.added_at,
                w.updated_at
            FROM watchlist w
            LEFT JOIN pairs p ON p.id = w.pair_id
            ORDER BY w.id DESC
            LIMIT ?
            """,
            (limit,),
        )

        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def get_sensor_status(idea: dict) -> str:
    """
    Базовый статус сенсора для Cockpit v0.1.

    В v0.1 не дублируем всю логику sensor_report.
    Делаем простой read-only индикатор:
    - reject, если пара похожа на обратную или мусорную
    - warning, если есть перегрев или слабые базовые показатели
    - ok, если явных проблем нет
    """

    pair = str(idea.get("pair_symbol") or "").upper()
    final_score = idea.get("final_score") or 0
    liquidity_usd = idea.get("liquidity_usd") or 0
    volume_24h = idea.get("volume_24h") or 0
    price_change_24h = idea.get("price_change_24h") or 0

    bad_prefixes = ("WETH/", "WBTC/", "USDC/", "USDT/", "DAI/")

    if pair.startswith(bad_prefixes):
        return "reject"

    if final_score < 70:
        return "reject"

    if liquidity_usd < 50_000:
        return "reject"

    if volume_24h < 10_000:
        return "warning"

    if price_change_24h > 30:
        return "warning"

    return "ok"


def format_number(value) -> str:
    if value is None:
        return "—"

    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return html.escape(str(value))
    
def normalize_lang(value: str | None) -> str:
    if value == "en":
        return "en"

    return "ru"
def build_paper_portfolio_preview(idea: dict, latest_check: dict, lang: str) -> dict:
    """
    Формирует первый read-only preview для Paper Portfolio v0.1.

    Без записи в БД.
    Без изменения структуры БД.
    Без биржевых API.
    Без реальных денег.
    """

    texts = TEXTS[lang]

    entry_price = idea.get("price_usd")
    latest_price = latest_check.get("new_price_usd")
    result_percent = latest_check.get("price_change_percent")

    if not latest_check:
        return {
            "available": False,
            "entry_price": entry_price,
            "latest_price": None,
            "result_percent": None,
            "status": texts["paper_status_preview"],
            "note": texts["paper_no_checks"],
        }

    return {
        "available": True,
        "entry_price": entry_price,
        "latest_price": latest_price,
        "result_percent": result_percent,
        "status": texts["paper_status_preview"],
        "note": texts["paper_note"],
    }

def build_ai_dossier(idea: dict, lang: str) -> dict:
    """
    Формирует первый read-only слой AI Investment Dossier v0.1.

    Без OpenAI API.
    Без записи в БД.
    Только на базе уже имеющихся полей Cockpit.
    """

    texts = TEXTS[lang]

    final_score = idea.get("final_score") or 0
    risk_score = idea.get("risk_score") or 0
    liquidity_usd = idea.get("liquidity_usd") or 0
    volume_24h = idea.get("volume_24h") or 0

    if final_score >= 80:
        signal = texts["dossier_signal_strong"]
    elif final_score >= 70:
        signal = texts["dossier_signal_medium"]
    else:
        signal = texts["dossier_signal_weak"]

    if risk_score <= 30:
        risk = texts["dossier_risk_low"]
    elif risk_score <= 60:
        risk = texts["dossier_risk_medium"]
    else:
        risk = texts["dossier_risk_high"]

    if liquidity_usd >= 100_000:
        liquidity = texts["dossier_liquidity_good"]
    else:
        liquidity = texts["dossier_liquidity_weak"]

    if volume_24h >= 50_000:
        activity = texts["dossier_activity_good"]
    else:
        activity = texts["dossier_activity_weak"]

    return {
        "signal": signal,
        "risk": risk,
        "liquidity": liquidity,
        "activity": activity,
    }


def status_text(status: str, lang: str) -> str:
    texts = TEXTS[lang]
    label = texts.get(status, status)

    if lang == "ru":
        return f"{label} / {status}"

    return status


def render_page(content: str, lang: str) -> str:
    texts = TEXTS[lang]

    return f"""
<!doctype html>
<html lang="{lang}">
<head>
    <meta charset="utf-8">
    <title>{texts["title"]}</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            background: #0f172a;
            color: #e5e7eb;
            margin: 0;
            padding: 0;
        }}
        header {{
            background: #111827;
            padding: 20px 32px;
            border-bottom: 1px solid #374151;
        }}
        h1 {{
            margin: 0;
            font-size: 26px;
        }}
        .subtitle {{
            color: #9ca3af;
            margin-top: 6px;
        }}
        .language-switch {{
            margin-top: 12px;
            font-size: 14px;
        }}
        .language-switch a {{
            margin-right: 12px;
        }}
        main {{
            padding: 24px 32px;
        }}
        .cards {{
            display: flex;
            gap: 16px;
            margin-bottom: 24px;
            flex-wrap: wrap;
        }}
        .card {{
            background: #111827;
            border: 1px solid #374151;
            border-radius: 12px;
            padding: 18px;
            min-width: 180px;
        }}
        .card-title {{
            color: #9ca3af;
            font-size: 14px;
        }}
        .card-value {{
            font-size: 28px;
            font-weight: bold;
            margin-top: 8px;
        }}
        .table-wrap {{
            width: 100%;
            overflow-x: auto;
            border-radius: 12px;
        }}
        table {{
            width: 100%;
            min-width: 1200px;
            border-collapse: collapse;
            background: #111827;
            border: 1px solid #374151;
            border-radius: 12px;
            overflow: hidden;
        }}
        th, td {{
            padding: 10px 12px;
            border-bottom: 1px solid #374151;
            text-align: left;
            font-size: 14px;
        }}
        th {{
            background: #1f2937;
            color: #d1d5db;
        }}
        a {{
            color: #93c5fd;
            text-decoration: none;
        }}
        .status-ok {{
            color: #86efac;
            font-weight: bold;
        }}
        .status-warning {{
            color: #fde68a;
            font-weight: bold;
        }}
        .status-reject {{
            color: #fca5a5;
            font-weight: bold;
        }}
        .section-title {{
            margin: 26px 0 12px;
            font-size: 20px;
        }}
        .note {{
            color: #9ca3af;
            font-size: 13px;
            margin-top: 18px;
        }}
    </style>
</head>
<body>
    <header>
        <h1>{texts["title"]}</h1>
        <div class="subtitle">{texts["subtitle"]}</div>
        <div class="language-switch">
            <a href="/?lang=ru">{texts["language_ru"]}</a>
            <a href="/?lang=en">{texts["language_en"]}</a>
        </div>
    </header>
    <main>
        {content}
        <div class="note">
            {texts["note"]}
        </div>
    </main>
</body>
</html>
"""


def render_dashboard(selected_idea: dict | None = None, lang: str = "ru") -> str:
    texts = TEXTS[lang]
    freshness = load_data_freshness()
    stats = load_dashboard_stats()
    checks_stats = load_checks_stats()
    latest_checks = load_latest_checks(limit=10)
    watchlist_stats = load_watchlist_stats()
    watchlist_items = load_watchlist_items(limit=10)
    ideas = load_last_ideas(limit=20)
    cards_html = f"""
    <div class="cards">
        <div class="card">
            <div class="card-title">{texts["total_ideas"]}</div>
            <div class="card-value">{stats["total_ideas"]}</div>
        </div>
        <div class="card">
            <div class="card-title">{texts["strong_ideas"]}</div>
            <div class="card-value">{stats["strong_ideas"]}</div>
        </div>
        <div class="card">
            <div class="card-title">{texts["weak_ideas"]}</div>
            <div class="card-value">{stats["weak_ideas"]}</div>
        </div>
    </div>
    """
    freshness_html = f"""
    <h2 class="section-title">{texts["data_source"]}</h2>
    <div class="cards">
        <div class="card">
            <div class="card-title">{texts["source_type"]}</div>
            <div class="card-value">{html.escape(str(freshness["data_source"]))}</div>
        </div>
        <div class="card">
            <div class="card-title">{texts["database_path"]}</div>
            <div class="card-value">{html.escape(str(freshness["database_path"]))}</div>
        </div>
        <div class="card">
            <div class="card-title">{texts["last_idea"]}</div>
            <div class="card-value">{html.escape(str(freshness["last_pair_created_at"] or "—"))}</div>
        </div>
        <div class="card">
            <div class="card-title">{texts["last_check"]}</div>
            <div class="card-value">{html.escape(str(freshness["last_check_checked_at"] or "—"))}</div>
        </div>
    </div>
    <div class="card">
        <div class="card-title">{texts["freshness_warning"]}</div>
        <div class="note">{texts["freshness_note"]}</div>
    </div>
    """
    checks_cards_html = f"""
    <h2 class="section-title">{texts["checks_statistics"]}</h2>
    <div class="cards">
        <div class="card">
            <div class="card-title">{texts["total_checks"]}</div>
            <div class="card-value">{checks_stats["total_checks"]}</div>
        </div>
        <div class="card">
            <div class="card-title">{texts["positive_checks"]}</div>
            <div class="card-value">{checks_stats["positive_checks"]}</div>
        </div>
        <div class="card">
            <div class="card-title">{texts["negative_checks"]}</div>
            <div class="card-value">{checks_stats["negative_checks"]}</div>
        </div>
        <div class="card">
            <div class="card-title">{texts["avg_change"]}</div>
            <div class="card-value">{format_number(checks_stats["avg_change"])}%</div>
        </div>
        <div class="card">
            <div class="card-title">{texts["best_change"]}</div>
            <div class="card-value">{format_number(checks_stats["best_change"])}%</div>
        </div>
        <div class="card">
            <div class="card-title">{texts["worst_change"]}</div>
            <div class="card-value">{format_number(checks_stats["worst_change"])}%</div>
        </div>
    </div>
    """
    if watchlist_stats["available"]:
        watchlist_cards_html = f"""
        <h2 class="section-title">{texts["watchlist"]}</h2>
        <div class="cards">
            <div class="card">
                <div class="card-title">{texts["watchlist_total"]}</div>
                <div class="card-value">{watchlist_stats["total"]}</div>
            </div>
            <div class="card">
                <div class="card-title">{texts["watchlist_watching"]}</div>
                <div class="card-value">{watchlist_stats["watching"]}</div>
            </div>
            <div class="card">
                <div class="card-title">{texts["watchlist_confirmed"]}</div>
                <div class="card-value">{watchlist_stats["confirmed"]}</div>
            </div>
            <div class="card">
                <div class="card-title">{texts["watchlist_rejected"]}</div>
                <div class="card-value">{watchlist_stats["rejected"]}</div>
            </div>
            <div class="card">
                <div class="card-title">{texts["watchlist_archived"]}</div>
                <div class="card-value">{watchlist_stats["archived"]}</div>
            </div>
            <div class="card">
                <div class="card-title">{texts["watchlist_data_missing"]}</div>
                <div class="card-value">{watchlist_stats["data_missing"]}</div>
            </div>
        </div>
        """
    else:
        watchlist_cards_html = f"""
        <h2 class="section-title">{texts["watchlist"]}</h2>
        <div class="card">
            <div class="card-title">{texts["watchlist_unavailable"]}</div>
            <div class="note">{texts["watchlist_unavailable_note"]}</div>
        </div>
        """

    rows = []

    for idea in ideas:
        status = get_sensor_status(idea)
        status_class = f"status-{status}"
        idea_id = idea["id"]
        pair = html.escape(str(idea.get("pair_symbol") or "UNKNOWN"))

        rows.append(
            f"""
            <tr>
                <td><a href="/?id={idea_id}&lang={lang}">{idea_id}</a></td>
                <td>{pair}</td>
                <td>{html.escape(str(idea.get("chain_id") or "—"))}</td>
                <td>{html.escape(str(idea.get("dex_id") or "—"))}</td>
                <td>{format_number(idea.get("final_score"))}</td>
                <td>{format_number(idea.get("risk_score"))}</td>
                <td>{format_number(idea.get("liquidity_usd"))}</td>
                <td>{format_number(idea.get("volume_24h"))}</td>
                <td>{format_number(idea.get("price_change_24h"))}%</td>
                <td class="{status_class}">{status_text(status, lang)}</td>
            </tr>
            """
        )

    table_html = f"""
    <h2 class="section-title">{texts["last_ideas"]}</h2>
    <div class="table-wrap">
    <table>
        <thead>
            <tr>
                <th>{texts["id"]}</th>
                <th>{texts["pair"]}</th>
                <th>{texts["chain"]}</th>
                <th>{texts["dex"]}</th>
                <th>{texts["final_score"]}</th>
                <th>{texts["risk_score"]}</th>
                <th>{texts["liquidity_usd"]}</th>
                <th>{texts["volume_24h"]}</th>
                <th>{texts["change_24h"]}</th>
                <th>{texts["sensor_status"]}</th>
            </tr>
        </thead>
        <tbody>
            {''.join(rows)}
        </tbody>
    </table>
    </div>
    """

    period_rows = []

    for period, count in checks_stats["by_period"]:
        period_rows.append(
            f"""
            <tr>
                <td>{html.escape(str(period))}</td>
                <td>{count}</td>
            </tr>
            """
        )

    checks_by_period_html = f"""
    <h2 class="section-title">{texts["checks_by_period"]}</h2>
    <div class="table-wrap">
    <table>
        <thead>
            <tr>
                <th>{texts["period"]}</th>
                <th>{texts["count"]}</th>
            </tr>
        </thead>
        <tbody>
            {''.join(period_rows)}
        </tbody>
    </table>
    </div>
    """

    check_rows = []

    for check in latest_checks:
        check_rows.append(
            f"""
            <tr>
                <td>{check.get("id")}</td>
                <td>{html.escape(str(check.get("pair_symbol") or "UNKNOWN"))}</td>
                <td>{html.escape(str(check.get("check_period") or "—"))}</td>
                <td>{format_number(check.get("old_price_usd"))}</td>
                <td>{format_number(check.get("new_price_usd"))}</td>
                <td>{format_number(check.get("price_change_percent"))}%</td>
                <td>{html.escape(str(check.get("checked_at") or "—"))}</td>
            </tr>
            """
        )

    latest_checks_html = f"""
    <h2 class="section-title">{texts["latest_checks"]}</h2>
    <div class="table-wrap">
    <table>
        <thead>
            <tr>
                <th>{texts["id"]}</th>
                <th>{texts["pair"]}</th>
                <th>{texts["period"]}</th>
                <th>{texts["old_price"]}</th>
                <th>{texts["new_price"]}</th>
                <th>{texts["change_percent"]}</th>
                <th>{texts["checked_at"]}</th>
            </tr>
        </thead>
        <tbody>
            {''.join(check_rows)}
        </tbody>
    </table>
    </div>
    """

    watchlist_rows = []

    for item in watchlist_items:
        watchlist_rows.append(
            f"""
            <tr>
                <td>{item.get("watchlist_id") or ""}</td>
                <td>{item.get("pair_id") or ""}</td>
                <td>{html.escape(str(item.get("pair_symbol") or ""))}</td>
                <td>{html.escape(str(item.get("chain_id") or ""))}</td>
                <td>{html.escape(str(item.get("dex_id") or ""))}</td>
                <td>{format_number(item.get("final_score"))}</td>
                <td>{format_number(item.get("risk_score"))}</td>
                <td>{html.escape(str(item.get("status") or ""))}</td>
                <td>{html.escape(str(item.get("note") or ""))}</td>
                <td>{html.escape(str(item.get("added_at") or ""))}</td>
                <td>{html.escape(str(item.get("updated_at") or ""))}</td>
            </tr>
            """
        )

    if watchlist_stats["available"] and watchlist_rows:
        latest_watchlist_html = f"""
        <h2 class="section-title">{texts["latest_watchlist"]}</h2>
        <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>{texts["watchlist_id"]}</th>
                    <th>{texts["id"]}</th>
                    <th>{texts["pair"]}</th>
                    <th>{texts["chain"]}</th>
                    <th>{texts["dex"]}</th>
                    <th>{texts["final_score"]}</th>
                    <th>{texts["risk_score"]}</th>
                    <th>{texts["sensor_status"]}</th>
                    <th>{texts["note_column"]}</th>
                    <th>{texts["added_at"]}</th>
                    <th>{texts["updated_at"]}</th>
                </tr>
            </thead>
            <tbody>
                {"".join(watchlist_rows)}
            </tbody>
        </table>
        </div>
        """
    else:
        latest_watchlist_html = ""

    idea_card_html = ""

    if selected_idea:
        status = get_sensor_status(selected_idea)
        status_class = f"status-{status}"
        dossier = build_ai_dossier(selected_idea, lang)
        latest_check = load_latest_check_for_idea(selected_idea["id"])
        paper_preview = build_paper_portfolio_preview(
            selected_idea,
            latest_check,
            lang,
        )
        

        idea_card_html = f"""
        <h2 class="section-title">{texts["idea_card"]}</h2>
        <div class="card">
            <div><b>{texts["id"]}:</b> {selected_idea.get("id")}</div>
            <div><b>{texts["pair"]}:</b> {html.escape(str(selected_idea.get("pair_symbol") or "UNKNOWN"))}</div>
            <div><b>{texts["chain"]}:</b> {html.escape(str(selected_idea.get("chain_id") or "—"))}</div>
            <div><b>{texts["dex"]}:</b> {html.escape(str(selected_idea.get("dex_id") or "—"))}</div>
            <div><b>{texts["final_score"]}:</b> {format_number(selected_idea.get("final_score"))}</div>
            <div><b>{texts["risk_score"]}:</b> {format_number(selected_idea.get("risk_score"))}</div>
            <div><b>{texts["liquidity_usd"]}:</b> {format_number(selected_idea.get("liquidity_usd"))}</div>
            <div><b>{texts["volume_24h"]}:</b> {format_number(selected_idea.get("volume_24h"))}</div>
            <div><b>{texts["change_24h"]}:</b> {format_number(selected_idea.get("price_change_24h"))}%</div>
            <div><b>{texts["sensor_status"]}:</b> <span class="{status_class}">{status_text(status, lang)}</span></div>
            <hr>
            <h3>{texts["ai_dossier"]}</h3>
            <div><b>{texts["dossier_summary"]}:</b></div>
            <div><b>{texts["dossier_signal"]}:</b> {html.escape(dossier["signal"])}</div>
            <div><b>{texts["dossier_risk"]}:</b> {html.escape(dossier["risk"])}</div>
            <div><b>{texts["dossier_liquidity"]}:</b> {html.escape(dossier["liquidity"])}</div>
            <div><b>{texts["dossier_activity"]}:</b> {html.escape(dossier["activity"])}</div>
            <div class="note">{texts["dossier_note"]}</div>
            <hr>
            <h3>{texts["paper_portfolio"]}</h3>
            <div><b>{texts["paper_preview"]}:</b></div>
            <div><b>{texts["paper_entry_price"]}:</b> {format_number(paper_preview["entry_price"])}</div>
            <div><b>{texts["paper_latest_price"]}:</b> {format_number(paper_preview["latest_price"])}</div>
            <div><b>{texts["paper_result"]}:</b> {format_number(paper_preview["result_percent"])}%</div>
            <div><b>{texts["paper_status"]}:</b> {html.escape(str(paper_preview["status"]))}</div>
            <div class="note">{html.escape(str(paper_preview["note"]))}</div>
        </div>
        """

    return render_page(
        cards_html
        + freshness_html
        + checks_cards_html
        + watchlist_cards_html
        + checks_by_period_html
        + latest_checks_html
        + latest_watchlist_html
        + idea_card_html
        + table_html,
        lang=lang,
    )


class CockpitHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_url = urlparse(self.path)
        query = parse_qs(parsed_url.query)

        lang = normalize_lang(query.get("lang", ["ru"])[0])

        selected_idea = None

        if "id" in query:
            try:
                selected_id = int(query["id"][0])
                selected_idea = load_idea(selected_id)
            except (TypeError, ValueError):
                selected_idea = None

        content = render_dashboard(selected_idea=selected_idea, lang=lang)

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

def main():
    server = HTTPServer((HOST, PORT), CockpitHandler)

    print("Crypto Radar Cockpit v0.1")
    print(f"Open: http://{HOST}:{PORT}")
    print("Mode: read-only")
    print("Press Ctrl+C to stop")

    server.serve_forever()


if __name__ == "__main__":
    main()