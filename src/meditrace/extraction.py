"""Extraction worker boundary and deterministic normalization helpers.

API requests only enqueue jobs. Run ``python -m src.meditrace.worker`` in a
separate process to parse documents (OCR for scans) and optionally submit
them to a model. Every fact produced here is sealed with provenance by the
caller (see provenance.py): source hash, extractor, extractor/prompt version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import io
import json
import re
from typing import Protocol
from urllib.request import Request, urlopen

from .schemas import DocumentType

LOINC_SUBSET_VERSION = "2026-09"
LOINC_SUBSET = {
    "hba1c": ("4548-4", "Hemoglobin A1c"),
    "hemoglobin a1c": ("4548-4", "Hemoglobin A1c"),
    "hbalc": ("4548-4", "Hemoglobin A1c"),  # common OCR confusion of 1/l
    "glucose": ("2345-7", "Glucose"),
    "creatinine": ("2160-0", "Creatinine"),
    "hemoglobin": ("718-7", "Hemoglobin"),
    "sodium": ("2951-2", "Sodium"),
    "potassium": ("2823-3", "Potassium"),
    "ldl": ("13457-7", "LDL Cholesterol"),
    "ldl cholesterol": ("13457-7", "LDL Cholesterol"),
    "total cholesterol": ("2093-3", "Total Cholesterol"),
    "cholesterol": ("2093-3", "Total Cholesterol"),
}
RXNORM_SUBSET = {
    "metformin": "6809",
    "lisinopril": "29046",
    "atorvastatin": "83367",
    "amoxicillin": "723",
}

MODEL_EXTRACTION_PROMPT = (
    "Extract only evidence-supported facts. Return JSON {facts: [...]} and "
    "retain evidence locations."
)
MODEL_EXTRACTION_PROMPT_LABEL = "extract-v1"


def classify_document(text: str) -> DocumentType:
    value = text.lower()
    scores = {
        DocumentType.lab_report: sum(
            x in value
            for x in (
                "reference range",
                "specimen",
                "laboratory",
                "hba1c",
                "creatinine",
            )
        ),
        DocumentType.medication_list: sum(
            x in value
            for x in (
                "medication",
                "tablet",
                "capsule",
                "mg daily",
                "rx",
                "sig:",
                "dispense",
                "refills",
                "prescription",
            )
        ),
        DocumentType.radiology_report: sum(
            x in value for x in ("radiology", "impression", "findings", "x-ray", "ct ")
        ),
        DocumentType.discharge_summary: sum(
            x in value
            for x in ("discharge", "admission", "hospital course", "follow-up")
        ),
    }
    winner = max(scores, key=scores.get)
    return winner if scores[winner] else DocumentType.unknown


# ----------------------------------------------------------- text sources


@dataclass
class ExtractedText:
    """Text plus how it was obtained. Pages are separated by form feeds."""

    text: str
    method: str  # text | pdf-text | ocr-image | ocr-pdf
    engine: str | None = None
    line_confidence: dict[tuple[int, int], float] = field(default_factory=dict)

    @property
    def is_ocr(self) -> bool:
        return self.method.startswith("ocr")


def _pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF extraction requires pypdf") from exc
    return "\f".join(
        (page.extract_text() or "") for page in PdfReader(io.BytesIO(content)).pages
    )


def extract_document(content: bytes, media_type: str) -> ExtractedText:
    if media_type in {"text/plain", "text/csv"}:
        return ExtractedText(content.decode("utf-8", errors="replace"), "text")
    if media_type == "application/pdf":
        text = _pdf_text(content)
        pages = text.split("\f")
        # A text layer this thin means a scanned PDF: fall through to OCR.
        if sum(len(p.strip()) for p in pages) >= 20 * max(1, len(pages)):
            import pypdf

            return ExtractedText(text, "pdf-text", f"pypdf {pypdf.__version__}")
        from .ocr import ocr_pdf

        result = ocr_pdf(content)
        return ExtractedText(result.text, "ocr-pdf", result.engine, result.line_confidence)
    if media_type in {"image/png", "image/jpeg"}:
        from .ocr import ocr_image

        result = ocr_image(content)
        return ExtractedText(result.text, "ocr-image", result.engine, result.line_confidence)
    raise RuntimeError(f"Text extraction is not configured for {media_type}")


def extract_text(content: bytes, media_type: str) -> str:
    return extract_document(content, media_type).text


# ----------------------------------------------------------------- dates

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "mdy"),
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?\s+(\d{4})\b"), "dmy_text"),
    (re.compile(r"\b([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b"), "mdy_text"),
]
_DATE_LABELS = re.compile(
    r"(collected|collection date|date of service|service date|report date|"
    r"visit date|exam date|discharge date|date prescribed|prescribed|rx date|date)\s*[:\-]",
    re.I,
)


def _parse_date(fragment: str) -> date | None:
    for pattern, kind in _DATE_PATTERNS:
        match = pattern.search(fragment)
        if not match:
            continue
        try:
            a, b, c = match.groups()
            if kind == "ymd":
                return date(int(a), int(b), int(c))
            if kind == "mdy":
                return date(int(c), int(a), int(b))
            if kind == "dmy_text" and b[:3].lower() in _MONTHS:
                return date(int(c), _MONTHS[b[:3].lower()], int(a))
            if kind == "mdy_text" and a[:3].lower() in _MONTHS:
                return date(int(c), _MONTHS[a[:3].lower()], int(b))
        except ValueError:
            continue
    return None


def _contains_date(line: str) -> bool:
    return any(pattern.search(line) for pattern, _ in _DATE_PATTERNS)


def find_document_date(text: str) -> tuple[date, str] | None:
    """The first labelled date ("Collected: 2026-03-14"), else the first date
    anywhere. Returns the date and the line it came from, as evidence."""
    lines = [line for line in text.replace("\f", "\n").splitlines() if line.strip()]
    for line in lines:
        label = _DATE_LABELS.search(line)
        if label:
            found = _parse_date(line[label.end():])
            if found:
                return found, line.strip()[:500]
    for line in lines:
        found = _parse_date(line)
        if found:
            return found, line.strip()[:500]
    return None


# ------------------------------------------------------------ extraction


def _iter_lines(text: str):
    """Yield (page, line_number_within_page, line)."""
    for page_number, page in enumerate(text.split("\f"), 1):
        for line_number, line in enumerate(page.splitlines(), 1):
            yield page_number, line_number, line


def _location(page: int, line_number: int, quote: str) -> dict:
    return {
        "page": page,
        "line_start": line_number,
        "line_end": line_number,
        "quote": quote.strip()[:500],
    }


_LAB_LINE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9 ]{1,40}?)\s*[:,-]?\s+(-?\d+(?:\.\d+)?)\s*([%A-Za-z/µ]+)?"
    r"(?:\s+(H|L|high|low|normal|abnormal)\b)?"
    r"(?:.*?\(?\s*(\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?)\s*\)?)?",
    re.I,
)
_NOT_A_TEST = {"page", "patient", "mrn", "id", "age", "phone", "fax", "room", "bed", "refills"}


def _normalize_lab_name(name: str, *, fuzzy: bool) -> tuple[tuple[str, str] | None, str | None]:
    """Exact LOINC-subset lookup; for OCR text also a close match (OCR turns
    "HbA1c" into "HbAi1c"/"HbAlc"). Returns (code, display) and the method."""
    key = name.lower().strip()
    if key in LOINC_SUBSET:
        return LOINC_SUBSET[key], "exact"
    if fuzzy:
        import difflib

        compact = re.sub(r"[^a-z0-9]", "", key)
        candidates = {re.sub(r"[^a-z0-9]", "", k): k for k in LOINC_SUBSET}
        match = difflib.get_close_matches(compact, candidates, n=1, cutoff=0.8)
        if match:
            return LOINC_SUBSET[candidates[match[0]]], "fuzzy_ocr"
    return None, None


def _status_from_range(value: str, reference_range: str | None) -> str | None:
    if not reference_range:
        return None
    try:
        low, high = (float(x) for x in reference_range.replace(" ", "").split("-"))
        numeric = float(value)
    except ValueError:
        return None
    return "high" if numeric > high else "low" if numeric < low else "normal"


def deterministic_facts(
    text: str,
    patient_id: str,
    document_type: DocumentType,
    *,
    fallback_date: date | None = None,
    line_confidence: dict[tuple[int, int], float] | None = None,
    ocr_engine: str | None = None,
) -> list[dict]:
    line_confidence = line_confidence or {}
    found = find_document_date(text)
    if found:
        observed_date, date_quote = found
        date_details = {"date_source": "document_text", "date_evidence": date_quote}
    else:
        observed_date = fallback_date or date.today()
        date_details = {"date_source": "upload_date_fallback"}
    is_prescription = document_type == DocumentType.medication_list and any(
        x in text.lower() for x in ("rx", "sig:", "dispense", "refills", "prescription")
    )

    def finish(page: int, line_no: int, base_confidence: float, details: dict) -> tuple[float, dict]:
        details = {**details, **date_details}
        confidence = base_confidence
        if (page, line_no) in line_confidence:
            ocr_conf = line_confidence[(page, line_no)]
            confidence = round(base_confidence * ocr_conf, 4)
            details["ocr"] = {"engine": ocr_engine, "line_confidence": ocr_conf}
        return confidence, details

    facts: list[dict] = []
    if document_type == DocumentType.lab_report:
        for page, number, line in _iter_lines(text):
            if _contains_date(line):
                continue
            match = _LAB_LINE.search(line)
            if not match:
                continue
            name, value, unit, status, reference_range = match.groups()
            name = name.strip()
            if name.lower() in _NOT_A_TEST:
                continue
            normalized, normalization = _normalize_lab_name(
                name, fuzzy=bool(line_confidence)
            )
            status_source = "document" if status else None
            if not status:
                status = _status_from_range(value, reference_range)
                status_source = "reference_range" if status else None
            status = {"h": "high", "l": "low"}.get((status or "").lower(), status)
            confidence, details = finish(
                page,
                number,
                (0.85 if normalization == "exact" else 0.75 if normalized else 0.65),
                {
                    "terminology": "LOINC",
                    "terminology_version": LOINC_SUBSET_VERSION,
                    **({"normalization": normalization, "source_text": name} if normalization == "fuzzy_ocr" else {}),
                    **({"status_source": status_source} if status_source else {}),
                },
            )
            facts.append(
                {
                    "patient_id": patient_id,
                    "fact_type": "lab",
                    "test_or_finding": normalized[1] if normalized else name,
                    "normalized_code": normalized[0] if normalized else None,
                    "value": value,
                    "unit": unit,
                    "reference_range": reference_range.replace(" ", "") if reference_range else None,
                    "status": status.lower() if status else None,
                    "observed_date": observed_date,
                    "evidence_location": _location(page, number, line),
                    "confidence": confidence,
                    "details": details,
                }
            )
    elif document_type == DocumentType.medication_list:
        for page, number, line in _iter_lines(text):
            lowered = line.lower()
            for name, code in RXNORM_SUBSET.items():
                if name not in lowered:
                    continue
                dose = re.search(r"(\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml))", line, re.I)
                action = (
                    "discontinued"
                    if re.search(r"\b(discontinue[d]?|stopped|d/c)\b", lowered)
                    else "prescribed" if is_prescription else "documented"
                )
                confidence, details = finish(
                    page, number, 0.85, {"terminology": "RxNorm", "action": action}
                )
                facts.append(
                    {
                        "patient_id": patient_id,
                        "fact_type": "medication",
                        "test_or_finding": name.title(),
                        "normalized_code": code,
                        "value": dose.group(1).replace(" ", "") if dose else None,
                        "observed_date": observed_date,
                        "evidence_location": _location(page, number, line),
                        "confidence": confidence,
                        "details": details,
                    }
                )
    elif document_type in {
        DocumentType.radiology_report,
        DocumentType.discharge_summary,
    }:
        for page, number, line in _iter_lines(text):
            if line.strip() and (
                "impression" in line.lower() or "diagnosis" in line.lower()
            ):
                confidence, details = finish(page, number, 0.7, {})
                facts.append(
                    {
                        "patient_id": patient_id,
                        "fact_type": (
                            "radiology"
                            if document_type == DocumentType.radiology_report
                            else "discharge"
                        ),
                        "test_or_finding": line.split(":", 1)[-1].strip()[:255] or "Impression",
                        "observed_date": observed_date,
                        "evidence_location": _location(page, number, line),
                        "confidence": confidence,
                        "details": details,
                    }
                )
    return facts


# ---------------------------------------------------------------- models


def call_openai_compatible_json(
    endpoint: str,
    model: str,
    api_key: str | None,
    system_prompt: str,
    user_payload: dict,
    timeout: int = 60,
) -> dict:
    """Calls any OpenAI-compatible chat completions gateway (incl. MedGemma) and
    parses its JSON response body. Used by extraction and by the Q&A retrieval
    verification pass; never sends raw document text unless the caller puts it
    in ``user_payload`` explicitly."""
    body = json.dumps(
        {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, default=str)},
            ],
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    with urlopen(Request(endpoint, data=body, headers=headers), timeout=timeout) as response:
        result = json.load(response)
    content = result.get("choices", [{}])[0].get("message", {}).get("content", result)
    return json.loads(content) if isinstance(content, str) else content


class ModelProvider(Protocol):
    name: str

    def extract(self, payload: dict) -> list[dict]: ...


class HttpModelProvider:
    """OpenAI-compatible structured extraction endpoint (including MedGemma gateways)."""

    system_prompt = MODEL_EXTRACTION_PROMPT

    def __init__(self, endpoint: str, model: str, api_key: str | None = None):
        self.endpoint, self.name, self.api_key = endpoint, model, api_key

    @property
    def prompt_version(self) -> str:
        from .provenance import prompt_version

        return prompt_version(self.system_prompt, MODEL_EXTRACTION_PROMPT_LABEL)

    def extract(self, payload: dict) -> list[dict]:
        content = call_openai_compatible_json(
            self.endpoint, self.name, self.api_key, self.system_prompt, payload
        )
        return content.get("facts", [])

