from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final
from urllib.parse import quote

import httpx

log = logging.getLogger("recoverly.payments.hedera")

MAINNET_MIRROR_NODE: Final = "https://mainnet-public.mirrornode.hedera.com"
TESTNET_MIRROR_NODE: Final = "https://testnet.mirrornode.hedera.com"
PREVIEWNET_MIRROR_NODE: Final = "https://previewnet.mirrornode.hedera.com"

MIRROR_NODES: Final[dict[str, str]] = {
    "mainnet": MAINNET_MIRROR_NODE,
    "testnet": TESTNET_MIRROR_NODE,
    "previewnet": PREVIEWNET_MIRROR_NODE,
}

USDC_TOKEN_IDS: Final[dict[str, str]] = {
    "mainnet": "0.0.456858",
    "testnet": "0.0.429274",
}

USDC_DECIMALS: Final = 6
USDC_SCALE: Final = Decimal(10) ** USDC_DECIMALS

DEFAULT_NETWORK: Final = "testnet"
DEFAULT_TIMEOUT_SECONDS: Final = 15.0
DEFAULT_TX_LIMIT: Final = 25

ACCOUNT_ID_RE: Final = re.compile(r"^\d+\.\d+\.\d+$")


class HederaError(RuntimeError):
    pass


def validate_account_id(account_id: str) -> str:
    if not isinstance(account_id, str) or not ACCOUNT_ID_RE.match(account_id.strip()):
        raise HederaError(f"account id must look like shard.realm.num, got {account_id!r}")
    return account_id.strip()


def network() -> str:
    return os.getenv("HEDERA_NETWORK", DEFAULT_NETWORK).strip().lower()


def mirror_node_url() -> str:
    return os.getenv("HEDERA_MIRROR_NODE") or MIRROR_NODES.get(network(), TESTNET_MIRROR_NODE)


def usdc_token_id() -> str:
    override = os.getenv("HEDERA_USDC_TOKEN_ID", "").strip()
    return override or USDC_TOKEN_IDS.get(network(), USDC_TOKEN_IDS["testnet"])


def operator_account_id() -> str:
    return os.getenv("HEDERA_OPERATOR_ID", "").strip()


def operator_private_key() -> str:
    return os.getenv("HEDERA_OPERATOR_KEY", "").strip()


def receiving_account_id() -> str:
    return os.getenv("HEDERA_RECEIVING_ACCOUNT_ID", "").strip()


def transaction_explorer_url(transaction_id: str, network_name: str | None = None) -> str:
    """Return the HashScan URL for a Hedera transaction, or an empty string."""
    if not transaction_id.strip():
        return ""
    selected_network = (network_name or network()).strip().lower()
    if selected_network not in {"mainnet", "testnet", "previewnet"}:
        selected_network = DEFAULT_NETWORK
    return f"https://hashscan.io/{selected_network}/transaction/{quote(transaction_id, safe='.@')}"


def is_configured() -> bool:
    return bool(operator_account_id() and operator_private_key())


def _tinybars_to_usdc(units: Any) -> Decimal:
    try:
        return (Decimal(str(units)) / USDC_SCALE).quantize(Decimal("0.000001"))
    except Exception:
        return Decimal(0)


@dataclass(frozen=True, slots=True)
class HederaTransfer:
    transaction_id: str
    consensus_ts: str
    amount_usdc: Decimal
    sender_account_id: str
    memo: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "explorer_url": transaction_explorer_url(self.transaction_id),
            "consensus_ts": self.consensus_ts,
            "amount_usdc": str(self.amount_usdc),
            "sender_account_id": self.sender_account_id,
            "memo": self.memo,
        }


class HederaClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or mirror_node_url()).rstrip("/")

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            response = httpx.get(
                f"{self.base_url}{path}",
                params=params or {},
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise HederaError(f"mirror node request {path} failed: {error}") from error
        return response.json()

    def usdc_balance(self, account_id: str) -> Decimal:
        validate_account_id(account_id)
        token = usdc_token_id()
        data = self._get(
            f"/api/v1/accounts/{account_id}/tokens",
            params={"token.id": token, "limit": 1},
        )

        for entry in data.get("tokens", []) or []:
            if entry.get("token_id") == token:
                return _tinybars_to_usdc(entry.get("balance", 0))
        return Decimal(0)

    def recent_inbound_transfers(
        self, account_id: str, limit: int = DEFAULT_TX_LIMIT
    ) -> list[HederaTransfer]:
        validate_account_id(account_id)
        token = usdc_token_id()

        data = self._get(
            "/api/v1/transactions",
            params={
                "account.id": account_id,
                "transactiontype": "CRYPTOTRANSFER",
                "result": "success",
                "order": "desc",
                "limit": min(limit, 100),
            },
        )

        transfers: list[HederaTransfer] = []
        for tx in data.get("transactions", []) or []:
            memo = str(tx.get("memo_base64", ""))
            try:
                import base64

                memo = base64.b64decode(memo).decode("utf-8", "replace") if memo else ""
            except Exception:
                memo = ""

            token_transfers = tx.get("token_transfers") or []
            inbound_units = 0
            sender = ""
            for transfer in token_transfers:
                if transfer.get("token_id") != token:
                    continue
                amount = int(transfer.get("amount") or 0)
                if transfer.get("account") == account_id and amount > 0:
                    inbound_units += amount
                elif amount < 0 and not sender:
                    sender = str(transfer.get("account") or "")

            if inbound_units <= 0:
                continue

            transfers.append(
                HederaTransfer(
                    transaction_id=str(tx.get("transaction_id", "")),
                    consensus_ts=str(tx.get("consensus_timestamp", "")),
                    amount_usdc=_tinybars_to_usdc(inbound_units),
                    sender_account_id=sender,
                    memo=memo,
                )
            )

        return transfers

    def send_usdc(self, to_account_id: str, amount_usdc: str | Decimal, memo: str = "") -> dict[str, Any]:
        if not is_configured():
            raise HederaError("HEDERA_OPERATOR_ID and HEDERA_OPERATOR_KEY are required to send USDC")

        try:
            from hedera_sdk_python import (
                Client,
                PrivateKey,
                TokenId,
                AccountId,
                TransferTransaction,
            )
        except ImportError as error:
            raise HederaError(
                "the hedera-sdk-python package is not installed; "
                "run pip install hedera-sdk-python"
            ) from error

        client = Client.for_name(network())
        client.set_operator(
            AccountId.from_string(operator_account_id()),
            PrivateKey.from_string(operator_private_key()),
        )

        amount = Decimal(str(amount_usdc))
        units = int((amount * USDC_SCALE).quantize(Decimal("1")))
        token = TokenId.from_string(usdc_token_id())

        transaction = (
            TransferTransaction()
            .add_token_transfer(token, AccountId.from_string(operator_account_id()), -units)
            .add_token_transfer(token, AccountId.from_string(to_account_id), units)
        )
        if memo:
            transaction.set_transaction_memo(memo[:100])

        submit = transaction.execute(client)
        receipt = submit.get_receipt(client)

        return {
            "transaction_id": str(submit.transaction_id),
            "explorer_url": transaction_explorer_url(str(submit.transaction_id)),
            "status": str(receipt.status),
            "amount_usdc": str(amount),
            "memo": memo,
            "to_account_id": to_account_id,
        }
