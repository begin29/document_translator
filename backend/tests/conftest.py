import os
from pathlib import Path

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

import pytest

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
    yield
    get_settings.cache_clear()
    reset_engine()
    reset_runtime()
