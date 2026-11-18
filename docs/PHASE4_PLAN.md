# Phase 4 plan: the LLM layer and Book B, plus the fixes due before G4b

> **About this copy.** The human approved this plan on 2026-10-14. It was kept outside the repository, at `C:\Users\Admin\.claude\plans\squishy-popping-honey.md`, and Claude Code's routine cleanup of old files removed it. This copy was restored on 2026-11-18 from the session that approved it; the text is unchanged. Decisions taken since are in `docs/DECISIONS.md` (D-055 onward), which governs where the two differ.

## Context
- **Phase 3 is done.** G3 passed on run 2 (2026-10-14, D-054); HEAD is 0226783 with 715 tests.
- **What Phase 4 builds:** Claude's two roles, and Book B itself.
  - Claude as Book B's morning veto filter: a 09:05 brief and a 09:35:05 vet.
  - Claude as the post-close analyst: the 16:15 review.
  - It ends with two human gates: G4a (`trader smoke llm`) and G4b (the first supervised A+B session).
- **Due before G4b** (not before coding):
  - D-054's fixes;
  - the shadow fix you chose (SIP trade tape);
  - fees booked from Alpaca's actual activities (your choice).
- **Gaps exploration found, which this plan settles:**
  - **Isolation.** The sandbox `runtime/llm_sandbox/` sits inside the repo. Claude Code loads CLAUDE.md from the working directory and every parent, so the trading model would read our developer instructions.
  - **Timeouts on Windows.** Windows has no SIGINT/SIGTERM for a child process, so A§7.2's timeout path doesn't exist there.
  - **Book B's pending wait** would block its own queue.
  - **Undefined so far:** the review's target and linking details, and how a day with LLM calls is replayed.
- **Facts checked against the official docs on 2026-10-14:**
  - All 13 A§7.2 flags exist as spelled.
  - `--safe-mode`: CLAUDE.md, hooks, MCP, skills and auto memory don't load; authentication and permissions work normally.
  - The output is in `structured_output`. A failure has subtype `error_max_structured_output_retries`; "success" with no `structured_output` also counts as a failure.
  - Schemas are draft-07.
  - `DISABLE_AUTOUPDATER=1` stops the CLI updating itself.
  - Alpaca: `GET /v2/account/activities/FEE`, with subtypes REG, TAF and CAT. Fees are charged at EOD and rounded up. The docs don't say whether paper lists these activities.

## Your decisions (2026-10-14)
- **Start:** Phase 4 starts now. The carry-over fixes land before G4b.
- **Shadow outcomes:** resolve the ambiguous trigger minutes from the SIP trade tape.
- **Fees:** book Alpaca's actual FEE activities.

## Decisions this plan makes (recorded in DECISIONS as each part lands)
1. **One runner with swappable transports.**
   - `llm/client.py` holds `Role`, `LLMClient`, `LLMResult`, and `LlmCalls`. `LlmCalls` owns call ids, the budget, `llm_calls` rows, artifacts, validation, clamping and the single repair.
   - Transports:
     - `ClaudeCliTransport` (real);
     - `ScriptedTransport` (tests and `--sim`);
     - `RecordingTransport` and `ReplayTransport`.
   - Fake and replayed days write the same rows as live ones.
   - Invariant 5 holds. Doctor's `claude --version` and `auth status` probes are not model calls.
   - An autouse test guard refuses the real binary (invariant 9).
2. **Isolation.**
   - The argv gains `--safe-mode`, and the subprocess env gains `DISABLE_AUTOUPDATER=1`.
   - The sandbox is checked empty before every call, failing closed.
   - The instruction goes right after `-p`.
   - Never `--bare`: it can't use the OAuth token, and the docs say it may become the default.
   - `trader smoke llm` runs an isolation canary.
3. **Provider `anthropic_api`** means the same CLI authenticated with `ANTHROPIC_API_KEY`. No direct API client and no new dependency; this amends D-002 and A§4.7.
4. **Attempts.**
   - Each CLI run, first or repair, is one `llm_calls` row.
   - The daily cap of 12 counts attempts and survives restarts.
   - **Toward `llm_degraded` (3):** every failed attempt counts, except one refused for budget or time, or abandoned in a crash.
   - **Repair** only output problems (bad JSON, schema-invalid, no structured output, wrong `pack_id`), and only when `min_attempt_s` remains before the deadline.
   - Smoke calls never count.
5. **Deadlines.**
   - `run(..., deadline)`: each attempt's timeout is min(the role's timeout, deadline − now), on the injected clock.
   - **Brief:** 09:28.
   - **Vet:** the last entry time.
   - **Review:** a cutoff of report − 1 min, shifted on early closes.
6. **Stopping a call.**
   - **Windows:** start the child in a new process group. Send CTRL_BREAK, wait 5 s, then `taskkill /T /F`.
   - **POSIX:** SIGINT, then SIGTERM, then SIGKILL.
   - A `.cmd` shim is refused, and doctor FAILs on one.
7. **Schemas.**
   - Draft-07, refs inlined, each under 8 KB.
   - Each output echoes the pack's `pack_id`, which proves the pack arrived on stdin.
   - Constraints are kept (`schema_mode: full`). If G4a shows the CLI rejects them, switch to a `wire_safe` fallback.
   - Pydantic is the final check. Lengths and counts are clamped; enums never are.
8. **Book B parks pending triggers instead of blocking its queue.**
   - `on_trigger` may return `Defer(until)`. The worker resumes parked triggers, in rank order, when verdicts change or at their deadline. Decisions are still made one at a time.
   - **Precedence:**
     1. a non-LLM halt → `book_halted`;
     2. `llm_degraded`;
     3. `day_mode_no_trade`;
     4. veto → `vetoed` (checked before the chase guard);
     5. a failed or uncovered call → `llm_unavailable`;
     6. pending: if already past the chase cap, `chased` at once; otherwise park, and at the deadline `vet_timeout`;
     7. the chase guard → `chased`, or `chased_after_wait` after a wait;
     8. sizing with the day-mode multiplier, then the gate.
   - **The deadline** is max(armed_at, the covering call's start) + 60 s.
9. **`--no-llm`** (or `llm.enabled: false`) turns Book B off for the day, and the report says so. The 09:00 scan, news, EDGAR and LLM work run only when Book B runs. Book-A-only days, and the G3 golden replay, are unchanged.
10. **Replaying an LLM day.**
    - Each attempt and its pack go into `recordings/<date>/llm.jsonl.gz`, flushed after every record.
    - Replay answers each call at max(now, the recorded end) and serves the recorded premarket and vet packs, so it needs no news or EDGAR.
    - `comparable()` adds day modes, verdicts and calls.
    - `boot.json` gains `capabilities` and a source hash. A replay of an older recording skips calls it never made.
11. **The review is in Phase 4,** per PLAN. D-048's "Phase 5" note is superseded.
    - **Targets:**
      - every signal either book filled;
      - every triggered long Book B vetoed;
      - up to 5 other triggered longs, by |shadow R|;
      - no shorts; at most 20.
    - **Decision time** is the `as_of` of the pack behind B's verdict, else the live trigger time, else the arming time.
    - **SPY's move** over each target's window comes from SIP bars. They are added to the shadow request only on review days.
    - **The review and its parts** are written in one transaction, so a rerun never calls twice.
    - **Lesson proposals** are stored with status `proposed` (amends P5.2).
12. **Pre-market candidates.**
    - Only eligible symbols with a print today (≥ 04:00 ET) qualify. The gap is measured from the universe's SIP prev close, which supersedes D-024's daily-bar source.
    - Take the top 30 gainers plus up to 10 symbols with news, capped at 40.
    - EDGAR is prefetched at 09:00.

## Order
| Step | What | Needs | Golden files |
|---|---|---|---|
| P4.0a | D-054 fixes #2 #3 #4 #7; migration 0005; actual fees | — | `max_intraday_dd` and `entry_delay_ms` change in both goldens; new columns |
| P4.0b | D-054 fixes #5 (skip labels) and #6 (polling budget) | P4.0a | CAP and STT reasons; a documented G3 diff |
| P4.0c | Shadow outcomes from the SIP tape; backtest rerun; D-042 and H4 restated | P4.0a; your tape fetch | shadow rows |
| P4.1 to P4.6 | Schemas, sanitizer, packs, runner, prompts, `smoke llm` | P4.0a (migration) | none |
| 🛑 G4a | You run `doctor` and `smoke llm` | H0.2 | — |
| P4.7 to P4.10 | Book B, orchestrator and replay, end-to-end days, review | G4a's findings | new `standard_day_ab.json`; the A-only goldens stay byte-identical |
| 🛑 G4b | The supervised A+B session | everything above, plus your pre-G4b checks | then it becomes a golden replay |

## Tasks (tests first wherever money, time, risk, the ledger, selection or sizing is touched)

### P4.0a: D-054 fixes (part 1), migration 0005, actual fees
**Migration `memory/migrations/0005_phase4.sql`.** It holds every Phase 4 column at once, since applied migrations never change (D-040):
- `daily_stats.cash_residual`;
- `llm_calls`: `logical_id`, `attempt`, `final`, `ok`, `failure`, `subtype`, `session_id`, `timed_out`, `effort`, `purpose`, `deadline`, `pack_sha256`, `pack_bytes`, `candidates_json`, `cli_version`, `schema_mode`, `clamped`, `eval_epoch`, plus an index;
- `verdicts`: `signal_id`, `eval_epoch`, plus an index;
- `day_modes`: `source`, `multiplier`, `eval_epoch`;
- `daily_reviews`: `failure`, `targets_json`;
- a new `review_mentions` table;
- status indexes on `process_issues` and `review_notes`;
- `lessons.source_date`;
- `shadow_outcomes.trigger_resolution` and `spy_move_pct`.

**Actual fees (your choice).**
1. **Probe first.** A new read-only `trader fees --book A --days 5` lists FEE activities. You run it once.
   - It shows whether paper lists the overnight charges seen on 10-13 and 10-14, and how they are dated.
   - If paper lists none, I fall back to your second option (count earlier days' estimates, and journal the residual), record why, and continue.
2. **The broker port.**
   - `BrokerAdapter.fee_activities(since)`, returning id, date, subtype, `net_amount` and status.
   - `AlpacaBroker`: `GET /v2/account/activities/FEE`, with `after`, `direction=asc`, `page_size` 100 and a page token. Field names are re-checked against Alpaca's docs before coding.
   - `SimBroker` can post fees at EOD, as paper does. Recording and replay handle the call like any other.
3. **The ledger** (`risk/ledger.py`).
   - Per-fill estimates stay, so intraday settled cash stays conservative.
   - **At boot and at EOD:** for each earlier trade date with fees posted, book the actual rows (kind `fee`, ref `fee:<activity id>`). Then book one `adjust` row reversing that date's estimates (ref `fee-est-reversal:<date>`). This is append-only.
4. **EOD cash check (amends D-034; same rule for paper and live).** Broker cash is compared with ledger cash counted as:
   - actual fees;
   - estimates of earlier dates not yet reconciled;
   - never today's estimates, because they post after EOD.

   The residual is stored in `daily_stats.cash_residual`. A P3 `fee_drift` fires when it exceeds half the tolerance.
5. **Old recordings:** fee calls run only when `boot.json`'s capabilities include `fees_v1`, so G3's replay stays divergence-free.

**The other D-054 fixes:**
- **#2 Stuck exit.** `_exit_covers` times its 20 s from our own send (`Records` `submitted_at`, which is never overwritten), not from Alpaca's `submitted_at`. `SimBroker._accept` rewrites `submitted_at`, as paper does.
- **#3 Stale marks.** `exposure()` marks a stale symbol at the broker's `current_price`, and falls back to cost only after that.
- **#4 Entry delay.** `trades.entry_delay_ms` is computed on the local clock, from the fill's ledger `created_at` minus `triggered_at`.
  - Also an hourly skew reading, journaled in `day_runs`, with a P2 `clock_skew` above 2 s.
  - Replay doesn't count an extra `clock` call as a divergence.
- **#7 Source hash.** `boot.json` gains `source_sha256` (over src, prompts and schemas, without git) and `capabilities`. Replay notes "code changed since the recording".
- **Small items:**
  - log lines for entry cancels, exits and flatten passes;
  - the P3 `partial_fill_protected` message names the bare seconds and what covered the shares;
  - the backup is taken after DONE is journaled.

**Tests first:**
- the paper check excludes today's estimates and includes earlier dates';
- actual fees reverse estimates exactly once;
- the probe output;
- the residual is stored, and drift raises a P3;
- a 10 s acceptance delay plus a stuck exit gives a P1 by send + 20 s;
- `SimBroker` acceptance rewrites `submitted_at`;
- stale marks use the broker's price;
- entry delay is on the local clock;
- hourly skew readings;
- a surplus clock reading is not a divergence;
- the boot source hash.

**Goldens:** both are rewritten (UPDATE_GOLDEN, with the diff reviewed). The G3 test must keep zero divergences.

### P4.0b: D-054 fixes (part 2)
**#5 Skip labels.**
- When qty < 1, R-006 or R-007 names the reason if it also binds, via `gate.capacity_violations()` and `reasons.reason_for_no_size()`. These are labels only; they never add a skip. The backtest uses the same rule.
- G3's `journal.json` stays the live truth. `g3_day.CODE_CHANGES` lists STT's expected diff line, citing the DECISIONS entry, and the test asserts the diff equals it.

**#6 Polling budget.**
- `RateLimiter` gains `reserve` and `acquire(urgent=True)`. Submits, cancels and sells (POST and DELETE) jump the queue, and polls never take the bucket below 4 tokens.
- `service.py`:
  - refreshes positions and orders every 1 s only while something is bare or a stop is pending, otherwise every 2 s;
  - skips `get_order` when the stream reported that order within the poll interval. It never skips while the entry is waiting for acceptance or has a cancel requested, because `pending_cancel` never comes on the stream.
- Tests: urgent calls jump the queue; the reserve is kept; normal callers stay FIFO; two open entries stay under 150 calls per minute. Every R-020 timing test passes unchanged.

### P4.0c: shadow outcomes from the SIP tape (your choice)
**The rule.**
- **Entry:** max(trigger, the trigger minute's open) + slippage, from bars, for every signal. This fixes gap-throughs at once.
- **A minute is ambiguous** (for a long) when its open < trigger, its low ≤ stop and its close > stop; shorts mirror this. Only those minutes (648 in the backtest window) need the tape.
- **On the tape:**
  - drop corrected or canceled prints (`u`) and prints whose conditions don't count for price;
  - the entry print is the first at or past the trigger;
  - the trade is stopped if any later print is at or beyond the stop;
  - the exit is min(stop, that print) − $0.01.
- **No usable tape:** the pessimistic fallback applies, with `trigger_resolution = fallback` and a P3.

**Code.**
- `AlpacaRest.trades(symbols, start, end, feed="sip")`: paginated, limit ≤ 10,000. Re-checked against Alpaca's docs; on the free plan the end must be 15 or more minutes old.
- A `SipTrade` model; the shadow job's request is recorded and replayed.
- `shadow.resolve_trigger_minute()`.
- A `bt_tape` cache table for the backtest.
- The backtest uses the same resolver for the shadow stats and Book A's leg.

**Live cost:** about 5–10 requests at 16:12.

**Then:**
- You run the tape fetch for the backtest window (`uv run trader backtest … --tape`, about 650–700 requests, roughly 5 min, outside 09:00–16:30).
- I re-run the report from the cache and write the DECISIONS entry restating D-042's numbers and H4.

**Tests:**
- entered first, then stopped;
- stop first, so not stopped;
- a gap-through enters at the open, and a touch stops it;
- corrected and odd-lot prints are ignored;
- a minute with no entry print falls back;
- shorts mirrored;
- one tape request per ambiguous signal;
- a tape failure falls back with a P3.

### P4.1: schemas
- `llm/schemas.py` holds:
  - the enums: CatalystType 18, RiskFlag 13, DslField 17, DslOp 8, OutcomeFactor 19;
  - the A§5 models, plus `pack_id`;
  - `wire_schema(model, mode)` (inlined, with `additionalProperties: false`);
  - `clamp_to_schema()`;
  - `python -m trader.llm.schemas --write|--check`.
- `schemas/*.json` and their `.wire.json` twins are committed.
- Fixtures go in `tests/fixtures/llm/`; `jsonschema` becomes a dev dependency (Draft-7, tests only).
- **Tests:**
  - the enums match the STRATEGY lists;
  - the committed schemas match the code;
  - each schema is inlined and under 8 KB;
  - the fixtures validate under both Draft-7 and pydantic;
  - `wire_safe` has no constraint keywords;
  - clamping cuts lengths but never fixes enums.

### P4.2: sanitizer
- `llm/sanitize.py`:
  - `clean_text()`: HTML text only (script and style dropped), NFKC, control, zero-width and bidi characters removed, an exact cut;
  - `is_suspicious()`: injection heuristics;
  - `untrusted()`;
  - `anonymize()`, off by default.
- **Tests:** about 15 adversarial headlines are flagged, about 20 benign ones aren't, HTML and bidi are stripped, and truncation is exact and idempotent.

### P4.3: context packs and pre-market candidates
- **`llm/context.py`** holds pure builders for the premarket, vet and review packs.
  - Common sections:
    - `pack_id`, `as_of`, `session`;
    - `limits` and `experiment`: every config number the prompts used to hard-code;
    - `events_today`, `market` (SPY/QQQ/IWM gaps, regime);
    - `book_b.recent`;
    - `playbook: []` until P5.3.
  - Third-party text appears only under `untrusted`.
  - `encode()` keeps a pack under 300 KB by trimming news, then summaries, then the lowest-ranked candidates.
- **`llm/candidates.py`:** `select_premarket()` (decision 12), a pure function.
- **Tests:**
  - per-role caps (40/8, 25/5, 20/5);
  - the worst case fits under 300 KB;
  - trim order;
  - untrusted placement;
  - before/after-decision stamps;
  - deterministic bytes;
  - candidate ranking and ties.

### P4.4: runner, transports, budget, fake CLI
**Files:**
- `llm/client.py` (decision 1);
- `llm/budget.py`: counts today's trading attempts from `llm_calls`; a semaphore enforces `max_concurrent`;
- `llm/roles.py`: one spec per role;
- `llm/claude_cli.py`: `build_argv`, `resolve_claude` (refuses `.cmd`), and `ClaudeCliTransport` (decision 6);
- `llm/fake.py`: `ScriptedTransport`;
- `scripts/fake_claude.py`.

**`fake_claude.py`** is run as `[python, scripts/fake_claude.py, --scenario X]` followed by the real argv.
- It first validates the argv: every flag present, `--safe-mode` set, no `--bare`, the instruction right after `-p`, and a schema under 8 KB.
- **Scenarios:**
  - `valid`, `invalid_json`, `schema_invalid`, `repair_ok`;
  - `no_structured_output`, `structured_retries`, `is_error`, `max_turns`;
  - `nonzero_exit`, `nonzero_with_envelope`;
  - `slow`, `slow_stubborn`;
  - `wrong_pack_id`, `huge`, `leaky`, `stdin_missing`.

**Every attempt:**
- An `llm_calls` row is inserted at the start and updated at the end.
- `runtime/llm/<date>/<call_id>.input.json` holds the exact stdin bytes.
- `.output.json` holds the envelope or raw output, a redacted stderr tail, the exit code and the argv (with the schema replaced by its hash).

**Config additions,** each with a default so the frozen G3 config still validates:
- `llm.schema_mode: full`, `llm.tools_flag: empty`, `llm.safe_mode: true`;
- `llm.kill_grace_s: 5`, `llm.max_pack_bytes: 300000`, `llm.max_tested_cli_version`;
- `min_attempt_s` per role: 60 for premarket, 15 for vet, 120 for review.

**Tests** (real `fake_claude` subprocesses on Windows):
- the argv;
- the isolated env;
- a dirty sandbox fails closed;
- every scenario is classified correctly;
- a timeout interrupts and then kills;
- a child that ignores the interrupt is killed;
- a cancelled call kills its child;
- stdout cap;
- stderr redaction;
- deterministic ids;
- one repair, with the errors appended;
- no repair for process failures, or when time is short;
- the 13th attempt is refused across a restart;
- smoke calls are excluded;
- a wrong `pack_id` fails.

### P4.5: prompts v1
- Strip every config-derived number from `prompts/*.md` and point to the pack's `limits` and `experiment` instead.
- Ask for `pack_id` to be copied. Drop the premarket web-tools sentence.
- `epoch_fingerprint()` is a hash over the prompts, the instructions, the schemas, the models and efforts, and the flags. A change at boot is a P3 `llm_fingerprint_changed`.
- **Tests:** no config numbers in the prompts; `pack_id` is requested; the fingerprint changes with a prompt or a model.

### P4.6: `trader smoke llm`
- **Package:** `llm/smoke.py`, plus `llm/smoke_data.json` (package data with fictional issuers, including one injection headline).
- **Command:** `trader smoke llm [--role …] [--schema-mode …] [--no-canary] [--canary-control] [--kill-test]`.
- **What it does:**
  - one real call per role (the vet at full size: 25 candidates with 5 news each);
  - prints ok or the failure, the duration, the exit code, the subtype, `num_turns`, `session_id`, cost, `model_resolved`, the envelope's keys, and whether the injection was flagged;
  - **canary:** a CLAUDE.md in a parent folder and one in the sandbox, each with a token, plus a bogus `.mcp.json` and a hook. It passes only if no token, hook output or MCP error appears;
  - `--kill-test` checks the Windows stop path;
  - reports whether `runtime/claude-config/.claude.json` exists (D-021);
  - ends with `SMOKE LLM PASSED` or `FAILED`.
- **Tests:** offline, with fakes.

### 🛑 G4a (you)
- **Before:** H0.2 must be done (Claude Code ≥ 2.1.268 on PATH as a real `.exe`, `claude setup-token`, the token and `CLAUDE_TOKEN_CREATED` in `.env`).
- **Run** `uv run trader doctor`, then `uv run trader smoke llm`.
- **Then I record (answers Q4 and D-021):**
  - whether `structured_output` works with `--tools ""` and `--safe-mode`;
  - the envelope's keys and subtypes;
  - `schema_mode`;
  - the exit codes;
  - the canary result;
  - durations;
  - `max_tested_cli_version`.

### P4.7: Book B
- `books/entry.py`: the shared pricing, sizing and entry path, extracted from `baseline.py` with no change in behavior.
- `books/base.py`:
  - `BookDecision.llm_call_id`, which `record_decision` passes;
  - `Defer(until)`;
  - `on_trigger(ev, sig, *, waited=False)`.
- `books/verdicts.py`: `VerdictStore`.
  - It holds the day mode, verdicts, coverage, failures and a version.
  - It persists before changing memory, and on a restart rebuilds from `verdicts`, `day_modes` and `llm_calls`. An abandoned call is re-vetted.
- `books/llm_filtered.py`: `LlmFilteredBook` (decision 8).
- **Tests:**
  - allow and neutral are `taken` and carry the call id;
  - a veto beats the chase guard;
  - a premarket verdict stands unless `revet_premarket` is set;
  - a pending trigger parks, then ends `entered_after_wait`, `chased_after_wait`, `vetoed` or `vet_timeout`;
  - pending and already chased gives `chased`;
  - `llm_unavailable`;
  - `llm_degraded` versus `book_halted`;
  - `no_trade` beats allow;
  - a reduced day halves the quantity;
  - restart rebuilding;
  - the deadline after a restart.

### P4.8: orchestrator, live wiring, recording, replay, reports
**Files:**
- `plan.py`: the pre-market scan, brief, deadline, review and cutoff times, with the early-close shift.
- `orchestrator/llm_jobs.py`:
  - the premarket scan, brief, vet and review jobs, each with its deadline;
  - persistence;
  - a P3 per failed attempt, and a P2 plus a session halt for `llm_degraded`.
- `orchestrator/evidence.py`: news and EDGAR, with budgets.
- `ports.py`: the new protocols.
- `day.py`:
  - `DayDeps.llm`;
  - B built from config;
  - `_work` handles `Defer`;
  - REVIEW runs after the shadow job;
  - `boot.json` records the LLM setup.
- `live.py`:
  - `--books A,B` and `--no-llm`;
  - NewsHub (with a sleep on the clock and news recording), EDGAR, and `RecordingTransport` around the CLI;
  - a scripted fake under `--sim`.
- `llm/replay.py`; the LLM path in `orchestrator/replay.py`.
- `reports/daily.py`: the header (LLM calls, budget, failures, B's day mode), a B-verdict column, an LLM-calls table, and Claude's review section, with model text escaped.

**Tests:**
- plan times and the early-close shift;
- `--books A,B` and `--no-llm`;
- every job's deadline and fallback;
- the report.

### P4.9: end-to-end A+B days
- **Fixtures:** `days.py` gains pre-market prints, news and filings. `DayRun` gains `books=("A","B")`, `llm_script=` and an 08:55 start. `golden.py` gains `TABLES_AB`.
- **Tests:** `test_orchestrator_b.py` covers:
  - a neutral day matches A;
  - a premarket veto frees cash for CAP;
  - a pending verdict delays WIN (`entered_after_wait`);
  - `chased_after_wait`;
  - vet timeout, then `llm_unavailable`;
  - an invalid answer after the repair, and degradation after 3 failures (A unaffected);
  - a `no_trade` day; a reduced day;
  - a missing brief sends everything to the vet;
  - a late start after the brief deadline;
  - a restart mid-vet re-vets;
  - a restart after the vet makes no new calls;
  - the budget cap fails closed;
  - an A-only day makes no LLM or pre-market calls, so the standard golden stays byte-identical.
- **New goldens:** `standard_day_ab.json`, checked under two hash seeds, plus LLM record-then-replay.

### P4.10: reviews and followups
- `memory/reviews.py`:
  - `select_targets` and `gather_evidence` (news: at most 3 before the decision and 2 after);
  - `history` (with the factor tally, by shadow-R sign);
  - `filter_review`;
  - `persist_review`: one transaction. Linking bumps counts, keeps the original text, puts today's text in `review_mentions`, and dedupes ids within one review.
  - `followups` and `set_followup`.
- **CLI:**
  - `trader followups list [--all]`;
  - `trader followups set issue|note ID --status S [--response T]` (only you close items).
- **Tests:**
  - PLAN's five;
  - shorts are never targets;
  - a SIP-only B veto is a target;
  - news stamps against the decision time;
  - the factor tally;
  - linking twice bumps once;
  - invalid after the repair gives status `invalid`;
  - a rerun after success makes no call;
  - proposals are stored as `proposed`;
  - followup statuses.

### 🛑 G4b (you)
- **Before:**
  - `trader news --symbols AAPL --hours 24`, `trader filings --symbol AAPL --days 30`, and `trader stream --news --minutes 2` during market hours (Phase 1's leftovers);
  - confirm `ALPACA_DATA_*` keys are set, so data calls don't share Book B's trading limit;
  - first boot of Book B's ledger;
  - the morning dry run.
- **Run:** `uv run trader run-day --books A,B`, with HANDOFF's runbook, which gains the new milestones and pass criteria.
- **Afterwards:**
  - the replay must match;
  - the day becomes a golden fixture;
  - I record G4b.

## Records and spec edits
**DECISIONS:** D-055 onward, one entry per part above, plus G4a and G4b.

**Specs:**
- **ARCHITECTURE:** §2, §3, §4.4, §4.7, §5, §6, §7.1–7.4, §8, §12; A§3's stale "movers" line.
- **STRATEGY:** §5.2–5.4, §6.5, §7.2.
- **RISK:** the meaning of `llm_failures_degrade_after`; §8's `llm.auth`; LLM incidents.
- **PLAN:** add P4.0a–c; amend P5.2; STATUS.
- **CLAUDE.md** (approved with this plan):
  - invariant 5 names `llm/client.py`'s `LLMClient` and the doctor probes;
  - Commands gains `smoke llm`, `followups` and `--no-llm`;
  - the repo map.

## Verification
- **After each task:** `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src`.
- **The gate** stays at 100% branch coverage.
- **Goldens** are rewritten only in P4.0a, P4.0b, P4.0c and P4.9/P4.10, each with a reviewed diff.
  - The G3 replay keeps zero divergences, and differs from `journal.json` only by `CODE_CHANGES`.
  - The A-only standard golden stays byte-identical from P4.7 on.
- **Offline:** nothing runs the real `claude` (the autouse guard). The `fake_claude` subprocess tests run on Windows.
- **Determinism:** the A+B golden is the same under two hash seeds, and an LLM day passes record-then-replay with no divergence.
- **An adversarial review** of each part's diff (P4.0, P4.4, P4.7–P4.8) before moving on.

## Human steps, in order
1. After P4.0a's code: `uv run trader fees --book A --days 5` (read-only); paste the output.
2. After P4.0c's code: the tape fetch for the backtest window, outside market hours.
3. H0.2, then G4a.
4. The pre-G4b checks, then G4b.

## Not in this plan
- Phase 5's stats and the veto value V, the lesson DSL and its promotion, the playbook, and the weekly report.
- A direct Anthropic API client.
- Web tools for the brief (`allow_web` stays false).

## Promises
- No `run-day`, `smoke` or `claude` without your instruction.
- No edits to `caps.py`, `config/risk*.yaml` or LIVE_APPROVED. No risk value changes; new settings carry defaults.
- No commits unless you ask.
