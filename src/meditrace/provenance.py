"""Content commitments for facts and answers.

Every fact and every persisted answer carries a SHA-256 commitment over its
canonical content plus a random salt. The salt is stored encrypted, so a copy
of the database (or of the audit log, or of an anchored Merkle root) does not
let anyone brute-force low-entropy values such as "7.2" back out of a hash.
Deleting a fact together with its salt leaves every hash that referenced it
unlinkable to the deleted content — which is why only hashes, never facts,
are ever anchored externally.

Canonicalisation rules are deliberately boring so the hash survives a round
trip through SQLite, Postgres, or MySQL:

* JSON with sorted keys, no whitespace, UTF-8.
* UUIDs as lowercase hyphenated strings, dates as ISO ``YYYY-MM-DD``.
* Confidence as a 4-decimal string (MySQL ``FLOAT`` is single precision).
* Timestamps truncated to whole seconds, UTC, ``...Z`` (MySQL ``DATETIME``
  drops microseconds by default).
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import secrets
from typing import Any

COMMITMENT_VERSION = "mt-commit-v1"

# Extractor identities recorded on every fact.
DETERMINISTIC_EXTRACTOR = "meditrace-deterministic"
DETERMINISTIC_EXTRACTOR_VERSION = "2026.09"
MANUAL_EXTRACTOR = "manual-entry"
LEGACY_EXTRACTOR = "legacy-unrecorded"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    ).encode("utf-8")


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return canonical_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_salt() -> str:
    return secrets.token_hex(16)


def canonical_timestamp(value: datetime) -> str:
    """Whole-second UTC timestamp; naive datetimes (SQLite) are treated as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def utc_now_seconds() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def prompt_version(system_prompt: str, label: str) -> str:
    """Human label plus a short digest of the exact prompt text, so an edited
    prompt can never masquerade as the old version."""
    return f"{label}+{sha256_hex(system_prompt.encode('utf-8'))[:12]}"


# ---------------------------------------------------------------- facts


def fact_commitment_payload(fact) -> dict:
    return {
        "v": COMMITMENT_VERSION,
        "kind": "fact",
        "fact_id": str(fact.id),
        "patient_id": fact.patient_id,
        "source_document_id": str(fact.source_document_id),
        "source_sha256": fact.source_sha256,
        "fact_type": fact.fact_type,
        "test_or_finding": fact.test_or_finding,
        "normalized_code": fact.normalized_code,
        "value": fact.value,
        "unit": fact.unit,
        "reference_range": fact.reference_range,
        "status": fact.status,
        "observed_date": fact.observed_date.isoformat() if fact.observed_date else None,
        "evidence_location": fact.evidence_location or {},
        "confidence": f"{float(fact.confidence):.4f}",
        "details": fact.details or {},
        "extractor": fact.extractor,
        "extractor_version": fact.extractor_version,
        "prompt_version": fact.prompt_version,
        "salt": fact.commitment_salt,
    }


def compute_fact_hash(fact) -> str:
    return sha256_hex(canonical_json(fact_commitment_payload(fact)))


def seal_fact(
    fact,
    *,
    source_sha256: str,
    extractor: str,
    extractor_version: str | None,
    prompt_version: str | None = None,
) -> str:
    """Stamp provenance onto a not-yet-committed Fact and return its hash.

    The fact must already have its ``id`` set (callers pass ``id=uuid4()``)
    because the ID is part of the commitment.
    """
    import uuid

    if fact.id is None:
        fact.id = uuid.uuid4()
    fact.source_sha256 = source_sha256
    fact.extractor = extractor
    fact.extractor_version = extractor_version
    fact.prompt_version = prompt_version
    if not fact.commitment_salt:
        fact.commitment_salt = new_salt()
    fact.content_hash = compute_fact_hash(fact)
    return fact.content_hash


# -------------------------------------------------------------- answers


def answer_commitment_payload(answer) -> dict:
    return {
        "v": COMMITMENT_VERSION,
        "kind": "answer",
        "answer_id": str(answer.id),
        "patient_id": answer.patient_id,
        "question": answer.question,
        "answer": answer.answer_text,
        "cited_fact_ids": sorted(answer.cited_fact_ids or []),
        "cited_fact_hashes": answer.cited_fact_hashes or {},
        "insufficient_evidence": bool(answer.insufficient_evidence),
        "answerer": answer.answerer,
        "answerer_version": answer.answerer_version,
        "prompt_version": answer.prompt_version,
        "salt": answer.commitment_salt,
    }


def compute_answer_hash(answer) -> str:
    return sha256_hex(canonical_json(answer_commitment_payload(answer)))
