"""`trader` command-line interface."""

from pathlib import Path
from typing import Annotated

import typer

from trader import __version__

app = typer.Typer(
    name="trader",
    help="ai-day-trader: long-only ORB experiment. Paper by default.",
    no_args_is_help=True,
    add_completion=False,
    # Rich tracebacks print local variables by default, and locals can hold secrets.
    pretty_exceptions_show_locals=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    root: Annotated[
        Path,
        typer.Option(
            envvar="TRADER_ROOT",
            help="Repo root holding config/, runtime/ and .env. Defaults to the current directory.",
        ),
    ] = Path("."),
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_print_version, is_eager=True, help="Print the version and exit."
        ),
    ] = False,
) -> None:
    """ai-day-trader command line."""
    ctx.obj = root.resolve()


@app.command()
def doctor(
    ctx: typer.Context,
    quick: Annotated[
        bool, typer.Option("--quick", help="Skip checks that start subprocesses (uv, claude).")
    ] = False,
) -> None:
    """Check config, .env key names, both Alpaca paper accounts, clock skew, the claude CLI,
    runtime dirs, token age, and events.yaml coverage. Exits 1 on any FAIL."""
    from trader.doctor import default_deps, format_report, run_doctor  # alpaca-py loads slowly

    checks = run_doctor(ctx.obj, default_deps(), quick=quick)
    for line in format_report(checks):
        typer.echo(line)
    if any(check.status == "FAIL" for check in checks):
        raise typer.Exit(code=1)
