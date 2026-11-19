"""Prompts v1 and the LLM setup's fingerprint (prompts/; llm/epoch.py; DECISIONS D-065)."""

import json
import re
import shutil
from collections.abc import Iterator
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import REPO
from tests.unit.test_llm_context import (
    BRIEF_AT,
    DAY,
    INJECTION,
    PREMARKET,
    VET,
    VET_AT,
    candidate,
    common,
    filing,
    news,
    review_inputs,
    review_trade,
    shadow,
    signal,
    target,
    veto,
)
from trader.config import AppConfig, Settings, load_config
from trader.core.clock import et_datetime
from trader.llm import client, epoch
from trader.llm.context import (
    BadProcess,
    BookB,
    Evidence,
    FactorCount,
    History,
    IncidentView,
    LessonView,
    OpenIssue,
    OpenNote,
    ReviewNews,
    ReviewSignal,
    VetCandidate,
    encode,
    experiment_section,
    premarket_pack,
    review_pack,
    vet_pack,
)
from trader.llm.epoch import Fingerprint, epoch_fingerprint, note_fingerprint
from trader.llm.roles import prompt_sha256, role_specs
from trader.llm.schemas import DailyReview, PremarketBrief, VetBatch, wire_schema
from trader.memory.journal import Journal

PROMPTS = REPO / "prompts"
ROLES = ("premarket", "vet", "review")


@pytest.fixture
def cfg(repo: Path) -> AppConfig:
    return load_config(repo)


@pytest.fixture
def settings(cfg: AppConfig) -> Settings:
    return cfg.settings


def prompt(settings: Settings, role: str) -> str:
    return (PROMPTS / role_specs(settings)[role].prompt_file).read_text(encoding="utf-8")


# ---------------------------------------------------------------- the packs the prompts read


def body(pack: Any, **kw: Any) -> dict[str, Any]:
    data = json.loads(encode(pack, **kw).data)
    assert isinstance(data, dict)
    return data


def setup(cfg: AppConfig) -> dict[str, Any]:
    """What every live pack carries as ``experiment``."""
    return experiment_section(cfg.settings, cfg.risk)


def premarket_bodies(cfg: AppConfig) -> list[dict[str, Any]]:
    items = [
        news(1, "AAA", minutes_before=30, summary="s" * 500),
        news(2, "BBB", minutes_before=20, headline=INJECTION),
    ]
    evidence = {
        "AAA": Evidence(news=items[:1], filings=[filing(1, date(2026, 11, 2), form="424B5")]),
        "BBB": Evidence(news=items[1:]),  # filings unknown
    }
    pack = premarket_pack(
        as_of=BRIEF_AT,
        common=common(experiment=setup(cfg)),
        candidates=[candidate("AAA"), candidate("BBB")],
        evidence=evidence,
        limits=PREMARKET,
    )
    whole = body(pack)
    trimmed = body(pack, max_bytes=len(encode(pack).data) - 200)  # drops AAA's summary
    return [whole, trimmed]


def vet_bodies(cfg: AppConfig) -> list[dict[str, Any]]:
    picks = [
        VetCandidate(
            signal(s, i + 1),
            f"{s} Corp",
            "NYSE",
            Evidence(news=[news(10 + i, s, minutes_before=5, at=VET_AT, summary="t" * 500)]),
        )
        for i, s in enumerate(("AAA", "BBB"))
    ]
    experiment = setup(cfg)
    pack = vet_pack(
        as_of=VET_AT, common=common(experiment=experiment), candidates=picks, limits=VET
    )
    return [body(pack), body(pack, max_bytes=len(encode(pack).data) - 200)]


def review_bodies(cfg: AppConfig) -> list[dict[str, Any]]:
    decided = et_datetime(DAY, time(9, 36))
    items = [
        ReviewNews(news(1, "XYZ", minutes_before=30, at=decided)),
        ReviewNews(news(2, "XYZ", minutes_before=-30, at=decided)),
    ]
    pack = review_pack(
        **review_inputs(
            common=common(
                book_b=BookB(equity=Decimal("996.08"), day_mode="normal"),
                experiment=setup(cfg),
            ),
            signals=[
                ReviewSignal(
                    signal("XYZ", 1),
                    shadow(),
                    decisions={"A": "taken", "B": "vetoed"},
                    trades={"A": review_trade()},
                    verdict=veto(),
                    verdict_source="premarket",
                )
            ],
            targets=[target("XYZ", 0, items=items, decision_at=decided)],
            incidents=[IncidentView(et_datetime(DAY, time(10)), "P3", "llm", "x", "m")],
            stats={"b_vetoes": 1},
            lessons=[
                LessonView(
                    id="L-001",
                    status="proposed",
                    statement="s",
                    evidence_query={"filters": [{"field": "gap_pct", "op": "gte", "value": 10}]},
                    claimed_direction="subset_worse",
                    suggested_use="veto_hint",
                    n_subset=21,
                    n_complement=40,
                    effect_r=-0.2,
                    ci=(-0.4, -0.05),
                )
            ],
            history=History(
                summaries=[(date(2026, 11, 2), "a quiet day")],
                bad_process=[BadProcess(date(2026, 11, 2), "20261102-ABC-orb5", "ABC", "w", "n")],
                factor_tally=[FactorCount("false_breakout", 12, 3, 9, -0.4, 2)],
                open_issues=[
                    OpenIssue(1, "warn", "exec", "late", date(2026, 10, 30), date(2026, 11, 2), 3)
                ],
                open_notes=[OpenNote(2, "a note", date(2026, 10, 30), date(2026, 11, 2), 2)],
            ),
        )
    )
    return [body(pack)]


BODIES = {"premarket": premarket_bodies, "vet": vet_bodies, "review": review_bodies}
OUTPUTS = {"premarket": PremarketBrief, "vet": VetBatch, "review": DailyReview}


def walk(node: Any) -> Iterator[tuple[str | None, Any]]:
    """Every (key, value) in ``node``, at any depth; a list item has no key."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield None, value
            yield from walk(value)


def resolves(node: Any, path: str) -> bool:
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def schema_words() -> set[str]:
    """The answers' field names and enum values, every role's: a prompt may name them."""
    words: set[str] = set()
    for model in OUTPUTS.values():
        for key, value in walk(wire_schema(model, "full")):
            if key == "properties":
                words |= set(value)
            elif key in ("enum", "const"):
                words |= {str(v) for v in (value if isinstance(value, list) else [value])}
    return words


# ---------------------------------------------------------------- the prompts


@pytest.mark.parametrize("role", ROLES)
def test_a_prompt_holds_no_number(settings: Settings, role: str) -> None:
    """The setup's numbers are in the pack (`experiment`, `limits`), never in a prompt, so a
    changed setting can't leave a prompt stating the old one (P4.5). Only SEC form names
    hold digits."""
    text = prompt(settings, role)
    for form in sorted([*settings.edgar.flag_forms, "424B"], key=len, reverse=True):
        text = text.replace(form, "")
    assert re.findall(r".{0,30}\d.{0,30}", text) == []


@pytest.mark.parametrize("role", ROLES)
def test_a_prompt_asks_for_the_pack_id(settings: Settings, role: str) -> None:
    assert "- `pack_id`: copy the pack's `pack_id` exactly;" in prompt(settings, role)


@pytest.mark.parametrize("role", ROLES)
def test_a_prompt_names_only_what_its_pack_or_the_schemas_have(cfg: AppConfig, role: str) -> None:
    """Every name a prompt quotes is a key or a value of its role's pack, a dotted path into
    it, or a field or enum value of the answers: a renamed key fails here."""
    bodies = BODIES[role](cfg)
    names = schema_words()
    for b in bodies:
        for key, value in walk(b):
            names |= {key} if key is not None else set()
            names |= {value} if isinstance(value, str) else set()
    quoted = re.findall(r"`([^`\n]+)`", prompt(cfg.settings, role))
    assert quoted
    unknown = [
        q for q in quoted if not (any(resolves(b, q) for b in bodies) if "." in q else q in names)
    ]
    assert unknown == []


def test_the_packs_hold_what_the_prompts_point_to(cfg: AppConfig) -> None:
    """The fixtures above reach the keys the prompts explain: a trimmed summary, unknown
    filings, a flagged item, both sides of a decision."""
    (pm, pm_trimmed), (_, vet_trimmed), (rv,) = (
        premarket_bodies(cfg),
        vet_bodies(cfg),
        review_bodies(cfg),
    )
    keys = {role: {k for b in BODIES[role](cfg) for k, _ in walk(b)} for role in ROLES}
    assert {"summary_omitted", "filings_unavailable", "suspicious", "picked_as"} <= keys[
        "premarket"
    ]
    assert "summary_omitted" in keys["vet"]
    published = {v for k, v in walk(rv) if k == "published"}
    assert published == {"before_decision", "after_decision"}
    assert [c["filings_unavailable"] for c in pm["candidates"]] == [False, True]
    assert pm_trimmed["candidates"][0]["news"][0].get("summary_omitted") is True
    assert vet_trimmed["candidates"][1]["news"][0].get("summary_omitted") is True


def test_the_brief_offers_no_web_tools(settings: Settings) -> None:
    """E5 isn't built (D-064): the brief runs with no tools at all."""
    assert "web" not in prompt(settings, "premarket").lower()


def test_each_role_has_its_prompt(settings: Settings) -> None:
    assert sorted(spec.prompt_file for spec in role_specs(settings).values()) == sorted(
        p.name for p in PROMPTS.glob("*.md")
    )


# ---------------------------------------------------------------- the fingerprint


def with_llm(settings: Settings, **changes: Any) -> Settings:
    return settings.model_copy(update={"llm": settings.llm.model_copy(update=changes)})


def with_role(settings: Settings, role: str, **changes: Any) -> Settings:
    roles = settings.llm.roles
    spec = getattr(roles, role).model_copy(update=changes)
    return with_llm(settings, roles=roles.model_copy(update={role: spec}))


def test_the_fingerprint_is_the_setup_not_its_path(settings: Settings, tmp_path: Path) -> None:
    copy = tmp_path / "elsewhere" / "prompts"
    shutil.copytree(PROMPTS, copy)
    first = epoch_fingerprint(settings, PROMPTS)
    assert first == epoch_fingerprint(settings, PROMPTS)
    assert epoch_fingerprint(settings, copy).sha256 == first.sha256  # a replay's root
    assert set(first.parts["roles"]) == set(ROLES)
    assert first.parts["roles"]["vet"]["argv"][:2] == ["-p", "<instruction>"]


def test_a_checkouts_line_endings_change_nothing(settings: Settings, tmp_path: Path) -> None:
    """``core.autocrlf`` writes CRLF on checkout: the text the model reads is the same."""
    crlf = tmp_path / "crlf"
    crlf.mkdir()
    for path in PROMPTS.glob("*.md"):
        text = path.read_bytes().replace(b"\r\n", b"\n")
        (crlf / path.name).write_bytes(text.replace(b"\n", b"\r\n"))
        assert prompt_sha256(crlf / path.name) == prompt_sha256(path)
    assert epoch_fingerprint(settings, crlf).sha256 == epoch_fingerprint(settings, PROMPTS).sha256


def edited_prompts(tmp_path: Path) -> Path:
    copy = tmp_path / "prompts"
    shutil.copytree(PROMPTS, copy)
    brief = copy / "premarket_brief.md"
    brief.write_bytes(brief.read_bytes() + b"\nOne more line.\n")
    return copy


CHANGES = [
    # (name, change, the parts it changes)
    ("a prompt", lambda s, tmp: (s, edited_prompts(tmp)), ["roles.premarket.prompt_sha256"]),
    ("a model", lambda s, tmp: (with_role(s, "vet", model="opus"), PROMPTS),
     ["roles.vet.argv", "roles.vet.model"]),
    ("an effort", lambda s, tmp: (with_role(s, "review", effort="medium"), PROMPTS),
     ["roles.review.argv", "roles.review.effort"]),
    ("a turn limit", lambda s, tmp: (with_role(s, "premarket", max_turns=5), PROMPTS),
     ["roles.premarket.argv"]),
    ("the schema mode", lambda s, tmp: (with_llm(s, schema_mode="wire_safe"), PROMPTS),
     ["roles.premarket.schema_sha256", "roles.review.schema_sha256",
      "roles.vet.schema_sha256", "schema_mode"]),
    ("the tools flag", lambda s, tmp: (with_llm(s, tools_flag="deny_all"), PROMPTS),
     ["roles.premarket.argv", "roles.review.argv", "roles.vet.argv"]),
    ("safe mode", lambda s, tmp: (with_llm(s, safe_mode=False), PROMPTS),
     ["roles.premarket.argv", "roles.review.argv", "roles.vet.argv"]),
]  # fmt: skip


@pytest.mark.parametrize(("name", "change", "parts"), CHANGES, ids=[c[0] for c in CHANGES])
def test_the_fingerprint_follows_what_a_call_asks(
    settings: Settings, tmp_path: Path, name: str, change: Any, parts: list[str]
) -> None:
    before = epoch_fingerprint(settings, PROMPTS)
    after = epoch_fingerprint(*change(settings, tmp_path))
    assert after.sha256 != before.sha256
    assert list(epoch.changes(before.parts, after.parts)) == parts


def test_the_fingerprint_follows_the_runners_texts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = epoch_fingerprint(settings, PROMPTS)
    monkeypatch.setattr("trader.llm.roles.COPY_PACK_ID", "Copy the pack_id.")
    monkeypatch.setattr(client, "REPAIR_NOTE", "Try again:")
    monkeypatch.setattr(epoch, "PACK_VERSION", epoch.PACK_VERSION + 1)
    after = epoch_fingerprint(settings, PROMPTS)
    assert list(epoch.changes(before.parts, after.parts)) == [
        "pack_version",
        "repair_note",
        "roles.premarket.instruction",
        "roles.review.instruction",
        "roles.vet.instruction",
    ]


def test_how_a_call_runs_is_not_what_it_asks(settings: Settings) -> None:
    """Timeouts, the budget and the stop path don't change what the model is asked."""
    before = epoch_fingerprint(settings, PROMPTS).sha256
    for changed in (
        with_role(settings, "vet", timeout_s=30, min_attempt_s=10),
        with_llm(settings, max_calls_per_day=5, max_concurrent=2, kill_grace_s=2.0),
        with_llm(settings, max_pack_bytes=200_000, max_tested_cli_version="2.1.280"),
    ):
        assert epoch_fingerprint(changed, PROMPTS).sha256 == before


async def test_a_changed_setup_is_a_p3_at_boot(tmp_path: Path, settings: Settings) -> None:
    journal = await Journal.open(tmp_path / "trader.db", inline=True)
    try:
        at = et_datetime(DAY, time(8, 25))
        first = epoch_fingerprint(settings, PROMPTS)
        assert await note_fingerprint(journal, first, at=at) is None  # nothing stored yet
        assert await note_fingerprint(journal, first, at=at) == ()
        changed = epoch_fingerprint(with_role(settings, "vet", model="opus"), PROMPTS)
        assert await note_fingerprint(journal, changed, at=at) == (
            "roles.vet.argv",
            "roles.vet.model",
        )
        assert await note_fingerprint(journal, changed, at=at) == ()  # one alert per change
        rows = await journal.fetch_all("SELECT severity, component, code, message FROM incidents")
        assert [tuple(r)[:3] for r in rows] == [("P3", "llm", "llm_fingerprint_changed")]
        assert "(roles.vet.argv, roles.vet.model)" in rows[0]["message"]
        assert "new epoch" in rows[0]["message"]
        assert await journal.get_meta("llm_fingerprint") == changed.sha256
    finally:
        await journal.aclose()


def test_a_missing_prompt_still_fingerprints(settings: Settings, tmp_path: Path) -> None:
    """The runner refuses the call (`prompt_missing`); boot can still say what changed."""
    fp = epoch_fingerprint(settings, tmp_path / "no-prompts")
    assert isinstance(fp, Fingerprint)
    assert {r["prompt_sha256"] for r in fp.parts["roles"].values()} == {None}
