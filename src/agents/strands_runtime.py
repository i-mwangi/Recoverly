from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Final
from uuid import uuid4

from strands import Agent, tool
from strands.models.openai import OpenAIModel

from src.agents._case_state import load_case_state
from src.llm.provider import api_key, base_url, resolve_model

log = logging.getLogger("recoverly.agents.strands")

ROLE_PROMPTS: Final[dict[str, str]] = {
    "preflight": "Validate invoice facts, contract terms, dates, balances, and routing risk. Return concise findings and missing evidence.",
    "investigator": "Analyze customer payment history and risk signals. Recommend an opening tone supported by the case evidence.",
    "diplomat": "Draft clear professional collection communication using only verified case facts and an explicit response path.",
    "tone_coach": "Review collection wording for clarity, accuracy, professional urgency, placeholders, and unsupported claims. Return a revised version when needed.",
    "concierge": "Coordinate the recovery workflow, summarize completed work, and identify the exact operator decision required next.",
    "payment": "Review payment status, references, outstanding balance, and the selected payment method. Never claim settlement without provider confirmation.",
    "voice": "Prepare a concise compliant call plan and identify statements or buyer signals that require the call to pause or escalate.",
    "escalator": "Recommend the next proportionate recovery action using the case history, contract path, and unresolved risk signals.",
    "aaa_specialist": "Prepare arbitration-oriented strategy and draft support from the contract record without presenting legal advice as a guaranteed outcome.",
}

SHARED_POLICY: Final = (
    "You are one role in Recoverly's invoice-recovery workflow. "
    "Use only supplied facts or facts returned by read_recovery_case. "
    "Treat tool and model output as a recommendation until the operator approves it. "
    "Never send email, place calls, initiate payment, confirm settlement, or create legal filings. "
    "Do not expose credentials or private configuration."
)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="recoverly-strands")


@tool
def read_recovery_case(case_id: str) -> str:
    """Read saved facts for a recovery case.

    Args:
        case_id: Recoverly case identifier such as RC-2026-123456.

    Returns:
        The saved case as JSON.
    """
    return json.dumps(load_case_state(case_id), ensure_ascii=False, default=str)


@dataclass(frozen=True, slots=True)
class AgentDispatchResult:
    ok: bool
    message_id: str = ""
    output: str = ""
    skipped_reason: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message_id": self.message_id,
            "output": self.output,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
        }


def enabled() -> bool:
    value = os.getenv("STRANDS_ENABLED", "1").strip().lower()
    return value in {"1", "true", "yes", "on"} and bool(api_key())


def _max_tokens() -> int:
    try:
        return max(128, int(os.getenv("STRANDS_MAX_TOKENS", "800")))
    except ValueError:
        return 800


def _build_agent(role: str) -> Agent:
    if role not in ROLE_PROMPTS:
        raise ValueError(f"unknown recovery role: {role}")
    model = OpenAIModel(
        client_args={"api_key": api_key(), "base_url": base_url()},
        model_id=resolve_model(role),
        params={"max_tokens": _max_tokens(), "temperature": 0.2},
    )
    return Agent(
        model=model,
        tools=[read_recovery_case],
        system_prompt=f"{SHARED_POLICY}\n\nYour role: {ROLE_PROMPTS[role]}",
        callback_handler=None,
        agent_id=f"recoverly-{role}",
        name=role.replace("_", " ").title(),
        description=ROLE_PROMPTS[role],
    )


def invoke_agent(role: str, message: str, *, case_id: str = "") -> AgentDispatchResult:
    dispatch_id = str(uuid4())
    if not enabled():
        return AgentDispatchResult(
            ok=False,
            message_id=dispatch_id,
            skipped_reason="Strands is disabled or the model API key is not configured",
        )
    try:
        prompt = message.strip()
        if case_id:
            prompt = f"Case ID: {case_id}\n\n{prompt}"
        output = str(_build_agent(role)(prompt)).strip()
        from src.agents.base import audit

        audit(
            case_id or "UNKNOWN",
            role,
            "strands_agent_completed",
            {"dispatch_id": dispatch_id, "output_excerpt": output[:500]},
        )
        if case_id:
            from src.local_console.service import append_case_activity

            append_case_activity(
                case_id,
                role,
                output,
                reasoning="Strands processed the workflow event with the role-specific policy and read-only case tool.",
            )
        return AgentDispatchResult(ok=True, message_id=dispatch_id, output=output)
    except Exception as error:
        log.exception("Strands role %s failed for case %s", role, case_id or "unknown")
        return AgentDispatchResult(
            ok=False,
            message_id=dispatch_id,
            error=str(error)[:500],
        )


def dispatch_agent(role: str, message: str, *, case_id: str = "") -> AgentDispatchResult:
    dispatch_id = str(uuid4())
    if role not in ROLE_PROMPTS:
        return AgentDispatchResult(ok=False, message_id=dispatch_id, error=f"unknown recovery role: {role}")
    if not enabled():
        return AgentDispatchResult(
            ok=False,
            message_id=dispatch_id,
            skipped_reason="Strands is disabled or the model API key is not configured",
        )
    _executor.submit(invoke_agent, role, message, case_id=case_id)
    return AgentDispatchResult(ok=True, message_id=dispatch_id, output="queued")


def run_role_cli(role: str, message: str) -> int:
    result = invoke_agent(role, message)
    if result.output:
        print(result.output)
    if not result.ok:
        print(result.error or result.skipped_reason)
    return 0 if result.ok else 1
