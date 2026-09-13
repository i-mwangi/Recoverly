from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final

from src.config import PROJECT_ROOT

log = logging.getLogger("recoverly.payments.wallet_ledger")

LEDGER_PATH: Final = PROJECT_ROOT / "data" / "wallet_case_ledger.json"


def load_ledger(path: Path | None = None) -> dict[str, str]:
    path = path or LEDGER_PATH
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        log.warning("the wallet ledger at %s is unreadable", path)
        return {}

    if not isinstance(entries, dict):
        return {}
    return {str(address).strip(): str(case_id) for address, case_id in entries.items()}


def case_for_address(address: str, path: Path | None = None) -> str | None:
    return load_ledger(path or LEDGER_PATH).get((address or "").strip())
