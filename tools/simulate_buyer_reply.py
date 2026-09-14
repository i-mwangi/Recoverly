from __future__ import annotations

import argparse
import logging

from src.concierge.inbound import accept_inbound

REPLY_TEMPLATES: dict[str, tuple[str, str]] = {
    "A1": ("Promise to pay today", "Thanks for reaching out. I'll wire the balance today."),
    "B1": ("Dispute amount", "We dispute the invoice amount, please send a corrected version."),
    "B2": ("Request payment plan", "Can we split this into three monthly installments?"),
    "B3": ("Already paid", "We already paid this on the 15th, please check your records."),
    "B4": ("Silent", "..."),
    "F1": ("Cease and desist", "Cease and desist from contacting us further."),
    "F5": ("Attorney letter", "Our counsel will respond in writing shortly."),
    "F8": ("Bankruptcy notice", "We filed for Chapter 11 last month."),
}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    parser = argparse.ArgumentParser(description="Send a fixture buyer reply through the inbound pipeline")
    parser.add_argument("persona", nargs="?", help="Buyer persona (e.g. abc_trading)")
    parser.add_argument("--case", required=False, help="Case id")
    parser.add_argument("--reply", default="A1", choices=sorted(REPLY_TEMPLATES))
    parser.add_argument("--list-replies", action="store_true")
    args = parser.parse_args()

    if args.list_replies:
        for key, (title, _) in REPLY_TEMPLATES.items():
            print(f"  {key}: {title}")
        return 0

    if not args.persona or not args.case:
        parser.error("persona and --case are required unless --list-replies is used")

    title, body = REPLY_TEMPLATES[args.reply]
    intents = accept_inbound(
        case_id=args.case,
        buyer_persona=args.persona,
        body_text=body,
        source="simulate_buyer_reply",
        sender_email=f"ap@{args.persona.replace('_', '-')}.example",
    )
    print(f"Reply {args.reply} ({title}) applied to {args.case}. Signals: {intents.signals() or 'none'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
