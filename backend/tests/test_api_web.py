import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from app.config import get_settings
from app.core.provider.fake_provider import reset_crash_memory
from app.core.service import reset_runtime, sync_gate
from app.db.models import Block, BlockTranslation, Document, TranslationJob
from app.db.session import reset_engine, session_factory

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")


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
                "TRUNCATE users, documents, blocks, translation_jobs, segments, block_translations, translation_memory, job_events, provider_calls, ocr_pages RESTART IDENTITY CASCADE"
            )
        )
        db.execute(text("UPDATE provider_gate SET inflight=0, max_inflight=0, effective_max=ceiling, window_tokens=0, last_429_at=NULL"))
        db.commit()
    with factory() as db:
        sync_gate(db)
        db.commit()
        yield db


def test_meta_config_reports_upload_limit(session):
    from fastapi.testclient import TestClient

    from app.main import app

    assert session.get_bind() is not None
    get_settings.cache_clear()
    response = TestClient(app).get("/api/meta/config")
    assert response.status_code == 200
    settings = get_settings()
    assert response.json() == {"max_upload_mb": settings.max_upload_mb, "allow_signup": settings.allow_signup}


def test_job_payloads_include_document_filename(session, make_user, auth_client):
    user = make_user()
    document = Document(
        owner_id=user.id,
        filename="quarterly-report.pdf",
        format="pdf",
        size_bytes=128,
        sha256="a" * 64,
        storage_path="/tmp/stark-files-B/quarterly-report.pdf",
        analysis_status="ready",
        detected_source_lang="en",
    )
    session.add(document)
    session.commit()
    client = auth_client(user)
    created = client.post(
        "/api/jobs",
        json={"document_id": str(document.id), "target_lang": "uk", "model": "gpt-4o-mini"},
        headers={"Idempotency-Key": "b-web-filename"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["document_filename"] == "quarterly-report.pdf"
    listed = client.get("/api/jobs")
    assert listed.status_code == 200
    match = next(item for item in listed.json() if item["id"] == body["id"])
    assert match["document_filename"] == "quarterly-report.pdf"
    fetched = client.get(f"/api/jobs/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["document_filename"] == "quarterly-report.pdf"


def _finished_job(session, user):
    document = Document(
        owner_id=user.id,
        filename="notes.pdf",
        format="pdf",
        size_bytes=128,
        sha256="b" * 64,
        storage_path="/tmp/stark-files-B/notes.pdf",
        analysis_status="ready",
        detected_source_lang="en",
    )
    session.add(document)
    session.flush()
    session.add_all(
        [
            Block(document_id=document.id, idx=0, page=1, seq=0, kind="paragraph", text="Hello", src_hash="c" * 64, meta={}),
            Block(document_id=document.id, idx=1, page=1, seq=1, kind="paragraph", text="World", src_hash="d" * 64, meta={}),
            Block(document_id=document.id, idx=2, page=1, seq=2, kind="image", text=None, src_hash="e" * 64, meta={}),
        ]
    )
    job = TranslationJob(
        owner_id=user.id,
        document_id=document.id,
        target_lang="uk",
        model="gpt-4o-mini",
        status="done",
        total_segments=1,
        done_segments=1,
    )
    session.add(job)
    session.flush()
    session.add_all(
        [
            BlockTranslation(job_id=job.id, block_idx=0, tgt_text="Привіт", source="model"),
            BlockTranslation(job_id=job.id, block_idx=1, tgt_text="Світ", source="model"),
        ]
    )
    session.commit()
    return job


def test_save_edits_updates_every_changed_block(session, make_user, auth_client):
    user = make_user()
    job = _finished_job(session, user)
    client = auth_client(user)
    saved = client.patch(
        f"/api/jobs/{job.id}/edits",
        json={"edits": [{"block_idx": 0, "text": "Добрий день"}, {"block_idx": 1, "text": "Земля"}]},
    )
    assert saved.status_code == 200, saved.text
    session.expire_all()
    rows = {
        row.block_idx: row
        for row in session.scalars(select(BlockTranslation).where(BlockTranslation.job_id == job.id)).all()
    }
    assert rows[0].tgt_text == "Добрий день"
    assert rows[0].source == "user"
    assert rows[1].tgt_text == "Земля"
    assert rows[1].source == "user"
    assert session.get(TranslationJob, job.id).output_stale is True

    rejected = client.patch(
        f"/api/jobs/{job.id}/edits",
        json={"edits": [{"block_idx": 0, "text": "should not stick"}, {"block_idx": 99, "text": "missing"}]},
    )
    assert rejected.status_code == 400
    assert rejected.json()["code"] == "invalid_edit"
    session.expire_all()
    assert session.get(BlockTranslation, (job.id, 0)).tgt_text == "Добрий день"

    image = client.patch(f"/api/jobs/{job.id}/edits", json={"edits": [{"block_idx": 2, "text": "caption"}]})
    assert image.status_code == 400
    assert image.json()["code"] == "invalid_edit"

    running = session.get(TranslationJob, job.id)
    running.status = "translating"
    session.commit()
    early = client.patch(f"/api/jobs/{job.id}/edits", json={"edits": [{"block_idx": 0, "text": "too soon"}]})
    assert early.status_code == 409
    assert early.json()["code"] == "not_ready"


def test_job_history_pages_ten_at_a_time(session, make_user, auth_client):
    user = make_user()
    document = Document(
        owner_id=user.id,
        filename="history.pdf",
        format="pdf",
        size_bytes=128,
        sha256="d" * 64,
        storage_path="/tmp/stark-files-B/history.pdf",
        analysis_status="ready",
        detected_source_lang="en",
    )
    session.add(document)
    session.flush()
    base = datetime(2026, 1, 1, tzinfo=UTC)
    jobs = []
    shared = base + timedelta(minutes=14)
    for index in range(25):
        created = shared if index in {14, 15} else base + timedelta(minutes=index)
        jobs.append(
            TranslationJob(
                id=uuid.UUID(int=index + 1),
                owner_id=user.id,
                document_id=document.id,
                target_lang="de",
                model="gpt-4o-mini",
                status="done",
                created_at=created,
            )
        )
    session.add_all(jobs)
    session.commit()
    ordered = sorted(jobs, key=lambda job: (job.created_at, job.id), reverse=True)
    client = auth_client(user)
    seen: list[str] = []
    cursor: dict | None = None
    pages = 0
    while True:
        params = {"limit": 10}
        if cursor is not None:
            params["before"] = cursor["created_at"]
            params["before_id"] = cursor["id"]
        response = client.get("/api/jobs", params=params)
        assert response.status_code == 200
        page = response.json()
        pages += 1
        assert pages <= 4
        if not page:
            break
        assert len(page) <= 10
        seen.extend(item["id"] for item in page)
        cursor = page[-1]
    assert pages == 4
    assert seen == [str(job.id) for job in ordered]
    assert len(seen) == 25


def test_upload_over_limit_is_json_413(session, monkeypatch, auth_client):
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    get_settings.cache_clear()
    payload = b"x" * (2 * 1024 * 1024)
    response = auth_client().post("/api/documents", files={"file": ("big.pdf", payload, "application/pdf")})
    assert response.status_code == 413
    body = response.json()
    assert body["code"] == "file_too_large"
    assert "MB limit" in body["message"]
    session.expire_all()
    assert session.scalar(select(func.count()).select_from(Document)) == 0
