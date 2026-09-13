from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from src.diplomat.templates import (
    DEFAULT_DUE_DATE,
    DEFAULT_INVOICE_NO,
    build_paylink,
    lookup_contract,
    outstanding_amount,
    supplier_name,
)

CURE_WINDOW_DAYS: Final = 14
DEFAULT_DAYS_PAST_DUE: Final = 60
DEFAULT_BUYER_STATE: Final = "the United States"
DEFAULT_CONTRACT_ID: Final = "the parties' master services agreement"
DEFAULT_SIGNED_AT: Final = "the date set forth in that agreement"

FILING_FEE_SCHEDULE: Final[tuple[tuple[float, int], ...]] = (
    (75_000, 925),
    (150_000, 1_775),
    (300_000, 2_950),
)
FILING_FEE_MAXIMUM: Final = 4_575

DEMAND_LETTER_TEMPLATE: Final = """\
{letter_date}

SENT BY EMAIL, DELIVERY CONFIRMATION REQUESTED

{buyer_legal_name}
Attention: Accounts Payable
{buyer_state}

Re: FORMAL NOTICE OF DEFAULT AND DEMAND FOR PAYMENT
    Invoice number: {invoice_no}
    Outstanding balance: USD ${outstanding_usd:,.2f}
    Days past due: {days_past_due}
    Agreement: {contract_id} dated {contract_signed_at}

To whom it may concern,

This letter is formal written notice that {buyer_legal_name} is in default of its
payment obligations to {supplier_name} under the agreement referenced above and
the corresponding invoice {invoice_no}.

As of the date of this letter the outstanding balance of USD ${outstanding_usd:,.2f}
remains unpaid despite repeated good-faith collection efforts. The original payment
due date was {due_date}, so the account is {days_past_due} days past due.

{supplier_name} demands payment in full of USD ${outstanding_usd:,.2f} within
{cure_window_days} calendar days of the date of this letter, by {cure_deadline}.

If full payment is not received by that date, {supplier_name} intends to file a
demand for arbitration with the American Arbitration Association under its
Commercial Arbitration Rules, with venue in the regional office covering
{buyer_state}. The claim will seek the outstanding balance together with interest,
the filing fee, currently estimated at USD ${filing_fee:,}, arbitrator compensation,
and any attorneys' fees and costs recoverable from the losing party under the
agreement.

Payment can be made through the secure settlement portal below, which supports wire,
ACH and USDC and pre-fills the invoice reference:

    Pay {invoice_no}: {paylink}

Bank routing details are presented inside the portal after identity verification,
never in correspondence.

This communication is a good-faith attempt to resolve the matter without
arbitration. Nothing in it waives any right or remedy, all of which are reserved.

Sincerely,

{supplier_name}
"""

ORIENTATION_TEMPLATE: Final = """\
**Arbitration roadmap for case `{case_id}`**

1. **Pre-arbitration demand letter** — a {cure_window_days}-day cure window is standard. If the balance is still unpaid after it closes, the claim can be filed.
2. **Filing fee** — approximately ${filing_fee:,} on the standard commercial schedule for a claim of ${outstanding_usd:,.0f}. Recoverable from the losing party where the agreement has a fees clause.
3. **Case management** — a single arbitrator for claims under one million dollars. Six to nine months to an award on documents, twelve to eighteen if the matter is fully heard.
4. **Venue** — as set by the agreement's forum clause, normally the administrator's regional office.
5. **Settlement pressure** — most commercial filings settle before the hearing, so the filing itself is the forcing function.

card_kind=aaa_specialist_orientation
"""


def filing_fee_estimate(amount_usd: float) -> int:
    for ceiling, fee in FILING_FEE_SCHEDULE:
        if amount_usd < ceiling:
            return fee
    return FILING_FEE_MAXIMUM


@dataclass(frozen=True, slots=True)
class DemandLetterContext:
    case_id: str
    invoice_no: str
    outstanding_usd: float
    filing_fee: int
    buyer_legal_name: str
    buyer_state: str
    buyer_persona: str
    days_past_due: int
    cure_deadline: str
    paylink: str


def _first(*values: Any, default: str = "") -> str:
    for value in values:
        if value:
            return str(value)
    return default


def build_context(case: dict[str, Any], today: date | None = None) -> DemandLetterContext:
    contract = lookup_contract(case)
    reference_day = today or date.today()
    case_id = str(case.get("case_id") or "UNKNOWN")
    outstanding = outstanding_amount(case)

    invoice_no = _first(
        case.get("invoice_no"),
        case.get("invoice_id"),
        (contract.get("active_invoices") or [None])[0],
        default=DEFAULT_INVOICE_NO,
    )

    try:
        days_past_due = int(case.get("days_past_due") or DEFAULT_DAYS_PAST_DUE)
    except (TypeError, ValueError):
        days_past_due = DEFAULT_DAYS_PAST_DUE

    return DemandLetterContext(
        case_id=case_id,
        invoice_no=invoice_no,
        outstanding_usd=outstanding,
        filing_fee=filing_fee_estimate(outstanding),
        buyer_legal_name=_first(
            contract.get("buyer_legal_name"),
            case.get("buyer_legal_name"),
            case.get("customer_company"),
            case.get("customer_name"),
            default="the buyer",
        ),
        buyer_state=_first(
            contract.get("buyer_state"),
            case.get("customer_state"),
            case.get("buyer_state"),
            default=DEFAULT_BUYER_STATE,
        ),
        buyer_persona=str(case.get("buyer_persona") or ""),
        days_past_due=days_past_due,
        cure_deadline=(reference_day + timedelta(days=CURE_WINDOW_DAYS)).isoformat(),
        paylink=build_paylink(case_id, invoice_no, outstanding),
    )


def render_demand_letter(
    case: dict[str, Any], today: date | None = None
) -> tuple[str, str, DemandLetterContext]:
    contract = lookup_contract(case)
    context = build_context(case, today)
    reference_day = today or date.today()

    body = DEMAND_LETTER_TEMPLATE.format(
        letter_date=reference_day.strftime("%B %d, %Y"),
        buyer_legal_name=context.buyer_legal_name,
        buyer_state=context.buyer_state,
        invoice_no=context.invoice_no,
        outstanding_usd=context.outstanding_usd,
        days_past_due=context.days_past_due,
        contract_id=_first(contract.get("contract_id"), default=DEFAULT_CONTRACT_ID),
        contract_signed_at=_first(contract.get("signed_at"), default=DEFAULT_SIGNED_AT),
        due_date=_first(case.get("due_date"), contract.get("due_date"), default=DEFAULT_DUE_DATE),
        cure_window_days=CURE_WINDOW_DAYS,
        cure_deadline=context.cure_deadline,
        filing_fee=context.filing_fee,
        paylink=context.paylink,
        supplier_name=supplier_name(),
    )

    subject = (
        f"Formal notice of default — invoice {context.invoice_no} "
        f"({CURE_WINDOW_DAYS}-day cure window)"
    )
    return subject, body, context


def render_orientation(case_id: str, outstanding_usd: float) -> str:
    return ORIENTATION_TEMPLATE.format(
        case_id=case_id,
        outstanding_usd=outstanding_usd,
        filing_fee=filing_fee_estimate(outstanding_usd),
        cure_window_days=CURE_WINDOW_DAYS,
    )
