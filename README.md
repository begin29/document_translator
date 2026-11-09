# Stark Translator

Stark Translator turns a PDF, DOCX, or photo of a page into a translated file. It is built for people who need a document back, with a price shown before any translation starts, and for an agent that can run the same job from Cursor or Claude Code without opening the browser.

The interface uses the Stark Future palette: gold `#CFAC57`, gray `#E0E0E0`, black, and white.

## Who it is for

A person who has a file in one language and needs the same file back in another: a contract, a manual, a scanned letter, a photo of a page. They care about three things: what it will cost before they commit, that a long document does not die halfway, and that they get a file, not a chat reply. The second user is an agent in Claude Code or Cursor that needs the same job done without a browser.

This is an MVP. It shows that many documents translate in parallel without falling over, that a crash or provider error does not lose or re-bill work, and that a price is shown before anything is spent. Layout fidelity on image-heavy pages and the price of the review step are the known weak spots; see `DECISIONS.md`.

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

The web app and the MCP server are two front doors onto one core. Routes in `app/api` and tools in `app/mcp` only call `app/core/service.py`, and the MCP server reaches it through the same HTTP API the browser uses.

### Core objects

| Object | What it is |
| --- | --- |
| `documents` | One uploaded file per owner and sha256. Holds format, page count, detected language, analysis status, and the first price view. |
| `blocks` | Text blocks and images extracted once at analysis, with page, bbox, and kind. |
| `translation_jobs` | One document into one target language with one model. Holds status, counters, estimate, cost, render mode, and `Idempotency-Key`. |
| `segments` | The unit of work and of billing. A group of blocks sized to the model's output budget. |
| `block_translations` | The translated text per block, written in the same transaction as its segment and its `provider_calls` row. |
| `provider_calls` | The cost ledger: one row per model call, including failed attempts. |
| `translation_memory` | Per-owner cache keyed by source hash and language pair. A hit is not sent to the model and not billed. |

### Formats

PDF, DOCX, and PNG/JPEG. Each format is a `DocumentAdapter` in `app/core/formats/` with `sniff`, `extract`, and `render`. DOCX was the second format and was added as one adapter, one `ADAPTERS` entry, and one magic-byte branch in `sniff.py`, without touching segmenting, translation, review, or cost. Images reuse the PDF OCR path and render back to PDF.

### Concurrency, retries, and failures

Two users and a 200-page document at the same time: work is claimed per segment with `FOR UPDATE SKIP LOCKED`, ordered by fewest in-flight segments, so a 2-page job is not queued behind 200 pages. A Postgres-backed gate caps provider concurrency and tokens per minute across worker replicas.

A retried segment cannot double-bill. Its translations, its completion, and its ledger row commit together, and a done segment is never sent again. `POST /api/jobs` with the same `Idempotency-Key` returns the existing job.

| Failure | What happens |
| --- | --- |
| Corrupt or encrypted PDF, renamed file, unsupported type, too large | Rejected with `corrupt_pdf`, `encrypted_pdf`, `invalid_file`, `unsupported_type`, or `file_too_large`. Nothing is written outside `uploads/`. |
| Scanned PDF or photo | Pages are classified and sent to vision OCR. Poor scans are deskewed and upscaled first. OCR cost is in the estimate and the ledger. |
| 400-page PDF | Split into segments and translated in parallel. Progress is streamed; a restart resumes from the last done segment. |
| Provider 500 or timeout | Retried up to `SEGMENT_ATTEMPT_LIMIT` (3). Each attempt is a ledger row. Then the job fails with `provider_error` or `provider_timeout` and can be retried without re-billing done segments. |
| Rate limit / spend limit | 429 rate limit halves concurrency and ramps back. A spend limit stops the job with `funds_exceeded`. |
| A language pair the model handles badly | Segments with the wrong language, odd length ratio, or untranslated text are flagged and sent to the review agent. If a flag remains, the job finishes with `quality_warning` and the UI says so. |
| Worker killed | `reap` requeues stale heartbeats after `STALE_SECONDS`. |

Every error has a specific `code` and a message in `frontend/src/copy.ts`.

## Why the review step is an agent

Bulk translation is a completion. The model is given numbered blocks and must return the same numbers. It does not choose tools or a next step.

The review step is different. It runs only when `PROVIDER=openai`, and only on segments flagged for length, language, glossary, or untranslated text. The agent can read the source, look at neighbors, check the glossary, retranslate with an instruction, or accept the text. That is the one place a tool-using agent earns its cost. The agent loop is ledger stage `review_agent`, and its retranslate calls are stage `review`. If the agent fails, the job records `review_agent_failed` and still finishes. In fake mode the same retranslate path runs from a heuristic, so tests do not call OpenAI. `not_a_document` is a plain check, not an agent guardrail.

## Observability

`provider_calls` in Postgres is the number to trust. `GET /api/jobs/{id}/cost` and `GET /api/stats/cost` sum that table. `/metrics` exposes the same counters for Prometheus.

| Backend | What it answers |
| --- | --- |
| `provider_calls` | Tokens, cents, latency, retries, and which stage spent the money. This is the ledger. |
| Prometheus + Grafana (`docker compose --profile observability up`) | Rates, queue depth, and in-flight concurrency while a job runs. Dashboard uid `stark-translator`. |
| Langfuse (same profile, `OTEL_EXPORTER=otlp`) | A trace tree of API stages for one job. |
| OpenAI Traces (`AGENT_TRACE_EXPORTER=openai`) | Prompt-level view of the review agent only. It does not replace the ledger. `AGENT_TRACE_EXPORTER=otlp` is still a no-op. |

On call at 3am, in this order:

1. `GET /ready` (database) and `docker compose ps` (is the worker alive).
2. Grafana `stark-translator`: queue depth, in-flight, and provider error rate. A flat queue with in-flight at the ceiling points at the provider gate.
3. `provider_calls` for the job: `status`, `error_code`, `latency_ms`. It shows whether the provider failed, timed out, or rate-limited, and what was spent.
4. `job_events` and worker logs, filtered by `job_id`. Each line carries `job_id` and `stage`.

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

## Testing guide

Tests never call OpenAI. `conftest.py` forces `APP_ENV=test` and `PROVIDER=fake`, and `build_provider()` raises if it is asked for OpenAI under test.

| File | What a regression there would break |
| --- | --- |
| `test_flow.py` | Job lifecycle, `Idempotency-Key`, cancel, retry without double billing, crash and resume skipping done segments, reaper recovery, `funds_exceeded`, cost endpoint equal to the ledger. |
| `test_core.py` | Sniffing, extraction, chunking, numbered-block parsing, page classes. |
| `test_cost.py` | Estimate math and ledger sums. |
| `test_render_fidelity.py` | Layout mode keeps boxes on pages that fit and reflows only the ones that overflow. |
| `test_languages.py` | Language detection and length-ratio bounds behind the quality flags. |
| `test_review_agent.py` | Flagged segments are retried and `quality_warning` is set when a flag remains. |
| `test_auth.py`, `test_api_web.py` | Accounts, CSRF, tokens, and that another user's id is 404. |
| `test_events.py`, `test_realtime.py` | After-commit publishing, tickets, and the SSE relay. |
| `test_mcp.py` | The six tools, signed links, `path_not_supported`, and workspace limits. |

Failure scenarios by hand use the fake provider knobs, for example `FAKE_FAIL_MODE=http500 FAKE_FAIL_SEGMENTS=1,3`, `FAKE_FAIL_MODE=timeout`, `FAKE_FAIL_MODE=rate_limit`, or `CRASH_AFTER_SEGMENT=2`.

## MCP

The MCP server starts with the stack on port 8001 (`/mcp`). It is Streamable HTTP and works as a standalone service: a client needs only the URL and an API token. The six tools call the API over HTTP. There is no in-process client.

Each person uses their own API token, created in the web app under Settings → Tokens. Put it in the client config below, and do not commit it. A request without `Authorization: Bearer stk_…` returns `unauthorized`.

Over HTTP the server never reads or writes a file path. A `path` or `output_path` argument returns `path_not_supported`. Documents are referenced by `document_id`:

- A file already uploaded in the web app: `list_documents` (optionally `name="contract"`) finds its `id`.
- A file on the client's machine: `create_upload_url` returns a signed `upload_url` and a ready-to-run `command`, for example `curl -fsS -F 'file=@/Users/me/contract.pdf' 'http://localhost:8080/api/uploads?ticket=…'`. An agent with a shell runs it itself. A chat client gives it to you to run. It prints the document as JSON, and its `id` is the `document_id`.
- The result: `translate_document` with `wait` true, or `download_translation`, returns a signed `download_url`. Fetch it with `curl -fsSOJ '<url>'` or open it in a browser.

Signed links are built from `PUBLIC_ORIGIN`, so it must be an address the client can reach. They expire after 15 minutes and do not need the API token.

The tools translate a whole file. They do not take a page range. To translate one page, upload that page as its own file.

`target_language` and `source_language` are codes: `en`, `de`, `fr`, `es`, `it`, `pt`, `nl`, `pl`, `uk`, `ja`, `zh`, `ko`. `source_language` is optional. The server detects it when you omit it.

| Tool | Arguments | What you get back |
| --- | --- | --- |
| `list_documents` | optional `name` (part of the filename), `limit` (default 20) | `documents`, newest first: `id`, `filename`, `format`, `page_count`, `detected_source_lang`, `analysis_status`, `created_at`. |
| `create_upload_url` | optional `filename` | `upload_url`, `expires_at`, and a `curl` `command` that uploads the file and prints its `document_id`. |
| `estimate_translation` | `document_id`, `target_language`, optional `source_language` | Waits until analysis is ready and returns `document_id`, `models` (a price for each model), and `pages`. |
| `translate_document` | `document_id`, `target_language`, optional `source_language`, `wait` (default false) | Without `wait`: `{job_id, status, document_id}`. With `wait` true: follows progress and returns `download_url` and `cost_cents`. |
| `get_translation_status` | `job_id` | The job, including `status`, `done_segments`, `total_segments`, and `cost_cents`. |
| `download_translation` | `job_id` | `download_url`. A photo comes back as a PDF. |

Claude Code, for every project:

```bash
claude mcp add --transport http --scope user stark-translator http://localhost:8001/mcp --header "Authorization: Bearer stk_…"
```

Cursor reads `.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` for every project:

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

Three-step check, without the web UI (ask the agent in Claude Code or Cursor):

1. "Call `create_upload_url` with filename `page.pdf` and run the command it returns for `/path/to/page.pdf`." The output is the document as JSON. Note its `id`.
2. "Call `estimate_translation` for that `document_id` with `target_language` `de`." Read the price in `models`.
3. "Call `translate_document` for that `document_id`, `target_language` `de`, `wait` true, then fetch the `download_url`." You get a German PDF, and `cost_cents` in the reply matches the job in the web app.

Without `wait`, `translate_document` returns `job_id` at once. Then `get_translation_status` shows progress and `download_translation` returns the link. With `wait`, progress comes from `GET /events?ticket=`. If that stream cannot be reached, the client polls REST.

The upload step needs a shell. Claude Code and Cursor run the `curl` command themselves. Claude Desktop and other chat apps cannot, so there the file has to be uploaded in the web app first and found with `list_documents`.

### Local stdio mode

A client can also start the server itself on the same machine. Then `path` replaces `document_id`, and the result is written next to the source as `{stem}.{target_language}{suffix}`, or to `output_path`:

```bash
MCP_TRANSPORT=stdio TRANSLATOR_API_URL=http://127.0.0.1:8000 TRANSLATOR_API_TOKEN=stk_… uv run --project backend python -m app.mcp
```

Paths must sit under `MCP_WORKSPACE`, which defaults to your home folder. Anything else returns `path_outside_workspace`. Stdio has no request header, so it reads the token from `TRANSLATOR_API_TOKEN`.
