from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from decimal import Decimal

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.config import get_settings
from app.observability.logging import get_logger

log = get_logger()

_client = None
_client_key: tuple[str, bool] | None = None
_sent: dict[str, tuple[float, str]] = {}
_sent_lock = threading.Lock()
_THROTTLE_SECONDS = 0.5
_TERMINAL_JOB = {"done", "failed", "cancelled"}


def _redis():
    global _client, _client_key
    settings = get_settings()
    if not settings.redis_url:
        return None
    key = (settings.redis_url, bool(settings.redis_cluster))
    if _client is None or _client_key != key:
        import redis

        _client = redis.Redis.from_url(
            settings.redis_url,
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
        )
        _client_key = key
    return _client


def channels_for(message: dict) -> list[str]:
    kind = message.get("type")
    ident = message.get("id")
    if kind in {"job", "document"} and ident:
        return [f"{kind}:{ident}"]
    return []


def _now() -> float:
    return time.monotonic()


def _allowed(job_id: str, status: str) -> bool:
    now = _now()
    with _sent_lock:
        if status in _TERMINAL_JOB:
            _sent.pop(job_id, None)
            return True
        previous = _sent.get(job_id)
        if previous is not None:
            sent_at, sent_status = previous
            if status == sent_status and now - sent_at < _THROTTLE_SECONDS:
                return False
        _sent[job_id] = (now, status)
        return True


def _text(value) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _int(value) -> int:
    if value is None or value == "":
        return 0
    return int(value)


def _float(value) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _optional_text(value):
    if value is None:
        return None
    return _text(value)


def publish_job(state: dict) -> None:
    status = _optional_text(state.get("status"))
    message = {
        "type": "job",
        "id": _text(state.get("id")),
        "status": status,
        "done_segments": _int(state.get("done_segments")),
        "total_segments": _int(state.get("total_segments")),
        "cost_cents": _float(state.get("cost_cents")),
        "error_code": _optional_text(state.get("error_code")),
        "seq": time.time_ns() // 1_000_000,
    }
    _emit(message, job_id=message["id"], status=status or "")


def publish_document(state: dict) -> None:
    message = {
        "type": "document",
        "id": _text(state.get("id")),
        "analysis_status": _optional_text(state.get("analysis_status")),
        "error_code": _optional_text(state.get("error_code")),
        "seq": time.time_ns() // 1_000_000,
    }
    _emit(message, job_id=None, status="")


def _spublish(client, channel: str, payload: str) -> None:
    send = getattr(client, "spublish", None)
    if send is None:
        client.execute_command("SPUBLISH", channel, payload)
        return
    send(channel, payload)


def _emit(message: dict, *, job_id: str | None, status: str) -> None:
    try:
        client = _redis()
        if client is None:
            return
        if job_id is not None and not _allowed(job_id, status):
            return
        payload = json.dumps(message, separators=(",", ":"))
        cluster = get_settings().redis_cluster
        for channel in channels_for(message):
            if cluster:
                _spublish(client, channel, payload)
            else:
                client.publish(channel, payload)
    except Exception as exc:
        log.warning("event_publish_failed", error=type(exc).__name__)


def after_commit(session: Session, fn: Callable[[], None]) -> None:
    state = {"done": False}

    def _publish(_session: Session) -> None:
        state["done"] = True
        fn()

    def _cancel(_session: Session) -> None:
        if state["done"]:
            return
        state["done"] = True
        event.remove(session, "after_commit", _publish)

    event.listen(session, "after_commit", _publish, once=True)
    event.listen(session, "after_rollback", _cancel, once=True)
