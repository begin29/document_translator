from __future__ import annotations

import hashlib
import hmac
import time
import uuid

from app.config import get_settings

TTL_SECONDS = 60


def issue(user_id: uuid.UUID) -> tuple[str, int]:
    uid = str(user_id)
    exp = int(time.time()) + TTL_SECONDS
    mac = hmac.new(
        get_settings().auth_secret.encode(),
        f"{uid}.{exp}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{uid}.{exp}.{mac}", exp


def verify(ticket: str | None) -> uuid.UUID | None:
    if not ticket:
        return None
    uid_text, sep, rest = ticket.partition(".")
    exp_text, sep2, mac = rest.partition(".")
    if not sep or not sep2 or not uid_text or not exp_text or not mac:
        return None
    expected = hmac.new(
        get_settings().auth_secret.encode(),
        f"{uid_text}.{exp_text}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, mac):
        return None
    try:
        exp = int(exp_text)
        uid = uuid.UUID(uid_text)
    except (ValueError, AttributeError, TypeError):
        return None
    if int(time.time()) >= exp:
        return None
    return uid
