from __future__ import annotations

from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
import os
import uuid
import logging

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from . import analysis, audit, imaging, qa
from .auth import (
    CurrentUser,
    authenticate,
    create_access_token,
    get_current_user,
    require_admin,
    require_write_access,
    ACCESS_TOKEN_MINUTES,
)
from .config import get_settings
from .database import create_schema, get_session, engine
from .extraction import HttpModelProvider, extract_text
from .models import AuditEvent, Document, ExtractionJob, Fact
from .schemas import (
    AskRequest,
    AskResponse,
    AuditEventRead,
    CXRAnalysisRead,
    DocumentList,
    DocumentRead,
    DocumentStatus,
    ExtractionJobRead,
    FactCreate,
    FactRead,
    HealthRead,
    LoginRequest,
    LoginResponse,
    ModelSubmissionRead,
    PatientFlagsRead,
    TimelineEntry,
    TimelineRead,
)
from .storage import build_object_store

DISCLAIMER = "Decision-support prototype on synthetic/de-identified research data — not a diagnostic device."
ALLOWED_MEDIA_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "text/plain",
    "text/csv",
}
settings = get_settings()
store = build_object_store(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        create_schema()
    except Exception as exc:
        if not settings.serverless:
            raise
        # Do not log exception messages that may contain connection credentials.
        logging.getLogger(__name__).error(
            "Database initialization failed (%s). Check database settings and run init_db.",
            type(exc).__name__,
        )
    yield


app = FastAPI(
    title="MediTrace AI",
    version="0.1.0",
    description=f"Evidence-first clinical document workspace. {DISCLAIMER}",
    lifespan=lifespan,
)

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'"
    )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


def _security_status() -> str:
    if os.getenv("SECRET_KEY") and os.getenv("ENCRYPTION_KEY"):
        return "configured"
    if settings.serverless:
        return "not_configured"
    return "dev_default"


@app.get("/api/health", response_model=HealthRead)
def health(response: Response) -> HealthRead:
    database_status = "connected"
    storage_status = "connected"
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        database_status = "unavailable"
    try:
        store.check()
    except Exception:
        storage_status = "unavailable"
    security_status = _security_status()
    ok = database_status == storage_status == "connected" and security_status != "not_configured"
    if not ok:
        response.status_code = 503
    return HealthRead(
        status="ok" if ok else "degraded",
        database=database_status,
        object_store=storage_status,
        model="configured" if settings.model_endpoint else "not_configured",
        security=security_status,
    )


@app.post("/api/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest, session: Session = Depends(get_session)) -> LoginResponse:
    try:
        user = authenticate(session, payload.email, payload.password)
    except HTTPException as exc:
        audit.record(
            session,
            user_email=payload.email,
            role=None,
            action="login_failed",
            resource_type="user",
            success=False,
            detail=exc.detail if isinstance(exc.detail, str) else None,
        )
        raise
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="login",
        resource_type="user",
        resource_id=str(user.id),
    )
    return LoginResponse(
        access_token=create_access_token(user),
        role=user.role,
        expires_in_minutes=ACCESS_TOKEN_MINUTES,
    )


@app.post("/api/documents", response_model=DocumentRead, status_code=201)
async def upload_document(
    patient_id: str = Form(min_length=1, max_length=128),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(require_write_access),
) -> Document:
    media_type = file.content_type or "application/octet-stream"
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(415, f"Unsupported media type: {media_type}")
    content = await file.read(settings.max_upload_bytes + 1)
    if not content:
        raise HTTPException(400, "The uploaded document is empty")
    if len(content) > settings.max_upload_bytes:
        raise HTTPException(413, "Document exceeds the upload limit")

    document_id = uuid.uuid4()
    safe_name = Path(file.filename or "document").name
    object_key = f"{patient_id}/{document_id}/{safe_name}"
    store.put(object_key, content)
    document = Document(
        id=document_id,
        patient_id=patient_id,
        filename=safe_name,
        media_type=media_type,
        object_key=object_key,
        size_bytes=len(content),
        sha256=sha256(content).hexdigest(),
        status=DocumentStatus.uploaded,
    )
    try:
        session.add(document)
        session.commit()
    except Exception:
        session.rollback()
        store.delete(object_key)
        raise
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="upload_document",
        resource_type="document",
        resource_id=str(document.id),
        patient_id=patient_id,
    )
    return document


@app.post(
    "/api/documents/{document_id}/extract",
    response_model=ExtractionJobRead,
    status_code=202,
)
def enqueue_extraction(
    document_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(require_write_access),
) -> ExtractionJob:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    existing = session.scalar(
        select(ExtractionJob).where(
            ExtractionJob.document_id == document_id,
            ExtractionJob.status.in_(("queued", "processing")),
        )
    )
    if existing:
        return existing
    job = ExtractionJob(document_id=document_id)
    document.status = DocumentStatus.processing
    session.add(job)
    session.commit()
    return job


@app.get("/api/jobs/{job_id}", response_model=ExtractionJobRead)
def get_job(
    job_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> ExtractionJob:
    job = session.get(ExtractionJob, job_id)
    if job is None:
        raise HTTPException(404, "Extraction job not found")
    return job


@app.post(
    "/api/documents/{document_id}/submit-to-model", response_model=ModelSubmissionRead
)
def submit_to_model(
    document_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(require_write_access),
) -> ModelSubmissionRead:
    """Send a source to the configured gateway and persist only schema-valid facts."""
    if not settings.model_endpoint:
        raise HTTPException(503, "MODEL_ENDPOINT is not configured")
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    text = extract_text(store.get(document.object_key), document.media_type)
    provider = HttpModelProvider(
        settings.model_endpoint, settings.model_name, settings.model_api_key
    )
    try:
        facts = []
        for item in provider.extract(
            {
                "patient_id": document.patient_id,
                "document_id": str(document.id),
                "document_type": document.document_type.value,
                "text": text,
            }
        ):
            item["patient_id"] = document.patient_id
            validated = FactCreate.model_validate(item)
            facts.append(Fact(source_document_id=document.id, **validated.model_dump()))
        session.add_all(facts)
        document.status = DocumentStatus.ready
        session.commit()
    except Exception as exc:
        session.rollback()
        raise HTTPException(502, f"Model submission failed: {exc}") from exc
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="submit_to_model",
        resource_type="document",
        resource_id=str(document.id),
        patient_id=document.patient_id,
        detail=f"{len(facts)} facts created",
    )
    return ModelSubmissionRead(
        document_id=document.id,
        provider=provider.name,
        accepted=True,
        facts_created=len(facts),
        message="Validated model facts persisted",
    )


@app.get("/api/documents", response_model=DocumentList)
def list_documents(
    patient_id: str | None = None,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> DocumentList:
    query = (
        select(Document)
        .options(selectinload(Document.facts))
        .order_by(Document.created_at.desc())
    )
    count_query = select(func.count(Document.id))
    if patient_id:
        query = query.where(Document.patient_id == patient_id)
        count_query = count_query.where(Document.patient_id == patient_id)
    return DocumentList(
        items=list(session.scalars(query)), total=session.scalar(count_query) or 0
    )


@app.get("/api/documents/{document_id}", response_model=DocumentRead)
def get_document(
    document_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> Document:
    document = session.scalar(
        select(Document)
        .where(Document.id == document_id)
        .options(selectinload(Document.facts))
    )
    if document is None:
        raise HTTPException(404, "Document not found")
    return document


@app.get("/api/documents/{document_id}/content")
def get_document_content(
    document_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="view_document_content",
        resource_type="document",
        resource_id=str(document.id),
        patient_id=document.patient_id,
    )
    return Response(store.get(document.object_key), media_type=document.media_type)


@app.post(
    "/api/documents/{document_id}/facts", response_model=FactRead, status_code=201
)
def create_fact(
    document_id: uuid.UUID,
    payload: FactCreate,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(require_write_access),
) -> Fact:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    if payload.patient_id != document.patient_id:
        raise HTTPException(409, "Fact patient does not match document patient")
    fact = Fact(source_document_id=document_id, **payload.model_dump())
    session.add(fact)
    document.status = DocumentStatus.ready
    session.commit()
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="create_fact",
        resource_type="fact",
        resource_id=str(fact.id),
        patient_id=document.patient_id,
    )
    return fact


@app.get("/api/patients/{patient_id}/timeline", response_model=TimelineRead)
def patient_timeline(
    patient_id: str,
    group_by: str = "month",
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> TimelineRead:
    if group_by not in {"month", "visit"}:
        raise HTTPException(422, "group_by must be month or visit")
    rows = session.execute(
        select(Fact, Document)
        .join(Document, Fact.source_document_id == Document.id)
        .where(Fact.patient_id == patient_id)
        .order_by(Fact.observed_date, Fact.created_at)
    ).all()
    groups: dict[str, list[TimelineEntry]] = {}
    prior: dict[tuple[str, str | None], Fact] = {}
    for fact, document in rows:
        key = (
            fact.observed_date.strftime("%Y-%m")
            if group_by == "month"
            else fact.observed_date.isoformat()
        )
        comparison = prior.get((fact.test_or_finding.lower(), fact.unit))
        delta = None
        if comparison and fact.value is not None and comparison.value is not None:
            try:
                delta = round(float(fact.value) - float(comparison.value), 6)
            except ValueError:
                pass
        entry = TimelineEntry(
            id=fact.id,
            observed_date=fact.observed_date,
            fact_type=fact.fact_type,
            test_or_finding=fact.test_or_finding,
            value=fact.value,
            unit=fact.unit,
            status=fact.status,
            source_document_id=document.id,
            source_filename=document.filename,
            evidence_location=fact.evidence_location,
            confidence=fact.confidence,
            reliability_tier=analysis.reliability_tier(fact),
            details=fact.details or {},
            prior_value=comparison.value if comparison else None,
            numeric_delta=delta,
        )
        groups.setdefault(key, []).append(entry)
        prior[(fact.test_or_finding.lower(), fact.unit)] = fact
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="view_timeline",
        resource_type="patient",
        patient_id=patient_id,
    )
    return TimelineRead(
        patient_id=patient_id,
        groups=dict(reversed(list(groups.items()))),
        total=len(rows),
    )


@app.get("/api/patients/{patient_id}/flags", response_model=PatientFlagsRead)
def patient_flags(
    patient_id: str,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> PatientFlagsRead:
    facts = list(
        session.scalars(
            select(Fact)
            .where(Fact.patient_id == patient_id)
            .order_by(Fact.observed_date, Fact.created_at)
        )
    )
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="view_flags",
        resource_type="patient",
        patient_id=patient_id,
    )
    return PatientFlagsRead(
        patient_id=patient_id,
        trends=analysis.compute_trends(facts),
        contradictions=analysis.detect_contradictions(facts),
    )


@app.post("/api/patients/{patient_id}/ask", response_model=AskResponse)
def ask_patient_question(
    patient_id: str,
    payload: AskRequest,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> AskResponse:
    facts = list(session.scalars(select(Fact).where(Fact.patient_id == patient_id)))
    result = qa.answer_question(
        facts,
        payload.question,
        model_endpoint=settings.model_endpoint,
        model_name=settings.model_name,
        model_api_key=settings.model_api_key,
    )
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="ask_question",
        resource_type="patient",
        patient_id=patient_id,
        detail=payload.question[:200],
    )
    return AskResponse(
        answer=result.answer,
        cited_fact_ids=result.cited_fact_ids,
        evidence=result.evidence,
        confidence=result.confidence,
        insufficient_evidence=result.insufficient_evidence,
    )


@app.post("/api/documents/{document_id}/cxr-analyze", response_model=CXRAnalysisRead)
def cxr_analyze(
    document_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(require_write_access),
) -> CXRAnalysisRead:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(404, "Document not found")
    if document.media_type not in {"image/png", "image/jpeg"}:
        raise HTTPException(415, "CXR analysis requires a PNG or JPEG chest X-ray image")
    try:
        result = imaging.analyze(store.get(document.object_key))
    except imaging.CXRUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    fact = Fact(
        source_document_id=document.id,
        patient_id=document.patient_id,
        fact_type="imaging",
        test_or_finding="Chest X-ray findings",
        observed_date=document.created_at.date(),
        evidence_location={"page": 1, "quote": None},
        confidence=max(result.findings.values()) if result.findings else 0.0,
        details={
            "findings": result.findings,
            "model_version": result.model_version,
            "checkpoint_sha256": result.checkpoint_sha256,
            "modality": "chest_xray",
        },
    )
    session.add(fact)
    session.commit()
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="cxr_analyze",
        resource_type="document",
        resource_id=str(document.id),
        patient_id=document.patient_id,
    )
    return CXRAnalysisRead(
        document_id=document.id,
        findings=result.findings,
        model_version=result.model_version,
        checkpoint_sha256=result.checkpoint_sha256,
    )


@app.get("/api/audit", response_model=list[AuditEventRead])
def list_audit_events(
    patient_id: str | None = None,
    limit: int = 200,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(require_admin),
) -> list[AuditEvent]:
    query = select(AuditEvent).order_by(AuditEvent.occurred_at.desc()).limit(min(limit, 1000))
    if patient_id:
        query = query.where(AuditEvent.patient_id == patient_id)
    return list(session.scalars(query))


frontend = Path(__file__).parent / "web"
app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
