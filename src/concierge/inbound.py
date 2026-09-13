from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from src.agents._utils import (
    detect_anomaly,
    detect_dispute,
    detect_extension_request,
    detect_partial_payment,
    detect_promise_to_pay,
)
from src.agents.base import audit
from src.concierge.band_relay import notify_agent
from src.config import PROJECT_ROOT
from src.utils.filelock import append_jsonl

log = logging.getLogger("recoverly.concierge.inbound")

INBOUND_QUEUE: Final = PROJECT_ROOT / "data" / "inbound_queue.jsonl"
AGENT: Final = "inbound"
BODY_EXCERPT_LIMIT: Final = 240


@dataclass(frozen=True, slots=True)
class InboundReply:
    case_id: str
    buyer_persona: str
    body_text: str
    source: str
    received_at: str
    sender_email: str | None = None
    sender_call_sid: str | None = None


@dataclass(frozen=True, slots=True)
class IntentDetection:
    dispute: bool = False
    partial_payment: bool = False
    extension_request: bool = False
    promise_to_pay: bool = False
    anomaly: str | None = None

    @property
    def any_detected(self) -> bool:
        return bool(
            self.dispute
            or self.partial_payment
            or self.extension_request
            or self.promise_to_pay
            or self.anomaly
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "dispute": self.dispute,
            "partial_payment": self.partial_payment,
            "extension_request": self.extension_request,
            "promise_to_pay": self.promise_to_pay,
            "anomaly": self.anomaly,
        }

    def signals(self) -> list[str]:
        found = [
            name
            for name, present in (
                ("dispute", self.dispute),
                ("partial_payment", self.partial_payment),
                ("extension_request", self.extension_request),
                ("promise_to_pay", self.promise_to_pay),
            )
            if present
        ]
        if self.anomaly:
            found.append(f"anomaly_{self.anomaly}")
        return found


def detect_intents(body_text: str) -> IntentDetection:
    return IntentDetection(
        dispute=detect_dispute(body_text),
        partial_payment=detect_partial_payment(body_text),
        extension_request=detect_extension_request(body_text),
        promise_to_pay=detect_promise_to_pay(body_text),
        anomaly=detect_anomaly(body_text),
    )


def _enqueue(reply: InboundReply, intents: IntentDetection) -> None:
    append_jsonl(
        INBOUND_QUEUE,
        {
            "case_id": reply.case_id,
            "buyer_persona": reply.buyer_persona,
            "source": reply.source,
            "received_at": reply.received_at,
            "sender_email": reply.sender_email,
            "body_text": reply.body_text,
            "intents": intents.as_dict(),
        },
    )


def accept_inbound(
    case_id: str,
    buyer_persona: str,
    body_text: str,
    source: str = "cli",
    sender_email: str | None = None,
    sender_call_sid: str | None = None,
) -> IntentDetection:
    reply = InboundReply(
        case_id=case_id,
        buyer_persona=buyer_persona,
        body_text=body_text,
        source=source,
        received_at=datetime.now(timezone.utc).isoformat(),
        sender_email=sender_email,
        sender_call_sid=sender_call_sid,
    )
    intents = detect_intents(body_text)

    log.info(
        "inbound reply on case %s from %s via %s: %s",
        case_id,
        buyer_persona,
        source,
        intents.signals() or "no signals",
    )

    audit(
        case_id,
        AGENT,
        "customer_reply_received",
        {
            "buyer_persona": buyer_persona,
            "source": source,
            "sender_email": sender_email,
            "body_excerpt": body_text[:BODY_EXCERPT_LIMIT],
            "body_length": len(body_text),
            "intents": intents.as_dict(),
        },
    )

    for signal in intents.signals():
        audit(
            case_id,
            AGENT,
            f"customer_intent_{signal}",
            {"buyer_persona": buyer_persona, "source": source},
        )

    _enqueue(reply, intents)

    if intents.any_detected:
        notify_agent(
            "concierge",
            f"event=approval_request for case `{case_id}`. A buyer reply arrived by "
            f"{source} carrying: {', '.join(intents.signals())}.",
            case_id=case_id,
        )

    return intents
