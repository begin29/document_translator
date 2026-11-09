# Decisions

## Postgres for the queue, Redis for live progress

Jobs, segments, and OCR pages are rows. A worker claims one with `FOR UPDATE SKIP LOCKED`, ordered by fewest in-flight segments and then fewest pending, so a short job is not stuck behind a long one. Completion of a segment, its translation, and its `provider_calls` row is one transaction. Redis is not that queue. A lost Redis does not stop a translation; the publisher logs `event_publish_failed` and the worker commits anyway.

Live progress is Redis pub/sub plus a separate realtime SSE service, not polling and not Postgres `LISTEN/NOTIFY`. NOTIFY takes a database-wide lock at commit, and a slow listener can fill the notify queue and fail the worker's own transaction. A snapshot query on every connect would turn a reconnect storm into a burst of primary reads, and Postgres would send every event to every listener. The API process is also the wrong place to hold a million idle streams: a deploy would drop them, and they would share a pool with uploads.

The worker publishes after commit. The realtime process subscribes only to the channels its browsers asked for and forwards those messages. It never opens a database connection. On load, reload, or `resync`, the client reads current state over REST. Pub/sub is best-effort. The next message carries absolute counters, so a gap is recovered without a replay log.

Contract v2 puts every update for one person on `user:{owner_id}`. The browser opens `GET /events?ticket=` with an HMAC ticket. The realtime service checks the ticket and does not look the user up in Postgres.

Sizing, not yet measured by the 20k connection run: one SSE connection is about 20–40 KB in asyncio with uvloop, so one process holds roughly 20–30k connections. A million concurrent connections needs about 40–50 realtime processes behind a load balancer. With the 500 ms throttle, Redis sees at most about 2 messages per second per active job. More throughput means Redis Cluster and sharded pub/sub (`redis_cluster=true`). Database load from streams is zero. A reload is one primary-key read (`GET /api/jobs/{id}`).

Next steps before 1M users, not part of this hand check: object storage for uploads, a queue other than Postgres, provider rate limits, and read replicas for REST.

## PDF layout

`layout` is the default. The automated fidelity tests passed: pages that fit keep their boxes, and only overflowing pages fall back to reflow. Translated text is longer than English in most target languages, so painting it back into the original boxes can overflow.

The hand check on image-heavy files (`text_with_pic_en.pdf`) shows the limit of this approach. Text that sits in a narrow box next to an image wraps one letter per line, so a word runs down the page letter by letter. Text-only pages and DOCX files look right. This is the main quality gap of the MVP and the first item for the next three weeks.

Layout mode removes text objects and draws the translation in the original boxes. If a page overflows, that page falls back to reflow. OCR pages are always reflowed: vision OCR does not return reliable image boxes, so a scanned page comes back as text, not as a rebuilt photo. A block grows down to the next overlapping block, or to the page bottom margin, and stops at 4× the source box when nothing sits below it. DOCX headers and footers are not extracted, so they stay as in the source file.

## Models and prices

Prices are OpenAI list rates recorded at build time, USD per 1M tokens.

| Model | Input | Cached input | Output | Role |
| --- | --- | --- | --- | --- |
| gpt-4o-mini-2024-07-18 | $0.15 | $0.075 | $0.60 | Default translation and the cheap page classifier |
| gpt-4o-2024-08-06 | $2.50 | $1.25 | $10.00 | Review of flagged segments, OCR |

Image token estimates: gpt-4o-mini base 2833 plus 5667 per tile; gpt-4o base 85 plus 170 per tile.

A 200-page English prose PDF is about 5k tokens in and a similar number out per segment. At mini rates that is about **$0.11** for translation. The same document on gpt-4o is about **$1.90**. Review of a handful of flagged segments adds about **$0.10**, so a mini job with review is about **$0.20**. OCR of a scanned page is about **$0.004** plus output tokens; a 200-page scan is about **$1–2** to OCR, which dominates translation. These are estimates from token budgets and list prices, not an invoice.

## Measured cost and latency

From `provider_calls` on 9 Nov 2026, `PROVIDER=openai`, 10 finished jobs. Cents are measured tokens times list price.

| Document | Pages | Target | Estimate | Translate + summary | OCR | Review | Total |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `short.pdf` (text) | 2 | de | 0.025¢ | 0.009¢ | | | **0.009¢** |
| `book_text_with_figures_page1.pdf` | 1 | de | 0.022¢ | 0.006¢ | | | **0.006¢** |
| `book_text_with_figures_en.pdf` | 20 | uk | 0.055¢ | 0.035¢ | | | **0.035¢** |
| `docx_with_images.docx` | 1 | uk | 0.021¢ | 0.005¢ | | | **0.005¢** |
| `ua_with_figure.docx` | 1 | en | 0.021¢ | 0.005¢ | | | **0.005¢** |
| `text_with_photo.jpg` | 1 | de | 0.59¢ | 0.020¢ | 0.38¢ | | **0.40¢** |
| `text_with_pic_en.pdf` | 11 | de | 1.24¢ | 2.02¢ | | 31.66¢ | **33.68¢** |
| `text_with_pic_en.pdf` | 11 | uk | 1.15¢ | 1.51¢ | | 49.53¢ | **51.04¢** |
| `text_with_pic_ua_huge.pdf` | 32 | uk | 4.64¢ | 5.14¢ | 0.62¢ | 24.16¢ | **29.92¢** |
| `text_with_pic_ua_huge.pdf` | 32 | uk | 4.64¢ | 5.13¢ | 0.88¢ | 26.36¢ | **32.37¢** |

A clean text document costs a fraction of a cent, and the estimate is above the real cost. When segments are flagged, the gpt-4o review step dominates: on `text_with_pic_en.pdf` to Ukrainian the bill was 51¢ against a 1.15¢ estimate, about **+4300%**, while the translation part alone (1.51¢) was close to the estimate. The estimate does not include review, because whether a segment gets flagged is only known after translation.

Per-call latency (status `ok`):

| Stage | Model | Calls | Mean | p95 |
| --- | --- | --- | --- | --- |
| translate | gpt-4o-mini | 114 | 19.2 s | **43.8 s** |
| summary | gpt-4o-mini | 10 | 1.9 s | 2.6 s |
| OCR | gpt-4o | 5 | 3.4 s | 6.7 s |
| review retranslate (`review`) | gpt-4o | 9 | 37.7 s | 62.5 s |
| review agent loop (`review_agent`) | gpt-4o | 11 | 22.6 s | 29.3 s |

Time from the first to the last provider call of a job: 1–8 s for text PDFs and DOCX files of 1–20 pages, about 4 minutes for the 11-page image-heavy PDF, and 5–6 minutes for the 32-page one, most of it in review. The one-page photo took about 6 minutes, although each OCR call was under 7 s; that run is not explained yet. Segments run in parallel up to `JOB_MAX_CONCURRENCY=16`, so a long document is bounded by its slowest segment and by the 2M TPM budget, not by page count.

## Cuts

- OpenAI Batch API and `service_tier: "flex"` are not implemented. They would be a `batch_submitted` state and a poller writing the same `block_translations` rows, at about half price and up to 24 hours of wait.
- `not_a_document` is a plain check (`ensure_is_document`) during analysis, before OCR. It is not an agent guardrail.
- Layout mode keeps pages that fit and reflows only the pages that overflow. Narrow boxes next to images wrap badly (see PDF layout).
- Translation memory is model-independent and per user. A later job in another model reuses that user's stored text and does not re-bill.
- The review step is not priced in the estimate. Knowingly: it is a post-translation decision. The cost of that choice is shown under "Measured cost and latency".
- Email verification, password reset, SSO, object storage, and a dedicated queue. None of them change what the MVP has to prove.

## Where I stopped

This is a basic MVP. It proves three things: many documents translate in parallel without falling over, a crash or provider error does not lose or re-bill work, and the user sees a price before anything is spent. Translation of text is accurate. Layout on image-heavy pages, the price of the review step, and MCP from desktop apps are not good enough yet, and are the plan below.

## Three more weeks

1. **Layout accuracy on complex documents.** Documents with images currently wrap text in narrow boxes one letter per line until the word is complete. Fix: measure the real free width next to images, merge adjacent line blocks into paragraphs before translation, shrink the font within a floor before wrapping, and fall back to reflow for the region rather than letter-wrapping. Then a visual diff of every page that fell back.
2. **Cost estimation that includes review.** Translation is estimated well, but the review agent is not in the estimate, and on image-heavy files the real bill was up to about +4000% of the quote. Fix: price an expected review share from the flag rate per page class and language pair, show it as a range on the price card, add a per-job review budget that stops review (not the job) when reached, and move bulk retranslate from gpt-4o to gpt-4o-mini with gpt-4o only for the agent's decision.
3. **MCP for desktop apps.** The MCP server works from Claude Code and Cursor, which can run the `curl` upload command. Claude Desktop and similar chat apps have no shell, so they cannot send a local file to the separate service. Fix: accept the file as an MCP resource or a chunked base64 upload tool with a size cap, and return the result as a resource as well as a link.
4. Batch API as an explicit economy option on the price card.

## MCP over Streamable HTTP

The MCP server is its own process on port 8001. Tools speak HTTP to the API through `ApiClient`. They do not import the worker or open Postgres. Over HTTP the server is a standalone service. It does not mount host folders and rejects `path` and `output_path` with `path_not_supported`. A workspace shared by every token holder would let one user read or overwrite another's files, and a client's path means nothing on the server anyway. Documents are referenced by `document_id`, found with `list_documents` or sent with a signed upload link. Stdio remains for a local client. It takes `path` under `MCP_WORKSPACE` (default: the home folder, never unrestricted) and uses `TRANSLATOR_API_TOKEN` instead of the request header.

A client that does not share the server's disk cannot pass a `path`. `create_upload_url` returns a signed URL, the client posts the file with `curl`, and the tools take the resulting `document_id`. A finished job from such a source comes back as a signed `download_url`. The bearer token never appears in text the model sees, and a large PDF never passes through the model as base64. A server-side fetch from a URL the client names was rejected because of SSRF. Upload and download tickets use the realtime ticket HMAC, with the purpose (and the job id for downloads) in the mac, so one kind cannot open another. They last 15 minutes and are not single-use. Reuse only lets the same user upload again or fetch their own file.

## Accounts

Sign-in is email and password, hashed with argon2. The browser gets an opaque session cookie. Cookie mutations check `Origin` against `PUBLIC_ORIGIN` so a foreign site cannot reuse the cookie. MCP uses a personal API token (`stk_…`) created under Settings → Tokens. Email verification, password reset, and SSO are deferred.

Documents, jobs, and translation memory are keyed by `owner_id`. Uploading the same bytes as someone else creates a new document. A route that loads a row calls `owned(...)`. A missing id and another user's id both return 404, so the response does not reveal that the row exists.

## Realtime tickets

`POST /api/events/ticket` returns an HMAC of `user id`, expiry, and `AUTH_SECRET`, valid for 60 seconds. `GET /events?ticket=` checks that mac and subscribes to `user:{id}`. The realtime process has no database credentials. A bad ticket is 401.

## Review agent

Flagged segments are reviewed by the agents SDK only when `PROVIDER=openai`. Usage is written as `provider_calls.stage = review_agent`, including cached input tokens, and that cost is added to the job from the cents `_record` returns. An agent exception writes `review_agent_failed` and the heuristic retranslate path still runs. OTLP export of those agent traces is still a no-op. Bulk translation stays a completion.

## Estimate

`estimate_for` is the price shown before Translate and the value stored on `translation_jobs.estimate_cents`. It prices the real system prompt (about 200 tokens of summary placeholder), output as block tokens times `expansion_ratio` plus 8 tokens per block, a summary call of about 250 output tokens, and OCR as `image_cost_cents` per page plus about 600 tokens of translate input. Cached input uses `ModelSpec.cached_input_price_per_1m`, or half the input price when that field is unset. Translation-memory hits for that user are left out of the prompt. `GET /api/documents/{id}/estimate?target_lang=` runs this for the requested language. The `models` array stored at analysis time is only the first view (target `en`, expansion 1.3). The ±10% check against a live `PROVIDER=openai` run has not been done.
