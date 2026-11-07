# Decisions

## Postgres for the queue, Redis for live progress

Jobs, segments, and OCR pages are rows. A worker claims one with `FOR UPDATE SKIP LOCKED`, ordered by fewest in-flight segments and then fewest pending, so a short job is not stuck behind a long one. Completion of a segment, its translation, and its `provider_calls` row is one transaction. Redis is not that queue. A lost Redis does not stop a translation; the publisher logs `event_publish_failed` and the worker commits anyway.

Live progress is Redis pub/sub plus a separate realtime SSE service, not polling and not Postgres `LISTEN/NOTIFY`. NOTIFY takes a database-wide lock at commit, and a slow listener can fill the notify queue and fail the worker's own transaction. A snapshot query on every connect would turn a reconnect storm into a burst of primary reads, and Postgres would send every event to every listener. The API process is also the wrong place to hold a million idle streams: a deploy would drop them, and they would share a pool with uploads.

The worker publishes after commit. The realtime process subscribes only to the channels its browsers asked for and forwards those messages. It never opens a database connection. On load, reload, or `resync`, the client reads current state over REST. Pub/sub is best-effort. The next message carries absolute counters, so a gap is recovered without a replay log.

Contract v2 puts every update for one person on `user:{owner_id}`. The browser opens `GET /events?ticket=` with an HMAC ticket. The realtime service checks the ticket and does not look the user up in Postgres.

Sizing, not yet measured by the 20k connection run: one SSE connection is about 20–40 KB in asyncio with uvloop, so one process holds roughly 20–30k connections. A million concurrent connections needs about 40–50 realtime processes behind a load balancer. With the 500 ms throttle, Redis sees at most about 2 messages per second per active job. More throughput means Redis Cluster and sharded pub/sub (`redis_cluster=true`). Database load from streams is zero. A reload is one primary-key read (`GET /api/jobs/{id}`).

Next steps before 1M users, not part of this hand check: object storage for uploads, a queue other than Postgres, provider rate limits, and read replicas for REST.

## PDF layout

`layout` is the default. The automated fidelity tests passed: pages that fit keep their boxes, and only overflowing pages fall back to reflow. The side-by-side look at `text_with_pic_en.pdf` has not been run, so that visual check is still outstanding. Translated text is longer than English in most target languages, so painting it back into the original boxes can overflow.

Layout mode removes text objects and draws the translation in the original boxes. If a page overflows, that page falls back to reflow. OCR pages are always reflowed: vision OCR does not return reliable image boxes, so a scanned page comes back as text, not as a rebuilt photo. A block grows down to the next overlapping block, or to the page bottom margin, and stops at 4× the source box when nothing sits below it. DOCX headers and footers are not extracted, so they stay as in the source file.

## Models and prices

Prices are OpenAI list rates recorded at build time, USD per 1M tokens.

| Model | Input | Cached input | Output | Role |
| --- | --- | --- | --- | --- |
| gpt-4o-mini-2024-07-18 | $0.15 | $0.075 | $0.60 | Default translation and the cheap page classifier |
| gpt-4o-2024-08-06 | $2.50 | $1.25 | $10.00 | Review of flagged segments, OCR |

Image token estimates: gpt-4o-mini base 2833 plus 5667 per tile; gpt-4o base 85 plus 170 per tile.

A 200-page English prose PDF is about 5k tokens in and a similar number out per segment. At mini rates that is about **$0.11** for translation. The same document on gpt-4o is about **$1.90**. Review of a handful of flagged segments adds about **$0.10**, so a mini job with review is about **$0.20**. OCR of a scanned page is about **$0.004** plus output tokens; a 200-page scan is about **$1–2** to OCR, which dominates translation. These are estimates from token budgets and list prices, not an invoice.

Measured with `PROVIDER=fake` and `FAKE_LATENCY_MS=0` on 5 Nov 2026 (`python scripts/benchmark.py`, 12 source lines extracted as one block):

- status `done`
- summary: 206 in, 15 out, 0.0040 cents, p95 0 ms
- translate: 261 in, 193 out, 0.0155 cents, p95 0 ms
- total **0.0195 cents**, p95 **0 ms**

That p95 is the fake client, not the model. Real gpt-4o-mini calls are expected around 15–25 seconds and about 5k tokens. Two hundred pages sequential is about 20–25 minutes. At `JOB_MAX_CONCURRENCY=16` that is about 1.5–2 minutes, bounded by the 2M TPM budget.

## Cuts

- OpenAI Batch API and `service_tier: "flex"` are not implemented. They would be a `batch_submitted` state and a poller writing the same `block_translations` rows, at about half price and up to 24 hours of wait.
- `not_a_document` is a plain check (`ensure_is_document`) during analysis, before OCR. It is not an agent guardrail.
- Layout mode keeps pages that fit and reflows only the pages that overflow. The real-file visual check is still outstanding.
- Translation memory is model-independent and per user. A later job in another model reuses that user's stored text and does not re-bill.

## Three more weeks

1. The outstanding visual check of layout mode on a real file, then a visual diff of pages that fell back to reflow.
2. Batch API as an explicit economy option on the price card.
3. A keyed quality run on the fixture set: target-language rate, length-ratio rate, and a real invoice next to `provider_calls`.

## MCP over Streamable HTTP

The MCP server is its own process on port 8001. Tools speak HTTP to the API through `ApiClient`. They do not import the worker or open Postgres. `MCP_WORKSPACE` is mounted at the same path in the container and on the host, so a file the model names is the file the server reads, and a download lands next to the source. Stdio remains for a local client and uses `TRANSLATOR_API_TOKEN` instead of the request header.

## Accounts

Sign-in is email and password, hashed with argon2. The browser gets an opaque session cookie. Cookie mutations check `Origin` against `PUBLIC_ORIGIN` so a foreign site cannot reuse the cookie. MCP uses a personal API token (`stk_…`) created under Settings → Tokens. Email verification, password reset, and SSO are deferred.

Documents, jobs, and translation memory are keyed by `owner_id`. Uploading the same bytes as someone else creates a new document. A route that loads a row calls `owned(...)`. A missing id and another user's id both return 404, so the response does not reveal that the row exists.

## Realtime tickets

`POST /api/events/ticket` returns an HMAC of `user id`, expiry, and `AUTH_SECRET`, valid for 60 seconds. `GET /events?ticket=` checks that mac and subscribes to `user:{id}`. The realtime process has no database credentials. A bad ticket is 401.

## Review agent

Flagged segments are reviewed by the agents SDK only when `PROVIDER=openai`. Usage is written as `provider_calls.stage = review_agent`, including cached input tokens, and that cost is added to the job from the cents `_record` returns. An agent exception writes `review_agent_failed` and the heuristic retranslate path still runs. OTLP export of those agent traces is still a no-op. Bulk translation stays a completion.

## Languages

Russian is not a target. Ukrainian is. Prompts name the language (`Ukrainian (uk)`, not a bare `uk`). `PROMPT_VERSION` is `v2`.

## Estimate

`estimate_for` is the price shown before Translate and the value stored on `translation_jobs.estimate_cents`. It prices the real system prompt (about 200 tokens of summary placeholder), output as block tokens times `expansion_ratio` plus 8 tokens per block, a summary call of about 250 output tokens, and OCR as `image_cost_cents` per page plus about 600 tokens of translate input. Cached input uses `ModelSpec.cached_input_price_per_1m`, or half the input price when that field is unset. Translation-memory hits for that user are left out of the prompt. `GET /api/documents/{id}/estimate?target_lang=` runs this for the requested language. The `models` array stored at analysis time is only the first view (target `en`, expansion 1.3). The ±10% check against a live `PROVIDER=openai` run has not been done.
