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
        # Only compare doses documented after the most recent discontinuation.
        stops = [i for i, f in enumerate(group) if (f.details or {}).get("action") == "discontinued"]
        if stops:
            group = group[stops[-1] + 1 :]
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


# ------------------------------------------------------------------ gaps
#
# Monitoring-gap rules find what the *record* does not contain, e.g. a
# medication with no follow-up lab. They are illustrative rules for a
# synthetic demo, NOT clinical guidance, and a gap may simply mean a document
# was never uploaded. Like every other flag, a gap cites the facts it was
# computed from and never asserts anything about the patient.

GAP_RULES_VERSION = "2026-09-demo"

# medication -> list of requirements; each requirement is a set of LOINC codes
# any one of which satisfies it, plus the window after the medication's first
# documented date in which a result is expected.
MONITORING_RULES: dict[str, dict] = {
    "metformin": {
        "rule_id": "metformin-glycemic-followup",
        "requirements": [({"4548-4"}, "Hemoglobin A1c")],
        "within_days": 180,
    },
    "lisinopril": {
        "rule_id": "lisinopril-renal-followup",
        "requirements": [({"2160-0"}, "Creatinine"), ({"2823-3"}, "Potassium")],
        "within_days": 90,
    },
    "atorvastatin": {
        "rule_id": "atorvastatin-lipid-followup",
        "requirements": [({"13457-7", "2093-3"}, "Lipid panel (LDL or total cholesterol)")],
        "within_days": 365,
    },
}
ABNORMAL_REPEAT_WINDOW_DAYS = 180


@dataclass
class GapFlag:
    kind: str
    rule_id: str
    rule_version: str
    summary: str
    due_by: str
    fact_ids: list[str] = field(default_factory=list)
    later_fact_ids: list[str] = field(default_factory=list)


def _lab_key(fact: Fact) -> str:
    return fact.normalized_code or fact.test_or_finding.lower()


def detect_gaps(facts: list[Fact], as_of=None) -> list[GapFlag]:
    """Facts must already be sorted by observed_date ascending."""
    from datetime import date, timedelta

    as_of = as_of or date.today()
    labs = [f for f in facts if f.fact_type == "lab"]
    gaps: list[GapFlag] = []

    # 1. Medication with no follow-up monitoring lab in its window.
    started: dict[str, list[Fact]] = {}
    for fact in facts:
        if fact.fact_type != "medication":
            continue
        if (fact.details or {}).get("action") == "discontinued":
            continue
        started.setdefault(fact.test_or_finding.lower(), []).append(fact)
    for medication, med_facts in started.items():
        rule = MONITORING_RULES.get(medication)
        if not rule:
            continue
        start = med_facts[0].observed_date
        due_by = start + timedelta(days=rule["within_days"])
        if as_of <= due_by:
            continue  # not yet due; absence is not a gap
        for codes, label in rule["requirements"]:
            matching = [f for f in labs if f.normalized_code in codes and f.observed_date > start]
            in_window = [f for f in matching if f.observed_date <= due_by]
            if in_window:
                continue
            gaps.append(
                GapFlag(
                    kind="missing_follow_up",
                    rule_id=rule["rule_id"],
                    rule_version=GAP_RULES_VERSION,
                    summary=(
                        f"{med_facts[0].test_or_finding} first documented {start.isoformat()}; "
                        f"no {label} result recorded within {rule['within_days']} days"
                        + (f" (a later one exists on {matching[0].observed_date.isoformat()})" if matching else "")
                    ),
                    due_by=due_by.isoformat(),
                    fact_ids=[str(f.id) for f in med_facts],
                    later_fact_ids=[str(f.id) for f in matching],
                )
            )

    # 2. Abnormal lab result that is never repeated within the window.
    for index, fact in enumerate(labs):
        if fact.status not in {"high", "low", "abnormal"}:
            continue
        due_by = fact.observed_date + timedelta(days=ABNORMAL_REPEAT_WINDOW_DAYS)
        if as_of <= due_by:
            continue
        repeats = [
            f
            for f in labs[index + 1 :]
            if _lab_key(f) == _lab_key(fact) and fact.observed_date < f.observed_date <= due_by
        ]
        if repeats:
            continue
        gaps.append(
            GapFlag(
                kind="abnormal_without_repeat",
                rule_id="abnormal-lab-repeat",
                rule_version=GAP_RULES_VERSION,
                summary=(
                    f"{fact.test_or_finding} was {fact.status} ({fact.value}{fact.unit or ''}) on "
                    f"{fact.observed_date.isoformat()}; no repeat result recorded within "
                    f"{ABNORMAL_REPEAT_WINDOW_DAYS} days"
                ),
                due_by=due_by.isoformat(),
                fact_ids=[str(fact.id)],
            )
        )
    return gaps
