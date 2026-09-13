from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Final, Sequence

from band import Agent, PlatformMessage
from band.core.simple_adapter import SimpleAdapter
from dotenv import load_dotenv

from src.agents._utils import append_jsonl_locked, detect_anomaly, strip_fenced_blocks
from src.config import PROJECT_ROOT, settings

load_dotenv(PROJECT_ROOT / ".env")

LOG_DIR: Final = PROJECT_ROOT / "logs"
LOG_FORMAT: Final = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
NOISY_LOGGERS: Final = (
    "websockets",
    "httpx",
    "httpcore",
    "phoenix_channels_python_client",
)

HANDLE_PREFIX: Final = "recoverly-"
SEEN_MESSAGE_LIMIT: Final = 1_000
SHUTDOWN_TIMEOUT_SECONDS: Final = 5

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

        for name in NOISY_LOGGERS:
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
        f"{HANDLE_PREFIX}{normalized_mention.replace("_", "-")}",
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
    if not reply or not participants:
        return []

    mentions = parse_mentions(strip_fenced_blocks(reply))
    if not mentions:
        return []

    resolved: list[str] = []
    seen: set[str] = set()

    for mention in mentions:
        for participant in participants:
            if not _participant_matches(participant, mention):
                continue
            handle = participant.get("handle") or ""
            if normalize_handle(handle) == normalize_handle(self_handle) or participant.get("id") == self_id:
                break
            if handle and handle not in seen:
                seen.add(handle)
                resolved.append(handle)
            break

    return resolved


def extract_message_text(msg: PlatformMessage) -> str:
    for attribute in ("content", "text"):
        value = getattr(msg, attribute, None)
        if value:
            return str(value)
    return str(msg)


def extract_case_id(text: str) -> str:
    match = CASE_ID_IN_TEXT_RE.search(text or "")
    return match.group(1) if match else "UNKNOWN"


class BandAgentAdapter(SimpleAdapter):
    role: str = "base"

    def __init__(self) -> None:
        super().__init__()
        self.log = configure_logging(self.role)
        self.handle = os.getenv(f"BAND_{self.role.upper()}_HANDLE", self.role).lstrip(
            "@"
        )
        self.agent_id = os.getenv(f"BAND_{self.role.upper()}_AGENT_ID", "")
        self._seen_message_ids: OrderedDict[str, None] = OrderedDict()

    async def handle_message(
        self,
        text: str,
        msg: PlatformMessage,
        tools: Any,
        history: Any,
        room_id: str,
    ) -> str | None:
        return None

    @property
    def aliases(self) -> set[str]:
        return {
            alias
            for alias in (
                self.agent_id,
                self.handle,
                short_role(self.handle),
                self.role,
                self.role.replace("_", "-"),
            )
            if alias
        }

    def _already_seen(self, msg: PlatformMessage) -> bool:
        message_id = getattr(msg, "id", None) or getattr(msg, "message_id", None)
        if not message_id:
            return False

        key = str(message_id)
        if key in self._seen_message_ids:
            return True

        self._seen_message_ids[key] = None
        while len(self._seen_message_ids) > SEEN_MESSAGE_LIMIT:
            self._seen_message_ids.popitem(last=False)
        return False

    @staticmethod
    def _participants(tools: Any) -> list[dict[str, Any]]:
        try:
            return list(tools.participants or [])
        except Exception:
            return []

    async def on_started(self, agent_name: str, agent_description: str) -> None:
        self.log.info("%s started, listening for @%s", agent_name, self.handle)

    async def on_message(
        self,
        msg: PlatformMessage,
        tools: Any,
        history: Any,
        participants_msg: Any,
        contacts_msg: Any,
        *,
        is_session_bootstrap: bool,
        room_id: str,
    ) -> None:
        if self._already_seen(msg):
            return

        text = extract_message_text(msg)
        mentions = parse_mentions(text)

        self.log.info(
            "message room=%s bootstrap=%s mentions=%s text=%.120s",
            room_id[:8],
            is_session_bootstrap,
            mentions,
            text.replace("\n", " "),
        )

        if mentions and not (set(mentions) & self.aliases):
            return

        if await self._escalate_anomaly(text, tools):
            return

        try:
            reply = await self.handle_message(text, msg, tools, history, room_id)
        except Exception as error:
            self.log.exception("handle_message failed")
            audit(
                extract_case_id(text),
                self.role,
                "agent_error",
                {"error": str(error), "text_excerpt": text[:200]},
            )
            reply = f"@{self.handle} hit an internal error: {error}"

        if reply:
            await self._send(reply, tools)

    async def _escalate_anomaly(self, text: str, tools: Any) -> bool:
        if self.role == "concierge":
            return False

        kind = detect_anomaly(text)
        if not kind:
            return False

        case_id = extract_case_id(text)
        audit(
            case_id,
            self.role,
            f"anomaly_detected_{kind}",
            {"kind": kind, "text_excerpt": text[:200]},
        )

        concierge_handle = next(
            (
                participant.get("handle")
                for participant in self._participants(tools)
                if "concierge" in (participant.get("handle") or "")
            ),
            None,
        )
        if not concierge_handle:
            self.log.warning("anomaly %s detected but no concierge in room", kind)
            return True

        body = (
            f"@concierge anomaly **{kind}** detected on case `{case_id}` "
            f"by @{self.handle}. Excerpt: _{text[:150]!r}_. card_kind=anomaly_{kind}"
        )
        try:
            await tools.send_message(body, mentions=[concierge_handle])
            self.log.info(
                "anomaly %s escalated to concierge for case %s", kind, case_id
            )
        except Exception as error:
            self.log.warning("anomaly escalation failed: %s", error)

        return True

    async def _send(self, reply: str, tools: Any) -> None:
        participants = self._participants(tools)
        mention_handles = build_mention_handles(
            reply, participants, self_handle=self.handle, self_id=self.agent_id
        )

        if not mention_handles:
            mention_handles = self._orchestrator_fallback(participants)

        if not mention_handles:
            self.log.warning(
                "reply has no resolvable mention and BAND_ORCHESTRATOR_HANDLE is unset; dropping"
            )
            return

        try:
            await tools.send_message(reply, mentions=mention_handles)
            self.log.info("sent %d chars to %s", len(reply), mention_handles)
        except Exception:
            self.log.exception("send_message failed (mentions=%s)", mention_handles)

    @staticmethod
    def _orchestrator_fallback(participants: Sequence[dict[str, Any]]) -> list[str]:
        configured = os.getenv("BAND_ORCHESTRATOR_HANDLE", "").strip()
        if not configured:
            return []
        target = normalize_handle(configured)
        match = next(
            (
                participant.get("handle")
                for participant in participants
                if normalize_handle(str(participant.get("handle") or "")) == target
            ),
            None,
        )
        return [match] if match else []

    async def on_cleanup(self, room_id: str) -> None:
        self.log.info("cleanup room=%s", room_id[:8])


def _install_stop_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(sig, stop.set)


async def _serve(agent: Agent, log: logging.Logger) -> int:
    await agent.start()
    stop = asyncio.Event()
    _install_stop_handlers(stop)

    runner = asyncio.create_task(agent.run_forever())
    waiter = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            {runner, waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        if runner in done:
            await runner
    finally:
        log.info("shutting down")
        await agent.stop(timeout=SHUTDOWN_TIMEOUT_SECONDS)
        runner.cancel()
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        with contextlib.suppress(asyncio.CancelledError):
            await waiter

    return 0


def run_agent(adapter_cls: type[BandAgentAdapter]) -> int:
    role_upper = adapter_cls.role.upper()
    agent_id = os.getenv(f"BAND_{role_upper}_AGENT_ID", "")
    api_key = os.getenv(f"BAND_{role_upper}_API_KEY", "")

    adapter = adapter_cls()

    if not agent_id or not api_key:
        adapter.log.error(
            "missing BAND_%s_AGENT_ID or BAND_%s_API_KEY in the environment",
            role_upper,
            role_upper,
        )
        return 1

    adapter.log.info("connecting as agent_id=%s", agent_id[:8])

    agent = Agent.create(
        adapter=adapter,
        agent_id=agent_id,
        api_key=api_key,
        ws_url=settings.api_keys.band_ws_url,
        rest_url=settings.api_keys.band_rest_url,
    )

    try:
        return asyncio.run(_serve(agent, adapter.log))
    except KeyboardInterrupt:
        return 0
