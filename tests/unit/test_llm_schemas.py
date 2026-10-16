"""Claude's output models and their wire schemas (P4.1; A§5, A§7.2-7.3, DECISIONS D-061)."""

import json
import re
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, get_args

import pytest
from jsonschema import Draft7Validator
from pydantic import BaseModel, Field, ValidationError

from trader.llm import schemas
from trader.llm.schemas import (
    OUTPUTS,
    SCHEMA_MODES,
    CatalystType,
    DailyReview,
    DslField,
    DslOp,
    OutcomeFactor,
    RiskFlag,
    VetBatch,
    clamp_to_schema,
    schema_bytes,
    wire_schema,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
VALID = sorted(FIXTURES.glob("*.valid.json"))
INVALID = sorted(FIXTURES.glob("*.invalid-*.json"))
CONSTRAINTS = {
    "minLength", "maxLength", "minItems", "maxItems", "minimum", "maximum",
    "exclusiveMinimum", "exclusiveMaximum", "pattern", "multipleOf", "format", "uniqueItems",
}  # fmt: skip
NOT_INLINE = {"$ref", "$defs", "definitions", "$schema", "title", "default"}
NOT_DRAFT_07 = {"prefixItems", "unevaluatedProperties", "unevaluatedItems", "dependentRequired",
                "dependentSchemas", "$anchor", "$dynamicRef", "$dynamicAnchor"}  # fmt: skip
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


# ---------------------------------------------------------------- the enums against STRATEGY.md


def _strategy() -> str:
    return (ROOT / "docs" / "STRATEGY.md").read_text(encoding="utf-8")


def _words(text: str) -> list[str]:
    """Comma-separated words, backticks stripped; each must be a bare identifier, so a value
    the doc adds in any spelling makes the comparison fail rather than vanish."""
    words = [w.strip().strip("`").strip() for w in text.split(",") if w.strip()]
    assert all(IDENTIFIER.fullmatch(w) for w in words), words
    return words


def _block_after(marker: str) -> list[str]:
    """The words of the first code block after ``marker``, comment lines dropped."""
    text = _strategy()
    found = re.search(r"```[a-z]*\n(.*?)```", text[text.index(marker) :], re.S)
    assert found is not None, marker
    lines = [ln for ln in found.group(1).splitlines() if not ln.lstrip().startswith("#")]
    return _words(",".join(lines))


def _dsl_fields() -> list[str]:
    """Every word after the colon of each bullet between "Whitelisted fields" and "Ops"."""
    text = _strategy()
    section = text[text.index("**Whitelisted fields:**") : text.index("**Ops:**")]
    bullets = [ln for ln in section.splitlines()[1:] if ln.strip().startswith("- ")]
    assert bullets, "no whitelist bullets"
    return [w for ln in bullets for w in _words(ln.split(":", 1)[1])]


def _dsl_ops() -> list[str]:
    line = next(ln for ln in _strategy().splitlines() if "**Ops:**" in ln)
    return _words(line.split("**Ops:**", 1)[1].split("(", 1)[0])


ENUMS: list[tuple[str, Any, Callable[[], list[str]], int]] = [
    ("catalyst_type", CatalystType, lambda: _block_after("`catalyst_type`:"), 18),
    ("risk_flags", RiskFlag, lambda: _block_after("`risk_flags` (zero or more):"), 13),
    ("dsl_field", DslField, _dsl_fields, 17),
    ("dsl_op", DslOp, _dsl_ops, 8),
    ("outcome_factor", OutcomeFactor, lambda: _block_after("`OutcomeFactor`:"), 19),
]


@pytest.mark.parametrize(("name", "enum", "listed", "count"), ENUMS, ids=[e[0] for e in ENUMS])
def test_enums_match_strategy_lists(
    name: str, enum: Any, listed: Callable[[], list[str]], count: int
) -> None:
    assert list(get_args(enum)) == listed()
    assert len(get_args(enum)) == count


def test_the_strategy_parsers_see_an_added_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review (D-061): a whitelist name with a digit, or a bare one, used to slip past."""
    text = _strategy()
    for edit in (
        ("`event_day`", "`event_day`, `adv14`"),
        ("`event_day`", "`event_day`, spread_pct"),
        ("lte, in, contains`", "lte, in, contains, between`"),
    ):
        assert text.count(edit[0]) >= 1
        monkeypatch.setattr(sys.modules[__name__], "_strategy", lambda e=edit: text.replace(*e, 1))
        assert (_dsl_fields(), _dsl_ops()) != (list(get_args(DslField)), list(get_args(DslOp)))


# ---------------------------------------------------------------- the schemas


def test_committed_schemas_match_the_code() -> None:
    assert schemas.check(ROOT) == []


def _nodes(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield node
    for prop in node.get("properties", {}).values():
        yield from _nodes(prop)
    if isinstance(node.get("items"), dict):
        yield from _nodes(node["items"])
    for branch in node.get("anyOf", []):
        yield from _nodes(branch)


@pytest.mark.parametrize("mode", SCHEMA_MODES)
@pytest.mark.parametrize("stem", OUTPUTS)
def test_each_schema_is_inlined_and_under_8kb(stem: str, mode: schemas.SchemaMode) -> None:
    schema = wire_schema(OUTPUTS[stem], mode)
    Draft7Validator.check_schema(schema)
    nodes = list(_nodes(schema))
    keys = {k for n in nodes for k in n}
    assert not keys & (NOT_INLINE | NOT_DRAFT_07)
    assert all(n["additionalProperties"] is False for n in nodes if n.get("type") == "object")
    wire = schema_bytes(schema)
    assert wire.isascii()
    assert len(wire) < schemas.MAX_SCHEMA_BYTES == 8192
    assert json.loads(wire) == schema


def test_fields_named_like_schema_keywords_survive() -> None:
    """Review (D-061): the transforms recurse into schemas, never into the ``properties``
    mapping, whose keys are field names; and a dict field keeps its value schema."""

    class Odd(BaseModel):
        title: str = Field(max_length=10)
        pattern: str = Field(max_length=10)
        default: int | None = None
        tags: dict[str, int]

    sample = {"title": "t", "pattern": "p", "default": None, "tags": {"a": 1}}
    for mode in SCHEMA_MODES:
        schema = wire_schema(Odd, mode)
        assert list(schema["properties"]) == ["title", "pattern", "default", "tags"]
        assert schema["properties"]["tags"]["additionalProperties"] == {"type": "integer"}
        assert Draft7Validator(schema).is_valid(sample)
    safe = wire_schema(Odd, "wire_safe")
    assert safe["required"] == ["title", "pattern", "default", "tags"]
    assert safe["properties"]["title"] == {"type": "string", "description": "at most 10 characters"}


def _model(path: Path) -> type[BaseModel]:
    return OUTPUTS[path.name.split(".")[0]]


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_every_output_has_fixtures() -> None:
    assert {_model(p) for p in VALID} == set(OUTPUTS.values())
    assert {_model(p) for p in INVALID} == set(OUTPUTS.values())


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name)
def test_fixtures_validate_with_draft7_and_pydantic(path: Path) -> None:
    model, data = _model(path), _load(path)
    for mode in SCHEMA_MODES:
        errors = [e.message for e in Draft7Validator(wire_schema(model, mode)).iter_errors(data)]
        assert errors == [], mode
    model.model_validate(data, context={"pack_id": data["pack_id"]})


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.name)
def test_invalid_fixtures_fail_draft7_and_pydantic(path: Path) -> None:
    """Rejected by the schema the call sends, and by pydantic, the final check, even where a
    ``wire_safe`` schema would let them through."""
    model, data = _model(path), _load(path)
    assert not Draft7Validator(wire_schema(model, "full")).is_valid(data)
    with pytest.raises(ValidationError):
        model.model_validate(data)


STRICTNESS = [  # (output, where, value, the schema accepts, pydantic accepts)
    ("vet_batch", ("assessments", 0, "catalyst_fresh"), "yes", False, False),
    ("vet_batch", ("assessments", 0, "catalyst_fresh"), 1, False, False),
    ("vet_batch", ("assessments", 0, "confidence"), "0.7", False, False),
    ("vet_batch", ("assessments", 0, "confidence"), 1, True, True),  # an integer is a number
    ("vet_batch", ("assessments", 0, "confidence"), float("nan"), True, False),
    ("vet_batch", ("assessments", 0, "symbol"), "ABC\n", True, False),  # Python re's $
    ("vet_batch", ("assessments", 0, "reason"), "a\ud800b", True, False),  # a lone surrogate
    ("daily_review", ("process_issues", 1, "existing_issue_id"), "3", False, False),
    ("daily_review", ("process_issues", 1, "existing_issue_id"), 3.5, False, False),
    ("daily_review", ("process_issues", 1, "existing_issue_id"), 3.0, True, False),
]


@pytest.mark.parametrize(("stem", "where", "value", "by_schema", "by_pydantic"), STRICTNESS)
def test_pydantic_is_never_looser_than_the_schema(
    stem: str, where: tuple[Any, ...], value: Any, by_schema: bool, by_pydantic: bool
) -> None:
    """Pydantic is the final check (A§7.3). Strict, it refuses everything the schema refuses
    ("yes" for true, "0.7" for a number), and a few things a Python Draft-7 validator lets
    through: a trailing newline under ``$``, NaN against a range, 3.0 for an integer."""
    data = _load(FIXTURES / f"{stem}.valid.json")
    node = data
    for key in where[:-1]:
        node = node[key]
    node[where[-1]] = value
    model = OUTPUTS[stem]
    assert Draft7Validator(wire_schema(model)).is_valid(data) is by_schema
    if by_pydantic:
        model.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            model.model_validate(data)


@pytest.mark.parametrize("stem", OUTPUTS)
def test_every_output_echoes_the_pack_id(stem: str) -> None:
    for mode in SCHEMA_MODES:
        assert "pack_id" in wire_schema(OUTPUTS[stem], mode)["required"]
    assert wire_schema(OUTPUTS[stem])["properties"]["pack_id"]["pattern"] == schemas.PACK_ID
    assert next(iter(OUTPUTS[stem].model_fields)) == "pack_id"  # first, so it is written first


def test_a_well_formed_but_wrong_pack_id_fails_given_the_pack() -> None:
    """Review (D-061): the pattern alone accepts any well-formed id. With the pack's id in
    the validation context, an answer about another pack fails, and the error doesn't name
    the expected id, so a repair has to read it from the pack."""
    data = _load(FIXTURES / "vet_batch.valid.json")
    VetBatch.model_validate(data, context={"pack_id": "20261103-vet-1"})
    with pytest.raises(ValidationError, match="not the pack's") as wrong:
        VetBatch.model_validate(data, context={"pack_id": "20261103-vet-2"})
    assert "20261103-vet-2" not in str(wrong.value)
    VetBatch.model_validate(data)  # without a context, only the format is checked


@pytest.mark.parametrize("stem", OUTPUTS)
def test_wire_safe_carries_no_constraint_keywords(stem: str) -> None:
    nodes = list(_nodes(wire_schema(OUTPUTS[stem], "wire_safe")))
    assert not {k for n in nodes for k in n} & CONSTRAINTS
    for node in nodes:
        if node.get("type") == "object":
            assert node["required"] == list(node["properties"])


def test_wire_safe_says_the_constraints_in_words() -> None:
    batch = wire_schema(VetBatch, "wire_safe")["properties"]
    item = batch["assessments"]["items"]["properties"]
    assert batch["assessments"]["description"] == "at most 25 items"
    assert item["reason"]["description"] == "at most 240 characters"
    assert item["confidence"]["description"] == "from 0 to 1"
    assert item["symbol"]["description"] == "matching the pattern ^[A-Z]{1,6}$"
    review = wire_schema(DailyReview, "wire_safe")["properties"]
    query = review["lesson_proposals"]["items"]["properties"]["evidence_query"]["properties"]
    assert query["filters"]["description"] == "1 to 4 items"
    assert query["compare_to"] == {"const": "complement", "type": "string"}  # required, constant
    issue = review["process_issues"]["items"]["properties"]["existing_issue_id"]
    assert {"type": "null"} in issue["anyOf"]  # required, but still optional by being null


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown schema mode"):
        wire_schema(VetBatch, "loose")  # type: ignore[arg-type]


# ---------------------------------------------------------------- clamping


def _assessment(**changes: Any) -> dict[str, Any]:
    base = _load(FIXTURES / "vet_batch.valid.json")["assessments"][1]
    return {**base, **changes}


def test_clamp_truncates_lengths_but_never_fixes_enums() -> None:
    schema = wire_schema(VetBatch)
    long = _assessment(reason="x" * 300, risk_flags=["stale_news"] * 9)
    data = {"pack_id": "20261103-vet-1", "assessments": [long, _assessment(symbol="QRST")]}
    with pytest.raises(ValidationError):
        VetBatch.model_validate(data)
    clamped, changed = clamp_to_schema(data, schema)
    batch = VetBatch.model_validate(clamped)
    assert len(batch.assessments[0].reason) == 240
    assert len(batch.assessments[0].risk_flags) == 8
    assert changed == 2
    assert data["assessments"][0]["reason"] == "x" * 300  # the input is untouched

    bad = {"pack_id": "the pack", "assessments": [_assessment(verdict="maybe", symbol="xyz")]}
    out, changed = clamp_to_schema(bad, schema)
    assert (out, changed) == (bad, 0)  # enums, patterns and types go to the repair
    with pytest.raises(ValidationError):
        VetBatch.model_validate(out)


def test_clamp_leaves_numbers_to_validation() -> None:
    """Review (D-061; plan decision 7 clamps lengths and counts only): a confidence read on a
    0-100 scale would have become 1.0, the highest, instead of getting its repair."""
    data = {"pack_id": "20261103-vet-1", "assessments": [_assessment(confidence=85)]}
    out, changed = clamp_to_schema(data, wire_schema(VetBatch))
    assert (out, changed) == (data, 0)
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        VetBatch.model_validate(out)


def test_clamp_never_cuts_a_list_whose_items_count_as_a_set() -> None:
    """A lesson's filters are ANDed, and there is one assessment per candidate and one review
    per target: cutting any of them changes what the answer says (A§7.3, D-061)."""
    batch = {"pack_id": "20261103-vet-1", "assessments": [_assessment()] * 26}
    out, changed = clamp_to_schema(batch, wire_schema(VetBatch))
    assert (len(out["assessments"]), changed) == (26, 0)
    review = _load(FIXTURES / "daily_review.valid.json")
    review["signal_reviews"] = review["signal_reviews"] * 11  # 22
    query = review["lesson_proposals"][0]["evidence_query"]
    query["filters"] = query["filters"] * 3  # 6
    out, changed = clamp_to_schema(review, wire_schema(DailyReview))
    assert len(out["signal_reviews"]) == 22
    assert len(out["lesson_proposals"][0]["evidence_query"]["filters"]) == 6
    assert changed == 0
    with pytest.raises(ValidationError):
        DailyReview.model_validate(out)


def test_clamp_cuts_the_tails_of_free_lists() -> None:
    review = _load(FIXTURES / "daily_review.valid.json")
    review["process_issues"] = review["process_issues"] * 6  # 12
    review["human_notes"] = review["human_notes"] * 7
    review["lesson_proposals"] = review["lesson_proposals"] * 4
    review["signal_reviews"][0]["factors"] = ["other"] * 5
    out, changed = clamp_to_schema(review, wire_schema(DailyReview))
    parsed = DailyReview.model_validate(out)
    assert (len(parsed.process_issues), len(parsed.human_notes)) == (10, 5)
    assert (len(parsed.lesson_proposals), len(parsed.signal_reviews[0].factors)) == (3, 4)
    assert changed == 4


def test_clamp_follows_any_of_and_leaves_patterned_strings_alone() -> None:
    """Review (D-061): no branch in today's schemas carries a limit, so a small schema pins
    the path an optional constrained field will take. A string with a pattern is an
    identifier: cutting it can't make it right."""
    schema = {
        "type": "object",
        "properties": {
            "note": {"anyOf": [{"type": "string", "maxLength": 3}, {"type": "null"}]},
            "code": {"type": "string", "maxLength": 6, "pattern": "^[A-Z]{1,6}H$"},
        },
    }
    assert clamp_to_schema({"note": "abcdef"}, schema) == ({"note": "abc"}, 1)
    assert clamp_to_schema({"note": None}, schema) == ({"note": None}, 0)
    assert clamp_to_schema({"code": "ABCDEFGH"}, schema) == ({"code": "ABCDEFGH"}, 0)
    review = _load(FIXTURES / "daily_review.valid.json")
    review["process_issues"][1]["description"] = "y" * 301
    review["lesson_proposals"][0]["evidence_query"]["filters"][0]["value"] = "z" * 500  # no limit
    review["unknown"] = "x" * 5000
    out, changed = clamp_to_schema(review, wire_schema(DailyReview))
    assert changed == 1
    assert len(out["process_issues"][1]["description"]) == 300
    assert out["process_issues"][1]["existing_issue_id"] == 3
    assert out["unknown"] == "x" * 5000
    with pytest.raises(ValidationError):
        DailyReview.model_validate(out)


def test_clamp_survives_deep_nesting() -> None:
    """Review (D-061): the walk follows the schema, never the junk, so a deeply nested value
    reaches validation instead of raising RecursionError."""
    deep: Any = []
    for _ in range(5000):
        deep = [deep]
    batch = {"pack_id": "20261103-vet-1", "assessments": [], "junk": deep}
    out, changed = clamp_to_schema(batch, wire_schema(VetBatch))
    assert changed == 0
    assert out["junk"] is deep
    review = _load(FIXTURES / "daily_review.valid.json")
    review["lesson_proposals"][0]["evidence_query"]["filters"][0]["value"] = [deep]
    assert clamp_to_schema(review, wire_schema(DailyReview))[1] == 0


# ---------------------------------------------------------------- the committed files


def test_the_module_writes_then_checks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert schemas.main(["--check", "--root", str(tmp_path)]) == 1
    assert "schemas/vet_batch.json is missing" in capsys.readouterr().err
    assert schemas.main(["--write", "--root", str(tmp_path)]) == 0
    assert schemas.main(["--check", "--root", str(tmp_path)]) == 0
    stale = tmp_path / "schemas" / "vet_batch.json"
    stale.write_text(stale.read_text(encoding="utf-8").replace('"maxItems": 25', '"maxItems": 26'),
                     encoding="utf-8")  # fmt: skip
    assert schemas.main(["--check", "--root", str(tmp_path)]) == 1
    assert "schemas/vet_batch.json differs from the code" in capsys.readouterr().err
    written = (tmp_path / "schemas" / "daily_review.wire.json").read_bytes()
    assert written.endswith(b"}\n")
    assert b"\r" not in written


def test_the_check_sees_a_reordering_but_not_crlf(tmp_path: Path) -> None:
    """Key order is the order Claude writes in, so a file with the same keys in another order
    is stale; a checkout's CRLF line ends are not."""
    schemas.write(tmp_path)
    target = tmp_path / "schemas" / "vet_batch.json"
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
    assert schemas.check(tmp_path) == []
    target.write_text(json.dumps(wire_schema(VetBatch), indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")  # fmt: skip
    assert schemas.check(tmp_path) == ["schemas/vet_batch.json differs from the code"]


def test_the_check_sees_a_stray_file_and_an_oversized_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    schemas.write(tmp_path)
    (tmp_path / "schemas" / "vet.json").write_text("{}\n", encoding="utf-8")
    assert schemas.check(tmp_path) == ["schemas/vet.json is not made by the code"]
    (tmp_path / "schemas" / "vet.json").unlink()
    monkeypatch.setattr(schemas, "MAX_SCHEMA_BYTES", 2000)
    assert schemas.check(tmp_path) == [
        "schemas/daily_review.json is over 2000 bytes compacted",
        "schemas/daily_review.wire.json is over 2000 bytes compacted",
    ]
