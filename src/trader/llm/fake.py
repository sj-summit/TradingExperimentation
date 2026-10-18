"""A scripted stand-in for the CLI, for tests and ``run-day --sim`` (DECISIONS D-064).

- ``ScriptedTransport`` answers each attempt from a script: ``Reply`` objects taken in turn, or
  a function of what the attempt received (``Seen``). A reply's ``delay_s`` passes on the
  injected ``sleep``, so on a ``SimClock`` a call takes simulated time; a reply slower than the
  attempt's timeout comes back timed out.
- ``answer_for(pack)`` is the answer a cautious model gives a pack: every candidate ``neutral``,
  every review target reviewed. ``envelope`` wraps an answer as the CLI's JSON result does.

``scripts/fake_claude.py`` uses the same helpers in a real subprocess. This module stays light
(standard library only, besides the transport's types), so the stand-in starts fast.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trader.llm.claude_cli import TransportResult

REPAIR_MARK = "did not validate"  # in a repair's instruction (llm/client.py)


@dataclass(frozen=True)
class Seen:
    """What one attempt sent."""

    argv: tuple[str, ...]
    stdin: bytes
    env: Mapping[str, str] = field(repr=False)  # it holds the credential
    cwd: Path
    timeout_s: float

    @property
    def instruction(self) -> str:
        return self.argv[self.argv.index("-p") + 1]

    @property
    def is_repair(self) -> bool:
        return REPAIR_MARK in self.instruction

    def pack(self) -> dict[str, Any]:
        data = json.loads(self.stdin)
        assert isinstance(data, dict)
        return data


@dataclass(frozen=True)
class Reply:
    envelope: Mapping[str, Any] | None = None  # printed as the JSON result
    stdout: bytes | None = None  # printed instead, as it is
    exit_code: int = 0
    stderr: bytes = b""
    delay_s: float = 0.0
    overflow: bool = False  # as if stdout ran past the cap


Script = Sequence[Reply] | Callable[[Seen], Reply]


async def _no_wait(seconds: float) -> None:
    await asyncio.sleep(0)


@dataclass
class ScriptedTransport:
    script: Script
    sleep: Callable[[float], Awaitable[None]] = _no_wait
    seen: list[Seen] = field(default_factory=list, repr=False)
    _replies: Iterator[Reply] | None = field(default=None, repr=False)

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes,
        env: Mapping[str, str],
        cwd: Path,
        timeout_s: float,
    ) -> TransportResult:
        seen = Seen(argv=tuple(argv), stdin=stdin, env=dict(env), cwd=cwd, timeout_s=timeout_s)
        self.seen.append(seen)
        reply = self._next(seen)
        if reply.delay_s > timeout_s:
            await self.sleep(timeout_s)
            return TransportResult(
                exit_code=None,
                stdout=b"",
                stderr_tail=reply.stderr,
                duration_s=timeout_s,
                timed_out=True,
                stopped="interrupt",
            )
        await self.sleep(reply.delay_s)
        if reply.stdout is not None:
            out = reply.stdout
        else:
            out = b"" if reply.envelope is None else json.dumps(reply.envelope).encode()
        return TransportResult(
            exit_code=reply.exit_code,
            stdout=out,
            stderr_tail=reply.stderr,
            duration_s=reply.delay_s,
            overflow=reply.overflow,
            stopped="kill" if reply.overflow else None,
        )

    def _next(self, seen: Seen) -> Reply:
        if callable(self.script):
            return self.script(seen)
        if self._replies is None:
            self._replies = iter(self.script)
        reply = next(self._replies, None)
        if reply is None:
            raise AssertionError(f"the script has no reply for attempt {len(self.seen)}")
        return reply


def envelope(
    structured: Mapping[str, Any] | None,
    *,
    subtype: str = "success",
    is_error: bool = False,
    result: str = "",
    errors: Sequence[str] = (),
    model: str = "claude-sonnet-5",
    session_id: str = "fake-session",
    num_turns: int = 1,
    cost: float = 0.0,
) -> dict[str, Any]:
    """The CLI's ``--output-format json`` result (the Agent SDK's SDKResultMessage)."""
    out: dict[str, Any] = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "duration_ms": 1,
        "duration_api_ms": 1,
        "num_turns": num_turns,
        "session_id": session_id,
        "total_cost_usd": cost,
        "usage": {},
        "modelUsage": {model: {}},
        "permission_denials": [],
    }
    if subtype == "success":
        out["result"] = result
        if structured is not None:
            out["structured_output"] = dict(structured)
    else:
        out["errors"] = list(errors)
    return out


def assessment(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "catalyst_type": "no_identifiable_news",
        "catalyst_fresh": False,
        "catalyst_confirmed": False,
        "risk_flags": [],
        "verdict": "neutral",
        "confidence": 0.5,
        "reason": "No strong view.",
    }


def signal_review(signal_id: str) -> dict[str, Any]:
    return {
        "signal_id": signal_id,
        "went_well": "",
        "went_badly": "",
        "why": "The data doesn't explain it.",
        "factors": [],
        "knowable_at_decision": "no",
        "b_process": "not_applicable",
        "b_note": "",
    }


def answer_for(pack: Mapping[str, Any]) -> dict[str, Any]:
    """A valid answer to ``pack``, echoing its ``pack_id``."""
    pack_id, role = pack["pack_id"], pack["role"]
    if role == "review":
        return {
            "pack_id": pack_id,
            "summary": "A quiet day.",
            "signal_reviews": [signal_review(t["signal_id"]) for t in pack["review_targets"]],
            "process_issues": [],
            "lesson_proposals": [],
            "human_notes": [],
        }
    assessments = [assessment(c["symbol"]) for c in pack["candidates"]]
    if role == "vet":
        return {"pack_id": pack_id, "assessments": assessments}
    return {
        "pack_id": pack_id,
        "day_mode": "normal",
        "day_mode_reason": "Ordinary conditions.",
        "market_notes": "",
        "assessments": assessments,
    }


def valid(seen: Seen) -> Reply:
    """A script that answers every attempt well."""
    return Reply(envelope=envelope(answer_for(seen.pack())))
