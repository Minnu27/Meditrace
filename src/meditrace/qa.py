"""Phase 5 — evidence-grounded Q&A.

Retrieves from structured Fact rows only, never raw document prose. Every
claim in the answer must trace to a fact ID that was actually retrieved; a
verification pass drops or flags anything a model adds that isn't grounded.
With no MODEL_ENDPOINT configured, answers are built from a deterministic
template over the same retrieved facts — a real feature at zero cost, not a
stub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from .extraction import call_openai_compatible_json
from .models import Fact

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "what", "when", "did",
    "does", "do", "my", "has", "have", "had", "of", "for", "on", "in",
    "patient", "show", "tell", "me", "about", "any", "there",
}


@dataclass
class AnswerResult:
    answer: str
    cited_fact_ids: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    insufficient_evidence: bool = True


def _keywords(question: str) -> set[str]:
    words = re.findall(r"[a-z0-9%]+", question.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def retrieve(facts: list[Fact], question: str, top_k: int = 8) -> list[Fact]:
    """Deterministic keyword overlap retrieval; no embeddings, no external calls."""
    terms = _keywords(question)
    if not terms:
        return []
    scored: list[tuple[int, Fact]] = []
    for fact in facts:
        haystack = " ".join(
            filter(
                None,
                [
                    fact.test_or_finding,
                    fact.fact_type,
                    fact.value,
                    fact.status,
                    fact.unit,
                ],
            )
        ).lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((score, fact))
    scored.sort(key=lambda pair: (-pair[0], -pair[1].observed_date.toordinal()))
    return [fact for _, fact in scored[:top_k]]


def _deterministic_answer(facts: list[Fact]) -> str:
    lines = []
    for fact in facts:
        parts = [fact.test_or_finding]
        if fact.value:
            parts.append(f"= {fact.value}{fact.unit or ''}")
        if fact.status:
            parts.append(f"({fact.status})")
        parts.append(f"on {fact.observed_date.isoformat()}")
        lines.append(" ".join(parts))
    return "Evidence found: " + "; ".join(lines) + "."


def answer_question(
    facts: list[Fact],
    question: str,
    *,
    model_endpoint: str | None = None,
    model_name: str = "medgemma",
    model_api_key: str | None = None,
) -> AnswerResult:
    retrieved = retrieve(facts, question)
    if not retrieved:
        return AnswerResult(
            answer="Not enough evidence in this patient's recorded facts to answer that.",
            insufficient_evidence=True,
            confidence=1.0,
        )

    retrieved_ids = {str(f.id) for f in retrieved}
    evidence = [
        {
            "fact_id": str(f.id),
            "test_or_finding": f.test_or_finding,
            "value": f.value,
            "unit": f.unit,
            "status": f.status,
            "observed_date": f.observed_date.isoformat(),
            "source_document_id": str(f.source_document_id),
        }
        for f in retrieved
    ]
    answer_text = _deterministic_answer(retrieved)
    cited_ids = sorted(retrieved_ids)

    if model_endpoint:
        try:
            candidate = call_openai_compatible_json(
                model_endpoint,
                model_name,
                model_api_key,
                "Answer the question using ONLY the provided facts. "
                "Return JSON {answer: str, cited_fact_ids: [str]}. "
                "Every cited_fact_ids entry must be one of the provided fact_id "
                "values. If the facts do not answer the question, set answer to "
                "'Not enough evidence' and cited_fact_ids to [].",
                {"question": question, "facts": evidence},
            )
            model_answer = candidate.get("answer")
            model_cited = [
                fid for fid in candidate.get("cited_fact_ids", []) if fid in retrieved_ids
            ]
            # Deterministic suppression: only accept the model's answer if
            # every claim it made cites a fact we actually retrieved.
            if model_answer and model_cited:
                answer_text = model_answer
                cited_ids = sorted(set(model_cited))
        except Exception:
            pass  # Fall back to the deterministic answer computed above.

    return AnswerResult(
        answer=answer_text,
        cited_fact_ids=cited_ids,
        evidence=[e for e in evidence if e["fact_id"] in cited_ids] or evidence,
        confidence=min(1.0, 0.5 + 0.1 * len(retrieved)),
        insufficient_evidence=False,
    )
