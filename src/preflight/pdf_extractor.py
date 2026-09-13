from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from src.personas import buyer_id_for

log = logging.getLogger("recoverly.preflight.pdf_extractor")

PDFTOTEXT_TIMEOUT_SECONDS: Final = 15
RAW_EXCERPT_LENGTH: Final = 800

PERSONA_HINTS: Final[dict[str, str]] = {
    "abc trading": "abc_trading",
    "abc-trading": "abc_trading",
    "abc_trading": "abc_trading",
    "polymatrix": "polymatrix",
    "xyz industries": "xyz",
    "xyz-industries": "xyz",
    "xyz_industries": "xyz",
    "newleaf wellness": "newleaf",
    "newleaf-wellness": "newleaf",
    "newleaf_wellness": "newleaf",
    "newleaf": "newleaf",
    "megacorp holdings": "megacorp",
    "megacorp-holdings": "megacorp",
    "megacorp_holdings": "megacorp",
    "megacorp": "megacorp",
}

CONTRACT_FILENAME_KEYWORDS: Final = ("contract", "agreement", "msa")

INVOICE_NO_RE: Final = re.compile(r"INV-(\d{4})-(\d{4})", re.IGNORECASE)
CONTRACT_ID_RE: Final = re.compile(r"CONTRACT-[A-Z][A-Z0-9-]{2,40}", re.IGNORECASE)
AMOUNT_RE: Final = re.compile(r"USD\s*\$?\s*([0-9,]+(?:\.\d{1,2})?)")
OUTSTANDING_RE: Final = re.compile(
    r"Outstanding\s*Balance[^0-9]*USD\s*\$?\s*([0-9,]+(?:\.\d{1,2})?)", re.IGNORECASE
)
GROSS_RE: Final = re.compile(
    r"Gross\s*Invoice\s*Total[^0-9]*USD\s*\$?\s*([0-9,]+(?:\.\d{1,2})?)", re.IGNORECASE
)
DUE_DATE_RE: Final = re.compile(r"Due\s*Date[^0-9]*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
NET_TERMS_RE: Final = re.compile(r"Net\s*(\d{1,3})", re.IGNORECASE)
GOVERNING_LAW_RE: Final = re.compile(
    r"(?:governed\s+by\s+(?:the\s+)?laws?\s+of|governing\s+law\s*(?:is|:)?)[^A-Za-z]*"
    r"([A-Z][A-Za-z ]{2,40}?)(?=\.|\n|$)",
    re.IGNORECASE,
)
FORUM_RE: Final = re.compile(r"(AAA[^.\n]{0,80}|arbitrat\w+[^.\n]{0,80})", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExtractedInvoice:
    invoice_no: str
    buyer_persona: str
    buyer_id: str
    outstanding_usd: float
    gross_usd: float
    due_date: str
    contract_id: str
    raw_text_excerpt: str


@dataclass(frozen=True, slots=True)
class ExtractedContract:
    contract_id: str
    buyer_persona: str
    buyer_id: str
    governing_law: str
    forum: str
    payment_terms: str
    raw_text_excerpt: str


@dataclass(frozen=True, slots=True)
class ExtractedFile:
    kind: str
    invoice: ExtractedInvoice | None = None
    contract: ExtractedContract | None = None
    error: str = ""
    source_path: str = ""
    source_filename: str = ""

    @property
    def buyer_persona(self) -> str:
        if self.invoice:
            return self.invoice.buyer_persona
        if self.contract:
            return self.contract.buyer_persona
        return ""


def match_persona(haystack: str) -> tuple[str, str]:
    lowered = haystack.lower()
    for hint, persona in PERSONA_HINTS.items():
        if hint in lowered:
            return persona, buyer_id_for(persona) or ""
    return "", ""


def parse_amount(raw: str) -> float:
    try:
        return float(raw.replace(",", "").replace("$", "").strip())
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _read_with_pdftotext(pdf_path: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True,
            timeout=PDFTOTEXT_TIMEOUT_SECONDS,
            check=True,
        )
    except FileNotFoundError:
        log.debug("pdftotext is not on PATH, falling back to pypdf")
        return None
    except subprocess.CalledProcessError as error:
        log.warning("pdftotext exited %s: %s", error.returncode, error.stderr[:200])
        return None
    except subprocess.TimeoutExpired:
        log.warning("pdftotext timed out on %s", pdf_path)
        return None

    return completed.stdout.decode("utf-8", errors="replace")


def _read_with_pypdf(pdf_path: Path) -> str | None:
    try:
        from pypdf import PdfReader
    except ImportError:
        log.debug("pypdf is not installed")
        return None

    try:
        reader = PdfReader(str(pdf_path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as error:
        log.warning("pypdf could not read %s: %s", pdf_path, error)
        return None


def read_pdf_text(pdf_path: Path) -> str | None:
    return _read_with_pdftotext(pdf_path) or _read_with_pypdf(pdf_path)


def _looks_like_contract(filename: str) -> bool:
    lowered = filename.lower()
    return any(keyword in lowered for keyword in CONTRACT_FILENAME_KEYWORDS)


def _resolve_persona(filename: str, text: str) -> tuple[str, str]:
    persona, buyer_id = match_persona(filename)
    if persona:
        return persona, buyer_id
    return match_persona(text)


def build_invoice(
    text: str, filename: str, path: Path, invoice_match: re.Match[str]
) -> ExtractedFile:
    persona, buyer_id = _resolve_persona(filename, text)

    outstanding_match = OUTSTANDING_RE.search(text)
    gross_match = GROSS_RE.search(text)
    outstanding = parse_amount(outstanding_match.group(1)) if outstanding_match else 0.0
    gross = parse_amount(gross_match.group(1)) if gross_match else 0.0

    if not outstanding and not gross:
        fallback = AMOUNT_RE.search(text)
        if fallback:
            gross = outstanding = parse_amount(fallback.group(1))

    due_match = DUE_DATE_RE.search(text)
    contract_match = CONTRACT_ID_RE.search(text)

    invoice = ExtractedInvoice(
        invoice_no=f"INV-{invoice_match.group(1)}-{invoice_match.group(2)}",
        buyer_persona=persona,
        buyer_id=buyer_id,
        outstanding_usd=outstanding or gross,
        gross_usd=gross or outstanding,
        due_date=due_match.group(1) if due_match else "",
        contract_id=contract_match.group(0).upper() if contract_match else "",
        raw_text_excerpt=text[:RAW_EXCERPT_LENGTH].strip(),
    )
    return ExtractedFile(
        kind="invoice", invoice=invoice, source_path=str(path), source_filename=filename
    )


def build_contract(text: str, filename: str, path: Path) -> ExtractedFile:
    persona, buyer_id = _resolve_persona(filename, text)

    contract_match = CONTRACT_ID_RE.search(text)
    law_match = GOVERNING_LAW_RE.search(text)
    forum_match = FORUM_RE.search(text)
    net_match = NET_TERMS_RE.search(text)

    contract = ExtractedContract(
        contract_id=contract_match.group(0).upper() if contract_match else "",
        buyer_persona=persona,
        buyer_id=buyer_id,
        governing_law=law_match.group(1).strip() if law_match else "",
        forum=forum_match.group(0).strip() if forum_match else "",
        payment_terms=f"Net {net_match.group(1)}" if net_match else "",
        raw_text_excerpt=text[:RAW_EXCERPT_LENGTH].strip(),
    )
    return ExtractedFile(
        kind="contract", contract=contract, source_path=str(path), source_filename=filename
    )


def classify_text(text: str, filename: str, path: Path) -> ExtractedFile:
    if _looks_like_contract(filename):
        return build_contract(text, filename, path)

    invoice_match = INVOICE_NO_RE.search(filename) or INVOICE_NO_RE.search(text)
    if invoice_match:
        return build_invoice(text, filename, path, invoice_match)

    return ExtractedFile(
        kind="unknown",
        error="no invoice number in the filename or body, and no contract keyword",
        source_path=str(path),
        source_filename=filename,
    )


def extract(pdf_path: Path | str, filename_hint: str = "") -> ExtractedFile:
    path = Path(pdf_path)
    filename = filename_hint or path.name

    if not path.exists():
        return ExtractedFile(
            kind="unknown",
            error=f"file not found: {path}",
            source_path=str(path),
            source_filename=filename,
        )

    text = read_pdf_text(path)
    if text is None:
        return ExtractedFile(
            kind="unknown",
            error="no usable PDF text extractor (install poppler's pdftotext or pypdf)",
            source_path=str(path),
            source_filename=filename,
        )

    return classify_text(text, filename, path)
