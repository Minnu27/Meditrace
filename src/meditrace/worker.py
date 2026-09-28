from __future__ import annotations

import argparse
from datetime import datetime, timezone
import logging
import time
import uuid

from sqlalchemy import select

from . import audit
from .config import get_settings
from .database import SessionLocal, create_schema
from .extraction import classify_document, deterministic_facts, extract_document
from .models import Document, ExtractionJob, Fact
from .provenance import (
    DETERMINISTIC_EXTRACTOR,
    DETERMINISTIC_EXTRACTOR_VERSION,
    seal_fact,
    sha256_hex,
)
from .schemas import DocumentStatus, FactCreate
from .storage import build_object_store

WORKER_IDENTITY = "extraction-worker"
log = logging.getLogger(__name__)


def process_one() -> bool:
    settings = get_settings()
    store = build_object_store(settings)
    with SessionLocal() as session:
        job = session.scalar(
            select(ExtractionJob)
            .where(ExtractionJob.status == "queued")
            .order_by(ExtractionJob.created_at)
            .with_for_update(skip_locked=True)
        )
        if not job:
            return False
        job.status = "processing"
        job.attempts += 1
        document = session.get(Document, job.document_id)
        document.status = DocumentStatus.processing
        session.commit()
        created: list[Fact] = []
        try:
            content = store.get(document.object_key)
            # Refuse to extract from bytes that no longer match the upload.
            if sha256_hex(content) != document.sha256:
                raise RuntimeError("Stored source bytes do not match the uploaded SHA-256")
            extracted = extract_document(content, document.media_type)
            document.document_type = classify_document(extracted.text)
            extractor_version = DETERMINISTIC_EXTRACTOR_VERSION
            if extracted.engine:
                extractor_version += f"; {extracted.method} via {extracted.engine}"
            for payload in deterministic_facts(
                extracted.text,
                document.patient_id,
                document.document_type,
                fallback_date=document.created_at.date(),
                line_confidence=extracted.line_confidence,
                ocr_engine=extracted.engine if extracted.is_ocr else None,
            ):
                validated = FactCreate.model_validate(payload)
                fact = Fact(id=uuid.uuid4(), source_document_id=document.id, **validated.model_dump())
                seal_fact(
                    fact,
                    source_sha256=document.sha256,
                    extractor=DETERMINISTIC_EXTRACTOR,
                    extractor_version=extractor_version,
                )
                session.add(fact)
                created.append(fact)
            document.status = DocumentStatus.ready
            document.extraction_error = None
            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            session.commit()
        except Exception as exc:
            session.rollback()
            created = []
            job = session.get(ExtractionJob, job.id)
            document = session.get(Document, job.document_id)
            job.status = "failed"
            job.error = str(exc)[:1000]
            job.completed_at = datetime.now(timezone.utc)
            document.status = DocumentStatus.failed
            document.extraction_error = job.error
            session.commit()
        for fact in created:
            audit.record(
                session,
                user_email=WORKER_IDENTITY,
                role="system",
                action="extract_fact",
                resource_type="fact",
                resource_id=str(fact.id),
                patient_id=fact.patient_id,
                payload_digest=fact.content_hash,
            )
        audit.record(
            session,
            user_email=WORKER_IDENTITY,
            role="system",
            action="extraction_completed" if job.status == "completed" else "extraction_failed",
            resource_type="document",
            resource_id=str(document.id),
            patient_id=document.patient_id,
            success=job.status == "completed",
            detail=f"{len(created)} facts; {document.document_type.value}",
            payload_digest=document.sha256,
        )
        return True


def maybe_anchor(last_anchor_at: float) -> float:
    settings = get_settings()
    if settings.anchor_interval_seconds <= 0:
        return last_anchor_at
    if time.monotonic() - last_anchor_at < settings.anchor_interval_seconds:
        return last_anchor_at
    from .anchoring import create_anchor

    try:
        with SessionLocal() as session:
            create_anchor(session, tsa_url=settings.anchor_tsa_url)
    except Exception as exc:
        log.error("Anchoring failed: %s", exc)
    return time.monotonic()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args()
    create_schema()
    last_anchor_at = 0.0
    while True:
        worked = process_one()
        last_anchor_at = maybe_anchor(last_anchor_at)
        if args.once:
            break
        if not worked:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
