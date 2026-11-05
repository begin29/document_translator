from __future__ import annotations

from pathlib import Path

from PIL import Image

from app.core.analysis.page_classifier import classify_page, page_info
from app.core.domain import ExtractedDocument
from app.core.errors import AppError
from app.core.formats.pdf import _render_reflow


class ImageAdapter:
    def __init__(self, format_id: str, magic: bytes) -> None:
        self.format_id = format_id
        self.magic = magic

    def sniff(self, data: bytes, filename: str) -> bool:
        return data.startswith(self.magic)

    def extract(self, path: Path, files_dir: Path) -> ExtractedDocument:
        try:
            image = Image.open(path)
            image.load()
        except Exception as exc:
            raise AppError("corrupt_file", "This image is damaged and could not be opened.", str(exc), False, 400) from exc
        dpi = None
        if image.info.get("dpi"):
            raw = image.info["dpi"]
            dpi = float(raw[0] if isinstance(raw, tuple) else raw)
        page_class, metrics = classify_page("", image, 1.0, dpi)
        thumb_dir = files_dir / "thumbs" / path.stem
        thumb_dir.mkdir(parents=True, exist_ok=True)
        full = thumb_dir / "1.full.png"
        image.convert("RGB").save(full, format="PNG")
        thumb = None
        if page_class in {"scanned_poor", "unknown", "scanned_good"}:
            preview = thumb_dir / "1.png"
            image.convert("RGB").resize((240, int(240 * image.height / max(image.width, 1)))).save(preview, format="PNG")
            thumb = str(preview)
        return ExtractedDocument(
            format=self.format_id,
            page_count=1,
            pages=[page_info(1, page_class, metrics, thumb)],
            blocks=[],
            detected_source_lang=None,
        )

    def render(self, source: Path, extracted: ExtractedDocument, translations: dict[int, str], out_path: Path, mode: str) -> dict:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        _render_reflow(extracted, translations, out_path)
        return {"ok": True, "mode": "reflow", "overflow_pages": []}
