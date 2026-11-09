import os
import socket
import threading
import time
from pathlib import Path

import anyio
import fakeredis
import httpx
import pytest
import redis
import redis.asyncio
import uvicorn
from alembic import command
from alembic.config import Config
from mcp.server.fastmcp.exceptions import ToolError
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import text

from app.config import get_settings
from app.core import events
from app.core.provider.fake_provider import reset_crash_memory
from app.core.service import reset_runtime, sync_gate
from app.db.session import reset_engine, session_factory
from app.mcp.__main__ import (
    create_upload_url,
    download_translation,
    list_documents,
    estimate_translation,
    get_translation_status,
    mcp,
    translate_document,
)
from app.mcp.client import ApiClient

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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_http(url: str, predicate) -> None:
    deadline = time.monotonic() + 15
    last = None
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=0.3)
            last = response.status_code
            if predicate(response):
                return
        except httpx.HTTPError as exc:
            last = exc
        time.sleep(0.05)
    raise RuntimeError(f"{url} did not become ready: {last}")


def _serve(app, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio", lifespan="on", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture(scope="module")
def _migrated():
    get_settings.cache_clear()
    reset_engine()
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", os.environ["TEST_DATABASE_URL"])
    command.upgrade(cfg, "head")
    yield


@pytest.fixture()
def stack(_migrated, monkeypatch, tmp_path):
    from app.main import app as api_app
    from app.realtime.app import app as realtime_app
    from app.worker import loop

    test_db = os.environ["TEST_DATABASE_URL"]
    fake = fakeredis.FakeServer()
    api_port = _free_port()
    realtime_port = _free_port()
    api_url = f"http://127.0.0.1:{api_port}"
    realtime_url = f"http://127.0.0.1:{realtime_port}"
    monkeypatch.setenv("DATABASE_URL", test_db)
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.setenv("TRANSLATOR_API_URL", api_url)
    monkeypatch.setenv("REALTIME_URL", realtime_url)
    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("PUBLIC_ORIGIN", api_url)
    monkeypatch.setenv("PROVIDER", "fake")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("FAKE_LATENCY_MS", "200")
    monkeypatch.setenv("FAKE_FAIL_MODE", "")
    monkeypatch.setenv("FAKE_FAIL_SEGMENTS", "")
    monkeypatch.setenv("FAKE_FAIL_RATE", "0")
    monkeypatch.setenv("CRASH_AFTER_SEGMENT", "0")
    monkeypatch.setattr(redis.Redis, "from_url", lambda *args, **kwargs: fakeredis.FakeRedis(server=fake))
    monkeypatch.setattr(redis.asyncio, "from_url", lambda *args, **kwargs: fakeredis.aioredis.FakeRedis(server=fake))
    get_settings.cache_clear()
    reset_engine()
    reset_runtime()
    reset_crash_memory()
    events._client = None
    events._client_key = None
    events._sent.clear()
    factory = session_factory()
    with factory() as db:
        db.execute(
            text(
                "TRUNCATE users, documents, blocks, translation_jobs, segments, block_translations, translation_memory, job_events, provider_calls, ocr_pages RESTART IDENTITY CASCADE"
            )
        )
        db.execute(text("UPDATE provider_gate SET inflight=0, max_inflight=0, effective_max=ceiling, window_tokens=0, last_429_at=NULL"))
        sync_gate(db)
        from app.auth.passwords import hash_password
        from app.auth.tokens import create_token
        from app.db.models import User

        account = User(email="mcp@example.com", password_hash=hash_password("password1234"))
        db.add(account)
        db.flush()
        token = create_token(db, account, "mcp")
        db.commit()
    monkeypatch.setenv("TRANSLATOR_API_TOKEN", token)
    auth_headers = {"Authorization": f"Bearer {token}"}
    realtime_server, realtime_thread = _serve(realtime_app, realtime_port)
    api_server, api_thread = _serve(api_app, api_port)
    stop = threading.Event()
    worker = threading.Thread(target=loop, args=("mcp-test", stop), daemon=True)
    try:
        _wait_http(f"{realtime_url}/health", lambda response: response.status_code == 200)
        _wait_http(f"{api_url}/ready", lambda response: response.status_code == 200)
        worker.start()
        warmup = tmp_path / "warmup.pdf"
        _pdf(warmup, ["Warmup paragraph so the worker imports its libraries before the ticker."])
        uploaded = httpx.post(
            f"{api_url}/api/documents",
            files={"file": ("warmup.pdf", warmup.read_bytes())},
            headers=auth_headers,
            timeout=10,
        )
        uploaded.raise_for_status()
        document_id = uploaded.json()["id"]
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            ready = httpx.get(f"{api_url}/api/documents/{document_id}", headers=auth_headers, timeout=2)
            ready.raise_for_status()
            status = ready.json()["analysis_status"]
            if status in {"ready", "failed"}:
                break
            time.sleep(0.1)
        if status != "ready":
            raise RuntimeError(f"warmup analysis did not finish: {status}")
        yield {"api": api_url, "realtime": realtime_url, "root": tmp_path, "headers": auth_headers}
    finally:
        stop.set()
        worker.join(timeout=5)
        api_server.should_exit = True
        realtime_server.should_exit = True
        api_thread.join(timeout=5)
        realtime_thread.join(timeout=5)
        events._client = None
        events._client_key = None
        get_settings.cache_clear()
        reset_engine()


def test_tools_registered():
    names = {tool.name for tool in mcp._tool_manager.list_tools()}
    assert names == {
        "create_upload_url",
        "list_documents",
        "estimate_translation",
        "translate_document",
        "get_translation_status",
        "download_translation",
    }
    assert all(tool.is_async for tool in mcp._tool_manager.list_tools())


async def test_translate_returns_job_id_immediately(stack):
    path = stack["root"] / "soon.pdf"
    _pdf(path, ["Immediate return once analysis of this short page is ready."])
    async with ApiClient(stack["api"], stack["realtime"]) as api:
        uploaded = await api.upload(str(path))
        await api.wait_document(uploaded["id"])
    started = time.monotonic()
    result = await translate_document(path=str(path), target_language="de", wait=False)
    elapsed = time.monotonic() - started
    assert elapsed < 1
    assert result["job_id"]
    assert result["document_id"]
    assert result["status"] != "done"
    status = await get_translation_status(result["job_id"])
    assert status["status"] != "done"


async def test_parallel_translations_do_not_block(stack):
    first = stack["root"] / "one.pdf"
    second = stack["root"] / "two.pdf"
    third = stack["root"] / "three.pdf"
    _pdf(first, ["Alpha paragraph about the northern bridge and its stone arches."])
    _pdf(second, ["Beta paragraph about the southern harbor and its quiet boats."])
    _pdf(third, ["Gamma paragraph kept for the estimate that runs beside the translations."])
    gaps: list[float] = []
    stop = anyio.Event()
    results: dict = {}

    async def ticker() -> None:
        last = time.monotonic()
        while not stop.is_set():
            await anyio.sleep(0.05)
            now = time.monotonic()
            gaps.append(now - last)
            last = now

    async def run_translate(name: str, path: Path) -> None:
        results[name] = await translate_document(path=str(path), target_language="de", wait=True)

    async def run_estimate() -> None:
        results["estimate"] = await estimate_translation(path=str(third), target_language="de")

    async def jobs() -> None:
        try:
            async with anyio.create_task_group() as inner:
                inner.start_soon(run_translate, "a", first)
                inner.start_soon(run_translate, "b", second)
                inner.start_soon(run_estimate)
        finally:
            stop.set()

    async with anyio.create_task_group() as group:
        group.start_soon(ticker)
        group.start_soon(jobs)
    assert set(results) == {"a", "b", "estimate"}
    assert results["a"]["status"] == "done"
    assert results["b"]["status"] == "done"
    assert results["a"]["job_id"] != results["b"]["job_id"]
    assert Path(results["a"]["output_path"]).read_bytes()[:4] == b"%PDF"
    assert Path(results["b"]["output_path"]).read_bytes()[:4] == b"%PDF"
    assert results["estimate"]["document_id"]
    assert results["estimate"]["models"]
    assert gaps
    assert max(gaps) <= 0.5


async def test_progress_reported(stack):
    path = stack["root"] / "progress.pdf"
    _pdf(path, ["Progress should climb while this page is translated into German."])

    class Ctx:
        def __init__(self) -> None:
            self.events: list[tuple[int, int, str]] = []

        async def report_progress(self, done: int, total: int, status: str | None = None) -> None:
            self.events.append((done, total or 1, status or ""))

    ctx = Ctx()
    result = await translate_document(path=str(path), target_language="de", wait=True, ctx=ctx)
    assert result["status"] == "done"
    dones = [item[0] for item in ctx.events]
    assert len(dones) >= 2
    assert dones[-1] > dones[0]
    assert all(left <= right for left, right in zip(dones, dones[1:]))


async def test_path_outside_workspace_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_WORKSPACE", str(tmp_path))
    with pytest.raises(ToolError, match="path_outside_workspace"):
        await estimate_translation(path="/etc/hosts", target_language="de")
    inside = tmp_path / "inside.pdf"
    with pytest.raises(ToolError, match="path_outside_workspace"):
        await translate_document(path=str(inside), target_language="de", output_path="/etc/outside.pdf", wait=False)


async def test_realtime_down_falls_back(stack, monkeypatch):
    closed = _free_port()
    monkeypatch.setenv("REALTIME_URL", f"http://127.0.0.1:{closed}")
    path = stack["root"] / "fallback.pdf"
    _pdf(path, ["This translation finishes by polling REST when realtime is closed."])
    result = await translate_document(path=str(path), target_language="de", wait=True)
    assert result["status"] == "done"
    assert Path(result["output_path"]).read_bytes()[:4] == b"%PDF"
    saved = await download_translation(result["job_id"], str(stack["root"] / "fallback-copy.pdf"))
    assert Path(saved["output_path"]).read_bytes()[:4] == b"%PDF"


async def test_api_error_mapped(stack):
    path = stack["root"] / "broken.pdf"
    path.write_bytes(b"this is not a pdf")
    with pytest.raises(ToolError, match="corrupt_pdf|encrypted_pdf"):
        await estimate_translation(path=str(path), target_language="de")


def _send(upload_url: str, path: Path) -> httpx.Response:
    return httpx.post(upload_url, files={"file": (path.name, path.read_bytes())}, timeout=10)


async def test_remote_upload_translate_and_download(stack, tmp_path_factory):
    outside = tmp_path_factory.mktemp("client") / "remote.pdf"
    _pdf(outside, ["A file that lives on the caller's machine, not in the workspace."])
    link = await create_upload_url(filename=str(outside))
    assert link["upload_url"].startswith(f"{stack['api']}/api/uploads?ticket=")
    assert str(outside) in link["command"]
    sent = _send(link["upload_url"], outside)
    assert sent.status_code == 200, sent.text
    document_id = sent.json()["id"]

    quote = await estimate_translation(document_id=document_id, target_language="de")
    assert quote["document_id"] == document_id
    assert quote["models"]

    result = await translate_document(document_id=document_id, target_language="de", wait=True)
    assert result["status"] == "done"
    assert "output_path" not in result
    fetched = httpx.get(result["download_url"], timeout=10)
    assert fetched.status_code == 200
    assert fetched.content[:4] == b"%PDF"

    again = await download_translation(result["job_id"])
    assert httpx.get(again["download_url"], timeout=10).content[:4] == b"%PDF"
    saved = await download_translation(result["job_id"], str(stack["root"] / "remote.de.pdf"))
    assert Path(saved["output_path"]).read_bytes()[:4] == b"%PDF"


async def test_path_or_document_id_required(stack):
    with pytest.raises(ToolError, match="invalid_arguments"):
        await translate_document(target_language="de")
    with pytest.raises(ToolError, match="invalid_arguments"):
        await estimate_translation(path=str(stack["root"] / "x.pdf"), document_id="abc", target_language="de")


async def test_transfer_tickets_are_scoped(stack):
    from app.auth import tickets

    path = stack["root"] / "scoped.pdf"
    _pdf(path, ["Tickets for one purpose must not open another."])
    events = httpx.post(f"{stack['api']}/api/events/ticket", headers=stack["headers"], timeout=10).json()["ticket"]
    rejected = _send(f"{stack['api']}/api/uploads?ticket={events}", path)
    assert rejected.status_code == 401
    assert rejected.json()["code"] == "invalid_ticket"

    link = await create_upload_url()
    tampered = link["upload_url"][:-1] + ("0" if link["upload_url"][-1] != "0" else "1")
    assert _send(tampered, path).status_code == 401

    document_id = _send(link["upload_url"], path).json()["id"]
    result = await translate_document(document_id=document_id, target_language="de", wait=True)
    download_url = result["download_url"]
    ticket = download_url.split("ticket=", 1)[1]
    other_job = await translate_document(document_id=document_id, target_language="fr", wait=False)
    wrong_job = httpx.get(f"{stack['api']}/api/downloads/{other_job['job_id']}?ticket={ticket}", timeout=10)
    assert wrong_job.status_code == 401
    upload_as_download = link["upload_url"].split("ticket=", 1)[1]
    assert httpx.get(f"{stack['api']}/api/downloads/{result['job_id']}?ticket={upload_as_download}", timeout=10).status_code == 401

    uid = tickets.verify(ticket, "download", result["job_id"])
    expired, _exp = tickets.issue(uid, "download", -1, result["job_id"])
    assert httpx.get(f"{stack['api']}/api/downloads/{result['job_id']}?ticket={expired}", timeout=10).status_code == 401


class _HttpCtx:
    def __init__(self, headers: dict) -> None:
        request = type("Request", (), {"headers": {key.lower(): value for key, value in headers.items()}})()
        self._request_context = type("Holder", (), {"request": request})()

    async def report_progress(self, done: int, total: int, status: str | None = None) -> None:
        return None


async def test_http_clients_cannot_pass_paths(stack):
    ctx = _HttpCtx(stack["headers"])
    inside = stack["root"] / "inside.pdf"
    _pdf(inside, ["Even a file in the workspace is off limits over HTTP."])
    with pytest.raises(ToolError, match="path_not_supported"):
        await estimate_translation(path=str(inside), target_language="de", ctx=ctx)
    link = await create_upload_url(ctx=ctx)
    document_id = _send(link["upload_url"], inside).json()["id"]
    with pytest.raises(ToolError, match="path_not_supported"):
        await translate_document(
            document_id=document_id, target_language="de", output_path=str(stack["root"] / "x.pdf"), ctx=ctx
        )
    result = await translate_document(document_id=document_id, target_language="de", wait=True, ctx=ctx)
    assert "output_path" not in result
    assert httpx.get(result["download_url"], timeout=10).content[:4] == b"%PDF"
    with pytest.raises(ToolError, match="path_not_supported"):
        await download_translation(result["job_id"], str(stack["root"] / "y.pdf"), ctx=ctx)
    assert (await download_translation(result["job_id"], ctx=ctx))["download_url"]


async def test_list_documents_filters_by_name(stack):
    for name in ("Invoice-March.pdf", "contract.pdf"):
        path = stack["root"] / name
        _pdf(path, [f"Body text for {name}."])
        _send((await create_upload_url())["upload_url"], path).raise_for_status()
    everything = (await list_documents())["documents"]
    assert [item["filename"] for item in everything[:2]] == ["contract.pdf", "Invoice-March.pdf"]
    assert set(everything[0]) == {"id", "filename", "format", "page_count", "detected_source_lang", "analysis_status", "created_at"}
    matched = (await list_documents(name="invoice"))["documents"]
    assert [item["filename"] for item in matched] == ["Invoice-March.pdf"]
    assert len((await list_documents(limit=1))["documents"]) == 1
