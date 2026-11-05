from __future__ import annotations

import time
from pathlib import Path

import httpx

from app.config import get_settings


class HttpClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=60)

    def estimate(self, path: str, target_language: str, source_language: str | None = None) -> dict:
        document = self._upload(path)
        self._wait_document(document["id"])
        document = self.http.get(f"/api/documents/{document['id']}").json()
        models = document.get("analysis", {}).get("models", [])
        return {"document_id": document["id"], "filename": document["filename"], "models": models, "pages": document.get("analysis", {}).get("pages", [])}

    def translate(self, path: str, target_language: str, source_language: str | None = None, output_path: str | None = None, on_progress=None) -> dict:
        document = self._upload(path)
        self._wait_document(document["id"])
        payload = {"document_id": document["id"], "target_lang": target_language}
        if source_language:
            payload["source_lang"] = source_language
        job = self.http.post("/api/jobs", json=payload).json()
        job = self._wait_job(job["id"], on_progress)
        target = Path(output_path) if output_path else Path(path).with_name(Path(path).stem + f".{target_language}" + Path(path).suffix)
        if Path(path).suffix.lower() in {".png", ".jpg", ".jpeg"}:
            target = target.with_suffix(".pdf")
        content = self.http.get(f"/api/jobs/{job['id']}/download")
        content.raise_for_status()
        target.write_bytes(content.content)
        return {"job_id": job["id"], "output_path": str(target), "cost_cents": job.get("cost_cents"), "status": job.get("status")}

    def status(self, job_id: str) -> dict:
        response = self.http.get(f"/api/jobs/{job_id}")
        response.raise_for_status()
        return response.json()

    def download(self, job_id: str, output_path: str) -> dict:
        response = self.http.get(f"/api/jobs/{job_id}/download")
        response.raise_for_status()
        Path(output_path).write_bytes(response.content)
        return {"output_path": output_path}

    def _upload(self, path: str) -> dict:
        data = Path(path).read_bytes()
        response = self.http.post("/api/documents", files={"file": (Path(path).name, data)})
        response.raise_for_status()
        return response.json()

    def _wait_document(self, document_id: str) -> None:
        for _ in range(600):
            document = self.http.get(f"/api/documents/{document_id}").json()
            if document["analysis_status"] in {"ready", "failed"}:
                if document["analysis_status"] == "failed":
                    raise RuntimeError(document.get("error_detail") or document.get("error_code"))
                return
            time.sleep(0.5)
        raise TimeoutError("analysis did not finish")

    def _wait_job(self, job_id: str, on_progress) -> dict:
        last = None
        for _ in range(1200):
            job = self.http.get(f"/api/jobs/{job_id}").json()
            marker = (job.get("status"), job.get("done_segments"))
            if marker != last and on_progress:
                total = job.get("total_segments") or 0
                done = job.get("done_segments") or 0
                on_progress(done, total or 1, job.get("status"))
                last = marker
            if job["status"] in {"done", "failed", "cancelled"}:
                if job["status"] != "done":
                    raise RuntimeError(job.get("error_detail") or job.get("error_code"))
                return job
            time.sleep(0.5)
        raise TimeoutError("translation did not finish")


class InProcessClient:
    def __init__(self) -> None:
        self.http = None

    def estimate(self, path: str, target_language: str, source_language: str | None = None) -> dict:
        document_id = self._prepare(path)
        from app.db.models import Document
        from app.db.session import session_factory

        with session_factory()() as session:
            document = session.get(Document, document_id)
            analysis = document.analysis or {}
            return {"document_id": str(document.id), "filename": document.filename, "models": analysis.get("models", []), "pages": analysis.get("pages", [])}

    def translate(self, path: str, target_language: str, source_language: str | None = None, output_path: str | None = None, on_progress=None) -> dict:
        from app.core.service import create_job
        from app.db.session import session_factory

        document_id = self._prepare(path)
        with session_factory()() as session:
            job = create_job(session, document_id, target_language, source_language, None, None, None)
            session.commit()
            job_id = job.id
        self._drain(on_progress, job_id)
        from app.db.models import TranslationJob

        with session_factory()() as session:
            job = session.get(TranslationJob, job_id)
            if job.status != "done":
                raise RuntimeError(job.error_detail or job.error_code)
            target = Path(output_path) if output_path else Path(path).with_suffix(f".{target_language}.pdf")
            target.write_bytes(Path(job.output_path).read_bytes())
            return {"job_id": str(job.id), "output_path": str(target), "cost_cents": float(job.cost_cents or 0), "status": job.status}

    def status(self, job_id: str) -> dict:
        import uuid

        from app.db.models import TranslationJob
        from app.db.session import session_factory

        with session_factory()() as session:
            job = session.get(TranslationJob, uuid.UUID(job_id))
            return {"id": str(job.id), "status": job.status, "done_segments": job.done_segments, "total_segments": job.total_segments, "error_code": job.error_code}

    def download(self, job_id: str, output_path: str) -> dict:
        import uuid

        from app.db.models import TranslationJob
        from app.db.session import session_factory

        with session_factory()() as session:
            job = session.get(TranslationJob, uuid.UUID(job_id))
            Path(output_path).write_bytes(Path(job.output_path).read_bytes())
            return {"output_path": output_path}

    def _prepare(self, path: str):
        from app.core.service import create_document, run_analysis
        from app.db.session import session_factory

        data = Path(path).read_bytes()
        with session_factory()() as session:
            document = create_document(session, Path(path).name, data)
            session.commit()
            document_id = document.id
            run_analysis(session, document_id)
            session.commit()
            return document_id

    def _drain(self, on_progress, job_id) -> None:
        from app.core.errors import InjectedCrash
        from app.core.service import process_available, reap
        from app.db.models import TranslationJob
        from app.db.session import session_factory

        for _ in range(500):
            with session_factory()() as session:
                reap(session)
                try:
                    worked = process_available(session, "mcp")
                except InjectedCrash:
                    worked = True
                session.commit()
                job = session.get(TranslationJob, job_id)
                if on_progress and job:
                    on_progress(job.done_segments or 0, job.total_segments or 1, job.status)
                if job and job.status in {"done", "failed", "cancelled"}:
                    return
                if not worked and job and job.status in {"done", "failed", "cancelled"}:
                    return
            if not worked:
                time.sleep(0.01)
