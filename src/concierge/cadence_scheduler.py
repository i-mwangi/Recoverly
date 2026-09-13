from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final

from src.concierge import slack_client
from src.concierge.reminders import parse_iso_date, post_at_epoch
from src.config import settings

log = logging.getLogger("recoverly.concierge.cadence_scheduler")

AAA_CADENCE_DAY: Final = 60
RUN_NOW_ACTION_PREFIX: Final = "run_collection_day_"


@dataclass(frozen=True, slots=True)
class CadenceStage:
    day: int
    label: str
    detail: str

    @property
    def drafting_agent(self) -> str:
        return "the arbitration specialist" if self.day >= AAA_CADENCE_DAY else "the diplomat"

    @property
    def action_id(self) -> str:
        return f"{RUN_NOW_ACTION_PREFIX}{self.day}"


CADENCE_STAGES: Final[tuple[CadenceStage, ...]] = (
    CadenceStage(7, "Friendly reminder", "warm first touch"),
    CadenceStage(14, "Second touch", "firmer, restates the terms"),
    CadenceStage(30, "Escalation notice", "formal delinquency notice"),
    CadenceStage(45, "Pre-arbitration warning", "final notice before a demand letter"),
    CadenceStage(60, "Demand letter trigger", "formal demand letter is drafted"),
)


@dataclass(frozen=True, slots=True)
class ScheduledReminder:
    day: int
    post_at: int
    ok: bool
    scheduled_message_id: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "post_at": self.post_at,
            "ok": self.ok,
            "scheduled_message_id": self.scheduled_message_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CalendarResult:
    ok: bool
    reminders: list[ScheduledReminder] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def scheduled_count(self) -> int:
        return sum(1 for reminder in self.reminders if reminder.ok)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "scheduled_count": self.scheduled_count,
            "reminders": [reminder.as_dict() for reminder in self.reminders],
            "errors": self.errors,
            "reason": self.reason,
        }


def build_reminder_blocks(
    stage: CadenceStage,
    case_id: str,
    buyer_name: str,
    invoice_no: str,
    outstanding_usd: float,
) -> list[dict[str, Any]]:
    body = (
        f"*{stage.label}* — {stage.detail}\n\n"
        f"• *Buyer*: {buyer_name}\n"
        f"• *Invoice*: `{invoice_no}`\n"
        f"• *Outstanding*: ${outstanding_usd:,.2f}\n"
        f"• *Case*: `{case_id}`\n\n"
        f"Day {stage.day} is due now. Run it to have {stage.drafting_agent} prepare the draft."
    )

    return [
        slack_client.header_block(f"Day {stage.day} reminder — case {case_id}"),
        slack_client.section_block(body),
        slack_client.context_block(
            f"Nothing is sent to the buyer until you approve the draft that follows."
        ),
        slack_client.actions_block(
            slack_client.button(
                "Run now", stage.action_id, f"{case_id}:{stage.day}", "primary"
            )
        ),
    ]


def schedule_collection_calendar(
    case_id: str,
    due_date: str,
    buyer_name: str,
    invoice_no: str,
    outstanding_usd: float,
    channel: str | None = None,
) -> CalendarResult:
    if not settings.api_keys.slack_bot_token:
        return CalendarResult(ok=False, reason="SLACK_BOT_TOKEN is not set")

    due = parse_iso_date(due_date)
    if due is None:
        return CalendarResult(ok=False, reason=f"the due date could not be parsed: {due_date!r}")

    target_channel = channel or slack_client.default_channel()
    now_epoch = int(datetime.now(timezone.utc).timestamp())

    reminders: list[ScheduledReminder] = []
    errors: list[str] = []

    for stage in CADENCE_STAGES:
        post_at = post_at_epoch(due + timedelta(days=stage.day))

        if post_at <= now_epoch:
            reminders.append(
                ScheduledReminder(stage.day, post_at, ok=False, reason="that day has passed")
            )
            continue

        result = slack_client.schedule_message(
            f"Day {stage.day} reminder for case {case_id}: {buyer_name}, invoice "
            f"{invoice_no}, ${outstanding_usd:,.2f} outstanding.",
            post_at,
            channel=target_channel,
            blocks=build_reminder_blocks(
                stage, case_id, buyer_name, invoice_no, outstanding_usd
            ),
        )

        if result.ok:
            reminders.append(
                ScheduledReminder(
                    stage.day,
                    post_at,
                    ok=True,
                    scheduled_message_id=result.scheduled_message_id or "",
                )
            )
        else:
            errors.append(f"day {stage.day}: {result.error}")
            reminders.append(
                ScheduledReminder(stage.day, post_at, ok=False, reason=str(result.error))
            )

    scheduled = sum(1 for reminder in reminders if reminder.ok)
    log.info("scheduled %d of %d reminders for case %s", scheduled, len(CADENCE_STAGES), case_id)

    return CalendarResult(ok=scheduled > 0, reminders=reminders, errors=errors)


def stage_for_day(day: int) -> CadenceStage | None:
    return next((stage for stage in CADENCE_STAGES if stage.day == day), None)
