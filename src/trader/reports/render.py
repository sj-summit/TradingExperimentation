"""What every report and the playbook render the same way (A§4.10; the Phase 5 plan's decision
13; DECISIONS D-077): STRATEGY.md §10's limitations, and Markdown that keeps the model's text
inert.

- **Text the model wrote** (``ModelText``, ``model_md``) goes on one line in a code span whose
  fence is longer than any run of backticks in it. So it opens no link (a bare URL included),
  image, HTML or emphasis; in a table its pipes are escaped too, which GFM requires even inside
  a code span. HTML pages escape every cell with ``html.escape``.
- **Other cells** (``md_cell``) lose their newlines and escape their pipes.

No template engine and no new dependency (D-046).
"""

import re

LIMITATIONS = (
    "Data: IEX-only real-time data makes triggers lag, and makes relative volume noisier.",
    "Paper fills: they ignore market impact, queue position, and latency slippage. Dividends "
    "aren't simulated either. Paper does charge regulatory fees, overnight; the ledger books "
    "what it charged.",
    "Sample size: small samples mean wide confidence intervals. Two to three months of signals "
    "cannot prove a durable edge.",
    "Backtest vs live: the paper's backtest (2016-2023, with leverage, long and short) is not a "
    "forecast. Day-trading edges decay, and most retail day traders lose money.",
    "LLM drift: model updates behind an alias can change behavior mid-epoch.",
)


class ModelText(str):
    """Text the model wrote: Markdown escapes all of it (HTML escapes every cell anyway)."""


def md_cell(cell: str) -> str:
    return cell.replace("|", "\\|").replace("\n", " ")


def model_md(text: str, *, in_table: bool = False) -> str:
    """The model's text for Markdown: on one line, in a code span longer than any run of
    backticks it holds. In a table cell its pipes are escaped too, which GFM requires even
    inside a code span."""
    flat = " ".join(text.split())
    if not flat:
        return ""
    fence = "`" * (max((len(run) for run in re.findall(r"`+", flat)), default=0) + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    if in_table:
        flat = flat.replace("|", "\\|")
    return f"{fence}{pad}{flat}{pad}{fence}"


def md_row(cells: list[str]) -> str:
    """A GFM table row; a ``ModelText`` cell is the model's."""
    return (
        "| "
        + " | ".join(
            model_md(c, in_table=True) if isinstance(c, ModelText) else md_cell(c) for c in cells
        )
        + " |"
    )
