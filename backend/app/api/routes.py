from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.deps import current_user, get_session, require_admin
from app.config import get_settings
from app.core.chunking import segment_budget
from app.core.errors import AppError
from app.core.languages import LANGUAGES, known_language
from app.core.model_registry import all_models
from app.core.service import (
    cancel_job,
    cost_for_job,
    cost_stats,
    create_document,
    create_job,
    debug_provider_calls,
    edit_translation,
    estimate_for,
    public_estimate,
    render_job,
)
from app.db.models import Block, BlockTranslation, Document, JobEvent, ProviderCall, Segment, TranslationJob, User

router = APIRouter()


class JobIn(BaseModel):
    document_id: uuid.UUID
    target_lang: str
    source_lang: str | None = None
    model: str | None = None
    glossary: list[dict] | None = None
    render_mode: str = "layout"


class EditIn(BaseModel):
    block_idx: int
    text: str


def owned(session: Session, model, ident: uuid.UUID, user: User):
    row = session.get(model, ident)
    if row is None or row.owner_id != user.id:
        label = "Document" if getattr(model, "__name__", "") == "Document" else "Job"
        raise AppError("not_found", f"{label} not found.", str(ident), False, 404)
    return row


def _document(document: Document) -> dict:
    return {
        "id": str(document.id),
        "filename": document.filename,
        "format": document.format,
        "size_bytes": document.size_bytes,
        "page_count": document.page_count,
        "char_count": document.char_count,
        "detected_source_lang": document.detected_source_lang,
        "analysis_status": document.analysis_status,
        "analysis": document.analysis,
        "error_code": document.error_code,
        "error_detail": document.error_detail,
        "created_at": document.created_at.isoformat() if document.created_at else None,
    }


def _filename(session: Session, document_id: uuid.UUID) -> str | None:
    document = session.get(Document, document_id)
    if document is None:
        return None
    return document.filename


def _job(job: TranslationJob, filename: str | None = None) -> dict:
    return {
        "id": str(job.id),
        "document_id": str(job.document_id),
        "document_filename": filename,
        "source_lang": job.source_lang,
        "target_lang": job.target_lang,
        "model": job.model,
        "segment_budget_tokens": job.segment_budget_tokens,
        "status": job.status,
        "total_segments": job.total_segments,
        "done_segments": job.done_segments,
        "tokens_in": job.tokens_in,
        "tokens_out": job.tokens_out,
        "cost_cents": float(job.cost_cents or 0),
        "error_code": job.error_code,
        "error_detail": job.error_detail,
        "output_stale": job.output_stale,
        "render_mode": job.render_mode,
        "quality_warning": job.quality_warning,
        "summary": job.summary,
        "estimate_cents": None if job.estimate_cents is None else float(job.estimate_cents),
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


@router.post("/documents")
async def upload_document(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    settings = get_settings()
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise AppError(
            "file_too_large",
            f"This file is {file.size} bytes, above the {settings.max_upload_mb} MB limit.",
            str(file.size),
            False,
            413,
        )
    data = await file.read()
    document = create_document(session, data, file.filename or "upload", user.id)
    return _document(document)


@router.get("/documents/{document_id}")
def read_document(document_id: uuid.UUID, session: Session = Depends(get_session), user: User = Depends(current_user)):
    return _document(owned(session, Document, document_id, user))


@router.get("/documents")
def list_documents(session: Session = Depends(get_session), user: User = Depends(current_user)):
    rows = session.scalars(
        select(Document).where(Document.owner_id == user.id).order_by(Document.created_at.desc()).limit(100)
    ).all()
    return [_document(row) for row in rows]


@router.post("/jobs")
def post_job(
    body: JobIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    owned(session, Document, body.document_id, user)
    job = create_job(
        session,
        body.document_id,
        body.target_lang,
        body.source_lang,
        body.model,
        body.glossary,
        idempotency_key,
        body.render_mode,
    )
    session.commit()
    return _job(job, _filename(session, job.document_id))


@router.get("/documents/{document_id}/estimate")
def document_estimate(
    document_id: uuid.UUID,
    target_lang: str,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    document = owned(session, Document, document_id, user)
    if not known_language(target_lang):
        raise AppError(
            "unknown_language",
            f"Target language '{target_lang}' is not supported.",
            target_lang,
            False,
            400,
        )
    return {"models": [public_estimate(estimate_for(session, document, target_lang, spec.alias)) for spec in all_models()]}


@router.get("/jobs")
def list_jobs(session: Session = Depends(get_session), user: User = Depends(current_user)):
    rows = session.execute(
        select(TranslationJob, Document.filename)
        .join(Document, Document.id == TranslationJob.document_id)
        .where(TranslationJob.owner_id == user.id)
        .order_by(TranslationJob.created_at.desc())
        .limit(100)
    ).all()
    return [_job(job, filename) for job, filename in rows]


@router.get("/jobs/{job_id}")
def read_job(job_id: uuid.UUID, session: Session = Depends(get_session), user: User = Depends(current_user)):
    job = owned(session, TranslationJob, job_id, user)
    return _job(job, _filename(session, job.document_id))


@router.post("/jobs/{job_id}/cancel")
def post_cancel(job_id: uuid.UUID, session: Session = Depends(get_session), user: User = Depends(current_user)):
    owned(session, TranslationJob, job_id, user)
    job = cancel_job(session, job_id)
    return _job(job, _filename(session, job.document_id))


@router.get("/jobs/{job_id}/segments")
def read_segments(job_id: uuid.UUID, session: Session = Depends(get_session), user: User = Depends(current_user)):
    job = owned(session, TranslationJob, job_id, user)
    segments = session.scalars(select(Segment).where(Segment.job_id == job_id).order_by(Segment.idx)).all()
    payload = []
    for segment in segments:
        blocks = session.scalars(
            select(Block).where(
                Block.document_id == job.document_id,
                Block.idx >= segment.block_start,
                Block.idx <= segment.block_end,
            ).order_by(Block.idx)
        ).all()
        items = []
        for block in blocks:
            translation = session.get(BlockTranslation, (job_id, block.idx))
            items.append(
                {
                    "block_idx": block.idx,
                    "kind": block.kind,
                    "page": block.page,
                    "source": block.text,
                    "target": translation.tgt_text if translation else None,
                    "origin": translation.source if translation else None,
                }
            )
        payload.append(
            {
                "idx": segment.idx,
                "status": segment.status,
                "attempt": segment.attempt,
                "tokens_in": segment.tokens_in,
                "tokens_out": segment.tokens_out,
                "review_state": segment.review_state,
                "blocks": items,
            }
        )
    return payload


@router.patch("/jobs/{job_id}/segments/{segment_idx}")
def patch_segment(
    job_id: uuid.UUID,
    segment_idx: int,
    body: EditIn,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    owned(session, TranslationJob, job_id, user)
    edit_translation(session, job_id, body.block_idx, body.text)
    return {"ok": True}


@router.post("/jobs/{job_id}/render")
def post_render(job_id: uuid.UUID, session: Session = Depends(get_session), user: User = Depends(current_user)):
    owned(session, TranslationJob, job_id, user)
    job = render_job(session, job_id)
    return _job(job, _filename(session, job.document_id))


@router.get("/jobs/{job_id}/download")
def download(job_id: uuid.UUID, session: Session = Depends(get_session), user: User = Depends(current_user)):
    job = owned(session, TranslationJob, job_id, user)
    if job.output_stale or not job.output_path:
        if job.status not in {"done", "rendering"}:
            raise AppError("not_ready", "The translated file is not ready yet.", job.status, True, 409)
        render_job(session, job_id)
        session.flush()
    path = job.output_path
    if not path:
        raise AppError("not_ready", "The translated file is not ready yet.", job.status, True, 409)
    filename = path.rsplit("/", 1)[-1]
    return FileResponse(path, filename=filename)


@router.get("/jobs/{job_id}/events")
def events(job_id: uuid.UUID, session: Session = Depends(get_session), user: User = Depends(current_user)):
    owned(session, TranslationJob, job_id, user)
    rows = session.scalars(select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)).all()
    return [
        {"id": row.id, "ts": row.ts.isoformat() if row.ts else None, "level": row.level, "event": row.event, "payload": row.payload}
        for row in rows
    ]


@router.get("/jobs/{job_id}/cost")
def job_cost(job_id: uuid.UUID, session: Session = Depends(get_session), user: User = Depends(current_user)):
    owned(session, TranslationJob, job_id, user)
    return cost_for_job(session, job_id)


@router.get("/stats/cost")
def stats(since_hours: int = 24, session: Session = Depends(get_session), user: User = Depends(current_user)):
    return cost_stats(session, user.id, since_hours)


@router.get("/meta/languages")
def languages():
    return LANGUAGES


@router.get("/meta/config")
def meta_config():
    settings = get_settings()
    return {"max_upload_mb": settings.max_upload_mb, "allow_signup": settings.allow_signup}


@router.get("/meta/models")
def models(source_lang: str = "en", target_lang: str = "de"):
    settings = get_settings()
    rows = []
    for spec in all_models():
        row = spec.public()
        row["segment_budget"] = segment_budget(
            spec,
            source_lang,
            target_lang,
            settings.prompt_overhead_tokens,
            settings.global_quality_cap,
        )
        rows.append(row)
    return rows


@router.get("/documents/{document_id}/pages/{page_no}/thumbnail")
def page_thumbnail(
    document_id: uuid.UUID,
    page_no: int,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    document = owned(session, Document, document_id, user)
    if not document.analysis:
        raise AppError("not_found", "Document not found.", str(document_id), False, 404)
    match = next((page for page in document.analysis.get("pages", []) if page.get("page") == page_no), None)
    raw = (match or {}).get("thumbnail")
    if not raw:
        raise AppError("not_found", "This page has no thumbnail.", str(page_no), False, 404)
    path = Path(raw).resolve()
    root = Path(get_settings().files_dir).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise AppError("not_found", "This page has no thumbnail.", str(page_no), False, 404)
    return FileResponse(path)


@router.get("/debug/provider-calls")
def provider_calls(session: Session = Depends(get_session), _admin: User = Depends(require_admin)):
    return debug_provider_calls(session)


@router.get("/meta/queue")
def queue(session: Session = Depends(get_session)):
    counts = dict(session.execute(select(TranslationJob.status, func.count()).group_by(TranslationJob.status)).all())
    calls = session.scalar(select(func.count()).select_from(ProviderCall)) or 0
    return {"jobs": {key: int(value) for key, value in counts.items()}, "provider_calls": int(calls)}
