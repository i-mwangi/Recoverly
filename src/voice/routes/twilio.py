from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from typing import Final

from flask import Blueprint, Response, jsonify, request

from src.agents._case_state import InvalidCaseIdError, validate_case_id
from src.concierge.band_relay import notify_agent
from src.voice.events import log_voice_event
from src.voice.security import strict_mode

log = logging.getLogger("recoverly.voice.routes.twilio")

blueprint = Blueprint("twilio", __name__)

SIGNATURE_HEADER: Final = "X-Twilio-Signature"
STRICT_FLAG: Final = "TWILIO_STRICT_SIGNATURE"
UNKNOWN_CASE: Final = "UNKNOWN"

STATUS_TO_CARD_KIND: Final[dict[str, str]] = {
    "completed": "voice_call_summary",
    "busy": "voice_no_answer",
    "no-answer": "voice_no_answer",
    "failed": "voice_failed",
    "canceled": "voice_failed",
}


def auth_token() -> str:
    return os.getenv("TWILIO_AUTH_TOKEN", "").strip()


def verify_twilio_signature(url: str, form: dict[str, str], signature: str) -> bool:
    token = auth_token()
    if not token or not signature:
        return False

    payload = url + "".join(f"{key}{form[key]}" for key in sorted(form))
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def _json_error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


@blueprint.route("/voice-webhook/call-status", methods=["POST"])
def call_status() -> tuple[Response, int]:
    form = request.form.to_dict()
    signature = request.headers.get(SIGNATURE_HEADER, "")

    if not auth_token():
        if strict_mode(STRICT_FLAG):
            log_voice_event("twilio_strict_rejected", {"reason": "TWILIO_AUTH_TOKEN is unset"})
            return _json_error("this endpoint is not configured", 503)
    elif not verify_twilio_signature(request.url, form, signature):
        log_voice_event(
            "twilio_signature_rejected",
            {"signature": signature[:20], "url": request.url[:120]},
        )
        return _json_error("unauthorized", 401)

    raw_case_id = form.get("case_id") or request.args.get("case_id", UNKNOWN_CASE)
    case_id = UNKNOWN_CASE
    if raw_case_id != UNKNOWN_CASE:
        try:
            case_id = validate_case_id(raw_case_id)
        except InvalidCaseIdError:
            log_voice_event("twilio_bad_case_id", {"case_id": repr(raw_case_id)[:80]})
            return _json_error("invalid case_id", 400)

    call_sid = form.get("CallSid", "unknown")
    status = (form.get("CallStatus") or "").lower()
    duration = form.get("CallDuration", "0")

    log_voice_event(
        "voice_call_status",
        {"call_sid": call_sid, "status": status, "duration_sec": duration},
        case_id=case_id,
    )

    card_kind = STATUS_TO_CARD_KIND.get(status)
    if card_kind is None:
        return jsonify({"ok": True, "ignored": status}), 200

    result = notify_agent(
        "concierge",
        f"event=approval_request card_kind={card_kind} for case `{case_id}`. "
        f"Call {call_sid} ended with status {status} after {duration} seconds.",
        case_id=case_id,
    )
    log_voice_event("twilio_relay", result.as_dict(), case_id=case_id)

    return jsonify({"ok": True, "card_kind": card_kind}), 200
