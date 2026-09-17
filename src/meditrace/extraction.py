"""Extraction worker boundary and deterministic normalization helpers.

API requests only enqueue jobs. Run ``python -m src.meditrace.worker`` in a
separate process to parse documents and optionally submit them to a model.
"""

from __future__ import annotations

from datetime import date
import csv
import io
import json
import re
from typing import Protocol
from urllib.request import Request, urlopen

from .schemas import DocumentType

LOINC_SUBSET_VERSION = "2026-01"
LOINC_SUBSET = {
    "hba1c": ("4548-4", "Hemoglobin A1c"),
    "hemoglobin a1c": ("4548-4", "Hemoglobin A1c"),
    "glucose": ("2345-7", "Glucose"),
    "creatinine": ("2160-0", "Creatinine"),
    "hemoglobin": ("718-7", "Hemoglobin"),
    "sodium": ("2951-2", "Sodium"),
    "potassium": ("2823-3", "Potassium"),
}
RXNORM_SUBSET = {
    "metformin": "6809",
    "lisinopril": "29046",
    "atorvastatin": "83367",
    "amoxicillin": "723",
}


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
            x in value for x in ("medication", "tablet", "capsule", "mg daily", "rx")
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


def extract_text(content: bytes, media_type: str) -> str:
    if media_type in {"text/plain", "text/csv"}:
        return content.decode("utf-8", errors="replace")
    if media_type == "application/pdf":
        try:
            from pypdf import PdfReader

            return "\n\f\n".join(
                page.extract_text() or ""
                for page in PdfReader(io.BytesIO(content)).pages
            )
        except ImportError as exc:
            raise RuntimeError(
                "PDF extraction requires pypdf; scanned PDF OCR requires the optional Docling worker image"
            ) from exc
    raise RuntimeError(
        f"Text extraction is not configured for {media_type}; use a Docling worker for OCR"
    )


def _location(line_number: int, quote: str) -> dict:
    return {
        "page": 1,
        "line_start": line_number,
        "line_end": line_number,
        "quote": quote[:500],
    }


def deterministic_facts(
    text: str, patient_id: str, document_type: DocumentType
) -> list[dict]:
    facts: list[dict] = []
    if document_type == DocumentType.lab_report:
        pattern = re.compile(
            r"^\s*([A-Za-z][A-Za-z0-9 ]{1,40})\s*[:,-]?\s+(-?\d+(?:\.\d+)?)\s*([%A-Za-z/µ]+)?(?:\s+([LH]|high|low|normal))?",
            re.I,
        )
        for number, line in enumerate(text.splitlines(), 1):
            match = pattern.search(line)
            if not match:
                continue
            name, value, unit, status = match.groups()
            normalized = LOINC_SUBSET.get(name.strip().lower())
            facts.append(
                {
                    "patient_id": patient_id,
                    "fact_type": "lab",
                    "test_or_finding": normalized[1] if normalized else name.strip(),
                    "normalized_code": normalized[0] if normalized else None,
                    "value": value,
                    "unit": unit,
                    "status": status.lower() if status else None,
                    "observed_date": date.today(),
                    "evidence_location": _location(number, line),
                    "confidence": 0.85 if normalized else 0.65,
                    "details": {
                        "terminology": "LOINC",
                        "terminology_version": LOINC_SUBSET_VERSION,
                    },
                }
            )
    elif document_type == DocumentType.medication_list:
        for number, line in enumerate(text.splitlines(), 1):
            for name, code in RXNORM_SUBSET.items():
                if name in line.lower():
                    dose = re.search(r"(\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml))", line, re.I)
                    facts.append(
                        {
                            "patient_id": patient_id,
                            "fact_type": "medication",
                            "test_or_finding": name.title(),
                            "normalized_code": code,
                            "value": dose.group(1) if dose else None,
                            "observed_date": date.today(),
                            "evidence_location": _location(number, line),
                            "confidence": 0.85,
                            "details": {
                                "terminology": "RxNorm",
                                "action": "documented",
                            },
                        }
                    )
    elif document_type in {
        DocumentType.radiology_report,
        DocumentType.discharge_summary,
    }:
        for number, line in enumerate(text.splitlines(), 1):
            if line.strip() and (
                "impression" in line.lower() or "diagnosis" in line.lower()
            ):
                facts.append(
                    {
                        "patient_id": patient_id,
                        "fact_type": (
                            "radiology"
                            if document_type == DocumentType.radiology_report
                            else "discharge"
                        ),
                        "test_or_finding": line.split(":", 1)[-1].strip(),
                        "observed_date": date.today(),
                        "evidence_location": _location(number, line),
                        "confidence": 0.7,
                        "details": {},
                    }
                )
    return facts


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
                {"role": "user", "content": json.dumps(user_payload)},
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

    def __init__(self, endpoint: str, model: str, api_key: str | None = None):
        self.endpoint, self.name, self.api_key = endpoint, model, api_key

    def extract(self, payload: dict) -> list[dict]:
        content = call_openai_compatible_json(
            self.endpoint,
            self.name,
            self.api_key,
            "Extract only evidence-supported facts. Return JSON {facts: [...]} and retain evidence locations.",
            payload,
        )
        return content.get("facts", [])
