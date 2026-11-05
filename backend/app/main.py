from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app.api.routes import router
from app.config import get_settings
from app.core.errors import AppError
from app.db.session import session_factory
from app.observability.logging import configure_logging
from app.observability.metrics import QUEUE_DEPTH, JOBS, render_metrics
from app.observability.tracing import configure_agent_tracing, configure_tracing

configure_logging()
configure_tracing()
configure_agent_tracing()

app = FastAPI(title="Stark Translator", version="0.1.0")
settings = get_settings()
origins = ["*"] if settings.cors_origins.strip() == "*" else [item.strip() for item in settings.cors_origins.split(",") if item.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router, prefix="/api")


@app.exception_handler(AppError)
def app_error(_request, exc: AppError):
    return JSONResponse(exc.as_dict(), status_code=exc.status)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    try:
        factory = session_factory()
        with factory() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as exc:
        return JSONResponse({"status": "not_ready", "detail": str(exc)}, status_code=503)


@app.get("/metrics")
def metrics():
    try:
        factory = session_factory()
        with factory() as session:
            rows = session.execute(text("SELECT status, count(*) FROM translation_jobs GROUP BY status")).all()
            for status, count in rows:
                JOBS.labels(status=status).set(count)
            queued = session.execute(text("SELECT count(*) FROM translation_jobs WHERE status = 'queued'")).scalar() or 0
            QUEUE_DEPTH.set(queued)
    except Exception:
        pass
    return Response(render_metrics(), media_type="text/plain; version=0.0.4")
