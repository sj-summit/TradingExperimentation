# ai-day-trader — build plan package for Claude Code

This package contains a plan and specifications that Claude Code can ingest. It builds an automated, long-only day-trading **experiment** with $1,000:

- **The engine** is deterministic code. It trades a published opening-range-breakout strategy ("Book A").
- **Claude** (through your Max subscription and the `claude` CLI) acts as a morning veto filter and an after-close analyst ("Book B").
- **The comparison:** both books run side by side in separate Alpaca paper accounts, so you learn whether the LLM actually helps.

> **Read this first.** This is an experiment, not an income plan. Most day traders lose money, and frontier models have lost money in live trading competitions. Everything runs on paper until the Phase 7 evaluation gate, which only you can pass. Nothing here is investment advice.

## What's in the package

| Path | Purpose |
|---|---|
| `CLAUDE.md` | Auto-loaded by Claude Code every session: non-negotiable rules, commands, and how to work |
| `docs/PLAN.md` | Phased build plan with checkboxes, acceptance criteria, and 🛑 human gates |
| `docs/ARCHITECTURE.md` | Modules, interfaces, data contracts, DB schema, and the exact `claude -p` invocation |
| `docs/STRATEGY.md` | Trading rules (exact numbers), Book A/B policies, memory and lessons, evaluation criteria |
| `docs/RISK.md` | Risk rules R-001…R-021, hard caps, kill switch, watchdog, live-mode gate |
| `docs/DECISIONS.md` | Why each major choice was made, with sources; plus open questions |
| `prompts/` | Draft system prompts for Claude's three roles |
| `config/` | Settings, risk, and env templates; macro-events calendar |
| `.claude/settings.json` | Dev-time guardrails for Claude Code (details below) |
| `.claude/skills/next-task/` | A `/next-task` command that advances the plan one task at a time |

## Quick start

1. **Prerequisites**
   - Python 3.12+, [uv](https://docs.astral.sh/uv/), and git.
   - Claude Code 2.1.268 or later, logged in with your Max plan.
   - A machine that stays awake 08:15–16:45 ET on weekdays: a desktop, mini PC, or small VPS. On Windows, native or WSL both work.
2. **Unpack and version it.**
   ```bash
   unzip ai-day-trader-plan.zip && cd ai-day-trader
   git init && git add -A && git commit -m "Plan package"
   ```
3. **Set up Alpaca** (free).
   1. Sign up at alpaca.markets. Prefer a regular brokerage account (identity check; no deposit needed to start). Alpaca says paper-only signups get IEX data only, and the plan relies on free 15-minute-delayed full-market history (DECISIONS D-006).
   2. In the dashboard, click the paper account number, choose **Open New Paper Account**, and set a **$1,000** balance.
   3. Do that **twice**: one account for Book A, one for Book B.
   4. Generate API keys for each account.
4. **Create the bot's Claude token.** Run `claude setup-token`. It prints a token that lasts about a year and uses your subscription.
5. **Configure.**
   ```bash
   cp config/env.example .env
   cp config/settings.example.yaml config/settings.yaml
   cp config/risk.example.yaml config/risk.yaml
   ```
   Fill in `.env`:
   - both Alpaca key pairs;
   - `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_TOKEN_CREATED`;
   - `SEC_EDGAR_USER_AGENT` (your name and email; the SEC requires it).

   Leave `ANTHROPIC_API_KEY` empty.
6. **Start building.** Run `claude` in the folder, then send:
   > Read CLAUDE.md and docs/PLAN.md. Start Phase 0 in plan mode.

   After that, `/next-task` advances one task at a time. Claude Code stops at each 🛑 gate and tells you exactly what to run.

## How the build goes

| Phase | Result | Your gates |
|---|---|---|
| 0 | Scaffold, config, `trader doctor` health check | Accounts and keys (H0.x) |
| 1 | Market data, calendar, universe, news, SEC filings | Verify the macro-event dates |
| 2 | Broker adapter, cash ledger, risk gate, kill switch | **G2:** run the paper smoke test |
| 3 | Strategy, Book A, day orchestrator, replay, backtest | **G3:** one supervised paper day |
| 4 | Claude integration and Book B | **G4a:** LLM smoke test; **G4b:** one supervised A+B day |
| 5 | Statistics, lessons pipeline, weekly reports | — |
| 6 | Scheduler, watchdog, runbook | **G6a:** install the scheduler; **G6b:** watchdog fire drill |
| 7 | 60+ trading-day evaluation, then the go/no-go memo | **G7:** go-live decision (yours alone) |
| 8 | Optional experiments (Book C, SIP data, web research) | — |

## Once it's running (after Phase 6)

**Every weekday the bot:**

1. boots at 08:25 ET;
2. scans pre-market and asks Claude for a brief at 09:05;
3. selects "stocks in play" at 09:35 and has Claude vet them;
4. trades both books with broker-side stops;
5. flattens at 15:55;
6. simulates every signal's outcome;
7. has Claude review the day at 16:15;
8. sends you a report.

**Your daily job:** read the 16:35 summary (about 2 minutes). **Your weekly job:** read the weekly report and the lessons (about 15 minutes).

## Costs

| Item | Cost |
|---|---|
| Alpaca paper accounts | $0 |
| Market data (free IEX feed) | $0 |
| Claude | About 3–6 calls per trading day from your Max plan's usage. These are shared with your normal Claude use, and limits reset every 5 hours. |
| Full-market data | $99/month. Optional and deliberately *not* used at first (experiment E4). |

## Guardrails while Claude Code builds it (`.claude/settings.json`)

- **Denied:** reading `.env` and the bot's isolated Claude config; editing `config/LIVE_APPROVED`.
- **Always asks you first:**
  - editing `src/trader/risk/caps.py`, `config/risk*.yaml`, or `config/settings.yaml`;
  - running anything that trades or calls Claude (`run-day`, `smoke`, `flatten`, `resume`, `schedule`, `claude`);
  - `git push`.
- **Allowed without asking:** tests, lint, type checks, `doctor`, `replay`, `report`.

These rules cover Claude Code's own tools. The runtime has its own controls: hard caps, risk gate, broker-side stops, kill switch, and watchdog (RISK.md).

## Things this project will never do

- **Automate Fidelity.** Its Terms of Use prohibit automated trading tools; see DECISIONS D-001.
- **Script claude.ai, or reuse your Claude login token in other tools.** See D-002.
- **Trade options, short, use margin, or trade outside regular hours.**
- **Let the LLM place orders.**

## Emergency

```bash
uv run trader halt --flatten --book all    # stop everything now
touch runtime/KILL                         # same effect via the kill file (works while the engine runs)
uv run trader flatten --book all           # works even if the engine isn't running
```

Alpaca's dashboard also lets you close positions manually.
