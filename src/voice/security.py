from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Final

log = logging.getLogger("recoverly.voice.security")

DEFAULT_MAX_AGE_SECONDS: Final = 300
POST_CALL_MAX_AGE_SECONDS: Final = 1_800
STRICT_ENABLED_VALUES: Final = frozenset({"1", "true", "yes", "on"})

SIGNATURE_TIMESTAMP_KEY: Final = "t"
SIGNATURE_DIGEST_KEY: Final = "v0"


def strict_mode(flag_name: str, default: bool = True) -> bool:
    raw = os.getenv(flag_name, "").strip().lower()
    if not raw:
        return default
    return raw in STRICT_ENABLED_VALUES


def _parse_signature_header(header: str) -> tuple[str, str] | None:
    parts = dict(
        piece.split("=", 1) for piece in header.split(",") if "=" in piece
    )
    timestamp = parts.get(SIGNATURE_TIMESTAMP_KEY, "")
    digest = parts.get(SIGNATURE_DIGEST_KEY, "")
    if not timestamp or not digest:
        return None
    return timestamp, digest


def verify_signature(
    body: bytes,
    signature_header: str | None,
    secret: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    if not signature_header or not secret:
        return False

    parsed = _parse_signature_header(signature_header)
    if parsed is None:
        return False

    timestamp, digest = parsed
    try:
        issued_at = int(timestamp)
    except ValueError:
        return False

    if abs(time.time() - issued_at) > max_age_seconds:
        log.warning("rejecting webhook signature outside the %ds replay window", max_age_seconds)
        return False

    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, digest)


def verify_slack_signature(
    body: bytes,
    timestamp_header: str | None,
    signature_header: str | None,
    signing_secret: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> bool:
    if not timestamp_header or not signature_header or not signing_secret:
        return False

    try:
        issued_at = int(timestamp_header)
    except ValueError:
        return False

    if abs(time.time() - issued_at) > max_age_seconds:
        log.warning("rejecting Slack signature outside the %ds replay window", max_age_seconds)
        return False

    basestring = b"v0:" + timestamp_header.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def signature_required(flag_name: str, secret: str) -> bool:
    if strict_mode(flag_name):
        return True
    if not secret:
        log.warning("%s is disabled and no secret is configured; accepting unsigned requests", flag_name)
    return False
