from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from src.config import PROJECT_ROOT

log = logging.getLogger("recoverly.concierge.contracts")

CONTRACTS_DIR: Final = PROJECT_ROOT / "data" / "contracts"
ID_PATTERN: Final = re.compile(r"^[A-Z][A-Z0-9-]{2,40}$")


class InvalidIdentifierError(ValueError):
    pass


def validate_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value.upper()):
        raise InvalidIdentifierError(
            f"invalid {label} {value!r}: must match {ID_PATTERN.pattern}"
        )
    return value


def contracts_dir() -> Path:
    return CONTRACTS_DIR


def load_contract(contract_id: str) -> dict[str, Any]:
    validate_identifier(contract_id, "contract_id")
    path = contracts_dir() / f"{contract_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"contract {contract_id!r} not found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def all_contracts() -> list[dict[str, Any]]:
    directory = contracts_dir()
    if not directory.exists():
        log.debug("contracts directory does not exist yet: %s", directory)
        return []

    contracts: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            log.error("could not read contract file %s: %s", path, error)
            continue
        if isinstance(document, dict):
            contracts.append(document)

    return contracts


def find_contract_by_buyer(buyer_id: str) -> dict[str, Any] | None:
    validate_identifier(buyer_id, "buyer_id")
    for contract in all_contracts():
        if contract.get("buyer_id") == buyer_id:
            return contract
    log.debug("no contract found for buyer %s", buyer_id)
    return None


def find_contract_by_invoice(invoice_id: str) -> dict[str, Any] | None:
    validate_identifier(invoice_id, "invoice_id")
    for contract in all_contracts():
        known = list(contract.get("active_invoices") or []) + list(
            contract.get("historical_invoices") or []
        )
        if invoice_id in known:
            return contract
    log.debug("no contract found for invoice %s", invoice_id)
    return None


@lru_cache(maxsize=1)
def _buyer_index() -> dict[str, str]:
    return {
        str(contract["buyer_id"]): str(contract.get("contract_id", ""))
        for contract in all_contracts()
        if contract.get("buyer_id")
    }


def known_buyer_ids() -> list[str]:
    return sorted(_buyer_index())


def clear_cache() -> None:
    _buyer_index.cache_clear()
