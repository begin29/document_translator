from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

import pypdfium2 as pdfium
from PIL import Image as PILImage
from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Image as FlowImage
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

from app.core.analysis.page_classifier import classify_page, page_info, png_bytes
from app.core.domain import BlockData, ExtractedDocument
from app.core.errors import AppError
from app.core.formats.fonts import ensure_font
from app.core.langdetect import detect_language


class PdfAdapter:
    format_id = "pdf"

    def sniff(self, data: bytes, filename: str) -> bool:
        return data.startswith(b"%PDF")

    def extract(self, path: Path, files_dir: Path) -> ExtractedDocument:
        try:
            document = pdfium.PdfDocument(str(path))
        except Exception as exc:
            message = str(exc).lower()
            if "password" in message or "encrypt" in message:
                raise AppError(
                    "encrypted_pdf",
                    "This PDF is encrypted. Remove the password and upload it again.",
                    str(exc),
                    False,
                    400,
                ) from exc
            raise AppError(
                "corrupt_pdf",
                "This PDF is damaged and could not be opened.",
                str(exc),
                False,
                400,
            ) from exc
        asset_dir = files_dir / "assets" / path.stem
        thumb_dir = files_dir / "thumbs" / path.stem
        asset_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir.mkdir(parents=True, exist_ok=True)
        pages = []
        blocks: list[BlockData] = []
        sample_text: list[str] = []
        try:
            for index in range(len(document)):
                page = document[index]
                text, rects = _page_text(page)
                images = _page_images(page, asset_dir, index)
                coverage = _coverage(page, images)
                dpi = _dpi(images)
                bitmap = page.render(scale=1.4)
                rendered = bitmap.to_pil()
                page_class, metrics = classify_page(text, rendered, coverage, dpi)
                thumb = None
                if page_class in {"scanned_poor", "unknown", "scanned_good"}:
                    thumb_path = thumb_dir / f"{index + 1}.png"
                    rendered.resize((240, int(240 * rendered.height / max(rendered.width, 1)))).save(thumb_path, format="PNG")
                    thumb = str(thumb_path)
                    (thumb_dir / f"{index + 1}.full.png").write_bytes(png_bytes(rendered))
                pages.append(page_info(index + 1, page_class, metrics, thumb))
                if page_class in {"text", "text_with_images", "text_garbled"}:
                    blocks.extend(_text_blocks(index + 1, text, rects))
                    sample_text.append(text)
                seq = 500 + len(images)
                for image_meta in images:
                    if page_class in {"text", "text_with_images"}:
                        blocks.append(
                            BlockData.make(
                                page=index + 1,
                                seq=seq,
                                kind="image",
                                text=None,
                                asset_path=image_meta["path"],
                                meta={"bbox": image_meta["bbox"], "contains_text": False},
                            )
                        )
                        seq += 1
        finally:
            document.close()
        if not pages:
            raise AppError("empty_document", "This PDF has no pages.", path.name, False, 400)
        return ExtractedDocument(
            format="pdf",
            page_count=len(pages),
            pages=pages,
            blocks=blocks,
            detected_source_lang=detect_language("\n".join(sample_text)),
        )

    def render(self, source: Path, extracted: ExtractedDocument, translations: dict[int, str], out_path: Path, mode: str) -> dict:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if mode == "layout":
            try:
                report = _render_layout(source, extracted, translations, out_path)
                if report.get("ok"):
                    return report
            except Exception as exc:
                report = {"ok": False, "error": str(exc), "overflow_pages": []}
            _render_reflow(extracted, translations, out_path)
            report["mode"] = "reflow"
            report["fallback"] = "layout_failed"
            return report
        _render_reflow(extracted, translations, out_path)
        return {"ok": True, "mode": "reflow", "overflow_pages": []}


def _page_text(page) -> tuple[str, list[dict]]:
    textpage = page.get_textpage()
    try:
        text = textpage.get_text_bounded() or ""
    except Exception:
        text = ""
    rects: list[dict] = []
    try:
        count = textpage.count_rects()
    except Exception:
        count = 0
    for index in range(count):
        try:
            left, bottom, right, top = textpage.get_rect(index)
            snippet = textpage.get_text_bounded(left, bottom, right, top) or ""
        except Exception:
            continue
        snippet = snippet.strip()
        if snippet:
            rects.append({"text": snippet, "bbox": [left, bottom, right, top]})
    if not text.strip() and rects:
        text = "\n".join(item["text"] for item in rects)
    return text, rects


def _text_blocks(page_no: int, text: str, rects: list[dict]) -> list[BlockData]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs and text.strip():
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    blocks: list[BlockData] = []
    for seq, paragraph in enumerate(paragraphs):
        kind = "heading" if len(paragraph) < 80 and "\n" not in paragraph and not paragraph.endswith(".") else "paragraph"
        bbox = rects[seq]["bbox"] if seq < len(rects) else None
        blocks.append(BlockData.make(page=page_no, seq=seq, kind=kind, text=paragraph, meta={"bbox": bbox}))
    return blocks


def _page_images(page, asset_dir: Path, index: int) -> list[dict]:
    found = []
    try:
        objects = list(page.get_objects(max_depth=2))
    except Exception:
        return found
    image_type = pdfium.raw.FPDF_PAGEOBJ_IMAGE
    for obj_index, obj in enumerate(objects):
        try:
            if obj.type != image_type:
                continue
            left, bottom, right, top = obj.get_bounds()
            bitmap = obj.get_bitmap(render=True)
            image = bitmap.to_pil()
        except Exception:
            continue
        target = asset_dir / f"p{index + 1}-{obj_index}.png"
        image.save(target, format="PNG")
        found.append(
            {
                "path": str(target),
                "bbox": [left, bottom, right, top],
                "pixel_width": image.width,
                "pixel_height": image.height,
            }
        )
    return found


def _coverage(page, images: list[dict]) -> float:
    width, height = page.get_size()
    area = max(width * height, 1)
    covered = 0.0
    for image in images:
        left, bottom, right, top = image["bbox"]
        covered += max(0, right - left) * max(0, top - bottom)
    return min(1.0, covered / area)


def _dpi(images: list[dict]) -> float | None:
    dpis = []
    for image in images:
        left, bottom, right, top = image["bbox"]
        inches = max(right - left, 1) / 72
        dpis.append(image["pixel_width"] / inches)
    if not dpis:
        return None
    return sum(dpis) / len(dpis)


def _render_reflow(extracted: ExtractedDocument, translations: dict[int, str], out_path: Path) -> None:
    font = ensure_font()
    styles = {
        "heading": ParagraphStyle("H", fontName=font, fontSize=16, leading=20, spaceAfter=8),
        "paragraph": ParagraphStyle("B", fontName=font, fontSize=11, leading=15, spaceAfter=6),
        "list_item": ParagraphStyle("L", fontName=font, fontSize=11, leading=15, leftIndent=8, spaceAfter=4),
        "caption": ParagraphStyle("C", fontName=font, fontSize=9, leading=12, spaceAfter=8, textColor="#333333"),
        "table_cell": ParagraphStyle("T", fontName=font, fontSize=10, leading=13, spaceAfter=4),
    }
    doc = SimpleDocTemplate(str(out_path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm)
    story = []
    current = None
    usable = A4[0] - 36 * mm
    ordered = sorted(extracted.blocks, key=lambda block: (block.page, block.seq, block.idx))
    groups: dict[str, list] = {}
    for block in ordered:
        group = block.meta.get("split_from_block")
        if group:
            groups.setdefault(str(group), []).append(block)
    emitted: set[int] = set()
    for block in ordered:
        if block.idx in emitted:
            continue
        group = block.meta.get("split_from_block")
        if group and len(groups.get(str(group), [])) > 1:
            siblings = groups[str(group)]
            text = " ".join((translations.get(item.idx, item.text) or "") for item in siblings).strip()
            for item in siblings:
                emitted.add(item.idx)
            kind = siblings[0].kind
            page_no = siblings[0].page
            asset = None
        else:
            text = translations.get(block.idx, block.text) or ""
            kind = block.kind
            page_no = block.page
            asset = block.asset_path
            emitted.add(block.idx)
        if current is not None and page_no != current:
            story.append(PageBreak())
        current = page_no
        if kind == "image" and asset and Path(asset).exists():
            with PILImage.open(asset) as image:
                scale = min(1, usable / max(image.width, 1))
                story.append(FlowImage(asset, width=image.width * scale, height=image.height * scale))
                story.append(Spacer(1, 6))
            continue
        if not str(text).strip():
            continue
        story.append(Paragraph(escape(str(text)).replace("\n", "<br/>"), styles.get(kind, styles["paragraph"])))
    if not story:
        story.append(Paragraph(" ", styles["paragraph"]))
    doc.build(story)


def _render_layout(source: Path, extracted: ExtractedDocument, translations: dict[int, str], out_path: Path) -> dict:
    font = ensure_font()
    stripped = out_path.with_suffix(".stripped.pdf")
    overlay = out_path.with_suffix(".overlay.pdf")
    document = pdfium.PdfDocument(str(source))
    overflow: list[int] = []
    page = None
    try:
        for index in range(len(document)):
            page = document[index]
            for obj in list(page.get_objects()):
                if obj.type == pdfium.raw.FPDF_PAGEOBJ_TEXT:
                    page.remove_obj(obj)
                obj.close()
            page.gen_content()
            page.close()
        document.save(str(stripped))
    finally:
        page = None
        document.close()
    base = pdfium.PdfDocument(str(source))
    from reportlab.pdfgen import canvas

    first = base[0]
    width, height = first.get_size()
    first.close()
    drawing = canvas.Canvas(str(overlay), pagesize=(width, height))
    blocks_by_page: dict[int, list] = {}
    for block in extracted.blocks:
        if block.kind == "image":
            continue
        blocks_by_page.setdefault(block.page, []).append(block)
    for index in range(len(base)):
        page = base[index]
        width, height = page.get_size()
        if index:
            drawing.showPage()
            drawing.setPageSize((width, height))
        page_blocks = blocks_by_page.get(index + 1, [])
        page_overflow = False
        for block in page_blocks:
            bbox = (block.meta or {}).get("bbox")
            text = translations.get(block.idx, block.text) or ""
            if not bbox or not str(text).strip():
                continue
            left, bottom, right, top = bbox
            box_w = max(4, right - left)
            box_h = max(4, top - bottom)
            size = 11
            drawing.setFont(font, size)
            while size > 6 and drawing.stringWidth(str(text), font, size) > box_w:
                size -= 0.5
            if drawing.stringWidth(str(text), font, size) > box_w or size * 1.2 > box_h + 4:
                page_overflow = True
                break
            drawing.setFillColorRGB(0, 0, 0)
            drawing.setFont(font, size)
            drawing.drawString(left, bottom, str(text)[:500])
        if page_overflow:
            overflow.append(index + 1)
        page.close()
    drawing.save()
    page = None
    base.close()
    overflow_set = set(overflow)
    reader = PdfReader(str(stripped))
    over = PdfReader(str(overlay))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        page_no = index + 1
        if page_no in overflow_set:
            single = out_path.with_suffix(f".page{page_no}.pdf")
            _reflow_page([block for block in extracted.blocks if block.page == page_no], translations, single)
            writer.add_page(PdfReader(str(single)).pages[0])
            single.unlink(missing_ok=True)
            continue
        if index < len(over.pages):
            page.merge_page(over.pages[index])
        writer.add_page(page)
    with out_path.open("wb") as handle:
        writer.write(handle)
    stripped.unlink(missing_ok=True)
    overlay.unlink(missing_ok=True)
    return {"ok": True, "mode": "layout", "overflow_pages": overflow}


def _reflow_page(blocks: list, translations: dict[int, str], dest: Path) -> None:
    cloned = [
        BlockData(
            page=1,
            seq=block.seq,
            kind=block.kind,
            text=block.text,
            src_hash=block.src_hash,
            asset_path=block.asset_path,
            meta=dict(block.meta or {}),
            idx=block.idx,
        )
        for block in blocks
    ]
    _render_reflow(ExtractedDocument("pdf", 1, [], cloned, None), translations, dest)
