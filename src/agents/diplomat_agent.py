from __future__ import annotations

import re
import sys
from typing import Any, Final

from src.agents._case_state import add_cost, is_halted, load_case_state, update_case_state
from src.agents._utils import is_error_envelope, parse_json_object
from src.agents.base import StrandsAgentAdapter, audit, run_agent
from src.diplomat.outbound import OutboundDispatchError, send_day7_reminder
from src.diplomat.templates import outstanding_amount
from src.investigator.pattern_tag import tone_for
from src.llm.provider import complete_async
from src.utils.text import slack_preview

AAA_HANDOFF_DAY: Final = 60
MAX_DRAFT_TOKENS: Final = 900

RUN_COLLECTION_DAY_RE: Final = re.compile(r"event\s*[=:]\s*run_collection_day\b", re.IGNORECASE)
DAY_OFFSET_RE: Final = re.compile(r"day_offset:\s*(\d+)", re.IGNORECASE)
CADENCE_STAGE_RE: Final = re.compile(r"cadence_stage:\s*(\w+)", re.IGNORECASE)
CASE_ID_RE: Final = re.compile(r"case\s+`([^`]+)`", re.IGNORECASE)
MENTION_PREFIX: Final = r"(?:@[\w/.-]+|@\[\[[\w-]+\]\])"
APPROVE_FORWARD_RE: Final = re.compile(
    rf"{MENTION_PREFIX}\s+operator\s+approved\s+case\s+`([^`]+)`", re.IGNORECASE
)
REVISE_FORWARD_RE: Final = re.compile(
    rf"{MENTION_PREFIX}\s+operator\s+requested\s+(?:revision|re-draft|script\s+revision)\s+"
    rf"(?:for\s+(?:\w+\s+)?)?(?:case\s+)?`([^`]+)`",
    re.IGNORECASE,
)
OPERATOR_NOTE_RE: Final = re.compile(r"_operator_note_=([^_\n]+)_", re.IGNORECASE)

BASE_PERSONA: Final = (
    "You are the Diplomat agent for Recoverly, a professional business-to-business "
    "accounts-receivable assistant. Output only the email body: no subject line, no JSON "
    "and no markdown fences. Keep the payment link placeholder {{CHECKOUT_URL}} exactly as "
    "written wherever the payment link belongs."
)

CADENCE_PROMPTS: Final[dict[int, str]] = {
    7: (
        f"{BASE_PERSONA} Draft a day-7 first-touch reminder. The tone is warm, respectful and "
        f"brief. Do not threaten legal action. Reference the invoice number and due date, and "
        f"ask whether anything is blocking payment. Maximum 120 words."
    ),
    14: (
        f"{BASE_PERSONA} Draft a day-14 second-touch follow-up. The tone is firm but "
        f"professional. Reference the earlier friendly reminder and ask for a concrete payment "
        f"date this week. Do not threaten legal action, but be clear that payment is materially "
        f"late. Maximum 160 words."
    ),
    30: (
        f"{BASE_PERSONA} Draft a day-30 escalation notice. The tone is formal and serious. "
        f"State the 30-day delinquency, reference the contract payment terms, cite the "
        f"outstanding balance, and request payment within five business days before the matter "
        f"goes to legal review. Maximum 200 words."
    ),
    45: (
        f"{BASE_PERSONA} Draft a day-45 pre-arbitration warning. The tone is formal and "
        f"pre-legal. Reference the contract's dispute resolution clause and give the buyer 14 "
        f"calendar days to cure before a formal demand letter is issued and an arbitration "
        f"filing is prepared. Note that the invoice and contract copies are attached. "
        f"Maximum 220 words."
    ),
}


def _match(pattern: re.Pattern[str], text: str) -> str:
    found = pattern.search(text)
    return found.group(1).strip() if found else ""


def _load_case(case_id: str) -> dict[str, Any]:
    try:
        return {**load_case_state(case_id), "case_id": case_id}
    except Exception:
        return {"case_id": case_id}


def build_cadence_prompt(case: dict[str, Any], day_offset: int, stage: str) -> str:
    return (
        f"Case `{case.get('case_id')}` — day-{day_offset} cadence (stage: {stage}).\n"
        f"Buyer: {case.get('buyer_legal_name') or case.get('customer_name') or 'Customer'}\n"
        f"Outstanding: USD ${outstanding_amount(case):,.2f}\n"
        f"Invoice: {case.get('invoice_no') or 'INV-UNKNOWN'}\n"
        f"Original due date: {case.get('due_date') or 'the original due date'}\n"
        f"Customer payment pattern: {case.get('pattern_tag') or 'unknown'}\n\n"
        f"Draft the day-{day_offset} email body per the rules above."
    )


class DiplomatAdapter(StrandsAgentAdapter):
    role = "diplomat"

    async def handle_message(self, text, msg, tools, history, room_id):
        if is_error_envelope(text):
            return None

        if RUN_COLLECTION_DAY_RE.search(text):
            return await self._run_cadence_day(text)

        approved_case_id = _match(APPROVE_FORWARD_RE, text)
        if approved_case_id:
            return self._dispatch_approved(approved_case_id)

        revise_case_id = _match(REVISE_FORWARD_RE, text)
        if revise_case_id:
            return self._acknowledge_revision(revise_case_id, text)

        return await self._draft_from_payload(text)

    async def _run_cadence_day(self, text: str) -> str | None:
        case_id = _match(CASE_ID_RE, text)
        raw_day = _match(DAY_OFFSET_RE, text)
        if not case_id or not raw_day:
            return None

        day_offset = int(raw_day)
        stage = _match(CADENCE_STAGE_RE, text) or f"day_{day_offset}"

        if day_offset >= AAA_HANDOFF_DAY:
            audit(
                case_id,
                self.role,
                "cadence_misroute",
                {"day_offset": day_offset, "reason": "day_60_and_later_belongs_to_aaa_specialist"},
            )
            return (
                f"[Diplomat] Day-{day_offset} cadence belongs to @aaa_specialist, not Diplomat. "
                f"No draft produced for case `{case_id}`."
            )

        system_prompt = CADENCE_PROMPTS.get(day_offset)
        if system_prompt is None:
            audit(case_id, self.role, "cadence_unknown_day", {"day_offset": day_offset})
            return (
                f"[Diplomat] No cadence template for day_offset={day_offset} on case "
                f"`{case_id}`. No draft produced."
            )

        case = _load_case(case_id)
        try:
            body, usage = await complete_async(
                system_prompt,
                build_cadence_prompt(case, day_offset, stage),
                agent_role=self.role,
                max_tokens=MAX_DRAFT_TOKENS,
            )
        except Exception as error:
            audit(
                case_id, self.role, "cadence_llm_error", {"day_offset": day_offset, "error": str(error)}
            )
            return (
                f"[Diplomat] Day-{day_offset} {stage} draft failed for case `{case_id}`: {error}"
            )

        add_cost(case_id, usage)
        self._persist_draft(case_id, body, current_stage="tone_review", cadence_day=day_offset)
        audit(
            case_id,
            self.role,
            "cadence_draft_done",
            {"day_offset": day_offset, "cadence_stage": stage, "body_length": len(body), "usage": usage},
        )

        return (
            f"@tone_coach event=draft_ready for case `{case_id}` "
            f"(cadence_stage: {stage}, day_offset: {day_offset}). Please audit the tone; a firmer "
            f"tone is allowed from day 30 onward.\n\n```\n{slack_preview(body)}\n```"
        )

    def _dispatch_approved(self, case_id: str) -> str:
        case = _load_case(case_id)
        outstanding = outstanding_amount(case)
        pattern_tag = case.get("pattern_tag") or "unknown"

        audit(
            case_id,
            self.role,
            "operator_approval_forwarded",
            {"pattern_tag": pattern_tag, "outstanding_balance_usd": outstanding},
        )

        try:
            result = send_day7_reminder(case)
        except OutboundDispatchError as error:
            return (
                f"[Diplomat] Acknowledged the approval on case `{case_id}`, but the email could "
                f"not be sent: {error}\nThe operator should retry once the mail backend "
                f"credentials are fixed."
            )

        return (
            f"[Diplomat] Approval received for case `{case_id}`. Pattern tag `{pattern_tag}`, "
            f"outstanding ${outstanding:,.2f}.\n"
            f"Sent via {result.backend} to {result.to} (message id {result.message_id}).\n"
            f"@concierge the HITL card can be sealed. case_decided case_id=`{case_id}` "
            f"event=card_decided"
        )

    def _acknowledge_revision(self, case_id: str, text: str) -> str:
        note = _match(OPERATOR_NOTE_RE, text) or "(no note)"
        audit(case_id, self.role, "operator_revision_forwarded", {"operator_note": note})
        return (
            f"[Diplomat] Revision request received for case `{case_id}`. Operator note: {note}\n"
            f"The redraft runs on the approval-router path; a new HITL card will follow in Slack."
        )

    async def _draft_from_payload(self, text: str) -> str | None:
        case = parse_json_object(text)
        if not case or not case.get("case_id"):
            return None

        case_id = str(case["case_id"])
        if is_halted(case_id):
            self.log.info("case %s is halted, skipping", case_id)
            return None

        pattern_tag = str(case.get("pattern_tag") or "unknown")
        outstanding = outstanding_amount(case)

        audit(
            case_id,
            self.role,
            "diplomat_start",
            {"pattern_tag": pattern_tag, "outstanding_balance_usd": outstanding},
        )

        prompt = (
            f"Case {case_id}: {case.get('customer_name', 'Customer')} owes "
            f"${outstanding:,.2f} outstanding on invoice "
            f"{case.get('invoice_no') or case.get('invoice_id') or 'N/A'} "
            f"(due {case.get('due_date', 'unknown')}). Today is day 7 past due. "
            f"Customer payment pattern: {pattern_tag}, so the opening tone should be "
            f"{tone_for(pattern_tag)}. "
            f"Accounts payable contact: {case.get('customer_ap_contact', 'the AP team')}. "
            f"Cite the outstanding balance, never the gross invoice, because the deposit is "
            f"already paid."
        )

        try:
            body, usage = await complete_async(
                CADENCE_PROMPTS[7], prompt, agent_role=self.role, max_tokens=MAX_DRAFT_TOKENS
            )
        except Exception as error:
            audit(case_id, self.role, "llm_error", {"error": str(error)})
            return f"@tone_coach Diplomat could not draft case `{case_id}`: {error}"

        add_cost(case_id, usage)
        self._persist_draft(case_id, body, current_stage="tone_review", cadence_day=7)
        audit(case_id, self.role, "diplomat_done", {"body_length": len(body), "usage": usage})

        return (
            f"@tone_coach Diplomat drafted the day-7 email for case `{case_id}`. "
            f"Please audit the tone:\n\n```\n{slack_preview(body)}\n```"
        )

    def _persist_draft(self, case_id: str, body: str, *, current_stage: str, cadence_day: int) -> None:
        try:
            update_case_state(
                case_id,
                current_stage=current_stage,
                revised_email_body=body,
                cadence_day=cadence_day,
            )
        except Exception as error:
            audit(case_id, self.role, "draft_persist_failed", {"error": str(error)})


if __name__ == "__main__":
    sys.exit(run_agent(DiplomatAdapter))
