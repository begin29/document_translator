from __future__ import annotations

import os
import shlex
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


def _workspace() -> Path:
    raw = os.environ.get("MCP_WORKSPACE", "").strip()
    return Path(raw or Path.home()).expanduser().resolve()


def _resolve(path: str, ctx=None) -> Path:
    if _http_request(ctx) is not None:
        raise ApiError(
            "path_not_supported",
            "This server cannot read files on your machine. Call create_upload_url, upload the file, and pass document_id.",
            False,
        )
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_relative_to(_workspace()):
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


def _source(path: str | None, document_id: str | None, ctx=None) -> Path | None:
    if bool(path) == bool(document_id):
        raise ApiError("invalid_arguments", "Pass either path or document_id, not both.", False)
    return _resolve(path, ctx) if path else None


_LISTED = ("id", "filename", "format", "page_count", "detected_source_lang", "analysis_status", "created_at")


async def _ready(api: ApiClient, source: Path | None, document_id: str | None) -> dict:
    if source is not None:
        document_id = (await api.upload(str(source)))["id"]
    return await api.wait_document(document_id)


@mcp.tool()
async def list_documents(name: str | None = None, limit: int = 20, ctx: Context | None = None) -> dict:
    """List the caller's documents, newest first, including files uploaded in the web app.

    Use it to find a document the user mentions by name; `name` matches part of the filename,
    case-insensitively. Pass the returned `id` as `document_id` to estimate_translation or translate_document.
    """
    try:
        async with _client(ctx) as api:
            documents = await api.list_documents()
    except ApiError as exc:
        raise _fail(exc) from exc
    if name:
        needle = name.casefold()
        documents = [item for item in documents if needle in str(item.get("filename") or "").casefold()]
    limit = max(1, min(limit, 100))
    return {"documents": [{key: item.get(key) for key in _LISTED} for item in documents[:limit]]}


@mcp.tool()
async def create_upload_url(filename: str | None = None, ctx: Context | None = None) -> dict:
    """Get a short-lived URL for sending a file from the caller's machine to the translator.

    Use this for a file on the user's machine that is not yet uploaded (check list_documents first).
    Run the returned `command` in a shell (replace the file path if needed); without a shell, give the
    command to the user. It prints the uploaded document as JSON. Pass its `id` as `document_id`
    to estimate_translation or translate_document.
    """
    try:
        async with _client(ctx) as api:
            link = await api.request_upload()
    except ApiError as exc:
        raise _fail(exc) from exc
    local = shlex.quote(f"file=@{filename or '<path-to-file>'}")
    return {
        "upload_url": link["upload_url"],
        "expires_at": link["expires_at"],
        "command": f"curl -fsS -F {local} {shlex.quote(link['upload_url'])}",
    }


@mcp.tool()
async def estimate_translation(
    target_language: str,
    path: str | None = None,
    document_id: str | None = None,
    source_language: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Estimate tokens, segments, and price for a document before translating it.

    Pass `document_id` (from list_documents or create_upload_url). `path` works only when the server
    runs locally over stdio.
    """
    try:
        source = _source(path, document_id, ctx)
        async with _client(ctx) as api:
            document = await _ready(api, source, document_id)
            quote = await api.estimate_document(document["id"], target_language)
        analysis = document.get("analysis") or {}
        return {
            "document_id": document["id"],
            "filename": document["filename"],
            "target_language": target_language,
            "models": quote.get("models") or [],
            "pages": analysis.get("pages") or [],
        }
    except ApiError as exc:
        raise _fail(exc) from exc


@mcp.tool()
async def translate_document(
    target_language: str,
    path: str | None = None,
    document_id: str | None = None,
    source_language: str | None = None,
    output_path: str | None = None,
    wait: bool = False,
    ctx: Context | None = None,
) -> dict:
    """Translate a PDF, DOCX, or photo. Returns a job id immediately unless wait is true.

    Pass `document_id` (from list_documents or create_upload_url). `path` and `output_path` work only
    when the server runs locally over stdio. With wait true, the result has a short-lived `download_url`,
    or `output_path` when the file was written locally.
    """
    try:
        source = _source(path, document_id, ctx)
        target = _resolve(output_path, ctx) if output_path else None
        async with _client(ctx) as api:
            document = await _ready(api, source, document_id)
            job = await api.create_job(document["id"], target_language, source_language)
            if source is not None:
                _remember(job["id"], source)
            if not wait:
                return {"job_id": job["id"], "status": job["status"], "document_id": document["id"]}

            async def on_progress(done: int, total: int, status: str) -> None:
                if ctx is not None:
                    await ctx.report_progress(done, total, status)

            finished = await api.wait_job(job["id"], on_progress)
            result = {
                "job_id": finished["id"],
                "cost_cents": finished.get("cost_cents"),
                "status": finished.get("status"),
            }
            if target is None and source is not None:
                target = _resolve(str(_default_output(source, target_language)))
            if target is None:
                link = await api.download_link(finished["id"])
                result["download_url"] = link["download_url"]
                result["expires_at"] = link["expires_at"]
                return result
            _write_atomic(target, await api.download(finished["id"]))
        result["output_path"] = str(target)
        return result
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
    """Get a finished translation.

    Returns a short-lived `download_url` the caller can fetch, e.g. with `curl -fsSOJ <url>`.
    On a local stdio server it writes to `output_path`, or next to the source of a `path` job.
    """
    try:
        target = _resolve(output_path, ctx) if output_path else None
        async with _client(ctx) as api:
            source_path = _sources.get(str(job_id))
            if target is None and source_path:
                job = await api.get_job(job_id)
                target = _resolve(str(_default_output(Path(source_path), job["target_lang"])))
            if target is None:
                link = await api.download_link(job_id)
                return {"job_id": job_id, "download_url": link["download_url"], "expires_at": link["expires_at"]}
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
