from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from typing import Any, Callable, Final

from flask import Blueprint, Response, jsonify, request

from src.agents._case_state import (
    InvalidCaseIdError,
    is_halted,
    load_case_state,
    stop_case,
    update_case_state,
    validate_case_id,
)
from src.agents._utils import safe_float
from src.agents.base import audit
from src.concierge.strands_relay import notify_agent
from src.concierge.cards import post_card
from src.config import settings
from src.voice.security import strict_mode, verify_slack_signature

log = logging.getLogger("recoverly.concierge.routes.commands")

blueprint = Blueprint("slack_commands", __name__)

STRICT_FLAG: Final = "SLACK_STRICT_SIGNATURE"
SIGNATURE_HEADER: Final = "X-Slack-Signature"
TIMESTAMP_HEADER: Final = "X-Slack-Request-Timestamp"
AGENT: Final = "concierge"
RECENT_EVENT_COUNT: Final = 5

KEY_VALUE_RE: Final = re.compile(r'(\w+)=(?:"([^"]*)"|(\S*))')


def _ephemeral(text: str, status: int = 200) -> tuple[Response, int]:
    return jsonify({"response_type": "ephemeral", "text": text}), status


def _verify(raw: bytes) -> tuple[Response, int] | None:
    secret = os.getenv("SLACK_SIGNING_SECRET", "").strip()

    if not secret:
        if strict_mode(STRICT_FLAG):
            return _ephemeral("The Slack signing secret is not configured.", 503)
        return None

    if not verify_slack_signature(
        raw,
        request.headers.get(TIMESTAMP_HEADER),
        request.headers.get(SIGNATURE_HEADER),
        secret,
    ):
        return _ephemeral("That request could not be verified.", 401)

    return None


def parse_params(text: str) -> dict[str, str]:
    parsed = {"case": "", "reason": "", "amount": "", "raw": text}
    if not text:
        return parsed

    pairs = {
        match.group(1).lower(): (match.group(2) or match.group(3) or "").strip()
        for match in KEY_VALUE_RE.finditer(text)
    }
    if pairs:
        parsed.update(
            {
                key: pairs.get(key, "")
                for key in ("case", "reason", "amount", "reference")
            }
        )
        return parsed

    parts = text.split(maxsplit=1)
    parsed["case"] = parts[0] if parts else ""
    parsed["reason"] = parts[1] if len(parts) > 1 else ""
    return parsed


def recent_events(
    case_id: str, limit: int = RECENT_EVENT_COUNT
) -> list[dict[str, Any]]:
    path = settings.data.audit_trail_jsonl
    if not path.exists():
        return []

    matched: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("case_id") == case_id:
                matched.append(event)

    return matched[-limit:]


def _command(handler: Callable[[str, dict[str, str]], tuple[Response, int]]):
    def view() -> tuple[Response, int]:
        raw = request.get_data()

        rejection = _verify(raw)
        if rejection:
            return rejection

        form = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace")))
        operator = form.get("user_name", "unknown")
        params = parse_params(form.get("text", ""))

        if not params["case"]:
            return _ephemeral("Include a case, for example `case=RC-2026-0042`.", 400)

        try:
            validate_case_id(params["case"])
        except InvalidCaseIdError as error:
            return _ephemeral(f"That case reference is not valid: {error}", 400)

        return handler(operator, params)

    view.__name__ = handler.__name__
    return view


def _unhold(operator: str, params: dict[str, str]) -> tuple[Response, int]:
    case_id = params["case"]
    reason = params["reason"] or "manual unhold"

    if not is_halted(case_id):
        return _ephemeral(f"Case `{case_id}` is not on hold.")

    update_case_state(case_id, status="active", halted_reason=None, anomaly_kind=None)
    audit(
        case_id, AGENT, "case_unhold_manual", {"operator": operator, "reason": reason}
    )
    return _ephemeral(
        f"Case `{case_id}` is off hold and back in the cadence. Reason: {reason}"
    )


def _reopen(operator: str, params: dict[str, str]) -> tuple[Response, int]:
    case_id = params["case"]
    reason = params["reason"] or "manual reopen"

    update_case_state(
        case_id,
        status="active",
        current_stage="awaiting_payment",
        halted_reason=None,
        anomaly_kind=None,
    )
    audit(
        case_id, AGENT, "case_reopen_manual", {"operator": operator, "reason": reason}
    )
    return _ephemeral(f"Case `{case_id}` is reopened. Reason: {reason}")


def _status(operator: str, params: dict[str, str]) -> tuple[Response, int]:
    case_id = params["case"]
    state = load_case_state(case_id)

    outstanding = safe_float(
        state.get("outstanding_balance_usd") or state.get("amount_balance_usd"), 0.0
    )
    history = (
        "\n".join(
            f"• `{event.get('event', 'unknown')}` at {str(event.get('ts', ''))[:19]}"
            for event in recent_events(case_id)
        )
        or "_no audit events yet_"
    )

    audit(case_id, AGENT, "case_status_queried", {"operator": operator})
    return _ephemeral(
        f"*Case `{case_id}`*\n"
        f"• Status: `{state.get('status', 'unknown')}`\n"
        f"• Stage: `{state.get('current_stage', 'unknown')}`\n"
        f"• Outstanding: `${outstanding:,.2f}`\n"
        f"• Cadence day: `{state.get('cadence_day', 0)}`\n"
        f"• Cost so far: `${safe_float(state.get('cost_usd'), 0.0):.4f}`\n"
        f"• On hold: `{bool(state.get('halted_reason'))}`\n\n"
        f"*Recent events*\n{history}"
    )


def _payment_received(operator: str, params: dict[str, str]) -> tuple[Response, int]:
    case_id = params["case"]
    from src.payments.reconciler import reconcile_inbound

    reference = params.get("reference", "")
    if not params.get("amount") or not reference:
        return _ephemeral(
            "Use case=RC-... amount=100 reference=BANK-TRANSACTION-ID; amount and reference are required.",
            400,
        )
    result = reconcile_inbound(case_id, params["amount"], "manual:" + reference)
    amount = result.amount_applied_usd
    audit(
        case_id,
        AGENT,
        "payment_received",
        {"operator": operator, "amount_usd": amount, "channel": "slash_command"},
    )
    notify_agent(
        "payment",
        f'{{"event": "payment_received", "case_id": "{case_id}", "reconciled": true}}',
        case_id=case_id,
    )
    return _ephemeral(
        f"Recorded ${amount:,.2f} received on case `{case_id}`: {result.status.value}. "
        f"Remaining balance: ${result.remaining_usd:,.2f}."
    )


def _stop(operator: str, params: dict[str, str]) -> tuple[Response, int]:
    case_id = params["case"]
    reason = params["reason"] or "operator stopped the case"

    stop_case(case_id, reason)
    audit(
        case_id, AGENT, "case_stopped_manual", {"operator": operator, "reason": reason}
    )
    return _ephemeral(f"Case `{case_id}` is stopped. Reason: {reason}")

def _voice_review(operator: str, params: dict[str, str]) -> tuple[Response, int]:
    case_id = params["case"]
    case = load_case_state(case_id)
    update_case_state(case_id, current_stage="voice_dial_review")
    outstanding = safe_float(case.get("outstanding_balance_usd") or case.get("amount_balance_usd"), 0.0)
    buyer = str(case.get("customer_name") or "the buyer")
    invoice = str(case.get("invoice_no") or "the outstanding invoice")
    result = post_card(case_id, f"Voice Agent is ready to call {buyer}.", f"*Buyer*: {buyer}\n*Invoice*: {invoice}\n*Outstanding*: ${outstanding:,.2f}\n\nApprove call to dial, revise script to change the wording, or reject to keep the normal cadence.", card_kind="voice_dial", case_meta=case)
    if not result.ok:
        return _ephemeral(f"Could not post the voice review card: {result.error}", 502)
    audit(case_id, AGENT, "voice_review_requested", {"operator": operator, "slack_ts": result.ts})
    return _ephemeral(f"Voice review card posted for `{case_id}`.")



COMMAND_ROUTES: Final[
    dict[str, Callable[[str, dict[str, str]], tuple[Response, int]]]
] = {
    "/slack/commands/case-unhold": _unhold,
    "/slack/commands/case-reopen": _reopen,
    "/slack/commands/case-status": _status,
    "/slack/commands/case-stop": _stop,
    "/slack/commands/payment-received": _payment_received,
    "/slack/commands/voice-review": _voice_review,
}

for path, command_handler in COMMAND_ROUTES.items():
    blueprint.add_url_rule(path, view_func=_command(command_handler), methods=["POST"])
