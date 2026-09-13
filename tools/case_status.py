from __future__ import annotations

import argparse
import json
import logging

from src.agents._case_state import load_case_state
from src.concierge.audit_logger import audit_log


def main() -> int:
    logging.basicConfig(level=logging.WARNING)

    parser = argparse.ArgumentParser(description="Print case state and recent audit events")
    parser.add_argument("case_id")
    parser.add_argument("--events", type=int, default=10)
    args = parser.parse_args()

    state = load_case_state(args.case_id)
    print("state:")
    print(json.dumps(state, indent=2, default=str))

    print(f"\nlast {args.events} audit events:")
    for event in audit_log.for_case(args.case_id)[-args.events:]:
        print(json.dumps(event, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
