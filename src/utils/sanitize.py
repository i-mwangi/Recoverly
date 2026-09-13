from __future__ import annotations

import re
from typing import Final

MENTION_PLACEHOLDER: Final = "[mention-stripped]"
ZERO_WIDTH_SPACE: Final = "​"

LITERAL_MENTIONS: Final[tuple[str, ...]] = (
    "<!channel>",
    "<!here>",
    "<!everyone>",
    "&lt;!channel&gt;",
    "&lt;!here&gt;",
    "&lt;!everyone&gt;",
)

STRUCTURED_MENTION_RE: Final = re.compile(
    r"<@U[A-Z0-9]+(?:\|[^>]*)?>"
    r"|<#C[A-Z0-9]+(?:\|[^>]*)?>"
    r"|<!subteam\^[A-Z0-9]+(?:\|[^>]*)?>"
)


def strip_slack_mentions(text: str) -> str:
    if not text:
        return ""

    cleaned = text
    for literal in LITERAL_MENTIONS:
        cleaned = cleaned.replace(literal, MENTION_PLACEHOLDER)

    cleaned = STRUCTURED_MENTION_RE.sub(MENTION_PLACEHOLDER, cleaned)
    return cleaned.replace("@", f"@{ZERO_WIDTH_SPACE}")
