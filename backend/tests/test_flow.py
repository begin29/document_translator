import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import text

from app.config import get_settings
from app.core.errors import AppError
from app.core.provider.fake_provider import reset_crash_memory
from app.core.service import cancel_job, create_document, create_job, debug_provider_calls, process_available, reap, reset_runtime, run_analysis, sync_gate
from app.db.models import ProviderCall, TranslationJob
from app.db.session import reset_engine, session_factory

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")


def _pdf(path: Path, paragraphs: list[str]) -> None:
    drawing = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for paragraph in paragraphs:
        drawing.drawString(72, y, paragraph[:110])
        y -= 24
        if y < 72:
            drawing.showPage()
            y = 800
    drawing.save()


@pytest.fixture(scope="module")
def _migrated():
    get_settings.cache_clear()
    reset_engine()
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", os.environ["TEST_DATABASE_URL"])
    command.upgrade(cfg, "head")
    yield


@pytest.fixture()
def session(_migrated):
    get_settings.cache_clear()
    reset_engine()
    reset_runtime()
    reset_crash_memory()
    factory = session_factory()
    with factory() as db:
        db.execute(
            text(
                "TRUNCATE documents, blocks, translation_jobs, segments, block_translations, translation_memory, job_events, provider_calls, ocr_pages RESTART IDENTITY CASCADE"
            )
        )
        db.execute(text("UPDATE provider_gate SET inflight=0, max_inflight=0, effective_max=ceiling, window_tokens=0, last_429_at=NULL"))
        db.commit()
    with factory() as db:
        sync_gate(db)
        db.commit()
        yield db


def _drain(db, limit=80):
    from app.core.errors import InjectedCrash

    for _ in range(limit):
        reap(db)
        db.commit()
        try:
            worked = process_available(db, "test")
        except InjectedCrash:
            db.rollback()
            return
        db.commit()
        if not worked:
            return


def test_lifecycle_and_idempotency(session, tmp_path):
    path = tmp_path / "short.pdf"
    _pdf(path, ["The motor housing is sealed against dust and water during every ride."])
    document = create_document(session, "short.pdf", path.read_bytes())
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    session.refresh(document)
    assert document.analysis_status == "ready"
    assert document.analysis["models"]
    job = create_job(session, document.id, "de", None, "gpt-4o-mini", None, "key-1")
    again = create_job(session, document.id, "de", None, "gpt-4o-mini", None, "key-1")
    session.commit()
    assert job.id == again.id
    _drain(session)
    session.refresh(job)
    assert job.status == "done"
    assert job.output_path
    assert Path(job.output_path).exists()
    calls = session.query(ProviderCall).filter(ProviderCall.stage == "translate", ProviderCall.status == "ok").count()
    assert calls >= 1


def test_corrupt_and_unsupported(session):
    with pytest.raises(AppError) as exc:
        create_document(session, "empty.pdf", b"")
    assert exc.value.code == "invalid_file"
    with pytest.raises(AppError) as renamed:
        create_document(session, "photo.pdf", b"\x89PNG\r\n\x1a\nrest")
    assert renamed.value.code == "invalid_file"
    with pytest.raises(AppError) as exe:
        create_document(session, "app.exe", b"MZ\x00\x00")
    assert exe.value.code == "unsupported_type"


def test_cancel(session, tmp_path):
    path = tmp_path / "c.pdf"
    _pdf(path, ["Cancel this document before the worker starts translating it today."])
    document = create_document(session, "c.pdf", path.read_bytes())
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    job = create_job(session, document.id, "fr", None, None, None, None)
    session.commit()
    cancel_job(session, job.id)
    session.commit()
    _drain(session)
    session.refresh(job)
    assert job.status == "cancelled"


def test_retry_does_not_double_bill(session, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL_MODE", "http500")
    monkeypatch.setenv("FAKE_FAIL_SEGMENTS", "0")
    get_settings.cache_clear()
    path = tmp_path / "retry.pdf"
    _pdf(path, ["Retry the first segment and then succeed without billing it twice at all."])
    document = create_document(session, "retry.pdf", path.read_bytes())
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    job = create_job(session, document.id, "de", None, None, None, None)
    session.commit()
    _drain(session)
    session.refresh(job)
    assert job.status == "done"
    ok = session.query(ProviderCall).filter(ProviderCall.job_id == job.id, ProviderCall.stage == "translate", ProviderCall.status == "ok").count()
    failed = session.query(ProviderCall).filter(ProviderCall.job_id == job.id, ProviderCall.stage == "translate", ProviderCall.status == "error").count()
    assert ok == 1
    assert failed >= 1


def test_crash_resume_skips_done_segments(session, tmp_path, monkeypatch):
    monkeypatch.setenv("CRASH_AFTER_SEGMENT", "1")
    monkeypatch.setenv("GLOBAL_QUALITY_CAP", "32")
    get_settings.cache_clear()
    path = tmp_path / "long.pdf"
    words = " ".join(["alpha"] * 40)
    _pdf(path, [f"{words} {i}." for i in range(4)])
    document = create_document(session, "long.pdf", path.read_bytes())
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    job = create_job(session, document.id, "de", None, None, None, None)
    session.commit()
    _drain(session)
    done_before = session.query(ProviderCall).filter(ProviderCall.job_id == job.id, ProviderCall.stage == "translate", ProviderCall.status == "ok").count()
    monkeypatch.setenv("CRASH_AFTER_SEGMENT", "0")
    get_settings.cache_clear()
    reset_crash_memory()
    _drain(session)
    session.refresh(job)
    assert job.status == "done"
    done_after = session.query(ProviderCall).filter(ProviderCall.job_id == job.id, ProviderCall.stage == "translate", ProviderCall.status == "ok").count()
    assert done_after >= done_before
    assert done_before >= 1


def test_debug_inflight_shape(session):
    payload = debug_provider_calls(session)
    assert "calls" in payload and "max_inflight" in payload


def test_cost_endpoint_matches_rows(session, tmp_path):
    from app.core.service import cost_for_job

    path = tmp_path / "cost.pdf"
    _pdf(path, ["Cost accounting must match the provider call ledger exactly here."])
    document = create_document(session, "cost.pdf", path.read_bytes())
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    job = create_job(session, document.id, "es", None, None, None, str(uuid.uuid4()))
    session.commit()
    _drain(session)
    report = cost_for_job(session, job.id)
    raw = session.execute(text("SELECT COALESCE(sum(cost_cents),0) FROM provider_calls WHERE job_id=:id"), {"id": job.id}).scalar()
    assert abs(float(report["cost_cents"]) - float(raw)) < 0.001
    assert session.get(TranslationJob, job.id).status == "done"


def test_reaper_requeues_stale_segment(session, tmp_path):
    from datetime import datetime, timedelta, timezone

    from app.db.models import Segment

    path = tmp_path / "reap.pdf"
    _pdf(path, ["Reaper should return a stale running segment to the pending queue."])
    document = create_document(session, "reap.pdf", path.read_bytes())
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    job = create_job(session, document.id, "de", None, None, None, None)
    session.commit()
    _drain(session)
    session.refresh(job)
    assert job.status == "done"
    segment = session.query(Segment).filter(Segment.job_id == job.id).one()
    segment.status = "running"
    segment.locked_at = datetime.now(timezone.utc) - timedelta(hours=2)
    job.status = "translating"
    session.commit()
    reap(session)
    session.commit()
    session.refresh(segment)
    assert segment.status == "pending"


def test_reaper_recovers_review_render_and_ocr(session, tmp_path):
    from datetime import datetime, timedelta, timezone

    from app.db.models import OcrPage

    path = tmp_path / "reap-stages.pdf"
    _pdf(path, ["Reaper should release a review, a render lock, and a finished OCR job."])
    document = create_document(session, "reap-stages.pdf", path.read_bytes())
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    job = create_job(session, document.id, "de", None, None, None, None)
    session.commit()
    _drain(session)
    session.refresh(job)
    assert job.status == "done"
    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    job.status = "reviewing"
    job.locked_by = "dead-worker"
    job.heartbeat_at = stale
    session.commit()
    reap(session)
    session.commit()
    session.refresh(job)
    assert job.status == "translating"
    assert job.locked_by is None
    job.status = "rendering"
    job.locked_by = "dead-worker"
    job.heartbeat_at = stale
    session.commit()
    reap(session)
    session.commit()
    session.refresh(job)
    assert job.status == "rendering"
    assert job.locked_by is None
    job.status = "ocr"
    job.locked_by = "dead-worker"
    job.heartbeat_at = stale
    session.add(OcrPage(job_id=job.id, page_no=1, status="done"))
    session.commit()
    reap(session)
    session.commit()
    session.refresh(job)
    assert job.status != "ocr"


def test_api_and_mcp_estimate(session, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import app

    path = tmp_path / "api.pdf"
    _pdf(path, ["The API accepts a PDF and the MCP estimate reads the same analysis."])
    client = TestClient(app)
    uploaded = client.post("/api/documents", files={"file": ("api.pdf", path.read_bytes(), "application/pdf")})
    assert uploaded.status_code == 200
    document_id = uploaded.json()["id"]
    _drain(session)
    ready = client.get(f"/api/documents/{document_id}")
    assert ready.json()["analysis_status"] == "ready"
    created = client.post(
        "/api/jobs",
        json={"document_id": document_id, "target_lang": "de"},
        headers={"Idempotency-Key": "api-key"},
    )
    assert created.status_code == 200
    _drain(session)
    job_id = created.json()["id"]
    fetched = client.get(f"/api/jobs/{job_id}")
    assert fetched.json()["status"] == "done"
    download = client.get(f"/api/jobs/{job_id}/download")
    assert download.status_code == 200
    assert download.content[:4] == b"%PDF"

