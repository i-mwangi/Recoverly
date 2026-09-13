from __future__ import annotations

from typing import Final

PERSONA_TO_BUYER_ID: Final[dict[str, str]] = {
    "abc_trading": "BUYER-ABC-TRADING-CA",
    "polymatrix": "BUYER-POLYMATRIX-NY",
    "xyz": "BUYER-XYZ-INDUSTRIES-TX",
    "newleaf": "BUYER-NEWLEAF-WELLNESS-OR",
    "megacorp": "BUYER-MEGACORP-HOLDINGS-NY",
}

BUYER_ID_TO_PERSONA: Final[dict[str, str]] = {
    buyer_id: persona for persona, buyer_id in PERSONA_TO_BUYER_ID.items()
}

KNOWN_PERSONAS: Final[tuple[str, ...]] = tuple(PERSONA_TO_BUYER_ID)


def normalize_persona(persona: str | None) -> str:
    return (persona or "").strip().lower().replace("-", "_")


def buyer_id_for(persona: str | None) -> str | None:
    return PERSONA_TO_BUYER_ID.get(normalize_persona(persona))


def persona_for(buyer_id: str | None) -> str | None:
    return BUYER_ID_TO_PERSONA.get((buyer_id or "").strip().upper())
