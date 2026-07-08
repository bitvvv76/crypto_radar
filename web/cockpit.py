import html
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse


DB_PATH = "crypto_radar.db"
HOST = "127.0.0.1"
PORT = 8080


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


def render_page(content: str) -> str:
    return f"""
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <title>Crypto Radar Cockpit v0.1</title>
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
        <h1>Crypto Radar Cockpit v0.1</h1>
        <div class="subtitle">Read-only панель / только просмотр данных</div>
    </header>
    <main>
        {content}
        <div class="note">
            v0.1: только чтение из SQLite. Без записи в БД, без OpenAI API, без автоторговли, без реальных денег.
        </div>
    </main>
</body>
</html>
"""


def render_dashboard(selected_idea: dict | None = None) -> str:
    stats = load_dashboard_stats()
    ideas = load_last_ideas(limit=20)

    cards_html = f"""
    <div class="cards">
        <div class="card">
            <div class="card-title">Всего идей / Total Ideas</div>
            <div class="card-value">{stats["total_ideas"]}</div>
        </div>
        <div class="card">
            <div class="card-title">Score ≥ 70</div>
            <div class="card-value">{stats["strong_ideas"]}</div>
        </div>
        <div class="card">
            <div class="card-title">Score &lt; 70</div>
            <div class="card-value">{stats["weak_ideas"]}</div>
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
                <td><a href="/?id={idea_id}">{idea_id}</a></td>
                <td>{pair}</td>
                <td>{html.escape(str(idea.get("chain_id") or "—"))}</td>
                <td>{html.escape(str(idea.get("dex_id") or "—"))}</td>
                <td>{format_number(idea.get("final_score"))}</td>
                <td>{format_number(idea.get("risk_score"))}</td>
                <td>{format_number(idea.get("liquidity_usd"))}</td>
                <td>{format_number(idea.get("volume_24h"))}</td>
                <td>{format_number(idea.get("price_change_24h"))}%</td>
                <td class="{status_class}">{status}</td>
            </tr>
            """
        )

    table_html = f"""
    <h2 class="section-title">Last Ideas / Последние идеи</h2>
    <div class="table-wrap">
    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Pair</th>
                <th>Chain</th>
                <th>DEX</th>
                <th>Final Score</th>
                <th>Risk Score</th>
                <th>Liquidity USD</th>
                <th>Volume 24h</th>
                <th>Change 24h</th>
                <th>Sensor Status</th>
            </tr>
        </thead>
        <tbody>
            {''.join(rows)}
        </tbody>
    </table>
    </div>
    """

    idea_card_html = ""

    if selected_idea:
        status = get_sensor_status(selected_idea)
        status_class = f"status-{status}"

        idea_card_html = f"""
        <h2 class="section-title">Idea Card / Карточка идеи</h2>
        <div class="card">
            <div><b>ID:</b> {selected_idea.get("id")}</div>
            <div><b>Pair:</b> {html.escape(str(selected_idea.get("pair_symbol") or "UNKNOWN"))}</div>
            <div><b>Chain:</b> {html.escape(str(selected_idea.get("chain_id") or "—"))}</div>
            <div><b>DEX:</b> {html.escape(str(selected_idea.get("dex_id") or "—"))}</div>
            <div><b>Final Score:</b> {format_number(selected_idea.get("final_score"))}</div>
            <div><b>Risk Score:</b> {format_number(selected_idea.get("risk_score"))}</div>
            <div><b>Liquidity USD:</b> {format_number(selected_idea.get("liquidity_usd"))}</div>
            <div><b>Volume 24h:</b> {format_number(selected_idea.get("volume_24h"))}</div>
            <div><b>Price Change 24h:</b> {format_number(selected_idea.get("price_change_24h"))}%</div>
            <div><b>Sensor Status:</b> <span class="{status_class}">{status}</span></div>
        </div>
        """

    return render_page(cards_html + idea_card_html + table_html)


class CockpitHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_url = urlparse(self.path)
        query = parse_qs(parsed_url.query)

        selected_idea = None

        if "id" in query:
            try:
                selected_id = int(query["id"][0])
                selected_idea = load_idea(selected_id)
            except (TypeError, ValueError):
                selected_idea = None

        content = render_dashboard(selected_idea=selected_idea)

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