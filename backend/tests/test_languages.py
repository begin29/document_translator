import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import text

from app.config import get_settings
from app.core.langdetect import detect_language
from app.core.languages import EXPANSION, LANGUAGES, known_language, language_name
from app.core.numbered import usable_translation
from app.core.prompts import PROMPT_VERSION, system_prompt
from app.core.quality import flags_for
from app.core.service import create_document, create_job, process_available, reap, reset_runtime, run_analysis, sync_gate
from app.db.models import Block, BlockTranslation
from app.db.session import reset_engine, session_factory

UK_PARAGRAPH = "Ми працюємо над тим, щоб документи перекладалися швидко і якісно. Це важливо для наших клієнтів."
UK_SENTENCE = "Ми працюємо над тим, щоб документи перекладалися швидко і якісно."
UK_SHARED = "Документи для клієнтів на столі в офісі."
RU_PARAGRAPH = "Это большой город, который был построен давно. Она сказала, что погода будет хорошей."
TIE = "Документ номер один про мотор."
MIXED = "Це был один документ про мотор."
EN_SOURCE = "We are working so that documents are translated quickly and accurately. This matters for our clients."


def test_detect_language_ukrainian_russian_and_tie():
    assert detect_language(UK_PARAGRAPH) == "uk"
    assert detect_language(UK_SENTENCE) == "uk"
    assert detect_language(UK_SHARED) == "uk"
    assert detect_language(RU_PARAGRAPH) == "ru"
    assert detect_language(MIXED) is None
    assert detect_language(TIE) is None


def test_ukrainian_output_is_usable():
    assert "script" not in flags_for(EN_SOURCE, UK_PARAGRAPH, "en", "uk", [])
    assert usable_translation(UK_PARAGRAPH, EN_SOURCE, "uk") == UK_PARAGRAPH


def test_none_detection_does_not_add_script():
    source = "A short note about the motor housing."
    assert detect_language(TIE) is None
    assert "script" not in flags_for(source, TIE, "en", "uk", [])


def test_prompt_names_languages_and_drops_russian_target():
    prompt = system_prompt("en", "uk", None, None)
    assert "Ukrainian" in prompt
    assert " to uk." not in prompt
    assert "Write the translation in Ukrainian (uk)." in prompt
    assert PROMPT_VERSION == "v2"
    assert all(item["code"] != "ru" for item in LANGUAGES)
    assert ("en", "ru") not in EXPANSION
    assert known_language("ru") is False
    assert known_language("uk") is True
    assert language_name("ru") == "Russian (ru)"
    assert language_name("uk") == "Ukrainian (uk)"
    assert language_name(None) == "auto-detected"
    assert language_name("auto") == "auto-detected"
    assert language_name("zz") == "zz"


def test_fake_uk_tag_fails_script_check():
    source = "The motor housing is sealed against dust and water during every ride."
    tagged = f"[uk] {source}"
    assert "script" in flags_for(source, tagged, "en", "uk", [])
    assert usable_translation(tagged, source, "uk") is None


def _pdf(path: Path, paragraphs: list[str]) -> None:
    drawing = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for paragraph in paragraphs:
        drawing.drawString(72, y, paragraph[:110])
        y -= 24
    drawing.save()


@pytest.fixture(scope="module")
def _migrated():
    get_settings.cache_clear()
    reset_engine()
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", os.environ["TEST_DATABASE_URL"])
    command.upgrade(cfg, "head")
    yield


@pytest.fixture()
def session(_migrated):
    get_settings.cache_clear()
    reset_engine()
    reset_runtime()
    factory = session_factory()
    with factory() as db:
        db.execute(
            text(
                "TRUNCATE documents, blocks, translation_jobs, segments, block_translations, translation_memory, job_events, provider_calls, ocr_pages RESTART IDENTITY CASCADE"
            )
        )
        db.execute(text("UPDATE provider_gate SET inflight=0, max_inflight=0, effective_max=ceiling, window_tokens=0, last_429_at=NULL"))
        db.commit()
    with factory() as db:
        sync_gate(db)
        db.commit()
        yield db


def _drain(db, limit=80):
    for _ in range(limit):
        reap(db)
        db.commit()
        worked = process_available(db, "test")
        db.commit()
        if not worked:
            return


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL is not set")
def test_fake_uk_job_reaches_done_and_stores_source(session, tmp_path):
    paragraph = "The motor housing is sealed against dust and water during every ride."
    path = tmp_path / "uk.pdf"
    _pdf(path, [paragraph])
    document = create_document(session, "uk.pdf", path.read_bytes())
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    job = create_job(session, document.id, "uk", None, "gpt-4o-mini", None, None)
    session.commit()
    _drain(session)
    session.refresh(job)
    assert job.status == "done"
    blocks = session.query(Block).filter(Block.document_id == document.id, Block.kind != "image").all()
    stored = {
        row.block_idx: row.tgt_text
        for row in session.query(BlockTranslation).filter(BlockTranslation.job_id == job.id)
    }
    assert stored
    text_blocks = [block for block in blocks if block.text]
    assert text_blocks
    for block in text_blocks:
        assert stored[block.idx] == block.text, (
            "FakeProvider emits [uk] plus the English source. "
            "The Cyrillic script check drops that output, so the pipeline stores the source."
        )
