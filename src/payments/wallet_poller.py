from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Final

from src.agents.base import audit
from src.agents._case_state import load_case_state
from src.concierge.cards import post_card
from src.config import PROJECT_ROOT, settings
from src.payments.dedup import already_processed, record_processed
from src.payments.hedera_client import (
    HederaClient,
    HederaError,
    HederaTransfer,
    is_configured as hedera_configured,
    receiving_account_id,
    transaction_explorer_url,
)
from src.payments.notify import send_payment_received
from src.payments.reconciler import SettlementStatus, reconcile_inbound
from src.payments.wallet_ledger import case_for_address
from src.local_console.service import append_case_activity
from src.utils.filelock import write_atomic

log = logging.getLogger("recoverly.payments.wallet_poller")

AGENT: Final = "wallet_poller"
STATE_PATH: Final = PROJECT_ROOT / "data" / "wallet_poller_state.json"
DEFAULT_INTERVAL_SECONDS: Final = 30
MEMO_CASE_PREFIX: Final = "recoverly:"


@dataclass(slots=True)
class PollerState:
    last_consensus_ts: str = ""
    seen_transaction_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_consensus_ts": self.last_consensus_ts,
            "seen_transaction_ids": self.seen_transaction_ids[-500:],
        }


def load_state(path=STATE_PATH) -> PollerState:
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return PollerState()

    if not isinstance(stored, dict):
        return PollerState()

    return PollerState(
        last_consensus_ts=str(stored.get("last_consensus_ts", "")),
        seen_transaction_ids=[str(tid) for tid in stored.get("seen_transaction_ids", []) or []],
    )


def save_state(state: PollerState, path=STATE_PATH) -> None:
    write_atomic(path, json.dumps(state.as_dict()))


def poll_interval_seconds() -> int:
    try:
        value = (
            os.getenv("ONCHAIN_STABLECOIN_POLL_INTERVAL_SEC", "").strip()
            or os.getenv("WALLET_POLL_INTERVAL_SEC", str(DEFAULT_INTERVAL_SECONDS))
        )
        return max(5, int(value))
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS


def case_id_from_transfer(transfer: HederaTransfer) -> str | None:
    memo = transfer.memo.strip()
    if memo.startswith(MEMO_CASE_PREFIX):
        return memo[len(MEMO_CASE_PREFIX):].strip() or None

    ledger_case = case_for_address(transfer.sender_account_id)
    return ledger_case


def apply_transfer(transfer: HederaTransfer) -> dict[str, Any]:
    if already_processed(transfer.transaction_id):
        return {
            "ok": True,
            "deduplicated": True,
            "transaction_id": transfer.transaction_id,
        }

    case_id = case_id_from_transfer(transfer)
    if not case_id:
        audit(
            "UNKNOWN",
            AGENT,
            "hedera_transfer_unmatched",
            {
                "transaction_id": transfer.transaction_id,
                "sender_account_id": transfer.sender_account_id,
                "amount_usdc": str(transfer.amount_usdc),
                "memo": transfer.memo,
            },
        )
        log.warning(
            "hedera transfer %s from %s carried no matchable case reference",
            transfer.transaction_id,
            transfer.sender_account_id,
        )
        return {"ok": True, "matched": False, "transaction_id": transfer.transaction_id}

    result = reconcile_inbound(case_id, str(transfer.amount_usdc), transfer.transaction_id)
    record_processed(transfer.transaction_id)

    if result.status is not SettlementStatus.UNMATCHED:
        explorer_url = transaction_explorer_url(transfer.transaction_id)
        receipt = (
            f"Payment Agent confirmed a Hedera USDC transfer of ${transfer.amount_usdc:,.2f}.\n\n"
            f"Status: {result.status.value}\n"
            f"Remaining balance: ${result.remaining_usd:,.2f}\n"
            f"Transaction: {explorer_url}"
        )
        case = load_case_state(case_id)
        post_card(
            case_id,
            f"Payment received for case {case_id}",
            receipt,
            card_kind="payment_received",
            case_meta=case,
        )
        append_case_activity(
            case_id,
            "payment",
            receipt,
            reasoning="Matched the Hedera transaction to the case memo, reconciled the balance, and sent the settlement notification.",
        )
        send_payment_received(
            case_id=case_id,
            buyer_name=transfer.sender_account_id,
            invoice_no=str(result.case_id),
            amount_paid=result.amount_applied_usd,
            remaining=result.remaining_usd,
        )

    return {"ok": True, "matched": True, "case_id": case_id, **result.as_dict()}


def poll_once(client: HederaClient, state: PollerState) -> dict[str, Any]:
    account_id = receiving_account_id()
    if not account_id:
        return {"ok": False, "reason": "HEDERA_RECEIVING_ACCOUNT_ID is not configured"}

    try:
        transfers = client.recent_inbound_transfers(account_id)
    except HederaError as error:
        log.warning("hedera mirror node lookup failed: %s", error)
        return {"ok": False, "reason": str(error)}

    new_transfers = [
        transfer
        for transfer in transfers
        if transfer.transaction_id not in state.seen_transaction_ids
    ]
    if not new_transfers:
        return {"ok": True, "changed": False, "checked": len(transfers)}

    applied: list[dict[str, Any]] = []
    for transfer in reversed(new_transfers):
        applied.append(apply_transfer(transfer))
        state.seen_transaction_ids.append(transfer.transaction_id)
        state.last_consensus_ts = transfer.consensus_ts

    save_state(state)
    return {"ok": True, "changed": True, "applied": applied}


async def run_forever() -> None:
    if not hedera_configured() and not receiving_account_id():
        log.error("hedera is not configured, so the poller cannot start")
        return

    client = HederaClient()
    state = load_state()
    interval = poll_interval_seconds()
    log.info(
        "watching hedera account %s every %d seconds via %s",
        receiving_account_id(),
        interval,
        client.base_url,
    )

    while True:
        try:
            poll_once(client, state)
        except Exception:
            log.exception("the hedera poll failed, continuing after the interval")
        await asyncio.sleep(interval)


def main() -> int:
    logging.basicConfig(level=settings.log_level)
    try:
        asyncio.run(run_forever())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
