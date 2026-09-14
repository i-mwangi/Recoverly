from __future__ import annotations

import logging
import math
from typing import Any, Callable, Final

from src.concierge.cards import post_card
from src.voice.events import log_voice_event, queue_agent_event

log = logging.getLogger("recoverly.voice.tools")

CASE_ROOM: Final = "case_room"

ALLOWED_EXCUSE_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "cash_flow",
        "quality_dispute",
        "approval_workflow",
        "boss_away",
        "check_in_mail",
        "already_paid",
        "never_received",
        "pay_when_paid",
        "disputed_authorization",
        "asked_for_waiver",
        "discount_proposed",
        "partial_dispute",
        "mentioned_other_invoice",
        "corporate_change",
        "inappropriate_comment",
        "payment_method_constraint",
    }
)

ALLOWED_ESCALATION_REASONS: Final[frozenset[str]] = frozenset(
    {
        "do_not_call_request",
        "recording_consent_refused",
        "wrong_person_reached",
        "no_payment_commitment",
        "silent_disconnect",
        "language_mismatch",
        "voicemail_left",
        "legal_counsel_engaged",
        "customer_impaired",
        "welfare_concern",
        "incomplete_call",
        "hostile_persistent",
        "customer_committed_to_followup",
        "bankruptcy_filing_disclosed",
        "customer_emotional_distress",
        "customer_recording_for_publish",
    }
)

ARBITRATION_TRIGGER_REASONS: Final[frozenset[str]] = frozenset(
    {
        "legal_counsel_engaged",
        "hostile_persistent",
        "do_not_call_request",
        "customer_recording_for_publish",
    }
)

WELFARE_REASONS: Final[frozenset[str]] = frozenset(
    {"welfare_concern", "customer_emotional_distress"}
)

ToolResult = dict[str, Any]
ToolHandler = Callable[[str, dict[str, Any]], ToolResult]


def _ok(**extra: Any) -> ToolResult:
    return {"ok": True, **extra}


def _error(message: str) -> ToolResult:
    return {"ok": False, "error": message}


def _positive_amount(value: Any) -> float | None:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount) or amount <= 0:
        return None
    return amount


def _post_sitrep(case_id: str, card_kind: str, summary: str, body: str) -> None:
    result = post_card(case_id, summary, body, card_kind=card_kind)
    log_voice_event(
        "voice_card_posted",
        {"card_kind": card_kind, **result.as_dict()},
        case_id=case_id,
    )


def log_excuse(case_id: str, params: dict[str, Any]) -> ToolResult:
    category = params.get("category")
    if category not in ALLOWED_EXCUSE_CATEGORIES:
        return _error(f"unknown excuse category: {category!r}")

    detail = str(params.get("detail", ""))
    log_voice_event(
        "voice_excuse_logged", {"category": category, "detail": detail}, case_id=case_id
    )
    queue_agent_event(
        case_id,
        CASE_ROOM,
        {"event": "excuse_logged_from_voice", "category": category, "detail": detail},
    )
    return _ok(category=category)


def propose_payment_date(case_id: str, params: dict[str, Any]) -> ToolResult:
    commitment_date = params.get("date")
    amount = _positive_amount(params.get("amount"))

    if not commitment_date:
        return _error("a commitment date is required")
    if amount is None:
        return _error(f"amount must be a finite positive number, got {params.get('amount')!r}")

    log_voice_event(
        "voice_payment_date_proposed",
        {"date": commitment_date, "amount_usd": amount},
        case_id=case_id,
    )
    queue_agent_event(
        case_id,
        CASE_ROOM,
        {
            "event": "payment_date_proposed_from_voice",
            "date": commitment_date,
            "amount_usd": amount,
        },
    )
    _post_sitrep(
        case_id,
        "voice_commitment_summary",
        f"The buyer committed to pay ${amount:,.2f} by {commitment_date}.",
        (
            f"*Commitment date*: {commitment_date}\n"
            f"*Amount*: ${amount:,.2f}\n"
            f"*Source*: voice call\n\n"
            f"No operator action is needed; the follow-up reminder is scheduled."
        ),
    )
    return _ok(date=commitment_date, amount_usd=amount)


def request_callback_to_human(case_id: str, params: dict[str, Any]) -> ToolResult:
    log_voice_event("voice_human_callback_requested", {}, case_id=case_id)
    queue_agent_event(
        case_id, CASE_ROOM, {"event": "human_callback_requested_from_voice"}
    )
    _post_sitrep(
        case_id,
        "voice_call_summary",
        "The buyer asked to speak with a person.",
        "*Source*: voice call\n*Action*: an operator needs to call back.",
    )
    return _ok()


def escalate_to_concierge(case_id: str, params: dict[str, Any]) -> ToolResult:
    reason = params.get("reason")
    if reason not in ALLOWED_ESCALATION_REASONS:
        return _error(f"unknown escalation reason: {reason!r}")

    log_voice_event("voice_escalated", {"reason": reason}, case_id=case_id)
    queue_agent_event(
        case_id, CASE_ROOM, {"event": "escalated_from_voice", "reason": reason}
    )

    if reason in WELFARE_REASONS:
        card_kind = "welfare_critical"
        summary = "The call raised a welfare concern and the operator has been paged."
        body = (
            f"*Reason*: `{reason}`\n*Source*: voice call\n\n"
            f"The agent read the support-line script and placed the account on hold. "
            f"An operator owns this case from here."
        )
    elif reason in ARBITRATION_TRIGGER_REASONS:
        card_kind = "voice_aaa_escalation"
        summary = f"The call escalated for arbitration review: {reason}."
        body = (
            f"*Reason*: `{reason}`\n*Source*: voice call\n\n"
            f"Approve to hand the case to the arbitration specialist for a strategy and "
            f"demand letter draft."
        )
    else:
        card_kind = "voice_call_summary"
        summary = f"The call ended without a commitment: {reason}."
        body = f"*Reason*: `{reason}`\n*Source*: voice call"

    _post_sitrep(case_id, card_kind, summary, body)
    return _ok(reason=reason, card_kind=card_kind)


TOOL_HANDLERS: Final[dict[str, ToolHandler]] = {
    "log_excuse": log_excuse,
    "propose_payment_date": propose_payment_date,
    "request_callback_to_human": request_callback_to_human,
    "escalate_to_concierge": escalate_to_concierge,
}


def handle_tool_call(case_id: str, tool_name: str, params: dict[str, Any]) -> ToolResult:
    handler = TOOL_HANDLERS.get(tool_name)
    if handler is None:
        return _error(f"unknown tool: {tool_name!r}")

    try:
        return handler(case_id, params or {})
    except Exception as error:
        log.exception("voice tool %s failed for case %s", tool_name, case_id)
        log_voice_event("voice_tool_error", {"tool": tool_name, "error": str(error)}, case_id=case_id)
        return _error(f"{tool_name} failed: {error}")
