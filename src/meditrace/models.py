from __future__ import annotations

from datetime import date, datetime, timezone
import uuid

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .crypto import EncryptedBinary, EncryptedJSON, EncryptedString
from .schemas import DocumentStatus, DocumentType


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[str] = mapped_column(String(128), index=True)
    filename: Mapped[str] = mapped_column(EncryptedString(1024))
    media_type: Mapped[str] = mapped_column(String(128))
    object_key: Mapped[str] = mapped_column(String(512), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus), default=DocumentStatus.uploaded
    )
    document_type: Mapped[DocumentType] = mapped_column(
        Enum(DocumentType), default=DocumentType.unknown
    )
    extraction_error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    facts: Mapped[list["Fact"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class Fact(Base):
    __tablename__ = "facts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    patient_id: Mapped[str] = mapped_column(String(128), index=True)
    fact_type: Mapped[str] = mapped_column(String(64), index=True)
    test_or_finding: Mapped[str] = mapped_column(String(255), index=True)
    normalized_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    value: Mapped[str | None] = mapped_column(EncryptedString(2048), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_range: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observed_date: Mapped[date] = mapped_column(Date, index=True)
    evidence_location: Mapped[dict] = mapped_column(EncryptedJSON(), default=dict)
    confidence: Mapped[float] = mapped_column(Float)
    details: Mapped[dict] = mapped_column(EncryptedJSON(), default=dict)
    # Provenance (see provenance.py). Nullable only so pre-existing rows can be
    # backfilled by schema_upgrade; every new fact is sealed before commit.
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extractor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    extractor_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(96), nullable=True)
    commitment_salt: Mapped[str | None] = mapped_column(
        EncryptedString(64), nullable=True
    )
    content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    document: Mapped[Document] = relationship(back_populates="facts")


class Answer(Base):
    """A persisted grounded answer, so an answer can be verified later the same
    way a fact can: content hash, cited fact hashes, audit entry, anchor."""

    __tablename__ = "answers"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    patient_id: Mapped[str] = mapped_column(String(128), index=True)
    question: Mapped[str] = mapped_column(EncryptedString(2048))
    answer_text: Mapped[str] = mapped_column(EncryptedString(8192))
    cited_fact_ids: Mapped[list] = mapped_column(JSON, default=list)
    cited_fact_hashes: Mapped[dict] = mapped_column(JSON, default=dict)
    insufficient_evidence: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    answerer: Mapped[str] = mapped_column(String(128))
    answerer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(96), nullable=True)
    commitment_salt: Mapped[str] = mapped_column(EncryptedString(64))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StoredObject(Base):
    """Small source files shared by the API and worker through the database."""

    __tablename__ = "stored_objects"
    object_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    content: Mapped[bytes] = mapped_column(EncryptedBinary())


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32))
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class AuditEvent(Base):
    """Append-only, hash-chained access log. No update/delete route exists.

    ``entry_hash`` commits to every other column plus ``prev_hash`` (the
    previous entry's ``entry_hash``), so editing, deleting, or reordering any
    entry breaks every hash after it. ``payload_digest`` carries the content
    hash of whatever was written (a fact, an answer, a document's bytes).
    """

    __tablename__ = "audit_events"
    # A named unique index (rather than an inline constraint) so schema_upgrade
    # can add it to databases created before the chain existed.
    __table_args__ = (Index("uq_audit_events_seq", "seq", unique=True),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    user_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    patient_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    success: Mapped[bool] = mapped_column(default=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


class Anchor(Base):
    """A signed Merkle root over audit entries ``(from_seq, to_seq]``.

    ``statement`` is the exact canonical JSON that was signed (and, when a TSA
    is configured, timestamped), stored verbatim so verification never depends
    on re-serialising it identically. Anchors chain through ``prev_root``.
    """

    __tablename__ = "anchors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_seq: Mapped[int] = mapped_column(Integer)
    to_seq: Mapped[int] = mapped_column(Integer, unique=True)
    leaf_count: Mapped[int] = mapped_column(Integer)
    merkle_root: Mapped[str] = mapped_column(String(64))
    prev_root: Mapped[str] = mapped_column(String(64))
    anchored_at: Mapped[str] = mapped_column(String(32))
    statement: Mapped[str] = mapped_column(Text)
    method: Mapped[str] = mapped_column(String(64))
    key_id: Mapped[str] = mapped_column(String(32))
    public_key: Mapped[str] = mapped_column(String(128))
    signature: Mapped[str] = mapped_column(String(128))
    tsa_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tsa_token: Mapped[str | None] = mapped_column(Text, nullable=True)
