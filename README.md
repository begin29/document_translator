# Stark Translator

Stark Translator turns a PDF, DOCX, or photo of a page into a translated file. It is built for people who need a document back, with a price shown before any translation starts, and for an agent that can run the same job from Cursor or Claude Code without opening the browser.

The interface uses the Stark Future palette: gold `#CFAC57`, gray `#E0E0E0`, black, and white.

## Architecture

```
browser or MCP client
        |
     FastAPI          realtime (SSE, no database)
        |                  |
   Postgres  ---- worker --+--> Redis pub/sub
        |
 OpenAI or FakeProvider
```

Postgres is the state store and the queue. Workers claim jobs, OCR pages, and segments with `FOR UPDATE SKIP LOCKED`. After each commit the worker publishes a small progress message to Redis. A separate realtime process holds the SSE connections and never queries Postgres. A restart continues from the last committed segment.

A document is analyzed once. Analysis classifies every page, extracts text blocks and images, and stores a first price view. The price shown before Translate comes from `GET /api/documents/{id}/estimate?target_lang=`, which prices the requested target language. A job then groups blocks into segments that fit the chosen model's output budget, translates them, reviews only the segments that look wrong, and renders a file. The default render mode is `layout`.

Default translation model is `gpt-4o-mini` (`gpt-4o-mini-2024-07-18`). Review of flagged segments and OCR of scanned pages use `gpt-4o` (`gpt-4o-2024-08-06`).

## Why the review step is an agent

Bulk translation is a completion. The model is given numbered blocks and must return the same numbers. It does not choose tools or a next step.

The review step is different. It runs only when `PROVIDER=openai`, and only on segments flagged for length, language, glossary, or untranslated text. The agent can read the source, look at neighbors, check the glossary, retranslate with an instruction, or accept the text. That is the one place a tool-using agent earns its cost. Its calls are ledger stage `review_agent`. If the agent fails, the job records `review_agent_failed` and still finishes. In fake mode the same retranslate path runs from a heuristic, so tests do not call OpenAI. `not_a_document` is a plain check, not an agent guardrail.

## Observability

`provider_calls` in Postgres is the number to trust. `GET /api/jobs/{id}/cost` and `GET /api/stats/cost` sum that table. `/metrics` exposes the same counters for Prometheus.

| Backend | What it answers |
| --- | --- |
| `provider_calls` | Tokens, cents, latency, retries, and which stage spent the money. This is the ledger. |
| Prometheus + Grafana (`docker compose --profile observability up`) | Rates, queue depth, and in-flight concurrency while a job runs. Dashboard uid `stark-translator`. |
| Langfuse (same profile, `OTEL_EXPORTER=otlp`) | A trace tree of API stages for one job. |
| OpenAI Traces (`AGENT_TRACE_EXPORTER=openai`) | Prompt-level view of the review agent only. It does not replace the ledger. `AGENT_TRACE_EXPORTER=otlp` is still a no-op. |

## Run it

```bash
cp .env.example .env
```

Set `AUTH_SECRET` (`openssl rand -hex 32`). With `PROVIDER=openai`, also set `OPENAI_API_KEY`. `PUBLIC_ORIGIN` defaults to `http://localhost:8080`, which matches compose. `npm run dev` serves the UI on port 5173, so set `PUBLIC_ORIGIN=http://localhost:5173` or cookie requests are rejected with `csrf_failed`.

```bash
docker compose up --build
```

That starts Postgres, the API on port 8000, the worker, the UI on port 8080, MCP on port 8001, the realtime service on port 8002, and Redis. The upload limit is `MAX_UPLOAD_MB` (default 40), read by the UI from `GET /api/meta/config`. With `PROVIDER=fake` no API key is required.

First run:

1. Register at `/register`, or run `python -m app.cli create-user --email you@example.com --password 'at least ten'`.
2. Open Settings → Tokens and create a token. The raw `stk_…` value is shown once.
3. Put that token in the MCP client config below.

Rows saved before accounts existed have no owner. `python -m app.cli claim-orphans --email you@example.com` assigns those documents and jobs to an account. A document whose sha256 that account already owns is left alone.

Local tests use Postgres and the backend virtualenv:

```bash
cd backend
uv sync --python 3.12 --extra dev
DATABASE_URL=postgresql+psycopg://translator@127.0.0.1:5433/translator \
TEST_DATABASE_URL=postgresql+psycopg://translator@127.0.0.1:5433/translator \
FILES_DIR=/tmp/stark-files \
uv run pytest
```

Frontend:

```bash
cd frontend
npm install
npm test
npm run build
```

`scripts/benchmark.py` prints cost and p95 latency from `provider_calls` for one generated PDF. Set `DATABASE_URL` first.

## MCP

The MCP server starts with the stack on port 8001 (`/mcp`). It is Streamable HTTP. Tools call the API over HTTP. There is no in-process client.

Set `MCP_WORKSPACE` in `.env` to the absolute host folder that holds your documents. Compose mounts that same path into the MCP container. The server only reads and writes paths inside it. Each person uses their own API token. A request without `Authorization: Bearer stk_…` returns `unauthorized`.

`estimate_translation` prices the requested target language. `translate_document` returns `{job_id, status, document_id}` immediately. Pass `wait=true` to follow progress and download in the same call. Progress comes from `GET /events?ticket=`. If that stream cannot be reached, the client polls REST.

Cursor config:

```json
{
  "mcpServers": {
    "stark-translator": {
      "url": "http://localhost:8001/mcp",
      "headers": {"Authorization": "Bearer stk_…"}
    }
  }
}
```

Claude Code:

```bash
claude mcp add --transport http stark-translator http://localhost:8001/mcp --header "Authorization: Bearer stk_…"
```

Stdio reads `TRANSLATOR_API_TOKEN` instead of a request header:

```bash
MCP_TRANSPORT=stdio TRANSLATOR_API_URL=http://127.0.0.1:8000 TRANSLATOR_API_TOKEN=stk_… uv run --project backend python -m app.mcp
```

Three-step check, without the web UI:

1. `translate_document` on a small PDF. The reply returns `job_id` immediately (`wait` defaults to false).
2. `get_translation_status` on that job id until the status is `done`.
3. `download_translation` writes the file. Pass `wait=true` to `translate_document` to report progress and download in the same call.

## Acceptance

1. Upload a text PDF, see page count and a price for the chosen target language before Translate, download a file that contains the translation. The default mode is `layout` (same page count and boxes when the translation fits; overflowing pages fall back to reflow).
2. Kill the worker after some segments are `done`. After restart those segments are not billed again and the job finishes. The browser keeps showing progress.
3. A corrupt PDF, an encrypted PDF, a renamed PNG, a file over `MAX_UPLOAD_MB`, and an unsupported type each return a specific `error_code` and message. None of them return a generic 500 or write a file outside the uploads directory.
4. Two accounts do not see each other's documents, jobs, or cost stats. Opening another user's id returns 404.
5. `GET /api/jobs/{id}/cost` matches the sum of `provider_calls` for that job, including `review_agent` when segments were flagged and OCR when pages were read.

## Quality metric

On the fixture set, a finished job should have:

- target-language detection on every translated text block, except blocks that are numbers, URLs, or glossary terms kept verbatim
- a length ratio inside the per-language bounds in `app/core/languages.py`

Blocks outside those bounds are flagged and sent to review. The job event `review_done` records `quality_warning` when any flag remains.
