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
from trader.doctor import (
    AccountInfo,
    Check,
    DoctorDeps,
    ProbeError,
    RunResult,
    format_report,
    run_doctor,
)

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
    ) -> None:
        self.status, self.equity, self.error = status, equity, error
        self.positions, self.orders, self.time = positions, orders, server_time

    def account(self) -> AccountInfo:
        if self.error:
            raise self.error
        equity = Decimal(self.equity) if self.equity is not None else None
        return AccountInfo(
            status=self.status, equity=equity, trading_blocked=False, account_blocked=False
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


def make_deps(
    *,
    probes: Mapping[str, FakeProbe] | None = None,
    runner: FakeRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    free_bytes: int = 100 * 1024**3,
    environ: Mapping[str, str] | None = None,
) -> DoctorDeps:
    known = dict(probes or {})
    return DoctorDeps(
        clock=SimClock(NOW),
        alpaca=lambda key_id, _secret: known.get(key_id, FakeProbe()),
        run=runner or FakeRunner(),
        which=which or (lambda name: f"C:/fake/bin/{name}.exe"),
        free_bytes=lambda _path: free_bytes,
        environ=environ if environ is not None else {"PATH": "C:/fake/bin", "SYSTEMROOT": "C:/W"},
    )


def write_secrets(root: Path, **overrides: str) -> None:
    write_env(root, {**FAKE_SECRETS, "CLAUDE_TOKEN_CREATED": "2026-10-01", **overrides})


def statuses(checks: list[Check]) -> dict[str, str]:
    return {c.name: c.status for c in checks}


def check(checks: list[Check], name: str) -> Check:
    return next(c for c in checks if c.name == name)


@pytest.fixture
def green(repo: Path) -> Path:
    """The `repo` fixture with a verified high-impact event next week."""
    (repo / "config" / "events.yaml").write_text(EVENTS_NEXT_WEEK, encoding="utf-8")
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
        "runtime.dirs": "PASS",
        "journal": "PASS",
        "disk": "PASS",
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


@pytest.mark.parametrize(("offset_s", "expected"), [(1.5, "PASS"), (-1.9, "PASS"), (2.5, "FAIL")])
def test_clock_skew(green: Path, offset_s: float, expected: str) -> None:
    probes = {KEY_A: FakeProbe(server_time=NOW + timedelta(seconds=offset_s))}
    assert check(run_doctor(green, make_deps(probes=probes)), "alpaca.clock").status == expected


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
    result = check(run_doctor(repo, make_deps()), "events")
    assert result.status == "WARN"
    assert "P1.8" in result.detail


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
