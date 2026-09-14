from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timezone
from typing import Any, Final, Sequence

from src.agents._utils import append_jsonl_locked, strip_fenced_blocks
from src.config import PROJECT_ROOT, settings

LOG_DIR: Final = PROJECT_ROOT / "logs"
LOG_FORMAT: Final = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
HANDLE_PREFIX: Final = "recoverly-"
MENTION_HANDLE_RE: Final = re.compile(r"@([a-zA-Z0-9_./-]+)")
MENTION_INTERNAL_RE: Final = re.compile(r"@\[\[([a-f0-9-]{8,})\]\]")
CASE_ID_IN_TEXT_RE: Final = re.compile(r"case `([^`]+)`")
_logging_configured = False


def configure_logging(role: str) -> logging.Logger:
    global _logging_configured
    if not _logging_configured:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        formatter = logging.Formatter(LOG_FORMAT)
        file_handler = logging.FileHandler(LOG_DIR / f"{role}.log", encoding="utf-8")
        file_handler.setFormatter(formatter)
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        root = logging.getLogger()
        root.setLevel(settings.log_level)
        root.handlers = [file_handler, stream_handler]
        for name in ("httpx", "httpcore", "openai", "strands"):
            logging.getLogger(name).setLevel(logging.WARNING)
        _logging_configured = True
    return logging.getLogger(f"recoverly.agent.{role}")


def audit(case_id: str, agent: str, event: str, payload: dict[str, Any]) -> None:
    from src.agents._case_state import audit_trail_path

    append_jsonl_locked(
        audit_trail_path(),
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "case_id": case_id or "UNKNOWN",
            "agent": agent,
            "event": event,
            "payload": payload,
        },
    )


def parse_mentions(text: str | None) -> list[str]:
    source = text or ""
    internal_ids = MENTION_INTERNAL_RE.findall(source)
    handles = [
        handle.rstrip(".,;:!?")
        for handle in MENTION_HANDLE_RE.findall(MENTION_INTERNAL_RE.sub(" ", source))
    ]
    return internal_ids + handles


def short_role(handle: str) -> str:
    bare = handle.lstrip("@")
    if "/" in bare:
        bare = bare.split("/", 1)[1]
    return bare.removeprefix(HANDLE_PREFIX).replace("-", "_")


def normalize_handle(handle: str) -> str:
    return handle.strip().lstrip("@").lower()


def _participant_matches(participant: dict[str, Any], mention: str) -> bool:
    handle = str(participant.get("handle") or "")
    normalized_mention = normalize_handle(mention)
    normalized_handle = normalize_handle(handle)
    if normalized_mention in {
        normalize_handle(str(participant.get("id") or "")),
        normalized_handle,
        normalize_handle(str(participant.get("name") or "")),
    }:
        return bool(normalized_mention)
    short_handle = normalized_handle.rsplit("/", 1)[-1]
    aliases = {
        normalized_mention,
        normalized_mention.replace("_", "-"),
        f"{HANDLE_PREFIX}{normalized_mention}",
        f"{HANDLE_PREFIX}{normalized_mention.replace('_', '-')}",
    }
    return short_handle in aliases or any(
        short_handle.startswith(f"{alias}-") for alias in aliases
    )


def build_mention_handles(
    reply: str,
    participants: Sequence[dict[str, Any]],
    *,
    self_handle: str = "",
    self_id: str = "",
) -> list[str]:
    mentions = parse_mentions(strip_fenced_blocks(reply))
    resolved: list[str] = []
    seen: set[str] = set()
    for mention in mentions:
        for participant in participants:
            if not _participant_matches(participant, mention):
                continue
            handle = str(participant.get("handle") or "")
            if normalize_handle(handle) == normalize_handle(self_handle) or participant.get("id") == self_id:
                break
            if handle and handle not in seen:
                seen.add(handle)
                resolved.append(handle)
            break
    return resolved


def extract_message_text(message: Any) -> str:
    for attribute in ("content", "text"):
        value = getattr(message, attribute, None)
        if value:
            return str(value)
    return str(message)


def extract_case_id(text: str) -> str:
    match = CASE_ID_IN_TEXT_RE.search(text or "")
    return match.group(1) if match else "UNKNOWN"


class StrandsAgentAdapter:
    role = "base"

    def __init__(self) -> None:
        self.log = configure_logging(self.role)
        self.handle = self.role
        self.agent_id = f"recoverly-{self.role}"

    async def handle_message(
        self,
        text: str,
        message: Any = None,
        tools: Any = None,
        history: Any = None,
        room_id: str = "",
    ) -> str | None:
        return None


def run_agent(adapter_cls: type[StrandsAgentAdapter]) -> int:
    from src.agents.strands_runtime import run_role_cli

    message = " ".join(sys.argv[1:]).strip()
    if not message:
        message = input(f"{adapter_cls.role}> ").strip()
    return run_role_cli(adapter_cls.role, message)
