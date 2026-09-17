# MediTrace AI

MediTrace is an evidence-first workspace for turning synthetic and de-identified research documents into a reviewable longitudinal record. The repository is being rebuilt in the phases described in [`PROJECT_PLAN.md`](PROJECT_PLAN.md); this release provides the **Phase 0 foundation, the Phase 1–3 workflow, a first pass at Phase 4–5 (trends, contradictions, evidence-grounded Q&A), an optional Phase 6 imaging hook, and Phase 7's audit log** — see the honest per-phase status in `PROJECT_PLAN.md` for exactly what's real versus still open.

> **Decision-support prototype on synthetic/de-identified research data — not a diagnostic device.** Do not upload identifiable patient information or use this software for clinical care.

## What works now

- A FastAPI service with upload, document register, source retrieval, and evidence-fact endpoints, all behind bearer-token authentication and role-based write access (`admin`/`clinician`/`reviewer`) — see [`docs/SECURITY.md`](docs/SECURITY.md).
- A stable fact contract carrying test/finding, value, unit, reference range, status, date, source document, location, and confidence.
- Page/line/quote/bounding-box evidence coordinates as structured data rather than an afterthought.
- SHA-256 fingerprints for immutable source identification and rollback-safe file persistence.
- SQLAlchemy storage that defaults to SQLite for a zero-setup demo and supports Postgres or MySQL through `DATABASE_URL` / `MYSQL_*`.
- Sensitive columns (document filenames, fact values/details, evidence quotes, raw stored document bytes) are encrypted at rest with Fernet; see `docs/SECURITY.md` for exactly which columns and why some (like `patient_id`) intentionally aren't.
- An append-only audit log (`audit_events`, `GET /api/audit`, admin-only) records who read or wrote what patient data and when.
- A responsive “Industry” interface with the research-only disclaimer visible at all times, a sign-in gate, and panels for trends/contradictions and evidence-grounded Q&A.
- Extraction requests create durable jobs; parsing runs only in a separate worker process.
- Deterministic lab, medication, radiology, and discharge extraction supports text sources with explicit `unknown` classification.
- A configured OpenAI-compatible MedGemma gateway can receive source text through an explicit endpoint; returned facts are validated before persistence. Without one configured, extraction, trend/contradiction flags, and Q&A still work fully deterministically at zero API cost.
- Patient timelines group facts by month or visit, calculate prior numeric deltas without a model, and show a static reliability tier per fact.
- Deterministic trend flags (e.g. a rising HbA1c) and contradiction flags (conflicting results across documents, conflicting medication doses) — flags only, never an auto-resolved winner (`GET /api/patients/{id}/flags`).
- Evidence-grounded Q&A that retrieves only from this patient's structured facts, cites fact IDs on every claim, and returns "not enough evidence" rather than guessing (`POST /api/patients/{id}/ask`).
- An optional chest X-ray inference endpoint (`POST /api/documents/{id}/cxr-analyze`) that runs a real model against a checkpoint you train — it does not ship a pretrained checkpoint and returns 503 honestly until you provide one via `CXR_MODEL_PATH` (see `notebooks/CXR_Sentinel_Full.ipynb`).

The pre-existing CXR classifier research modules remain under `src/` for reference; `notebooks/CXR_Sentinel_Full.ipynb` is a complete, ready-to-run pipeline (real NIH ChestX-ray14 data, training, calibration, Grad-CAM, longitudinal comparison) that trains the checkpoint the optional endpoint above consumes.

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

Open <http://127.0.0.1:8000>. SQLite metadata is written to `meditrace.db`; source documents are stored beneath `data/documents/`. Both are ignored by Git. A throwaway auth/encryption key is generated automatically for this mode; sign in by first bootstrapping an account (see "Sign in" below).

### Postgres mode

```bash
cp .env.example .env
docker compose up -d postgres
set -a; source .env; set +a
uvicorn src.meditrace.api:app --reload
```

The compose service is intended for local development only. Change its credentials before using it outside an isolated development machine.

### MySQL mode (e.g. root@127.0.0.1:3308)

```bash
docker compose up -d mysql   # publishes MySQL on 127.0.0.1:3308
cp .env.mysql.example .env
set -a; source .env; set +a
python -m src.meditrace.init_db
uvicorn src.meditrace.api:app --reload
```

See [MySQL setup and deployment](docs/MYSQL_DEPLOYMENT.md) for the full walkthrough (including without Docker), hosted TLS connections, Vercel variables, and the separate extraction worker.

### Sign in

There is no public sign-up endpoint — creating one on an internet-facing health-data app is exactly the kind of thing this project avoids. Bootstrap the first account from the machine running against your database:

```bash
python -m src.meditrace.create_user --email you@example.com --role admin
```

Then sign in at `/` with that email/password. Roles are `admin`, `clinician` (read/write), and `reviewer` (read-only). See [`docs/SECURITY.md`](docs/SECURITY.md) for the full security model, including what `SECRET_KEY` and `ENCRYPTION_KEY` protect and why they're required outside local development.

The API includes `api/index.py` and `vercel.json` for Vercel. MySQL is supported through PyMySQL, using `DATABASE_URL`/`MYSQL_URL` or individual `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER`, and `MYSQL_PASSWORD` settings. Local `.env` files load automatically.

Use `OBJECT_STORE_BACKEND=database` to persist small source files in `stored_objects`, accessible to both the API and worker. This is the Vercel default when a non-SQLite database is configured. Local filesystem storage remains the local default; `/tmp` storage and SQLite on Vercel remain ephemeral. Existing files are not automatically migrated.

The health route probes its dependencies and returns HTTP 503 when unavailable. Database connection/schema initialization failures on Vercel are reported without hiding the web interface. The worker remains a separate process; a Vercel deployment alone does not process queued jobs.

## API contract

Interactive API documentation is available at `/docs`. Every route below except `/api/health` and `/api/auth/login` requires `Authorization: Bearer <token>`; routes marked **write** additionally require the `clinician` or `admin` role.

| Method | Route | Purpose |
|---|---|---|
| `GET` | `/api/health` | Dependency status (public, no PHI) |
| `POST` | `/api/auth/login` | Exchange email/password for a JWT (public) |
| `POST` | `/api/documents` | **write** — Register and persist an allowed source document |
| `GET` | `/api/documents` | List documents, optionally filtered by `patient_id` |
| `GET` | `/api/documents/{id}` | Retrieve document metadata and linked facts |
| `GET` | `/api/documents/{id}/content` | Retrieve original source bytes |
| `POST` | `/api/documents/{id}/facts` | **write** — Add a schema-validated, evidence-linked fact |
| `POST` | `/api/documents/{id}/extract` | **write** — Queue background extraction |
| `GET` | `/api/jobs/{id}` | Read extraction status/errors |
| `POST` | `/api/documents/{id}/submit-to-model` | **write** — Explicitly submit text to the configured model |
| `GET` | `/api/patients/{patient_id}/timeline` | Evidence-linked timeline, deterministic deltas, and reliability tier |
| `GET` | `/api/patients/{patient_id}/flags` | Deterministic trend and contradiction flags (Phase 4) |
| `POST` | `/api/patients/{patient_id}/ask` | Evidence-grounded Q&A over this patient's facts only (Phase 5) |
| `POST` | `/api/documents/{id}/cxr-analyze` | **write** — Chest X-ray inference; 503 until `CXR_MODEL_PATH` is configured (Phase 6) |
| `GET` | `/api/audit` | **admin only** — Append-only access log |

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
- **Current phase:** Phase 0 through the Phase 1–3 text workflow are solid. Phase 4 (trends/contradictions) and Phase 5 (grounded Q&A) have a real, tested first implementation but haven't been through the fixed evaluation sets `PROJECT_PLAN.md` calls for. Phase 6 (CXR) is wired end-to-end but ships no pretrained checkpoint — you train one. Scanned-image OCR still requires a Docling-enabled worker deployment; the base worker fails explicitly rather than fabricating text.
- **Security:** see [`docs/SECURITY.md`](docs/SECURITY.md) for the full picture. In short: authentication, role-based write access, encryption at rest for sensitive columns, an append-only audit log, and security headers are implemented; there is no per-patient access control, no malware scanning on uploads, and no compliance certification. This is a defensible starting posture for research data, not a production clinical deployment.
