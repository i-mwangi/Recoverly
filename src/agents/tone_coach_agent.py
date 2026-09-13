from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Final

from src.agents._case_state import add_cost, load_case_state, update_case_state
from src.agents._utils import is_error_envelope, parse_json_object
from src.agents.base import BandAgentAdapter, audit, extract_case_id, run_agent
from src.concierge import slack_client
from src.diplomat.templates import build_context, substitute_placeholders
from src.llm.provider import complete_async
from src.utils.text import extract_code_block, slack_preview

MIN_AUDITABLE_LENGTH: Final = 20
MAX_AUDIT_TOKENS: Final = 600
MAX_REVISE_ROUNDS: Final = 0
OPERATOR_NOTE_LIMIT: Final = 500
DRAFT_PREVIEW_LIMIT: Final = 1_800

HOSTILE_KEYWORDS_RE: Final = re.compile(
    r"\b(kill|killing|killed|die|died|dying|murder|destroy|punish|fuck|fucking|"
    r"damn|hate|sue|sued|lawsuit|criminal|prosecute|idiot|stupid|incompetent|"
    r"ridiculous|threat|threaten|threatening)\b",
    re.IGNORECASE,
)

REVISE_NOTE_EVENT_RE: Final = re.compile(r"event\s*[=:]\s*revise_note_review", re.IGNORECASE)
OPERATOR_NOTE_RE: Final = re.compile(
    r"operator_note:\s*\"?(.+?)\"?\s*(?:\n[a-z_]+\s*[=:]|$)", re.DOTALL | re.IGNORECASE
)
ORIGINAL_BODY_RE: Final = re.compile(r"original_body:\s*```(.+?)```", re.DOTALL | re.IGNORECASE)
DRAFT_SUBJECT_RE: Final = re.compile(r"draft_subject:\s*(.+)")
REVISE_ROUND_RE: Final = re.compile(r"revise_round=(\d+)")

CONCIERGE_ECHO_MARKERS: Final = ("Concierge posted", "Concierge could not post")

SYSTEM_PROMPT: Final = (
    "You are the Tone Coach agent for Recoverly. You audit outgoing English business "
    "communications for native fluency, professional tone and zero translation feel. "
    "Your entire response must be a single JSON object with no prose and no preamble. "
    "Start with { and end with }. "
    'Schema: {"verdict":"approve|revise","issues":["..."],"revised":"<full revised text or empty>"}. '
    'If the verdict is approve, issues must be [] and revised must be "". '
    "If the verdict is revise, list one to three specific issues and provide the full "
    "revised text."
)


@dataclass(frozen=True, slots=True)
class ToneVerdict:
    verdict: str
    issues: tuple[str, ...]
    revised: str

    @property
    def approved(self) -> bool:
        return self.verdict == "approve"

    def issues_markdown(self) -> str:
        return "\n".join(f"  - {issue}" for issue in self.issues) or "  - (none listed)"


def parse_verdict(raw: str) -> ToneVerdict | None:
    parsed = parse_json_object(raw)
    if not parsed:
        return None

    verdict = str(parsed.get("verdict", "revise")).lower()
    if verdict not in {"approve", "revise"}:
        verdict = "revise"

    issues = parsed.get("issues")
    issue_tuple = tuple(str(issue) for issue in issues) if isinstance(issues, list) else ()

    return ToneVerdict(
        verdict=verdict, issues=issue_tuple, revised=str(parsed.get("revised") or "").strip()
    )


def _match_group(pattern: re.Pattern[str], text: str, limit: int | None = None) -> str:
    match = pattern.search(text)
    if not match:
        return ""
    value = match.group(1).strip()
    return value[:limit] if limit else value


def build_override_blocks(
    case_id: str, subject: str, body: str, matched_keyword: str, operator_note: str
) -> list[dict[str, Any]]:
    context = f"*Verdict*: revise · *Reason*: hostile keyword pre-filter"
    detail = f"Detected hostile keyword `{matched_keyword}` before any model call."
    if operator_note:
        detail += f"\n*Operator note*: {operator_note[:200]}"

    return [
        slack_client.header_block(f"Tone Coach override available — case {case_id}"),
        slack_client.context_block(context),
        slack_client.section_block(detail),
        slack_client.divider_block(),
        slack_client.section_block(
            f"*Original draft (not yet approved by Tone Coach)*\n"
            f"*Subject*: {subject}\n```\n{body[:DRAFT_PREVIEW_LIMIT]}\n```"
        ),
        slack_client.divider_block(),
        slack_client.actions_block(
            slack_client.button(
                "Force approve original", "tone_coach_override_force_approve", case_id, "primary"
            ),
            slack_client.button("Reject case", "tone_coach_override_reject", case_id, "danger"),
            slack_client.button("Revise", "tone_coach_override_revise", case_id),
        ),
    ]


class ToneCoachAdapter(BandAgentAdapter):
    role = "tone_coach"

    async def handle_message(self, text, msg, tools, history, room_id):
        if is_error_envelope(text):
            return None
        if any(marker in text for marker in CONCIERGE_ECHO_MARKERS):
            return None

        if REVISE_NOTE_EVENT_RE.search(text):
            return self._review_revise_note(text)

        case_id = extract_case_id(text)
        target = extract_code_block(text)
        if len(target) < MIN_AUDITABLE_LENGTH:
            return None

        blocked = self._check_hostile(case_id, text, target)
        if blocked:
            return blocked

        audit(case_id, self.role, "tone_coach_start", {"target_length": len(target)})
        return await self._audit_draft(case_id, text, target)

    def _review_revise_note(self, text: str) -> str:
        case_id = extract_case_id(text)
        note = _match_group(OPERATOR_NOTE_RE, text, OPERATOR_NOTE_LIMIT)
        original_body = _match_group(ORIGINAL_BODY_RE, text)

        hostile = HOSTILE_KEYWORDS_RE.search(note)
        if not hostile:
            audit(case_id, self.role, "tone_coach_revise_note_clean", {"note_excerpt": note[:200]})
            return (
                f"[Tone Coach] revise note on case `{case_id}` is clean — "
                f"no hostile language detected. Diplomat may proceed with the redraft."
            )

        matched = hostile.group(0)
        audit(
            case_id,
            self.role,
            "tone_coach_revise_note_blocked",
            {"matched_keyword": matched, "note_excerpt": note[:200]},
        )
        return (
            f"@concierge event=approval_request for case `{case_id}` card_kind=tone_block\n\n"
            f"**Tone Coach blocked this revision** — hostile keyword `{matched}` in the operator note.\n\n"
            f"*Operator note*:\n>>> {note}\n\n"
            f"*Rationale*: Recoverly runs B2B collections under voluntary FDCPA-style conduct "
            f"rules. Threatening, profane or abusive language toward the buyer violates tone "
            f"policy. The operator must submit a non-hostile revision or approve the original "
            f"draft as written.\n\n"
            f"*Original attested draft (re-surfacing for approval)*:\n"
            f"```\n{original_body[:DRAFT_PREVIEW_LIMIT]}\n```\n\n"
            f"Please post the block card plus a fresh approve/revise/reject card."
        )

    def _check_hostile(self, case_id: str, text: str, target: str) -> str | None:
        match = HOSTILE_KEYWORDS_RE.search(f"{text}\n{target}")
        if not match:
            return None

        matched = match.group(0)
        operator_note = _match_group(OPERATOR_NOTE_RE, text, OPERATOR_NOTE_LIMIT)
        subject = _match_group(DRAFT_SUBJECT_RE, text) or f"Case {case_id} draft"

        audit(
            case_id,
            self.role,
            "tone_coach_prefilter_blocked",
            {"matched_keyword": matched, "operator_note_length": len(operator_note)},
        )

        result = slack_client.post_message(
            f"Tone Coach override — case {case_id}",
            blocks=build_override_blocks(case_id, subject, target, matched, operator_note),
        )
        audit(case_id, self.role, "tone_coach_override_card_posted", result.as_dict())

        return (
            f"[Tone Coach] case `{case_id}` blocked — hostile keyword `{matched}` detected. "
            f"An override card was posted to Slack so the operator can force-approve or revise."
        )

    async def _audit_draft(self, case_id: str, text: str, target: str) -> str:
        prompt = (
            "Audit this English text for native fluency and a tone appropriate to B2B "
            f"collections:\n\n{target}"
        )
        try:
            raw, usage = await complete_async(
                SYSTEM_PROMPT, prompt, agent_role=self.role, max_tokens=MAX_AUDIT_TOKENS
            )
        except Exception as error:
            audit(case_id, self.role, "llm_error", {"error": str(error)})
            return f"@concierge Tone Coach could not audit case `{case_id}`: {error}"

        add_cost(case_id, usage)

        verdict = parse_verdict(raw)
        if verdict is None:
            audit(case_id, self.role, "tone_coach_parse_failed", {"raw": raw[:200]})
            return (
                f"@concierge Tone Coach could not parse its own verdict for case `{case_id}`. "
                f"Raw response: `{slack_preview(raw)}`"
            )

        audit(
            case_id,
            self.role,
            "tone_coach_done",
            {
                "verdict": verdict.verdict,
                "issues": list(verdict.issues),
                "usage": usage,
            },
        )

        final_body = target if verdict.approved else (verdict.revised or target)
        final_body = self._render_for_buyer(case_id, final_body)
        self._persist_body(case_id, final_body)

        if verdict.approved:
            return (
                f"@concierge Tone Coach **approved** the case `{case_id}` draft — ready for the "
                f"HITL card. card_kind=ai_attested\n"
                f"Final text:\n```\n{slack_preview(final_body)}\n```"
            )

        round_number = int(_match_group(REVISE_ROUND_RE, text) or 0)
        if round_number < MAX_REVISE_ROUNDS:
            return (
                f"@diplomat Tone Coach asks for a **revision** of case `{case_id}` "
                f"(revise_round={round_number + 1}).\n"
                f"Issues:\n{verdict.issues_markdown()}\n\n"
                f"Suggested revision:\n```\n{slack_preview(final_body)}\n```\n"
                f"Please re-attest with this revision or send your own polished version."
            )

        return (
            f"@concierge Tone Coach **revised** the case `{case_id}` draft. "
            f"card_kind=ai_revised (not human-attested)\n"
            f"Issues:\n{verdict.issues_markdown()}\n\n"
            f"Revised:\n```\n{slack_preview(final_body)}\n```"
        )

    def _render_for_buyer(self, case_id: str, body: str) -> str:
        try:
            return substitute_placeholders(body, build_context(load_case_state(case_id)))
        except Exception as error:
            audit(case_id, self.role, "tone_coach_placeholder_cleanup_failed", {"error": str(error)})
            return body

    def _persist_body(self, case_id: str, body: str) -> None:
        if not body:
            return
        try:
            update_case_state(case_id, revised_email_body=body)
        except Exception as error:
            audit(case_id, self.role, "tone_coach_persist_failed", {"error": str(error)})


if __name__ == "__main__":
    sys.exit(run_agent(ToneCoachAdapter))
