from __future__ import annotations

from typing import Final
import asyncio
import html
from decimal import Decimal

from flask import Blueprint, request, redirect

from src.agents._case_state import (
    InvalidCaseIdError,
    validate_case_id,
    case_path,
    load_case_state,
    update_case_state,
)
from src.payments.channels import build_provider, outstanding_for, dry_run_enabled
from src.payments.base import PaymentProviderError
from src.checkout.portal import build_context, render

blueprint = Blueprint("checkout", __name__)

HTML_CONTENT_TYPE: Final = "text/html; charset=utf-8"


@blueprint.route("/pay/<case_id>", methods=["GET"])
def settlement_portal(
    case_id: str,
) -> tuple[str, int, dict[str, str]] | tuple[str, int]:
    try:
        validate_case_id(case_id)
    except InvalidCaseIdError:
        return "That case reference is not valid.", 400

    if not case_path(case_id).is_file():
        return "Invoice not found.", 404

    context = build_context(
        case_id,
    )
    return render(context), 200, {"Content-Type": HTML_CONTENT_TYPE}


@blueprint.route("/pay/<case_id>/start", methods=["POST"])
def start_payment(case_id: str):
    try:
        if not case_path(case_id).is_file():
            return "Invoice not found.", 404
    except InvalidCaseIdError:
        return "Invalid case reference.", 400
    case = load_case_state(case_id)
    amount = outstanding_for(case)
    if amount <= 0 or case.get("halted_reason") or case.get("status") == "halted":
        return (
            "This invoice is not accepting payments through this page. Contact your account representative.",
            409,
        )
    method = request.form.get("method", "")
    if method not in {"card", "usdc", "wire", "ach"}:
        return "Choose a supported payment method.", 400
    try:
        if dry_run_enabled():
            return "Payment dry run is enabled. No payment was initiated.", 200
        provider = build_provider(method)
        configured = getattr(provider, "is_configured", None)
        if configured is None:
            configured = provider.beneficiary.is_configured
        if not configured:
            return (
                "This payment method is not configured. Contact your account representative.",
                503,
            )
        result = asyncio.run(
            provider.create_payment(
                Decimal(str(amount)),
                "USDC" if method == "usdc" else "USD",
                {
                    "case_id": case_id,
                    "customer_email": case.get("buyer_email")
                    or case.get("customer_email")
                    or "",
                },
            )
        )
    except (PaymentProviderError, ValueError, RuntimeError):
        return (
            "Payment could not be started. Contact your account representative or try again later.",
            503,
        )
    if result.checkout_url:
        if not result.checkout_url.startswith("https://"):
            return "Payment provider returned an invalid checkout address.", 502
        update_case_state(
            case_id,
            payment_reference=result.metadata.get("paystack_reference"),
            payment_id=result.payment_id,
        )
        return redirect(result.checkout_url, code=303)
    details = result.metadata
    fields = (
        ("Network", details.get("network")),
        ("Receiving account", details.get("receiving_account_id")),
        ("USDC token", details.get("token_id")),
        ("Memo", details.get("memo")),
        ("Beneficiary", details.get("beneficiary_name")),
        ("Bank", details.get("bank_name")),
        ("Account", details.get("account_number")),
        ("SWIFT/BIC", details.get("swift_bic")),
        ("Routing code", details.get("routing_or_branch_code")),
        ("Reference", details.get("payment_reference")),
    )
    rows = "".join(
        f"<dt>{label}</dt><dd>{html.escape(str(value))}</dd>"
        for label, value in fields
        if value
    )
    return (
        f'<!doctype html><meta charset="utf-8"><title>Payment instructions</title><main><h1>Payment instructions</h1><p>Amount: {amount:,.2f} {result.currency}</p><dl>{rows}</dl><p>Use the exact reference above. Payment is pending until verified.</p></main>',
        200,
        {"Content-Type": HTML_CONTENT_TYPE},
    )
