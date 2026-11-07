from __future__ import annotations

import os
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://translator:translator@localhost:5432/translator"
    files_dir: str = "./data"
    provider: str = "fake"
    openai_api_key: str = ""
    app_env: str = "dev"
    max_upload_mb: int = 40
    max_job_cost_cents: float = 2000
    provider_max_concurrency: int = 32
    provider_tpm_budget: int = 2_000_000
    job_max_concurrency: int = 16
    segment_attempt_limit: int = 3
    stale_seconds: int = 120
    default_model: str = "gpt-4o-mini"
    review_model: str = "gpt-4o"
    ocr_model: str = "gpt-4o"
    classify_model: str = "gpt-4o-mini"
    models_config: str = ""
    fake_latency_ms: int = 0
    fake_fail_mode: str = ""
    fake_fail_segments: str = ""
    fake_fail_rate: float = 1
    crash_after_segment: int = 0
    otel_exporter: str = "none"
    otel_endpoint: str = "http://langfuse:4318/v1/traces"
    agent_trace_exporter: str = "openai"
    cors_origins: str = "*"
    redis_url: str = ""
    redis_cluster: bool = False
    worker_id: str = ""
    global_quality_cap: int = 2000
    prompt_overhead_tokens: int = 800

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


def worker_identity() -> str:
    settings = get_settings()
    if settings.worker_id:
        return settings.worker_id
    return f"worker-{os.getpid()}"
