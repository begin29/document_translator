import json
import os
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import text
from structlog.testing import capture_logs

from app.config import get_settings
from app.core import events
from app.core.errors import InjectedCrash
from app.core.service import _add_progress, create_document, create_job, process_available, reap, run_analysis, sync_gate
from app.db.models import Document, TranslationJob
from app.db.session import reset_engine, session_factory

try:
    import fakeredis
except ImportError:  # pragma: no cover
    fakeredis = None


needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")


class RecordingRedis(fakeredis.FakeRedis):
    def __init__(self):
        super().__init__()
        self.published = []

    def publish(self, *args, **kwargs):
        self.published.append((args[0], _text(args[1])))
        return super().publish(*args, **kwargs)

    def spublish(self, *args, **kwargs):
        self.published.append((args[0], _text(args[1])))
        return super().publish(*args, **kwargs)


class RaisingRedis(fakeredis.FakeRedis):
    def publish(self, *args, **kwargs):
        raise RuntimeError("redis down")

    def spublish(self, *args, **kwargs):
        raise RuntimeError("redis down")


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return value


def _patch_redis(monkeypatch, client=None):
    client = client or RecordingRedis()
    monkeypatch.setattr(events, "_redis", lambda: client)
    events._sent.clear()
    return client


def _job_state(status="translating", done=1, total=4, cost=1):
    return {
        "id": "job-throttle",
        "status": status,
        "done_segments": done,
        "total_segments": total,
        "cost_cents": cost,
        "error_code": None,
    }


@pytest.fixture(autouse=True)
def _clear_publish_state():
    events._sent.clear()
    yield
    events._sent.clear()


def test_channels_for_names_phase1_channels():
    assert events.channels_for({"type": "job", "id": "abc"}) == ["job:abc"]
    assert events.channels_for({"type": "document", "id": "def"}) == ["document:def"]
    assert events.channels_for({"type": "other", "id": "abc"}) == []


def test_empty_redis_url_does_not_connect(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")
    get_settings.cache_clear()
    events._client = None
    events._client_key = None
    calls = []

    def from_url(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("from_url")

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    events.publish_job(_job_state())
    events.publish_document({"id": "doc-1", "analysis_status": "ready", "error_code": None})
    assert calls == []


def test_throttle_drops_repeat_and_keeps_status_and_terminal(monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(events, "_now", lambda: clock["now"])
    client = _patch_redis(monkeypatch)
    events.publish_job(_job_state(done=1))
    events.publish_job(_job_state(done=2))
    assert len(client.published) == 1
    clock["now"] += 0.499
    events.publish_job(_job_state(status="rendering", done=2))
    events.publish_job(_job_state(status="cancelled", done=2))
    assert [json.loads(body)["status"] for _channel, body in client.published] == [
        "translating",
        "rendering",
        "cancelled",
    ]
    assert "job-throttle" not in events._sent
    events.publish_job(_job_state(done=3))
    assert len(client.published) == 4
    clock["now"] += 0.5
    events.publish_job(_job_state(done=4))
    assert len(client.published) == 5
    assert client.published[0][0] == "job:job-throttle"


def test_document_messages_are_not_throttled(monkeypatch):
    client = _patch_redis(monkeypatch)
    state = {"id": "doc-1", "analysis_status": "running", "error_code": None}
    events.publish_document(state)
    events.publish_document(state)
    events.publish_document({"id": "doc-1", "analysis_status": "ready", "error_code": None})
    assert len(client.published) == 3
    assert [channel for channel, _body in client.published] == ["document:doc-1"] * 3
    assert json.loads(client.published[-1][1])["analysis_status"] == "ready"


def test_cluster_uses_spublish(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setenv("REDIS_CLUSTER", "true")
    get_settings.cache_clear()
    calls = []

    class Cluster:
        def publish(self, *args, **kwargs):
            calls.append("publish")

        def spublish(self, *args, **kwargs):
            calls.append("spublish")

    monkeypatch.setattr(events, "_redis", lambda: Cluster())
    events._sent.clear()
    events.publish_job(_job_state())
    assert calls == ["spublish"]


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


def _seed(session):
    document = Document(
        id=uuid.uuid4(),
        filename="a.pdf",
        format="pdf",
        size_bytes=4,
        sha256=uuid.uuid4().hex + uuid.uuid4().hex,
        storage_path="/tmp/stark-events.pdf",
        analysis_status="ready",
    )
    session.add(document)
    session.flush()
    job = TranslationJob(
        document_id=document.id,
        source_lang="en",
        target_lang="de",
        model="gpt-4o-mini",
        status="translating",
        total_segments=4,
        done_segments=0,
        cost_cents=0,
    )
    session.add(job)
    session.flush()
    return document, job


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


def _drain(db, limit=80):
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


@needs_db
def test_commit_publishes_once(session, monkeypatch):
    _document, job = _seed(session)
    session.commit()
    client = _patch_redis(monkeypatch)
    _add_progress(session, job, segments=2, cost=Decimal("1.25"))
    session.commit()
    assert len(client.published) == 1
    channel, raw = client.published[0]
    message = json.loads(raw)
    session.refresh(job)
    assert channel == f"job:{job.id}"
    assert message["type"] == "job"
    assert message["id"] == str(job.id)
    assert message["status"] == job.status
    assert message["done_segments"] == job.done_segments == 2
    assert message["total_segments"] == job.total_segments
    assert message["cost_cents"] == pytest.approx(float(job.cost_cents))
    assert message["error_code"] is None
    assert isinstance(message["seq"], int)


@needs_db
def test_rollback_publishes_nothing(session, monkeypatch):
    _document, job = _seed(session)
    session.commit()
    client = _patch_redis(monkeypatch)
    _add_progress(session, job, segments=1)
    session.rollback()
    session.commit()
    assert client.published == []
    _add_progress(session, job, segments=1)
    session.commit()
    assert len(client.published) == 1


@needs_db
def test_raising_client_does_not_fail_progress(session, monkeypatch):
    _document, job = _seed(session)
    session.commit()
    _patch_redis(monkeypatch, RaisingRedis())
    with capture_logs() as captured:
        _add_progress(session, job, segments=1)
        session.commit()
    session.refresh(job)
    assert job.done_segments == 1
    assert any(entry.get("event") == "event_publish_failed" for entry in captured)


@needs_db
def test_full_job_publishes_done_matching_db(session, monkeypatch, tmp_path):
    client = _patch_redis(monkeypatch)
    path = tmp_path / "short.pdf"
    _pdf(path, ["The motor housing is sealed against dust and water during every ride."])
    document = create_document(session, "short.pdf", path.read_bytes())
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    session.refresh(document)
    assert document.analysis_status == "ready"
    job = create_job(session, document.id, "de", None, "gpt-4o-mini", None, f"events-{uuid.uuid4()}")
    session.commit()
    _drain(session)
    session.refresh(job)
    assert job.status == "done"
    documents = [json.loads(body) for channel, body in client.published if channel == f"document:{document.id}"]
    assert any(item["analysis_status"] == "ready" and item["error_code"] is None for item in documents)
    done = [
        json.loads(body)
        for channel, body in client.published
        if channel == f"job:{job.id}" and json.loads(body)["status"] == "done"
    ]
    assert len(done) == 1
    assert done[0]["done_segments"] == job.done_segments
    assert done[0]["total_segments"] == job.total_segments
    assert done[0]["cost_cents"] == pytest.approx(float(job.cost_cents or 0))
    assert done[0]["error_code"] == job.error_code
    assert done[0]["type"] == "job"
    assert isinstance(done[0]["seq"], int)
