---
name: next-task
description: Continue the build plan. Finds the next unchecked task in docs/PLAN.md, implements it with tests per CLAUDE.md, verifies, and updates the plan status. Use when the user says "next", "continue", "keep going", or asks to work on the plan.
---

# Next task

1. **Locate.** Read the STATUS block and the current phase in `docs/PLAN.md`.
   - If the next item is a 🛑 HUMAN GATE, stop. Explain what the human must do and why, give the exact command, and wait.
   - If this is the first task of a phase, present a file-level plan for the whole phase and wait for approval.
2. **Prepare.**
   - Re-read the spec sections the task references (A§ = ARCHITECTURE, S§ = STRATEGY, R§ = RISK) and any relevant `docs/DECISIONS.md` entries.
   - Check external API details against the official docs linked in `CLAUDE.md` before writing code that calls them.
3. **Build.**
   - Write or extend tests first for anything touching money, time, risk, the ledger, selection, or sizing.
   - Implement in small, typed modules with injected dependencies (clock, broker, market data, LLM client).
4. **Verify.** Run `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src`. Fix failures before moving on.
5. **Record.**
   - Tick the task's box in `docs/PLAN.md`.
   - Update STATUS: `current_phase`, `last_completed_task`, `open_questions`.
   - If you deviated from a spec or found API behavior that differs from the docs, append an entry to `docs/DECISIONS.md`.
6. **Report** in five lines or fewer: what changed, test results, anything the human should know, and the next task.

**Never do these without an explicit instruction from the human in this session:**

- run `trader smoke …`, `trader run-day`, `trader schedule …`, or `claude` itself;
- edit `src/trader/risk/caps.py`, `config/risk*.yaml`, or `config/LIVE_APPROVED`;
- set `mode: live`.
