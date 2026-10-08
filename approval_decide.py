"""
Решение по заявке Human Approval.

python approval_decide.py <request_id> buy
python approval_decide.py <request_id> skip
"""

import argparse
import sys

import database
from human_approval import REASON_NOT_FOUND, decide_buy, decide_skip


def configure_console():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def render_decision(result):
    print("HUMAN APPROVAL — РЕШЕНИЕ")
    print("========================")
    print("Request:", result.get("request_id"))
    print("Status:", result.get("status"))
    print("Outcome:", result.get("outcome"))
    print("Reason:", result.get("reason"))
    print("Allocation:", result.get("allocation_id"))
    print("Execution price:", result.get("execution_price"))
    print("Quoted at:", result.get("quoted_at"))
    print("Execution at:", result.get("execution_at"))
    if result.get("detail"):
        print("Detail:", result.get("detail"))


def main(argv=None):
    configure_console()
    parser = argparse.ArgumentParser(description="BUY или SKIP по заявке Human Approval")
    parser.add_argument("request_id", type=int)
    parser.add_argument("action", choices=["buy", "skip"])
    parser.add_argument("--db", dest="db_path", default=None)
    args = parser.parse_args(argv)
    db_path = args.db_path or database.DB_NAME
    if args.action == "buy":
        result = decide_buy(args.request_id, db_path=db_path)
    else:
        result = decide_skip(args.request_id, db_path=db_path)
    render_decision(result)
    if result.get("reason") == REASON_NOT_FOUND:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
