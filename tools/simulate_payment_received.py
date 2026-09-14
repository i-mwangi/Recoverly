from __future__ import annotations

import argparse
import logging

from src.agents._case_state import load_case_state
from src.payments.reconciler import reconcile_inbound


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Apply a simulated inbound payment to a case")
    parser.add_argument("case_id")
    parser.add_argument("amount_usd", type=float)
    parser.add_argument("--channel", default="usdc")
    args = parser.parse_args()

    state = load_case_state(args.case_id)
    result = reconcile_inbound(args.case_id, str(args.amount_usd), transaction_id=f"sim-{args.case_id}")
    print(
        f"case {args.case_id} was {state.get('status')}, now {result.status.value}: "
        f"paid ${result.paid_to_date_usd:,.2f} of ${result.outstanding_usd:,.2f}, "
        f"remaining ${result.remaining_usd:,.2f}, channel {args.channel}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
