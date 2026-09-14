from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from src.agents._case_lock import try_acquire
from src.agents._case_state import add_cost, update_case_state
from src.agents._utils import parse_json_object, safe_float
from src.agents.base import StrandsAgentAdapter, audit, run_agent
from src.config import settings
from src.llm.provider import complete_async
from src.preflight.amount_router import select_path
from src.preflight.intake_pairing import PairingDecision, pair_or_pend
from src.preflight.pdf_extractor import ExtractedFile, extract
from src.preflight.routing import route_case
from src.utils.filelock import write_atomic
from src.utils.text import slack_preview

MAX_CLAUSE_TOKENS: Final = 800

INVOICE_UPLOAD_EVENT_RE: Final = re.compile(
    r'"event"\s*:\s*"invoice_uploaded_slack"|event\s*[=:]\s*invoice_uploaded_slack',
    re.IGNORECASE,
)

ACTION_TO_REQUEST_KIND: Final[dict[str, str]] = {
    "case_create": "preflight_start_cadence",
    "merge_and_create": "preflight_start_cadence",
    "pend_waiting_contract": "preflight_need_contract",
    "pend_waiting_invoice": "preflight_need_invoice",
    "duplicate_ignored": "preflight_duplicate",
    "unrecognized": "preflight_unrecognized",
}

CLAUSE_KEYS: Final[tuple[str, ...]] = (
    "governing_law",
    "forum_clause",
    "late_fee_rate",
    "dispute_window_days",
    "payment_terms",
    "fees_clause",
    "arbitration_clause",
)

SYSTEM_PROMPT: Final = (
    "You are the Pre-flight agent for Recoverly, a stateless contract analyser. "
    "Your entire response must be a single JSON object with no prose, no preamble and "
    "no markdown fences. Start with { and end with }. "
    "Required keys: " + ", ".join(CLAUSE_KEYS) + ". "
    'If a key cannot be extracted, set its value to "UNKNOWN" and add the key name to a '
    '"missing" array. Keep every value a short string.'
)


def resolve_outstanding(case: dict[str, Any]) -> tuple[float, float, float]:
    gross = safe_float(case.get("amount_usd"), 0.0)
    deposit_pct = safe_float(case.get("deposit_pct"), 0.0)
    outstanding = safe_float(case.get("outstanding_balance_usd"), 0.0)
    if outstanding <= 0 and gross > 0:
        outstanding = round(gross * (1.0 - deposit_pct), 2)
    return outstanding, gross, deposit_pct


def document_summary(
    extracted: ExtractedFile, filename: str
) -> tuple[str, dict[str, Any], str]:
    if extracted.kind == "invoice" and extracted.invoice:
        invoice = extracted.invoice
        meta = {
            "invoice_no": invoice.invoice_no,
            "outstanding_usd": invoice.outstanding_usd,
            "gross_usd": invoice.gross_usd,
            "due_date": invoice.due_date,
            "buyer_id": invoice.buyer_id,
            "filename": extracted.source_filename,
        }
        return (
            invoice.buyer_persona or "unknown",
            meta,
            f"invoice {invoice.invoice_no} (${invoice.outstanding_usd:,.2f} outstanding)",
        )

    if extracted.kind == "contract" and extracted.contract:
        contract = extracted.contract
        meta = {
            "contract_id": contract.contract_id,
            "governing_law": contract.governing_law,
            "payment_terms": contract.payment_terms,
            "buyer_id": contract.buyer_id,
            "filename": extracted.source_filename,
        }
        return (
            contract.buyer_persona or "unknown",
            meta,
            f"contract {contract.contract_id or 'with no readable id'}",
        )

    return (
        "unknown",
        {"filename": filename, "error": extracted.error},
        f"unreadable file {filename}",
    )


class PreflightAdapter(StrandsAgentAdapter):
    role = "preflight"

    async def handle_message(self, text, msg, tools, history, room_id):
        if INVOICE_UPLOAD_EVENT_RE.search(text):
            return self._handle_invoice_upload(text)

        case = parse_json_object(text)
        if not case:
            return (
                "@diplomat Pre-flight needs a JSON case payload with at least case_id, "
                "amount_usd, deposit_pct, outstanding_balance_usd, customer_name and "
                "contract_excerpt."
            )

        return await self._handle_case(case)

    async def _handle_case(self, case: dict[str, Any]) -> str | None:
        case_id = str(case.get("case_id") or "UNKNOWN")
        outstanding, gross, deposit_pct = resolve_outstanding(case)

        if case_id != "UNKNOWN" and not try_acquire(case_id, self.role):
            self.log.info("case %s is already in the pipeline, skipping", case_id)
            return None

        audit(
            case_id,
            self.role,
            "preflight_start",
            {
                "gross_amount_usd": gross,
                "deposit_pct": deposit_pct,
                "outstanding_balance_usd": outstanding,
            },
        )

        if outstanding <= 0:
            return (
                f"@concierge Pre-flight cannot route case `{case_id}`: no positive outstanding "
                f"balance was supplied (gross ${gross:,.2f}, deposit {deposit_pct:.0%})."
            )

        path = select_path(outstanding, gross_amount_usd=gross, deposit_pct=deposit_pct)
        audit(
            case_id,
            self.role,
            "path_decision",
            {
                "mode": path.mode,
                "band": path.band_label,
                "rationale": path.rationale,
                "outstanding_balance_usd": path.outstanding_balance_usd,
                "gross_amount_usd": path.gross_amount_usd,
                "deposit_pct": path.deposit_pct,
            },
        )

        contract_excerpt = str(case.get("contract_excerpt") or "")
        if not contract_excerpt:
            return (
                f"@concierge Case `{case_id}` carries no contract excerpt, so no clauses could be "
                f"extracted. Path mode is `{path.mode}` ({path.band_label})."
            )

        clauses, usage = await self._extract_clauses(case_id, contract_excerpt)
        if clauses is None:
            return (
                f"@concierge Pre-flight could not parse the clause JSON for case `{case_id}`. "
                f"Raw model output: `{slack_preview(usage)}`"
            )

        self._persist_path(case_id, path.mode, path.band_label)

        route = route_case(case, path)
        enriched = route.enrich({**case, "clauses": clauses}, path)
        update_case_state(
            case_id,
            **{key: value for key, value in enriched.items() if key != "case_id"},
        )

        return (
            f"@{route.target} Pre-flight finished case `{case_id}` ({path.band_label}).\n"
            f"Mode: **{path.mode}** — {route.note}\n"
            f"Governing law: {clauses.get('governing_law', 'unknown')} | "
            f"Forum: {clauses.get('forum_clause', 'unknown')} | "
            f"Late fee: {clauses.get('late_fee_rate', 'unknown')}\n"
            f"Case payload:\n```json\n{json.dumps(enriched, indent=2)}\n```"
        )

    async def _extract_clauses(
        self, case_id: str, contract_excerpt: str
    ) -> tuple[dict[str, Any] | None, Any]:
        prompt = f"Contract excerpt for case {case_id}:\n\n{contract_excerpt}\n\nExtract the clauses."
        try:
            raw, usage = await complete_async(
                SYSTEM_PROMPT,
                prompt,
                agent_role=self.role,
                max_tokens=MAX_CLAUSE_TOKENS,
            )
        except Exception as error:
            audit(case_id, self.role, "llm_error", {"error": str(error)})
            return None, str(error)

        add_cost(case_id, usage)

        clauses = parse_json_object(raw)
        if not clauses:
            audit(case_id, self.role, "clause_parse_failed", {"raw": raw[:300]})
            return None, raw

        audit(
            case_id, self.role, "preflight_done", {"clauses": clauses, "usage": usage}
        )
        return clauses, usage

    def _persist_path(self, case_id: str, mode: str, band_label: str) -> None:
        if case_id == "UNKNOWN":
            return
        try:
            update_case_state(case_id, path_mode=mode, path_band=band_label)
        except Exception as error:
            audit(case_id, self.role, "path_persist_failed", {"error": str(error)})

    def _handle_invoice_upload(self, text: str) -> str:
        payload = parse_json_object(text) or {}
        file_path = str(payload.get("file_path") or payload.get("local_path") or "")
        filename = str(payload.get("filename") or payload.get("file_name") or "")
        intake_case_id = str(
            payload.get("case_id") or payload.get("intake_case_id") or "UNKNOWN"
        )

        audit(
            intake_case_id,
            self.role,
            "invoice_upload_received",
            {"file_path": file_path, "filename": filename},
        )

        if file_path and Path(file_path).exists():
            extracted = extract(Path(file_path), filename_hint=filename)
        else:
            extracted = ExtractedFile(
                kind="unknown",
                error="the uploaded file is not available on this host",
                source_filename=filename,
            )

        audit(
            intake_case_id,
            self.role,
            "pdf_extracted",
            {"kind": extracted.kind, "error": extracted.error, "filename": filename},
        )

        persona, meta, summary = document_summary(extracted, filename)

        if persona == "unknown":
            decision = PairingDecision(
                action="unrecognized",
                note=f"Could not identify the buyer from {filename or 'the upload'}.",
            )
        else:
            decision = pair_or_pend(persona, extracted.kind, meta)

        case_id = decision.case_id or intake_case_id
        request_kind = ACTION_TO_REQUEST_KIND.get(decision.action, "preflight_pend")

        audit(
            case_id,
            self.role,
            "intake_decision",
            {"persona": persona, "request_kind": request_kind, **decision.as_dict()},
        )

        if decision.creates_case:
            self._write_pairing_sidecar(case_id, persona, decision, payload)

        status = (
            "A new case is open and ready to start the cadence."
            if decision.creates_case
            else "Held as pending until the matching document arrives."
        )
        return (
            f"[Pre-flight] Processed {summary} from {filename or 'the upload'}.\n"
            f"Buyer: {persona} | document kind: {extracted.kind}\n"
            f"Pairing decision: {decision.action} — {decision.note}\n"
            f"@concierge approval_request for case `{case_id}`: request_kind={request_kind}\n"
            f"{status}"
        )

    def _write_pairing_sidecar(
        self,
        case_id: str,
        persona: str,
        decision: PairingDecision,
        payload: dict[str, Any],
    ) -> None:
        path = settings.data.case_state_dir / f"{case_id}_pairing.json"
        document = {
            "case_id": case_id,
            "persona": persona,
            "invoice_meta": decision.invoice_meta,
            "contract_meta": decision.contract_meta,
            "slack_channel": payload.get("slack_channel", ""),
            "minted_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            write_atomic(path, json.dumps(document, indent=2, ensure_ascii=False))
        except Exception as error:
            audit(case_id, self.role, "pairing_sidecar_failed", {"error": str(error)})
            return
        audit(case_id, self.role, "pairing_sidecar_written", {"path": str(path)})


if __name__ == "__main__":
    sys.exit(run_agent(PreflightAdapter))
