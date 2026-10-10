"""Async client for Alpaca's REST APIs: read-only trading calls (paper host) and market data.

Uses httpx directly rather than alpaca-py (DECISIONS D-023): async, explicit timeouts, one rate
limiter per host, exact Decimal prices, and respx-mockable tests. ``AlpacaHttp`` holds the
request policy and is shared with execution/alpaca_broker.py, which places orders; nothing in
this module does.
"""

import asyncio
import json
import random
import re
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, Self

import httpx
import structlog

from trader.config import AppConfig
from trader.core.clock import Clock, ensure_et, parse_et_iso
from trader.core.models import Asset, Bar, Mover, NewsItem, Quote, Snapshot, Split, Trade
from trader.data.ratelimit import RateLimiter

log = structlog.get_logger(__name__)

TRADING_PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
MAX_BARS_PER_PAGE = 10_000  # a total across all symbols in the request, not per symbol
MAX_ACTIONS_PER_PAGE = 1_000
MAX_NEWS_PER_PAGE = 50
MAX_BACKOFF_S = 30.0
MAX_RESET_WAIT_S = 60.0

Feed = Literal["iex", "sip"]
Timeframe = Literal["1Min", "5Min", "15Min", "1Hour", "1Day"]
Adjustment = Literal["raw", "split", "dividend", "all"]


class AlpacaError(Exception):
    """A request failed for good. ``status`` is the HTTP status, 0 for network errors.
    ``code`` is Alpaca's numeric error code from the body (e.g. 40310000), when it sent one."""

    def __init__(self, status: int, message: str, path: str, code: int | None = None) -> None:
        where = f"Alpaca {path}"
        super().__init__(f"{where}: HTTP {status}: {message}" if status else f"{where}: {message}")
        self.status, self.message, self.path, self.code = status, message, path, code


class AlpacaAuthError(AlpacaError):
    """401: the keys are missing or invalid."""


class AlpacaForbiddenError(AlpacaError):
    """403: authenticated, but the plan or account doesn't allow this request."""


class SipRecentError(AlpacaForbiddenError):
    """403 for SIP data newer than 15 minutes, which the free plan can't query."""


class AlpacaUncertainError(AlpacaError):
    """A POST got no definite answer (timeout, transport error, 5xx): it may have taken effect.

    Never resend it blindly; look the order up by ``client_order_id`` first (A§10).
    """


class AlpacaHttp:
    """Authenticated JSON requests to Alpaca's REST hosts, with one rate limiter per host.

    GET and DELETE are idempotent, so they retry 429, 5xx and transport errors with jittered
    backoff; a 429 waits for ``X-RateLimit-Reset`` (Alpaca sends no Retry-After) and pauses the
    whole host. A POST is retried only after a 429, which Alpaca answers before doing anything;
    any other answer that isn't definite raises AlpacaUncertainError.
    """

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        *,
        clock: Clock,
        limiters: Mapping[str, RateLimiter],
        client: httpx.AsyncClient | None = None,
        retries: int = 3,
        backoff_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._headers = {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key}
        self._clock = clock
        self._limiters = dict(limiters)
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None
        self._retries, self._backoff_s = retries, backoff_s
        self._sleep, self._rng = sleep, rng or random.Random()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get(self, base: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return await self._request("GET", base, path, params=params)

    async def delete(self, base: str, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """Returns the parsed body, or None for 204 No Content."""
        return await self._request("DELETE", base, path, params=params)

    async def post(self, base: str, path: str, body: Mapping[str, Any]) -> Any:
        return await self._request("POST", base, path, body=body)

    async def _request(
        self,
        method: str,
        base: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        limiter = self._limiters[base]
        idempotent = method != "POST"
        attempt = 0
        while True:
            await limiter.acquire()
            try:
                response = await self._client.request(
                    method, base + path, params=params, json=body, headers=self._headers
                )
            except httpx.TransportError as exc:
                if not idempotent:
                    raise AlpacaUncertainError(
                        0, f"{type(exc).__name__}: no answer, outcome unknown", path
                    ) from None
                if attempt >= self._retries:
                    raise AlpacaError(
                        0, f"{type(exc).__name__} after {attempt + 1} tries", path
                    ) from None
                attempt += 1
                await self._sleep(self._backoff(attempt))
                continue
            status = response.status_code
            if 200 <= status < 300:
                return _body(response, path)
            if status >= 500 and not idempotent:
                error = _error(response, path)
                raise AlpacaUncertainError(
                    status, f"{error.message}; outcome unknown", path, error.code
                )
            if not (status == 429 or status >= 500) or attempt >= self._retries:
                raise _error(response, path)
            attempt += 1
            wait = self._backoff(attempt)
            if status == 429:
                wait = max(wait, self._until_reset(response))
                limiter.pause_for(wait)
            log.warning(
                "alpaca retry",
                method=method,
                path=path,
                status=status,
                attempt=attempt,
                wait_s=round(wait, 2),
            )
            await self._sleep(wait)

    def _backoff(self, attempt: int) -> float:
        """Jittered exponential backoff: base * 2**(attempt-1) * U[0.5, 1.5), capped."""
        return min(
            MAX_BACKOFF_S, self._backoff_s * 2.0 ** (attempt - 1) * (0.5 + self._rng.random())
        )

    def _until_reset(self, response: httpx.Response) -> float:
        """Seconds until ``X-RateLimit-Reset`` (UNIX epoch); Alpaca sends no Retry-After."""
        try:
            reset = float(response.headers["X-RateLimit-Reset"])
        except (KeyError, ValueError):
            return 0.0
        return max(0.0, min(MAX_RESET_WAIT_S, reset - self._clock.now().timestamp()))


class AlpacaRest:
    def __init__(
        self,
        key_id: str,
        secret_key: str,
        *,
        clock: Clock,
        data_limiter: RateLimiter,
        trading_limiter: RateLimiter,
        client: httpx.AsyncClient | None = None,
        batch_size: int = 200,
        max_parallel: int = 4,
        retries: int = 3,
        backoff_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        trading_url: str = TRADING_PAPER_URL,
        data_url: str = DATA_URL,
        timeout_s: float = 10.0,
    ) -> None:
        self._http = AlpacaHttp(
            key_id,
            secret_key,
            clock=clock,
            limiters={data_url: data_limiter, trading_url: trading_limiter},
            client=client,
            retries=retries,
            backoff_s=backoff_s,
            sleep=sleep,
            rng=rng,
            timeout_s=timeout_s,
        )
        self._batch_size, self._max_parallel = batch_size, max_parallel
        self._trading_url, self._data_url = trading_url, data_url

    @classmethod
    def from_config(cls, cfg: AppConfig, clock: Clock) -> Self:
        key, secret = cfg.data_credentials()
        if key is None or secret is None:
            raise AlpacaAuthError(0, "no data keys in .env (ALPACA_DATA_* or Book B's)", "-")
        per_min = cfg.settings.data.rest_rate_limit_per_min
        return cls(
            key.get_secret_value(),
            secret.get_secret_value(),
            clock=clock,
            data_limiter=RateLimiter(per_min),
            trading_limiter=RateLimiter(per_min),
            batch_size=cfg.settings.data.rest_batch_symbols,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- trading API (read-only)

    async def calendar(self, start: date, end: date) -> list[dict[str, Any]]:
        """Raw v2 calendar rows for [start, end]; parsed by core/calendar.py."""
        rows = await self._http.get(
            self._trading_url, "/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()}
        )
        if not isinstance(rows, list):
            raise AlpacaError(200, "unexpected calendar payload", "/v2/calendar")
        return rows

    async def assets(self) -> list[Asset]:
        """Every active us_equity asset. Tradability and exchange are filtered by the caller."""
        rows = await self._http.get(
            self._trading_url, "/v2/assets", {"status": "active", "asset_class": "us_equity"}
        )
        return [_asset(row) for row in rows]

    async def asset(self, symbol: str) -> Asset | None:
        """One asset by symbol, or None if Alpaca doesn't know it."""
        try:
            row = await self._http.get(self._trading_url, f"/v2/assets/{symbol}")
        except AlpacaError as exc:
            if exc.status == 404:
                return None
            raise
        return _asset(row)

    # ---------------------------------------------------------------- market data

    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: Feed,
        adjustment: Adjustment = "raw",
    ) -> dict[str, list[Bar]]:
        """Bars per symbol, oldest first. Symbols with no bars are absent from the result.

        Leave ``end`` as None for SIP on the free plan: the server then defaults it to 15 minutes
        ago, while an explicit recent ``end`` raises SipRecentError.
        """
        params: dict[str, Any] = {
            "timeframe": timeframe,
            "start": rfc3339(start),
            "feed": feed,
            "adjustment": adjustment,
            "limit": MAX_BARS_PER_PAGE,
            "sort": "asc",
        }
        if end is not None:
            params["end"] = rfc3339(end)

        async def fetch(chunk: list[str]) -> dict[str, list[Bar]]:
            out: dict[str, list[Bar]] = defaultdict(list)
            async for page in self._pages(
                "/v2/stocks/bars", {**params, "symbols": ",".join(chunk)}
            ):
                for symbol, rows in (page.get("bars") or {}).items():
                    out[symbol].extend(_bar(row) for row in rows)
            return dict(out)

        return _merge(await self._batched(symbols, fetch))

    async def snapshots(self, symbols: Sequence[str], *, feed: Feed) -> dict[str, Snapshot]:
        """Latest trade, quote and bars per symbol; symbols without data are absent."""

        async def fetch(chunk: list[str]) -> dict[str, Snapshot]:
            data = await self._http.get(
                self._data_url, "/v2/stocks/snapshots", {"symbols": ",".join(chunk), "feed": feed}
            )
            return {symbol: _snapshot(symbol, raw) for symbol, raw in data.items() if raw}

        return _merge(await self._batched(symbols, fetch))

    async def splits(self, start: date, end: date) -> list[Split]:
        """Forward and reverse splits Alpaca lists for [start, end], all US symbols.

        Which date field the server filters on isn't documented, so callers ask for a wider
        window and filter on ``ex_date`` themselves.
        """
        params = {
            "types": "forward_split,reverse_split",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": MAX_ACTIONS_PER_PAGE,
            "sort": "asc",
        }
        out: list[Split] = []
        async for page in self._pages("/v1/corporate-actions", params):
            actions = page.get("corporate_actions") or {}
            out += [_split(row, reverse=False) for row in actions.get("forward_splits") or []]
            out += [_split(row, reverse=True) for row in actions.get("reverse_splits") or []]
        return out

    async def movers(self, top: int = 50) -> list[Mover] | None:
        """Today's top gainers, or None when the plan has no screener access (Q1).

        Until the open the screener still shows the previous session's movers (D-024).
        """
        try:
            data = await self._http.get(
                self._data_url, "/v1beta1/screener/stocks/movers", {"top": top}
            )
        except AlpacaForbiddenError:
            return None
        except AlpacaError as exc:
            if exc.status == 404:
                return None
            raise
        return [_mover(row) for row in data.get("gainers") or []]

    async def news(
        self,
        *,
        start: datetime,
        end: datetime | None = None,
        symbols: Sequence[str] | None = None,
    ) -> list[NewsItem]:
        """Articles created or updated in [start, end], oldest first, without bodies."""
        params: dict[str, Any] = {
            "start": rfc3339(start),
            "limit": MAX_NEWS_PER_PAGE,
            "sort": "asc",
            "include_content": "false",
        }
        if end is not None:
            params["end"] = rfc3339(end)
        if symbols:
            params["symbols"] = ",".join(symbols)
        out: list[NewsItem] = []
        async for page in self._pages("/v1beta1/news", params):
            out += [news_item(row) for row in page.get("news") or []]
        return out

    # ---------------------------------------------------------------- plumbing

    async def _pages(self, path: str, params: Mapping[str, Any]) -> AsyncIterator[Any]:
        """Yield each page of a data-API listing, following ``next_page_token``."""
        token: str | None = None
        while True:
            page = await self._http.get(
                self._data_url, path, {**params, "page_token": token} if token else params
            )
            yield page
            token = page.get("next_page_token")
            if not token:
                return

    async def _batched[T](
        self, symbols: Sequence[str], fetch: Callable[[list[str]], Awaitable[T]]
    ) -> list[T]:
        unique = list(dict.fromkeys(symbols))
        chunks = [unique[i : i + self._batch_size] for i in range(0, len(unique), self._batch_size)]
        gate = asyncio.Semaphore(self._max_parallel)

        async def run(chunk: list[str]) -> T:
            async with gate:
                return await fetch(chunk)

        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(run(chunk)) for chunk in chunks]
        except ExceptionGroup as failed:
            raise failed.exceptions[0] from None  # the rest were cancelled by the TaskGroup
        return [task.result() for task in tasks]


# ---------------------------------------------------------------- parsing


def rfc3339(when: datetime) -> str:
    """UTC ``...Z`` form for Alpaca query parameters."""
    return ensure_et(when).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _body(response: httpx.Response, path: str) -> Any:
    """A success body parsed with exact Decimals; None when there is none (204)."""
    if response.status_code == 204 or not response.content:
        return None
    try:
        return json.loads(response.content, parse_float=Decimal)
    except ValueError:
        raise AlpacaError(response.status_code, "response is not JSON", path) from None


def _error(response: httpx.Response, path: str) -> AlpacaError:
    message, code = _error_details(response)
    status = response.status_code
    if status == 401:
        return AlpacaAuthError(status, message, path, code)
    if status == 403:
        if "recent sip data" in message.lower():
            return SipRecentError(status, message, path, code)
        return AlpacaForbiddenError(status, message, path, code)
    return AlpacaError(status, message, path, code)


def _error_details(response: httpx.Response) -> tuple[str, int | None]:
    """The body's ``message`` and numeric ``code``; for an HTML page, its visible text."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        code = body.get("code")
        return str(body["message"])[:300], code if isinstance(code, int) else None
    text = " ".join(re.sub(r"<[^>]+>", " ", response.text).split())  # e.g. an nginx 401 page
    return text[:300] or response.reason_phrase, None


def _merge[V](parts: Sequence[dict[str, V]]) -> dict[str, V]:
    merged: dict[str, V] = {}
    for part in parts:
        merged.update(part)
    return merged


def _bar(raw: Mapping[str, Any]) -> Bar:
    return Bar(
        ts=parse_et_iso(raw["t"]),
        open=raw["o"],
        high=raw["h"],
        low=raw["l"],
        close=raw["c"],
        volume=int(raw["v"]),
        trades=raw.get("n"),
        vwap=raw.get("vw"),
    )


def _trade(symbol: str, raw: Mapping[str, Any]) -> Trade:
    return Trade(
        symbol=symbol,
        ts=parse_et_iso(raw["t"]),
        price=raw["p"],
        size=int(raw["s"]),
        conditions=tuple(raw.get("c") or ()),
    )


def _quote(symbol: str, raw: Mapping[str, Any]) -> Quote:
    return Quote(
        symbol=symbol,
        ts=parse_et_iso(raw["t"]),
        bid=raw["bp"],
        bid_size=int(raw["bs"]),
        ask=raw["ap"],
        ask_size=int(raw["as"]),
    )


def _snapshot(symbol: str, raw: Mapping[str, Any]) -> Snapshot:
    def bar(key: str) -> Bar | None:
        return _bar(raw[key]) if raw.get(key) else None

    return Snapshot(
        symbol=symbol,
        latest_trade=_trade(symbol, raw["latestTrade"]) if raw.get("latestTrade") else None,
        latest_quote=_quote(symbol, raw["latestQuote"]) if raw.get("latestQuote") else None,
        minute_bar=bar("minuteBar"),
        daily_bar=bar("dailyBar"),
        prev_daily_bar=bar("prevDailyBar"),
    )


def _asset(raw: Mapping[str, Any]) -> Asset:
    return Asset(
        symbol=raw["symbol"],
        name=raw.get("name") or "",
        exchange=raw.get("exchange") or "",
        asset_class=raw.get("class") or "",
        status=raw.get("status") or "",
        tradable=bool(raw.get("tradable")),
        attributes=tuple(raw.get("attributes") or ()),
    )


def _split(raw: Mapping[str, Any], *, reverse: bool) -> Split:
    process = raw.get("process_date")
    return Split(
        symbol=raw["symbol"],
        ex_date=date.fromisoformat(raw["ex_date"]),
        process_date=date.fromisoformat(process) if process else None,
        old_rate=Decimal(str(raw["old_rate"])),
        new_rate=Decimal(str(raw["new_rate"])),
        reverse=reverse,
    )


def news_item(raw: Mapping[str, Any]) -> NewsItem:
    """One article, from REST (``news`` list) or the news stream (``T: "n"``)."""
    return NewsItem(
        id=int(raw["id"]),
        headline=raw.get("headline") or "",
        summary=raw.get("summary") or "",
        author=raw.get("author") or "",
        source=raw.get("source") or "",
        url=raw.get("url") or None,
        symbols=tuple(raw.get("symbols") or ()),
        created_at=parse_et_iso(raw["created_at"]),
        updated_at=parse_et_iso(raw.get("updated_at") or raw["created_at"]),
    )


def _mover(raw: Mapping[str, Any]) -> Mover:
    return Mover(
        symbol=raw["symbol"],
        percent_change=float(raw["percent_change"]),
        change=raw["change"],
        price=raw["price"],
    )
