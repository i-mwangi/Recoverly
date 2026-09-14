from __future__ import annotations

import pytest

from src.preflight.intake_pairing import (
    ACTION_DUPLICATE_IGNORED,
    ACTION_MERGE_AND_CREATE,
    ACTION_PEND_WAITING_CONTRACT,
    ACTION_PEND_WAITING_INVOICE,
    ACTION_UNRECOGNIZED,
    pair_or_pend,
)


class TestPairOrPend:
    def test_invoice_alone_pends_for_contract(self, tmp_path):
        decision = pair_or_pend(
            "abc_trading",
            "invoice",
            {"invoice_no": "INV-2026-0184", "outstanding_usd": 8_750},
            pending_dir=tmp_path,
        )
        assert decision.action == ACTION_PEND_WAITING_CONTRACT
        assert decision.case_id == ""

    def test_contract_after_invoice_creates_case(self, tmp_path):
        pair_or_pend(
            "abc_trading",
            "invoice",
            {"invoice_no": "INV-1"},
            pending_dir=tmp_path,
        )
        decision = pair_or_pend(
            "abc_trading",
            "contract",
            {"contract_id": "CONTRACT-ABC"},
            pending_dir=tmp_path,
        )
        assert decision.action == ACTION_MERGE_AND_CREATE
        assert decision.case_id.startswith("RC-")

    def test_duplicate_invoice_ignored(self, tmp_path):
        pair_or_pend("abc_trading", "invoice", {"invoice_no": "INV-1"}, pending_dir=tmp_path)
        decision = pair_or_pend(
            "abc_trading", "invoice", {"invoice_no": "INV-1"}, pending_dir=tmp_path
        )
        assert decision.action == ACTION_DUPLICATE_IGNORED

    def test_different_invoice_replaces_pending(self, tmp_path):
        pair_or_pend("abc_trading", "invoice", {"invoice_no": "INV-1"}, pending_dir=tmp_path)
        decision = pair_or_pend(
            "abc_trading", "invoice", {"invoice_no": "INV-2"}, pending_dir=tmp_path
        )
        assert decision.action == ACTION_PEND_WAITING_CONTRACT

    def test_expired_pending_purged(self, tmp_path):
        pair_or_pend(
            "abc_trading",
            "invoice",
            {"invoice_no": "INV-1"},
            pending_dir=tmp_path,
            now_iso="2026-06-01T00:00:00+00:00",
        )
        decision = pair_or_pend(
            "abc_trading",
            "contract",
            {"contract_id": "CONTRACT-ABC"},
            pending_dir=tmp_path,
            now_iso="2026-06-05T00:00:00+00:00",
        )
        assert decision.action == ACTION_PEND_WAITING_INVOICE

    def test_unrecognised_kind_short_circuits(self, tmp_path):
        assert pair_or_pend(
            "abc_trading", "mystery", {}, pending_dir=tmp_path
        ).action == ACTION_UNRECOGNIZED

    def test_rejects_persona_with_no_safe_chars(self, tmp_path):
        with pytest.raises(ValueError):
            pair_or_pend("///", "invoice", {"invoice_no": "INV-1"}, pending_dir=tmp_path)


class TestPdfExtractor:
    def _text(self):
        return (
            "Invoice INV-2026-0184\n"
            "ABC Trading Co.\n"
            "Outstanding Balance: USD $8,750.00\n"
            "Gross Invoice Total: USD $12,500.00\n"
            "Due Date: 2026-05-08\n"
            "CONTRACT-ABC-TRADING\n"
        )

    def test_invoice_classified(self, tmp_path):
        from src.preflight.pdf_extractor import classify_text

        result = classify_text(self._text(), "INV-2026-0184_ABC_Trading.pdf", tmp_path / "x.pdf")
        assert result.kind == "invoice"
        assert result.invoice.invoice_no == "INV-2026-0184"
        assert result.invoice.outstanding_usd == 8_750.0
        assert result.invoice.due_date == "2026-05-08"

    def test_contract_classified(self, tmp_path):
        from src.preflight.pdf_extractor import classify_text

        result = classify_text(
            "Governing Law: California\nAAA Commercial Arbitration in San Francisco\nNet 30\n"
            "CONTRACT-ABC-TRADING",
            "ABC_Trading_contract.pdf",
            tmp_path / "x.pdf",
        )
        assert result.kind == "contract"
        assert result.contract.governing_law == "California"
        assert result.contract.payment_terms == "Net 30"

    def test_unrecognised_file(self, tmp_path):
        from src.preflight.pdf_extractor import classify_text

        result = classify_text("random text", "misc.pdf", tmp_path / "x.pdf")
        assert result.kind == "unknown"
        assert result.error
