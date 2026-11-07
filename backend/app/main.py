from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app.api.auth_routes import router as auth_router
from app.api.routes import router
from app.config import ensure_auth_secret, get_settings
from app.core.errors import AppError
from app.db.session import session_factory
from app.observability.logging import configure_logging
from app.observability.metrics import QUEUE_DEPTH, JOBS, render_metrics
from app.observability.tracing import configure_agent_tracing, configure_tracing

configure_logging()
configure_tracing()
configure_agent_tracing()

_MUTATIONS = {"POST", "PUT", "PATCH", "DELETE"}


def _header_map(scope) -> dict[str, str]:
    found: dict[str, str] = {}
    for key, value in scope.get("headers") or []:
        found[key.decode("latin-1").lower()] = value.decode("latin-1")
    return found


def _origin_of(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _session_cookie(header: str) -> bool:
    for part in header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == "st_session" and value:
            return True
    return False


def _bearer(header: str) -> bool:
    scheme, sep, value = header.strip().partition(" ")
    return bool(sep) and scheme.lower() == "bearer" and bool(value.strip())


def _csrf_blocked(scope) -> bool:
    if scope.get("type") != "http":
        return False
    method = str(scope.get("method", "")).upper()
    path = str(scope.get("path", ""))
    if method not in _MUTATIONS or not path.startswith("/api/"):
        return False
    headers = _header_map(scope)
    if not _session_cookie(headers.get("cookie", "")):
        return False
    if _bearer(headers.get("authorization", "")):
        return False
    origin = headers.get("origin", "")
    if not origin:
        origin = _origin_of(headers.get("referer", ""))
    else:
        origin = _origin_of(origin)
    return origin != _origin_of(get_settings().public_origin)


class CsrfMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if _csrf_blocked(scope):
            body = AppError("csrf_failed", "The request was rejected.", "", False, 403)
            await JSONResponse(body.as_dict(), status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


ensure_auth_secret()

app = FastAPI(title="Stark Translator", version="0.1.0")
settings = get_settings()
origins = ["*"] if settings.cors_origins.strip() == "*" else [item.strip() for item in settings.cors_origins.split(",") if item.strip()]
app.add_middleware(CsrfMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth_router, prefix="/api")
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
