from __future__ import annotations

import pytest

from src.agents._case_state import stop_case, update_case_state
from src.payments.dedup import already_processed, record_processed, synthetic_id
from src.payments.reconciler import (
    OVERPAYMENT_TOLERANCE_USD,
    SettlementStatus,
    classify,
    reconcile_inbound,
)


class TestClassify:
    def test_partial(self):
        assert classify(3_000, 8_750) is SettlementStatus.PARTIAL

    def test_settled_exact(self):
        assert classify(8_750, 8_750) is SettlementStatus.SETTLED

    def test_settled_within_tolerance(self):
        assert classify(8_750 - OVERPAYMENT_TOLERANCE_USD, 8_750) is SettlementStatus.SETTLED

    def test_overpaid(self):
        assert classify(9_000, 8_750) is SettlementStatus.OVERPAID

    def test_zero_outstanding_with_payment(self):
        assert classify(100, 0) is SettlementStatus.SETTLED

    def test_zero_outstanding_no_payment(self):
        assert classify(0, 0) is SettlementStatus.UNMATCHED


class TestReconcile:
    def _seed(self):
        update_case_state(
            "RC-2026-0042",
            amount_usd=12_500,
            deposit_pct=0.3,
            outstanding_balance_usd=8_750,
            status="active",
        )

    def test_partial_leaves_case_active(self, isolated_data):
        self._seed()
        result = reconcile_inbound("RC-2026-0042", "3000", "tx1")
        assert result.status is SettlementStatus.PARTIAL
        assert result.remaining_usd == 5_750
        assert not result.closes_case

    def test_full_payment_closes_case(self, isolated_data):
        self._seed()
        reconcile_inbound("RC-2026-0042", "3000", "tx1")
        result = reconcile_inbound("RC-2026-0042", "5750", "tx2")
        assert result.status is SettlementStatus.SETTLED
        assert result.remaining_usd == 0
        assert result.closes_case

    def test_overpayment_reports_excess(self, isolated_data):
        self._seed()
        result = reconcile_inbound("RC-2026-0042", "10000", "tx1")
        assert result.status is SettlementStatus.OVERPAID
        assert result.overpaid_usd == 1_250

    def test_halted_case_still_records_payment(self, isolated_data):
        self._seed()
        stop_case("RC-2026-0042", "cease_and_desist")
        result = reconcile_inbound("RC-2026-0042", "8750", "tx1")
        assert result.halted is True
        assert result.paid_to_date_usd == 8_750

    def test_non_positive_amount_rejected(self, isolated_data):
        self._seed()
        assert reconcile_inbound("RC-1", "0", "tx1").status is SettlementStatus.UNMATCHED

    def test_invalid_case_id_rejected(self, isolated_data):
        assert reconcile_inbound("../etc", "100", "tx1").status is SettlementStatus.UNMATCHED


class TestDedup:
    def test_records_and_detects(self, tmp_path):
        path = tmp_path / "processed.json"
        assert not already_processed("tx1", path)
        record_processed("tx1", path)
        assert already_processed("tx1", path)

    def test_synthetic_id_stable(self):
        assert synthetic_id("0xa", "100", "USDC") == synthetic_id("0xa", "100", "USDC")

    def test_synthetic_id_differs_by_inputs(self):
        assert synthetic_id("0xa", "100", "USDC") != synthetic_id("0xb", "100", "USDC")

    def test_empty_transaction_id_is_never_seen(self, tmp_path):
        assert not already_processed("", tmp_path / "processed.json")
