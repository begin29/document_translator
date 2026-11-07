import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import fakeredis
import httpx
import pytest
import redis.asyncio
import uvicorn

from app.auth.tickets import issue
from app.realtime.app import app
from app.realtime.app import _uid_connections


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _subscribers(client, channel: str) -> int:
    rows = client.pubsub_numsub(channel)
    if not rows:
        return 0
    return int(rows[0][1])


@pytest.fixture()
def realtime(monkeypatch):
    fake = fakeredis.FakeServer()
    sync = fakeredis.FakeRedis(server=fake)

    def from_url(*args, **kwargs):
        return fakeredis.aioredis.FakeRedis(server=fake)

    monkeypatch.setenv("REDIS_URL", "redis://fake:6379/0")
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.setenv("PROVIDER", "fake")
    _uid_connections.clear()
    monkeypatch.setattr(redis.asyncio, "from_url", from_url)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio", lifespan="on", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    health = None
    while time.monotonic() < deadline:
        try:
            health = httpx.get(f"http://127.0.0.1:{port}/health", timeout=0.2)
            if health.status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=2)
        raise RuntimeError(f"realtime server did not start: {health}")
    try:
        yield {"url": f"http://127.0.0.1:{port}", "redis": sync, "server": fake, "hub": app.state.hub}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def _next_line(iterator, timeout: float):
    return await asyncio.wait_for(iterator.__anext__(), timeout)


@pytest.mark.asyncio
def _ticket(uid=None):
    uid = uid or uuid.uuid4()
    ticket, _exp = issue(uid)
    return uid, ticket


async def test_subscribed_then_update(realtime):
    job_id = str(uuid.uuid4())
    uid, ticket = _ticket()
    payload = {
        "type": "job",
        "id": job_id,
        "status": "translating",
        "done_segments": 1,
        "total_segments": 3,
        "cost_cents": 1.5,
        "error_code": None,
        "owner_id": str(uid),
        "seq": 123,
    }
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", f"{realtime['url']}/events", params={"ticket": ticket}) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
            iterator = response.aiter_lines()
            events = []
            body = None
            retry = None
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and body is None:
                line = await _next_line(iterator, 1)
                if line.startswith("retry:"):
                    retry = int(line.split(":", 1)[1].strip())
                elif line.startswith("event:"):
                    events.append(line.split(":", 1)[1].strip())
                    if events[-1] == "subscribed":
                        realtime["redis"].publish(f"user:{uid}", json.dumps(payload))
                elif line.startswith("data:") and events and events[-1] == "update":
                    body = json.loads(line.split(":", 1)[1].strip())
    assert retry is not None and 3000 <= retry <= 8000
    assert events[0] == "subscribed"
    assert "update" in events
    assert events.index("subscribed") < events.index("update")
    assert body == payload


@pytest.mark.asyncio
async def test_one_subscription_for_two_clients(realtime):
    uid, ticket = _ticket()
    channel = f"user:{uid}"
    url = f"{realtime['url']}/events?ticket={ticket}"

    async def hold(started: asyncio.Event):
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url) as response:
                async for line in response.aiter_lines():
                    if line.startswith("event:") and line.split(":", 1)[1].strip() == "subscribed":
                        started.set()

    first = asyncio.Event()
    second = asyncio.Event()
    tasks = [asyncio.create_task(hold(first)), asyncio.create_task(hold(second))]
    await asyncio.wait_for(first.wait(), 3)
    await asyncio.wait_for(second.wait(), 3)
    assert _subscribers(realtime["redis"], channel) == 1
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if _subscribers(realtime["redis"], channel) == 0:
            break
        await asyncio.sleep(0.05)
    assert _subscribers(realtime["redis"], channel) == 0


def test_eleventh_connection_is_limited(realtime):
    _uid, ticket = _ticket()
    url = f"{realtime['url']}/events"
    held = []
    try:
        for _ in range(10):
            client = httpx.Client(timeout=None)
            response = client.send(client.build_request("GET", url, params={"ticket": ticket}), stream=True)
            assert response.status_code == 200
            held.append((client, response))
        blocked = httpx.get(url, params={"ticket": ticket}, timeout=2)
        assert blocked.status_code == 429
        assert blocked.json()["code"] == "too_many_attempts"
    finally:
        for client, response in held:
            response.close()
            client.close()


def test_rejects_a_missing_or_bad_ticket(realtime):
    missing = httpx.get(f"{realtime['url']}/events", timeout=2)
    assert missing.status_code == 401
    assert missing.headers["content-type"].startswith("application/json")
    assert missing.json()["code"] == "unauthorized"
    invalid = httpx.get(f"{realtime['url']}/events", params={"ticket": "not-a-ticket"}, timeout=2)
    assert invalid.status_code == 401
    assert invalid.headers["content-type"].startswith("application/json")
    assert invalid.json()["code"] == "unauthorized"


def test_import_does_not_load_sqlalchemy():
    backend = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend) + os.pathsep + env.get("PYTHONPATH", "")
    script = (
        "import sys\n"
        "import app.realtime.app\n"
        "assert 'sqlalchemy' not in sys.modules, sorted(name for name in sys.modules if 'sqlalchemy' in name)\n"
        "assert 'app.db' not in sys.modules\n"
        "assert 'app.core.service' not in sys.modules\n"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=backend, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_replaced_redis_connection_sends_resync(realtime):
    _uid, ticket = _ticket()
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", f"{realtime['url']}/events", params={"ticket": ticket}) as response:
            iterator = response.aiter_lines()
            events = []
            deadline = time.monotonic() + 4
            replaced = False
            while time.monotonic() < deadline and "resync" not in events:
                line = await _next_line(iterator, 1)
                if not line.startswith("event:"):
                    continue
                name = line.split(":", 1)[1].strip()
                events.append(name)
                if name == "subscribed" and not replaced:
                    realtime["hub"].redis = object()
                    replaced = True
    assert events[0] == "subscribed"
    assert "resync" in events


@pytest.mark.asyncio
async def test_ping_and_metrics(realtime, monkeypatch):
    monkeypatch.setattr("app.realtime.app.PING_INTERVAL_SECONDS", 0.3)
    _uid, ticket = _ticket()
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", f"{realtime['url']}/events", params={"ticket": ticket}) as response:
            iterator = response.aiter_lines()
            saw_ping = False
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not saw_ping:
                line = await _next_line(iterator, 1)
                if line.startswith(": ping"):
                    saw_ping = True
    assert saw_ping
    metrics = httpx.get(f"{realtime['url']}/metrics", timeout=2)
    assert metrics.status_code == 200
    assert "realtime_connections" in metrics.text
    assert "realtime_channels" in metrics.text
    assert "realtime_messages_total" in metrics.text
