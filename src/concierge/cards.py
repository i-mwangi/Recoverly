from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final

from src.agents._utils import safe_float
from src.concierge import slack_client
from src.config import settings
from src.utils.text import slack_preview

log = logging.getLogger("recoverly.concierge.cards")

DEFAULT_KIND: Final = "default"
SUMMARY_LIMIT: Final = 160


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    text: str
    action_id: str
    style: str | None = None
    value_template: str = "{case_id}"

    def render(self, case_id: str) -> dict[str, Any]:
        return slack_client.button(
            self.text,
            self.action_id.format(case_id=case_id),
            self.value_template.format(case_id=case_id),
            self.style,
        )


@dataclass(frozen=True, slots=True)
class CardSpec:
    badge: str
    buttons: tuple[ButtonSpec, ...] = ()
    informational: bool = False
    alerts_channel: bool = False
    block_id_prefix: str = ""

    def actions_block(self, case_id: str) -> dict[str, Any] | None:
        if self.informational or not self.buttons:
            return None
        block = slack_client.actions_block(*(button.render(case_id) for button in self.buttons))
        if self.block_id_prefix:
            block["block_id"] = f"{self.block_id_prefix}_{case_id}"
        return block


APPROVE_REJECT_REVISE: Final = (
    ButtonSpec("Approve", "hitl_approve", "primary"),
    ButtonSpec("Reject", "hitl_reject", "danger"),
    ButtonSpec("Revise", "hitl_revise"),
)

AAA_LETTER_VALUE: Final = "case={case_id}&request_id={case_id}"

CARD_SPECS: Final[dict[str, CardSpec]] = {
    DEFAULT_KIND: CardSpec("HITL approval", APPROVE_REJECT_REVISE),
    "ai_attested": CardSpec("AI-attested draft", APPROVE_REJECT_REVISE),
    "ai_revised": CardSpec("AI-revised draft (not human-attested)", APPROVE_REJECT_REVISE),
    "tone_block": CardSpec("Tone Coach blocked this revision", APPROVE_REJECT_REVISE),
    "demand_letter": CardSpec("Day 60+ demand letter", APPROVE_REJECT_REVISE),
    "attorney_recommended": CardSpec(
        "Attorney recommended (above sweet spot)",
        (
            ButtonSpec("Run full flow and tag for attorney", "attorney_run_{case_id}", "primary"),
            ButtonSpec("Skip and get referrals", "attorney_skip_{case_id}"),
        ),
        block_id_prefix="attorney_decision",
    ),
    "lite": CardSpec(
        "Lite mode (below sweet spot)",
        (
            ButtonSpec("Send lite final notice", "lite_run_{case_id}", "primary"),
            ButtonSpec("Skip and write off", "lite_writeoff_{case_id}"),
        ),
        block_id_prefix="lite_decision",
    ),
    "payment_received": CardSpec(
        "Payment received",
        (
            ButtonSpec("Confirm settled and close", "payment_settled_confirm", "primary"),
            ButtonSpec("Reopen, not settled", "payment_reopen_case", "danger"),
        ),
        block_id_prefix="payment_received",
    ),
    "payment_intent": CardSpec("Payment intent", APPROVE_REJECT_REVISE),
    "voice_dial": CardSpec(
        "Voice Agent call review",
        (
            ButtonSpec("Approve call", "voice_dial_approve", "primary"),
            ButtonSpec("Revise script", "revise_voice_dial_"),
            ButtonSpec("Reject", "voice_dial_reject", "danger"),
        ),
        block_id_prefix="voice_dial",
    ),
    "voice_aaa_escalation": CardSpec(
        "Voice call flagged for arbitration escalation",
        (
            ButtonSpec("Escalate to arbitration", "voice_aaa_escalate_approve", "primary"),
            ButtonSpec("Stand down", "voice_aaa_stand_down", "danger"),
            ButtonSpec("Try voice once more", "voice_try_once_more"),
        ),
        block_id_prefix="voice_aaa_escalation",
    ),
    "aaa_strategy_recommendation": CardSpec(
        "Arbitration strategy recommendation",
        (
            ButtonSpec("Draft the demand letter now", "aaa_draft_demand_letter_now", "primary"),
            ButtonSpec("Hold for legal review", "aaa_hold_for_legal_review"),
        ),
        block_id_prefix="aaa_strategy",
    ),
    "aaa_demand_letter": CardSpec(
        "Arbitration demand letter draft",
        (
            ButtonSpec("Approve and send", "approve_aaa_letter_{case_id}", "primary", AAA_LETTER_VALUE),
            ButtonSpec("Revise", "revise_aaa_letter_{case_id}", None, AAA_LETTER_VALUE),
            ButtonSpec("Reject", "reject_aaa_letter_{case_id}", "danger", AAA_LETTER_VALUE),
        ),
        block_id_prefix="aaa_demand_letter",
    ),
    "investigation_summary": CardSpec("Investigation summary", informational=True),
    "voice_call_summary": CardSpec("Voice call complete", informational=True),
    "voice_commitment_summary": CardSpec(
        "Voice call complete, payment commitment logged", informational=True
    ),
    "voice_no_answer": CardSpec("Voice call went unanswered", informational=True),
    "voice_failed": CardSpec("Voice call failed", informational=True),
    "voice_deferred": CardSpec("Voice call deferred outside the calling window", informational=True),
    "voice_in_progress": CardSpec("Voice call placed", informational=True),
    "welfare_critical": CardSpec(
        "Welfare concern, operator paged", informational=True, alerts_channel=True
    ),
    "anomaly_welfare": CardSpec("Anomaly: welfare concern", informational=True, alerts_channel=True),
    "anomaly_bankruptcy": CardSpec("Anomaly: bankruptcy notice", APPROVE_REJECT_REVISE),
    "anomaly_wrong_person": CardSpec("Anomaly: wrong contact", APPROVE_REJECT_REVISE),
    "anomaly_cease_and_desist": CardSpec("Anomaly: cease and desist", APPROVE_REJECT_REVISE),
    "preflight_need_contract": CardSpec("Waiting for the contract upload", informational=True),
    "preflight_need_invoice": CardSpec("Waiting for the invoice upload", informational=True),
    "preflight_duplicate": CardSpec("Duplicate upload ignored", informational=True),
    "preflight_unrecognized": CardSpec("Upload not recognised", informational=True),
}


def spec_for(card_kind: str) -> CardSpec:
    return CARD_SPECS.get(card_kind, CARD_SPECS[DEFAULT_KIND])


def build_context_line(case_meta: dict[str, Any]) -> str:
    parts: list[str] = []

    if "outstanding_balance_usd" in case_meta:
        outstanding = safe_float(case_meta["outstanding_balance_usd"], 0.0)
        if "amount_usd" in case_meta:
            gross = safe_float(case_meta["amount_usd"], 0.0)
            parts.append(f"*Outstanding*: ${outstanding:,.2f} (gross ${gross:,.2f})")
        else:
            parts.append(f"*Outstanding*: ${outstanding:,.2f}")
    elif "amount_usd" in case_meta:
        parts.append(f"*Gross*: ${safe_float(case_meta['amount_usd'], 0.0):,.2f}")

    if "days_past_due" in case_meta:
        parts.append(f"*Past due*: {case_meta['days_past_due']} days")
    if "customer_name" in case_meta:
        parts.append(f"*Customer*: {case_meta['customer_name']}")
    if "est_cost_usd" in case_meta:
        parts.append(f"*Cost so far*: ${safe_float(case_meta['est_cost_usd'], 0.0):.4f}")

    return " · ".join(parts)


def build_blocks(
    case_id: str, summary: str, body: str, card_kind: str, case_meta: dict[str, Any]
) -> list[dict[str, Any]]:
    spec = spec_for(card_kind)
    blocks: list[dict[str, Any]] = []

    if spec.alerts_channel:
        blocks.append(
            slack_client.section_block(
                "<!here> *Welfare concern* — this case needs an operator now."
            )
        )

    blocks.append(slack_client.header_block(f"{spec.badge} — case {case_id}"))

    context = build_context_line(case_meta)
    if context:
        blocks.append(slack_client.context_block(context))

    blocks.append(slack_client.section_block(f"*Summary*: {summary}"))
    blocks.append(slack_client.divider_block())
    blocks.append(slack_client.section_block(slack_preview(body)))

    actions = spec.actions_block(case_id)
    if actions:
        blocks.append(actions)

    return blocks


def fallback_text(case_id: str, summary: str, card_kind: str) -> str:
    if spec_for(card_kind).alerts_channel:
        return f"<!here> Welfare concern on case {case_id}: an operator is needed now."
    return f"HITL: {summary}"


def post_card(
    case_id: str,
    summary: str,
    body: str,
    *,
    card_kind: str = DEFAULT_KIND,
    case_meta: dict[str, Any] | None = None,
    channel: str | None = None,
) -> slack_client.SlackResult:
    meta = case_meta or {}
    return slack_client.post_message(
        fallback_text(case_id, summary, card_kind),
        channel=channel,
        blocks=build_blocks(case_id, summary, body, card_kind, meta),
    )


def estimate_case_cost(case_id: str) -> float | None:
    path = settings.data.audit_trail_jsonl
    if not path.exists():
        return None

    total = 0.0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("case_id") != case_id:
                continue
            usage = (event.get("payload") or {}).get("usage")
            if isinstance(usage, dict):
                total += safe_float(usage.get("cost_usd"), 0.0)

    return round(total, 6) if total > 0 else None
