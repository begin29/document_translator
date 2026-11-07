from __future__ import annotations

import random
import re
import time

from app.config import get_settings
from app.core.errors import InjectedCrash, ProviderFailure, ProviderTimeout, RateLimited
from app.core.provider.base import TranslateRequest, TranslateResult

_crashed_segments: set[int] = set()


def reset_crash_memory() -> None:
    _crashed_segments.clear()


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[TranslateRequest] = []

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4) if text else 0

    def translate(self, request: TranslateRequest) -> TranslateResult:
        settings = get_settings()
        self.calls.append(request)
        if settings.fake_latency_ms:
            time.sleep(settings.fake_latency_ms / 1000)
        started = time.perf_counter()
        self._maybe_fail(request, settings)
        self._maybe_crash(request, settings)
        text = self._render(request)
        tokens_in = self.count_tokens(request.system) + self.count_tokens(request.user) + (120 if request.images else 0)
        tokens_out = self.count_tokens(text)
        elapsed = int((time.perf_counter() - started) * 1000)
        cached = 0 if request.attempt == 1 and request.segment_index in (None, 0) else min(tokens_in, 400)
        return TranslateResult(
            text=text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cached=cached,
            model=request.model,
            latency_ms=elapsed,
        )

    def _maybe_fail(self, request: TranslateRequest, settings) -> None:
        mode = (settings.fake_fail_mode or "").strip()
        if not mode or request.stage not in {"translate", "review"}:
            return
        selected = _segments(settings.fake_fail_segments)
        if selected and request.segment_index not in selected:
            return
        if request.attempt > 1 and mode in {"http500", "rate_limit"}:
            return
        if settings.fake_fail_rate < 1 and random.random() > settings.fake_fail_rate:
            return
        if mode == "http500":
            raise ProviderFailure("injected http 500")
        if mode == "timeout":
            raise ProviderTimeout("injected timeout")
        if mode == "rate_limit":
            raise RateLimited(0.05, "injected 429")
        if mode == "untranslated":
            return

    def _maybe_crash(self, request: TranslateRequest, settings) -> None:
        target = settings.crash_after_segment
        if not target or request.stage != "translate" or request.segment_index is None:
            return
        if request.segment_index == target and request.segment_index not in _crashed_segments:
            _crashed_segments.add(request.segment_index)
            raise InjectedCrash(f"crash before segment {target}")

    def _render(self, request: TranslateRequest) -> str:
        settings = get_settings()
        if settings.fake_fail_mode == "untranslated" and request.stage == "translate" and request.attempt == 1:
            selected = _segments(settings.fake_fail_segments)
            if not selected or request.segment_index in selected:
                return request.user
        if request.stage == "classify":
            return '{"kind":"document","legibility":"good","layout":"prose","handwriting":false,"orientation_deg":0,"language":"en","image_contains_text":true}'
        if request.stage == "ocr":
            return '{"blocks":[{"kind":"paragraph","text":"Transcribed page text from the scanned document."}]}'
        if request.stage == "summary":
            return "A short document summary used to keep later segments coherent."
        lang = request.target_lang or "xx"
        lines = []
        matched = False
        for line in request.user.splitlines():
            match = re.match(r"^\[(\d+)\]\s*(.*)$", line)
            if match:
                matched = True
                lines.append(f"[{match.group(1)}] {_tagged(lang, match.group(2))}")
            elif line.strip():
                lines.append(_tagged(lang, line))
        if not matched:
            return _tagged(lang, request.user)
        return "\n".join(lines)


def _tagged(lang: str, text: str) -> str:
    body = text
    if lang.lower() == "uk":
        body = f"{text} Переклад"
    return f"[{lang}] {body}"


def _segments(raw: str) -> set[int]:
    found = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            found.add(int(part))
    return found
