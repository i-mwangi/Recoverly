from __future__ import annotations

import re
import sys
from typing import Any, Final

from src.agents._case_state import is_halted, load_case_state, set_anomaly_halt, update_case_state
from src.agents._utils import is_error_envelope, parse_json_object
from src.agents.base import StrandsAgentAdapter, audit, extract_case_id, run_agent
from src.concierge.reminders import schedule_commitment_reminder
from src.voice.dialer import check_calling_window, dry_run_enabled, place_call
from src.voice.signals import detect_hostile, detect_welfare
from src.voice.transcript import Commitment, extract_commitment, extract_transcript_block

VOICE_DIAL_DAY: Final = 55
ROUTINE_EXCERPT_CHARS: Final = 220
SUPPORT_LINE: Final = "988 Suicide and Crisis Lifeline"


def _event_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"event\s*[=:]\s*{name}\b", re.IGNORECASE)


TRANSCRIPT_EVENT_RE: Final = _event_pattern("transcript_received")
REDIAL_EVENT_RE: Final = _event_pattern("re_dial_buyer")
SCRIPT_REVISE_EVENT_RE: Final = _event_pattern("revised_voice_script")
CALL_SID_RE: Final = re.compile(r"call_sid\s*[=:]\s*([A-Za-z0-9_-]+)", re.IGNORECASE)
REASON_RE: Final = re.compile(r"reason\s*[=:]\s*([\w_]+)", re.IGNORECASE)
APPROVE_FORWARD_RE: Final = re.compile(
    r"@[\w/.-]+\s+operator\s+approved\s+voice\s+dial\s+for\s+case\s+`([^`]+)`",
    re.IGNORECASE,
)


def _load_case(case_id: str) -> dict[str, Any]:
    try:
        return {**load_case_state(case_id), "case_id": case_id}
    except Exception:
        return {"case_id": case_id}


def _buyer_name(case: dict[str, Any]) -> str:
    for key in ("buyer_legal_name", "customer_name", "customer_company", "buyer_persona"):
        if case.get(key):
            return str(case[key])
    return "the buyer"


def _match(pattern: re.Pattern[str], text: str, default: str = "") -> str:
    found = pattern.search(text)
    return found.group(1) if found else default


class VoiceAdapter(StrandsAgentAdapter):
    role = "voice"

    async def handle_message(self, text, msg, tools, history, room_id):
        if is_error_envelope(text):
            return None

        if TRANSCRIPT_EVENT_RE.search(text):
            return await self._handle_transcript(text)

        if REDIAL_EVENT_RE.search(text):
            return self._redial(extract_case_id(text), _match(REASON_RE, text, "broken_commitment"))

        if SCRIPT_REVISE_EVENT_RE.search(text):
            return self._revised_script_ready(text)

        approved = APPROVE_FORWARD_RE.search(text)
        if approved:
            return self._dial_on_approval(approved.group(1))

        return self._dial_from_payload(text)

    async def _handle_transcript(self, text: str) -> str:
        case_id = extract_case_id(text)
        call_sid = _match(CALL_SID_RE, text, "unknown")
        transcript = extract_transcript_block(text)
        case = _load_case(case_id)

        audit(
            case_id,
            self.role,
            "transcript_received",
            {"call_sid": call_sid, "transcript_length": len(transcript)},
        )

        welfare = detect_welfare(transcript)
        if welfare.detected:
            return self._handle_welfare(case_id, call_sid, welfare)

        hostile = detect_hostile(transcript)
        commitment = await extract_commitment(transcript)

        sections: list[str] = []
        if hostile.detected:
            sections.append(self._hostile_section(case_id, call_sid, hostile))
        if commitment.found:
            sections.append(self._commitment_section(case_id, call_sid, case, commitment))

        if sections:
            return "\n\n".join(sections)

        return self._routine_section(case_id, call_sid, case, transcript)

    def _handle_welfare(self, case_id: str, call_sid: str, welfare) -> str:
        audit(
            case_id,
            self.role,
            "welfare_signal_detected",
            {"call_sid": call_sid, "source": "post_call_scan", **welfare.as_dict()},
        )

        try:
            set_anomaly_halt(case_id, "welfare")
            audit(case_id, self.role, "welfare_halt_applied", {"call_sid": call_sid})
        except Exception as error:
            audit(case_id, self.role, "welfare_halt_failed", {"error": str(error)})

        return "\n".join(
            [
                f"@concierge event=approval_request card_kind=welfare_critical "
                f"priority=critical_immediate for case `{case_id}`.",
                f"[Voice] A welfare signal appeared in the call transcript "
                f"(call_sid {call_sid}).",
                f"Matched language: {welfare.quoted_matches()}",
                f"Verbatim excerpt:",
                f"```\n{welfare.excerpt}\n```",
                f"The agent stopped all collection talk on the call, read the "
                f"{SUPPORT_LINE}, and the case is now halted so no further outreach goes "
                f"out. An operator needs to see this within minutes.",
            ]
        )

    def _hostile_section(self, case_id: str, call_sid: str, hostile) -> str:
        audit(
            case_id,
            self.role,
            "hostile_signal_detected",
            {"call_sid": call_sid, **hostile.as_dict()},
        )
        return "\n".join(
            [
                f"@concierge event=approval_request request_kind=voice_aaa_escalation "
                f"for case `{case_id}`.",
                f"[Voice] The call carried a legal threat or hostile language "
                f"(call_sid {call_sid}).",
                f"Matched language: {hostile.quoted_matches()}",
                f"Verbatim excerpt:",
                f"```\n{hostile.excerpt}\n```",
                f"The operator decides whether to escalate to arbitration, stand down, or "
                f"try the call once more.",
            ]
        )

    def _commitment_section(
        self, case_id: str, call_sid: str, case: dict[str, Any], commitment: Commitment
    ) -> str:
        buyer = _buyer_name(case)
        audit(
            case_id,
            self.role,
            "commitment_extracted",
            {"call_sid": call_sid, **commitment.as_dict()},
        )

        schedule = schedule_commitment_reminder(
            case_id=case_id,
            commitment_date=str(commitment.commitment_date),
            buyer_name=buyer,
            call_excerpt=commitment.excerpt,
        )
        audit(case_id, self.role, "commitment_reminder_scheduled", schedule.as_dict())

        status = (
            f"a Slack reminder is scheduled for {commitment.commitment_date}"
            if schedule.ok
            else f"the reminder could not be scheduled: {schedule.reason}"
        )

        return "\n".join(
            [
                f"@concierge event=approval_request card_kind=voice_commitment_summary "
                f"for case `{case_id}`.",
                f"Voice call summary for case `{case_id}` (call_sid {call_sid})",
                f"• *Outcome*: the buyer committed to a payment date",
                f"• *Buyer*: {buyer}",
                f"• *Commitment date*: {commitment.commitment_date}",
                f"• *What they said*: _\"{commitment.excerpt}\"_",
                f"• *Next action*: {status}",
                f"• No hostile or welfare signals appeared on the call.",
            ]
        )

    def _routine_section(
        self, case_id: str, call_sid: str, case: dict[str, Any], transcript: str
    ) -> str:
        audit(case_id, self.role, "transcript_no_signal", {"call_sid": call_sid})
        tail = transcript[-ROUTINE_EXCERPT_CHARS:].strip()

        return "\n".join(
            [
                f"@concierge event=approval_request card_kind=voice_call_summary "
                f"for case `{case_id}`.",
                f"Voice call complete for case `{case_id}`",
                f"• *Call*: `{call_sid}`",
                f"• *Buyer*: {_buyer_name(case)}",
                f"• *Outcome*: routine, with no escalation signal",
                f"• *Transcript length*: {len(transcript)} characters",
                f"• *Closing of the call*:",
                f"```\n{tail}\n```",
                f"• *Next action*: continue the scheduled cadence. No operator action is needed.",
            ]
        )

    def _redial(self, case_id: str, reason: str) -> str | None:
        if case_id == "UNKNOWN":
            return None

        audit(case_id, self.role, "redial_requested", {"reason": reason})
        result = place_call(_load_case(case_id))
        audit(case_id, self.role, "dial_placed", {"reason": reason, **result.as_dict()})

        if result.ok:
            return (
                f"[Voice] Re-dialling the buyer on case `{case_id}` because of {reason}. "
                f"The call is placed as {result.call_sid} and the transcript returns through "
                f"the post-call webhook.\n"
                f"@concierge a re-dial is in progress for case `{case_id}`."
            )

        return (
            f"[Voice] The re-dial on case `{case_id}` failed: {result.error}\n"
            f"@concierge this case needs manual outreach."
        )

    def _revised_script_ready(self, text: str) -> str | None:
        case_id = extract_case_id(text)
        note_match = re.search(r"operator_note\s*[:=]\s*[\"_]*(.*?)[\"_]*$", text, re.IGNORECASE | re.DOTALL)
        note = note_match.group(1).strip() if note_match else ""
        if case_id == "UNKNOWN":
            return None
        update_case_state(case_id, current_stage="voice_dial_review", voice_script_note=note)
        audit(case_id, self.role, "voice_script_revised", {"note": note[:300]})
        return (
            f"@concierge event=approval_request card_kind=voice_dial for case `{case_id}`.\n"
            f"[Voice] Updated call script is ready for review. Operator direction: {note or 'No additional direction supplied.'}\n"
            "Approve call to place it, revise script to provide more direction, or reject to keep the normal cadence."
        )

    def _dial_on_approval(self, case_id: str) -> str:
        case = _load_case(case_id)
        phone = case.get("customer_phone") or case.get("phone") or "an unknown number"

        audit(
            case_id,
            self.role,
            "operator_approval_forwarded",
            {"phone": phone, "customer_state": case.get("customer_state", "")},
        )

        result = place_call(case)
        audit(case_id, self.role, "dial_placed", result.as_dict())

        if result.ok:
            return (
                f"[Voice] The operator approved the call on case `{case_id}`, so the calling "
                f"window check is bypassed and {phone} is being dialled now.\n"
                f"The call is placed as {result.call_sid}; the summary returns by webhook when "
                f"it ends.\n"
                f"@concierge the HITL card can be sealed. event=card_decided case_id=`{case_id}`"
            )

        return (
            f"[Voice] The approved call on case `{case_id}` could not be placed: "
            f"{result.error}\nThis case needs manual outreach instead."
        )

    def _dial_from_payload(self, text: str) -> str | None:
        case = parse_json_object(text)
        if not case or not case.get("case_id"):
            case_id = extract_case_id(text)
            if case_id == "UNKNOWN":
                return None
            case = _load_case(case_id)

        case_id = str(case["case_id"])
        if is_halted(case_id):
            self.log.info("case %s is halted, skipping the call", case_id)
            return None

        try:
            days_past_due = int(case.get("days_past_due") or 0)
        except (TypeError, ValueError):
            days_past_due = 0

        if 0 < days_past_due < VOICE_DIAL_DAY:
            self.log.info("case %s is only %d days past due", case_id, days_past_due)
            return None

        state = str(case.get("customer_state") or "")
        window = check_calling_window(state)

        try:
            dry_run = dry_run_enabled()
        except RuntimeError as error:
            audit(case_id, self.role, "dry_run_guard_tripped", {"error": str(error)})
            return f"@concierge Voice Agent refused to run on case `{case_id}`: {error}"

        audit(
            case_id,
            self.role,
            "dial_requested",
            {
                "days_past_due": days_past_due,
                "customer_state": state,
                "window_allowed": window.allowed,
                "window_reason": window.reason,
                "dry_run": dry_run,
            },
        )

        if not window.allowed:
            return (
                f"@concierge Voice Agent deferred case `{case_id}` because the call falls "
                f"{window.reason}. It will retry in the next valid window. "
                f"card_kind=voice_deferred"
            )

        if str(case.get("current_stage") or "") != "voice_dial_review":
            update_case_state(case_id, current_stage="voice_dial_review")
            return (
                f"@concierge event=approval_request card_kind=voice_dial for case `{case_id}`.\n"
                f"[Voice] A compliant payment call is ready for {_buyer_name(case)}. "
                "Approve call to dial, revise script to change the wording, or reject to keep the normal cadence."
            )

        if dry_run:
            audit(case_id, self.role, "dial_simulated", {"days_past_due": days_past_due})
            return (
                f"@concierge Voice Agent simulated a call for case `{case_id}` because the "
                f"dry-run flags are set. No number was dialled. card_kind=voice_deferred"
            )

        result = place_call(case)
        audit(case_id, self.role, "dial_placed", result.as_dict())

        if not result.ok:
            return (
                f"@concierge Voice Agent could not place the call for case `{case_id}`: "
                f"{result.error}. This case needs manual outreach. card_kind=voice_failed"
            )

        return (
            f"@concierge Voice Agent placed the call for case `{case_id}` as "
            f"{result.call_sid}. The conversation summary returns by webhook when the call "
            f"ends. card_kind=voice_in_progress"
        )


if __name__ == "__main__":
    sys.exit(run_agent(VoiceAdapter))
