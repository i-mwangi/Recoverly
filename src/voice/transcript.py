from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from src.agents._utils import parse_json_object
from src.llm.provider import complete_async

MAX_TRANSCRIPT_CHARS: Final = 4_000
MAX_EXTRACTION_TOKENS: Final = 200
EXCERPT_LIMIT: Final = 200
ISO_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FENCED_BLOCK_RE: Final = re.compile(r"```(.*?)```", re.DOTALL)

EXTRACTION_PROMPT: Final = (
    "You analyse buyer collection-call transcripts. Read the transcript and extract any "
    "date the buyer committed to pay, for example 'we will pay by August 5', 'next "
    "Friday', or 'end of the month'. Resolve relative dates against the reference date "
    "given to you. Output only a JSON object with the keys commitment_date, a string in "
    "YYYY-MM-DD form or null, confidence, a number between 0 and 1, and excerpt, a "
    "verbatim quote from the buyer of at most 120 characters. No markdown fences and no "
    "commentary."
)


@dataclass(frozen=True, slots=True)
class Commitment:
    commitment_date: str | None
    confidence: float
    excerpt: str
    error: str = ""

    @property
    def found(self) -> bool:
        return bool(self.commitment_date)

    def as_dict(self) -> dict[str, Any]:
        return {
            "commitment_date": self.commitment_date,
            "confidence": self.confidence,
            "excerpt": self.excerpt,
            "error": self.error,
        }


def extract_transcript_block(text: str | None) -> str:
    match = FENCED_BLOCK_RE.search(text or "")
    if match:
        return match.group(1).strip()

    lines = [
        line
        for line in (text or "").splitlines()
        if not line.lstrip().startswith("@") and "event=" not in line.lower()
    ]
    return "\n".join(lines).strip()


def _normalize_date(value: Any) -> str | None:
    if not isinstance(value, str) or not ISO_DATE_RE.match(value.strip()):
        return None
    candidate = value.strip()
    try:
        date.fromisoformat(candidate)
    except ValueError:
        return None
    return candidate


def _normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, confidence))


async def extract_commitment(
    transcript: str, *, reference_date: date | None = None
) -> Commitment:
    if not transcript.strip():
        return Commitment(None, 0.0, "", "the transcript was empty")

    today = reference_date or date.today()
    prompt = (
        f"Reference date: {today.isoformat()}\n\n"
        f"Transcript:\n{transcript[:MAX_TRANSCRIPT_CHARS]}"
    )

    try:
        raw, _usage = await complete_async(
            EXTRACTION_PROMPT, prompt, agent_role="voice", max_tokens=MAX_EXTRACTION_TOKENS
        )
    except Exception as error:
        return Commitment(None, 0.0, "", f"the model call failed: {error}")

    parsed = parse_json_object(raw) or {}
    return Commitment(
        commitment_date=_normalize_date(parsed.get("commitment_date")),
        confidence=_normalize_confidence(parsed.get("confidence")),
        excerpt=str(parsed.get("excerpt") or "")[:EXCERPT_LIMIT],
    )
