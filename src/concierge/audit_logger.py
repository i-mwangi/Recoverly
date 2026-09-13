from __future__ import annotations

import datetime as dt
import json
import os
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from src.config import settings
from src.utils.filelock import exclusive_lock

DEFAULT_ROTATE_BYTES: Final = 100 * 1024 * 1024
TAIL_DEFAULT: Final = 20

EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "request",
        "response",
        "parked",
        "resumed",
        "auto_cancelled",
        "partial_payment_received",
        "confirmation_outreach_sent",
        "new_commitment_received",
        "cadence_reset",
        "extension_denied",
        "payment_received_manual_confirmed",
        "payment_received_auto_webhook",
        "payment_command_rejected",
        "pre_flight_path_selected",
        "lite_mode_skip_voice",
        "lite_mode_skip_demand_letter",
        "attorney_referral_provided",
        "attorney_tag_attached_to_demand_letter",
        "written_off_with_template",
        "outreach_queued",
        "outreach_executed",
        "outreach_revoked_after_approval",
        "critical_alert_fired",
        "tier2_dossier_generated",
        "linkedin_signal_verified",
    }
)

REQUIRED_FIELDS: Final[dict[str, frozenset[str]]] = {
    "request": frozenset({"request_id", "case_id", "requesting_agent", "action_type"}),
    "response": frozenset({"request_id", "selected"}),
    "parked": frozenset({"request_id"}),
    "resumed": frozenset({"request_id"}),
    "auto_cancelled": frozenset({"request_id", "reason"}),
    "partial_payment_received": frozenset({"case_id", "invoice_id", "amount_received"}),
    "confirmation_outreach_sent": frozenset({"case_id"}),
    "new_commitment_received": frozenset({"case_id", "new_promised_date"}),
    "cadence_reset": frozenset({"case_id", "new_due_date", "source"}),
    "extension_denied": frozenset({"case_id"}),
    "payment_received_manual_confirmed": frozenset({"case_id", "amount", "channel"}),
    "payment_received_auto_webhook": frozenset({"case_id", "amount", "channel"}),
    "payment_command_rejected": frozenset({"reason"}),
    "pre_flight_path_selected": frozenset({"case_id", "mode"}),
    "lite_mode_skip_voice": frozenset({"case_id"}),
    "lite_mode_skip_demand_letter": frozenset({"case_id"}),
    "attorney_referral_provided": frozenset({"case_id"}),
    "attorney_tag_attached_to_demand_letter": frozenset({"case_id"}),
    "written_off_with_template": frozenset({"case_id"}),
    "outreach_queued": frozenset(
        {"case_id", "channel", "scheduled_execution_customer_local", "scheduled_execution_local"}
    ),
    "outreach_executed": frozenset({"case_id", "channel"}),
    "outreach_revoked_after_approval": frozenset({"case_id", "reason"}),
    "critical_alert_fired": frozenset({"case_id", "trigger"}),
    "tier2_dossier_generated": frozenset({"case_id"}),
    "linkedin_signal_verified": frozenset({"case_id", "officer_name"}),
}


class UnknownEventTypeError(ValueError):
    pass


class MissingRequiredFieldError(ValueError):
    pass


def rotate_threshold_bytes() -> int:
    try:
        return int(os.getenv("AUDIT_TRAIL_ROTATE_BYTES", str(DEFAULT_ROTATE_BYTES)))
    except ValueError:
        return DEFAULT_ROTATE_BYTES


def _iter_records(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


@dataclass(slots=True)
class AuditLogger:
    path: Path = field(default_factory=lambda: settings.data.audit_trail_jsonl)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    def validate(self, event_type: str, fields: dict[str, Any]) -> None:
        if event_type not in EVENT_TYPES:
            raise UnknownEventTypeError(
                f"unknown event type {event_type!r}; known types are {sorted(EVENT_TYPES)}"
            )

        missing = REQUIRED_FIELDS.get(event_type, frozenset()) - fields.keys()
        if missing:
            raise MissingRequiredFieldError(
                f"event {event_type!r} is missing required fields: {sorted(missing)}"
            )

    def write(self, event_type: str, **fields: Any) -> dict[str, Any]:
        self.validate(event_type, fields)

        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "type": event_type,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"

        with self._lock, exclusive_lock(self.lock_path):
            self._rotate_if_needed()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

        return record

    def _rotate_if_needed(self) -> bool:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return False

        if size < rotate_threshold_bytes():
            return False

        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path.rename(self.path.with_name(f"{self.path.stem}.{stamp}.jsonl"))
        return True

    def tail(self, count: int = TAIL_DEFAULT) -> list[dict[str, Any]]:
        return list(_iter_records(self.path))[-count:]

    def count_by_type(self, since: dt.datetime | None = None) -> dict[str, int]:
        counts: Counter[str] = Counter()

        for record in _iter_records(self.path):
            if since is not None:
                try:
                    stamp = dt.datetime.fromisoformat(str(record.get("ts", "")))
                except ValueError:
                    continue
                if stamp < since:
                    continue
            counts[str(record.get("type", "unknown"))] += 1

        return dict(counts)

    def for_case(self, case_id: str) -> list[dict[str, Any]]:
        return [record for record in _iter_records(self.path) if record.get("case_id") == case_id]


audit_log = AuditLogger()
