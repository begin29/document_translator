from __future__ import annotations

from app.config import get_settings
from app.observability.logging import get_logger

log = get_logger()

WINDOW_SECONDS = 15 * 60
_client = None
_client_key: str | None = None


def reset_login_limit() -> None:
    global _client, _client_key
    _client = None
    _client_key = None


def _redis():
    global _client, _client_key
    url = get_settings().redis_url.strip()
    if not url:
        return None
    if _client is not None and _client_key == url:
        return _client
    try:
        import redis

        client = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        client.ping()
    except Exception:
        log.warning("login_rate_limit_unavailable")
        return None
    _client = client
    _client_key = url
    return client


def _key(email: str, ip: str) -> str:
    return f"login_fail:{email}:{ip}"


def failure_count(email: str, ip: str) -> int | None:
    client = _redis()
    if client is None:
        return None
    try:
        raw = client.get(_key(email, ip))
        return int(raw or 0)
    except Exception:
        log.warning("login_rate_limit_unavailable")
        return None


def record_failure(email: str, ip: str) -> None:
    client = _redis()
    if client is None:
        return
    key = _key(email, ip)
    try:
        count = int(client.incr(key))
        if count == 1:
            client.expire(key, WINDOW_SECONDS)
    except Exception:
        log.warning("login_rate_limit_unavailable")


def clear_failures(email: str, ip: str) -> None:
    client = _redis()
    if client is None:
        return
    try:
        client.delete(_key(email, ip))
    except Exception:
        log.warning("login_rate_limit_unavailable")
