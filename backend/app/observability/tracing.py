from __future__ import annotations

from contextlib import contextmanager

from app.config import get_settings


def configure_tracing() -> None:
    settings = get_settings()
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
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint)))
    trace.set_tracer_provider(provider)


@contextmanager
def span(name: str, **attributes):
    tracer = None
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("stark-translator")
    except Exception:
        tracer = None
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


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
