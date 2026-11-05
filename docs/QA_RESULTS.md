# QA results

Automated checks on 5 Nov 2026 against local Postgres (`127.0.0.1:5433`) and `PROVIDER=fake`. Docker-based restart, Grafana, and a live OpenAI run were not executed in this pass.

| ID | Result | Evidence | Notes |
| --- | --- | --- | --- |
| HP | pass (API and browser) | `tests/test_flow.py::test_lifecycle_and_idempotency`; browser at `http://127.0.0.1:5173` | Uploaded `short.pdf`, saw 2 pages and both prices, translated to German, edited page 2, download contained the edit. A renamed PNG showed `invalid_file`. DOCX was not clicked in the browser. |
| ED | pass (API codes) | `test_corrupt_and_unsupported` | Zero-byte, renamed PNG, and exe. Encrypted, oversize, and traversal filename still belong to the manual fixture pass. |
| CL | partial | `tests/test_core.py` classifier and PDF image round-trip | Fixture class table not executed end to end. |
| SC | not run | | Needs generated fixtures plus one keyed OCR run. |
| LG | not run | | Generate with `--large`. |
| CC | not run | | Fairness is implemented in the claim query; two-worker docker scale was not run. |
| RS | partial | `test_crash_resume_skips_done_segments`, `test_reaper_requeues_stale_segment` | Process crash and reaper, not `docker compose kill`. |
| ID | pass | `test_lifecycle_and_idempotency`, `test_retry_does_not_double_bill` | Timeout and rate-limit modes not in this run. |
| QL | not run | | Untranslated mode is implemented in FakeProvider and the review heuristic. |
| TM | partial | crash-resume asserts done segments are not billed again | 50x repeat fixture not run. |
| MD | partial | `tests/test_core.py` budget and chunking | UI card not checked in a browser. |
| PV | not run | | Edit and re-render endpoints exist. |
| MCP | partial | tool schema lists the four tools; in-process estimate returns models | Stdio progress against Cursor was not run. |
| OB | partial | `test_cost_endpoint_matches_rows`, `/metrics` route | Grafana and Langfuse profile not started. |
| FR | not run | | |

## Benchmark

Fill this section from `python scripts/benchmark.py` with `DATABASE_URL` set. The figure is token usage priced at list rates through FakeProvider, not a live invoice.

```
status=done cost_cents=0.0195 p95_latency_ms=0.0
summary: calls=1 tokens_in=206 tokens_out=15 cost_cents=0.0040 p95_ms=0.0
translate: calls=1 tokens_in=261 tokens_out=193 cost_cents=0.0155 p95_ms=0.0
```

Twelve source lines were extracted as one block. Latency is the fake client (`FAKE_LATENCY_MS=0`), not a live model. Cents are list prices times measured tokens.
