from __future__ import annotations

import re
import sys
from typing import Any, Final

from src.aaa.outbound import OutboundDispatchError, send_demand_letter
from src.aaa.templates import (
    CURE_WINDOW_DAYS,
    filing_fee_estimate,
    render_demand_letter,
    render_orientation,
)
from src.agents._case_state import add_cost, load_case_state, update_case_state
from src.agents._utils import is_error_envelope
from src.agents.base import StrandsAgentAdapter, audit, extract_case_id, run_agent
from src.diplomat.templates import outstanding_amount
from src.llm.provider import complete_async
from src.utils.text import slack_preview

AAA_CADENCE_DAY: Final = 60
MAX_STRATEGY_TOKENS: Final = 400
HOSTILE_EXCERPT_LIMIT: Final = 400
DEFAULT_BUYER_STATE: Final = "the buyer's home state"


def _event_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"event\s*[=:]\s*{name}\b", re.IGNORECASE)


VOICE_HOSTILE_EVENT_RE: Final = _event_pattern("voice_hostile_escalation")
DRAFT_DEMAND_LETTER_EVENT_RE: Final = _event_pattern("draft_demand_letter")
RUN_COLLECTION_DAY_RE: Final = _event_pattern("run_collection_day")

DAY_OFFSET_RE: Final = re.compile(r"day_offset:\s*(\d+)", re.IGNORECASE)
CADENCE_STAGE_RE: Final = re.compile(r"cadence_stage:\s*(\w+)", re.IGNORECASE)
FENCED_EXCERPT_RE: Final = re.compile(r"```(.*?)```", re.DOTALL)
AMOUNT_IN_TEXT_RE: Final = re.compile(r"\$([\d,]+(?:\.\d+)?)")
APPROVE_FORWARD_RE: Final = re.compile(
    r"@[\w/.-]+\s+operator\s+approved\s+(?:aaa\s+(?:letter|demand\s+letter)|arbitration)\s+"
    r"(?:for\s+)?case\s+`([^`]+)`",
    re.IGNORECASE,
)

STRATEGY_PROMPT: Final = (
    "You are an arbitration specialist working on commercial collections. The buyer "
    "showed hostile or legal-threat behaviour on a follow-up call, such as a cease and "
    "desist, a lawyer mention, or a harassment claim. Recommend a strategy in exactly "
    "four numbered markdown bullets totalling 200 words or fewer: "
    "(1) governing law and venue, drawn from the contract clause and the buyer's state; "
    "(2) the filing fee estimate on the standard commercial schedule; "
    "(3) the timeline from filing to award; "
    "(4) the next step, recommending either a cure-period letter or an immediate filing, "
    "with the reason that the hostile signal justifies that choice. "
    "Output only the four bullets with no preamble."
)


def _load_case(case_id: str) -> dict[str, Any]:
    try:
        return {**load_case_state(case_id), "case_id": case_id}
    except Exception:
        return {"case_id": case_id}


def _hostile_excerpt(text: str) -> str:
    match = FENCED_EXCERPT_RE.search(text)
    return match.group(1).strip()[:HOSTILE_EXCERPT_LIMIT] if match else ""


def fallback_strategy(
    outstanding: float, filing_fee: int, buyer_state: str
) -> str:
    return (
        f"1. **Governing law and venue** — as set by the agreement's dispute clause, with "
        f"venue in the regional office covering {buyer_state}.\n"
        f"2. **Filing fee** — roughly ${filing_fee:,} on the standard commercial schedule "
        f"for a claim of ${outstanding:,.0f}, recoverable from the losing party where the "
        f"agreement has a fees clause.\n"
        f"3. **Timeline** — six to nine months to an award on documents, twelve to eighteen "
        f"if the matter is fully heard. Most commercial filings settle before the hearing.\n"
        f"4. **Next step** — given the explicit legal-threat language, send the "
        f"{CURE_WINDOW_DAYS}-day cure letter first. It preserves the good-faith record and "
        f"gives the buyer's counsel a defined window to respond before anything is filed."
    )


class AAASpecialistAdapter(StrandsAgentAdapter):
    role = "aaa_specialist"

    async def handle_message(self, text, msg, tools, history, room_id):
        if is_error_envelope(text):
            return None

        if VOICE_HOSTILE_EVENT_RE.search(text):
            return await self._recommend_strategy(extract_case_id(text), text)

        if DRAFT_DEMAND_LETTER_EVENT_RE.search(text):
            case_id = extract_case_id(text)
            audit(case_id, self.role, "draft_demand_letter_requested", {"source": "operator"})
            return self._draft_demand_letter(case_id, AAA_CADENCE_DAY, "aaa_demand_letter")

        if RUN_COLLECTION_DAY_RE.search(text):
            return self._handle_cadence_day(text)

        approved = APPROVE_FORWARD_RE.search(text)
        if approved:
            return self._dispatch_letter(approved.group(1))

        return self._orientation(text)

    async def _recommend_strategy(self, case_id: str, text: str) -> str:
        case = _load_case(case_id)
        outstanding = outstanding_amount(case)
        filing_fee = filing_fee_estimate(outstanding)
        buyer_state = str(
            case.get("customer_state") or case.get("buyer_state") or DEFAULT_BUYER_STATE
        )
        excerpt = _hostile_excerpt(text)

        try:
            days_past_due = int(case.get("days_past_due") or AAA_CADENCE_DAY)
        except (TypeError, ValueError):
            days_past_due = AAA_CADENCE_DAY

        prompt = (
            f"Case `{case_id}`: outstanding USD ${outstanding:,.2f}, {days_past_due} days "
            f"past due, buyer in {buyer_state}. The filing fee estimate is ${filing_fee:,}.\n\n"
            f"Verbatim hostile excerpt from the call transcript:\n\"{excerpt}\""
        )

        try:
            strategy, usage = await complete_async(
                STRATEGY_PROMPT, prompt, agent_role=self.role, max_tokens=MAX_STRATEGY_TOKENS
            )
            add_cost(case_id, usage)
        except Exception as error:
            audit(case_id, self.role, "strategy_llm_error", {"error": str(error)})
            strategy = fallback_strategy(outstanding, filing_fee, buyer_state)

        audit(
            case_id,
            self.role,
            "strategy_drafted",
            {
                "outstanding_balance_usd": outstanding,
                "filing_fee": filing_fee,
                "buyer_state": buyer_state,
                "hostile_excerpt": excerpt[:120],
            },
        )
        self._persist(case_id, current_stage="aaa_strategy_review", aaa_strategy=strategy)

        return (
            f"@concierge event=approval_request request_kind=aaa_strategy_recommendation "
            f"for case `{case_id}`.\n"
            f"[AAA Specialist] The voice call flagged a legal threat. Strategy drafted with a "
            f"filing fee estimate of ${filing_fee:,} on ${outstanding:,.2f} outstanding, buyer "
            f"in {buyer_state}.\n\n"
            f"**Arbitration strategy — case `{case_id}`**\n\n{strategy}"
        )

    def _handle_cadence_day(self, text: str) -> str | None:
        case_id = extract_case_id(text)
        day_match = DAY_OFFSET_RE.search(text)
        if case_id == "UNKNOWN" or not day_match:
            return None

        day_offset = int(day_match.group(1))
        if day_offset < AAA_CADENCE_DAY:
            audit(
                case_id,
                self.role,
                "cadence_misroute",
                {"day_offset": day_offset, "reason": "earlier_days_belong_to_diplomat"},
            )
            return None

        stage_match = CADENCE_STAGE_RE.search(text)
        stage = stage_match.group(1) if stage_match else "aaa_demand_letter"
        return self._draft_demand_letter(case_id, day_offset, stage)

    def _draft_demand_letter(self, case_id: str, day_offset: int, stage: str) -> str:
        case = _load_case(case_id)
        subject, letter, context = render_demand_letter(case)

        audit(
            case_id,
            self.role,
            "demand_letter_drafted",
            {
                "day_offset": day_offset,
                "cadence_stage": stage,
                "invoice_no": context.invoice_no,
                "outstanding_balance_usd": context.outstanding_usd,
                "filing_fee": context.filing_fee,
                "cure_deadline": context.cure_deadline,
                "body_length": len(letter),
            },
        )
        self._persist(
            case_id,
            current_stage="aaa_demand_letter_review",
            demand_letter_subject=subject,
            demand_letter_body=letter,
            cadence_day=day_offset,
        )

        return (
            f"@concierge event=approval_request request_kind=aaa_demand_letter for case "
            f"`{case_id}`. The day-{day_offset} demand letter is drafted with a "
            f"{CURE_WINDOW_DAYS}-day cure window closing {context.cure_deadline} and a filing "
            f"fee estimate of ${context.filing_fee:,}.\n\n```\n{slack_preview(letter)}\n```"
        )

    def _dispatch_letter(self, case_id: str) -> str:
        case = _load_case(case_id)
        outstanding = outstanding_amount(case)
        filing_fee = filing_fee_estimate(outstanding)

        audit(
            case_id,
            self.role,
            "operator_approval_forwarded",
            {"outstanding_balance_usd": outstanding, "filing_fee": filing_fee},
        )

        try:
            result = send_demand_letter(case)
        except OutboundDispatchError as error:
            return (
                f"[AAA Specialist] Acknowledged the approval on case `{case_id}`, but the "
                f"demand letter could not be sent: {error}\n"
                f"The mail backend credentials need attention before retrying."
            )

        return (
            f"[AAA Specialist] Demand letter sent for case `{case_id}`.\n"
            f"Outstanding ${outstanding:,.2f}, filing fee estimate ${filing_fee:,}.\n"
            f"Delivered via {result.backend} to {result.to} (message id {result.message_id}).\n"
            f"The {CURE_WINDOW_DAYS}-day cure window starts now.\n"
            f"@concierge the HITL card can be sealed. event=card_decided case_id=`{case_id}`"
        )

    def _orientation(self, text: str) -> str:
        case_id = extract_case_id(text)
        amount_match = AMOUNT_IN_TEXT_RE.search(text)
        outstanding = float(amount_match.group(1).replace(",", "")) if amount_match else 0.0

        audit(
            case_id,
            self.role,
            "orientation_delivered",
            {"outstanding_balance_usd": outstanding, "filing_fee": filing_fee_estimate(outstanding)},
        )
        return render_orientation(case_id, outstanding)

    def _persist(self, case_id: str, **patch: Any) -> None:
        try:
            update_case_state(case_id, **patch)
        except Exception as error:
            audit(case_id, self.role, "state_persist_failed", {"error": str(error)})


if __name__ == "__main__":
    sys.exit(run_agent(AAASpecialistAdapter))
