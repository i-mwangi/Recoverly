from __future__ import annotations

import argparse
import json
import logging

from src.concierge.actions import dispatch


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Fire a Slack button dispatch through the action registry")
    parser.add_argument("action_id", help="e.g. hitl_approve, lite_run_RC-1")
    parser.add_argument("case_id")
    parser.add_argument("--user", default="operator")
    args = parser.parse_args()

    result = dispatch(
        {"action_id": args.action_id, "value": args.case_id},
        {"user": {"username": args.user}, "channel": {"id": "SIM"}, "message": {"ts": "0"}},
    )
    print(json.dumps(result.as_response(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
