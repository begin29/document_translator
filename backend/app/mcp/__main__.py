from __future__ import annotations

import os

from mcp.server.fastmcp import Context, FastMCP

from app.mcp.client import HttpClient, InProcessClient

mcp = FastMCP("stark-translator")


def _client():
    base = os.environ.get("TRANSLATOR_API_URL", "").strip()
    if base:
        return HttpClient(base)
    return InProcessClient()


@mcp.tool()
def estimate_translation(path: str, target_language: str, source_language: str | None = None) -> dict:
    """Estimate tokens, segments, and price for a document before translating it."""
    return _client().estimate(path, target_language, source_language)


@mcp.tool()
async def translate_document(
    path: str,
    target_language: str,
    source_language: str | None = None,
    output_path: str | None = None,
    ctx: Context = None,
) -> dict:
    """Translate a PDF, DOCX, or photo and write the result next to the source file."""
    import anyio

    holder: dict = {"result": None, "error": None, "progress": None}

    def work() -> None:
        def on_progress(done: int, total: int, status: str) -> None:
            holder["progress"] = (done, total or 1, status)

        try:
            holder["result"] = _client().translate(path, target_language, source_language, output_path, on_progress)
        except Exception as exc:
            holder["error"] = exc

    async with anyio.create_task_group() as group:
        group.start_soon(anyio.to_thread.run_sync, work)

        async def watch() -> None:
            last = None
            while holder["result"] is None and holder["error"] is None:
                progress = holder.get("progress")
                if ctx is not None and progress and progress != last:
                    await ctx.report_progress(progress[0], progress[1])
                    last = progress
                await anyio.sleep(0.2)

        group.start_soon(watch)
    if holder["error"]:
        raise holder["error"]
    return holder["result"]


@mcp.tool()
def get_translation_status(job_id: str) -> dict:
    """Read the status of a translation job."""
    return _client().status(job_id)


@mcp.tool()
def download_translation(job_id: str, output_path: str) -> dict:
    """Download a finished translation to output_path."""
    return _client().download(job_id, output_path)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
