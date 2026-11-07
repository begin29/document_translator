from __future__ import annotations

import re

import pypdfium2 as pdfium
import pytest
from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from PIL import Image
from reportlab.lib.colors import HexColor, black
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

from app.core.domain import BlockData, ExtractedDocument
from app.core.formats.docx import DocxAdapter
from app.core.formats.fonts import ensure_font, ensure_fonts
from app.core.formats.pdf import PdfAdapter


HEADING = "Motor housing"
BODY = (
    "The motor housing protects the windings and routes the cable through the sealed gland. "
    "It is machined from aluminium and checked against the drawing before the cover is fitted. "
    "A third sentence keeps the paragraph long enough to wrap onto three lines at this width."
)
CAPTION = "Figure 1. Housing cross-section."


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _longer(text: str, ratio: float = 1.3) -> str:
    target = max(len(text) + 1, int(round(len(text) * ratio)))
    words = text.split() or ["text"]
    output = text
    index = 0
    while len(output) < target and index < 40:
        output = f"{output} {words[index % len(words)]}"
        index += 1
    return output


def _assign(extracted: ExtractedDocument) -> None:
    for index, block in enumerate(extracted.blocks, start=1):
        block.idx = index


def _styled_pdf(path) -> None:
    drawing = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    drawing.setFillColor(black)
    drawing.setFont("Helvetica-Bold", 20)
    drawing.drawCentredString(width / 2, height - 72, HEADING)
    style = ParagraphStyle(
        "body",
        fontName="Helvetica",
        fontSize=10,
        leading=14,
        textColor=HexColor("#1f3a93"),
        alignment=TA_JUSTIFY,
    )
    paragraph = Paragraph(BODY, style)
    _wrapped, wrapped_height = paragraph.wrap(width - 144, 1000)
    paragraph.drawOn(drawing, 72, height - 112 - wrapped_height)
    drawing.setFillColor(HexColor("#333333"))
    drawing.setFont("Helvetica-Oblique", 9)
    drawing.drawString(72, height - 112 - wrapped_height - 28, CAPTION)
    image_path = path.parent / "figure.png"
    image = Image.new("RGB", (120, 60), (20, 20, 20))
    image.save(image_path)
    drawing.drawImage(str(image_path), 72, 72, width=120, height=60)
    drawing.showPage()
    drawing.save()


def _glyphs(path):
    found = []
    document = pdfium.PdfDocument(str(path))
    try:
        for page in document:
            textpage = page.get_textpage()
            try:
                count = pdfium.raw.FPDFText_CountChars(textpage.raw)
                for index in range(count):
                    code = int(pdfium.raw.FPDFText_GetUnicode(textpage.raw, index) or 0)
                    if code in (0, 10, 13) or code > 0x10FFFF:
                        continue
                    try:
                        left, bottom, right, top = textpage.get_charbox(index)
                    except Exception:
                        continue
                    size = float(pdfium.raw.FPDFText_GetFontSize(textpage.raw, index) or 0)
                    found.append((chr(code), size, (float(left), float(bottom), float(right), float(top)), page.get_size()))
            finally:
                textpage.close()
    finally:
        document.close()
    return found


def _inside(glyphs, bbox) -> str:
    left, bottom, right, top = bbox
    chosen = []
    for character, _size, box, _page in glyphs:
        center_x = (box[0] + box[2]) / 2
        center_y = (box[1] + box[3]) / 2
        if left - 1 <= center_x <= right + 1 and bottom - 1 <= center_y <= top + 1:
            chosen.append(character)
    return _squash("".join(chosen))


def _block(blocks, text: str):
    wanted = _squash(text)
    for block in blocks:
        if block.text and _squash(block.text) == wanted:
            return block
    raise AssertionError(text)


def _page_count(path) -> int:
    document = pdfium.PdfDocument(str(path))
    try:
        return len(document)
    finally:
        document.close()


def _page_size(path, index: int = 0):
    document = pdfium.PdfDocument(str(path))
    try:
        return document[index].get_size()
    finally:
        document.close()


def test_registered_fonts_cover_family_weights():
    faces = ensure_fonts()
    assert set(faces) == {
        ("sans", False, False),
        ("sans", True, False),
        ("sans", False, True),
        ("sans", True, True),
        ("serif", False, False),
        ("serif", True, False),
        ("serif", False, True),
        ("serif", True, True),
        ("mono", False, False),
        ("mono", True, False),
        ("mono", False, True),
        ("mono", True, True),
    }
    assert ensure_font() == faces[("sans", False, False)]
    assert faces[("sans", True, False)]
    assert faces[("sans", False, True)]


def test_extract_keeps_source_style(tmp_path):
    src = tmp_path / "styled.pdf"
    _styled_pdf(src)
    extracted = PdfAdapter().extract(src, tmp_path)
    glyphs = _glyphs(src)
    text_blocks = [block for block in extracted.blocks if block.kind != "image"]
    heading = _block(text_blocks, HEADING)
    body = _block(text_blocks, BODY)
    caption = _block(text_blocks, CAPTION)
    assert heading.kind == "heading"
    assert heading.meta["font_size"] == pytest.approx(20, abs=1)
    assert heading.meta["bold"] is True
    assert heading.meta["align"] == "center"
    assert body.meta["color"].lower() == "#1f3a93"
    assert body.meta["align"] == "justify"
    assert caption.meta["font_size"] == pytest.approx(9, abs=1)
    assert caption.meta["italic"] is True
    for block in text_blocks:
        assert tuple(block.meta["page_size"]) == letter
        left, bottom, right, top = block.meta["bbox"]
        assert 0 <= left < right <= letter[0] + 1
        assert 0 <= bottom < top <= letter[1] + 1
        assert _inside(glyphs, block.meta["bbox"]).replace(" ", "") == _squash(block.text).replace(" ", "")
    assert body.meta["bbox"][3] - body.meta["bbox"][1] > body.meta["font_size"] * 2
    images = [block for block in extracted.blocks if block.kind == "image"]
    assert len(images) == 1
    assert images[0].meta["bbox"]
    assert tuple(images[0].meta["page_size"]) == letter


def test_layout_keeps_page_and_heading_weight(tmp_path):
    src = tmp_path / "styled.pdf"
    _styled_pdf(src)
    extracted = PdfAdapter().extract(src, tmp_path)
    _assign(extracted)
    source_body = _block(extracted.blocks, BODY)
    source_x = source_body.meta["bbox"][0]
    translations = {block.idx: _longer(block.text) for block in extracted.blocks if block.text}
    out = tmp_path / "layout.pdf"
    report = PdfAdapter().render(src, extracted, translations, out, "layout")
    assert report["ok"] is True
    assert report["mode"] == "layout"
    assert report["overflow_pages"] == []
    assert _page_count(out) == 1
    width, height = _page_size(out)
    assert width == pytest.approx(letter[0], abs=0.5)
    assert height == pytest.approx(letter[1], abs=0.5)
    rendered = PdfAdapter().extract(out, tmp_path / "layout-extract")
    heading = _block(rendered.blocks, translations[next(block.idx for block in extracted.blocks if _squash(block.text) == HEADING)])
    body = _block(rendered.blocks, translations[next(block.idx for block in extracted.blocks if _squash(block.text) == BODY)])
    assert heading.meta["font_size"] >= 14
    assert heading.meta["bold"] is True
    assert abs(body.meta["bbox"][0] - source_x) <= 5


def test_reflow_keeps_letter_and_heading_hierarchy(tmp_path):
    src = tmp_path / "styled.pdf"
    _styled_pdf(src)
    extracted = PdfAdapter().extract(src, tmp_path)
    _assign(extracted)
    translations = {block.idx: f"[de] {block.text}" for block in extracted.blocks if block.text}
    out = tmp_path / "reflow.pdf"
    report = PdfAdapter().render(src, extracted, translations, out, "reflow")
    assert report["mode"] == "reflow"
    width, height = _page_size(out)
    assert width == pytest.approx(letter[0], abs=0.5)
    assert height == pytest.approx(letter[1], abs=0.5)
    heading_idx = next(block.idx for block in extracted.blocks if _squash(block.text) == HEADING)
    body_idx = next(block.idx for block in extracted.blocks if _squash(block.text) == BODY)
    glyphs = _glyphs(out)
    text = "".join(character for character, _size, _box, _page in glyphs)
    heading_at = text.find(translations[heading_idx][:12])
    body_at = text.find(translations[body_idx][:12])
    assert heading_at >= 0 and body_at >= 0
    heading_size = max(size for index, (_character, size, _box, _page) in enumerate(glyphs) if heading_at <= index < heading_at + 8)
    body_size = max(size for index, (_character, size, _box, _page) in enumerate(glyphs) if body_at <= index < body_at + 8)
    assert heading_size > body_size


def test_missing_style_falls_back(tmp_path):
    src = tmp_path / "plain.pdf"
    drawing = canvas.Canvas(str(src), pagesize=A4)
    drawing.drawString(72, 760, "Plain sentence for an old document.")
    drawing.showPage()
    drawing.save()
    block = BlockData.make(page=1, seq=0, kind="paragraph", text="Plain sentence for an old document.", meta={})
    block.idx = 1
    extracted = ExtractedDocument("pdf", 1, [], [block], None)
    out = tmp_path / "old-reflow.pdf"
    PdfAdapter().render(src, extracted, {1: "Alter Satz."}, out, "reflow")
    width, height = _page_size(out)
    assert width == pytest.approx(A4[0], abs=0.5)
    assert height == pytest.approx(A4[1], abs=0.5)
    placed = BlockData.make(
        page=1,
        seq=0,
        kind="paragraph",
        text="Plain sentence for an old document.",
        meta={"bbox": [72, 748, 360, 764]},
    )
    placed.idx = 1
    layout_out = tmp_path / "old-layout.pdf"
    report = PdfAdapter().render(src, ExtractedDocument("pdf", 1, [], [placed], None), {1: "Alter Satz."}, layout_out, "layout")
    assert report["ok"] is True
    assert _page_count(layout_out) == 1


def test_split_siblings_join_before_fit(tmp_path):
    src = tmp_path / "blank.pdf"
    drawing = canvas.Canvas(str(src), pagesize=letter)
    drawing.showPage()
    drawing.save()
    meta = {
        "split_from_block": "group",
        "bbox": [72, 700, 360, 716],
        "page_size": [letter[0], letter[1]],
        "font_size": 12,
        "family": "sans",
    }
    first = BlockData.make(page=1, seq=0, kind="paragraph", text="Hello there", meta=dict(meta))
    second = BlockData.make(page=1, seq=1, kind="paragraph", text="friend", meta=dict(meta))
    first.idx = 1
    second.idx = 2
    extracted = ExtractedDocument("pdf", 1, [], [first, second], None)
    out = tmp_path / "joined.pdf"
    PdfAdapter().render(src, extracted, {1: "Hallo", 2: "Welt"}, out, "reflow")
    text = "".join(character for character, _size, _box, _page in _glyphs(out))
    assert "Hallo" in text and "Welt" in text
    assert text.count("Hallo") == 1
    layout_out = tmp_path / "joined-layout.pdf"
    report = PdfAdapter().render(src, extracted, {1: "Hallo", 2: "Welt"}, layout_out, "layout")
    assert report["overflow_pages"] == []
    layout_text = "".join(character for character, _size, _box, _page in _glyphs(layout_out))
    assert layout_text.count("Hallo") == 1
    assert "Welt" in layout_text


def _drawing_run(paragraph):
    run = paragraph.add_run()
    drawing = OxmlElement("w:drawing")
    drawing.append(OxmlElement("w:inline"))
    run._element.append(drawing)
    return run


def _field_runs(paragraph):
    begin = paragraph.add_run()
    start = OxmlElement("w:fldChar")
    start.set(qn("w:fldCharType"), "begin")
    begin._element.append(start)
    instruction = paragraph.add_run()
    node = OxmlElement("w:instrText")
    node.set(qn("xml:space"), "preserve")
    node.text = " PAGE "
    instruction._element.append(node)
    separate = paragraph.add_run()
    middle = OxmlElement("w:fldChar")
    middle.set(qn("w:fldCharType"), "separate")
    separate._element.append(middle)
    paragraph.add_run("3")
    end = paragraph.add_run()
    stop = OxmlElement("w:fldChar")
    stop.set(qn("w:fldCharType"), "end")
    end._element.append(stop)


def _hyperlink(paragraph, text: str):
    relation = paragraph.part.relate_to("https://example.com/specs", RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), relation)
    run = OxmlElement("w:r")
    node = OxmlElement("w:t")
    node.text = text
    run.append(node)
    link.append(run)
    paragraph._p.append(link)


def test_docx_uses_dominant_run_and_keeps_drawing(tmp_path):
    src = tmp_path / "notes.docx"
    document = Document()
    paragraph = document.add_paragraph()
    lead = paragraph.add_run("Note:")
    lead.bold = True
    body = paragraph.add_run(" long sentence about the motor housing and the cable route.")
    body.bold = False
    _drawing_run(paragraph)
    _field_runs(paragraph)
    _hyperlink(paragraph, "see specs")
    table = document.add_table(rows=1, cols=1)
    cell = table.cell(0, 0)
    label = cell.paragraphs[0]
    marked = label.add_run("Label")
    marked.bold = True
    _drawing_run(label)
    longer = cell.add_paragraph().add_run("A much longer cell sentence about the motor housing route.")
    longer.bold = False
    document.save(src)
    extracted = DocxAdapter().extract(src, tmp_path)
    _assign(extracted)
    translations = {}
    for block in extracted.blocks:
        if block.kind == "table_cell":
            translations[block.idx] = "Translated cell sentence."
        elif block.text:
            translations[block.idx] = "Translated note about the housing."
    out = tmp_path / "notes-out.docx"
    DocxAdapter().render(src, extracted, translations, out, "docx")
    rendered = Document(str(out))
    runs = rendered.paragraphs[0].runs
    assert runs[0].bold is True
    assert runs[0].text == ""
    assert runs[1].bold is not True
    assert runs[1].text == "Translated note about the housing."
    assert runs[2]._element.xpath(".//w:drawing")
    xml = rendered.paragraphs[0]._p.xml
    assert "PAGE" in xml
    assert ">3<" in xml or ">3</w:t>" in xml
    links = rendered.paragraphs[0]._p.xpath(".//w:hyperlink//w:t")
    assert "".join(node.text or "" for node in links) == "see specs"
    cell_out = rendered.tables[0].cell(0, 0)
    assert len(cell_out.paragraphs) == 2
    joined = "\n".join(item.text for item in cell_out.paragraphs)
    assert joined.count("Translated cell sentence.") == 1
    assert "Label" not in joined
    assert "much longer" not in joined
    assert len(cell_out.paragraphs[0]._p.xpath(".//w:drawing")) == 1
