from __future__ import annotations

import re
import sys
import time
from typing import Any, Final

from src.agents._case_lock import release as release_case_lock
from src.agents._utils import parse_json_object
from src.agents.base import BandAgentAdapter, audit, run_agent
from src.concierge.cards import DEFAULT_KIND, estimate_case_cost, post_card

DEDUP_WINDOW_SECONDS: Final = 60
DEDUP_MAX_ENTRIES: Final = 256
SUMMARY_LIMIT: Final = 160

CASE_META_KEYS: Final[tuple[str, ...]] = (
    "amount_usd",
    "deposit_pct",
    "outstanding_balance_usd",
    "days_past_due",
    "customer_name",
    "customer_state",
)

OPERATOR_FORWARD_RE: Final = re.compile(
    r"@[\w/.-]+\s+operator\s+(?:approved|rejected|revised|cancelled|accepted|overrode|"
    r"requested\s+\w+|skipped\w*)\s+(?:\w+\s+)?(?:for\s+)?case\s+`(?P<case_id>[^`]+)`",
    re.IGNORECASE,
)


def _event_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf'"event"\s*:\s*"{name}"|event\s*[=:]\s*{name}', re.IGNORECASE)


OPERATOR_ACTION_EVENT_RE: Final = _event_pattern("operator_action")
APPROVAL_REQUEST_EVENT_RE: Final = _event_pattern("approval_request")
CARD_DECIDED_EVENT_RE: Final = _event_pattern("card_decided")
INVOICE_UPLOADED_EVENT_RE: Final = _event_pattern("invoice_uploaded_slack")

CASE_ID_BACKTICK_RE: Final = re.compile(r"case\s+`([^`]+)`", re.IGNORECASE)
CASE_ID_JSON_RE: Final = re.compile(r'"case_id"\s*:\s*"([^"]+)"')
CASE_ID_BARE_RE: Final = re.compile(r"\b(RC-\d{4}-\d{4,})\b")
CARD_KIND_RE: Final = re.compile(r"""card_kind["']?\s*[:=]\s*["']?(\w+)""")
REQUEST_KIND_RE: Final = re.compile(r"""request_kind["']?\s*[:=]\s*["']?(\w+)""")
LEADING_MENTION_RE: Final = re.compile(r"@(\S+)")

OPERATOR_VERBS: Final[tuple[tuple[str, str], ...]] = (
    ("approved", "approved"),
    ("reject", "rejected"),
    ("revis", "revision requested"),
    ("cancel", "cancelled"),
)


def extract_case_id(text: str) -> str:
    for pattern in (CASE_ID_BACKTICK_RE, CASE_ID_JSON_RE, CASE_ID_BARE_RE):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return "UNKNOWN"


def classify_operator_verb(text: str) -> str:
    lowered = text.lower()
    for needle, verb in OPERATOR_VERBS:
        if needle in lowered:
            return verb
    return "decided"


def target_agent_from(text: str) -> str:
    match = LEADING_MENTION_RE.search(text.strip())
    if not match:
        return "the downstream agent"
    handle = match.group(1)
    if "/" in handle:
        handle = handle.split("/", 1)[1]
    return handle.removeprefix("recoverly-").replace("-", "_")


def extract_card_kind(text: str) -> str:
    match = CARD_KIND_RE.search(text) or REQUEST_KIND_RE.search(text)
    return match.group(1) if match else DEFAULT_KIND


def extract_case_meta(text: str) -> dict[str, Any]:
    embedded = parse_json_object(text)
    if not isinstance(embedded, dict):
        return {}
    return {key: embedded[key] for key in CASE_META_KEYS if key in embedded}


def build_summary(text: str) -> tuple[str, str]:
    cleaned = re.sub(r"@\S+\s*", "", text).strip()
    return cleaned.split("\n", 1)[0][:SUMMARY_LIMIT], cleaned


class ConciergeAdapter(BandAgentAdapter):
    role = "concierge"

    def __init__(self) -> None:
        super().__init__()
        self._recent_posts: dict[str, float] = {}

    async def handle_message(self, text, msg, tools, history, room_id):
        is_invoice_event = bool(INVOICE_UPLOADED_EVENT_RE.search(text))

        forwarded = OPERATOR_FORWARD_RE.search(text)
        if forwarded and not is_invoice_event:
            return self._narrate_operator_action(text, forwarded.group("case_id"), "forwarded")

        if OPERATOR_ACTION_EVENT_RE.search(text):
            return self._narrate_operator_action(text, extract_case_id(text), "band_event")

        if CARD_DECIDED_EVENT_RE.search(text):
            case_id = extract_case_id(text)
            audit(case_id, self.role, "card_decided_ack", {"excerpt": text[:200]})
            return (
                f"[Concierge] Card decided for case `{case_id}`. Sealing the card and recording "
                f"the outcome in the audit trail."
            )

        if is_invoice_event:
            case_id = extract_case_id(text)
            audit(case_id, self.role, "invoice_intake_routed", {"excerpt": text[:200]})
            return (
                f"[Concierge] Invoice intake received for case `{case_id}`. Routing to @preflight "
                f"for extraction and pairing."
            )

        is_approval_request = bool(APPROVAL_REQUEST_EVENT_RE.search(text))
        has_card_request = bool(CARD_KIND_RE.search(text) or REQUEST_KIND_RE.search(text))
        case_id = extract_case_id(text)
        if is_approval_request or has_card_request:
            if case_id == "UNKNOWN":
                return (
                    "[Concierge] I need a valid case ID before I can create an approval card. "
                    "No Slack card was posted."
                )
            audit(
                case_id,
                self.role,
                "approval_request_received",
                {"excerpt": text[:200]},
            )
            return self._post_approval_card(text, room_id)

        if "status" in text.lower() and "agent" in text.lower():
            return (
                "[Concierge] Online. Connected Recoverly agents are Preflight, Investigator, "
                "Diplomat, Tone Coach, Band payment, Voice agent, Escalator, and AAA specialist."
            )

        return (
            "[Concierge] I can route an invoice intake, process a case-specific approval request, "
            "or record an operator decision. Include a valid case ID for case work."
        )

    def _narrate_operator_action(self, text: str, case_id: str, source: str) -> str:
        verb = classify_operator_verb(text)
        target = target_agent_from(text)
        audit(
            case_id,
            self.role,
            "operator_action_narration",
            {"verb": verb, "target_agent": target, "source": source},
        )
        return (
            f"[Concierge] @{target} the operator {verb} case `{case_id}`. Please proceed per that "
            f"decision. The audit trail is updated."
        )

    def _is_duplicate(self, case_id: str, card_kind: str) -> bool:
        now = time.time()

        if len(self._recent_posts) > DEDUP_MAX_ENTRIES:
            cutoff = now - DEDUP_WINDOW_SECONDS * 10
            self._recent_posts = {
                key: seen for key, seen in self._recent_posts.items() if seen > cutoff
            }

        key = f"{case_id}::{card_kind}"
        last_seen = self._recent_posts.get(key, 0.0)
        if case_id != "UNKNOWN" and (now - last_seen) < DEDUP_WINDOW_SECONDS:
            self.log.info(
                "skipping duplicate card for case %s kind %s, posted %.0fs ago",
                case_id,
                card_kind,
                now - last_seen,
            )
            return True

        self._recent_posts[key] = now
        return False

    def _post_approval_card(self, text: str, room_id: str) -> str | None:
        case_id = extract_case_id(text)
        card_kind = extract_card_kind(text)

        if self._is_duplicate(case_id, card_kind):
            return None

        summary, body = build_summary(text)
        case_meta = extract_case_meta(text)

        cost = estimate_case_cost(case_id)
        if cost is not None:
            case_meta["est_cost_usd"] = cost

        audit(
            case_id,
            self.role,
            "concierge_received",
            {"summary": summary, "card_kind": card_kind, "room": room_id[:8]},
        )

        result = post_card(case_id, summary, body, card_kind=card_kind, case_meta=case_meta)
        audit(case_id, self.role, "slack_post", {"card_kind": card_kind, **result.as_dict()})

        if case_id != "UNKNOWN":
            release_case_lock(case_id)

        if result.ok:
            return (
                f"Concierge posted the `{card_kind}` card for case `{case_id}` to Slack "
                f"(ts {result.ts}). Waiting on the operator."
            )

        return (
            f"Concierge could not post the `{card_kind}` card for case `{case_id}`: "
            f"{result.error}. The audit trail still recorded the request."
        )


if __name__ == "__main__":
    sys.exit(run_agent(ConciergeAdapter))
