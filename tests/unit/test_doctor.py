import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.helpers import FAKE_SECRETS, set_yaml, write_env
from trader import doctor
from trader.cli import app
from trader.core.clock import ET, SimClock
from trader.core.models import Bar, Mover
from trader.data.alpaca_rest import (
    Adjustment,
    AlpacaAuthError,
    AlpacaForbiddenError,
    Feed,
    Timeframe,
)
from trader.doctor import (
    AccountInfo,
    Check,
    DoctorDeps,
    ProbeError,
    RunResult,
    format_report,
    run_doctor,
)
from trader.ops.power import SleepSettings

NOW = datetime(2026, 10, 9, 10, 0, tzinfo=ET)
KEY_A = FAKE_SECRETS["ALPACA_PAPER_A_KEY_ID"]
KEY_B = FAKE_SECRETS["ALPACA_PAPER_B_KEY_ID"]
EVENTS_NEXT_WEEK = """\
events:
  - date: 2026-10-14
    time: "08:30"
    name: CPI
    impact: high
    source: https://www.bls.gov/schedule/
    verified_by_human: true
"""


class FakeProbe:
    def __init__(
        self,
        *,
        status: str = "ACTIVE",
        equity: str | None = "1000.00",
        positions: int = 0,
        orders: int = 0,
        server_time: datetime = NOW,
        error: Exception | None = None,
        shorting: bool = False,
    ) -> None:
        self.status, self.equity, self.error = status, equity, error
        self.shorting = shorting
        self.positions, self.orders, self.time = positions, orders, server_time

    def account(self) -> AccountInfo:
        if self.error:
            raise self.error
        equity = Decimal(self.equity) if self.equity is not None else None
        return AccountInfo(
            status=self.status,
            equity=equity,
            trading_blocked=False,
            account_blocked=False,
            shorting_enabled=self.shorting,
        )

    def position_count(self) -> int:
        return self.positions

    def open_order_count(self) -> int:
        return self.orders

    def server_time(self) -> datetime:
        return self.time


class FakeRunner:
    """Answers `uv --version`, `claude --version` and `claude auth status`, recording each call.

    Like the real CLI, `auth status` reports the auth method implied by the env it was given.
    """

    def __init__(
        self, *, claude_version: str = "2.1.280 (Claude Code)", auth: dict[str, Any] | None = None
    ) -> None:
        self.claude_version, self.auth = claude_version, auth
        self.calls: list[tuple[list[str], dict[str, str], Path]] = []

    def __call__(
        self, args: Sequence[str], *, env: Mapping[str, str], cwd: Path, timeout_s: float
    ) -> RunResult:
        argv = list(args)
        self.calls.append((argv, dict(env), cwd))
        if "uv" in Path(argv[0]).name:
            return RunResult(returncode=0, stdout="uv 0.12.24\n", stderr="")
        if argv[1:] == ["--version"]:
            return RunResult(returncode=0, stdout=self.claude_version + "\n", stderr="")
        if argv[1:] == ["auth", "status"]:
            payload = self.auth or {
                "authMethod": "oauth_token" if "CLAUDE_CODE_OAUTH_TOKEN" in env else "none",
                "configDirectory": env.get("CLAUDE_CONFIG_DIR"),
            }
            code = 0 if payload.get("authMethod") != "none" else 1
            return RunResult(returncode=code, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"unexpected command {argv}")


class FakeMarketData:
    """Stands in for AlpacaRest: SPY daily SIP bars and a screener, unless told otherwise."""

    def __init__(
        self,
        *,
        error: Exception | None = None,
        sip_bars: int = 9,
        movers: list[Mover] | None = None,
        has_screener: bool = True,
    ) -> None:
        self.error, self.sip_bars, self.has_screener = error, sip_bars, has_screener
        default = Mover(symbol="XYZ", percent_change=12.5, change=Decimal("1"), price=Decimal("9"))
        self.screener = movers if movers is not None else [default]
        self.closed = False
        self.requests: list[tuple[list[str], str, str]] = []

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
        self.requests.append((list(symbols), timeframe, feed))
        if self.error:
            raise self.error
        one = Decimal(1)
        day = Bar(ts=NOW, open=one, high=one, low=one, close=one, volume=1)
        return {"SPY": [day] * self.sip_bars} if self.sip_bars else {}

    async def movers(self, top: int = 50) -> list[Mover] | None:
        return self.screener if self.has_screener else None

    async def aclose(self) -> None:
        self.closed = True


NEVER_SLEEPS = SleepSettings(standby_s=0, wake_timers=1)  # this PC's own settings


def make_deps(
    *,
    probes: Mapping[str, FakeProbe] | None = None,
    market_data: FakeMarketData | None = None,
    runner: FakeRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    free_bytes: int = 100 * 1024**3,
    environ: Mapping[str, str] | None = None,
    sleep: SleepSettings | None = NEVER_SLEEPS,
) -> DoctorDeps:
    known = dict(probes or {})
    data = market_data or FakeMarketData()
    return DoctorDeps(
        clock=SimClock(NOW),
        alpaca=lambda key_id, _secret: known.get(key_id, FakeProbe()),
        market_data=lambda _key_id, _secret: data,
        run=runner or FakeRunner(),
        which=which or (lambda name: f"C:/fake/bin/{name}.exe"),
        free_bytes=lambda _path: free_bytes,
        environ=environ if environ is not None else {"PATH": "C:/fake/bin", "SYSTEMROOT": "C:/W"},
        sleep=lambda: sleep,
    )


def write_secrets(root: Path, **overrides: str) -> None:
    write_env(root, {**FAKE_SECRETS, "CLAUDE_TOKEN_CREATED": "2026-10-01", **overrides})


def statuses(checks: list[Check]) -> dict[str, str]:
    return {c.name: c.status for c in checks}


def check(checks: list[Check], name: str) -> Check:
    return next(c for c in checks if c.name == name)


@pytest.fixture
def green(repo: Path) -> Path:
    """The `repo` fixture with a verified high-impact event next week, and ntfy for alerts."""
    (repo / "config" / "events.yaml").write_text(EVENTS_NEXT_WEEK, encoding="utf-8")
    set_yaml(repo / "config" / "settings.yaml", "notify.provider", "ntfy")
    write_secrets(repo, NTFY_TOPIC="t0p1c")
    return repo


# ---------------------------------------------------------------- the happy path


def test_everything_green(green: Path) -> None:
    checks = run_doctor(green, make_deps())
    assert statuses(checks) == {
        "python": "PASS",
        "uv": "PASS",
        "config": "PASS",
        "mode": "PASS",
        "env.keys": "PASS",
        "env.hygiene": "PASS",
        "alpaca.A": "PASS",
        "alpaca.B": "PASS",
        "alpaca.clock": "PASS",
        "data.sip_history": "PASS",
        "data.movers": "PASS",
        "runtime.dirs": "PASS",
        "journal": "PASS",
        "disk": "PASS",
        "machine.sleep": "PASS",
        "notify": "PASS",
        "claude.version": "PASS",
        "claude.auth": "PASS",
        "claude.token": "PASS",
        "events": "PASS",
    }
    for rel in ("runtime/logs", "runtime/llm_sandbox", "runtime/claude-config", "runtime/llm"):
        assert (green / rel).is_dir()
    assert (green / "runtime" / "trader.db").is_file()


def test_report_never_contains_secret_values(green: Path) -> None:
    error = RuntimeError(f"boom {FAKE_SECRETS['ALPACA_PAPER_A_SECRET_KEY']}")
    checks = run_doctor(green, make_deps(probes={KEY_A: FakeProbe(error=error)}))
    report = "\n".join(format_report(checks))
    assert "***ALPACA_PAPER_A_SECRET_KEY***" in check(checks, "alpaca.A").detail
    for value in FAKE_SECRETS.values():
        assert value not in report


def test_quick_mode_starts_no_subprocess(green: Path) -> None:
    runner = FakeRunner()
    checks = run_doctor(green, make_deps(runner=runner), quick=True)
    assert runner.calls == []
    assert check(checks, "uv").status == "SKIP"
    assert check(checks, "claude").status == "SKIP"


# ---------------------------------------------------------------- config and .env


def test_invalid_config_stops_early(repo: Path) -> None:
    (repo / "config" / "settings.yaml").unlink()
    checks = run_doctor(repo, make_deps())
    assert check(checks, "config").status == "FAIL"
    assert checks[-1].status == "SKIP"
    assert "alpaca.A" not in statuses(checks)


def test_live_mode_fails(green: Path) -> None:
    set_yaml(green / "config" / "settings.yaml", "mode", "live")
    assert check(run_doctor(green, make_deps()), "mode").status == "FAIL"


def test_missing_keys_are_named(green: Path) -> None:
    keep = {k: v for k, v in FAKE_SECRETS.items() if not k.startswith("ALPACA_PAPER_B")}
    write_env(green, {**keep, "CLAUDE_TOKEN_CREATED": "2026-10-01"})
    checks = run_doctor(green, make_deps())
    keys = check(checks, "env.keys")
    assert keys.status == "FAIL"
    assert "ALPACA_PAPER_B_KEY_ID" in keys.detail
    assert "ALPACA_PAPER_B_SECRET_KEY" in keys.detail
    assert check(checks, "alpaca.B").status == "SKIP"


def test_edgar_placeholder_fails(green: Path) -> None:
    write_secrets(green, SEC_EDGAR_USER_AGENT="Your Name your-email@example.com")
    keys = check(run_doctor(green, make_deps()), "env.keys")
    assert keys.status == "FAIL"
    assert "real name and email" in keys.detail


def test_half_set_data_keys_fail(green: Path) -> None:
    write_secrets(green, ALPACA_DATA_KEY_ID="PKDATA0000000001")
    assert "ALPACA_DATA_SECRET_KEY" in check(run_doctor(green, make_deps()), "env.keys").detail


def test_misplaced_env_and_stray_api_key_warn(green: Path) -> None:
    (green / "config" / ".env").write_text("X=1\n", encoding="utf-8")
    write_secrets(green, ANTHROPIC_API_KEY="sk-ant-api-stray-value")
    hygiene = check(run_doctor(green, make_deps()), "env.hygiene")
    assert hygiene.status == "WARN"
    assert "config/.env" in hygiene.detail
    assert "ANTHROPIC_API_KEY" in hygiene.detail


# ---------------------------------------------------------------- Alpaca


def test_bad_credentials_fail_that_book_only(green: Path) -> None:
    probes = {KEY_A: FakeProbe(error=ProbeError("auth", "HTTP 401"))}
    checks = run_doctor(green, make_deps(probes=probes))
    assert check(checks, "alpaca.A").status == "FAIL"
    assert "authentication failed" in check(checks, "alpaca.A").detail
    assert check(checks, "alpaca.B").status == "PASS"
    assert check(checks, "alpaca.clock").status == "PASS"  # measured through book B


def test_inactive_account_fails(green: Path) -> None:
    probes = {KEY_A: FakeProbe(status="ACCOUNT_UPDATED")}
    assert check(run_doctor(green, make_deps(probes=probes)), "alpaca.A").status == "FAIL"


@pytest.mark.parametrize(
    "probe",
    [
        FakeProbe(equity="1500.00"),
        FakeProbe(equity=None),
        FakeProbe(positions=1),
        FakeProbe(orders=2),
    ],
)
def test_account_not_fresh_and_flat_warns(green: Path, probe: FakeProbe) -> None:
    assert check(run_doctor(green, make_deps(probes={KEY_B: probe})), "alpaca.B").status == "WARN"


def test_an_account_that_may_short_warns_with_every_other_warning(green: Path) -> None:
    """Two flatteners' sells racing could open a short where the account allows one (D-083)."""
    probes = {KEY_A: FakeProbe(shorting=True, positions=1)}
    found = check(run_doctor(green, make_deps(probes=probes)), "alpaca.A")
    assert found.status == "WARN"
    assert found.detail.endswith(
        "; expected flat; shorting is enabled: set the account's no_shorting configuration"
    )


@pytest.mark.parametrize(("offset_s", "expected"), [(1.5, "PASS"), (-1.9, "PASS"), (2.5, "FAIL")])
def test_clock_skew(green: Path, offset_s: float, expected: str) -> None:
    probes = {KEY_A: FakeProbe(server_time=NOW + timedelta(seconds=offset_s))}
    assert check(run_doctor(green, make_deps(probes=probes)), "alpaca.clock").status == expected


# ---------------------------------------------------------------- market data (Q1, Q7)


def test_sip_history_probe_asks_for_daily_sip_bars(green: Path) -> None:
    data = FakeMarketData()
    run_doctor(green, make_deps(market_data=data))
    assert data.requests == [(["SPY"], "1Day", "sip")]
    assert data.closed


def test_account_without_sip_history_fails_and_points_at_q7(green: Path) -> None:
    denied = AlpacaForbiddenError(403, "subscription does not permit querying SIP data", "/bars")
    deps = make_deps(market_data=FakeMarketData(error=denied))
    result = check(run_doctor(green, deps), "data.sip_history")
    assert result.status == "FAIL"
    assert "Q7" in result.detail


def test_rejected_data_keys_fail(green: Path) -> None:
    rejected = AlpacaAuthError(401, "401 Authorization Required", "/bars")
    data = FakeMarketData(error=rejected)
    assert (
        check(run_doctor(green, make_deps(market_data=data)), "data.sip_history").status == "FAIL"
    )


def test_no_screener_on_the_plan_warns(green: Path) -> None:
    data = FakeMarketData(has_screener=False)
    result = check(run_doctor(green, make_deps(market_data=data)), "data.movers")
    assert result.status == "WARN"
    assert "Q1" in result.detail


def test_market_data_checks_skip_without_keys(green: Path) -> None:
    keep = {k: v for k, v in FAKE_SECRETS.items() if not k.startswith("ALPACA_PAPER_B")}
    write_env(green, {**keep, "CLAUDE_TOKEN_CREATED": "2026-10-01"})
    checks = run_doctor(green, make_deps())
    assert check(checks, "data.sip_history").status == "SKIP"
    assert check(checks, "data.movers").status == "SKIP"


# ---------------------------------------------------------------- claude CLI


def test_claude_missing_points_at_path(green: Path) -> None:
    deps = make_deps(which=lambda name: None if name == "claude" else f"C:/fake/bin/{name}.exe")
    checks = run_doctor(green, deps)
    assert check(checks, "claude.version").status == "FAIL"
    assert "PATH" in check(checks, "claude.version").detail
    assert check(checks, "claude.auth").status == "SKIP"


def test_claude_too_old_fails(green: Path) -> None:
    runner = FakeRunner(claude_version="2.1.200 (Claude Code)")
    assert check(run_doctor(green, make_deps(runner=runner)), "claude.version").status == "FAIL"


@pytest.mark.parametrize("shim", ["claude.cmd", "claude.BAT", "claude.ps1"])
def test_a_script_shim_fails(green: Path, shim: str) -> None:
    """D-064: cmd.exe would mangle the inline schema and keep the interrupt from the CLI."""
    runner = FakeRunner()
    found = {"claude": f"C:/npm/{shim}"}
    deps = make_deps(runner=runner, which=found.get)
    result = check(run_doctor(green, deps), "claude.version")
    assert result.status == "FAIL"
    assert "shim" in result.detail
    assert not [call for call in runner.calls if call[0][1:] == ["--version"]]  # never run


def test_a_cli_newer_than_the_tested_one_warns(green: Path) -> None:
    set_yaml(green / "config" / "settings.yaml", "llm.max_tested_cli_version", "2.1.270")
    result = check(run_doctor(green, make_deps(runner=FakeRunner())), "claude.version")
    assert result.status == "WARN"
    assert "2.1.280 is newer than llm.max_tested_cli_version 2.1.270" in result.detail
    set_yaml(green / "config" / "settings.yaml", "llm.max_tested_cli_version", "2.1.280")
    assert check(run_doctor(green, make_deps(runner=FakeRunner())), "claude.version").status == (
        "PASS"
    )


def test_claude_auth_runs_isolated(green: Path) -> None:
    runner = FakeRunner()
    environ = {"PATH": "C:/fake/bin", "ANTHROPIC_API_KEY": "sk-ant-api-in-shell"}
    run_doctor(green, make_deps(runner=runner, environ=environ))
    ((_, env, cwd),) = [call for call in runner.calls if call[0][1:] == ["auth", "status"]]
    assert env["CLAUDE_CONFIG_DIR"] == str(green / "runtime" / "claude-config")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_SECRETS["CLAUDE_CODE_OAUTH_TOKEN"]
    assert "ANTHROPIC_API_KEY" not in env
    assert cwd == green / "runtime" / "llm_sandbox"


@pytest.mark.parametrize(
    ("auth", "fragment"),
    [
        ({"authMethod": "claude.ai"}, "expected 'oauth_token'"),
        ({"authMethod": "oauth_token", "configDirectory": "C:/Users/me/.claude"}, "not isolated"),
        ({"authMethod": "none"}, "CLAUDE_CODE_OAUTH_TOKEN"),
    ],
)
def test_claude_auth_problems_fail(green: Path, auth: dict[str, Any], fragment: str) -> None:
    result = check(run_doctor(green, make_deps(runner=FakeRunner(auth=auth))), "claude.auth")
    assert result.status == "FAIL"
    assert fragment in result.detail


def test_claude_auth_without_json_fails(green: Path) -> None:
    class TextRunner(FakeRunner):
        def __call__(
            self, args: Sequence[str], *, env: Mapping[str, str], cwd: Path, timeout_s: float
        ) -> RunResult:
            if list(args)[1:] == ["auth", "status"]:
                return RunResult(returncode=0, stdout="Logged in.", stderr="")
            return super().__call__(args, env=env, cwd=cwd, timeout_s=timeout_s)

    result = check(run_doctor(green, make_deps(runner=TextRunner())), "claude.auth")
    assert result.status == "FAIL"
    assert "no JSON" in result.detail


@pytest.mark.parametrize(
    ("created", "expected"),
    [
        ("2026-10-01", "PASS"),
        ("2025-11-09", "PASS"),  # 334 days old
        ("2025-11-08", "WARN"),  # 335 days: within 30 days of the one-year expiry
        ("2025-10-09", "FAIL"),  # 365 days: expired
        ("2026-12-01", "FAIL"),  # in the future
        ("YYYY-MM-DD", "FAIL"),  # the env.example placeholder
    ],
)
def test_token_age(green: Path, created: str, expected: str) -> None:
    write_env(green, {**FAKE_SECRETS, "CLAUDE_TOKEN_CREATED": created})
    assert check(run_doctor(green, make_deps()), "claude.token").status == expected


# ---------------------------------------------------------------- runtime, disk, events


def test_sandbox_must_be_empty(green: Path) -> None:
    sandbox = green / "runtime" / "llm_sandbox"
    sandbox.mkdir(parents=True)
    (sandbox / "CLAUDE.md").write_text("instructions", encoding="utf-8")
    result = check(run_doctor(green, make_deps()), "runtime.dirs")
    assert result.status == "FAIL"
    assert "CLAUDE.md" in result.detail


def test_low_disk_warns(green: Path) -> None:
    assert check(run_doctor(green, make_deps(free_bytes=10**9)), "disk").status == "WARN"


def test_empty_events_file_warns(repo: Path) -> None:
    (repo / "config" / "events.yaml").write_text("events: []\n", encoding="utf-8")
    result = check(run_doctor(repo, make_deps()), "events")
    assert result.status == "WARN"
    assert "extend events.yaml" in result.detail


def test_unverified_event_warns(green: Path) -> None:
    body = EVENTS_NEXT_WEEK.replace("verified_by_human: true", "verified_by_human: false")
    (green / "config" / "events.yaml").write_text(body, encoding="utf-8")
    assert check(run_doctor(green, make_deps()), "events").status == "WARN"


# ---------------------------------------------------------------- CLI


def test_cli_exit_code_follows_failures(green: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "default_deps", make_deps)
    ok = CliRunner().invoke(app, ["--root", str(green), "doctor"])
    assert ok.exit_code == 0, ok.output
    assert "0 FAIL" in ok.output
    (green / "config" / "risk.yaml").unlink()
    failed = CliRunner().invoke(app, ["--root", str(green), "doctor"])
    assert failed.exit_code == 1
    for value in FAKE_SECRETS.values():
        assert value not in ok.output + failed.output


def test_format_report_indents_multiline_details() -> None:
    lines = format_report([Check(name="config", status="FAIL", detail="first\nsecond")])
    assert lines[0] == "FAIL  config  first"
    assert lines[1] == " " * len("FAIL  config  ") + "second"


# ---------------------------------------------------------------- sleep and alerts (P6.1b)


SLEEPS = [
    (SleepSettings(0, 0), "PASS", "never sleeps on AC power; wake timers off"),
    (SleepSettings(1800, 1), "PASS", "after 30 min idle on AC power; wake timers are on, so the"),
    (SleepSettings(1800, 0), "WARN", "wake timers off: the 08:25 run-day may not start"),
    (SleepSettings(600, 2), "WARN", "wake timers important ones only"),
    (None, "WARN", "couldn't be read"),
]


@pytest.mark.parametrize(
    ("settings", "status", "says"),
    SLEEPS,
    ids=["never sleeps", "sleeps, wakes", "sleeps, no wake timers", "important only", "unreadable"],
)
def test_machine_sleep(green: Path, settings: SleepSettings | None, status: str, says: str) -> None:
    found = check(run_doctor(green, make_deps(sleep=settings)), "machine.sleep")
    assert found.status == status
    assert says in found.detail


def test_notify_warns_while_alerts_reach_nobody(green: Path) -> None:
    assert check(run_doctor(green, make_deps()), "notify").detail.startswith("ntfy; ")
    write_secrets(green)  # no NTFY_TOPIC
    found = check(run_doctor(green, make_deps()), "notify")
    assert (found.status, found.detail) == (
        "WARN",
        "notify.provider is ntfy, but NTFY_TOPIC isn't set in .env",
    )
    set_yaml(green / "config" / "settings.yaml", "notify.provider", "none")
    found = check(run_doctor(green, make_deps()), "notify")
    assert (found.status, found.detail) == (
        "WARN",
        "notify.provider is none: alerts reach nobody (PLAN H0.5)",
    )
