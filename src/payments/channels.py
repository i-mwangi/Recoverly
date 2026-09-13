from __future__ import annotations

import os
from typing import Any, Final

from src.agents._utils import safe_float
from src.payments.base import PaymentProvider
from src.payments.router import PaymentRouter
from src.payments.paystack import PaystackProvider
from src.payments.usdc_display import UsdcDepositProvider
from src.payments.wire_display import WireDisplayProvider

CHANNEL_USDC: Final = "usdc"
CHANNEL_CARD: Final = "card"
CHANNEL_WIRE: Final = "wire"
CHANNEL_ACH: Final = "ach"

CHANNEL_LABELS: Final[dict[str, str]] = {
    CHANNEL_USDC: "USDC settlement on Hedera",
    CHANNEL_CARD: "Card payment via Paystack",
    CHANNEL_WIRE: "Wire transfer to the operating account",
    CHANNEL_ACH: "ACH transfer to the operating account",
}

MANUAL_CONFIRMATION_CHANNELS: Final = frozenset({CHANNEL_WIRE, CHANNEL_ACH})

USDC_FLOOR_USD: Final = 10_000
WIRE_FLOOR_USD: Final = 50_000


def outstanding_for(case: dict[str, Any]) -> float:
    if case.get("status") in {"closed_recovered", "closed_external"}:
        return 0.0
    outstanding = safe_float(case.get("outstanding_balance_usd"), 0.0)
    if outstanding <= 0:
        gross = safe_float(case.get("amount_usd"), 0.0)
        outstanding = gross * (1.0 - safe_float(case.get("deposit_pct"), 0.0))
    return max(
        0.0, round(outstanding - safe_float(case.get("amount_paid_usd"), 0.0), 2)
    )


def select_channel(case: dict[str, Any]) -> str:
    explicit = str(case.get("method") or case.get("channel") or "").lower()
    if explicit in CHANNEL_LABELS:
        return explicit

    outstanding = outstanding_for(case)
    if outstanding >= WIRE_FLOOR_USD:
        return CHANNEL_WIRE
    if outstanding >= USDC_FLOOR_USD:
        return CHANNEL_USDC
    return CHANNEL_CARD


def channel_label(channel: str) -> str:
    return CHANNEL_LABELS.get(channel, channel)


def build_provider(channel: str) -> PaymentProvider:
    if channel == CHANNEL_CARD:
        return PaystackProvider()
    if channel == CHANNEL_USDC:
        return UsdcDepositProvider()
    if channel in MANUAL_CONFIRMATION_CHANNELS:
        return WireDisplayProvider()
    raise ValueError(f"unknown payment channel: {channel!r}")


def build_router() -> PaymentRouter:
    wire = WireDisplayProvider()
    return PaymentRouter(
        {
            CHANNEL_CARD: PaystackProvider(),
            CHANNEL_USDC: UsdcDepositProvider(),
            CHANNEL_WIRE: wire,
            CHANNEL_ACH: wire,
        }
    )


def dry_run_enabled() -> bool:
    requested = os.getenv("PAYMENT_DRY_RUN", "").strip() == "1"
    allowed = os.getenv("RECOVERLY_ALLOW_PAYMENT_DRY_RUN", "").strip() == "1"
    if requested and not allowed:
        raise RuntimeError(
            "PAYMENT_DRY_RUN=1 is set without RECOVERLY_ALLOW_PAYMENT_DRY_RUN=1. "
            "Unset PAYMENT_DRY_RUN to send real settlements, or add the allow flag "
            "if the dry run is intentional."
        )
    return requested and allowed
