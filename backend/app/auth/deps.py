from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth.sessions import lookup as lookup_session
from app.auth.tokens import lookup as lookup_token
from app.core.errors import AppError
from app.db.models import User
from app.db.session import session_factory


def get_session():
    factory = session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_user(request: Request, session: Session = Depends(get_session)) -> User:
    cookie = request.cookies.get("st_session")
    if cookie:
        user = lookup_session(session, cookie)
        if user is not None:
            request.state.user = user
            return user
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        raw = header[7:].strip()
        if raw.startswith("stk_"):
            user = lookup_token(session, raw)
            if user is not None:
                request.state.user = user
                return user
    raise AppError("unauthorized", "Sign in to continue.", "", False, 401)


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise AppError("not_found", "Not found.", "", False, 404)
    return user
