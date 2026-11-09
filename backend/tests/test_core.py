from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.core.analysis.page_classifier import classify_page, contrast_spread, laplacian_variance
from app.core.analysis.preprocess import preprocess_page
from app.core.chunking import chunk_blocks, segment_budget
from app.core.domain import BlockData
from app.core.formats.sniff import sniff_format
from app.core.model_registry import ModelSpec, cost_cents
from app.core.numbered import parse_numbered_blocks, usable_translation
from app.core.quality import flags_for
from app.core.errors import AppError
import pytest


def _spec(**overrides) -> ModelSpec:
    base = dict(
        id="tiny",
        alias="tiny",
        tokenizer="o200k_base",
        context_window=128000,
        max_output_tokens=16384,
        input_price_per_1m=0.15,
        output_price_per_1m=0.60,
        quality_tier="fast",
    )
    base.update(overrides)
    return ModelSpec(**base)


def _block(text: str, page: int = 1, seq: int = 0) -> BlockData:
    return BlockData.make(page=page, seq=seq, kind="paragraph", text=text)


def test_segment_budget_shrinks_for_small_context_and_output():
    wide = segment_budget(_spec(), "en", "de", 800, 2000)
    narrow = segment_budget(_spec(context_window=600, max_output_tokens=100), "en", "ja", 100, 2000)
    assert wide == 2000
    assert narrow < wide
    assert narrow >= 32


def test_chunk_respects_budget_and_splits_oversized_blocks():
    def count(text: str) -> int:
        return max(1, len(text.split()))

    blocks = [_block(" ".join(["word"] * 30), seq=0), _block("short sentence.", seq=1)]
    groups = chunk_blocks(blocks, 10, count)
    assert groups
    for group in groups:
        total = sum(count(block.text or "") for block in group.blocks)
        assert total <= 10 or len(group.blocks) == 1
    flat = [block for group in groups for block in group.blocks]
    assert any(block.meta.get("split_from_block") for block in flat)
    assert all(count(block.text or "") <= 10 for block in flat)


def test_rechunk_smaller_model_creates_more_segments():
    from app.core.chunking import rechunk_for_model

    blocks = [_block(f"Sentence {i} is long enough to matter.", seq=i) for i in range(12)]

    def count(text: str) -> int:
        return len(text)

    big = rechunk_for_model(blocks, _spec(segment_cap_tokens=5000, max_output_tokens=8000), "en", "de", 10, 5000, count)
    small = rechunk_for_model(blocks, _spec(segment_cap_tokens=80, max_output_tokens=80, context_window=500), "en", "de", 10, 80, count)
    assert len(small) > len(big)


def test_numbered_blocks_json_and_lines():
    found, missing, extra = parse_numbered_blocks('{"blocks":[{"n":1,"text":"Hallo"},{"n":2,"text":"Welt"}]}', [1, 2])
    assert found == {1: "Hallo", 2: "Welt"}
    assert missing == []
    assert extra == []
    found, missing, extra = parse_numbered_blocks("[1] one\n[2] two\n[4] extra", [1, 2, 3])
    assert missing == [3]
    assert extra == [4]


def test_cost_matches_prices():
    spec = _spec(input_price_per_1m=0.15, output_price_per_1m=0.60)
    cents = cost_cents(spec, 1_000_000, 1_000_000)
    assert cents == pytest.approx(75, abs=0.001)


def test_flags_detect_untranslated_and_glossary():
    assert "untranslated" in flags_for("Hello", "Hello", "en", "de", [])
    assert "glossary" in flags_for("Use Stark VARG today", "Nutze das Fahrrad heute", "en", "de", [{"source": "Stark VARG", "target": "Stark VARG"}])
    assert flags_for("Hello", "Hallo", "en", "de", []) == []
    english = "The motor is quiet and the chain is ready for the long ride."
    assert "script" in flags_for("Workshop notes about the sealed drivetrain.", english, "en", "de", [])
    italian = "Il motore resta sigillato durante la giornata di guida sul sentiero."
    assert "script" not in flags_for("The sealed motor stays quiet on the trail.", italian, "en", "it", [])
    assert usable_translation(english, "Workshop notes about the sealed drivetrain.", "de") is None
    assert usable_translation("Hello", "Hello", "de") is None
    assert usable_translation("[de] Hello", "Hello", "de") == "[de] Hello"
    assert usable_translation("Der Motor ist leise und die Kette ist bereit.", "The motor is quiet.", "de") == "Der Motor ist leise und die Kette ist bereit."


def test_sniff_rejects_edges():
    with pytest.raises(AppError) as empty:
        sniff_format(b"", "a.pdf")
    assert empty.value.code == "invalid_file"
    with pytest.raises(AppError) as exe:
        sniff_format(b"MZ\x00\x00", "run.exe")
    assert exe.value.code == "unsupported_type"
    with pytest.raises(AppError) as renamed:
        sniff_format(b"\x89PNG\r\n\x1a\n", "notes.pdf")
    assert renamed.value.code == "invalid_file"
    assert sniff_format(b"%PDF-1.4", "doc.pdf") == "pdf"


def _image(draw) -> Image.Image:
    image = Image.new("RGB", (800, 1000), "white")
    draw(ImageDraw.Draw(image))
    return image


def _body_font(size: int) -> ImageFont.ImageFont:
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _sparse_page(blur: int = 0) -> Image.Image:
    image = Image.new("RGB", (1600, 2100), "white")
    draw = ImageDraw.Draw(image)
    face = _body_font(56)
    y = 160
    for line in (
        "This scanned page describes the rear axle and the chain.",
        "Torque stays available from the first corner.",
        "Check the torque values before riding.",
    ):
        draw.text((90, y), line, fill="black", font=face)
        y += 84
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    return image


def test_classifier_pages():
    sharp = _image(lambda draw: [draw.line([(40, y), (760, y)], fill="black", width=3) for y in range(80, 900, 28)])
    assert classify_page("", sharp, 1, 300)[0] == "scanned_good"
    blurry = sharp.filter(ImageFilter.GaussianBlur(12))
    assert classify_page("", blurry, 1, 72)[0] == "scanned_poor"
    blank = Image.new("RGB", (400, 400), "white")
    assert classify_page("", blank, 1, 300)[0] == "blank"
    low = Image.new("RGB", (400, 400), (180, 180, 180))
    assert classify_page("", low, 1, 300)[0] in {"blank", "scanned_poor", "unknown"}
    sparse = _sparse_page()
    assert classify_page("", sparse, 1, 300)[0] == "scanned_good"
    assert classify_page("", _sparse_page(10), 1, 300)[0] == "scanned_poor"
    assert classify_page("", sparse, 1, 72)[0] == "scanned_poor"
    skewed = sharp.rotate(12, expand=True, fillcolor="white")
    assert classify_page("", skewed, 1, 300)[0] == "scanned_poor"
    abstract = Image.new("RGB", (400, 400), "white")
    paint = ImageDraw.Draw(abstract)
    for i in range(0, 400, 20):
        paint.rectangle([i, 0, i + 10, 400], fill=(i % 255, 40, 200))
    assert classify_page("", abstract, 1, None)[0] == "unknown"
    prose = "The quick brown fox jumps over the lazy dog. " * 8
    assert classify_page(prose, None, 0, None)[0] == "text"
    assert classify_page(prose, sharp, 0.4, 300)[0] == "text_with_images"
    assert classify_page("xqzpt vlkj qzxv wqzx mnbv", None, 0, None)[0] == "text_garbled"


def test_preprocess_improves_blurry_scan():
    image = _image(lambda draw: [draw.line([(40, y), (760, y)], fill="black", width=2) for y in range(80, 900, 36)])
    image = image.filter(ImageFilter.GaussianBlur(2))
    before_blur = laplacian_variance(image)
    before_contrast = contrast_spread(image)
    improved = preprocess_page(image, 1200)
    assert laplacian_variance(improved) >= before_blur * 0.5
    assert contrast_spread(improved) >= before_contrast


def test_fake_ocr_follows_ink():
    from app.core.analysis.page_classifier import png_bytes
    from app.core.ocr.fake_ocr import FakeOcrProvider

    ocr = FakeOcrProvider()
    sharp = ocr.transcribe(png_bytes(_sparse_page()), "")
    assert sharp.blocks and sharp.blocks[0].text.startswith("Transcribed")
    soft = ocr.transcribe(png_bytes(_sparse_page(10)), "")
    assert soft.blocks and soft.blocks[0].text == "[illegible]"
    blank = ocr.transcribe(png_bytes(Image.new("RGB", (400, 400), "white")), "")
    assert blank.blocks == []


def test_pdf_scan_routes_to_ocr(tmp_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from app.core.formats.pdf import PdfAdapter

    image_path = tmp_path / "scan.png"
    _sparse_page().save(image_path)
    src = tmp_path / "scan.pdf"
    page_w, page_h = A4
    drawing = canvas.Canvas(str(src), pagesize=A4)
    drawing.drawImage(str(image_path), 0, 0, width=page_w, height=page_h, preserveAspectRatio=True, anchor="c")
    drawing.showPage()
    drawing.save()
    page = PdfAdapter().extract(src, tmp_path).pages[0]
    assert page.page_class == "scanned_good"
    assert page.route == "ocr"


def test_pdf_round_trip_keeps_image(tmp_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from app.core.formats.pdf import PdfAdapter
    import pypdfium2 as pdfium

    src = tmp_path / "book.pdf"
    drawing = canvas.Canvas(str(src), pagesize=A4)
    drawing.setFont("Helvetica", 14)
    drawing.drawString(72, 760, "The figure below shows the motor housing and the cable route in detail.")
    img = tmp_path / "fig.png"
    image = Image.new("RGB", (120, 60), (20, 20, 20))
    ImageDraw.Draw(image).rectangle([8, 8, 100, 48], outline="white")
    image.save(img)
    drawing.drawImage(str(img), 72, 640, width=120, height=60)
    drawing.showPage()
    drawing.save()
    extracted = PdfAdapter().extract(src, tmp_path)
    assert extracted.page_count == 1
    assert any(block.kind == "paragraph" for block in extracted.blocks)
    assert any(block.kind == "image" for block in extracted.blocks)
    out = tmp_path / "out.pdf"
    translations = {block.idx: f"[de] {block.text}" for block in extracted.blocks if block.text}
    PdfAdapter().render(src, extracted, translations, out, "reflow")
    rendered = pdfium.PdfDocument(str(out))
    assert len(rendered) >= 1
    rendered.close()
    assert out.stat().st_size > 500


def test_layout_reflows_only_overflow_pages(tmp_path):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from app.core.formats.pdf import PdfAdapter
    import pypdfium2 as pdfium

    src = tmp_path / "two.pdf"
    drawing = canvas.Canvas(str(src), pagesize=A4)
    drawing.drawString(72, 760, "Short line on the first page.")
    drawing.showPage()
    drawing.drawString(72, 760, "Short line on the second page.")
    drawing.showPage()
    drawing.save()
    extracted = PdfAdapter().extract(src, tmp_path)
    translations = {}
    for position, block in enumerate(extracted.blocks, start=1):
        block.idx = position
        if not block.text:
            continue
        translations[block.idx] = ("translated " * 40) if block.page == 1 else "[de] short"
    out = tmp_path / "layout.pdf"
    report = PdfAdapter().render(src, extracted, translations, out, "layout")
    assert report["ok"] is True
    assert report["overflow_pages"] == [1]
    rendered = pdfium.PdfDocument(str(out))
    try:
        assert len(rendered) == 2
    finally:
        rendered.close()


def test_not_a_document_guardrail_trips():
    from app.core.agents.document_guardrail import ensure_is_document, pages_are_not_a_document
    from app.core.domain import PageInfo

    assert pages_are_not_a_document(["non_document", "non_document"])
    assert not pages_are_not_a_document(["blank"])
    with pytest.raises(AppError) as exc:
        ensure_is_document([PageInfo(page=1, page_class="non_document", route="reject")])
    assert exc.value.code == "not_a_document"
    ensure_is_document([PageInfo(page=1, page_class="text", route="text")])


class _ProviderError(Exception):
    def __init__(self, status_code: int, body=None, code: str | None = None, headers: dict | None = None) -> None:
        super().__init__("provider error")
        self.status_code = status_code
        self.body = body
        self.code = code
        self.response = type("Response", (), {"headers": headers or {}})()


def test_spend_limit_is_not_a_rate_limit():
    from app.core.errors import FundsExceeded, ProviderFailure, RateLimited
    from app.core.provider.openai_provider import raise_mapped

    spend = _ProviderError(
        429,
        {
            "error": {
                "message": "Your project has reached its configured enforced spend limit.",
                "type": "insufficient_quota",
                "param": None,
                "code": "project_spend_limit_exceeded",
            }
        },
    )
    with pytest.raises(FundsExceeded) as funds:
        raise_mapped(spend)
    assert funds.value.code == "funds_exceeded"
    assert funds.value.retryable is False

    quota = _ProviderError(429, {"error": {"message": "You exceeded your current quota.", "type": "insufficient_quota", "code": "insufficient_quota"}})
    with pytest.raises(FundsExceeded):
        raise_mapped(quota)

    billing = _ProviderError(429, code="billing_hard_limit_reached")
    with pytest.raises(FundsExceeded):
        raise_mapped(billing)

    limited = _ProviderError(
        429,
        {"error": {"message": "Rate limit reached for gpt-4o-mini.", "type": "tokens", "code": "rate_limit_exceeded"}},
        headers={"retry-after": "2"},
    )
    with pytest.raises(RateLimited) as rate:
        raise_mapped(limited)
    assert rate.value.retryable is True
    assert rate.value.retry_after == 2

    with pytest.raises(RateLimited):
        raise_mapped(_ProviderError(429))

    with pytest.raises(ProviderFailure):
        raise_mapped(_ProviderError(500, {"error": {"message": "server error"}}))


def test_meta_models_include_segment_budget():
    from app.api.routes import models

    rows = models("en", "de")
    assert rows
    assert all(row["segment_budget"] >= 32 for row in rows)
    assert {row["alias"] for row in rows} >= {"gpt-4o-mini", "gpt-4o"}
