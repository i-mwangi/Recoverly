from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import base64
import time
import re
from urllib.parse import quote
import httpx
from typing import Any, Final

from flask import Blueprint, Response, jsonify, request

from src.agents._case_state import (
    InvalidCaseIdError,
    validate_case_id,
    case_path,
    cases_dir,
    load_case_state,
)
from src.agents.base import audit
from src.concierge.inbound import accept_inbound
from src.personas import normalize_persona
from src.voice.security import strict_mode

log = logging.getLogger("recoverly.concierge.routes.resend")

blueprint = Blueprint("resend", __name__)

SIGNATURE_HEADER: Final = "Resend-Signature"
STRICT_FLAG: Final = "RESEND_STRICT_SIGNATURE"
AGENT: Final = "inbound"
UNKNOWN_CASE: Final = "UNKNOWN"

DELIVERY_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "email.sent",
        "email.delivered",
        "email.delivery_delayed",
        "email.bounced",
        "email.complained",
        "email.opened",
        "email.clicked",
    }
)
INBOUND_EVENT: Final = "email.received"


def webhook_secret() -> str:
    return os.getenv("RESEND_WEBHOOK_SECRET", "").strip()


def verify_resend_signature(
    body: bytes, signature: str | None, message_id: str = "", timestamp: str = ""
) -> bool:
    secret = webhook_secret()

    if not secret:
        if strict_mode(STRICT_FLAG):
            log.error(
                "no Resend webhook secret is configured, so the request is rejected"
            )
            return False
        log.warning("accepting an unverified Resend webhook because strict mode is off")
        return True

    if not signature:
        return False

    try:
        if not message_id or abs(time.time() - int(timestamp)) > 300:
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"), validate=True)
    except (ValueError, TypeError):
        return False
    signed = message_id.encode() + b"." + timestamp.encode() + b"." + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(
        hmac.compare_digest(expected, candidate[3:])
        for candidate in signature.split()
        if candidate.startswith("v1,")
    )


def _case_id_from(data: dict[str, Any]) -> str:
    for key in ("case_id", "caseId"):
        if data.get(key):
            try:
                return validate_case_id(str(data[key]))
            except InvalidCaseIdError:
                log.warning("a Resend event carried an unusable case reference")
                return UNKNOWN_CASE

    tags = data.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if (
                isinstance(tag, dict)
                and tag.get("name") == "case_id"
                and tag.get("value")
            ):
                try:
                    return validate_case_id(str(tag["value"]))
                except InvalidCaseIdError:
                    return UNKNOWN_CASE

    subject = str(data.get("subject") or "")
    for candidate in re.findall(r"\bRC-[A-Za-z0-9-]+\b", subject):
        if case_path(candidate).is_file():
            return candidate
    invoice_ids = set(re.findall(r"\bINV-[A-Za-z0-9-]+\b", subject))
    if invoice_ids:
        matches = [
            path.stem
            for path in cases_dir().glob("*.json")
            if load_case_state(path.stem).get("invoice_no") in invoice_ids
        ]
        if len(matches) == 1:
            return matches[0]
    return UNKNOWN_CASE


def _persona_from(data: dict[str, Any]) -> str:
    tags = data.get("tags")
    if isinstance(tags, list):
        for tag in tags:
            if (
                isinstance(tag, dict)
                and tag.get("name") == "buyer"
                and tag.get("value")
            ):
                return normalize_persona(str(tag["value"]))
    return "unknown"


def _body_text(data: dict[str, Any]) -> str:
    for key in ("text", "plain", "body", "html"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


@blueprint.route("/webhooks/resend", methods=["POST"])
def resend_webhook() -> tuple[Response, int]:
    raw = request.get_data()

    if not verify_resend_signature(
        raw,
        request.headers.get("svix-signature"),
        request.headers.get("svix-id", ""),
        request.headers.get("svix-timestamp", ""),
    ):
        return jsonify({"error": "unauthorized"}), 401

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return jsonify({"error": "invalid json"}), 400

    if not isinstance(payload, dict):
        return jsonify({"error": "invalid payload"}), 400

    event_type = str(payload.get("type", ""))
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    case_id = _case_id_from(data)

    if event_type in DELIVERY_EVENTS:
        audit(
            case_id,
            AGENT,
            f"email_{event_type.removeprefix('email.')}",
            {
                "message_id": data.get("email_id") or data.get("id", ""),
                "to": data.get("to", ""),
            },
        )
        return jsonify({"ok": True, "event": event_type}), 200

    if event_type == INBOUND_EVENT:
        body_text = _body_text(data)
        if not body_text and data.get("email_id"):
            key = os.getenv("RESEND_API_KEY", "").strip()
            if not key:
                return jsonify(error="receiving API key is not configured"), 503
            try:
                response = httpx.get(
                    "https://api.resend.com/emails/receiving/"
                    + quote(str(data["email_id"]), safe=""),
                    headers={"Authorization": "Bearer " + key},
                    timeout=15,
                )
                response.raise_for_status()
                received = response.json()
                if not isinstance(received, dict):
                    raise ValueError("invalid received email")
                data = {**data, **received}
                body_text = _body_text(data)
                case_id = _case_id_from(data)
            except (httpx.HTTPError, ValueError):
                return jsonify(
                    error="received email could not be retrieved; retry required"
                ), 503
        if not body_text:
            audit(case_id, AGENT, "inbound_email_empty", {"from": data.get("from", "")})
            return jsonify({"ok": True, "ignored": "empty body"}), 200

        intents = accept_inbound(
            case_id=case_id,
            buyer_persona=_persona_from(data),
            body_text=body_text,
            source="resend_webhook",
            sender_email=str(data.get("from", "")) or None,
        )
        return jsonify({"ok": True, "intents": intents.as_dict()}), 200

    log.info("ignoring an unhandled Resend event: %s", event_type)
    return jsonify({"ok": True, "ignored": event_type}), 200
