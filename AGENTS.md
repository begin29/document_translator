# Stark Translator

PDF, DOCX, and page-photo translation. One Python core serves a FastAPI web API and an MCP server (Streamable HTTP on :8001; stdio still works). Postgres is the state store and the queue (`FOR UPDATE SKIP LOCKED`). Redis pub/sub carries live progress to a separate realtime service that never touches the database. Workers claim work; a restart continues from the last committed segment. Default provider is `PROVIDER=fake`.

Brand is Stark Future: gold `#CFAC57`, gray `#E0E0E0`, black `#000000`, white `#FFFFFF`. Tokens live in the frontend Tailwind config. Gold is an accent.

Deeper context, read only when the task needs it: `README.md` (run, MCP, acceptance), `DECISIONS.md` (cuts and prices), `PROMPTS.md` (prompt text), `docs/QA_PLAN.md` (scenario IDs). `docs/` is gitignored.

## Layout

| Path | Owns |
| --- | --- |
| `backend/app/core/service.py` | Pipeline, claims, retries, cost, TM, render trigger |
| `backend/app/core/formats/` | `DocumentAdapter`: `sniff`, `extract`, `render`. Registry in `registry.py`, magic-byte sniff in `sniff.py` |
| `backend/app/core/chunking.py` | `segment_budget` and `chunk_blocks` |
| `backend/app/core/model_registry.py` | `ModelSpec`. Override with `MODELS_CONFIG` (`models.yaml`) |
| `backend/app/core/provider/` | `TranslationProvider`. Factory in `factory.py` |
| `backend/app/core/ocr/` | `OcrProvider` (`VisionOcrProvider`, `FakeOcrProvider`) |
| `backend/app/core/analysis/` | Tier 0 `page_classifier`, Tier 1 vision, preprocess |
| `backend/app/core/agents/` | Review agent (`PROVIDER=openai`) and the plain `ensure_is_document` check |
| `backend/app/core/events.py` | After-commit Redis publisher. Channel names live in `channels_for` |
| `backend/app/core/prompts.py` | Prompt text. Bump `PROMPT_VERSION` when a prompt changes |
| `backend/app/core/errors.py` | `AppError` and provider error types |
| `backend/app/api/routes.py` | HTTP shell. Routers mounted at `/api` |
| `backend/app/api/auth_routes.py` | Register, login, logout, tokens, `POST /api/events/ticket` |
| `backend/app/auth/` | Passwords (argon2), opaque session cookies, API tokens, HMAC tickets |
| `backend/app/cli.py` | `create-user` and `claim-orphans` |
| `backend/app/worker.py` | Thread loop: `reap`, `sync_gate`, `process_available` |
| `backend/app/mcp/` | Four async tools plus `ApiClient`. HTTP transport on :8001 |
| `backend/app/realtime/` | SSE relay. No database access |
| `backend/app/db/models.py` | SQLAlchemy models. Migrations in `backend/alembic/` |
| `backend/app/config.py` | `Settings` from env. `get_settings()` is cached |
| `compose.realtime.yml` | Redis and the realtime service, included by `docker-compose.yml` |
| `frontend/src/` | `App.tsx` (routes), `api.ts`, `copy.ts` (error strings), `useEventStream.ts` |

A new format is one adapter plus an `ADAPTERS` entry plus a sniff branch. Image bytes stay on the files volume (`asset_path`). Blocks with `kind=image` are never sent to a provider.

## Pipeline

Document analysis is once per file (`documents.analysis_status`: `queued` → `ready` or `failed`). A job then runs:

`queued` → `extracting` → (`ocr` if scanned pages) → `translating` → `reviewing` → `rendering` → `done`

`failed` and `cancelled` are terminal. Stale heartbeats (`STALE_SECONDS`, default 120) are requeued by `reap`.

Page classes: `text`, `text_with_images` (text pipeline); `text_garbled`, `scanned_good` (OCR); `scanned_poor` (deskew, autocontrast, upscale, then OCR); `blank` (skipped); `unknown` (Tier 1 vision only). All pages `non_document` or `blank` fail as `not_a_document` before OCR.

During `translating` the unit of work is the **segment**. Claim prefers the job with the fewest in-flight segments. Segment completion, its `block_translations`, and its `provider_calls` row are one transaction. Done segments are not translated again.

Chunking groups blocks up to `segment_budget` (stored on the job as `segment_budget_tokens`). Oversized blocks split on sentence boundaries, then tokens, and are flagged so render rejoins them. Prompts use numbered blocks and require numbered blocks back (`numbered.py`).

Translation memory key is `(owner_id, src_hash, source_lang, target_lang)`. Hits are model-independent (`source=tm`) and are excluded from the prompt. User edits set `output_stale`; `POST /api/jobs/{id}/render` rebuilds the file.

Review (`openai-agents`, `REVIEW_MODEL`) runs only when `PROVIDER=openai`, and only on flagged segments (length ratio, language, glossary, untranslated). Tools: `get_source_segment`, `get_neighbor_translations`, `lookup_glossary`, `retranslate_segment`, `accept_segment`, and `view_source_page_image` for OCR pages. Its provider calls use ledger stage `review_agent`. A failure writes a `review_agent_failed` event and the job still finishes on the fake-provider retranslate path. Bulk translation stays a completion. In fake mode the retranslate path is a heuristic. `not_a_document` is a plain check (`ensure_is_document`), not an agent guardrail. `AGENT_TRACE_EXPORTER=otlp` is still a no-op.

OCR and review use `gpt-4o` (`gpt-4o-2024-08-06`). Bulk translation and Tier 1 classification use `gpt-4o-mini` (`gpt-4o-mini-2024-07-18`). Send `max_completion_tokens`. Prices live on the `ModelSpec`, including `cached_input_price_per_1m` (half the input price when unset). `estimate_for` prices a job before it runs; `GET /api/documents/{id}/estimate?target_lang=` returns that quote for every model. `provider_calls` is the cost ledger; `GET /api/jobs/{id}/cost` must match that table. Real OCR writes a `provider_calls` row and adds that cost to the job. `MAX_JOB_COST_CENTS` aborts with `cost_cap_exceeded`.

PDF render modes: `layout` (default) and `reflow`. Layout paints into the original boxes and falls back to reflow per page. OCR pages always reflow. DOCX writes the translation into the first text run and leaves `w:drawing` runs untouched. DOCX headers and footers are not extracted, so render leaves them unchanged. PNG/JPEG uploads are one OCR page rendered to PDF.

## API and MCP

Errors are `AppError` → `{code, message, detail, retryable}` with a real status. Add a specific message in `frontend/src/copy.ts` for every new code. Upload codes already in use: `file_too_large`, `invalid_file`, `corrupt_pdf`, `corrupt_file`, `encrypted_pdf`, `unsupported_type`, `empty_document`, `not_a_document`. Job codes: `ocr_no_text`, `provider_timeout`, `provider_error`, `rate_limited`, `funds_exceeded`, `cost_cap_exceeded`, `not_ready`, `unknown_language`, `unknown_model`, `cancelled`.

`POST /api/jobs` honors `Idempotency-Key` and returns the existing job. The handler commits before the response is sent. Same sha256 returns the existing document for that user. Uploads stay under `FILES_DIR` (`uploads/`, `assets/`, `thumbs/`). The upload limit is `MAX_UPLOAD_MB` (default 40) and the UI reads it from `GET /api/meta/config`. Filename is the basename only.

Every document and job route depends on `current_user`. Rows load through `owned(...)`. Another user's id returns 404, the same response as a missing id. Dedupe and translation memory are per user. MCP calls send `Authorization: Bearer stk_…`. Cookie mutations must send `Origin` equal to `PUBLIC_ORIGIN` or they return 403 `csrf_failed`. `npm run dev` on port 5173 must set `PUBLIC_ORIGIN` to that origin. Compose on port 8080 matches the default `http://localhost:8080`.

Env: `AUTH_SECRET` (required when `PROVIDER` is not `fake`), `ALLOW_SIGNUP`, `COOKIE_SECURE`, `PUBLIC_ORIGIN`. `python -m app.cli create-user` adds an account. `python -m app.cli claim-orphans` assigns rows that have no owner.

Realtime contract v2: one channel per user, `user:{owner_id}`. The browser and MCP client obtain an HMAC ticket with `POST /api/events/ticket` and open `GET /events?ticket=`. The realtime process verifies the ticket and subscribes to that user's channel. It does not read Postgres. Messages carry absolute counters (`done_segments`, `total_segments`, `cost_cents`, `status`). A client loads current state over REST, then applies `update` events. `resync` means refetch over REST. A lost message is covered by the next one or by that REST read. There is one EventSource per tab. Nginx and the Vite dev server proxy `/events` with buffering off.

MCP tools, and only these: `list_documents`, `create_upload_url`, `estimate_translation`, `translate_document`, `get_translation_status`, `download_translation`. There is no in-process client. Tools build one `ApiClient` and talk HTTP. `TRANSLATOR_API_URL` defaults to `http://127.0.0.1:8000` (compose sets `http://api:8000`). Over HTTP (a request is present on the tool context) `path` and `output_path` return `path_not_supported`. Clients find a document with `list_documents` (`GET /api/documents`, filtered by filename in the tool) or send one with `create_upload_url` (`POST /api/upload-links` → `POST /api/uploads?ticket=`), then pass `document_id`. Results come back as `download_url` (`POST /api/jobs/{id}/download-link` → `GET /api/downloads/{job_id}?ticket=`). Signed URLs are built from `PUBLIC_ORIGIN`. Only stdio takes `path`, restricted to `MCP_WORKSPACE` (default: home folder), which returns `path_outside_workspace` outside it. Exactly one of `path` or `document_id` is required (`invalid_arguments`). The server speaks streamable HTTP on port 8001 (`/mcp`) and can still run over stdio. Waiting follows the realtime SSE stream, with REST polling only if that stream cannot be reached. `translate_document` returns `{job_id, status, document_id}` unless `wait` is true. `estimate_translation` calls `GET /api/documents/{id}/estimate?target_lang=` for the requested target language.

`GET /health`, `GET /ready` (DB ping), `GET /metrics` are unprefixed. `GET /api/debug/provider-calls` exists only when `PROVIDER=fake`.

## Providers and limits

`build_provider()` returns `OpenAIProvider` only when `PROVIDER=openai`. It raises if `APP_ENV=test`. Tests and QA failure scenarios stay on `FakeProvider`.

Fake knobs: `FAKE_LATENCY_MS`, `FAKE_FAIL_MODE` (`http500|timeout|rate_limit|funds_exceeded|untranslated`), `FAKE_FAIL_SEGMENTS`, `FAKE_FAIL_RATE`, `CRASH_AFTER_SEGMENT`.

Concurrency ceilings: `PROVIDER_MAX_CONCURRENCY` (32), `PROVIDER_TPM_BUDGET` (2_000_000), `JOB_MAX_CONCURRENCY` (16). The gate is a Postgres counter shared by worker replicas. A rate-limit 429 halves effective concurrency and ramps one slot per quiet minute. A spend-limit or insufficient-quota 429 is `funds_exceeded`: the job stops immediately and the gate stays unchanged. `SEGMENT_ATTEMPT_LIMIT` is 3.

`OTEL_EXPORTER=none|otlp` (default `none`) exports API spans when the exporter package is installed. `AGENT_TRACE_EXPORTER` covers the review agent only. `AGENT_TRACE_EXPORTER=otlp` is still a no-op. Observability containers are the `observability` compose profile.

## Commands

```bash
cp .env.example .env
docker compose up --build          # UI :8080, API :8000, MCP :8001, realtime :8002, Redis
docker compose --profile observability up

cd backend && uv sync --python 3.12 --extra dev
cd backend && DATABASE_URL=postgresql+psycopg://translator@127.0.0.1:5433/translator \
  TEST_DATABASE_URL=postgresql+psycopg://translator@127.0.0.1:5433/translator \
  FILES_DIR=/tmp/stark-files uv run pytest

cd frontend && npm test && npm run build
```

Compose DB is not published on the host. Local pytest expects Postgres on `127.0.0.1:5433`. The worker container runs `alembic upgrade head` before `python -m app.worker`.

Tests: `backend/tests/test_core.py`, `test_flow.py`. `conftest.py` forces `APP_ENV=test` and `PROVIDER=fake`, and clears the settings cache. After changing settings in a test, call `get_settings.cache_clear()` and `reset_runtime()`. Frontend tests are vitest on `copy.ts`.

QA: `PROVIDER=fake` for failure, restart, and concurrency scenarios. `PROVIDER=openai` only for the single real end-to-end run and the quality metric. Record results in `docs/QA_RESULTS.md`. Fixtures come from `docs/fixtures/make_fixtures.py`.

## Working rules

- Untracked plans for the current build are in `.ai/specs/`. If that folder has files, read the file named in your task before editing.
- Keep business logic in `core`. Routes and MCP tools call the service or the client.
- Do not add code comments.
- Do not put secrets in commands, logs, or commits. `.env`, `tmp/`, and `data/` stay untracked.
- A new schema change is a new Alembic revision. Do not edit `001_initial` once it has shipped.
- Quality bar for a finished job: target-language detection on translated text blocks, and length ratio inside `languages.py`. Remaining flags set `quality_warning` and a `review_done` event.
- Acceptance: price shown before translate; kill/restart does not rebill done segments; bad uploads return a specific `error_code` and write nothing outside the uploads directory.
