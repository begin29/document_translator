from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from prometheus_client import Counter, Gauge, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

REALTIME_CONNECTIONS = Gauge("realtime_connections", "Open SSE connections")
REALTIME_CHANNELS = Gauge("realtime_channels", "Redis channels with local listeners")
REALTIME_MESSAGES = Counter("realtime_messages_total", "Messages received from Redis")
PING_INTERVAL_SECONDS = 15
_POLL_SECONDS = 0.2
_BACKOFF_START = 0.5
_BACKOFF_MAX = 10.0
_PING = object()


class SubscriptionError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def parse_subscriptions(request: Request) -> list[str]:
    jobs = request.query_params.getlist("job")
    documents = request.query_params.getlist("document")
    if not 1 <= len(jobs) + len(documents) <= 50:
        raise SubscriptionError("Provide between 1 and 50 job or document ids.")
    channels: list[str] = []
    seen: set[str] = set()
    for kind, values in (("job", jobs), ("document", documents)):
        for value in values:
            try:
                parsed = uuid.UUID(value)
            except (ValueError, AttributeError, TypeError) as exc:
                raise SubscriptionError("Job and document ids must be UUIDs.") from exc
            channel = f"{kind}:{parsed}"
            if channel not in seen:
                seen.add(channel)
                channels.append(channel)
    return channels


def _decode(value):
    if isinstance(value, bytes):
        return value.decode()
    return value


async def _close_quietly(obj) -> None:
    if obj is None:
        return
    for name in ("aclose", "close"):
        method = getattr(obj, name, None)
        if method is None:
            continue
        try:
            result = method()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            return
        return


class Hub:
    def __init__(self, url: str) -> None:
        self.url = url
        self.redis = None
        self.pubsub = None
        self.channels: dict[str, set[asyncio.Queue]] = {}
        self.connections = 0
        self._bound = None
        self._commands: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stopped = False

    async def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._close_clients()
        self._fail_pending(ConnectionError("realtime stopped"))

    async def _run(self) -> None:
        backoff = _BACKOFF_START
        announce = False
        while not self._stopped:
            try:
                await self._connect()
                if announce:
                    self._fanout({"type": "resync"})
                announce = False
                backoff = _BACKOFF_START
                await self._pump()
                announce = True
            except asyncio.CancelledError:
                raise
            except Exception:
                announce = True
                await self._close_clients()
                if self._stopped:
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_MAX)

    async def _connect(self) -> None:
        import redis.asyncio as aioredis

        client = aioredis.from_url(self.url, socket_connect_timeout=0.5)
        pubsub = client.pubsub()
        self.redis = client
        self.pubsub = pubsub
        self._bound = client
        names = list(self.channels)
        if names:
            await pubsub.subscribe(*names)

    async def _close_clients(self) -> None:
        pubsub = self.pubsub
        client = self.redis
        self.pubsub = None
        self.redis = None
        self._bound = None
        await _close_quietly(pubsub)
        await _close_quietly(client)

    async def _pump(self) -> None:
        pubsub = self.pubsub
        bound = self._bound
        while not self._stopped:
            if self.redis is not bound or self.pubsub is not pubsub or pubsub is None:
                raise ConnectionError("redis connection replaced")
            await self._apply_commands()
            if self.redis is not bound or self.pubsub is not pubsub:
                raise ConnectionError("redis connection replaced")
            if not pubsub.subscribed:
                await asyncio.sleep(_POLL_SECONDS)
                continue
            try:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=_POLL_SECONDS)
            except RuntimeError as exc:
                if "pubsub connection not set" not in str(exc):
                    raise
                await asyncio.sleep(_POLL_SECONDS)
                continue
            if self.redis is not bound or self.pubsub is not pubsub:
                raise ConnectionError("redis connection replaced")
            if message is None:
                continue
            self._on_message(message)

    async def _apply_commands(self) -> None:
        while True:
            try:
                kind, channels, future = self._commands.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if self.pubsub is None:
                    raise ConnectionError("redis unavailable")
                if channels:
                    if kind == "subscribe":
                        await self.pubsub.subscribe(*channels)
                    elif kind == "unsubscribe":
                        await self.pubsub.unsubscribe(*channels)
                if not future.done():
                    future.set_result(None)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)

    def _fail_pending(self, exc: Exception) -> None:
        while True:
            try:
                _kind, _channels, future = self._commands.get_nowait()
            except asyncio.QueueEmpty:
                return
            if not future.done():
                future.set_exception(exc)

    def _on_message(self, message: dict) -> None:
        if message.get("type") != "message":
            return
        channel = _decode(message.get("channel"))
        raw = _decode(message.get("data"))
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        REALTIME_MESSAGES.inc()
        for queue in list(self.channels.get(channel, ())):
            self._offer(queue, payload)

    def _fanout(self, payload: dict) -> None:
        for queues in self.channels.values():
            for queue in list(queues):
                self._offer(queue, payload)

    def _offer(self, queue: asyncio.Queue, payload) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            pass

    async def _command(self, kind: str, channels: list[str]) -> None:
        if not channels:
            return
        future = asyncio.get_running_loop().create_future()
        await self._commands.put((kind, channels, future))
        await future

    async def _detach(self, queue: asyncio.Queue, channels: list[str]) -> list[str]:
        stale: list[str] = []
        async with self._lock:
            for channel in channels:
                listeners = self.channels.get(channel)
                if not listeners:
                    continue
                listeners.discard(queue)
                if not listeners:
                    self.channels.pop(channel, None)
                    stale.append(channel)
        return stale

    @asynccontextmanager
    async def listen(self, channels: list[str]) -> AsyncIterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        async with self._lock:
            self.connections += 1
            fresh: list[str] = []
            for channel in channels:
                listeners = self.channels.setdefault(channel, set())
                if not listeners:
                    fresh.append(channel)
                listeners.add(queue)
        try:
            try:
                await self._command("subscribe", fresh)
            except Exception:
                await self._detach(queue, channels)
                raise
            try:
                yield queue
            finally:
                stale = await self._detach(queue, channels)
                with contextlib.suppress(Exception):
                    await self._command("unsubscribe", stale)
        finally:
            async with self._lock:
                self.connections -= 1


async def _next_item(queue: asyncio.Queue, request: Request):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PING_INTERVAL_SECONDS
    getter = asyncio.create_task(queue.get())
    try:
        while True:
            if await request.is_disconnected():
                return None
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _PING
            try:
                return await asyncio.wait_for(asyncio.shield(getter), min(_POLL_SECONDS, remaining))
            except asyncio.TimeoutError:
                continue
    finally:
        if not getter.done():
            getter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await getter


def _event_stream(request: Request, channels: list[str]):
    hub: Hub = request.app.state.hub

    async def stream():
        async with hub.listen(channels) as queue:
            yield f"retry: {random.randint(3000, 8000)}\n\n"
            yield "event: subscribed\ndata: {}\n\n"
            while True:
                item = await _next_item(queue, request)
                if item is None:
                    return
                if item is _PING:
                    yield ": ping\n\n"
                    continue
                if isinstance(item, dict) and item.get("type") == "resync":
                    yield "event: resync\ndata: {}\n\n"
                    continue
                yield "event: update\ndata: " + json.dumps(item, separators=(",", ":")) + "\n\n"

    return stream()


async def events(request: Request):
    try:
        channels = parse_subscriptions(request)
    except SubscriptionError as exc:
        return JSONResponse({"detail": exc.detail}, status_code=400)
    return StreamingResponse(
        _event_stream(request, channels),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def health(request: Request):
    hub: Hub = request.app.state.hub
    try:
        if hub.redis is None:
            raise ConnectionError("redis unavailable")
        pong = await hub.redis.ping()
        if not pong:
            raise ConnectionError("redis unavailable")
    except Exception:
        return JSONResponse({"status": "unavailable"}, status_code=503)
    return JSONResponse({"status": "ok"})


async def metrics(request: Request):
    hub: Hub = request.app.state.hub
    REALTIME_CONNECTIONS.set(hub.connections)
    REALTIME_CHANNELS.set(len(hub.channels))
    return Response(generate_latest(), media_type="text/plain; version=0.0.4")


@asynccontextmanager
async def lifespan(app: Starlette):
    hub = Hub(os.environ.get("REDIS_URL", ""))
    app.state.hub = hub
    await hub.start()
    try:
        yield
    finally:
        await hub.stop()


app = Starlette(
    routes=[
        Route("/events", events, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Route("/metrics", metrics, methods=["GET"]),
    ],
    lifespan=lifespan,
)
