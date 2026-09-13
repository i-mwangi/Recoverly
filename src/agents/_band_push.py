from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Final

from band.client.rest import ChatMessageRequest, ChatMessageRequestMentionsItem, RestClient

from src.config import settings

log = logging.getLogger("recoverly.agents.band_push")

CONCIERGE_ROLE: Final = "concierge"


@dataclass(frozen=True, slots=True)
class PushTarget:
    agent_id: str
    handle: str
    name: str

    @property
    def mention(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True)
class PushResult:
    ok: bool
    message_id: str | None = None
    skipped_reason: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "message_id": self.message_id,
            "skipped_reason": self.skipped_reason,
            "error": self.error,
        }


def resolve_target(role: str = CONCIERGE_ROLE) -> PushTarget | None:
    agent_id = os.getenv(f"BAND_{role.upper()}_AGENT_ID", "").strip()
    if not agent_id:
        return None
    handle = os.getenv(f"BAND_{role.upper()}_HANDLE", role).strip().lstrip("@")
    return PushTarget(agent_id=agent_id, handle=handle, name=handle.split("/")[-1] or role)


def sender_api_key(sender_role: str) -> str:
    return os.getenv(f"BAND_{sender_role.upper()}_API_KEY", "").strip()


def push_message(body: str, *, sender_role: str, target_role: str = CONCIERGE_ROLE) -> PushResult:
    target = resolve_target(target_role)
    api_key = sender_api_key(sender_role)
    room_id = os.getenv("BAND_ROOM_ID", "").strip()

    missing = [
        label
        for label, value in (
            (f"BAND_{target_role.upper()}_AGENT_ID", target),
            (f"BAND_{sender_role.upper()}_API_KEY", api_key),
            ("BAND_ROOM_ID", room_id),
        )
        if not value
    ]
    if missing:
        reason = "missing " + ", ".join(missing)
        log.info("band push skipped: %s", reason)
        return PushResult(ok=False, skipped_reason=reason)

    assert target is not None

    try:
        client = RestClient(api_key=api_key, base_url=os.getenv("BAND_REST_URL", "").strip())
        response = client.agent_api_messages.create_agent_chat_message(
            chat_id=room_id,
            message=ChatMessageRequest(
                content=body,
                mentions=[
                    ChatMessageRequestMentionsItem(
                        id=target.agent_id, handle=target.handle, name=target.name
                    )
                ],
            ),
        )
    except Exception as error:
        log.warning("band push %s -> %s failed: %s", sender_role, target_role, error)
        return PushResult(ok=False, error=str(error)[:300])

    return PushResult(ok=True, message_id=str(getattr(response.data, "id", "") or "") or None)


def push_card(
    *,
    sender_role: str,
    case_id: str,
    card_kind: str,
    body: str,
    target_role: str = CONCIERGE_ROLE,
) -> PushResult:
    target = resolve_target(target_role)
    mention = target.mention if target else target_role
    header = (
        f"@{mention} event=approval_request for case `{case_id}` card_kind={card_kind}\n\n"
    )
    return push_message(header + body, sender_role=sender_role, target_role=target_role)
