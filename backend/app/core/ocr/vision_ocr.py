from __future__ import annotations

import json

from app.core.ocr.base import OcrBlock, OcrResult
from app.core.prompts import OCR_PROMPT, PROMPT_VERSION
from app.core.provider.base import TranslateRequest


class VisionOcrProvider:
    def __init__(self, provider, model: str) -> None:
        self.provider = provider
        self.model = model

    def transcribe(self, image_png: bytes, source_hint: str = "") -> OcrResult:
        result = self.provider.translate(
            TranslateRequest(
                stage="ocr",
                model=self.model,
                system=OCR_PROMPT,
                user=f"Source language hint: {source_hint or 'unknown'}. Transcribe the page.",
                source_lang=source_hint or "",
                target_lang="",
                prompt_version=PROMPT_VERSION,
                images=[image_png],
                image_detail="high",
                json_mode=True,
                temperature=0,
            )
        )
        blocks = _parse(result.text)
        return OcrResult(blocks=blocks, tokens_in=result.tokens_in, tokens_out=result.tokens_out, already_recorded=True)


def _parse(raw: str) -> list[OcrBlock]:
    text = (raw or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        if text:
            return [OcrBlock(kind="paragraph", text=text)]
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return [OcrBlock(kind="paragraph", text=text)]
    rows = data.get("blocks") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    blocks: list[OcrBlock] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = str(row.get("text") or "").strip()
        if not value:
            continue
        kind = str(row.get("kind") or "paragraph")
        if kind not in {"heading", "paragraph", "list_item", "table_cell", "caption"}:
            kind = "paragraph"
        blocks.append(OcrBlock(kind=kind, text=value))
    return blocks
