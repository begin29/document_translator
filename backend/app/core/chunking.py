from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.domain import BlockData, block_hash
from app.core.languages import expansion_ratio
from app.core.model_registry import ModelSpec


@dataclass
class Chunk:
    blocks: list[BlockData]


def segment_budget(spec: ModelSpec, source_lang: str | None, target_lang: str | None, overhead_tokens: int, quality_cap: int) -> int:
    expansion = expansion_ratio(source_lang, target_lang)
    by_output = spec.max_output_tokens * 0.8 / max(expansion, 0.1)
    by_context = (spec.context_window - overhead_tokens) * 0.8 / (1 + expansion)
    cap = spec.segment_cap_tokens or quality_cap
    return max(32, int(min(by_output, by_context, cap)))


def chunk_blocks(blocks: list[BlockData], budget: int, count_tokens) -> list[Chunk]:
    pieces = _split_oversized(blocks, budget, count_tokens)
    groups: list[list[BlockData]] = []
    current: list[BlockData] = []
    used = 0
    for block in pieces:
        tokens = _tokens(block, count_tokens)
        if current and tokens and used + tokens > budget:
            groups.append(current)
            current = []
            used = 0
        current.append(block)
        used += tokens
    if current:
        groups.append(current)
    return [Chunk(blocks=group) for group in groups if group]


def _tokens(block: BlockData, count_tokens) -> int:
    if block.kind == "image" or not block.text:
        return 0
    return count_tokens(block.text)


def _split_oversized(blocks: list[BlockData], budget: int, count_tokens) -> list[BlockData]:
    output: list[BlockData] = []
    for block in blocks:
        if block.kind == "image" or not block.text or count_tokens(block.text) <= budget:
            output.append(block)
            continue
        parts = _split_text(block.text, budget, count_tokens)
        for offset, part in enumerate(parts):
            meta = dict(block.meta)
            meta["split_from_block"] = block.idx or block.meta.get("split_from_block") or f"{block.page}:{block.seq}"
            output.append(
                BlockData.make(
                    page=block.page,
                    seq=block.seq * 100 + offset,
                    kind=block.kind,
                    text=part,
                    asset_path=block.asset_path,
                    meta=meta,
                )
            )
    return output


def _split_text(text: str, budget: int, count_tokens) -> list[str]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?。])\s+", text) if part.strip()]
    if len(sentences) <= 1:
        return _hard_split(text, budget, count_tokens)
    parts: list[str] = []
    buf = ""
    for sentence in sentences:
        if count_tokens(sentence) > budget:
            if buf:
                parts.append(buf)
                buf = ""
            parts.extend(_hard_split(sentence, budget, count_tokens))
            continue
        candidate = f"{buf} {sentence}".strip() if buf else sentence
        if buf and count_tokens(candidate) > budget:
            parts.append(buf)
            buf = sentence
        else:
            buf = candidate
    if buf:
        parts.append(buf)
    return parts or [text]


def _hard_split(text: str, budget: int, count_tokens) -> list[str]:
    words = text.split()
    if not words:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    for word in words:
        candidate = " ".join(buf + [word])
        if buf and count_tokens(candidate) > budget:
            parts.append(" ".join(buf))
            buf = [word]
        else:
            buf.append(word)
    if buf:
        parts.append(" ".join(buf))
    checked: list[str] = []
    for part in parts:
        if count_tokens(part) <= budget or len(part) < 8:
            checked.append(part)
            continue
        step = max(1, len(part) // max(2, count_tokens(part) // max(budget, 1) + 1))
        for start in range(0, len(part), step):
            checked.append(part[start : start + step])
    return [item for item in checked if item.strip()]


def rechunk_for_model(blocks: list[BlockData], spec: ModelSpec, source: str | None, target: str | None, overhead: int, quality_cap: int, count_tokens) -> list[Chunk]:
    budget = segment_budget(spec, source, target, overhead, quality_cap)
    return chunk_blocks(blocks, budget, count_tokens)


def hash_of(text: str) -> str:
    return block_hash(text)
