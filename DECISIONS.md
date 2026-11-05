# Decisions

## Postgres instead of Redis and a broker

Jobs, segments, and OCR pages are rows. A worker claims one with `FOR UPDATE SKIP LOCKED`, ordered by fewest in-flight segments and then fewest pending, so a short job is not stuck behind a long one. Completion of a segment, its translation, and its `provider_calls` row is one transaction.

A broker would deliver work faster and would still need Postgres for the document, the translations, and the cost ledger. Restart safety would then depend on two systems agreeing. Redis as a second state store fails closed when Redis is down even if Postgres is healthy. One database is the smaller failure domain for this workload. The cost is polling and a reaper (`STALE_SECONDS`, default 120) instead of push delivery.

## PDF layout

Reflow is the default. Translated text is longer than English in most target languages, so painting it back into the original boxes overflows. Reflow keeps reading order, headings, tables, and embedded images, and it rebuilds pages.

Layout mode removes text objects and draws the translation in the original boxes. If a page overflows, that page falls back to reflow. OCR pages are always reflowed: vision OCR does not return reliable image boxes, so a scanned page comes back as text, not as a rebuilt photo.

## Models and prices

Prices are OpenAI list rates recorded at build time, USD per 1M tokens.

| Model | Input | Output | Role |
| --- | --- | --- | --- |
| gpt-4o-mini-2024-07-18 | $0.15 | $0.60 | Default translation and the cheap page classifier |
| gpt-4o-2024-08-06 | $2.50 | $10.00 | Review of flagged segments, OCR |

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
- `not_a_document` is an Agents SDK input guardrail that runs during analysis, before OCR.
- Layout mode keeps pages that fit and reflows only the pages that overflow. There is no visual diff of those pages.
- Translation memory is model-independent. A later job in another model reuses the stored text and does not re-bill.

## Three more weeks

1. Layout mode that keeps line boxes when the translation fits and only reflows the overflowing pages, with a visual diff.
2. Batch API as an explicit economy option on the price card.
3. A keyed quality run on the fixture set: target-language rate, length-ratio rate, and a real invoice next to `provider_calls`.
