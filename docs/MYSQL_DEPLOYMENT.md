# Deploy Meditrace with MySQL

This change supports MySQL 8 and adds `stored_objects` for small source files. Existing documents/facts/jobs tables remain intact. No credentials are included. The target database connection and Vercel build must still be verified in your accounts.

## Local Windows setup

1. Create the database in MySQL Workbench:

   ```sql
   CREATE DATABASE IF NOT EXISTS Meditrace CHARACTER SET utf8mb4 COLLATE utf8mb4_bin;
   ```

2. Use an application database account with access to this database. Initial schema creation needs CREATE and INDEX privileges as well as normal SELECT/INSERT/UPDATE/DELETE access. Do not use a public root account.
3. Install requirements with `python -m pip install -r requirements.txt`.
4. Copy `.env.mysql.example` to `.env` and set your actual host, port, database, username and password. Use the port shown in Workbench (for example 3306 or 3308). Individual MYSQL_* fields accept a raw password; do not URL-encode MYSQL_PASSWORD.
5. Remove an old DATABASE_URL, MYSQL_URL or POSTGRES_URL from `.env` AND your terminal environment if switching to individual MYSQL_* fields. Connection-string precedence is DATABASE_URL, MYSQL_URL, POSTGRES_URL, then MYSQL_* fields.
6. Run `python -m src.meditrace.init_db`. This creates missing tables; it does not migrate or replace existing table definitions.
7. Run `python -m uvicorn src.meditrace.api:app --reload`.
8. In another terminal in the same directory run `python -m src.meditrace.worker`.
9. Open http://127.0.0.1:8000 and test a synthetic text document.

Local `.env` values load automatically without overriding terminal variables. Vercel uses dashboard environment variables.

## Vercel configuration

Deploy the updated branch or merge the PR to the production branch. Use the repository root, existing vercel.json, preset Other, and no custom build/output/install overrides. Do not set a Uvicorn start command on Vercel.

Set the following variables for the deployment environment (Preview for a PR; Production for main):

| Variable | Value |
| --- | --- |
| MYSQL_HOST | Public hostname of your hosted MySQL server |
| MYSQL_PORT | Provider's connection port |
| MYSQL_DATABASE | Meditrace, or your actual database name |
| MYSQL_USER | Application database username |
| MYSQL_PASSWORD | Actual password, entered only in Vercel |
| MYSQL_SSL | true |
| OBJECT_STORE_BACKEND | database |
| MAX_UPLOAD_BYTES | 4000000 |

Remove stale connection-string variables if using these fields. A connection string may instead be used as `mysql+pymysql://USER:URL_ENCODED_PASSWORD@HOST:PORT/DATABASE?charset=utf8mb4`.

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

The local build still lacks user authentication and access control. Keep the deployment access-restricted and use synthetic/de-identified research data. This is not a production clinical deployment.

## Validation

Run `python -m pytest -q` after installing pytest and httpx. Tests cover MySQL URL/password handling, TLS verification, schema compilation, durable upload/retrieval and worker extraction, dependency failures and existing API routes. The GitHub workflow also runs the integration test against a disposable MySQL 8 service. Passing CI is not proof that your hosted credentials or Vercel configuration work.
