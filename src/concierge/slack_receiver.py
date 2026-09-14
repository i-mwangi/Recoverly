from __future__ import annotations

import logging
from typing import Any, Final

from src.concierge.actions import dispatch
from src.concierge.actions.base import build_context
from src.concierge.strands_relay import notify_agent
from src.utils.sanitize import strip_slack_mentions
from src.voice.security import verify_slack_signature

log = logging.getLogger("recoverly.concierge.slack_receiver")

VIEW_SUBMISSION: Final = "view_submission"
BLOCK_ACTIONS: Final = "block_actions"


def verify_signature(timestamp: str | None, body: bytes, signature: str | None, secret: str) -> bool:
    return verify_slack_signature(body, timestamp, signature, secret)


def dispatch_block_action(payload: dict[str, Any]) -> dict[str, Any]:
    actions = payload.get("actions") or []
    if not actions:
        return {"ok": True, "text": "no action in payload"}

    action = actions[0]
    context = build_context(action, payload)
    result = dispatch(action, payload)

    log.info(
        "operator %s pressed %s on case %s: %s",
        context.user,
        context.action_id,
        context.case_id,
        result.text[:120],
    )
    return {"ok": True, "text": result.text, "seal_card": result.seal_card}


def _first_input_value(view: dict[str, Any]) -> str:
    values = (view.get("state") or {}).get("values") or {}
    for block in values.values():
        for element in block.values():
            if isinstance(element, dict) and element.get("value"):
                return str(element["value"])
    return ""


def dispatch_view_submission(payload: dict[str, Any]) -> dict[str, Any]:
    view = payload.get("view") or {}
    callback_id = str(view.get("callback_id", ""))
    case_id = callback_id.split("|", 1)[1] if "|" in callback_id else ""
    user = str((payload.get("user") or {}).get("username", "unknown"))
    note = strip_slack_mentions(_first_input_value(view))

    log.info(
        "modal %s submitted by %s on case %s: %s",
        callback_id,
        user,
        case_id,
        note[:120] or "no note",
    )

    if callback_id.startswith(("revise_voice_dial_", "revise_aaa_letter_", "revise_diplomat_draft_")):
        notify_agent(
            "tone_coach",
            f"event=revise_note_review for case `{case_id}` operator_note: \"{note}\"",
            case_id=case_id,
        )
        notify_agent(
            "diplomat",
            f"operator requested revision for case `{case_id}` _operator_note_={note}_",
            case_id=case_id,
        )

    return {"ok": True, "case_id": case_id, "note": note}


def dispatch_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payload_type = payload.get("type")

    if payload_type == BLOCK_ACTIONS:
        return dispatch_block_action(payload)

    if payload_type == VIEW_SUBMISSION:
        return dispatch_view_submission(payload)

    log.info("ignoring an unsupported Slack payload type: %s", payload_type)
    return {"ok": True, "ignored": payload_type}
