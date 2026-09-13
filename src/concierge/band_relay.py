from __future__ import annotations

import logging
import os
from typing import Final

from src.agents._band_push import PushResult, push_message

log = logging.getLogger("recoverly.concierge.band_relay")

SENDER_ROLE: Final = "concierge"


def agent_mention(role: str) -> str:
    handle = os.getenv(f"BAND_{role.upper()}_HANDLE", role).strip().lstrip("@")
    return handle.split("/")[-1] or role


def notify_agent(role: str, message: str, *, case_id: str = "") -> PushResult:
    body = f"@{agent_mention(role)} {message}"
    result = push_message(body, sender_role=SENDER_ROLE, target_role=role)

    if not result.ok:
        log.info(
            "could not relay to %s for case %s: %s",
            role,
            case_id or "unknown",
            result.skipped_reason or result.error,
        )

    return result
