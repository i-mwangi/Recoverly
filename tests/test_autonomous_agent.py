from __future__ import annotations

import json

from src.agents._case_state import update_case_state
from src.agents.recoverly_agent import (
    auto_send_routine_reminder,
    list_actionable_cases,
    request_operator_decision,
)
from src.concierge.slack_client import SlackResult
from src.outbound import DispatchResult


def _save_case(case_id: str, **patch):
    return update_case_state(
        case_id,
        invoice_no="INV-100",
        due_date="2026-09-07",
        outstanding_balance_usd=1250,
        customer_email="buyer@example.com",
        buyer_persona="abc_trading",
        status="active",
        **patch,
    )


def test_cycle_finds_routine_work_and_human_decisions():
    _save_case("RC-ROUTINE")
    _save_case("RC-DISPUTE", dispute=True)

    result = json.loads(list_actionable_cases("2026-09-14"))

    assert result["cases"][0]["recommended_tool"] == "request_operator_decision"
    assert result["cases"][1]["recommended_tool"] == "auto_send_routine_reminder"


def test_routine_tool_sends_once(monkeypatch):
    _save_case("RC-SEND")
    calls = []

    def send(case):
        calls.append(case)
        return DispatchResult(True, "email-1", "buyer@example.com", "test")

    monkeypatch.setattr("src.agents.recoverly_agent.send_day7_reminder", send)

    first = json.loads(auto_send_routine_reminder("RC-SEND", "2026-09-14"))
    second = json.loads(auto_send_routine_reminder("RC-SEND", "2026-09-14"))

    assert first["status"] == "sent"
    assert second["status"] == "blocked"
    assert len(calls) == 1


def test_routine_tool_blocks_disputes(monkeypatch):
    _save_case("RC-BLOCK", dispute=True)
    monkeypatch.setattr(
        "src.agents.recoverly_agent.send_day7_reminder",
        lambda case: (_ for _ in ()).throw(AssertionError("must not send")),
    )

    result = json.loads(auto_send_routine_reminder("RC-BLOCK", "2026-09-14"))

    assert result["status"] == "blocked"
    assert "dispute" in result["reason"]


def test_decision_tool_posts_once(monkeypatch):
    _save_case("RC-HITL", dispute=True)
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return SlackResult(ok=True, ts="1", channel="C1")

    monkeypatch.setattr("src.agents.recoverly_agent.post_card", post)

    first = json.loads(request_operator_decision("RC-HITL", "buyer disputed", "2026-09-14"))
    second = json.loads(request_operator_decision("RC-HITL", "buyer disputed", "2026-09-14"))

    assert first["status"] == "requested"
    assert second["status"] == "already_requested"
    assert len(calls) == 1
