from __future__ import annotations

import argparse
import json
import logging

from src.concierge.cadence_scheduler import CADENCE_STAGES, schedule_collection_calendar


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Schedule the five-stage Slack reminder calendar")
    parser.add_argument("case_id")
    parser.add_argument("due_date")
    parser.add_argument("buyer_name")
    parser.add_argument("invoice_no")
    parser.add_argument("outstanding_usd", type=float)
    parser.add_argument("--channel", default=None, help="Slack channel override")
    parser.add_argument("--list-stages", action="store_true")
    args = parser.parse_args()

    if args.list_stages:
        for stage in CADENCE_STAGES:
            print(f"  day {stage.day:>2}: {stage.label} ({stage.drafting_agent})")
        return 0

    result = schedule_collection_calendar(
        args.case_id, args.due_date, args.buyer_name, args.invoice_no, args.outstanding_usd, args.channel
    )
    print(json.dumps(result.as_dict(), indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
