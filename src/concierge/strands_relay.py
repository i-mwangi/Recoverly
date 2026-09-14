from __future__ import annotations

import logging

from src.agents.strands_runtime import AgentDispatchResult, dispatch_agent

log = logging.getLogger("recoverly.concierge.strands_relay")


def notify_agent(role: str, message: str, *, case_id: str = "") -> AgentDispatchResult:
    result = dispatch_agent(role, message, case_id=case_id)
    if not result.ok:
        log.info(
            "could not dispatch to %s for case %s: %s",
            role,
            case_id or "unknown",
            result.skipped_reason or result.error,
        )
    return result
