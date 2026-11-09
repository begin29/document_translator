from __future__ import annotations

import base64
import json
import time

from app.config import get_settings
from app.core.errors import FundsExceeded, ProviderFailure, ProviderTimeout, RateLimited
from app.core.model_registry import resolve_model
from app.core.provider.base import TranslateRequest, TranslateResult

_FUNDS_CODES = frozenset({
    "insufficient_quota",
    "project_spend_limit_exceeded",
    "billing_hard_limit_reached",
    "billing_not_active",
})
_FUNDS_TYPES = frozenset({
    "insufficient_quota",
    "billing_limit_user_error",
})
_FUNDS_MARKERS = (
    "insufficient_quota",
    "project_spend_limit_exceeded",
    "billing_hard_limit_reached",
    "billing_not_active",
    "spend limit",
    "exceeded your current quota",
    "billing hard limit",
    "check your plan and billing",
    "insufficient funds",
)
from app.core.tokenize import count_tokens


class OpenAIProvider:
    def __init__(self) -> None:
        settings = get_settings()
        if settings.app_env == "test":
            raise RuntimeError("OpenAIProvider cannot be constructed when APP_ENV=test")
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is required when PROVIDER=openai")
        from app.observability.tracing import langfuse_enabled

        if langfuse_enabled(settings):
            from langfuse.openai import OpenAI

            self._traced = True
        else:
            from openai import OpenAI

            self._traced = False
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
        if self._traced:
            from app.observability.tracing import generation_name

            payload["name"] = generation_name(request.stage)
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
        raise_mapped(exc)


def raise_mapped(exc: Exception) -> None:
    status = getattr(exc, "status_code", None)
    code, err_type, message = _error_parts(exc)
    blob = f"{code}\n{err_type}\n{message}\n{exc}".lower()
    if _is_funds(status, code, err_type, blob):
        raise FundsExceeded(message) from exc
    if status == 429 or code == "rate_limit_exceeded" or err_type in {"tokens", "requests"} or "rate limit" in message.lower():
        raise RateLimited(_retry_after(exc), message) from exc
    name = exc.__class__.__name__
    if status in {408, 504} or "timeout" in name.lower() or "timeout" in message.lower():
        raise ProviderTimeout(message) from exc
    raise ProviderFailure(message) from exc


def _is_funds(status: int | None, code: str, err_type: str, blob: str) -> bool:
    if status == 402 or code in _FUNDS_CODES or err_type in _FUNDS_TYPES:
        return True
    return any(marker in blob for marker in _FUNDS_MARKERS)


def _error_parts(exc: Exception) -> tuple[str, str, str]:
    message = str(exc)
    code = ""
    err_type = ""
    body = getattr(exc, "body", None)
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            body = None
    if isinstance(body, dict):
        error = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(error, dict):
            raw_code = error.get("code")
            raw_type = error.get("type")
            raw_message = error.get("message")
            if isinstance(raw_code, str):
                code = raw_code
            if isinstance(raw_type, str):
                err_type = raw_type
            if isinstance(raw_message, str) and raw_message:
                message = raw_message
    if not code:
        raw = getattr(exc, "code", None)
        if isinstance(raw, str):
            code = raw
    return code.lower(), err_type.lower(), message


def _retry_after(exc: Exception) -> float:
    response = getattr(exc, "response", None)
    if response is None:
        return 1.0
    header = response.headers.get("retry-after")
    if not header:
        return 1.0
    try:
        return float(header)
    except (TypeError, ValueError):
        return 1.0
