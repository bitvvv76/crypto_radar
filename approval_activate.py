"""
Ручная активация счёта Human Approval.

python approval_activate.py
python approval_activate.py --db crypto_radar.db

Cron этот счёт не создаёт.
"""

import argparse
import sys

import database
from human_approval import activate_approval_account


def configure_console():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def main(argv=None):
    configure_console()
    parser = argparse.ArgumentParser(
        description="Создать бумажный счёт Human Approval на 10 000 USD"
    )
    parser.add_argument("--db", dest="db_path", default=None)
    args = parser.parse_args(argv)
    result = activate_approval_account(db_path=args.db_path or database.DB_NAME)
    print("HUMAN APPROVAL — АКТИВАЦИЯ")
    print("==========================")
    print("Портфель:", result["portfolio_id"])
    print("Создан сейчас:", "да" if result["created"] else "нет")
    print("activated_at:", result["activated_at"])
    print("Cash:", result["cash_usd"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
