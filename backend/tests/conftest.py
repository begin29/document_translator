import os
import uuid
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent / "_tmp"
ROOT.mkdir(exist_ok=True)
os.environ.setdefault("FILES_DIR", str(ROOT))
os.environ.setdefault("PROVIDER", "fake")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("FAKE_FAIL_MODE", "")
os.environ.setdefault("FAKE_FAIL_SEGMENTS", "")
os.environ.setdefault("FAKE_LATENCY_MS", "0")
os.environ.setdefault("CRASH_AFTER_SEGMENT", "0")
os.environ.setdefault("MAX_JOB_COST_CENTS", "2000")
os.environ.setdefault("AUTH_SECRET", "test-secret")
os.environ.setdefault("PUBLIC_ORIGIN", "http://localhost:8080")
os.environ.setdefault("ALLOW_SIGNUP", "true")

import pytest

from app.auth.throttle import reset_login_limit
from app.config import get_settings
from app.core.provider.fake_provider import reset_crash_memory
from app.core.service import reset_runtime
from app.db.session import reset_engine


@pytest.fixture(autouse=True)
def _fresh_settings():
    get_settings.cache_clear()
    reset_engine()
    reset_runtime()
    reset_crash_memory()
    reset_login_limit()
    yield
    get_settings.cache_clear()
    reset_engine()
    reset_runtime()
    reset_login_limit()


@pytest.fixture
def make_user(session):
    from app.auth.passwords import hash_password
    from app.db.models import User

    def _make(email: str | None = None, password: str = "password1234", is_admin: bool = False):
        address = (email or f"user-{uuid.uuid4()}@example.com").strip().lower()
        user = User(email=address, password_hash=hash_password(password), is_admin=is_admin)
        session.add(user)
        session.commit()
        return SimpleNamespace(id=user.id, email=user.email, password=password, is_admin=is_admin)

    return _make


@pytest.fixture
def auth_client(make_user):
    from fastapi.testclient import TestClient

    from app.main import app

    def _client(user=None, origin: str | None = None):
        account = user or make_user()
        chosen = origin or get_settings().public_origin
        client = TestClient(app)
        response = client.post(
            "/api/auth/login",
            json={"email": account.email, "password": account.password},
            headers={"Origin": chosen},
        )
        assert response.status_code == 200, response.text
        client.headers.update({"Origin": chosen})
        return client

    return _client


@pytest.fixture
def token_client(session):
    from fastapi.testclient import TestClient

    from app.auth.tokens import create_token
    from app.db.models import User
    from app.main import app

    def _client(user):
        row = session.get(User, user.id)
        raw = create_token(session, row, "test")
        session.commit()
        client = TestClient(app)
        client.headers.update({"Authorization": f"Bearer {raw}"})
        return client

    return _client
