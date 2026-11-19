"""Recording and replaying the LLM's answers (llm/replay.py; P4.8; decision 10, D-069)."""

import gzip
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trader.core.clock import ET, SimClock
from trader.data.recorder import Recorder, read_jsonl
from trader.llm.claude_cli import TransportResult
from trader.llm.client import CUT_MARGIN, REPAIR_NOTE
from trader.llm.replay import LLM_FILE, RecordingTransport, ReplayTransport, is_repair, pack_key
from trader.orchestrator.simdrive import drive

T0 = datetime(2026, 10, 13, 9, 35, 5, tzinfo=ET)
TOKEN = "sk-ant-oat01-test-token-0123456789abcdef"
PACK = json.dumps({"role": "vet", "pack_id": "vet-20261013-0123456789abcdef"}).encode()


def argv(instruction: str = "Produce the VetBatch.") -> list[str]:
    return ["claude", "-p", instruction, "--output-format", "json"]


class Inner:
    """A transport that takes ``took`` seconds on the clock and answers ``result``."""

    def __init__(self, clock: SimClock, result: TransportResult | Exception, took: float) -> None:
        self.clock, self.result, self.took = clock, result, took

    async def run(self, argv: list[str], *, stdin: bytes, env: dict[str, str], cwd: Path,
                  timeout_s: float) -> TransportResult:  # fmt: skip
        await self.clock.sleep_until(self.clock.now() + timedelta(seconds=self.took))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def record(
    tmp_path: Path,
    result: TransportResult | Exception,
    *,
    took: float = 16.5,
    instruction: str = "Produce the VetBatch.",
) -> list[dict[str, object]]:
    clock = SimClock(T0)
    recorder = Recorder(tmp_path, T0.date())
    transport = RecordingTransport(
        Inner(clock, result, took), clock, lambda r: recorder.append(LLM_FILE, r, flush=True)
    )

    async def call() -> None:
        await transport.run(
            argv(instruction),
            stdin=PACK,
            env={"CLAUDE_CODE_OAUTH_TOKEN": TOKEN},
            cwd=tmp_path,
            timeout_s=60,
        )

    if isinstance(result, Exception):
        with pytest.raises(type(result)):
            await drive(clock, call())
    else:
        await drive(clock, call())
    # flushed at once: readable before the recorder closes, as after a crash
    records = list(read_jsonl(tmp_path / T0.date().isoformat() / LLM_FILE))
    recorder.close()
    return records


async def test_an_attempt_replays_byte_for_byte_at_its_recorded_end(tmp_path: Path) -> None:
    stdout = b'{"type": "result", "x": "\xff\xfe bytes that are not UTF-8"}'
    result = TransportResult(
        exit_code=0,
        stdout=stdout,
        stderr_tail=b"warn\x80",
        duration_s=16.5,
        timed_out=False,
        overflow=False,
        stopped=None,
        stderr_cut=False,
    )
    records = await record(tmp_path, result)
    assert [(r["kind"], r["role"], r["pack_id"], r["repair"]) for r in records] == [
        ("attempt", "vet", "vet-20261013-0123456789abcdef", False)
    ]
    clock = SimClock(T0)
    replay = ReplayTransport(records, clock)
    got = await drive(clock, replay.run(argv(), stdin=PACK, env={}, cwd=tmp_path, timeout_s=60))
    assert got == result
    assert clock.now() == T0 + timedelta(seconds=16.5)  # at the recorded end
    assert (replay.divergences, replay.unused) == ([], 0)


async def test_how_a_late_attempt_ended_replays_too(tmp_path: Path) -> None:
    """A timeout, the stop that ended it and an overflow come back as recorded: the runner
    classifies by them (D-064)."""
    result = TransportResult(exit_code=None, stdout=b"x" * 2000, stderr_tail=b"", duration_s=60.0,
                             timed_out=True, overflow=True, stopped="kill")  # fmt: skip
    records = await record(tmp_path, result, took=60.0)
    clock = SimClock(T0)
    got = await drive(clock, ReplayTransport(records, clock).run(
        argv(), stdin=PACK, env={}, cwd=tmp_path, timeout_s=60))  # fmt: skip
    assert (got.timed_out, got.overflow, got.stopped, got.exit_code) == (True, True, "kill", None)
    assert got.stdout == b"x" * (2000 - CUT_MARGIN)  # its cut end dropped (below)


async def test_a_cut_text_keeps_no_part_of_a_secret_cut_in_two(tmp_path: Path) -> None:
    """D-064's cut margin, here as in the artifacts: a tail that starts inside the token, or a
    stdout cut at its cap inside it, keeps none of it."""
    tail = TOKEN[20:] + " then 16 KB of stderr"  # the tail starts 20 characters into the token
    capped = b'{"type": "result"} ' + TOKEN[:30].encode()  # cut at the cap inside the token
    result = TransportResult(exit_code=1, stdout=capped, stderr_tail=tail.encode(), duration_s=1,
                             overflow=True, stopped="kill", stderr_cut=True)  # fmt: skip
    records = await record(tmp_path, result, took=1.0)
    # each was shorter than the margin: nothing of either is kept
    assert (records[0]["stderr_tail"], records[0]["stdout"]) == ("", "")
    assert (records[0]["stderr_cut"], records[0]["overflow"]) == (True, True)
    long_tail = TransportResult(exit_code=1, stdout=b"{}", stderr_tail=(tail + "x" * 600).encode(),
                                duration_s=1, stderr_cut=True)  # fmt: skip
    kept = (await record(tmp_path / "long", long_tail, took=1.0))[0]["stderr_tail"]
    assert kept == "x" * (len(tail) + 600 - CUT_MARGIN)  # only the tail's sound end


async def test_a_late_ask_is_answered_at_once(tmp_path: Path) -> None:
    result = TransportResult(0, b"{}", b"", 2.0)
    records = await record(tmp_path, result, took=2.0)
    clock = SimClock(T0 + timedelta(seconds=30))  # asked after the recorded end
    got = await drive(clock, ReplayTransport(records, clock).run(
        argv(), stdin=PACK, env={}, cwd=tmp_path, timeout_s=60))  # fmt: skip
    assert got == result
    assert clock.now() == T0 + timedelta(seconds=30)


async def test_the_credential_never_reaches_the_recording(tmp_path: Path) -> None:
    result = TransportResult(1, f"oops {TOKEN}".encode(), f"auth {TOKEN} failed".encode(), 1.0)
    records = await record(tmp_path, result, took=1.0)
    text = gzip.decompress((tmp_path / "2026-10-13" / LLM_FILE).read_bytes()).decode()
    assert "***CLAUDE_CODE_OAUTH_TOKEN***" in text  # redacted where it was, not dropped
    assert TOKEN not in text
    assert TOKEN not in json.dumps(records)


async def test_a_spawn_failure_replays_as_one(tmp_path: Path) -> None:
    records = await record(tmp_path, FileNotFoundError(f"claude.exe is gone ({TOKEN})"), took=0)
    assert records[0]["error"] == "claude.exe is gone (***CLAUDE_CODE_OAUTH_TOKEN***)"
    clock = SimClock(T0)
    with pytest.raises(OSError, match=r"claude\.exe is gone"):
        await drive(clock, ReplayTransport(records, clock).run(
            argv(), stdin=PACK, env={}, cwd=tmp_path, timeout_s=60))  # fmt: skip


async def test_what_does_not_match_the_recording_is_a_divergence(tmp_path: Path) -> None:
    records = await record(tmp_path, TransportResult(0, b"{}", b"", 1.0), took=1.0)
    clock = SimClock(T0)
    replay = ReplayTransport(records, clock)
    other = json.dumps({"role": "vet", "pack_id": "vet-20261013-ffffffffffffffff"}).encode()
    repair = f"Produce the VetBatch.\n\n{REPAIR_NOTE}\n- x: missing"
    await drive(clock, replay.run(argv(repair), stdin=other, env={}, cwd=tmp_path, timeout_s=60))
    assert replay.divergences == [
        "llm: a vet attempt sent pack vet-20261013-ffffffffffffffff; the recorded one sent "
        "vet-20261013-0123456789abcdef",
        "llm: a vet attempt the recording has as a first try",
    ]
    missing = await drive(clock, replay.run(argv(), stdin=PACK, env={}, cwd=tmp_path,
                                            timeout_s=60))  # fmt: skip
    assert (missing.exit_code, missing.stdout) == (None, b"")  # fails as a non-zero exit would
    assert replay.divergences[-1] == "llm: a vet attempt the recording doesn't have"


async def test_unused_attempts_are_counted(tmp_path: Path) -> None:
    records = await record(tmp_path, TransportResult(0, b"{}", b"", 1.0), took=1.0)
    assert ReplayTransport(records + records, SimClock(T0)).unused == 2


def test_an_attempt_is_known_by_its_pack_and_instruction() -> None:
    assert pack_key(PACK) == ("vet", "vet-20261013-0123456789abcdef")
    assert pack_key(b"not json") == ("?", "?")
    assert pack_key(b"[]") == ("?", "?")
    assert not is_repair(argv())
    assert is_repair(argv(f"x\n\n{REPAIR_NOTE}\n- y"))
    assert not is_repair(["claude", "-p"])
    assert not is_repair(["claude"])
