from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final

from src.config import PROJECT_ROOT, settings
from src.utils.filelock import append_jsonl

AGENT_EVENT_OUTBOX: Final = PROJECT_ROOT / "data" / "agent_event_out.jsonl"
VOICE_SOURCE: Final = "elevenlabs_convai"
VOICE_AGENT: Final = "voice"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_voice_event(event: str, payload: dict[str, Any], case_id: str = "UNKNOWN") -> None:
    append_jsonl(
        settings.data.audit_trail_jsonl,
        {
            "ts": _timestamp(),
            "case_id": case_id,
            "agent": VOICE_AGENT,
            "event": event,
            "source": VOICE_SOURCE,
            "payload": payload,
        },
    )


def queue_agent_event(case_id: str, channel: str, body: dict[str, Any]) -> None:
    append_jsonl(
        AGENT_EVENT_OUTBOX,
        {
            "ts": _timestamp(),
            "case_id": case_id,
            "channel": channel,
            "from_agent": VOICE_AGENT,
            "body": body,
        },
    )
