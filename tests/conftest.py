from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    import socket
    from dataclasses import replace

    connect = socket.socket.connect

    def no_network(sock, address):
        if isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return connect(sock, address)
        raise AssertionError(
            "External network is disabled in tests; mock the transport"
        )

    monkeypatch.setattr(socket.socket, "connect", no_network)
    for key in (
        "SLACK_BOT_TOKEN",
        "SLACK_SIGNING_SECRET",
        "TWILIO_AUTH_TOKEN",
        "RESEND_WEBHOOK_SECRET",
        "STRANDS_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)

    from src.agents import _case_state
    from src.concierge import audit_logger, buyer_send, email_intake, inbound
    from src.config import settings
    from src.payments import dedup
    from src.voice import events

    cases = tmp_path / "cases"
    cases.mkdir()
    audit_trail = tmp_path / "audit_trail.jsonl"

    monkeypatch.setattr(_case_state, "cases_dir", lambda: cases)
    monkeypatch.setattr(_case_state, "audit_trail_path", lambda: audit_trail)
    monkeypatch.setattr(dedup, "DEFAULT_PATH", tmp_path / "processed.json")
    monkeypatch.setattr(
        audit_logger, "audit_log", audit_logger.AuditLogger(path=audit_trail)
    )
    monkeypatch.setattr(inbound, "INBOUND_QUEUE", tmp_path / "inbound.jsonl")
    monkeypatch.setattr(email_intake, "INBOUND_QUEUE", tmp_path / "email-intake.jsonl")
    monkeypatch.setattr(events, "AGENT_EVENT_OUTBOX", tmp_path / "voice-outbox.jsonl")
    monkeypatch.setenv("STRANDS_ENABLED", "0")
    monkeypatch.setenv("AUTONOMOUS_AGENT_ENABLED", "0")
    monkeypatch.setattr(
        events,
        "settings",
        replace(settings, data=replace(settings.data, audit_trail_jsonl=audit_trail)),
    )
    monkeypatch.setattr(
        buyer_send,
        "settings",
        replace(settings, data=replace(settings.data, audit_trail_jsonl=audit_trail)),
    )
    monkeypatch.setenv("CASE_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("PAYSTACK_CHARGE_CURRENCY", raising=False)
    monkeypatch.delenv("PAYSTACK_USD_RATE", raising=False)

    return tmp_path


@pytest.fixture
def flask_client(monkeypatch, isolated_data):
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "testsigningsecret")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "testtwiliotoken")
    import base64

    monkeypatch.setenv(
        "RESEND_WEBHOOK_SECRET",
        "whsec_" + base64.b64encode(b"testresendsecret").decode(),
    )
    monkeypatch.setenv("ELEVENLABS_WEBHOOK_SECRET", "testelevenlabssecret")
    from src.webapp import create_app

    return create_app().test_client()


import pytest_asyncio  # noqa: F401
