from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import anyio
import httpx

_TERMINAL = {
    "document": {"ready", "failed"},
    "job": {"done", "failed", "cancelled"},
}
_CONNECT_ERRORS = (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError)
_BACKOFF_START = 0.5
_BACKOFF_MAX = 5.0
_MAX_ATTEMPTS = 10
_SILENCE_SECONDS = 30.0
_POLL_SECONDS = 2.0
_EOF = object()


class ApiError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _status(kind: str, payload: dict) -> str | None:
    key = "analysis_status" if kind == "document" else "status"
    value = payload.get(key)
    if value is None:
        return None
    return str(value)


def _terminal(kind: str, status: str | None) -> bool:
    return status in _TERMINAL.get(kind, set())


def _seq(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _apply(kind: str, state: dict | None, current_seq: int | None, message: dict) -> tuple[dict | None, int | None, bool]:
    if not isinstance(message, dict):
        return state, current_seq, False
    incoming = message.get("type")
    if incoming and incoming != kind:
        return state, current_seq, False
    incoming_status = _status(kind, message)
    current_status = _status(kind, state or {})
    incoming_terminal = _terminal(kind, incoming_status)
    current_terminal = _terminal(kind, current_status)
    if current_terminal and not incoming_terminal:
        return state, current_seq, False
    seq = _seq(message.get("seq"))
    newer = seq is None or current_seq is None or seq > current_seq
    if not newer and not (incoming_terminal and not current_terminal):
        return state, current_seq, False
    merged = dict(state or {})
    for key, value in message.items():
        if key == "type":
            continue
        merged[key] = value
    if seq is not None and (current_seq is None or seq > current_seq):
        current_seq = seq
    return merged, current_seq, True


def _merge_rest(state: dict | None, fresh: dict, current_seq: int | None) -> tuple[dict, int | None]:
    merged = dict(fresh)
    seq = current_seq
    if seq is None and state is not None:
        seq = _seq(state.get("seq"))
    if seq is not None:
        merged["seq"] = seq
    return merged, seq


class ApiClient:
    def __init__(self, base_url: str, realtime_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.realtime_url = realtime_url.rstrip("/")
        if token is None:
            token = os.environ.get("TRANSLATOR_API_TOKEN", "").strip()
        self.token = token or None
        self._poll_only = False
        self._http: httpx.AsyncClient | None = None
        self._realtime: httpx.AsyncClient | None = None

    async def __aenter__(self) -> ApiClient:
        headers: dict[str, str] = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        timeout = httpx.Timeout(60.0, read=None)
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers=headers)
        self._realtime = httpx.AsyncClient(base_url=self.realtime_url, timeout=timeout, headers=headers)
        return self

    async def __aexit__(self, *_exc) -> None:
        if self._http is not None:
            await self._http.aclose()
        if self._realtime is not None:
            await self._realtime.aclose()

    def _check(self, response: httpx.Response, text: str | None = None) -> None:
        if response.status_code < 400:
            return
        body_text = response.text if text is None else text
        try:
            body = response.json() if text is None else json.loads(body_text)
        except Exception:
            raise ApiError(
                f"http_{response.status_code}",
                body_text or response.reason_phrase,
                response.status_code >= 500,
            ) from None
        if not isinstance(body, dict):
            raise ApiError(f"http_{response.status_code}", body_text or str(body), response.status_code >= 500)
        raise ApiError(
            str(body.get("code") or f"http_{response.status_code}"),
            str(body.get("message") or body_text or response.reason_phrase),
            bool(body.get("retryable")),
        )

    async def upload(self, path: str) -> dict:
        file_path = Path(path)
        response = await self._http.post("/api/documents", files={"file": (file_path.name, file_path.read_bytes())})
        self._check(response)
        return response.json()

    async def get_document(self, document_id: str) -> dict:
        return await self._get_visible(f"/api/documents/{document_id}")

    async def create_job(self, document_id: str, target: str, source: str | None) -> dict:
        payload: dict[str, str] = {"document_id": document_id, "target_lang": target}
        if source:
            payload["source_lang"] = source
        response = await self._http.post(
            "/api/jobs",
            json=payload,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        self._check(response)
        return response.json()

    async def _get_visible(self, path: str) -> dict:
        last: ApiError | None = None
        for attempt in range(8):
            response = await self._http.get(path)
            try:
                self._check(response)
            except ApiError as exc:
                if exc.code != "not_found" or attempt == 7:
                    raise
                last = exc
                await anyio.sleep(0.05)
                continue
            return response.json()
        assert last is not None
        raise last

    async def get_job(self, job_id: str) -> dict:
        return await self._get_visible(f"/api/jobs/{job_id}")

    async def download(self, job_id: str) -> bytes:
        response = await self._http.get(f"/api/jobs/{job_id}/download")
        self._check(response)
        return response.content

    async def _read(self, kind: str, ident: str) -> dict:
        if kind == "document":
            return await self.get_document(ident)
        return await self.get_job(ident)

    async def _poll(self, kind: str, ident: str) -> AsyncIterator[dict]:
        while True:
            yield await self._read(kind, ident)
            await anyio.sleep(_POLL_SECONDS)

    async def _watch_connected(self, kind: str, ident: str) -> AsyncIterator[dict]:
        assert self._realtime is not None
        async with self._realtime.stream("GET", "/events", params={kind: ident}) as response:
            if response.status_code >= 400:
                raw = await response.aread()
                self._check(response, raw.decode(errors="replace"))
                return
            queue: asyncio.Queue = asyncio.Queue()

            async def pump() -> None:
                try:
                    async for line in response.aiter_lines():
                        await queue.put(line)
                    await queue.put(_EOF)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await queue.put(exc)

            pump_task = asyncio.create_task(pump())
            subscribed = False
            current_seq: int | None = None
            state: dict | None = None
            pending_event = ""
            data_lines: list[str] = []
            last_event = time.monotonic()
            try:
                while True:
                    remaining = _SILENCE_SECONDS - (time.monotonic() - last_event)
                    if remaining <= 0:
                        if subscribed:
                            fresh = await self._read(kind, ident)
                            state, current_seq = _merge_rest(state, fresh, current_seq)
                            yield state
                        else:
                            raise httpx.ReadError("timed out waiting for subscribed")
                        last_event = time.monotonic()
                        continue
                    with anyio.move_on_after(remaining) as scope:
                        item = await queue.get()
                    if scope.cancelled_caught:
                        if subscribed:
                            fresh = await self._read(kind, ident)
                            state, current_seq = _merge_rest(state, fresh, current_seq)
                            yield state
                            last_event = time.monotonic()
                            continue
                        raise httpx.ReadError("timed out waiting for subscribed")
                    if isinstance(item, Exception):
                        raise item
                    if item is _EOF:
                        raise httpx.RemoteProtocolError("event stream closed")
                    line = str(item).rstrip("\r")
                    if line == "":
                        name = pending_event
                        payload = "\n".join(data_lines).strip()
                        pending_event = ""
                        data_lines = []
                        if not name and not payload:
                            continue
                        if name == "subscribed":
                            subscribed = True
                            fresh = await self._read(kind, ident)
                            state, current_seq = _merge_rest(None, fresh, None)
                            last_event = time.monotonic()
                            yield state
                        elif name == "update":
                            last_event = time.monotonic()
                            if not payload:
                                continue
                            try:
                                message = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            state, current_seq, applied = _apply(kind, state, current_seq, message)
                            if applied and state is not None:
                                yield state
                        elif name == "resync":
                            last_event = time.monotonic()
                            fresh = await self._read(kind, ident)
                            state, current_seq = _merge_rest(state, fresh, current_seq)
                            yield state
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        pending_event = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].lstrip())
            finally:
                pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pump_task

    async def _watch(self, kind: str, ident: str) -> AsyncIterator[dict]:
        if self._poll_only:
            async for state in self._poll(kind, ident):
                yield state
            return
        delay = _BACKOFF_START
        for attempt in range(_MAX_ATTEMPTS):
            delivered = False
            try:
                async for state in self._watch_connected(kind, ident):
                    delivered = True
                    yield state
                return
            except _CONNECT_ERRORS as exc:
                unreachable = isinstance(exc, httpx.ConnectError) and not delivered and attempt == 0
                if unreachable or attempt == _MAX_ATTEMPTS - 1:
                    self._poll_only = True
                    async for state in self._poll(kind, ident):
                        yield state
                    return
                await anyio.sleep(delay)
                delay = min(delay * 2, _BACKOFF_MAX)

    async def wait_document(self, document_id: str) -> dict:
        async for state in self._watch("document", document_id):
            if not _terminal("document", _status("document", state)):
                continue
            document = await self.get_document(document_id)
            current = _status("document", document)
            if current == "ready":
                return document
            if current == "failed":
                raise ApiError(
                    str(document.get("error_code") or "analysis_failed"),
                    str(document.get("error_detail") or "Analysis failed."),
                    False,
                )
        raise ApiError("analysis_failed", "Analysis ended before the document was ready.", False)

    async def wait_job(
        self,
        job_id: str,
        on_progress: Callable[[int, int, str], Awaitable[None]] | None = None,
    ) -> dict:
        async for state in self._watch("job", job_id):
            done = int(state.get("done_segments") or 0)
            total = int(state.get("total_segments") or 0) or 1
            status = _status("job", state) or ""
            if on_progress is not None:
                await on_progress(done, total, status)
            if not _terminal("job", status):
                continue
            job = await self.get_job(job_id)
            current = _status("job", job)
            if not _terminal("job", current):
                continue
            if current != "done":
                raise ApiError(
                    str(job.get("error_code") or "job_failed"),
                    str(job.get("error_detail") or current or "Translation failed."),
                    False,
                )
            return job
        raise ApiError("job_failed", "Translation ended before the job finished.", False)
