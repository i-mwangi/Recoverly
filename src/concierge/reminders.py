from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Final
from zoneinfo import ZoneInfo

from src.concierge import slack_client
from src.config import settings
from src.utils.text import truncate

log = logging.getLogger("recoverly.concierge.reminders")

EXCERPT_LIMIT: Final = 240

REDIAL_ACTION: Final = "voice_redial_now"
PAYMENT_RECEIVED_ACTION: Final = "voice_payment_received_confirm"


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    ok: bool
    scheduled_message_id: str = ""
    post_at: int = 0
    channel: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "scheduled_message_id": self.scheduled_message_id,
            "post_at": self.post_at,
            "channel": self.channel,
            "reason": self.reason,
        }


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def post_at_epoch(target: date) -> int:
    zone = ZoneInfo(settings.operator_timezone)
    local = datetime.combine(
        target,
        time(settings.daily_digest_hour, settings.daily_digest_minute),
        tzinfo=zone,
    )
    return int(local.timestamp())


def build_commitment_blocks(
    case_id: str, commitment_date: str, buyer_name: str, call_excerpt: str
) -> list[dict[str, Any]]:
    excerpt = truncate((call_excerpt or "").strip(), EXCERPT_LIMIT)
    body = (
        f"*{buyer_name} promised to pay by {commitment_date}.*\n\n"
        f"Confirm the payment landed, or trigger a re-dial if it has not.\n\n"
        f"• *Case*: `{case_id}`\n"
        f"• *Commitment date*: {commitment_date}\n"
        f"• *What the buyer said*: _\"{excerpt}\"_"
    )

    return [
        slack_client.header_block(f"Voice commitment reminder — case {case_id}"),
        slack_client.section_block(body),
        slack_client.context_block(
            f"Extracted from the post-call transcript for case `{case_id}`."
        ),
        slack_client.actions_block(
            slack_client.button("Re-dial now", REDIAL_ACTION, case_id, "primary"),
            slack_client.button("Payment received", PAYMENT_RECEIVED_ACTION, case_id),
        ),
    ]


def schedule_commitment_reminder(
    case_id: str,
    commitment_date: str,
    buyer_name: str,
    call_excerpt: str = "",
    channel: str | None = None,
) -> ScheduleResult:
    target_channel = channel or slack_client.default_channel()

    if not settings.api_keys.slack_bot_token:
        return ScheduleResult(ok=False, channel=target_channel, reason="SLACK_BOT_TOKEN is not set")

    target = parse_iso_date(commitment_date)
    if target is None:
        return ScheduleResult(
            ok=False,
            channel=target_channel,
            reason=f"the commitment date could not be parsed: {commitment_date!r}",
        )

    post_at = post_at_epoch(target)
    if post_at <= int(datetime.now(timezone.utc).timestamp()):
        return ScheduleResult(
            ok=False,
            post_at=post_at,
            channel=target_channel,
            reason="the commitment date has already passed",
        )

    result = slack_client.schedule_message(
        f"Voice commitment reminder for case {case_id}: {buyer_name} promised to pay by "
        f"{commitment_date}.",
        post_at,
        channel=target_channel,
        blocks=build_commitment_blocks(case_id, commitment_date, buyer_name, call_excerpt),
    )

    if not result.ok:
        return ScheduleResult(
            ok=False, post_at=post_at, channel=target_channel, reason=str(result.error)
        )

    log.info("scheduled the commitment reminder for case %s on %s", case_id, commitment_date)
    return ScheduleResult(
        ok=True,
        scheduled_message_id=result.scheduled_message_id or "",
        post_at=post_at,
        channel=target_channel,
        reason="scheduled",
    )
