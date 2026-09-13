from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from src.agents._case_state import load_case_state, update_case_state
from src.agents._utils import safe_float
from src.diplomat.templates import build_paylink, render_day7
from src.concierge.cards import post_card
from src.preflight.amount_router import select_path
from src.preflight.pdf_extractor import extract
from src.preflight.intake_pairing import pair_or_pend

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "data" / "local_console" / "sessions.json"
LOCK = Lock()
AGENTS = (
    {"id": "preflight", "name": "Preflight", "description": "Checks case data, payment terms, and the recommended path."},
    {"id": "investigator", "name": "Investigator", "description": "Reviews the payment pattern and selects an opening tone."},
    {"id": "diplomat", "name": "Diplomat", "description": "Prepares the collection email and payment request."},
    {"id": "tone_coach", "name": "Tone Coach", "description": "Reviews the draft for clarity and professional tone."},
    {"id": "concierge", "name": "Concierge", "description": "Coordinates approvals, dispatch, and exceptions."},
    {"id": "payment", "name": "Payment", "description": "Tracks settlement and opens the secure payment portal."},
    {"id": "voice", "name": "Voice Agent", "description": "Places a compliant, conversational payment call."},
    {"id": "escalator", "name": "Escalator", "description": "Prepares the next action for unresolved cases."},
    {"id": "aaa", "name": "AAA Specialist", "description": "Prepares an arbitration strategy when escalation requires it."},
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read() -> dict[str, Any]:
    try:
        value = json.loads(STORE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {"sessions": []}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"sessions": []}


def _write(value: dict[str, Any]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    temp = STORE.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(STORE)


def list_sessions() -> list[dict[str, Any]]:
    with LOCK:
        sessions = _read().get("sessions", [])
        return [{key: item.get(key) for key in ("id", "title", "updated_at", "preview")} for item in sessions]


def get_session(session_id: str) -> dict[str, Any] | None:
    with LOCK:
        for session in _read().get("sessions", []):
            if session.get("id") == session_id:
                return session
    return None


def create_session() -> dict[str, Any]:
    session = {"id": str(uuid4()), "title": "New recovery session", "created_at": _now(), "updated_at": _now(), "preview": "Start a case workflow", "messages": []}
    with LOCK:
        data = _read()
        data.setdefault("sessions", []).insert(0, session)
        _write(data)
    return session


def _save_session(session: dict[str, Any]) -> None:
    with LOCK:
        data = _read()
        for index, item in enumerate(data.setdefault("sessions", [])):
            if item.get("id") == session["id"]:
                data["sessions"][index] = session
                break
        else:
            data["sessions"].insert(0, session)
        _write(data)


def _message(
    actor: str,
    body: str,
    *,
    actions: list[dict[str, str]] | None = None,
    case_id: str = "",
    reasoning: str = "",
) -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "actor": actor,
        "body": body,
        "actions": actions or [],
        "case_id": case_id,
        "reasoning": reasoning,
        "created_at": _now(),
    }


def _case_from_text(text: str) -> dict[str, Any]:
    start = text.find("{")
    if start < 0:
        raise ValueError("Include a JSON case payload after @preflight.")
    try:
        value = json.loads(text[start:])
    except json.JSONDecodeError as error:
        raise ValueError(f"The case JSON is invalid: {error.msg}.") from None
    if not isinstance(value, dict):
        raise ValueError("The case payload must be a JSON object.")
    if not value.get("case_id"):
        raise ValueError("The case payload needs a case_id.")
    return value


def _clauses(case: dict[str, Any]) -> dict[str, Any]:
    excerpt = str(case.get("contract_excerpt") or "")
    governing = "New York" if re.search(r"laws? of New York|New York law", excerpt, re.I) else "UNKNOWN"
    payment_terms = re.search(r"within (\d+) days", excerpt, re.I)
    late_fee = re.search(r"late fee of ([\d.]+%[^.\n]*)", excerpt, re.I)
    arbitration = "AAA Commercial Arbitration Rules" if re.search(r"AAA|arbitration", excerpt, re.I) else "UNKNOWN"
    values = {
        "governing_law": governing,
        "forum_clause": "UNKNOWN",
        "late_fee_rate": late_fee.group(1) if late_fee else "UNKNOWN",
        "dispute_window_days": "UNKNOWN",
        "payment_terms": f"{payment_terms.group(1)} days" if payment_terms else "UNKNOWN",
        "fees_clause": "UNKNOWN",
        "arbitration_clause": arbitration,
    }
    values["missing"] = [key for key, value in values.items() if value == "UNKNOWN"]
    return values


def _snapshot(case_id: str) -> dict[str, Any]:
    return {
        "customer_id": case_id,
        "lookback_months": 6,
        "days_since_onboarded": 0,
        "invoices_in_window": 0,
        "late_payment_count": 0,
        "avg_days_late": 0.0,
        "max_days_late": 0,
        "walked_to_escalator": False,
        "paid_early_count": 0,
        "previous_extensions_granted": 0,
        "has_partial_payment_pattern": False,
    }


def _pretty(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def _draft(case: dict[str, Any]) -> str:
    first_name = str(case.get("buyer_first_name") or str(case.get("customer_name") or "Customer").split(" ")[0])
    invoice = str(case.get("invoice_no") or "INV-UNKNOWN")
    due_date = str(case.get("due_date") or "the original due date")
    balance = safe_float(case.get("outstanding_balance_usd"), safe_float(case.get("amount_usd"), 0.0))
    return (
        f"Hi {first_name},\n\nThis is a reminder that invoice {invoice} for ${balance:,.2f} "
        f"was due on {due_date}. Please remit payment within five business days, or reply "
        "if there is a discrepancy or you need an updated invoice.\n\n"
        "Best regards,\nRecoverly Collections"
    )


def run_case_workflow(session: dict[str, Any], text: str) -> list[dict[str, Any]]:
    case = _case_from_text(text)
    case_id = str(case["case_id"])
    amount = safe_float(case.get("outstanding_balance_usd"), safe_float(case.get("amount_usd"), 0.0))
    if amount <= 0:
        raise ValueError("The case needs a positive outstanding_balance_usd or amount_usd.")
    decision = select_path(amount, gross_amount_usd=safe_float(case.get("amount_usd"), amount), deposit_pct=safe_float(case.get("deposit_pct"), 0.0))
    clauses = _clauses(case)
    snapshot = _snapshot(case_id)
    invoice = str(case.get("invoice_no") or "INV-UNKNOWN")
    name = str(case.get("buyer_first_name") or str(case.get("customer_name") or "Customer").split(" ")[0])
    paylink = build_paylink(case_id, invoice, amount, decision.mode)
    draft = _draft(case) + f"\n\nPay securely here: {paylink}"
    enriched = {
        **case,
        "outstanding_balance_usd": amount,
        "clauses": clauses,
        "path_mode": decision.mode,
        "path_band": decision.band_label,
        "pattern_tag": "new_customer",
        "pattern_rationale": "Customer has not yet completed 90 days of relationship history or submitted two prior invoices.",
        "suggested_tone": "polite",
        "internal_history_snapshot": snapshot,
    }
    state_patch = {key: value for key, value in enriched.items() if key != "case_id"}
    state_patch.update(amount_balance_usd=amount, status="active", current_stage="concierge", revised_email_body=draft, console_payment_link=paylink)
    update_case_state(case_id, **state_patch)
    preflight_body = (
        f"@investigator Pre-flight finished case {case_id} ({decision.band_label}).\n"
        f"Mode: {decision.mode} — {decision.rationale}\n"
        f"Governing law: {clauses['governing_law']} | Forum: {clauses['forum_clause']} | Late fee: {clauses['late_fee_rate']}\n"
        f"Case payload:\n{_pretty({**case, 'outstanding_balance_usd': amount, 'clauses': clauses, 'path_mode': decision.mode})}"
    )
    investigator_body = (
        f"@diplomat Investigator tagged case {case_id} as new_customer — Customer has not yet completed 90 days of relationship history or submitted two prior invoices.\n"
        "Suggested opening tone: polite.\n"
        f"Investigation summary — 6 months of customer history reviewed.\n"
        "Pattern tag: new_customer\nInvoices in window: 0\nLate payments: 0\nAverage days late: 0.0\nReached escalation before: no\n"
        f"Enriched case payload:\n{_pretty(enriched)}"
    )
    diplomat_body = (
        f"@tone_coach Diplomat drafted the day-7 email for case {case_id}. Please audit the tone:\n\n{draft}"
    )
    revised = (
        f"Subject: Action required — overdue invoice {invoice} (${amount:,.2f})\n\n"
        f"Hi {name},\n\nThis is a reminder that invoice {invoice} for ${amount:,.2f} was due on {case.get('due_date') or 'the original due date'}. "
        "Please remit payment within five business days. If there is a discrepancy or you require an updated invoice, reply with the details and we will respond within one business day.\n\n"
        f"Pay securely here: {paylink}\n\nBest regards,\nRecoverly Collections"
    )
    update_case_state(case_id, revised_email_body=revised)
    concierge_body = (
        f"AI-revised draft (not human-attested) — case {case_id}\n"
        "Cost so far: $0.0006\n\n"
        f"Summary: Tone Coach revised the case {case_id} draft. card_kind=ai_revised (not human-attested)\n\n"
        f"Tone Coach revised the case {case_id} draft. card_kind=ai_revised (not human-attested)\n"
        "Issues:\n"
        "- The original wording was overly familiar and softened the professional urgency expected in B2B collections.\n"
        "- The payment request needed a specific deadline and a clear path for raising a discrepancy.\n"
        "- The draft needed the buyer name, invoice, balance, due date, and secure payment link.\n\n"
        f"Revised:\n```\n{revised}\n```"
    )
    slack_result = post_card(
        case_id,
        f"Tone Coach revised the case {case_id} draft. card_kind=ai_revised (not human-attested)",
        concierge_body,
        card_kind="ai_revised",
        case_meta={**case, "outstanding_balance_usd": amount, "est_cost_usd": 0.0006},
    )
    if slack_result.ok:
        update_case_state(case_id, slack_approval_ts=slack_result.ts or "", current_stage="hitl_pending")
        concierge_body += f"\n\nConcierge posted this approval card to Slack (ts {slack_result.ts}). Waiting on the operator."
    else:
        concierge_body += f"\n\nSlack approval card could not be posted: {slack_result.error or 'unknown error'}."
    payment_body = (
        f"@concierge Payment Agent opened a USDC settlement for case {case_id} (${amount:,.2f}).\n"
        f"Invoice: {invoice}\nBuyer pays at: {paylink}\n"
        "The payment watcher will reconcile a confirmed transaction and update the case balance.\n"
        "card_kind=payment_created"
    )
    return [
        _message("preflight", preflight_body, case_id=case_id, reasoning="Validated the case amount, contract terms, balance, and collection path."),
        _message("investigator", investigator_body, case_id=case_id, reasoning="Reviewed the available customer history and selected the appropriate collection tone."),
        _message("diplomat", diplomat_body, case_id=case_id, reasoning="Prepared the buyer-facing reminder with the invoice details and secure payment link."),
        _message("tone_coach", f"@concierge Tone Coach revised the case {case_id} draft. card_kind=ai_revised\n\nIssues reviewed: unclear payment deadline, placeholders, and informal collections wording.\n\nRevised draft:\n{revised}", case_id=case_id, reasoning="Checked the message for unfilled placeholders, a clear deadline, and professional collections language."),
        _message("concierge", concierge_body, case_id=case_id, reasoning="Collected the case artifacts and posted the approval card to Slack for the operator decision."),
        _message("payment", payment_body, case_id=case_id, reasoning="Created the settlement instruction and prepared the payment watcher for reconciliation."),
    ]


def ingest_document(session_id: str, path: Path, filename: str) -> dict[str, Any]:
    session = get_session(session_id)
    if session is None:
        raise ValueError("Session not found.")
    extracted = extract(path, filename_hint=filename)
    if extracted.kind == "unknown":
        raise ValueError(extracted.error or "The upload is not a readable contract or invoice.")
    if extracted.invoice:
        item = extracted.invoice
        persona = item.buyer_persona
        meta = {"invoice_no": item.invoice_no, "outstanding_usd": item.outstanding_usd, "gross_usd": item.gross_usd, "due_date": item.due_date, "raw_text_excerpt": item.raw_text_excerpt}
    else:
        item = extracted.contract
        assert item is not None
        persona = item.buyer_persona
        meta = {"governing_law": item.governing_law, "payment_terms": item.payment_terms, "raw_text_excerpt": item.raw_text_excerpt}
    if not persona:
        raise ValueError("Could not identify the customer from this document. Include the customer name in the file.")
    decision = pair_or_pend(persona, extracted.kind, meta)
    if not decision.creates_case:
        body = f"Preflight read {filename} as a {extracted.kind}. {decision.note}"
        session.setdefault("messages", []).append(_message("preflight", body, reasoning="Extracted the uploaded document and checked for its matching case document."))
        session["updated_at"] = _now(); session["preview"] = body[:72]; _save_session(session)
        return session
    invoice = decision.invoice_meta
    contract = decision.contract_meta
    name = persona.replace("_", " ").title()
    case = {"case_id": decision.case_id, "invoice_no": invoice.get("invoice_no", "INV-UNKNOWN"), "amount_usd": safe_float(invoice.get("gross_usd"), 0), "outstanding_balance_usd": safe_float(invoice.get("outstanding_usd"), 0), "customer_name": name, "buyer_first_name": name.split()[0], "due_date": invoice.get("due_date", ""), "customer_state": contract.get("governing_law", ""), "contract_excerpt": contract.get("raw_text_excerpt", ""), "days_past_due": 7}
    session.setdefault("messages", []).append(_message("preflight", f"Paired {filename} with the matching document. Starting case {decision.case_id}.", case_id=decision.case_id))
    session["messages"].extend(run_case_workflow(session, "@preflight " + json.dumps(case)))
    session["updated_at"] = _now(); session["preview"] = session["messages"][-1]["body"][:72]; _save_session(session)
    return session


def append_case_activity(
    case_id: str, actor: str, body: str, *, reasoning: str = ""
) -> dict[str, Any] | None:
    """Mirror a Slack-originated agent update into the console session for its case."""
    with LOCK:
        data = _read()
        session = next(
            (
                item
                for item in data.get("sessions", [])
                if any(str(message.get("case_id") or "") == case_id for message in item.get("messages", []))
            ),
            None,
        )
        if session is None:
            return None
        session.setdefault("messages", []).append(
            _message(actor, body, case_id=case_id, reasoning=reasoning)
        )
        session["updated_at"] = _now()
        session["preview"] = body[:72]
        _write(data)
        return session

def submit_message(session_id: str, text: str) -> dict[str, Any]:
    session = get_session(session_id)
    if session is None:
        raise ValueError("Session not found.")
    text = text.strip()
    if not text:
        raise ValueError("Write a message first.")
    messages = session.setdefault("messages", [])
    messages.append(_message("operator", text))
    if text.lower().startswith("@preflight"):
        replies = run_case_workflow(session, text)
    elif text.lower().startswith("@concierge") and "status" in text.lower():
        replies = [_message("concierge", "Online. Available agents: " + ", ".join(agent["name"] for agent in AGENTS) + ".")]
    else:
        replies = [_message("concierge", "Start a new workflow by sending @preflight followed by a JSON case payload. You can also ask @concierge for a status check.")]
    messages.extend(replies)
    session["updated_at"] = _now()
    session["preview"] = replies[-1]["body"][:72]
    _save_session(session)
    return session


def run_action(session_id: str, message_id: str, action_id: str) -> dict[str, Any]:
    session = get_session(session_id)
    if session is None:
        raise ValueError("Session not found.")
    source = next((item for item in session.get("messages", []) if item.get("id") == message_id), None)
    if source is None or not source.get("case_id"):
        raise ValueError("This action is not connected to a case.")
    case_id = str(source["case_id"])
    case = load_case_state(case_id)
    result: list[dict[str, Any]]
    if action_id == "approve_email":
        from src.diplomat.outbound import send_day7_reminder
        dispatch = send_day7_reminder(case)
        if dispatch.ok:
            update_case_state(case_id, status="approved", current_stage="awaiting_payment")
            result = [_message("concierge", f"@diplomat [Concierge] Operator approval received for case {case_id}. The reviewed payment reminder has been sent and the case is now awaiting settlement.\nDelivered via {dispatch.backend}. The approval card is sealed and the outcome is recorded in the audit trail.\nevent=card_decided case_id={case_id}", case_id=case_id, reasoning="Recorded the approval and confirmed the email provider accepted the dispatch.")]
        else:
            result = [_message("concierge", f"@operator [Concierge] Approval was recorded for case {case_id}, but email delivery could not be completed: {dispatch.error}. The card remains open for review.", case_id=case_id, reasoning="The delivery provider did not confirm the dispatch, so the case is not marked as sent.")]
    elif action_id == "reject_email":
        update_case_state(case_id, status="rejected", current_stage="draft_revision")
        result = [_message("concierge", f"@tone_coach [Concierge] The operator rejected the AI-revised draft for case {case_id}. The draft is retained for revision; no email was sent. event=card_decided case_id={case_id}", case_id=case_id, reasoning="Recorded the operator rejection and held delivery.")]
    elif action_id == "revise_email":
        update_case_state(case_id, current_stage="draft_revision")
        result = [_message("tone_coach", f"@operator Revision requested for case {case_id}. The current draft is preserved. Send the revision guidance in this chat and the updated draft will return for approval.", case_id=case_id, reasoning="Returned the draft to the review stage without sending email.")]
    elif action_id == "open_payment":
        result = [_message("payment", f"@operator Payment instructions for case {case_id}:\n{str(case.get("console_payment_link") or "Payment portal is not available for this case.")}\nThe payment watcher will update the settlement status after verification.", case_id=case_id, reasoning="Retrieved the case-specific payment link and current settlement instructions.")]
    elif action_id == "start_voice":
        from src.voice.dialer import place_call
        dial = place_call(case)
        if dial.ok:
            phone = str(case.get("customer_phone") or case.get("phone") or "the buyer")
            body = (
                f"@concierge [Voice] The operator approved the call on case {case_id}, so {phone} is being dialled now.\n"
                f"The call is placed as {dial.call_sid}. The summary returns by webhook when it ends.\n"
                f"@concierge the HITL card can be sealed. event=card_decided case_id={case_id}"
            )
        else:
            body = f"@operator [Voice] The operator approved a call for case {case_id}, but the call was not started: {dial.error}."
        result = [_message("voice", body, case_id=case_id, reasoning="Checked the case phone details and calling configuration before requesting the call.")]
    elif action_id == "escalate":
        update_case_state(case_id, current_stage="escalation_review")
        result = [_message("escalator", f"@aaa Escalator opened an escalation review for case {case_id}.\nOutstanding balance: ${safe_float(case.get('outstanding_balance_usd'), 0.0):,.2f}.\nNext action: prepare the arbitration strategy and confirm governing law, venue, filing cost, and cure-window status before any filing decision.", case_id=case_id, reasoning="Moved the case into escalation review and retained the operator decision point.", actions=[{"id": "aaa_strategy", "label": "Prepare AAA strategy"}])]
    elif action_id == "aaa_strategy":
        update_case_state(case_id, current_stage="aaa_strategy_review")
        outstanding = safe_float(case.get("outstanding_balance_usd"), 0.0)
        result = [_message("aaa", f"@concierge event=approval_request request_kind=aaa_strategy_recommendation for case {case_id}.\n\nArbitration strategy — case {case_id}\n1. Pre-arbitration demand letter — provide a 14-day cure window before filing.\n2. Filing fee — estimate $925 for this commercial claim.\n3. Case management — confirm the contract's dispute clause and available evidence.\n4. Venue — use the agreement's forum clause; governing law is {case.get('customer_state') or 'the stated jurisdiction'}.\n5. Settlement pressure — filing preparation may support a negotiated resolution.\n\nOutstanding balance: ${outstanding:,.2f}. No filing or legal action has been initiated.", case_id=case_id, reasoning="Prepared the strategy from the case facts without initiating legal action.")]
    else:
        raise ValueError("Unknown action.")
    session.setdefault("messages", []).extend(result)
    session["updated_at"] = _now()
    session["preview"] = result[-1]["body"][:72]
    _save_session(session)
    return session
