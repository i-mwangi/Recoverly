from __future__ import annotations

import re
from typing import Final

SLACK_BLOCK_TEXT_LIMIT: Final = 3_000
SLACK_PREVIEW_CAP: Final = 2_700
SLACK_PREVIEW_SUFFIX: Final = "\n\n… (truncated for preview; full text is sent on approve)"

CODE_BLOCK_RE: Final = re.compile(r"```(?:\w+)?\n(.+?)\n```", re.DOTALL)


def slack_preview(text: str, cap: int = SLACK_PREVIEW_CAP) -> str:
    if len(text) <= cap:
        return text
    return text[:cap] + SLACK_PREVIEW_SUFFIX


def extract_code_block(text: str) -> str:
    match = CODE_BLOCK_RE.search(text or "")
    return match.group(1).strip() if match else (text or "").strip()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix
