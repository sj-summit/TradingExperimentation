import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import FAKE_SECRETS, REPO, RealClaudeError, write_env
from trader.config import Secrets


@pytest.fixture(autouse=True)
def _isolate_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets in the developer's own environment must never reach a test."""
    for name in Secrets.env_names():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("TRADER_ROOT", raising=False)


def names_claude(part: object) -> bool:
    """Whether ``part`` names the claude executable, however it is spelled: Windows drops a
    trailing dot or space, and any suffix (``.exe``, ``.cmd``) still runs it."""
    if not isinstance(part, str | bytes | os.PathLike):
        return False
    name = Path(os.fsdecode(part)).name.rstrip(" .").lower()
    return name.split(".")[0] == "claude"


OS_TOOLS = frozenset({"schtasks", "tzutil", "powercfg"})


def names_os_tool(part: object) -> bool:
    """Whether ``part`` names a Windows tool that changes or reads the machine's scheduling or
    power setup (Phase 6): tests use fakes, never the real one."""
    if not isinstance(part, str | bytes | os.PathLike):
        return False
    name = Path(os.fsdecode(part)).name.rstrip(" .").lower()
    return name.split(".")[0] in OS_TOOLS


def _parts(args: Any) -> list[Any]:
    if isinstance(args, str | bytes):  # a command line: its words, unquoted
        return [word.strip('"') for word in os.fsdecode(args).split()]
    if isinstance(args, os.PathLike):
        return [args]
    return list(args)


@pytest.fixture(autouse=True)
def power_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """run-day keeps the PC awake (P6.1b); no test touches the real power state. The flags each
    SetThreadExecutionState call would have passed."""
    calls: list[int] = []

    def fake(flags: int) -> int:
        calls.append(flags)
        return 0x80000000  # the previous state: success

    monkeypatch.setattr("trader.ops.power.set_execution_state", fake)
    return calls


@pytest.fixture(autouse=True)
def _never_the_real_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLAUDE.md invariant 9: no test runs the real claude CLI, by any path (the transport,
    doctor's probes, a wrapper such as ``cmd /c claude``): every process starts through
    ``subprocess.Popen``, asyncio's too. scripts/fake_claude.py stands in for it (D-064).
    Neither do schtasks, tzutil or powercfg (Phase 6): no test may touch the PC's schedule."""
    init = subprocess.Popen.__init__

    def guarded(self: subprocess.Popen[Any], args: Any, *rest: Any, **kw: Any) -> None:
        parts = [*_parts(args), kw.get("executable")]
        if any(names_claude(part) for part in parts):
            raise RealClaudeError("a test tried to run the real claude CLI (CLAUDE.md invariant 9)")
        if any(names_os_tool(part) for part in parts):
            raise RealClaudeError(f"a test tried to run a real Windows tool: {parts[0]!r}")
        init(self, args, *rest, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throw-away repo root: config copied from the committed examples plus a fake .env."""
    config = tmp_path / "config"
    config.mkdir()
    shutil.copy(REPO / "config" / "settings.example.yaml", config / "settings.yaml")
    shutil.copy(REPO / "config" / "risk.example.yaml", config / "risk.yaml")
    shutil.copy(REPO / "config" / "events.yaml", config / "events.yaml")
    write_env(tmp_path, {**FAKE_SECRETS, "CLAUDE_TOKEN_CREATED": "2026-10-01"})
    return tmp_path
