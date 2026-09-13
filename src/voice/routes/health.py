from __future__ import annotations

from typing import Final

from flask import Blueprint, Response, jsonify

from src.llm.provider import api_key as llm_api_key
from src.config import settings

blueprint = Blueprint("health", __name__)

SERVICE_NAME: Final = "recoverly-webhooks"


@blueprint.route("/health", methods=["GET"])
def health() -> tuple[Response, int]:
    return (
        jsonify(
            {
                "status": "ok",
                "service": SERVICE_NAME,
                "demo_mode": settings.demo_mode,
                "llm_key_present": bool(llm_api_key()),
                "slack_key_present": bool(settings.api_keys.slack_bot_token),
            }
        ),
        200,
    )
