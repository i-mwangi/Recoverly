from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Final, Iterable

from src.utils.filelock import append_jsonl

log = logging.getLogger("recoverly.agents.utils")

FENCED_BLOCK_RE: Final = re.compile(r"```.*?```", re.DOTALL)
LEGACY_ERROR_MARKER: Final = "⚠️"


def strip_fenced_blocks(text: str | None) -> str:
    return FENCED_BLOCK_RE.sub(" ", text or "")


def _strip_code_fence(text: str) -> str:
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    _, _, remainder = cleaned.partition("\n")
    if remainder.rstrip().endswith("```"):
        remainder = remainder.rstrip()[: -len("```")]
    return remainder.strip()


def _balanced_object_span(text: str) -> tuple[int, int] | None:
    start = text.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if in_string:
            if char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return start, index + 1

    return None


def parse_json_object(text: str | None) -> dict[str, Any] | None:
    if not text:
        return None

    cleaned = _strip_code_fence(text)
    span = _balanced_object_span(cleaned)

    candidates: list[str] = []
    if span is not None:
        candidates.append(cleaned[span[0] : span[1]])
    first, last = cleaned.find("{"), cleaned.rfind("}")
    if first != -1 and last > first:
        candidates.append(cleaned[first : last + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    return None


def error_envelope(agent: str, reason: str, case_id: str = "") -> dict[str, Any]:
    return {"_error": True, "_agent": agent, "_reason": reason, "_case_id": case_id}


def is_error_envelope(text: str | None) -> bool:
    if not text:
        return False
    parsed = parse_json_object(text)
    if parsed is not None and parsed.get("_error") is True:
        return True
    return LEGACY_ERROR_MARKER in strip_fenced_blocks(text)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


ANOMALY_PATTERNS: Final[dict[str, tuple[str, ...]]] = {
    "welfare": (
        r"\bsuicid",
        r"\bself[- ]?harm\b",
        r"\bending it all\b",
        r"\b(?:kill|hurt)\s+myself\b",
        r"\bwelfare concern\b",
    ),
    "bankruptcy": (
        r"\bchapter\s*(?:7|11|13)\b",
        r"\bbankrupt(?:cy|ed)?\b",
        r"\bfiled.*(?:insolven|administration)\b",
    ),
    "wrong_person": (
        r"\bwrong\s+(?:number|person|contact)\b",
        r"\b(?:not|don'?t).*work (?:here|there)\b",
        r"\bremove (?:me )?from your list\b",
    ),
    "cease_and_desist": (
        r"\bcease\s+(?:and|&)\s+desist\b",
        r"\bdo not contact\b",
    ),
}

DISPUTE_PATTERNS: Final[tuple[str, ...]] = (
    r"\b(?:disput(?:e|ing)|don'?t owe|invalid invoice|not (?:owed|valid))\b",
    r"\bnever\s+(?:received|got)\s+(?:the\s+)?(?:goods|shipment|product)\b",
    r"\b(?:wrong\s+(?:amount|quantity)|amount.*is\s+(?:wrong|incorrect))\b",
    r"\b(?:defective|damaged)\s+(?:goods|product|shipment)\b",
    r"\bcredit\s+memo\s+(?:requested|pending|due)\b",
    r"\bnever\s+(?:placed|signed)\s+(?:this\s+)?order\b",
)

PARTIAL_PAYMENT_PATTERNS: Final[tuple[str, ...]] = (
    r"\b(?:pay|paying|paid)\s+(?:partial|part(?:ial)?\s+(?:of|amount))\b",
    r"\b(?:half|50\s*%|partial)\s+now,?\s+(?:rest|balance|remainder)\b",
    r"\bcan\s+(?:only\s+)?(?:pay|afford)\s+\$?[\d,]+\b",
    r"\b(?:installment|payment\s+plan)\b",
    r"\bwill\s+pay\s+\$?[\d,]+\s+(?:now|today),?\s+(?:and|then|plus)\b",
)

EXTENSION_REQUEST_PATTERNS: Final[tuple[str, ...]] = (
    r"\b(?:extension|extend|delay|postpone)\s+(?:the\s+)?(?:payment|invoice|due)\b",
    r"\bcan\s+(?:we|i)\s+pay\s+(?:by|on|next)\b",
    r"\b(?:push\s+(?:back|out)|defer)\s+(?:the\s+)?(?:payment|due)\b",
    r"\b(?:request(?:ing)?|need(?:ing)?)\s+(?:more\s+time|an?\s+extension)\b",
    r"\b(?:next\s+month|two\s+weeks|30\s+days)\b.*\b(?:pay|due)\b",
)

PROMISE_TO_PAY_PATTERNS: Final[tuple[str, ...]] = (
    r"\b(?:will|i'?ll|we'?ll)\s+(?:pay|wire|send|remit)\b",
    r"\b(?:promise|commit)\s+to\s+pay\b",
    r"\b(?:initiated|sent)\s+(?:the\s+)?(?:wire|payment|transfer)\b",
    r"\bfunds\s+(?:will\s+)?(?:hit|arrive|clear)\s+by\b",
)


def _matches_any(text: str | None, patterns: Iterable[str]) -> bool:
    if not text:
        return False
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def detect_dispute(text: str | None) -> bool:
    return _matches_any(text, DISPUTE_PATTERNS)


def detect_partial_payment(text: str | None) -> bool:
    return _matches_any(text, PARTIAL_PAYMENT_PATTERNS)


def detect_extension_request(text: str | None) -> bool:
    return _matches_any(text, EXTENSION_REQUEST_PATTERNS)


def detect_promise_to_pay(text: str | None) -> bool:
    return _matches_any(text, PROMISE_TO_PAY_PATTERNS)


def detect_anomaly(text: str | None) -> str | None:
    if not text:
        return None
    for kind, patterns in ANOMALY_PATTERNS.items():
        if _matches_any(text, patterns):
            return kind
    return None


def append_jsonl_locked(path: Path, record: dict[str, Any]) -> None:
    append_jsonl(path, record)
