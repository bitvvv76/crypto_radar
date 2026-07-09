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
        "note": "v0.1: read-only SQLite mode. No DB writes, no OpenAI API, no autotrading, no real money.",
        "ok": "ok",
        "warning": "warning",
        "reject": "reject",
    },
}


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
    stats = load_dashboard_stats()
    checks_stats = load_checks_stats()
    latest_checks = load_latest_checks(limit=10)
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

    idea_card_html = ""

    if selected_idea:
        status = get_sensor_status(selected_idea)
        status_class = f"status-{status}"

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
        </div>
        """

    return render_page(
        cards_html
        + checks_cards_html
        + checks_by_period_html
        + latest_checks_html
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