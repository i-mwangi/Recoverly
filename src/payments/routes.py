"""Paystack's signed charge.success event is the authority for card settlement."""

from __future__ import annotations

import hashlib
import hmac
import os
from decimal import Decimal, InvalidOperation

from flask import Blueprint, jsonify, request

from src.payments.reconciler import reconcile_inbound

blueprint = Blueprint("payment_webhooks", __name__)


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
    # The case ledger is denominated in USD. Never silently treat KES/NGN as USD.
    if data.get("currency") != "USD":
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
        if not amount.is_finite() or amount <= 0:
            raise ValueError("invalid amount")
    except (KeyError, InvalidOperation, ValueError):
        return jsonify(error="invalid amount"), 400
    result = reconcile_inbound(
        str(metadata["case_id"]), str(amount), "paystack:" + str(data["reference"])
    )
    return jsonify(ok=True, **result.as_dict()), 200
