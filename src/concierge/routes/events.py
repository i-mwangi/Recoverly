from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Final
from threading import Lock

import httpx
import os
from flask import Blueprint, Response, jsonify, request

from src.agents.base import audit
from src.concierge.slack_client import post_message
from src.local_console import service as local_service
from src.voice.security import strict_mode, verify_slack_signature

log = logging.getLogger("recoverly.concierge.routes.events")

blueprint = Blueprint("slack_events", __name__)

STRICT_FLAG: Final = "SLACK_STRICT_SIGNATURE"
SIGNATURE_HEADER: Final = "X-Slack-Signature"
TIMESTAMP_HEADER: Final = "X-Slack-Request-Timestamp"
AGENT: Final = "concierge"
CHANNEL_SESSIONS: dict[str, str] = {}
CHANNEL_SESSIONS_LOCK = Lock()

URL_VERIFICATION: Final = "url_verification"
EVENT_CALLBACK: Final = "event_callback"
FILE_SHARE_SUBTYPE: Final = "file_share"
FILE_SHARED_EVENT: Final = "file_shared"

ALLOWED_MIME_TYPES: Final[frozenset[str]] = frozenset(
    {"application/pdf", "text/markdown", "text/plain", "image/png", "image/jpeg"}
)
MAX_FILE_BYTES: Final = 10 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS: Final = 15.0
METADATA_TIMEOUT_SECONDS: Final = 10.0


def _slack_token() -> str:
    return os.getenv("SLACK_BOT_TOKEN", "").strip()


def intake_dir() -> Path:
    return Path(tempfile.gettempdir()) / "recoverly_intake"


def _json_error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


def _verify(raw: bytes) -> tuple[Response, int] | None:
    secret = __import__("os").getenv("SLACK_SIGNING_SECRET", "").strip()

    if not secret:
        if strict_mode(STRICT_FLAG):
            return _json_error("the Slack signing secret is not configured", 503)
        return None

    if not verify_slack_signature(
        raw,
        request.headers.get(TIMESTAMP_HEADER),
        request.headers.get(SIGNATURE_HEADER),
        secret,
    ):
        return _json_error("unauthorized", 401)

    return None


def _slack_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_slack_token()}"}


def fetch_file_metadata(file_id: str) -> dict[str, Any]:
    if not _slack_token() or not file_id:
        return {}

    try:
        response = httpx.get(
            "https://slack.com/api/files.info",
            params={"file": file_id},
            headers=_slack_headers(),
            timeout=METADATA_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
    except Exception as error:
        log.warning("could not read metadata for file %s: %s", file_id, error)
        return {}

    return body.get("file") or {} if body.get("ok") else {}


def download_file(file_id: str, url: str, suffix: str = ".pdf") -> Path | None:
    if not url or not _slack_token():
        return None

    target = intake_dir() / f"{file_id}{suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.stream(
            "GET", url, headers=_slack_headers(), timeout=DOWNLOAD_TIMEOUT_SECONDS
        ) as response:
            response.raise_for_status()
            with target.open("wb") as handle:
                for chunk in response.iter_bytes():
                    handle.write(chunk)
    except Exception as error:
        log.warning("could not download file %s: %s", file_id, error)
        return None

    return target


def _rejection_reason(mimetype: str, size: int) -> str | None:
    if mimetype and mimetype not in ALLOWED_MIME_TYPES:
        return f"the file type {mimetype} is not accepted"
    if size and size > MAX_FILE_BYTES:
        return f"the file is {size} bytes, over the {MAX_FILE_BYTES} byte limit"
    return None


def handle_shared_file(file_info: dict[str, Any], channel: str) -> dict[str, Any]:
    file_id = str(file_info.get("id", ""))
    metadata = {**file_info, **fetch_file_metadata(file_id)}

    name = str(metadata.get("name", ""))
    mimetype = str(metadata.get("mimetype") or metadata.get("file_type") or "")
    size = int(metadata.get("size") or 0)

    rejection = _rejection_reason(mimetype, size)
    if rejection:
        audit(
            "UNKNOWN",
            AGENT,
            "upload_rejected",
            {"file_id": file_id, "file_name": name, "reason": rejection},
        )
        return {"file_id": file_id, "accepted": False, "reason": rejection}

    download_url = str(
        metadata.get("url_private_download") or metadata.get("url_private") or ""
    )
    local_path = download_file(file_id, download_url)

    audit(
        "UNKNOWN",
        AGENT,
        "invoice_uploaded_slack",
        {
            "file_id": file_id,
            "file_name": name,
            "mimetype": mimetype,
            "size": size,
            "local_path": str(local_path) if local_path else "",
        },
    )

    if local_path:
        with CHANNEL_SESSIONS_LOCK:
            session_id = CHANNEL_SESSIONS.get(channel)
            session = local_service.get_session(session_id) if session_id else None
            if session is None:
                session = local_service.create_session()
                CHANNEL_SESSIONS[channel] = session["id"]
        try:
            updated = local_service.ingest_document(session["id"], local_path, name)
            summary = str((updated.get("messages") or [{}])[-1].get("body") or "Preflight processed the upload.")
            post_message(summary, channel=channel)
        except ValueError as error:
            post_message(f"Preflight could not process `{name}`: {error}", channel=channel)

    return {"file_id": file_id, "accepted": True, "local_path": str(local_path or "")}


@blueprint.route("/slack/events", methods=["POST"])
def slack_events() -> tuple[Response, int]:
    raw = request.get_data()

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {}

    if body.get("type") == URL_VERIFICATION:
        challenge = str(body.get("challenge", ""))
        log.info("answering the Slack URL verification challenge")
        return jsonify({"challenge": challenge}), 200

    rejection = _verify(raw)
    if rejection:
        return rejection

    if body.get("type") != EVENT_CALLBACK:
        return jsonify({"ok": True}), 200

    event = body.get("event") or {}
    event_type = str(event.get("type") or "")
    is_message_file_share = event.get("subtype") == FILE_SHARE_SUBTYPE
    is_file_shared_event = event_type == FILE_SHARED_EVENT
    if not (is_message_file_share or is_file_shared_event):
        return jsonify({"ok": True}), 200

    channel = str(event.get("channel") or event.get("channel_id") or "")
    files = [item for item in (event.get("files") or []) if isinstance(item, dict)]
    if not files and event.get("file_id"):
        files = [{"id": str(event["file_id"])}]

    audit(
        "UNKNOWN",
        AGENT,
        "slack_file_event_received",
        {"event_type": event_type, "channel": channel, "file_count": len(files)},
    )
    results = [handle_shared_file(file_info, channel) for file_info in files]

    return jsonify({"ok": True, "files": results}), 200
