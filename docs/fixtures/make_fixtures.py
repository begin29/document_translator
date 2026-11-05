from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from docx import Document
from docx.shared import Inches
from pypdf import PdfReader, PdfWriter


ROOT = Path(__file__).resolve().parent / "generated"


def _pdf(path: Path, pages: list[list[str]]) -> None:
    drawing = canvas.Canvas(str(path), pagesize=A4)
    for lines in pages:
        y = 800
        for line in lines:
            drawing.drawString(50, y, line[:110])
            y -= 18
        drawing.showPage()
    drawing.save()


def _page_image(text: str | None, blur: int = 0, dpi: int = 300, colorful: bool = False) -> Image.Image:
    image = Image.new("RGB", (1200, 1600), "white" if not colorful else (20, 20, 20))
    draw = ImageDraw.Draw(image)
    if colorful:
        for i in range(0, 1200, 30):
            draw.rectangle([i, 0, i + 15, 1600], fill=((i * 3) % 255, 80, 180))
        return image
    if text:
        y = 80
        for line in text.splitlines():
            draw.text((60, y), line[:80], fill="black")
            y += 36
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    image.info["dpi"] = (dpi, dpi)
    return image


def _embed_image_pdf(path: Path, image: Image.Image, text: str | None) -> None:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    img_path = path.with_suffix(".tmp.png")
    img_path.write_bytes(buffer.getvalue())
    drawing = canvas.Canvas(str(path), pagesize=A4)
    if text:
        drawing.drawString(50, 800, text[:110])
    drawing.drawImage(str(img_path), 50, 400, width=200, height=260, preserveAspectRatio=True, mask="auto")
    drawing.showPage()
    drawing.save()
    img_path.unlink(missing_ok=True)


def build(large: bool) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    prose = "The Stark motor is quiet, sealed, and built for a full day of riding."
    _pdf(ROOT / "short.pdf", [[prose, "A second sentence keeps the page in the text class."], ["The second page repeats the service note in plain text."]])
    _pdf(ROOT / "empty_text.pdf", [[" "]])
    (ROOT / "zero_byte.pdf").write_bytes(b"")
    (ROOT / "corrupt_truncated.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>")
    (ROOT / "png_renamed.pdf").write_bytes(b"\x89PNG\r\n\x1a\nnot-really")
    (ROOT / "unsupported.exe").write_bytes(b"MZ\x00\x00fake")
    (ROOT / "oversize.pdf").write_bytes(b"%PDF-1.4\n" + b"0" * (41 * 1024 * 1024))
    _pdf(ROOT / "repeat_paragraph_50x.pdf", [[prose] * 50])
    _pdf(ROOT / "unicode_cjk.pdf", [["エンジンは静かに動き、丘を越えて走り続ける。"]])
    _pdf(ROOT / "garbled_font.pdf", [["xqzpt vlkj qzxv wqzx mnbv qzxv wqzx qzxv"]])
    scanned = _page_image("This scanned page describes the rear axle and the chain.\nTorque stays available from the first corner.", blur=0)
    _embed_image_pdf(ROOT / "scanned_300dpi.pdf", scanned, None)
    poor = _page_image("Blurry axle note\nLow contrast copy", blur=8)
    _embed_image_pdf(ROOT / "scanned_72dpi.pdf", poor, None)
    blank = Image.new("RGB", (800, 1000), (250, 250, 250))
    _embed_image_pdf(ROOT / "scanned_blank.pdf", blank, None)
    _pdf(ROOT / "scanned_mixed.pdf", [[prose]])
    mixed = canvas.Canvas(str(ROOT / "scanned_mixed.pdf"), pagesize=A4)
    mixed.drawString(50, 800, prose)
    mixed.showPage()
    scanned_path = ROOT / "_scan.png"
    scanned.save(scanned_path)
    for _ in range(2):
        mixed.drawImage(str(scanned_path), 40, 200, width=400, height=520, preserveAspectRatio=True, mask="auto")
        mixed.showPage()
    mixed.save()
    scanned_path.unlink(missing_ok=True)
    figure = _page_image(None)
    ImageDraw.Draw(figure).rectangle([100, 100, 500, 400], outline="black", width=4)
    book = canvas.Canvas(str(ROOT / "book_text_with_figures.pdf"), pagesize=A4)
    fig_path = ROOT / "_fig.png"
    figure.save(fig_path)
    for page in range(20):
        book.drawString(50, 800, f"Chapter note {page}. The housing carries the caption under the drawing.")
        book.drawImage(str(fig_path), 50, 420, width=180, height=220, preserveAspectRatio=True, mask="auto")
        book.showPage()
    book.save()
    fig_path.unlink(missing_ok=True)
    photo = _page_image("Photo of a service page.\nCheck the torque values before riding.")
    photo.save(ROOT / "photo_page.jpg", format="JPEG", quality=90)
    photo.filter(ImageFilter.GaussianBlur(10)).save(ROOT / "photo_blurry.jpg", format="JPEG", quality=30)
    photo.rotate(18, expand=True, fillcolor="white").save(ROOT / "photo_skewed.jpg", format="JPEG", quality=85)
    _page_image(None, colorful=True).save(ROOT / "non_document.jpg", format="JPEG", quality=85)
    doc = Document()
    doc.add_heading("Service notes", level=1)
    doc.add_paragraph("Replace the seal before the next race weekend.")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Part"
    table.cell(0, 1).text = "Torque"
    table.cell(1, 0).text = "Axle nut"
    table.cell(1, 1).text = "50 Nm"
    doc.save(ROOT / "short.docx")
    styled = Document()
    styled.add_paragraph("Keep the inline drawing.", style=None)
    styled.save(ROOT / "tables_styles.docx")
    docx_images = Document()
    docx_images.add_paragraph("Caption beside the figure.")
    img = ROOT / "_inline.png"
    Image.new("RGB", (40, 40), "black").save(img)
    docx_images.add_picture(str(img), width=Inches(0.5))
    docx_images.save(ROOT / "docx_with_images.docx")
    img.unlink(missing_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook/>")
    (ROOT / "unsupported.xlsx").write_bytes(buffer.getvalue())
    reader = PdfReader(str(ROOT / "short.pdf"))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt("secret")
    with (ROOT / "encrypted.pdf").open("wb") as handle:
        writer.write(handle)
    _pdf(ROOT / "passwd.pdf", [["This file is uploaded under a traversal name in the QA plan."]])
    if large:
        _pdf(ROOT / "long_200p.pdf", [[prose, f"Page marker {i}"] for i in range(200)])
        _pdf(ROOT / "huge_400p.pdf", [[prose, f"Page marker {i}"] for i in range(400)])
    print(f"wrote fixtures to {ROOT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--large", action="store_true")
    build(parser.parse_args().large)
