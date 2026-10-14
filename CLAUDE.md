# CLAUDE.md — ai-day-trader

An automated, long-only **day-trading experiment** on a $1,000 cash budget.

A deterministic Python engine trades a published opening-range-breakout strategy. Claude, invoked headless through `claude -p`, acts only as a **veto filter** in the morning and as a **post-close analyst** after the bell.

Two paper "books" run side by side so the LLM's value is measurable:

- **Book A** — rules only.
- **Book B** — rules plus Claude's vetoes.

This is paper trading only until the human completes the go-live gate in `docs/PLAN.md` Phase 7.

## Read first

- `docs/PLAN.md` — phased build plan, task checklist, acceptance criteria, and **HUMAN GATES**. It is the source of truth for what to do next.
- `docs/ARCHITECTURE.md` — modules, interfaces, data contracts, DB schema, and the exact `claude -p` invocation.
- `docs/STRATEGY.md` — trading rules with exact numbers, the Book A/B policies, and measurement and evaluation.
- `docs/RISK.md` — risk-gate rules R-001…R-021, hard caps, kill switch, watchdog, and the live-mode gate.
- `docs/DECISIONS.md` — why things are the way they are. Append new entries; never rewrite old ones.

## Non-negotiable invariants

1. **The LLM never places, modifies, or cancels orders.** Only `execution/service.py` talks to the broker, and only after `risk/gate.py` approves an `OrderIntent`. LLM output can only veto a signal or reduce size (STRATEGY.md §5).
2. **Paper by default.** Unless the human explicitly instructs it in the current session, never:
   - set `mode: live` or add live credentials;
   - create or edit `config/LIVE_APPROVED`;
   - loosen any value in `src/trader/risk/caps.py` or `config/risk.yaml` (raise a limit, lower a floor, shorten a blackout, or disable a check).
3. **No naked positions.** Every open position has a broker-side stop at all times. An unprotected position is flattened within `unprotected_position_max_s` (RISK.md R-020). The one exception: partly filled shares whose entry's cancel is still pending at the broker get up to `partial_fill_cancel_max_s` (D-053).
4. **Cash-account semantics, whatever the broker account type:**
   - long only, no leverage, whole shares only;
   - buy only with settled cash, as tracked by `risk/ledger.py`.
5. **No unofficial automation.**
   - Never automate Fidelity or any broker without an official API.
   - Never script claude.ai.
   - LLM access goes only through `llm/client.py`: the `claude` CLI with the subscription token, or an Anthropic API key. Never pass the OAuth token to any other tool.
6. **Secrets stay in `.env`** (gitignored). Never print, log, or commit secret values; `trader doctor` prints key *names* only.
7. **Time comes from `core/clock.py`.** All timestamps are timezone-aware America/New_York. No naive datetimes and no `datetime.now()` outside that module.
8. **Everything that can lead to a trade is journaled:** signal, each book's decision and reason code, the risk-gate result, orders, fills, and LLM call artifacts.
9. **Tests are offline.** They never touch the network, place orders, or call `claude`. Paper smoke tests and LLM smoke tests are separate CLI commands, run only at HUMAN GATES.

## How to work

- **Go phase by phase from `docs/PLAN.md`.** Start each phase in plan mode and present the plan. At a HUMAN GATE, stop and ask.
- **Take the first unchecked task** in the current phase.
  1. Write tests first for anything touching money, time, risk, the ledger, selection, or sizing.
  2. Implement and run the checks.
  3. Tick the box and update the status block at the top of `PLAN.md`.
- **Verify external APIs against official docs before coding them.** Don't guess field names or behavior.
  - Alpaca: https://docs.alpaca.markets/us/llms.txt (append `.md` to any docs page URL for its markdown).
  - Claude Code CLI: https://code.claude.com/docs/en/cli-reference and https://code.claude.com/docs/en/headless
  - SEC EDGAR: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
  - Record anything surprising in `docs/DECISIONS.md`.
- **Don't improvise around spec problems.** If a spec in `docs/` is ambiguous or contradicts how an API actually behaves, stop and propose a resolution.
- **Keep modules small with injected dependencies** (clock, broker, market data, LLM client) so the whole day can run under `SimClock` + `SimBroker`.

## Commands

| Purpose | Command |
|---|---|
| Install | `uv sync` |
| Tests (excludes `paper`, `llm`, `network` markers) | `uv run pytest` |
| Lint and format | `uv run ruff check . && uv run ruff format --check .` |
| Types | `uv run mypy src` |
| Health check | `uv run trader doctor` |
| Full paper day (HUMAN GATE until Phase 6 is done) | `uv run trader run-day --books A,B` |
| Replay a recorded day offline | `uv run trader replay --date YYYY-MM-DD` |
| Emergency stop | `uv run trader halt --flatten --book all` |

## Repo map (target; details in ARCHITECTURE.md §2)

```
src/trader/   core/ data/ strategy/ books/ risk/ execution/ llm/ memory/ orchestrator/ reports/
config/       settings.yaml, risk.yaml (copied from *.example.yaml), events.yaml, LIVE_APPROVED (human-only)
prompts/      system prompts for the three Claude roles (each call logs the prompt hash)
schemas/      JSON Schemas generated from llm/schemas.py (committed)
runtime/      gitignored: trader.db, logs/, recordings/, llm/, claude-config/, llm_sandbox/, heartbeat.json, KILL
```

## Style

- Python 3.12, type hints everywhere.
- Pydantic v2 models for all data crossing module boundaries.
- `Decimal` for order prices, quantities, and ledger amounts; floats are fine in analytics.
- structlog JSON logs; no global mutable state.
- Pure functions for strategy, sizing, and risk math.
