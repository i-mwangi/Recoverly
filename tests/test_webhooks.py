from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time



def _resend_headers(body):
    ts = str(int(time.time()))
    message_id = "msg-test"
    signed = message_id.encode() + b"." + ts.encode() + b"." + body
    signature = base64.b64encode(
        hmac.new(b"testresendsecret", signed, hashlib.sha256).digest()
    ).decode()
    return {
        "svix-id": message_id,
        "svix-timestamp": ts,
        "svix-signature": "v1," + signature,
    }


def _slack_signed(client, path, form_body, secret="testsigningsecret"):
    ts = str(int(time.time()))
    raw = form_body.encode() if isinstance(form_body, str) else form_body
    signature = (
        "v0="
        + hmac.new(
            secret.encode(), b"v0:" + ts.encode() + b":" + raw, hashlib.sha256
        ).hexdigest()
    )
    return client.post(
        path,
        data=raw,
        headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": signature},
    )


class TestSlackInteractivity:
    def test_rejects_unsigned(self, flask_client):
        response = flask_client.post("/slack/interactivity", data=b"payload={}")
        assert response.status_code == 401

    def test_dispatches_signed_action(self, flask_client, isolated_data):
        payload = {
            "type": "block_actions",
            "user": {"username": "dana"},
            "channel": {"id": "C1"},
            "message": {"ts": "1.0"},
            "actions": [
                {"action_id": "lite_writeoff_RC-2026-0042", "value": "RC-2026-0042"}
            ],
        }
        response = _slack_signed(
            flask_client,
            "/slack/interactivity",
            b"payload=" + json.dumps(payload).encode(),
        )
        assert response.status_code == 200
        body = response.get_json()
        assert "written off" in body["text"]

    def test_unknown_action_reports_gracefully(self, flask_client, isolated_data):
        payload = {
            "type": "block_actions",
            "user": {"username": "dana"},
            "channel": {"id": "C1"},
            "actions": [{"action_id": "not_wired_yet", "value": "RC-1"}],
        }
        body = _slack_signed(
            flask_client,
            "/slack/interactivity",
            b"payload=" + json.dumps(payload).encode(),
        ).get_json()
        assert "not wired" in body["text"]


class TestSlackEvents:
    def test_url_verification_answered(self, flask_client):
        response = flask_client.post(
            "/slack/events",
            data=json.dumps({"type": "url_verification", "challenge": "abc"}).encode(),
            content_type="application/json",
        )
        assert response.get_json() == {"challenge": "abc"}


class TestTwilioCallback:
    def test_rejects_unsigned(self, flask_client):
        response = flask_client.post(
            "/voice-webhook/call-status", data={"CallSid": "CA1"}
        )
        assert response.status_code == 401

    def test_accepts_signed_completion(self, flask_client, isolated_data):
        form = {
            "CallSid": "CA123",
            "CallStatus": "completed",
            "CallDuration": "45",
            "case_id": "RC-2026-0042",
        }
        url = "http://localhost/voice-webhook/call-status"
        payload = url + "".join(k + form[k] for k in sorted(form))
        signature = base64.b64encode(
            hmac.new(b"testtwiliotoken", payload.encode(), hashlib.sha1).digest()
        ).decode()
        response = flask_client.post(
            "/voice-webhook/call-status",
            data=form,
            headers={"X-Twilio-Signature": signature},
        )
        assert response.status_code == 200
        assert response.get_json()["card_kind"] == "voice_call_summary"


class TestResendWebhook:
    def test_rejects_unsigned(self, flask_client):
        response = flask_client.post("/webhooks/resend", data=b"{}")
        assert response.status_code == 401

    def test_signed_delivery_event(self, flask_client, isolated_data):
        body = json.dumps(
            {
                "type": "email.delivered",
                "data": {
                    "email_id": "msg-1",
                    "to": "buyer@example.com",
                    "tags": [{"name": "case_id", "value": "RC-2026-0042"}],
                },
            }
        ).encode()
        sig = hmac.new(b"testresendsecret", body, hashlib.sha256).hexdigest()
        response = flask_client.post(
            "/webhooks/resend", data=body, headers=_resend_headers(body)
        )
        assert response.status_code == 200
        assert response.get_json()["event"] == "email.delivered"

    def test_signed_inbound_reply(self, flask_client, isolated_data):
        body = json.dumps(
            {
                "type": "email.received",
                "data": {
                    "from": "ap@buyer.com",
                    "text": "we will wire the payment friday",
                    "tags": [
                        {"name": "case_id", "value": "RC-2026-0042"},
                        {"name": "buyer", "value": "abc_trading"},
                    ],
                },
            }
        ).encode()
        sig = hmac.new(b"testresendsecret", body, hashlib.sha256).hexdigest()
        response = flask_client.post(
            "/webhooks/resend", data=body, headers=_resend_headers(body)
        )
        assert response.get_json()["intents"]["promise_to_pay"] is True


class TestCheckoutPortal:
    def test_renders_case(self, flask_client, isolated_data):
        from src.agents._case_state import update_case_state

        update_case_state(
            "RC-2026-0042", invoice_no="INV-1", outstanding_balance_usd=8750
        )
        response = flask_client.get("/pay/RC-2026-0042?inv=INV-1&amt=8750")
        assert response.status_code == 200
        assert b"INV-1" in response.data

    def test_rejects_traversal(self, flask_client):
        response = flask_client.get("/pay/..%2F..%2Fetc%2Fpasswd")
        assert response.status_code in (400, 404)


class TestHealth:
    def test_health_ok(self, flask_client):
        assert flask_client.get("/health").get_json()["status"] == "ok"
