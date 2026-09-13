from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.concierge.email_intake import ingest_invoice_email


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Feed a raw invoice email through the intake pipeline")
    parser.add_argument("--file", type=Path, help="Path to a plain-text email body")
    parser.add_argument("--subject", default="Invoice for review")
    parser.add_argument("--sender", default="ap@example.com")
    args = parser.parse_args()

    if args.file:
        body = args.file.read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        parser.error("either --file or piped stdin is required")

    record = ingest_invoice_email(subject=args.subject, body=body, sender=args.sender)
    parsed = record["parsed"]
    print(f"Case {record['case_id']}: {parsed['invoice_no']} for {parsed['buyer_persona'] or 'unknown buyer'}")
    print(f"  gross ${parsed['gross_usd']:,.2f}, outstanding ${parsed['outstanding_usd']:,.2f}")
    if parsed["needs_review"]:
        print(f"  needs review: {parsed['review_reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
