"""The LLM setup's fingerprint (STRATEGY.md §9; the Phase 4 plan's P4.5; DECISIONS D-065).

What Book B's calls ask is set by the prompts, each role's instruction and the repair's note,
the schemas sent, the models and their efforts, and the CLI's flags. A change to any of them
starts a new evaluation epoch unless it is a bug fix (S§9); only the human bumps
``eval_epoch``, with a DECISIONS entry.

- ``epoch_fingerprint(settings, prompts_dir)`` hashes all of it. A prompt counts by its text,
  hashed as ``llm_calls.prompt_sha256`` is (line endings made LF), never by its path, so a
  replay's root and a checkout's CRLF hash the same.
- ``note_fingerprint(journal, fingerprint, at=...)`` stores it at boot when Book B runs (P4.8).
  One that differs from the last stored raises a P3 ``llm_fingerprint_changed`` naming what
  changed, in the same transaction.

How a call is run is left out: timeouts, the day's budget, the stop path. So is the CLI's
version; each call's row records it, and doctor warns past ``llm.max_tested_cli_version``.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.config import Settings
from trader.llm import client
from trader.llm.claude_cli import build_argv
from trader.llm.context import PACK_VERSION
from trader.llm.roles import prompt_sha256, role_specs
from trader.llm.schemas import schema_bytes, wire_schema
from trader.memory.incidents import insert_incident
from trader.memory.journal import Journal

META_KEY = "llm_fingerprint"
META_PARTS = "llm_fingerprint_parts"
CODE = "llm_fingerprint_changed"


@dataclass(frozen=True)
class Fingerprint:
    sha256: str
    parts: Mapping[str, Any]  # what was hashed, so a change can be named


def epoch_fingerprint(settings: Settings, prompts_dir: Path) -> Fingerprint:
    llm = settings.llm
    roles: dict[str, Any] = {}
    for role, spec in sorted(role_specs(settings).items()):
        prompt = prompts_dir / spec.prompt_file
        roles[role] = {
            "prompt_sha256": prompt_sha256(prompt) if prompt.is_file() else None,
            "instruction": spec.instruction,
            "schema_sha256": _sha256(schema_bytes(wire_schema(spec.output, llm.schema_mode))),
            "model": spec.model,
            "effort": spec.effort,
            # the flags as sent, the values hashed above left as placeholders
            "argv": build_argv(
                (),
                instruction="<instruction>",
                schema="<schema>",
                model=spec.model,
                effort=spec.effort,
                system_prompt_file=Path("<prompt>"),
                max_turns=spec.max_turns,
                tools_flag=llm.tools_flag,
                safe_mode=llm.safe_mode,
            ),
        }
    parts = {
        "pack_version": PACK_VERSION,
        "repair_note": client.REPAIR_NOTE,
        "schema_mode": llm.schema_mode,
        "roles": roles,
    }
    return Fingerprint(_sha256(_canonical(parts)), parts)


async def note_fingerprint(
    journal: Journal, fingerprint: Fingerprint, *, at: datetime
) -> tuple[str, ...] | None:
    """Store ``fingerprint`` as the setup's. None the first time one is stored; else what
    changed since the last (empty when nothing did)."""
    return await journal.write(lambda conn: _note(conn, fingerprint, at))


def changes(old: Mapping[str, Any], new: Mapping[str, Any], prefix: str = "") -> Iterator[str]:
    """The dotted names of the parts that differ, in order."""
    for key in sorted(set(old) | set(new)):
        a, b = old.get(key), new.get(key)
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            yield from changes(a, b, f"{prefix}{key}.")
        elif a != b:
            yield f"{prefix}{key}"


def _note(conn: sqlite3.Connection, fp: Fingerprint, at: datetime) -> tuple[str, ...] | None:
    stored = _meta(conn, META_KEY)
    if stored == fp.sha256:
        return ()
    old = _meta(conn, META_PARTS)
    for key, value in ((META_KEY, fp.sha256), (META_PARTS, _canonical(fp.parts).decode())):
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    if stored is None:
        return None
    # parts round-trip through JSON, so compare them in that form
    changed = tuple(changes(json.loads(old) if old else {}, json.loads(_canonical(fp.parts))))
    insert_incident(
        conn,
        at=at,
        severity="P3",
        component="llm",
        code=CODE,
        message=(
            f"the LLM setup changed ({', '.join(changed)}): unless it is a bug fix, it starts "
            "a new epoch (STRATEGY.md §9)"
        ),
        detail={"from": stored, "to": fp.sha256, "changed": list(changed)},
    )
    return changed


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _canonical(parts: Mapping[str, Any]) -> bytes:
    return json.dumps(parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
