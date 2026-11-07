from __future__ import annotations

import hashlib
import time
import uuid
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.analysis.page_classifier import route_for
from app.core.analysis.preprocess import preprocess_page, to_png
from app.core.chunking import chunk_blocks, segment_budget
from app.core.domain import BlockData
from app.core.errors import AppError, CostCapExceeded, InjectedCrash, RateLimited
from app.core.events import after_commit, publish_document, publish_job
from app.core.formats.registry import adapter_for, detect_format
from app.core.formats.sniff import sanitized_filename
from app.core.languages import known_language
from app.core.model_registry import all_models, cost_cents, image_cost_cents, image_tokens, resolve_model
from app.core.numbered import parse_numbered_blocks, usable_translation
from app.core.prompts import PROMPT_VERSION, SUMMARY_PROMPT, system_prompt, user_prompt
from app.core.provider.base import TranslateRequest
from app.core.provider.factory import build_ocr, build_provider, build_vision
from app.core.quality import flags_for
from app.core.tokenize import count_tokens
from app.db.models import (
    Block,
    BlockTranslation,
    Document,
    JobEvent,
    OcrPage,
    ProviderCall,
    Segment,
    TranslationJob,
    TranslationMemory,
)
from app.db.session import session_factory
from app.observability.logging import get_logger
from app.observability.metrics import COST, INFLIGHT, PROVIDER_ERRORS, RATE_LIMITS, SEGMENT_LATENCY, TOKENS
from app.observability.tracing import span

log = get_logger()


def _provider():
    if not hasattr(_provider, "instance"):
        _provider.instance = build_provider()
    return _provider.instance


def _ocr():
    if not hasattr(_ocr, "instance"):
        _ocr.instance = build_ocr(_provider())
    return _ocr.instance


def _vision():
    if not hasattr(_vision, "instance"):
        _vision.instance = build_vision(_provider())
    return _vision.instance


def reset_runtime() -> None:
    for fn in (_provider, _ocr, _vision):
        if hasattr(fn, "instance"):
            delattr(fn, "instance")


def create_document(session: Session, filename: str, data: bytes) -> Document:
    settings = get_settings()
    if len(data) > settings.max_upload_bytes:
        raise AppError(
            "file_too_large",
            f"This file is {len(data)} bytes, above the {settings.max_upload_mb} MB limit.",
            str(len(data)),
            False,
            413,
        )
    fmt = detect_format(data, filename)
    digest = hashlib.sha256(data).hexdigest()
    existing = session.scalar(select(Document).where(Document.sha256 == digest).order_by(Document.created_at.desc()))
    if existing and existing.analysis_status in {"queued", "running", "ready", "failed"}:
        return existing
    document_id = uuid.uuid4()
    uploads = Path(settings.files_dir) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    ext = {"pdf": ".pdf", "docx": ".docx", "png": ".png", "jpeg": ".jpg"}[fmt]
    path = uploads / f"{document_id}{ext}"
    path.write_bytes(data)
    root = Path(settings.files_dir).resolve()
    if not path.resolve().is_relative_to(root):
        path.unlink(missing_ok=True)
        raise AppError("invalid_file", "The file name tried to escape the upload directory.", filename, False, 400)
    document = Document(
        id=document_id,
        filename=sanitized_filename(filename),
        format=fmt,
        size_bytes=len(data),
        sha256=digest,
        storage_path=str(path),
        analysis_status="queued",
    )
    session.add(document)
    session.flush()
    return document


def run_analysis(session: Session, document_id: uuid.UUID) -> None:
    document = session.get(Document, document_id)
    if document is None or document.analysis_status == "ready":
        return
    settings = get_settings()
    logger = log.bind(job_id=None, stage="analyze", document_id=str(document_id))
    logger.info("analysis_started")
    try:
        with span("analyze", document_id=str(document_id)):
            adapter = adapter_for(document.format)
            extracted = adapter.extract(Path(document.storage_path), Path(settings.files_dir))
            _apply_tier1(session, document, extracted)
            from app.core.agents.document_guardrail import ensure_is_document

            ensure_is_document(extracted.pages)
            session.query(Block).filter(Block.document_id == document.id).delete()
            ordered = sorted(extracted.blocks, key=lambda block: (block.page, block.seq))
            for position, block in enumerate(ordered):
                block.idx = block.page * 100000 + block.seq
                session.add(_block_row(document.id, block))
            char_count = sum(len(block.text or "") for block in ordered)
            analysis = _analysis_payload(extracted, ordered)
            document.page_count = extracted.page_count
            document.char_count = char_count
            document.detected_source_lang = extracted.detected_source_lang
            document.analysis = analysis
            document.analysis_status = "ready"
            document.error_code = None
            document.error_detail = None
            _publish_document(session, document)
            logger.info("analysis_ready", pages=extracted.page_count, blocks=len(ordered))
    except AppError as exc:
        document.analysis_status = "failed"
        document.error_code = exc.code
        document.error_detail = exc.message
        _publish_document(session, document)
        logger.info("analysis_failed", error_code=exc.code)
    except Exception as exc:
        document.analysis_status = "failed"
        document.error_code = "analysis_failed"
        document.error_detail = str(exc)
        _publish_document(session, document)
        logger.info("analysis_failed", error_code="analysis_failed")


def _apply_tier1(session: Session, document: Document, extracted) -> None:
    vision = _vision()
    for page in extracted.pages:
        if page.page_class != "unknown":
            continue
        image = _page_png(document, page.page)
        if image is None:
            page.page_class = "scanned_poor"
            page.route = route_for(page.page_class)
            continue
        started = time.perf_counter()
        verdict = vision.classify(image)
        kind = verdict.get("kind")
        if kind == "non_document":
            page.page_class = "non_document"
        elif verdict.get("legibility") == "poor":
            page.page_class = "scanned_poor"
        else:
            page.page_class = "scanned_good"
        page.route = route_for(page.page_class)
        page.metrics["tier1"] = verdict
        elapsed = int((time.perf_counter() - started) * 1000)
        _record(
            stage="classify",
            model=get_settings().classify_model,
            tokens_in=200,
            tokens_out=40,
            tokens_cached=0,
            latency_ms=elapsed,
            status="ok",
            error_code=None,
            attempt=1,
            page_no=page.page,
        )


def _analysis_payload(extracted, blocks: list[BlockData]) -> dict:
    settings = get_settings()
    counts: dict[str, int] = {}
    for page in extracted.pages:
        counts[page.page_class] = counts.get(page.page_class, 0) + 1
    text = "\n".join(block.text or "" for block in blocks if block.text)
    ocr_pages = [page for page in extracted.pages if page.route in {"ocr", "ocr_enhance"}]
    ocr_spec = resolve_model(settings.ocr_model)
    per_model = []
    for spec in all_models():
        tokens = count_tokens(spec, text)
        synthetic = [
            BlockData.make(page=block.page, seq=block.seq, kind=block.kind, text=block.text, meta=dict(block.meta))
            for block in blocks
            if block.kind != "image"
        ]
        for page in ocr_pages:
            synthetic.append(BlockData.make(page=page.page, seq=9000, kind="paragraph", text="x " * 200))
        budget = segment_budget(spec, extracted.detected_source_lang, "en", settings.prompt_overhead_tokens, settings.global_quality_cap)
        groups = chunk_blocks(synthetic, budget, lambda value: count_tokens(spec, value))
        ocr_cents = Decimal(0)
        for page in ocr_pages:
            ocr_cents += image_cost_cents(ocr_spec, 1500, 2000, "high")
        translate_in = tokens + len(ocr_pages) * 400 + settings.prompt_overhead_tokens * max(len(groups), 1)
        translate_out = int(translate_in * 0.8)
        cents = cost_cents(spec, translate_in, translate_out) + ocr_cents
        duration = round(max(len(groups), 1) / max(settings.job_max_concurrency, 1) * 20, 1)
        per_model.append(
            {
                "model": spec.alias,
                "model_id": spec.id,
                "tier": spec.quality_tier,
                "tokens": tokens,
                "segments": len(groups),
                "segment_budget": budget,
                "cost_cents": float(cents),
                "ocr_cost_cents": float(ocr_cents),
                "duration_seconds": duration,
                "input_price_per_1m": spec.input_price_per_1m,
                "output_price_per_1m": spec.output_price_per_1m,
            }
        )
    return {
        "pages": [
            {
                "page": page.page,
                "class": page.page_class,
                "route": page.route,
                "metrics": page.metrics,
                "thumbnail": page.thumbnail_path,
            }
            for page in extracted.pages
        ],
        "class_counts": counts,
        "class_lines": _class_lines(extracted.pages, ocr_spec),
        "models": per_model,
        "ocr_pages": [page.page for page in ocr_pages],
    }


def _class_lines(pages, ocr_spec) -> list[dict]:
    grouped: dict[str, list] = {}
    for page in pages:
        grouped.setdefault(page.page_class, []).append(page)
    lines = []
    for page_class, members in grouped.items():
        route = members[0].route
        if route == "ocr_enhance":
            action = "ocr_enhance"
            cents = sum(float(image_cost_cents(ocr_spec, 1500, 2000, "high")) for _ in members)
            warning = "Pages are enhanced before reading and may be less accurate."
        elif route == "ocr":
            action = "ocr"
            cents = sum(float(image_cost_cents(ocr_spec, 1500, 2000, "high")) for _ in members)
            warning = None
        elif route == "skip":
            action = "skipped"
            cents = 0.0
            warning = None
        elif page_class == "non_document":
            action = "rejected"
            cents = 0.0
            warning = None
        else:
            action = "translate"
            cents = 0.0
            warning = None
        lines.append(
            {
                "class": page_class,
                "pages": len(members),
                "action": action,
                "cost_cents": cents,
                "warning": warning,
            }
        )
    return lines


def create_job(
    session: Session,
    document_id: uuid.UUID,
    target_lang: str,
    source_lang: str | None,
    model: str | None,
    glossary: list[dict] | None,
    idempotency_key: str | None,
    render_mode: str = "reflow",
) -> TranslationJob:
    if idempotency_key:
        existing = session.scalar(select(TranslationJob).where(TranslationJob.idempotency_key == idempotency_key))
        if existing:
            return existing
    if not known_language(target_lang):
        raise AppError("unknown_language", f"Target language '{target_lang}' is not supported.", target_lang, False, 400)
    document = session.get(Document, document_id)
    if document is None:
        raise AppError("not_found", "Document not found.", str(document_id), False, 404)
    if document.analysis_status == "failed":
        raise AppError(
            document.error_code or "analysis_failed",
            document.error_detail or "This document could not be analyzed.",
            document.error_detail or "",
            False,
            409,
        )
    if document.analysis_status != "ready":
        raise AppError("not_ready", "Document analysis is still running. Poll the document and retry.", document.analysis_status, True, 409)
    spec = resolve_model(model)
    source = source_lang or document.detected_source_lang
    if render_mode not in {"reflow", "layout"}:
        raise AppError("invalid_render_mode", "Render mode must be reflow or layout.", render_mode, False, 400)
    job = TranslationJob(
        document_id=document.id,
        source_lang=source,
        target_lang=target_lang.lower(),
        model=spec.alias,
        glossary=glossary or [],
        idempotency_key=idempotency_key,
        render_mode=render_mode,
        status="queued",
    )
    session.add(job)
    session.flush()
    _event(session, job.id, "job_queued", {"model": spec.alias, "target": target_lang})
    return job


def cancel_job(session: Session, job_id: uuid.UUID) -> TranslationJob:
    job = session.get(TranslationJob, job_id)
    if job is None:
        raise AppError("not_found", "Job not found.", str(job_id), False, 404)
    if job.status in {"done", "failed", "cancelled"}:
        return job
    job.status = "cancelled"
    job.error_code = "cancelled"
    job.error_detail = "Cancelled by the client."
    _event(session, job.id, "cancelled", {})
    _publish_job(session, job)
    return job


def edit_translation(session: Session, job_id: uuid.UUID, block_idx: int, text: str) -> None:
    job = session.get(TranslationJob, job_id)
    if job is None:
        raise AppError("not_found", "Job not found.", str(job_id), False, 404)
    row = session.get(BlockTranslation, (job_id, block_idx))
    if row is None:
        session.add(BlockTranslation(job_id=job_id, block_idx=block_idx, tgt_text=text, source="user"))
    else:
        row.tgt_text = text
        row.source = "user"
    job.output_stale = True
    _event(session, job.id, "translation_edited", {"block_idx": block_idx})


def render_job(session: Session, job_id: uuid.UUID) -> TranslationJob:
    job = session.get(TranslationJob, job_id)
    if job is None:
        raise AppError("not_found", "Job not found.", str(job_id), False, 404)
    if job.status not in {"done", "rendering"} and not job.output_path:
        raise AppError("not_ready", "The translation is not ready to render yet.", job.status, True, 409)
    _render(session, job)
    return job


def process_available(session: Session, worker_id: str) -> bool:
    if _claim_analysis(session, worker_id):
        return True
    if _claim_extract(session, worker_id):
        return True
    if _claim_ocr(session, worker_id):
        return True
    if _claim_segment(session, worker_id):
        return True
    if _claim_review(session, worker_id):
        return True
    if _claim_render(session, worker_id):
        return True
    return False


def reap(session: Session) -> int:
    settings = get_settings()
    stale = settings.stale_seconds
    session.execute(
        text(
            """
            UPDATE segments
            SET status = CASE WHEN attempt >= :limit THEN 'failed' ELSE 'pending' END,
                locked_by = NULL
            WHERE status = 'running' AND locked_at < now() - make_interval(secs => :stale)
            """
        ),
        {"limit": settings.segment_attempt_limit, "stale": stale},
    )
    session.execute(
        text(
            """
            UPDATE ocr_pages SET status = 'pending', locked_by = NULL
            WHERE status = 'running' AND locked_at < now() - make_interval(secs => :stale)
            """
        ),
        {"stale": stale},
    )
    session.execute(
        text(
            """
            UPDATE translation_jobs
            SET status = 'queued', locked_by = NULL
            WHERE status = 'extracting' AND heartbeat_at < now() - make_interval(secs => :stale)
            """
        ),
        {"stale": stale},
    )
    session.execute(
        text(
            """
            UPDATE translation_jobs
            SET status = 'translating', locked_by = NULL
            WHERE status = 'reviewing' AND heartbeat_at < now() - make_interval(secs => :stale)
            """
        ),
        {"stale": stale},
    )
    session.execute(
        text(
            """
            UPDATE translation_jobs
            SET locked_by = NULL
            WHERE status = 'rendering'
              AND locked_by IS NOT NULL
              AND heartbeat_at < now() - make_interval(secs => :stale)
            """
        ),
        {"stale": stale},
    )
    stuck_ocr = session.execute(
        text(
            """
            SELECT id FROM translation_jobs
            WHERE status = 'ocr'
              AND heartbeat_at < now() - make_interval(secs => :stale)
              AND NOT EXISTS (
                SELECT 1 FROM ocr_pages p
                WHERE p.job_id = translation_jobs.id AND p.status <> 'done'
              )
            """
        ),
        {"stale": stale},
    ).scalars().all()
    for job_id in stuck_ocr:
        job = session.get(TranslationJob, job_id)
        if job is not None and job.status == "ocr":
            _advance_ocr_job(session, job)
    session.execute(
        text(
            """
            UPDATE documents SET analysis_status = 'queued'
            WHERE analysis_status = 'running'
              AND created_at < now() - interval '30 minutes'
              AND error_code IS NULL
            """
        )
    )
    session.execute(
        text(
            """
            UPDATE provider_gate
            SET effective_max = LEAST(effective_max + 1, ceiling),
                last_ramp_at = now()
            WHERE id = 1
              AND effective_max < ceiling
              AND (last_429_at IS NULL OR last_429_at < now() - interval '60 seconds')
              AND (last_ramp_at IS NULL OR last_ramp_at < now() - interval '60 seconds')
            """
        )
    )
    return 1


def sync_gate(session: Session) -> None:
    settings = get_settings()
    session.execute(
        text(
            """
            UPDATE provider_gate
            SET ceiling = :ceiling,
                tpm_budget = :tpm,
                effective_max = LEAST(effective_max, :ceiling)
            WHERE id = 1
            """
        ),
        {"ceiling": settings.provider_max_concurrency, "tpm": settings.provider_tpm_budget},
    )


def _claim_analysis(session: Session, worker_id: str) -> bool:
    row = session.execute(
        text(
            """
            UPDATE documents SET analysis_status = 'running'
            WHERE id = (
                SELECT id FROM documents WHERE analysis_status = 'queued'
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING id
            """
        )
    ).first()
    if not row:
        return False
    session.commit()
    run_analysis(session, row.id)
    session.commit()
    return True


def _claim_extract(session: Session, worker_id: str) -> bool:
    row = session.execute(
        text(
            """
            UPDATE translation_jobs
            SET status = 'extracting', locked_by = :worker, locked_at = now(), heartbeat_at = now()
            WHERE id = (
                SELECT id FROM translation_jobs WHERE status = 'queued'
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING id
            """
        ),
        {"worker": worker_id},
    ).first()
    if not row:
        return False
    session.commit()
    _extract_job(session, row.id, worker_id)
    session.commit()
    return True


def _extract_job(session: Session, job_id: uuid.UUID, worker_id: str) -> None:
    job = session.get(TranslationJob, job_id)
    document = session.get(Document, job.document_id)
    logger = log.bind(job_id=str(job_id), stage="extract", worker_id=worker_id)
    pages = (document.analysis or {}).get("pages") or []
    blank_pages = [page["page"] for page in pages if page["class"] == "blank"]
    ocr_pages = [page["page"] for page in pages if page["route"] in {"ocr", "ocr_enhance"}]
    text_blocks = session.scalars(select(Block).where(Block.document_id == document.id, Block.kind != "image", Block.text.is_not(None))).all()
    if not text_blocks and not ocr_pages:
        _fail(session, job, "ocr_no_text" if blank_pages else "empty_document", _empty_message(blank_pages))
        return
    if ocr_pages:
        for page_no in ocr_pages:
            session.merge(OcrPage(job_id=job.id, page_no=page_no, status="pending"))
        job.status = "ocr"
        job.locked_by = None
        _event(session, job.id, "ocr_started", {"pages": ocr_pages})
        _publish_job(session, job)
        logger.info("ocr_queued", pages=ocr_pages)
        return
    _ensure_summary(session, job, document)
    _build_segments(session, job)
    logger.info("segments_ready", total=job.total_segments)


def _claim_ocr(session: Session, worker_id: str) -> bool:
    row = session.execute(
        text(
            """
            UPDATE ocr_pages
            SET status = 'running', locked_by = :worker, locked_at = now()
            WHERE (job_id, page_no) = (
                SELECT job_id, page_no FROM ocr_pages
                WHERE status = 'pending'
                ORDER BY job_id, page_no
                FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING job_id, page_no
            """
        ),
        {"worker": worker_id},
    ).first()
    if not row:
        return False
    session.commit()
    _ocr_page(session, row.job_id, row.page_no, worker_id)
    session.commit()
    return True


def _ocr_page(session: Session, job_id: uuid.UUID, page_no: int, worker_id: str) -> None:
    job = session.get(TranslationJob, job_id)
    if job.status == "cancelled":
        return
    document = session.get(Document, job.document_id)
    logger = log.bind(job_id=str(job_id), stage="ocr", worker_id=worker_id, segment_id=None)
    page_meta = next((page for page in (document.analysis or {}).get("pages", []) if page["page"] == page_no), {})
    image = _page_png(document, page_no)
    if image and page_meta.get("route") == "ocr_enhance":
        from PIL import Image
        import io

        enhanced = preprocess_page(Image.open(io.BytesIO(image)))
        image = to_png(enhanced)
    started = time.perf_counter()
    try:
        with span("ocr_page", job_id=str(job_id), page=page_no):
            result = _ocr().transcribe(image or b"", job.source_lang or "")
    except AppError as exc:
        _record(stage="ocr", model=get_settings().ocr_model, tokens_in=0, tokens_out=0, tokens_cached=0, latency_ms=0, status="error", error_code=exc.code, attempt=1, job_id=job.id, page_no=page_no)
        _fail(session, job, exc.code, exc.message)
        return
    if not result.already_recorded:
        _record(
            stage="ocr",
            model=get_settings().ocr_model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            tokens_cached=0,
            latency_ms=int((time.perf_counter() - started) * 1000),
            status="ok",
            error_code=None,
            attempt=1,
            job_id=job.id,
            page_no=page_no,
        )
        _add_cost(session, job, get_settings().ocr_model, result.tokens_in, result.tokens_out)
    session.execute(text("DELETE FROM blocks WHERE document_id = :doc AND page = :page AND kind <> 'image'"), {"doc": document.id, "page": page_no})
    for offset, block in enumerate(result.blocks):
        if not block.text.strip():
            continue
        data = BlockData.make(page=page_no, seq=offset, kind=block.kind, text=block.text, meta={"ocr": True, "page_class": page_meta.get("class")})
        data.idx = page_no * 100000 + offset
        session.merge(_block_row(document.id, data))
    page_row = session.get(OcrPage, (job.id, page_no))
    page_row.status = "done"
    job.heartbeat_at = _now(session)
    logger.info("ocr_page_done", page=page_no, blocks=len(result.blocks))
    _advance_ocr_job(session, job)


def _advance_ocr_job(session: Session, job: TranslationJob) -> None:
    session.flush()
    remaining = session.execute(
        text("SELECT count(*) FROM ocr_pages WHERE job_id = :job AND status <> 'done'"),
        {"job": job.id},
    ).scalar()
    if remaining:
        return
    document = session.get(Document, job.document_id)
    text_left = session.scalar(
        select(Block).where(Block.document_id == document.id, Block.kind != "image", Block.text.is_not(None), Block.text != "")
    )
    if text_left is None:
        empty_pages = [row.page_no for row in session.scalars(select(OcrPage).where(OcrPage.job_id == job.id))]
        _fail(session, job, "ocr_no_text", f"No readable text was found on pages {', '.join(str(n) for n in empty_pages)}.")
        return
    _ensure_summary(session, job, document)
    _build_segments(session, job)


def _build_segments(session: Session, job: TranslationJob) -> None:
    settings = get_settings()
    spec = resolve_model(job.model)
    document = session.get(Document, job.document_id)
    rows = session.scalars(select(Block).where(Block.document_id == document.id).order_by(Block.page, Block.seq, Block.idx)).all()
    blocks = [_from_row(row) for row in rows if row.kind != "image"]
    counter = lambda value: count_tokens(spec, value)
    budget = segment_budget(spec, job.source_lang, job.target_lang, settings.prompt_overhead_tokens, settings.global_quality_cap)
    from app.core.chunking import _split_oversized

    expanded = _split_oversized(blocks, budget, counter)
    session.execute(text("DELETE FROM blocks WHERE document_id = :doc AND kind <> 'image'"), {"doc": document.id})
    for position, block in enumerate(expanded, start=1):
        block.idx = position
        session.add(_block_row(document.id, block))
    session.flush()
    pieces = chunk_blocks(expanded, budget, counter)
    session.execute(text("DELETE FROM segments WHERE job_id = :job"), {"job": job.id})
    source_lang = job.source_lang or "en"
    done = 0
    for index, chunk in enumerate(pieces):
        for block in chunk.blocks:
            if not block.text:
                continue
            hit = _tm_get(session, block.src_hash, source_lang, job.target_lang)
            if hit is None:
                continue
            _upsert_translation(session, job.id, block.idx, hit, "tm", None)
        segment = Segment(
            job_id=job.id,
            idx=index,
            block_start=min(block.idx for block in chunk.blocks),
            block_end=max(block.idx for block in chunk.blocks),
            model=spec.alias,
            status="pending",
        )
        session.add(segment)
        session.flush()
        if _segment_fully_translated(session, job.id, chunk.blocks):
            segment.status = "done"
            done += 1
    job.segment_budget_tokens = budget
    job.total_segments = len(pieces)
    job.done_segments = done
    job.status = "translating" if pieces else "reviewing"
    job.locked_by = None
    _publish_job(session, job)
    if not pieces:
        _fail(session, job, "empty_document", "Nothing translatable was found in this document.")
        return
    _event(session, job.id, "translating", {"segments": job.total_segments, "budget": budget, "already_done": done})


def _claim_segment(session: Session, worker_id: str) -> bool:
    settings = get_settings()
    if not _try_acquire(800):
        return False
    row = session.execute(
        text(
            """
            UPDATE segments
            SET status = 'running', locked_by = :worker, locked_at = now(), attempt = attempt + 1
            WHERE id = (
                SELECT s.id
                FROM segments s
                JOIN translation_jobs j ON j.id = s.job_id
                WHERE s.status = 'pending' AND j.status = 'translating'
                  AND (
                    SELECT count(*) FROM segments r
                    WHERE r.job_id = s.job_id AND r.status = 'running'
                  ) < :job_max
                ORDER BY (
                    SELECT count(*) FROM segments r
                    WHERE r.job_id = s.job_id AND r.status = 'running'
                ),
                (
                    SELECT count(*) FROM segments p
                    WHERE p.job_id = s.job_id AND p.status = 'pending'
                ),
                s.idx
                FOR UPDATE OF s SKIP LOCKED
                LIMIT 1
            )
            RETURNING id
            """
        ),
        {"worker": worker_id, "job_max": settings.job_max_concurrency},
    ).first()
    if not row:
        _release()
        return False
    session.commit()
    try:
        _translate_segment(session, row.id, worker_id)
        session.commit()
    except InjectedCrash:
        session.rollback()
        with session_factory()() as recovery:
            segment = recovery.get(Segment, row.id)
            if segment and segment.status == "running":
                segment.status = "pending"
                segment.locked_by = None
                recovery.commit()
        raise
    finally:
        _release()
    return True


def _translate_segment(session: Session, segment_id: uuid.UUID, worker_id: str) -> None:
    segment = session.get(Segment, segment_id)
    job = session.get(TranslationJob, segment.job_id)
    if job.status != "translating":
        segment.status = "pending"
        segment.locked_by = None
        return
    logger = log.bind(job_id=str(job.id), stage="translate", segment_id=str(segment.id), worker_id=worker_id)
    document = session.get(Document, job.document_id)
    rows = session.scalars(
        select(Block).where(
            Block.document_id == document.id,
            Block.idx >= segment.block_start,
            Block.idx <= segment.block_end,
            Block.kind != "image",
        ).order_by(Block.idx)
    ).all()
    pending = []
    seen_hash: set[str] = set()
    for row in rows:
        if not row.text:
            continue
        existing = session.get(BlockTranslation, (job.id, row.idx))
        if existing:
            continue
        if row.src_hash in seen_hash:
            continue
        seen_hash.add(row.src_hash)
        pending.append(row)
    if not pending:
        _copy_duplicate_hashes(session, job, rows)
        segment.status = "done"
        segment.locked_by = None
        _add_progress(session, job, segments=1)
        logger.info("segment_skipped_tm")
        return
    spec = resolve_model(segment.model)
    pairs = [(offset + 1, row.text) for offset, row in enumerate(pending)]
    request = TranslateRequest(
        stage="translate",
        model=spec.alias,
        system=system_prompt(job.source_lang or "auto", job.target_lang, job.glossary, job.summary),
        user=user_prompt(pairs),
        source_lang=job.source_lang or "",
        target_lang=job.target_lang,
        prompt_version=PROMPT_VERSION,
        attempt=segment.attempt,
        segment_index=segment.idx,
        job_id=str(job.id),
        segment_id=str(segment.id),
        json_mode=True,
    )
    started = time.perf_counter()
    try:
        with span("translate_segment", job_id=str(job.id), segment=segment.idx):
            result = _call_with_retries(session, request)
    except InjectedCrash:
        raise
    except AppError as exc:
        logger.info("segment_failed", error_code=exc.code)
        exhausted = request.attempt >= get_settings().segment_attempt_limit or not exc.retryable
        if exhausted:
            segment.status = "failed"
            _fail(session, job, exc.code, exc.message)
        else:
            segment.status = "pending"
            segment.locked_by = None
        return
    found, missing, extra = parse_numbered_blocks(result.text, [number for number, _ in pairs])
    if missing and segment.attempt < get_settings().segment_attempt_limit:
        _add_progress(
            session,
            job,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost=cost_cents(spec, result.tokens_in, result.tokens_out),
        )
        segment.status = "pending"
        segment.locked_by = None
        logger.info("segment_incomplete", missing=missing)
        _check_cap(session, job)
        return
    by_hash: dict[str, tuple[str, bool]] = {}
    for number, row in zip([n for n, _ in pairs], pending):
        translated = usable_translation(found.get(number), row.text or "", job.target_lang)
        remembered = translated is not None
        if translated is None:
            translated = row.text or ""
        by_hash[row.src_hash] = (translated, remembered)
        _upsert_translation(session, job.id, row.idx, translated, "model", segment.id)
        if remembered:
            _tm_put(session, row.src_hash, job.source_lang or "en", job.target_lang, translated, spec.alias)
    for row in rows:
        if row.src_hash in by_hash and session.get(BlockTranslation, (job.id, row.idx)) is None:
            translated, _remembered = by_hash[row.src_hash]
            _upsert_translation(session, job.id, row.idx, translated, "model", segment.id)
    segment.status = "done"
    segment.locked_by = None
    segment.tokens_in = result.tokens_in
    segment.tokens_out = result.tokens_out
    _add_progress(
        session,
        job,
        segments=1,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        cost=cost_cents(spec, result.tokens_in, result.tokens_out),
    )
    SEGMENT_LATENCY.labels(stage="translate").observe(time.perf_counter() - started)
    logger.info("segment_done", segment=segment.idx, tokens_in=result.tokens_in, tokens_out=result.tokens_out)
    _check_cap(session, job)


def _claim_review(session: Session, worker_id: str) -> bool:
    row = session.execute(
        text(
            """
            UPDATE translation_jobs
            SET status = 'reviewing', locked_by = :worker, heartbeat_at = now()
            WHERE id = (
                SELECT j.id FROM translation_jobs j
                WHERE j.status = 'translating'
                  AND j.total_segments > 0
                  AND NOT EXISTS (
                    SELECT 1 FROM segments s
                    WHERE s.job_id = j.id AND s.status IN ('pending', 'running')
                  )
                ORDER BY j.created_at
                FOR UPDATE OF j SKIP LOCKED
                LIMIT 1
            )
            RETURNING id
            """
        ),
        {"worker": worker_id},
    ).first()
    if not row:
        return False
    session.commit()
    _review_job(session, row.id, worker_id)
    session.commit()
    return True


def _review_job(session: Session, job_id: uuid.UUID, worker_id: str) -> None:
    from app.core.agents.review_agent import review_job

    job = session.get(TranslationJob, job_id)
    logger = log.bind(job_id=str(job_id), stage="review", worker_id=worker_id)
    with span("review_segment", job_id=str(job_id)):
        warning = review_job(session, job, _provider())
    if warning:
        job.quality_warning = True
        _event(session, job.id, "quality_warning", {"detail": "One or more segments were retranslated after a quality check."})
    job.status = "rendering"
    job.locked_by = None
    _publish_job(session, job)
    logger.info("review_done", quality_warning=bool(warning))


def _claim_render(session: Session, worker_id: str) -> bool:
    row = session.execute(
        text(
            """
            UPDATE translation_jobs
            SET locked_by = :worker, heartbeat_at = now()
            WHERE id = (
                SELECT id FROM translation_jobs
                WHERE status = 'rendering' AND (locked_by IS NULL)
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED LIMIT 1
            )
            RETURNING id
            """
        ),
        {"worker": worker_id},
    ).first()
    if not row:
        return False
    session.commit()
    job = session.get(TranslationJob, row.id)
    try:
        with span("render", job_id=str(job.id)):
            _render(session, job)
        job.status = "done"
        job.output_stale = False
        job.locked_by = None
        _event(session, job.id, "done", {"output": job.output_path, "cost_cents": float(job.cost_cents or 0)})
        _publish_job(session, job)
        log.bind(job_id=str(job.id), stage="render", worker_id=worker_id).info("render_done")
    except AppError as exc:
        _fail(session, job, exc.code, exc.message)
    except Exception as exc:
        _fail(session, job, "render_failed", str(exc))
    session.commit()
    return True


def _render(session: Session, job: TranslationJob) -> None:
    document = session.get(Document, job.document_id)
    settings = get_settings()
    rows = session.scalars(select(Block).where(Block.document_id == document.id).order_by(Block.page, Block.seq)).all()
    translations = {
        item.block_idx: item.tgt_text
        for item in session.scalars(select(BlockTranslation).where(BlockTranslation.job_id == job.id))
    }
    from app.core.domain import ExtractedDocument, PageInfo

    extracted = ExtractedDocument(
        format=document.format,
        page_count=document.page_count or 1,
        pages=[PageInfo(page=1, page_class="text", route="text")],
        blocks=[_from_row(row) for row in rows],
    )
    out = Path(settings.files_dir) / "outputs" / f"{job.id}{_output_ext(document.format)}"
    report = adapter_for(document.format if document.format != "jpeg" else "jpeg").render(
        Path(document.storage_path), extracted, translations, out, job.render_mode
    )
    if document.format == "png":
        report = adapter_for("png").render(Path(document.storage_path), extracted, translations, out, job.render_mode)
    job.output_path = str(out)
    job.output_stale = False
    if report.get("overflow_pages"):
        _event(session, job.id, "layout_overflow", {"pages": report["overflow_pages"]})


def _output_ext(fmt: str) -> str:
    if fmt == "docx":
        return ".docx"
    return ".pdf"


def _call_with_retries(session: Session, request: TranslateRequest):
    settings = get_settings()
    delay = 0.05
    while True:
        try:
            result = _provider().translate(request)
        except InjectedCrash:
            _record(
                stage=request.stage,
                model=request.model,
                tokens_in=0,
                tokens_out=0,
                tokens_cached=0,
                latency_ms=0,
                status="error",
                error_code="injected_crash",
                attempt=request.attempt,
                job_id=_uuid(request.job_id),
                segment_id=_uuid(request.segment_id),
                page_no=request.page_no,
            )
            raise
        except RateLimited as exc:
            RATE_LIMITS.inc()
            PROVIDER_ERRORS.labels(code="rate_limited").inc()
            _note_429()
            _record(stage=request.stage, model=request.model, tokens_in=0, tokens_out=0, tokens_cached=0, latency_ms=0, status="error", error_code="rate_limited", attempt=request.attempt, job_id=_uuid(request.job_id), segment_id=_uuid(request.segment_id), page_no=request.page_no)
            if request.attempt >= settings.segment_attempt_limit:
                raise
            time.sleep(exc.retry_after or delay)
            request.attempt += 1
            delay = min(delay * 2, 2)
            continue
        except AppError as exc:
            PROVIDER_ERRORS.labels(code=exc.code).inc()
            _record(stage=request.stage, model=request.model, tokens_in=0, tokens_out=0, tokens_cached=0, latency_ms=0, status="error", error_code=exc.code, attempt=request.attempt, job_id=_uuid(request.job_id), segment_id=_uuid(request.segment_id), page_no=request.page_no)
            if request.attempt >= settings.segment_attempt_limit or not exc.retryable:
                raise
            time.sleep(delay)
            request.attempt += 1
            delay = min(delay * 2, 2)
            continue
        _record(
            stage=request.stage,
            model=result.model or request.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            tokens_cached=result.tokens_cached,
            latency_ms=result.latency_ms,
            status="ok",
            error_code=None,
            attempt=request.attempt,
            job_id=_uuid(request.job_id),
            segment_id=_uuid(request.segment_id),
            page_no=request.page_no,
        )
        return result


def _ensure_summary(session: Session, job: TranslationJob, document: Document) -> None:
    if job.summary:
        return
    rows = session.scalars(select(Block.text).where(Block.document_id == document.id, Block.text.is_not(None)).limit(8)).all()
    excerpt = "\n".join(text for text in rows if text)[:4000]
    if not excerpt:
        job.summary = ""
        return
    request = TranslateRequest(
        stage="summary",
        model=get_settings().default_model,
        system=SUMMARY_PROMPT,
        user=excerpt,
        source_lang=job.source_lang or "",
        target_lang=job.target_lang,
        prompt_version=PROMPT_VERSION,
        job_id=str(job.id),
    )
    if not _try_acquire(500):
        job.summary = excerpt[:400]
        return
    try:
        result = _call_with_retries(session, request)
        job.summary = result.text[:1200]
        _add_cost(session, job, request.model, result.tokens_in, result.tokens_out)
    except AppError:
        job.summary = excerpt[:400]
    finally:
        _release()


def _try_acquire(estimate: int) -> bool:
    factory = session_factory()
    with factory() as gate:
        row = gate.execute(
            text(
                """
                UPDATE provider_gate
                SET inflight = inflight + 1,
                    max_inflight = GREATEST(max_inflight, inflight + 1),
                    window_tokens = CASE
                        WHEN window_start IS NULL OR window_start < now() - interval '60 seconds' THEN :est
                        ELSE window_tokens + :est
                    END,
                    window_start = CASE
                        WHEN window_start IS NULL OR window_start < now() - interval '60 seconds' THEN now()
                        ELSE window_start
                    END
                WHERE id = 1
                  AND inflight < effective_max
                  AND (
                    CASE
                      WHEN window_start IS NULL OR window_start < now() - interval '60 seconds' THEN :est
                      ELSE window_tokens + :est
                    END
                  ) <= tpm_budget
                RETURNING inflight
                """
            ),
            {"est": estimate},
        ).first()
        gate.commit()
        if row:
            INFLIGHT.set(row.inflight)
            return True
        return False


def _release() -> None:
    factory = session_factory()
    with factory() as gate:
        row = gate.execute(
            text("UPDATE provider_gate SET inflight = GREATEST(inflight - 1, 0) WHERE id = 1 RETURNING inflight")
        ).first()
        gate.commit()
        if row:
            INFLIGHT.set(row.inflight)


def _note_429() -> None:
    factory = session_factory()
    with factory() as gate:
        gate.execute(
            text(
                """
                UPDATE provider_gate
                SET effective_max = GREATEST(1, effective_max / 2),
                    last_429_at = now()
                WHERE id = 1
                """
            )
        )
        gate.commit()


def _record(**kwargs) -> None:
    spec = None
    try:
        spec = resolve_model(kwargs.get("model"))
    except AppError:
        spec = None
    cents = cost_cents(spec, kwargs.get("tokens_in") or 0, kwargs.get("tokens_out") or 0) if spec and kwargs.get("status") == "ok" else Decimal(0)
    factory = session_factory()
    with factory() as session:
        session.add(
            ProviderCall(
                job_id=kwargs.get("job_id"),
                segment_id=kwargs.get("segment_id"),
                page_no=kwargs.get("page_no"),
                stage=kwargs["stage"],
                model=kwargs.get("model") or "unknown",
                prompt_version=PROMPT_VERSION,
                attempt=kwargs.get("attempt") or 1,
                tokens_in=kwargs.get("tokens_in") or 0,
                tokens_out=kwargs.get("tokens_out") or 0,
                tokens_cached=kwargs.get("tokens_cached") or 0,
                cost_cents=cents,
                latency_ms=kwargs.get("latency_ms") or 0,
                status=kwargs["status"],
                error_code=kwargs.get("error_code"),
            )
        )
        session.commit()
    stage = kwargs["stage"]
    model = kwargs.get("model") or "unknown"
    if kwargs.get("status") == "ok":
        TOKENS.labels(stage=stage, model=model, direction="in").inc(kwargs.get("tokens_in") or 0)
        TOKENS.labels(stage=stage, model=model, direction="out").inc(kwargs.get("tokens_out") or 0)
        COST.labels(stage=stage, model=model).inc(float(cents))


def _add_progress(
    session: Session,
    job: TranslationJob,
    *,
    segments: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost: Decimal | None = None,
) -> None:
    cents = cost if cost is not None else Decimal(0)
    if not segments and not tokens_in and not tokens_out and cents == 0:
        return
    result = session.execute(
        text(
            """
            UPDATE translation_jobs
            SET done_segments = COALESCE(done_segments, 0) + :segments,
                tokens_in = COALESCE(tokens_in, 0) + :tokens_in,
                tokens_out = COALESCE(tokens_out, 0) + :tokens_out,
                cost_cents = COALESCE(cost_cents, 0) + :cost
            WHERE id = :id
            RETURNING id, status, done_segments, total_segments, cost_cents, error_code
            """
        ),
        {"segments": segments, "tokens_in": tokens_in, "tokens_out": tokens_out, "cost": cents, "id": job.id},
    ).mappings().first()
    if result is not None:
        state = dict(result)
        after_commit(session, lambda state=state: publish_job(state))
    session.expire(job, ["done_segments", "tokens_in", "tokens_out", "cost_cents"])


def _add_cost(session: Session, job: TranslationJob, model: str, tokens_in: int, tokens_out: int) -> None:
    try:
        spec = resolve_model(model)
    except AppError:
        return
    _add_progress(session, job, cost=cost_cents(spec, tokens_in, tokens_out))


def _check_cap(session: Session, job: TranslationJob) -> None:
    cap = Decimal(str(get_settings().max_job_cost_cents))
    session.refresh(job, attribute_names=["cost_cents"])
    if (job.cost_cents or 0) > cap:
        raise_cost = CostCapExceeded(str(job.cost_cents), str(cap))
        _fail(session, job, raise_cost.code, raise_cost.message)


def _publish_job(session: Session, job: TranslationJob) -> None:
    state = {
        "id": job.id,
        "status": job.status,
        "done_segments": job.done_segments or 0,
        "total_segments": job.total_segments or 0,
        "cost_cents": job.cost_cents or 0,
        "error_code": job.error_code,
    }
    after_commit(session, lambda state=state: publish_job(state))


def _publish_document(session: Session, document: Document) -> None:
    state = {
        "id": document.id,
        "analysis_status": document.analysis_status,
        "error_code": document.error_code,
    }
    after_commit(session, lambda state=state: publish_document(state))


def _fail(session: Session, job: TranslationJob, code: str, message: str) -> None:
    job.status = "failed"
    job.error_code = code
    job.error_detail = message
    job.locked_by = None
    _event(session, job.id, "failed", {"code": code, "message": message})
    _publish_job(session, job)


def _event(session: Session, job_id: uuid.UUID, event: str, payload: dict, level: str = "info") -> None:
    session.add(JobEvent(job_id=job_id, level=level, event=event, payload=payload))


def _block_row(document_id: uuid.UUID, block: BlockData) -> Block:
    return Block(
        document_id=document_id,
        idx=block.idx,
        page=block.page,
        seq=block.seq,
        kind=block.kind,
        text=block.text,
        src_hash=block.src_hash,
        asset_path=block.asset_path,
        meta=block.meta or {},
    )


def _from_row(row: Block) -> BlockData:
    return BlockData(
        page=row.page,
        seq=row.seq,
        kind=row.kind,
        text=row.text,
        src_hash=row.src_hash,
        asset_path=row.asset_path,
        meta=dict(row.meta or {}),
        idx=row.idx,
    )


def _upsert_translation(session, job_id, block_idx, text, source, segment_id) -> None:
    stmt = pg_insert(BlockTranslation).values(
        job_id=job_id, block_idx=block_idx, tgt_text=text, source=source, segment_id=segment_id
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["job_id", "block_idx"],
        set_={"tgt_text": text, "source": source, "segment_id": segment_id},
    )
    session.execute(stmt)


def _tm_get(session: Session, src_hash: str, source: str, target: str) -> str | None:
    row = session.get(TranslationMemory, (src_hash, source, target))
    if row is None:
        return None
    row.hits = (row.hits or 0) + 1
    return row.tgt_text


def _tm_put(session, src_hash, source, target, text, model) -> None:
    stmt = pg_insert(TranslationMemory).values(
        src_hash=src_hash, source_lang=source, target_lang=target, tgt_text=text, model=model, hits=1
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["src_hash", "source_lang", "target_lang"],
        set_={"hits": TranslationMemory.hits + 1},
    )
    session.execute(stmt)


def _segment_fully_translated(session, job_id, blocks) -> bool:
    for block in blocks:
        if block.kind == "image" or not block.text:
            continue
        if session.get(BlockTranslation, (job_id, block.idx)) is None:
            return False
    return True


def _copy_duplicate_hashes(session, job, rows) -> None:
    for row in rows:
        if not row.text or session.get(BlockTranslation, (job.id, row.idx)):
            continue
        hit = _tm_get(session, row.src_hash, job.source_lang or "en", job.target_lang)
        if hit:
            _upsert_translation(session, job.id, row.idx, hit, "tm", None)


def _page_png(document: Document, page_no: int) -> bytes | None:
    stem = Path(document.storage_path).stem
    full = Path(get_settings().files_dir) / "thumbs" / stem / f"{page_no}.full.png"
    if full.exists():
        return full.read_bytes()
    if document.format == "pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(document.storage_path)
        try:
            if page_no - 1 >= len(pdf):
                return None
            image = pdf[page_no - 1].render(scale=2).to_pil()
        finally:
            pdf.close()
        from app.core.analysis.page_classifier import png_bytes

        return png_bytes(image)
    if document.format in {"png", "jpeg"} and page_no == 1:
        return Path(document.storage_path).read_bytes()
    return None


def _empty_message(pages: list[int]) -> str:
    if not pages:
        return "This document has no extractable text."
    return f"No readable text was found on pages {', '.join(str(page) for page in pages)}."


def _now(session: Session):
    return session.execute(text("SELECT now()")).scalar()


def _uuid(value: str | None):
    if not value:
        return None
    return uuid.UUID(value)


def debug_provider_calls(session: Session) -> dict:
    if get_settings().provider != "fake":
        raise AppError("not_available", "Provider call debug is only enabled when PROVIDER=fake.", "", False, 404)
    rows = session.execute(text("SELECT count(*) AS calls, count(*) FILTER (WHERE status = 'ok') AS ok FROM provider_calls")).one()
    gate = session.execute(text("SELECT inflight, max_inflight, effective_max FROM provider_gate WHERE id = 1")).one()
    return {
        "calls": rows.calls,
        "ok": rows.ok,
        "inflight": gate.inflight,
        "max_inflight": gate.max_inflight,
        "effective_max": gate.effective_max,
    }


def cost_for_job(session: Session, job_id: uuid.UUID) -> dict:
    rows = session.execute(
        text(
            """
            SELECT stage, model,
                   COALESCE(sum(tokens_in), 0) AS tokens_in,
                   COALESCE(sum(tokens_out), 0) AS tokens_out,
                   COALESCE(sum(tokens_cached), 0) AS tokens_cached,
                   COALESCE(sum(cost_cents), 0) AS cost_cents,
                   COALESCE(sum(latency_ms), 0) AS latency_ms,
                   count(*) AS calls
            FROM provider_calls
            WHERE job_id = :job
            GROUP BY stage, model
            ORDER BY stage
            """
        ),
        {"job": job_id},
    ).mappings()
    stages = [dict(row) for row in rows]
    for row in stages:
        row["cost_cents"] = float(row["cost_cents"])
    return {
        "job_id": str(job_id),
        "stages": stages,
        "cost_cents": sum(row["cost_cents"] for row in stages),
        "tokens_in": sum(row["tokens_in"] for row in stages),
        "tokens_out": sum(row["tokens_out"] for row in stages),
    }


def cost_stats(session: Session, since_hours: int = 24) -> list[dict]:
    rows = session.execute(
        text(
            """
            SELECT stage, model, prompt_version,
                   COALESCE(sum(tokens_in), 0) AS tokens_in,
                   COALESCE(sum(tokens_out), 0) AS tokens_out,
                   COALESCE(sum(cost_cents), 0) AS cost_cents,
                   count(*) AS calls
            FROM provider_calls
            WHERE created_at > now() - make_interval(hours => :hours)
            GROUP BY stage, model, prompt_version
            ORDER BY cost_cents DESC
            """
        ),
        {"hours": since_hours},
    ).mappings()
    result = []
    for row in rows:
        item = dict(row)
        item["cost_cents"] = float(item["cost_cents"])
        result.append(item)
    return result
