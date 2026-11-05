# Stark Translator

Stark Translator turns a PDF, DOCX, or photo of a page into a translated file. It is built for people who need a document back, with a price shown before any translation starts, and for an agent that can run the same job from Cursor or Claude Code without opening the browser.

The interface uses the Stark Future palette: gold `#CFAC57`, gray `#E0E0E0`, black, and white.

## Architecture

```
browser or MCP client
        |
     FastAPI
        |
   Postgres  ---- worker processes
        |
 OpenAI or FakeProvider
```

Postgres is the state store and the queue. Workers claim jobs, OCR pages, and segments with `FOR UPDATE SKIP LOCKED`. There is no Redis and no message broker. A restart continues from the last committed segment.

A document is analyzed once. Analysis classifies every page, extracts text blocks and images, and prices each model in `models.yaml`. A job then groups blocks into segments that fit the chosen model's output budget, translates them, reviews only the segments that look wrong, and renders a file.

Default translation model is `gpt-4o-mini` (`gpt-4o-mini-2024-07-18`). Review of flagged segments and OCR of scanned pages use `gpt-4o` (`gpt-4o-2024-08-06`).

## Why the review step is an agent

Bulk translation is a completion. The model is given numbered blocks and must return the same numbers. It does not choose tools or a next step.

The review step is different. It runs only on segments flagged for length, language, glossary, or untranslated text. The agent can read the source, look at neighbors, check the glossary, retranslate with an instruction, or accept the text. That is the one place a tool-using agent earns its cost. In fake mode the same retranslate path runs from a heuristic, so tests do not call OpenAI.

## Observability

`provider_calls` in Postgres is the number to trust. `GET /api/jobs/{id}/cost` and `GET /api/stats/cost` sum that table. `/metrics` exposes the same counters for Prometheus.

| Backend | What it answers |
| --- | --- |
| `provider_calls` | Tokens, cents, latency, retries, and which stage spent the money. This is the ledger. |
| Prometheus + Grafana (`docker compose --profile observability up`) | Rates, queue depth, and in-flight concurrency while a job runs. Dashboard uid `stark-translator`. |
| Langfuse (same profile, `OTEL_EXPORTER=otlp`) | A trace tree of stages for one job. |
| OpenAI Traces (`AGENT_TRACE_EXPORTER=openai`) | Prompt-level view of the review agent only. It does not replace the ledger. |

## Run it

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8080`. The API is on port 8000. With `PROVIDER=fake` no API key is required.

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

Stdio server:

```bash
uv run --project backend python -m app.mcp
```

Cursor or Claude Code config:

```json
{
  "mcpServers": {
    "stark-translator": {
      "command": "uv",
      "args": ["run", "--project", "backend", "python", "-m", "app.mcp"],
      "env": {
        "TRANSLATOR_API_URL": "http://127.0.0.1:8000",
        "DATABASE_URL": "postgresql+psycopg://translator:translator@127.0.0.1:5432/translator"
      }
    }
  }
}
```

Leave `TRANSLATOR_API_URL` empty to use the in-process client, which needs `DATABASE_URL` and `FILES_DIR` and does not need the API process.

Three-step check, without the web UI:

1. `estimate_translation` on a small PDF. The reply lists models and a price.
2. `translate_document` to German. The tool reports progress and returns an output path.
3. `get_translation_status` on that job id returns `done`. The file from step 2 opens as a PDF or DOCX.

## Acceptance

1. Upload a text PDF, see page count and a price before Translate, download a file that contains the translation, and keep the page count.
2. Kill the worker after some segments are `done`. After restart those segments are not billed again and the job finishes.
3. A corrupt PDF, an encrypted PDF, a renamed PNG, an oversize file, and an unsupported type each return a specific `error_code` and message. None of them return a generic 500 or write a file outside the uploads directory.

## Quality metric

On the fixture set, a finished job should have:

- target-language detection on every translated text block, except blocks that are numbers, URLs, or glossary terms kept verbatim
- a length ratio inside the per-language bounds in `app/core/languages.py`

Blocks outside those bounds are flagged and sent to review. The job event `review_done` records `quality_warning` when any flag remains.
