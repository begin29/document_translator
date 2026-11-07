import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text

from app.config import get_settings
from app.core.provider.fake_provider import reset_crash_memory
from app.core.service import reset_runtime, sync_gate
from app.db.models import Document
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
