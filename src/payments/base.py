from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


class PaymentStatus(enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in {PaymentStatus.COMPLETED, PaymentStatus.FAILED, PaymentStatus.EXPIRED}


class PaymentProviderError(RuntimeError):
    pass


class PaymentNotFoundError(PaymentProviderError):
    pass


@dataclass(frozen=True, slots=True)
class PaymentResult:
    payment_id: str
    status: PaymentStatus
    amount: Decimal
    currency: str
    checkout_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "payment_id": self.payment_id,
            "status": self.status.value,
            "amount": str(self.amount),
            "currency": self.currency,
            "checkout_url": self.checkout_url,
            "metadata": self.metadata,
        }


class PaymentProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str: ...

    @property
    @abstractmethod
    def supported_currencies(self) -> tuple[str, ...]: ...

    @abstractmethod
    async def create_payment(
        self, amount: Decimal, currency: str, metadata: dict[str, Any]
    ) -> PaymentResult: ...

    @abstractmethod
    async def verify_payment(self, payment_id: str) -> PaymentStatus: ...

    @abstractmethod
    async def get_payment(self, payment_id: str) -> dict[str, Any]: ...

    def supports(self, currency: str) -> bool:
        return currency.upper() in {code.upper() for code in self.supported_currencies}

    def assert_supported(self, currency: str) -> None:
        if not self.supports(currency):
            raise PaymentProviderError(
                f"{self.provider_name} does not support {currency}; "
                f"supported: {', '.join(self.supported_currencies)}"
            )
