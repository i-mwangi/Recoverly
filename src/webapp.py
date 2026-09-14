from __future__ import annotations

import logging
import os
from typing import Final

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from src.checkout import routes as checkout_routes
from src.concierge.routes import commands as slack_commands
from src.concierge.routes import events as slack_events
from src.concierge.routes import interactivity as slack_interactivity
from src.concierge.routes import resend as resend_routes
from src.config import settings
from src.local_console import blueprint as local_console_blueprint
from src.payments import routes as payment_routes
from src.voice.routes import fish as fish_routes
from src.voice.routes import health as health_routes
from src.voice.routes import twilio as twilio_routes

log = logging.getLogger("recoverly.webapp")

DEFAULT_PORT: Final = 8400
DEFAULT_HOST: Final = "127.0.0.1"

BLUEPRINTS: Final = (
    local_console_blueprint,
    payment_routes.blueprint,
    health_routes.blueprint,
    fish_routes.blueprint,
    checkout_routes.blueprint,
    slack_interactivity.blueprint,
    slack_commands.blueprint,
    slack_events.blueprint,
    resend_routes.blueprint,
    twilio_routes.blueprint,
)


def _warn_on_open_endpoints() -> None:
    from src.voice.security import strict_mode

    if not settings.api_keys.slack_signing_secret and not strict_mode("SLACK_STRICT_SIGNATURE"):
        log.warning(
            "SLACK_SIGNING_SECRET is unset and SLACK_STRICT_SIGNATURE is disabled, so "
            "Slack endpoints will accept unsigned requests"
        )


def create_app() -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
    _warn_on_open_endpoints()
    from src.agents.background import start_background_agent

    start_background_agent()
    log.info("registered %d blueprints", len(BLUEPRINTS))
    return app


def main() -> int:
    logging.basicConfig(level=settings.log_level)
    port = int(os.getenv("WEBHOOK_PORT", str(DEFAULT_PORT)))
    host = os.getenv("WEBHOOK_HOST", DEFAULT_HOST)
    app = create_app()
    log.info("serving the web app on http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
