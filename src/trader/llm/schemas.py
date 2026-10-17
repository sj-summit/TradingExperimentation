"""Claude's output models (A§5) and the JSON Schemas sent with ``claude --json-schema`` (A§7.2).

The models are the last word on what Claude may say (STRATEGY.md §5.1): enums for every
category, bounded free text and lists, and a ``pack_id`` echoed from the context pack, which
shows the answer is about the pack that was sent.

- ``wire_schema(model, mode)`` builds the schema a call sends: JSON Schema draft-07 with every
  ``$ref`` inlined, titles and defaults dropped, and every object closed
  (``additionalProperties: false``). It names no ``$schema``: nothing in it differs between
  draft-07 and 2020-12, so it reads the same whichever draft the CLI assumes.
  - ``full`` keeps the constraints: lengths, counts, ranges and patterns.
  - ``wire_safe`` moves them into each property's description and makes every property
    required; the optional ones stay satisfiable by being nullable or constant. It is the
    subset strict structured outputs accept, for use if G4a shows the CLI refuses ``full``.
- ``schema_bytes(schema)`` is the compact form, ASCII only, in the models' field order.
- ``clamp_to_schema(data, schema)`` cuts over-long free text and the tails of free lists before
  validation (A§7.3). Everything else is left to validation and the call's one repair: types,
  enums, patterns, ranges, required fields, and the lists whose items count as a set.
- Validating with ``context={"pack_id": ...}`` also requires the echoed ``pack_id`` to match.
- ``python -m trader.llm.schemas --write|--check`` writes or checks ``schemas/*.json``.
"""

import argparse
import json
import sys
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

# ---------------------------------------------------------------- enums (STRATEGY.md §5.5, §6)

CatalystType = Literal[
    "earnings", "guidance", "fda_regulatory", "clinical_trial", "m_and_a",
    "contract_partnership", "product_launch", "analyst_action", "offering_dilution",
    "reverse_split", "legal_regulatory", "management_change", "macro_sector",
    "sympathy_move", "social_meme", "crypto_related", "no_identifiable_news", "other",
]  # fmt: skip
RiskFlag = Literal[
    "dilution_offering", "shelf_or_atm", "reverse_split_recent", "promotional_or_pump",
    "stale_news", "rumor_unconfirmed", "binary_event_today", "earnings_after_close_today",
    "halt_or_luld_risk", "extreme_gap", "sympathy_only", "conflicting_reports",
    "suspicious_content",
]  # fmt: skip
DslField = Literal[  # S§6.2's whitelist: features recorded before the outcome
    "catalyst_type", "risk_flags", "verdict", "verdict_source", "confidence",
    "gap_pct", "rel_vol", "rel_vol_rank", "or_range_atr", "or_return_pct",
    "price", "atr_pct", "minutes_to_trigger",
    "regime_spy_trend", "regime_vol_bucket", "day_of_week", "event_day",
]  # fmt: skip
DslOp = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "contains"]
OutcomeFactor = Literal[  # S§6.5: hindsight labels, never DSL fields
    "catalyst_strong", "catalyst_weak_or_none", "catalyst_stale", "dilution_or_offering",
    "extreme_gap", "thin_liquidity", "sympathy_only", "event_day",
    "late_entry", "entry_slippage", "partial_fill",
    "trend_continuation", "false_breakout", "faded_gap", "stopped_by_noise",
    "news_after_entry", "market_tailwind", "market_headwind",
    "other",
]  # fmt: skip
Verdict = Literal["allow", "neutral", "veto"]
DayMode = Literal["normal", "reduced", "no_trade"]
DAY_MODE_MULTIPLIERS: Mapping[DayMode, Decimal] = MappingProxyType(  # S§5.1's size multipliers
    {"normal": Decimal(1), "reduced": Decimal("0.5"), "no_trade": Decimal(0)}
)

PACK_ID = r"^[A-Za-z0-9-]{1,64}$"
SYMBOL = r"^[A-Z]{1,6}$"  # core.ids.is_ticker


class _Output(BaseModel):
    """Strict, so pydantic is never looser than the schema: no "yes" for true, no "0.7" for a
    number, no 3.0 for an integer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _Answer(_Output):
    """A whole answer: it echoes the pack's ``pack_id``. The format is checked always; with
    ``context={"pack_id": ...}``, so is the value, and the error never names the expected id."""

    pack_id: str = Field(pattern=PACK_ID)

    @field_validator("pack_id")
    @classmethod
    def _matches_the_pack(cls, value: str, info: ValidationInfo) -> str:
        expected = info.context.get("pack_id") if isinstance(info.context, dict) else None
        if expected is not None and value != expected:
            raise ValueError("pack_id is not the pack's: copy it from the context pack")
        return value


# ---------------------------------------------------------------- Book B's verdicts (S§5)


class SymbolAssessment(_Output):
    symbol: str = Field(pattern=SYMBOL)
    catalyst_type: CatalystType
    catalyst_fresh: bool
    catalyst_confirmed: bool
    risk_flags: list[RiskFlag] = Field(max_length=8)
    verdict: Verdict
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(max_length=240)


class PremarketBrief(_Answer):
    day_mode: DayMode
    day_mode_reason: str = Field(max_length=300)
    market_notes: str = Field(max_length=600)  # informational only, never parsed for actions
    assessments: list[SymbolAssessment] = Field(max_length=60)


class VetBatch(_Answer):
    assessments: list[SymbolAssessment] = Field(max_length=25)


# ---------------------------------------------------------------- the post-close review (S§6)


class EvidenceFilter(_Output):
    field: DslField
    op: DslOp
    value: str | float | bool | list[str]  # unbounded in A§5; S§6.2's evaluator checks it


class EvidenceQuery(_Output):
    filters: list[EvidenceFilter] = Field(min_length=1, max_length=4)
    compare_to: Literal["complement"] = "complement"
    metric: Literal["shadow_r"] = "shadow_r"


class LessonProposal(_Output):
    statement: str = Field(max_length=200)
    rationale: str = Field(max_length=400)
    evidence_query: EvidenceQuery
    claimed_direction: Literal["subset_worse", "subset_better"]
    suggested_use: Literal["veto_hint", "caution_hint", "allow_hint"]


class SignalReview(_Output):
    signal_id: str  # unbounded in A§5; a review for anything but a target is dropped (A§7.3)
    went_well: str = Field(max_length=200)
    went_badly: str = Field(max_length=200)
    why: str = Field(max_length=300)
    factors: list[OutcomeFactor] = Field(max_length=4)
    knowable_at_decision: Literal["yes", "partly", "no"]
    b_process: Literal["good_process", "bad_process", "unclear", "not_applicable"]
    b_note: str = Field(max_length=200)


class ProcessIssue(_Output):
    existing_issue_id: int | None = None  # links an open issue from the pack's history
    severity: Literal["info", "warn", "error"]
    component: str = Field(max_length=40)
    description: str = Field(max_length=300)


class HumanNote(_Output):
    existing_note_id: int | None = None  # links an open note from the pack's history
    note: str = Field(max_length=300)


class DailyReview(_Answer):
    summary: str = Field(max_length=2000)
    signal_reviews: list[SignalReview] = Field(max_length=20)
    process_issues: list[ProcessIssue] = Field(max_length=10)
    lesson_proposals: list[LessonProposal] = Field(max_length=3)
    human_notes: list[HumanNote] = Field(max_length=5)


# ---------------------------------------------------------------- wire schemas (A§7.2)

SchemaMode = Literal["full", "wire_safe"]
SCHEMA_MODES: tuple[SchemaMode, ...] = ("full", "wire_safe")
MAX_SCHEMA_BYTES = 8 * 1024  # passed inline: Windows limits the command line (A§7.2)
OUTPUTS: dict[str, type[_Answer]] = {  # file stem -> model, one per role
    "premarket_brief": PremarketBrief,
    "vet_batch": VetBatch,
    "daily_review": DailyReview,
}
SCHEMAS_DIR = Path("schemas")

# The constraint keywords wire_safe moves into descriptions: (low, high, unit) per pair.
_RANGES: tuple[tuple[str, str, str], ...] = (
    ("minLength", "maxLength", "character"),
    ("minItems", "maxItems", "item"),
    ("minimum", "maximum", ""),
)
_CONSTRAINTS = frozenset(
    {k for pair in _RANGES for k in pair[:2]} | {"exclusiveMinimum", "exclusiveMaximum", "pattern"}
)
_DROPPED = frozenset({"title", "default"})
_SCHEMA_LISTS = ("anyOf", "allOf", "oneOf")  # keywords whose value is a list of schemas


def wire_schema(model: type[BaseModel], mode: SchemaMode = "full") -> dict[str, Any]:
    """The draft-07 schema of ``model`` as a call sends it (see the module docstring)."""
    if mode not in SCHEMA_MODES:
        raise ValueError(f"unknown schema mode {mode!r}")
    raw = model.model_json_schema(mode="validation")
    defs = raw.pop("$defs", {})
    schema: dict[str, Any] = _inline(raw, defs)
    if mode == "wire_safe":
        safe: dict[str, Any] = _wire_safe(schema)
        return safe
    return schema


def schema_bytes(schema: Mapping[str, Any]) -> bytes:
    """Compact JSON, ASCII only: the bytes a call passes on the command line. Keys keep the
    models' order, which is roughly the order Claude writes its answer in: an assessment
    names its symbol first, a review its signal first."""
    return json.dumps(schema, separators=(",", ":"), ensure_ascii=True).encode()


def _subschemas(
    node: Mapping[str, Any], rebuild: Callable[[Mapping[str, Any]], dict[str, Any]]
) -> dict[str, Any]:
    """``node`` with ``rebuild`` applied to each schema it holds: the values of ``properties``
    (never the mapping itself, whose keys are field names), ``items``, an
    ``additionalProperties`` schema, and each branch of ``anyOf``/``allOf``/``oneOf``."""
    out = dict(node)
    if isinstance(node.get("properties"), Mapping):
        out["properties"] = {name: rebuild(s) for name, s in node["properties"].items()}
    for key in ("items", "additionalProperties"):
        if isinstance(node.get(key), Mapping):
            out[key] = rebuild(node[key])
    for key in _SCHEMA_LISTS:
        if isinstance(node.get(key), list):
            out[key] = [rebuild(s) for s in node[key]]
    return out


def _inline(node: Mapping[str, Any], defs: Mapping[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if ref is not None:
        name = ref.removeprefix("#/$defs/")
        if name == ref or name not in defs:
            raise ValueError(f"unexpected $ref {ref!r}")
        return _inline({**defs[name], **{k: v for k, v in node.items() if k != "$ref"}}, defs)
    out = _subschemas(
        {k: v for k, v in node.items() if k not in _DROPPED}, lambda s: _inline(s, defs)
    )
    if out.get("type") == "object" and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


def _wire_safe(node: Mapping[str, Any]) -> dict[str, Any]:
    out = _subschemas({k: v for k, v in node.items() if k not in _CONSTRAINTS}, _wire_safe)
    said = _describe(node)
    if said:
        out["description"] = f"{out['description']} ({said})" if "description" in out else said
    if out.get("type") == "object" and "properties" in out:
        out["required"] = list(out["properties"])
    return out


def _describe(node: Mapping[str, Any]) -> str:
    """``node``'s constraints in words, e.g. "1 to 4 items" or "at most 240 characters"."""

    def units(n: Any, unit: str) -> str:
        return f"{n} {unit}{'' if n == 1 else 's'}" if unit else f"{n}"

    said = []
    for low_key, high_key, unit in _RANGES:
        low, high = node.get(low_key), node.get(high_key)
        if low is not None and high is not None:
            said.append(f"{low} to {units(high, unit)}" if unit else f"from {low} to {high}")
        elif low is not None:
            said.append(f"at least {units(low, unit)}")
        elif high is not None:
            said.append(f"at most {units(high, unit)}")
    if "exclusiveMinimum" in node:
        said.append(f"more than {node['exclusiveMinimum']}")
    if "exclusiveMaximum" in node:
        said.append(f"less than {node['exclusiveMaximum']}")
    if "pattern" in node:
        said.append(f"matching the pattern {node['pattern']}")
    return "; ".join(said)


# ---------------------------------------------------------------- clamping (A§7.3)

# Lists never cut: their items count as a set. A lesson's filters are ANDed, so dropping one
# would test a broader subset than the proposal states. One assessment per candidate and one
# review per target: a stray entry ahead of a real one would push it out, so those lists are
# filtered by membership before the clamp and left whole by it (A§7.3).
UNCUT = frozenset({"filters", "assessments", "signal_reviews"})


def clamp_to_schema(data: Any, schema: Mapping[str, Any]) -> tuple[Any, int]:
    """``data`` with over-long strings cut to their ``maxLength`` and over-long lists to their
    ``maxItems``, plus how many were cut. Only free text and free lists are cut: never a string
    with a pattern, a list in ``UNCUT`` or a number, and nothing is added. Those failures go to
    validation and the call's one repair. ``data`` itself is left unchanged. Pass the ``full``
    schema whichever mode the call sent: ``wire_safe`` carries no limits to clamp to."""
    clamper = _Clamper()
    return clamper.walk(data, schema, cut=True), clamper.count


class _Clamper:
    """Walks ``data`` along its schema, never into what the schema doesn't describe, and
    builds new containers instead of changing the input."""

    def __init__(self) -> None:
        self.count = 0

    def walk(self, data: Any, schema: Mapping[str, Any], *, cut: bool) -> Any:
        if "anyOf" in schema:
            branch = next((s for s in schema["anyOf"] if _fits(data, s)), None)
            return data if branch is None else self.walk(data, branch, cut=cut)
        if not _fits(data, schema):
            return data
        if isinstance(data, str):
            limit = schema.get("maxLength")
            if limit is not None and len(data) > limit and "pattern" not in schema:
                self.count += 1
                return data[:limit]
        elif isinstance(data, list):
            limit = schema.get("maxItems")
            if cut and limit is not None and len(data) > limit:
                self.count += 1
                data = data[:limit]
            items = schema.get("items")
            if isinstance(items, Mapping):
                return [self.walk(v, items, cut=True) for v in data]
        elif isinstance(data, dict):
            props = schema.get("properties", {})
            return {
                k: self.walk(v, props[k], cut=k not in UNCUT) if k in props else v
                for k, v in data.items()
            }
        return data


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _fits(value: Any, schema: Mapping[str, Any]) -> bool:
    """Whether ``value`` has the JSON type ``schema`` names (any type when it names none)."""
    kind = schema.get("type")
    if kind is None:
        return True
    kinds = kind if isinstance(kind, list) else [kind]
    return any(_is_type(value, k) for k in kinds)


def _is_type(value: Any, kind: str) -> bool:
    match kind:
        case "string":
            return isinstance(value, str)
        case "array":
            return isinstance(value, list)
        case "object":
            return isinstance(value, dict)
        case "boolean":
            return isinstance(value, bool)
        case "null":
            return value is None
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return _is_number(value)
    return False


# ---------------------------------------------------------------- the committed files


def schema_files() -> Iterator[tuple[Path, dict[str, Any]]]:
    """Each committed file under ``schemas/`` and the schema the code makes for it:
    ``<output>.json`` in ``full`` mode, ``<output>.wire.json`` in ``wire_safe``."""
    for stem, model in OUTPUTS.items():
        yield SCHEMAS_DIR / f"{stem}.json", wire_schema(model, "full")
        yield SCHEMAS_DIR / f"{stem}.wire.json", wire_schema(model, "wire_safe")


def render(schema: Mapping[str, Any]) -> str:
    """A committed file's text: indented, ASCII, in the models' order, one trailing newline."""
    return json.dumps(schema, indent=2, ensure_ascii=True) + "\n"


def check(root: Path) -> list[str]:
    """Every way ``root/schemas/`` differs from the code, key order included, and any JSON
    file there the code doesn't make; empty when they agree. A checkout's CRLF line ends
    don't count."""
    problems = []
    expected = set()
    for path, schema in schema_files():
        target, name = root / path, path.as_posix()
        expected.add(name)
        if not target.is_file():
            problems.append(f"{name} is missing")
        elif target.read_text(encoding="utf-8").replace("\r\n", "\n") != render(schema):
            problems.append(f"{name} differs from the code")
        if len(schema_bytes(schema)) > MAX_SCHEMA_BYTES:
            problems.append(f"{name} is over {MAX_SCHEMA_BYTES} bytes compacted")
    folder = root / SCHEMAS_DIR
    if folder.is_dir():
        for stray in sorted(folder.glob("*.json")):
            name = (SCHEMAS_DIR / stray.name).as_posix()
            if name not in expected:
                problems.append(f"{name} is not made by the code")
    return problems


def write(root: Path) -> list[Path]:
    written = []
    for path, schema in schema_files():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render(schema), encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m trader.llm.schemas",
        description="Write or check the committed JSON Schemas of Claude's outputs (A§5).",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="write schemas/*.json")
    action.add_argument("--check", action="store_true", help="exit 1 if schemas/ is stale")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the repository root")
    args = parser.parse_args(argv)
    if args.write:
        for path in write(args.root):
            sys.stdout.write(f"wrote {path.as_posix()}\n")
        return 0
    problems = check(args.root)
    for problem in problems:
        sys.stderr.write(f"{problem}\n")
    if not problems:
        files = len(OUTPUTS) * len(SCHEMA_MODES)
        sys.stdout.write(f"schemas/ matches the code ({files} files)\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
