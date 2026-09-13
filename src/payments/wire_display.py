from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from src.payments.base import PaymentProvider, PaymentResult, PaymentStatus

DEFAULT_REFERENCE_FORMAT: Final = "Recoverly invoice {invoice_id}"
SETTLEMENT_ETA_BUSINESS_DAYS: Final = "1-3"
FEE_DISCLAIMER: Final = (
    "The sender pays correspondent bank fees, and the beneficiary bank may deduct "
    "a receiving fee of USD 10 to 25."
)


@dataclass(frozen=True, slots=True)
class WireBeneficiary:
    beneficiary_name: str
    bank_name: str
    bank_address: str
    swift_bic: str
    account_number: str
    routing_or_branch_code: str | None = None
    intermediary_bank: str | None = None
    reference_format: str = DEFAULT_REFERENCE_FORMAT

    @classmethod
    def from_env(cls) -> WireBeneficiary:
        return cls(
            beneficiary_name=os.getenv("WIRE_BENEFICIARY_NAME", "Recoverly Operating Account"),
            bank_name=os.getenv("WIRE_BANK_NAME", ""),
            bank_address=os.getenv("WIRE_BANK_ADDRESS", ""),
            swift_bic=os.getenv("WIRE_SWIFT_BIC", ""),
            account_number=os.getenv("WIRE_ACCOUNT_NUMBER", ""),
            routing_or_branch_code=os.getenv("WIRE_ROUTING_CODE") or None,
            intermediary_bank=os.getenv("WIRE_INTERMEDIARY_BANK") or None,
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.bank_name and self.swift_bic and self.account_number)


class WireDisplayProvider(PaymentProvider):
    def __init__(self, beneficiary: WireBeneficiary | None = None) -> None:
        self.beneficiary = beneficiary or WireBeneficiary.from_env()

    @property
    def provider_name(self) -> str:
        return "wire"

    @property
    def supported_currencies(self) -> tuple[str, ...]:
        return ("USD",)

    async def create_payment(
        self, amount: Decimal, currency: str = "USD", metadata: dict[str, Any] | None = None
    ) -> PaymentResult:
        self.assert_supported(currency)
        details = metadata or {}
        invoice_id = str(details.get("case_id") or details.get("invoice_id") or "UNKNOWN")

        return PaymentResult(
            payment_id=f"wire_{invoice_id}",
            status=PaymentStatus.PENDING,
            amount=amount,
            currency=currency,
            checkout_url=None,
            metadata={
                "display_type": "wire_swift",
                "beneficiary_name": self.beneficiary.beneficiary_name,
                "bank_name": self.beneficiary.bank_name,
                "bank_address": self.beneficiary.bank_address,
                "swift_bic": self.beneficiary.swift_bic,
                "account_number": self.beneficiary.account_number,
                "routing_or_branch_code": self.beneficiary.routing_or_branch_code,
                "intermediary_bank": self.beneficiary.intermediary_bank,
                "payment_reference": self.beneficiary.reference_format.format(
                    invoice_id=invoice_id
                ),
                "settlement_eta_business_days": SETTLEMENT_ETA_BUSINESS_DAYS,
                "settlement_detection": "manual",
                "fee_disclaimer": FEE_DISCLAIMER,
                "beneficiary_configured": self.beneficiary.is_configured,
            },
        )

    async def verify_payment(self, payment_id: str) -> PaymentStatus:
        return PaymentStatus.PENDING

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        return {
            "payment_id": payment_id,
            "provider": self.provider_name,
            "status": PaymentStatus.PENDING.value,
            "beneficiary_name": self.beneficiary.beneficiary_name,
            "bank_name": self.beneficiary.bank_name,
            "swift_bic": self.beneficiary.swift_bic,
            "note": "Wire settlement is confirmed by an operator, never automatically.",
        }
