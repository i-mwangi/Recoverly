from __future__ import annotations

import logging
from typing import Iterator, Mapping

from src.payments.base import PaymentProvider, PaymentProviderError

log = logging.getLogger("recoverly.payments.router")


class UnknownPaymentMethodError(PaymentProviderError):
    pass


class PaymentRouter:
    def __init__(self, providers: Mapping[str, PaymentProvider]) -> None:
        self._providers = {name.lower(): provider for name, provider in providers.items()}
        log.info(
            "payment router ready with %d providers: %s",
            len(self._providers),
            ", ".join(sorted(self._providers)) or "none",
        )

    def route(self, payment_method: str) -> PaymentProvider | None:
        if not payment_method:
            return None
        return self._providers.get(payment_method.lower())

    def require(self, payment_method: str) -> PaymentProvider:
        provider = self.route(payment_method)
        if provider is None:
            raise UnknownPaymentMethodError(
                f"no provider registered for {payment_method!r}; "
                f"available: {', '.join(self.methods()) or 'none'}"
            )
        return provider

    def methods(self) -> list[str]:
        return sorted(self._providers)

    def register(self, payment_method: str, provider: PaymentProvider) -> None:
        self._providers[payment_method.lower()] = provider

    def __len__(self) -> int:
        return len(self._providers)

    def __contains__(self, payment_method: object) -> bool:
        return isinstance(payment_method, str) and payment_method.lower() in self._providers

    def __iter__(self) -> Iterator[str]:
        return iter(self.methods())
