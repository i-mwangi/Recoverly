from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("recoverly.voice.dialer")

TWILIO_CALLS_URL: Final = "https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
REQUEST_TIMEOUT_SECONDS: Final = 30.0
ACCEPTED_STATUS_CODES: Final = frozenset({200, 201})

DEFAULT_TIMEZONE: Final = "America/New_York"
CALLING_WEEKDAYS: Final = frozenset({1, 2, 3})
CALLING_HOURS: Final = frozenset({10, 14})

STATE_TIMEZONES: Final[dict[str, str]] = {
    "CA": "America/Los_Angeles",
    "OR": "America/Los_Angeles",
    "WA": "America/Los_Angeles",
    "NV": "America/Los_Angeles",
    "AZ": "America/Phoenix",
    "TX": "America/Chicago",
    "IL": "America/Chicago",
    "MN": "America/Chicago",
    "NY": "America/New_York",
    "FL": "America/New_York",
    "MA": "America/New_York",
    "GA": "America/New_York",
}


class DialerConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class WindowCheck:
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DialResult:
    ok: bool
    call_sid: str = ""
    conversation_id: str = ""
    error: str = ""
    detail: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "call_sid": self.call_sid,
            "conversation_id": self.conversation_id,
            "error": self.error,
            "detail": self.detail,
        }


def timezone_for_state(state: str | None) -> str:
    return STATE_TIMEZONES.get((state or "").strip().upper(), DEFAULT_TIMEZONE)


def check_calling_window(state: str | None, now: datetime | None = None) -> WindowCheck:
    zone = timezone_for_state(state)
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(zone))

    if local.weekday() not in CALLING_WEEKDAYS:
        return WindowCheck(False, f"outside the calling days ({local:%a}, {zone})")

    if local.hour not in CALLING_HOURS:
        return WindowCheck(False, f"outside the calling hours ({local.hour}:00, {zone})")

    try:
        import holidays
    except ImportError:
        return WindowCheck(True, f"inside the window ({local.hour}:00, {zone})")

    observed = holidays.US(years=local.year)
    if local.date() in observed:
        return WindowCheck(False, f"federal holiday: {observed.get(local.date())}")

    return WindowCheck(True, f"inside the window ({local.hour}:00, {zone})")


def callback_base_url() -> str:
    return os.getenv("TWILIO_CALLBACK_BASE_URL", "").strip().rstrip("/")


def dry_run_enabled() -> bool:
    requested = os.getenv("VOICE_DRY_RUN", "").strip() == "1"
    allowed = os.getenv("RECOVERLY_ALLOW_VOICE_DRY_RUN", "").strip() == "1"
    if requested and not allowed:
        raise RuntimeError(
            "VOICE_DRY_RUN=1 is set without RECOVERLY_ALLOW_VOICE_DRY_RUN=1. "
            "Unset VOICE_DRY_RUN to place real calls, or add the allow flag if the "
            "dry run is intentional."
        )
    return requested and allowed


def _missing_configuration(case: dict[str, Any]) -> str | None:
    if not (case.get("customer_phone") or case.get("phone")):
        return "the case has no customer phone number"
    for label in ("FISH_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "TWILIO_CALLBACK_BASE_URL"):
        if not os.getenv(label, "").strip():
            return f"{label} is not configured"
    return None


def build_dynamic_variables(case: dict[str, Any]) -> dict[str, str]:
    from src.voice.dynamic_vars import build_dynamic_vars

    variables = build_dynamic_vars(case)
    variables["case_id"] = str(case.get("case_id") or "UNKNOWN")
    return {key: str(value) for key, value in variables.items() if value != ""}


def place_call(case: dict[str, Any]) -> DialResult:
    missing = _missing_configuration(case)
    if missing:
        return DialResult(ok=False, error=missing)

    case_id = str(case.get("case_id") or "")
    if not case_id:
        return DialResult(ok=False, error="the case has no case_id")
    base_url = callback_base_url()
    query = urlencode({"case_id": case_id})
    account_sid = os.environ["TWILIO_ACCOUNT_SID"].strip()

    try:
        response = httpx.post(
            TWILIO_CALLS_URL.format(account_sid=account_sid),
            auth=(account_sid, os.environ["TWILIO_AUTH_TOKEN"].strip()),
            data={
                "To": str(case.get("customer_phone") or case.get("phone")),
                "From": os.environ["TWILIO_FROM_NUMBER"].strip(),
                "Url": f"{base_url}/voice-webhook/twiml?{query}",
                "StatusCallback": f"{base_url}/voice-webhook/call-status?{query}",
                "StatusCallbackEvent": "completed",
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        log.warning("Twilio call request failed: %s", error)
        return DialResult(ok=False, error=str(error))

    if response.status_code not in ACCEPTED_STATUS_CODES:
        return DialResult(
            ok=False,
            error=f"Twilio returned {response.status_code}",
            detail={"body": response.text[:400]},
        )

    body = response.json()
    return DialResult(
        ok=True,
        call_sid=str(body.get("sid") or ""),
    )
