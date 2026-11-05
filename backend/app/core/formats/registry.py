from __future__ import annotations

from app.core.formats.docx import DocxAdapter
from app.core.formats.image import ImageAdapter
from app.core.formats.pdf import PdfAdapter
from app.core.formats.sniff import sniff_format

ADAPTERS = {
    "pdf": PdfAdapter(),
    "docx": DocxAdapter(),
    "png": ImageAdapter("png", b"\x89PNG"),
    "jpeg": ImageAdapter("jpeg", b"\xff\xd8\xff"),
}


def adapter_for(fmt: str):
    adapter = ADAPTERS.get(fmt)
    if adapter is None:
        from app.core.errors import AppError

        raise AppError("unsupported_type", f"No adapter is registered for {fmt}.", fmt, False, 415)
    return adapter


def detect_format(data: bytes, filename: str) -> str:
    return sniff_format(data, filename)
