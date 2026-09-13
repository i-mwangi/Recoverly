from __future__ import annotations

import logging
import os
from typing import Any, Final

from src.aaa.outbound import send_demand_letter
from src.aaa.templates import CURE_WINDOW_DAYS, filing_fee_estimate, render_demand_letter
from src.agents._case_state import stop_case, update_case_state
from src.agents._utils import safe_float
from src.config import settings
from src.concierge.actions.base import (
    ActionContext,
    ActionResult,
    register,
    register_prefix,
)
from src.concierge.band_relay import notify_agent
from src.concierge.cadence_scheduler import AAA_CADENCE_DAY, stage_for_day
from src.concierge.cards import post_card
from src.diplomat.outbound import send_day7_reminder, send_lite_final_notice
from src.outbound import OutboundDispatchError
from src.voice.dialer import place_call

log = logging.getLogger("recoverly.concierge.actions.handlers")

LITE_MODE: Final = "lite"
ATTORNEY_MODE: Final = "attorney_recommended"


def _aaa_strategy(case_id: str, case: dict[str, Any]) -> str:
    outstanding = safe_float(
        case.get("outstanding_balance_usd") or case.get("amount_balance_usd") or case.get("amount_usd"),
        0.0,
    )


def _mirror_to_console(case_id: str, actor: str, body: str, reasoning: str) -> None:
    from src.local_console.service import append_case_activity

    append_case_activity(case_id, actor, body, reasoning=reasoning)
    filing_fee = filing_fee_estimate(outstanding)
    venue = str(case.get("customer_state") or case.get("buyer_state") or "the buyer's home state")
    return (
        f"1. **Governing law and venue** — follow the agreement's dispute clause, with venue in the regional office covering {venue}.\n"
        f"2. **Filing fee** — roughly ${filing_fee:,} on the standard commercial schedule for a claim of ${outstanding:,.0f}; recoverable where the agreement permits fees.\n"
        "3. **Timeline** — six to nine months to an award on documents, and twelve to eighteen months if the matter is fully heard.\n"
        f"4. **Next step** — send a {CURE_WINDOW_DAYS}-day cure letter first. The legal-threat signal requires outbound calling to remain paused while the buyer or counsel has a clear response window."
    )


def _sent_line(result: Any) -> str:
    return f"Sent via {result.backend} to {result.to} (message id {result.message_id})."


@register("hitl_approve")
def approve_draft(context: ActionContext) -> ActionResult:
    case = context.case()
    path_mode = str(case.get("path_mode") or "")
    context.audit("operator_approved", {"path_mode": path_mode})

    if path_mode == ATTORNEY_MODE and not case.get("attorney_tag"):
        return ActionResult(
            f"Case `{context.case_id}` is on the attorney path, so use the buttons on that "
            f"card instead of the generic approve."
        )

    try:
        result = send_day7_reminder(case)
    except OutboundDispatchError as error:
        context.audit("operator_approval_send_failed", {"error": str(error)})
        return ActionResult(
            f"The email for case `{context.case_id}` did not send: {error}"
        )

    update_case_state(
        context.case_id, status="approved", current_stage="awaiting_payment"
    )
    notify_agent(
        "diplomat",
        f"event=email_dispatched for case `{context.case_id}`. The operator-approved reminder has been sent.",
        case_id=context.case_id,
    )
    return ActionResult(
        f"Approved case `{context.case_id}`. {_sent_line(result)}", seal_card=True
    )


@register("hitl_reject")
def reject_draft(context: ActionContext) -> ActionResult:
    context.audit("operator_rejected")
    stop_case(context.case_id, "operator_rejected")
    notify_agent(
        "diplomat",
        f"operator rejected case `{context.case_id}`. No outreach goes out.",
        case_id=context.case_id,
    )
    return ActionResult(
        f"Rejected case `{context.case_id}`. The cadence is stopped.", seal_card=True
    )


@register("hitl_revise")
def revise_draft(context: ActionContext) -> ActionResult:
    context.audit("operator_requested_revision")
    notify_agent(
        "diplomat",
        f"operator requested revision for case `{context.case_id}`.",
        case_id=context.case_id,
    )
    return ActionResult(
        f"Asked the diplomat to redraft case `{context.case_id}`. A new card will follow."
    )


@register_prefix("attorney_run_")
def attorney_run(context: ActionContext) -> ActionResult:
    context.audit("attorney_tag_set")
    update_case_state(context.case_id, attorney_tag=True, current_stage="investigator")
    notify_agent(
        "investigator",
        f"operator approved attorney tagging for case `{context.case_id}`. "
        f"Run the enrichment, then hand off for the demand letter.",
        case_id=context.case_id,
    )
    return ActionResult(
        f"Case `{context.case_id}` continues through the full flow, tagged for attorney "
        f"follow-up.",
        seal_card=True,
    )


@register_prefix("attorney_skip_")
def attorney_skip(context: ActionContext) -> ActionResult:
    context.audit("attorney_referrals_requested")
    stop_case(context.case_id, "operator_chose_attorney_referral")
    return ActionResult(
        f"Case `{context.case_id}` is out of the automated flow. Referral contacts are in "
        f"`config/attorney_referrals.yaml`.",
        seal_card=True,
    )


@register_prefix("lite_run_")
def lite_run(context: ActionContext) -> ActionResult:
    case = {**context.case(), "lite_decision": True}
    context.audit("lite_final_notice_approved")

    try:
        result = send_lite_final_notice(case)
    except OutboundDispatchError as error:
        context.audit("lite_final_notice_send_failed", {"error": str(error)})
        return ActionResult(
            f"The final notice for case `{context.case_id}` did not send: {error}"
        )

    update_case_state(
        context.case_id,
        lite_decision=True,
        status="approved",
        current_stage="awaiting_payment",
    )
    return ActionResult(
        f"Final notice sent for case `{context.case_id}`. {_sent_line(result)}",
        seal_card=True,
    )


@register_prefix("lite_writeoff_")
def lite_write_off(context: ActionContext) -> ActionResult:
    context.audit("lite_written_off")
    stop_case(context.case_id, "operator_wrote_off")
    return ActionResult(
        f"Case `{context.case_id}` is written off and closed.", seal_card=True
    )


@register("voice_dial_approve")
def approve_voice_dial(context: ActionContext) -> ActionResult:
    case = context.case()
    # A demo document intentionally contains no real buyer telephone number.
    # Route that case to the operator's Twilio-verified demo phone instead.
    if not (case.get("customer_phone") or case.get("phone")) and settings.demo_mode:
        demo_phone = os.getenv("TWILIO_VERIFIED_DEMO_NUMBER", "").strip()
        if demo_phone:
            case["customer_phone"] = demo_phone
            update_case_state(context.case_id, customer_phone=demo_phone)

    result = place_call(case)
    context.audit("voice_dial_approved", result.as_dict())
    if not result.ok:
        update_case_state(context.case_id, current_stage="voice_dial_review")
        return ActionResult(
            f"Call for case `{context.case_id}` was not placed: {result.error}. "
            "Check the case phone number and Twilio configuration, then try again."
        )

    update_case_state(
        context.case_id,
        current_stage="voice_dialling",
        twilio_call_sid=result.call_sid,
    )
    return ActionResult(
        f"Call approved for case `{context.case_id}`. Twilio is dialling now "
        f"(call {result.call_sid}).",
        seal_card=True,
    )


@register("voice_dial_reject")
def reject_voice_dial(context: ActionContext) -> ActionResult:
    context.audit("voice_dial_rejected")
    update_case_state(context.case_id, current_stage="awaiting_payment")
    return ActionResult(
        f"Voice call rejected for case `{context.case_id}`. No call was placed.",
        seal_card=True,
    )


@register("voice_aaa_escalate_approve")
def escalate_to_arbitration(context: ActionContext) -> ActionResult:
    case = context.case()
    strategy = _aaa_strategy(context.case_id, case)
    context.audit("voice_escalation_approved", {"strategy_generated": True})
    update_case_state(context.case_id, current_stage="aaa_strategy_review")
    result = post_card(
        context.case_id,
        f"AAA Specialist prepared an arbitration strategy for case {context.case_id}.",
        f"**Arbitration strategy — case `{context.case_id}`**\n\n{strategy}",
        card_kind="aaa_strategy_recommendation",
        case_meta=case,
    )
    if not result.ok:
        return ActionResult(
            f"The escalation was recorded, but the AAA strategy card could not be posted: {result.error or 'unknown Slack error'}."
        )
    _mirror_to_console(
        context.case_id,
        "aaa",
        f"Arbitration strategy — case {context.case_id}\n\n{strategy}",
        "Prepared the AAA strategy after the operator approved escalation and kept outbound calling paused.",
    )
    return ActionResult(
        f"Case `{context.case_id}` is with the AAA Specialist. The strategy card is now in Slack.",
        seal_card=True,
    )


@register("voice_aaa_stand_down")
def stand_down(context: ActionContext) -> ActionResult:
    context.audit("voice_escalation_declined")
    update_case_state(context.case_id, current_stage="awaiting_payment")
    return ActionResult(
        f"Standing down on case `{context.case_id}`. The normal cadence continues.",
        seal_card=True,
    )


@register("voice_try_once_more", "voice_redial_now")
def redial(context: ActionContext) -> ActionResult:
    context.audit("voice_redial_requested")
    notify_agent(
        "voice",
        f"event=re_dial_buyer for case `{context.case_id}` reason=operator_request",
        case_id=context.case_id,
    )
    return ActionResult(
        f"Asked the voice agent to call case `{context.case_id}` again."
    )


@register("aaa_draft_demand_letter_now")
def draft_demand_letter(context: ActionContext) -> ActionResult:
    case = context.case()
    subject, letter, _ = render_demand_letter(case)
    context.audit("demand_letter_requested", {"draft_generated": True})
    update_case_state(context.case_id, current_stage="aaa_demand_letter_review", aaa_demand_letter=letter)
    result = post_card(
        context.case_id,
        subject,
        f"**Demand letter draft — case `{context.case_id}`**\n\n```\n{letter}\n```",
        card_kind="aaa_demand_letter",
        case_meta=case,
    )
    if not result.ok:
        return ActionResult(
            f"The demand letter was drafted, but Slack could not post it: {result.error or 'unknown Slack error'}."
        )
    _mirror_to_console(
        context.case_id,
        "aaa",
        f"Demand letter draft — case {context.case_id}\n\n{letter}",
        "Drafted the demand letter for operator approval; it has not been sent.",
    )
    return ActionResult(
        f"The demand letter for case `{context.case_id}` is ready in Slack for approval.",
        seal_card=True,
    )


@register("aaa_hold_for_legal_review")
def hold_for_legal(context: ActionContext) -> ActionResult:
    context.audit("held_for_legal_review")
    update_case_state(context.case_id, current_stage="legal_review_hold")
    return ActionResult(
        f"Case `{context.case_id}` is on hold for legal review. Nothing further goes out.",
        seal_card=True,
    )


@register_prefix("approve_aaa_letter_")
def approve_demand_letter(context: ActionContext) -> ActionResult:
    case = context.case()
    context.audit("demand_letter_approved")

    try:
        result = send_demand_letter(case)
    except OutboundDispatchError as error:
        context.audit("demand_letter_send_failed", {"error": str(error)})
        return ActionResult(
            f"The demand letter for case `{context.case_id}` did not send: {error}"
        )

    update_case_state(
        context.case_id, current_stage="cure_window", status="demand_sent"
    )
    return ActionResult(
        f"Demand letter sent for case `{context.case_id}`. {_sent_line(result)}",
        seal_card=True,
    )


@register_prefix("revise_aaa_letter_")
def revise_demand_letter(context: ActionContext) -> ActionResult:
    context.audit("demand_letter_revision_requested")
    notify_agent(
        "aaa_specialist",
        f"operator requested revision for case `{context.case_id}`.",
        case_id=context.case_id,
    )
    return ActionResult(
        f"Asked for a redraft of the demand letter on case `{context.case_id}`."
    )


@register_prefix("reject_aaa_letter_")
def reject_demand_letter(context: ActionContext) -> ActionResult:
    context.audit("demand_letter_rejected")
    stop_case(context.case_id, "operator_rejected_demand_letter")
    return ActionResult(
        f"The demand letter for case `{context.case_id}` is rejected and the case is stopped.",
        seal_card=True,
    )


@register("payment_settled_confirm", "voice_payment_received_confirm")
def confirm_settled(context: ActionContext) -> ActionResult:
    case = context.case()
    from src.payments.channels import outstanding_for
    from src.payments.reconciler import reconcile_inbound

    amount = outstanding_for(case)
    if amount <= 0:
        return ActionResult(
            f"Case `{context.case_id}` has no remaining balance.", seal_card=True
        )
    result = reconcile_inbound(
        context.case_id,
        amount,
        f"manual:{context.case_id}:{context.message_ts or 'confirmed'}",
    )
    context.audit("payment_confirmed", result.as_dict())
    notify_agent(
        "payment",
        f'{{"event": "payment_received", "case_id": "{context.case_id}", "reconciled": true}}',
        case_id=context.case_id,
    )
    return ActionResult(
        f"Case `{context.case_id}` payment recorded: {result.status.value}. Remaining ${result.remaining_usd:,.2f}.",
        seal_card=True,
    )


@register("payment_reopen_case")
def reopen_case(context: ActionContext) -> ActionResult:
    context.audit("payment_reopened")
    update_case_state(
        context.case_id,
        status="active",
        current_stage="awaiting_payment",
        halted_reason=None,
    )
    return ActionResult(
        f"Case `{context.case_id}` is open again and back in the cadence."
    )


@register_prefix("run_collection_day_")
def run_cadence_day(context: ActionContext) -> ActionResult:
    from src.agents._case_state import TERMINAL_STATUSES

    case = context.case()
    if case.get("status") in TERMINAL_STATUSES or case.get("halted_reason"):
        return ActionResult(
            f"Case `{context.case_id}` is closed or halted. No follow-up was started."
        )
    day = (
        context.value.split(":", 1)[1]
        if ":" in context.value
        else (context.action_id.removeprefix("run_collection_day_"))
    )
    stage = stage_for_day(int(day)) if day.isdigit() else None

    if stage is None:
        return ActionResult(f"There is no cadence stage for day {day}.")

    target = "aaa_specialist" if stage.day >= AAA_CADENCE_DAY else "diplomat"
    context.audit("cadence_day_run", {"day_offset": stage.day, "target": target})
    notify_agent(
        target,
        f"event=run_collection_day case `{context.case_id}` "
        f"day_offset: {stage.day} cadence_stage: {stage.label.lower().replace(' ', '_')}",
        case_id=context.case_id,
    )
    return ActionResult(
        f"Day {stage.day} is running on case `{context.case_id}`. {stage.drafting_agent.capitalize()} "
        f"is drafting, and the draft comes back for approval before anything is sent."
    )


@register("tone_coach_override_force_approve")
def force_approve_after_tone_block(context: ActionContext) -> ActionResult:
    context.audit("tone_block_overridden")
    return approve_draft(context)


@register("tone_coach_override_reject")
def reject_after_tone_block(context: ActionContext) -> ActionResult:
    return reject_draft(context)


@register("tone_coach_override_revise")
def revise_after_tone_block(context: ActionContext) -> ActionResult:
    return revise_draft(context)
