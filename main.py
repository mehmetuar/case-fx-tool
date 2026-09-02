"""FX conversion tool service.

One endpoint an AI agent can call as a tool:

    GET /tools/convert?amount=250&from=EUR&to=TRY&date=2026-08-28

Rates come from a Frankfurter-compatible upstream (ECB data). The base URL is
read from FX_UPSTREAM_BASE (default https://api.frankfurter.dev); the service
talks to `{FX_UPSTREAM_BASE}/v1/...`, matching Frankfurter's real path layout.

Design rule, from the brief: a wrong number is worse than no number. The
service never invents a rate and never presents a rate as belonging to a date
it does not belong to — `rate_date` always carries the date the upstream says
the rate is from, and `note` spells out any difference in plain language.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import OrderedDict
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

DEFAULT_UPSTREAM_BASE = "https://api.frankfurter.dev"
SERIES_START = date(1999, 1, 4)  # first date the ECB reference series covers
MAX_FALLBACK_DAYS = 7  # how far back an "earlier published rate" may reach
LATEST_TTL_SECONDS = 600  # historical rates never change; "latest" does
MAX_CACHE_ENTRIES = 10_000
MAX_AMOUNT = Decimal("1000000000000")  # 1e12 — anything above is a typo
UPSTREAM_TIMEOUT_SECONDS = 5.0
SOURCE = "ECB via frankfurter.dev"
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class ApiError(Exception):
    """An error we chose to return, as `{"error": code, "message": ...}`."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Upstream client


def _payload_of(response: httpx.Response) -> dict:
    """Parse an upstream body as JSON, keeping numbers as Decimal."""
    try:
        payload = json.loads(response.text, parse_float=Decimal)
    except json.JSONDecodeError:
        raise ApiError(502, "upstream_invalid_response",
                       "The rate provider returned something that is not JSON.")
    if not isinstance(payload, dict):
        raise ApiError(502, "upstream_invalid_response",
                       "The rate provider returned an unexpected document.")
    return payload


class Upstream:
    """Thin client for the Frankfurter-style API, with typed failures."""

    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self.base = base_url.rstrip("/") + "/v1"
        self.client = client
        self._currencies: set[str] | None = None
        self._currencies_checked = False

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        try:
            return await self.client.get(f"{self.base}/{path}", params=params)
        except httpx.TimeoutException:
            raise ApiError(504, "upstream_timeout",
                           "The rate provider took too long to answer.")
        except httpx.HTTPError:
            raise ApiError(502, "upstream_unavailable",
                           "The rate provider could not be reached.")

    async def known_currencies(self) -> set[str] | None:
        """The upstream's currency list, or None if it cannot tell us.

        Checked once per process. If the endpoint is missing or broken we fail
        open — conversions must not depend on an auxiliary endpoint.
        """
        if not self._currencies_checked:
            self._currencies_checked = True
            try:
                response = await self._get("currencies")
                if response.status_code == 200:
                    codes = {str(code).upper() for code in _payload_of(response)}
                    if codes:
                        self._currencies = codes
            except ApiError:
                self._currencies = None
        return self._currencies

    async def rate(self, base: str, target: str, path: str) -> tuple[Decimal, date]:
        """Fetch one rate. Returns (rate, the date the rate belongs to)."""
        response = await self._get(path, params={"base": base, "symbols": target})
        if response.status_code == 404:
            raise ApiError(404, "no_rate_for_date",
                           "The rate provider has no data for this request — "
                           "the date may be unavailable or the currency unknown.")
        if response.status_code != 200:
            raise ApiError(502, "upstream_error",
                           f"The rate provider answered with HTTP {response.status_code}.")

        payload = _payload_of(response)
        try:
            rate = payload["rates"][target]
            rate_date = date.fromisoformat(str(payload["date"]))
        except (KeyError, TypeError, ValueError):
            raise ApiError(502, "upstream_invalid_response",
                           "The rate provider's answer is missing the rate or its date.")
        if not isinstance(rate, Decimal):
            try:
                rate = Decimal(str(rate))
            except InvalidOperation:
                raise ApiError(502, "upstream_invalid_response",
                               "The rate provider returned a rate that is not a number.")
        if not rate.is_finite() or rate <= 0:
            raise ApiError(502, "upstream_invalid_response",
                           "The rate provider returned an impossible rate.")
        return rate, rate_date


# ---------------------------------------------------------------------------
# Request validation


def parse_amount(raw: str) -> Decimal:
    try:
        amount = Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        raise ApiError(400, "invalid_amount", f"'{raw}' is not a number.")
    if not amount.is_finite():
        raise ApiError(400, "invalid_amount", "The amount must be a finite number.")
    if amount <= 0:
        raise ApiError(400, "invalid_amount", "The amount must be greater than zero.")
    if amount > MAX_AMOUNT:
        raise ApiError(400, "invalid_amount",
                       "The amount is implausibly large; please check it.")
    return amount


def parse_currency(raw: str, side: str) -> str:
    code = raw.strip().upper()
    if not CURRENCY_RE.fullmatch(code):
        raise ApiError(400, "invalid_currency",
                       f"'{raw}' is not a three-letter currency code ({side}).")
    return code


def parse_date(raw: str) -> date:
    try:
        asked = date.fromisoformat(raw.strip())
    except ValueError:
        raise ApiError(400, "invalid_date",
                       f"'{raw}' is not a date in YYYY-MM-DD form.")
    if asked > date.today():
        raise ApiError(400, "date_in_future",
                       f"{asked} is in the future; exchange rates only exist "
                       "for published days.")
    if asked < SERIES_START:
        raise ApiError(400, "date_before_series",
                       f"The ECB reference series starts on {SERIES_START}; "
                       f"there is no data for {asked}.")
    return asked


def echo_number(value: Decimal) -> int | float:
    """Render a Decimal as a JSON number, without a fake trailing .0."""
    if value == value.to_integral_value():
        return int(value)
    return float(value)


# ---------------------------------------------------------------------------
# Application


def create_app(base_url: str | None = None,
               client: httpx.AsyncClient | None = None) -> FastAPI:
    app = FastAPI(title="fx-tool", version="1.0")

    if client is None:
        client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS)
    upstream = Upstream(base_url or os.environ.get("FX_UPSTREAM_BASE",
                                                   DEFAULT_UPSTREAM_BASE), client)
    # (from, to, "YYYY-MM-DD" | "latest") -> (rate, rate_date, expires_at | None)
    cache: OrderedDict[tuple[str, str, str], tuple[Decimal, date, float | None]] = OrderedDict()

    @app.exception_handler(ApiError)
    async def on_api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status,
                            content={"error": exc.code, "message": exc.message})

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        missing = [str(err["loc"][-1]) for err in exc.errors()]
        return JSONResponse(status_code=400,
                            content={"error": "missing_parameter",
                                     "message": "Required query parameters are missing "
                                                f"or unreadable: {', '.join(missing)}."})

    async def fetch_cached(base: str, target: str, key: str) -> tuple[Decimal, date]:
        cached = cache.get((base, target, key))
        if cached is not None:
            rate, rate_date, expires_at = cached
            if expires_at is None or expires_at > time.monotonic():
                return rate, rate_date
            del cache[(base, target, key)]

        known = await upstream.known_currencies()
        if known is not None:
            for code in (base, target):
                if code not in known:
                    raise ApiError(404, "unknown_currency",
                                   f"'{code}' is not a currency the ECB publishes "
                                   "rates for.")

        rate, rate_date = await upstream.rate(base, target, key)

        expires_at = None
        if key == "latest":
            expires_at = time.monotonic() + LATEST_TTL_SECONDS
        cache[(base, target, key)] = (rate, rate_date, expires_at)
        while len(cache) > MAX_CACHE_ENTRIES:
            cache.popitem(last=False)
        return rate, rate_date

    @app.get("/tools/convert")
    async def convert(request: Request,
                      amount: str = Query(...),
                      from_code: str = Query(..., alias="from"),
                      to_code: str = Query(..., alias="to"),
                      asked_raw: str | None = Query(None, alias="date")) -> JSONResponse:
        amount_dec = parse_amount(amount)
        base = parse_currency(from_code, "from")
        target = parse_currency(to_code, "to")
        if base == target:
            raise ApiError(400, "same_currency",
                           f"'from' and 'to' are both {base}; there is nothing "
                           "to convert.")

        asked = parse_date(asked_raw) if asked_raw is not None else None
        key = str(asked) if asked is not None else "latest"

        rate, rate_date = await fetch_cached(base, target, key)

        # Never present a rate as belonging to a date it does not belong to.
        if asked is not None:
            if rate_date > asked:
                raise ApiError(502, "upstream_invalid_response",
                               "The rate provider answered with a rate from a "
                               "later date than was asked for.")
            if (asked - rate_date).days > MAX_FALLBACK_DAYS:
                raise ApiError(404, "no_rate_for_date",
                               f"No ECB rate exists for {asked}, and the nearest "
                               f"published rate ({rate_date}) is more than "
                               f"{MAX_FALLBACK_DAYS} days older.")

        asked_date = asked if asked is not None else date.today()
        note = None
        if rate_date != asked_date:
            note = (f"No ECB rate was published for {asked_date}; the rate is "
                    f"from {rate_date}, the most recent published day.")

        result = (amount_dec * rate).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return JSONResponse({
            "amount": echo_number(amount_dec),
            "from": base,
            "to": target,
            "rate": float(rate),
            "result": float(result),
            "rate_date": str(rate_date),
            "asked_date": str(asked_date),
            "note": note,
            "source": SOURCE,
        })

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    return app


app = create_app()
