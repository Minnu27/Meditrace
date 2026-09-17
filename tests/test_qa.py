from __future__ import annotations

import os
from datetime import date
from uuid import uuid4

os.environ.setdefault("ENCRYPTION_KEY", "kX8f1QhZ2sYbYQxvV3v4qz5rN0jz3sVfE9pJcRZmA0g=")

from src.meditrace.models import Fact
from src.meditrace.qa import answer_question


def _fact(**overrides) -> Fact:
    defaults = dict(
        id=uuid4(),
        source_document_id=uuid4(),
        patient_id="SYN-1",
        fact_type="lab",
        test_or_finding="HbA1c",
        normalized_code="4548-4",
        value="7.2",
        unit="%",
        status="high",
        observed_date=date(2026, 1, 1),
        evidence_location={"page": 1},
        confidence=0.9,
        details={},
    )
    defaults.update(overrides)
    return Fact(**defaults)


def test_answer_question_cites_retrieved_facts():
    facts = [_fact(), _fact(test_or_finding="Glucose", value="110", unit="mg/dL")]
    result = answer_question(facts, "What was the HbA1c?")
    assert not result.insufficient_evidence
    assert result.cited_fact_ids == [str(facts[0].id)]
    assert "HbA1c" in result.answer


def test_answer_question_insufficient_evidence_for_unrelated_question():
    facts = [_fact()]
    result = answer_question(facts, "asdkjhasd zzz qqq")
    assert result.insufficient_evidence
    assert result.cited_fact_ids == []
    assert "Not enough evidence" in result.answer


def test_answer_question_no_facts_at_all():
    result = answer_question([], "What was the HbA1c?")
    assert "Not enough evidence" in result.answer
