import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import select, text

from app.config import get_settings
from app.core.model_registry import ModelSpec, cost_cents, image_cost_cents, image_tokens, resolve_model
from app.core.provider.fake_provider import reset_crash_memory
from app.core.service import (
    create_document,
    create_job,
    estimate_for,
    process_available,
    reap,
    reset_runtime,
    run_analysis,
    sync_gate,
)
from app.core.tokenize import count_tokens
from app.db.models import Block, ProviderCall, TranslationJob, TranslationMemory
from app.db.session import reset_engine, session_factory

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")


def _pdf(path: Path, paragraphs: list[str]) -> None:
    drawing = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for paragraph in paragraphs:
        drawing.drawString(72, y, paragraph[:110])
        y -= 16
        if y < 72:
            drawing.showPage()
            y = 800
    drawing.save()


def _lines(count: int) -> list[str]:
    base = "The sealed motor housing stays quiet on the long stage through dust and rain."
    return [f"{index:04d} {base}" for index in range(count)]


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


def _drain(db, limit=120):
    for _ in range(limit):
        reap(db)
        db.commit()
        worked = process_available(db, "test")
        db.commit()
        if not worked:
            return


def _prepare(session, tmp_path, owner, name: str, paragraphs: list[str]):
    path = tmp_path / name
    _pdf(path, paragraphs)
    document = create_document(session, path.read_bytes(), name, owner.id)
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    session.refresh(document)
    assert document.analysis_status == "ready"
    return document


def _mini_cost(payload: dict) -> float:
    match = next(item for item in payload["models"] if item["model"] == "gpt-4o-mini")
    return match["cost_cents"]


def test_cached_tokens_price_uncached_and_cached_input_separately():
    spec = resolve_model("gpt-4o-mini")
    assert spec.cached_input_price_per_1m == 0.075
    assert cost_cents(spec, 1000, 100, 400) == Decimal("0.0180")
    bare = ModelSpec(
        id="tiny",
        alias="tiny",
        tokenizer="o200k_base",
        context_window=128000,
        max_output_tokens=16384,
        input_price_per_1m=2.0,
        output_price_per_1m=8.0,
        quality_tier="fast",
    )
    assert cost_cents(bare, 1000, 0, 1000) == Decimal("0.1000")
    assert image_cost_cents(spec, 1500, 2000, "high") == cost_cents(spec, image_tokens(spec, 1500, 2000, "high"), 0)


def test_ledger_matches_provider_calls_including_review(session, make_user, tmp_path):
    owner = make_user()
    document = _prepare(session, tmp_path, owner, "ledger.pdf", _lines(220))
    job = create_job(
        session,
        document.id,
        "uk",
        None,
        "gpt-4o-mini",
        [{"source": "motor", "target": "двигун"}],
        f"ledger-{uuid.uuid4()}",
    )
    session.commit()
    _drain(session)
    session.refresh(job)
    assert job.status == "done"
    assert job.total_segments >= 2
    assert job.summary
    review_calls = session.query(ProviderCall).filter(ProviderCall.job_id == job.id, ProviderCall.stage == "review", ProviderCall.status == "ok").count()
    summary_calls = session.query(ProviderCall).filter(ProviderCall.job_id == job.id, ProviderCall.stage == "summary", ProviderCall.status == "ok").count()
    assert review_calls >= 1
    assert summary_calls >= 1
    total = session.execute(
        text("SELECT COALESCE(SUM(cost_cents), 0) FROM provider_calls WHERE job_id = :id"),
        {"id": job.id},
    ).scalar()
    session.refresh(job)
    assert job.cost_cents == total


def test_estimate_tracks_translate_tokens_and_segment_count(session, make_user, tmp_path):
    owner = make_user()
    spec = resolve_model("gpt-4o-mini")
    settings = get_settings()
    from app.core.chunking import segment_budget

    budget = segment_budget(spec, "en", "de", settings.prompt_overhead_tokens, settings.global_quality_cap)
    lines = []
    used = 0
    target = int(budget * 0.9)
    index = 0
    while used < target:
        line = f"{index:04d} The sealed motor housing stays quiet on the long stage through dust and rain."
        tokens = count_tokens(spec, line)
        if lines and used + tokens > target:
            break
        lines.append(line)
        used += tokens
        index += 1
    document = _prepare(session, tmp_path, owner, "estimate.pdf", lines)
    quote = estimate_for(session, document, "de", "gpt-4o-mini")
    job = create_job(session, document.id, "de", None, "gpt-4o-mini", None, f"estimate-{uuid.uuid4()}")
    session.commit()
    session.refresh(job)
    assert job.estimate_cents == quote["cost_cents"]
    _drain(session)
    session.refresh(job)
    assert job.status == "done"
    assert job.total_segments == quote["segments"]
    actual = session.execute(
        text(
            """
            SELECT COALESCE(SUM(tokens_in), 0)
            FROM provider_calls
            WHERE job_id = :id AND stage = 'translate' AND status = 'ok'
            """
        ),
        {"id": job.id},
    ).scalar()
    actual = int(actual)
    assert actual > 0
    gap = abs(quote["tokens_in"] - actual) / actual
    assert gap <= 0.10, f"estimate={quote['tokens_in']} actual={actual} gap={gap:.3f} segments={quote['segments']}"


def test_estimate_endpoint_target_memory_and_isolation(session, make_user, auth_client, tmp_path):
    owner = make_user()
    other = make_user()
    path = tmp_path / "shared.pdf"
    _pdf(path, _lines(8))
    data = path.read_bytes()
    document = create_document(session, data, "shared.pdf", owner.id)
    other_document = create_document(session, data, "shared.pdf", other.id)
    session.commit()
    run_analysis(session, document.id)
    run_analysis(session, other_document.id)
    session.commit()
    session.refresh(document)
    session.refresh(other_document)
    assert document.analysis_status == "ready"
    assert other_document.analysis_status == "ready"
    client = auth_client(owner)
    german = client.get(f"/api/documents/{document.id}/estimate", params={"target_lang": "de"})
    japanese = client.get(f"/api/documents/{document.id}/estimate", params={"target_lang": "ja"})
    assert german.status_code == 200, german.text
    assert japanese.status_code == 200, japanese.text
    assert _mini_cost(japanese.json()) > _mini_cost(german.json())
    unknown = client.get(f"/api/documents/{document.id}/estimate", params={"target_lang": "xx"})
    assert unknown.status_code == 400
    assert unknown.json()["code"] == "unknown_language"
    block = session.scalars(
        select(Block).where(Block.document_id == document.id, Block.kind != "image", Block.text.is_not(None)).order_by(Block.idx)
    ).first()
    assert block is not None and block.text
    source = (document.detected_source_lang or "en").lower()
    before = _mini_cost(german.json())
    session.add(
        TranslationMemory(
            owner_id=owner.id,
            src_hash=block.src_hash,
            source_lang=source,
            target_lang="de",
            tgt_text="Schon übersetzt",
            model="gpt-4o-mini",
        )
    )
    session.commit()
    after = client.get(f"/api/documents/{document.id}/estimate", params={"target_lang": "de"})
    assert after.status_code == 200
    assert _mini_cost(after.json()) < before
    stranger = auth_client(other)
    hidden = stranger.get(f"/api/documents/{document.id}/estimate", params={"target_lang": "de"})
    assert hidden.status_code == 404
    assert hidden.json()["code"] == "not_found"
    own_quote = estimate_for(session, document, "de", "gpt-4o-mini")
    other_quote = estimate_for(session, other_document, "de", "gpt-4o-mini")
    session.add(
        TranslationMemory(
            owner_id=other.id,
            src_hash=block.src_hash,
            source_lang=source,
            target_lang="de",
            tgt_text="Fremde Erinnerung",
            model="gpt-4o-mini",
        )
    )
    session.commit()
    session.expire_all()
    untouched = estimate_for(session, document, "de", "gpt-4o-mini")
    lowered = estimate_for(session, other_document, "de", "gpt-4o-mini")
    assert untouched["cost_cents"] == own_quote["cost_cents"]
    assert lowered["cost_cents"] < other_quote["cost_cents"]
    assert lowered["tm_hits"] >= 1
