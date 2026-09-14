from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Final

from strands import Agent, tool
from strands.models.openai import OpenAIModel

from src.agents import _case_state
from src.agents._case_state import TERMINAL_STATUSES, load_case_state, update_case_state
from src.agents._utils import safe_float
from src.agents.base import audit
from src.concierge.cards import post_card
from src.diplomat.outbound import OutboundDispatchError, send_day7_reminder
from src.llm.provider import api_key, base_url, resolve_model

log = logging.getLogger("recoverly.agents.autonomous")

SYSTEM_PROMPT: Final = """
You are Recoverly, an autonomous accounts-receivable agent for small businesses.
Your job is to quietly handle repetitive invoice follow-up end to end.
At every cycle, call list_actionable_cases exactly once. For every returned case, call
the tool named in recommended_tool exactly once. Do not merely describe or recommend
those calls. Routine reminders may be sent only through auto_send_routine_reminder,
whose deterministic policy is authoritative. Use request_operator_decision for every
dispute, legal threat, anomaly, missing fact, high-value case, call, final notice, or
legal step. Never claim that an action succeeded unless its tool result says it did.
When no work is due, finish silently with a short machine-readable cycle summary.
""".strip()

ROUTINE_MIN_DAY: Final = 7
ROUTINE_MAX_DAY: Final = 14


def _today(value: str = "") -> date:
    if value:
        return date.fromisoformat(value)
    return datetime.now(timezone.utc).date()


def _days_past_due(case: dict[str, Any], reference: date) -> int | None:
    try:
        due = date.fromisoformat(str(case.get("due_date", "")))
    except ValueError:
        return None
    return (reference - due).days


def _max_auto_balance() -> float:
    return safe_float(os.getenv("AUTONOMOUS_MAX_BALANCE_USD"), 20000.0)


def _risk_reason(case: dict[str, Any], days: int | None) -> str:
    if case.get("status") in TERMINAL_STATUSES or case.get("halted_reason"):
        return "case is halted or closed"
    if case.get("anomaly_kind"):
        return "case has an anomaly"
    if case.get("dispute") or case.get("disputed") or case.get("legal_threat"):
        return "buyer raised a dispute or legal threat"
    if days is None:
        return "due date is missing or invalid"
    if safe_float(case.get("outstanding_balance_usd") or case.get("amount_balance_usd")) > _max_auto_balance():
        return "balance exceeds the autonomous-action limit"
    if not (case.get("customer_email") or case.get("buyer_email") or os.getenv("RECOVERLY_DEMO_BUYER_EMAIL")):
        return "buyer email is missing"
    if days > ROUTINE_MAX_DAY:
        return "case has reached a judgment-heavy escalation stage"
    return ""


def _candidate(case: dict[str, Any], reference: date) -> dict[str, Any] | None:
    case_id = str(case.get("case_id", ""))
    days = _days_past_due(case, reference)
    if (
        not case_id
        or days is None
        or days < ROUTINE_MIN_DAY
        or case.get("status") in {"closed_recovered", "closed_external"}
    ):
        return None
    if case.get("autonomous_last_action_date") == reference.isoformat():
        return None
    reason = _risk_reason(case, days)
    return {
        "case_id": case_id,
        "invoice_no": case.get("invoice_no", ""),
        "days_past_due": days,
        "outstanding_usd": safe_float(
            case.get("outstanding_balance_usd") or case.get("amount_balance_usd")
        ),
        "recommended_tool": "request_operator_decision" if reason else "auto_send_routine_reminder",
        "reason": reason or "routine day-7-to-day-14 reminder is policy-approved",
    }


@tool
def list_actionable_cases(reference_date: str = "") -> str:
    """Find recovery cases requiring work during this cycle.

    Args:
        reference_date: Optional UTC date in YYYY-MM-DD form.

    Returns:
        JSON with each case and the required next tool.
    """
    reference = _today(reference_date)
    candidates: list[dict[str, Any]] = []
    for path in sorted(_case_state.cases_dir().glob("*.json")):
        if path.name.endswith("_pairing.json"):
            continue
        candidate = _candidate(load_case_state(path.stem), reference)
        if candidate:
            candidates.append(candidate)
    return json.dumps({"date": reference.isoformat(), "cases": candidates})


@tool
def auto_send_routine_reminder(case_id: str, reference_date: str = "") -> str:
    """Send one policy-approved routine reminder and record the result.

    Args:
        case_id: Recoverly case identifier.
        reference_date: Optional UTC date in YYYY-MM-DD form.

    Returns:
        JSON describing whether the reminder was sent or blocked.
    """
    reference = _today(reference_date)
    case = load_case_state(case_id)
    days = _days_past_due(case, reference)
    reason = _risk_reason(case, days)
    if days is None or not ROUTINE_MIN_DAY <= days <= ROUTINE_MAX_DAY:
        reason = reason or "case is outside the routine reminder window"
    if case.get("autonomous_last_reminder_date") == reference.isoformat():
        reason = "a reminder has already been sent today"
    if reason:
        audit(case_id, "recoverly", "autonomous_action_blocked", {"reason": reason})
        return json.dumps({"ok": False, "status": "blocked", "reason": reason})
    prepared = {**case, "days_past_due": days}
    try:
        result = send_day7_reminder(prepared)
    except (OutboundDispatchError, ValueError) as error:
        audit(case_id, "recoverly", "autonomous_reminder_failed", {"error": str(error)[:500]})
        return json.dumps({"ok": False, "status": "failed", "error": str(error)[:500]})
    update_case_state(
        case_id,
        status="active",
        current_stage="awaiting_payment",
        days_past_due=days,
        autonomous_last_action_date=reference.isoformat(),
        autonomous_last_reminder_date=reference.isoformat(),
        autonomous_last_message_id=result.message_id,
    )
    audit(case_id, "recoverly", "autonomous_reminder_sent", {"message_id": result.message_id, "days_past_due": days})
    return json.dumps({"ok": True, "status": "sent", "message_id": result.message_id})


@tool
def request_operator_decision(case_id: str, reason: str, reference_date: str = "") -> str:
    """Ask the operator in Slack to decide a judgment-heavy recovery action.

    Args:
        case_id: Recoverly case identifier.
        reason: Concise reason a person must decide.
        reference_date: Optional UTC date in YYYY-MM-DD form.

    Returns:
        JSON describing the Slack decision request.
    """
    reference = _today(reference_date)
    case = load_case_state(case_id)
    if case.get("autonomous_last_decision_request_date") == reference.isoformat():
        return json.dumps({"ok": True, "status": "already_requested"})
    summary = f"Recoverly needs a decision on invoice {case.get('invoice_no') or case_id}."
    body = f"*Reason*: {reason}\n*Outstanding*: ${safe_float(case.get('outstanding_balance_usd') or case.get('amount_balance_usd')):,.2f}\n\nReview the evidence and approve, revise, or reject the next action."
    result = post_card(case_id, summary, body, case_meta=case)
    if not result.ok:
        return json.dumps({"ok": False, "status": "failed", "error": result.error})
    update_case_state(
        case_id,
        current_stage="hitl_pending",
        autonomous_last_action_date=reference.isoformat(),
        autonomous_last_decision_request_date=reference.isoformat(),
    )
    audit(case_id, "recoverly", "operator_decision_requested", {"reason": reason})
    return json.dumps({"ok": True, "status": "requested", "channel": "slack"})


def enabled() -> bool:
    value = os.getenv("AUTONOMOUS_AGENT_ENABLED", "1").strip().lower()
    strands = os.getenv("STRANDS_ENABLED", "1").strip().lower()
    return value in {"1", "true", "yes", "on"} and strands in {"1", "true", "yes", "on"} and bool(api_key())


def build_agent() -> Agent:
    model = OpenAIModel(
        client_args={"api_key": api_key(), "base_url": base_url()},
        model_id=resolve_model("concierge"),
        params={"max_tokens": int(os.getenv("STRANDS_MAX_TOKENS", "800")), "temperature": 0.1},
    )
    return Agent(
        model=model,
        tools=[list_actionable_cases, auto_send_routine_reminder, request_operator_decision],
        system_prompt=SYSTEM_PROMPT,
        callback_handler=None,
        agent_id="recoverly-autonomous",
        name="Recoverly",
        description="Autonomous accounts-receivable follow-up agent",
    )


def run_cycle(reference_date: str = "") -> dict[str, Any]:
    if not enabled():
        return {"ok": False, "status": "disabled"}
    cycle_id = datetime.now(timezone.utc).isoformat()
    prompt = f"Run the complete recovery cycle for UTC date {reference_date or _today().isoformat()}. Use the tools now."
    try:
        result = build_agent()(prompt)
        output = str(result).strip()
        audit("SYSTEM", "recoverly", "autonomous_cycle_completed", {"cycle_id": cycle_id, "output": output[:1000]})
        return {"ok": True, "status": "completed", "output": output}
    except Exception as error:
        log.exception("autonomous Recoverly cycle failed")
        audit("SYSTEM", "recoverly", "autonomous_cycle_failed", {"cycle_id": cycle_id, "error": str(error)[:500]})
        return {"ok": False, "status": "failed", "error": str(error)[:500]}
