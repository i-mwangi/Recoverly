from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Final

from src.agents._case_state import InvalidCaseIdError, load_case_state, validate_case_id
from src.agents.base import audit
from src.utils.sanitize import strip_slack_mentions

log = logging.getLogger("recoverly.concierge.actions")

AGENT: Final = "concierge"
UNKNOWN_CASE: Final = "UNKNOWN"
NOTE_LIMIT: Final = 500

CASE_IN_VALUE_RE: Final = re.compile(r"case=([^&]+)")


@dataclass(frozen=True, slots=True)
class ActionContext:
    action_id: str
    case_id: str
    user: str
    channel: str
    message_ts: str
    value: str
    raw: dict[str, Any] = field(default_factory=dict)

    def case(self) -> dict[str, Any]:
        try:
            return {**load_case_state(self.case_id), "case_id": self.case_id}
        except Exception:
            return {"case_id": self.case_id}

    def audit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        audit(
            self.case_id,
            AGENT,
            event,
            {"action_id": self.action_id, "operator": self.user, **(payload or {})},
        )


@dataclass(frozen=True, slots=True)
class ActionResult:
    text: str
    ephemeral: bool = True
    seal_card: bool = False

    def as_response(self) -> dict[str, Any]:
        return {
            "response_type": "ephemeral" if self.ephemeral else "in_channel",
            "text": self.text,
        }


Handler = Callable[[ActionContext], ActionResult]

_EXACT_HANDLERS: dict[str, Handler] = {}
_PREFIX_HANDLERS: list[tuple[str, Handler]] = []


def register(*action_ids: str) -> Callable[[Handler], Handler]:
    def decorator(handler: Handler) -> Handler:
        for action_id in action_ids:
            _EXACT_HANDLERS[action_id] = handler
        return handler

    return decorator


def register_prefix(*prefixes: str) -> Callable[[Handler], Handler]:
    def decorator(handler: Handler) -> Handler:
        for prefix in prefixes:
            _PREFIX_HANDLERS.append((prefix, handler))
        _PREFIX_HANDLERS.sort(key=lambda pair: len(pair[0]), reverse=True)
        return handler

    return decorator


def resolve(action_id: str) -> Handler | None:
    handler = _EXACT_HANDLERS.get(action_id)
    if handler is not None:
        return handler
    for prefix, prefix_handler in _PREFIX_HANDLERS:
        if action_id.startswith(prefix):
            return prefix_handler
    return None


def registered_actions() -> list[str]:
    return sorted(_EXACT_HANDLERS) + sorted(f"{prefix}*" for prefix, _ in _PREFIX_HANDLERS)


def case_id_from_value(value: str, action_id: str = "") -> str:
    candidate = value.strip()

    match = CASE_IN_VALUE_RE.search(candidate)
    if match:
        candidate = match.group(1)
    elif ":" in candidate:
        candidate = candidate.split(":", 1)[0]

    if not candidate and action_id:
        candidate = action_id.rsplit("_", 1)[-1]

    try:
        return validate_case_id(candidate)
    except InvalidCaseIdError:
        log.warning("action carried an unusable case reference: %r", value[:80])
        return UNKNOWN_CASE


def build_context(action: dict[str, Any], payload: dict[str, Any]) -> ActionContext:
    action_id = str(action.get("action_id", ""))
    value = str(action.get("value", ""))

    return ActionContext(
        action_id=action_id,
        case_id=case_id_from_value(value, action_id),
        user=str((payload.get("user") or {}).get("username", "unknown")),
        channel=str((payload.get("channel") or {}).get("id", "")),
        message_ts=str((payload.get("message") or {}).get("ts", "")),
        value=value,
        raw=payload,
    )


def clean_note(note: str | None) -> str:
    return strip_slack_mentions((note or "").strip())[:NOTE_LIMIT]


def dispatch(action: dict[str, Any], payload: dict[str, Any]) -> ActionResult:
    context = build_context(action, payload)
    handler = resolve(context.action_id)

    if handler is None:
        log.info("no handler registered for action %s", context.action_id)
        context.audit("action_unhandled")
        return ActionResult(
            f"That button (`{context.action_id}`) is not wired to anything yet."
        )

    try:
        return handler(context)
    except Exception as error:
        log.exception("action %s failed", context.action_id)
        context.audit("action_failed", {"error": str(error)})
        return ActionResult(
            f"Something went wrong handling that button: {error}"
        )
