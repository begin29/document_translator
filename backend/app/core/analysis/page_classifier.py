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


def _sample_gray(image: Image.Image, long_edge: int = 512) -> np.ndarray:
    gray = image.convert("L")
    scale = long_edge / max(gray.size)
    if scale < 1:
        gray = gray.resize((max(1, int(gray.width * scale)), max(1, int(gray.height * scale))), Image.Resampling.BILINEAR)
    return np.asarray(gray, dtype=np.float32)


def _tile_laplacian(patch: np.ndarray) -> float:
    if patch.shape[0] < 3 or patch.shape[1] < 3:
        return 0.0
    center = patch[1:-1, 1:-1]
    lap = -4 * center + patch[:-2, 1:-1] + patch[2:, 1:-1] + patch[1:-1, :-2] + patch[1:-1, 2:]
    return float(lap.var())


def ink_signal(image: Image.Image) -> tuple[bool, float, float]:
    arr = _sample_gray(image)
    height, width = arr.shape
    tile = 64
    sharpnesses: list[float] = []
    contrasts: list[float] = []
    for y in range(0, height, tile):
        for x in range(0, width, tile):
            patch = arr[y : y + tile, x : x + tile]
            if patch.size < 16:
                continue
            spread = float(np.percentile(patch, 95) - np.percentile(patch, 5))
            if spread < 18 and float(patch.std()) < 8:
                continue
            sharpnesses.append(_tile_laplacian(patch))
            contrasts.append(spread)
    if not sharpnesses:
        return False, 0.0, 0.0
    order = sorted(range(len(sharpnesses)), key=sharpnesses.__getitem__)[-3:]
    sharpness = float(sum(sharpnesses[index] for index in order) / len(order))
    contrast = float(sum(contrasts[index] for index in order) / len(order))
    return True, sharpness, contrast


def is_blank(image: Image.Image | None, text: str) -> bool:
    if text and len(text.strip()) > 8:
        return False
    if image is None:
        return len((text or "").strip()) < 5
    has_ink, _, _ = ink_signal(image)
    return not has_ink


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
    stripped = (text or "").strip()
    chars = len(stripped)
    quality = text_quality(text or "")
    has_ink = False
    sharpness = 0.0
    ink_contrast = 0.0
    if image is not None:
        has_ink, sharpness, ink_contrast = ink_signal(image)
    blur = sharpness if image is not None else None
    contrast = ink_contrast if image is not None else None
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
    if chars <= 8 and ((image is None and chars < 5) or (image is not None and not has_ink)):
        return "blank", metrics
    readable = chars >= 20 and quality >= 0.45
    if readable and image_coverage >= 0.08:
        return "text_with_images", metrics
    if readable:
        return "text", metrics
    if chars >= 20 and quality < 0.2:
        return "text_garbled", metrics
    image_dominant = image is not None and (image_coverage >= 0.2 or (chars < 12 and image_coverage > 0))
    if image is not None and chars < 12 and image_coverage == 0 and (chars > 8 or has_ink):
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
