"""Run one fixture through the fake provider and print cost and latency."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

os.environ.setdefault("PROVIDER", "fake")
os.environ.setdefault("APP_ENV", "dev")

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import text

from app.auth.passwords import hash_password
from app.config import get_settings
from app.core.service import create_document, create_job, estimate_for, process_available, public_estimate, reap, run_analysis, sync_gate
from app.db.models import Document, TranslationJob, User
from app.db.session import session_factory


def _pdf(path: Path) -> None:
    drawing = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for index in range(12):
        drawing.drawString(50, y, f"Paragraph {index}: the sealed motor stays quiet on a long stage.")
        y -= 40
    drawing.save()


def _owner(session) -> User:
    user = User(email=f"bench-{uuid.uuid4()}@example.com", password_hash=hash_password("benchmark-pass-1"))
    session.add(user)
    session.flush()
    return user


def _percent(estimate, actual) -> str:
    if estimate in (None, 0):
        return "n/a"
    error = (Decimal(actual) - Decimal(estimate)) / Decimal(estimate) * Decimal(100)
    return f"{error:.1f}%"


def _compare(session, limit: int) -> None:
    jobs = session.execute(
        text(
            """
            SELECT id, document_id, model, source_lang, target_lang, estimate_cents, cost_cents
            FROM translation_jobs
            WHERE status = 'done'
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    ).mappings().all()
    if not jobs:
        print("no done jobs")
        return
    for job in jobs:
        stages = session.execute(
            text(
                """
                SELECT stage,
                       count(*) AS calls,
                       COALESCE(sum(tokens_in), 0) AS tokens_in,
                       COALESCE(sum(tokens_out), 0) AS tokens_out,
                       COALESCE(sum(cost_cents), 0) AS cost_cents
                FROM provider_calls
                WHERE job_id = :id
                GROUP BY stage
                ORDER BY stage
                """
            ),
            {"id": job["id"]},
        ).mappings().all()
        actual = sum((row["cost_cents"] or 0) for row in stages)
        estimate = job["estimate_cents"] or 0
        print(
            f"job={job['id']} model={job['model']} target={job['target_lang']} "
            f"estimate_cents={estimate} actual_cents={actual} error={_percent(estimate, actual)}"
        )
        document = session.get(Document, job["document_id"])
        quote = None
        if document is not None:
            quote = public_estimate(
                estimate_for(
                    session,
                    document,
                    job["target_lang"],
                    job["model"],
                    source_lang=job["source_lang"],
                    apply_tm=False,
                )
            )
        by_stage = {row["stage"]: row for row in stages}
        translate_estimate = None
        if quote is not None:
            translate_estimate = Decimal(str(quote["cost_cents"])) - Decimal(str(quote["ocr_cost_cents"])) - Decimal(str(quote["summary_cost_cents"]))
        compared = {
            "translate": translate_estimate,
            "summary": None if quote is None else quote["summary_cost_cents"],
            "ocr": None if quote is None else quote["ocr_cost_cents"],
        }
        seen = set()
        for stage, expected in compared.items():
            seen.add(stage)
            row = by_stage.get(stage)
            actual_stage = 0 if row is None else row["cost_cents"]
            calls = 0 if row is None else row["calls"]
            tokens_in = 0 if row is None else row["tokens_in"]
            tokens_out = 0 if row is None else row["tokens_out"]
            print(
                f"  {stage}: calls={calls} tokens_in={tokens_in} tokens_out={tokens_out} "
                f"estimate_cents={expected if expected is not None else 'n/a'} actual_cents={actual_stage} "
                f"error={_percent(expected, actual_stage)}"
            )
        for stage, row in by_stage.items():
            if stage in seen:
                continue
            print(
                f"  {stage}: calls={row['calls']} tokens_in={row['tokens_in']} tokens_out={row['tokens_out']} "
                f"estimate_cents=0 actual_cents={row['cost_cents']} error={_percent(0, row['cost_cents'])}"
            )


def _run_one(session) -> None:
    sync_gate(session)
    path = Path(tempfile.mkdtemp()) / "bench.pdf"
    _pdf(path)
    owner = _owner(session)
    document = create_document(session, path.read_bytes(), "bench.pdf", owner.id)
    session.commit()
    run_analysis(session, document.id)
    session.commit()
    job = create_job(session, document.id, "de", None, "gpt-4o-mini", None, None)
    session.commit()
    job_id = job.id
    for _ in range(100):
        reap(session)
        if not process_available(session, "bench"):
            session.commit()
            job = session.get(TranslationJob, job_id)
            if job.status in {"done", "failed"}:
                break
        session.commit()
    rows = session.execute(
        text(
            """
            SELECT stage, count(*), sum(tokens_in), sum(tokens_out), sum(cost_cents),
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
            FROM provider_calls WHERE job_id = :id GROUP BY stage
            """
        ),
        {"id": job_id},
    ).all()
    total = session.execute(
        text("SELECT sum(cost_cents), percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) FROM provider_calls WHERE job_id=:id"),
        {"id": job_id},
    ).one()
    print(f"status={job.status} cost_cents={total[0]} p95_latency_ms={total[1]}")
    for stage, calls, tin, tout, cents, p95 in rows:
        print(f"{stage}: calls={calls} tokens_in={tin} tokens_out={tout} cost_cents={cents} p95_ms={p95}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare-estimate", nargs="?", const=10, type=int, default=None)
    args = parser.parse_args()
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is required")
    get_settings.cache_clear()
    factory = session_factory()
    with factory() as session:
        if args.compare_estimate is not None:
            _compare(session, args.compare_estimate)
            return
        _run_one(session)


if __name__ == "__main__":
    main()
