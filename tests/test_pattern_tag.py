from __future__ import annotations

import datetime as dt

import pytest

from src.investigator.pattern_tag import (
    InternalHistorySnapshot,
    TAG_TO_DAY55_VOICE,
    TAG_TO_TONE,
    aggregate_customer,
    classify_customer,
    classify_pattern_tag,
    day55_voice_action,
    should_trigger_critical_alert,
    tone_for,
)


def snapshot(**overrides) -> InternalHistorySnapshot:
    defaults = dict(
        customer_id="C1",
        lookback_months=6,
        days_since_onboarded=365,
        invoices_in_window=5,
        late_payment_count=0,
        avg_days_late=0.0,
        max_days_late=0,
        walked_to_escalator=False,
        paid_early_count=0,
    )
    defaults.update(overrides)
    return InternalHistorySnapshot(**defaults)


class TestClassify:
    def test_new_customer_under_90_days(self):
        assert classify_pattern_tag(snapshot(days_since_onboarded=30)) == "new_customer"

    def test_new_customer_at_boundary(self):
        assert classify_pattern_tag(snapshot(days_since_onboarded=89)) == "new_customer"
        assert classify_pattern_tag(snapshot(days_since_onboarded=90)) != "new_customer"

    def test_clean_history(self):
        assert classify_pattern_tag(snapshot(late_payment_count=0)) == "clean_history"

    def test_escalation_history_beats_chronic(self):
        assert classify_pattern_tag(
            snapshot(late_payment_count=5, avg_days_late=45, walked_to_escalator=True)
        ) == "escalation_history"

    def test_chronic_by_count(self):
        assert classify_pattern_tag(
            snapshot(late_payment_count=3, avg_days_late=10)
        ) == "chronic_late"

    def test_chronic_by_average(self):
        assert classify_pattern_tag(
            snapshot(late_payment_count=1, avg_days_late=22)
        ) == "chronic_late"

    def test_occasional_drift(self):
        assert classify_pattern_tag(
            snapshot(late_payment_count=2, avg_days_late=10)
        ) == "occasional_drift"

    def test_first_time_late_boundary(self):
        assert classify_pattern_tag(
            snapshot(late_payment_count=1, avg_days_late=14)
        ) == "first_time_late"


class TestTagRoutingTables:
    def test_every_tag_has_tone(self):
        for tag in TAG_TO_TONE:
            assert TAG_TO_TONE[tag] in ("polite", "polite_firm")

    def test_every_tag_has_day55_action(self):
        for tag in TAG_TO_DAY55_VOICE:
            assert TAG_TO_DAY55_VOICE[tag] in ("skip", "concierge_hitl", "auto_fire")

    def test_tone_for_falls_back_to_polite(self):
        assert tone_for("mystery_tag") == "polite"

    def test_day55_action_falls_back_to_concierge(self):
        assert day55_voice_action("mystery_tag") == "concierge_hitl"


class TestCriticalAlert:
    def test_escalation_history_on_day_zero(self):
        assert should_trigger_critical_alert("escalation_history", 0)

    def test_other_tags_do_not_trigger(self):
        assert not should_trigger_critical_alert("chronic_late", 0)

    def test_later_days_do_not_trigger(self):
        assert not should_trigger_critical_alert("escalation_history", 7)


class TestAggregate:
    def _customer(self, invoices):
        return {
            "customer_id": "C1",
            "onboarded_date": "2024-01-01",
            "invoices": invoices,
        }

    def test_all_paid_on_time(self):
        snap = aggregate_customer(
            self._customer(
                [
                    {"issued_date": "2026-01-01", "paid_date": "2026-01-15", "days_late": 0},
                    {"issued_date": "2026-02-01", "paid_date": "2026-02-15", "days_late": 0},
                ]
            ),
            as_of=dt.date(2026, 6, 1),
        )
        assert snap.late_payment_count == 0
        assert snap.avg_days_late == 0.0

    def test_lookback_excludes_old_invoices(self):
        snap = aggregate_customer(
            self._customer(
                [
                    {"issued_date": "2025-01-01", "paid_date": "2025-02-01", "days_late": 50},
                    {"issued_date": "2026-05-01", "paid_date": "2026-05-10", "days_late": 0},
                ]
            ),
            as_of=dt.date(2026, 6, 1),
            lookback_months=3,
        )
        assert snap.invoices_in_window == 1
        assert snap.late_payment_count == 0

    def test_walked_to_escalator_detected(self):
        snap = aggregate_customer(
            self._customer(
                [
                    {"issued_date": "2026-01-01", "paid_date": "2026-04-01", "days_late": 60, "walked_to_escalator": True},
                ]
            ),
            as_of=dt.date(2026, 5, 1),
        )
        assert snap.walked_to_escalator is True

    def test_classify_customer_returns_tag_and_snapshot(self):
        tag, snap = classify_customer(
            self._customer(
                [
                    {"issued_date": "2026-01-01", "paid_date": "2026-01-15", "days_late": 0},
                ]
            ),
            as_of=dt.date(2026, 6, 1),
        )
        assert tag in ("clean_history", "new_customer")
        assert snap.customer_id == "C1"
