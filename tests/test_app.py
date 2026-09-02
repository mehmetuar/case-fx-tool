"""Offline tests for the FX tool service.

No network is used anywhere: the upstream is an in-process fake handed to the
app through httpx.MockTransport, except one test that points the app at a
deliberately unreachable local port — the same way test.sh is run in review.
"""

from __future__ import annotations

import socket
from datetime import date, timedelta

import httpx
from fastapi.testclient import TestClient

from main import create_app

BASE = "http://upstream.test"

TODAY = date.today()
# A Friday..Monday layout anchored on real weekdays so weekend logic is stable
# whichever day the suite runs; always at least a week in the past.
FRIDAY = TODAY - timedelta(days=TODAY.weekday() + 10)  # the Friday before last
SATURDAY = FRIDAY + timedelta(days=1)
MONDAY = FRIDAY + timedelta(days=3)


class FakeUpstream:
    """A tiny Frankfurter: /v1/currencies, /v1/latest, /v1/{date}.

    Mirrors the real API's observed behavior: a request for a day with no
    published rate answers 200 with the most recent earlier date; unknown
    data answers 404 {"message": "not found"}.
    """

    def __init__(self, rates: dict[str, dict[str, str]],
                 currencies: list[str] | None = None) -> None:
        self.rates = {date.fromisoformat(d): pairs for d, pairs in rates.items()}
        self.currencies = currencies
        self.rate_calls = 0
        self.transport = httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert path.startswith("/v1/"), f"expected /v1/ prefix, got {path}"
        tail = path[len("/v1/"):]

        if tail == "currencies":
            if self.currencies is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json={c: c for c in self.currencies})

        self.rate_calls += 1
        params = dict(request.url.params)
        base, symbol = params.get("base"), params.get("symbols")
        asked = max(self.rates) if tail == "latest" else date.fromisoformat(tail)
        published = [d for d in self.rates if d <= asked]
        if not published:
            return httpx.Response(404, json={"message": "not found"})
        rate_date = max(published)
        pair = f"{base}-{symbol}"
        if pair not in self.rates[rate_date]:
            return httpx.Response(404, json={"message": "not found"})
        return httpx.Response(200, json={
            "amount": 1.0,
            "base": base,
            "date": str(rate_date),
            "rates": {symbol: float(self.rates[rate_date][pair])},
        })


def make_client(fake: FakeUpstream) -> TestClient:
    app = create_app(base_url=BASE,
                     client=httpx.AsyncClient(transport=fake.transport))
    return TestClient(app, raise_server_exceptions=False)


def eur_try(rate: str = "56.1718") -> FakeUpstream:
    return FakeUpstream({str(FRIDAY): {"EUR-TRY": rate}, str(MONDAY): {"EUR-TRY": rate}},
                        currencies=["EUR", "TRY", "USD"])


# ---------------------------------------------------------------------------
# The happy path, and the dates that are the point of this task


def test_exact_date():
    client = make_client(eur_try())
    body = client.get(f"/tools/convert?amount=250&from=EUR&to=TRY&date={FRIDAY}").json()
    assert body == {
        "amount": 250,
        "from": "EUR",
        "to": "TRY",
        "rate": 56.1718,
        "result": 14042.95,  # 250 * 56.1718 = 14042.95 exactly
        "rate_date": str(FRIDAY),
        "asked_date": str(FRIDAY),
        "note": None,
        "source": "ECB via frankfurter.dev",
    }


def test_weekend_uses_previous_rate_and_says_so():
    client = make_client(eur_try())
    body = client.get(f"/tools/convert?amount=100&from=EUR&to=TRY&date={SATURDAY}").json()
    assert body["rate_date"] == str(FRIDAY)
    assert body["asked_date"] == str(SATURDAY)
    assert str(FRIDAY) in body["note"] and str(SATURDAY) in body["note"]


def test_gap_beyond_seven_days_is_an_error_not_a_stale_number():
    fake = FakeUpstream({"2026-01-02": {"EUR-TRY": "50"}}, currencies=["EUR", "TRY"])
    response = make_client(fake).get(
        "/tools/convert?amount=1&from=EUR&to=TRY&date=2026-03-01")
    assert response.status_code == 404
    assert response.json()["error"] == "no_rate_for_date"


def test_future_date_rejected_without_calling_upstream():
    fake = eur_try()
    response = make_client(fake).get(
        f"/tools/convert?amount=1&from=EUR&to=TRY&date={TODAY + timedelta(days=1)}")
    assert response.status_code == 400
    assert response.json()["error"] == "date_in_future"
    assert fake.rate_calls == 0


def test_date_before_series_rejected_without_calling_upstream():
    fake = eur_try()
    response = make_client(fake).get(
        "/tools/convert?amount=1&from=EUR&to=TRY&date=1998-12-31")
    assert response.status_code == 400
    assert response.json()["error"] == "date_before_series"
    assert fake.rate_calls == 0


def test_latest_reports_todays_ask_and_the_real_rate_date():
    client = make_client(eur_try())
    body = client.get("/tools/convert?amount=10&from=EUR&to=TRY").json()
    assert body["asked_date"] == str(TODAY)
    assert body["rate_date"] == str(MONDAY)
    assert body["note"] is not None  # MONDAY is in the past, and it says so


# ---------------------------------------------------------------------------
# Currencies and amounts


def test_same_currency_is_an_error():
    fake = eur_try()
    response = make_client(fake).get("/tools/convert?amount=1&from=EUR&to=EUR")
    assert response.status_code == 400
    assert response.json()["error"] == "same_currency"
    assert fake.rate_calls == 0


def test_malformed_currency_code():
    response = make_client(eur_try()).get("/tools/convert?amount=1&from=EU&to=TRY")
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_currency"


def test_unknown_currency_when_upstream_lists_currencies():
    response = make_client(eur_try()).get("/tools/convert?amount=1&from=XXX&to=TRY")
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_currency"


def test_unknown_currency_when_upstream_has_no_currency_list():
    fake = FakeUpstream({str(FRIDAY): {"EUR-TRY": "56"}}, currencies=None)
    response = make_client(fake).get(
        f"/tools/convert?amount=1&from=XXX&to=TRY&date={FRIDAY}")
    assert response.status_code == 404  # fail open: upstream's 404, surfaced honestly
    assert response.json()["error"] == "no_rate_for_date"


def test_lowercase_codes_are_normalized():
    body = make_client(eur_try()).get(
        f"/tools/convert?amount=1&from=eur&to=try&date={FRIDAY}").json()
    assert (body["from"], body["to"]) == ("EUR", "TRY")


def test_bad_amounts():
    client = make_client(eur_try())
    for bad in ("abc", "0", "-5", "nan", "inf", "9999999999999"):
        response = client.get(f"/tools/convert?amount={bad}&from=EUR&to=TRY")
        assert response.status_code == 400, bad
        assert response.json()["error"] == "invalid_amount", bad


def test_missing_amount():
    response = make_client(eur_try()).get("/tools/convert?from=EUR&to=TRY")
    assert response.status_code == 400
    assert response.json()["error"] == "missing_parameter"


def test_rate_is_not_rounded_before_multiplying():
    # TRY->EUR-style tiny rate: rounding 0.0178 to 0.02 first would give 20.00.
    fake = FakeUpstream({str(FRIDAY): {"TRY-EUR": "0.0178"}},
                        currencies=["EUR", "TRY"])
    body = make_client(fake).get(
        f"/tools/convert?amount=1000&from=TRY&to=EUR&date={FRIDAY}").json()
    assert body["result"] == 17.8
    assert body["rate"] == 0.0178


def test_ten_decimal_amount_is_accepted_and_result_rounded_to_cents():
    body = make_client(eur_try("2")).get(
        f"/tools/convert?amount=33.3333333333&from=EUR&to=TRY&date={FRIDAY}").json()
    assert body["result"] == 66.67
    assert body["amount"] == 33.3333333333


# ---------------------------------------------------------------------------
# Upstream failure modes — never a 200 with a made-up number


def test_upstream_500():
    fake = eur_try()
    fake.transport = httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
    response = make_client(fake).get(
        f"/tools/convert?amount=1&from=EUR&to=TRY&date={FRIDAY}")
    assert response.status_code == 502
    assert response.json()["error"] == "upstream_error"


def test_upstream_not_json():
    fake = eur_try()
    fake.transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="<html>maintenance</html>"))
    response = make_client(fake).get(
        f"/tools/convert?amount=1&from=EUR&to=TRY&date={FRIDAY}")
    assert response.status_code == 502
    assert response.json()["error"] == "upstream_invalid_response"


def test_upstream_timeout():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    fake = eur_try()
    fake.transport = httpx.MockTransport(slow)
    response = make_client(fake).get(
        f"/tools/convert?amount=1&from=EUR&to=TRY&date={FRIDAY}")
    assert response.status_code == 504
    assert response.json()["error"] == "upstream_timeout"


def test_unreachable_upstream_via_env_like_review(monkeypatch):
    # The reviewers run test.sh with FX_UPSTREAM_BASE at a closed port; this is
    # that exact scenario. Find a port nothing listens on, point the app at it.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("FX_UPSTREAM_BASE", f"http://127.0.0.1:{port}")
    client = TestClient(create_app(), raise_server_exceptions=False)
    response = client.get("/tools/convert?amount=1&from=EUR&to=TRY")
    assert response.status_code == 502
    assert response.json()["error"] == "upstream_unavailable"


# ---------------------------------------------------------------------------
# Caching — a repeat of the same question must not re-ask the upstream


def test_repeat_question_hits_upstream_once():
    fake = eur_try()
    client = make_client(fake)
    for _ in range(3):
        body = client.get(f"/tools/convert?amount=5&from=EUR&to=TRY&date={FRIDAY}").json()
        assert body["rate"] == 56.1718
    assert fake.rate_calls == 1


def test_latest_is_cached_too():
    fake = eur_try()
    client = make_client(fake)
    client.get("/tools/convert?amount=5&from=EUR&to=TRY")
    client.get("/tools/convert?amount=7&from=EUR&to=TRY")
    assert fake.rate_calls == 1


def test_health():
    assert make_client(eur_try()).get("/health").json() == {"ok": True}
