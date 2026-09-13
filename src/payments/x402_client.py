from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

log = logging.getLogger("recoverly.payments.x402_client")

DEFAULT_NETWORK: Final = "eip155:8453"
BASE_SEPOLIA_NETWORK: Final = "eip155:84532"
SUCCESS_MIN: Final = 200
SUCCESS_MAX: Final = 300


class X402ClientError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ApiCallResult:
    status_code: int
    data: Any
    transaction_hash: str | None = None
    cost_usdc: Decimal | None = None
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return SUCCESS_MIN <= self.status_code < SUCCESS_MAX

    def as_dict(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "data": self.data,
            "transaction_hash": self.transaction_hash,
            "cost_usdc": str(self.cost_usdc) if self.cost_usdc is not None else None,
            "headers": self.headers,
            "error": self.error,
            "ok": self.ok,
        }


class X402AgentClient:
    def __init__(self, private_key: str, network: str = DEFAULT_NETWORK) -> None:
        if not private_key:
            raise X402ClientError("a private key is required")

        self._private_key = private_key
        self._network = network
        self._client = None
        self._http_client = None
        self._wallet_address: str | None = None

    @classmethod
    def from_env(cls) -> "X402AgentClient":
        key = (os.getenv("EVM_PRIVATE_KEY") or os.getenv("BUYER_PRIVATE_KEY") or "").strip()
        if not key:
            raise X402ClientError(
                "either EVM_PRIVATE_KEY or BUYER_PRIVATE_KEY must be set in the environment"
            )
        return cls(private_key=key, network=os.getenv("NETWORK", DEFAULT_NETWORK))

    @property
    def wallet_address(self) -> str | None:
        return self._wallet_address

    @property
    def network(self) -> str:
        return self._network

    def _load_sdk(self) -> None:
        if self._client is not None:
            return

        try:
            from eth_account import Account
            from x402 import x402Client
            from x402.http import x402HTTPClient
            from x402.mechanisms.evm import EthAccountSigner
            from x402.mechanisms.evm.exact.register import register_exact_evm_client
        except ImportError as error:
            raise X402ClientError(
                "the x402 SDK is not installed; run pip install 'x402[httpx,evm]'"
            ) from error

        account = Account.from_key(self._private_key)
        self._wallet_address = account.address

        client = x402Client()
        register_exact_evm_client(client, EthAccountSigner(account))
        self._client = client
        self._http_client = x402HTTPClient(client)

        log.info(
            "x402 client ready: wallet %s on %s", self._wallet_address, self._network
        )

    def call(self, method: str, url: str, **kwargs: Any) -> ApiCallResult:
        self._load_sdk()
        try:
            response = self._http_client.request(method, url, **kwargs)
        except Exception as error:
            log.warning("x402 call to %s failed: %s", url, error)
            return ApiCallResult(status_code=0, data=None, error=str(error))

        transaction_hash = str(response.headers.get("PAYMENT-TX", "")) or None
        raw_cost = response.headers.get("PAYMENT-AMOUNT")
        cost_usdc = Decimal(raw_cost) if raw_cost else None

        try:
            data = response.json()
        except Exception:
            data = response.text

        return ApiCallResult(
            status_code=response.status_code,
            data=data,
            transaction_hash=transaction_hash,
            cost_usdc=cost_usdc,
            headers=dict(response.headers),
        )

    def get(self, url: str, **kwargs: Any) -> ApiCallResult:
        return self.call("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> ApiCallResult:
        return self.call("POST", url, **kwargs)
