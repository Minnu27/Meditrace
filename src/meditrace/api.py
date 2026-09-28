from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from hashlib import sha256
import json
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

from . import analysis, anchoring, audit, imaging, qa, verify
from .auth import (
    CurrentUser,
    authenticate,
    create_access_token,
    get_current_user,
    login_required,
    require_admin,
    require_write_access,
    ACCESS_TOKEN_MINUTES,
)
from .config import get_settings
from .database import create_schema, get_session, engine
from .extraction import HttpModelProvider, extract_text
from .models import Answer, AuditEvent, Document, ExtractionJob, Fact
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
from .provenance import (
    MANUAL_EXTRACTOR,
    compute_answer_hash,
    new_salt,
    seal_fact,
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
        "default-src 'self'; img-src 'self' data: blob:; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; frame-src blob:; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api/"):
        # Patient data must never sit in a browser, proxy or CDN cache.
        response.headers["Cache-Control"] = "no-store"
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


def _security_status() -> str:
    secret_ok = bool(os.getenv("SECRET_KEY")) or not login_required()
    if secret_ok and os.getenv("ENCRYPTION_KEY"):
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
        login_required=login_required(),
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
        payload_digest=document.sha256,
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
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="enqueue_extraction",
        resource_type="document",
        resource_id=str(document.id),
        patient_id=document.patient_id,
    )
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
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="view_job",
        resource_type="extraction_job",
        resource_id=str(job.id),
    )
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
            fact = Fact(id=uuid.uuid4(), source_document_id=document.id, **validated.model_dump())
            seal_fact(
                fact,
                source_sha256=document.sha256,
                extractor=f"model:{provider.name}",
                extractor_version=None,
                prompt_version=provider.prompt_version,
            )
            facts.append(fact)
        session.add_all(facts)
        document.status = DocumentStatus.ready
        session.commit()
    except Exception as exc:
        session.rollback()
        # The exception text can carry the gateway URL or source text; keep it
        # out of the response and the logs.
        logging.getLogger(__name__).error(
            "Model submission failed (%s)", type(exc).__name__
        )
        raise HTTPException(502, "Model submission failed") from exc
    for fact in facts:
        audit.record(
            session,
            user_email=user.email,
            role=user.role,
            action="submit_to_model",
            resource_type="fact",
            resource_id=str(fact.id),
            patient_id=fact.patient_id,
            payload_digest=fact.content_hash,
        )
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="model_submission_completed",
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
    result = DocumentList(
        items=list(session.scalars(query)), total=session.scalar(count_query) or 0
    )
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="list_documents",
        resource_type="document",
        patient_id=patient_id,
        detail=f"{result.total} documents",
    )
    return result


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
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="view_document",
        resource_type="document",
        resource_id=str(document.id),
        patient_id=document.patient_id,
    )
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
    fact = Fact(id=uuid.uuid4(), source_document_id=document_id, **payload.model_dump())
    seal_fact(
        fact,
        source_sha256=document.sha256,
        extractor=f"{MANUAL_EXTRACTOR}:{user.role}",
        extractor_version=None,
    )
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
        payload_digest=fact.content_hash,
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
    as_of: date | None = None,
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
    as_of = as_of or date.today()
    return PatientFlagsRead(
        patient_id=patient_id,
        as_of=as_of,
        trends=analysis.compute_trends(facts),
        contradictions=analysis.detect_contradictions(facts),
        gaps=analysis.detect_gaps(facts, as_of=as_of),
        rule_versions={
            "trends": analysis.TREND_THRESHOLD_VERSION,
            "gaps": analysis.GAP_RULES_VERSION,
        },
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
    by_id = {str(f.id): f for f in facts}
    answer = Answer(
        id=uuid.uuid4(),
        patient_id=patient_id,
        question=payload.question,
        answer_text=result.answer,
        cited_fact_ids=list(result.cited_fact_ids),
        cited_fact_hashes={fid: by_id[fid].content_hash for fid in result.cited_fact_ids if fid in by_id},
        insufficient_evidence=result.insufficient_evidence,
        confidence=result.confidence,
        answerer=result.answerer,
        answerer_version=result.answerer_version,
        prompt_version=result.prompt_version,
        commitment_salt=new_salt(),
    )
    answer.content_hash = compute_answer_hash(answer)
    session.add(answer)
    session.commit()
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="ask_question",
        resource_type="answer",
        resource_id=str(answer.id),
        patient_id=patient_id,
        # Questions can contain identifying free text: the log keeps only its
        # size in the clear; the text itself is encrypted in `answers` and
        # committed to through payload_digest.
        detail=f"question of {len(payload.question)} characters",
        payload_digest=answer.content_hash,
    )
    return AskResponse(
        answer=result.answer,
        cited_fact_ids=result.cited_fact_ids,
        evidence=result.evidence,
        confidence=result.confidence,
        insufficient_evidence=result.insufficient_evidence,
        answer_id=answer.id,
        content_hash=answer.content_hash,
        answerer=result.answerer,
        prompt_version=result.prompt_version,
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
        id=uuid.uuid4(),
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
    seal_fact(
        fact,
        source_sha256=document.sha256,
        extractor=f"cxr:{result.model_version}",
        extractor_version=result.checkpoint_sha256[:16],
    )
    session.add(fact)
    session.commit()
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="cxr_analyze",
        resource_type="fact",
        resource_id=str(fact.id),
        patient_id=document.patient_id,
        payload_digest=fact.content_hash,
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
    query = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(min(limit, 1000))
    if patient_id:
        query = query.where(AuditEvent.patient_id == patient_id)
    events = list(session.scalars(query))
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="view_audit_log",
        resource_type="audit",
        patient_id=patient_id,
    )
    return events


@app.get("/api/audit/verify")
def verify_audit_chain(
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(require_admin),
) -> dict:
    report = audit.verify_chain(session)
    latest = anchoring.latest_anchor(session)
    anchors_ok = True
    if latest is not None:
        checks = anchoring.anchor_checks(session, latest)
        anchors_ok = checks["signature_valid"] and checks["current_log_matches_root"]
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action="verify_audit_chain",
        resource_type="audit",
        success=report.ok and anchors_ok,
        detail=f"{report.length} entries checked",
    )
    return {
        "ok": report.ok and anchors_ok,
        "length": report.length,
        "head_seq": report.head_seq,
        "head_hash": report.head_hash,
        "problems": report.problems,
        "latest_anchor": anchoring.anchor_public(latest) if latest else None,
        "latest_anchor_valid": anchors_ok if latest else None,
        "unanchored_entries": (report.head_seq or 0) - (latest.to_seq if latest else 0),
    }


# ------------------------------------------------------------- anchors


@app.get("/api/anchors/public-key")
def anchor_public_key() -> dict:
    """Public on purpose: third parties need it to check anchor signatures."""
    try:
        return anchoring.server_public_key()
    except anchoring.AnchorError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/anchors")
def list_anchors(
    limit: int = 50,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    rows = session.scalars(
        select(anchoring.Anchor).order_by(anchoring.Anchor.to_seq.desc()).limit(min(limit, 500))
    )
    return [anchoring.anchor_public(a) for a in rows]


@app.post("/api/anchors", status_code=201)
def create_anchor_now(
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(require_admin),
) -> dict:
    try:
        anchor = anchoring.create_anchor(session, tsa_url=settings.anchor_tsa_url)
    except anchoring.AnchorError as exc:
        raise HTTPException(409, str(exc)) from exc
    if anchor is None:
        return {"anchored": False, "message": "No new audit entries since the last anchor"}
    return {"anchored": True, "anchor": anchoring.anchor_public(anchor)}


# -------------------------------------------------------------- verify


def _audited_verify(session: Session, user: CurrentUser, kind: str, record_id: uuid.UUID, report: dict | None, action: str):
    if report is None:
        raise HTTPException(404, f"{kind.title()} not found")
    audit.record(
        session,
        user_email=user.email,
        role=user.role,
        action=action,
        resource_type=kind,
        resource_id=str(record_id),
        patient_id=(report.get("fact") or report.get("answer") or report.get("commitment") or {}).get("patient_id"),
        success=report.get("verdict", "verified") != "failed",
        detail=report.get("verdict"),
    )


@app.get("/api/verify/facts/{fact_id}")
def verify_fact(
    fact_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    report = verify.verify_fact(session, store, fact_id)
    _audited_verify(session, user, "fact", fact_id, report, "verify_fact")
    return report


@app.get("/api/verify/answers/{answer_id}")
def verify_answer(
    answer_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> dict:
    report = verify.verify_answer(session, store, answer_id)
    _audited_verify(session, user, "answer", answer_id, report, "verify_answer")
    return report


@app.get("/api/verify/{kind}/{record_id}/bundle")
def download_proof_bundle(
    kind: str,
    record_id: uuid.UUID,
    session: Session = Depends(get_session),
    user: CurrentUser = Depends(get_current_user),
) -> Response:
    if kind not in {"facts", "answers"}:
        raise HTTPException(404, "Unknown record kind")
    singular = kind[:-1]
    bundle = verify.proof_bundle(session, singular, record_id)
    _audited_verify(session, user, singular, record_id, bundle, "export_proof_bundle")
    return Response(
        json.dumps(bundle, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="meditrace-proof-{singular}-{str(record_id)[:8]}.json"'},
    )


frontend = Path(__file__).parent / "web"
app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
