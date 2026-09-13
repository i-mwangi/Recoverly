from __future__ import annotations

import datetime as dt
import logging
from typing import Final, Literal
from zoneinfo import ZoneInfo

from src.config import settings

log = logging.getLogger("recoverly.utils.timezone")

ChannelType = Literal["email", "voice", "demand_letter"]

DEFAULT_FALLBACK_TIMEZONE: Final = "America/New_York"
MAX_LOOKAHEAD_DAYS: Final = 14

STATE_TIMEZONE: Final[dict[str, str]] = {
    "CA": "America/Los_Angeles",
    "WA": "America/Los_Angeles",
    "OR": "America/Los_Angeles",
    "NV": "America/Los_Angeles",
    "AZ": "America/Phoenix",
    "CO": "America/Denver",
    "UT": "America/Denver",
    "NM": "America/Denver",
    "MT": "America/Denver",
    "WY": "America/Denver",
    "ID": "America/Denver",
    "TX": "America/Chicago",
    "IL": "America/Chicago",
    "MO": "America/Chicago",
    "MN": "America/Chicago",
    "WI": "America/Chicago",
    "IA": "America/Chicago",
    "KS": "America/Chicago",
    "OK": "America/Chicago",
    "AR": "America/Chicago",
    "LA": "America/Chicago",
    "MS": "America/Chicago",
    "AL": "America/Chicago",
    "TN": "America/Chicago",
    "KY": "America/Chicago",
    "ND": "America/Chicago",
    "SD": "America/Chicago",
    "NE": "America/Chicago",
    "NY": "America/New_York",
    "NJ": "America/New_York",
    "PA": "America/New_York",
    "MA": "America/New_York",
    "CT": "America/New_York",
    "RI": "America/New_York",
    "VT": "America/New_York",
    "NH": "America/New_York",
    "ME": "America/New_York",
    "DE": "America/New_York",
    "MD": "America/New_York",
    "VA": "America/New_York",
    "WV": "America/New_York",
    "NC": "America/New_York",
    "SC": "America/New_York",
    "GA": "America/New_York",
    "FL": "America/New_York",
    "OH": "America/New_York",
    "MI": "America/New_York",
    "IN": "America/New_York",
    "AK": "America/Anchorage",
    "HI": "Pacific/Honolulu",
    "PR": "America/Puerto_Rico",
    "VI": "America/St_Thomas",
}

TIMEZONE_ABBREVIATIONS: Final[dict[str, str]] = {
    "America/Los_Angeles": "PT",
    "America/New_York": "ET",
    "America/Chicago": "CT",
    "America/Denver": "MT",
    "America/Phoenix": "MST",
    "America/Anchorage": "AKT",
    "Pacific/Honolulu": "HST",
    "Africa/Nairobi": "EAT",
    "Africa/Dar_es_Salaam": "EAT",
    "Africa/Kampala": "EAT",
    "Africa/Addis_Ababa": "EAT",
}

EMAIL_WINDOW: Final[tuple[int, int]] = (9, 16)
VOICE_WINDOWS: Final[tuple[tuple[int, int], ...]] = ((10, 11), (14, 15))
DEMAND_LETTER_WINDOW: Final[tuple[int, int]] = (9, 12)
CALLING_HARD_FLOOR: Final[tuple[int, int]] = (8, 19)

EMAIL_WEEKDAYS: Final = frozenset({0, 1, 2, 3, 4})
VOICE_WEEKDAYS: Final = frozenset({1, 2, 3})
DEMAND_LETTER_WEEKDAYS: Final = frozenset({0, 1, 2, 3})

CHANNEL_WEEKDAYS: Final[dict[str, frozenset[int]]] = {
    "email": EMAIL_WEEKDAYS,
    "voice": VOICE_WEEKDAYS,
    "demand_letter": DEMAND_LETTER_WEEKDAYS,
}

PRE_APPROVAL_LEAD_HOURS: Final[dict[str, int]] = {
    "email": settings.lead_time_email_hours,
    "voice": settings.lead_time_voice_hours,
    "demand_letter": settings.lead_time_demand_letter_hours,
}


class NoAllowedWindowError(RuntimeError):
    pass


def operator_timezone() -> ZoneInfo:
    return ZoneInfo(settings.operator_timezone)


def customer_state_to_timezone(state: str | None) -> str:
    code = (state or "").strip().upper()
    if code in STATE_TIMEZONE:
        return STATE_TIMEZONE[code]

    log.warning(
        "unknown customer state %r, falling back to %s", state, DEFAULT_FALLBACK_TIMEZONE
    )
    return DEFAULT_FALLBACK_TIMEZONE


def is_us_holiday(day: dt.date) -> bool:
    try:
        import holidays
    except ImportError:
        log.debug("the holidays package is not installed, so no dates are treated as holidays")
        return False

    return day in holidays.US(years=day.year)


def is_business_day(day: dt.date, channel: ChannelType) -> bool:
    if is_us_holiday(day):
        return False
    return day.weekday() in CHANNEL_WEEKDAYS.get(channel, EMAIL_WEEKDAYS)


def is_within_business_hours(customer_local: dt.datetime, channel: ChannelType) -> bool:
    hour = customer_local.hour

    if channel == "email":
        return EMAIL_WINDOW[0] <= hour < EMAIL_WINDOW[1]

    if channel == "demand_letter":
        return DEMAND_LETTER_WINDOW[0] <= hour < DEMAND_LETTER_WINDOW[1]

    if channel == "voice":
        if not CALLING_HARD_FLOOR[0] <= hour < CALLING_HARD_FLOOR[1]:
            return False
        return any(start <= hour < end for start, end in VOICE_WINDOWS)

    return False


def _target_hour(channel: ChannelType, preferred_local_hour: int | None) -> int:
    if channel == "email":
        if preferred_local_hour is not None and EMAIL_WINDOW[0] <= preferred_local_hour < EMAIL_WINDOW[1]:
            return preferred_local_hour
        return EMAIL_WINDOW[0]

    if channel == "demand_letter":
        return DEMAND_LETTER_WINDOW[0]

    if channel == "voice":
        if preferred_local_hour is not None:
            for start, end in VOICE_WINDOWS:
                if start <= preferred_local_hour < end:
                    return preferred_local_hour
        return VOICE_WINDOWS[0][0]

    raise ValueError(f"unknown channel: {channel!r}")


def next_allowed_window(
    *,
    channel: ChannelType,
    customer_timezone: str,
    not_before: dt.datetime | None = None,
    preferred_local_hour: int | None = None,
) -> dt.datetime:
    zone = ZoneInfo(customer_timezone)
    start = (not_before.astimezone(zone) if not_before else dt.datetime.now(zone))

    candidate = start.replace(
        hour=_target_hour(channel, preferred_local_hour), minute=0, second=0, microsecond=0
    )
    if candidate <= start:
        candidate += dt.timedelta(days=1)

    for _ in range(MAX_LOOKAHEAD_DAYS):
        if is_business_day(candidate.date(), channel) and is_within_business_hours(
            candidate, channel
        ):
            return candidate
        candidate += dt.timedelta(days=1)

    raise NoAllowedWindowError(
        f"no allowed slot for channel {channel!r} in {customer_timezone} within "
        f"{MAX_LOOKAHEAD_DAYS} days"
    )


def compute_pre_approval_deadline(
    *, customer_local_send_at: dt.datetime, channel: ChannelType
) -> dt.datetime:
    lead_hours = PRE_APPROVAL_LEAD_HOURS[channel]
    return (customer_local_send_at - dt.timedelta(hours=lead_hours)).astimezone(
        operator_timezone()
    )


def timezone_abbreviation(zone_name: str) -> str:
    return TIMEZONE_ABBREVIATIONS.get(zone_name, zone_name.split("/")[-1].replace("_", " "))


def format_dual_timezone(
    *, customer_local: dt.datetime, customer_name: str, customer_state: str
) -> dict[str, str]:
    zone_name = str(customer_local.tzinfo)
    abbreviation = timezone_abbreviation(zone_name)
    operator_local = customer_local.astimezone(operator_timezone())
    operator_abbreviation = timezone_abbreviation(settings.operator_timezone)

    return {
        "customer_local_str": (
            f"{abbreviation} {customer_local:%H:%M %a %Y-%m-%d} "
            f"({customer_name}, {customer_state})"
        ),
        "operator_local_str": f"{operator_abbreviation} {operator_local:%H:%M %a %Y-%m-%d}",
        "customer_tz_short": abbreviation,
        "iso_customer_local": customer_local.isoformat(),
        "iso_operator_local": operator_local.isoformat(),
    }
