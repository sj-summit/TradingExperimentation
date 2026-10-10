"""`trader doctor`: environment, configuration, account, clock and CLI health checks (PLAN.md P0.4).

Output names .env keys, never their values, and every detail line passes through secret
redaction. Each check is isolated: one that crashes reports FAIL and the others still run.
"""

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

import pydantic
import requests
import yaml
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.models import Clock as AlpacaClock
from alpaca.trading.models import TradeAccount
from alpaca.trading.requests import GetOrdersRequest
from pydantic import BaseModel, ConfigDict
from requests.adapters import HTTPAdapter

from trader.config import AppConfig, BookName, ConfigError, load_config
from trader.core.clock import Clock, RealClock, ensure_et
from trader.core.events import EVENTS_FILE, load_events
from trader.core.models import Bar, Mover
from trader.core.redact import redact_text
from trader.data.alpaca_rest import (
    Adjustment,
    AlpacaAuthError,
    AlpacaError,
    AlpacaForbiddenError,
    AlpacaRest,
    Feed,
    Timeframe,
)
from trader.data.ratelimit import RateLimiter
from trader.llm.claude_cli import build_claude_env
from trader.memory.journal import open_database

Status = Literal["PASS", "WARN", "FAIL", "SKIP"]
Result = tuple[Status, str]

EXPECTED_START_EQUITY = Decimal("1000")  # each paper account starts at $1,000 (PLAN.md H0.1)
MAX_CLOCK_SKEW_S = 2.0  # RISK.md §9
TOKEN_LIFETIME_DAYS = 365  # `claude setup-token` issues a one-year token
TOKEN_WARN_DAYS = 30  # RISK.md §9
EVENTS_HORIZON_DAYS = 14  # ARCHITECTURE.md §9
MIN_FREE_DISK_BYTES = 2 * 1024**3
SUBPROCESS_TIMEOUT_S = 30.0
ALPACA_TIMEOUT_S = 10.0
SIP_LOOKBACK_DAYS = 14
EDGAR_PLACEHOLDER = "Your Name your-email@example.com"  # the config/env.example value


class Check(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    status: Status
    detail: str


# ---------------------------------------------------------------- injected dependencies


class AccountInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    equity: Decimal | None
    trading_blocked: bool
    account_blocked: bool


class ProbeError(Exception):
    """A broker probe failed. ``kind`` separates bad credentials from other failures."""

    def __init__(self, kind: Literal["auth", "http", "network", "data"], detail: str) -> None:
        super().__init__(detail)
        self.kind = kind


class AccountProbe(Protocol):
    def account(self) -> AccountInfo: ...
    def position_count(self) -> int: ...
    def open_order_count(self) -> int: ...
    def server_time(self) -> datetime: ...


class RunResult(BaseModel):
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def __call__(
        self, args: Sequence[str], *, env: Mapping[str, str], cwd: Path, timeout_s: float
    ) -> RunResult: ...


class MarketDataProbe(Protocol):
    """The slice of AlpacaRest that doctor uses."""

    async def bars(
        self,
        symbols: Sequence[str],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        *,
        feed: Feed,
        adjustment: Adjustment = "raw",
    ) -> dict[str, list[Bar]]: ...
    async def movers(self, top: int = 50) -> list[Mover] | None: ...
    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class DoctorDeps:
    clock: Clock
    alpaca: Callable[[str, str], AccountProbe]  # (key_id, secret_key) -> probe
    market_data: Callable[[str, str], MarketDataProbe]  # (key_id, secret_key) -> probe
    run: Runner
    which: Callable[[str], str | None]
    free_bytes: Callable[[Path], int]
    environ: Mapping[str, str]


# ---------------------------------------------------------------- report


def run_doctor(root: Path, deps: DoctorDeps, *, quick: bool = False) -> list[Check]:
    """Run every check. ``quick`` skips the ones that start subprocesses (uv, claude)."""
    checks = [_guarded("python", _python, {})]
    checks.append(_skip("uv", "--quick") if quick else _guarded("uv", lambda: _uv(root, deps), {}))
    try:
        cfg = load_config(root)
    except ConfigError as exc:
        checks.append(Check(name="config", status="FAIL", detail="\n".join(exc.problems)))
        checks.append(_skip("remaining", "fix the configuration first"))
        return checks
    checks.append(Check(name="config", status="PASS", detail="settings, risk and .env are valid"))
    secrets = cfg.secrets.redaction_map()

    def add(name: str, check: Callable[[], Result]) -> None:
        checks.append(_guarded(name, check, secrets))

    add("mode", lambda: _mode(cfg))
    add("env.keys", lambda: _env_keys(cfg))
    add("env.hygiene", lambda: _env_hygiene(cfg))
    probes: dict[BookName, AccountProbe] = {}
    add("alpaca.A", lambda: _alpaca_book(cfg, deps, "A", probes))
    add("alpaca.B", lambda: _alpaca_book(cfg, deps, "B", probes))
    if probes:
        probe = next(iter(probes.values()))
        add("alpaca.clock", lambda: _clock_skew(deps, probe))
    else:
        checks.append(_skip("alpaca.clock", "no Alpaca account reachable"))
    try:
        sip, movers = _market_data(cfg, deps)
    except Exception as exc:  # one broken check must not hide the others
        sip = movers = ("FAIL", f"{type(exc).__name__}: {exc}")
    add("data.sip_history", lambda: sip)
    add("data.movers", lambda: movers)
    add("runtime.dirs", lambda: _runtime_dirs(cfg))
    add("journal", lambda: _journal(cfg))
    add("disk", lambda: _disk(root, deps))
    if not cfg.settings.llm.enabled:
        checks.append(_skip("claude", "llm.enabled is false"))
    elif quick:
        checks.append(_skip("claude", "--quick"))
    else:
        exe = deps.which(cfg.settings.llm.claude_bin)
        add("claude.version", lambda: _claude_version(cfg, deps, exe))
        if exe is None:
            checks.append(_skip("claude.auth", "claude binary not found"))
        else:
            add("claude.auth", lambda: _claude_auth(cfg, deps, exe))
    add("claude.token", lambda: _token_age(cfg, deps))
    add("events", lambda: _events(root, deps))
    return checks


def format_report(checks: Sequence[Check]) -> list[str]:
    width = max(len(c.name) for c in checks)
    lines: list[str] = []
    for check in checks:
        first, *rest = check.detail.splitlines() or [""]
        lines.append(f"{check.status:<4}  {check.name:<{width}}  {first}")
        lines.extend(f"{'':<4}  {'':<{width}}  {more}" for more in rest)
    fails = sum(c.status == "FAIL" for c in checks)
    warns = sum(c.status == "WARN" for c in checks)
    lines.append(f"\n{fails} FAIL, {warns} WARN")
    return lines


def _guarded(name: str, check: Callable[[], Result], secrets: Mapping[str, str]) -> Check:
    try:
        status, detail = check()
    except Exception as exc:  # one broken check must not hide the others
        status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
    return Check(name=name, status=status, detail=redact_text(detail, secrets))


def _skip(name: str, why: str) -> Check:
    return Check(name=name, status="SKIP", detail=why)


# ---------------------------------------------------------------- checks


def _python() -> Result:
    version = platform.python_version()
    major, minor, _ = platform.python_version_tuple()  # not sys.version_info: mypy folds that
    if (major, minor) == ("3", "12"):
        return "PASS", version
    return "FAIL", f"{version}; the project targets Python 3.12 (run through `uv run`)"


def _uv(root: Path, deps: DoctorDeps) -> Result:
    uv = deps.environ.get("UV") or deps.which("uv")
    if not uv:
        return "WARN", "uv not found on PATH (https://docs.astral.sh/uv/)"
    result = deps.run([uv, "--version"], env=deps.environ, cwd=root, timeout_s=SUBPROCESS_TIMEOUT_S)
    if result.returncode != 0:
        return "WARN", f"`uv --version` exited {result.returncode}"
    return "PASS", result.stdout.strip()


def _mode(cfg: AppConfig) -> Result:
    if cfg.settings.mode == "paper":
        return "PASS", "paper"
    return "FAIL", "mode is live, which needs the Phase 7 live gate (RISK.md section 7)"


def _required_env_names(cfg: AppConfig) -> list[str]:
    s = cfg.settings
    names: list[str] = []
    for book in ("A", "B"):
        cfg_book = s.books.get(book)
        if cfg_book.enabled:
            prefix = cfg_book.account_env_prefix
            names += [f"{prefix}_KEY_ID", f"{prefix}_SECRET_KEY"]
    if s.llm.enabled:
        if s.llm.provider == "claude_cli":
            names += ["CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_TOKEN_CREATED"]
        else:
            names.append("ANTHROPIC_API_KEY")
    names.append(s.edgar.user_agent_env)
    if s.notify.provider == "ntfy":
        names.append(s.notify.ntfy_topic_env)
    elif s.notify.provider == "discord":
        names.append(s.notify.discord_webhook_env)
    return names


def _env_keys(cfg: AppConfig) -> Result:
    secrets, s = cfg.secrets, cfg.settings
    required = _required_env_names(cfg)
    problems: list[str] = []
    missing = [name for name in required if not secrets.is_set(name)]
    if missing:
        problems.append(f"missing: {', '.join(missing)}")
    agent = secrets.secret(s.edgar.user_agent_env)
    if agent is not None:
        value = agent.get_secret_value()
        if value.strip() == EDGAR_PLACEHOLDER or "example.com" in value.lower() or "@" not in value:
            problems.append(
                f"{s.edgar.user_agent_env} must hold your real name and email (SEC fair access)"
            )
    prefix = s.data.credentials_env_prefix
    pair = [secrets.is_set(f"{prefix}_KEY_ID"), secrets.is_set(f"{prefix}_SECRET_KEY")]
    if any(pair) and not all(pair):
        problems.append(f"set both {prefix}_KEY_ID and {prefix}_SECRET_KEY, or neither")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", f"all {len(required)} required keys are set"


def _env_hygiene(cfg: AppConfig) -> Result:
    warnings: list[str] = []
    if (cfg.root / "config" / ".env").exists():
        warnings.append("config/.env exists but only the repo-root .env is read: move it there")
    secrets = cfg.secrets
    if cfg.settings.llm.provider == "claude_cli" and secrets.is_set("ANTHROPIC_API_KEY"):
        warnings.append("ANTHROPIC_API_KEY is set; claude subprocesses never see it (D-002)")
    if cfg.settings.mode == "paper":
        live = [
            name
            for name in ("ALPACA_LIVE_KEY_ID", "ALPACA_LIVE_SECRET_KEY", "TRADER_LIVE_CONFIRM")
            if secrets.is_set(name)
        ]
        if live:
            warnings.append(f"live-trading values set before the Phase 7 gate: {', '.join(live)}")
        if (cfg.root / "config" / "LIVE_APPROVED").exists():
            warnings.append("config/LIVE_APPROVED exists while mode is paper")
    if warnings:
        return "WARN", "; ".join(warnings)
    return "PASS", "no stray credentials or misplaced files"


def _alpaca_book(
    cfg: AppConfig, deps: DoctorDeps, book: BookName, probes: dict[BookName, AccountProbe]
) -> Result:
    cfg_book = cfg.settings.books.get(book)
    if not cfg_book.enabled:
        return "SKIP", "book disabled"
    prefix = cfg_book.account_env_prefix
    key, secret = cfg.alpaca_credentials(book)
    if key is None or secret is None:
        return "SKIP", f"{prefix}_KEY_ID / {prefix}_SECRET_KEY not set"
    probe = deps.alpaca(key.get_secret_value(), secret.get_secret_value())
    try:
        acct = probe.account()
        if acct.status != "ACTIVE":
            return "FAIL", f"account status is {acct.status}, expected ACTIVE"
        if acct.trading_blocked or acct.account_blocked:
            return "FAIL", "the account or its trading is blocked"
        positions, orders = probe.position_count(), probe.open_order_count()
    except ProbeError as exc:
        if exc.kind == "auth":
            return "FAIL", f"authentication failed: are {prefix}_* this account's paper keys?"
        return "FAIL", f"{exc.kind} error: {exc}"
    probes[book] = probe
    detail = f"paper, ACTIVE, equity {_usd(acct.equity)}, {positions} positions, {orders} orders"
    band = cfg.risk.account_equity_band_pct / 100 * EXPECTED_START_EQUITY
    if acct.equity is None or abs(acct.equity - EXPECTED_START_EQUITY) > band:
        return "WARN", f"{detail}; expected about {_usd(EXPECTED_START_EQUITY)}"
    if positions or orders:
        return "WARN", f"{detail}; expected flat"
    return "PASS", detail


def _clock_skew(deps: DoctorDeps, probe: AccountProbe) -> Result:
    before = deps.clock.now()
    server = ensure_et(probe.server_time())
    after = deps.clock.now()
    skew = abs((server - (before + (after - before) / 2)).total_seconds())
    detail = (
        f"{skew:.2f} s from Alpaca's clock (round trip {(after - before).total_seconds():.2f} s)"
    )
    return ("PASS" if skew < MAX_CLOCK_SKEW_S else "FAIL"), detail


def _market_data(cfg: AppConfig, deps: DoctorDeps) -> tuple[Result, Result]:
    """Can these keys read SIP history (Q7), and does the plan include the screener (Q1)?"""
    key, secret = cfg.data_credentials()
    if key is None or secret is None:
        skipped: Result = ("SKIP", "no market-data keys (ALPACA_DATA_* or Book B's)")
        return skipped, skipped
    probe = deps.market_data(key.get_secret_value(), secret.get_secret_value())
    return asyncio.run(_probe_market_data(probe, deps.clock))


async def _probe_market_data(probe: MarketDataProbe, clock: Clock) -> tuple[Result, Result]:
    try:
        return await _sip_history(probe, clock), await _screener(probe)
    finally:
        await probe.aclose()


async def _sip_history(probe: MarketDataProbe, clock: Clock) -> Result:
    start = clock.now() - timedelta(days=SIP_LOOKBACK_DAYS)
    try:
        bars = (await probe.bars(["SPY"], "1Day", start, feed="sip")).get("SPY", [])
    except AlpacaAuthError:
        return "FAIL", "the market-data keys were rejected (HTTP 401)"
    except AlpacaForbiddenError as exc:
        return "FAIL", f"this account can't read SIP history ({exc.message}): see DECISIONS Q7"
    except AlpacaError as exc:
        return "FAIL", str(exc)
    if not bars:
        return "WARN", f"no SPY daily SIP bars in the last {SIP_LOOKBACK_DAYS} days"
    return "PASS", f"SPY: {len(bars)} daily SIP bars, latest {bars[-1].ts.date()}"


async def _screener(probe: MarketDataProbe) -> Result:
    try:
        movers = await probe.movers(top=10)
    except AlpacaError as exc:
        return "WARN", f"screener check failed: {exc}"
    if movers is None:
        return "WARN", "no screener on this plan (Q1); the 09:00 scan uses snapshots anyway (D-024)"
    return "PASS", f"screener available ({len(movers)} gainers)"


def _runtime_dirs(cfg: AppConfig) -> Result:
    s = cfg.settings
    dirs = [
        s.storage.logs_dir,
        s.storage.recordings_dir,
        s.storage.reports_dir,
        s.storage.llm_artifacts_dir,
        s.storage.db_path.parent,
        s.llm.config_dir,
        s.llm.sandbox_dir,
    ]
    problems: list[str] = []
    for rel in dict.fromkeys(dirs):
        path = cfg.path(rel)
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe_file = path / ".doctor-write-probe"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink()
        except OSError as exc:
            problems.append(
                f"{rel.as_posix()} is not writable ({exc.strerror or type(exc).__name__})"
            )
    sandbox = cfg.path(s.llm.sandbox_dir)
    leftovers = sorted(p.name for p in sandbox.iterdir()) if sandbox.is_dir() else []
    if leftovers:
        where = s.llm.sandbox_dir.as_posix()
        problems.append(
            f"{where} must be empty (ARCHITECTURE.md 7.1) but holds: {', '.join(leftovers)}"
        )
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", f"{len(dict.fromkeys(dirs))} dirs writable; llm sandbox empty"


def _journal(cfg: AppConfig) -> Result:
    db_path = cfg.settings.storage.db_path
    conn = open_database(cfg.path(db_path))  # applies any pending migrations
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    return "PASS", f"{db_path.as_posix()}: schema v{version}, journal_mode {mode}"


def _disk(root: Path, deps: DoctorDeps) -> Result:
    free = deps.free_bytes(root)
    detail = f"{free / 1024**3:.1f} GB free"
    return ("PASS", detail) if free >= MIN_FREE_DISK_BYTES else ("WARN", f"only {detail}")


def _claude_env(cfg: AppConfig, deps: DoctorDeps) -> dict[str, str]:
    llm = cfg.settings.llm
    token = cfg.secrets.secret("CLAUDE_CODE_OAUTH_TOKEN")
    key = cfg.secrets.secret("ANTHROPIC_API_KEY")
    return build_claude_env(
        parent=deps.environ,
        config_dir=cfg.path(llm.config_dir),
        oauth_token=token.get_secret_value() if token and llm.provider == "claude_cli" else None,
        api_key=key.get_secret_value() if key and llm.provider == "anthropic_api" else None,
    )


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def _claude_version(cfg: AppConfig, deps: DoctorDeps, exe: str | None) -> Result:
    llm = cfg.settings.llm
    if exe is None:
        return "FAIL", (
            f"`{llm.claude_bin}` not found: add its folder (e.g. %USERPROFILE%\\.local\\bin) "
            "to PATH, or set llm.claude_bin to the full path"
        )
    result = deps.run(
        [exe, "--version"],
        env=_claude_env(cfg, deps),
        cwd=cfg.path(llm.sandbox_dir),
        timeout_s=SUBPROCESS_TIMEOUT_S,
    )
    found = parse_version(result.stdout)
    if result.returncode != 0 or found is None:
        return "FAIL", f"`claude --version` failed (exit {result.returncode})"
    minimum = parse_version(llm.min_cli_version)
    shown = ".".join(map(str, found))
    if minimum is not None and found < minimum:
        return "FAIL", f"{shown} is older than llm.min_cli_version {llm.min_cli_version}"
    return "PASS", f"{shown} ({exe})"


def _claude_auth(cfg: AppConfig, deps: DoctorDeps, exe: str) -> Result:
    llm = cfg.settings.llm
    expected = "oauth_token" if llm.provider == "claude_cli" else "api_key"
    config_dir = cfg.path(llm.config_dir)
    result = deps.run(
        [exe, "auth", "status"],
        env=_claude_env(cfg, deps),
        cwd=cfg.path(llm.sandbox_dir),
        timeout_s=SUBPROCESS_TIMEOUT_S,
    )
    try:
        payload: Any = json.loads(result.stdout)
    except json.JSONDecodeError:
        return "FAIL", f"`claude auth status` printed no JSON (exit {result.returncode})"
    if not isinstance(payload, dict):
        return "FAIL", "`claude auth status` printed unexpected JSON"
    method = payload.get("authMethod")
    if method != expected:
        hint = "set CLAUDE_CODE_OAUTH_TOKEN (PLAN.md H0.2)" if expected == "oauth_token" else ""
        return "FAIL", f"authMethod is {method!r}, expected {expected!r}. {hint}".strip()
    reported = payload.get("configDirectory")
    if isinstance(reported, str) and Path(reported).resolve() != config_dir.resolve():
        return "FAIL", f"claude used config dir {reported}, expected {config_dir}: not isolated"
    return "PASS", f"authMethod {method}, isolated config dir {llm.config_dir.as_posix()}"


def _token_age(cfg: AppConfig, deps: DoctorDeps) -> Result:
    llm = cfg.settings.llm
    if not llm.enabled or llm.provider != "claude_cli":
        return "SKIP", "the subscription token is not in use"
    raw = cfg.secrets.claude_token_created
    if raw is None:
        return "FAIL", "CLAUDE_TOKEN_CREATED is not set (PLAN.md H0.2)"
    try:
        created = date.fromisoformat(raw)
    except ValueError:
        return "FAIL", "CLAUDE_TOKEN_CREATED must be the YYYY-MM-DD you ran `claude setup-token`"
    age = (deps.clock.now().date() - created).days
    expires = created + timedelta(days=TOKEN_LIFETIME_DAYS)
    if age < 0:
        return "FAIL", "CLAUDE_TOKEN_CREATED is in the future"
    if age >= TOKEN_LIFETIME_DAYS:
        return "FAIL", f"token expired {expires}: run `claude setup-token` and update .env"
    if age >= TOKEN_LIFETIME_DAYS - TOKEN_WARN_DAYS:
        return "WARN", f"token expires {expires}: renew with `claude setup-token`"
    return "PASS", f"{age} days old; expires {expires}"


def _events(root: Path, deps: DoctorDeps) -> Result:
    path = root / EVENTS_FILE
    if not path.is_file():
        return "FAIL", f"{EVENTS_FILE.as_posix()} not found"
    try:
        events = load_events(path)
    except yaml.YAMLError as exc:
        return "FAIL", f"{EVENTS_FILE.as_posix()}: invalid YAML: {exc}"
    except pydantic.ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
        where = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errors)
        return "FAIL", f"{EVENTS_FILE.as_posix()}: {where}"
    now = deps.clock.now()
    upcoming = events.between(now, now + timedelta(days=EVENTS_HORIZON_DAYS))
    if not upcoming:
        return "WARN", f"no events in the next {EVENTS_HORIZON_DAYS} days: extend events.yaml"
    unverified = sum(not e.verified_by_human for e in upcoming)
    detail = f"{len(upcoming)} in the next {EVENTS_HORIZON_DAYS} days; next {upcoming[0].name}"
    if unverified:
        return "WARN", f"{detail}; {unverified} not verified_by_human"
    return "PASS", detail


def _usd(amount: Decimal | None) -> str:
    return "unknown" if amount is None else f"${amount:,.2f}"


# ---------------------------------------------------------------- real dependencies


class _TimeoutAdapter(HTTPAdapter):
    """alpaca-py passes no timeout to requests; without one a stalled call hangs forever."""

    def __init__(self, timeout_s: float) -> None:
        super().__init__()
        self._timeout_s = timeout_s

    def send(
        self, request: requests.PreparedRequest, *args: Any, **kwargs: Any
    ) -> requests.Response:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self._timeout_s
        return super().send(request, *args, **kwargs)


class AlpacaAccountProbe:
    """Read-only calls against one Alpaca paper account (paper-api host only, D-020).

    Paper keys only authenticate against the paper host, so a successful call proves the keys
    belong to a paper account; the API has no ``is_paper`` field.
    """

    def __init__(self, key_id: str, secret_key: str, timeout_s: float = ALPACA_TIMEOUT_S) -> None:
        self._client = TradingClient(api_key=key_id, secret_key=secret_key, paper=True)
        self._client._session.mount("https://", _TimeoutAdapter(timeout_s))

    def account(self) -> AccountInfo:
        acct = _alpaca_call(self._client.get_account)
        if not isinstance(acct, TradeAccount):
            raise ProbeError("data", "unexpected account payload")
        return AccountInfo(
            status=acct.status.value,
            equity=Decimal(acct.equity) if acct.equity is not None else None,
            trading_blocked=bool(acct.trading_blocked),
            account_blocked=bool(acct.account_blocked),
        )

    def position_count(self) -> int:
        positions = _alpaca_call(self._client.get_all_positions)
        if not isinstance(positions, list):
            raise ProbeError("data", "unexpected positions payload")
        return len(positions)

    def open_order_count(self) -> int:
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        orders = _alpaca_call(lambda: self._client.get_orders(filter=request))
        if not isinstance(orders, list):
            raise ProbeError("data", "unexpected orders payload")
        return len(orders)

    def server_time(self) -> datetime:
        clock = _alpaca_call(self._client.get_clock)
        if not isinstance(clock, AlpacaClock):
            raise ProbeError("data", "unexpected clock payload")
        return clock.timestamp


def _alpaca_call[T](call: Callable[[], T]) -> T:
    try:
        return call()
    except APIError as exc:
        code = exc.status_code
        raise ProbeError("auth" if code in (401, 403) else "http", f"HTTP {code}") from None
    except requests.RequestException as exc:
        raise ProbeError("network", type(exc).__name__) from None
    except pydantic.ValidationError:
        raise ProbeError("data", "alpaca-py could not parse the response") from None


def run_subprocess(
    args: Sequence[str], *, env: Mapping[str, str], cwd: Path, timeout_s: float
) -> RunResult:
    proc = subprocess.run(
        list(args),
        env=dict(env),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    return RunResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _alpaca_market_data(key_id: str, secret_key: str) -> MarketDataProbe:
    clock = RealClock()
    return AlpacaRest(
        key_id,
        secret_key,
        clock=clock,
        data_limiter=RateLimiter(60),
        trading_limiter=RateLimiter(60),
    )


def default_deps() -> DoctorDeps:
    return DoctorDeps(
        clock=RealClock(),
        alpaca=AlpacaAccountProbe,
        market_data=_alpaca_market_data,
        run=run_subprocess,
        which=shutil.which,
        free_bytes=lambda path: shutil.disk_usage(path).free,
        environ=dict(os.environ),
    )
