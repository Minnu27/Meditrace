from __future__ import annotations

import argparse
from datetime import datetime, timezone
import time
from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal, create_schema
from .extraction import classify_document, deterministic_facts, extract_text
from .models import Document, ExtractionJob, Fact
from .schemas import DocumentStatus, FactCreate
from .storage import build_object_store


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
        try:
            text = extract_text(store.get(document.object_key), document.media_type)
            document.document_type = classify_document(text)
            for payload in deterministic_facts(
                text, document.patient_id, document.document_type
            ):
                validated = FactCreate.model_validate(payload)
                session.add(
                    Fact(source_document_id=document.id, **validated.model_dump())
                )
            document.status = DocumentStatus.ready
            document.extraction_error = None
            job.status = "completed"
            job.completed_at = datetime.now(timezone.utc)
            session.commit()
        except Exception as exc:
            session.rollback()
            job = session.get(ExtractionJob, job.id)
            document = session.get(Document, job.document_id)
            job.status = "failed"
            job.error = str(exc)[:1000]
            job.completed_at = datetime.now(timezone.utc)
            document.status = DocumentStatus.failed
            document.extraction_error = job.error
            session.commit()
        return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2)
    args = parser.parse_args()
    create_schema()
    while True:
        worked = process_one()
        if args.once:
            break
        if not worked:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
