from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Final, Literal

PatternTag = Literal[
    "new_customer",
    "clean_history",
    "first_time_late",
    "occasional_drift",
    "chronic_late",
    "escalation_history",
]

PATTERN_TAGS: Final[tuple[PatternTag, ...]] = (
    "new_customer",
    "clean_history",
    "first_time_late",
    "occasional_drift",
    "chronic_late",
    "escalation_history",
)

Tone = Literal["polite", "polite_firm"]
Day55VoiceAction = Literal["skip", "concierge_hitl", "auto_fire"]

TAG_TO_TONE: Final[dict[PatternTag, Tone]] = {
    "new_customer": "polite",
    "clean_history": "polite",
    "first_time_late": "polite",
    "occasional_drift": "polite",
    "chronic_late": "polite_firm",
    "escalation_history": "polite_firm",
}

TAG_TO_DAY55_VOICE: Final[dict[PatternTag, Day55VoiceAction]] = {
    "new_customer": "skip",
    "clean_history": "skip",
    "first_time_late": "concierge_hitl",
    "occasional_drift": "concierge_hitl",
    "chronic_late": "auto_fire",
    "escalation_history": "auto_fire",
}

NEW_CUSTOMER_DAYS: Final = 90
CHRONIC_LATE_COUNT: Final = 3
CHRONIC_LATE_AVG_DAYS: Final = 21
OCCASIONAL_DRIFT_AVG_DAYS: Final = 14
DEFAULT_LOOKBACK_MONTHS: Final = 6
DAYS_PER_MONTH: Final = 30


@dataclass(frozen=True, slots=True)
class InternalHistorySnapshot:
    customer_id: str
    lookback_months: int
    days_since_onboarded: int
    invoices_in_window: int
    late_payment_count: int
    avg_days_late: float
    max_days_late: int
    walked_to_escalator: bool
    paid_early_count: int
    previous_extensions_granted: int = 0
    has_partial_payment_pattern: bool = False
    sub_cycle_count_current_invoice: int = 0
    preferred_local_window: tuple[int, int] | None = None
    reply_window_sample_size: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "lookback_months": self.lookback_months,
            "days_since_onboarded": self.days_since_onboarded,
            "invoices_in_window": self.invoices_in_window,
            "late_payment_count": self.late_payment_count,
            "avg_days_late": round(self.avg_days_late, 2),
            "max_days_late": self.max_days_late,
            "walked_to_escalator": self.walked_to_escalator,
            "paid_early_count": self.paid_early_count,
            "previous_extensions_granted": self.previous_extensions_granted,
            "has_partial_payment_pattern": self.has_partial_payment_pattern,
            "sub_cycle_count_current_invoice": self.sub_cycle_count_current_invoice,
            "preferred_local_window": self.preferred_local_window,
            "reply_window_sample_size": self.reply_window_sample_size,
        }


def classify_pattern_tag(snapshot: InternalHistorySnapshot) -> PatternTag:
    if snapshot.days_since_onboarded < NEW_CUSTOMER_DAYS:
        return "new_customer"
    if snapshot.late_payment_count == 0:
        return "clean_history"
    if snapshot.walked_to_escalator:
        return "escalation_history"
    if (
        snapshot.late_payment_count >= CHRONIC_LATE_COUNT
        or snapshot.avg_days_late > CHRONIC_LATE_AVG_DAYS
    ):
        return "chronic_late"
    if snapshot.late_payment_count <= 2 and snapshot.avg_days_late < OCCASIONAL_DRIFT_AVG_DAYS:
        return "occasional_drift"
    return "first_time_late"


def tone_for(tag: str) -> Tone:
    return TAG_TO_TONE.get(tag, "polite")


def day55_voice_action(tag: str) -> Day55VoiceAction:
    return TAG_TO_DAY55_VOICE.get(tag, "concierge_hitl")


def should_trigger_critical_alert(tag: str, day_offset: int) -> bool:
    return tag == "escalation_history" and day_offset == 0


def _parse_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return dt.date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def _days_late(invoice: dict[str, Any]) -> int:
    try:
        return int(invoice.get("days_late") or 0)
    except (TypeError, ValueError):
        return 0


def aggregate_customer(
    customer: dict[str, Any],
    *,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
    as_of: dt.date | None = None,
) -> InternalHistorySnapshot:
    invoices = customer.get("invoices") or []
    issued_dates = [date for date in (_parse_date(inv.get("issued_date")) for inv in invoices) if date]

    if as_of is None:
        as_of = max(issued_dates) if issued_dates else dt.date.today()

    cutoff = as_of - dt.timedelta(days=lookback_months * DAYS_PER_MONTH)
    onboarded = _parse_date(customer.get("onboarded_date")) or as_of

    settled = [
        invoice
        for invoice in invoices
        if invoice.get("paid_date") is not None
        and (_parse_date(invoice.get("issued_date")) or cutoff) >= cutoff
    ]
    late = [invoice for invoice in settled if _days_late(invoice) > 0]

    return InternalHistorySnapshot(
        customer_id=str(customer.get("customer_id", "")),
        lookback_months=lookback_months,
        days_since_onboarded=(as_of - onboarded).days,
        invoices_in_window=len(settled),
        late_payment_count=len(late),
        avg_days_late=(sum(_days_late(inv) for inv in late) / len(late)) if late else 0.0,
        max_days_late=max((_days_late(inv) for inv in late), default=0),
        walked_to_escalator=any(inv.get("walked_to_escalator") for inv in settled),
        paid_early_count=sum(1 for inv in settled if _days_late(inv) < 0),
    )


def classify_customer(
    customer: dict[str, Any],
    *,
    lookback_months: int = DEFAULT_LOOKBACK_MONTHS,
    as_of: dt.date | None = None,
) -> tuple[PatternTag, InternalHistorySnapshot]:
    snapshot = aggregate_customer(customer, lookback_months=lookback_months, as_of=as_of)
    return classify_pattern_tag(snapshot), snapshot
