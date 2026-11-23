"""The playbook (STRATEGY.md §6.4; the Phase 5 plan's P5.3, decision 12; DECISIONS D-077).

Promoted lessons only, at most ``lessons.max_playbook_lessons`` (15), ranked by n x |effect|,
where n is the signals the lesson's conditions matched in its test. They reach Claude as soft
guidance, and never change a rule, a risk limit or a parameter.
- **The packs** (``playbook``): every pack's ``playbook`` section, each lesson with its numbers,
  the conditions code validated, and its statement under ``untrusted`` (A§8).
- **The vet's candidates** (``matching``, ``vet_features``): ``matching_lessons`` names the
  lessons whose conditions hold on the candidate's features known at arming.
  - The model sets Book B's own fields (catalyst, flags, verdict, its source, confidence)
    itself, so code leaves conditions on them for the model to read in ``conditions``.
  - A condition on another field the candidate lacks at arming (``minutes_to_trigger``) means
    no match: code can't confirm it.
  - A lesson matches only if code checked at least one of its conditions.
  - The brief's candidates get none: before the open their ``gap_pct`` is the pre-market move,
    not the opening gap the lessons were tested on.
- **``runtime/playbook.md``** (``render``), for the human, rewritten by
  ``trader lessons evaluate --promote``.
"""

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.config import LessonsConfig
from trader.core.clock import fmt_iso
from trader.core.models import Signal
from trader.llm.context import PlaybookEntry
from trader.memory.lessons import Condition, QueryError, validate
from trader.reports.render import model_md

MODEL_FIELDS = frozenset({"catalyst_type", "risk_flags", "verdict", "verdict_source", "confidence"})
PLAYBOOK_FILE = "playbook.md"


def playbook(conn: sqlite3.Connection, cfg: LessonsConfig) -> list[PlaybookEntry]:
    """The promoted lessons with their numbers, ranked, at most ``max_playbook_lessons``."""
    entries = []
    for r in conn.execute(
        "SELECT * FROM lessons WHERE status = 'promoted' AND n_subset IS NOT NULL "
        "AND effect_r IS NOT NULL AND ci_low IS NOT NULL AND ci_high IS NOT NULL ORDER BY id"
    ):
        try:
            conditions = validate(r["evidence_query_json"] or "")
        except QueryError:
            continue  # never promoted with one; skipped all the same
        entries.append(
            PlaybookEntry(
                id=r["id"],
                suggested_use=r["suggested_use"] or "",
                n=int(r["n_subset"]),
                effect_r=float(r["effect_r"]),
                ci=(float(r["ci_low"]), float(r["ci_high"])),
                conditions=tuple((c.field, c.op, c.value) for c in conditions),
                statement=r["statement"] or "",
            )
        )
    entries.sort(key=lambda e: (-e.n * abs(e.effect_r), e.id))
    return entries[: cfg.max_playbook_lessons]


def vet_features(s: Signal) -> dict[str, Any]:
    """The S§6.2 fields a vet candidate has at arming, as the lessons' rows carry them; the
    rest (Book B's own, ``minutes_to_trigger``) are unknown."""
    feats = s.features

    def number(name: str) -> float | None:
        value = feats.get(name)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    return {
        "gap_pct": s.gap_pct,
        "rel_vol": s.rel_vol,
        "rel_vol_rank": float(s.rel_vol_rank),
        "or_range_atr": number("or_range_atr"),
        "or_return_pct": number("or_return_pct"),
        "price": None if s.trigger_price is None else float(s.trigger_price),
        "atr_pct": number("atr_pct"),
        "regime_spy_trend": feats.get("regime_spy_trend"),
        "regime_vol_bucket": feats.get("regime_vol_bucket"),
        "day_of_week": feats.get("day_of_week"),
        "event_day": feats.get("event_day") if isinstance(feats.get("event_day"), bool) else None,
    }


def matching(entries: Sequence[PlaybookEntry], known: Mapping[str, Any]) -> list[str]:
    """The ids of the lessons whose conditions all hold on ``known``, at least one checked.
    Conditions on Book B's own fields are the model's to apply; one on any other field
    ``known`` lacks means no match."""
    out = []
    for e in entries:
        checked = [
            Condition(field, op, value)
            for field, op, value in e.conditions
            if field not in MODEL_FIELDS and known.get(field) is not None
        ]
        unknown = [
            field
            for field, _, _ in e.conditions
            if field not in MODEL_FIELDS and known.get(field) is None
        ]
        if checked and not unknown and all(c.holds(known[c.field]) for c in checked):
            out.append(e.id)
    return out


def render(entries: Sequence[PlaybookEntry], *, as_of: datetime) -> str:
    """``runtime/playbook.md``: the model's statements stay inert (``reports.render``)."""
    lines = [
        "# Playbook",
        "",
        f"Promoted lessons as of {fmt_iso(as_of)}, ranked by n x |effect|. Soft guidance for "
        "Book B's brief and vet; a lesson never changes a rule, a risk limit or a parameter "
        "(STRATEGY.md 6.4).",
        "",
    ]
    if not entries:
        lines.append("(no promoted lessons)")
    for e in entries:
        lines += [
            f"## {e.id} ({e.suggested_use})",
            "",
            f"- {model_md(e.statement)}",
            f"- n {e.n}, effect {e.effect_r:+.3f} R, CI [{e.ci[0]:+.3f}, {e.ci[1]:+.3f}]",
            "- conditions: "
            + "; ".join(f"{field} {op} {_value(value)}" for field, op, value in e.conditions),
            "",
        ]
    return "\n".join(lines) + "\n"


def _value(value: Any) -> str:
    if isinstance(value, tuple):
        return "[" + ", ".join(value) + "]"
    return str(value).lower() if isinstance(value, bool) else str(value)


def write_playbook(path: Path, entries: Sequence[PlaybookEntry], *, as_of: datetime) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(entries, as_of=as_of), encoding="utf-8")
    return path
