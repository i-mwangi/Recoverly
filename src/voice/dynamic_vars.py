from __future__ import annotations

import json
import logging
from typing import Any, Final

from src.agents._case_state import case_path, load_case_state
from src.agents._utils import safe_float
from src.config import settings
from src.voice.speech import amount_to_words, date_to_words, number_to_words

log = logging.getLogger("recoverly.voice.dynamic_vars")

DEFAULT_ROLE: Final = "Accounts Payable team member"
DEFAULT_OPERATOR_NAME: Final = "Alex"
DEFAULT_PATTERN_TAG: Final = "clean_history"
DEFAULT_ESCALATION_DEADLINE: Final = "this Friday"
DEFAULT_RECORDING_CONSENT: Final = "none"


class UnknownCaseError(ValueError):
    pass


def _load_json_list(path) -> list[dict[str, Any]]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"required fixture is missing: {path}") from None
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"fixture {path} is unreadable: {error}") from error

    if isinstance(loaded, dict):
        for key in ("cases", "customers"):
            if isinstance(loaded.get(key), list):
                return [item for item in loaded[key] if isinstance(item, dict)]
        return []
    return [item for item in loaded if isinstance(item, dict)]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def resolve_amounts(case: dict[str, Any]) -> tuple[float, float, float]:
    gross = max(0.0, safe_float(case.get("amount_usd"), 0.0))

    deposit_pct = safe_float(case.get("deposit_pct"), 0.0)
    if not 0.0 <= deposit_pct < 1.0:
        deposit_pct = 0.0

    outstanding = safe_float(case.get("outstanding_balance_usd"), 0.0)
    if outstanding <= 0 and gross > 0:
        outstanding = round(gross * (1.0 - deposit_pct), 2)
    outstanding = max(0.0, outstanding)

    return gross, outstanding, max(0.0, round(gross - outstanding, 2))


def _prior_invoice_summary(history: dict[str, Any]) -> str:
    prior = _as_int(history.get("prior_invoices"))
    if prior <= 0:
        return "first invoice with this customer"

    average = _as_int(history.get("avg_days_late", history.get("avg_days_to_pay")))
    if average <= 0:
        return f"{number_to_words(prior)} prior invoices, all paid on time"
    return (
        f"{number_to_words(prior)} prior invoices, paid an average of "
        f"{number_to_words(average)} days late"
    )


def build_dynamic_vars(
    case: dict[str, Any], history: dict[str, Any] | None = None
) -> dict[str, str]:
    gross, outstanding, deposit_amount = resolve_amounts(case)
    history = history or {}
    full_name = str(
        case.get("customer_full_name")
        or case.get("buyer_name")
        or case.get("customer_name")
        or ""
    )
    first_name = str(
        case.get("customer_first_name")
        or case.get("buyer_first_name")
        or (full_name.split()[0] if full_name else "")
    )

    return {
        "customer_first_name": first_name,
        "customer_full_name": full_name,
        "customer_company": str(case.get("customer_company", "")),
        "customer_role": str(case.get("customer_role", DEFAULT_ROLE)),
        "invoice_id": str(case.get("invoice_id", case.get("invoice_no", ""))),
        "invoice_gross": f"${gross:,.2f}",
        "invoice_outstanding": f"${outstanding:,.2f}",
        "invoice_outstanding_spoken": amount_to_words(outstanding),
        "deposit_amount_spoken": amount_to_words(deposit_amount) if deposit_amount else "",
        "po_id": str(case.get("po_id", "")),
        "due_date_spoken": date_to_words(case.get("due_date", "")),
        "days_past_due": str(_as_int(case.get("days_past_due"))),
        "prior_outreach_summary": str(
            case.get(
                "prior_outreach_summary",
                f"{number_to_words(_as_int(case.get('emails_sent')))} emails since the "
                f"original due date",
            )
        ),
        "escalation_deadline_spoken": str(
            case.get("escalation_deadline_spoken", DEFAULT_ESCALATION_DEADLINE)
        ),
        "pattern_tag": str(case.get("pattern_tag", DEFAULT_PATTERN_TAG)),
        "prior_invoices_paid_summary": _prior_invoice_summary(history),
        "recent_excuse_summary": str(case.get("recent_excuse_summary", "")),
        "operator_first_name": str(case.get("operator_first_name", DEFAULT_OPERATOR_NAME)),
        "human_callback_offered": str(case.get("human_callback_offered", True)).lower(),
        "customer_state_recording_consent": str(
            case.get("customer_state_recording_consent", DEFAULT_RECORDING_CONSENT)
        ),
    }


def fetch_case_dynamic_vars(case_id: str) -> dict[str, str]:
    """Use the live case state when available, then fall back to fixture data."""
    live_path = case_path(case_id)
    if live_path.exists():
        case = load_case_state(case_id)
    else:
        cases = _load_json_list(settings.data.cases_json)
        case = next((entry for entry in cases if entry.get("case_id") == case_id), None)
    if case is None:
        raise UnknownCaseError(f"unknown case_id: {case_id}")

    customers = _load_json_list(settings.data.customer_history_json)
    history = next(
        (
            entry
            for entry in customers
            if entry.get("customer_id") and entry["customer_id"] == case.get("customer_id")
        ),
        None,
    )

    return build_dynamic_vars(case, history)
