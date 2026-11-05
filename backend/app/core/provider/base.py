from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TranslateRequest:
    stage: str
    model: str
    system: str
    user: str
    source_lang: str
    target_lang: str
    prompt_version: str
    attempt: int = 1
    segment_index: int | None = None
    job_id: str | None = None
    segment_id: str | None = None
    page_no: int | None = None
    images: list[bytes] = field(default_factory=list)
    image_detail: str = "high"
    json_mode: bool = False
    temperature: float = 0


@dataclass
class TranslateResult:
    text: str
    tokens_in: int
    tokens_out: int
    tokens_cached: int
    model: str
    latency_ms: int
