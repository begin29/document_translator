from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from app.mcp.client import ApiClient, ApiError

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
_sources: dict[str, str] = {}


def _security() -> TransportSecuritySettings:
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["localhost:*", "127.0.0.1:*", "mcp:*", "[::1]:*"],
        allowed_origins=["http://localhost:*", "http://127.0.0.1:*", "http://mcp:*", "http://[::1]:*"],
    )


mcp = FastMCP(
    "stark-translator",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "8001")),
    streamable_http_path="/mcp",
    transport_security=_security(),
)


_UNAUTHORIZED = "unauthorized: create a token in the web app under Settings → Tokens"


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, sep, value = header.strip().partition(" ")
    if not sep or scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def _http_request(ctx):
    if ctx is None:
        return None
    holder = getattr(ctx, "_request_context", None)
    if holder is None:
        return None
    return getattr(holder, "request", None)


def _require_token(ctx) -> str:
    request = _http_request(ctx)
    if request is not None:
        headers = getattr(request, "headers", None)
        header = headers.get("authorization") if headers is not None else None
        token = _bearer(header)
        if not token:
            raise ToolError(_UNAUTHORIZED)
        return token
    token = os.environ.get("TRANSLATOR_API_TOKEN", "").strip()
    if not token:
        raise ToolError(_UNAUTHORIZED)
    return token


def _client(ctx=None) -> ApiClient:
    token = _require_token(ctx)
    base = os.environ.get("TRANSLATOR_API_URL", "").strip() or "http://127.0.0.1:8000"
    realtime = os.environ.get("REALTIME_URL", "").strip() or "http://127.0.0.1:8002"
    return ApiClient(base, realtime, token=token)


def _workspace() -> Path | None:
    raw = os.environ.get("MCP_WORKSPACE", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def _resolve(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    root = _workspace()
    if root is not None and not resolved.is_relative_to(root):
        raise ApiError("path_outside_workspace", f"{resolved} is outside the workspace.", False)
    return resolved


def _fail(exc: ApiError) -> ToolError:
    return ToolError(f"{exc.code}: {exc.message}")


def _default_output(source: Path, target_language: str) -> Path:
    suffix = ".pdf" if source.suffix.lower() in _IMAGE_SUFFIXES else source.suffix
    return source.with_name(f"{source.stem}.{target_language}{suffix}")


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = Path(f"{path}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _remember(job_id: str, source: Path) -> None:
    _sources[str(job_id)] = str(source)


@mcp.tool()
async def estimate_translation(
    path: str,
    target_language: str,
    source_language: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Estimate tokens, segments, and price for a document before translating it."""
    try:
        source = _resolve(path)
        async with _client(ctx) as api:
            uploaded = await api.upload(str(source))
            document = await api.wait_document(uploaded["id"])
        analysis = document.get("analysis") or {}
        return {
            "document_id": document["id"],
            "filename": document["filename"],
            "models": analysis.get("models") or [],
            "pages": analysis.get("pages") or [],
        }
    except ApiError as exc:
        raise _fail(exc) from exc


@mcp.tool()
async def translate_document(
    path: str,
    target_language: str,
    source_language: str | None = None,
    output_path: str | None = None,
    wait: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Translate a PDF, DOCX, or photo. Returns a job id immediately unless wait is true."""
    try:
        source = _resolve(path)
        target = _resolve(output_path) if output_path else None
        async with _client(ctx) as api:
            uploaded = await api.upload(str(source))
            document = await api.wait_document(uploaded["id"])
            job = await api.create_job(document["id"], target_language, source_language)
            _remember(job["id"], source)
            if not wait:
                return {"job_id": job["id"], "status": job["status"], "document_id": document["id"]}

            async def on_progress(done: int, total: int, status: str) -> None:
                if ctx is not None:
                    await ctx.report_progress(done, total, status)

            finished = await api.wait_job(job["id"], on_progress)
            destination = target or _resolve(str(_default_output(source, target_language)))
            _write_atomic(destination, await api.download(finished["id"]))
        return {
            "job_id": finished["id"],
            "output_path": str(destination),
            "cost_cents": finished.get("cost_cents"),
            "status": finished.get("status"),
        }
    except ApiError as exc:
        raise _fail(exc) from exc


@mcp.tool()
async def get_translation_status(job_id: str, ctx: Context | None = None) -> dict:
    """Read the status of a translation job."""
    try:
        async with _client(ctx) as api:
            return await api.get_job(job_id)
    except ApiError as exc:
        raise _fail(exc) from exc


@mcp.tool()
async def download_translation(job_id: str, output_path: str | None = None, ctx: Context | None = None) -> dict:
    """Download a finished translation. Writes next to the source when output_path is omitted."""
    try:
        async with _client(ctx) as api:
            if output_path:
                target = _resolve(output_path)
            else:
                source_path = _sources.get(str(job_id))
                if not source_path:
                    raise ApiError(
                        "source_unknown",
                        "Pass output_path. The source file for this job is not known.",
                        False,
                    )
                job = await api.get_job(job_id)
                target = _resolve(str(_default_output(Path(source_path), job["target_lang"])))
            _write_atomic(target, await api.download(job_id))
        return {"job_id": job_id, "output_path": str(target)}
    except ApiError as exc:
        raise _fail(exc) from exc


def main() -> None:
    from app.config import ensure_auth_secret

    ensure_auth_secret()
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http").strip() or "streamable-http"
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
