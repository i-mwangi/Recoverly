from __future__ import annotations

import html
import logging
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Final

from src.config import PROJECT_ROOT

log = logging.getLogger("recoverly.checkout.portal")

TEMPLATE_PATH: Final = PROJECT_ROOT / "web" / "checkout.html"
DEFAULT_INVOICE_NO: Final = "INV-UNKNOWN"
DEFAULT_BUYER_LABEL: Final = "Settlement portal"
USDC_DECIMALS: Final = 6

FALLBACK_TEMPLATE: Final = """\
<!doctype html>
<meta charset="utf-8">
<title>Settle {{invoice_no}}</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 16px/1.5 system-ui, sans-serif; margin: 0; padding: 3rem 1.5rem; }
  main { max-width: 32rem; margin: 0 auto; }
  h1 { font-size: 1.35rem; margin: 0 0 .25rem; }
  p.sub { margin: 0 0 2rem; opacity: .7; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: .5rem 1.5rem; margin: 0 0 2rem; }
  dt { opacity: .7; }
  dd { margin: 0; text-align: right; font-variant-numeric: tabular-nums; }
  .total { font-size: 1.25rem; font-weight: 600; }
</style>
<main>
  <h1>Settle invoice {{invoice_no}}</h1>
  <p class="sub">{{buyer_label}}</p>
  <dl>
    <dt>Invoice</dt><dd>{{invoice_no}}</dd>
    <dt>Case</dt><dd>{{case_id}}</dd>
    <dt class="total">Amount due</dt><dd class="total">${{amount}}</dd>
  </dl>
  <p>Payment options are arranged with your account contact. Quote {{invoice_no}} as the
  reference on any transfer.</p>
</main>
"""


@dataclass(frozen=True, slots=True)
class PortalContext:
    case_id: str
    invoice_no: str
    amount_usd: float
    buyer_label: str
    network: str
    receiving_account_id: str
    usdc_token_id: str
    payment_memo: str
    amount_atomic: int
    walletconnect_project_id: str

    def as_tokens(self) -> dict[str, str]:
        return {
            "case_id": self.case_id,
            "invoice_no": self.invoice_no,
            "amount": f"{self.amount_usd:,.2f}",
            "amount_raw": f"{self.amount_usd:.2f}",
            "buyer_label": self.buyer_label,
            "network": self.network,
            "receiving_account_id": self.receiving_account_id,
            "usdc_token_id": self.usdc_token_id,
            "payment_memo": self.payment_memo,
            "amount_atomic": str(self.amount_atomic),
            "walletconnect_project_id": self.walletconnect_project_id,
        }


def _first_consolidated_invoice(state: dict[str, Any]) -> str | None:
    consolidated = state.get("consolidated_invoices") or []
    if consolidated and isinstance(consolidated[0], dict):
        return consolidated[0].get("invoice_no")
    return None


def build_context(
    case_id: str, invoice_no: str | None = None, amount_usd: float | None = None
) -> PortalContext:
    from src.agents._case_state import load_case_state

    try:
        state = load_case_state(case_id)
    except Exception:
        state = {}

    from src.payments.channels import outstanding_for

    amount = outstanding_for(state)

    invoice = (
        state.get("invoice_no")
        or _first_consolidated_invoice(state)
        or DEFAULT_INVOICE_NO
    )

    buyer = state.get("buyer_legal_name") or state.get("customer_name")
    label = f"{buyer} · settlement portal" if buyer else DEFAULT_BUYER_LABEL
    amount_atomic = int(
        (Decimal(str(amount)) * Decimal(10**USDC_DECIMALS)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )

    return PortalContext(
        case_id=case_id,
        invoice_no=str(invoice),
        amount_usd=amount,
        buyer_label=label,
        network=(
            os.getenv("ONCHAIN_STABLECOIN_NETWORK", "").strip()
            or os.getenv("HEDERA_NETWORK", "testnet").strip()
        ).lower(),
        receiving_account_id=(
            os.getenv("ONCHAIN_STABLECOIN_RECEIVING_ACCOUNT", "").strip()
            or os.getenv("HEDERA_RECEIVING_ACCOUNT_ID", "").strip()
        ),
        usdc_token_id=(
            os.getenv("ONCHAIN_STABLECOIN_TOKEN_ID", "").strip()
            or os.getenv("HEDERA_USDC_TOKEN_ID", "").strip()
        ),
        payment_memo=f"recoverly:{case_id}",
        amount_atomic=amount_atomic,
        walletconnect_project_id=(
            os.getenv("ONCHAIN_STABLECOIN_WALLETCONNECT_PROJECT_ID", "").strip()
            or os.getenv("HEDERA_WALLETCONNECT_PROJECT_ID", "").strip()
        ),
    )


def load_template() -> str:
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        log.info("no checkout template at %s, using the built-in page", TEMPLATE_PATH)
        return FALLBACK_TEMPLATE


def render(context: PortalContext) -> str:
    page = load_template()
    for token, value in context.as_tokens().items():
        page = page.replace("{{" + token + "}}", html.escape(value))
    return page
