# fx-tool

A small HTTP service an AI agent can call as a tool to convert money between
currencies, using ECB reference rates from a
[Frankfurter](https://frankfurter.dev)-compatible upstream.

The design rule, from the brief: **a wrong number is worse than no number.**
The service never invents a rate and never presents a rate as belonging to a
date it does not belong to.

## Run

```bash
./run.sh                      # serves on :8080
PORT=9000 ./run.sh            # or any port
FX_UPSTREAM_BASE=http://localhost:9999 ./run.sh   # custom upstream
```

The script creates `.venv` and installs dependencies on first run
(Python 3.10+). The upstream base defaults to `https://api.frankfurter.dev`;
the service calls `{FX_UPSTREAM_BASE}/v1/...` — the `/v1` path prefix matches
Frankfurter's real layout and is assumed for any replacement upstream.

## Test

```bash
./test.sh
```

No network needed: the upstream is faked in-process, and one test deliberately
points `FX_UPSTREAM_BASE` at a closed port to prove the service fails loudly
instead of answering with a made-up number.

## The endpoint

```
GET /tools/convert?amount=250&from=EUR&to=TRY&date=2026-08-28
```

`date` is optional; without it the latest published rates are used. On success:

```json
{
  "amount": 250,
  "from": "EUR",
  "to": "TRY",
  "rate": 56.1718,
  "result": 14042.95,
  "rate_date": "2026-08-28",
  "asked_date": "2026-08-28",
  "note": null,
  "source": "ECB via frankfurter.dev"
}
```

`rate_date` is the date the rate actually belongs to (as reported by the
upstream); `asked_date` is what the caller asked for (today, when `date` is
omitted). Whenever they differ, `note` says so in a sentence the model can
relay to the customer. `result` is `amount × rate` computed in `Decimal` and
rounded to 2 decimal places; the rate itself is never rounded before
multiplying — rounding a TRY→EUR-class rate like 0.0178 to 0.02 first would
inflate every conversion by ~12% (exactly the defect found in `REVIEW.md`).

## What happens in the edge cases

- **Weekend or holiday date:** the most recent published rate is used, with
  `rate_date` and `note` making that visible. If the nearest published rate is
  more than 7 days older than the asked date, the service returns
  `no_rate_for_date` instead of a stale number.
- **Future date, or before 1999-01-04** (the start of the ECB series):
  rejected locally with `date_in_future` / `date_before_series`; the upstream
  is not called and "latest" is never substituted.
- **Unknown currency:** validated against the upstream's `/currencies` list
  (fetched once per process, fail-open if unavailable) → `unknown_currency`.
  Malformed codes are rejected as `invalid_currency`; `from == to` is
  `same_currency`.
- **Upstream slow / down / broken:** a 5-second timeout; failures map to the
  `upstream_*` errors below. Never a 200 with a fabricated rate.
- **Amount:** must be a finite number > 0 (and below 10¹²). Long decimals are
  accepted and echoed back as given; only the result is rounded to cents.

Repeats of the same question are served from an in-process cache — historical
rates are immutable so they cache indefinitely (bounded), `latest` for
10 minutes. Errors are never cached.

## Error codes

All failures return a non-2xx status with `{"error": code, "message": text}`.

| Code | Status | When |
|---|---|---|
| `missing_parameter` | 400 | `amount`, `from` or `to` absent |
| `invalid_amount` | 400 | not a number, ≤ 0, non-finite, or implausibly large |
| `invalid_currency` | 400 | not a three-letter code |
| `invalid_date` | 400 | not `YYYY-MM-DD` |
| `date_in_future` | 400 | asked date is after today |
| `date_before_series` | 400 | asked date is before 1999-01-04 |
| `same_currency` | 400 | `from` and `to` are the same |
| `unknown_currency` | 404 | code is not in the upstream's currency list |
| `no_rate_for_date` | 404 | upstream has no data, or no published rate within 7 days of the asked date |
| `upstream_error` | 502 | upstream answered with an unexpected HTTP status |
| `upstream_invalid_response` | 502 | upstream body is not JSON / missing the rate or date |
| `upstream_unavailable` | 502 | upstream could not be reached |
| `upstream_timeout` | 504 | upstream took longer than 5 seconds |

## Files

- `main.py` — the service (Part A)
- `tests/test_app.py` — the offline test suite
- `NOTES.md` — decisions, what I'd do next, how AI tools were used
- `REVIEW.md` — Part B: review of `tool.py`
- `tool.py` — the AI-written version under review (unchanged, from the template)
