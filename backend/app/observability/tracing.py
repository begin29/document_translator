from __future__ import annotations

import os
from contextlib import contextmanager

from app.config import Settings, get_settings

_client = None
_agents_instrumented = False

_GENERATION_NAMES = {
    "translate": "translate-blocks",
    "ocr": "ocr-page",
    "classify": "classify-page",
    "review": "review-retranslate",
    "summary": "summarize-document",
}


def _credential(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    return text


def langfuse_enabled(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return bool(
        settings.app_env != "test"
        and _credential(settings.langfuse_public_key)
        and _credential(settings.langfuse_secret_key)
    )


def generation_name(stage: str) -> str:
    return _GENERATION_NAMES.get(stage, stage or "complete")


def configure_tracing() -> None:
    settings = get_settings()
    if langfuse_enabled(settings):
        _start_langfuse(settings)
        return
    if settings.otel_exporter != "otlp":
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except Exception:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "stark-translator"}))
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=settings.otel_endpoint,
                headers=_export_headers(settings.otel_exporter_otlp_headers),
            )
        )
    )
    trace.set_tracer_provider(provider)


def flush_tracing() -> None:
    client = _active_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        return


@contextmanager
def span(
    name: str,
    *,
    trace_id: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    observation_input=None,
    as_type: str = "span",
    **attributes,
):
    client = _active_client()
    if client is None:
        yield None
        return
    from langfuse import propagate_attributes

    metadata = {key: value for key, value in attributes.items() if value is not None}
    propagated = {}
    if user_id:
        propagated["user_id"] = user_id
    if session_id:
        propagated["session_id"] = session_id
    if tags:
        propagated["tags"] = tags
    if metadata:
        propagated["metadata"] = metadata
    trace_context = None
    compact = _trace_id(trace_id)
    if compact:
        trace_context = {"trace_id": compact}
    entered = False
    try:
        with propagate_attributes(**propagated):
            with client.start_as_current_observation(
                name=name,
                as_type=as_type,
                input=observation_input,
                metadata=metadata or None,
                trace_context=trace_context,
            ) as current:
                entered = True
                yield current
    except Exception:
        if entered:
            raise
        yield None


def configure_agent_tracing() -> None:
    settings = get_settings()
    exporter = settings.agent_trace_exporter
    if exporter == "none" or (exporter == "openai" and not settings.openai_api_key):
        from agents import set_tracing_disabled

        set_tracing_disabled(True)
        return
    if exporter == "langsmith":
        try:
            from langsmith.integrations.openai_agents_sdk import OpenAIAgentsTracingProcessor
            from agents import set_trace_processors

            set_trace_processors([OpenAIAgentsTracingProcessor()])
        except Exception:
            return
        return
    if exporter == "otlp":
        return


def _start_langfuse(settings: Settings) -> None:
    global _client, _agents_instrumented

    public_key = _credential(settings.langfuse_public_key)
    secret_key = _credential(settings.langfuse_secret_key)
    base_url = _credential(settings.langfuse_base_url)
    os.environ["LANGFUSE_PUBLIC_KEY"] = public_key
    os.environ["LANGFUSE_SECRET_KEY"] = secret_key
    if base_url:
        os.environ["LANGFUSE_BASE_URL"] = base_url
    os.environ["LANGFUSE_TRACING_ENVIRONMENT"] = settings.app_env
    from langfuse import Langfuse

    _client = Langfuse(
        public_key=public_key,
        secret_key=secret_key,
        base_url=base_url or None,
        environment=settings.app_env,
    )
    if _agents_instrumented:
        return
    try:
        from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor

        OpenAIAgentsInstrumentor().instrument()
        _agents_instrumented = True
    except Exception:
        return


def _active_client():
    global _client
    if _client is not None:
        return _client
    settings = get_settings()
    if not langfuse_enabled(settings):
        return None
    _start_langfuse(settings)
    return _client


def _trace_id(value: str | None) -> str | None:
    if not value:
        return None
    compact = value.replace("-", "").lower()
    if len(compact) == 32 and all(character in "0123456789abcdef" for character in compact):
        return compact
    return None


def _export_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            headers[key] = value
    if not any(key.lower() == "x-langfuse-ingestion-version" for key in headers):
        headers["x-langfuse-ingestion-version"] = "4"
    return headers
