import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import text

from app.config import get_settings
from app.core.agents.document_guardrail import ensure_is_document
from app.core.agents.review_agent import ReviewToolkit, review_job
from app.core.domain import PageInfo
from app.core.errors import AppError
from app.core.model_registry import cost_cents, resolve_model
from app.core.provider.fake_provider import reset_crash_memory
from app.core.service import create_document, create_job, process_available, reap, reset_runtime, run_analysis, sync_gate
from app.db.models import Block, BlockTranslation, JobEvent, ProviderCall
from app.db.session import reset_engine, session_factory

needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")


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
                "TRUNCATE users, documents, blocks, translation_jobs, segments, block_translations, translation_memory, job_events, provider_calls, ocr_pages RESTART IDENTITY CASCADE"
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


def _page(number: int, page_class: str) -> PageInfo:
    return PageInfo(page=number, page_class=page_class, route="skip")


def _flagged_job(session, tmp_path, name: str, text: str | None = None, owner_id=None):
    body = text or "The motor housing is sealed against dust and water during every ride."
    path = tmp_path / f"{name}.pdf"
    _pdf(path, ["The motor housing is sealed against dust and water during every ride."])
    document = create_document(session, path.read_bytes(), f"{name}.pdf", owner_id)
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    session.refresh(document)
    assert document.analysis_status == "ready"
    job = create_job(session, document.id, "de", None, "gpt-4o-mini", None, f"key-{name}")
    session.commit()
    _drain(session)
    session.refresh(job)
    rows = session.query(Block).filter(Block.document_id == document.id, Block.kind != "image").all()
    for row in rows:
        if not row.text:
            continue
        row.text = body
        translation = session.get(BlockTranslation, (job.id, row.idx))
        assert translation is not None
        translation.tgt_text = body
    session.commit()
    return job


def _usage_result():
    usage = SimpleNamespace(input_tokens=1000, output_tokens=200, input_tokens_details=SimpleNamespace(cached_tokens=0))
    return SimpleNamespace(raw_responses=[SimpleNamespace(usage=usage)])


@needs_db
def test_agent_usage_recorded(session, tmp_path, monkeypatch, make_user):
    body = ("Alpha beta gamma delta. " * 80) + "ENDMARKER"
    assert len(body) > 1500
    job = _flagged_job(session, tmp_path, "usage", body, make_user().id)
    held = {}
    original_init = ReviewToolkit.__init__

    def init(self, db, current, provider):
        original_init(self, db, current, provider)
        held["toolkit"] = self
        held["provider"] = provider

    def stub(_agent, prompt, **kwargs):
        indexes = [int(number) for number in re.findall(r"(?m)^segment (\d+)$", prompt)]
        assert indexes
        assert kwargs["max_turns"] == 4 + 2 * len(indexes)
        assert body[:1500] in prompt
        assert "ENDMARKER" not in prompt
        assert "untranslated" in prompt
        assert _agent.model_settings.temperature == 0
        for index in indexes:
            held["toolkit"].accept_segment(index)
        return _usage_result()

    monkeypatch.setattr("app.core.agents.review_agent._agent_enabled", lambda: True)
    monkeypatch.setattr(ReviewToolkit, "__init__", init)
    monkeypatch.setattr("agents.Runner.run_sync", stub)
    provider = object()
    session.refresh(job)
    before = job.cost_cents or 0
    review_job(session, job, provider)
    session.expire_all()
    session.refresh(job)
    expected = cost_cents(resolve_model("gpt-4o"), 1000, 200)
    assert job.cost_cents - before == expected
    calls = session.query(ProviderCall).filter(ProviderCall.job_id == job.id, ProviderCall.stage == "review_agent").all()
    assert len(calls) == 1
    assert calls[0].tokens_in == 1000
    assert calls[0].tokens_out == 200
    assert calls[0].tokens_cached == 0
    assert calls[0].model == "gpt-4o"
    assert calls[0].status == "ok"
    assert held["provider"] is provider


@needs_db
def test_agent_failure_logged_and_falls_back(session, tmp_path, monkeypatch, make_user):
    job = _flagged_job(session, tmp_path, "fallback", owner_id=make_user().id)

    def stub(*_args, **_kwargs):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr("app.core.agents.review_agent._agent_enabled", lambda: True)
    monkeypatch.setattr("agents.Runner.run_sync", stub)
    before = session.query(ProviderCall).filter(ProviderCall.job_id == job.id, ProviderCall.stage == "review").count()
    review_job(session, job, object())
    session.flush()
    events = session.query(JobEvent).filter(JobEvent.job_id == job.id, JobEvent.event == "review_agent_failed").all()
    assert len(events) == 1
    assert events[0].level == "warning"
    assert "agent exploded" in (events[0].payload or {}).get("error", "")
    review_calls = session.query(ProviderCall).filter(ProviderCall.job_id == job.id, ProviderCall.stage == "review", ProviderCall.status == "ok").count()
    assert review_calls == before + 1
    assert session.query(ProviderCall).filter(ProviderCall.job_id == job.id, ProviderCall.stage == "review_agent").count() == 0


def test_not_a_document_plain_check():
    with pytest.raises(AppError) as exc:
        ensure_is_document([_page(1, "non_document"), _page(2, "non_document")])
    assert exc.value.code == "not_a_document"
    assert exc.value.detail == "pages=1,2"
    ensure_is_document([_page(1, "text")])
    ensure_is_document([_page(1, "blank")])
    ensure_is_document([_page(1, "non_document"), _page(2, "text")])
    ensure_is_document([])
