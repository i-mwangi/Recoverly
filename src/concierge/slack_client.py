from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Final

import httpx

from src.config import settings

log = logging.getLogger("recoverly.concierge.slack")

SLACK_API_BASE: Final = "https://slack.com/api"
DEFAULT_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_CHANNEL: Final = "#concierge-recoverly"


@dataclass(frozen=True, slots=True)
class SlackResult:
    ok: bool
    ts: str | None = None
    channel: str | None = None
    error: str | None = None
    scheduled_message_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "ts": self.ts,
            "channel": self.channel,
            "error": self.error,
            "scheduled_message_id": self.scheduled_message_id,
        }


def default_channel() -> str:
    return os.getenv("SLACK_CONCIERGE_CHANNEL", "").strip() or DEFAULT_CHANNEL


def _call(method: str, payload: dict[str, Any]) -> SlackResult:
    token = settings.api_keys.slack_bot_token
    if not token:
        return SlackResult(ok=False, error="SLACK_BOT_TOKEN is not set")

    try:
        response = httpx.post(
            f"{SLACK_API_BASE}/{method}",
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=DEFAULT_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
    except Exception as error:
        log.warning("slack %s failed: %s", method, error)
        return SlackResult(ok=False, error=str(error)[:300])

    if not body.get("ok"):
        log.warning("slack %s rejected: %s", method, body.get("error"))
        return SlackResult(ok=False, error=str(body.get("error")))

    return SlackResult(
        ok=True,
        ts=body.get("ts"),
        channel=body.get("channel"),
        scheduled_message_id=body.get("scheduled_message_id"),
    )


def open_revision_modal(trigger_id: str, case_id: str, target: str) -> SlackResult:
    title = "Revise message"
    callback_id = f"revision|{target}|{case_id}"
    view = {
        "type": "modal",
        "callback_id": callback_id,
        "title": {"type": "plain_text", "text": title},
        "submit": {"type": "plain_text", "text": "Send revision"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "private_metadata": case_id,
        "blocks": [
            {
                "type": "input",
                "block_id": "revision_note",
                "label": {"type": "plain_text", "text": "What should change?"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "note",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Example: Make the opening friendlier and keep the five-business-day deadline.",
                    },
                },
            }
        ],
    }
    return _call("views.open", {"trigger_id": trigger_id, "view": view})


def post_message(
    text: str,
    *,
    channel: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
    thread_ts: str | None = None,
) -> SlackResult:
    payload: dict[str, Any] = {"channel": channel or default_channel(), "text": text}
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return _call("chat.postMessage", payload)


def schedule_message(
    text: str,
    post_at: int,
    *,
    channel: str | None = None,
    blocks: list[dict[str, Any]] | None = None,
) -> SlackResult:
    payload: dict[str, Any] = {
        "channel": channel or default_channel(),
        "post_at": post_at,
        "text": text,
    }
    if blocks:
        payload["blocks"] = blocks
    return _call("chat.scheduleMessage", payload)


def header_block(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def section_block(markdown: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": markdown}}


def context_block(markdown: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": markdown}]}


def divider_block() -> dict[str, Any]:
    return {"type": "divider"}


def button(text: str, action_id: str, value: str, style: str | None = None) -> dict[str, Any]:
    element: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": text},
        "action_id": action_id,
        "value": value,
    }
    if style:
        element["style"] = style
    return element


def actions_block(*buttons: dict[str, Any]) -> dict[str, Any]:
    return {"type": "actions", "elements": list(buttons)}
