import asyncio
import json
import os
import socket
import threading
import time
import uuid
from pathlib import Path

import fakeredis
import httpx
import pytest
import redis
import redis.asyncio
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import text

from app.auth import tickets
from app.auth.throttle import reset_login_limit
from app.config import get_settings
from app.core.provider.fake_provider import reset_crash_memory
from app.core.service import create_document, create_job, process_available, reap, reset_runtime, run_analysis, sync_gate
from app.db.models import BlockTranslation, ProviderCall
from app.db.session import reset_engine, session_factory
from app.main import app
from app.mcp.__main__ import create_upload_url, estimate_translation, list_documents, translate_document

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")
ORIGIN = "http://localhost:8080"


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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
    for _ in range(limit):
        reap(db)
        db.commit()
        worked = process_available(db, "test")
        db.commit()
        if not worked:
            return


def _client() -> TestClient:
    return TestClient(app)


def test_register_happy_path(session):
    client = _client()
    response = client.post(
        "/api/auth/register",
        json={"email": "New.User@Example.com", "password": "password1234"},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "new.user@example.com"
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == "new.user@example.com"
    stored = session.execute(text("SELECT email FROM users WHERE id = CAST(:id AS uuid)"), {"id": body["id"]}).scalar()
    session.rollback()
    assert stored == "new.user@example.com"


def test_register_rejects_taken_weak_and_invalid_email(session, monkeypatch):
    client = _client()
    first = client.post(
        "/api/auth/register",
        json={"email": "ada@example.com", "password": "password1234"},
        headers={"Origin": ORIGIN},
    )
    assert first.status_code == 200
    taken = _client().post(
        "/api/auth/register",
        json={"email": "Ada@Example.com", "password": "password1234"},
        headers={"Origin": ORIGIN},
    )
    assert taken.status_code == 409
    assert taken.json()["code"] == "email_taken"
    weak = _client().post(
        "/api/auth/register",
        json={"email": "weak@example.com", "password": "short"},
        headers={"Origin": ORIGIN},
    )
    assert weak.status_code == 400
    assert weak.json()["code"] == "weak_password"
    invalid = _client().post(
        "/api/auth/register",
        json={"email": "not-an-email", "password": "password1234"},
        headers={"Origin": ORIGIN},
    )
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "invalid_email"
    monkeypatch.setenv("ALLOW_SIGNUP", "false")
    get_settings.cache_clear()
    disabled = _client().post(
        "/api/auth/register",
        json={"email": "other@example.com", "password": "password1234"},
        headers={"Origin": ORIGIN},
    )
    assert disabled.status_code == 403
    assert disabled.json()["code"] == "signup_disabled"


def test_login_logout_and_expired_session(session, make_user, auth_client):
    user = make_user(email="ada@example.com")
    client = auth_client(user)
    assert client.get("/api/auth/me").json()["email"] == "ada@example.com"
    wrong = _client().post(
        "/api/auth/login",
        json={"email": "ada@example.com", "password": "not-the-password"},
        headers={"Origin": ORIGIN},
    )
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "invalid_credentials"
    missing = _client().post(
        "/api/auth/login",
        json={"email": "nobody@example.com", "password": "not-the-password"},
        headers={"Origin": ORIGIN},
    )
    assert missing.status_code == 401
    assert missing.json()["code"] == "invalid_credentials"
    logged_out = auth_client(user)
    assert logged_out.post("/api/auth/logout").status_code == 200
    assert logged_out.get("/api/auth/me").status_code == 401
    expired = auth_client(user)
    session.execute(text("UPDATE sessions SET expires_at = now() - interval '1 day'"))
    session.commit()
    assert expired.get("/api/auth/me").status_code == 401


def test_login_locks_after_five_failures(session, make_user, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    get_settings.cache_clear()
    reset_login_limit()
    fake = fakeredis.FakeServer()
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fakeredis.FakeRedis(server=fake))
    user = make_user(email="lock@example.com")
    client = _client()
    for _ in range(5):
        response = client.post(
            "/api/auth/login",
            json={"email": user.email, "password": "wrong-password"},
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 401
        assert response.json()["code"] == "invalid_credentials"
    blocked = client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "wrong-password"},
        headers={"Origin": ORIGIN},
    )
    assert blocked.status_code == 429
    assert blocked.json()["code"] == "too_many_attempts"


def test_api_token_authorizes_and_revokes(session, make_user, auth_client, token_client):
    user = make_user()
    cookie = auth_client(user)
    created = cookie.post("/api/auth/tokens", json={"name": "laptop"})
    assert created.status_code == 200
    raw = created.json()["token"]
    assert raw.startswith("stk_")
    assert created.json()["prefix"] == raw[4:12]
    listed = cookie.get("/api/auth/tokens")
    assert listed.status_code == 200
    assert raw not in listed.text
    assert "token_hash" not in listed.text
    bearer = token_client(user)
    assert bearer.get("/api/jobs").status_code == 200
    token_id = listed.json()[0]["id"]
    assert cookie.delete(f"/api/auth/tokens/{token_id}").status_code == 200
    revoked = TestClient(app)
    revoked.headers.update({"Authorization": f"Bearer {raw}"})
    assert revoked.get("/api/jobs").status_code == 401


def _ready(session, owner, tmp_path, name):
    path = tmp_path / f"{name}.pdf"
    _pdf(path, ["The motor housing is sealed against dust and water during every ride."])
    document = create_document(session, path.read_bytes(), f"{name}.pdf", owner.id)
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    job = create_job(session, document.id, "de", None, "gpt-4o-mini", None, f"auth-{name}-{uuid.uuid4()}")
    session.commit()
    return document, job


def test_users_cannot_see_each_others_rows(session, tmp_path, make_user, auth_client):
    owner = make_user(email="owner@example.com")
    other = make_user(email="other@example.com")
    document, job = _ready(session, owner, tmp_path, "private")
    client = auth_client(other)
    paths = [
        f"/api/documents/{document.id}",
        f"/api/documents/{document.id}/pages/1/thumbnail",
        f"/api/jobs/{job.id}",
        f"/api/jobs/{job.id}/segments",
        f"/api/jobs/{job.id}/download",
        f"/api/jobs/{job.id}/events",
        f"/api/jobs/{job.id}/cost",
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["code"] == "not_found"
    cancelled = client.post(f"/api/jobs/{job.id}/cancel")
    assert cancelled.status_code == 404
    assert cancelled.json()["code"] == "not_found"
    assert all(item["id"] != str(document.id) for item in client.get("/api/documents").json())
    assert all(item["id"] != str(job.id) for item in client.get("/api/jobs").json())
    own = auth_client(owner)
    assert own.get(f"/api/documents/{document.id}").status_code == 200
    assert any(item["id"] == str(job.id) for item in own.get("/api/jobs").json())


def test_same_bytes_are_separate_documents(session, tmp_path, make_user, auth_client):
    first = make_user(email="first@example.com")
    second = make_user(email="second@example.com")
    path = tmp_path / "same.pdf"
    _pdf(path, ["Shared bytes still belong to the person who uploaded them."])
    payload = path.read_bytes()
    uploaded = []
    for user in (first, second):
        response = auth_client(user).post("/api/documents", files={"file": ("same.pdf", payload, "application/pdf")})
        assert response.status_code == 200
        uploaded.append(response.json()["id"])
    assert uploaded[0] != uploaded[1]


def test_translation_memory_stays_with_its_owner(session, tmp_path, make_user):
    first = make_user(email="memory-a@example.com")
    second = make_user(email="memory-b@example.com")
    _document_a, job_a = _ready(session, first, tmp_path, "memory")
    _drain(session)
    session.refresh(job_a)
    assert job_a.status == "done"
    _document_b, job_b = _ready(session, second, tmp_path, "memory-b")
    _drain(session)
    session.refresh(job_b)
    assert job_b.status == "done"
    calls = session.query(ProviderCall).filter(ProviderCall.job_id == job_b.id, ProviderCall.stage == "translate").count()
    assert calls >= 1
    sources = {
        row.source
        for row in session.query(BlockTranslation).filter(BlockTranslation.job_id == job_b.id).all()
    }
    assert "model" in sources


def test_csrf_blocks_a_foreign_origin_and_allows_bearer(session, make_user, auth_client, token_client):
    user = make_user()
    cookie = auth_client(user)
    blocked = cookie.post("/api/events/ticket", headers={"Origin": "http://evil"})
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "csrf_failed"
    both = token_client(user)
    both.cookies.update(cookie.cookies)
    allowed = both.post("/api/events/ticket")
    assert allowed.status_code == 200
    assert allowed.json()["ticket"]


def test_tickets_round_trip_and_reject_bad_values():
    uid = uuid.uuid4()
    ticket, exp = tickets.issue(uid)
    assert tickets.verify(ticket) == uid
    assert exp > int(time.time())
    head, mac = ticket.rsplit(".", 1)
    flipped = ("0" if mac[0] != "0" else "1") + mac[1:]
    assert tickets.verify(f"{head}.{flipped}") is None
    future = time.time() + 120

    def later():
        return future

    monkey = pytest.MonkeyPatch()
    monkey.setattr(tickets.time, "time", later)
    try:
        assert tickets.verify(ticket) is None
    finally:
        monkey.undo()


def _serve(application, port: int):
    config = uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning", loop="asyncio", lifespan="on", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def _wait_http(url: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=0.3).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.05)
    raise RuntimeError(f"{url} did not become ready")


@pytest.fixture()
def realtime(monkeypatch):
    from app.realtime.app import _uid_connections
    from app.realtime.app import app as realtime_app

    fake = fakeredis.FakeServer()
    sync = fakeredis.FakeRedis(server=fake)
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.setenv("PROVIDER", "fake")
    get_settings.cache_clear()
    _uid_connections.clear()
    monkeypatch.setattr(redis.asyncio, "from_url", lambda *args, **kwargs: fakeredis.aioredis.FakeRedis(server=fake))
    port = _free_port()
    server, thread = _serve(realtime_app, port)
    try:
        _wait_http(f"http://127.0.0.1:{port}/health")
        yield {"url": f"http://127.0.0.1:{port}", "redis": sync}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_realtime_rejects_a_bad_ticket(realtime):
    response = httpx.get(f"{realtime['url']}/events", params={"ticket": "bad"}, timeout=2)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["code"] == "unauthorized"


async def test_realtime_delivers_only_to_the_owning_user(realtime):
    owner = uuid.uuid4()
    stranger = uuid.uuid4()
    owner_ticket, _exp = tickets.issue(owner)
    stranger_ticket, _exp = tickets.issue(stranger)
    payload = {"type": "job", "id": str(uuid.uuid4()), "status": "translating", "owner_id": str(owner), "seq": 5}
    seen = {"owner": False, "stranger": False}
    owner_ready = asyncio.Event()
    stranger_ready = asyncio.Event()

    async def listen(ticket, ready, key):
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", f"{realtime['url']}/events", params={"ticket": ticket}) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line.startswith("event:") and line.split(":", 1)[1].strip() == "subscribed":
                        ready.set()
                    if line.startswith("data:") and payload["id"] in line:
                        seen[key] = True
                        return

    owner_task = asyncio.create_task(listen(owner_ticket, owner_ready, "owner"))
    stranger_task = asyncio.create_task(listen(stranger_ticket, stranger_ready, "stranger"))
    await asyncio.wait_for(owner_ready.wait(), 3)
    await asyncio.wait_for(stranger_ready.wait(), 3)
    realtime["redis"].publish(f"user:{owner}", json.dumps(payload))
    await asyncio.wait_for(owner_task, 3)
    await asyncio.sleep(0.3)
    stranger_task.cancel()
    await asyncio.gather(stranger_task, return_exceptions=True)
    assert seen["owner"] is True
    assert seen["stranger"] is False


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class _Request:
    def __init__(self, token: str) -> None:
        self.headers = _Headers({"authorization": f"Bearer {token}"})


class _Holder:
    def __init__(self, token: str) -> None:
        self.request = _Request(token)


class _Ctx:
    def __init__(self, token: str) -> None:
        self._request_context = _Holder(token)


async def test_mcp_requires_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("TRANSLATOR_API_TOKEN", raising=False)
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    path = tmp_path / "note.pdf"
    _pdf(path, ["A token is required before this page can be uploaded."])
    with pytest.raises(ToolError, match="unauthorized"):
        await estimate_translation(path=str(path), target_language="de")


async def test_mcp_token_job_is_listed_for_that_user(session, tmp_path, monkeypatch):
    from app.worker import loop

    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.setenv("PROVIDER", "fake")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("TRANSLATOR_API_TOKEN", raising=False)
    api_port = _free_port()
    api_url = f"http://127.0.0.1:{api_port}"
    monkeypatch.setenv("TRANSLATOR_API_URL", api_url)
    monkeypatch.setenv("REALTIME_URL", f"http://127.0.0.1:{_free_port()}")
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    get_settings.cache_clear()
    server, thread = _serve(app, api_port)
    stop = threading.Event()
    worker = threading.Thread(target=loop, args=("auth-mcp", stop), daemon=True)
    try:
        _wait_http(f"{api_url}/ready")
        worker.start()
        with httpx.Client(base_url=api_url, timeout=30) as http:
            created = http.post(
                "/api/auth/register",
                json={"email": "mcp-owner@example.com", "password": "password1234"},
                headers={"Origin": ORIGIN},
            )
            assert created.status_code == 200, created.text
            minted = http.post("/api/auth/tokens", json={"name": "agent"}, headers={"Origin": ORIGIN})
            assert minted.status_code == 200, minted.text
            token = minted.json()["token"]
            other = httpx.post(
                f"{api_url}/api/auth/register",
                json={"email": "mcp-other@example.com", "password": "password1234"},
                headers={"Origin": ORIGIN},
                timeout=30,
            )
            assert other.status_code == 200, other.text
        path = tmp_path / "owned.pdf"
        _pdf(path, ["This translation belongs only to the token holder."])
        with pytest.raises(ToolError, match="path_not_supported"):
            await translate_document(path=str(path), target_language="de", wait=False, ctx=_Ctx(token))
        link = await create_upload_url(ctx=_Ctx(token))
        sent = httpx.post(link["upload_url"].replace(ORIGIN, api_url, 1), files={"file": (path.name, path.read_bytes())}, timeout=10)
        assert sent.status_code == 200, sent.text
        listed_docs = await list_documents(name="OWNED", ctx=_Ctx(token))
        assert [item["id"] for item in listed_docs["documents"]] == [sent.json()["id"]]
        result = await translate_document(document_id=sent.json()["id"], target_language="de", wait=False, ctx=_Ctx(token))
        listed = httpx.get(f"{api_url}/api/jobs", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        assert listed.status_code == 200
        assert any(item["id"] == result["job_id"] for item in listed.json())
        stranger = httpx.Client(base_url=api_url, timeout=10)
        stranger.post(
            "/api/auth/login",
            json={"email": "mcp-other@example.com", "password": "password1234"},
            headers={"Origin": ORIGIN},
        )
        hidden = stranger.get("/api/jobs")
        assert all(item["id"] != result["job_id"] for item in hidden.json())
        stranger.close()
    finally:
        stop.set()
        worker.join(timeout=5)
        server.should_exit = True
        thread.join(timeout=5)
        get_settings.cache_clear()
