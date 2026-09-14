"""Paystack's signed charge.success event is the authority for card settlement."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request

from src.payments.paystack import charge_currency, usd_rate
from src.payments.reconciler import reconcile_inbound
from src.payments.reconciler import SettlementStatus

blueprint = Blueprint("payment_webhooks", __name__)
log = logging.getLogger("recoverly.payments.webhooks")


@blueprint.post("/webhooks/paystack")
def paystack_webhook():
    secret = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
    raw = request.get_data()
    signature = request.headers.get("x-paystack-signature", "")
    if not secret or not hmac.compare_digest(
        hmac.new(secret.encode(), raw, hashlib.sha512).hexdigest(), signature
    ):
        return jsonify(error="unauthorized"), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error="invalid payload"), 400
    if payload.get("event") != "charge.success":
        return jsonify(ok=True, ignored=True), 200
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("status") != "success":
        return jsonify(error="invalid charge"), 400
    currency = data.get("currency")
    if currency not in {"USD", charge_currency()}:
        return jsonify(error="unsupported settlement currency"), 422
    metadata = data.get("metadata")
    if (
        not isinstance(metadata, dict)
        or not metadata.get("case_id")
        or not data.get("reference")
    ):
        return jsonify(error="missing payment reference or case metadata"), 400
    try:
        amount = Decimal(str(data["amount"])) / 100
        if currency != "USD":
            amount = (amount / usd_rate()).quantize(Decimal("0.01"))
        if not amount.is_finite() or amount <= 0:
            raise ValueError("invalid amount")
    except (KeyError, InvalidOperation, ValueError):
        return jsonify(error="invalid amount"), 400
    result = reconcile_inbound(
        str(metadata["case_id"]), str(amount), "paystack:" + str(data["reference"])
    )
    if result.status is not SettlementStatus.UNMATCHED and result.amount_applied_usd:
        try:
            _publish_card_receipt(str(metadata["case_id"]), str(data["reference"]), result)
        except Exception:
            log.exception("card receipt notification failed for %s", metadata["case_id"])
    return jsonify(ok=True, **result.as_dict()), 200


def _publish_card_receipt(case_id, reference, result):
    from src.concierge.cards import post_card
    from src.payments.notify import send_payment_received
    from src.agents._case_state import load_case_state
    from src.local_console.service import append_case_activity
    receipt = (
        f"Payment Agent confirmed a Paystack card payment of ${result.amount_applied_usd:,.2f}.\n\n"
        f"Status: {result.status.value}\n"
        f"Remaining balance: ${result.remaining_usd:,.2f}\n"
        f"Reference: {reference}"
    )
    case = load_case_state(case_id)
    post_card(
        case_id,
        f"Payment received for case {case_id}",
        receipt,
        card_kind="payment_received",
        case_meta=case,
    )
    append_case_activity(
        case_id,
        "payment",
        receipt,
        reasoning="Verified the signed Paystack webhook, reconciled the balance, and sent the settlement notification.",
    )
    send_payment_received(
        case_id=case_id,
        buyer_name=str(case.get("customer_name") or ""),
        invoice_no=str(case.get("invoice_no") or case_id),
        amount_paid=result.amount_applied_usd,
        remaining=result.remaining_usd,
    )
