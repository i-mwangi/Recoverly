from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from src.payments.base import (
    PaymentProvider,
    PaymentProviderError,
    PaymentResult,
    PaymentStatus,
)

log = logging.getLogger("recoverly.payments.x402")

DEFAULT_FACILITATOR_URL: Final = "https://x402.org/facilitator"
DEFAULT_NETWORK: Final = "base-sepolia"
PAYMENT_ID_PREFIX: Final = "x402_"
PAYMENT_ID_ENTROPY_CHARS: Final = 16


@dataclass(frozen=True, slots=True)
class X402Config:
    wallet_address: str = ""
    facilitator_url: str = DEFAULT_FACILITATOR_URL
    network: str = DEFAULT_NETWORK

    @property
    def enabled(self) -> bool:
        return bool(self.wallet_address)

    @classmethod
    def from_env(cls) -> X402Config:
        return cls(
            wallet_address=os.getenv("X402_WALLET_ADDRESS", "").strip(),
            facilitator_url=os.getenv("X402_FACILITATOR_URL", "").strip()
            or DEFAULT_FACILITATOR_URL,
            network=os.getenv("X402_CHAIN", "").strip() or DEFAULT_NETWORK,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "wallet_address": self.wallet_address,
            "facilitator_url": self.facilitator_url,
            "network": self.network,
            "enabled": self.enabled,
        }


class X402Provider(PaymentProvider):
    SUPPORTED: Final[tuple[str, ...]] = ("USDC",)

    def __init__(self, config: X402Config | None = None) -> None:
        self.config = config or X402Config.from_env()
        if not self.config.enabled:
            log.info("the x402 provider has no wallet configured, so it is inactive")

    @property
    def provider_name(self) -> str:
        return "x402"

    @property
    def supported_currencies(self) -> tuple[str, ...]:
        return self.SUPPORTED

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise PaymentProviderError(
                "the x402 provider needs X402_WALLET_ADDRESS to be configured"
            )

    async def create_payment(
        self, amount: Decimal, currency: str, metadata: dict[str, Any]
    ) -> PaymentResult:
        self._require_enabled()
        self.assert_supported(currency)

        if amount <= 0:
            raise PaymentProviderError(f"amount must be positive, got {amount}")

        return PaymentResult(
            payment_id=f"{PAYMENT_ID_PREFIX}{uuid.uuid4().hex[:PAYMENT_ID_ENTROPY_CHARS]}",
            status=PaymentStatus.PENDING,
            amount=amount,
            currency=currency.upper(),
            checkout_url=None,
            metadata={**self.config.as_dict(), **metadata},
        )

    async def verify_payment(self, payment_id: str) -> PaymentStatus:
        if not payment_id:
            raise PaymentProviderError("payment_id is required")
        return PaymentStatus.PENDING

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        if not payment_id:
            raise PaymentProviderError("payment_id is required")
        return {
            "payment_id": payment_id,
            "provider": self.provider_name,
            "status": PaymentStatus.PENDING.value,
            **self.config.as_dict(),
        }
