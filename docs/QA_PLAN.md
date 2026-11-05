# QA plan

Run failure and restart cases with `PROVIDER=fake`. Use `PROVIDER=openai` only for one real PDF, one scanned PDF, and the quality metric. Do not commit `.env` or generated fixtures.

Generate fixtures:

```bash
cd backend
uv run python ../docs/fixtures/make_fixtures.py
uv run python ../docs/fixtures/make_fixtures.py --large
```

`docs/fixtures/generated/` is gitignored. `--large` writes `long_200p.pdf` and `huge_400p.pdf`. `oversize.pdf` is about 41MB.

`passwd.pdf` is the body for the traversal case. Upload it with the filename `../../etc/passwd.pdf`.

## Environment

```bash
cp .env.example .env
docker compose up --build
```

Faults are worker env vars: `FAKE_FAIL_MODE` (`http500`, `timeout`, `rate_limit`, `untranslated`), `FAKE_FAIL_SEGMENTS`, `FAKE_FAIL_RATE`, `FAKE_LATENCY_MS`, `CRASH_AFTER_SEGMENT`, `PROVIDER_MAX_CONCURRENCY`, `MAX_JOB_COST_CENTS`.

Record each scenario in `docs/QA_RESULTS.md`: id, pass/fail, evidence, notes. A failure needs the fixture name, env flags, and a log excerpt.

## Scenarios

### HP — Happy path

Preconditions: stack up, `PROVIDER=fake`.

1. Upload `short.pdf`. Analysis shows pages, tokens, and a price before Translate.
2. Translate to German. Download the PDF. Page count matches. The file contains translated text.
3. Repeat with `short.docx`.

Pass: both downloads open, page or section count is preserved, no error event.

### ED — Edge inputs

Upload each file and record `error_code`. No job row. No 500. No file outside the uploads directory.

| File | Expected code |
| --- | --- |
| `zero_byte.pdf` | `invalid_file` |
| `corrupt_truncated.pdf` | `corrupt_pdf` |
| `png_renamed.pdf` | `invalid_file` |
| `encrypted.pdf` | `encrypted_pdf` |
| `oversize.pdf` | `file_too_large` |
| `unsupported.xlsx` | `unsupported_type` |
| `unsupported.exe` | `unsupported_type` |
| `empty_text.pdf` | analysis fails with a specific code, no translate job |
| `../../etc/passwd.pdf` (body of `passwd.pdf`) | stored basename only |

### CL — Page class and images

Read `documents.analysis.pages[]` after analysis.

| Fixture | Expected class |
| --- | --- |
| `book_text_with_figures.pdf` | `text_with_images` on every page |
| `scanned_300dpi.pdf` | `scanned_good` |
| `scanned_72dpi.pdf`, `photo_blurry.jpg` | `scanned_poor` |
| `garbled_font.pdf` | `text_garbled` |
| `scanned_blank.pdf` | `blank` |
| `non_document.jpg` | job rejected `not_a_document` and zero OCR calls |

Tier 1 vision runs only for `unknown` pages. Count FakeVisionClassifier calls. The analysis card shows per-class cost. The translated book has the same image count as the source, captions translated, reading order kept. `docx_with_images.docx` keeps every inline image. Layout mode, if selected, keeps image positions within tolerance or reports the page as reflowed.

### SC — Scanned and photos

1. `scanned_300dpi.pdf`: analysis lists the scanned page and an OCR cost before Translate. One real `PROVIDER=openai` run is allowed here.
2. `scanned_mixed.pdf`: OCR call count is 2.
3. `scanned_blank.pdf`: `ocr_no_text` naming the pages. No paid OCR.
4. `photo_page.jpg` finishes. `photo_blurry.jpg` is `ocr_no_text` or contains `[illegible]`, never an empty success.
5. `MAX_JOB_COST_CENTS` below the estimate aborts with `cost_cap_exceeded`.
6. Kill the worker mid-OCR. Restart. Done OCR pages are not called again.

### LG — Large documents

`long_200p.pdf` and `huge_400p.pdf` reach `done`. Progress is monotonic. Actual cost is within the tolerance shown on the analysis card.

### CC — Concurrency

1. Start `long_200p.pdf`, then `short.pdf`. The short job finishes first.
2. `docker compose up --scale worker=2`. `/debug/provider-calls` has one ok translate call per segment minus translation-memory hits.
3. `FAKE_LATENCY_MS=2000` and `PROVIDER_MAX_CONCURRENCY=4`. Max in-flight never exceeds 4.
4. `FAKE_FAIL_MODE=rate_limit`. Concurrency drops in `/metrics` and the job still completes.

### RS — Restart

1. `docker compose kill worker` at about 30% progress, then start it. Done segments are not billed again. The output is valid or absent, never partial.
2. Restart the API during polling. The UI reconnects.
3. `docker compose stop db` for 20 seconds, then start. No job stays stuck longer than `STALE_SECONDS`.

### ID — Idempotency and retries

1. The same `Idempotency-Key` returns the same job id.
2. `FAKE_FAIL_MODE=http500` and `FAKE_FAIL_SEGMENTS=5,7` retries then succeeds, with one ok translate call per segment.
3. `FAKE_FAIL_MODE=timeout` fails the job with `provider_timeout` after `SEGMENT_ATTEMPT_LIMIT`. It does not stay `translating`.
4. `FAKE_FAIL_MODE=rate_limit` completes.

### QL — Bad language pair

`FAKE_FAIL_MODE=untranslated`. Review flags the segment and retranslates. Events include `quality_warning`. The UI shows that warning, not a generic error.

### TM — Translation memory

1. `repeat_paragraph_50x.pdf` pays for the repeated paragraph once.
2. A second job on the same file costs about 0.
3. A third job with the other model still costs about 0.

### MD — Model and chunking

The analysis card lists different tokens, segments, and cents per model. A test model with a small `max_output_tokens` increases segment count, and every `tokens_in` from `GET /api/jobs/{id}/segments` stays inside the budget. A second job that changes model bills only blocks that are not already in memory.

### PV — Preview and edit

Edit one target segment. Download matches the edit. `output_stale` is clear after re-render.

### MCP — Agent tools

Use only the README config. Do not open the web UI.

1. `estimate_translation` on `short.pdf`.
2. `translate_document` to German. The output file exists.
3. `get_translation_status` returns `done`.

### OB — Observability

Worker logs include `job_id` and `stage`. `/metrics` has job and provider counters labeled by stage and model. `GET /api/jobs/{id}/events` explains each failure above. `GET /api/jobs/{id}/cost` equals the sum of `provider_calls` for that job, including failed attempts. On one real OpenAI run, `tokens_cached` is non-zero after the first segment. `docker compose --profile observability up` shows the Grafana dashboard and, with `OTEL_EXPORTER=otlp`, a Langfuse trace.

### FR — Fresh clone

Clone into a temp directory. `docker compose up --build` with `.env.example` copied to `.env` and `PROVIDER=fake`. No secrets in git history.
