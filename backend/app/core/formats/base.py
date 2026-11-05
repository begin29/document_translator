from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.core.domain import ExtractedDocument


class DocumentAdapter(Protocol):
    format_id: str

    def sniff(self, data: bytes, filename: str) -> bool:
        ...

    def extract(self, path: Path, files_dir: Path) -> ExtractedDocument:
        ...

    def render(self, source: Path, extracted: ExtractedDocument, translations: dict[int, str], out_path: Path, mode: str) -> dict:
        ...
