from __future__ import annotations

import os
from dataclasses import dataclass
import re
from typing import Any, Final
from urllib.parse import quote

from src.concierge.contracts import find_contract_by_buyer
from src.personas import buyer_id_for

DEFAULT_PAYLINK_BASE: Final = "https://pay.example.com/recoverly"
DEFAULT_SUPPLIER_NAME: Final = "Recoverly Collections"
DEFAULT_INVOICE_NO: Final = "INV-UNKNOWN"
DEFAULT_DUE_DATE: Final = "the original invoice due date"
DEFAULT_BUYER_NAME: Final = "Customer"
DEFAULT_BUYER_FIRST_NAME: Final = "there"
DEFAULT_DAY7_OFFSET: Final = 7
MIN_REVISED_BODY_LENGTH: Final = 50

CHECKOUT_PLACEHOLDERS: Final[tuple[str, ...]] = (
    "{{CHECKOUT_URL}}",
    "{{checkout_url}}",
    "{CHECKOUT_URL}",
    "{{PAYLINK}}",
    "{{paylink}}",
    "{PAYLINK}",
    "[CHECKOUT_URL]",
    "[PAYLINK]",
)

DAY7_REMINDER_TEMPLATE: Final = """\
Hi {buyer_first_name},

Hope your week is going well. This is a gentle reminder that invoice {invoice_no} for
{buyer_legal_name}, USD ${outstanding_balance_usd:,.2f}, is now {days_past_due} days past
its {due_date} payment date.

If the payment is already on its way, please ignore this note. Otherwise, could you let us
know roughly when we can expect it? You can settle in seconds through the secure portal
below, with the invoice reference pre-filled:

    Pay {invoice_no}: {paylink}

If anything about the invoice needs adjusting, such as a purchase-order mismatch or a terms
question, just reply here and we will sort it out the same day.

Thanks so much,
{operator_first_name}
{supplier_name}
"""

LITE_FINAL_NOTICE_TEMPLATE: Final = """\
Dear {buyer_first_name},

This is the final notice on invoice {invoice_no} for {buyer_legal_name}, which was due on
{due_date} and is now {days_past_due} days past due.

Outstanding balance owed: USD ${outstanding_balance_usd:,.2f}

Under our lite-mode policy for smaller balances, no further reminders will follow. Please
settle the balance using the link below to close the case.

Pay now: {paylink}

If we do not receive payment within seven calendar days of this notice, the account moves to
write-off and follows our standard delinquency process.

Thank you,
{operator_first_name}
{supplier_name}
"""

DAY7_SUBJECT: Final = "Friendly reminder — invoice {invoice_no} due {due_date}"
LITE_SUBJECT: Final = "Final notice — invoice {invoice_no}"


@dataclass(frozen=True, slots=True)
class EmailContext:
    case_id: str
    invoice_no: str
    due_date: str
    days_past_due: int
    outstanding_balance_usd: float
    buyer_legal_name: str
    buyer_first_name: str
    buyer_persona: str
    operator_first_name: str
    supplier_name: str
    paylink: str

    def as_format_kwargs(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "invoice_no": self.invoice_no,
            "due_date": self.due_date,
            "days_past_due": self.days_past_due,
            "outstanding_balance_usd": self.outstanding_balance_usd,
            "buyer_legal_name": self.buyer_legal_name,
            "buyer_first_name": self.buyer_first_name,
            "operator_first_name": self.operator_first_name,
            "supplier_name": self.supplier_name,
            "paylink": self.paylink,
        }


def paylink_base() -> str:
    return os.getenv("RECOVERLY_PAYLINK_BASE", "").strip() or DEFAULT_PAYLINK_BASE


def supplier_name() -> str:
    return os.getenv("RECOVERLY_SUPPLIER_NAME", "").strip() or DEFAULT_SUPPLIER_NAME


def build_paylink(case_id: str, invoice_no: str, amount_usd: float, mode: str | None = None) -> str:
    link = f"{paylink_base()}/{quote(case_id)}?inv={quote(invoice_no)}&amt={amount_usd:.2f}"
    return f"{link}&mode={quote(mode)}" if mode else link


def outstanding_amount(case: dict[str, Any]) -> float:
    from src.payments.channels import outstanding_for
    return outstanding_for(case)


def lookup_contract(case: dict[str, Any]) -> dict[str, Any]:
    buyer_id = buyer_id_for(case.get("buyer_persona"))
    if not buyer_id:
        return {}
    try:
        return find_contract_by_buyer(buyer_id) or {}
    except Exception:
        return {}


def _first(*values: Any, default: Any = "") -> Any:
    for value in values:
        if value:
            return value
    return default


def resolve_invoice_no(case: dict[str, Any], contract: dict[str, Any]) -> str:
    consolidated = case.get("consolidated_invoices") or []
    first_consolidated = consolidated[0].get("invoice_no") if consolidated else None
    active = contract.get("active_invoices") or []
    return str(
        _first(
            case.get("invoice_no"),
            case.get("invoice_id"),
            active[0] if active else None,
            first_consolidated,
            default=DEFAULT_INVOICE_NO,
        )
    )


def build_context(case: dict[str, Any], *, mode: str | None = None) -> EmailContext:
    contract = lookup_contract(case)
    case_id = str(case.get("case_id") or "UNKNOWN")
    amount = outstanding_amount(case)
    invoice_no = resolve_invoice_no(case, contract)

    try:
        days_past_due = int(
            case.get("days_past_due") or case.get("cadence_day") or DEFAULT_DAY7_OFFSET
        )
    except (TypeError, ValueError):
        days_past_due = DEFAULT_DAY7_OFFSET

    return EmailContext(
        case_id=case_id,
        invoice_no=invoice_no,
        due_date=str(_first(case.get("due_date"), contract.get("due_date"), default=DEFAULT_DUE_DATE)),
        days_past_due=days_past_due,
        outstanding_balance_usd=amount,
        buyer_legal_name=str(
            _first(
                contract.get("buyer_legal_name"),
                case.get("buyer_legal_name"),
                case.get("customer_company"),
                case.get("customer_name"),
                default=DEFAULT_BUYER_NAME,
            )
        ),
        buyer_first_name=str(
            _first(
                case.get("buyer_first_name"),
                case.get("customer_first_name"),
                str(case.get("customer_name") or "").strip().split(" ")[0],
                default=DEFAULT_BUYER_FIRST_NAME,
            )
        ),
        buyer_persona=str(case.get("buyer_persona") or ""),
        operator_first_name=str(_first(case.get("operator_first_name"), default=supplier_name())),
        supplier_name=supplier_name(),
        paylink=build_paylink(case_id, invoice_no, amount, mode),
    )


def substitute_placeholders(body: str, context: EmailContext) -> str:
    for placeholder in CHECKOUT_PLACEHOLDERS:
        body = body.replace(placeholder, context.paylink)
    body = body.replace("[Your Name]", context.operator_first_name)
    body = body.replace("[DATE]", context.due_date)
    body = body.replace("[First Name]", context.buyer_first_name)
    body = body.replace("[AP Contact Name]", context.buyer_first_name)
    body = body.replace("[Contact Name]", context.buyer_first_name)
    body = body.replace("[Client Name]", context.buyer_first_name)
    body = body.replace("[Customer Name]", context.buyer_first_name)
    body = body.replace(" on [Date, if known; otherwise omit]", "")
    body = body.replace("[Due Date]", context.due_date)
    body = body.replace(
        "by [Specific Date, e.g., 5 business days from send]",
        "within five business days",
    )
    body = re.sub(r"\[Date,[^\]]*\]", "", body)
    body = re.sub(r"\[Specific Date,[^\]]*\]", "within five business days", body)
    body = re.sub(r"by \[date[^\]]*\]", "within five business days", body, flags=re.IGNORECASE)
    body = re.sub(r"\s+or call \[phone number\]", "", body, flags=re.IGNORECASE)
    return body


def render_day7(case: dict[str, Any]) -> tuple[str, str, EmailContext]:
    context = build_context(case)
    revised = case.get("revised_email_body")

    if isinstance(revised, str) and len(revised.strip()) >= MIN_REVISED_BODY_LENGTH:
        body = revised
    else:
        body = DAY7_REMINDER_TEMPLATE.format(**context.as_format_kwargs())

    subject = DAY7_SUBJECT.format(invoice_no=context.invoice_no, due_date=context.due_date)
    return subject, substitute_placeholders(body, context), context


def render_lite_final_notice(case: dict[str, Any]) -> tuple[str, str, EmailContext]:
    context = build_context(case, mode="lite")
    body = LITE_FINAL_NOTICE_TEMPLATE.format(**context.as_format_kwargs())
    subject = LITE_SUBJECT.format(invoice_no=context.invoice_no)
    return subject, substitute_placeholders(body, context), context
