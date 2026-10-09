from typer.testing import CliRunner

from trader import __version__
from trader.cli import app


def test_help_exits_zero() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0, result.output
    assert "trader" in result.output.lower()


def test_version_flag() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_tracebacks_never_show_locals() -> None:
    # Locals can hold secrets (CLAUDE.md invariant 6).
    assert app.pretty_exceptions_show_locals is False
