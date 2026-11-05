from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

from app.core.domain import BlockData, ExtractedDocument, PageInfo
from app.core.errors import AppError
from app.core.langdetect import detect_language


class DocxAdapter:
    format_id = "docx"

    def sniff(self, data: bytes, filename: str) -> bool:
        if not data.startswith(b"PK"):
            return False
        try:
            names = zipfile.ZipFile(BytesIO(data)).namelist()
        except zipfile.BadZipFile:
            return False
        return any(name.startswith("word/") for name in names)

    def extract(self, path: Path, files_dir: Path) -> ExtractedDocument:
        try:
            document = Document(str(path))
        except Exception as exc:
            raise AppError("corrupt_file", "This DOCX file is damaged and could not be opened.", str(exc), False, 400) from exc
        asset_dir = files_dir / "assets" / path.stem
        asset_dir.mkdir(parents=True, exist_ok=True)
        blocks: list[BlockData] = []
        seq = 0
        texts: list[str] = []
        for paragraph in document.paragraphs:
            text, drawings = _paragraph_parts(paragraph)
            if drawings:
                for offset, blob in enumerate(drawings):
                    target = asset_dir / f"img-{seq}-{offset}.png"
                    target.write_bytes(blob)
                    blocks.append(
                        BlockData.make(
                            page=1,
                            seq=seq,
                            kind="image",
                            text=None,
                            asset_path=str(target),
                            meta={"anchor": "paragraph"},
                        )
                    )
                    seq += 1
            if text:
                kind = "heading" if paragraph.style and paragraph.style.name and paragraph.style.name.lower().startswith("heading") else "paragraph"
                blocks.append(
                    BlockData.make(
                        page=1,
                        seq=seq,
                        kind=kind,
                        text=text,
                        meta={"style": paragraph.style.name if paragraph.style else None, "container": "body"},
                    )
                )
                texts.append(text)
                seq += 1
        for table_index, table in enumerate(document.tables):
            for row_index, row in enumerate(table.rows):
                for cell_index, cell in enumerate(row.cells):
                    text = cell.text.strip()
                    if not text:
                        continue
                    blocks.append(
                        BlockData.make(
                            page=1,
                            seq=seq,
                            kind="table_cell",
                            text=text,
                            meta={"container": "table", "table": table_index, "row": row_index, "cell": cell_index},
                        )
                    )
                    texts.append(text)
                    seq += 1
        page = PageInfo(page=1, page_class="text_with_images" if any(b.kind == "image" for b in blocks) else "text", route="text", metrics={"chars": sum(len(t) for t in texts)})
        if not texts and not any(b.kind == "image" for b in blocks):
            page = PageInfo(page=1, page_class="blank", route="skip", metrics={"chars": 0})
        return ExtractedDocument(
            format="docx",
            page_count=1,
            pages=[page],
            blocks=blocks,
            detected_source_lang=detect_language("\n".join(texts)),
        )

    def render(self, source: Path, extracted: ExtractedDocument, translations: dict[int, str], out_path: Path, mode: str) -> dict:
        document = Document(str(source))
        body_blocks = [block for block in extracted.blocks if block.kind not in {"image"} and block.meta.get("container") == "body"]
        paragraphs = list(document.paragraphs)
        for block, paragraph in zip(body_blocks, paragraphs):
            _replace_paragraph(paragraph, translations.get(block.idx, block.text) or "")
        for block in extracted.blocks:
            if block.kind != "table_cell":
                continue
            table = document.tables[block.meta["table"]]
            cell = table.rows[block.meta["row"]].cells[block.meta["cell"]]
            _replace_cell(cell, translations.get(block.idx, block.text) or "")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        document.save(str(out_path))
        return {"ok": True, "mode": "docx", "overflow_pages": []}


def _paragraph_parts(paragraph) -> tuple[str, list[bytes]]:
    texts: list[str] = []
    drawings: list[bytes] = []
    for run in paragraph.runs:
        if _has_drawing(run):
            blob = _drawing_blob(run)
            if blob:
                drawings.append(blob)
            continue
        texts.append(run.text or "")
    return "".join(texts).strip(), drawings


def _has_drawing(run) -> bool:
    element = run._element
    return bool(element.xpath(".//w:drawing")) or bool(element.xpath(".//w:pict"))


def _drawing_blob(run) -> bytes | None:
    blips = run._element.xpath(".//a:blip")
    if not blips:
        return None
    embed = blips[0].get(qn("r:embed"))
    if not embed:
        return None
    part = run.part.related_parts.get(embed)
    if part is None:
        return None
    return part.blob


def _replace_paragraph(paragraph, text: str) -> None:
    text_runs = [run for run in paragraph.runs if not _has_drawing(run)]
    if not text_runs:
        return
    text_runs[0].text = text
    for run in text_runs[1:]:
        run.text = ""


def _replace_cell(cell, text: str) -> None:
    if not cell.paragraphs:
        return
    _replace_paragraph(cell.paragraphs[0], text)
    for paragraph in cell.paragraphs[1:]:
        text_runs = [run for run in paragraph.runs if not _has_drawing(run)]
        for run in text_runs:
            run.text = ""
