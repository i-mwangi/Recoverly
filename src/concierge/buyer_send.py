from __future__ import annotations

import logging
import os
import smtplib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any, Final

from src.agents._utils import append_jsonl_locked
from src.config import settings

log = logging.getLogger("recoverly.concierge.buyer_send")

SMTP_HOST: Final = "smtp.gmail.com"
SMTP_PORT: Final = 465
SMTP_TIMEOUT_SECONDS: Final = 30

DEFAULT_SUPPLIER_NAME: Final = "Recoverly Collections"
DEFAULT_FROM_ADDRESS: Final = "onboarding@resend.dev"
DEFAULT_ALIAS_BASE: Final = "operator"
DEFAULT_ALIAS_DOMAIN: Final = "example.com"

BUYER_DISPLAY_NAMES: Final[dict[str, str]] = {
    "abc_trading": "ABC Trading Co.",
    "polymatrix": "PolyMatrix Industries LLC",
    "xyz": "XYZ Industries",
    "newleaf": "NewLeaf Wellness Co.",
}


class EmailBackendError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BuyerEmailDraft:
    case_id: str
    buyer_persona: str
    subject: str
    body_text: str
    body_html: str | None = None
    to_email: str | None = None


@dataclass(frozen=True, slots=True)
class SendResult:
    message_id: str
    to: str
    backend: str

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.message_id, "to": self.to, "backend": self.backend}


def supplier_name() -> str:
    return os.getenv("RECOVERLY_SUPPLIER_NAME", "").strip() or DEFAULT_SUPPLIER_NAME


def reply_to_address() -> str:
    return (
        os.getenv("RECOVERLY_REPLY_TO", "").strip()
        or os.getenv("RESEND_FROM_EMAIL", "").strip()
        or DEFAULT_FROM_ADDRESS
    )


def buyer_email_alias(persona: str) -> str:
    base = os.getenv("RECOVERLY_BUYER_ALIAS_BASE", "").strip() or DEFAULT_ALIAS_BASE
    domain = os.getenv("RECOVERLY_BUYER_ALIAS_DOMAIN", "").strip() or DEFAULT_ALIAS_DOMAIN
    return f"{base}+{persona.replace('_', '-')}@{domain}"


def demo_override_address() -> str:
    return os.getenv("RECOVERLY_DEMO_BUYER_EMAIL", "").strip()


def buyer_display_name(persona: str) -> str:
    return BUYER_DISPLAY_NAMES.get(persona, persona.replace("_", " ").title())


def active_backend() -> str:
    return os.getenv("RECOVERLY_EMAIL_BACKEND", "smtp").strip().lower()


def _audit(case_id: str, event: str, payload: dict[str, Any]) -> None:
    append_jsonl_locked(
        settings.data.audit_trail_jsonl,
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "case_id": case_id,
            "agent": "buyer_send",
            "event": event,
            "payload": payload,
        },
    )


def _send_via_smtp(draft: BuyerEmailDraft, to_email: str) -> SendResult:
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_APP_PASSWORD", "").strip()
    if not user or not password:
        raise EmailBackendError(
            "SMTP_USER and SMTP_APP_PASSWORD must be set for the smtp email backend"
        )

    from_address = os.getenv("RECOVERLY_OUTBOUND_FROM", "").strip() or user
    message = EmailMessage()
    message["From"] = f"{supplier_name()} <{from_address}>"
    message["To"] = to_email
    message["Reply-To"] = reply_to_address()
    message["Subject"] = draft.subject
    message_id = make_msgid(domain=from_address.split("@")[-1] or "recoverly.local")
    message["Message-ID"] = message_id
    message.set_content(draft.body_text)
    if draft.body_html:
        message.add_alternative(draft.body_html, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context, timeout=SMTP_TIMEOUT_SECONDS) as server:
        server.login(user, password)
        server.send_message(message)

    return SendResult(message_id=message_id, to=to_email, backend="smtp")


def _send_via_resend(draft: BuyerEmailDraft, to_email: str) -> SendResult:
    api_key = settings.api_keys.resend
    if not api_key:
        raise EmailBackendError("RESEND_API_KEY must be set for the resend email backend")

    try:
        import resend
    except ImportError as error:
        raise EmailBackendError("the resend package is not installed") from error

    resend.api_key = api_key
    from_address = (
        os.getenv("RECOVERLY_OUTBOUND_FROM", "").strip()
        or os.getenv("RESEND_FROM_EMAIL", "").strip()
        or f"{supplier_name()} <{DEFAULT_FROM_ADDRESS}>"
    )

    params: dict[str, Any] = {
        "from": from_address,
        "to": [to_email],
        "subject": draft.subject,
        "text": draft.body_text,
        "tags": [
            {"name": "event", "value": "buyer_outbound"},
            {"name": "case_id", "value": draft.case_id},
            {"name": "buyer", "value": draft.buyer_persona},
        ],
    }
    if draft.body_html:
        params["html"] = draft.body_html

    response = resend.Emails.send(params)
    return SendResult(
        message_id=str(response.get("id", "")), to=to_email, backend="resend"
    )


BACKENDS: Final = {"smtp": _send_via_smtp, "resend": _send_via_resend}


def send_buyer_outbound(draft: BuyerEmailDraft) -> dict[str, Any]:
    backend = active_backend()
    sender = BACKENDS.get(backend)
    if sender is None:
        raise EmailBackendError(
            f"unknown RECOVERLY_EMAIL_BACKEND {backend!r}; expected one of {sorted(BACKENDS)}"
        )

    to_email = draft.to_email or demo_override_address() or buyer_email_alias(draft.buyer_persona)
    log.info(
        "sending buyer outbound case=%s persona=%s to=%s via %s",
        draft.case_id,
        draft.buyer_persona,
        to_email,
        backend,
    )

    result = sender(draft, to_email)

    _audit(
        draft.case_id,
        "email_sent",
        {
            "buyer_persona": draft.buyer_persona,
            "buyer_display_name": buyer_display_name(draft.buyer_persona),
            "to": result.to,
            "subject": draft.subject,
            "message_id": result.message_id,
            "backend": result.backend,
        },
    )
    return result.as_dict()
