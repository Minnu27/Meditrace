# MediTrace AI — Execution Checklist

This checklist turns the product roadmap into repository-sized increments. A checked item is implemented and testable here; unchecked work is not presented as shipped.

## Phase 0 — Foundations (current)

- [x] Canonical document and evidence-linked fact schemas.
- [x] FastAPI ingestion, document listing, source download, and fact creation.
- [x] SQLAlchemy persistence with Postgres configuration and local SQLite fallback.
- [x] Durable local object-store adapter with atomic writes.
- [x] Upload and source-register web interface using the Industry visual language.
- [x] Persistent in-product and README research-use disclaimer.
- [x] Synthetic API integration test for the complete upload → fact → evidence loop.
- [ ] S3/MinIO adapter (local filesystem is the intentional first adapter).
- [ ] Alembic migrations before the schema begins evolving.

**Gate:** files go in, can be retrieved byte-for-byte, metadata persists, and facts cannot exist without document-scoped evidence.

## Phase 1 — Lab extraction

- [ ] Introduce a background job boundary; never run parsing in the request process.
- [ ] Parse text-native and scanned PDF lab reports with Docling.
- [ ] Preserve page, line, and bounding-box coordinates through extraction.
- [ ] Add a model-provider interface and MedGemma structured-output implementation.
- [ ] Normalize lab tests against a versioned LOINC reference subset.
- [ ] Add the split document/evidence review UI.
- [ ] Hand-check a fixed 20-document synthetic/de-identified evaluation set and publish the rubric.

## Phase 2 — Additional text documents

- [ ] Document-type classification with explicit `unknown` behavior.
- [ ] Medication schema and RxNorm normalization.
- [ ] Radiology report text schema.
- [ ] Discharge summary handling through shared fact types.

## Phase 3 — Timeline

- [ ] Patient-scoped chronological query and visit/month grouping.
- [ ] Deterministic prior-value deltas; no model call for sorting or arithmetic.
- [ ] Timeline UI with source links on every entry.

## Phase 4 — Trends and contradictions

- [x] Versioned threshold table for a small, reviewed set of common lab trends. (`analysis.TREND_THRESHOLDS`, `TREND_THRESHOLD_VERSION`)
- [x] Candidate grouping followed by deterministic conflict checks. (`analysis.detect_contradictions`)
- [x] Medication-dose contradiction rules. Allergy contradiction rules are **not implemented** — there is no allergy fact type in the extraction pipeline yet.
- [x] Flag cards cite both sources and never auto-resolve a conflict. (`GET /api/patients/{id}/flags`, always returns both/all conflicting fact IDs)

**Gate status:** met for the implemented rules; not yet hand-checked against a reviewed evaluation set.

## Phase 5 — Evidence-grounded Q&A (v1 gate)

- [x] Retrieve from structured facts, not unbounded raw prose. (`qa.retrieve`, keyword-overlap over Fact columns only)
- [x] Require fact IDs for every generated claim. (`cited_fact_ids`, always a subset of retrieved fact IDs)
- [x] Deterministic verification/suppression pass with “not enough evidence” fallback. (a model's citations are dropped unless they match retrieved fact IDs; empty retrieval short-circuits before any model call)
- [ ] Fixed 15–20 question evaluation set in CI. **Not implemented** — `tests/test_qa.py` covers unit behavior, not a reviewed evaluation rubric.

**Gate status:** the mechanism is real and tested; the v1 gate's evaluation-set requirement is still open.

## Phase 6 — Chest X-ray module (v1.5)

- [ ] CXR Foundation embedding adapter and separately evaluated classifier head. **Not implemented** — `imaging.py` wires the existing DenseNet121 classifier (`src/model.py`), not a CXR Foundation embedding adapter.
- [ ] MedGemma vision adapter. **Not implemented.** Model/version provenance (checkpoint filename + SHA-256) is recorded for the DenseNet path that does exist.
- [x] Imaging facts use the same evidence and timeline contracts. (`POST /api/documents/{id}/cxr-analyze` persists a `Fact` with `fact_type="imaging"`, appears in the same timeline as text-derived facts)
- [x] No CT, MRI, pathology, dermatology, or retinal claims in this phase. (media-type check restricts input to PNG/JPEG; response always carries the disclaimer)
- **No pretrained checkpoint ships with this repo.** The endpoint returns 503 until `CXR_MODEL_PATH` points at a checkpoint you train with `notebooks/CXR_Sentinel_Full.ipynb` against real, license-compliant data.

## Phases 7–9 — Optional differentiation and packaging

- [ ] Relational fact graph only when a demonstrated query requires it. Not needed yet.
- [x] Static, visible document reliability tiers. (`analysis.reliability_tier`, shown in the timeline UI)
- [x] Append-only audit events for document/fact reads and writes, logins, timeline/flags/ask views, and CXR analysis. (`audit_events`, `GET /api/audit`, admin-only, no update/delete route exists). Not yet broken out into the exact query/answer/view/evidence/outcome/model-version taxonomy this line originally specified.
- [x] Consistent confidence and insufficient-evidence displays. (fact `confidence` + `reliability_tier` throughout; `AskResponse.insufficient_evidence` surfaced in the UI)
- [ ] Three-minute walkthrough using conflicting synthetic sources only. **Not implemented.**

## Non-negotiable release rules

1. Never ingest real identifiable patient records.
2. Never represent an unchecked phase as implemented.
3. Every displayed fact must retain a source document and precise evidence location.
4. Never rank conflicting records or silently choose a winner.
5. Imaging is cut before trends, contradictions, or grounded answers if schedule slips.
