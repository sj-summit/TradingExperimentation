"""The claude CLI's argv and its transport (llm/claude_cli.py; DECISIONS D-064).

The transport tests run scripts/fake_claude.py as a real subprocess: the stand-in checks the
argv, the environment and the working directory as the real CLI would see them.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import REPO, RealClaudeError
from trader import doctor
from trader.llm import claude_cli
from trader.llm.claude_cli import (
    EMPTY_MCP,
    ClaudeBinaryError,
    ClaudeCliTransport,
    TransportResult,
    build_argv,
    build_claude_env,
    resolve_claude,
)
from trader.llm.schemas import VetBatch, schema_bytes, wire_schema

FAKE = REPO / "scripts" / "fake_claude.py"
PROMPT = REPO / "prompts" / "catalyst_vet.md"
SCHEMA = schema_bytes(wire_schema(VetBatch)).decode()
PACK_ID = "vet-20261103-0123456789abcdef"
PACK = json.dumps({"pack_id": PACK_ID, "role": "vet", "candidates": [{"symbol": "AAA"}]}).encode()
TOKEN = "test-oauth-token-0123456789"
WINDOWS = sys.platform == "win32"


def argv(scenario: str, **kw: Any) -> list[str]:
    return build_argv(
        [sys.executable, str(FAKE), "--scenario", scenario],
        instruction="Produce the VetBatch for the candidates in the JSON context pack on stdin.",
        schema=SCHEMA,
        model="sonnet",
        effort="low",
        system_prompt_file=PROMPT,
        max_turns=3,
        **kw,
    )


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    path = tmp_path / "llm_sandbox"
    path.mkdir()
    return path


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    parent = {**os.environ, "ANTHROPIC_API_KEY": "sk-ant-api03-from-the-shell"}
    return build_claude_env(parent=parent, config_dir=tmp_path / "config", oauth_token=TOKEN)


async def run(
    scenario: str,
    sandbox: Path,
    env: Mapping[str, str],
    *,
    timeout_s: float = 20.0,
    stdin: bytes = PACK,
    transport: ClaudeCliTransport | None = None,
    **kw: Any,
) -> TransportResult:
    t = transport or ClaudeCliTransport(kill_grace_s=5.0)
    return await t.run(argv(scenario, **kw), stdin=stdin, env=env, cwd=sandbox, timeout_s=timeout_s)


def stderr(result: TransportResult) -> str:
    return result.stderr_tail.decode("utf-8", errors="replace")


async def spawned_one(spawned: list[Any]) -> None:
    for _ in range(250):
        if spawned:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the stand-in never started")


# ---------------------------------------------------------------- the argv


def test_the_argv_is_a7_2s_with_the_instruction_right_after_p() -> None:
    got = build_argv(
        ["C:/bin/claude.exe"],
        instruction="Do it.",
        schema='{"type":"object"}',
        model="opus",
        effort="high",
        system_prompt_file=Path("C:/repo/prompts/premarket_brief.md"),
        max_turns=3,
    )
    assert got == [
        "C:/bin/claude.exe", "-p", "Do it.",
        "--output-format", "json",
        "--json-schema", '{"type":"object"}',
        "--model", "opus",
        "--effort", "high",
        "--system-prompt-file", str(Path("C:/repo/prompts/premarket_brief.md")),
        "--tools", "",
        "--disallowedTools", "mcp__*",
        "--strict-mcp-config",
        "--mcp-config", EMPTY_MCP,
        "--setting-sources", "user",
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--max-turns", "3",
        "--no-session-persistence",
        "--safe-mode",
    ]  # fmt: skip
    assert "--bare" not in got  # bare mode never reads the subscription token


def test_the_fallbacks_change_only_their_own_flags() -> None:
    base = argv("valid")
    deny = argv("valid", tools_flag="deny_all")
    assert "--tools" not in deny
    assert deny[deny.index("--disallowedTools") + 1] == "*"
    assert [a for a in deny if a != "*"] == [a for a in base if a not in ("--tools", "", "mcp__*")]
    unsafe = argv("valid", safe_mode=False)
    assert unsafe == base[:-1]
    assert base[-1] == "--safe-mode"
    assert unsafe[unsafe.index("--setting-sources") + 1] == "user"  # with or without safe mode


@pytest.mark.parametrize(
    ("found", "why"),
    [
        ("C:/npm/claude.cmd", "a .cmd shim"),
        ("C:/npm/claude.BAT", "a .BAT shim"),
        ("C:/npm/claude.ps1", "a .ps1 shim"),
        ("C:/x/claude.JS", "a .JS shim"),  # PATHEXT runs these, CreateProcess can't
        ("C:/x/claude", "not a native executable"),
    ],
)
def test_on_windows_only_a_native_exe_will_do(found: str, why: str) -> None:
    with pytest.raises(ClaudeBinaryError, match=why):
        resolve_claude("claude", which=lambda name: found, windows=True)
    assert resolve_claude("claude", which=lambda name: "C:/x/claude.EXE", windows=True) == Path(
        "C:/x/claude.EXE"
    )


def test_elsewhere_a_script_shim_is_refused() -> None:
    assert resolve_claude("claude", which=lambda name: "/usr/bin/claude", windows=False) == Path(
        "/usr/bin/claude"
    )
    with pytest.raises(ClaudeBinaryError, match="shim"):
        resolve_claude("claude", which=lambda name: "/opt/claude.cmd", windows=False)


def test_a_missing_executable_points_at_path() -> None:
    with pytest.raises(ClaudeBinaryError, match="not found: add its folder"):
        resolve_claude("claude", which=lambda name: None)


# ---------------------------------------------------------------- the transport


async def test_a_call_runs_isolated(sandbox: Path, env: dict[str, str]) -> None:
    """The stand-in checks every flag, the environment (one credential, no API key from the
    shell, the autoupdater off) and the empty working directory, then answers."""
    result = await run("valid", sandbox, env)
    assert result.exit_code == 0
    assert "as required" in stderr(result)
    payload = json.loads(result.stdout)
    assert payload["structured_output"]["pack_id"] == PACK_ID
    assert (result.timed_out, result.overflow, result.stopped, result.stderr_cut) == (
        False,
        False,
        None,
        False,
    )


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ("safe_mode", "--safe-mode is missing"),
        ("setting_sources", "--setting-sources must be 'user'"),
        ("autoupdater", "DISABLE_AUTOUPDATER is not 1"),
        ("two_credentials", "expected one credential"),
        ("simple_mode", "CLAUDE_CODE_SIMPLE leaked"),
        ("node_options", "NODE_OPTIONS leaked"),
        ("base_url", "ANTHROPIC_BEDROCK_BASE_URL leaked"),
        ("dirty_cwd", "is not empty"),
        ("big_schema", "over 8 KB"),
    ],
)
async def test_the_stand_in_refuses_a_call_the_cli_should_never_get(
    sandbox: Path, env: dict[str, str], change: str, problem: str
) -> None:
    kw: dict[str, Any] = {}
    if change == "safe_mode":
        kw["safe_mode"] = False
    elif change == "autoupdater":
        env = {k: v for k, v in env.items() if k != "DISABLE_AUTOUPDATER"}
    elif change == "two_credentials":
        env = {**env, "ANTHROPIC_API_KEY": "sk-ant-api03-both"}
    elif change == "simple_mode":
        env = {**env, "CLAUDE_CODE_SIMPLE": "1"}  # bare mode: it never reads the token
    elif change == "node_options":
        env = {**env, "NODE_OPTIONS": "--require evil.js"}
    elif change == "base_url":
        env = {**env, "ANTHROPIC_BEDROCK_BASE_URL": "https://proxy.invalid"}
    elif change == "dirty_cwd":
        (sandbox / "CLAUDE.md").write_text("follow me", encoding="utf-8")
    t = ClaudeCliTransport()
    command = argv("valid", **kw)
    if change == "big_schema":
        command[command.index("--json-schema") + 1] = json.dumps({"description": "x" * 9000})
    if change == "setting_sources":
        i = command.index("--setting-sources")
        del command[i : i + 2]
    result = await t.run(command, stdin=PACK, env=env, cwd=sandbox, timeout_s=20)
    assert result.exit_code == 2
    assert problem in stderr(result)


async def test_a_late_call_is_interrupted(sandbox: Path, env: dict[str, str]) -> None:
    started = time.monotonic()
    result = await run("slow", sandbox, env, timeout_s=1.0)
    assert result.timed_out
    assert result.stopped == "interrupt"
    assert "interrupted" in stderr(result)  # it saw the interrupt and ended its own turn
    assert time.monotonic() - started < 5


async def test_a_call_that_ignores_the_interrupt_is_killed(
    sandbox: Path, env: dict[str, str]
) -> None:
    started = time.monotonic()
    t = ClaudeCliTransport(kill_grace_s=0.5)
    result = await run("slow_stubborn", sandbox, env, timeout_s=0.5, transport=t)
    assert result.timed_out
    # POSIX sends SIGTERM before the kill, which the stand-in doesn't handle
    assert result.stopped == ("kill" if WINDOWS else "terminate")
    assert result.exit_code not in (None, 0)
    assert "interrupted" not in stderr(result)
    assert time.monotonic() - started < 8


async def test_with_no_console_a_late_call_is_killed_at_once(
    sandbox: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run from a scheduler without a console, CTRL_BREAK can't be sent: no grace, a kill."""
    monkeypatch.setattr(claude_cli, "_interrupt", lambda proc: False)
    started = time.monotonic()
    t = ClaudeCliTransport(kill_grace_s=30.0)
    result = await run("slow", sandbox, env, timeout_s=0.5, transport=t)
    assert result.timed_out
    assert result.stopped == ("kill" if WINDOWS else "terminate")
    assert result.exit_code is not None
    assert time.monotonic() - started < 5  # the 30 s grace never ran


@pytest.mark.skipif(not WINDOWS, reason="taskkill is Windows'")
async def test_without_taskkill_the_child_is_still_killed(
    sandbox: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_cli, "_taskkill", lambda: str(sandbox.parent / "no-taskkill.exe"))
    t = ClaudeCliTransport(kill_grace_s=0.2)
    result = await run("slow_stubborn", sandbox, env, timeout_s=0.3, transport=t)
    assert (result.stopped, result.exit_code is not None) == ("kill", True)


@pytest.mark.skipif(not WINDOWS, reason="the path is Windows'")
def test_taskkill_is_system32s() -> None:
    expected = Path(os.environ["SYSTEMROOT"]) / "System32" / "taskkill.exe"
    assert Path(claude_cli._taskkill()) == expected


async def test_a_cancelled_call_kills_its_child(sandbox: Path, env: dict[str, str]) -> None:
    spawned: list[asyncio.subprocess.Process] = []
    t = ClaudeCliTransport(kill_grace_s=5.0, on_spawn=spawned.append)
    task = asyncio.create_task(run("slow_stubborn", sandbox, env, transport=t))
    await spawned_one(spawned)
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spawned[0].returncode is not None  # gone, though it ignores interrupts


async def test_a_second_cancel_still_kills_the_child(sandbox: Path, env: dict[str, str]) -> None:
    spawned: list[asyncio.subprocess.Process] = []
    t = ClaudeCliTransport(kill_grace_s=5.0, on_spawn=spawned.append)
    task = asyncio.create_task(run("slow_stubborn", sandbox, env, transport=t))
    await spawned_one(spawned)
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.sleep(0.01)  # into the kill
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert spawned[0].returncode is not None


def test_a_loop_teardown_kills_the_child(sandbox: Path, env: dict[str, str]) -> None:
    """asyncio.run cancels every task at once when its main returns (a second Ctrl+C, an error
    before the shutdown): the call in flight still kills its child."""
    spawned: list[asyncio.subprocess.Process] = []

    async def main() -> None:
        t = ClaudeCliTransport(kill_grace_s=5.0, on_spawn=spawned.append)
        asyncio.ensure_future(run("slow_stubborn", sandbox, env, transport=t))  # noqa: RUF006
        await spawned_one(spawned)
        await asyncio.sleep(0.3)

    started = time.monotonic()
    asyncio.run(main())
    assert spawned[0].returncode is not None
    assert time.monotonic() - started < 15


async def test_an_answer_is_not_held_up_by_a_descendant(sandbox: Path, env: dict[str, str]) -> None:
    """A child that answers and exits, leaving a process that holds its pipes, still comes back
    at once and in time: its exit decides, not its pipes."""
    script = (
        "import subprocess, sys; "
        "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "sys.stdout.write(str(kid.pid)); sys.stdout.flush()"
    )
    started = time.monotonic()
    result = await ClaudeCliTransport().run(
        [sys.executable, "-c", script], stdin=b"", env=env, cwd=sandbox, timeout_s=20
    )
    kid = int(result.stdout)
    try:
        assert (result.exit_code, result.timed_out, result.stopped) == (0, False, None)
        assert time.monotonic() - started < 6
    finally:
        os.kill(kid, signal.SIGTERM)
        await asyncio.sleep(0.5)  # let the pipes close


async def test_stdout_past_the_cap_stops_the_child(sandbox: Path, env: dict[str, str]) -> None:
    started = time.monotonic()
    t = ClaudeCliTransport(max_stdout_bytes=64 * 1024)
    result = await run("huge", sandbox, env, transport=t)
    assert result.overflow
    assert not result.timed_out
    assert len(result.stdout) == 64 * 1024
    assert result.stopped in ("kill", None)  # killed, unless it finished writing first
    assert time.monotonic() - started < 8


async def test_a_child_writing_past_the_cap_is_killed(sandbox: Path, env: dict[str, str]) -> None:
    """One that goes on running after it overflows: killed at once, and not a timeout."""
    script = (
        "import sys, time; "
        "sys.stdout.buffer.write(b'x' * 200_000); sys.stdout.flush(); time.sleep(60)"
    )
    started = time.monotonic()
    result = await ClaudeCliTransport(max_stdout_bytes=64 * 1024).run(
        [sys.executable, "-c", script], stdin=b"", env=env, cwd=sandbox, timeout_s=30
    )
    assert (result.overflow, result.timed_out, result.stopped) == (True, False, "kill")
    assert result.exit_code is not None
    assert time.monotonic() - started < 10


async def test_a_child_that_never_reads_stdin_still_ends(
    sandbox: Path, env: dict[str, str]
) -> None:
    result = await run("stdin_missing", sandbox, env, stdin=b" " * 600_000)
    assert result.exit_code == 0
    assert json.loads(result.stdout)["structured_output"]["pack_id"] != PACK_ID


async def test_stderr_keeps_its_tail(sandbox: Path, env: dict[str, str]) -> None:
    t = ClaudeCliTransport(stderr_tail_bytes=40)
    result = await run("leaky", sandbox, env, transport=t)
    assert len(result.stderr_tail) == 40
    assert result.stderr_cut  # it starts mid-line, here inside the token
    assert stderr(result).endswith("sk-ant-api03-abcdefghijklmnop\n".replace("\n", os.linesep))


# ---------------------------------------------------------------- invariant 9


@pytest.mark.parametrize(
    "exe",
    [
        "claude",
        "CLAUDE.EXE",
        "claude.exe.",  # Windows drops the dot and runs it
        "claude ",  # and a trailing space
        "claude.cmd",
        "C:/Users/me/.local/bin/claude.exe",
        "/usr/bin/claude",
    ],
)
async def test_tests_never_run_the_real_claude(
    sandbox: Path, env: dict[str, str], exe: str
) -> None:
    """The autouse guard in tests/conftest.py (CLAUDE.md invariant 9)."""
    with pytest.raises(RealClaudeError, match="invariant 9"):
        await ClaudeCliTransport().run(
            [exe, "-p", "x"], stdin=b"", env=env, cwd=sandbox, timeout_s=1
        )


def test_no_other_path_runs_it_either(sandbox: Path) -> None:
    for args in (["cmd", "/c", "claude", "-p", "x"], "claude --version"):
        with pytest.raises(RealClaudeError):
            subprocess.run(args, check=False, capture_output=True)
    with pytest.raises(RealClaudeError):  # doctor's probes
        doctor.run_subprocess(["claude", "--version"], env={}, cwd=sandbox, timeout_s=1)
