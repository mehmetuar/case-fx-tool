# Review of tool.py

Ranked — most harmful to a paying customer first. "Verify" means a check I ran
or would run, not a guess. Line numbers refer to `tool.py` as committed.

## 1. Every failure returns HTTP 200 with `rate: 0.0` (lines 71–81)

The catch-all `except` turns *any* problem — upstream down, bad JSON, invalid
currency — into a confident `{"rate": 0.0, "result": 0.0}` with status 200.
The agent has no way to know it failed, so the customer is told their 250 EUR
is worth 0 TRY. This is the single worst behavior a pricing tool can have.
**Verify:** point the upstream at a closed port (or unplug the network) and
call the endpoint — you get 200 and zeros.
**This is the one I would fix before shipping tonight** — it converts every
other failure in this list into a confident wrong answer.

## 2. Invalid dates silently get today's rate, relabeled (lines 36–40)

If the asked date has no data (a future date, or one before 1999), the code
falls back to `/latest` and stamps the result with the *asked* date. Ask
for 2027-01-01 and you get today's rate presented as a 2027 rate. The customer
is quoted a number for a date no rate exists for.
**Verify:** `GET /tools/convert?amount=1&on=2027-01-01` → 200 with
`rate_date: "2027-01-01"`.

## 3. `rate_date` is the asked date, not the rate's date (lines 30, 44)

The upstream's own `date` field is never read. Frankfurter answers a Saturday
request with HTTP 200 and Friday's rate — I probed this — so weekend/holiday
queries label Friday's rate as Saturday's. The spec's `asked_date` field is
missing entirely, so the model cannot tell the customer which day the number
is from.
**Verify:** request any Saturday date; compare the response's `rate_date`
with the upstream payload's `date`.

## 4. Cache has no date in its key and no expiry (lines 21, 29–30, 43)

Keyed only by currency pair. Whichever rate is fetched first for `EUR-TRY` is
returned for *every* later request — any date, and "latest" — for the life of
the process, and stamped with whatever date was asked (line 30). A week-old
process quotes week-old rates as today's.
**Verify:** ask for a 2020 date, then ask for `latest` — same rate, different
labels.

## 5. The `from` parameter doesn't exist (line 48)

The parameter is `from_`, defaulting to `"EUR"`. An agent calling the
documented `?from=USD&to=TRY` silently gets EUR→TRY: the wrong pair, with no
error. Defaults on `from`/`to` hide integration mistakes; both should be
required.
**Verify:** `GET /tools/convert?amount=1&from=USD&to=TRY` → the response says
`"from": "EUR"`.

## 6. The rate is rounded before multiplying (line 60)

`round(rate, 2)` first: a TRY→EUR rate of 0.0178 becomes 0.02 — a 12% error
on every conversion, worse for KRW/IDR-class rates. Float math on money also
accumulates cent-level drift.
**Verify:** convert 1000 TRY to EUR; compare with the unrounded rate by hand.

Also worth noting, briefly: no timeout on the shared client (a slow upstream
hangs the agent's tool call indefinitely) and no `raise_for_status` (a 500's
HTML body goes to `.json()` and lands in finding 1); `amount` accepts zero and
negatives; the upstream host is hardcoded and `PORT` is ignored, so the service
can't be tested against a fake upstream at all.

## Things that looked suspicious but are fine

- `on: date | None` and `amount: float` — I tried garbled values and noticed
  FastAPI already rejects them with a 422 before the handler runs; a broken
  date never reaches the upstream. (The error *shape* doesn't match the spec,
  but the parsing is sound.)
- The module-level `AsyncClient` and dict cache — normal for a single-process
  service; not a bug, just undersized (see finding 4 for the real problem).
- `from __future__ import annotations` — I checked and the app runs fine with
  it on the Python version I tested (3.10).
- `/health` — does exactly what it should.
