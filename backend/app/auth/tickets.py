from __future__ import annotations

import hashlib
import hmac
import time
import uuid

from app.config import get_settings

TTL_SECONDS = 60
TRANSFER_TTL_SECONDS = 15 * 60


def _mac(purpose: str, uid: str, subject: str, exp: str) -> str:
    return hmac.new(
        get_settings().auth_secret.encode(),
        f"{purpose}:{uid}.{subject}.{exp}".encode(),
        hashlib.sha256,
    ).hexdigest()


def issue(
    user_id: uuid.UUID,
    purpose: str = "events",
    ttl: int = TTL_SECONDS,
    subject: str = "",
) -> tuple[str, int]:
    uid = str(user_id)
    exp = int(time.time()) + ttl
    return f"{uid}.{exp}.{_mac(purpose, uid, subject, str(exp))}", exp


def verify(ticket: str | None, purpose: str = "events", subject: str = "") -> uuid.UUID | None:
    if not ticket:
        return None
    uid_text, sep, rest = ticket.partition(".")
    exp_text, sep2, mac = rest.partition(".")
    if not sep or not sep2 or not uid_text or not exp_text or not mac:
        return None
    if not hmac.compare_digest(_mac(purpose, uid_text, subject, exp_text), mac):
        return None
    try:
        exp = int(exp_text)
        uid = uuid.UUID(uid_text)
    except (ValueError, AttributeError, TypeError):
        return None
    if int(time.time()) >= exp:
        return None
    return uid
