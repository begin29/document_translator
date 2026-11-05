from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, generate_latest

JOBS = Gauge("translator_jobs", "Jobs by status", ["status"])
QUEUE_DEPTH = Gauge("translator_queue_depth", "Queued jobs")
SEGMENT_LATENCY = Histogram(
    "translator_segment_latency_seconds",
    "Segment stage latency",
    ["stage"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120),
)
TOKENS = Counter("provider_tokens_total", "Provider tokens", ["stage", "model", "direction"])
COST = Counter("provider_cost_cents_total", "Provider cost in cents", ["stage", "model"])
PROVIDER_ERRORS = Counter("provider_errors_total", "Provider errors", ["code"])
RATE_LIMITS = Counter("provider_rate_limits_total", "429 responses")
INFLIGHT = Gauge("provider_inflight", "Provider calls in flight")
HEARTBEAT = Gauge("worker_heartbeat", "Worker heartbeat unix time", ["worker_id"])


def render_metrics() -> bytes:
    return generate_latest()
