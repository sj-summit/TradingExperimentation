"""Recording and replaying the LLM's answers (the Phase 4 plan's decision 10; D-064, D-069).

A live day records two kinds of line into ``recordings/<date>/llm.jsonl.gz``, each flushed at
once, so a crash keeps everything up to it:

- **A pack** (``orchestrator.packs.RecordingPacks``): every pack a Book B job built, sent or not.
- **An attempt** (``RecordingTransport``, here): what the transport gave back for one attempt:
  the exit code, stdout, stderr's tail, the timings and how a late child was stopped, or the
  error that kept it from starting. Stdout and stderr are kept as text (``surrogateescape``, so
  any bytes survive) with the credential redacted, as the artifacts are (D-064). Text cut at an
  arbitrary byte (stderr's tail, a stdout past its cap) also loses ``CUT_MARGIN`` characters at
  the cut, where a secret cut in two would otherwise survive in part. Neither is ever classified
  (a stdout past its cap is ``output_too_large`` whatever it holds), so the replay loses nothing.

A replay answers each attempt with its recorded result (``ReplayTransport``) at max(now, the
recorded end), so it needs no ``claude``. Attempts are matched by role and order, and each is
checked by its pack's ``pack_id`` and whether it is a repair; what doesn't match is a divergence,
as in the market data's replay (D-047). The runner (``LlmCalls``) runs as it does live, so the
replay journals the same ``llm_calls`` rows and stores the same verdicts.
"""

import json
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from trader.core.clock import Clock, fmt_iso, parse_et_iso
from trader.llm.claude_cli import Transport, TransportResult
from trader.llm.client import CUT_MARGIN, REPAIR_NOTE, redactor

LLM_FILE = "llm.jsonl.gz"
Sink = Callable[[dict[str, Any]], None]
ERROR_CHARS = 300


def pack_key(stdin: bytes) -> tuple[str, str]:
    """The role and ``pack_id`` of the pack an attempt sends; ``?`` for what isn't one."""
    try:
        body = json.loads(stdin)
        return str(body["role"]), str(body["pack_id"])
    except (ValueError, KeyError, TypeError):
        return "?", "?"


def is_repair(argv: Sequence[str]) -> bool:
    """Whether an attempt is the call's repair: its instruction carries the repair's note."""
    parts = list(argv)
    try:
        return REPAIR_NOTE in parts[parts.index("-p") + 1]
    except (ValueError, IndexError):
        return False


def _text(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def _bytes(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


class RecordingTransport:
    """A transport, with each attempt's result recorded once it ends. A cancelled attempt
    records nothing: the crash or shutdown that cancelled it ends the run a replay covers."""

    def __init__(self, inner: Transport, clock: Clock, sink: Sink) -> None:
        self._inner, self._clock, self._sink = inner, clock, sink

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
    ) -> TransportResult:
        role, pack_id = pack_key(stdin)
        head: dict[str, Any] = {
            "kind": "attempt",
            "role": role,
            "pack_id": pack_id,
            "repair": is_repair(argv),
            "start_rx": fmt_iso(self._clock.now()),
        }
        redact = redactor(env)
        try:
            result = await self._inner.run(argv, stdin=stdin, env=env, cwd=cwd, timeout_s=timeout_s)
        except OSError as exc:  # it never started: the runner's spawn_failed
            end = fmt_iso(self._clock.now())
            self._sink(head | {"end_rx": end, "error": redact(str(exc))[:ERROR_CHARS]})
            raise
        stdout, stderr = _text(result.stdout), _text(result.stderr_tail)
        if result.overflow:  # cut at the cap: its end is an arbitrary byte
            stdout = stdout[:-CUT_MARGIN]
        if result.stderr_cut:  # a tail: its start is an arbitrary byte
            stderr = stderr[CUT_MARGIN:]
        self._sink(
            head
            | {
                "end_rx": fmt_iso(self._clock.now()),
                "exit_code": result.exit_code,
                "stdout": redact(stdout),
                "stderr_tail": redact(stderr),
                "duration_s": result.duration_s,
                "timed_out": result.timed_out,
                "overflow": result.overflow,
                "stopped": result.stopped,
                "stderr_cut": result.stderr_cut,
            }
        )
        return result


class ReplayTransport:
    """The recorded attempts, each answered at max(now, its recorded end)."""

    def __init__(self, records: Iterable[Mapping[str, Any]], clock: Clock) -> None:
        self._clock = clock
        self._attempts: dict[str, deque[Mapping[str, Any]]] = defaultdict(deque)
        for record in records:
            if record.get("kind") == "attempt":
                self._attempts[str(record["role"])].append(record)
        self.divergences: list[str] = []

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
    ) -> TransportResult:
        role, pack_id = pack_key(stdin)
        queue = self._attempts.get(role)
        if not queue:
            self.divergences.append(f"llm: a {role} attempt the recording doesn't have")
            return TransportResult(None, b"", b"not in the recording", 0.0)
        record = queue.popleft()
        if record["pack_id"] != pack_id:
            self.divergences.append(
                f"llm: a {role} attempt sent pack {pack_id}; the recorded one sent "
                f"{record['pack_id']}"
            )
        if bool(record["repair"]) != is_repair(argv):
            what = "a repair" if record["repair"] else "a first try"
            self.divergences.append(f"llm: a {role} attempt the recording has as {what}")
        end = parse_et_iso(str(record["end_rx"]))
        await self._clock.sleep_until(max(self._clock.now(), end))
        if "error" in record:
            raise OSError(str(record["error"]))
        return TransportResult(
            exit_code=record["exit_code"],
            stdout=_bytes(str(record["stdout"])),
            stderr_tail=_bytes(str(record["stderr_tail"])),
            duration_s=float(record["duration_s"]),
            timed_out=bool(record["timed_out"]),
            overflow=bool(record["overflow"]),
            stopped=record["stopped"],
            stderr_cut=bool(record["stderr_cut"]),
        )

    @property
    def unused(self) -> int:
        """Recorded attempts the replay never asked for."""
        return sum(len(q) for q in self._attempts.values())
