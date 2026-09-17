from __future__ import annotations

import os
from datetime import date
from uuid import uuid4

os.environ.setdefault("ENCRYPTION_KEY", "kX8f1QhZ2sYbYQxvV3v4qz5rN0jz3sVfE9pJcRZmA0g=")

from src.meditrace.analysis import compute_trends, detect_contradictions, reliability_tier
from src.meditrace.crypto import hash_password, verify_password
from src.meditrace.models import Fact


def test_password_hash_roundtrip_and_rejects_wrong_password():
    encoded = hash_password("correct horse battery staple", iterations=10_000)
    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong password", encoded)


def _fact(**overrides) -> Fact:
    defaults = dict(
        id=uuid4(),
        source_document_id=uuid4(),
        patient_id="SYN-1",
        fact_type="lab",
        test_or_finding="HbA1c",
        normalized_code="4548-4",
        value="7.0",
        unit="%",
        status=None,
        observed_date=date(2026, 1, 1),
        evidence_location={"page": 1},
        confidence=0.9,
        details={},
    )
    defaults.update(overrides)
    return Fact(**defaults)


def test_compute_trends_flags_rising_hba1c():
    facts = [
        _fact(value="7.0", observed_date=date(2026, 1, 1)),
        _fact(value="7.6", observed_date=date(2026, 2, 1)),
    ]
    trends = compute_trends(facts)
    assert len(trends) == 1
    assert trends[0].direction == "rising"
    assert trends[0].magnitude == 0.6


def test_compute_trends_ignores_small_changes():
    facts = [
        _fact(value="7.0", observed_date=date(2026, 1, 1)),
        _fact(value="7.1", observed_date=date(2026, 2, 1)),
    ]
    assert compute_trends(facts) == []


def test_detect_contradictions_conflicting_results_across_documents():
    doc_a, doc_b = uuid4(), uuid4()
    facts = [
        _fact(source_document_id=doc_a, value="7.0", observed_date=date(2026, 1, 1)),
        _fact(source_document_id=doc_b, value="9.0", observed_date=date(2026, 1, 1)),
    ]
    flags = detect_contradictions(facts)
    assert any(f.kind == "conflicting_results" for f in flags)


def test_detect_contradictions_dose_conflict():
    facts = [
        _fact(
            fact_type="medication",
            test_or_finding="Metformin",
            value="500mg",
            observed_date=date(2026, 1, 1),
        ),
        _fact(
            fact_type="medication",
            test_or_finding="Metformin",
            value="1000mg",
            observed_date=date(2026, 2, 1),
        ),
    ]
    flags = detect_contradictions(facts)
    assert any(f.kind == "dose_conflict" for f in flags)


def test_reliability_tier():
    assert reliability_tier(_fact(normalized_code="4548-4", confidence=0.9)) == "high"
    assert reliability_tier(_fact(normalized_code=None, confidence=0.75)) == "medium"
    assert reliability_tier(_fact(normalized_code=None, confidence=0.3)) == "low"
