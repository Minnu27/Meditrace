"""Append-only, hash-chained audit log.

Each entry commits to its own content and to the previous entry's hash::

    entry_hash = SHA-256(canonical_json({seq, occurred_at, user_email, role,
                                         action, resource_type, resource_id,
                                         patient_id, success, detail,
                                         payload_digest, prev_hash}))

Appends are serialised by a UNIQUE constraint on ``seq``: two writers that
read the same head both try to insert ``head + 1``; one wins, the other rolls
back its own insert and retries against the new head. Callers commit their
own work before calling :func:`record`, so a retry never discards it.

No route updates or deletes this table. ``verify_chain`` recomputes the whole
chain, and periodic Merkle anchors (anchoring.py) make even a fully
recomputed, internally-consistent rewrite detectable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import AuditEvent
from .provenance import canonical_json, canonical_timestamp, sha256_hex, utc_now_seconds

GENESIS_HASH = "0" * 64
CHAIN_VERSION = "mt-audit-v1"
_MAX_APPEND_ATTEMPTS = 8


def entry_payload(event: AuditEvent) -> dict:
    return {
        "v": CHAIN_VERSION,
        "seq": event.seq,
        "occurred_at": canonical_timestamp(event.occurred_at),
        "user_email": event.user_email,
        "role": event.role,
        "action": event.action,
        "resource_type": event.resource_type,
        "resource_id": event.resource_id,
        "patient_id": event.patient_id,
        "success": bool(event.success),
        "detail": event.detail,
        "payload_digest": event.payload_digest,
        "prev_hash": event.prev_hash,
    }


def compute_entry_hash(event: AuditEvent) -> str:
    return sha256_hex(canonical_json(entry_payload(event)))


def chain_head(session: Session) -> AuditEvent | None:
    return session.scalar(
        select(AuditEvent)
        .where(AuditEvent.seq.is_not(None))
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )


def record(
    session: Session,
    *,
    user_email: str | None,
    role: str | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    patient_id: str | None = None,
    success: bool = True,
    detail: str | None = None,
    payload_digest: str | None = None,
) -> AuditEvent:
    last_error: Exception | None = None
    for _ in range(_MAX_APPEND_ATTEMPTS):
        head = chain_head(session)
        event = AuditEvent(
            occurred_at=utc_now_seconds(),
            user_email=user_email,
            role=role,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            patient_id=patient_id,
            success=success,
            detail=detail[:500] if detail else None,
            payload_digest=payload_digest,
            seq=(head.seq + 1) if head else 1,
            prev_hash=head.entry_hash if head else GENESIS_HASH,
        )
        event.entry_hash = compute_entry_hash(event)
        session.add(event)
        try:
            session.commit()
            return event
        except IntegrityError as exc:  # another writer took this seq
            session.rollback()
            last_error = exc
    raise RuntimeError("Could not append to the audit chain") from last_error


@dataclass
class ChainReport:
    ok: bool
    length: int
    head_seq: int | None
    head_hash: str | None
    problems: list[dict] = field(default_factory=list)


def verify_chain(
    session: Session, *, from_seq: int = 1, to_seq: int | None = None
) -> ChainReport:
    """Recompute every entry hash and every link in ``[from_seq, to_seq]``."""
    query = select(AuditEvent).where(AuditEvent.seq >= from_seq).order_by(AuditEvent.seq)
    if to_seq is not None:
        query = query.where(AuditEvent.seq <= to_seq)
    problems: list[dict] = []
    expected_prev: str | None = None
    if from_seq > 1:
        prior = session.scalar(select(AuditEvent).where(AuditEvent.seq == from_seq - 1))
        expected_prev = prior.entry_hash if prior else None
    else:
        expected_prev = GENESIS_HASH
    expected_seq = from_seq
    last: AuditEvent | None = None
    count = 0
    for event in session.scalars(query):
        count += 1
        if event.seq != expected_seq:
            problems.append(
                {"seq": expected_seq, "problem": f"missing entry (next present is {event.seq})"}
            )
            expected_seq = event.seq
        if expected_prev is not None and event.prev_hash != expected_prev:
            problems.append({"seq": event.seq, "problem": "prev_hash does not link to the previous entry"})
        if compute_entry_hash(event) != event.entry_hash:
            problems.append({"seq": event.seq, "problem": "entry content does not match entry_hash"})
        expected_prev = event.entry_hash
        expected_seq += 1
        last = event
    unchained = session.scalar(select(AuditEvent).where(AuditEvent.seq.is_(None)).limit(1))
    if unchained is not None:
        problems.append({"seq": None, "problem": "audit rows exist outside the chain"})
    return ChainReport(
        ok=not problems,
        length=count,
        head_seq=last.seq if last else None,
        head_hash=last.entry_hash if last else None,
        problems=problems,
    )
