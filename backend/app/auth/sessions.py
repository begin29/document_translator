from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Session as SessionRecord
from app.db.models import User


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_session(session: Session, user: User) -> str:
    raw = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    session.add(
        SessionRecord(
            id=_digest(raw),
            user_id=user.id,
            expires_at=now + timedelta(days=get_settings().session_days),
            last_seen_at=now,
        )
    )
    session.flush()
    return raw


def lookup(session: Session, raw: str | None) -> User | None:
    if not raw:
        return None
    row = session.get(SessionRecord, _digest(raw))
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    if _aware(row.expires_at) <= now:
        return None
    last = _aware(row.last_seen_at) if row.last_seen_at is not None else None
    if last is None or now - last >= timedelta(hours=1):
        row.last_seen_at = now
        row.expires_at = now + timedelta(days=get_settings().session_days)
    return session.get(User, row.user_id)


def revoke(session: Session, raw: str | None) -> None:
    if not raw:
        return
    row = session.get(SessionRecord, _digest(raw))
    if row is not None:
        session.delete(row)
