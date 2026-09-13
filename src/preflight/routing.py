from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from src.preflight.amount_router import PathDecision

ESCALATOR_DAY: Final = 60
VOICE_DIAL_DAY: Final = 55

DEFAULT_LITE_PATTERN_TAG: Final = "clean_history"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    target: str
    note: str
    card_kind: str | None = None
    extra_fields: dict[str, Any] | None = None

    def enrich(self, case: dict[str, Any], path: PathDecision) -> dict[str, Any]:
        enriched = {
            **case,
            "path_mode": path.mode,
            "path_band": path.band_label,
            "outstanding_balance_usd": path.outstanding_balance_usd,
        }
        if self.card_kind:
            enriched["card_kind"] = self.card_kind
        if self.extra_fields:
            enriched.update(self.extra_fields)
        return enriched


def days_past_due(case: dict[str, Any]) -> int:
    try:
        return int(case.get("days_past_due") or 0)
    except (TypeError, ValueError):
        return 0


def route_case(case: dict[str, Any], path: PathDecision) -> RouteDecision:
    attorney_tag = bool(case.get("attorney_tag"))
    lite_decided = bool(case.get("lite_decision"))
    overdue = days_past_due(case)

    if path.mode == "attorney_recommended" and not attorney_tag:
        return RouteDecision(
            target="concierge",
            note="above the sweet-spot ceiling, so the operator decides how to proceed",
            card_kind="attorney_recommended",
        )

    if path.mode == "lite" and not lite_decided:
        return RouteDecision(
            target="concierge",
            note="below the sweet-spot floor, so the operator chooses between a final notice and a write-off",
            card_kind="lite",
        )

    if attorney_tag:
        return RouteDecision(
            target="investigator",
            note="operator tagged this for attorney follow-up, so it stays in the cadence for enrichment first",
        )

    if overdue >= ESCALATOR_DAY:
        return RouteDecision(
            target="escalator",
            note=f"day {overdue} hard escalation, so the escalator drafts the demand letter",
        )

    if overdue >= VOICE_DIAL_DAY:
        return RouteDecision(
            target="voice",
            note=f"day {overdue} falls in the voice dial window",
        )

    if path.mode == "lite":
        return RouteDecision(
            target="diplomat",
            note="lite mode confirmed by the operator, so the diplomat sends the final notice",
            extra_fields={
                "pattern_tag": DEFAULT_LITE_PATTERN_TAG,
                "pattern_rationale": "lite mode default; the investigator step is skipped",
            },
        )

    return RouteDecision(
        target="investigator",
        note="inside the sweet spot, so the full pipeline runs",
    )
