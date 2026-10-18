"""The LLM runner (ARCHITECTURE.md §4.7, §7; the Phase 4 plan's decisions 1 and 4-7; D-064).

``LlmCalls.run(role, pack, deadline=...)`` makes one logical call: an attempt, and at most one
repair. It never raises for a failure: whatever goes wrong comes back as ``ok=False`` with a
``failure`` code, and only a cancellation goes on.

Before each attempt it refuses, leaving no row, when:

- the deadline leaves less than the role's ``min_attempt_s``;
- a trading call finds the day's ``max_calls_per_day`` attempts spent (checked in the insert).

It also refuses when the pack is over ``max_pack_bytes``, the role's prompt file is missing, or
the sandbox isn't empty (every call runs in an empty folder, A§7.1, and this fails closed). These
come back on every call until a human fixes their cause, so each is journaled as an attempt that
failed without a CLI run: it counts toward Book B's degradation, not toward the cap.

Each attempt then:

1. inserts its ``llm_calls`` row, which also takes its id, and writes ``<attempt>.input.json``,
   the exact stdin;
2. runs the transport, its timeout min(the role's ``timeout_s``, the time left to the deadline);
3. is classified (``classify``): the process, then the envelope, then the answer, filtered to
   the pack's members, clamped and validated with the pack's ``pack_id`` (A§7.3);
4. writes ``<attempt>.output.json``, redacted: the envelope or raw stdout, the answer as the
   runner returns it, stderr's tail, the exit code, and the argv with the schema shown by its
   hash. Then it completes its row, and a good final answer's ``persist`` runs in that same
   transaction.

An artifact that can't be written fails its attempt (``artifact_failed``): nothing the model
says may lead to a trade without it on disk (CLAUDE.md invariant 8). A cancellation closes the
attempt as abandoned; any other error inside the runner closes it as ``internal_error``.

Only an output problem gets the one repair, and only while the deadline leaves
``min_attempt_s``. The repair sends the same pack with the errors appended to the instruction:
pydantic's messages, located by our own field names and list positions only, never the model's
text, a key it invented, or the expected ``pack_id``.

A good answer then has its assessments de-duplicated by symbol (the more confident one stays),
and a candidate whose pack news was flagged gets ``suspicious_content`` (D-062, D-063).
"""

import asyncio
import dataclasses
import functools
import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, Self

import structlog
from pydantic import BaseModel, ValidationError

from trader.config import AppConfig
from trader.core.clock import Clock, fmt_iso
from trader.core.redact import redact_text
from trader.llm.budget import ABANDONED, CLI_RUNS, PERSISTENT_REFUSALS, Budget
from trader.llm.claude_cli import (
    ClaudeBinaryError,
    ToolsFlag,
    Transport,
    TransportResult,
    build_argv,
    build_claude_env,
    resolve_claude,
)
from trader.llm.context import EncodedPack, Pack, Role
from trader.llm.context import encode as encode_pack
from trader.llm.roles import RoleSpec, prompt_sha256, role_specs
from trader.llm.schemas import (
    SchemaMode,
    SymbolAssessment,
    clamp_to_schema,
    schema_bytes,
    wire_schema,
)
from trader.memory.journal import Journal

log = structlog.get_logger(__name__)

Purpose = Literal["trading", "smoke"]
Persist = Callable[[sqlite3.Connection, BaseModel, str], None]

# Output problems: the one repair may fix them. A process, envelope or limit failure won't be.
REPAIRABLE = frozenset(
    {
        "invalid_json",
        "no_structured_output",
        "structured_retries",
        "schema_invalid",
        "wrong_pack_id",
    }
)
_ERROR_SUBTYPES = {
    "error_max_turns": "max_turns",
    "error_during_execution": "execution_error",
    "error_max_budget_usd": "budget_usd",
    "error_max_structured_output_retries": "structured_retries",
}
_CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
REPAIR_NOTE = "Your previous answer did not validate. Fix these errors and answer again:"
MAX_REPAIR_ERRORS = 10
MAX_RAW_STDOUT = 64 * 1024  # of a stdout that isn't a JSON result, kept in the artifact
CUT_MARGIN = 512  # characters dropped at a cut edge: more than any secret's length
MAX_FLAGS = int(SymbolAssessment.model_json_schema()["properties"]["risk_flags"]["maxItems"])
CLEANUP_S = 2.0  # how long a cancelled attempt's last journal write is waited for
_RENAME_RETRY_S = (0.05, 0.1, 0.2)  # a scanner can hold a new file for a moment on Windows
_KEY_LIKE = re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")


class LlmUnavailableError(RuntimeError):
    """The settings leave no way to call the model: a missing credential or executable."""


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    ok: bool
    output: T | None
    # The attempt whose result stands: an llm_calls.id, what every llm_call_id column holds.
    # None when the call was refused before any attempt.
    call_id: str | None
    duration_ms: int
    error: str | None
    failure: str | None = None  # the code: timeout, schema_invalid, no_time, ...
    attempts: tuple[str, ...] = ()  # every attempt's id, the first try first
    logical_id: str | None = None  # the call's own id, ``YYYYMMDD-role-NN``


class LLMClient(Protocol):
    async def run(
        self,
        role: Role,
        pack: EncodedPack,
        *,
        deadline: datetime,
        purpose: Purpose = "trading",
        persist: Persist | None = None,
    ) -> LLMResult[Any]: ...


# ---------------------------------------------------------------- classifying an attempt


@dataclass(frozen=True)
class Outcome:
    failure: str | None
    output: BaseModel | None = None
    envelope: Mapping[str, Any] | None = None
    errors: tuple[str, ...] = ()
    clamped: int = 0
    dropped: int = 0
    validated: bool | None = None  # None: the answer never reached validation

    @property
    def ok(self) -> bool:
        return self.failure is None


def classify(
    result: TransportResult, spec: RoleSpec, pack: EncodedPack, schema: Mapping[str, Any]
) -> Outcome:
    """What an attempt's result means. ``schema`` is the role's ``full`` wire schema, the
    clamp's guide whichever mode was sent (D-061)."""
    if result.timed_out:
        return Outcome("timeout")
    if result.overflow:
        return Outcome("output_too_large")
    envelope = _envelope(result.stdout)
    if envelope is None:
        if result.exit_code != 0:
            return Outcome("nonzero_exit")
        return Outcome("invalid_json", errors=("The output was not a JSON result.",))
    subtype = envelope.get("subtype")
    if subtype in _ERROR_SUBTYPES:
        failure = _ERROR_SUBTYPES[subtype]
        errors = ("No answer matched the schema.",) if failure == "structured_retries" else ()
        return Outcome(failure, envelope=envelope, errors=errors)
    if envelope.get("is_error") is True:
        return Outcome("is_error", envelope=envelope)
    if result.exit_code != 0:
        return Outcome("nonzero_exit", envelope=envelope)
    if subtype != "success":
        return Outcome("invalid_envelope", envelope=envelope)
    structured = envelope.get("structured_output")
    if structured is None:
        errors = ("The answer had no structured output.",)
        return Outcome("no_structured_output", envelope=envelope, errors=errors)
    data, dropped = _keep_members(structured, spec, pack.kept)
    data, clamped = clamp_to_schema(data, schema)
    try:
        output = spec.output.model_validate(data, context={"pack_id": pack.pack_id})
    except ValidationError as exc:
        wrong_id = any(e["loc"] == ("pack_id",) for e in exc.errors())
        return Outcome(
            "wrong_pack_id" if wrong_id else "schema_invalid",
            envelope=envelope,
            errors=_errors(exc, field_names(spec.output)),
            clamped=clamped,
            dropped=dropped,
            validated=False,
        )
    settled = _settle(output, spec, pack)
    return Outcome(
        None, output=settled, envelope=envelope, clamped=clamped, dropped=dropped, validated=True
    )


def _envelope(stdout: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("type") == "result" else None


def _keep_members(data: Any, spec: RoleSpec, kept: Collection[str]) -> tuple[Any, int]:
    """Drop the entries naming anything but the pack's candidates or targets, before the clamp,
    so a stray can't push a real one out (A§7.3). An entry too malformed to name one is left to
    validation."""
    if not isinstance(data, dict) or not isinstance(data.get(spec.members), list):
        return data, 0
    allowed = set(kept)
    items = data[spec.members]
    keep = [
        item
        for item in items
        if not (
            isinstance(item, dict)
            and isinstance(item.get(spec.member_key), str)
            and item[spec.member_key] not in allowed
        )
    ]
    return {**data, spec.members: keep}, len(items) - len(keep)


@functools.cache
def field_names(model: type[BaseModel]) -> frozenset[str]:
    """Every property name ``model``'s schema declares, its nested models' included: the only
    names a repair's instruction may show."""
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                names.update(properties)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(wire_schema(model, "full"))
    return frozenset(names)


def _errors(exc: ValidationError, fields: Collection[str]) -> tuple[str, ...]:
    """Validation errors for the repair's instruction: pydantic's messages, located only by our
    own field names (``fields``) and list positions, anything else shown as ``?``. A key the
    model invented, or a value it gave, never comes back in a trusted instruction."""
    found = exc.errors(include_input=False, include_url=False)
    lines = []
    for e in found[:MAX_REPAIR_ERRORS]:
        loc = list(e["loc"])
        if e["type"] == "extra_forbidden" and loc:
            loc[-1] = "?"  # the key is the model's, even when it spells one of ours
        where = ".".join(str(p) if isinstance(p, int) or p in fields else "?" for p in loc)
        lines.append(f"{where or '(the answer)'}: {e['msg']}")
    if len(found) > MAX_REPAIR_ERRORS:
        lines.append(f"and {len(found) - MAX_REPAIR_ERRORS} more")
    return tuple(lines)


def _settle(output: BaseModel, spec: RoleSpec, pack: EncodedPack) -> BaseModel:
    """A§7.3 step 3: one assessment per symbol, the more confident (ties: the first); and
    ``suspicious_content`` for a candidate whose pack news was flagged."""
    if spec.members != "assessments":
        return output
    best: dict[str, SymbolAssessment] = {}
    for a in getattr(output, spec.members):
        if a.symbol not in best or a.confidence > best[a.symbol].confidence:
            best[a.symbol] = a
    settled = [flag_suspicious(a) if a.symbol in pack.flagged else a for a in best.values()]
    return output.model_copy(update={spec.members: settled})


def flag_suspicious(a: SymbolAssessment) -> SymbolAssessment:
    """``a`` with ``suspicious_content``: flags de-duplicated in order, and at the cap the
    model's last one gives way, so a stored verdict always validates (D-063)."""
    flags = list(dict.fromkeys(a.risk_flags))
    if "suspicious_content" not in flags:
        flags = [*flags[: MAX_FLAGS - 1], "suspicious_content"]
    return a.model_copy(update={"risk_flags": flags})


def repair_instruction(spec: RoleSpec, errors: Sequence[str]) -> str:
    return "\n".join([spec.instruction, "", REPAIR_NOTE, *(f"- {e}" for e in errors)])


# ---------------------------------------------------------------- the runner


@dataclass(frozen=True)
class _Schema:
    text: str  # what --json-schema gets
    sha256: str
    full: Mapping[str, Any]  # the clamp's guide


@dataclass(frozen=True)
class _Attempt:
    id: str
    logical_id: str
    outcome: Outcome
    duration_ms: int


_NO_RUN = TransportResult(exit_code=None, stdout=b"", stderr_tail=b"", duration_s=0.0)


class LlmCalls:
    """The one way trading code calls the model (CLAUDE.md invariant 5)."""

    def __init__(
        self,
        *,
        journal: Journal,
        clock: Clock,
        transport: Transport,
        command: Sequence[str],
        roles: Mapping[Role, RoleSpec],
        env: Mapping[str, str],
        prompts_dir: Path,
        sandbox_dir: Path,
        artifacts_dir: Path,
        budget: Budget,
        schema_mode: SchemaMode = "full",
        tools_flag: ToolsFlag = "empty",
        safe_mode: bool = True,
        max_pack_bytes: int = 300_000,
        cli_version: str | None = None,
    ) -> None:
        self._journal, self._clock, self._transport = journal, clock, transport
        self._command, self._roles, self._env = tuple(command), dict(roles), dict(env)
        self._prompts, self._sandbox, self._artifacts = prompts_dir, sandbox_dir, artifacts_dir
        self.budget = budget
        self._schema_mode, self._tools_flag, self._safe_mode = schema_mode, tools_flag, safe_mode
        self._max_pack_bytes, self._cli_version = max_pack_bytes, cli_version
        # what the artifacts never show: the credential the calls run with
        self._secrets = {name: value for name in _CREDENTIALS if (value := self._env.get(name))}
        self._schemas = {role: _schema(spec, schema_mode) for role, spec in self._roles.items()}

    @classmethod
    def from_config(
        cls,
        cfg: AppConfig,
        *,
        journal: Journal,
        clock: Clock,
        transport: Transport,
        command: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
        sandbox_dir: Path | None = None,
        artifacts_dir: Path | None = None,
        cli_version: str | None = None,
        parent_env: Mapping[str, str] | None = None,
    ) -> Self:
        """The runner the settings describe. Raises ``LlmUnavailableError`` without the
        credential or the executable.

        - ``command``: by default ``llm.claude_bin``, resolved on PATH (``resolve_claude``).
        - ``env``: by default built from the provider's credential and ``parent_env``. Provider
          ``anthropic_api`` runs the same CLI with ``ANTHROPIC_API_KEY`` instead of the
          subscription token (plan decision 3). A transport that never starts the CLI (a replay,
          ``--sim``) passes its own, with no credential in it.
        - ``sandbox_dir``, ``artifacts_dir``: by default the settings'. A replay or a sim day
          passes its own, or it would write over the live day's artifacts (ids repeat).
        """
        llm = cfg.settings.llm
        if env is None:
            env = _credentialed_env(cfg, os.environ if parent_env is None else parent_env)
        if command is None:
            try:
                command = (str(resolve_claude(llm.claude_bin)),)
            except ClaudeBinaryError as exc:
                raise LlmUnavailableError(str(exc)) from exc
        return cls(
            journal=journal,
            clock=clock,
            transport=transport,
            command=command,
            roles=role_specs(cfg.settings),
            env=env,
            prompts_dir=cfg.root / "prompts",
            sandbox_dir=cfg.path(llm.sandbox_dir) if sandbox_dir is None else sandbox_dir,
            artifacts_dir=(
                cfg.path(cfg.settings.storage.llm_artifacts_dir)
                if artifacts_dir is None
                else artifacts_dir
            ),
            budget=Budget(
                journal, max_calls_per_day=llm.max_calls_per_day, max_concurrent=llm.max_concurrent
            ),
            schema_mode=llm.schema_mode,
            tools_flag=llm.tools_flag,
            safe_mode=llm.safe_mode,
            max_pack_bytes=llm.max_pack_bytes,
            cli_version=cli_version,
        )

    def encode(self, pack: Pack) -> EncodedPack:
        """``pack`` as stdin, trimmed to ``llm.max_pack_bytes``: the cap ``run`` holds it to."""
        return encode_pack(pack, max_bytes=self._max_pack_bytes)

    async def run(
        self,
        role: Role,
        pack: EncodedPack,
        *,
        deadline: datetime,
        purpose: Purpose = "trading",
        persist: Persist | None = None,
    ) -> LLMResult[Any]:
        """One logical call (the module's docstring). ``persist(conn, output, attempt_id)``
        runs in the transaction that completes a good answer's attempt, so what the caller
        stores from it commits with the attempt's row, or neither does; if it raises, the
        attempt fails as ``internal_error``."""
        spec = self._roles[role]
        day = self._clock.now().date()
        logical: str | None = None
        attempts: list[_Attempt] = []
        errors: tuple[str, ...] = ()
        refused: str | None = None
        for number in (1, 2):
            async with self.budget.slot:
                made: _Attempt | str | None = self._refusal(spec, pack, deadline)
                if made is None:
                    made = await self._attempt(
                        spec, pack, day, deadline, purpose, logical, number, errors, persist
                    )
                if isinstance(made, str) and made in PERSISTENT_REFUSALS:
                    made = await self._refused(
                        spec, pack, day, deadline, purpose, logical, number, made
                    )
            if isinstance(made, str):
                refused = made
                break
            attempt = made
            attempts.append(attempt)
            logical = attempt.logical_id
            failure = attempt.outcome.failure
            if failure is None or failure not in REPAIRABLE or number == 2:
                break
            errors = attempt.outcome.errors
        if not attempts:
            log.warning("llm call refused", role=role, purpose=purpose, why=refused)
            return LLMResult(False, None, None, 0, f"refused: {refused}", failure=refused)
        last = attempts[-1]
        if refused is not None:  # the repair was refused: the first attempt's result stands
            log.warning("llm repair refused", call=logical, why=refused)
            try:
                await self._journal.write(
                    lambda conn: conn.execute(
                        "UPDATE llm_calls SET final = 1 WHERE id = ?", (last.id,)
                    )
                )
            except Exception:
                log.exception("llm attempt not marked final", attempt=last.id)
        outcome = last.outcome
        return LLMResult(
            ok=outcome.ok,
            output=outcome.output,
            call_id=last.id,
            duration_ms=sum(a.duration_ms for a in attempts),
            error=None if outcome.ok else _message(outcome),
            failure=outcome.failure,
            attempts=tuple(a.id for a in attempts),
            logical_id=last.logical_id,
        )

    async def close_abandoned(self) -> int:
        """Mark the attempts a crash left open (boot, before any call): they count toward the
        day's cap, never toward Book B's degradation. Returns how many.

        A call whose last attempt is still not final (a repairable first attempt, then a
        cancellation or a crash before its repair) never produced a result either: readers treat
        it as abandoned too, and its first attempt's failure still counts (D-064)."""
        at = fmt_iso(self._clock.now())
        return await self._journal.write(
            lambda conn: (
                conn.execute(
                    "UPDATE llm_calls SET ended_at = ?, ok = 0, failure = ?, final = 1 "
                    "WHERE ended_at IS NULL",
                    (at, ABANDONED),
                ).rowcount
            )
        )

    async def _refused(
        self,
        spec: RoleSpec,
        pack: EncodedPack,
        day: date,
        deadline: datetime,
        purpose: Purpose,
        logical: str | None,
        number: int,
        code: str,
    ) -> _Attempt | str:
        """A persistent refusal, journaled as an attempt that failed without a CLI run."""
        try:
            ids = await self._journal.write(
                lambda conn: self._insert(
                    conn, spec, pack, day, deadline, purpose, logical, number, refused=code
                )
            )
        except Exception:
            log.exception("llm refusal not journaled", role=spec.role, why=code)
            return code
        assert ids is not None  # a refusal takes nothing from the cap, so it is never refused
        logical_id, attempt_id = ids
        log.warning("llm call refused", attempt=attempt_id, role=spec.role, why=code)
        return _Attempt(attempt_id, logical_id, Outcome(code), 0)

    def _refusal(self, spec: RoleSpec, pack: EncodedPack, deadline: datetime) -> str | None:
        """Why no attempt may start now, if one may not. The day's cap is checked in the
        attempt's own insert, so two attempts can never both take the last call."""
        if (deadline - self._clock.now()).total_seconds() < spec.min_attempt_s:
            return "no_time"
        if len(pack.data) > self._max_pack_bytes:
            return "pack_too_large"
        if not (self._prompts / spec.prompt_file).is_file():
            return "prompt_missing"
        if not _sandbox_ready(self._sandbox):
            return "sandbox_dirty"
        return None

    async def _attempt(
        self,
        spec: RoleSpec,
        pack: EncodedPack,
        day: date,
        deadline: datetime,
        purpose: Purpose,
        logical: str | None,
        number: int,
        errors: Sequence[str],
        persist: Persist | None,
    ) -> _Attempt | str:
        prompt = (self._prompts / spec.prompt_file).resolve()
        try:
            prompt_sha = await self._off(prompt_sha256, prompt)
        except OSError:
            return "prompt_missing"  # gone since the check
        folder = self._artifacts / day.isoformat()
        # The insert commits even if this call is cancelled meanwhile: shielded, so its row is
        # then closed as abandoned rather than left open.
        inserting = asyncio.ensure_future(
            self._journal.write(
                lambda conn: self._insert(
                    conn, spec, pack, day, deadline, purpose, logical, number, prompt_sha, folder
                )
            )
        )
        try:
            ids = await asyncio.shield(inserting)
        except asyncio.CancelledError:
            if (inserted := await _bounded(inserting)) is not None:
                await _bounded(self._close(inserted[1], ABANDONED))
            raise
        except Exception:
            log.exception("llm attempt not journaled", role=spec.role)
            return "internal_error"
        if ids is None:
            return "budget_exhausted"
        logical_id, attempt_id = ids
        try:
            result, outcome = await self._call(
                spec, pack, deadline, number, errors, persist, attempt_id, folder, prompt
            )
        except asyncio.CancelledError:
            await _bounded(self._close(attempt_id, ABANDONED))
            raise
        except Exception as exc:  # a bug, or the journal failing: close the row as a failure
            log.exception("llm attempt failed in the runner", attempt=attempt_id)
            failed = Outcome("internal_error", errors=(type(exc).__name__,))
            await _bounded(self._close(attempt_id, "internal_error", _message(failed)))
            return _Attempt(attempt_id, logical_id, failed, 0)
        log.info(
            "llm attempt",
            attempt=attempt_id,
            role=spec.role,
            ok=outcome.ok,
            failure=outcome.failure,
            duration_ms=int(result.duration_s * 1000),
            clamped=outcome.clamped,
            dropped=outcome.dropped,
        )
        return _Attempt(attempt_id, logical_id, outcome, int(result.duration_s * 1000))

    async def _call(
        self,
        spec: RoleSpec,
        pack: EncodedPack,
        deadline: datetime,
        number: int,
        errors: Sequence[str],
        persist: Persist | None,
        attempt_id: str,
        folder: Path,
        prompt: Path,
    ) -> tuple[TransportResult, Outcome]:
        """The attempt from its input artifact to its completed row."""
        result, output_path = _NO_RUN, None
        input_path = await self._save(folder / f"{attempt_id}.input.json", pack.data)
        if input_path is None:
            outcome = Outcome("artifact_failed")
        else:
            schema = self._schemas[spec.role]
            argv = build_argv(
                self._command,
                instruction=repair_instruction(spec, errors) if errors else spec.instruction,
                schema=schema.text,
                model=spec.model,
                effort=spec.effort,
                system_prompt_file=prompt,
                max_turns=spec.max_turns,
                tools_flag=self._tools_flag,
                safe_mode=self._safe_mode,
            )
            timeout = min(float(spec.timeout_s), (deadline - self._clock.now()).total_seconds())
            try:
                result = await self._transport.run(
                    argv, stdin=pack.data, env=self._env, cwd=self._sandbox, timeout_s=timeout
                )
            except OSError as exc:  # the executable vanished, or couldn't start
                result = TransportResult(None, b"", str(exc).encode(), 0.0)
                outcome = Outcome("spawn_failed")
            else:
                try:
                    outcome = classify(result, spec, pack, schema.full)
                except Exception:  # a bug here must still close the row, as a failure
                    log.exception("llm attempt not classified", attempt=attempt_id)
                    outcome = Outcome("internal_error")
            text = self._output_text(attempt_id, argv, result, outcome)
            output_path = await self._save(folder / f"{attempt_id}.output.json", text.encode())
            if output_path is None:
                outcome = dataclasses.replace(
                    outcome, failure="artifact_failed", output=None, errors=()
                )
        final = outcome.ok or outcome.failure not in REPAIRABLE or number == 2
        await self._journal.write(
            lambda conn: self._complete(
                conn, attempt_id, result, outcome, final, input_path, output_path, persist
            )
        )
        return result, outcome

    async def _off[T](self, fn: Callable[..., T], *args: Any) -> T:
        """File work: on a thread beside a threaded journal, else inline. Under simdrive (an
        inline journal) a thread would hide the work from ``settle``, and the SimClock would
        jump in the middle of an attempt (D-047)."""
        if self._journal.inline:
            return fn(*args)
        return await asyncio.to_thread(fn, *args)

    async def _save(self, path: Path, data: bytes) -> str | None:
        """Write an artifact; its path, or None (logged) if it couldn't be written."""
        try:
            await self._off(_write_bytes, path, data)
        except OSError as exc:
            log.error("llm artifact not written", path=str(path), error=str(exc))
            return None
        return str(path)

    def _insert(
        self,
        conn: sqlite3.Connection,
        spec: RoleSpec,
        pack: EncodedPack,
        day: date,
        deadline: datetime,
        purpose: Purpose,
        logical: str | None,
        number: int,
        prompt_sha: str | None = None,
        folder: Path | None = None,
        *,
        refused: str | None = None,
    ) -> tuple[str, str] | None:
        """The attempt's row, in one transaction with the id it takes (``YYYYMMDD-role-NN``,
        numbered from the journal, so ids repeat on a replay and continue after a restart) and
        with the day's cap: None, and no row, if a trading call finds it spent. A ``refused``
        attempt's row is complete at once, and takes nothing from the cap."""
        if purpose == "trading" and refused is None:
            spent = conn.execute(
                f"SELECT COUNT(*) FROM llm_calls WHERE date = ? AND purpose = 'trading' "
                f"AND {CLI_RUNS}",
                (day.isoformat(),),
            ).fetchone()[0]
            if spent >= self.budget.max_calls_per_day:
                return None
        if logical is None:
            prefix = f"{day:%Y%m%d}-{'smoke-' if purpose == 'smoke' else ''}{spec.role}"
            n = conn.execute(
                "SELECT COUNT(DISTINCT logical_id) FROM llm_calls WHERE logical_id LIKE ?",
                (f"{prefix}-%",),
            ).fetchone()[0]
            logical = f"{prefix}-{n + 1:02d}"
        else:
            conn.execute(
                "UPDATE llm_calls SET repair_attempted = 1 WHERE logical_id = ? AND attempt = ?",
                (logical, number - 1),
            )
        attempt_id = f"{logical}-{number}"
        epoch = conn.execute("SELECT value FROM meta WHERE key = 'eval_epoch'").fetchone()
        now = fmt_iso(self._clock.now())
        conn.execute(
            "INSERT INTO llm_calls (id, date, role, model_alias, prompt_sha256, schema_sha256, "
            "started_at, input_path, repair_attempted, logical_id, attempt, final, effort, "
            "purpose, deadline, pack_id, pack_sha256, pack_bytes, candidates_json, cli_version, "
            "schema_mode, eval_epoch, ended_at, ok, failure, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                attempt_id,
                day.isoformat(),
                spec.role,
                spec.model,
                prompt_sha,
                self._schemas[spec.role].sha256,
                now,
                None if folder is None else str(folder / f"{attempt_id}.input.json"),
                logical,
                number,
                int(refused is not None),
                spec.effort,
                purpose,
                fmt_iso(deadline),
                pack.pack_id,
                pack.sha256,
                len(pack.data),
                json.dumps(list(pack.kept)),
                self._cli_version,
                self._schema_mode,
                int(epoch[0]) if epoch else None,
                None if refused is None else now,
                None if refused is None else 0,
                refused,
                refused,
            ),
        )
        return logical, attempt_id

    def _complete(
        self,
        conn: sqlite3.Connection,
        attempt_id: str,
        result: TransportResult,
        outcome: Outcome,
        final: bool,
        input_path: str | None,
        output_path: str | None,
        persist: Persist | None,
    ) -> None:
        env = outcome.envelope or {}
        models = env.get("modelUsage")
        conn.execute(
            "UPDATE llm_calls SET ended_at = ?, duration_ms = ?, exit_code = ?, is_error = ?, "
            "num_turns = ?, cost_estimate_usd = ?, schema_valid = ?, model_resolved = ?, "
            "input_path = ?, output_path = ?, error = ?, ok = ?, failure = ?, subtype = ?, "
            "session_id = ?, timed_out = ?, clamped = ?, final = ? WHERE id = ?",
            (
                fmt_iso(self._clock.now()),
                int(result.duration_s * 1000),
                result.exit_code,
                _flag(env.get("is_error")),
                _int(env.get("num_turns")),
                _float(env.get("total_cost_usd")),
                None if outcome.validated is None else int(outcome.validated),
                ",".join(sorted(models)) if isinstance(models, dict) and models else None,
                input_path,
                output_path,
                None if outcome.ok else _message(outcome),
                int(outcome.ok),
                outcome.failure,
                _str(env.get("subtype")),
                _str(env.get("session_id")),
                int(result.timed_out),
                outcome.clamped,
                int(final),
                attempt_id,
            ),
        )
        if persist is not None and outcome.output is not None:
            persist(conn, outcome.output, attempt_id)

    async def _close(self, attempt_id: str, failure: str, error: str | None = None) -> None:
        """End an attempt that couldn't complete; one that did keeps its result."""
        at = fmt_iso(self._clock.now())
        await self._journal.write(
            lambda conn: conn.execute(
                "UPDATE llm_calls SET ended_at = ?, ok = 0, failure = ?, error = ?, final = 1 "
                "WHERE id = ? AND ended_at IS NULL",
                (at, failure, error, attempt_id),
            )
        )

    def _output_text(
        self, attempt_id: str, argv: Sequence[str], result: TransportResult, outcome: Outcome
    ) -> str:
        """The output artifact, redacted. ASCII, so the model's JSON can't make it unwritable
        (a lone surrogate stays an escape). Text cut at an arbitrary byte loses ``CUT_MARGIN``
        characters at the cut, which takes any part of a secret the cut went through."""
        envelope = outcome.envelope
        stdout = None
        if envelope is None:
            stdout = result.stdout[:MAX_RAW_STDOUT].decode("utf-8", errors="replace")
            if result.overflow or len(result.stdout) > MAX_RAW_STDOUT:
                stdout = stdout[:-CUT_MARGIN]
        stderr = result.stderr_tail.decode("utf-8", errors="replace")
        if result.stderr_cut:
            stderr = stderr[CUT_MARGIN:]
        record = {
            "attempt": attempt_id,
            "argv": _shown(argv),
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "overflow": result.overflow,
            "stopped": result.stopped,
            "duration_ms": int(result.duration_s * 1000),
            "failure": outcome.failure,
            "errors": list(outcome.errors),
            "clamped": outcome.clamped,
            "dropped": outcome.dropped,
            "output": None if outcome.output is None else outcome.output.model_dump(mode="json"),
            "envelope": envelope,
            "stdout": stdout,
            "stderr_tail": stderr,
        }
        return self._redact(json.dumps(record, ensure_ascii=True, indent=1))

    def _redact(self, text: str) -> str:
        return _KEY_LIKE.sub("***KEY***", redact_text(text, self._secrets))


def _credentialed_env(cfg: AppConfig, parent: Mapping[str, str]) -> dict[str, str]:
    llm = cfg.settings.llm
    name = "CLAUDE_CODE_OAUTH_TOKEN" if llm.provider == "claude_cli" else "ANTHROPIC_API_KEY"
    secret = cfg.secrets.secret(name)
    if secret is None:
        raise LlmUnavailableError(f"{name} is not set in .env (PLAN.md H0.2)")
    value = secret.get_secret_value()
    return build_claude_env(
        parent=parent,
        config_dir=cfg.path(llm.config_dir),
        oauth_token=value if name == "CLAUDE_CODE_OAUTH_TOKEN" else None,
        api_key=value if name == "ANTHROPIC_API_KEY" else None,
    )


async def _bounded[T](aw: Awaitable[T], seconds: float = CLEANUP_S) -> T | None:
    """``aw``'s result, waited for ``seconds`` at most; None if it failed or ran late. A
    cancelled attempt's cleanup must end: at a loop's teardown the journal's writer may be
    gone already, and boot's ``close_abandoned`` then closes the row."""
    try:
        return await asyncio.wait_for(asyncio.shield(aw), seconds)
    except Exception:
        log.warning("llm cleanup did not finish", exc_info=True)
        return None


def _schema(spec: RoleSpec, mode: SchemaMode) -> _Schema:
    sent = schema_bytes(wire_schema(spec.output, mode))
    return _Schema(
        text=sent.decode("ascii"),
        sha256=hashlib.sha256(sent).hexdigest(),
        full=wire_schema(spec.output, "full"),
    )


def _write_bytes(path: Path, data: bytes) -> None:
    """Write a file whole: a reader never sees half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(data)
    for wait_s in _RENAME_RETRY_S:
        try:
            partial.replace(path)
        except PermissionError:
            time.sleep(wait_s)
        else:
            return
    partial.replace(path)


def _sandbox_ready(path: Path) -> bool:
    """The empty folder every call runs in (A§7.1): made if missing; anything in it fails the
    call closed, since a CLAUDE.md, a .mcp.json or a .claude folder there would load."""
    path.mkdir(parents=True, exist_ok=True)
    return not any(path.iterdir())


def _shown(argv: Sequence[str]) -> list[str]:
    """The argv for the artifact, the schema replaced by its hash."""
    out = list(argv)
    for i, part in enumerate(out[:-1]):
        if part == "--json-schema":
            out[i + 1] = f"<schema sha256 {hashlib.sha256(out[i + 1].encode()).hexdigest()}>"
    return out


def _message(outcome: Outcome) -> str:
    first = f": {outcome.errors[0]}" if outcome.errors else ""
    return f"{outcome.failure}{first}"[:300]


def _flag(value: Any) -> int | None:
    return int(value) if isinstance(value, bool) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _str(value: Any) -> str | None:
    return value[:200] if isinstance(value, str) else None
