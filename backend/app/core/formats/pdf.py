from __future__ import annotations

import ctypes
import re
from collections import Counter
from pathlib import Path
from statistics import median
from xml.sax.saxutils import escape

import pypdfium2 as pdfium
from PIL import Image as PILImage
from pypdf import PdfReader, PdfWriter
from reportlab.lib.colors import HexColor, black
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import BaseDocTemplate, Frame, NextPageTemplate, PageBreak, PageTemplate, Spacer
from reportlab.platypus import Image as FlowImage
from reportlab.platypus import Paragraph

from app.core.analysis.page_classifier import classify_page, page_info, png_bytes
from app.core.domain import BlockData, ExtractedDocument
from app.core.errors import AppError
from app.core.formats.fonts import ensure_font, ensure_fonts
from app.core.langdetect import detect_language

_LIST_RE = re.compile(r"^(?:[\u2022\u2023\u25e6\u2043\u2219\u00b7\-\*\u2013\u2014]|\d{1,3}[.)]|[A-Za-z][.)])\s+\S")
_ALIGN = {"left": TA_LEFT, "center": TA_CENTER, "right": TA_RIGHT, "justify": TA_JUSTIFY}
_STYLE_KEYS = ("font_size", "bold", "italic", "family", "color", "align", "line_height")
_KIND_SIZE = {"heading": 16, "paragraph": 11, "list_item": 11, "caption": 9, "table_cell": 10}
_BOTTOM_MARGIN = 16 * mm
_FREE_GROWTH = 4


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
                textpage = page.get_textpage()
                try:
                    try:
                        text = textpage.get_text_bounded() or ""
                    except Exception:
                        text = ""
                    try:
                        char_blocks = _blocks_from_chars(textpage, page, index + 1)
                    except Exception:
                        char_blocks = None
                finally:
                    textpage.close()
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
                width, height = page.get_size()
                page_size = [float(width), float(height)]
                if page_class in {"text", "text_with_images", "text_garbled"}:
                    if char_blocks is None or (not char_blocks and text.strip()):
                        blocks.extend(_fallback_blocks(index + 1, text, page_size))
                    else:
                        blocks.extend(char_blocks)
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
                                meta={
                                    "bbox": image_meta["bbox"],
                                    "contains_text": False,
                                    "page_size": page_size,
                                },
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


def _font_info(textpage, index: int) -> tuple[str, int]:
    flags = ctypes.c_int(0)
    needed = pdfium.raw.FPDFText_GetFontInfo(textpage.raw, index, None, 0, flags)
    if needed <= 1:
        return "", int(flags.value)
    buffer = ctypes.create_string_buffer(int(needed))
    pdfium.raw.FPDFText_GetFontInfo(textpage.raw, index, buffer, int(needed), flags)
    name = buffer.value.decode("utf-8", "replace")
    return name, int(flags.value)


def _fill_color(textpage, index: int) -> str | None:
    red = ctypes.c_uint()
    green = ctypes.c_uint()
    blue = ctypes.c_uint()
    alpha = ctypes.c_uint()
    ok = pdfium.raw.FPDFText_GetFillColor(textpage.raw, index, red, green, blue, alpha)
    if not ok:
        return None
    return f"#{int(red.value) & 255:02x}{int(green.value) & 255:02x}{int(blue.value) & 255:02x}"


def _looks_bold(weight: int, flags: int, name: str) -> bool:
    if weight >= 600 or flags & 0x40000:
        return True
    lowered = name.lower()
    return any(part in lowered for part in ("bold", "black", "semibold", "demi", "heavy"))


def _looks_italic(flags: int, name: str) -> bool:
    if flags & 0x40:
        return True
    lowered = name.lower()
    return "italic" in lowered or "oblique" in lowered


def _family_of(flags: int, name: str) -> str:
    lowered = name.lower()
    if flags & 0x1 or any(part in lowered for part in ("mono", "courier", "consolas", "typewriter")):
        return "mono"
    if "sans" in lowered:
        return "sans"
    if flags & 0x2 or any(part in lowered for part in ("serif", "times", "georgia", "cambria", "palatino", "roman")):
        return "serif"
    return "sans"


def _median(values: list[float]) -> float:
    return float(median(values))


def _majority_bool(values: list[bool]) -> bool:
    if not values:
        return False
    return sum(1 for value in values if value) * 2 >= len(values)


def _majority_value(values: list):
    usable = [value for value in values if value is not None]
    if not usable:
        return None
    return Counter(usable).most_common(1)[0][0]


def _blocks_from_chars(textpage, page, page_no: int) -> list[BlockData]:
    raw = pdfium.raw
    count = raw.FPDFText_CountChars(textpage.raw)
    if count < 0:
        raise RuntimeError("character count failed")
    width, height = page.get_size()
    page_size = [float(width), float(height)]
    glyphs: list[dict] = []
    for index in range(count):
        code = int(raw.FPDFText_GetUnicode(textpage.raw, index) or 0)
        if code in (10, 13):
            glyphs.append({"break": True})
            continue
        if code <= 0 or code > 0x10FFFF:
            continue
        character = chr(code)
        if character == "\x00":
            continue
        try:
            left, bottom, right, top = textpage.get_charbox(index)
        except Exception:
            continue
        left, right = min(float(left), float(right)), max(float(left), float(right))
        bottom, top = min(float(bottom), float(top)), max(float(bottom), float(top))
        try:
            size = float(raw.FPDFText_GetFontSize(textpage.raw, index) or 0)
            weight = int(raw.FPDFText_GetFontWeight(textpage.raw, index))
            name, flags = _font_info(textpage, index)
            color = _fill_color(textpage, index)
        except Exception:
            size, weight, name, flags, color = 0, -1, "", 0, None
        glyphs.append(
            {
                "break": False,
                "ch": character,
                "left": left,
                "bottom": bottom,
                "right": right,
                "top": top,
                "size": size if size > 0 else 0,
                "bold": _looks_bold(weight, flags, name),
                "italic": _looks_italic(flags, name),
                "family": _family_of(flags, name),
                "color": color,
            }
        )
    lines = _lines_from_glyphs(glyphs)
    return _blocks_from_lines(lines, page_no, page_size)


def _finish_line(chars: list[dict]) -> dict:
    ordered = sorted(chars, key=lambda item: item["left"])
    visible = [item for item in ordered if not item["ch"].isspace()]
    pool = visible or ordered
    sizes = [item["size"] for item in pool if item["size"] > 0.5]
    font_size = _median(sizes) if sizes else 10.0
    return {
        "chars": ordered,
        "text": "".join(item["ch"] for item in ordered),
        "left": min(item["left"] for item in ordered),
        "right": max(item["right"] for item in ordered),
        "bottom": min(item["bottom"] for item in ordered),
        "top": max(item["top"] for item in ordered),
        "baseline": max(item["bottom"] for item in ordered),
        "font_size": font_size,
    }


def _lines_from_glyphs(glyphs: list[dict]) -> list[dict]:
    raw_lines: list[dict] = []
    current: list[dict] = []
    for glyph in glyphs:
        if glyph.get("break"):
            if current:
                raw_lines.append(_finish_line(current))
                current = []
            continue
        if not current:
            current = [glyph]
            continue
        size = max([item["size"] for item in current if item["size"] > 0.5] + [glyph["size"] or 0, 1])
        baseline = max(item["bottom"] for item in current)
        if abs(glyph["bottom"] - baseline) <= 0.3 * size:
            current.append(glyph)
            continue
        raw_lines.append(_finish_line(current))
        current = [glyph]
    if current:
        raw_lines.append(_finish_line(current))
    raw_lines.sort(key=lambda line: (-line["baseline"], line["left"]))
    merged: list[dict] = []
    for line in raw_lines:
        if not line["text"].strip():
            continue
        if not merged:
            merged.append(line)
            continue
        previous = merged[-1]
        size = max(previous["font_size"], line["font_size"], 1)
        same_row = abs(previous["baseline"] - line["baseline"]) <= 0.3 * size
        gap = line["left"] - previous["right"]
        if same_row and gap < max(size * 2, 8):
            merged[-1] = _finish_line(previous["chars"] + line["chars"])
            continue
        merged.append(line)
    return merged


def _continues(previous: dict, line: dict) -> bool:
    size = max(previous["font_size"], line["font_size"], 1)
    if abs(line["font_size"] - previous["font_size"]) > 0.1 * max(previous["font_size"], 1):
        return False
    baseline_gap = previous["baseline"] - line["baseline"]
    if baseline_gap <= 0 or baseline_gap - size >= 0.8 * size:
        return False
    left_close = abs(line["left"] - previous["left"]) <= size
    previous_mid = (previous["left"] + previous["right"]) / 2
    line_mid = (line["left"] + line["right"]) / 2
    return left_close or abs(line_mid - previous_mid) <= size


def _alignment(lines: list[dict], page_width: float, font_size: float) -> str:
    tolerance = max(3.0, font_size * 0.35)
    if len(lines) >= 2:
        lefts = [line["left"] for line in lines]
        rights = [line["right"] for line in lines]
        left_aligned = max(lefts) - min(lefts) <= tolerance
        right_aligned = max(rights) - min(rights) <= tolerance
        centers = [(line["left"] + line["right"]) / 2 for line in lines]
        center_aligned = max(centers) - min(centers) <= tolerance
        fullest = max(rights)
        full = sum(1 for right in rights if fullest - right <= tolerance)
        if left_aligned and full >= 2 and fullest - min(rights) > tolerance:
            return "justify"
        if left_aligned and right_aligned and max(rights) - min(lefts) >= page_width * 0.45:
            return "justify"
        if center_aligned and not left_aligned:
            return "center"
        if right_aligned and not left_aligned:
            return "right"
        if left_aligned:
            return "left"
    line = lines[0]
    width = line["right"] - line["left"]
    midpoint = (line["left"] + line["right"]) / 2
    if width < page_width * 0.85 and abs(midpoint - page_width / 2) <= max(8.0, page_width * 0.04):
        return "center"
    if line["right"] >= page_width * 0.82 and line["left"] > page_width * 0.35:
        return "right"
    return "left"


def _kind_for(text: str, font_size: float, bold: bool, body_size: float) -> str:
    if _LIST_RE.match(text):
        return "list_item"
    if font_size >= body_size * 1.25 or (bold and len(text) < 120):
        return "heading"
    return "paragraph"


def _blocks_from_lines(lines: list[dict], page_no: int, page_size: list[float]) -> list[BlockData]:
    if not lines:
        return []
    groups: list[list[dict]] = [[lines[0]]]
    for line in lines[1:]:
        if _continues(groups[-1][-1], line):
            groups[-1].append(line)
        else:
            groups.append([line])
    drafted = []
    for group in groups:
        parts = [line["text"].strip() for line in group if line["text"].strip()]
        text = " ".join(parts).strip()
        if not text:
            continue
        chars = [char for line in group for char in line["chars"]]
        visible = [char for char in chars if not char["ch"].isspace()] or chars
        sizes = [char["size"] for char in visible if char["size"] > 0.5]
        font_size = _median(sizes) if sizes else _median([line["font_size"] for line in group])
        baselines = [line["baseline"] for line in group]
        gaps = [baselines[index] - baselines[index + 1] for index in range(len(baselines) - 1)]
        gaps = [gap for gap in gaps if gap > 0]
        line_height = max(_median(gaps), font_size) if gaps else font_size * 1.2
        drafted.append(
            {
                "text": text,
                "font_size": font_size,
                "bold": _majority_bool([bool(char["bold"]) for char in visible]),
                "italic": _majority_bool([bool(char["italic"]) for char in visible]),
                "family": _majority_value([char["family"] for char in visible]) or "sans",
                "color": _majority_value([char["color"] for char in visible if char["color"]]) or "#000000",
                "align": _alignment(group, page_size[0], font_size),
                "line_height": line_height,
                "bbox": [
                    min(char["left"] for char in chars),
                    min(char["bottom"] for char in chars),
                    max(char["right"] for char in chars),
                    max(char["top"] for char in chars),
                ],
                "top": max(line["top"] for line in group),
                "left": min(line["left"] for line in group),
            }
        )
    if not drafted:
        return []
    weighted: list[float] = []
    for item in drafted:
        weighted.extend([item["font_size"]] * max(1, len(item["text"])))
    body_size = _median(weighted)
    drafted.sort(key=lambda item: (-item["top"], item["left"]))
    blocks = []
    for seq, item in enumerate(drafted):
        blocks.append(
            BlockData.make(
                page=page_no,
                seq=seq,
                kind=_kind_for(item["text"], item["font_size"], item["bold"], body_size),
                text=item["text"],
                meta={
                    "bbox": item["bbox"],
                    "font_size": round(item["font_size"], 2),
                    "bold": item["bold"],
                    "italic": item["italic"],
                    "family": item["family"],
                    "color": item["color"],
                    "align": item["align"],
                    "line_height": round(item["line_height"], 2),
                    "page_size": page_size,
                },
            )
        )
    return blocks


def _fallback_blocks(page_no: int, text: str, page_size: list[float]) -> list[BlockData]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs and text.strip():
        paragraphs = [line.strip() for line in text.splitlines() if line.strip()]
    blocks: list[BlockData] = []
    for seq, paragraph in enumerate(paragraphs):
        kind = "heading" if len(paragraph) < 80 and "\n" not in paragraph and not paragraph.endswith(".") else "paragraph"
        blocks.append(
            BlockData.make(
                page=page_no,
                seq=seq,
                kind=kind,
                text=paragraph,
                meta={"bbox": None, "page_size": page_size},
            )
        )
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
            left, bottom, right, top = _object_box(obj)
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


def _object_box(obj) -> tuple[float, float, float, float]:
    getter = getattr(obj, "get_pos", None)
    if getter is not None:
        try:
            left, bottom, right, top = getter()
            if right > left and top > bottom:
                return float(left), float(bottom), float(right), float(top)
        except Exception:
            pass
    left, bottom, right, top = obj.get_bounds()
    return float(left), float(bottom), float(right), float(top)


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


def _has_style(meta: dict | None) -> bool:
    return any(key in (meta or {}) for key in _STYLE_KEYS)


def _color(value):
    if not value:
        return None
    try:
        return HexColor(str(value))
    except Exception:
        return None


def _fixed_styles(font: str) -> dict[str, ParagraphStyle]:
    return {
        "heading": ParagraphStyle("H", fontName=font, fontSize=16, leading=20, spaceAfter=8),
        "paragraph": ParagraphStyle("B", fontName=font, fontSize=11, leading=15, spaceAfter=6),
        "list_item": ParagraphStyle("L", fontName=font, fontSize=11, leading=15, leftIndent=8, spaceAfter=4),
        "caption": ParagraphStyle("C", fontName=font, fontSize=9, leading=12, spaceAfter=8, textColor=HexColor("#333333")),
        "table_cell": ParagraphStyle("T", fontName=font, fontSize=10, leading=13, spaceAfter=4),
    }


def _font_for(meta: dict, fonts: dict, fallback: str) -> str:
    family = meta.get("family") or "sans"
    if family not in {"sans", "serif", "mono"}:
        family = "sans"
    bold = bool(meta.get("bold"))
    italic = bool(meta.get("italic"))
    return fonts.get((family, bold, italic)) or fonts.get((family, False, False)) or fonts.get(("sans", False, False)) or fallback


def _style_for(kind: str, meta: dict, fonts: dict, fallback: dict[str, ParagraphStyle], name: str, layout: bool) -> ParagraphStyle:
    base = fallback.get(kind, fallback["paragraph"])
    if not layout and not _has_style(meta):
        return base
    try:
        size = float(meta.get("font_size") or (_KIND_SIZE.get(kind, base.fontSize) if layout else base.fontSize))
    except (TypeError, ValueError):
        size = float(base.fontSize)
    size = max(6.0, min(size, 96.0))
    try:
        leading = float(meta["line_height"]) if meta.get("line_height") else size * 1.2
    except (TypeError, ValueError):
        leading = size * 1.2
    if leading < size:
        leading = size * 1.2
    color = _color(meta.get("color")) or (base.textColor if not layout else black)
    alignment = _ALIGN.get(str(meta.get("align") or "left"), TA_LEFT)
    return ParagraphStyle(
        name,
        fontName=_font_for(meta, fonts, base.fontName),
        fontSize=size,
        leading=leading,
        textColor=color,
        alignment=alignment,
        spaceBefore=0,
        spaceAfter=0 if layout else base.spaceAfter,
        leftIndent=0 if layout or _has_style(meta) else base.leftIndent,
        firstLineIndent=0,
        borderPadding=0,
    )


def _page_size_of(metas: list[dict]) -> tuple[float, float]:
    for meta in metas:
        raw = (meta or {}).get("page_size")
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            try:
                width, height = float(raw[0]), float(raw[1])
            except (TypeError, ValueError):
                continue
            if width > 36 and height > 36:
                return width, height
    return A4


def _template_id(size: tuple[float, float]) -> str:
    return f"p{size[0]:.1f}x{size[1]:.1f}"


def _coalesce(blocks: list, translations: dict[int, str]) -> list[dict]:
    ordered = sorted(blocks, key=lambda block: (block.page, block.seq, block.idx))
    groups: dict[str, list] = {}
    for block in ordered:
        key = (block.meta or {}).get("split_from_block")
        if key:
            groups.setdefault(str(key), []).append(block)
    emitted: set[int] = set()
    items = []
    for block in ordered:
        identity = id(block)
        if identity in emitted:
            continue
        key = (block.meta or {}).get("split_from_block")
        siblings = groups.get(str(key), []) if key else []
        if key and len(siblings) > 1:
            text = " ".join((translations.get(item.idx, item.text) or "") for item in siblings).strip()
            for item in siblings:
                emitted.add(id(item))
            meta = dict(siblings[0].meta or {})
            boxes = [item.meta.get("bbox") for item in siblings if (item.meta or {}).get("bbox")]
            if boxes:
                meta["bbox"] = [
                    min(float(box[0]) for box in boxes),
                    min(float(box[1]) for box in boxes),
                    max(float(box[2]) for box in boxes),
                    max(float(box[3]) for box in boxes),
                ]
            items.append(
                {
                    "page": siblings[0].page,
                    "kind": siblings[0].kind,
                    "text": text,
                    "meta": meta,
                    "asset": None,
                }
            )
            continue
        emitted.add(identity)
        items.append(
            {
                "page": block.page,
                "kind": block.kind,
                "text": translations.get(block.idx, block.text) or "",
                "meta": dict(block.meta or {}),
                "asset": block.asset_path,
            }
        )
    return items


def _flow_image(asset: str, meta: dict, page_size: tuple[float, float], usable: float):
    bbox = (meta or {}).get("bbox")
    if bbox and len(bbox) >= 4 and page_size[0] > 0:
        box_w = abs(float(bbox[2]) - float(bbox[0]))
        box_h = abs(float(bbox[3]) - float(bbox[1]))
        if box_w > 1 and box_h > 1:
            width = min(usable, box_w / page_size[0] * page_size[0])
            return FlowImage(asset, width=width, height=width * box_h / box_w)
    with PILImage.open(asset) as image:
        scale = min(1, usable / max(image.width, 1))
        return FlowImage(asset, width=image.width * scale, height=image.height * scale)


def _render_reflow(extracted: ExtractedDocument, translations: dict[int, str], out_path: Path) -> None:
    fonts = ensure_fonts()
    font = ensure_font()
    styles = _fixed_styles(font)
    items = _coalesce(extracted.blocks, translations)
    grouped: list[tuple[int, list[dict]]] = []
    for item in items:
        if not grouped or grouped[-1][0] != item["page"]:
            grouped.append((item["page"], []))
        grouped[-1][1].append(item)
    pages: list[tuple[tuple[float, float], list]] = []
    for _page_no, group in grouped:
        size = _page_size_of([item["meta"] for item in group])
        usable = max(36, size[0] - 36 * mm)
        flows = []
        for index, item in enumerate(group):
            if item["kind"] == "image" and item["asset"] and Path(item["asset"]).exists():
                flows.append(_flow_image(item["asset"], item["meta"], size, usable))
                flows.append(Spacer(1, 6))
                continue
            if not str(item["text"]).strip():
                continue
            style = _style_for(item["kind"], item["meta"], fonts, styles, f"R{item['page']}-{index}", False)
            flows.append(Paragraph(escape(str(item["text"])).replace("\n", "<br/>"), style))
        pages.append((size, flows))
    if not pages:
        pages = [(A4, [Paragraph(" ", styles["paragraph"])])]
    sizes: list[tuple[float, float]] = []
    for size, _flows in pages:
        if not any(abs(size[0] - seen[0]) < 0.5 and abs(size[1] - seen[1]) < 0.5 for seen in sizes):
            sizes.append(size)
    doc = BaseDocTemplate(
        str(out_path),
        pagesize=sizes[0],
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
    )
    templates = []
    for size in sizes:
        frame_width = max(36, size[0] - doc.leftMargin - doc.rightMargin)
        frame_height = max(36, size[1] - doc.topMargin - doc.bottomMargin)
        frame = Frame(
            doc.leftMargin,
            doc.bottomMargin,
            frame_width,
            frame_height,
            id=_template_id(size),
            showBoundary=0,
            leftPadding=0,
            rightPadding=0,
            topPadding=0,
            bottomPadding=0,
        )
        templates.append(PageTemplate(id=_template_id(size), frames=[frame], pagesize=size))
    doc.addPageTemplates(templates)
    story = []
    for index, (size, flows) in enumerate(pages):
        if index:
            story.append(NextPageTemplate(_template_id(size)))
            story.append(PageBreak())
        story.extend(flows or [Paragraph(" ", styles["paragraph"])])
    if not story:
        story.append(Paragraph(" ", styles["paragraph"]))
    doc.build(story)


def _bbox_list(meta: dict) -> list[float] | None:
    raw = (meta or {}).get("bbox")
    if not isinstance(raw, (list, tuple)) or len(raw) < 4:
        return None
    try:
        box = [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
    except (TypeError, ValueError):
        return None
    if box[2] <= box[0] or box[3] <= box[1]:
        return None
    return box


def _floor_for(bbox: list[float], obstacles: list[list[float]]) -> float:
    left, bottom, right, top = bbox
    nearest = None
    for other in obstacles:
        if other is bbox:
            continue
        if min(right, other[2]) - max(left, other[0]) <= 1:
            continue
        if other[3] > bottom - 0.5:
            continue
        if nearest is None or other[3] > nearest:
            nearest = other[3]
    floor = _BOTTOM_MARGIN
    if nearest is not None:
        return max(floor, nearest + 2)
    return max(floor, bottom - max(4.0, top - bottom) * (_FREE_GROWTH - 1))


def _fit_paragraph(text: str, meta: dict, kind: str, bbox: list[float], obstacles: list[list[float]], fonts: dict, styles: dict):
    left, _bottom, right, top = bbox
    box_w = max(8.0, right - left)
    box_h = max(4.0, top - bbox[1])
    style = _style_for(kind, meta, fonts, styles, "layout-block", True)
    source_size = float(style.fontSize)
    min_size = source_size * 0.7
    ratio = style.leading / source_size if source_size else 1.2
    size = source_size
    paragraph = None
    height = box_h
    for _step in range(80):
        style.fontSize = size
        style.leading = max(size * ratio, size)
        paragraph = Paragraph(escape(str(text)).replace("\n", "<br/>"), style)
        _width, height = paragraph.wrap(box_w, 10_000)
        if height <= box_h or size <= min_size + 1e-6:
            break
        size = max(min_size, size - 0.5)
    if paragraph is None:
        return None
    if height > box_h:
        floor = _floor_for(bbox, obstacles)
        limit = max(box_h, top - floor)
        if height > limit:
            return None
    return paragraph, left, top - height


def _render_layout(source: Path, extracted: ExtractedDocument, translations: dict[int, str], out_path: Path) -> dict:
    fonts = ensure_fonts()
    styles = _fixed_styles(ensure_font())
    stripped = out_path.with_suffix(".stripped.pdf")
    overlay = out_path.with_suffix(".overlay.pdf")
    document = pdfium.PdfDocument(str(source))
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
    overflow: list[int] = []
    try:
        first = base[0]
        width, height = first.get_size()
        first.close()
        drawing = pdf_canvas.Canvas(str(overlay), pagesize=(width, height))
        items = _coalesce(extracted.blocks, translations)
        by_page: dict[int, list[dict]] = {}
        for item in items:
            by_page.setdefault(item["page"], []).append(item)
        for index in range(len(base)):
            page = base[index]
            width, height = page.get_size()
            if index:
                drawing.showPage()
            drawing.setPageSize((width, height))
            page_items = by_page.get(index + 1, [])
            obstacles = []
            for item in page_items:
                box = _bbox_list(item["meta"])
                if box:
                    item["box"] = box
                    obstacles.append(box)
            page_overflow = False
            for item in page_items:
                if item["kind"] == "image":
                    continue
                text = item["text"]
                box = item.get("box")
                if not box or not str(text).strip():
                    continue
                fitted = _fit_paragraph(str(text), item["meta"], item["kind"], box, obstacles, fonts, styles)
                if fitted is None:
                    page_overflow = True
                    break
                paragraph, origin_x, origin_y = fitted
                paragraph.drawOn(drawing, origin_x, origin_y)
            if page_overflow:
                overflow.append(index + 1)
            page.close()
        drawing.save()
    finally:
        page = None
        base.close()
    overflow_set = set(overflow)
    reader = PdfReader(str(stripped))
    over = PdfReader(str(overlay))
    writer = PdfWriter()
    extras = []
    for index, pdf_page in enumerate(reader.pages):
        page_no = index + 1
        if page_no in overflow_set:
            single = out_path.with_suffix(f".page{page_no}.pdf")
            extras.append(single)
            _reflow_page([block for block in extracted.blocks if block.page == page_no], translations, single)
            writer.add_page(PdfReader(str(single)).pages[0])
            continue
        added = writer.add_page(pdf_page)
        if index < len(over.pages):
            added.merge_page(over.pages[index])
    with out_path.open("wb") as handle:
        writer.write(handle)
    stripped.unlink(missing_ok=True)
    overlay.unlink(missing_ok=True)
    for extra in extras:
        extra.unlink(missing_ok=True)
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
