from __future__ import annotations

import base64
import time

from app.config import get_settings
from app.core.errors import ProviderFailure, ProviderTimeout, RateLimited
from app.core.model_registry import resolve_model
from app.core.provider.base import TranslateRequest, TranslateResult
from app.core.tokenize import count_tokens


class OpenAIProvider:
    def __init__(self) -> None:
        settings = get_settings()
        if settings.app_env == "test":
            raise RuntimeError("OpenAIProvider cannot be constructed when APP_ENV=test")
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY or OPEN_AI_API_KEY is required when PROVIDER=openai")
        from openai import OpenAI

        self.client = OpenAI(api_key=settings.openai_api_key, timeout=90, max_retries=0)

    def count_tokens(self, text: str, model: str | None = None) -> int:
        spec = resolve_model(model or get_settings().default_model)
        return count_tokens(spec, text)

    def translate(self, request: TranslateRequest) -> TranslateResult:
        spec = resolve_model(request.model)
        content: list | str
        if request.images:
            content = [{"type": "text", "text": request.user}]
            for image in request.images:
                encoded = base64.b64encode(image).decode("ascii")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}", "detail": request.image_detail},
                    }
                )
        else:
            content = request.user
        payload = {
            "model": spec.id,
            "temperature": request.temperature,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": content},
            ],
            "max_completion_tokens": min(4096, spec.max_output_tokens),
        }
        if request.json_mode and not request.images:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            response = self.client.chat.completions.create(**payload)
        except Exception as exc:
            self._raise_mapped(exc)
            raise
        elapsed = int((time.perf_counter() - started) * 1000)
        choice = response.choices[0].message.content or ""
        usage = response.usage
        tokens_in = getattr(usage, "prompt_tokens", 0) or 0
        tokens_out = getattr(usage, "completion_tokens", 0) or 0
        details = getattr(usage, "prompt_tokens_details", None)
        cached = 0
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        return TranslateResult(
            text=choice,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cached=cached,
            model=response.model or spec.id,
            latency_ms=elapsed,
        )

    def _raise_mapped(self, exc: Exception) -> None:
        name = exc.__class__.__name__
        status = getattr(exc, "status_code", None)
        message = str(exc)
        if status == 429 or "rate limit" in message.lower():
            retry_after = 1.0
            response = getattr(exc, "response", None)
            if response is not None:
                header = response.headers.get("retry-after")
                if header:
                    try:
                        retry_after = float(header)
                    except ValueError:
                        retry_after = 1.0
            raise RateLimited(retry_after, message) from exc
        if status in {408, 504} or "timeout" in name.lower() or "timeout" in message.lower():
            raise ProviderTimeout(message) from exc
        if status is None or status >= 500:
            raise ProviderFailure(message) from exc
        raise ProviderFailure(message) from exc
