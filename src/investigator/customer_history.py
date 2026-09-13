from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Final

from src.config import settings

log = logging.getLogger("recoverly.investigator.customer_history")

RECENT_EXCUSE_LIMIT: Final = 3

PERSONA_ALIASES: Final[dict[str, str]] = {
    "abc_trading": "abc-corp",
    "abc": "abc-corp",
    "polymatrix": "polymatrix",
    "xyz": "xyz-inc",
    "xyz_industries": "xyz-inc",
    "newleaf": "newleaf",
    "newleaf_wellness": "newleaf",
    "megacorp": "megacorp",
}

DISPUTE_PHRASES: Final[tuple[str, ...]] = (
    "dispute",
    "wrong amount",
    "incorrect amount",
    "do not owe",
    "don't owe",
    "incorrect invoice",
    "billing error",
)

PARTIAL_PAYMENT_TAGS: Final[frozenset[str]] = frozenset(
    {"partial_paid", "partial_payment", "partial_payments"}
)


def load_fixture() -> dict[str, Any]:
    path = settings.data.customer_history_json
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("customer history fixture is missing at %s", path)
        return {}
    except (OSError, json.JSONDecodeError) as error:
        log.warning("customer history fixture is unreadable: %s", error)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def find_customer(fixture: dict[str, Any], persona_key: str) -> dict[str, Any] | None:
    key = (persona_key or "").strip().lower()
    if not key:
        return None

    customers = [c for c in (fixture.get("customers") or []) if isinstance(c, dict)]

    alias = PERSONA_ALIASES.get(key)
    if alias:
        for customer in customers:
            if alias in str(customer.get("customer_id", "")).lower():
                return customer

    for customer in customers:
        if str(customer.get("_persona_tag", "")).lower() == key:
            return customer

    for customer in customers:
        if key in str(customer.get("customer_id", "")).lower():
            return customer

    return None


def days_between(start_iso: str, end_iso: str) -> int:
    if not start_iso or not end_iso:
        return 0
    try:
        start = dt.date.fromisoformat(start_iso[:10])
        end = dt.date.fromisoformat(end_iso[:10])
    except ValueError:
        return 0
    return (end - start).days


def _days_late(invoice: dict[str, Any]) -> int:
    try:
        return int(invoice.get("days_late") or 0)
    except (TypeError, ValueError):
        return 0


def _settled(invoices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [invoice for invoice in invoices if invoice.get("paid_date")]


def count_disputes(invoices: list[dict[str, Any]]) -> int:
    total = 0
    for invoice in invoices:
        replies = invoice.get("customer_replies") or []
        snippets = " ".join(str(reply.get("snippet", "")) for reply in replies).lower()
        if any(phrase in snippets for phrase in DISPUTE_PHRASES):
            total += 1
    return total


def count_partial_payments(invoices: list[dict[str, Any]]) -> int:
    return sum(
        1
        for invoice in invoices
        if {str(tag).lower() for tag in (invoice.get("excuse_tags") or [])} & PARTIAL_PAYMENT_TAGS
    )


def recent_excuses(invoices: list[dict[str, Any]], limit: int = RECENT_EXCUSE_LIMIT) -> list[str]:
    ordered: list[str] = []
    for invoice in invoices:
        for tag in invoice.get("excuse_tags") or []:
            if tag not in ordered:
                ordered.append(str(tag))
    return ordered[-limit:]


def aggregate_persona_history(persona_key: str, as_of_date: str | None = None) -> dict[str, Any]:
    fixture = load_fixture()
    if not fixture:
        return {}

    customer = find_customer(fixture, persona_key)
    if not customer:
        log.info("no customer history found for persona %r", persona_key)
        return {}

    invoices = [inv for inv in (customer.get("invoices") or []) if isinstance(inv, dict)]
    settled = _settled(invoices)
    late = [invoice for invoice in settled if _days_late(invoice) > 0]

    average_days_late = (
        round(sum(_days_late(invoice) for invoice in late) / len(late), 1) if late else 0.0
    )
    meta = fixture.get("_meta") or {}
    effective_as_of = as_of_date or meta.get("as_of_date") or ""
    onboarded = customer.get("onboarded_date") or ""

    return {
        "customer_id": customer.get("customer_id", ""),
        "days_since_onboarded": days_between(onboarded, effective_as_of),
        "customer_name": customer.get("customer_name", ""),
        "prior_invoices": len(settled),
        "late_payment_count": len(late),
        "avg_days_late": average_days_late,
        "max_days_late": max((_days_late(invoice) for invoice in late), default=0),
        "escalations": sum(1 for invoice in invoices if invoice.get("walked_to_escalator")),
        "walked_to_escalator": any(invoice.get("walked_to_escalator") for invoice in invoices),
        "disputes": count_disputes(invoices),
        "partial_payments": count_partial_payments(invoices),
        "paid_early_count": sum(1 for invoice in settled if _days_late(invoice) < 0),
        "first_engagement": customer.get("onboarded_date") or "",
        "recent_excuses": recent_excuses(invoices),
        "as_of_date": effective_as_of,
    }
