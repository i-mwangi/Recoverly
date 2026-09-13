from __future__ import annotations

import argparse
import datetime as dt
import json
import logging

from src.concierge.invoice_calendar import run


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Run one tick of the invoice calendar")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--today", help="ISO date override for backfill or replay")
    args = parser.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else None
    print(json.dumps(run(dry_run=args.dry_run, today=today), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
