from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.deps import current_user, get_session
from app.auth.passwords import dummy_password_hash, hash_password, normalize_email, password_needs_rehash, verify_password
from app.auth.sessions import create_session, revoke
from app.auth.throttle import clear_failures, failure_count, record_failure
from app.auth.tickets import issue
from app.auth.tokens import create_token, list_tokens, revoke_token
from app.config import get_settings
from app.core.errors import AppError
from app.db.models import ApiToken, User

router = APIRouter()


class Credentials(BaseModel):
    email: str
    password: str


class TokenIn(BaseModel):
    name: str


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded.strip():
        return forwarded.split(",")[0].strip() or "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _set_cookie(response: Response, raw: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key="st_session",
        value=raw,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
        max_age=settings.session_days * 24 * 60 * 60,
    )


def _clear_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(
        key="st_session",
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def _stamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _token_row(row) -> dict:
    return {
        "id": str(row.id),
        "name": row.name,
        "prefix": row.prefix,
        "created_at": _stamp(row.created_at),
        "last_used_at": _stamp(row.last_used_at),
        "revoked_at": _stamp(row.revoked_at),
    }


@router.post("/auth/register")
def register(body: Credentials, response: Response, session: Session = Depends(get_session)):
    if not get_settings().allow_signup:
        raise AppError("signup_disabled", "Sign up is disabled.", "", False, 403)
    email = normalize_email(body.email)
    if len(body.password) < 10:
        raise AppError("weak_password", "Password must be at least 10 characters.", "", False, 400)
    user = User(email=email, password_hash=hash_password(body.password), is_admin=False)
    session.add(user)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise AppError("email_taken", "An account with that email already exists.", email, False, 409) from None
    raw = create_session(session, user)
    _set_cookie(response, raw)
    return {"id": str(user.id), "email": user.email}


@router.post("/auth/login")
def login(body: Credentials, request: Request, response: Response, session: Session = Depends(get_session)):
    email = body.email.strip().lower()
    ip = _client_ip(request)
    count = failure_count(email, ip)
    if count is not None and count >= 5:
        raise AppError("too_many_attempts", "Too many attempts. Wait a few minutes and try again.", "", True, 429)
    user = session.scalar(select(User).where(User.email == email))
    if user is None:
        verify_password(dummy_password_hash(), body.password)
        ok = False
    else:
        ok = verify_password(user.password_hash, body.password)
        if ok and password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(body.password)
    if not ok or user is None:
        record_failure(email, ip)
        raise AppError("invalid_credentials", "The email or password is incorrect.", "", False, 401)
    clear_failures(email, ip)
    raw = create_session(session, user)
    _set_cookie(response, raw)
    return {"id": str(user.id), "email": user.email, "is_admin": user.is_admin}


@router.post("/auth/logout")
def logout(request: Request, response: Response, session: Session = Depends(get_session)):
    revoke(session, request.cookies.get("st_session"))
    _clear_cookie(response)
    return {"ok": True}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": str(user.id), "email": user.email, "is_admin": user.is_admin}


@router.get("/auth/tokens")
def tokens(session: Session = Depends(get_session), user: User = Depends(current_user)):
    return [_token_row(row) for row in list_tokens(session, user)]


@router.post("/auth/tokens")
def post_token(body: TokenIn, session: Session = Depends(get_session), user: User = Depends(current_user)):
    raw = create_token(session, user, body.name)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    stored = session.scalar(select(ApiToken).where(ApiToken.token_hash == digest))
    session.refresh(stored)
    payload = _token_row(stored)
    payload["token"] = raw
    return payload


@router.delete("/auth/tokens/{token_id}")
def delete_token(token_id: uuid.UUID, session: Session = Depends(get_session), user: User = Depends(current_user)):
    revoke_token(session, user, token_id)
    return {"ok": True}


@router.post("/events/ticket")
def event_ticket(user: User = Depends(current_user)):
    ticket, exp = issue(user.id)
    return {"ticket": ticket, "expires_at": datetime.fromtimestamp(exp, timezone.utc).isoformat()}
