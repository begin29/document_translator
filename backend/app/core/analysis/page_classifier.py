from __future__ import annotations

import io

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from app.core.domain import PageInfo


def laplacian_variance(image: Image.Image) -> float:
    gray = image.convert("L").resize((256, 256))
    arr = np.asarray(gray, dtype=np.float32)
    if arr.shape[0] < 3 or arr.shape[1] < 3:
        return 0.0
    center = arr[1:-1, 1:-1]
    lap = -4 * center + arr[:-2, 1:-1] + arr[2:, 1:-1] + arr[1:-1, :-2] + arr[1:-1, 2:]
    return float(lap.var())


def contrast_spread(image: Image.Image) -> float:
    gray = np.asarray(image.convert("L").resize((128, 128)), dtype=np.float32)
    return float(np.percentile(gray, 95) - np.percentile(gray, 5))


def mean_saturation(image: Image.Image) -> float:
    hsv = np.asarray(image.convert("HSV").resize((128, 128)), dtype=np.float32)
    return float(hsv[:, :, 1].mean())


def white_ratio(image: Image.Image) -> float:
    gray = np.asarray(image.convert("L").resize((128, 128)))
    return float((gray > 230).mean())


def is_blank(image: Image.Image | None, text: str) -> bool:
    if text and len(text.strip()) > 8:
        return False
    if image is None:
        return len((text or "").strip()) < 5
    gray = np.asarray(image.convert("L").resize((128, 128)), dtype=np.float32)
    return float(gray.std()) < 6


def text_quality(text: str) -> float:
    tokens = [tok for tok in text.split() if any(ch.isalpha() for ch in tok)]
    if not tokens:
        return 0.0
    vowels = set("aeiouyаеєиіїоуюяыэAEIOUYАЕЄИІЇОУЮЯЫЭ")
    good = 0
    for token in tokens:
        letters = [ch for ch in token if ch.isalpha()]
        if len(letters) < 2:
            continue
        if any(ch in vowels for ch in letters) and len(set(letters)) > 1:
            good += 1
    return good / max(len(tokens), 1)


def estimate_skew_degrees(image: Image.Image) -> float:
    gray = np.asarray(image.convert("L").resize((180, 180)), dtype=np.float32)
    ink = gray < 180
    best_angle = 0.0
    best_score = -1.0
    for angle in range(-15, 16, 3):
        rotated = np.asarray(Image.fromarray(ink.astype(np.uint8) * 255).rotate(angle, resample=Image.NEAREST))
        row_ink = (rotated < 128).sum(axis=1).astype(np.float32)
        score = float(row_ink.var())
        if score > best_score:
            best_score = score
            best_angle = float(angle)
    return best_angle


def looks_document_like(image: Image.Image) -> bool:
    saturation = mean_saturation(image)
    white = white_ratio(image)
    spread = contrast_spread(image)
    if saturation < 35 and spread >= 12:
        return True
    if white > 0.35 and spread > 20:
        return True
    if saturation > 70 and white < 0.4:
        return False
    return spread > 40 and saturation < 80


def classify_page(
    text: str,
    image: Image.Image | None,
    image_coverage: float,
    effective_dpi: float | None,
) -> tuple[str, dict]:
    chars = len((text or "").strip())
    quality = text_quality(text or "")
    blur = laplacian_variance(image) if image is not None else None
    contrast = contrast_spread(image) if image is not None else None
    skew = estimate_skew_degrees(image) if image is not None else 0.0
    metrics = {
        "chars": chars,
        "text_quality": round(quality, 3),
        "image_coverage": round(image_coverage, 3),
        "effective_dpi": effective_dpi,
        "blur": None if blur is None else round(blur, 2),
        "contrast": None if contrast is None else round(contrast, 2),
        "skew": round(skew, 2),
    }
    if is_blank(image, text or ""):
        return "blank", metrics
    readable = chars >= 20 and quality >= 0.45
    if readable and image_coverage >= 0.08:
        return "text_with_images", metrics
    if readable:
        return "text", metrics
    if chars >= 20 and quality < 0.2:
        return "text_garbled", metrics
    image_dominant = image is not None and (image_coverage >= 0.2 or (chars < 12 and image_coverage > 0))
    if image is not None and chars < 12 and image_coverage == 0 and not is_blank(image, text or ""):
        image_dominant = True
    if image_dominant:
        document_like = looks_document_like(image) or white_ratio(image) > 0.45
        if image is not None and not document_like and chars < 12:
            return "unknown", metrics
        poor = False
        if effective_dpi is not None and effective_dpi < 150:
            poor = True
        if blur is not None and blur < 80:
            poor = True
        if contrast is not None and contrast < 35:
            poor = True
        if abs(skew) >= 8:
            poor = True
        if poor:
            return "scanned_poor", metrics
        return "scanned_good", metrics
    if chars < 8:
        return "blank", metrics
    return "unknown", metrics


def route_for(page_class: str) -> str:
    return {
        "text": "text",
        "text_with_images": "text",
        "text_garbled": "ocr",
        "scanned_good": "ocr",
        "scanned_poor": "ocr_enhance",
        "blank": "skip",
        "unknown": "classify",
        "non_document": "reject",
    }.get(page_class, "classify")


def page_info(page: int, page_class: str, metrics: dict, thumbnail_path: str | None = None) -> PageInfo:
    return PageInfo(page=page, page_class=page_class, route=route_for(page_class), metrics=metrics, thumbnail_path=thumbnail_path)


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
