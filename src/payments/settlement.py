from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from src.agents.base import audit
from src.config import PROJECT_ROOT
from src.payments.hedera_client import (
    HederaClient,
    HederaError,
    is_configured as hedera_is_configured,
    receiving_account_id,
    transaction_explorer_url,
    validate_account_id,
)
from src.personas import normalize_persona
from src.utils.filelock import append_jsonl

log = logging.getLogger("recoverly.payments.settlement")

AGENT: Final = "settlement"
PAYMENT_INTENTS: Final = PROJECT_ROOT / "data" / "payment_intents.jsonl"


class SettlementError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SettlementResult:
    ok: bool
    transaction_id: str = ""
    explorer_url: str = ""
    status: str = ""
    amount_usdc: str = ""
    case_id: str = ""
    memo: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "transaction_id": self.transaction_id,
            "explorer_url": self.explorer_url,
            "status": self.status,
            "amount_usdc": self.amount_usdc,
            "case_id": self.case_id,
            "memo": self.memo,
            "error": self.error,
        }


def buyer_account_id(persona: str) -> str:
    key = f"HEDERA_BUYER_{normalize_persona(persona).upper()}_ACCOUNT_ID"
    return os.getenv(key, "").strip()


def record_intent(
    case_id: str, transaction_id: str, persona: str, amount_usdc: str, memo: str
) -> None:
    append_jsonl(
        PAYMENT_INTENTS,
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "case_id": case_id,
            "transaction_id": transaction_id,
            "buyer_persona": persona,
            "amount_usdc": amount_usdc,
            "memo": memo,
        },
    )


def lookup_case_for_transaction(transaction_id: str) -> str | None:
    if not PAYMENT_INTENTS.exists() or not transaction_id:
        return None

    with PAYMENT_INTENTS.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("transaction_id") == transaction_id:
                return str(record.get("case_id", "")) or None

    return None


async def settle_from_buyer_async(
    case_id: str, buyer_persona: str, amount_usdc: str
) -> SettlementResult:
    if not hedera_is_configured():
        raise SettlementError(
            "HEDERA_OPERATOR_ID and HEDERA_OPERATOR_KEY are required to move USDC"
        )

    destination = receiving_account_id()
    if not destination:
        raise SettlementError("HEDERA_RECEIVING_ACCOUNT_ID is not configured")

    buyer_id = buyer_account_id(buyer_persona)
    if not buyer_id:
        raise SettlementError(f"no hedera account is configured for buyer {buyer_persona!r}")

    validate_account_id(buyer_id)
    validate_account_id(destination)
    from src.payments.hedera_client import operator_account_id
    if buyer_id != operator_account_id():
        raise SettlementError("The configured signing account must be the buyer account; refusing to debit the operator for another buyer")

    memo = f"recoverly:{case_id}"
    audit(
        case_id,
        AGENT,
        "settlement_intent",
        {
            "buyer_persona": buyer_persona,
            "buyer_account_id": buyer_id,
            "amount_usdc": amount_usdc,
            "destination_account_id": destination,
            "memo": memo,
        },
    )

    client = HederaClient()
    try:
        transfer = await asyncio.to_thread(client.send_usdc, destination, amount_usdc, memo)
    except HederaError as error:
        audit(case_id, AGENT, "settlement_failed", {"error": str(error)})
        return SettlementResult(ok=False, case_id=case_id, memo=memo, error=str(error))

    transaction_id = str(transfer.get("transaction_id", ""))
    record_intent(case_id, transaction_id, buyer_persona, amount_usdc, memo)

    result = SettlementResult(
        ok=True,
        transaction_id=transaction_id,
        explorer_url=str(transfer.get("explorer_url") or transaction_explorer_url(transaction_id)),
        status=str(transfer.get("status", "")),
        amount_usdc=amount_usdc,
        case_id=case_id,
        memo=memo,
    )
    audit(case_id, AGENT, "settlement_submitted", result.as_dict())
    log.info("submitted %s USDC on hedera for case %s", amount_usdc, case_id)
    return result


def settle_from_buyer(case_id: str, buyer_persona: str, amount_usdc: str) -> SettlementResult:
    return asyncio.run(settle_from_buyer_async(case_id, buyer_persona, amount_usdc))
