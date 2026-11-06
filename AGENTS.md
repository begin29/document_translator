# Stark Translator

PDF, DOCX, and page-photo translation. One Python core serves a FastAPI web API and an MCP stdio server. Postgres is the state store and the queue (`FOR UPDATE SKIP LOCKED`). Workers claim work; a restart continues from the last committed segment. Default provider is `PROVIDER=fake`.

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
| `backend/app/core/agents/` | Review agent and `not_a_document` guardrail |
| `backend/app/core/prompts.py` | Prompt text. Bump `PROMPT_VERSION` when a prompt changes |
| `backend/app/core/errors.py` | `AppError` and provider error types |
| `backend/app/api/routes.py` | HTTP shell. Routers mounted at `/api` |
| `backend/app/worker.py` | Thread loop: `reap`, `sync_gate`, `process_available` |
| `backend/app/mcp/` | Four tools plus `HttpClient` / `InProcessClient` |
| `backend/app/db/models.py` | SQLAlchemy models. Migrations in `backend/alembic/` |
| `backend/app/config.py` | `Settings` from env. `get_settings()` is cached |
| `frontend/src/` | `App.tsx` (routes), `api.ts`, `copy.ts` (error strings) |
| `scripts/benchmark.py` | Cost and p95 from `provider_calls` |

A new format is one adapter plus an `ADAPTERS` entry plus a sniff branch. Image bytes stay on the files volume (`asset_path`). Blocks with `kind=image` are never sent to a provider.

## Pipeline

Document analysis is once per file (`documents.analysis_status`: `queued` → `ready` or `failed`). A job then runs:

`queued` → `extracting` → (`ocr` if scanned pages) → `translating` → `reviewing` → `rendering` → `done`

`failed` and `cancelled` are terminal. Stale heartbeats (`STALE_SECONDS`, default 120) are requeued by `reap`.

Page classes: `text`, `text_with_images` (text pipeline); `text_garbled`, `scanned_good` (OCR); `scanned_poor` (deskew, autocontrast, upscale, then OCR); `blank` (skipped); `unknown` (Tier 1 vision only). All pages `non_document` or `blank` fail as `not_a_document` before OCR.

During `translating` the unit of work is the **segment**. Claim prefers the job with the fewest in-flight segments. Segment completion, its `block_translations`, and its `provider_calls` row are one transaction. Done segments are not translated again.

Chunking groups blocks up to `segment_budget` (stored on the job as `segment_budget_tokens`). Oversized blocks split on sentence boundaries, then tokens, and are flagged so render rejoins them. Prompts use numbered blocks and require numbered blocks back (`numbered.py`).

Translation memory key is `(src_hash, source_lang, target_lang)`. Hits are model-independent (`source=tm`) and are excluded from the prompt. User edits set `output_stale`; `POST /api/jobs/{id}/render` rebuilds the file.

Review (`openai-agents`, `REVIEW_MODEL`) runs only on flagged segments (length ratio, language, glossary, untranslated). Tools: `get_source_segment`, `get_neighbor_translations`, `lookup_glossary`, `retranslate_segment`, `accept_segment`, and `view_source_page_image` for OCR pages. Bulk translation stays a completion. In fake mode the retranslate path is a heuristic.

OCR and review use `gpt-4o` (`gpt-4o-2024-08-06`). Bulk translation and Tier 1 classification use `gpt-4o-mini` (`gpt-4o-mini-2024-07-18`). Send `max_completion_tokens`. Prices live on the `ModelSpec`. `provider_calls` is the cost ledger; `GET /api/jobs/{id}/cost` must match that table. `MAX_JOB_COST_CENTS` aborts with `cost_cap_exceeded`.

PDF render modes: `reflow` (default) and `layout` (bbox overlay, per-page fallback to reflow). OCR pages always reflow. DOCX writes the translation into the first text run and leaves `w:drawing` runs untouched. PNG/JPEG uploads are one OCR page rendered to PDF.

## API and MCP

Errors are `AppError` → `{code, message, detail, retryable}` with a real status. Add a specific message in `frontend/src/copy.ts` for every new code. Upload codes already in use: `file_too_large`, `invalid_file`, `corrupt_pdf`, `corrupt_file`, `encrypted_pdf`, `unsupported_type`, `empty_document`, `not_a_document`. Job codes: `ocr_no_text`, `provider_timeout`, `provider_error`, `rate_limited`, `cost_cap_exceeded`, `not_ready`, `unknown_language`, `unknown_model`, `cancelled`.

`POST /api/jobs` honors `Idempotency-Key` and returns the existing job. Same sha256 returns the existing document. Uploads stay under `FILES_DIR` (`uploads/`, `assets/`, `thumbs/`). Filename is the basename only.

MCP tools, and only these: `estimate_translation`, `translate_document`, `get_translation_status`, `download_translation`. `TRANSLATOR_API_URL` selects `HttpClient`; empty uses `InProcessClient`.

`GET /health`, `GET /ready` (DB ping), `GET /metrics` are unprefixed. `GET /api/debug/provider-calls` exists only when `PROVIDER=fake`.

## Providers and limits

`build_provider()` returns `OpenAIProvider` only when `PROVIDER=openai`. It raises if `APP_ENV=test`. Tests and QA failure scenarios stay on `FakeProvider`.

Fake knobs: `FAKE_LATENCY_MS`, `FAKE_FAIL_MODE` (`http500|timeout|rate_limit|untranslated`), `FAKE_FAIL_SEGMENTS`, `FAKE_FAIL_RATE`, `CRASH_AFTER_SEGMENT`.

Concurrency ceilings: `PROVIDER_MAX_CONCURRENCY` (32), `PROVIDER_TPM_BUDGET` (2_000_000), `JOB_MAX_CONCURRENCY` (16). The gate is a Postgres counter shared by worker replicas. A 429 halves effective concurrency and ramps one slot per quiet minute. `SEGMENT_ATTEMPT_LIMIT` is 3.

`OTEL_EXPORTER=none|otlp` (default `none`). `AGENT_TRACE_EXPORTER` covers the review agent only. Observability containers are the `observability` compose profile.

## Commands

```bash
cp .env.example .env
docker compose up --build          # UI :8080, API :8000, provider from .env
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
