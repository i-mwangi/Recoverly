from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

EXCERPT_BEFORE: Final = 80
EXCERPT_AFTER: Final = 120
EXCERPT_LIMIT: Final = 200
MAX_REPORTED_MATCHES: Final = 3

HOSTILE_KEYWORDS_RE: Final = re.compile(
    r"\b(?:fuck|harass(?:ment|ing|ed)?|lawyer|attorney|sue|sued|cease|"
    r"stop\s+calling|never\s+call|cease\s+and\s+desist)\b",
    re.IGNORECASE,
)

WELFARE_KEYWORDS_RE: Final = re.compile(
    r"\b(?:kill\s+myself|end\s+(?:my\s+)?life|suicide|suicidal|self.harm|"
    r"don'?t\s+want\s+to\s+live|can'?t\s+go\s+on|hurt\s+myself|end\s+it\s+all|"
    r"no\s+reason\s+to\s+live|wanna\s+die|want\s+to\s+die)\b"
    r"|(?:自殺|自杀|不想活|活不下去|想結束生命|想结束生命|了結自己|了结自己|"
    r"想死|不想活了|活著沒意思|活着没意思|傷害自己|伤害自己)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SignalMatch:
    detected: bool
    matches: tuple[str, ...] = field(default_factory=tuple)
    excerpt: str = ""

    @property
    def reported_matches(self) -> tuple[str, ...]:
        return self.matches[:MAX_REPORTED_MATCHES]

    def quoted_matches(self) -> str:
        return ", ".join(repr(match) for match in self.reported_matches)

    def as_dict(self) -> dict[str, object]:
        return {
            "detected": self.detected,
            "matches": list(self.matches),
            "excerpt": self.excerpt,
        }


def _excerpt_around(text: str, index: int) -> str:
    start = max(0, index - EXCERPT_BEFORE)
    end = min(len(text), index + EXCERPT_AFTER)
    excerpt = text[start:end].strip()
    if len(excerpt) > EXCERPT_LIMIT:
        excerpt = excerpt[: EXCERPT_LIMIT - 3] + "..."
    return excerpt


def scan(transcript: str | None, pattern: re.Pattern[str]) -> SignalMatch:
    if not transcript:
        return SignalMatch(detected=False)

    first = pattern.search(transcript)
    if first is None:
        return SignalMatch(detected=False)

    matches = tuple(
        dict.fromkeys(match.lower() for match in pattern.findall(transcript) if match)
    )
    return SignalMatch(
        detected=True,
        matches=matches or (first.group(0).lower(),),
        excerpt=_excerpt_around(transcript, first.start()),
    )


def detect_hostile(transcript: str | None) -> SignalMatch:
    return scan(transcript, HOSTILE_KEYWORDS_RE)


def detect_welfare(transcript: str | None) -> SignalMatch:
    return scan(transcript, WELFARE_KEYWORDS_RE)
