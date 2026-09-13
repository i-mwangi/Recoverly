from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Final

from src.config import PROJECT_ROOT
from src.utils.filelock import exclusive_lock, write_atomic

log = logging.getLogger("recoverly.payments.dedup")

DEFAULT_PATH: Final = PROJECT_ROOT / "data" / "processed_transactions.json"
RING_CAPACITY: Final = 500
SYNTHETIC_PREFIX: Final = "synthetic_"
SYNTHETIC_DIGEST_CHARS: Final = 24


def _load(path: Path) -> list[str]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError):
        log.warning("the processed-transaction ring at %s is unreadable, resetting it", path)
        return []
    return [str(entry) for entry in entries] if isinstance(entries, list) else []


def synthetic_id(*parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return f"{SYNTHETIC_PREFIX}{digest[:SYNTHETIC_DIGEST_CHARS]}"


def already_processed(transaction_id: str, path: Path | None = None) -> bool:
    if not transaction_id:
        return False
    return transaction_id in _load(path or DEFAULT_PATH)


def record_processed(transaction_id: str, path: Path | None = None) -> None:
    if not transaction_id:
        return
    target = path or DEFAULT_PATH

    with exclusive_lock(target.with_suffix(".json.lock")):
        entries = _load(target)
        if transaction_id in entries:
            return
        entries.append(transaction_id)
        write_atomic(target, json.dumps(entries[-RING_CAPACITY:]))
