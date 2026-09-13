from __future__ import annotations

import logging
import os
import uuid
from decimal import Decimal
from typing import Any, Final

import httpx

from src.payments.base import (
    PaymentProvider,
    PaymentProviderError,
    PaymentResult,
    PaymentStatus,
)

log = logging.getLogger("recoverly.payments.paystack")

PAYSTACK_API_BASE: Final = "https://api.paystack.co"
DEFAULT_CURRENCY: Final = "USD"
DEFAULT_TIMEOUT_SECONDS: Final = 15.0
SUPPORTED_CURRENCIES: Final[tuple[str, ...]] = ("USD", "KES", "NGN", "GHS", "ZAR")
CURRENCY_SMALLEST_UNIT: Final = 100

PAYMENT_ID_PREFIX: Final = "paystack_"

STATUS_MAP: Final[dict[str, PaymentStatus]] = {
    "success": PaymentStatus.COMPLETED,
    "successful": PaymentStatus.COMPLETED,
    "pending": PaymentStatus.PENDING,
    "ongoing": PaymentStatus.PENDING,
    "queued": PaymentStatus.PENDING,
    "processing": PaymentStatus.PENDING,
    "abandoned": PaymentStatus.EXPIRED,
    "reversed": PaymentStatus.FAILED,
    "failed": PaymentStatus.FAILED,
}


class PaystackProvider(PaymentProvider):
    def __init__(
        self,
        secret_key: str | None = None,
        callback_url: str | None = None,
    ) -> None:
        self._secret_key = (secret_key or os.getenv("PAYSTACK_SECRET_KEY", "")).strip()
        self._callback_url = (
            callback_url
            or os.getenv("PAYSTACK_CALLBACK_URL", "").strip()
            or "https://example.com/paid"
        )

    @property
    def provider_name(self) -> str:
        return "paystack"

    @property
    def supported_currencies(self) -> tuple[str, ...]:
        return SUPPORTED_CURRENCIES

    @property
    def is_configured(self) -> bool:
        return bool(self._secret_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._secret_key}",
            "Content-Type": "application/json",
        }

    def _require_secret(self) -> None:
        if not self._secret_key:
            raise PaymentProviderError("PAYSTACK_SECRET_KEY is not configured")

    def _reference(self, payment_id: str) -> str:
        if not payment_id:
            raise PaymentProviderError("payment_id is required")
        return payment_id.removeprefix(PAYMENT_ID_PREFIX)

    async def create_payment(
        self, amount: Decimal, currency: str, metadata: dict[str, Any]
    ) -> PaymentResult:
        if amount <= 0:
            raise PaymentProviderError(f"amount must be positive, got {amount}")
        self.assert_supported(currency)
        self._require_secret()

        customer_email = str(metadata.get("customer_email") or metadata.get("email") or "").strip()
        if not customer_email:
            raise PaymentProviderError("paystack requires a customer_email in metadata")

        normalized_currency = currency.upper()
        smallest_units = int((Decimal(str(amount)) * CURRENCY_SMALLEST_UNIT).quantize(Decimal("1")))
        reference = str(metadata.get("reference") or f"recoverly-{uuid.uuid4().hex[:16]}")

        body: dict[str, Any] = {
            "amount": smallest_units,
            "email": customer_email,
            "currency": normalized_currency,
            "reference": reference,
            "callback_url": str(metadata.get("callback_url") or self._callback_url),
            "metadata": {key: str(value) for key, value in metadata.items() if key != "email"},
        }

        try:
            response = httpx.post(
                f"{PAYSTACK_API_BASE}/transaction/initialize",
                headers=self._headers(),
                json=body,
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            raise PaymentProviderError(f"paystack initialize failed: {error}") from error

        if not payload.get("status"):
            raise PaymentProviderError(
                f"paystack rejected initialize: {payload.get('message', 'unknown reason')}"
            )

        data = payload.get("data") or {}
        checkout_url = str(data.get("authorization_url") or "") or None

        return PaymentResult(
            payment_id=f"{PAYMENT_ID_PREFIX}{reference}",
            status=PaymentStatus.PENDING,
            amount=Decimal(str(amount)),
            currency=normalized_currency,
            checkout_url=checkout_url,
            metadata={
                "paystack_reference": reference,
                "paystack_access_code": str(data.get("access_code") or ""),
                "customer_email": customer_email,
            },
        )

    async def _retrieve(self, payment_id: str) -> dict[str, Any]:
        self._require_secret()
        reference = self._reference(payment_id)

        try:
            response = httpx.get(
                f"{PAYSTACK_API_BASE}/transaction/verify/{reference}",
                headers=self._headers(),
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as error:
            raise PaymentProviderError(f"paystack verify failed: {error}") from error

        if not payload.get("status"):
            raise PaymentProviderError(
                f"paystack rejected verify: {payload.get('message', 'unknown reason')}"
            )

        return payload.get("data") or {}

    async def verify_payment(self, payment_id: str) -> PaymentStatus:
        data = await self._retrieve(payment_id)
        return STATUS_MAP.get(str(data.get("status", "")).lower(), PaymentStatus.PENDING)

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        data = await self._retrieve(payment_id)
        return {"payment_id": payment_id, "provider": self.provider_name, **data}
