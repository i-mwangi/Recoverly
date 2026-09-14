from __future__ import annotations

import pytest

from src.concierge.actions.base import (
    build_context,
    case_id_from_value,
    registered_actions,
    resolve,
)


class TestCaseIdExtraction:
    def test_bare_id(self):
        assert case_id_from_value("RC-2026-0042") == "RC-2026-0042"

    def test_from_query_style_value(self):
        assert case_id_from_value("case=RC-2026-0067&request_id=REQ-1") == "RC-2026-0067"

    def test_from_colon_suffix(self):
        assert case_id_from_value("RC-2026-0042:60") == "RC-2026-0042"

    def test_from_action_id_when_value_empty(self):
        assert case_id_from_value("", "run_collection_day_7_RC-1") == "RC-1"

    def test_bad_value_returns_unknown(self):
        assert case_id_from_value("../etc/passwd") == "UNKNOWN"


class TestRegistry:
    @pytest.mark.parametrize(
        "action_id",
        [
            "hitl_approve",
            "hitl_reject",
            "hitl_revise",
            "voice_aaa_escalate_approve",
            "voice_aaa_stand_down",
            "voice_try_once_more",
            "aaa_draft_demand_letter_now",
            "aaa_hold_for_legal_review",
            "payment_settled_confirm",
            "payment_reopen_case",
            "tone_coach_override_force_approve",
        ],
    )
    def test_exact_actions_registered(self, action_id):
        assert resolve(action_id) is not None

    @pytest.mark.parametrize(
        "action_id",
        [
            "attorney_run_RC-2026-0067",
            "attorney_skip_RC-2026-0067",
            "lite_run_RC-2026-0068",
            "lite_writeoff_RC-2026-0068",
            "approve_aaa_letter_RC-2026-0044",
            "revise_aaa_letter_RC-2026-0044",
            "reject_aaa_letter_RC-2026-0044",
            "run_collection_day_7",
        ],
    )
    def test_prefix_actions_resolve(self, action_id):
        assert resolve(action_id) is not None

    def test_unknown_action_returns_none(self):
        assert resolve("mystery_button") is None

    def test_registered_actions_listing(self):
        listing = registered_actions()
        assert "hitl_approve" in listing
        assert any(item.endswith("*") for item in listing)


class TestContextBuilder:
    def test_extracts_user_and_case(self):
        context = build_context(
            {"action_id": "hitl_approve", "value": "RC-2026-0042"},
            {"user": {"username": "dana"}, "channel": {"id": "C123"}, "message": {"ts": "1.0"}},
        )
        assert context.user == "dana"
        assert context.case_id == "RC-2026-0042"
        assert context.channel == "C123"

    def test_missing_fields_default(self):
        context = build_context({"action_id": "hitl_approve", "value": "RC-1"}, {})
        assert context.user == "unknown"
