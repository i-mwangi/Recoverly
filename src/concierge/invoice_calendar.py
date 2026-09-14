from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from typing import Any, Final

from src.agents.base import audit
from src.concierge.strands_relay import notify_agent
from src.concierge.contracts import find_contract_by_buyer
from src.config import PROJECT_ROOT
from src.personas import buyer_id_for
from src.utils.filelock import exclusive_lock, write_atomic

log = logging.getLogger("recoverly.concierge.invoice_calendar")

AGENT: Final = "invoice_calendar"
INVOICES_LEDGER: Final = PROJECT_ROOT / "data" / "invoices.jsonl"
CALENDAR_STATE: Final = PROJECT_ROOT / "data" / "invoice_calendar_state.json"

PRE_DUE_LEAD_DAYS: Final = 3
OVERDUE_BACKSTOP_DAYS: Final = 7


class InvoiceEvent:
    PRE_DUE = "pre_due_reminder"
    DUE_TODAY = "due_today_reminder"
    OVERDUE = "open_overdue_case"


def _parse_due(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def classify(due_date: str, today: date) -> str | None:
    due = _parse_due(due_date)
    if due is None:
        return None

    delta = (due - today).days
    if delta == PRE_DUE_LEAD_DAYS:
        return InvoiceEvent.PRE_DUE
    if delta == 0:
        return InvoiceEvent.DUE_TODAY
    if -OVERDUE_BACKSTOP_DAYS <= delta < 0:
        return InvoiceEvent.OVERDUE
    return None


def load_invoices() -> list[dict[str, Any]]:
    if not INVOICES_LEDGER.exists():
        return []

    invoices: list[dict[str, Any]] = []
    with INVOICES_LEDGER.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                log.debug("skipping a malformed line in the invoice ledger")
                continue
            if isinstance(entry, dict):
                invoices.append(entry)
    return invoices


def load_state() -> dict[str, Any]:
    try:
        stored = json.loads(CALENDAR_STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"per_invoice": {}}
    return stored if isinstance(stored, dict) else {"per_invoice": {}}


def save_state(state: dict[str, Any]) -> None:
    with exclusive_lock(CALENDAR_STATE.with_suffix(".json.lock")):
        write_atomic(CALENDAR_STATE, json.dumps(state, indent=2))


def has_signed_contract(buyer_persona: str) -> bool:
    buyer_id = buyer_id_for(buyer_persona)
    if not buyer_id:
        return False
    try:
        return find_contract_by_buyer(buyer_id) is not None
    except Exception:
        return False


def _event_key(kind: str) -> str:
    return f"last_{kind}"


def process_invoice(
    invoice: dict[str, Any], today: date, state: dict[str, Any], dry_run: bool
) -> str | None:
    invoice_no = str(invoice.get("invoice_no") or invoice.get("invoice_id") or "")
    if not invoice_no:
        return None

    kind = classify(str(invoice.get("due_date", "")), today)
    if kind is None:
        return None

    per_invoice = state.setdefault("per_invoice", {})
    record = per_invoice.setdefault(invoice_no, {})
    if record.get(_event_key(kind)) == today.isoformat():
        return None

    buyer_persona = str(invoice.get("buyer_persona", ""))
    case_id = str(invoice.get("case_id") or invoice_no)

    if kind == InvoiceEvent.OVERDUE and not has_signed_contract(buyer_persona):
        audit(
            case_id,
            AGENT,
            "overdue_without_contract",
            {"invoice_no": invoice_no, "buyer_persona": buyer_persona},
        )
        return None

    if not dry_run:
        record[_event_key(kind)] = today.isoformat()
        audit(
            case_id,
            AGENT,
            f"invoice_{kind}",
            {
                "invoice_no": invoice_no,
                "buyer_persona": buyer_persona,
                "due_date": invoice.get("due_date"),
                "outstanding_usd": invoice.get("outstanding_usd"),
            },
        )
        notify_agent(
            "concierge",
            f"event=invoice_{kind} for case `{case_id}`. Invoice `{invoice_no}` due "
            f"{invoice.get('due_date')} for buyer {buyer_persona}.",
            case_id=case_id,
        )

    return kind


def run(dry_run: bool = False, today: date | None = None) -> dict[str, Any]:
    reference = today or datetime.now(timezone.utc).date()
    state = load_state()
    fired: dict[str, list[str]] = {}

    for invoice in load_invoices():
        kind = process_invoice(invoice, reference, state, dry_run)
        if kind:
            fired.setdefault(kind, []).append(
                str(invoice.get("invoice_no") or invoice.get("invoice_id"))
            )

    if not dry_run:
        save_state(state)

    return {"date": reference.isoformat(), "dry_run": dry_run, "fired": fired}
