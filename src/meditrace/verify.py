"""End-to-end verification of a fact or an answer.

For a fact, every link in the evidence chain is recomputed from stored data,
never read back from a cached "verified" flag:

    source bytes --sha256--> document hash --(sealed into)--> fact commitment
    fact commitment --(payload_digest of)--> audit entry --(hash chain)--> prev entry
    audit entry --(Merkle inclusion proof)--> anchored root --(Ed25519 / RFC 3161)--> statement

Each check reports pass / fail / pending / skipped with a plain-English note.
A proof bundle carries exactly what ``scripts/verify_proof.py`` needs to redo
the hash, chain, Merkle, and signature checks offline without this server.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, merkle
from .anchoring import anchor_checks, anchor_containing, anchor_public
from .extraction import extract_document
from .models import Answer, AuditEvent, Document, Fact
from .provenance import (
    COMMITMENT_VERSION,
    answer_commitment_payload,
    compute_answer_hash,
    compute_fact_hash,
    fact_commitment_payload,
    sha256_hex,
)

FACT_WRITE_ACTIONS = ("create_fact", "extract_fact", "submit_to_model", "cxr_analyze", "provenance_backfill")


@dataclass
class Check:
    id: str
    label: str
    status: str  # pass | fail | pending | skipped
    detail: str


def _verdict(checks: list[Check]) -> str:
    if any(c.status == "fail" for c in checks):
        return "failed"
    if any(c.status == "pending" for c in checks):
        return "verified_pending_anchor"
    return "verified"


def _write_entry(session: Session, resource_type: str, resource_id: str) -> AuditEvent | None:
    return session.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.resource_type == resource_type,
            AuditEvent.resource_id == resource_id,
            AuditEvent.payload_digest.is_not(None),
            AuditEvent.seq.is_not(None),
        )
        .order_by(AuditEvent.seq)
        .limit(1)
    )


def _entry_public(event: AuditEvent) -> dict:
    return {
        "seq": event.seq,
        "action": event.action,
        "occurred_at": audit.entry_payload(event)["occurred_at"],
        "actor": event.user_email,
        "role": event.role,
        "payload_digest": event.payload_digest,
        "prev_hash": event.prev_hash,
        "entry_hash": event.entry_hash,
    }


def _chain_and_anchor_checks(
    session: Session, event: AuditEvent | None, expected_digest: str | None, what: str
) -> tuple[list[Check], dict | None, dict | None]:
    checks: list[Check] = []
    if event is None:
        checks.append(Check("audit_entry", f"{what} write is in the audit log", "fail",
                            f"No audit entry records this {what}'s content hash."))
        return checks, None, None
    checks.append(Check(
        "audit_digest", f"Audit log recorded this exact {what}",
        "pass" if event.payload_digest == expected_digest else "fail",
        f"Entry #{event.seq} ({event.action}) committed digest {event.payload_digest[:16]}…"
        + ("" if event.payload_digest == expected_digest else f" but the {what} now hashes to {(expected_digest or '')[:16]}…"),
    ))
    entry_ok = audit.compute_entry_hash(event) == event.entry_hash
    checks.append(Check("audit_entry_hash", "Audit entry is unaltered", "pass" if entry_ok else "fail",
                        f"Entry #{event.seq} recomputes to its stored hash." if entry_ok
                        else f"Entry #{event.seq}'s fields no longer match its hash."))
    previous = session.scalar(select(AuditEvent).where(AuditEvent.seq == event.seq - 1)) if event.seq > 1 else None
    expected_prev = previous.entry_hash if previous else audit.GENESIS_HASH
    link_ok = event.prev_hash == expected_prev and (event.seq == 1 or previous is not None)
    checks.append(Check("audit_chain_link", "Entry links to the previous entry", "pass" if link_ok else "fail",
                        f"prev_hash matches entry #{event.seq - 1}." if link_ok and event.seq > 1
                        else "First entry, links to genesis." if link_ok
                        else "The chain is broken immediately before this entry."))

    anchor = anchor_containing(session, event.seq)
    if anchor is None:
        checks.append(Check("anchor", "Covered by a signed anchor", "pending",
                            "Not anchored yet — it will be included in the next periodic anchor."))
        return checks, None, None
    results = anchor_checks(session, anchor)
    leaves = [e.entry_hash for e in results["entries"]]
    index = event.seq - anchor.from_seq - 1
    proof = merkle.inclusion_proof(leaves, index) if 0 <= index < len(leaves) else []
    included = merkle.verify_inclusion(event.entry_hash, proof, anchor.merkle_root)
    checks += [
        Check("merkle_inclusion", "Entry is inside the anchored Merkle root", "pass" if included else "fail",
              f"{len(proof)}-step proof reaches root {anchor.merkle_root[:16]}… of anchor #{anchor.id}."
              if included else "The inclusion proof does not reach the anchored root."),
        Check("anchor_log_consistent", "Log still matches the anchored root",
              "pass" if results["current_log_matches_root"] else "fail",
              f"All {anchor.leaf_count} entries in #{anchor.from_seq + 1}–#{anchor.to_seq} rehash to the root."
              if results["current_log_matches_root"]
              else "Entries covered by this anchor were changed after it was signed."),
        Check("anchor_signature", "Anchor statement signature is valid",
              "pass" if results["signature_valid"] and results["statement_matches_record"] else "fail",
              f"Ed25519 signature by key {anchor.key_id}"
              + (" (this server's published key)." if results["signed_by_this_server"] else " (NOT this server's current key).")),
        Check("anchor_chain", "Anchor links to the previous anchor",
              "pass" if results["links_to_previous_anchor"] else "fail",
              f"prev_root {anchor.prev_root[:16]}…"),
    ]
    if anchor.tsa_token:
        checks.append(Check("anchor_timestamp", "Independent RFC 3161 timestamp", "pass",
                            f"Token from {anchor.tsa_url} bound to the statement at anchoring time; "
                            "verify its signature with `openssl ts -verify` (see proof bundle)."))
    proof_obj = {"leaf_index": index, "leaf_entry_hash": event.entry_hash, "path": proof, "root": anchor.merkle_root}
    return checks, anchor_public(anchor), proof_obj


# ------------------------------------------------------------------ facts


def verify_fact(session: Session, store, fact_id: uuid.UUID) -> dict | None:
    fact = session.get(Fact, fact_id)
    if fact is None:
        return None
    document = session.get(Document, fact.source_document_id)
    checks: list[Check] = []

    # 1. Source bytes still match the fingerprint taken at upload.
    content = None
    try:
        content = store.get(document.object_key)
        actual = sha256_hex(content)
        ok = actual == document.sha256
        checks.append(Check("source_hash", "Source document is unchanged since upload", "pass" if ok else "fail",
                            f"Stored bytes hash to {actual[:16]}…" + ("" if ok else f", expected {document.sha256[:16]}…")))
    except Exception:
        checks.append(Check("source_hash", "Source document is unchanged since upload", "fail",
                            "Stored source bytes are missing or unreadable."))
    sealed_ok = fact.source_sha256 == document.sha256
    checks.append(Check("source_binding", "Fact is bound to that source hash", "pass" if sealed_ok else "fail",
                        "The fact commitment includes the document's SHA-256." if sealed_ok
                        else "The fact was sealed against a different source hash."))

    # 2. Evidence quote is really in the source (text sources only).
    quote = (fact.evidence_location or {}).get("quote")
    if not quote:
        checks.append(Check("quote_in_source", "Evidence quote appears in the source", "skipped",
                            "This fact has no text quote (e.g. an imaging finding)."))
    elif content is not None and document.media_type in {"text/plain", "text/csv", "application/pdf"}:
        try:
            extracted = extract_document(content, document.media_type)
            if extracted.is_ocr:
                raise LookupError
            normalize = lambda s: " ".join(s.split())  # noqa: E731
            found = normalize(quote) in normalize(extracted.text)
            checks.append(Check("quote_in_source", "Evidence quote appears in the source", "pass" if found else "fail",
                                f"Found on page {fact.evidence_location.get('page')}." if found
                                else "The quote is not present in the source text."))
        except LookupError:
            checks.append(Check("quote_in_source", "Evidence quote appears in the source", "skipped",
                                "Scanned PDF: the quote came from OCR; re-OCR is not re-run on every verify."))
        except Exception:
            checks.append(Check("quote_in_source", "Evidence quote appears in the source", "skipped",
                                "Source text could not be re-extracted here."))
    else:
        checks.append(Check("quote_in_source", "Evidence quote appears in the source", "skipped",
                            "Image source: the quote came from OCR; compare it against the image."))

    # 3. Fact content still matches its commitment.
    if fact.content_hash is None:
        checks.append(Check("fact_hash", "Fact content matches its sealed hash", "fail", "Fact was never sealed."))
        recomputed = None
    else:
        recomputed = compute_fact_hash(fact)
        ok = recomputed == fact.content_hash
        checks.append(Check("fact_hash", "Fact content matches its sealed hash", "pass" if ok else "fail",
                            f"Recomputed {recomputed[:16]}…" + ("" if ok else f" ≠ sealed {fact.content_hash[:16]}…")))

    # 4–5. Audit chain and anchor. Compare against the *recomputed* hash, so
    # a tampered row with a re-stamped content_hash still fails here.
    event = _write_entry(session, "fact", str(fact.id))
    chain_checks, anchor, proof = _chain_and_anchor_checks(session, event, recomputed, "fact")
    checks += chain_checks
    if fact.extractor == "legacy-unrecorded":
        checks.append(Check("legacy", "Provenance recorded at write time", "pending",
                            "Legacy fact sealed by backfill: integrity is attested only from the backfill onward."))

    return {
        "kind": "fact",
        "id": str(fact.id),
        "verdict": _verdict(checks),
        "fact": {
            "patient_id": fact.patient_id,
            "fact_type": fact.fact_type,
            "test_or_finding": fact.test_or_finding,
            "normalized_code": fact.normalized_code,
            "value": fact.value,
            "unit": fact.unit,
            "reference_range": fact.reference_range,
            "status": fact.status,
            "observed_date": fact.observed_date.isoformat(),
            "confidence": fact.confidence,
        },
        "evidence": {"quote": quote, "location": fact.evidence_location, "details": fact.details or {}},
        "provenance": {
            "source_document_id": str(document.id),
            "source_filename": document.filename,
            "source_media_type": document.media_type,
            "source_sha256": document.sha256,
            "extractor": fact.extractor,
            "extractor_version": fact.extractor_version,
            "prompt_version": fact.prompt_version,
            "content_hash": fact.content_hash,
            "commitment_version": COMMITMENT_VERSION,
        },
        "audit_entry": _entry_public(event) if event else None,
        "anchor": anchor,
        "merkle_proof": proof,
        "checks": [asdict(c) for c in checks],
    }


# ---------------------------------------------------------------- answers


def verify_answer(session: Session, store, answer_id: uuid.UUID) -> dict | None:
    answer = session.get(Answer, answer_id)
    if answer is None:
        return None
    checks: list[Check] = []
    recomputed = compute_answer_hash(answer)
    ok = recomputed == answer.content_hash
    checks.append(Check("answer_hash", "Answer text and citations match the sealed hash", "pass" if ok else "fail",
                        f"Recomputed {recomputed[:16]}…" + ("" if ok else f" ≠ sealed {answer.content_hash[:16]}…")))
    cited = []
    for fact_id in answer.cited_fact_ids or []:
        report = verify_fact(session, store, uuid.UUID(fact_id))
        recorded_hash = (answer.cited_fact_hashes or {}).get(fact_id)
        current = report["provenance"]["content_hash"] if report else None
        unchanged = report is not None and report["verdict"] != "failed" and current == recorded_hash
        checks.append(Check(f"cited_{fact_id[:8]}", f"Cited fact {fact_id[:8]} is intact and unchanged since answering",
                            "pass" if unchanged else "fail",
                            (f"{report['fact']['test_or_finding']} {report['fact']['value'] or ''} — {report['verdict'].replace('_', ' ')}."
                             if report else "Cited fact no longer exists.")
                            + ("" if report is None or current == recorded_hash else " Its hash differs from the one cited at answer time.")))
        cited.append({"fact_id": fact_id, "cited_hash": recorded_hash,
                      "verdict": report["verdict"] if report else "missing",
                      "summary": report["fact"] if report else None,
                      "quote": report["evidence"]["quote"] if report else None})
    event = _write_entry(session, "answer", str(answer.id))
    chain_checks, anchor, proof = _chain_and_anchor_checks(session, event, recomputed, "answer")
    checks += chain_checks
    return {
        "kind": "answer",
        "id": str(answer.id),
        "verdict": _verdict(checks),
        "answer": {
            "patient_id": answer.patient_id,
            "question": answer.question,
            "answer": answer.answer_text,
            "insufficient_evidence": answer.insufficient_evidence,
            "created_at": answer.created_at.isoformat(),
        },
        "provenance": {
            "answerer": answer.answerer,
            "answerer_version": answer.answerer_version,
            "prompt_version": answer.prompt_version,
            "content_hash": answer.content_hash,
            "commitment_version": COMMITMENT_VERSION,
        },
        "cited_facts": cited,
        "audit_entry": _entry_public(event) if event else None,
        "anchor": anchor,
        "merkle_proof": proof,
        "checks": [asdict(c) for c in checks],
    }


# ---------------------------------------------------------------- bundles


def proof_bundle(session: Session, kind: str, record_id: uuid.UUID) -> dict | None:
    """Everything needed to re-verify offline with scripts/verify_proof.py.

    It contains the commitment payload (including the salt and values), so it
    is as sensitive as the record itself — share it only with an auditor.
    """
    model = Fact if kind == "fact" else Answer
    record = session.get(model, record_id)
    if record is None:
        return None
    payload = fact_commitment_payload(record) if kind == "fact" else answer_commitment_payload(record)
    event = _write_entry(session, kind, str(record.id))
    bundle = {
        "bundle_version": "mt-proof-v1",
        "kind": kind,
        "id": str(record.id),
        "commitment": payload,
        "audit_entry": audit.entry_payload(event) if event else None,
        "entry_hash": event.entry_hash if event else None,
        "anchor": None,
        "merkle_proof": None,
    }
    if event is not None:
        anchor = anchor_containing(session, event.seq)
        if anchor is not None:
            leaves = list(
                session.scalars(
                    select(AuditEvent.entry_hash)
                    .where(AuditEvent.seq > anchor.from_seq, AuditEvent.seq <= anchor.to_seq)
                    .order_by(AuditEvent.seq)
                )
            )
            index = event.seq - anchor.from_seq - 1
            bundle["anchor"] = anchor_public(anchor)
            bundle["merkle_proof"] = {
                "leaf_index": index,
                "path": merkle.inclusion_proof(leaves, index),
                "root": anchor.merkle_root,
            }
    # Round-trip through JSON so the bundle is exactly what gets downloaded.
    return json.loads(json.dumps(bundle, default=str))
