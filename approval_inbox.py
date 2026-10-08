"""
Исполнимые заявки Human Approval.

python approval_inbox.py
python approval_inbox.py --db crypto_radar.db
"""

import argparse
import sys

import database
from human_approval import list_actionable
from paper_engine import utc_now


def configure_console():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def render_inbox(rows):
    print("HUMAN APPROVAL — INBOX")
    print("======================")
    print("Только PENDING, baseline OPEN, окно ещё не закончилось.")
    if not rows:
        print("Нет исполнимых заявок.")
        return
    for row in rows:
        print()
        print("Request:", row["id"])
        print("Pair:", row.get("pair_symbol") or row["pair_id"])
        print("Chain:", row.get("chain_id"))
        print("Address:", row.get("pair_address"))
        print("Score:", row.get("final_score"))
        print("Signal:", row.get("signal_type"))
        print("Cohort:", row.get("cohort"))
        print("Recommended USD:", row.get("recommended_usd"))
        print("Reference price:", row.get("reference_price"))
        print("Eligible until:", row.get("eligible_until"))


def main(argv=None):
    configure_console()
    parser = argparse.ArgumentParser(
        description="Показать исполнимые заявки Human Approval"
    )
    parser.add_argument("--db", dest="db_path", default=None)
    args = parser.parse_args(argv)
    rows = list_actionable(utc_now(), db_path=args.db_path or database.DB_NAME)
    render_inbox(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
