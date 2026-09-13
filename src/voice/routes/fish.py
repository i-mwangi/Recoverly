from __future__ import annotations

import logging
import os
import asyncio
import json
from pathlib import Path
from typing import Final
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import httpx
from flask import Blueprint, Response, request

from src.agents._case_state import InvalidCaseIdError, validate_case_id, set_anomaly_halt, update_case_state
from src.voice.dynamic_vars import UnknownCaseError, fetch_case_dynamic_vars
from src.voice.routes.twilio import auth_token, verify_twilio_signature
from src.voice.security import strict_mode
from src.config import settings
from src.llm.provider import complete_async
from src.agents.base import audit
from src.concierge.cards import post_card
from src.voice.signals import detect_hostile
from src.voice.transcript import extract_commitment

blueprint = Blueprint("fish_audio", __name__)
log = logging.getLogger("recoverly.voice.routes.fish")
FISH_TTS_URL: Final = "https://api.fish.audio/v1/tts"
MAX_CONVERSATION_TURNS: Final = 3


def _conversation_path(call_sid: str) -> Path:
    safe_sid = "".join(char for char in call_sid if char.isalnum() or char in "_-")
    return settings.data.audit_trail_jsonl.parent / "voice_conversations" / f"{safe_sid}.json"


def _load_conversation(call_sid: str) -> dict:
    path = _conversation_path(call_sid)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_conversation(call_sid: str, payload: dict) -> None:
    path = _conversation_path(call_sid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _conversation_reply(case_id: str, transcript: str, history: list[dict]) -> str:
    """Keep the phone agent brief and limited to the active invoice."""
    values = fetch_case_dynamic_vars(case_id)
    fallback = (
        f"Thank you. Your balance is {values.get('invoice_outstanding_spoken', 'outstanding')}. "
        "Would you like to confirm a payment date, or is there a billing issue we should record?"
    )
    prompt = (
        "You are Recoverly's phone collections assistant. Give one helpful, professional reply "
        "in 45 words or fewer. Discuss only the invoice, payment options, a payment date, or a "
        "billing dispute. Do not threaten, give legal advice, invent facts, or claim a payment "
        "was received.\n"
        f"Invoice: {values.get('invoice_id')}; outstanding: {values.get('invoice_outstanding')}.\n"
        f"Buyer said: {transcript}\n"
        f"Earlier turns: {history[-4:]}"
    )
    try:
        reply, _ = asyncio.run(complete_async("Follow the caller-safety instructions exactly.", prompt, agent_role="voice", max_tokens=100))
        return str(reply).strip() or fallback
    except Exception as error:
        log.warning("conversation response generation failed: %s", error)
        return fallback


def _authorize() -> Response | None:
    token = auth_token()
    if not token and strict_mode("TWILIO_STRICT_SIGNATURE"):
        return Response("Twilio authentication is not configured", status=503)
    form = request.form.to_dict() if request.method == "POST" else {}
    if token and not verify_twilio_signature(
        request.url, form, request.headers.get("X-Twilio-Signature", "")
    ):
        return Response("unauthorized", status=401)
    return None


def _case_id() -> str | None:
    try:
        return validate_case_id(request.args.get("case_id", ""))
    except InvalidCaseIdError:
        return None


def _script(case_id: str) -> str:
    values = fetch_case_dynamic_vars(case_id)
    name = values.get("customer_first_name") or "there"
    return (
        f"Hello {name}. This is {values.get('operator_first_name', 'Alex')} calling about invoice "
        f"{values.get('invoice_id', '')}. Our records show an outstanding balance of "
        f"{values.get('invoice_outstanding_spoken', 'the remaining balance')}. "
        "Please contact us to discuss payment options. Thank you."
    )


def _intro(case_id: str) -> str:
    """Return the short message Twilio can speak while Fish Audio is prepared."""
    values = fetch_case_dynamic_vars(case_id)
    name = values.get("customer_first_name") or "there"
    invoice = values.get("invoice_id") or "your outstanding invoice"
    return (
        f"Hello {name}. This is Recoverly calling about invoice {invoice}. "
        "Please stay on the line for an important account message."
    )


@blueprint.route("/voice-webhook/twiml", methods=["POST"])
def twiml() -> Response:
    if rejection := _authorize():
        return rejection
    case_id = _case_id()
    base_url = os.getenv("TWILIO_CALLBACK_BASE_URL", "").strip().rstrip("/")
    if not case_id:
        return Response("invalid case_id", status=400)
    if not base_url:
        return Response("TWILIO_CALLBACK_BASE_URL is not configured", status=503)
    try:
        intro = _intro(case_id)
    except (FileNotFoundError, UnknownCaseError, ValueError) as error:
        return Response(str(error), status=400)
    action = f"{base_url}/voice-webhook/conversation?{urlencode({'case_id': case_id})}"
    return Response(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        f"<Response><Gather input=\"speech\" action=\"{escape(action)}\" method=\"POST\" "
        f"speechTimeout=\"auto\" timeout=\"8\"><Say>{escape(intro)} What can I help with today?</Say>"
        "</Gather><Say>We did not hear a response. Thank you for your time. Goodbye.</Say></Response>",
        mimetype="application/xml",
    )


@blueprint.route("/voice-webhook/conversation", methods=["POST"])
def conversation() -> Response:
    if rejection := _authorize():
        return rejection
    case_id = _case_id()
    call_sid = request.form.get("CallSid", "")
    transcript = request.form.get("SpeechResult", "").strip()
    base_url = os.getenv("TWILIO_CALLBACK_BASE_URL", "").strip().rstrip("/")
    if not case_id or not call_sid or not base_url:
        return Response("invalid conversation request", status=400)

    state = _load_conversation(call_sid)
    turns = state.get("turns") if isinstance(state.get("turns"), list) else []
    hostile = detect_hostile(transcript)
    if hostile.detected:
        set_anomaly_halt(case_id, "legal_threat")
        audit(case_id, "voice", "voice_legal_threat", {"excerpt": hostile.excerpt, "matches": list(hostile.reported_matches)})
        post_card(case_id, "The buyer requested no further calls or raised a legal threat.", f"*Source*: voice call\n*Excerpt*: {hostile.excerpt}\n\nOutbound activity is paused. Approve escalation to prepare an arbitration strategy, or stand down.", card_kind="voice_aaa_escalation")
        return Response("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Say>Understood. I will end the call now. Goodbye.</Say><Hangup/></Response>", mimetype="application/xml")
    commitment = asyncio.run(extract_commitment(transcript)) if transcript else None
    if commitment and commitment.found:
        update_case_state(case_id, promise_date=commitment.commitment_date, current_stage="promise_to_pay")
        audit(case_id, "voice", "voice_commitment_logged", commitment.as_dict())
        post_card(case_id, f"Buyer committed to a payment date of {commitment.commitment_date}.", f"*Source*: voice call\n*Commitment*: {commitment.excerpt or commitment.commitment_date}\n\nDiplomat will send a recap and schedule the follow-up.", card_kind="voice_commitment_summary")
        return Response("<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Say>Thank you. I will send a recap email to confirm. Goodbye.</Say><Hangup/></Response>", mimetype="application/xml")
    if not transcript:
        reply = "I did not catch that. Please say your expected payment date or describe the billing issue."
    else:
        reply = _conversation_reply(case_id, transcript, turns)
        turns.append({"buyer": transcript, "agent": reply})
    turn = len(turns)
    _save_conversation(call_sid, {"case_id": case_id, "turns": turns, "reply": reply, "turn": turn})

    if turn >= MAX_CONVERSATION_TURNS:
        return Response(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>"
            f"<Play>{escape(f'{base_url}/voice-webhook/fish-audio?{urlencode({'case_id': case_id, 'call_sid': call_sid})}')}</Play>"
            "<Say>Thank you. We have recorded this conversation. Goodbye.</Say></Response>",
            mimetype="application/xml",
        )
    audio_url = f"{base_url}/voice-webhook/fish-audio?{urlencode({'case_id': case_id, 'call_sid': call_sid})}"
    action = f"{base_url}/voice-webhook/conversation?{urlencode({'case_id': case_id})}"
    return Response(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>"
        f"<Play>{escape(audio_url)}</Play><Gather input=\"speech\" action=\"{escape(action)}\" method=\"POST\" speechTimeout=\"auto\" timeout=\"8\">"
        "<Say>Please tell me anything else I should record.</Say></Gather><Say>Thank you. Goodbye.</Say></Response>",
        mimetype="application/xml",
    )


@blueprint.route("/voice-webhook/fish-audio", methods=["GET", "POST"])
def fish_audio() -> Response:
    if rejection := _authorize():
        return rejection
    case_id = _case_id()
    key = os.getenv("FISH_API_KEY", "").strip()
    if not case_id:
        return Response("invalid case_id", status=400)
    if not key:
        return Response("FISH_API_KEY is not configured", status=503)
    try:
        call_sid = request.args.get("call_sid", "")
        text = str(_load_conversation(call_sid).get("reply") or "") if call_sid else _script(case_id)
        if not text:
            text = _script(case_id)
    except (FileNotFoundError, UnknownCaseError, ValueError) as error:
        return Response(str(error), status=400)
    payload = {"text": text, "format": "mp3"}
    reference_id = os.getenv("FISH_REFERENCE_ID", "").strip()
    if reference_id:
        payload["reference_id"] = reference_id
    try:
        result = httpx.post(
            FISH_TTS_URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json", "model": os.getenv("FISH_MODEL", "s2-pro")},
            json=payload,
            timeout=60.0,
        )
    except httpx.HTTPError as error:
        log.warning("Fish Audio request failed: %s", error)
        return Response("voice generation failed", status=502)
    if result.status_code != 200:
        log.warning("Fish Audio returned %d: %s", result.status_code, result.text[:300])
        return Response("voice generation failed", status=502)
    return Response(result.content, mimetype="audio/mpeg")
