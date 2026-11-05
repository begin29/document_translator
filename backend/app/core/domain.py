from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def block_hash(text: str | None) -> str:
    return hashlib.sha256(normalize_text(text or "").encode("utf-8")).hexdigest()


@dataclass
class BlockData:
    page: int
    seq: int
    kind: str
    text: str | None
    src_hash: str
    asset_path: str | None = None
    meta: dict = field(default_factory=dict)
    idx: int = 0

    @staticmethod
    def make(
        page: int,
        seq: int,
        kind: str,
        text: str | None,
        asset_path: str | None = None,
        meta: dict | None = None,
    ) -> "BlockData":
        return BlockData(
            page=page,
            seq=seq,
            kind=kind,
            text=text,
            src_hash=block_hash(text or ""),
            asset_path=asset_path,
            meta=dict(meta or {}),
        )


@dataclass
class PageInfo:
    page: int
    page_class: str
    route: str
    metrics: dict = field(default_factory=dict)
    thumbnail_path: str | None = None


@dataclass
class ExtractedDocument:
    format: str
    page_count: int
    pages: list[PageInfo]
    blocks: list[BlockData]
    detected_source_lang: str | None = None
