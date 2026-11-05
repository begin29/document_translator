from __future__ import annotations

import io
import zipfile
from pathlib import Path

from app.core.errors import AppError


def sanitized_filename(name: str) -> str:
    base = Path(name or "upload").name.replace("\x00", "").strip()
    if base in {"", ".", ".."}:
        return "upload"
    return base[:180]


def sniff_format(data: bytes, filename: str) -> str:
    if not data:
        raise AppError("invalid_file", "The uploaded file is empty.", filename, False, 400)
    lower = sanitized_filename(filename).lower()
    if data.startswith(b"MZ") or lower.endswith(".exe"):
        raise AppError(
            "unsupported_type",
            "Executable files are not accepted. Upload a PDF, DOCX, PNG, or JPEG.",
            lower,
            False,
            415,
        )
    if data.startswith(b"%PDF"):
        return "pdf"
    if data.startswith(b"\x89PNG"):
        if lower.endswith(".pdf"):
            raise AppError(
                "invalid_file",
                "The file is named like a PDF but its contents are a PNG image.",
                filename,
                False,
                400,
            )
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"PK"):
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except zipfile.BadZipFile as exc:
            raise AppError("corrupt_file", "The file looks like a zip archive but it is damaged.", str(exc), False, 400) from exc
        if any(item.startswith("word/") for item in names):
            return "docx"
        if any(item.startswith("xl/") for item in names) or lower.endswith(".xlsx"):
            raise AppError(
                "unsupported_type",
                "Excel workbooks are not supported. Upload a PDF or DOCX.",
                lower,
                False,
                415,
            )
        raise AppError(
            "unsupported_type",
            "This zip-based file is not a DOCX document.",
            lower,
            False,
            415,
        )
    if lower.endswith(".pdf"):
        raise AppError("corrupt_pdf", "The file name says PDF but the contents are not a PDF.", filename, False, 400)
    raise AppError(
        "unsupported_type",
        "This file type is not supported. Upload a PDF, DOCX, PNG, or JPEG.",
        lower or "unknown",
        False,
        415,
    )


def extension_matches(fmt: str, filename: str) -> bool:
    lower = sanitized_filename(filename).lower()
    expected = {
        "pdf": [".pdf"],
        "docx": [".docx"],
        "png": [".png"],
        "jpeg": [".jpg", ".jpeg"],
    }[fmt]
    return any(lower.endswith(ext) for ext in expected)
