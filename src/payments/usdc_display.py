from __future__ import annotations

from decimal import Decimal
from typing import Any, Final

from src.payments.base import PaymentProvider, PaymentResult, PaymentStatus
from src.payments.hedera_client import (
    is_configured as hedera_is_configured,
    network as hedera_network,
    receiving_account_id,
    usdc_token_id,
)

SETTLEMENT_ETA_SECONDS: Final = "3-5"
DEFAULT_MEMO_PREFIX: Final = "recoverly"


class UsdcDepositProvider(PaymentProvider):
    def __init__(
        self,
        receiving_id: str | None = None,
        token_id: str | None = None,
        network_name: str | None = None,
    ) -> None:
        self.receiving_account_id = (receiving_id or receiving_account_id()).strip()
        self.token_id = (token_id or usdc_token_id()).strip()
        self.network = (network_name or hedera_network()).strip()

    @property
    def provider_name(self) -> str:
        return "usdc"

    @property
    def supported_currencies(self) -> tuple[str, ...]:
        return ("USD", "USDC")

    @property
    def is_configured(self) -> bool:
        return bool(self.receiving_account_id and self.token_id)

    def _memo_for(self, case_id: str) -> str:
        return f"{DEFAULT_MEMO_PREFIX}:{case_id}"

    async def create_payment(
        self, amount: Decimal, currency: str = "USDC", metadata: dict[str, Any] | None = None
    ) -> PaymentResult:
        self.assert_supported(currency)
        details = metadata or {}
        case_id = str(details.get("case_id") or details.get("invoice_id") or "UNKNOWN")

        return PaymentResult(
            payment_id=f"hedera_usdc_{case_id}",
            status=PaymentStatus.PENDING,
            amount=amount,
            currency="USDC",
            checkout_url=None,
            metadata={
                "display_type": "hedera_usdc",
                "network": self.network,
                "receiving_account_id": self.receiving_account_id,
                "token_id": self.token_id,
                "memo": self._memo_for(case_id),
                "settlement_eta_seconds": SETTLEMENT_ETA_SECONDS,
                "settlement_detection": "mirror_node_poll",
                "receiving_account_configured": self.is_configured,
                "operator_configured": hedera_is_configured(),
            },
        )

    async def verify_payment(self, payment_id: str) -> PaymentStatus:
        return PaymentStatus.PENDING

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        return {
            "payment_id": payment_id,
            "provider": self.provider_name,
            "status": PaymentStatus.PENDING.value,
            "receiving_account_id": self.receiving_account_id,
            "token_id": self.token_id,
            "network": self.network,
            "note": (
                "Settlement is confirmed by the Mirror Node poller. Include the case "
                "reference as the transaction memo."
            ),
        }
