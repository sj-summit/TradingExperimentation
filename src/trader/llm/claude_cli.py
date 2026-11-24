"""Claude Code CLI integration (ARCHITECTURE.md §7; DECISIONS D-002, D-021, D-064).

- ``build_claude_env``: the subprocess's environment, an allowlist plus our own settings.
- ``build_argv``: A§7.2's flags, the instruction right after ``-p`` (plan decision 2).
- ``resolve_claude``: the ``claude`` executable. On Windows only a native ``.exe`` will do: a
  ``.cmd`` or ``.bat`` shim runs through cmd.exe, which parses the inline schema's quotes again,
  and an interrupt meant for the CLI stops at it.
- ``ClaudeCliTransport``: runs one attempt (plan decision 6).
  - The child starts in its own process group. Past its time it is interrupted (CTRL_BREAK on
    Windows, SIGINT elsewhere), given ``kill_grace_s``, then killed with its whole tree
    (``taskkill /T /F``; SIGTERM, then SIGKILL, elsewhere).
  - Whether the child is still running comes from its return code alone. ``wait()`` also
    waits for the pipes, which a descendant can hold open, and a loop's teardown can cancel it.
  - Stdout is read up to ``max_stdout_bytes``; a child that writes more is killed. Stderr's tail
    is kept.
  - A cancelled call kills its child before the cancellation goes on, however often it is
    cancelled.
"""

import asyncio
import contextlib
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from trader.ops.containment import KillOnCloseJob

MAX_STDOUT_BYTES = 4 * 1024 * 1024  # a review's answer runs to tens of KB
STDERR_TAIL_BYTES = 16 * 1024
SHIMS = frozenset({".cmd", ".bat", ".ps1"})
EMPTY_MCP = '{"mcpServers":{}}'
_CHUNK = 64 * 1024
_POLL_S = 0.05  # how often a running child's return code is checked
_DRAIN_S = 1.0  # how long an exited child's pipes get to reach their end
_REAP_S = 10.0  # how long a killed child gets to be reaped

ToolsFlag = Literal["empty", "deny_all"]
Stop = Literal["interrupt", "terminate", "kill"]

# Variables copied from the parent environment; everything else is dropped. Credentials and
# provider switches (ANTHROPIC_*, CLAUDE_CODE_USE_*, ...) outrank the subscription token, so they
# must never leak into a call (A§7.1, D-002, code.claude.com/docs/en/authentication).
_PASSTHROUGH = frozenset(
    {
        # process basics (Windows and POSIX)
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "COMPUTERNAME", "USERNAME", "USER", "LOGNAME", "LANG", "LC_ALL", "TZ",
        # network plumbing: proxies and corporate CA bundles, never credentials
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
    }
)  # fmt: skip


class ClaudeBinaryError(RuntimeError):
    """No usable ``claude`` executable."""


def build_claude_env(
    *,
    parent: Mapping[str, str],
    config_dir: Path,
    oauth_token: str | None,
    api_key: str | None = None,
) -> dict[str, str]:
    """Environment for a ``claude`` subprocess: an OS-basics allowlist plus our own settings.

    ``CLAUDE_CONFIG_DIR`` isolates the bot from the human's ``~/.claude`` (settings, hooks, MCP
    servers, memory), and ``DISABLE_AUTOUPDATER`` keeps the CLI from updating itself between
    calls (D-064). Pass the subscription ``oauth_token`` or, only when ``llm.provider`` is
    ``anthropic_api``, an ``api_key`` — never both.
    """
    if oauth_token and api_key:
        raise ValueError("pass the OAuth token or an API key, not both")
    env = {name: value for name, value in parent.items() if name.upper() in _PASSTHROUGH}
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["DISABLE_AUTOUPDATER"] = "1"
    if oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env


def build_argv(
    command: Sequence[str],
    *,
    instruction: str,
    schema: str,
    model: str,
    effort: str,
    system_prompt_file: Path,
    max_turns: int,
    tools_flag: ToolsFlag = "empty",
    safe_mode: bool = True,
) -> list[str]:
    """A§7.2's invocation. ``command`` is the executable, or the test stand-in's interpreter,
    script and scenario.

    - ``tools_flag`` ``deny_all`` is A§7.2's fallback: no ``--tools``, every tool denied instead.
    - ``--setting-sources user`` loads only ``CLAUDE_CONFIG_DIR``'s settings, never a project's:
      no settings file, ``CLAUDE.md`` or ``.mcp.json`` from the sandbox's parent folders, and on
      POSIX no ``settings.local.json`` from the repository's root (D-064). ``--safe-mode`` turns
      off what is left: skills, plugins, hooks, MCP servers and auto memory.
    """
    argv = [
        *command,
        "-p",
        instruction,
        "--output-format", "json",
        "--json-schema", schema,
        "--model", model,
        "--effort", effort,
        "--system-prompt-file", str(system_prompt_file),
    ]  # fmt: skip
    if tools_flag == "empty":
        argv += ["--tools", "", "--disallowedTools", "mcp__*"]
    else:
        argv += ["--disallowedTools", "*"]
    argv += [
        "--strict-mcp-config",
        "--mcp-config", EMPTY_MCP,
        "--setting-sources", "user",
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--max-turns", str(max_turns),
        "--no-session-persistence",
    ]  # fmt: skip
    if safe_mode:
        argv.append("--safe-mode")
    return argv


def resolve_claude(
    name: str,
    *,
    which: Callable[[str], str | None] = shutil.which,
    windows: bool = sys.platform == "win32",
) -> Path:
    """The executable ``name`` (``llm.claude_bin``) runs. On Windows it must be a native
    ``.exe``; elsewhere a script shim is refused."""
    found = which(name)
    if found is None:
        raise ClaudeBinaryError(
            f"`{name}` not found: add its folder (e.g. %USERPROFILE%\\.local\\bin) to PATH, "
            "or set llm.claude_bin to the full path"
        )
    path = Path(found)
    suffix = path.suffix.lower()
    if (suffix != ".exe") if windows else (suffix in SHIMS):
        kind = f"a {path.suffix} shim" if path.suffix else "not a native executable"
        raise ClaudeBinaryError(
            f"{path} is {kind}: install Claude Code's native executable (PLAN.md H0.2); a shim "
            "mangles the inline schema and swallows the interrupt (D-064)"
        )
    return path


def parse_version(text: str) -> tuple[int, int, int] | None:
    """The first ``X.Y.Z`` in ``text``, such as ``claude --version``'s output."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


# ---------------------------------------------------------------- the transport


@dataclass(frozen=True)
class TransportResult:
    exit_code: int | None
    stdout: bytes
    stderr_tail: bytes
    duration_s: float
    timed_out: bool = False
    overflow: bool = False  # stdout ran past the cap; the child was killed
    stopped: Stop | None = None  # how a late or overflowing child was stopped
    stderr_cut: bool = False  # stderr ran past its tail: the tail starts at an arbitrary byte


class Transport(Protocol):
    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
    ) -> TransportResult: ...


class ClaudeCliTransport:
    """One attempt as a subprocess: ``run`` returns once the child has exited or been killed."""

    def __init__(
        self,
        *,
        kill_grace_s: float = 5.0,
        max_stdout_bytes: int = MAX_STDOUT_BYTES,
        stderr_tail_bytes: int = STDERR_TAIL_BYTES,
        on_spawn: Callable[[asyncio.subprocess.Process], None] | None = None,
        job: KillOnCloseJob | None = None,
    ) -> None:
        self._grace = kill_grace_s
        self._max_stdout, self._tail = max_stdout_bytes, stderr_tail_bytes
        self._on_spawn = on_spawn
        # Each child joins a job that dies with this process, so a killed engine leaves no
        # claude call running (D-083). Held for the transport's life, never closed by it.
        self._job = job if job is not None else KillOnCloseJob()

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
    ) -> TransportResult:
        started = time.monotonic()
        proc = await _spawn(argv, env=env, cwd=cwd)
        self._job.assign(proc.pid)
        if self._on_spawn is not None:
            self._on_spawn(proc)
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        out, err = _Sink(), _Sink()
        reading = asyncio.create_task(_pump(proc.stdout, out, cap=self._max_stdout))
        tailing = asyncio.create_task(_pump(proc.stderr, err, tail=self._tail))
        feeding = asyncio.create_task(_feed(proc.stdin, stdin))
        timed_out = False
        stopped: Stop | None = None
        try:
            try:
                end = started + max(timeout_s, 0.0)
                if not await _exits(proc, until=end, unless=out.overflowed):
                    timed_out = not out.overflow  # still running at the deadline
                    stopped = await self._stop(proc, gently=timed_out)
            except asyncio.CancelledError:
                await _finish(asyncio.ensure_future(self._stop(proc, gently=False)))
                raise
            # The child is gone. Its pipes end with it, unless a descendant holds them open.
            await asyncio.wait({reading, tailing}, timeout=_DRAIN_S)
        finally:
            for task in (reading, tailing, feeding):
                task.cancel()
        return TransportResult(
            exit_code=proc.returncode,
            stdout=bytes(out.data),
            stderr_tail=bytes(err.data),
            duration_s=time.monotonic() - started,
            timed_out=timed_out,
            overflow=out.overflow,
            stopped=stopped,
            stderr_cut=err.cut,
        )

    async def _stop(self, proc: asyncio.subprocess.Process, *, gently: bool) -> Stop | None:
        """Stop the child: interrupt it and give it the grace (``gently``), then kill its tree.
        None if it had exited already."""
        if proc.returncode is not None:
            return None
        if gently:
            if _interrupt(proc) and await _exits_within(proc, self._grace):
                return "interrupt"
            if sys.platform != "win32":
                _signal_group(proc, signal.SIGTERM)
                if await _exits_within(proc, self._grace):
                    return "terminate"
        await _kill_tree(proc)
        await _exits_within(proc, _REAP_S)
        return "kill"


async def _spawn(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: Path
) -> asyncio.subprocess.Process:
    """Start the child in its own process group, so an interrupt reaches it and nothing else."""
    group: dict[str, Any] = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env),
        cwd=cwd,
        **group,
    )


class _Sink:
    def __init__(self) -> None:
        self.data = bytearray()
        self.overflowed = asyncio.Event()
        self.cut = False  # bytes were dropped from the front (a tail)

    @property
    def overflow(self) -> bool:
        return self.overflowed.is_set()


async def _pump(
    stream: asyncio.StreamReader, sink: _Sink, *, cap: int | None = None, tail: int | None = None
) -> None:
    """Read ``stream`` to its end into ``sink``: its first ``cap`` bytes, or its last ``tail``.
    Past the cap it reads on and drops what it reads: a pipe left full never ends."""
    while chunk := await stream.read(_CHUNK):
        if sink.overflow:
            continue
        sink.data += chunk
        if cap is not None and len(sink.data) > cap:
            del sink.data[cap:]
            sink.overflowed.set()
        elif tail is not None and len(sink.data) > tail:
            del sink.data[:-tail]
            sink.cut = True


async def _feed(pipe: asyncio.StreamWriter, data: bytes) -> None:
    try:
        pipe.write(data)
        await pipe.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # the child exited, or closed stdin, before reading it all
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            pipe.close()


async def _exits(
    proc: asyncio.subprocess.Process, *, until: float, unless: asyncio.Event | None = None
) -> bool:
    """Whether the child exits by ``until`` (``time.monotonic``), from its return code. False at
    once when ``unless`` is set."""
    while proc.returncode is None:
        left = until - time.monotonic()
        if left <= 0 or (unless is not None and unless.is_set()):
            return False
        await asyncio.sleep(min(_POLL_S, left))
    return True


async def _exits_within(proc: asyncio.subprocess.Process, seconds: float) -> bool:
    return await _exits(proc, until=time.monotonic() + seconds)


async def _finish(task: asyncio.Future[Any]) -> None:
    """Wait for ``task`` however often the caller is cancelled meanwhile; its own waits are
    bounded. What it raises is dropped: the caller's cancellation goes on."""
    while not task.done():
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(task)


def _interrupt(proc: asyncio.subprocess.Process) -> bool:
    """Ask the child to stop: CTRL_BREAK to its group on Windows (the only console event a new
    group receives), SIGINT elsewhere. False if it couldn't be sent, as with no console."""
    try:
        if sys.platform == "win32":
            proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        else:
            os.killpg(proc.pid, signal.SIGINT)
    except (OSError, ValueError):
        return False
    return True


def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, sig)


def _taskkill() -> str:
    """System32's taskkill by its full path: a bare name is searched for in the interpreter's
    folder and the working directory first."""
    return os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if sys.platform == "win32":
        with contextlib.suppress(OSError, TimeoutError):
            killer = await asyncio.create_subprocess_exec(
                _taskkill(), "/T", "/F", "/PID", str(proc.pid),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )  # fmt: skip
            await asyncio.wait_for(killer.wait(), _REAP_S)
        if proc.returncode is None:
            with contextlib.suppress(OSError):
                proc.kill()
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
