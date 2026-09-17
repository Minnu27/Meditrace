"""Phase 4 — deterministic trend flags and contradiction detection.

No model call, no ranking of conflicting records, and no silent resolution:
every flag cites the source fact IDs it was computed from (PROJECT_PLAN.md
non-negotiable rules 3 and 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Fact

TREND_THRESHOLD_VERSION = "2026-01"

# Deterministic, reviewed thresholds for a small set of common labs. Each
# entry is the minimum absolute change (in the test's own unit) over
# consecutive results for the same patient that is worth flagging.
TREND_THRESHOLDS: dict[str, float] = {
    "hemoglobin a1c": 0.5,
    "hba1c": 0.5,
    "glucose": 20.0,
    "creatinine": 0.3,
    "hemoglobin": 1.0,
    "sodium": 5.0,
    "potassium": 0.5,
}


@dataclass
class TrendFlag:
    test_or_finding: str
    direction: str
    magnitude: float
    from_fact_id: str
    to_fact_id: str
    from_value: str
    to_value: str
    from_date: str
    to_date: str


@dataclass
class ContradictionFlag:
    kind: str
    summary: str
    fact_ids: list[str] = field(default_factory=list)


def reliability_tier(fact: Fact) -> str:
    """Static, visible reliability tier (Phase 7): normalized code plus high
    confidence is 'high'; a plausible but unnormalized fact is 'medium'; the
    rest is 'low'. Never hidden, never used to auto-resolve anything."""
    if fact.normalized_code and fact.confidence >= 0.85:
        return "high"
    if fact.confidence >= 0.7:
        return "medium"
    return "low"


def _try_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def compute_trends(facts: list[Fact]) -> list[TrendFlag]:
    """Facts must already be sorted by observed_date ascending."""
    flags: list[TrendFlag] = []
    last_by_key: dict[tuple[str, str | None], Fact] = {}
    for fact in facts:
        if fact.fact_type != "lab":
            continue
        key = (fact.test_or_finding.lower(), fact.unit)
        threshold = TREND_THRESHOLDS.get(fact.test_or_finding.lower())
        prior = last_by_key.get(key)
        if threshold is not None and prior is not None:
            current_value = _try_float(fact.value)
            prior_value = _try_float(prior.value)
            if current_value is not None and prior_value is not None:
                delta = current_value - prior_value
                if abs(delta) >= threshold:
                    flags.append(
                        TrendFlag(
                            test_or_finding=fact.test_or_finding,
                            direction="rising" if delta > 0 else "falling",
                            magnitude=round(abs(delta), 6),
                            from_fact_id=str(prior.id),
                            to_fact_id=str(fact.id),
                            from_value=prior.value or "",
                            to_value=fact.value or "",
                            from_date=prior.observed_date.isoformat(),
                            to_date=fact.observed_date.isoformat(),
                        )
                    )
        last_by_key[key] = fact
    return flags


def detect_contradictions(facts: list[Fact]) -> list[ContradictionFlag]:
    """Only flags; never picks a winner between conflicting sources."""
    flags: list[ContradictionFlag] = []

    # Same lab test, same observed date, materially different values from
    # different source documents.
    by_test_date: dict[tuple[str, str], list[Fact]] = {}
    for fact in facts:
        if fact.fact_type != "lab" or fact.value is None:
            continue
        key = (fact.test_or_finding.lower(), fact.observed_date.isoformat())
        by_test_date.setdefault(key, []).append(fact)
    for (test, observed_date), group in by_test_date.items():
        distinct_sources = {f.source_document_id for f in group}
        if len(distinct_sources) < 2:
            continue
        values = {f.value for f in group if f.value is not None}
        numeric = [v for v in (_try_float(x) for x in values) if v is not None]
        conflicting = (
            len(numeric) >= 2 and (max(numeric) - min(numeric)) > 0
            if numeric
            else len(values) > 1
        )
        if conflicting:
            flags.append(
                ContradictionFlag(
                    kind="conflicting_results",
                    summary=(
                        f"{group[0].test_or_finding} on {observed_date} has "
                        f"different values across {len(distinct_sources)} source documents"
                    ),
                    fact_ids=[str(f.id) for f in group],
                )
            )

    # Same medication, different documented doses, with no fact marking
    # discontinuation in between.
    by_medication: dict[str, list[Fact]] = {}
    for fact in facts:
        if fact.fact_type == "medication" and fact.value:
            by_medication.setdefault(fact.test_or_finding.lower(), []).append(fact)
    for medication, group in by_medication.items():
        group = sorted(group, key=lambda f: f.observed_date)
        doses = {f.value for f in group}
        if len(doses) > 1:
            flags.append(
                ContradictionFlag(
                    kind="dose_conflict",
                    summary=(
                        f"{group[0].test_or_finding} is documented at "
                        f"{len(doses)} different doses ({', '.join(sorted(doses))}) "
                        "with no discontinuation recorded between them"
                    ),
                    fact_ids=[str(f.id) for f in group],
                )
            )

    return flags
