# HANDOFF: G3 passed (run 2, 2026-10-14); next, two plans for the human (updated 2026-10-14, evening)

**Latest:**
- **G3 passed on run 2 (D-054).** The run reached DONE with no P1, Book A was flat from 10:14, the report and journal were written, and `trader replay` matches.
  - **The day:** 4 entries. VRNS and KBH didn't fill; BWA and FAST stopped out. Net −$4.21.
  - **D-053 at the open:**
    - VRNS waited 12.1 s for acceptance, and its cancel went 10.5 s after it, never during `pending_new`.
    - FAST's partial fill was bare 1.04 s, which gave the expected P3.
  - **Not yet exercised live:** the 20 s partial-fill allowance, re-sends, `cancel rejected`, `entry_stuck`, and a flatten with a position open.
  - **The human's last check:** `uv run trader doctor --quick` should show Book A with `0 positions, 0 orders`. The recording already shows it flat at 16:05.
- **Commits:**
  - `cc6eb60` is the D-052/D-053 tree, exactly what ran.
  - Uncommitted: D-054, the golden replay test and fixture, and these PLAN and HANDOFF updates. Commit only if asked.
- **Golden replay:**
  - The fixture is `tests/golden/recordings/2026-10-14/` (3.3 MB): the stream is trimmed to every trade plus at most one quote per symbol every 10 s.
  - Rebuilder: `tests/fixtures/g3_day.py`. Pinned journal: `tests/golden/g3_2026-10-14.json`.
  - The test is `test_the_g3_day_replays_as_it_was_recorded` (22 s). 715 tests pass.
- **Next** (Claude, in plan mode; the human approves each plan):
  1. **D-054's minor fixes:**
     - the paper fee check, before about session 15;
     - stuck-exit timing from our own send;
     - stale marks from the broker's price;
     - `entry_delay_ms` on one clock, plus a periodic skew reading;
     - the skip-reason order;
     - the polling budget;
     - a dirty-tree flag in `boot.json`;
     - small log and report items.
  2. **The shadow entry on gap-through trigger bars, together with the trigger-minute rule.** The human chose to decide them together.
     - Compare bar-open entry with resolving from the SIP trade tape, with backtest numbers for each; the human picks.
     - Then restate D-042's shadow numbers and H4.
  - **Timing:** Phase 3's done-when list is met, since D-042 already notes H4's direction. Both plans must land before G4b, Phase 4's first live session, because Book B's veto value and the review are judged on shadow outcomes. Neither blocks Phase 4's coding.
  - **Still pending:** H0.2 (the Claude token, needed for G4a) and Q2 (14 symbols didn't reach the limit).

To resume: read this block, the STATUS block in `docs/PLAN.md`, `CLAUDE.md`, and D-052 to D-054. The G3 runbook below served both runs and is the template for G4b. Everything below the line further down is the older G2 handoff, kept for its environment notes and the Phase 2 code map.

## Where Phase 3 stands

- **Done:** P3.1–P3.9 and G3. The records are D-040 to D-054.
- **Commits:** the repo is git. The user committed Phase 3 as b2f9f09 and D-051 as 24c7814; D-052 and D-053 are cc6eb60. Commit only when the user asks.
- **G3 prerequisites (2026-10-12 evening):**
  - **P1.8 is done:** the human verified every date in `events.yaml`.
  - **Universe:** built for 2026-10-13, with 1,587 eligible.
  - **Dry run:** ran at 20:42, so it showed the finished 10-12 session. It must be run again on the morning of the session.
  - **Skipped:** the `--sim` rehearsal and the `trader stream` check. Run-day's own 09:35 subscription answers Q2.
- **D-051 (late 2026-10-12):** a read-only pre-flight review of the run-day path found no blockers. It led to eight fixes on failure paths, each with a test that fails on the old code.
  - The suite is now 682 tests. ruff, format and mypy are clean, and the gate is still at 100% coverage. A second review, of the fixes themselves, found no blocker; its minor points are fixed too (D-051 item 9).
  - The G3 runbook below includes the review's operator guidance.
- **P3.9 (backtest) is done.** It ran 2026-04-01 to 2026-10-09; the results are at the end of D-042.
  - **Book A:** −20.2%; longs average −0.31R.
  - **But:** the shadow R for longs lies between −0.31 and +0.14 depending on how a trigger minute that touched both trigger and stop is read. The approved rule is the pessimistic end.
  - **Waiting on the human:** whether to resolve it from the SIP trade tape.
  - **Other fixes found by the run:** placeholder symbols are now filtered out (D-050). The cache (`runtime/backtest/cache.db`) makes reruns free.
- **P3.8b is done (D-049).**
  - `trader replay --date D` replays a recorded day into `runtime/replay/<date>/`. It exits 1 on any divergence or difference from the journal.
  - The golden day is `tests/golden/standard_day.json` (`UPDATE_GOLDEN=1` rewrites it).
  - `ReplayBroker` matches reads by the nearest recorded call, unused answers first (D-049 says why).
- **Then 🛑 G3 on 2026-10-13**, with the runbook below; the human has it. Not pending: H0.2 isn't needed for Book A.

## 🛑 G3: the supervised paper session (run 1 on 2026-10-13; this runbook is for run 2)

**The evening before** (the human, in PowerShell at the repo root):
- `uv run trader resume --book A` (type `RESUME A`). This clears run 1's `p1_incident` halt.
- `uv run trader universe build` for the session day.

**Before 08:25** on the session day:
1. **Machine:**
   - Set sleep to Never while plugged in, and keep the PC on AC power.
   - Keep Windows Update paused.
   - Run the session in a standalone PowerShell window. A VS Code restart kills its integrated terminal, and run-day with it.
2. **Pre-flight:** run `uv run trader run-day --books A --dry-run` at about 08:10. Expect:
   - the session's date;
   - `clock skew … (ok)`;
   - the universe's eligible count;
   - `Book A: cash about 1000.3, 0 positions, 0 open orders; ledger E 1000.29; halts none`. Broker cash may sit a few cents below the ledger: paper charges fees overnight (D-052);
   - no `KILL FILE PRESENT` line.
3. **Start:** `uv run trader run-day --books A`. Within about 30 s, look for `stream connected … /v2/iex`.

**During the day:**
- **Never stop run-day before its DONE line.** No Ctrl+C, no closing the window, no sleep. Stopping it skips the 15:55 flatten, and the broker-side stops are DAY orders that expire at 16:00 (D-051).
- **Run no other `trader` command meanwhile.** They share the API rate limit and the single market-data stream, and they write to the same log. The exception is the emergency stop.
- **Milestones:**
  - 09:29 `subscribed`, then 09:35 `scan done` and the armed signals.
  - At 15:30 (or right after the scan if nothing was armed), `day state FLATTEN` means waiting for 15:55.
  - Then 16:05 EOD, 16:12 shadow outcomes, 16:30 the report, and `run-day <date>: DONE`.
- **Expected, not a problem:**
  - one P2 `subscription_limited` near 09:35 (that's Q2's answer);
  - a `broker_rejected` decision (the price reversed before the order);
  - a P3 `scan_slow`;
  - from D-053 at the open:
    - `entry accepted waited_s=…` (paper can take 10–13 s);
    - warnings `entry cancel sent again` or `cancel rejected` (the latter also for a stop that fills during the 15:55 flatten);
    - a P3 `partial_fill_protected`.
- **Stop and paste:** copy the lines and paste them to Claude, and leave run-day running. Do this for:
  - any P1 or traceback;
  - `not_flat`, `scan_deadline_missed` or `state_failed`;
  - a repeated `subscription_refused`;
  - `stream_unavailable` not followed by `stream connected`;
  - `data_stale`;
  - a `clock_skew` or `assets_unavailable` that the 09:29 re-check doesn't clear.
- **To stop trading** (emergency): from a second PowerShell at the repo root, run `New-Item -ItemType File runtime\KILL`.
  - Run-day halts Book A, flattens it, and still finishes the day.
  - Afterwards, `Remove-Item runtime\KILL`.
  - Use `uv run trader flatten --book A` only if run-day isn't running.
- **If run-day exits early:**
  - Copy its last lines.
  - Run `uv run trader run-day --books A` again at once, the same day. It resumes: it adopts today's position and still flattens at 15:55.
  - A restart costs the replay criterion.

**Pass:**
- the last line is `run-day <date>: DONE`;
- no P1 incidents (`runtime\reports\<date>.md` lists them);
- Book A is flat: after DONE, `uv run trader doctor --quick` shows `0 positions, 0 orders` for Book A;
- the report exists;
- the journal has signals, decisions, shadow outcomes and `daily_stats` (orders, fills and trades if anything triggered);
- if the day ran with no restart, kill or halt, `uv run trader replay --date <date>` ends with `the replay matches the recorded day`. It prints nothing until it's done (about 4 minutes for a full day).

**Afterwards (Claude):**
- review the journal, the log and the report with the human;
- add the day as a golden replay test;
- record G3 in DECISIONS, including:
  - the Q2 answer, if the day subscribed enough symbols to tell (run 1's 11 didn't);
  - how D-053 behaved at the open (acceptance waits, re-sent cancels, partial fills);
- clear any halt with `uv run trader resume --book A` before the next session.

## Phase 3 code map

**`strategy/`**
- `orb_sip.py`: the scan, plus `select`/`to_signal`, `save`/`load_signals`. ScanFlag is `scan_late`, `late_start` or `clock_skew`.
- `pricing.py`, `sizing.py`.
- `triggers.py`: `TriggerEngine` with `arm`, `rearm`, `run`, `on_trade`, `expire`; per-book queues; `next_batch`.

**`books/`**
- `base.py`: `PreparedInputs` (with `halts`), `EntryInputs`, decisions (append-only, `final_decisions`), `finalize_decisions`.
- `baseline.py`: Book A. A halted book declines first.
- `reasons.py`.

**`memory/`**
- `trades.py`: trades rebuilt at EOD.
- `shadow.py`: `compute_shadow`.
- `regime.py`: `ensure_regime` at boot for t−1.

**`orchestrator/`** (D-048)
- `plan.py`: `DayPlan`.
- `day.py`: `DayOrchestrator(DayDeps).run()` and `.dry_run()`.
- `eod.py`: steps marked in `day_runs`; the breakers.
- `tasks.py`: `every`, `Heartbeat`, `StaleWatch`, `Subscriptions` (405 → trades only → drop by rank).
- `inputs.py`: `LiveInputs`.
- `lock.py`: an OS file lock.
- `live.py`: the `run-day` wiring for paper, `--sim` and `--dry-run`.
- `simdrive.py`: `drive(clock, job, watched)`.
- `ports.py`: the `Hub` and `DayMarket` protocols.

**`data/`**
- `replay.py`: `RecordingMarketData`, `ReplayMarketData`, `ReplayHub`, `load_stream`. Also `orchestrator/replay.py` (`replay_day`, `seed`, `comparable`) and `execution/replay.py` (`ReplayBroker`).
- `stream.py`: `HubState`, taps, the `channels=` option, `TRADING_CHANNELS`.
- `ws.py`: bad-input hardening, ack timeout (408), `on_error`.
- `recorder.py`: `REST_FILE`, `BOOT_FILE`, `broker_file(book)`, `append`, `record_event(event, rx)`.
- `conditions.py`.

**`execution/`**
- `recording.py`: `RecordingBroker`.
- `service.py`: `flatten(purpose=)` with a lock; `accepting`; `held`; a reconcile mismatch reported once.
- `records.py`: `orders.purpose`.
- `sim_broker.py`: `hold()`.

**Elsewhere**
- `notify.py`: providers and `pump_alerts`.
- `reports/daily.py`.
- `backtest/`.
- **CLI:** `trader scan`, `run-day`, `backtest`, `report daily`, `notify test`.

**Tests**
- `tests/fixtures/days.py`: the synthetic day and `SyntheticMarket`.
- `tests/fixtures/dayrun.py`: `DayRun`, a whole day on a SimClock with an always-on exchange feed for the brokers, crash and restart (`run(until=)`, `wait_until`, `new_hub`), and hooks (`at`).
- `tests/unit/test_orchestrator.py`: 17 scenarios.
- `tests/fixtures/golden.py` and `tests/unit/test_golden.py`: the golden day, the two-process determinism test, record-then-replay, and `trader replay`.

---

# HANDOFF: 2026-10-10 (Sat) → next session (Mon 2026-10-12)

> **Update 2026-10-12:** G2 passed. Both smoke runs printed `SMOKE PASSED`, and the results are in D-039. The stop leg is `new` after a fill, and the stream delivered the fill. The stream-counter proposal was dropped as unneeded. **Phase 2 is done; next is the Phase 3 plan in plan mode** (`/next-task`). Below, the G2 sections are history; the rest still applies.

To resume, read this file, the STATUS block in `docs/PLAN.md`, and `CLAUDE.md`. Open the other docs only for the section a task names.

## State

- **Phases 0–2:** the code is done.
  - 507 tests pass offline.
  - ruff, format and mypy are clean.
  - `risk/gate.py` has 100% branch coverage.
  - This folder is not a git repo, and nothing is committed.
- **Current step: 🛑 G2.** The human runs the paper smoke test during market hours.
  - A try on Saturday 10-10 stopped at "not a trading day", as designed. That run read the config, used the keys and called Alpaca's REST API successfully.
- **Human setup:**
  - **H0.1 and H0.3 are done.** On 2026-10-10 doctor passed:
    - both paper accounts: ACTIVE, $1,000, flat;
    - SIP history and the movers screener (this answers Q7 and Q1);
    - the clock;
    - the journal (schema v3).
  - **H0.2 is still open.** Doctor's three FAILs all come from it, and none of them affects G2:
    - `claude` was not on PATH. Its folder is now in the user PATH, but it takes effect only after a VS Code restart.
    - `.env` is missing `CLAUDE_TOKEN_CREATED`. Use `2026-10-08`, the earliest date the user could have run `claude setup-token`.
- **Still open from Phase 1:**
  - **P1.8:** the human checks `config/events.yaml` against each entry's `source:` and sets `verified_by_human: true`. The first event is CPI on 10-14 at 08:30, before the open, so it blocks nothing.
  - **The done-checks** listed under "Monday" below.

## Waiting on the user: ask on Monday, before the run

**The smoke output can't show whether the trade-updates stream delivered any events.**
- The service also polls its open orders over REST every second, so G2 passes even if the stream sends nothing.
- `trade stream connected` appears on the console and in the log, but it only proves the handshake: auth `authorized`, then `listening` on `trade_updates`.
- The user was asked on 2026-10-10 and hasn't answered.

**The proposed change:**
- Count the updates by event in `ExecutionService._consume`, and count frames by type (text or binary) in `AlpacaTradeStream._recv`.
- Have the smoke test print a line like `trade stream: 5 updates (new, fill, canceled); frames: binary`, and fail when it received none.
- Add offline tests in `tests/unit/test_smoke.py`: one where SimBroker's stream delivers updates, and one where a paused stream means `SMOKE FAILED`.

## Monday 2026-10-12

It's Columbus Day: the stock market is open and the banks are closed.

**The human runs, between 09:36 and 15:29 ET:**
1. `uv run trader smoke paper`: a 1-share limit order for F about 5% under the bid. It should be accepted, then cancelled.
2. `uv run trader smoke paper --marketable`: buys 1 share of F at the ask + 0.2%, then flattens.
3. `uv run trader doctor`: `alpaca.A` should show `0 positions, 0 orders`.

**Phase 1 done-checks:**
- During market hours: `uv run trader stream --symbols SPY,QQQ --minutes 2 --news`. It should print minute bars. `--news` also shows whether a second socket can connect, which bears on Q2.
- Any time:
  - `uv run trader universe build`: expect 800–2,000 eligible symbols (this answers Q6);
  - `uv run trader news --symbols AAPL --hours 72`;
  - `uv run trader filings --symbol AAPL`.

### G2 pass criteria (given to the user on 2026-10-10)

**Required:**
1. **Both runs end with `SMOKE PASSED`.**
   - **Default run:** the risk gate approves, then `accepted: entry new, stop leg held`, then `cancelled: entry canceled, stop leg canceled`.
   - **`--marketable`:** the gate approves and the order fills within 10 s (`entry_timeout_s`). Then `stop leg after the fill: held|new|accepted`, then `flattened: flat`.
2. **The output shows `trade stream connected`.**
3. **Book A is flat afterwards,** per doctor. Book B is never touched.
4. **The journal printed by the marketable run shows:**
   - one approved risk check;
   - a 1-share buy fill and a 1-share sell fill;
   - ledger rows for the buy, the sell and a `fee-est:` estimate. The sell should settle Tue 10-13.

**Not failures:**
- the leg's status after the fill, `held` or `new` (that's the fact we're collecting);
- `seeded Book A's ledger with $1000.00 (first use)` on the first run;
- Book A ending a few cents down (the spread; paper charges no fees);
- a refusal before anything is sent.

**Stop and paste everything:**
- `SMOKE FAILED`, or a traceback;
- `risk gate: rejected`, `broker_rejected` or `uncertain`;
- `stop leg MISSING`;
- `still held after flatten`.

A leftover share is sold with `uv run trader flatten --book A`. That also halts Book A until `uv run trader resume --book A`, where the human types `RESUME A`.

### After G2 (Claude)

1. **Read `runtime/logs/2026-10-12.jsonl`.** Look for:
   - `trade stream connected` and `order submitted`;
   - any `unreadable trade update`, `foreign order`, `trade update failed` or `protective stop rejected`.
2. **Write DECISIONS D-039:**
   - the leg's status after a fill. If it's `held`, D-032 already counts a held leg as working once its entry is `filled`. If it's `new`, nothing changes.
   - the stream evidence;
   - that the auth handshake worked.

   If anything contradicts D-030 or D-031, stop and propose a fix.
3. **Tick G2,** set STATUS to Phase 3, and run `/next-task` to get a Phase 3 plan in plan mode.
   - When planning, check memory `deferred-design-items`. One of its items suggests running the P3.9 rules-only backtest early, and it has no decision yet.
   - Don't push redesigns before the end-to-end tests (memory `build-first-then-decide`).

## Environment

The machine runs Windows 11 with PowerShell 7. Memory `windows-path-env` has the details.

- **Bash commands need this prefix until VS Code has been restarted.** Check `command -v uv` first:
  `export PATH="/c/Users/Admin/AppData/Local/Microsoft/WinGet/Packages/astral-sh.uv_Microsoft.Winget.Source_8wekyb3d8bbwe:$PATH" UV_LINK_MODE=copy; cd /d/Projects/StockExperiment && …`
- **Checks:**
  - all: `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src`;
  - gate coverage: `uv run pytest tests/unit/test_gate.py --cov=trader.risk.gate --cov-branch --cov-fail-under=100`.
- **Never, without an explicit instruction from the human in this session:**
  - read `.env` or `config/.env`;
  - run `trader smoke`, `trader run-day`, `trader schedule` or `claude`;
  - edit `risk/caps.py`, `config/risk*.yaml`, `config/settings.yaml` or `config/LIVE_APPROVED`;
  - commit.

## Phase 2 as built (a map for Phase 3 planning)

**`execution/service.py`**
- `ExecutionService(book, broker, journal, records, halts, gate, prices, clock, risk, day: TradingDay)`.
- Methods:
  - `start`, `stop`, `recover`;
  - `try_enter(intent, build_state) → EntryResult`. Its outcome is submitted, rejected, broker_rejected, uncertain or halted. One per-book lock covers building the state, the check, journaling and the submit.
  - `exposure()`, which feeds `GateState`;
  - `cancel_entry`;
  - `flatten(deadline)`.
- Loops:
  - the stream consumer;
  - a 1 s monitor: entry polling, protection within 5 s, the daily-loss check, and a 60 s reconcile confirmed after 5 s.
- The module-level `flatten_account(...)` is the one flatten routine. The CLI and the kill switch use it too.

**`risk/`**
- `gate.py`:
  - `RiskGate(risk, universe).check(intent, GateState) → RiskCheck`;
  - the caller supplies the market data, asset, session times, events and broker equity; the service supplies the exposure;
  - `record_risk_check` writes the result.
- `ledger.py`: `SettlementLedger(journal, book, clock)` with:
  - `seed`: only from a flat account, and only once;
  - `available`;
  - `start_of_day_equity`;
  - a pure `eod_check`, wired in P3.5.
- `breakers.py`:
  - pure evaluators;
  - `Halts`, with scopes `until_resume`, `session` and `marker`;
  - `BookStats`: peak equity and the losing streak.
- `killswitch.py`: `KillSwitch.watch`, wired in P3.5.

**Other execution modules**
- `records.py`: the only writer of broker state to the journal. It books fills as deltas of the cumulative `filled_qty`, and has `read_book_day`.
- `reconcile.py`: reconciliation.
- `alpaca_broker.py`: paper only, 180 requests/min.
- `trade_stream.py`: the trade-updates stream.
- `sim_broker.py`: the simulated broker, with fault injection.
- `orders.py`: tick sizes and request bodies.
- `smoke.py`: `trader smoke paper`.

**Elsewhere**
- `core/calendar.py`: `day_times(...)` → `DayTimes(open, close, entry_start, last_entry, flatten, flat_by)`.
- `runtime.py`: `open_runtime(root)`.
- Migration 0003 (schema v3): adds `halts`, a unique `(book, ref)` on ledger entries, and `orders.group_id`.
- Test rigs in `tests/fixtures/`: `exec_harness.py` (`Harness.settle` / `advance`), `simtime.py`, `fake_trading_ws.py`, `nyse.py`.

**Carried into Phase 3:**
- P3.4: `trades` rows, and a `broker_rejected` reason code.
- P3.5:
  - seed each book's ledger at the first boot;
  - the EOD drawdown and losing-streak breakers, and the EOD cash check;
  - the kill-switch loop;
  - `flatten` at the flatten time.

## Alpaca facts that shape the code (D-030, D-031)

- **OTO orders:**
  - An OTO is a limit entry plus a `stop_loss` leg.
  - The leg is `held` and inactive until the entry has completely filled, and cancelling the entry cancels the leg.
  - Paper partially fills about 10% of orders at random, which is why D-032's protection routine exists.
- **Selling:**
  - The wash-trade rule answers 403.
  - A working stop holds the shares, so a sell gets 403 code 40310000 ("insufficient qty"). Cancel the stop and confirm the cancel before selling.
  - `DELETE /v2/positions` doesn't cancel open orders.
- **Error codes:**
  - 40010001: duplicate client order id (look it up and adopt it);
  - 42210000: sub-penny price;
  - a 403 is classified by its body code, never by the status alone.
- **Account rules:**
  - There's no PDT rule; FINRA retired it on 2026-06-04.
  - Under $2,000 equity an account gets 1x buying power and can't short.
  - Alpaca has no cash accounts, so our ledger enforces settlement.
- **Trade stream:**
  - The endpoint is `wss://paper-api.alpaca.markets/stream`.
  - Paper sends binary frames.
  - Missed events are never replayed, so every connection is followed by a REST resync.
