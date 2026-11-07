from __future__ import annotations

import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.prompts import PROMPT_VERSION, system_prompt, user_prompt
from app.core.provider.base import TranslateRequest
from app.core.quality import flags_for
from app.db.models import Block, BlockTranslation, Segment, TranslationJob
from app.observability.logging import get_logger

log = get_logger()


def _agent_enabled() -> bool:
    return get_settings().provider == "openai"


def review_job(session: Session, job: TranslationJob, provider) -> bool:
    segments = session.scalars(select(Segment).where(Segment.job_id == job.id).order_by(Segment.idx)).all()
    flagged: list[tuple[Segment, list[str]]] = []
    for segment in segments:
        source, target = _segment_text(session, job, segment)
        reasons = flags_for(source, target, job.source_lang, job.target_lang, job.glossary)
        if reasons:
            segment.review_state = "flagged"
            flagged.append((segment, reasons))
        else:
            segment.review_state = "accepted"
    if not flagged:
        return False
    if _agent_enabled():
        try:
            ran = _run_agent(session, job, flagged, provider)
        except Exception as exc:
            log.bind(job_id=str(job.id), stage="review").warning("review_agent_failed", error=str(exc))
            from app.core.service import _event

            _event(session, job.id, "review_agent_failed", {"error": str(exc)[:500]}, level="warning")
            ran = False
        if ran:
            return _still_flagged(session, job, flagged)
    for segment, reasons in flagged:
        _retranslate(session, job, segment, provider, "; ".join(reasons))
    return _still_flagged(session, job, flagged)


def _still_flagged(session: Session, job: TranslationJob, flagged: list[tuple[Segment, list[str]]]) -> bool:
    for segment, _reasons in flagged:
        source, target = _segment_text(session, job, segment)
        if flags_for(source, target, job.source_lang, job.target_lang, job.glossary):
            segment.review_state = "flagged"
        elif segment.review_state != "accepted":
            segment.review_state = "retried"
    return True


def _segment_text(session: Session, job: TranslationJob, segment: Segment) -> tuple[str, str]:
    rows = session.scalars(
        select(Block).where(
            Block.document_id == job.document_id,
            Block.idx >= segment.block_start,
            Block.idx <= segment.block_end,
            Block.kind != "image",
        ).order_by(Block.idx)
    ).all()
    source_parts = []
    target_parts = []
    for row in rows:
        if not row.text:
            continue
        source_parts.append(row.text)
        translation = session.get(BlockTranslation, (job.id, row.idx))
        target_parts.append(translation.tgt_text if translation else "")
    return "\n".join(source_parts), "\n".join(target_parts)


def _retranslate(session: Session, job: TranslationJob, segment: Segment, provider, instructions: str) -> None:
    from app.core.service import _add_cost, _call_with_retries, _tm_put, _try_acquire, _release, _upsert_translation

    rows = session.scalars(
        select(Block).where(
            Block.document_id == job.document_id,
            Block.idx >= segment.block_start,
            Block.idx <= segment.block_end,
            Block.kind != "image",
        ).order_by(Block.idx)
    ).all()
    pairs = [(offset + 1, row.text or "") for offset, row in enumerate(rows) if row.text]
    if not pairs:
        return
    request = TranslateRequest(
        stage="review",
        model=get_settings().review_model,
        system=system_prompt(job.source_lang or "auto", job.target_lang, job.glossary, job.summary) + f"\nFix the previous translation. {instructions}",
        user=user_prompt(pairs),
        source_lang=job.source_lang or "",
        target_lang=job.target_lang,
        prompt_version=PROMPT_VERSION,
        attempt=segment.attempt + 1,
        segment_index=segment.idx,
        job_id=str(job.id),
        segment_id=str(segment.id),
        json_mode=True,
    )
    if not _try_acquire(600):
        return
    try:
        result = _call_with_retries(session, request)
    finally:
        _release()
    from app.core.numbered import parse_numbered_blocks, usable_translation

    found, _, _ = parse_numbered_blocks(result.text, [number for number, _ in pairs])
    text_rows = [row for row in rows if row.text]
    for number, row in zip([n for n, _ in pairs], text_rows):
        translated = usable_translation(found.get(number), row.text or "", job.target_lang)
        if translated is None:
            continue
        _upsert_translation(session, job.id, row.idx, translated, "agent", segment.id)
        _tm_put(session, row.src_hash, job.source_lang or "en", job.target_lang, translated, get_settings().review_model)
    _add_cost(session, job, get_settings().review_model, result.tokens_in, result.tokens_out)


class ReviewToolkit:
    def __init__(self, session: Session, job: TranslationJob, provider) -> None:
        self.session = session
        self.job = job
        self.provider = provider

    def get_source_segment(self, segment_idx: int) -> str:
        segment = self._segment(segment_idx)
        source, _target = _segment_text(self.session, self.job, segment)
        return source

    def get_neighbor_translations(self, segment_idx: int) -> str:
        parts = []
        for neighbor in (segment_idx - 1, segment_idx + 1):
            segment = self.session.scalar(select(Segment).where(Segment.job_id == self.job.id, Segment.idx == neighbor))
            if segment is None:
                continue
            _source, target = _segment_text(self.session, self.job, segment)
            parts.append(f"[{neighbor}] {target}")
        return "\n".join(parts) or "(no neighbors)"

    def lookup_glossary(self, term: str) -> str:
        for item in self.job.glossary or []:
            if term.lower() in (item.get("source") or "").lower():
                return item.get("target") or item.get("source") or ""
        return ""

    def retranslate_segment(self, segment_idx: int, instructions: str) -> str:
        segment = self._segment(segment_idx)
        _retranslate(self.session, self.job, segment, self.provider, instructions)
        segment.review_state = "retried"
        return "retranslated"

    def accept_segment(self, segment_idx: int) -> str:
        segment = self._segment(segment_idx)
        segment.review_state = "accepted"
        return "accepted"

    def view_source_page_image(self, page_no: int) -> str:
        from app.core.service import _page_png
        from app.db.models import Document

        document = self.session.get(Document, self.job.document_id)
        image = _page_png(document, page_no) if document else None
        if not image:
            return f"No source image for page {page_no}."
        return f"Source page image for page {page_no} is available ({len(image)} bytes)."

    def _segment(self, segment_idx: int) -> Segment:
        segment = self.session.scalar(select(Segment).where(Segment.job_id == self.job.id, Segment.idx == segment_idx))
        if segment is None:
            raise ValueError(f"segment {segment_idx} not found")
        return segment


def _clip(text: str, limit: int = 1500) -> str:
    return (text or "")[:limit]


def _flagged_prompt(session: Session, job: TranslationJob, flagged: list[tuple[Segment, list[str]]]) -> str:
    lines = ["Review these flagged segments:"]
    for segment, reasons in flagged:
        source, target = _segment_text(session, job, segment)
        lines.append(
            "\n".join(
                [
                    f"segment {segment.idx}",
                    f"reasons: {_clip(', '.join(reasons))}",
                    f"source: {_clip(source)}",
                    f"target: {_clip(target)}",
                ]
            )
        )
    return "\n".join(lines)


def _usage_counts(usage) -> tuple[int, int, int]:
    if usage is None:
        return 0, 0, 0
    if isinstance(usage, dict):
        details = usage.get("input_tokens_details") or {}
        cached = details.get("cached_tokens") if isinstance(details, dict) else getattr(details, "cached_tokens", 0)
        return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), int(cached or 0)
    details = getattr(usage, "input_tokens_details", None)
    if isinstance(details, dict):
        cached = details.get("cached_tokens") or 0
    else:
        cached = getattr(details, "cached_tokens", 0) if details is not None else 0
    return int(getattr(usage, "input_tokens", 0) or 0), int(getattr(usage, "output_tokens", 0) or 0), int(cached or 0)


def _raw_responses(source) -> list:
    direct = getattr(source, "raw_responses", None)
    if direct:
        return list(direct)
    details = getattr(source, "run_data", None)
    nested = getattr(details, "raw_responses", None) if details is not None else None
    if nested:
        return list(nested)
    return []


def _record_agent_usage(session: Session, job: TranslationJob, model: str, responses: list, started: float) -> None:
    if not responses:
        return
    from app.core.service import _add_cost, _record

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    share = elapsed_ms // len(responses)
    for response in responses:
        tokens_in, tokens_out, tokens_cached = _usage_counts(getattr(response, "usage", None))
        _record(
            stage="review_agent",
            model=model,
            job_id=job.id,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cached=tokens_cached,
            latency_ms=share,
            status="ok",
        )
        _add_cost(session, job, model, tokens_in, tokens_out)


def _run_agent(session: Session, job: TranslationJob, flagged: list[tuple[Segment, list[str]]], provider) -> bool:
    from app.core.service import _release, _try_acquire

    if not _try_acquire(2000):
        return False
    toolkit = ReviewToolkit(session, job, provider)
    spec_model = get_settings().review_model
    started = time.perf_counter()
    try:
        result = _run_agent_body(session, job, flagged, toolkit, spec_model)
        _record_agent_usage(session, job, spec_model, _raw_responses(result), started)
    except Exception as exc:
        _record_agent_usage(session, job, spec_model, _raw_responses(exc), started)
        raise
    finally:
        _release()
    return True


def _run_agent_body(session, job, flagged, toolkit, spec_model):
    from agents import Agent, ModelSettings, Runner, function_tool

    @function_tool
    def get_source_segment(segment_idx: int) -> str:
        """Return the source text for a segment."""
        return toolkit.get_source_segment(segment_idx)

    @function_tool
    def get_neighbor_translations(segment_idx: int) -> str:
        """Return translations of the previous and next segments."""
        return toolkit.get_neighbor_translations(segment_idx)

    @function_tool
    def lookup_glossary(term: str) -> str:
        """Look up a glossary term."""
        return toolkit.lookup_glossary(term)

    @function_tool
    def retranslate_segment(segment_idx: int, instructions: str) -> str:
        """Retranslate a flagged segment. Pass a short instruction describing the fix."""
        return toolkit.retranslate_segment(segment_idx, instructions)

    @function_tool
    def accept_segment(segment_idx: int) -> str:
        """Accept a segment as good enough."""
        return toolkit.accept_segment(segment_idx)

    @function_tool
    def view_source_page_image(page_no: int) -> str:
        """Check whether the source page image exists for an OCR'd page."""
        return toolkit.view_source_page_image(page_no)

    agent = Agent(
        name="Translation reviewer",
        instructions=(
            "You review flagged translation segments. For each flagged segment, inspect the source, "
            "neighbors, and glossary, then either retranslate_segment with a concrete instruction or accept_segment. "
            "Do not retranslate a segment that is already acceptable. Bulk translation is not your job."
        ),
        tools=[
            get_source_segment,
            get_neighbor_translations,
            lookup_glossary,
            retranslate_segment,
            accept_segment,
            view_source_page_image,
        ],
        model=spec_model,
        model_settings=ModelSettings(temperature=0),
    )
    return Runner.run_sync(
        agent,
        _flagged_prompt(session, job, flagged),
        max_turns=4 + 2 * len(flagged),
    )
