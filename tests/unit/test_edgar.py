from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from tests.helpers import FAKE_SECRETS, write_env
from trader.config import load_config
from trader.core.clock import ET, SimClock
from trader.data.edgar import (
    ARCHIVE_URL,
    SUBMISSIONS_URL,
    TICKERS_URL,
    EdgarBlockedError,
    EdgarClient,
    EdgarError,
)
from trader.data.ratelimit import RateLimiter
from trader.memory.journal import Journal

NOW = datetime(2026, 10, 9, 8, 0, tzinfo=ET)
AGENT = "Testy McTestface testy@mail.invalid"
FORMS = ("S-1", "S-3", "424B5", "8-K", "6-K", "10-Q", "10-K")
TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1067983, "ticker": "BRK-B", "title": "Berkshire Hathaway"},
}
RECENT = {
    "accessionNumber": [
        "0000320193-26-000020",
        "0001140361-26-038672",
        "0000320193-26-000010",
        "0000320193-25-000099",
    ],
    "filingDate": ["2026-10-08", "2026-10-06", "2026-10-07", "2026-09-01"],
    "form": ["8-K", "4", "424B5", "10-K"],  # Form 4 isn't flagged; the 10-K is too old
    "items": ["2.02,9.01", "", "", ""],
    "primaryDocument": ["aapl-8k.htm", "xslF345X06/form4.xml", "aapl-424b5.htm", "aapl-10k.htm"],
}
SUBMISSIONS = {"cik": "0000320193", "tickers": ["AAPL"], "filings": {"recent": RECENT}}
AAPL = SUBMISSIONS_URL.format(cik="0000320193")


def header_url(accession: str) -> str:
    folder = ARCHIVE_URL.format(cik=320193, folder=accession.replace("-", ""))
    return f"{folder}{accession}.hdr.sgml"


def header(stamp: str) -> httpx.Response:
    return httpx.Response(200, content=f"<SEC-HEADER>\n<ACCEPTANCE-DATETIME>{stamp}\n".encode())


def make_client(tmp_path: Path, clock: SimClock, journal: Journal | None = None) -> EdgarClient:
    return EdgarClient(
        user_agent=AGENT,
        clock=clock,
        cache_dir=tmp_path / "edgar",
        limiter=RateLimiter(1_000_000, capacity=1_000),
        flag_forms=FORMS,
        journal=journal,
    )


@pytest.fixture
async def journal(tmp_path: Path) -> AsyncIterator[Journal]:
    async with await Journal.open(tmp_path / "trader.db") as opened:
        yield opened


@pytest.fixture
def sec(respx_mock: respx.MockRouter) -> respx.MockRouter:
    respx_mock.get(TICKERS_URL).mock(return_value=httpx.Response(200, json=TICKERS))
    respx_mock.get(AAPL).mock(return_value=httpx.Response(200, json=SUBMISSIONS))
    respx_mock.get(header_url("0000320193-26-000020")).mock(return_value=header("20261008163105"))
    respx_mock.get(header_url("0000320193-26-000010")).mock(return_value=header("20261007060102"))
    return respx_mock


async def test_recent_filings_keep_flagged_forms_with_header_times(
    tmp_path: Path, journal: Journal, sec: respx.MockRouter
) -> None:
    async with make_client(tmp_path, SimClock(NOW), journal) as client:
        found = await client.recent_filings("aapl", days=5)
    assert [(f.form, f.items) for f in found] == [("8-K", "2.02,9.01"), ("424B5", "")]
    eight_k = found[0]
    assert eight_k.filed_at == datetime(2026, 10, 8, 16, 31, 5, tzinfo=ET)
    assert eight_k.url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000020/aapl-8k.htm"
    )
    stored = await journal.fetch_all("SELECT accession, form, filed_at FROM filings")
    assert {(r["accession"], r["filed_at"]) for r in stored} == {
        ("0000320193-26-000020", "2026-10-08T16:31:05-04:00"),
        ("0000320193-26-000010", "2026-10-07T06:01:02-04:00"),
    }


async def test_requests_declare_the_user_agent(tmp_path: Path, sec: respx.MockRouter) -> None:
    async with make_client(tmp_path, SimClock(NOW)) as client:
        await client.ciks("AAPL")
    request = sec.calls.last.request
    assert request.headers["User-Agent"] == AGENT
    assert request.headers["Accept-Encoding"] == "gzip, deflate"


async def test_repeat_lookups_reuse_the_ticker_map_and_known_headers(
    tmp_path: Path, journal: Journal, sec: respx.MockRouter
) -> None:
    async with make_client(tmp_path, SimClock(NOW), journal) as client:
        await client.recent_filings("AAPL", days=5)
        await client.recent_filings("AAPL", days=5)
    assert sec.routes[0].call_count == 1  # company_tickers.json
    assert sec.routes[1].call_count == 2  # submissions are always fresh
    assert sec.routes[2].call_count == 1  # each header is read once
    assert sec.routes[3].call_count == 1


async def test_ticker_map_is_cached_on_disk_for_a_day(
    tmp_path: Path, sec: respx.MockRouter
) -> None:
    clock = SimClock(NOW)
    async with make_client(tmp_path, clock) as client:
        await client.ciks("AAPL")
    async with make_client(tmp_path, clock) as client:  # a new process, same day
        assert await client.ciks("AAPL") == ["0000320193"]
    assert sec.routes[0].call_count == 1
    await clock.advance(timedelta(days=1))
    async with make_client(tmp_path, clock) as client:
        await client.ciks("AAPL")
    assert sec.routes[0].call_count == 2


async def test_class_shares_use_edgars_hyphen(tmp_path: Path, sec: respx.MockRouter) -> None:
    async with make_client(tmp_path, SimClock(NOW)) as client:
        assert await client.ciks("BRK.B") == ["0001067983"]
        assert await client.recent_filings("NOPE", days=5) == []


async def test_a_403_pauses_every_call_for_ten_minutes(
    tmp_path: Path, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.get(TICKERS_URL).mock(return_value=httpx.Response(403, text="blocked"))
    clock = SimClock(NOW)
    async with make_client(tmp_path, clock) as client:
        with pytest.raises(EdgarBlockedError):
            await client.ciks("AAPL")
        with pytest.raises(EdgarBlockedError, match="paused"):
            await client.ciks("AAPL")
        assert route.call_count == 1  # the second call never left the machine
        await clock.advance(timedelta(minutes=11))
        route.mock(return_value=httpx.Response(200, json=TICKERS))
        assert await client.ciks("AAPL") == ["0000320193"]


async def test_server_errors_are_retried(tmp_path: Path, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(TICKERS_URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=TICKERS)]
    )
    client = EdgarClient(
        user_agent=AGENT,
        clock=SimClock(NOW),
        cache_dir=tmp_path / "edgar",
        limiter=RateLimiter(1_000_000, capacity=1_000),
        flag_forms=FORMS,
        sleep=_no_sleep,
    )
    async with client:
        assert await client.ciks("AAPL") == ["0000320193"]
    assert route.call_count == 2


async def _no_sleep(_seconds: float) -> None:
    return None


def test_from_config_refuses_the_placeholder_user_agent(repo: Path) -> None:
    write_env(repo, {**FAKE_SECRETS, "SEC_EDGAR_USER_AGENT": "Your Name your-email@example.com"})
    with pytest.raises(EdgarError, match="SEC_EDGAR_USER_AGENT"):
        EdgarClient.from_config(load_config(repo), SimClock(NOW))
