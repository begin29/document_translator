from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from email_validator import EmailNotValidError, validate_email

from app.core.errors import AppError

_hasher = PasswordHasher()
_dummy: str | None = None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def dummy_password_hash() -> str:
    global _dummy
    if _dummy is None:
        _dummy = hash_password(secrets.token_urlsafe(32))
    return _dummy


def normalize_email(value: str) -> str:
    try:
        parsed = validate_email(value, check_deliverability=False)
    except EmailNotValidError as exc:
        raise AppError("invalid_email", "Enter a valid email address.", str(exc), False, 400) from None
    return parsed.normalized.lower()
