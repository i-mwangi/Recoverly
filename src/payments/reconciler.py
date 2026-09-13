from __future__ import annotations

import enum
import logging
import math
from dataclasses import dataclass
from typing import Any, Final

from src.agents._case_state import (
    InvalidCaseIdError,
    case_path,
    is_halted,
    mutate_case_state,
)
from src.agents._utils import safe_float
from src.agents.base import audit

log = logging.getLogger("recoverly.payments.reconciler")

AGENT: Final = "reconciler"
OVERPAYMENT_TOLERANCE_USD: Final = 0.01


class SettlementStatus(str, enum.Enum):
    UNMATCHED = "unmatched"
    PARTIAL = "partial"
    SETTLED = "settled"
    OVERPAID = "overpaid"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    status: SettlementStatus
    case_id: str
    amount_applied_usd: float = 0.0
    paid_to_date_usd: float = 0.0
    outstanding_usd: float = 0.0
    remaining_usd: float = 0.0
    overpaid_usd: float = 0.0
    transaction_id: str = ""
    halted: bool = False

    @property
    def closes_case(self) -> bool:
        return self.status in {SettlementStatus.SETTLED, SettlementStatus.OVERPAID}

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "case_id": self.case_id,
            "amount_applied_usd": self.amount_applied_usd,
            "paid_to_date_usd": self.paid_to_date_usd,
            "outstanding_usd": self.outstanding_usd,
            "remaining_usd": self.remaining_usd,
            "overpaid_usd": self.overpaid_usd,
            "transaction_id": self.transaction_id,
            "halted": self.halted,
        }


def _outstanding_for(state: dict[str, Any]) -> float:
    outstanding = safe_float(state.get("outstanding_balance_usd"), 0.0)
    if outstanding > 0:
        return outstanding

    gross = safe_float(state.get("amount_usd"), 0.0)
    return round(gross * (1.0 - safe_float(state.get("deposit_pct"), 0.0)), 2)


def classify(paid_to_date: float, outstanding: float) -> SettlementStatus:
    if outstanding <= 0:
        return (
            SettlementStatus.SETTLED if paid_to_date > 0 else SettlementStatus.UNMATCHED
        )
    if paid_to_date > outstanding + OVERPAYMENT_TOLERANCE_USD:
        return SettlementStatus.OVERPAID
    if paid_to_date >= outstanding - OVERPAYMENT_TOLERANCE_USD:
        return SettlementStatus.SETTLED
    return SettlementStatus.PARTIAL


def reconcile_inbound(
    case_id: str, amount_usd: str | float, transaction_id: str = ""
) -> ReconcileResult:
    applied = safe_float(amount_usd, 0.0)

    if not math.isfinite(applied) or applied <= 0:
        log.warning("ignoring a non-positive inbound amount for case %s", case_id)
        return ReconcileResult(
            SettlementStatus.UNMATCHED, case_id, transaction_id=transaction_id
        )

    try:
        if not case_path(case_id).is_file():
            return ReconcileResult(
                SettlementStatus.UNMATCHED, case_id, transaction_id=transaction_id
            )
        halted = is_halted(case_id)
    except InvalidCaseIdError:
        log.error("inbound payment carried an unusable case reference: %r", case_id)
        return ReconcileResult(
            SettlementStatus.UNMATCHED, case_id, transaction_id=transaction_id
        )

    ledger: dict[str, Any] = {}

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        outstanding = _outstanding_for(state)
        seen = list(state.get("payment_transaction_ids") or [])
        delta = 0.0 if transaction_id and transaction_id in seen else applied
        if outstanding <= 0 or not math.isfinite(outstanding):
            delta = 0.0
        paid_to_date = round(safe_float(state.get("amount_paid_usd"), 0.0) + delta, 2)
        status = (
            classify(paid_to_date, outstanding)
            if outstanding > 0
            else SettlementStatus.UNMATCHED
        )

        ledger.update(
            {
                "outstanding": outstanding,
                "paid_to_date": paid_to_date,
                "status": status,
                "applied": delta,
            }
        )

        patch: dict[str, Any] = {
            "amount_paid_usd": paid_to_date,
            "amount_balance_usd": max(0.0, round(outstanding - paid_to_date, 2)),
        }
        if transaction_id and delta:
            patch["payment_transaction_ids"] = [*seen, transaction_id]
        if (
            status in {SettlementStatus.SETTLED, SettlementStatus.OVERPAID}
            and not halted
        ):
            patch["status"] = "closed_recovered"
            patch["current_stage"] = "settled"
        return patch

    mutate_case_state(case_id, apply)

    outstanding = float(ledger["outstanding"])
    paid_to_date = float(ledger["paid_to_date"])
    status = ledger["status"]

    result = ReconcileResult(
        status=status,
        case_id=case_id,
        amount_applied_usd=ledger["applied"],
        paid_to_date_usd=paid_to_date,
        outstanding_usd=outstanding,
        remaining_usd=max(0.0, round(outstanding - paid_to_date, 2)),
        overpaid_usd=max(0.0, round(paid_to_date - outstanding, 2)),
        transaction_id=transaction_id,
        halted=halted,
    )

    audit(case_id, AGENT, "payment_reconciled", result.as_dict())
    log.info(
        "case %s reconciled as %s: applied %.2f of %.2f outstanding",
        case_id,
        status.value,
        applied,
        outstanding,
    )
    return result
