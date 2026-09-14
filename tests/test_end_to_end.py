"""Local integration tests: real workflow functions, fake external transports."""

import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock

import pytest

from src.agents._case_state import load_case_state, update_case_state, case_path
from src.payments.reconciler import reconcile_inbound, SettlementStatus

CASE = "RC-2026-9001"


def charge(client, amount, reference="test-1", currency="USD", case_id=CASE):
    payload = {
        "event": "charge.success",
        "data": {
            "status": "success",
            "reference": reference,
            "currency": currency,
            "amount": amount,
            "metadata": {"case_id": case_id},
        },
    }
    raw = json.dumps(payload).encode()
    return client.post(
        "/webhooks/paystack",
        data=raw,
        content_type="application/json",
        headers={
            "x-paystack-signature": hmac.new(
                b"test-key", raw, hashlib.sha512
            ).hexdigest()
        },
    )


def test_invoice_to_approved_email_to_card_settlement(
    flask_client, monkeypatch, isolated_data
):
    from src.agents.preflight_agent import PreflightAdapter
    from src.agents.investigator_agent import InvestigatorAdapter
    from src.agents.diplomat_agent import DiplomatAdapter
    from src.agents.tone_coach_agent import ToneCoachAdapter
    from src.concierge.actions.base import dispatch
    from src.agents import (
        preflight_agent,
        investigator_agent,
        diplomat_agent,
        tone_coach_agent,
    )
    from src import outbound
    from src.payments import paystack
    from src.agents.strands_runtime import AgentDispatchResult

    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test-key")
    monkeypatch.delenv("PAYMENT_DRY_RUN", raising=False)
    monkeypatch.setenv("CASE_LOCK_DIR", str(isolated_data / "locks"))
    usage = {"cost_usd": 0.001}
    monkeypatch.setattr(
        preflight_agent,
        "complete_async",
        AsyncMock(
            return_value=(
                json.dumps(
                    {
                        "governing_law": "California",
                        "forum_clause": "AAA",
                        "late_fee_rate": "1%",
                    }
                ),
                usage,
            )
        ),
    )
    monkeypatch.setattr(
        investigator_agent,
        "complete_async",
        AsyncMock(return_value=('{"rationale":"Clean payment history."}', usage)),
    )
    monkeypatch.setattr(
        investigator_agent, "post_card", lambda **kw: AgentDispatchResult(ok=True)
    )
    monkeypatch.setattr(
        diplomat_agent,
        "complete_async",
        AsyncMock(
            return_value=(
                "Please arrange payment of the overdue invoice using {{CHECKOUT_URL}}. Thank you.",
                usage,
            )
        ),
    )
    monkeypatch.setattr(
        tone_coach_agent,
        "complete_async",
        AsyncMock(
            return_value=('{"verdict":"approve","issues":[],"revised":""}', usage)
        ),
    )
    sent = []

    def send(draft):
        sent.append(draft)
        return {"id": "email-1", "to": draft.to_email, "backend": "test"}

    monkeypatch.setattr(outbound, "send_buyer_outbound", send)
    case = {
        "case_id": CASE,
        "amount_usd": 10000,
        "deposit_pct": 0.2,
        "outstanding_balance_usd": 8000,
        "customer_name": "Test Buyer",
        "buyer_email": "buyer@example.com",
        "invoice_no": "INV-9001",
        "contract_excerpt": "Payment due net 30; California law; AAA arbitration.",
        "days_past_due": 7,
    }

    async def pipeline():
        text = json.dumps(case)
        for adapter in (
            PreflightAdapter(),
            InvestigatorAdapter(),
            DiplomatAdapter(),
            ToneCoachAdapter(),
        ):
            text = await adapter.handle_message(text, None, None, None, "test-room")
            assert text
        return text

    result = asyncio.run(pipeline())
    assert "concierge" in result
    assert not sent
    assert load_case_state(CASE)["buyer_email"] == "buyer@example.com"
    result = dispatch(
        {"action_id": "hitl_approve", "value": CASE}, {"user": {"username": "test"}}
    )
    assert result.seal_card
    assert len(sent) == 1
    assert "{{CHECKOUT_URL}}" not in sent[0].body_text
    dispatch(
        {"action_id": "hitl_approve", "value": CASE}, {"user": {"username": "test"}}
    )
    assert len(sent) == 1
    page = flask_client.get(f"/pay/{CASE}?amt=1&inv=FAKE")
    assert b"8,000.00" in page.data and b"FAKE" not in page.data
    requests = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "status": True,
                "data": {"authorization_url": "https://checkout.paystack.com/test"},
            }

    def initialize(url, **kwargs):
        requests.append(kwargs["json"])
        return Response()

    monkeypatch.setattr(paystack.httpx, "post", initialize)
    payment = flask_client.post(
        f"/pay/{CASE}/start", data={"method": "card", "amount": "1"}
    )
    assert payment.status_code == 303
    assert requests[0]["amount"] == 800000
    assert requests[0]["email"] == "buyer@example.com"
    assert charge(flask_client, 300000).get_json()["status"] == "partial"
    assert charge(flask_client, 300000).get_json()["amount_applied_usd"] == 0
    assert b"5,000.00" in flask_client.get(f"/pay/{CASE}").data
    assert charge(flask_client, 500000, "test-2").get_json()["status"] == "settled"
    assert load_case_state(CASE)["status"] == "closed_recovered"
    assert load_case_state(CASE)["amount_paid_usd"] == 8000
    assert (
        flask_client.post(f"/pay/{CASE}/start", data={"method": "card"}).status_code
        == 409
    )
    result = dispatch({"action_id": "hitl_approve", "value": CASE}, {})
    assert not result.seal_card
    assert len(sent) == 1


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity", "0", "-1"])
def test_invalid_payment_is_not_recorded(isolated_data, amount):
    update_case_state(CASE, outstanding_balance_usd=8000)
    assert reconcile_inbound(CASE, amount, "bad").status == SettlementStatus.UNMATCHED
    assert load_case_state(CASE)["amount_paid_usd"] == 0


def test_unknown_case_cannot_be_settled(isolated_data):
    assert reconcile_inbound(CASE, 100, "unknown").status == SettlementStatus.UNMATCHED
    assert not case_path(CASE).exists()


def test_payment_webhook_security(flask_client, monkeypatch):
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "test-key")
    update_case_state(CASE, outstanding_balance_usd=8000)
    assert flask_client.post("/webhooks/paystack", json={}).status_code == 401
    assert charge(flask_client, 10000, currency="KES").status_code == 422
    assert load_case_state(CASE)["amount_paid_usd"] == 0


def test_unknown_checkout_returns_404(flask_client):
    assert flask_client.get(f"/pay/{CASE}").status_code == 404


def test_agent_payment_confirmation_preserves_partial_balance(isolated_data):
    from src.agents.payment_agent import PaymentAdapter

    update_case_state(CASE, outstanding_balance_usd=8000, status="active")
    text = json.dumps(
        {
            "event": "payment_received",
            "case_id": CASE,
            "amount_usd": 3000,
            "transaction_id": "partial",
        }
    )
    PaymentAdapter()._confirm_payment(text)
    PaymentAdapter()._confirm_payment(text)
    state = load_case_state(CASE)
    assert state["status"] == "active"
    assert state["amount_paid_usd"] == 3000


def test_resend_rejects_stale_signature(flask_client):
    from tests.test_webhooks import _resend_headers

    body = b"{}"
    headers = _resend_headers(body)
    headers["svix-timestamp"] = "1"
    assert (
        flask_client.post("/webhooks/resend", data=body, headers=headers).status_code
        == 401
    )


def test_resend_fetches_body_and_matches_invoice(flask_client, monkeypatch):
    from src.concierge.routes import resend
    from tests.test_webhooks import _resend_headers

    update_case_state(CASE, invoice_no="INV-2026-9001")
    monkeypatch.setenv("RESEND_API_KEY", "test")

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "text": "We will pay Friday",
                "subject": "Re: INV-2026-9001",
                "from": "buyer@example.com",
            }

    monkeypatch.setattr(resend.httpx, "get", lambda *args, **kwargs: Response())
    raw = json.dumps(
        {"type": "email.received", "data": {"email_id": "email-1"}}
    ).encode()
    result = flask_client.post(
        "/webhooks/resend", data=raw, headers=_resend_headers(raw)
    )
    assert result.status_code == 200
    assert result.get_json()["intents"]["promise_to_pay"]


def test_reconciliation_replay_preserves_balance_and_dedup(isolated_data):
    from src.agents._case_state import rebuild_all_from_audit

    update_case_state(CASE, outstanding_balance_usd=8000)
    reconcile_inbound(CASE, 3000, "replay-1")
    rebuild_all_from_audit()
    state = load_case_state(CASE)
    assert state["amount_balance_usd"] == 5000
    assert state["status"] == "active"
    assert reconcile_inbound(CASE, 3000, "replay-1").amount_applied_usd == 0


def test_agent_dispatch_stays_off_without_configuration(monkeypatch):
    from src.agents.strands_runtime import dispatch_agent

    monkeypatch.setenv("STRANDS_ENABLED", "0")
    result = dispatch_agent("preflight", "Review the case", case_id=CASE)
    assert result.ok is False
    assert "disabled" in result.skipped_reason


def test_manual_command_partial_and_retries(flask_client):
    from src.concierge.routes.commands import _payment_received

    update_case_state(CASE, outstanding_balance_usd=8000)
    with flask_client.application.app_context():
        params = {"case": CASE, "amount": "3000", "reference": "bank-1"}
        _payment_received("operator", params)
        _payment_received("operator", params)
    state = load_case_state(CASE)
    assert state["amount_paid_usd"] == 3000
    assert state["status"] != "closed_recovered"


@pytest.mark.parametrize("method", ["wire", "usdc"])
def test_transfer_instructions_render(flask_client, monkeypatch, method):
    update_case_state(CASE, outstanding_balance_usd=8000)
    monkeypatch.delenv("PAYMENT_DRY_RUN", raising=False)
    monkeypatch.setenv("WIRE_BANK_NAME", "Test Bank")
    monkeypatch.setenv("WIRE_SWIFT_BIC", "TESTSWIFT")
    monkeypatch.setenv("WIRE_ACCOUNT_NUMBER", "123456")
    monkeypatch.setenv("HEDERA_RECEIVING_ACCOUNT_ID", "0.0.12345")
    response = flask_client.post(f"/pay/{CASE}/start", data={"method": method})
    assert response.status_code == 200
    assert (b"Test Bank" if method == "wire" else b"0.0.12345") in response.data
    assert b"8,000.00" in response.data


def test_unconfigured_card_reports_unavailable(flask_client, monkeypatch):
    update_case_state(CASE, outstanding_balance_usd=8000)
    monkeypatch.delenv("PAYMENT_DRY_RUN", raising=False)
    monkeypatch.delenv("PAYSTACK_SECRET_KEY", raising=False)
    assert (
        flask_client.post(f"/pay/{CASE}/start", data={"method": "card"}).status_code
        == 503
    )


def test_checkout_renders_hashpack_wallet_configuration(flask_client, monkeypatch):
    update_case_state(CASE, outstanding_balance_usd=20)
    monkeypatch.setenv("HEDERA_NETWORK", "testnet")
    monkeypatch.setenv("HEDERA_RECEIVING_ACCOUNT_ID", "0.0.12345")
    monkeypatch.setenv("HEDERA_USDC_TOKEN_ID", "0.0.429274")
    monkeypatch.setenv("HEDERA_WALLETCONNECT_PROJECT_ID", "walletconnect-project")
    response = flask_client.get(f"/pay/{CASE}")
    assert response.status_code == 200
    assert b"Pay USDC with HashPack" in response.data
    assert b"Pay by card with Paystack" not in response.data
    assert b"walletconnect-project" in response.data
    assert b"recoverly:" in response.data


def test_settlement_cannot_debit_a_different_operator(isolated_data, monkeypatch):
    from src.payments import settlement, hedera_client

    monkeypatch.setattr(settlement, "hedera_is_configured", lambda: True)
    monkeypatch.setattr(settlement, "receiving_account_id", lambda: "0.0.999")
    monkeypatch.setattr(settlement, "buyer_account_id", lambda _: "0.0.123")
    monkeypatch.setattr(hedera_client, "operator_account_id", lambda: "0.0.456")
    with pytest.raises(settlement.SettlementError, match="signing account"):
        asyncio.run(settlement.settle_from_buyer_async(CASE, "buyer", "100"))
