from __future__ import annotations

from typing import Any, Final

from src.diplomat.templates import render_day7, render_lite_final_notice
from src.outbound import (
    DispatchEvents,
    DispatchResult,
    OutboundDispatchError,
    dispatch_email,
)

AGENT: Final = "diplomat"

DAY7_EVENTS: Final = DispatchEvents(
    intent="day7_reminder_intent",
    sent="day7_reminder_sent",
    failed="day7_reminder_failed",
)

LITE_EVENTS: Final = DispatchEvents(
    intent="lite_final_notice_intent",
    sent="lite_final_notice_sent",
    failed="lite_final_notice_failed",
)

__all__ = ["DispatchResult", "OutboundDispatchError", "send_day7_reminder", "send_lite_final_notice"]


def send_day7_reminder(case: dict[str, Any]) -> DispatchResult:
    return dispatch_email(
        case,
        agent=AGENT,
        renderer=render_day7,
        events=DAY7_EVENTS,
        intent_payload={
            "trigger": "operator_approval",
            "used_revised_body": bool(case.get("revised_email_body")),
        },
    )


def send_lite_final_notice(case: dict[str, Any]) -> DispatchResult:
    return dispatch_email(
        case,
        agent=AGENT,
        renderer=render_lite_final_notice,
        events=LITE_EVENTS,
        intent_payload={"trigger": "operator_approval", "path_mode": "lite"},
    )
