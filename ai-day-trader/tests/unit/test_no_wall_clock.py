"""CLAUDE.md invariant 7: only core/clock.py reads the wall clock. Backs up ruff's TID251 bans."""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "trader"
CLOCK_MODULE = SRC / "core" / "clock.py"
BANNED = {("datetime", "now"), ("datetime", "utcnow"), ("datetime", "today"), ("date", "today")}


def _offences(tree: ast.AST) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            owner = node.value
            owner_name = (
                owner.id
                if isinstance(owner, ast.Name)
                else owner.attr
                if isinstance(owner, ast.Attribute)
                else None
            )
            if (owner_name, node.attr) in BANNED or (owner_name, node.attr) == ("time", "time"):
                found.append((node.lineno, f"{owner_name}.{node.attr}"))
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            if any(alias.name == "time" for alias in node.names):
                found.append((node.lineno, "from time import time"))
    return found


def test_no_wall_clock_reads_outside_the_clock_module() -> None:
    offenders = [
        f"{path.relative_to(SRC)}:{line} {what}"
        for path in sorted(SRC.rglob("*.py"))
        if path != CLOCK_MODULE
        for line, what in _offences(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []


def test_the_scan_catches_offences() -> None:
    sample = (
        "import datetime\nfrom time import time\nx = datetime.datetime.now()\ny = date.today()\n"
    )
    assert [what for _, what in _offences(ast.parse(sample))] == [
        "from time import time",
        "datetime.now",
        "date.today",
    ]
