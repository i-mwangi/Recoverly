from __future__ import annotations

import pytest

from src.preflight.amount_router import (
    derive_outstanding,
    select_path,
    select_path_from_gross,
)


class TestDeriveOutstanding:
    def test_zero_deposit(self):
        assert derive_outstanding(12_500, 0.0) == 12_500.0

    def test_fifty_percent_deposit(self):
        assert derive_outstanding(47_300, 0.5) == 23_650.0

    def test_rounds_to_cents(self):
        assert derive_outstanding(1_234.567, 0.1) == 1_111.11

    def test_rejects_zero_gross(self):
        with pytest.raises(ValueError):
            derive_outstanding(0, 0.0)

    def test_rejects_negative_gross(self):
        with pytest.raises(ValueError):
            derive_outstanding(-1, 0.0)

    def test_rejects_full_deposit(self):
        with pytest.raises(ValueError):
            derive_outstanding(1_000, 1.0)

    def test_rejects_negative_deposit(self):
        with pytest.raises(ValueError):
            derive_outstanding(1_000, -0.1)


class TestSelectPath:
    @pytest.mark.parametrize(
        "outstanding, expected",
        [
            (1_500, "lite"),
            (2_999, "lite"),
            (3_000, "in_spot"),
            (8_750, "in_spot"),
            (23_650, "in_spot"),
            (40_000, "in_spot"),
            (40_001, "attorney_recommended"),
            (65_000, "attorney_recommended"),
        ],
    )
    def test_bands(self, outstanding, expected):
        assert select_path(outstanding).mode == expected

    def test_rejects_zero(self):
        with pytest.raises(ValueError):
            select_path(0)

    def test_rejects_negative(self):
        with pytest.raises(ValueError):
            select_path(-100)

    def test_rejects_inverted_bounds(self):
        with pytest.raises(ValueError):
            select_path(5_000, sweet_spot_min=50_000, sweet_spot_max=10_000)

    def test_lite_requires_concierge_approval(self):
        assert select_path(1_500).requires_concierge_approval is True
        assert select_path(1_500).concierge_card_template == "09_lite_mode_2button.json"

    def test_attorney_requires_concierge_approval(self):
        decision = select_path(65_000)
        assert decision.requires_concierge_approval is True
        assert decision.concierge_card_template == "10_attorney_path_2button.json"

    def test_in_spot_bypasses_concierge(self):
        assert select_path(8_500).requires_concierge_approval is False
        assert select_path(8_500).concierge_card_template is None

    def test_lite_reports_cost_percentage(self):
        decision = select_path(1_500)
        assert decision.cost_pct_of_outstanding == pytest.approx(32.0)

    def test_d038_polymatrix_case(self):
        decision = select_path_from_gross(47_300, 0.5)
        assert decision.mode == "in_spot"
        assert decision.outstanding_balance_usd == 23_650.0
        assert decision.gross_amount_usd == 47_300

    def test_d038_newleaf_lite(self):
        decision = select_path_from_gross(3_200, 0.2)
        assert decision.mode == "lite"
        assert decision.outstanding_balance_usd == 2_560.0

    def test_rationale_mentions_outstanding(self):
        assert "$23,650" in select_path_from_gross(47_300, 0.5).rationale
