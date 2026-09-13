from __future__ import annotations

import json
import sys
from typing import Any, Final

from src.agents._band_push import push_card
from src.agents._case_state import add_cost, is_halted
from src.agents._utils import is_error_envelope, parse_json_object
from src.agents.base import BandAgentAdapter, audit, run_agent
from src.investigator.pattern_tag import (
    PATTERN_TAGS,
    InternalHistorySnapshot,
    classify_customer,
    classify_pattern_tag,
    tone_for,
)
from src.llm.provider import complete_async

ESCALATOR_HANDOFF_DAYS: Final = 60
MAX_RATIONALE_TOKENS: Final = 200
NET_TERMS_DAYS: Final = 30

SYSTEM_PROMPT: Final = (
    "You are the Investigator agent for Recoverly, a B2B invoice recovery system. "
    "Your entire response must be a single JSON object with no prose, no preamble and "
    "no markdown fences. "
    'Schema: {"pattern_tag":"<one of ' + "|".join(PATTERN_TAGS) + '>",'
    '"rationale":"<one sentence under 30 words>"}. '
    "Definitions: "
    "new_customer means fewer than 90 days of relationship or under two prior invoices; "
    "clean_history means no late payments in the lookback window; "
    "first_time_late means exactly one prior late payment; "
    "occasional_drift means one or two prior late payments averaging under 14 days late; "
    "chronic_late means three or more late payments or an average over 21 days late; "
    "escalation_history means the customer has previously reached day 60 or later. "
    "Start your response with { and end with }."
)


def _snapshot_from_summary(case_id: str, history: dict[str, Any]) -> InternalHistorySnapshot:
    def number(key: str, default: float = 0.0) -> float:
        try:
            return float(history.get(key) or default)
        except (TypeError, ValueError):
            return default

    prior_invoices = int(number("prior_invoices"))
    escalations = int(number("escalations"))
    avg_days_late = number("avg_days_late", max(0.0, number("avg_days_to_pay") - NET_TERMS_DAYS))

    if "late_payment_count" in history:
        late_count = int(number("late_payment_count"))
    elif avg_days_late > 0:
        late_count = max(1, prior_invoices)
    else:
        late_count = escalations

    return InternalHistorySnapshot(
        customer_id=str(history.get("customer_id") or case_id),
        lookback_months=int(number("lookback_months", 6)),
        days_since_onboarded=int(number("days_since_onboarded", 0 if prior_invoices < 2 else 365)),
        invoices_in_window=prior_invoices,
        late_payment_count=late_count,
        avg_days_late=avg_days_late,
        max_days_late=int(number("max_days_late", avg_days_late)),
        walked_to_escalator=bool(history.get("walked_to_escalator") or escalations > 0),
        paid_early_count=int(number("paid_early_count")),
        has_partial_payment_pattern=bool(number("partial_payments") > 0),
    )


def classify_history(case_id: str, history: dict[str, Any]) -> tuple[str, InternalHistorySnapshot]:
    if history.get("invoices"):
        tag, snapshot = classify_customer({"customer_id": case_id, **history})
        return tag, snapshot

    snapshot = _snapshot_from_summary(case_id, history)
    return classify_pattern_tag(snapshot), snapshot


def _summary_card(case_id: str, tag: str, rationale: str, snapshot: InternalHistorySnapshot) -> str:
    return (
        f"**Investigation summary** — {snapshot.lookback_months} months of customer history reviewed.\n\n"
        f"- Pattern tag: `{tag}`\n"
        f"- Suggested opening tone: `{tone_for(tag)}`\n"
        f"- Rationale: {rationale}\n"
        f"- Invoices in window: {snapshot.invoices_in_window}\n"
        f"- Late payments: {snapshot.late_payment_count}\n"
        f"- Average days late: {snapshot.avg_days_late:.1f}\n"
        f"- Reached escalation before: {'yes' if snapshot.walked_to_escalator else 'no'}\n"
    )


def _next_target(case: dict[str, Any]) -> str:
    try:
        days_past_due = int(case.get("days_past_due") or 0)
    except (TypeError, ValueError):
        days_past_due = 0
    return "escalator" if days_past_due >= ESCALATOR_HANDOFF_DAYS else "diplomat"


class InvestigatorAdapter(BandAgentAdapter):
    role = "investigator"

    async def handle_message(self, text, msg, tools, history, room_id):
        if is_error_envelope(text):
            return None

        case = parse_json_object(text)
        if not case or not case.get("case_id"):
            return None

        case_id = str(case["case_id"])
        if is_halted(case_id):
            self.log.info("case %s is halted, skipping", case_id)
            return None

        customer_history = case.get("internal_history") or {}
        audit(case_id, self.role, "investigator_start", {"history": customer_history})

        tag, snapshot = classify_history(case_id, customer_history)
        rationale, usage = await self._rationale(case_id, tag, snapshot)

        if usage:
            add_cost(case_id, usage)

        audit(
            case_id,
            self.role,
            "investigator_done",
            {
                "pattern_tag": tag,
                "rationale": rationale,
                "snapshot": snapshot.as_dict(),
                "usage": usage,
            },
        )

        push_result = push_card(
            sender_role=self.role,
            case_id=case_id,
            card_kind="investigation_summary",
            body=_summary_card(case_id, tag, rationale, snapshot),
        )
        audit(case_id, self.role, "investigator_summary_card", push_result.as_dict())

        enriched = {
            **case,
            "pattern_tag": tag,
            "pattern_rationale": rationale,
            "suggested_tone": tone_for(tag),
            "internal_history_snapshot": snapshot.as_dict(),
        }
        target = _next_target(case)

        return (
            f"@{target} Investigator tagged case `{case_id}` as **{tag}** — {rationale}\n"
            f"Suggested opening tone: `{tone_for(tag)}`.\n"
            f"Enriched case payload:\n```json\n{json.dumps(enriched, indent=2)}\n```"
        )

    async def _rationale(
        self, case_id: str, tag: str, snapshot: InternalHistorySnapshot
    ) -> tuple[str, dict[str, Any] | None]:
        prompt = (
            f"Case {case_id} was classified as pattern_tag={tag} by the deterministic "
            f"classifier. Write the one-sentence rationale that explains that tag from "
            f"these facts:\n{json.dumps(snapshot.as_dict(), indent=2)}\n"
            f"Return the tag exactly as given."
        )
        try:
            raw, usage = await complete_async(
                SYSTEM_PROMPT, prompt, agent_role=self.role, max_tokens=MAX_RATIONALE_TOKENS
            )
        except Exception as error:
            audit(case_id, self.role, "llm_error", {"error": str(error)})
            return _deterministic_rationale(tag, snapshot), None

        parsed = parse_json_object(raw) or {}
        rationale = str(parsed.get("rationale") or "").strip()
        if not rationale:
            audit(case_id, self.role, "investigator_rationale_fallback", {"raw": raw[:200]})
            return _deterministic_rationale(tag, snapshot), usage

        return rationale, usage


def _deterministic_rationale(tag: str, snapshot: InternalHistorySnapshot) -> str:
    if tag == "new_customer":
        return f"Only {snapshot.days_since_onboarded} days of relationship history so far."
    if tag == "clean_history":
        return f"No late payments across {snapshot.invoices_in_window} settled invoices."
    if tag == "escalation_history":
        return "This customer has previously been walked to escalation."
    if tag == "chronic_late":
        return (
            f"{snapshot.late_payment_count} late payments averaging "
            f"{snapshot.avg_days_late:.0f} days late."
        )
    if tag == "occasional_drift":
        return (
            f"{snapshot.late_payment_count} mild late payments averaging "
            f"{snapshot.avg_days_late:.0f} days late."
        )
    return f"One prior late payment of {snapshot.max_days_late} days."


if __name__ == "__main__":
    sys.exit(run_agent(InvestigatorAdapter))
