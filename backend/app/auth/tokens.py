from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.db.models import ApiToken, User


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def create_token(session: Session, user: User, name: str) -> str:
    secret = secrets.token_urlsafe(32)
    raw = f"stk_{secret}"
    session.add(
        ApiToken(
            user_id=user.id,
            name=name,
            prefix=secret[:8],
            token_hash=_digest(raw),
        )
    )
    session.flush()
    return raw


def lookup(session: Session, raw: str | None) -> User | None:
    if not raw or not raw.startswith("stk_"):
        return None
    row = session.scalar(select(ApiToken).where(ApiToken.token_hash == _digest(raw)))
    if row is None or row.revoked_at is not None:
        return None
    now = datetime.now(timezone.utc)
    last = _aware(row.last_used_at) if row.last_used_at is not None else None
    if last is None or now - last >= timedelta(hours=1):
        row.last_used_at = now
    return session.get(User, row.user_id)


def list_tokens(session: Session, user: User) -> list[ApiToken]:
    return list(
        session.scalars(
            select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc())
        ).all()
    )


def revoke_token(session: Session, user: User, token_id: uuid.UUID) -> None:
    row = session.get(ApiToken, token_id)
    if row is None or row.user_id != user.id:
        raise AppError("not_found", "Token not found.", str(token_id), False, 404)
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
