from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from src.agents.base import audit
from src.agents._utils import safe_float
from src.concierge.contracts import find_contract_by_buyer
from src.config import PROJECT_ROOT
from src.personas import buyer_id_for, KNOWN_PERSONAS
from src.utils.filelock import append_jsonl

log = logging.getLogger("recoverly.concierge.email_intake")

INBOUND_QUEUE: Final = PROJECT_ROOT / "data" / "email_intake.jsonl"
AGENT: Final = "email_intake"

INVOICE_NO_RE: Final = re.compile(r"\bINV-(\d{4})-(\d{4})\b", re.IGNORECASE)
AMOUNT_RE: Final = re.compile(r"\$([0-9,]+(?:\.\d{1,2})?)")
DEPOSIT_RE: Final = re.compile(
    r"deposit(?:\s+paid)?\s*[:=]?\s*\$?([0-9,]+(?:\.\d{1,2})?)", re.IGNORECASE
)
DEPOSIT_PCT_RE: Final = re.compile(r"(\d{1,2})\s*%\s*deposit", re.IGNORECASE)
DUE_DATE_RE: Final = re.compile(r"due(?:\s*date)?\s*[:=]?\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
NET_TERMS_RE: Final = re.compile(r"\bnet\s*(\d{1,3})\b", re.IGNORECASE)
PO_RE: Final = re.compile(r"\bPO[-#\s]*([A-Z0-9-]{3,20})\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ParsedInvoice:
    invoice_no: str
    buyer_persona: str
    buyer_id: str
    gross_usd: float
    deposit_usd: float
    outstanding_usd: float
    due_date: str
    po_ref: str
    net_terms_days: int
    needs_review: bool
    review_reason: str = ""

    @property
    def contract_id(self) -> str:
        contract = find_contract_by_buyer(self.buyer_id) if self.buyer_id else None
        return str(contract.get("contract_id", "")) if contract else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "invoice_no": self.invoice_no,
            "buyer_persona": self.buyer_persona,
            "buyer_id": self.buyer_id,
            "gross_usd": self.gross_usd,
            "deposit_usd": self.deposit_usd,
            "outstanding_usd": self.outstanding_usd,
            "due_date": self.due_date,
            "po_ref": self.po_ref,
            "net_terms_days": self.net_terms_days,
            "needs_review": self.needs_review,
            "review_reason": self.review_reason,
        }


def _parse_amount(value: str) -> float:
    return safe_float(value.replace(",", ""), 0.0)


def match_persona(haystack: str) -> tuple[str, str]:
    lowered = haystack.lower()
    for persona in KNOWN_PERSONAS:
        needle = persona.replace("_", " ")
        if persona in lowered or needle in lowered or persona.replace("_", "-") in lowered:
            return persona, buyer_id_for(persona) or ""
    return "", ""


def _first(match: re.Match[str] | None, group: int = 1, default: str = "") -> str:
    return match.group(group) if match else default


def _resolve_due_date(explicit: str, invoice_year_hint: int, net_terms_days: int) -> str:
    if explicit:
        return explicit
    if net_terms_days > 0 and invoice_year_hint:
        return f"{invoice_year_hint}-12-31"
    return ""


def parse_invoice_email(subject: str, body: str, sender: str = "") -> ParsedInvoice:
    text = f"{subject}\n{body}\n{sender}"
    review: list[str] = []

    invoice_match = INVOICE_NO_RE.search(text)
    if not invoice_match:
        review.append("no invoice number in subject or body")
        invoice_no = "INV-UNKNOWN"
    else:
        invoice_no = f"INV-{invoice_match.group(1)}-{invoice_match.group(2)}"

    persona, buyer_id = match_persona(text)
    if not persona:
        review.append("could not identify the buyer")

    amount_match = AMOUNT_RE.search(body) or AMOUNT_RE.search(subject)
    gross = _parse_amount(_first(amount_match, default="0"))
    if gross <= 0:
        review.append("no invoice amount")

    deposit_amount = _parse_amount(_first(DEPOSIT_RE.search(body), default="0"))
    deposit_pct_match = DEPOSIT_PCT_RE.search(body)
    if deposit_pct_match and gross > 0 and deposit_amount == 0:
        deposit_amount = round(gross * int(deposit_pct_match.group(1)) / 100, 2)

    outstanding = max(0.0, round(gross - deposit_amount, 2)) if gross > 0 else 0.0
    due_date = _first(DUE_DATE_RE.search(body), default="")
    po_ref = _first(PO_RE.search(text), default="")

    net_terms_match = NET_TERMS_RE.search(body)
    net_terms = int(net_terms_match.group(1)) if net_terms_match else 0

    if not due_date and not net_terms:
        review.append("no due date and no net terms")

    return ParsedInvoice(
        invoice_no=invoice_no,
        buyer_persona=persona,
        buyer_id=buyer_id,
        gross_usd=gross,
        deposit_usd=deposit_amount,
        outstanding_usd=outstanding,
        due_date=due_date,
        po_ref=po_ref,
        net_terms_days=net_terms,
        needs_review=bool(review),
        review_reason="; ".join(review),
    )


def mint_case_id(now: datetime | None = None) -> str:
    when = now or datetime.now(timezone.utc)
    return f"RC-{when.year}-e{int(when.timestamp()) % 100_000:05d}"


def ingest_invoice_email(
    subject: str, body: str, sender: str = "", now: datetime | None = None
) -> dict[str, Any]:
    parsed = parse_invoice_email(subject, body, sender)
    case_id = mint_case_id(now)

    record = {
        "case_id": case_id,
        "received_at": (now or datetime.now(timezone.utc)).isoformat(),
        "sender": sender,
        "subject": subject,
        "parsed": parsed.as_dict(),
    }

    append_jsonl(INBOUND_QUEUE, record)
    audit(
        case_id,
        AGENT,
        "invoice_email_ingested",
        {"sender": sender, "needs_review": parsed.needs_review, **parsed.as_dict()},
    )

    if parsed.needs_review:
        log.warning("invoice email ingested for %s but needs review: %s", case_id, parsed.review_reason)
    else:
        log.info("invoice email ingested for %s (%s)", case_id, parsed.invoice_no)

    return record
