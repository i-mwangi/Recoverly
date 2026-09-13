from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Final

from src.agents.base import audit
from src.concierge.buyer_send import EmailBackendError, supplier_name
from src.config import settings

log = logging.getLogger("recoverly.payments.notify")

AGENT: Final = "payment_notify"
DEFAULT_FROM_ADDRESS: Final = "onboarding@resend.dev"

SETTLEMENT_TEMPLATE: Final = """\
{buyer_name} has paid.

• Case: {case_id}
• Invoice: {invoice_no}
• Amount received: USD ${amount_paid:,.2f}
• Remaining balance: USD ${remaining:,.2f}
• Settlement channel: {channel}

{closing_line}

{supplier}
"""

CLOSING_SETTLED: Final = "The balance is clear and the case is closed. No further reminders will go out."
CLOSING_PARTIAL: Final = (
    "This was a partial payment, so the collection cadence continues on the remaining balance."
)


@dataclass(frozen=True, slots=True)
class NotifyResult:
    ok: bool
    message_id: str = ""
    to: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message_id": self.message_id, "to": self.to, "reason": self.reason}


def recipient() -> str:
    return os.getenv("RECOVERLY_NOTIFY_TO_EMAIL", "").strip()


def sender() -> str:
    return (
        os.getenv("RECOVERLY_NOTIFY_FROM", "").strip()
        or os.getenv("RESEND_FROM_EMAIL", "").strip()
        or f"{supplier_name()} <{DEFAULT_FROM_ADDRESS}>"
    )


def build_body(
    case_id: str,
    buyer_name: str,
    invoice_no: str,
    amount_paid: float,
    remaining: float,
    channel: str,
) -> str:
    return SETTLEMENT_TEMPLATE.format(
        case_id=case_id,
        buyer_name=buyer_name,
        invoice_no=invoice_no,
        amount_paid=amount_paid,
        remaining=remaining,
        channel=channel,
        closing_line=CLOSING_SETTLED if remaining <= 0 else CLOSING_PARTIAL,
        supplier=supplier_name(),
    )


def send_payment_received(
    case_id: str,
    buyer_name: str,
    invoice_no: str,
    amount_paid: float,
    remaining: float = 0.0,
    channel: str = "USDC",
) -> NotifyResult:
    to_address = recipient()
    if not to_address:
        log.info("no supplier notification address is set, so nothing is sent")
        return NotifyResult(ok=False, reason="RECOVERLY_NOTIFY_TO_EMAIL is not set")

    api_key = settings.api_keys.resend
    if not api_key:
        return NotifyResult(ok=False, to=to_address, reason="RESEND_API_KEY is not set")

    try:
        import resend
    except ImportError as error:
        raise EmailBackendError("the resend package is not installed") from error

    resend.api_key = api_key
    subject = (
        f"Payment received on {invoice_no}: ${amount_paid:,.2f} from {buyer_name}"
    )

    try:
        response = resend.Emails.send(
            {
                "from": sender(),
                "to": [to_address],
                "subject": subject,
                "text": build_body(
                    case_id, buyer_name, invoice_no, amount_paid, remaining, channel
                ),
                "tags": [
                    {"name": "event", "value": "payment_received"},
                    {"name": "case_id", "value": case_id},
                ],
            }
        )
    except Exception as error:
        log.warning("could not notify the supplier for case %s: %s", case_id, error)
        audit(case_id, AGENT, "supplier_notification_failed", {"error": str(error)})
        return NotifyResult(ok=False, to=to_address, reason=str(error))

    result = NotifyResult(ok=True, message_id=str(response.get("id", "")), to=to_address)
    audit(case_id, AGENT, "supplier_notified", result.as_dict())
    log.info("notified the supplier that case %s received a payment", case_id)
    return result
