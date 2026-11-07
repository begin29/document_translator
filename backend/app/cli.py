from __future__ import annotations

import argparse

from sqlalchemy import select, update

from app.auth.passwords import hash_password, normalize_email
from app.core.errors import AppError
from app.db.models import Document, TranslationJob, User
from app.db.session import session_factory


def _run(fn) -> None:
    session = session_factory()()
    try:
        result = fn(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
    return result


def create_user(email: str, password: str, admin: bool) -> None:
    address = normalize_email(email)
    if len(password) < 10:
        raise SystemExit("Password must be at least 10 characters.")

    def write(session) -> None:
        existing = session.scalar(select(User).where(User.email == address))
        if existing is not None:
            raise SystemExit(f"{address} already has an account.")
        session.add(User(email=address, password_hash=hash_password(password), is_admin=admin))

    _run(write)
    print(address)


def claim_orphans(email: str) -> None:
    address = normalize_email(email)

    def write(session):
        user = session.scalar(select(User).where(User.email == address))
        if user is None:
            raise SystemExit(f"No account for {address}.")
        owned = set(session.scalars(select(Document.sha256).where(Document.owner_id == user.id)).all())
        orphans = session.scalars(select(Document).where(Document.owner_id.is_(None))).all()
        claimed = 0
        for document in orphans:
            if document.sha256 in owned:
                continue
            document.owner_id = user.id
            owned.add(document.sha256)
            claimed += 1
        jobs = session.execute(update(TranslationJob).where(TranslationJob.owner_id.is_(None)).values(owner_id=user.id))
        return claimed, jobs.rowcount or 0

    claimed, jobs = _run(write)
    print(f"documents={claimed} jobs={jobs}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user")
    create.add_argument("--email", required=True)
    create.add_argument("--password", required=True)
    create.add_argument("--admin", action="store_true")
    claim = sub.add_parser("claim-orphans")
    claim.add_argument("--email", required=True)
    args = parser.parse_args()
    try:
        if args.command == "create-user":
            create_user(args.email, args.password, args.admin)
            return
        if args.command == "claim-orphans":
            claim_orphans(args.email)
    except AppError as exc:
        raise SystemExit(exc.message) from None


if __name__ == "__main__":
    main()
