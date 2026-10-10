"""SEC EDGAR: ticker to CIK, and recent filings of flagged forms (A§4.2, RISK.md §10).

Fair access (DECISIONS D-028):
- a declared User-Agent;
- at most ``edgar.max_rps`` requests per second across www.sec.gov and data.sec.gov;
- a 10-minute stop after any 403, which SEC returns both for rate blocks and for a missing or
  bad User-Agent.

Acceptance times come from each filing's ``.hdr.sgml`` header (Eastern time), because the
submissions JSON ``acceptanceDateTime`` is off by the UTC offset for many filings.
"""

import asyncio
import json
import re
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Self

import httpx
import structlog

from trader.config import AppConfig
from trader.core.clock import Clock, et_datetime, fmt_iso, parse_et_iso
from trader.core.models import Filing
from trader.data.ratelimit import RateLimiter
from trader.memory.journal import Journal

log = structlog.get_logger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{folder}/"
BLOCK = timedelta(minutes=10)
_ACCEPTANCE = re.compile(rb"<ACCEPTANCE-DATETIME>(\d{14})")
_UPSERT = (
    "INSERT INTO filings (accession, cik, symbol, form, filed_at, items, url, fetched_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (accession) DO UPDATE SET "
    "filed_at = COALESCE(excluded.filed_at, filings.filed_at), fetched_at = excluded.fetched_at"
)


class EdgarError(Exception):
    """An EDGAR request failed; the caller marks the candidate's filings unavailable."""


class EdgarBlockedError(EdgarError):
    """EDGAR answered 403/429; every call is refused locally until the pause ends."""


class EdgarClient:
    def __init__(
        self,
        *,
        user_agent: str,
        clock: Clock,
        cache_dir: Path,
        limiter: RateLimiter,
        flag_forms: Iterable[str],
        journal: Journal | None = None,
        client: httpx.AsyncClient | None = None,
        retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout_s: float = 15.0,
    ) -> None:
        self._headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._clock, self._cache_dir, self._limiter = clock, cache_dir, limiter
        self._flag_forms = frozenset(flag_forms)
        self._journal = journal
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None
        self._retries, self._sleep = retries, sleep
        self._blocked_until: datetime | None = None
        self._tickers: dict[str, list[str]] | None = None
        self._tickers_day: date | None = None

    @classmethod
    def from_config(cls, cfg: AppConfig, clock: Clock, journal: Journal | None = None) -> Self:
        edgar = cfg.settings.edgar
        agent = cfg.secrets.secret(edgar.user_agent_env)
        if agent is None or "example.com" in agent.get_secret_value().lower():
            raise EdgarError(f"set {edgar.user_agent_env} in .env to your name and email (SEC)")
        return cls(
            user_agent=agent.get_secret_value(),
            clock=clock,
            cache_dir=cfg.path(cfg.settings.storage.db_path).parent / "edgar",
            limiter=RateLimiter(edgar.max_rps * 60, capacity=1),
            flag_forms=edgar.flag_forms,
            journal=journal,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- lookups

    async def ciks(self, symbol: str) -> list[str]:
        """Zero-padded CIKs for a ticker (EDGAR writes class shares as BRK-B, not BRK.B)."""
        mapping = await self._ticker_map()
        return mapping.get(symbol.upper().replace(".", "-"), [])

    async def recent_filings(self, symbol: str, days: int) -> list[Filing]:
        """Flagged filings with a filing date in the last ``days`` days, newest first."""
        since = self._clock.now().date() - timedelta(days=days)
        found: list[Filing] = []
        for cik in await self.ciks(symbol):
            data = await self._json(SUBMISSIONS_URL.format(cik=cik))
            recent = (data.get("filings") or {}).get("recent") or {}
            columns = (
                recent.get(key) or []
                for key in ("accessionNumber", "filingDate", "form", "items", "primaryDocument")
            )
            for accession, filed, form, items, document in zip(*columns, strict=False):
                if form not in self._flag_forms or date.fromisoformat(filed) < since:
                    continue
                folder = ARCHIVE_URL.format(cik=int(cik), folder=accession.replace("-", ""))
                found.append(
                    Filing(
                        accession=accession,
                        cik=cik,
                        symbol=symbol.upper(),
                        form=form,
                        filing_date=date.fromisoformat(filed),
                        filed_at=None,
                        items=items or "",
                        url=folder + document,
                    )
                )
        known = await self._known_acceptance([f.accession for f in found])
        resolved = []
        for filing in found:
            accepted = known.get(filing.accession) or await self._acceptance(filing)
            resolved.append(filing.model_copy(update={"filed_at": accepted}))
        await self._persist(resolved)
        return sorted(resolved, key=lambda f: (f.filing_date, f.accession), reverse=True)

    # ---------------------------------------------------------------- internals

    async def _ticker_map(self) -> dict[str, list[str]]:
        today = self._clock.now().date()
        if self._tickers is not None and self._tickers_day == today:
            return self._tickers
        cache = self._cache_dir / "company_tickers.json"
        raw: dict[str, Any] | None = None
        if cache.is_file():
            stored = json.loads(cache.read_text(encoding="utf-8"))
            if stored.get("fetched") == today.isoformat():
                raw = stored["tickers"]
        if raw is None:
            raw = await self._json(TICKERS_URL)
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            payload = {"fetched": today.isoformat(), "tickers": raw}
            cache.write_text(json.dumps(payload), encoding="utf-8")
        mapping: dict[str, list[str]] = {}
        for row in raw.values():
            ciks = mapping.setdefault(str(row["ticker"]).upper(), [])
            cik = f"{int(row['cik_str']):010d}"
            if cik not in ciks:
                ciks.append(cik)
        shared = sorted(t for t, ciks in mapping.items() if len(ciks) > 1)
        if shared:
            log.warning("tickers mapped to several CIKs", tickers=shared[:20], count=len(shared))
        self._tickers, self._tickers_day = mapping, today
        return mapping

    async def _acceptance(self, filing: Filing) -> datetime | None:
        """EDGAR's acceptance time from the filing header, in ET; None if unreadable."""
        folder = ARCHIVE_URL.format(cik=int(filing.cik), folder=filing.accession.replace("-", ""))
        try:
            body = (await self._get(f"{folder}{filing.accession}.hdr.sgml")).content
        except EdgarBlockedError:
            raise
        except EdgarError as exc:
            log.warning("filing header unavailable", accession=filing.accession, error=str(exc))
            return None
        match = _ACCEPTANCE.search(body)
        if match is None:
            return None
        s = match.group(1).decode()
        day = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        try:
            return et_datetime(day, time(int(s[8:10]), int(s[10:12]), int(s[12:14])))
        except ValueError:  # a wall time inside a DST switch; EDGAR doesn't accept filings then
            return None

    async def _known_acceptance(self, accessions: list[str]) -> dict[str, datetime]:
        if self._journal is None or not accessions:
            return {}
        marks = ",".join("?" * len(accessions))
        rows = await self._journal.fetch_all(
            f"SELECT accession, filed_at FROM filings WHERE accession IN ({marks})", accessions
        )
        return {row["accession"]: parse_et_iso(row["filed_at"]) for row in rows if row["filed_at"]}

    async def _persist(self, filings: list[Filing]) -> None:
        if self._journal is None or not filings:
            return
        fetched = fmt_iso(self._clock.now())
        rows = [
            (
                f.accession,
                f.cik,
                f.symbol,
                f.form,
                fmt_iso(f.filed_at) if f.filed_at else None,
                f.items,
                f.url,
                fetched,
            )
            for f in filings
        ]

        def write(conn: sqlite3.Connection) -> None:
            conn.executemany(_UPSERT, rows)

        await self._journal.write(write)

    async def _json(self, url: str) -> Any:
        try:
            return (await self._get(url)).json()
        except ValueError:
            raise EdgarError(f"{url}: response is not JSON") from None

    async def _get(self, url: str) -> httpx.Response:
        if self._blocked_until is not None and self._clock.now() < self._blocked_until:
            raise EdgarBlockedError(f"EDGAR calls paused until {self._blocked_until:%H:%M} (403)")
        attempt = 0
        while True:
            await self._limiter.acquire()
            try:
                response = await self._client.get(url, headers=self._headers)
            except httpx.TransportError as exc:
                if attempt >= self._retries:
                    raise EdgarError(f"{url}: {type(exc).__name__}") from None
                attempt += 1
                await self._sleep(2.0**attempt)
                continue
            status = response.status_code
            if status == 200:
                return response
            if status in (403, 429):
                self._blocked_until = self._clock.now() + BLOCK
                log.warning("EDGAR refused a request; pausing EDGAR for 10 minutes", status=status)
                raise EdgarBlockedError(
                    f"EDGAR answered {status} (rate limit or User-Agent); paused for 10 minutes"
                )
            if status >= 500 and attempt < self._retries:
                attempt += 1
                await self._sleep(2.0**attempt)
                continue
            raise EdgarError(f"{url}: HTTP {status}")
