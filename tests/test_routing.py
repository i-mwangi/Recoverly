from __future__ import annotations

from src.payments.channels import (
    CHANNEL_LABELS,
    channel_label,
    outstanding_for,
    select_channel,
)
from src.preflight.amount_router import select_path_from_gross
from src.preflight.routing import days_past_due, route_case


class TestPreflightRouting:
    def test_in_spot_goes_to_investigator(self):
        path = select_path_from_gross(12_500, 0.3)
        route = route_case({"days_past_due": 7}, path)
        assert route.target == "investigator"

    def test_above_ceiling_asks_operator(self):
        path = select_path_from_gross(65_000, 0.0)
        route = route_case({"days_past_due": 7}, path)
        assert route.target == "concierge"
        assert route.card_kind == "attorney_recommended"

    def test_attorney_tag_bypasses_concierge(self):
        path = select_path_from_gross(65_000, 0.0)
        route = route_case({"days_past_due": 7, "attorney_tag": True}, path)
        assert route.target == "investigator"

    def test_below_floor_asks_operator(self):
        path = select_path_from_gross(2_000, 0.0)
        route = route_case({"days_past_due": 7}, path)
        assert route.card_kind == "lite"

    def test_lite_decided_goes_to_diplomat(self):
        path = select_path_from_gross(2_000, 0.0)
        route = route_case({"days_past_due": 7, "lite_decision": True}, path)
        assert route.target == "diplomat"

    def test_day55_to_voice(self):
        path = select_path_from_gross(12_500, 0.0)
        route = route_case({"days_past_due": 57}, path)
        assert route.target == "voice"

    def test_day60_to_escalator(self):
        path = select_path_from_gross(12_500, 0.0)
        route = route_case({"days_past_due": 108}, path)
        assert route.target == "escalator"

    def test_days_past_due_parses_junk(self):
        assert days_past_due({"days_past_due": "invalid"}) == 0
        assert days_past_due({"days_past_due": None}) == 0

    def test_enrich_carries_path_fields(self):
        path = select_path_from_gross(12_500, 0.3)
        route = route_case({"days_past_due": 7}, path)
        enriched = route.enrich({"case_id": "RC-1"}, path)
        assert enriched["path_mode"] == "in_spot"
        assert enriched["outstanding_balance_usd"] == 8_750


class TestPaymentChannel:
    def test_small_amount_card(self):
        assert select_channel({"amount_usd": 5_000}) == "card"

    def test_mid_amount_usdc(self):
        assert select_channel({"amount_usd": 47_300, "deposit_pct": 0.5}) == "usdc"

    def test_large_amount_wire(self):
        assert select_channel({"amount_usd": 80_000}) == "wire"

    def test_explicit_method_wins(self):
        assert select_channel({"amount_usd": 100, "method": "wire"}) == "wire"

    def test_outstanding_for_uses_gross_deposit(self):
        assert outstanding_for({"amount_usd": 47_300, "deposit_pct": 0.5}) == 23_650

    def test_labels_registered_for_every_channel(self):
        for channel in ("card", "usdc", "wire", "ach"):
            assert channel_label(channel) == CHANNEL_LABELS[channel]
