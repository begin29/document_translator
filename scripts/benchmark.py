"""Run one fixture through the fake provider and print cost and latency."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

os.environ.setdefault("PROVIDER", "fake")
os.environ.setdefault("APP_ENV", "dev")

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlalchemy import text

from app.config import get_settings
from app.core.service import create_document, create_job, process_available, reap, run_analysis, sync_gate
from app.db.session import session_factory


def _pdf(path: Path) -> None:
    drawing = canvas.Canvas(str(path), pagesize=A4)
    y = 800
    for index in range(12):
        drawing.drawString(50, y, f"Paragraph {index}: the sealed motor stays quiet on a long stage.")
        y -= 40
    drawing.save()


def main() -> None:
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL is required")
    get_settings.cache_clear()
    path = Path(tempfile.mkdtemp()) / "bench.pdf"
    _pdf(path)
    factory = session_factory()
    with factory() as session:
        sync_gate(session)
        document = create_document(session, "bench.pdf", path.read_bytes())
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
                job = session.get(type(job), job_id)
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


if __name__ == "__main__":
    main()
