"""`trader smoke llm` (llm/smoke.py, llm/smoke_packs.py, llm/descendants.py; P4.6).

The real run is HUMAN GATE G4a. Nothing here runs the claude CLI: the calls go to the scripted
transport, or to scripts/fake_claude.py for the kill test, and a stand-in that reads the
canary's planted files plays a CLI that isn't isolated.
"""

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.helpers import FAKE_SECRETS, REPO, set_yaml, write_env
from trader.cli import app
from trader.config import AppConfig, load_config
from trader.core.clock import SimClock, et_datetime
from trader.llm import descendants as watching
from trader.llm.budget import Budget
from trader.llm.claude_cli import ClaudeBinaryError, ClaudeCliTransport, build_argv
from trader.llm.context import encode, experiment_section, role_limits
from trader.llm.descendants import DescendantWatch, descendants
from trader.llm.fake import Reply, ScriptedTransport, Seen, answer_for, envelope, valid
from trader.llm.roles import role_specs
from trader.llm.schemas import DailyReview, VetBatch, schema_bytes, wire_schema
from trader.llm.smoke import (
    HOOK_EVENTS,
    ROLES,
    Canary,
    SmokeOptions,
    SmokeTools,
    answer_lines,
    cli_version,
    problem_lines,
    run_smoke_llm,
    smoke_options,
    without_isolation,
)
from trader.llm.smoke_packs import load_data, smoke_packs
from trader.memory.journal import Journal
from trader.orchestrator.lock import RunLock

NOW = et_datetime(date(2026, 10, 19), time(18))
FAKE = REPO / "scripts" / "fake_claude.py"
WINDOWS = sys.platform == "win32"
TOKEN = re.compile(r"CANARY-[A-Z]+-[0-9a-z]+")


@pytest.fixture
def root(repo: Path) -> Path:
    shutil.copytree(REPO / "prompts", repo / "prompts")
    set_yaml(repo / "config" / "settings.yaml", "llm.kill_grace_s", 1)
    return repo


@pytest.fixture
def cfg(root: Path) -> AppConfig:
    return load_config(root)


@pytest.fixture
async def journal(root: Path) -> AsyncIterator[Journal]:
    opened = await Journal.open(root / "runtime" / "trader.db")
    try:
        yield opened
    finally:
        await opened.aclose()


@dataclass
class Run:
    ok: bool
    lines: list[str]
    seen: list[Seen]

    def has(self, text: str) -> bool:
        return any(text in line for line in self.lines)


async def smoke(
    cfg: AppConfig,
    journal: Journal,
    script: Any = valid,
    *,
    options: SmokeOptions | None = None,
    **tools: Any,
) -> Run:
    """The smoke test on the scripted transport (or on ``tools``' stand-ins)."""
    transport = ScriptedTransport(script)
    defaults: dict[str, Any] = {
        "command": lambda _name: ("stand-in",),
        "version": lambda *_: "2.1.290 (Claude Code)",
        "transport": lambda _grace, _on_spawn: transport,
        "git": lambda _folder: False,
        "token": lambda: "tok3n",
    }
    lines: list[str] = []
    ok = await run_smoke_llm(
        cfg,
        journal,
        SimClock(NOW),
        lines.append,
        options or SmokeOptions(),
        SmokeTools(**{**defaults, **tools}),
    )
    return Run(ok, lines, transport.seen)


def sandbox(cfg: AppConfig) -> Path:
    return cfg.path(cfg.settings.llm.sandbox_dir)


def canary_root(cfg: AppConfig) -> Path:
    return sandbox(cfg).parent / "llm_canary" / f"{NOW:%Y%m%d-%H%M%S}"


def instruction(seen: Seen) -> str:
    return seen.argv[seen.argv.index("-p") + 1]


# ---------------------------------------------------------------- the packs


def test_the_packs_are_fixed_fictional_and_full_size(cfg: AppConfig) -> None:
    first, again = smoke_packs(cfg.settings, cfg.risk), smoke_packs(cfg.settings, cfg.risk)
    sizes = {}
    for name in ("premarket", "vet", "review", "canary"):
        a, b = encode(getattr(first, name)), encode(getattr(again, name))
        assert a.data == b.data  # the same bytes every run
        assert len(a.data) < cfg.settings.llm.max_pack_bytes
        assert (a.trimmed.news, a.trimmed.summaries, a.trimmed.items) == (0, 0, 0)
        sizes[name] = len(a.kept)
    assert sizes == {"premarket": 40, "vet": 25, "review": 8, "canary": 3}
    vet = json.loads(encode(first.vet).data)
    assert [len(c["news"]) for c in vet["candidates"]] == [5] * 25  # the vet at full size
    symbols = [issuer.symbol for issuer in load_data().issuers]
    assert len(set(symbols)) == 40
    assert all(re.fullmatch(r"[A-Z]{6}", s) for s in symbols)  # no US exchange lists six letters


def test_each_role_sees_the_injection_flagged(cfg: AppConfig) -> None:
    packs = smoke_packs(cfg.settings, cfg.risk)
    target = load_data().injection.symbol
    for role in ROLES:
        flagged = encode(packs.role(role)).flagged
        assert flagged == {f"20261103-{target}-orb5" if role == "review" else target}


def test_the_packs_carry_the_live_setup(cfg: AppConfig) -> None:
    """The prompts read the numbers a trading day's packs would hold (D-065)."""
    packs = smoke_packs(cfg.settings, cfg.risk)
    expected = json.loads(json.dumps(experiment_section(cfg.settings, cfg.risk)))
    for role in ROLES:
        body = json.loads(encode(packs.role(role)).data)
        assert body["experiment"] == expected
        assert body["limits"] == role_limits(cfg.settings, role).section()
    premarket = json.loads(encode(packs.premarket).data)["candidates"]
    assert {c["filings_unavailable"] for c in premarket} == {True, False}
    assert {c["picked_as"] for c in premarket} == {"gainer", "news"}


@pytest.mark.parametrize("role", ROLES)
def test_a_cautious_answer_to_each_pack_validates(cfg: AppConfig, role: str) -> None:
    pack = encode(smoke_packs(cfg.settings, cfg.risk).role(role))  # type: ignore[arg-type]
    spec = role_specs(cfg.settings)[role]  # type: ignore[index]
    answer = answer_for(json.loads(pack.data))
    spec.output.model_validate(answer, context={"pack_id": pack.pack_id})


# ---------------------------------------------------------------- the role calls


async def test_a_clean_run_passes_and_counts_toward_nothing(
    cfg: AppConfig, journal: Journal
) -> None:
    run = await smoke(cfg, journal)
    assert run.ok
    assert run.lines[-1] == "SMOKE LLM PASSED"
    assert [instruction(s).split()[2] for s in run.seen] == [
        "PremarketBrief", "VetBatch", "DailyReview", "VetBatch", "VetBatch",
    ]  # fmt: skip
    assert [s.cwd for s in run.seen[:3]] == [sandbox(cfg)] * 3  # the real sandbox
    rows = await journal.fetch_all("SELECT id, purpose, ok FROM llm_calls ORDER BY rowid")
    assert {(r["purpose"], r["ok"]) for r in rows} == {("smoke", 1)}
    assert [r["id"] for r in rows][:3] == [
        "20261019-smoke-premarket-01-1", "20261019-smoke-vet-01-1", "20261019-smoke-review-01-1",
    ]  # fmt: skip
    budget = Budget(journal, max_calls_per_day=12, max_concurrent=1)
    assert (await budget.attempts(NOW.date()), await budget.failures(NOW.date())) == (0, 0)
    assert (cfg.root / "runtime/llm/smoke/2026-10-19/20261019-smoke-vet-01-1.input.json").is_file()
    injected = load_data().injection.symbol
    expected = f"   injection: {injected} flagged in the pack; neutral, flags: suspicious_content"
    assert run.lines.count(expected) == 2  # the brief and the vet
    assert run.has("envelope keys: duration_api_ms, duration_ms, is_error, modelUsage")
    assert run.has("the sandbox is empty after the review call")
    assert run.has("canary clean: its folder is removed")
    assert not canary_root(cfg).exists()
    assert run.has('set llm.max_tested_cli_version: "2.1.290"')


async def test_a_failed_call_fails_the_run_and_shows_the_clis_message(
    cfg: AppConfig, journal: Journal
) -> None:
    refused = envelope(None, is_error=True, result="Invalid API key · Please run /login")
    run = await smoke(
        cfg,
        journal,
        [Reply(envelope=refused, exit_code=1, stderr=b"auth failed\n")],
        options=SmokeOptions(roles=("vet",), canary=False),
    )
    assert not run.ok
    assert run.has("attempt 20261019-smoke-vet-01-1: is_error in 0.0 s; exit 1, subtype success")
    assert run.has("result: Invalid API key · Please run /login")
    assert run.has("stderr: auth failed")
    assert run.has("FAIL: vet: is_error")
    assert run.lines[-1] == "SMOKE LLM FAILED"


async def test_a_refused_call_says_so(cfg: AppConfig, journal: Journal) -> None:
    (cfg.root / "prompts" / "catalyst_vet.md").unlink()
    run = await smoke(cfg, journal, options=SmokeOptions(roles=("vet",), canary=False))
    assert not run.ok
    assert run.has("attempt 20261019-smoke-vet-01-1: prompt_missing")
    assert run.has("FAIL: vet: prompt_missing")
    assert run.seen == []


async def test_a_pack_too_big_to_send_fails_plainly(root: Path, journal: Journal) -> None:
    set_yaml(root / "config" / "settings.yaml", "llm.max_pack_bytes", 1_000)
    run = await smoke(load_config(root), journal, options=SmokeOptions(roles=("vet",)))
    assert not run.ok
    assert run.has("FAIL: vet: the vet pack needs")
    assert run.has("(llm.max_pack_bytes 1,000)")
    assert run.has("FAIL: canary: the vet pack needs")
    assert run.seen == []
    assert run.lines[-1] == "SMOKE LLM FAILED"


async def test_a_call_that_writes_into_the_sandbox_fails(cfg: AppConfig, journal: Journal) -> None:
    def littering(seen: Seen) -> Reply:
        (seen.cwd / "settings.local.json").write_text("{}", encoding="utf-8")
        return valid(seen)

    run = await smoke(cfg, journal, littering, options=SmokeOptions(roles=("vet",), canary=False))
    assert not run.ok
    assert run.has("holds settings.local.json after the vet call: every call refuses")


async def test_a_dirty_sandbox_stops_it_before_any_call(cfg: AppConfig, journal: Journal) -> None:
    sandbox(cfg).mkdir(parents=True)
    (sandbox(cfg) / "stray.txt").write_text("left by hand", encoding="utf-8")
    run = await smoke(cfg, journal)
    assert not run.ok
    assert run.seen == []
    assert run.has("holds stray.txt before any call")


async def test_only_the_chosen_roles_run(cfg: AppConfig, journal: Journal) -> None:
    run = await smoke(cfg, journal, options=smoke_options(role="review", canary=False))
    assert run.ok
    assert [instruction(s).split()[2] for s in run.seen] == ["DailyReview"]
    assert run.has("answer: 8 reviews of 8 targets, 0 process issues")
    nothing = await smoke(cfg, journal, options=smoke_options(role="none", canary=False))
    assert (nothing.ok, nothing.seen) == (True, [])


async def test_the_fallbacks_reach_the_argv(cfg: AppConfig, journal: Journal) -> None:
    options = smoke_options(
        role="vet", schema_mode="wire_safe", tools_flag="deny_all", canary=False
    )
    run = await smoke(cfg, journal, options=options)
    argv = run.seen[0].argv
    assert "--tools" not in argv
    assert argv[argv.index("--disallowedTools") + 1] == "*"
    assert argv[argv.index("--json-schema") + 1] == schema_bytes(
        wire_schema(VetBatch, "wire_safe")
    ).decode("ascii")
    assert run.has("schema_mode wire_safe, tools_flag deny_all, safe_mode on")


# ---------------------------------------------------------------- the preflight


async def test_without_a_usable_cli_nothing_runs(cfg: AppConfig, journal: Journal) -> None:
    def missing(name: str) -> tuple[str, ...]:
        raise ClaudeBinaryError(f"`{name}` not found: add its folder to PATH")

    run = await smoke(cfg, journal, command=missing)
    assert not run.ok
    assert run.seen == []
    assert run.has("FAIL: `claude` not found: add its folder to PATH")
    assert run.lines[-1] == "SMOKE LLM FAILED"


async def test_without_the_credential_nothing_runs(root: Path, journal: Journal) -> None:
    secrets = {k: v for k, v in FAKE_SECRETS.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    write_env(root, {**secrets, "CLAUDE_TOKEN_CREATED": "2026-10-01"})
    run = await smoke(load_config(root), journal)
    assert not run.ok
    assert run.has("CLAUDE_CODE_OAUTH_TOKEN is not set in .env")
    assert run.seen == []


@pytest.mark.parametrize(
    ("printed", "why"),
    [("", "printed no version"), ("2.1.100 (Claude Code)", "older than llm.min_cli_version")],
)
async def test_a_silent_or_old_cli_stops_it(
    cfg: AppConfig, journal: Journal, printed: str, why: str
) -> None:
    run = await smoke(cfg, journal, version=lambda *_: printed)
    assert not run.ok
    assert run.has(why)
    assert run.seen == []


async def test_the_version_is_read_as_the_calls_run(cfg: AppConfig, journal: Journal) -> None:
    probed: list[tuple[Any, ...]] = []

    def version(command: Any, env: Any, cwd: Path) -> str:
        probed.append((command, env, cwd))
        return "claude 2.1.290 (Claude Code)"

    run = await smoke(cfg, journal, version=version, options=SmokeOptions(roles=(), canary=False))
    assert run.ok
    ((command, env, cwd),) = probed
    assert (command, cwd) == (("stand-in",), sandbox(cfg))
    assert env["CLAUDE_CONFIG_DIR"] == str(cfg.path(cfg.settings.llm.config_dir))
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == FAKE_SECRETS["CLAUDE_CODE_OAUTH_TOKEN"]
    assert run.has("claude: stand-in, version 2.1.290")


def test_the_version_probe_returns_what_the_cli_prints(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k.upper() in ("PATH", "SYSTEMROOT")}
    prints = [sys.executable, "-c", "import sys; print('2.1.290 (Claude Code)', sys.argv[1:])"]
    assert cli_version(prints, env, tmp_path).startswith("2.1.290 (Claude Code) ['--version']")
    assert cli_version([sys.executable, "-c", "raise SystemExit(3)"], env, tmp_path) == ""
    assert cli_version([str(tmp_path / "no-such.exe")], env, tmp_path) == ""


async def test_the_config_dir_report(cfg: AppConfig, journal: Journal) -> None:
    folder = cfg.path(cfg.settings.llm.config_dir)
    folder.mkdir(parents=True)
    (folder / ".claude.json").write_text("{}", encoding="utf-8")
    run = await smoke(cfg, journal, options=SmokeOptions(roles=(), canary=False))
    assert run.has(".claude.json is there: CLAUDE_CONFIG_DIR holds it (D-021)")
    assert run.has("it holds: .claude.json")


# ---------------------------------------------------------------- the canary


@dataclass
class LeakyCli:
    """A stand-in for a CLI that isn't isolated: it reads the instructions in its working
    directory and above it, the settings and the ``.mcp.json`` in its working directory, and
    ``settings.local.json`` at the git root (POSIX's rule). ``leaks`` names what it acts on;
    ``unisolated_only`` keeps it isolated while the isolation flags are there."""

    leaks: frozenset[str]
    unisolated_only: bool = False
    git_root: bool = False
    calls: list[Path] = field(default_factory=list)

    def __call__(self, seen: Seen) -> Reply:
        self.calls.append(seen.cwd)
        leaks = self.leaks
        if self.unisolated_only and "--setting-sources" in seen.argv:
            leaks = frozenset()
        answer = answer_for(seen.pack())
        model = "claude-sonnet-5"
        settings = [seen.cwd / ".claude" / "settings.json"]
        root = next((p for p in (seen.cwd, *seen.cwd.parents) if (p / ".git").exists()), None)
        if self.git_root and root is not None:
            settings.append(root / ".claude" / "settings.local.json")
        if "instructions" in leaks:
            words = [w for folder in (seen.cwd, *seen.cwd.parents) for w in _code_words(folder)]
            for a in answer["assessments"]:
                a["reason"] = f"{' '.join(words)} {a['reason']}"
        for path in (p for p in settings if p.is_file()):
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if "hooks" in leaks:
                for groups in loaded["hooks"].values():
                    for hook in groups[0]["hooks"]:
                        marker = Path(hook["command"].split("'")[1])
                        with marker.open("a", encoding="utf-8") as f:
                            f.write("ran\n")
            if "env" in leaks and "env" in loaded:
                model = loaded["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"]
        mcp = seen.cwd / ".mcp.json"
        if "mcp" in leaks and mcp.is_file():
            server = json.loads(mcp.read_text(encoding="utf-8"))["mcpServers"]["trader-canary"]
            subprocess.run([server["command"], *server["args"]], check=True, timeout=30)
        return Reply(envelope=envelope(answer, model=model))


def _code_words(folder: Path) -> list[str]:
    found = []
    for name in ("CLAUDE.md", "CLAUDE.local.md"):
        path = folder / name
        if path.is_file():
            found += TOKEN.findall(path.read_text(encoding="utf-8"))
    return found


CANARY_ONLY = SmokeOptions(roles=())


async def test_the_canary_plants_every_source_and_a_clean_cli_passes(
    cfg: AppConfig, journal: Journal
) -> None:
    planted: dict[str, Any] = {}

    def look(seen: Seen) -> Reply:
        if not planted:  # the parent half: everything is planted by now
            isolated = seen.cwd.parent
            planted["files"] = sorted(
                p.relative_to(isolated).as_posix() for p in isolated.rglob("*") if p.is_file()
            )
            planted["settings"] = json.loads(
                (isolated / "inside" / ".claude" / "settings.json").read_text(encoding="utf-8")
            )
            planted["sandbox"] = sorted(seen.cwd.iterdir())
        return valid(seen)

    run = await smoke(cfg, journal, look, options=CANARY_ONLY)
    assert run.ok, run.lines
    root = canary_root(cfg)
    assert [s.cwd for s in run.seen] == [
        root / "isolated" / "sandbox",
        root / "isolated" / "inside",
    ]
    sources = [
        "CLAUDE.local.md",
        "CLAUDE.md",
        ".claude/settings.json",
        ".claude/settings.local.json",
    ]
    assert set(planted["files"]) == {
        *sources,
        ".mcp.json",
        *(f"inside/{s}" for s in [*sources, ".mcp.json"]),
    }
    assert planted["sandbox"] == []
    settings = planted["settings"]
    assert sorted(settings["hooks"]) == sorted(HOOK_EVENTS)
    assert set(settings["env"]) == {
        "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    }  # fmt: skip
    assert settings["apiKeyHelper"] == "echo canary-key-inside"
    assert set(settings["env"].values()) == {"canary-model-inside"}  # no code word in either
    assert settings["enableAllProjectMcpServers"] is True
    assert run.has("parent half: clean")
    assert run.has("in-sandbox half: ok in 0.0 s; exit 0")
    assert run.has("in-sandbox half: clean")
    assert run.has("git isn't on PATH")
    assert not root.exists()  # a clean canary leaves nothing behind


LEAKS = [
    # (what leaks, what the run must say)
    ({"instructions"}, "canary, parent half: parent's instructions reached it"),
    ({"instructions"}, "canary, in-sandbox half: inside's instructions reached it"),
    ({"hooks"}, "canary, in-sandbox half: a hook ran (hook-inside.txt)"),
    ({"mcp"}, "canary, in-sandbox half: an MCP server started (mcp-inside.txt)"),
    ({"env"}, "canary, in-sandbox half: a planted env block chose its model"),
]


@pytest.mark.parametrize(
    ("leaks", "said"), LEAKS, ids=[f"{sorted(x[0])}-{i}" for i, x in enumerate(LEAKS)]
)
async def test_each_leak_is_caught(
    cfg: AppConfig, journal: Journal, leaks: set[str], said: str
) -> None:
    run = await smoke(cfg, journal, LeakyCli(frozenset(leaks)), options=CANARY_ONLY)
    assert not run.ok
    assert run.has(f"FAIL: {said}")
    assert run.has("the canary's folder stays for you to look at")
    assert canary_root(cfg).exists()


async def test_a_cli_reading_the_git_roots_local_settings_is_caught(
    cfg: AppConfig, journal: Journal
) -> None:
    """On POSIX the CLI reads ``settings.local.json`` from the git root, a parent of the
    sandbox: the canary makes its folders git roots to catch it."""

    def git(folder: Path) -> bool:
        (folder / ".git").mkdir()
        (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        return True

    cli = LeakyCli(frozenset({"hooks"}), git_root=True)

    def cli_running_git(seen: Seen) -> Reply:
        root = next(p for p in (seen.cwd, *seen.cwd.parents) if (p / ".git").is_dir())
        (root / ".git" / "index").write_bytes(b"DIRC")  # as `git status` would refresh it
        return cli(seen)

    run = await smoke(cfg, journal, cli_running_git, options=CANARY_ONLY, git=git)
    fails = [line for line in run.lines if line.startswith("   FAIL")]
    # the in-sandbox half runs the git root's hooks too: a marker is reported once
    assert [line for line in fails if "hook-parent.txt" in line] == [
        "   FAIL: canary, parent half: a hook ran (hook-parent.txt)"
    ]
    assert "   FAIL: canary, in-sandbox half: a hook ran (hook-inside.txt)" in fails
    assert not run.has("left files behind")  # what git writes in .git is no leak
    assert not run.has("git isn't on PATH")


async def test_files_a_call_leaves_are_caught_once(cfg: AppConfig, journal: Journal) -> None:
    def littering(seen: Seen) -> Reply:
        (seen.cwd / "notes.md").write_text("x", encoding="utf-8")
        return valid(seen)

    run = await smoke(cfg, journal, littering, options=CANARY_ONLY)
    fails = [line for line in run.lines if line.startswith("   FAIL") and "left files" in line]
    assert fails == [
        "   FAIL: canary, parent half: it left files behind: isolated/sandbox/notes.md",
        "   FAIL: canary, in-sandbox half: it left files behind: isolated/inside/notes.md",
    ]


async def test_the_control_shows_each_detector_fires(cfg: AppConfig, journal: Journal) -> None:
    cli = LeakyCli(frozenset({"instructions", "hooks", "mcp"}), unisolated_only=True)
    options = SmokeOptions(roles=(), canary_control=True)
    run = await smoke(cfg, journal, cli, options=options)
    assert run.ok, run.lines
    assert run.has("parent half: clean")
    assert run.has("in-sandbox half: clean")
    assert run.has("control: every detector fired (instructions, hooks, the MCP server)")
    control = run.seen[-1]
    assert control.cwd == canary_root(cfg) / "control"
    for flag in ("--setting-sources", "--safe-mode", "--strict-mcp-config", "--mcp-config"):
        assert flag not in control.argv
    assert control.argv[control.argv.index("--tools") + 1] == ""
    assert control.env["CLAUDE_CONFIG_DIR"] == str(canary_root(cfg) / "control-config")


async def test_a_control_that_sees_nothing_fails(cfg: AppConfig, journal: Journal) -> None:
    run = await smoke(cfg, journal, options=SmokeOptions(roles=(), canary_control=True))
    assert not run.ok
    assert run.has(
        "FAIL: control: the canary can't vouch for what never fired: instructions (no code word "
        "in the answer); hooks (no marker); the MCP server (no marker)"
    )


async def test_a_failed_canary_call_shows_why(cfg: AppConfig, journal: Journal) -> None:
    turns = envelope(None, subtype="error_max_turns", is_error=True, errors=["max turns"])
    token = FAKE_SECRETS["CLAUDE_CODE_OAUTH_TOKEN"]
    broke = Reply(stdout=b"", exit_code=1, stderr=f"Error: it broke (token {token})\n".encode())
    run = await smoke(
        cfg, journal, [Reply(envelope=turns, exit_code=1), broke], options=CANARY_ONLY
    )
    assert run.has("FAIL: canary, parent half: the call failed (max_turns)")
    assert run.has("in-sandbox half: nonzero_exit in 0.0 s; exit 1")
    assert run.has("stderr: Error: it broke (token ***CLAUDE_CODE_OAUTH_TOKEN***)")
    assert not run.has(token)
    record = (canary_root(cfg) / "canary-inside.output.json").read_text(encoding="utf-8")
    assert json.loads(record)["attempt"] == "canary-inside"
    assert token not in record


async def test_a_planted_model_or_key_in_an_error_is_caught(
    cfg: AppConfig, journal: Journal
) -> None:
    """A leaked env block or apiKeyHelper fails the call; its error may name what leaked."""
    said = b"API Error: 404 model canary-model-parent not found; key canary-key-parent rejected"
    run = await smoke(
        cfg, journal, lambda _seen: Reply(stdout=said, exit_code=1), options=CANARY_ONLY
    )
    assert run.has(
        "parent half: a planted env block reached it (its model's name is in its output)"
    )
    assert run.has("parent half: a planted apiKeyHelper reached it (its key is in its output)")


async def test_a_canary_that_cant_be_planted_fails_plainly(root: Path, journal: Journal) -> None:
    set_yaml(root / "config" / "settings.yaml", "llm.sandbox_dir", "runtime/it's/llm_sandbox")
    run = await smoke(load_config(root), journal, options=CANARY_ONLY)
    assert not run.ok
    assert run.has("FAIL: canary: not planted: the canary's folder")


def test_without_isolation_drops_only_the_isolation_flags() -> None:
    argv = build_argv(
        ("cli",),
        instruction="Produce it.",
        schema="{}",
        model="sonnet",
        effort="low",
        system_prompt_file=Path("prompt.md"),
        max_turns=3,
        safe_mode=False,
    )
    assert without_isolation(argv) == [
        "cli", "-p", "Produce it.", "--output-format", "json", "--json-schema", "{}",
        "--model", "sonnet", "--effort", "low", "--system-prompt-file", "prompt.md",
        "--tools", "", "--disallowedTools", "mcp__*", "--permission-mode", "dontAsk",
        "--permission-prompts", "none", "--max-turns", "3", "--no-session-persistence",
    ]  # fmt: skip


def _git_bash() -> str | None:
    found = shutil.which("bash")
    if found is None or (WINDOWS and "system32" in found.lower()):  # WSL's launcher
        return None
    return found


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.parametrize(
    ("shell", "args"),
    [(_git_bash, ["-c"]), (_powershell, ["-NoProfile", "-NonInteractive", "-Command"])],
    ids=["bash", "powershell"],
)
def test_a_planted_hook_runs_in_the_shells_the_cli_uses(
    tmp_path: Path, shell: Callable[[], str | None], args: list[str]
) -> None:
    """The CLI runs a hook through Git Bash (sh on POSIX), or PowerShell without Git Bash: the
    planted command leaves its marker in each."""
    found = shell()
    if found is None:
        pytest.skip("this shell isn't installed")
    canary = Canary(tmp_path / "canary", token=lambda: "t", python=sys.executable)
    canary.plant(lambda _folder: False)
    settings = json.loads((canary.inside / ".claude" / "settings.json").read_text("utf-8"))
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    subprocess.run([found, *args, command], check=True, timeout=60, stdin=subprocess.DEVNULL)
    assert canary.mark("inside", "hook").stat().st_size > 0


def test_a_planted_mcp_server_leaves_its_marker(tmp_path: Path) -> None:
    canary = Canary(tmp_path / "canary", token=lambda: "t", python=sys.executable)
    canary.plant(lambda _folder: False)
    config = json.loads((canary.isolated / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["trader-canary"]
    subprocess.run([server["command"], *server["args"]], check=True, timeout=30)
    assert canary.mark("parent", "mcp").read_text(encoding="utf-8") == "started"


# ---------------------------------------------------------------- the kill test


def fake_cli(scenario: str) -> dict[str, Any]:
    """The kill test on scripts/fake_claude.py, through the real transport."""
    return {
        "command": lambda _name: (sys.executable, str(FAKE), "--scenario", scenario),
        "transport": lambda grace, on_spawn: ClaudeCliTransport(
            kill_grace_s=grace, on_spawn=on_spawn
        ),
    }


KILL_ONLY = SmokeOptions(roles=(), canary=False, kill_test=True)


@pytest.mark.parametrize(
    ("scenario", "stopped"),
    [
        ("slow", "the interrupt"),
        ("slow_stubborn", "a kill of its process tree" if WINDOWS else "SIGTERM"),
    ],
)
async def test_the_kill_test_reports_the_stop(
    cfg: AppConfig, journal: Journal, scenario: str, stopped: str
) -> None:
    run = await smoke(cfg, journal, options=KILL_ONLY, **fake_cli(scenario))
    assert run.ok, run.lines
    assert run.has(f"stopped by {stopped}")
    assert run.has("attempt 20261019-smoke-vet-01-1: timeout in")
    assert run.has("nothing it started outlived the stop")
    if WINDOWS:  # the venv's python.exe starts the interpreter, a process the watch sees
        assert run.has("processes the CLI started: python.exe (")
    rows = await journal.fetch_all("SELECT purpose, failure, timed_out FROM llm_calls")
    assert [tuple(r) for r in rows] == [("smoke", "timeout", 1)]


async def test_a_process_left_running_fails_the_kill_test(cfg: AppConfig, journal: Journal) -> None:
    run = await smoke(cfg, journal, options=KILL_ONLY, **fake_cli("slow_orphan"))
    orphans = [int(m) for line in run.lines for m in re.findall(r"fake_claude: orphan (\d+)", line)]
    try:
        assert not run.ok
        assert run.has("FAIL: kill test: still running after the stop:")
        if WINDOWS:
            assert run.has(f"({orphans[0]})")
        assert run.lines[-1] == "SMOKE LLM FAILED"
    finally:
        for pid in orphans:
            _end(pid)


async def test_a_call_that_ends_in_time_fails_the_kill_test(
    cfg: AppConfig, journal: Journal
) -> None:
    run = await smoke(cfg, journal, options=KILL_ONLY)
    assert not run.ok
    assert run.has("FAIL: kill test: the call ended as ok inside its 3 s")
    assert run.has("processes the CLI started: none seen")


def _end(pid: int) -> None:
    with contextlib.suppress(OSError):  # gone already
        os.kill(pid, signal.SIGTERM)


# ---------------------------------------------------------------- the process watch


def test_descendants_follow_the_parent_links() -> None:
    rows = [(0, 0, "Idle"), (4, 0, "System"), (10, 1, "a"), (11, 10, "b"), (12, 11, "c"),
            (13, 99, "x"), (20, 21, "loop"), (21, 20, "loop")]  # fmt: skip
    assert descendants(10, rows) == {11: "b", 12: "c"}
    assert descendants(0, rows) == {4: "System"}  # the idle process isn't its own child
    assert descendants(13, rows) == {}
    assert descendants(20, rows) == {21: "loop"}  # a cycle of reused ids ends
    # a listing that names a pid twice (taken while an id was reused) can't loop forever
    assert descendants(30, [(31, 30, "a"), (32, 31, "b"), (31, 32, "a")]) == {31: "a", 32: "b"}


@dataclass
class FakeProc:
    pid: int
    returncode: int | None = None


async def test_the_watch_keeps_what_it_saw_and_reports_what_outlives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watching, "group_alive", lambda _pgid: False)
    rows = [(1, 0, "init"), (50, 1, "old")]
    watch = DescendantWatch(listing=lambda: list(rows), poll_s=0.01)
    child = FakeProc(100)
    rows += [(100, 1, "claude.exe"), (101, 100, "node.exe"), (102, 101, "rg.exe")]
    rows.append((50, 100, "old"))  # names the child as parent, but ran before it: not its own
    watch.started(child)  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    rows[:] = [(1, 0, "init"), (50, 1, "old"), (101, 100, "node.exe")]  # 102 ended, 101 lives
    child.returncode = 1
    await asyncio.sleep(0.05)
    assert watch.seen == {101: "node.exe", 102: "rg.exe"}
    rows.append((102, 1, "notepad.exe"))  # 102's id, reused by another program: not a survivor
    assert watch.survivors() == ["node.exe (101)"]


def test_a_watch_that_never_saw_a_child_reports_nothing() -> None:
    assert DescendantWatch(listing=list).survivors() == []


async def test_a_process_started_since_the_last_look_is_still_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child can exit between two looks: the report looks once more first."""
    monkeypatch.setattr(watching, "group_alive", lambda _pgid: False)
    rows = [(1, 0, "init")]
    watch = DescendantWatch(listing=lambda: list(rows), poll_s=60)
    child = FakeProc(100)
    rows.append((100, 1, "claude.exe"))
    watch.started(child)  # type: ignore[arg-type]
    await asyncio.sleep(0.05)  # the first look, then a long wait
    rows.append((101, 100, "node.exe"))
    child.returncode = 0
    assert watch.survivors() == ["node.exe (101)"]


async def test_the_watch_sees_a_real_grandchild_outlive_its_parent() -> None:
    script = (
        "import subprocess, sys, time; "
        "python = getattr(sys, '_base_executable', sys.executable); "
        "quiet = subprocess.DEVNULL; "
        "kid = subprocess.Popen([python, '-c', 'import time; time.sleep(60)'], stdout=quiet); "
        "print(kid.pid, flush=True); time.sleep(60)"
    )
    group: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if WINDOWS
        else {"start_new_session": True}
    )
    watch = DescendantWatch(poll_s=0.05)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE, **group
    )
    grandchild = 0
    try:
        watch.started(proc)
        assert proc.stdout is not None
        grandchild = int(await asyncio.wait_for(proc.stdout.readline(), 30))
        await asyncio.sleep(0.3)  # the watch looks a few times
        proc.kill()
        for _ in range(200):  # its return code, not wait(): wait() also waits for the pipes
            if proc.returncode is not None:
                break
            await asyncio.sleep(0.05)
        left = watch.survivors()
        assert left
        if WINDOWS:
            assert any(f"({grandchild})" in item for item in left)
    finally:
        if grandchild:
            _end(grandchild)
        if proc.returncode is None:
            proc.kill()


# ---------------------------------------------------------------- what it prints


def test_text_from_the_cli_is_printed_safely() -> None:
    escape = chr(27)
    out = {
        "envelope": {"result": f"{escape}[31mred{escape}[0m\nand more", "errors": ["a", "b"]},
        "errors": ["pack_id: wrong"],
        "stopped": "kill",
        "timed_out": True,
        "stderr_tail": "one\n\ntwo\nthree\nfour\n",
    }
    lines = problem_lines(out)
    assert all(escape not in line for line in lines)
    assert lines == [
        "result: [31mred [0m and more",
        "errors: a; b",
        "validation: pack_id: wrong",
        "stopped: kill (timed out: yes)",
        "stderr: two",
        "stderr: three",
        "stderr: four",
    ]
    assert problem_lines({"stdout": "x" * 500})[0] == f"stdout: {'x' * 197}..."


def test_an_answer_is_shown_by_its_counts(cfg: AppConfig) -> None:
    pack = encode(smoke_packs(cfg.settings, cfg.risk).vet)
    answer = answer_for(json.loads(pack.data))
    injected = load_data().injection.symbol
    answer["assessments"] = [a for a in answer["assessments"] if a["symbol"] != injected]
    answer["assessments"][0] = {**answer["assessments"][0], "verdict": "veto"}
    lines = answer_lines(VetBatch.model_validate(answer, context={"pack_id": pack.pack_id}), pack)
    assert lines == [
        "answer: 24 assessments of 25 candidates (neutral 23, veto 1)",
        f"not assessed: {injected}",
        f"injection: {injected} flagged in the pack; the answer doesn't assess it",
    ]
    review = encode(smoke_packs(cfg.settings, cfg.risk).review)
    body = answer_for(json.loads(review.data))
    body["signal_reviews"] = body["signal_reviews"][1:]
    reviewed = DailyReview.model_validate(body, context={"pack_id": review.pack_id})
    assert answer_lines(reviewed, review)[:2] == [
        "answer: 7 reviews of 8 targets, 0 process issues, 0 lesson proposals, 0 notes for you",
        f"not reviewed: {review.kept[0]}",
    ]


# ---------------------------------------------------------------- the command


def test_the_choices_are_checked() -> None:
    assert smoke_options().roles == ROLES
    assert smoke_options(role=" Vet ").roles == ("vet",)
    for bad in (
        {"role": "brief"},
        {"schema_mode": "loose"},
        {"tools_flag": "none"},
        {"canary": False, "control": True},
    ):
        with pytest.raises(ValueError, match="--"):
            smoke_options(**bad)


def test_the_command_fails_plainly_without_the_cli(root: Path) -> None:
    set_yaml(root / "config" / "settings.yaml", "llm.claude_bin", "no-such-cli-here")
    result = CliRunner().invoke(app, ["--root", str(root), "smoke", "llm", "--role", "vet"])
    assert result.exit_code == 1
    assert "`no-such-cli-here` not found" in result.output
    assert result.output.strip().splitlines()[-1] == "SMOKE LLM FAILED"


def test_the_command_waits_for_no_session(root: Path) -> None:
    with RunLock(root / "runtime" / "run-day.lock"):
        result = CliRunner().invoke(app, ["--root", str(root), "smoke", "llm"])
    assert result.exit_code == 1
    assert "another `trader run-day` is running" in result.output


def test_the_command_refuses_a_bad_choice(root: Path) -> None:
    result = CliRunner().invoke(app, ["--root", str(root), "smoke", "llm", "--role", "brief"])
    assert result.exit_code == 1
    assert "--role must be all, none, premarket, vet or review" in result.output
