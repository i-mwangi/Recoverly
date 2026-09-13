from __future__ import annotations

import json
import logging
import os
import urllib.parse
from typing import Any, Final

from flask import Blueprint, Response, jsonify, request

from src.concierge.actions import dispatch
from src.concierge.actions.base import build_context, clean_note
from src.concierge.band_relay import notify_agent
from src.concierge.slack_client import open_revision_modal
from src.config import settings
from src.voice.events import log_voice_event
from src.voice.security import strict_mode, verify_slack_signature

log = logging.getLogger("recoverly.concierge.routes.interactivity")

blueprint = Blueprint("slack_interactivity", __name__)

STRICT_FLAG: Final = "SLACK_STRICT_SIGNATURE"
SIGNATURE_HEADER: Final = "X-Slack-Signature"
TIMESTAMP_HEADER: Final = "X-Slack-Request-Timestamp"

BLOCK_ACTIONS: Final = "block_actions"
VIEW_SUBMISSION: Final = "view_submission"

REVISE_ACTION_TARGETS: Final = {
    "hitl_revise": "email",
    "revise_voice_dial_": "voice",
    "revise_aaa_letter_": "aaa",
    "revise_diplomat_draft_": "email",
}


def _json_error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


def _verify(raw: bytes) -> tuple[Response, int] | None:
    secret = os.getenv("SLACK_SIGNING_SECRET", "").strip()

    if not secret:
        if strict_mode(STRICT_FLAG):
            log.warning("rejecting a Slack callback because no signing secret is configured")
            return _json_error("the Slack signing secret is not configured", 503)
        return None

    if not verify_slack_signature(
        raw,
        request.headers.get(TIMESTAMP_HEADER),
        request.headers.get(SIGNATURE_HEADER),
        secret,
    ):
        log.warning("rejecting a Slack callback with an invalid signature")
        return _json_error("unauthorized", 401)

    return None


def _parse_payload(raw: bytes) -> dict[str, Any] | None:
    form = dict(urllib.parse.parse_qsl(raw.decode("utf-8", "replace")))
    try:
        payload = json.loads(form.get("payload", "{}"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _case_id_from_callback(callback_id: str) -> str:
    parts = callback_id.split("|")
    return parts[-1] if len(parts) >= 2 else ""


def _submitted_note(view: dict[str, Any]) -> str:
    values = (view.get("state") or {}).get("values") or {}
    for block in values.values():
        for element in block.values():
            if isinstance(element, dict) and element.get("value"):
                return str(element["value"])
    return ""


def _revision_target(callback_id: str) -> str:
    parts = callback_id.split("|", 2)
    return parts[1] if len(parts) == 3 else ""


def _handle_view_submission(payload: dict[str, Any]) -> tuple[Response, int]:
    view = payload.get("view") or {}
    callback_id = str(view.get("callback_id", ""))
    case_id = _case_id_from_callback(callback_id)
    target = _revision_target(callback_id)
    user = str((payload.get("user") or {}).get("username", "unknown"))
    note = clean_note(_submitted_note(view))

    log_voice_event(
        "slack_modal_submitted",
        {"callback_id": callback_id, "operator": user, "note": note[:300]},
        case_id=case_id or "UNKNOWN",
    )

    if target == "aaa":
        notify_agent(
            "aaa_specialist",
            f"operator requested revision for case `{case_id}` _operator_note_={note}_",
            case_id=case_id,
        )
    elif target == "voice":
        notify_agent(
            "voice",
            f"event=revised_voice_script for case `{case_id}` operator_note: \"{note}\"",
            case_id=case_id,
        )
    elif target == "email":
        notify_agent(
            "tone_coach",
            f"event=revise_note_review for case `{case_id}`\noperator_note: \"{note}\"",
            case_id=case_id,
        )
        notify_agent(
            "diplomat",
            f"operator requested revision for case `{case_id}` _operator_note_={note}_",
            case_id=case_id,
        )

    return jsonify({"response_action": "clear"}), 200


def _handle_block_actions(payload: dict[str, Any]) -> tuple[Response, int]:
    actions = payload.get("actions") or []
    if not actions:
        return jsonify({"ok": True}), 200

    action = actions[0]
    action_id = str(action.get("action_id", ""))
    for prefix, target in REVISE_ACTION_TARGETS.items():
        if action_id == prefix or action_id.startswith(prefix):
            context = build_context(action, payload)
            result = open_revision_modal(str(payload.get("trigger_id", "")), context.case_id, target)
            if not result.ok:
                return _json_error("Could not open the revision form in Slack.", 502)
            context.audit("operator_opened_revision_form", {"target": target})
            return jsonify({"ok": True}), 200

    result = dispatch(action, payload)
    context = build_context(action, payload)
    log.info(
        "operator %s pressed %s on case %s", context.user, context.action_id, context.case_id
    )
    return jsonify(result.as_response()), 200


@blueprint.route("/slack/interactivity", methods=["POST"])
def slack_interactivity() -> tuple[Response, int]:
    raw = request.get_data()

    rejection = _verify(raw)
    if rejection:
        return rejection

    payload = _parse_payload(raw)
    if payload is None:
        return _json_error("the payload was not valid json", 400)

    payload_type = payload.get("type")

    if payload_type == VIEW_SUBMISSION:
        return _handle_view_submission(payload)

    if payload_type == BLOCK_ACTIONS:
        return _handle_block_actions(payload)

    log.info("ignoring an unsupported Slack payload type: %s", payload_type)
    return jsonify({"ok": True}), 200
