from __future__ import annotations

from app.config import get_settings
from app.core.ocr.fake_ocr import FakeOcrProvider
from app.core.ocr.vision_ocr import VisionOcrProvider
from app.core.analysis.vision_classifier import FakeVisionClassifier, OpenAIVisionClassifier
from app.core.provider.fake_provider import FakeProvider


def build_provider():
    settings = get_settings()
    if settings.provider == "openai":
        if settings.app_env == "test":
            raise RuntimeError("Refusing to call OpenAI when APP_ENV=test")
        from app.core.provider.openai_provider import OpenAIProvider

        return OpenAIProvider()
    return FakeProvider()


def build_ocr(provider):
    settings = get_settings()
    if settings.provider == "openai":
        return VisionOcrProvider(provider, settings.ocr_model)
    return FakeOcrProvider()


def build_vision(provider):
    settings = get_settings()
    if settings.provider == "openai":
        return OpenAIVisionClassifier(provider, settings.classify_model)
    return FakeVisionClassifier()
