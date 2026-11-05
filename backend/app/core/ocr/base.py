from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class OcrBlock:
    kind: str
    text: str


@dataclass
class OcrResult:
    blocks: list[OcrBlock]
    tokens_in: int = 0
    tokens_out: int = 0
    already_recorded: bool = False


class OcrProvider:
    def transcribe(self, image_png: bytes, source_hint: str = "") -> OcrResult:
        raise NotImplementedError
