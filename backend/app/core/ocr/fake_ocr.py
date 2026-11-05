from __future__ import annotations

import io

from PIL import Image

from app.core.analysis.page_classifier import ink_signal
from app.core.ocr.base import OcrBlock, OcrResult


class FakeOcrProvider:
    def transcribe(self, image_png: bytes, source_hint: str = "") -> OcrResult:
        image = Image.open(io.BytesIO(image_png))
        has_ink, sharpness, contrast = ink_signal(image)
        if not has_ink:
            return OcrResult(blocks=[], tokens_in=80, tokens_out=1)
        if sharpness < 80 or contrast < 35:
            return OcrResult(blocks=[OcrBlock(kind="paragraph", text="[illegible]")], tokens_in=120, tokens_out=8)
        return OcrResult(
            blocks=[OcrBlock(kind="paragraph", text="Transcribed page text from the scanned document.")],
            tokens_in=400,
            tokens_out=24,
        )
