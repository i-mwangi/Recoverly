from __future__ import annotations

from typing import Any, Final

from src.aaa.templates import render_demand_letter
from src.outbound import (
    DispatchEvents,
    DispatchResult,
    OutboundDispatchError,
    dispatch_email,
)

AGENT: Final = "aaa_specialist"

DEMAND_LETTER_EVENTS: Final = DispatchEvents(
    intent="demand_letter_intent",
    sent="demand_letter_sent",
    failed="demand_letter_failed",
)

__all__ = ["DispatchResult", "OutboundDispatchError", "send_demand_letter"]


def send_demand_letter(case: dict[str, Any]) -> DispatchResult:
    return dispatch_email(
        case,
        agent=AGENT,
        renderer=render_demand_letter,
        events=DEMAND_LETTER_EVENTS,
        intent_payload={"trigger": "operator_approval", "document": "demand_letter"},
    )
