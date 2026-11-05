from __future__ import annotations

import json
from typing import Protocol

from app.core.prompts import CLASSIFY_PROMPT, PROMPT_VERSION


class VisionClassifier(Protocol):
    def classify(self, image_png: bytes) -> dict:
        ...


class FakeVisionClassifier:
    def __init__(self) -> None:
        self.calls = 0

    def classify(self, image_png: bytes) -> dict:
        self.calls += 1
        from io import BytesIO

        from PIL import Image

        from app.core.analysis.page_classifier import looks_document_like

        image = Image.open(BytesIO(image_png))
        if looks_document_like(image):
            return {
                "kind": "document",
                "legibility": "good",
                "layout": "prose",
                "handwriting": False,
                "orientation_deg": 0,
                "language": "en",
                "image_contains_text": True,
            }
        return {
            "kind": "non_document",
            "legibility": "poor",
            "layout": "mixed",
            "handwriting": False,
            "orientation_deg": 0,
            "language": "",
            "image_contains_text": False,
        }


class OpenAIVisionClassifier:
    def __init__(self, provider, model: str) -> None:
        self.provider = provider
        self.model = model

    def classify(self, image_png: bytes) -> dict:
        from app.core.provider.base import TranslateRequest

        result = self.provider.translate(
            TranslateRequest(
                stage="classify",
                model=self.model,
                system=CLASSIFY_PROMPT,
                user="Classify the attached page.",
                source_lang="",
                target_lang="",
                prompt_version=PROMPT_VERSION,
                images=[image_png],
                image_detail="low",
                json_mode=True,
                temperature=0,
            )
        )
        try:
            return json.loads(result.text)
        except json.JSONDecodeError:
            return {"kind": "document", "legibility": "partial", "layout": "prose", "handwriting": False, "orientation_deg": 0, "language": "", "image_contains_text": True}
