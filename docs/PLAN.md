# PLAN.md — build plan for Claude Code

**How to use this file (Claude Code):**

- Work top to bottom. Start each phase in plan mode and present your file-level plan.
- Do tasks in order unless a task says it can run in parallel.
- After each task, tick its box, update the status block, and run `uv run pytest && uv run ruff check .`.
- At a **🛑 HUMAN GATE**, stop and ask the human; do not continue past it on your own.
- If reality contradicts a spec (an API behaves differently, a number doesn't work), stop, propose a fix, and record the outcome in `docs/DECISIONS.md`.

```
STATUS
current_phase: 2
last_completed_task: P2.7
open_questions: 🛑 G2 awaits the human (smoke paper in market hours; first chance Mon 2026-10-12); H0.1 + H0.3 done (doctor 2026-10-10: Alpaca, data, journal green); H0.2 open (`claude` not on PATH, CLAUDE_TOKEN_CREATED missing); P1.8 drafted, awaiting human date check (config/events.yaml); Phase 1 done-checks pending; Q2 partly open (D-027); after G2 record the OTO leg status after a fill (D-032, D-038)
eval_epoch: 0
```

Specs referenced: ARCHITECTURE.md (A§), STRATEGY.md (S§), RISK.md (R§).

---

## Phase 0 — Scaffold, configuration, health checks

### Human prerequisites (the human does these; Claude Code verifies them in 0.4)

- [x] **H0.1** Create an Alpaca account. In the dashboard, create **two paper accounts with a $1,000 starting balance each**: click the paper account number → "Open New Paper Account". Generate API keys for each.
  - **Data note:** Alpaca says *paper-only* accounts are entitled to IEX data only. The plan uses free 15-minute-delayed SIP *history* (ADV/ATR filters, shadow outcomes), which the free plan gives regular accounts. So prefer a regular brokerage account (identity check; no deposit needed to start) and create the paper accounts under it. P1.2 verifies access (DECISIONS Q7).
- [ ] **H0.2** Install Claude Code ≥ 2.1.268. Run `claude setup-token`, then copy the printed token into `.env` as `CLAUDE_CODE_OAUTH_TOKEN`, with `CLAUDE_TOKEN_CREATED=<today>`.
- [x] **H0.3** Copy `config/env.example` → `.env`, `config/settings.example.yaml` → `config/settings.yaml`, and `config/risk.example.yaml` → `config/risk.yaml`. Fill in `.env`, including `SEC_EDGAR_USER_AGENT` with your name and email.
- [ ] **H0.4** Pick the machine that will run the bot: always on, awake 08:15–16:45 ET on weekdays, with a stable network. A desktop, mini PC, or small VPS all work.
- [ ] **H0.5** (Optional) Pick a notification channel (ntfy topic or Discord webhook).

### Tasks

- [x] **P0.1** Create `pyproject.toml` (uv, Python 3.12) with the A§13 deps, the src layout from A§2, ruff, mypy, and pytest config (markers `paper`, `llm`, `network`, excluded by default; sockets disabled in tests). Add a `trader` CLI entry point with `--help`.
- [x] **P0.2** Write `config.py`, which loads `settings.yaml`, `risk.yaml`, and `.env` into pydantic models.
  - Reject unknown keys.
  - Enforce `risk/caps.py` and fail fast on any violation (R§1).
  - Add a secret-redaction helper; secrets never appear in `repr` or logs.
- [x] **P0.3** Write the core modules:
  - `core/clock.py` (`RealClock`, `SimClock`, ET helpers; ban naive datetimes with a lint rule or test);
  - `core/logging.py` (structlog JSON + console);
  - `core/ids.py`.
- [x] **P0.4** Write `trader doctor`, which reports:
  - Python and uv versions; config validity;
  - required `.env` keys present (names only);
  - Alpaca A and B: authenticated, `is_paper`, equity ≈ $1,000, no positions;
  - Alpaca clock and skew under 2 s;
  - the `claude` binary is present and its version ≥ min;
  - `claude auth status` → `authMethod == oauth_token`, run under the isolated env (A§7.1);
  - runtime dirs writable; free disk space;
  - token age; `events.yaml` coverage.
- [x] **P0.5** Write the SQLite journal: migrations runner + `0001_init.sql` (A§6) + repository skeleton with a single-writer queue (WAL).
- [x] **P0.6** Create `.gitignore` coverage for `runtime/` and `.env`, plus a test asserting no secret values reach the logs.

**Done when:**

- `uv run pytest` is green;
- `uv run ruff check .` and `uv run mypy src` are clean;
- `uv run trader doctor` is all green against both paper accounts.

---

## Phase 1 — Market data, calendar, universe, news

- [x] **P1.1** Write `core/calendar.py`: Alpaca calendar → `trading_days` cache, sessions, early closes, `add_trading_days`. Use fixture tests: DST weeks (March and November), the day after Thanksgiving, Christmas Eve, and July 3.
- [x] **P1.2** Write `data/ratelimit.py` and `data/alpaca_rest.py`: bars (multi-symbol, batching, pagination, feed param), snapshots, assets, corporate actions, and `movers` (return `None` if the plan lacks it). Write respx-mocked tests.
- [x] **P1.3** Write `data/indicators.py`: Wilder ATR14, ADV14, OR features (`or_range_atr`, `or_return_pct`), `rel_vol`. Check against hand-computed fixtures.
- [x] **P1.4** Write `data/universe.py` (S§3.1): `trader universe build` and `trader universe or-baseline`.
  - Daily features come from SIP daily bars.
  - OR baselines come from the live feed.
  - Log counts: total assets, eligible, excluded by reason.
- [x] **P1.5** Write `data/stream.py` (`MarketDataHub`): one WebSocket; subscription limit enforced; consumer fan-out; reconnect, resubscribe and backfill; staleness metrics. Add the dev command `trader stream`. Write fake-WS tests.
- [x] **P1.6** Write `data/news.py` (`NewsHub`: stream + REST backfill) and `data/edgar.py` (user agent, rate limit, ticker→CIK cache, recent-filing flags). Write mocked tests.
- [x] **P1.7** Write `data/recorder.py`: daily recordings per A§4.2.
- [ ] **P1.8** Fill `config/events.yaml` for the next 3 months. Claude Code drafts it from the official Federal Reserve FOMC calendar and the BLS release schedule (CPI, Employment Situation), with source URLs. **The human verifies the dates.**

**Done when:**

- unit and component tests are green;
- `trader universe build` produces plausible counts. Expect roughly 800–2,000 eligible symbols; investigate if far outside that range;
- the OR baseline exists for at least 14 sessions;
- during market hours, `trader stream --symbols SPY,QQQ --minutes 2` prints minute bars (a manual check by the human is fine);
- news and EDGAR lookups work for a known ticker.

---

## Phase 2 — Broker, ledger, risk gate, execution (no strategy yet)

- [x] **P2.1** Write `execution/broker.py` (protocol), `execution/orders.py`, and `execution/alpaca_broker.py`.
  - `orders.py` covers OTO construction (limit entry plus a `stop_loss` leg), sub-penny rounding, and `client_order_id`.
  - Verify the request classes against Alpaca docs (`/us/docs/orders-at-alpaca.md`). Whole shares only.
- [x] **P2.2** Write `execution/sim_broker.py`: deterministic fills (A§4.6), partial-fill option, rejections, and a trade-updates stream.
- [x] **P2.3** Write `risk/caps.py` (🛑 the human approves the file contents; the `.claude/settings.json` ask rule will prompt) and `risk/ledger.py` (R§4), with unit tests and hypothesis tests. (`caps.py` was created and approved in P0.2 and is unchanged, D-014.)
- [x] **P2.4** Write `risk/gate.py` implementing R-001…R-021, with pass and fail tests for every rule. Target 100% branch coverage of `gate.py`.
- [x] **P2.5** Write `risk/breakers.py` and `risk/killswitch.py`, plus the `halt`, `resume` (typed confirmation), and `flatten` CLI commands. `flatten` must work without the engine running.
- [x] **P2.6** Write `execution/service.py` and `execution/reconcile.py`:
  - submit, track, entry timeout;
  - the R-020 unprotected-position loop;
  - flatten with verification;
  - a reconciliation diff.
  Run SimBroker scenario tests for: a happy path; a stop-out; an entry that never fills; a partial fill; a rejected stop leg (it must protect or flatten within 5 s); a submit timeout (it must look up by `client_order_id` and never double-submit).
- [x] **P2.7** Write `trader smoke paper`.
  - Place one **non-marketable** 1-share OTO buy (limit far below market) in paper account A, confirm it was accepted with the stop leg attached, cancel it, and confirm it's cancelled.
  - With `--marketable`, buy 1 share of a liquid sub-$50 stock, confirm the stop leg is live, then `flatten` it.
  - Print the journal rows.
- [ ] 🛑 **HUMAN GATE G2:** The human runs `uv run trader smoke paper` (then `--marketable`) during market hours and confirms the results. Do not run it yourself.

**Done when:**

- all tests are green, and the gate is 100% branch-covered;
- the human has run the smoke tests successfully;
- the kill switch flattens SimBroker positions within 10 s in tests.

---

## Phase 3 — Strategy engine, Book A, orchestrator, shadow outcomes, replay, backtest

- [ ] **P3.1** Write `strategy/orb_sip.py` (S§3.2): parallel batched OR scan, filters, top-20, direction, persistence of all top-20 rows. Test the scan with fixture bars: ties, missing bars, doji, liquidity guards.
- [ ] **P3.2** Write `strategy/triggers.py` (S§3.3): one-shot triggers; entry window; chase guard; `missed_at_arm` handling.
- [ ] **P3.3** Write `strategy/sizing.py` (S§3.5) as a pure function, with property tests: never over the settled cash, risk cap, or notional cap.
- [ ] **P3.4** Write `books/base.py` and `books/baseline.py` (Book A), journaling decisions with the reason-code enum (A§4.4).
  - Also, moved here from Phase 2 (D-037): add a `broker_rejected` reason code, for an entry the broker refused after the gate approved it; and write the `trades` rows (P&L, R, estimated fees, exit reason) from the journaled fills.
- [ ] **P3.5** Write `orchestrator/day.py`: the state machine and timeline (A§3), early-close handling, late start, crash-restart recovery, and heartbeat. Add `trader run-day --books A [--sim]`.
  - Also, from Phase 2 (D-036, D-037):
    - seed each book's ledger from a flat account at the first boot;
    - wire the EOD drawdown and losing-streak breakers, and the EOD cash check (`ledger.eod_check`, then an incident and a halt);
    - run the kill switch's watch loop;
    - call `ExecutionService.flatten` at the flatten time, and keep calling it until the book is flat or the close.
- [ ] **P3.6** Write `memory/shadow.py` (S§7.2, including the `mfe_r`, `mae_r`, and `minutes_held` path stats) and `memory/regime.py`. Run them at 16:12.
- [ ] **P3.7** Write `reports/daily.py` (MD and HTML) and `notify.py` (providers: none, ntfy, discord).
- [ ] **P3.8** Write `trader replay --date`: recorded day → `ReplayMarketData` + `SimClock` + `SimBroker`. Add a golden test on a synthetic fixture day.
- [ ] **P3.9** Write `trader backtest`: the rules-only ORB on historical SIP data over a configurable range. Report:
  - long-only leg stats and long+short shadow stats;
  - mean R by rel_vol bucket (H4 sanity).
  Run it on at least 6 months of history. **Never include the LLM in a backtest.**
- [ ] 🛑 **HUMAN GATE G3:** The human approves running Book A in paper for one supervised session: `uv run trader run-day --books A`. Afterwards, Claude Code reviews the journal and report with the human, and adds a golden replay test from that recorded day.

**Done when:**

- one supervised paper session for Book A completed: correct journal, flat by close − 2 min, report generated;
- the replay of that day reproduces the same signals and decisions;
- the backtest report exists, and H4's direction is checked and noted in DECISIONS.md.

---

## Phase 4 — LLM layer and Book B

- [ ] **P4.1** Write `llm/schemas.py` (A§5), the schema generator to `schemas/*.json` (refs inlined, each file under 8 KB), and tests that the generated schemas validate the fixtures.
- [ ] **P4.2** Write `llm/sanitize.py`: HTML strip, truncation, injection heuristics → `suspicious`, optional entity anonymization. Write tests with adversarial headlines.
- [ ] **P4.3** Write `llm/context.py`: the premarket, vet, and review packs (A§8), plus size-cap tests.
- [ ] **P4.4** Write `llm/claude_cli.py` per A§7: isolated env and cwd, exact flags, timeout handling, envelope parsing, one repair retry, artifact persistence, and `llm_calls` rows. Add `llm/budget.py`. Test everything with `scripts/fake_claude.py` scenarios.
- [ ] **P4.5** Turn the `prompts/` drafts into v1. Prompts stay **static**: all dynamic data, including date and playbook, goes in the context pack. Log each prompt's SHA-256 per call; a prompt change starts a new epoch.
- [ ] **P4.6** Write `trader smoke llm`: one real call per role with fixture context packs (no market impact), printing validated outputs, durations, and envelope fields.
- [ ] 🛑 **HUMAN GATE G4a:** The human runs `uv run trader smoke llm` (it uses the Claude subscription). Then Claude Code records in DECISIONS.md the observed CLI behavior: does `--tools ""` work with `--json-schema`, what the envelope fields are, and the exit codes. If structured output fails, apply the A§7.2 fallback.
- [ ] **P4.7** Write `books/llm_filtered.py` (Book B): the VerdictStore (pre-market + vet), day-mode multiplier, the pending policy (wait up to 60 s, re-check the chase guard), and the failure and degradation rules (S§5.4).
- [ ] **P4.8** Integrate with the orchestrator: pre-market scan at 09:00, brief at 09:05 (deadline 09:28), vet at 09:35:05, review at 16:15; the `--books A,B` flag; `--no-llm` to force Book B off.
- [ ] **P4.9** Write FakeLLMClient end-to-end tests of a full simulated day with A and B, including: a veto, `neutral`, a timeout, invalid output, `no_trade` mode, and an entry delayed by a pending verdict.
- [ ] **P4.10** Write `memory/reviews.py` (S§6.5–6.6, A§4.8):
  - review-target selection (traded, B-vetoed, and notable untraded signals; at most 20) and their evidence for the pack;
  - the review pack's `history` section, including the factor tally;
  - persistence into `daily_reviews`, `signal_reviews`, `process_issues`, and `review_notes`, with linking of recurring issues and notes;
  - the `trader followups list|set` CLI.

  Write tests for: target selection; a linked issue bumping its count rather than duplicating; an unknown or closed `existing_issue_id` becoming a new issue; reviews for non-target signals being dropped; and a missing review recorded as `status = missing`.
- [ ] 🛑 **HUMAN GATE G4b:** The human approves one supervised paper session with both books: `uv run trader run-day --books A,B`.

**Done when:**

- the supervised A+B session completed;
- every LLM call is logged with its artifacts;
- B's decisions are journaled with reasons;
- that day's review is in its tables, with a signal review for every target;
- a replay of that day (with recorded LLM outputs replayed, not re-queried) reproduces the decisions.

---

## Phase 5 — Memory, statistics, lessons, weekly reporting

- [ ] **P5.1** Write `memory/stats.py` (S§7.3): per-book and per-epoch metrics, a seeded bootstrap, veto value V with CI, and SPY buy-and-hold comparison.
- [ ] **P5.2** Write `memory/lessons.py`: store proposals from the review, the DSL evaluator with a whitelist (unknown fields and ops rejected), and the promotion and demotion rules (S§6.3). Add the `trader lessons evaluate --promote` CLI. Write tests, including DSL injection attempts.
- [ ] **P5.3** Write `memory/playbook.py`: render `runtime/playbook.md` (at most 15 lessons with evidence), plus the `matching_lessons` computation for context packs.
- [ ] **P5.4** Write `reports/weekly.py`: equity curves A vs B vs SPY, trade stats, V with CI, lessons status, the factor tally, the week's `bad_process` reviews, open process issues and notes, incidents, epoch, and the limitations footer (A§4.10).
- [ ] **P5.5** Write `reports/evaluation.py`: the pre-registered evaluation memo (S§8), with every criterion shown as pass, fail, or insufficient data.

**Done when:**

- the reports render from a fixture journal;
- the DSL and promotion logic are fully tested;
- the playbook goes into the prompts within the size caps.

---

## Phase 6 — Automation and operations

- [ ] **P6.1** Write `scripts/install_scheduler_{cron,launchd,windows}.py` and `trader schedule install|uninstall|show`.
  - **Entries:**
    - `run-day` at 08:25 ET on weekdays (the process exits on holidays);
    - `watchdog` every 5 min from 09:25 to 16:05;
    - the weekly job on Saturday at 10:00;
    - optional nightly universe build at 20:00.
  - **Convert ET to the machine's local timezone,** handling DST with two entries or a self-adjusting wrapper.
  - **Show the exact entries and ask before installing.**
- [ ] **P6.2** Write `orchestrator/watchdog.py` per R§6, with tests using SimBroker and a stale heartbeat.
- [ ] **P6.3** Retention and backups:
  - logs and recordings kept for `storage.retention_days`;
  - a daily SQLite online backup, 30 copies kept;
  - a disk-space alert.
- [ ] **P6.4** Write `docs/RUNBOOK.md`. It covers:
  - the daily 2-minute check;
  - reading the reports;
  - halt, resume, and flatten;
  - common failures: Alpaca outage, WebSocket flapping, Claude usage limit or token expiry, a reconciliation mismatch, clock skew, a machine reboot;
  - token renewal (yearly) and key rotation;
  - what to do after a P1.
- [ ] 🛑 **HUMAN GATE G6a:** The human reviews and runs `uv run trader schedule install`.
- [ ] 🛑 **HUMAN GATE G6b:** A watchdog fire drill in paper, during market hours, with the human present:
  1. Open a position with `smoke paper --marketable`.
  2. Kill the engine process.
  3. Confirm the watchdog flattens the position and alerts within 5 min.

**Done when:**

- 5 consecutive unattended paper sessions (A+B) run with no manual intervention;
- all reports were produced;
- the fire drill passed;
- the human has read the runbook.

---

## Phase 7 — Evaluation period and go-live gate (human-led)

- [ ] **P7.1** Freeze parameters, prompts, and models. Set `eval_epoch: 1` in the DB and record the frozen config hashes in DECISIONS.md.
- [ ] **P7.2** Run for at least 60 trading days. Hold a weekly review with the human (weekly report plus 15 minutes). Only bug fixes are allowed; anything else starts a new epoch (S§9).
- [ ] **P7.3** Generate the evaluation memo: `trader report evaluation` (S§8).
- [ ] 🛑 **HUMAN GATE G7 — go/no-go.** Only the human decides. If the answer is go:
  1. The human opens a separate live Alpaca **cash** account and funds it with $1,000.
  2. The human adds the live keys to `.env`.
  3. The human creates `config/LIVE_APPROVED` (R§7).
  4. Run the first 10 live sessions at `live.size_multiplier: 0.5`.
  5. The live kill criteria in R§7 apply.

  Claude Code may prepare the checklist but must not create `LIVE_APPROVED`, add live keys, or set `mode: live`.

---

## Phase 8 — Optional extensions (only after Phase 7 data exists)

- [ ] **E1** Anonymized vet (S§5.3, `anonymize_entities: true`) as a new epoch, an A/B test of prompt variants.
- [ ] **E2** Book C: Claude chooses from a fixed menu of parameterized setups (S§11), in a third paper account.
- [ ] **E3** An intraday news check for open positions that can only exit or tighten, never add.
- [ ] **E4** Evaluate SIP data ($99/mo) against IEX: compare selection overlap and trigger latency before paying.
- [ ] **E5** Pre-market brief with web research (`allow_web: true`), as a new epoch.

**Not planned, ever:** Fidelity automation (D-001), scripting claude.ai (D-002), margin or shorting with this budget, options.
