from __future__ import annotations

import pytest

from src.agents._case_state import (
    InvalidCaseIdError,
    add_cost,
    apply_event,
    default_state,
    is_halted,
    load_case_state,
    mutate_case_state,
    rebuild_all_from_audit,
    set_anomaly_halt,
    stop_case,
    update_case_state,
    validate_case_id,
)
from src.utils.filelock import append_jsonl


class TestValidateCaseId:
    @pytest.mark.parametrize("case_id", ["RC-2026-0042", "case_1", "ABC.123", "x", "0"])
    def test_accepts_safe_ids(self, case_id):
        assert validate_case_id(case_id) == case_id

    @pytest.mark.parametrize(
        "case_id",
        ["", "../etc/passwd", ".hidden", "case/with/slash", " leading", "trailing.", "case\x00"],
    )
    def test_rejects_unsafe_ids(self, case_id):
        with pytest.raises(InvalidCaseIdError):
            validate_case_id(case_id)

    def test_rejects_oversized(self):
        with pytest.raises(InvalidCaseIdError):
            validate_case_id("A" + "b" * 129)


class TestCaseStateLifecycle:
    def test_default_state_shape(self, isolated_data):
        state = load_case_state("RC-2026-0042")
        assert state["status"] == "initiated"
        assert state["current_stage"] == "preflight"
        assert state["amount_paid_usd"] == 0.0

    def test_update_persists(self, isolated_data):
        update_case_state("RC-1", status="approved", amount_usd=8_750)
        reloaded = load_case_state("RC-1")
        assert reloaded["status"] == "approved"
        assert reloaded["amount_usd"] == 8_750
        assert reloaded["events_seen_count"] == 1

    def test_mutate_reads_current_state(self, isolated_data):
        update_case_state("RC-1", amount_usd=100)

        def increment(state):
            return {"amount_usd": state["amount_usd"] + 50}

        mutate_case_state("RC-1", increment)
        assert load_case_state("RC-1")["amount_usd"] == 150

    def test_stop_case_halts(self, isolated_data):
        stop_case("RC-1", "operator_stop")
        assert is_halted("RC-1")

    def test_anomaly_halt_records_kind(self, isolated_data):
        set_anomaly_halt("RC-1", "welfare")
        state = load_case_state("RC-1")
        assert state["anomaly_kind"] == "welfare"
        assert state["halted_reason"] == "anomaly_welfare"


class TestCost:
    def test_accumulates(self, isolated_data):
        add_cost("RC-1", {"cost_usd": 0.10})
        add_cost("RC-1", {"cost_usd": 0.15})
        assert load_case_state("RC-1")["cost_usd"] == pytest.approx(0.25)

    def test_cap_halts(self, isolated_data):
        add_cost("RC-1", {"cost_usd": 0.75})
        state = load_case_state("RC-1")
        assert state["status"] == "halted"
        assert state["halted_reason"].startswith("cost_cap_exceeded")

    def test_ignores_non_dict_usage(self, isolated_data):
        result = add_cost("RC-1", None)
        assert result["cost_usd"] == 0.0


class TestReplay:
    def test_rebuilds_from_audit_events(self, isolated_data):
        for event in (
            {"ts": "2026-06-01T00:00:00Z", "case_id": "RC-9", "agent": "preflight",
             "event": "preflight_done", "payload": {}},
            {"ts": "2026-06-01T00:01:00Z", "case_id": "RC-9", "agent": "investigator",
             "event": "investigator_done", "payload": {}},
            {"ts": "2026-06-01T00:02:00Z", "case_id": "RC-9", "agent": "concierge",
             "event": "human_approved", "payload": {}},
        ):
            append_jsonl(isolated_data / "audit_trail.jsonl", event)

        summary = rebuild_all_from_audit()
        assert summary["cases_written"] == 1

        state = load_case_state("RC-9")
        assert state["status"] == "approved"
        assert state["current_stage"] == "email_send"

    def test_rejects_bad_case_ids_in_replay(self, isolated_data):
        append_jsonl(
            isolated_data / "audit_trail.jsonl",
            {"ts": "2026-06-01T00:00:00Z", "case_id": "../etc/passwd", "event": "x", "payload": {}},
        )
        summary = rebuild_all_from_audit()
        assert "../etc/passwd" in summary["skipped_case_ids"]

    def test_apply_event_settles_case(self):
        state = default_state("RC-1")
        apply_event(state, {"event": "payment_received", "payload": {"amount_usd": 500}})
        assert state["status"] == "closed_recovered"
        assert state["amount_paid_usd"] == 500
