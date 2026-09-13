from __future__ import annotations

import argparse
import datetime as dt
from zoneinfo import ZoneInfo

from src.utils.timezone import customer_state_to_timezone


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute days past due in customer local time")
    parser.add_argument("due_date", help="ISO date, for example 2026-05-08")
    parser.add_argument("customer_tz_or_state", help="IANA name or two-letter US state")
    args = parser.parse_args()

    tz_name = (
        args.customer_tz_or_state
        if "/" in args.customer_tz_or_state
        else customer_state_to_timezone(args.customer_tz_or_state)
    )
    zone = ZoneInfo(tz_name)

    due = dt.datetime.strptime(args.due_date, "%Y-%m-%d").replace(tzinfo=zone).date()
    today_customer = dt.datetime.now(zone).date()
    today_operator = dt.datetime.now(ZoneInfo("Asia/Seoul")).date()

    print(f"Due: {due} in {tz_name}")
    print(f"Customer local today: {today_customer} ({(today_customer - due).days} days past due)")
    print(f"Operator local today: {today_operator} ({(today_operator - due).days} days past due)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
