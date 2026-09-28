# Verifiable record: provenance, hash chain, and anchors

MediTrace is an evidence-grounded longitudinal record in which every fact
and every answer is **tamper-evident** and **independently verifiable**.

```
upload ─► OCR / NLP extraction ─► sealed facts ─► timeline + trend / contradiction / gap flags
  │              │                     │                        │
  │ sha256       │ extractor,          │ content hash           ├─► grounded Q&A ─► sealed answer
  │              │ model & prompt      │ (salted)               │   (cites fact IDs + their hashes)
  ▼              ▼ version             ▼                        ▼
  └──────────── every read, write, and answer ─► hash-chained audit log ─► periodic Merkle-root anchor
                                                                              (Ed25519, + optional RFC 3161)
```

Synthetic or de-identified research data only. Not for clinical use.

## The chain of evidence

| Link | What is stored | Where |
|---|---|---|
| Source | SHA-256 of the uploaded bytes, taken at upload; the worker refuses to extract from bytes that no longer match | `documents.sha256` |
| Evidence | page, line, and verbatim quote (plus OCR engine and per-line OCR confidence for scans) | `facts.evidence_location`, `facts.details` |
| Provenance | source hash, extractor, extractor version (incl. OCR engine), model name and prompt version (label + digest of the exact prompt text) | `facts.source_sha256`, `extractor`, `extractor_version`, `prompt_version` |
| Commitment | SHA-256 over the fact's canonical content + a random salt | `facts.content_hash`, `facts.commitment_salt` (encrypted) |
| Answers | question, answer, cited fact IDs **and the hashes those facts had when cited**, answerer and prompt version, salted commitment | `answers` |
| Audit | every read, write, and answer, each committing to the previous entry's hash and to the written record's commitment (`payload_digest`) | `audit_events.seq/prev_hash/entry_hash/payload_digest` |
| Anchor | signed Merkle root over all audit entries since the previous anchor; anchors chain to each other | `anchors` |

Canonicalisation, hash formats, and the Merkle construction are defined in
`provenance.py`, `audit.py`, and `merkle.py` (RFC 6962-style leaf/node domain
separation; odd nodes promoted, never duplicated).

## What each layer catches

`tests/test_verifiable_record.py` has a test for each row.

| Tampering | Caught by |
|---|---|
| Source file edited or replaced in storage | `source_hash` (and the evidence quote no longer found for text sources); re-extraction refuses |
| Fact value edited in the database | `fact_hash`: content no longer matches its commitment |
| Fact edited **and** its hash re-stamped (attacker holds the encryption key) | `audit_digest`: the audit entry written at extraction time committed to the old hash |
| An audit entry edited or deleted | `verify_chain`: entry hash or `prev_hash` link breaks; a gap in `seq` |
| The **whole** chain rewritten consistently after the edit | the anchor: the rewritten entries no longer hash to the signed Merkle root |
| A cited fact changed after an answer was given | the answer's `cited_…` check: the fact's hash differs from the one recorded in the answer |

## What it does *not* prove — read this

- **Anchors bound the trust window.** Entries written after the latest
  anchor are only protected by the chain itself. A key-holding insider could
  rewrite them undetectably until the next anchor. Anchor often
  (`ANCHOR_INTERVAL_SECONDS`).
- **A self-signed anchor is only as independent as its key.** Ed25519 anchors
  prove the log hasn't changed since *this server's key* signed it. An
  operator holding that key could re-sign a rewritten history. Configure
  `ANCHOR_TSA_URL` so an independent RFC 3161 timestamp authority
  countersigns each anchor. The operator cannot backdate those tokens.
  Publishing each root somewhere public (a testnet transaction, a
  transparency log, even a dated repository commit) is the next step up and
  only needs the 64-character root.
- **Integrity, not truth.** Verification proves a fact is exactly what the
  extractor produced from exactly that file. It does not prove the extractor
  read the file correctly (OCR can misread; see the per-line confidence) or
  that the source document itself was accurate.
- **Legacy data.** Facts that existed before this release are sealed by
  `schema_upgrade.py` with `extractor = legacy-unrecorded`. They are
  attested only from the backfill onward, and the verify page says so.
- **The TSA token's own signature** is checked by OpenSSL, not by the app. The
  app binds the token to the anchor statement (message imprint and nonce),
  and `scripts/verify_proof.py --tsa-ca …` runs `openssl ts -verify`.

## Why only hashes are anchored

Immutability and deletion conflict. Anchors therefore contain **no** fact,
answer, patient ID, or audit field, only Merkle roots and sequence ranges.
Commitments are salted and the salt is encrypted at rest. Deleting a record
together with its salt leaves every hash that ever referenced it
unlinkable to the deleted content. The audit log keeps a question's length,
never its text. The text lives encrypted in `answers`.

## Using it

**In the app.** Click any fact ID (timeline, flags, citations) or answer ID.
The Verify panel recomputes every link and shows the proof path, from source
hash to evidence quote, fact commitment, audit entry, Merkle path, and signed
anchor. Admins see the chain status and an *Anchor now* button. Deep links
look like `/#verify/fact/<uuid>`.

**Periodic anchoring.**

```bash
python -m src.meditrace.anchoring --generate-key        # once; store as ANCHOR_SIGNING_KEY
python -m src.meditrace.anchoring --loop --interval 600 # or set ANCHOR_INTERVAL_SECONDS on the worker
ANCHOR_TSA_URL=https://freetsa.org/tsr                  # optional independent timestamps
```

The public key is served at `GET /api/anchors/public-key`. Publish it out of
band (README, website) so auditors can pin it.

**Offline verification.** *Download proof bundle* on the verify page
(`GET /api/verify/{facts|answers}/{id}/bundle`), then:

```bash
python scripts/verify_proof.py bundle.json --public-key <published key> [--tsa-ca tsa-ca.pem]
```

The script is standalone and needs only `cryptography`. It re-derives the
commitment hash, the audit entry hash, the Merkle inclusion, and the anchor
signature without contacting the server. A bundle contains the record's
committed values and salt, so share it only with an auditor.

**Demo.** `python scripts/demo_walkthrough.py --tamper` seeds a synthetic
patient: a text lab, an OCR'd scanned lab, a prescription, a later medication
list, and a conflicting outside copy. It extracts, flags, answers, anchors,
verifies, then edits a fact at the database level and shows the check that
catches it.

## Endpoints

| Method | Route | Access |
|---|---|---|
| `GET` | `/api/verify/facts/{id}` | any signed-in role (audited) |
| `GET` | `/api/verify/answers/{id}` | any signed-in role (audited) |
| `GET` | `/api/verify/{facts\|answers}/{id}/bundle` | any signed-in role (audited) |
| `GET` | `/api/audit/verify` | admin: full chain + latest anchor check |
| `GET` | `/api/anchors` | any signed-in role |
| `POST` | `/api/anchors` | admin: anchor now |
| `GET` | `/api/anchors/public-key` | public |
