from __future__ import annotations

import re
import sys
from decimal import Decimal
from typing import Any, Final

from src.agents._case_state import load_case_state
from src.agents._utils import is_error_envelope, parse_json_object, safe_float
from src.agents.base import StrandsAgentAdapter, audit, run_agent
from src.diplomat.templates import build_paylink
from src.payments.base import PaymentProviderError, PaymentResult
from src.payments.reconciler import reconcile_inbound
from src.payments.channels import (
    build_provider,
    channel_label,
    dry_run_enabled,
    outstanding_for,
    select_channel,
)

PAYMENT_RECEIVED_EVENT_RE: Final = re.compile(
    r'"event"\s*:\s*"payment_received(?:_manual_confirmed)?"'
    r"|event\s*[=:]\s*payment_received(?:_manual_confirmed)?",
    re.IGNORECASE,
)
SEAL_HANDOFF_RE: Final = re.compile(
    r"HITL\s+card\s+can\s+be\s+sealed.*?case_id=`([^`]+)`", re.IGNORECASE | re.DOTALL
)
CASE_ID_RE: Final = re.compile(r"case[_\s]+`([^`]+)`", re.IGNORECASE)
CASE_ID_BARE_RE: Final = re.compile(r"\b(RC-\d{4}-\d{4,})\b")


def extract_case_id(text: str) -> str:
    for pattern in (CASE_ID_RE, CASE_ID_BARE_RE):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return "UNKNOWN"


def _load_case(case_id: str) -> dict[str, Any]:
    try:
        return {**load_case_state(case_id), "case_id": case_id}
    except Exception:
        return {"case_id": case_id}


class PaymentAdapter(StrandsAgentAdapter):
    role = "payment"

    async def handle_message(self, text, msg, tools, history, room_id):
        if is_error_envelope(text):
            return None

        if PAYMENT_RECEIVED_EVENT_RE.search(text):
            return self._confirm_payment(text)

        sealed = SEAL_HANDOFF_RE.search(text)
        if sealed:
            return self._prepare_paylink(sealed.group(1))

        case = parse_json_object(text)
        if not case or not case.get("case_id"):
            case_id = extract_case_id(text)
            if case_id == "UNKNOWN":
                return None
            case = _load_case(case_id)

        return await self._create_intent(case)

    def _confirm_payment(self, text: str) -> str:
        payload = parse_json_object(text) or {}
        case_id = str(payload.get("case_id") or extract_case_id(text))
        amount = safe_float(
            payload.get("amount_usd")
            or payload.get("payable_usd")
            or payload.get("outstanding_balance_usd"),
            0.0,
        )
        channel = str(payload.get("channel") or payload.get("method") or "manual")

        audit(
            case_id,
            self.role,
            "payment_received",
            {"amount_usd": amount, "channel": channel},
        )
        if payload.get("reconciled"):
            state = load_case_state(case_id)
            return f"@concierge Payment recorded for case `{case_id}`. Status: {state['status']}; remaining ${outstanding_for(state):,.2f}."
        transaction_id = str(payload.get("transaction_id") or "")
        if not transaction_id:
            return f"@concierge Payment on case `{case_id}` needs a transaction reference before reconciliation."
        result = reconcile_inbound(case_id, amount, transaction_id)
        return (
            f"[Payment] @concierge payment on case `{case_id}`: "
            f"{result.status.value}, ${result.amount_applied_usd:,.2f} via {channel}. "
            f"Remaining balance: ${result.remaining_usd:,.2f}."
        )

    def _prepare_paylink(self, case_id: str) -> str:
        case = _load_case(case_id)
        outstanding = outstanding_for(case)
        channel = select_channel(case)
        invoice_no = str(case.get("invoice_no") or "INV-UNKNOWN")
        paylink = build_paylink(case_id, invoice_no, outstanding)

        audit(
            case_id,
            self.role,
            "paylink_prepared",
            {
                "channel": channel,
                "outstanding_balance_usd": outstanding,
                "paylink": paylink,
            },
        )
        return (
            f"[Payment] @concierge paylink ready for case `{case_id}`.\n"
            f"Channel: {channel_label(channel)} — outstanding ${outstanding:,.2f}\n"
            f"Paylink: {paylink}\n"
            f"Now watching for the settlement webhook."
        )

    async def _create_intent(self, case: dict[str, Any]) -> str:
        case_id = str(case.get("case_id") or "UNKNOWN")
        channel = select_channel(case)
        outstanding = outstanding_for(case)
        label = channel_label(channel)

        try:
            dry_run = dry_run_enabled()
        except RuntimeError as error:
            audit(case_id, self.role, "dry_run_guard_tripped", {"error": str(error)})
            return (
                f"@concierge Payment Agent refused to run on case `{case_id}`: {error}"
            )

        audit(
            case_id,
            self.role,
            "payment_intent_created",
            {
                "channel": channel,
                "label": label,
                "outstanding_balance_usd": outstanding,
                "payable_usd": outstanding,
                "dry_run": dry_run,
            },
        )

        if outstanding <= 0:
            return (
                f"@concierge Payment Agent cannot open a settlement for case `{case_id}`: "
                f"the outstanding balance is zero."
            )

        if dry_run:
            audit(
                case_id,
                self.role,
                "payment_dry_run_settled",
                {"channel": channel, "payable_usd": outstanding},
            )
            return (
                f"@concierge Payment Agent prepared a dry-run `{channel}` settlement intent for "
                f"case `{case_id}` (${outstanding:,.2f}, {label}). No charge was made. "
                f"card_kind=payment_intent"
            )

        try:
            result = await self._place(channel, case_id, outstanding)
        except (PaymentProviderError, ValueError) as error:
            audit(
                case_id,
                self.role,
                "payment_error",
                {"channel": channel, "error": str(error)},
            )
            return (
                f"@concierge Payment Agent could not open the `{channel}` settlement for case "
                f"`{case_id}`: {error}. Configure a payment method before retrying. "
                f"card_kind=payment_failed"
            )

        audit(case_id, self.role, "payment_created", result.as_dict())
        destination = (
            result.checkout_url
            or result.metadata.get("receiving_address")
            or "the details on the card"
        )
        return (
            f"@concierge Payment Agent opened a `{channel}` settlement for case `{case_id}` "
            f"(${outstanding:,.2f}, {label}).\n"
            f"Payment id {result.payment_id}, buyer pays at: {destination}\n"
            f"card_kind=payment_created"
        )

    async def _place(
        self, channel: str, case_id: str, outstanding: float
    ) -> PaymentResult:
        provider = build_provider(channel)
        case = _load_case(case_id)
        currency = "USDC" if channel == "usdc" else "USD"
        return await provider.create_payment(
            Decimal(str(outstanding)),
            currency,
            {
                "case_id": case_id,
                "channel": channel,
                "customer_email": case.get("customer_email")
                or case.get("buyer_email")
                or "",
            },
        )


if __name__ == "__main__":
    sys.exit(run_agent(PaymentAdapter))
