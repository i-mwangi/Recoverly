from __future__ import annotations

import asyncio

from src.agents.concierge_agent import ConciergeAdapter


def test_status_request_does_not_create_a_slack_card():
    adapter = ConciergeAdapter()
    reply = asyncio.run(
        adapter.handle_message(
            "@concierge status check: list available agents",
            None,
            None,
            [],
            "room-1",
        )
    )
    assert "Online" in reply
    assert "Slack" not in reply


def test_approval_request_without_case_is_rejected_before_slack():
    adapter = ConciergeAdapter()
    reply = asyncio.run(
        adapter.handle_message(
            "event=approval_request card_kind=payment_plan",
            None,
            None,
            [],
            "room-1",
        )
    )
    assert "valid case ID" in reply
