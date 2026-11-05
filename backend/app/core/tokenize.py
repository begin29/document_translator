from __future__ import annotations

import tiktoken

from app.core.model_registry import ModelSpec

_ENCODERS: dict[str, tiktoken.Encoding] = {}


def encoder(spec: ModelSpec):
    if spec.tokenizer in _ENCODERS:
        return _ENCODERS[spec.tokenizer]
    try:
        _ENCODERS[spec.tokenizer] = tiktoken.get_encoding(spec.tokenizer)
    except Exception:
        _ENCODERS[spec.tokenizer] = None
    return _ENCODERS[spec.tokenizer]


def count_tokens(spec: ModelSpec, text: str) -> int:
    if not text:
        return 0
    enc = encoder(spec)
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text))
