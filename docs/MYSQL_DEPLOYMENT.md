# Deploy Meditrace with MySQL

This change supports MySQL 8 and adds `stored_objects` for small source files. Existing documents/facts/jobs tables remain intact. No credentials are included. The target database connection and Vercel build must still be verified in your accounts.

## Local setup: root@127.0.0.1:3308 with Docker

The fastest way to get a real MySQL instance running locally at
`127.0.0.1:3308` (matching `.env.mysql.example`) is the bundled Compose
service. This is a **local-only, throwaway container** — `root` is fine here
because nothing outside your machine can reach port 3308, but never point a
production or shared deployment at a root database account (see step 5).

1. `docker compose up -d mysql` — starts MySQL 8.4, publishes it on
   `127.0.0.1:3308`, and creates the `Meditrace` database. Override the root
   password with `MYSQL_ROOT_PASSWORD=your-password docker compose up -d mysql`
   if you don't want the compose-file default.
2. Install requirements: `python -m pip install -r requirements.txt`.
3. Copy `.env.mysql.example` to `.env`. Its defaults (`MYSQL_HOST=127.0.0.1`,
   `MYSQL_PORT=3308`, `MYSQL_USER=root`) already match step 1 — set
   `MYSQL_PASSWORD` to whatever you used there.
4. Generate `SECRET_KEY` and `ENCRYPTION_KEY` (commands are in
   `.env.mysql.example` and in `docs/SECURITY.md`) and paste them into `.env`.
   Locally you can also leave them blank — a throwaway key is auto-generated
   and cached in `.meditrace_dev_key` — but a real key survives container
   restarts and lets you switch to a cloud database later without losing
   access to already-encrypted rows.
5. **Before this goes anywhere another person or the internet can reach**,
   create a scoped application user instead of using root:
   ```sql
   CREATE USER 'meditrace_app'@'%' IDENTIFIED BY 'a-real-password';
   GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX ON Meditrace.* TO 'meditrace_app'@'%';
   ```
   and update `MYSQL_USER`/`MYSQL_PASSWORD` accordingly. This is the one step
   in this workflow that must change before "next, in production, we'll
   switch to a cloud DB" actually happens.
6. Run `python -m src.meditrace.init_db`. This creates missing tables; it does not migrate or replace existing table definitions.
7. Bootstrap the first account (there is no public sign-up — see
   `docs/SECURITY.md`): `python -m src.meditrace.create_user --email you@example.com --role admin`.
8. Run `python -m uvicorn src.meditrace.api:app --reload`.
9. In another terminal in the same directory run `python -m src.meditrace.worker`.
10. Open http://127.0.0.1:8000, sign in with the account from step 7, and test a synthetic text document.

Local `.env` values load automatically without overriding terminal variables. Vercel uses dashboard environment variables.

### Local setup without Docker (MySQL Workbench / native install)

If you already run MySQL natively (e.g. via MySQL Workbench on Windows) instead of Docker:

1. Create the database:

   ```sql
   CREATE DATABASE IF NOT EXISTS Meditrace CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
   ```

2. Use an application database account with access to this database (see step 5 above); do not use a public root account for anything beyond your own single-user machine.
3. Follow steps 2–10 above, using the host/port your local server actually listens on (Workbench shows this — commonly 3306, or 3308 if something else already used 3306).

## Vercel configuration

Deploy the updated branch or merge the PR to the production branch. Use the repository root, existing vercel.json, preset Other, and no custom build/output/install overrides. Do not set a Uvicorn start command on Vercel.

This repository is set up to deploy to
[vercel.com/manishchowdarygorantla/meditrace](https://vercel.com/manishchowdarygorantla/meditrace).
Connect that Vercel project to this GitHub repository (Project Settings →
Git) if it isn't already, or run `vercel link` from a machine authenticated
to that account, then `vercel --prod` (or push to the branch Vercel is
watching) to deploy. Deploying from here requires a Vercel account/token
this session does not have — the project and this branch are ready to go the
moment you connect them.

Set the following variables for the deployment environment (Preview for a PR; Production for main):

| Variable | Value |
| --- | --- |
| MYSQL_HOST | Public hostname of your hosted MySQL server (not 127.0.0.1 — see below) |
| MYSQL_PORT | Provider's connection port |
| MYSQL_DATABASE | Meditrace, or your actual database name |
| MYSQL_USER | Application database username (not root — create one per docs/SECURITY.md) |
| MYSQL_PASSWORD | Actual password, entered only in Vercel |
| MYSQL_SSL | true |
| OBJECT_STORE_BACKEND | database |
| MAX_UPLOAD_BYTES | 4000000 |
| SECRET_KEY | Required. `python -c "import secrets; print(secrets.token_urlsafe(32))"` — startup fails without it |
| ENCRYPTION_KEY | Required. `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` — startup fails without it |
| ALLOWED_ORIGINS | Leave unset unless a separate frontend origin calls this API cross-origin |
| MODEL_ENDPOINT / MODEL_API_KEY / MODEL_NAME | Optional model gateway, unset by default |
| CXR_MODEL_PATH | Optional; only set once you've trained and shipped a checkpoint to the ml worker |

Remove stale connection-string variables if using these fields. A connection string may instead be used as `mysql+pymysql://USER:URL_ENCODED_PASSWORD@HOST:PORT/DATABASE?charset=utf8mb4`.

**Your local `root@127.0.0.1:3308` database is not reachable from Vercel.**
`127.0.0.1` on Vercel's servers means Vercel's own container, not your
machine — this is true of any serverless/cloud platform, not a bug here.
Point `MYSQL_HOST` at a database your Vercel deployment can reach over the
public internet or a private network peering: a managed provider (e.g.
PlanetScale, AWS RDS, Google Cloud SQL, Azure Database for MySQL, a MySQL
instance on a VPS with a public IP and firewall rules) all work with the
`MYSQL_*` variables above unchanged. This is exactly the "next, in
production we'll switch to a cloud DB" step — nothing else in the app needs
to change, only these environment variables.

After the schema exists in that database (`python -m src.meditrace.init_db`
run from a machine with network access to it), bootstrap the first account
with `python -m src.meditrace.create_user --email you@example.com --role admin`
run the same way — there is no HTTP registration endpoint (see
`docs/SECURITY.md`).

Use the provider's required network access rules and TLS settings. The client validates certificates and hostnames. MYSQL_SSL_CA can point to a deployed provider CA certificate file when system trust is insufficient; never disable verification to work around certificate errors. `localhost` and `127.0.0.1` refer to Vercel's runtime, not your PC. A Windows-only local MySQL installation is not a hosted Vercel database.

The database itself must already exist. Run `python -m src.meditrace.init_db` from an environment with access to the hosted database to initialize tables. Startup also creates missing tables for convenience. If a database connection/schema creation fails at Vercel startup, the interface remains available and health reports degraded dependencies. Invalid environment values or import failures can still prevent startup; consult runtime logs.

Redeploy after changing variables. Verify `/`, `/docs`, `/api/health`, then upload, list and retrieve a small synthetic text file. Health checks database connectivity and storage availability, not model inference, worker liveness or all application permissions.

## Extraction worker is a separate deployment

The API queues extraction; it does not execute the worker in a Vercel function. Deploy a persistent Python worker using this same repository with:

- Install: `pip install -r requirements.txt`
- Start: `python -m src.meditrace.worker`
- Same MYSQL_* credentials, TLS configuration and `OBJECT_STORE_BACKEND=database` as the API.

Run a single worker for this prototype. Without it, jobs remain queued. Images/scanned PDFs still require OCR support; no OCR/model training is added by this change. MODEL_ENDPOINT, MODEL_API_KEY and MODEL_NAME are optional and configure an existing compatible model gateway.

## Storage and scope

Database storage persists source bytes in the new stored_objects table, shared by API and worker. This is intended for small demo documents. Large datasets need a dedicated object store and direct uploads; database limits such as max_allowed_packet must accommodate files and protocol overhead. The Vercel default upload cap is 4,000,000 bytes.

Existing local files are not migrated. Before switching storage backend, preserve the old files and re-upload synthetic documents, or implement a controlled migration. Existing metadata pointing to files absent from stored_objects will not magically recover those files.

Every data route now requires a bearer JWT and role-based write access (see `docs/SECURITY.md`), and sensitive columns are encrypted at rest — but there is still no per-patient access control (any account can see any patient), no malware scanning on uploads, and no compliance certification. Keep the deployment access-restricted and use synthetic/de-identified research data. This is not a production clinical deployment.

## Validation

Run `python -m pytest -q` after installing pytest and httpx. Tests cover MySQL URL/password handling, TLS verification, schema compilation, durable upload/retrieval and worker extraction, dependency failures and existing API routes. The GitHub workflow also runs the integration test against a disposable MySQL 8 service. Passing CI is not proof that your hosted credentials or Vercel configuration work.
