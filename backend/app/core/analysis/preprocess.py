from __future__ import annotations

import io

from PIL import Image, ImageOps


def preprocess_page(image: Image.Image, long_edge: int = 2000) -> Image.Image:
    prepared = ImageOps.autocontrast(image.convert("RGB"))
    prepared = ImageOps.grayscale(prepared)
    prepared = prepared.rotate(_deskew_angle(prepared), expand=True, fillcolor=255)
    prepared = prepared.convert("RGB")
    width, height = prepared.size
    scale = long_edge / max(width, height, 1)
    if scale > 1:
        prepared = prepared.resize((int(width * scale), int(height * scale)), Image.Resampling.LANCZOS)
    return prepared


def _deskew_angle(image: Image.Image) -> float:
    from app.core.analysis.page_classifier import estimate_skew_degrees

    return -estimate_skew_degrees(image)


def to_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
