"""Idempotent, additive schema upgrade for databases created before the
verifiable-record release (the project has no Alembic yet — PROJECT_PLAN
Phase 0). Runs from ``create_schema()``; safe to run repeatedly and from two
processes at once.

1. Adds any missing nullable columns and indexes to ``facts`` and
   ``audit_events`` (new tables come from ``create_all``).
2. Chains legacy audit rows (``seq IS NULL``) in their original order.
3. Seals legacy facts with provenance (``extractor = legacy-unrecorded``) and
   appends a ``provenance_backfill`` audit entry for each, so they become
   anchorable from this point on. That is honestly labelled: it proves the
   fact has not changed *since the backfill*, not since it was first written.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

from . import audit
from .models import AuditEvent, Document, Fact
from .provenance import LEGACY_EXTRACTOR, seal_fact

log = logging.getLogger(__name__)
_UPGRADED_TABLES = (Fact.__table__, AuditEvent.__table__)


def _add_missing_columns(engine) -> None:
    inspector = inspect(engine)
    for table in _UPGRADED_TABLES:
        existing = {column["name"] for column in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in existing:
                continue
            ddl_type = column.type.compile(dialect=engine.dialect)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl_type}")
                    )
            except (OperationalError, ProgrammingError):
                log.info("Column %s.%s already added by another process", table.name, column.name)
        for index in table.indexes:
            try:
                index.create(engine, checkfirst=True)
            except (OperationalError, ProgrammingError):
                log.info("Index %s already created by another process", index.name)


def _chain_legacy_audit_rows(session) -> int:
    legacy = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.seq.is_(None))
            .order_by(AuditEvent.occurred_at, AuditEvent.id)
        )
    )
    if not legacy:
        return 0
    head = audit.chain_head(session)
    seq = head.seq if head else 0
    prev = head.entry_hash if head else audit.GENESIS_HASH
    for event in legacy:
        seq += 1
        event.seq, event.prev_hash = seq, prev
        event.entry_hash = prev = audit.compute_entry_hash(event)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # another process chained them first
        return 0
    return len(legacy)


def _seal_legacy_facts(session) -> int:
    rows = session.execute(
        select(Fact, Document.sha256)
        .join(Document, Fact.source_document_id == Document.id)
        .where(Fact.content_hash.is_(None))
    ).all()
    for fact, document_sha in rows:
        seal_fact(
            fact,
            source_sha256=document_sha,
            extractor=(fact.details or {}).get("model_version") or LEGACY_EXTRACTOR,
            extractor_version=None,
        )
        session.commit()
        audit.record(
            session,
            user_email="system",
            role="system",
            action="provenance_backfill",
            resource_type="fact",
            resource_id=str(fact.id),
            patient_id=fact.patient_id,
            payload_digest=fact.content_hash,
            detail="legacy fact sealed; provenance attests only from this point",
        )
    return len(rows)


def upgrade(engine, session_factory) -> dict:
    _add_missing_columns(engine)
    with session_factory() as session:
        chained = _chain_legacy_audit_rows(session)
        sealed = _seal_legacy_facts(session)
    if chained or sealed:
        log.info("Schema upgrade chained %s audit rows and sealed %s facts", chained, sealed)
    return {"audit_rows_chained": chained, "facts_sealed": sealed}
