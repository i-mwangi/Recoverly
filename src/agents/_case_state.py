from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final

from src.agents._utils import safe_float
from src.config import settings
from src.utils.filelock import exclusive_lock, write_atomic

log = logging.getLogger("recoverly.agents.case_state")

CASE_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,127}$")

TERMINAL_STATUSES: Final = frozenset({"closed_recovered", "closed_external", "halted"})

STAGE_TRANSITIONS: Final[dict[str, str]] = {
    "preflight_done": "investigator",
    "investigator_done": "diplomat",
    "diplomat_done": "tone_coach",
    "tone_coach_done": "concierge",
    "cure_window_expired": "aaa_filing_recommended",
}


class InvalidCaseIdError(ValueError):
    pass


def cases_dir() -> Path:
    return settings.data.case_state_dir


def audit_trail_path() -> Path:
    return settings.data.audit_trail_jsonl


def validate_case_id(case_id: str) -> str:
    if not isinstance(case_id, str) or not CASE_ID_PATTERN.match(case_id):
        raise InvalidCaseIdError(f"case_id rejected by path-traversal guard: {case_id!r}")
    if case_id.endswith("."):
        raise InvalidCaseIdError(f"case_id rejected (trailing dot): {case_id!r}")
    return case_id


def case_path(case_id: str) -> Path:
    return cases_dir() / f"{validate_case_id(case_id)}.json"


def case_lock_path(case_id: str) -> Path:
    return cases_dir() / f"{validate_case_id(case_id)}.json.lock"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state(case_id: str) -> dict[str, Any]:
    timestamp = _now()
    return {
        "case_id": case_id,
        "status": "initiated",
        "current_stage": "preflight",
        "cadence_day": 0,
        "amount_usd": 0.0,
        "amount_paid_usd": 0.0,
        "amount_balance_usd": 0.0,
        "cost_usd": 0.0,
        "cost_cap_usd": settings.per_case_cost_cap_usd,
        "promise_date": None,
        "extension_until": None,
        "hitl_card_posted_at": None,
        "hitl_sla_seconds": settings.hitl_sla_seconds,
        "events_seen_count": 0,
        "last_event_ts": None,
        "halted_reason": None,
        "anomaly_kind": None,
        "consolidated_invoices": [],
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _read_state(path: Path, case_id: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_state(case_id)
    except OSError as error:
        log.warning("case %s unreadable, using default state: %s", case_id, error)
        return default_state(case_id)

    if not raw.strip():
        return default_state(case_id)

    try:
        state = json.loads(raw)
    except json.JSONDecodeError as error:
        log.warning("case %s is corrupt JSON, using default state: %s", case_id, error)
        return default_state(case_id)

    return state if isinstance(state, dict) else default_state(case_id)


def load_case_state(case_id: str) -> dict[str, Any]:
    return _read_state(case_path(case_id), case_id)


def mutate_case_state(case_id: str, mutator: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    path = case_path(case_id)

    with exclusive_lock(case_lock_path(case_id)):
        state = _read_state(path, case_id)
        state.update(mutator(state))
        state["updated_at"] = _now()
        state["events_seen_count"] = int(state.get("events_seen_count", 0)) + 1
        state["last_event_ts"] = state["updated_at"]
        write_atomic(path, json.dumps(state, indent=2, ensure_ascii=False))

    return state


def update_case_state(case_id: str, **patch: Any) -> dict[str, Any]:
    return mutate_case_state(case_id, lambda _state: patch)


def add_cost(case_id: str, usage: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return load_case_state(case_id)

    delta = safe_float(usage.get("cost_usd"), 0.0)
    if delta <= 0:
        return load_case_state(case_id)

    state = load_case_state(case_id)
    new_cost = round(safe_float(state.get("cost_usd"), 0.0) + delta, 8)
    cap = safe_float(state.get("cost_cap_usd"), settings.per_case_cost_cap_usd)

    patch: dict[str, Any] = {"cost_usd": new_cost}
    if new_cost > cap and not state.get("halted_reason"):
        patch["status"] = "halted"
        patch["halted_reason"] = f"cost_cap_exceeded_usd_{cap}"
        log.warning("case %s halted: cost %.6f exceeds cap %.6f", case_id, new_cost, cap)

    return update_case_state(case_id, **patch)


def set_anomaly_halt(case_id: str, kind: str) -> dict[str, Any]:
    return update_case_state(
        case_id, status="halted", anomaly_kind=kind, halted_reason=f"anomaly_{kind}"
    )


def stop_case(case_id: str, reason: str) -> dict[str, Any]:
    return update_case_state(case_id, status="halted", halted_reason=reason)


def is_halted(case_id: str) -> bool:
    state = load_case_state(case_id)
    return state.get("status") == "halted" or bool(state.get("halted_reason"))


def set_hitl_posted(case_id: str, sla_seconds: int | None = None) -> dict[str, Any]:
    return update_case_state(
        case_id,
        hitl_card_posted_at=_now(),
        hitl_sla_seconds=sla_seconds or settings.hitl_sla_seconds,
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def list_breached_hitl_cases() -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    breached: list[dict[str, Any]] = []

    for path in cases_dir().glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            log.warning("skipping unreadable case file %s: %s", path.name, error)
            continue

        if not isinstance(state, dict) or state.get("status") in TERMINAL_STATUSES:
            continue

        posted_at = _parse_timestamp(state.get("hitl_card_posted_at"))
        if posted_at is None:
            continue

        sla = int(state.get("hitl_sla_seconds") or settings.hitl_sla_seconds)
        if (now - posted_at).total_seconds() > sla:
            breached.append(state)

    return breached


def apply_event(state: dict[str, Any], event: dict[str, Any]) -> None:
    name = event.get("event", "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}

    state["last_event_ts"] = event.get("ts", state.get("last_event_ts"))
    state["events_seen_count"] = int(state.get("events_seen_count", 0)) + 1

    if name in STAGE_TRANSITIONS:
        state["current_stage"] = STAGE_TRANSITIONS[name]
    elif name == "slack_post":
        state["current_stage"] = "hitl_pending"
        state["hitl_card_posted_at"] = event.get("ts")
    elif name == "human_approved":
        state["status"] = "approved"
        state["current_stage"] = "email_send"
    elif name == "human_rejected":
        state["status"] = "rejected"
        state["halted_reason"] = "human_rejected"
    elif name == "email_sent":
        state["current_stage"] = "awaiting_payment"
    elif name == "payment_reconciled":
        state["amount_paid_usd"] = safe_float(payload.get("paid_to_date_usd"), 0.0)
        state["outstanding_balance_usd"] = safe_float(payload.get("outstanding_usd"), 0.0)
        state["amount_balance_usd"] = safe_float(payload.get("remaining_usd"), 0.0)
        transaction_id = payload.get("transaction_id")
        if transaction_id and payload.get("amount_applied_usd"):
            seen = state.setdefault("payment_transaction_ids", [])
            if transaction_id not in seen:
                seen.append(transaction_id)
        if payload.get("halted"):
            state["status"] = "halted"
        elif payload.get("status") in {"settled", "overpaid"}:
            state["status"] = "closed_recovered"
            state["current_stage"] = "settled"
        elif payload.get("status") == "partial":
            state["status"] = "active"
            state["current_stage"] = "awaiting_payment"
    elif name == "payment_received":
        state["status"] = "closed_recovered"
        state["amount_paid_usd"] = safe_float(
            payload.get("amount_usd", state.get("amount_paid_usd")), 0.0
        )
    elif name == "case_closed_external":
        state["status"] = "closed_external"
        state["halted_reason"] = payload.get("reason", "external_stop")
    elif "anomaly" in name:
        kind = payload.get("kind") or name.removeprefix("anomaly_detected_") or "unknown"
        state["status"] = "halted"
        state["anomaly_kind"] = kind
        state["halted_reason"] = f"anomaly_{kind}"

    usage = payload.get("usage")
    if isinstance(usage, dict):
        state["cost_usd"] = round(
            safe_float(state.get("cost_usd"), 0.0) + safe_float(usage.get("cost_usd"), 0.0), 8
        )


def _iter_audit_events(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                log.debug("skipping malformed audit line %d: %s", line_number, error)
                continue
            if isinstance(event, dict):
                yield event


def rebuild_all_from_audit(audit_path: Path | None = None) -> dict[str, Any]:
    path = audit_path or audit_trail_path()
    if not path.exists():
        return {"replayed_events": 0, "cases_written": 0, "skipped_case_ids": []}

    states: dict[str, dict[str, Any]] = {}
    skipped: set[str] = set()
    replayed = 0

    for event in _iter_audit_events(path):
        replayed += 1
        case_id = event.get("case_id") or ""
        if not case_id or case_id == "?":
            continue
        try:
            validate_case_id(case_id)
        except InvalidCaseIdError:
            skipped.add(case_id)
            continue
        apply_event(states.setdefault(case_id, default_state(case_id)), event)

    written = 0
    for case_id, state in states.items():
        with exclusive_lock(case_lock_path(case_id)):
            write_atomic(case_path(case_id), json.dumps(state, indent=2, ensure_ascii=False))
        written += 1

    if skipped:
        log.warning("replay skipped %d malformed case_ids", len(skipped))

    return {
        "replayed_events": replayed,
        "cases_written": written,
        "skipped_case_ids": sorted(skipped),
    }
