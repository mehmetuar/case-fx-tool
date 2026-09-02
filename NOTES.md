# Notes

## Decisions

**When the ECB published no rate for the asked date** (weekend, holiday), I
answer with the most recent published rate instead of failing — a customer
asking "how much on Saturday?" is best served by "here is Friday's rate, and
it is Friday's". The upstream already reports which date its rates belong to,
so `rate_date` carries that date and a `note` field spells the difference out
in a sentence the model can relay. Two guards keep this honest: if the nearest
published rate is more than 7 days older than the asked date, I return an
error rather than a stale number; and a rate dated *after* the asked date is
treated as an upstream fault. I probed the real API before designing this:
asking for Saturday 2026-08-29 returns HTTP 200 — not an error — with
`"date": "2026-08-28"` and Friday's rate inside, no warning of any kind. Any
code that ignores the payload's `date` field quietly presents Friday's rate
as Saturday's; that observation shaped both this design and finding 3 of the
review.

**Future dates and dates before 1999-01-04** are rejected locally without
calling the upstream. There is no rate for the future; substituting "latest"
would present a number as belonging to a date it does not belong to.

**Money math** is `Decimal` end to end (upstream JSON parsed with
`parse_float=Decimal`); the rate is never rounded before multiplying, only
the final result to 2 dp. Long-decimal amounts are accepted — LLMs often pass
computed values — and echoed back as given.

**Caching** is keyed by `(from, to, date)`. Historical rates are immutable so
they cache indefinitely (bounded at 10k entries); `latest` gets a 10-minute
TTL; errors are never cached.

**Currency validation** uses the upstream's `/currencies` list, fetched once
per process and fail-open: if that auxiliary endpoint is missing or broken,
conversions still work and an unknown code surfaces as the upstream's own 404.

**Assumption:** the service calls `{FX_UPSTREAM_BASE}/v1/...`. The brief's
default base has no `/v1`, but the real Frankfurter API requires it, so I
assumed a replacement upstream mirrors the real path layout.

## With another day

- Structured logging with request IDs (failures currently surface only as
  error responses; operating this for real needs visibility into *why*).
- "Today" is the server's local date; the ECB publishes around 16:00 CET, so
  I would make the future-date check and `asked_date` CET-aware.
- A TTL + periodic refresh for the `/currencies` list instead of
  once-per-process.
- One retry with jitter on upstream 5xx/timeouts (never on 4xx), and a lock
  so concurrent identical requests don't stampede the upstream.
- Exact dependency pinning (a lock file) rather than ranges.

## AI tools

Claude Code, throughout. How the work was split:

- Before any code, I used it to probe the real Frankfurter API — weekend
  dates, future dates, pre-series dates, unknown codes, same-currency pairs.
  The design decisions above came from those observed behaviors, not from
  assumptions (e.g. the silent date-fallback on weekends, the `/v1` prefix,
  404 vs 422 semantics).
- It drafted the endpoint, error model and test scaffolding; I reviewed every
  diff before committing.
- The tests were written against behaviors I chose myself, not behaviors the
  generated code happened to have. In other words: I did not write tests from
  the AI's code and copy its mistakes into them — I decided the expected
  behavior first, pinned it down, and then made the code match it. So when a
  test fails, the code is wrong; the expectation was fixed.

## One thing the AI got wrong

The first draft of the test suite anchored its fake calendar with
`FRIDAY = today - (weekday + 3)`, so on Mondays the fake's "latest" date
would coincide with the real today and the `latest`-note assertion would
flip — a test that passes all week and fails on Mondays. I caught it while
reviewing the diff before the first run (walking the arithmetic through each
weekday) and moved the anchor a full week back. Small, but exactly the kind
of flake that erodes trust in a suite.
