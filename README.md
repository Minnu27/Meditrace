# MediTrace AI

MediTrace is an evidence-first workspace for turning synthetic and de-identified research documents into a reviewable longitudinal record. The repository is being rebuilt in the phases described in [`PROJECT_PLAN.md`](PROJECT_PLAN.md); this release provides the **Phase 0 foundation and Phase 1–3 workflow**: queued extraction, typed text facts, optional model submission, and evidence-linked timelines.

> **Decision-support prototype on synthetic/de-identified research data — not a diagnostic device.** Do not upload identifiable patient information or use this software for clinical care.

## What works now

- A FastAPI service with upload, document register, source retrieval, and evidence-fact endpoints.
- A stable fact contract carrying test/finding, value, unit, reference range, status, date, source document, location, and confidence.
- Page/line/quote/bounding-box evidence coordinates as structured data rather than an afterthought.
- SHA-256 fingerprints for immutable source identification and rollback-safe file persistence.
- SQLAlchemy storage that defaults to SQLite for a zero-setup demo and supports the Phase 0 Postgres service through `DATABASE_URL`.
- A responsive “Industry” interface with the research-only disclaimer visible at all times.
- Extraction requests create durable jobs; parsing runs only in a separate worker process.
- Deterministic lab, medication, radiology, and discharge extraction supports text sources with explicit `unknown` classification.
- A configured OpenAI-compatible MedGemma gateway can receive source text through an explicit endpoint; returned facts are validated before persistence.
- Patient timelines group facts by month or visit and calculate prior numeric deltas without a model.

The pre-existing CXR classifier research modules remain under `src/` for reference, but they are not connected to the MediTrace document workflow and are not represented as clinical functionality.

## Run locally

### Zero-infrastructure mode

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn src.meditrace.api:app --reload
# In another terminal, process queued extraction jobs:
python -m src.meditrace.worker
```

Open <http://127.0.0.1:8000>. SQLite metadata is written to `meditrace.db`; source documents are stored beneath `data/documents/`. Both are ignored by Git.

### Postgres mode

```bash
cp .env.example .env
docker compose up -d postgres
set -a; source .env; set +a
uvicorn src.meditrace.api:app --reload
```

The compose service is intended for local development only. Change its credentials before using it outside an isolated development machine.

### MySQL and Vercel

See [MySQL setup and deployment](docs/MYSQL_DEPLOYMENT.md) for local Windows configuration, hosted TLS connections, Vercel variables, and the separate extraction worker.

The API includes `api/index.py` and `vercel.json` for Vercel. MySQL is supported through PyMySQL, using `DATABASE_URL`/`MYSQL_URL` or individual `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER`, and `MYSQL_PASSWORD` settings. Local `.env` files load automatically.

Use `OBJECT_STORE_BACKEND=database` to persist small source files in `stored_objects`, accessible to both the API and worker. This is the Vercel default when a non-SQLite database is configured. Local filesystem storage remains the local default; `/tmp` storage and SQLite on Vercel remain ephemeral. Existing files are not automatically migrated.

The health route probes its dependencies and returns HTTP 503 when unavailable. Database connection/schema initialization failures on Vercel are reported without hiding the web interface. The worker remains a separate process; a Vercel deployment alone does not process queued jobs.

## API contract

Interactive API documentation is available at `/docs`.

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/health` | Dependency status |
| `POST` | `/api/documents` | Register and persist an allowed source document |
| `GET` | `/api/documents` | List documents, optionally filtered by `patient_id` |
| `GET` | `/api/documents/{id}` | Retrieve document metadata and linked facts |
| `GET` | `/api/documents/{id}/content` | Retrieve original source bytes |
| `POST` | `/api/documents/{id}/facts` | Add a schema-validated, evidence-linked fact |
| `POST` | `/api/documents/{id}/extract` | Queue background extraction |
| `GET` | `/api/jobs/{id}` | Read extraction status/errors |
| `POST` | `/api/documents/{id}/submit-to-model` | Explicitly submit text to the configured model |
| `GET` | `/api/patients/{patient_id}/timeline` | Evidence-linked timeline and deterministic deltas |

## Connect your database and model

Set `DATABASE_URL` to a SQLAlchemy Postgres URL. The `postgresql://` form is normalized to the installed psycopg driver. Use a dedicated, access-controlled database and environment secrets, never committed credentials. Set `MODEL_ENDPOINT`, `MODEL_API_KEY`, and `MODEL_NAME` for an OpenAI-compatible gateway. No source is sent automatically: submission is an explicit per-document operation.

Example evidence fact:

```json
{
  "patient_id": "SYN-1048",
  "fact_type": "lab",
  "test_or_finding": "HbA1c",
  "normalized_code": "4548-4",
  "value": "7.2",
  "unit": "%",
  "reference_range": "4.0-5.6",
  "status": "high",
  "observed_date": "2026-08-30",
  "evidence_location": {
    "page": 1,
    "line_start": 14,
    "line_end": 14,
    "quote": "HbA1c 7.2 %",
    "bounding_box": {"page": 1, "x": 72, "y": 281, "width": 188, "height": 19}
  },
  "confidence": 0.98
}
```

## Verification

```bash
pytest -q
python -m src.selftest
```

The first command covers the MediTrace upload-to-evidence loop on synthetic text. The second exercises the legacy CXR research pipeline on generated images only.

## Scope boundaries

- **Data:** public, synthetic, or properly de-identified research datasets only. Dataset access terms still apply.
- **Clinical use:** prohibited. This prototype does not diagnose, recommend treatment, or replace professional review.
- **Current phase:** Phase 0 plus the Phase 1–3 text workflow. Scanned-image OCR requires a Docling-enabled worker deployment; the base worker fails explicitly rather than fabricating text. Contradiction detection, grounded Q&A, and imaging integration remain future work.
- **Security:** the local build has no production authentication, encryption/key management, malware scanning, audit log, or deployment hardening. Do not expose it publicly.
