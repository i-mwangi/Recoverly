from __future__ import annotations

from datetime import date

from src.aaa.templates import filing_fee_estimate, render_demand_letter, render_orientation
from src.diplomat.templates import (
    outstanding_amount,
    render_day7,
    render_lite_final_notice,
    substitute_placeholders,
)


class TestOutstandingAmount:
    def test_from_explicit_outstanding(self):
        assert outstanding_amount({"outstanding_balance_usd": 8_750}) == 8_750

    def test_derived_from_gross_and_deposit(self):
        assert outstanding_amount({"amount_usd": 12_500, "deposit_pct": 0.3}) == 8_750


class TestDay7Reminder:
    def _case(self, **overrides):
        base = {
            "case_id": "RC-2026-0042",
            "amount_usd": 12_500,
            "deposit_pct": 0.3,
            "buyer_persona": "abc_trading",
            "invoice_no": "INV-2026-0184",
            "due_date": "2026-05-08",
            "customer_company": "ABC Trading Co.",
            "customer_first_name": "Dana",
        }
        base.update(overrides)
        return base

    def test_subject_includes_invoice(self):
        subject, _, _ = render_day7(self._case())
        assert "INV-2026-0184" in subject

    def test_body_uses_outstanding_not_gross(self):
        _, body, _ = render_day7(self._case())
        assert "$8,750" in body
        assert "$12,500" not in body

    def test_revised_body_takes_precedence(self):
        revised = "Custom body that already reads well enough for delivery."
        _, body, _ = render_day7(self._case(revised_email_body=revised))
        assert revised.split(".")[0] in body

    def test_placeholders_substituted(self):
        _, body, ctx = render_day7(self._case())
        assert "{{CHECKOUT_URL}}" not in body
        assert ctx.paylink in body

    def test_substitute_replaces_variants(self):
        _, _, ctx = render_day7(self._case())
        replaced = substitute_placeholders("{{PAYLINK}} and [CHECKOUT_URL]", ctx)
        assert "{{PAYLINK}}" not in replaced

    def test_revised_draft_placeholders_are_safe_for_a_buyer(self):
        case = self._case() | {"customer_name": "Demo Buyer", "buyer_first_name": "", "customer_first_name": ""}
        _, _, context = render_day7(case)
        revised = (
            "Hi [First Name],\n\n"
            "Services rendered on [Date, if known; otherwise omit]. Payment was due on "
            "[Due Date]. Please remit payment by [Specific Date, e.g., 5 business days "
            "from send]."
        )
        rendered = substitute_placeholders(revised, context)
        assert "[" not in rendered
        assert "Hi Demo," in rendered
        assert substitute_placeholders("Hi [AP Contact Name],", context) == "Hi Demo,"
        rendered = substitute_placeholders(
            "Dear [Client Name], please remit payment by [date — e.g., Friday] or call [phone number].",
            context,
        )
        assert rendered == "Dear Demo, please remit payment within five business days."


class TestLiteFinalNotice:
    def test_body_marks_final(self):
        subject, body, _ = render_lite_final_notice(
            {"case_id": "RC-1", "amount_usd": 2_000, "invoice_no": "INV-9"}
        )
        assert "Final" in subject
        assert "write-off" in body


class TestArbitrationTemplates:
    def test_filing_fee_bands(self):
        assert filing_fee_estimate(23_650) == 925
        assert filing_fee_estimate(80_000) == 1_775
        assert filing_fee_estimate(200_000) == 2_950
        assert filing_fee_estimate(500_000) == 4_575

    def test_demand_letter_uses_case_invoice(self):
        subject, body, ctx = render_demand_letter(
            {
                "case_id": "RC-2026-0067",
                "buyer_persona": "megacorp",
                "amount_usd": 47_300,
                "deposit_pct": 0.5,
                "invoice_no": "INV-2026-0220",
                "due_date": "2026-03-01",
                "days_past_due": 108,
                "customer_company": "MegaCorp Holdings",
                "customer_state": "New York",
            },
            today=date(2026, 6, 17),
        )
        assert "INV-2026-0220" in subject
        assert "$23,650" in body
        assert ctx.filing_fee == 925

    def test_orientation_carries_case(self):
        text = render_orientation("RC-1", 25_000)
        assert "RC-1" in text
        assert "$25,000" in text
