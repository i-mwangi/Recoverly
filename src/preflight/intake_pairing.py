from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from src.config import PROJECT_ROOT
from src.utils.filelock import exclusive_lock, write_atomic

log = logging.getLogger("recoverly.preflight.intake_pairing")

DEFAULT_PENDING_DIR: Final = PROJECT_ROOT / "data" / "intake_pending"
DEFAULT_WINDOW_HOURS: Final = 24
SECONDS_PER_HOUR: Final = 3_600

ACTION_CASE_CREATE: Final = "case_create"
ACTION_MERGE_AND_CREATE: Final = "merge_and_create"
ACTION_PEND_WAITING_CONTRACT: Final = "pend_waiting_contract"
ACTION_PEND_WAITING_INVOICE: Final = "pend_waiting_invoice"
ACTION_DUPLICATE_IGNORED: Final = "duplicate_ignored"
ACTION_UNRECOGNIZED: Final = "unrecognized"

INVOICE_SLOT: Final = "invoice"
CONTRACT_SLOT: Final = "contract"
SLOTS: Final = (INVOICE_SLOT, CONTRACT_SLOT)


@dataclass(frozen=True, slots=True)
class PairingDecision:
    action: str
    case_id: str = ""
    invoice_meta: dict[str, Any] = field(default_factory=dict)
    contract_meta: dict[str, Any] = field(default_factory=dict)
    note: str = ""

    @property
    def creates_case(self) -> bool:
        return self.action in {ACTION_CASE_CREATE, ACTION_MERGE_AND_CREATE}

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "case_id": self.case_id,
            "invoice_meta": self.invoice_meta,
            "contract_meta": self.contract_meta,
            "note": self.note,
        }


def _now(now_iso: str | None) -> datetime:
    if not now_iso:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def mint_case_id(now: datetime) -> str:
    return f"RC-{now.year}-9{int(now.timestamp()) % 100_000:05d}"


def _sidecar_path(pending_dir: Path | str, persona: str) -> Path:
    safe = "".join(char for char in persona if char.isalnum() or char in "-_")
    if not safe:
        raise ValueError(f"persona has no filesystem-safe characters: {persona!r}")
    return Path(pending_dir) / f"{safe}.json"


def _read_sidecar(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("sidecar %s is malformed, starting fresh", path)
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _purge_expired(sidecar: dict[str, Any], now: datetime, window_hours: int) -> dict[str, Any]:
    cutoff = now.timestamp() - window_hours * SECONDS_PER_HOUR
    purged = dict(sidecar)

    for slot in SLOTS:
        entry = purged.get(slot)
        if not isinstance(entry, dict):
            continue
        received_at = entry.get("received_at")
        if not received_at:
            continue
        try:
            timestamp = _now(str(received_at)).timestamp()
        except ValueError:
            continue
        if timestamp < cutoff:
            log.info("purging expired %s from sidecar (received %s)", slot, received_at)
            purged[slot] = None

    return purged


def _stamp(meta: dict[str, Any], now: datetime) -> dict[str, Any]:
    if not meta:
        return {}
    stamped = dict(meta)
    stamped.setdefault("received_at", now.isoformat())
    return stamped


def _handle_invoice(
    persona: str, incoming: dict[str, Any], sidecar: dict[str, Any], now: datetime
) -> PairingDecision:
    waiting_contract = sidecar.get(CONTRACT_SLOT)
    waiting_invoice = sidecar.get(INVOICE_SLOT)
    invoice_no = incoming.get("invoice_no", "unknown")

    if isinstance(waiting_contract, dict):
        case_id = mint_case_id(now)
        return PairingDecision(
            action=ACTION_MERGE_AND_CREATE,
            case_id=case_id,
            invoice_meta=incoming,
            contract_meta=waiting_contract,
            note=f"Invoice {invoice_no} paired with the waiting contract for {persona}; opened case {case_id}.",
        )

    if isinstance(waiting_invoice, dict):
        existing_no = waiting_invoice.get("invoice_no", "")
        if existing_no and existing_no == invoice_no:
            return PairingDecision(
                action=ACTION_DUPLICATE_IGNORED,
                invoice_meta=incoming,
                note=f"Duplicate invoice {invoice_no} for {persona} ignored; the first upload stays pending.",
            )
        return PairingDecision(
            action=ACTION_PEND_WAITING_CONTRACT,
            invoice_meta=incoming,
            note=f"Invoice {invoice_no} replaces pending invoice {existing_no} for {persona}; waiting for a contract.",
        )

    return PairingDecision(
        action=ACTION_PEND_WAITING_CONTRACT,
        invoice_meta=incoming,
        note=f"Invoice {invoice_no} received for {persona}; waiting for the contract upload.",
    )


def _handle_contract(
    persona: str, incoming: dict[str, Any], sidecar: dict[str, Any], now: datetime
) -> PairingDecision:
    waiting_invoice = sidecar.get(INVOICE_SLOT)
    waiting_contract = sidecar.get(CONTRACT_SLOT)

    if isinstance(waiting_invoice, dict):
        case_id = mint_case_id(now)
        invoice_no = waiting_invoice.get("invoice_no", "unknown")
        return PairingDecision(
            action=ACTION_MERGE_AND_CREATE,
            case_id=case_id,
            invoice_meta=waiting_invoice,
            contract_meta=incoming,
            note=f"Contract paired with waiting invoice {invoice_no} for {persona}; opened case {case_id}.",
        )

    if isinstance(waiting_contract, dict):
        return PairingDecision(
            action=ACTION_DUPLICATE_IGNORED,
            contract_meta=incoming,
            note=f"Duplicate contract for {persona} ignored; the first upload stays pending.",
        )

    return PairingDecision(
        action=ACTION_PEND_WAITING_INVOICE,
        contract_meta=incoming,
        note=f"Contract received for {persona}; waiting for the invoice upload.",
    )


def _next_sidecar(
    decision: PairingDecision, persona: str, previous: dict[str, Any], now: datetime
) -> dict[str, Any]:
    if decision.action == ACTION_MERGE_AND_CREATE:
        return {"persona": persona, INVOICE_SLOT: None, CONTRACT_SLOT: None}

    if decision.action == ACTION_PEND_WAITING_CONTRACT:
        return {
            "persona": persona,
            INVOICE_SLOT: _stamp(decision.invoice_meta, now),
            CONTRACT_SLOT: previous.get(CONTRACT_SLOT),
        }

    if decision.action == ACTION_PEND_WAITING_INVOICE:
        return {
            "persona": persona,
            INVOICE_SLOT: previous.get(INVOICE_SLOT),
            CONTRACT_SLOT: _stamp(decision.contract_meta, now) or previous.get(CONTRACT_SLOT),
        }

    return previous


def pair_or_pend(
    persona: str,
    incoming_kind: str,
    incoming_meta: dict[str, Any],
    *,
    pending_dir: Path | str = DEFAULT_PENDING_DIR,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now_iso: str | None = None,
) -> PairingDecision:
    if incoming_kind not in {INVOICE_SLOT, CONTRACT_SLOT}:
        return PairingDecision(
            action=ACTION_UNRECOGNIZED,
            note="The uploaded file was not recognised as an invoice or a contract.",
        )

    now = _now(now_iso)
    path = _sidecar_path(pending_dir, persona)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = _handle_invoice if incoming_kind == INVOICE_SLOT else _handle_contract

    with exclusive_lock(path.with_suffix(".json.lock")):
        sidecar = _purge_expired(_read_sidecar(path), now, window_hours)
        decision = handler(persona, incoming_meta, sidecar, now)
        if decision.action != ACTION_DUPLICATE_IGNORED:
            updated = _next_sidecar(decision, persona, sidecar, now)
            write_atomic(path, json.dumps(updated, indent=2, ensure_ascii=False))

    log.info("pairing decision for %s: %s", persona, decision.action)
    return decision
