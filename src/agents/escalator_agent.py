from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any, Final

from src.agents._case_state import add_cost, is_halted, update_case_state
from src.agents._utils import is_error_envelope, parse_json_object, safe_float
from src.agents.base import BandAgentAdapter, audit, extract_case_id, run_agent
from src.llm.provider import complete_async
from src.utils.text import slack_preview

HARD_ESCALATION_DAY: Final = 60
DYNAMIC_DISCOVERY_DAY: Final = 65
MAX_LETTER_TOKENS: Final = 500
PEER_PAGE_SIZE: Final = 50
ARBITRATION_CAPABILITIES: Final = ("arbitration", "aaa")

DEFAULT_LATE_FEE_RATE: Final = "1.5% per month"
DEFAULT_FORUM: Final = "AAA Commercial Arbitration Rules"

SYSTEM_PROMPT: Final = (
    "You are the Escalator agent for Recoverly, handling day 60 and later escalation. "
    "Draft a formal pre-arbitration demand letter to the accounts payable contact. "
    "The tone is firm and professional. Cite the unpaid principal, the late fees accrued "
    "under the contract's late fee clause, the forum or arbitration clause, and a seven-day "
    "cure window. Output plain text email body only: no JSON and no markdown fences. "
    "Maximum 200 words."
)


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    found: bool
    detail: str
    peer: dict[str, Any] | None = None

    @property
    def peer_handle(self) -> str:
        peer = self.peer or {}
        return str(peer.get("handle") or peer.get("name") or "aaa-specialist")

    @property
    def peer_id(self) -> str:
        peer = self.peer or {}
        return str(peer.get("id") or peer.get("agent_id") or "")


def advertises_capability(peer: dict[str, Any], capability: str) -> bool:
    haystack = " ".join(
        str(peer.get(field, ""))
        for field in ("description", "capabilities", "tags", "name", "handle")
    ).lower()
    return capability.lower() in haystack


def normalize_records(result: Any) -> list[dict[str, Any]]:
    if hasattr(result, "data"):
        items = result.data or []
    elif isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        items = result.get("data") or result.get("peers") or result.get("participants") or []
    else:
        items = []
    return [item if isinstance(item, dict) else getattr(item, "__dict__", {}) for item in items]


def pick_arbitration_peer(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        if any(advertises_capability(record, cap) for cap in ARBITRATION_CAPABILITIES):
            return record
    return None


async def discover_arbitration_peer(tools: Any, log: Any) -> DiscoveryOutcome:
    try:
        in_room = pick_arbitration_peer(normalize_records(await tools.get_participants()))
    except Exception as error:
        log.warning("get_participants failed: %s", error)
        in_room = None

    if in_room:
        handle = in_room.get("handle") or in_room.get("name") or "aaa-specialist"
        log.info("arbitration specialist %s already in room", handle)
        return DiscoveryOutcome(True, f"already in room as @{handle}", in_room)

    try:
        peers = normalize_records(await tools.lookup_peers(page=1, page_size=PEER_PAGE_SIZE))
    except Exception as error:
        log.warning("lookup_peers failed: %s", error)
        return DiscoveryOutcome(False, f"peer lookup failed: {error}")

    pick = pick_arbitration_peer(peers)
    if not pick:
        return DiscoveryOutcome(
            False, f"no arbitration capability among {len(peers)} discoverable peers"
        )

    outcome = DiscoveryOutcome(True, "", pick)
    try:
        await tools.add_participant(outcome.peer_id)
    except Exception as error:
        log.warning("add_participant failed, will still mention the peer: %s", error)
        return DiscoveryOutcome(
            True, f"discovered @{outcome.peer_handle} but could not add them: {error}", pick
        )

    log.info("added arbitration specialist %s to room", outcome.peer_handle)
    return DiscoveryOutcome(True, f"discovered and added @{outcome.peer_handle}", pick)


def resolve_outstanding(case: dict[str, Any]) -> tuple[float, float, float]:
    gross = safe_float(case.get("amount_usd"), 0.0)
    deposit_pct = safe_float(case.get("deposit_pct"), 0.0)
    outstanding = safe_float(case.get("outstanding_balance_usd"), 0.0)
    if outstanding <= 0 and gross > 0:
        outstanding = round(gross * (1.0 - deposit_pct), 2)
    return outstanding, gross, deposit_pct


def _clause(case: dict[str, Any], key: str, default: str) -> str:
    clauses = case.get("clauses")
    if isinstance(clauses, dict) and clauses.get(key):
        return str(clauses[key])
    return default


def build_letter_prompt(case: dict[str, Any], case_id: str, days_past_due: int) -> str:
    outstanding, gross, deposit_pct = resolve_outstanding(case)
    pattern_tag = case.get("pattern_tag") or "unknown"

    prompt = (
        f"Case {case_id}: ${outstanding:,.2f} outstanding "
        f"(gross invoice ${gross:,.2f}, deposit {deposit_pct:.0%} paid at order). "
        f"Days past due: {days_past_due}. "
        f"Late fee clause: {_clause(case, 'late_fee_rate', DEFAULT_LATE_FEE_RATE)}. "
        f"Forum clause: {_clause(case, 'forum_clause', DEFAULT_FORUM)}. "
        f"Accounts payable contact: {case.get('customer_ap_contact', 'AP team')} "
        f"({case.get('customer_ap_email', '')}). "
        f"Customer payment pattern: {pattern_tag}. "
        f"Draft the demand letter with a seven-day cure window. "
        f"The demand must cite the outstanding balance, never the gross invoice."
    )

    if case.get("attorney_tag"):
        prompt += (
            " This case is flagged for attorney consultation because the outstanding balance "
            "exceeds the sweet-spot ceiling. Include an explicit paragraph recommending that "
            "the buyer engage cross-border collections counsel within the cure window, and note "
            "that arbitration filing plus ancillary litigation costs make voluntary settlement "
            f"materially cheaper. Reference the buyer's risk classification of {pattern_tag}."
        )

    return prompt


def _parse_case(text: str) -> dict[str, Any] | None:
    case = parse_json_object(text)
    if case and case.get("case_id"):
        return case
    case_id = extract_case_id(text)
    return {"case_id": case_id} if case_id != "UNKNOWN" else None


class EscalatorAdapter(BandAgentAdapter):
    role = "escalator"

    async def handle_message(self, text, msg, tools, history, room_id):
        if is_error_envelope(text):
            return None

        case = _parse_case(text)
        if not case:
            return None

        case_id = str(case["case_id"])
        if is_halted(case_id):
            self.log.info("case %s is halted, skipping", case_id)
            return None

        try:
            days_past_due = int(case.get("days_past_due") or 0)
        except (TypeError, ValueError):
            days_past_due = 0

        if 0 < days_past_due < HARD_ESCALATION_DAY:
            self.log.info("case %s is only %d days past due, too early", case_id, days_past_due)
            return None

        outstanding, gross, deposit_pct = resolve_outstanding(case)
        audit(
            case_id,
            self.role,
            "escalator_start",
            {
                "days_past_due": days_past_due,
                "gross_amount_usd": gross,
                "deposit_pct": deposit_pct,
                "outstanding_balance_usd": outstanding,
            },
        )

        discovery_note = ""
        if days_past_due >= DYNAMIC_DISCOVERY_DAY:
            discovery_note = await self._run_discovery(case_id, tools)

        letter, usage = await self._draft_letter(case, case_id, days_past_due)
        if usage:
            add_cost(case_id, usage)

        audit(
            case_id,
            self.role,
            "demand_letter_drafted",
            {"letter_length": len(letter), "usage": usage},
        )
        update_case_state(
            case_id,
            current_stage="aaa_demand_letter_review",
            demand_letter_subject=f"Formal Pre-Arbitration Demand — Outstanding Balance {case_id}",
            demand_letter_body=letter,
        )

        return (
            f"@concierge Escalator day-{days_past_due} **demand letter draft** for case "
            f"`{case_id}`:{discovery_note}\n\n```\n{slack_preview(letter)}\n```\n"
            f"card_kind=aaa_demand_letter"
        )

    async def _run_discovery(self, case_id: str, tools: Any) -> str:
        outcome = await discover_arbitration_peer(tools, self.log)
        audit(
            case_id,
            self.role,
            "arbitration_discovery",
            {"found": outcome.found, "detail": outcome.detail, "peer_id": outcome.peer_id},
        )

        if outcome.found:
            return (
                f"\n\nDay {DYNAMIC_DISCOVERY_DAY} discovery: @{outcome.peer_handle} is on this "
                f"case with capability=arbitration ({outcome.detail}). They receive a case brief "
                f"separately."
            )
        return (
            f"\n\nDay {DYNAMIC_DISCOVERY_DAY} discovery did not find an arbitration specialist "
            f"({outcome.detail}). Will retry on the next cadence tick or fall back to manual "
            f"outreach."
        )

    async def _draft_letter(
        self, case: dict[str, Any], case_id: str, days_past_due: int
    ) -> tuple[str, dict[str, Any] | None]:
        prompt = build_letter_prompt(case, case_id, days_past_due)
        try:
            return await complete_async(
                SYSTEM_PROMPT, prompt, agent_role=self.role, max_tokens=MAX_LETTER_TOKENS
            )
        except Exception as error:
            audit(case_id, self.role, "llm_error", {"error": str(error)})
            return (
                f"[demand letter generation failed: {error}. "
                f"Fall back to the jurisdiction template under data/rag-sources/demand_letters/.]",
                None,
            )


if __name__ == "__main__":
    sys.exit(run_agent(EscalatorAdapter))
