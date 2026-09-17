# Security model

This documents what is actually implemented, what depends on your deployment
choices, and what is explicitly out of scope. Read this before putting real
data (even de-identified research data) behind a public URL.

## Threat model and what "secure" means here

The user of this app is a research/decision-support workflow, not a clinical
system. The chosen security posture is **strong practical security**, not
literal end-to-end encryption: the server must be able to read facts to
extract, search, timeline, and answer questions about them, and no app in
this category can do that while also guaranteeing the server never sees
plaintext. Anyone who tells you otherwise for a feature-complete app is
either not doing the features or not doing the encryption.

What "strong practical security" means concretely:

1. **Transport**: TLS end-to-end. Vercel terminates TLS for the API; set
   `MYSQL_SSL=true` (default when `VERCEL=1`) so the database connection is
   also encrypted, with certificate and hostname verification on
   (`ssl.create_default_context()`, never disabled here).
2. **Authentication**: every data route requires a bearer JWT
   (`src/meditrace/auth.py`). There is intentionally no public
   self-registration endpoint — an operator creates accounts with
   `python -m src.meditrace.create_user`, which runs against the database
   directly and is never exposed over HTTP.
3. **Authorization**: three roles — `admin`, `clinician`, `reviewer`.
   Clinicians and admins can write (upload, extract, create facts, submit to
   a model, run CXR inference); reviewers are read-only. There is no
   per-patient ACL — anyone with an account can see any patient. If you need
   that, it is the next thing to add, not something silently assumed away.
4. **Passwords**: PBKDF2-HMAC-SHA256 with a random salt and 600,000
   iterations (OWASP's current minimum), not a fast general-purpose hash.
   Five failed logins locks the account for 15 minutes. Login timing is
   equalized between "no such user" and "wrong password" so the endpoint
   doesn't leak which accounts exist.
5. **Encryption at rest, applied where it doesn't break the product**:
   `src/meditrace/crypto.py` wraps document filenames, fact values, fact
   `details`, `evidence_location` (which can contain a source quote), and
   raw stored document bytes in Fernet (AES-128-CBC + HMAC) before they hit
   the database. `patient_id`, `fact_type`, `test_or_finding`, and
   `observed_date` are **not** app-level encrypted, because the app filters,
   groups, and joins on them in SQL — encrypting those with a randomized
   cipher would either break every query or require a deterministic/blind
   index, which trades away the security property it's meant to provide.
   Close that remaining gap at the infrastructure layer: MySQL 8 supports
   table/tablespace encryption (`innodb_encrypt_tables`,
   `innodb_redo_log_encrypt`), and every managed MySQL provider (PlanetScale,
   RDS, Cloud SQL, Azure) encrypts the underlying volume by default. Turn
   that on; it costs nothing in query semantics.
6. **Audit log**: `audit_events` is append-only — no route updates or
   deletes it — and every read or write of patient data records who, what,
   and when (`src/meditrace/audit.py`). `GET /api/audit` is admin-only.
7. **Security headers**: HSTS, `X-Content-Type-Options: nosniff`,
   `X-Frame-Options: DENY`, a restrictive `Content-Security-Policy`, and
   `Referrer-Policy: no-referrer` are set both by FastAPI middleware and by
   `vercel.json` (belt and suspenders across the two ways Vercel can serve
   this app).
8. **CORS**: closed by default. Set `ALLOWED_ORIGINS` (comma-separated) only
   if a separate frontend origin needs to call this API directly.

## Secrets you must set yourself

`SECRET_KEY` (JWT signing) and `ENCRYPTION_KEY` (Fernet key) have **no
default in production**. On Vercel (`VERCEL=1`), the app raises at startup
rather than run with an implicit key. Locally, a throwaway key is generated
once and cached in `.meditrace_dev_key` (gitignored) purely for developer
convenience — it does not survive being deleted, and it is never acceptable
for a shared or public deployment.

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"                       # SECRET_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # ENCRYPTION_KEY
```

Set both as Vercel environment variables (Production and Preview) before
deploying. Rotate `SECRET_KEY` any time and every session invalidates.
Rotating `ENCRYPTION_KEY` requires re-encrypting existing rows first — there
is no built-in migration for that; treat it as a one-way key, back it up
somewhere your secrets manager controls, and never commit it.

## What this is not

- Not literal end-to-end encryption. The server (and anyone with production
  DB or Vercel function access plus a copy of `ENCRYPTION_KEY`) can read
  patient data. That is what lets extraction, timelines, trend flags, and
  Q&A work at all.
- Not HIPAA-certified, not a BAA, not a substitute for a compliance review.
  It is a defensible starting posture for synthetic/de-identified research
  data, not a clearance to load real patient records (see the top-level
  README's scope boundaries — that rule is unchanged by anything here).
- No malware/virus scanning on uploaded files, no key-rotation tooling, no
  intrusion detection. These are legitimate next steps, not silently solved.
