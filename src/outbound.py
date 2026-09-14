from __future__ import annotations

import logging
import hashlib
from dataclasses import dataclass
from typing import Any, Callable, Final, Protocol

from src.agents.base import audit
from src.concierge.buyer_send import BuyerEmailDraft, send_buyer_outbound

log = logging.getLogger("recoverly.outbound")

DEFAULT_PERSONA: Final = "unknown"
BODY_PREVIEW_LIMIT: Final = 200


class OutboundDispatchError(RuntimeError):
    pass


class RenderedEmail(Protocol):
    @property
    def buyer_persona(self) -> str: ...


@dataclass(frozen=True, slots=True)
class DispatchEvents:
    intent: str
    sent: str
    failed: str


@dataclass(frozen=True, slots=True)
class DispatchResult:
    ok: bool
    message_id: str
    to: str
    backend: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message_id": self.message_id,
            "to": self.to,
            "backend": self.backend,
        }


Renderer = Callable[[dict[str, Any]], tuple[str, str, Any]]


def _dispatch_email(
    case: dict[str, Any],
    *,
    agent: str,
    renderer: Renderer,
    events: DispatchEvents,
    intent_payload: dict[str, Any] | None = None,
    fallback_persona: str = DEFAULT_PERSONA,
) -> DispatchResult:
    case_id = str(case.get("case_id") or "UNKNOWN")
    from src.agents._case_state import load_case_state, TERMINAL_STATUSES

    state = load_case_state(case_id)
    if state.get("status") in TERMINAL_STATUSES or state.get("halted_reason"):
        raise OutboundDispatchError(
            f"case {case_id} is closed or halted; outreach is stopped"
        )
    subject, body, context = renderer(case)
    persona = getattr(context, "buyer_persona", "") or fallback_persona

    audit(
        case_id,
        agent,
        events.intent,
        {
            "subject": subject,
            "buyer_persona": persona,
            "body_length": len(body),
            **(intent_payload or {}),
        },
    )

    draft = BuyerEmailDraft(
        case_id=case_id,
        buyer_persona=persona,
        subject=subject,
        body_text=body,
        to_email=str(case.get("buyer_email") or case.get("customer_email") or "")
        or None,
    )

    try:
        sent = send_buyer_outbound(draft)
    except Exception as error:
        audit(
            case_id,
            agent,
            events.failed,
            {"error": str(error), "body_preview": body[:BODY_PREVIEW_LIMIT]},
        )
        raise OutboundDispatchError(
            f"outbound email dispatch failed for case {case_id}: {error}"
        ) from error

    result = DispatchResult(
        ok=True,
        message_id=str(sent.get("id") or ""),
        to=str(sent.get("to") or ""),
        backend=str(sent.get("backend") or ""),
    )
    audit(case_id, agent, events.sent, result.as_dict())
    log.info(
        "%s dispatched %s for case %s to %s", agent, events.sent, case_id, result.to
    )
    return result


def dispatch_email(
    case: dict[str, Any],
    *,
    agent: str,
    renderer: Renderer,
    events: DispatchEvents,
    intent_payload: dict[str, Any] | None = None,
    fallback_persona: str = DEFAULT_PERSONA,
) -> DispatchResult:
    from src.agents._case_state import (
        case_path,
        load_case_state,
        update_case_state,
        TERMINAL_STATUSES,
    )
    from src.utils.filelock import exclusive_lock

    case_id = str(case.get("case_id") or "UNKNOWN")
    with exclusive_lock(case_path(case_id).with_suffix(".outbound.lock")):
        state = load_case_state(case_id)
        if state.get("status") in TERMINAL_STATUSES or state.get("halted_reason"):
            raise OutboundDispatchError(
                f"case {case_id} is closed or halted; outreach is stopped"
            )
        rendered = renderer(case)
        subject, body, _ = rendered
        recipient = (
            case.get("buyer_email")
            or case.get("customer_email")
            or case.get("buyer_persona")
            or ""
        )
        digest = hashlib.sha256(f"{recipient}\n{subject}\n{body}".encode()).hexdigest()
        receipts = state.get("outbound_receipts") or {}
        if digest in receipts:
            return DispatchResult(**receipts[digest])
        result = _dispatch_email(
            case,
            agent=agent,
            renderer=lambda _: rendered,
            events=events,
            intent_payload=intent_payload,
            fallback_persona=fallback_persona,
        )
        update_case_state(
            case_id, outbound_receipts={**receipts, digest: result.as_dict()}
        )
        return result
