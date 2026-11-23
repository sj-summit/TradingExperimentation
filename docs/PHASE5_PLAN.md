# Phase 5 plan: G4b's fixes, then statistics, lessons, the playbook and the reports

> **About this copy.** The human approved this plan on 2026-11-21, with S§8's new row and the four decisions below. Its working file was `C:\Users\Admin\.claude\plans\squishy-popping-honey.md`; this copy in the repository is the one to read. Decisions taken while building it are in `docs/DECISIONS.md` (D-073 onward), which governs where the two differ.

## Context
- **Phase 4 is done.** G4b passed on 2026-11-20 (D-072), and the day is now a golden replay. 1,535 tests pass. HEAD is `acea04f`; D-072 and the G4b fixture are uncommitted.
- **What Phase 5 builds** (PLAN P5.1–P5.5):
  - `memory/stats.py`: the S§7.3 metrics, the veto value V with its CI, and SPY buy-and-hold;
  - `memory/lessons.py`: the S§6.2 DSL, S§6.3's promotion and demotion, and `trader lessons evaluate`;
  - `memory/playbook.py`: `runtime/playbook.md`, and the `playbook` and `matching_lessons` the packs carry empty today;
  - `reports/weekly.py` and `reports/evaluation.py`, with `trader report weekly|evaluation`.
- **First come D-072's fixes** (P5.0).
- **Facts found exploring the code:**
  - **Bootstrap:** nothing computes a bootstrap or a CI. D-059's intervals were worked out offline.
  - **Lesson fields:**
    - `price` and `minutes_to_trigger` are DSL fields (S§6.2) that nothing stores. The other 15 live in `signals`, in `features_json` and in `verdicts`.
    - Verdicts join their signal by date and symbol: the brief's verdicts have no `signal_id`.
  - **Packs:** `playbook: []` is hard-coded for every role. `matching_lessons` exists only on vet candidates, always empty. The prompts already describe both, so filling them needs no prompt change.
  - **The epoch:**
    - `eval_epoch` is seeded to 0 and nothing ever changes it.
    - `shadow_outcomes`, `decisions` and `lessons` carry no epoch; it comes from their signal.
  - **SPY closes:** `regime_daily` stores SPY's close per session, but only for sessions before a run day, and a day late: Friday's row lands at Monday's boot.
  - **Fixtures:** every fixture and recording is one session. Nothing seeds several days of stats, regime or incidents.
  - **Reports:** `daily.py` builds strings with `html.escape`, with no templates and no charts. Its escaping and limitations helpers are private.
  - **The `foreign order` race (D-072):**
    - The entry's row is written as `submitting` before the POST, but its `group_id` lands only when `_adopt` writes the answer.
    - Live, the journal's separate reader can see the leg's update first. Alpaca sent the leg's `held` event 1–10.5 ms after every recorded submit's answer.
    - SimBroker never emits a leg event at submit, and no test covers the warning.
  - **What the fixes touch:**
    - **The A+B golden** pins each call's `pack_id`, so changing how packs are built changes its ids, and nothing else.
    - **The G4b replay** serves its recorded packs, so pack changes don't touch it.
    - **Adding a column** to `trades` or `daily_stats` would change all four golden pins, since they `SELECT *`. This plan adds none.
    - **Fingerprint:** `PACK_VERSION` and `max_turns` are both in the LLM fingerprint, so the next live boot raises a P3 `llm_fingerprint_changed`. That is expected in epoch 0.
- **Epoch 0 is the shakedown.** P7.1 freezes parameters, prompts and models and sets `eval_epoch: 1`.

## Your decisions (2026-11-21)
1. **Lessons count only later signals:** forward confirmation. A proposal is tested only on signals dated after the day that proposed it.
2. **The bootstrap resamples whole trading days**, each with all its signals: 10,000 resamples, `lessons.bootstrap_seed`, 90%.
3. **S§8 gains a pre-registered "inconclusive" outcome.** The wording is below, for you to approve with this plan.
4. **`llm.max_turns` rises to 4** for every role (P5.0).

## Decisions this plan makes (each recorded in DECISIONS as its part lands)
1. **The packs' market section** (pack version 3).
   - The brief keeps `{sym}_gap_pct`, the pre-market move.
   - After the open (the vet and the review), `{sym}_gap_pct` becomes the real opening gap: today's daily bar's open against the previous close.
   - A new `{sym}_move_pct` holds the move since the previous close: today's daily bar's close, which leaves out after-hours prints.
   - On G4b's day the review would have read gap −0.32 and move −3.51.
2. **The review's money keys say what they hold.**
   - A trade's `pnl_usd` becomes `gross_pnl_usd`, beside a new `fees_est_usd`.
   - A book's `pnl_usd` becomes `net_pnl_usd`.
   - The smoke pack follows the same keys.
3. **The review's veto counts:**
   - `b_vetoes` becomes `b_vetoed_triggers`, Book B's `vetoed` decisions;
   - a new `b_veto_verdicts` counts every veto the brief or the vet gave a long, triggered or not;
   - from P5.1, the stats also carry V, its interval and each side's n.
4. **The foreign-order race.**
   - When `is_ours` misses while a submit is in flight, `_consume` waits for in-flight submits to finish (they `_adopt` their answer), then checks again. Only a second miss is `foreign`.
   - The wait is capped at the submit's own timeout.
   - SimBroker is unchanged, so its pinned sequences stay. The test uses a broker that sends the leg's update before its submit returns, on the threaded journal.
5. **One standing-verdict rule.** "The verdict Book B's decision rests on, else the day's latest" is written twice today (`reviews._b_verdicts`, `daily._b_verdicts`). It becomes one function that stats, lessons, the review and the reports all use.
6. **The veto value** (S§7.3, settled in S§7.3's text):
   - **Population:** the epoch's longs whose shadow triggered (SIP bars, so a SIP-only trigger counts), with a shadow R and a standing verdict, on days Book B ran.
   - **Sides:** allowed means `allow`, or `neutral` per `neutral_counts_as`; vetoed means `veto`.
   - **V** = mean R allowed − mean R vetoed, with its 90% interval from resampled days. Both sides' n are shown, and so is the vetoed side's own mean R with its interval (held item 3).
   - **Signals without a verdict** are counted and left out.
7. **Stats are pure functions over one journal read.**
   - **Per book and epoch:** trades, win rate, mean R, expectancy in $ (gross and net of estimated fees) and in R, profit factor; max drawdown and daily Sharpe from `daily_stats`; exposure from trade times; mean entry slippage and entry delay.
   - **New:** live R − shadow R per trade, which measures D-072's stop-fill bias before anyone changes the shadow rule.
   - **SPY buy-and-hold** over the same sessions, from `regime_daily`.
   - The review pack's stats come from here.
8. **SPY closes for the reports.**
   - `report weekly` and `report evaluation` first fill missing `regime_daily` sessions with run-day's own `ensure_regime`: one SIP daily-bars request.
   - Offline, or if it fails, the SPY line covers the sessions it has and says so.
9. **The epoch record** (held item 2).
   - Migration 0010 adds an `epochs` table: epoch, started_at, reason, DECISIONS id, optional note id, and the hashes of the config files and the LLM fingerprint.
   - `trader epoch show`, and `trader epoch start N --decision D-xxx --reason "…" [--note ID]`, which only you run (as with `followups set`), bump `meta.eval_epoch` and write the row.
   - P7.1 uses it. The weekly report shows the epoch, and Book B − Book A per epoch.
10. **The lesson DSL** (S§6.2).
    - **Validated by type on top of the schema's whitelist:**
      - numeric fields take `eq ne gt gte lt lte in`;
      - categorical fields take `eq ne in`, with enum values;
      - `event_day` takes `eq ne`;
      - `risk_flags` takes only `contains`;
      - `in` takes at most 10 values.
    - **Evaluated in Python over feature rows.** No SQL is ever built from a query.
    - **An invalid query** makes its proposal `rejected`, and the reason is logged and stored (migration 0010 adds `lessons.status_reason`).
    - **Defined here, and in S§6.2:**
      - `price` is the trigger price;
      - `minutes_to_trigger` is minutes from arming to the SIP trigger minute;
      - verdict fields come from the standing verdict.
    - **The population:** the epoch's triggered longs with a shadow R, dated after the proposal's day, for which every field the query names is known. The subset matches every filter; the complement is the rest.
11. **Promotion and demotion** (S§6.3).
    - **One look:** a proposal stays `proposed` until each side has `min_n_per_side`. It is then tested once:
      - it passes if |effect| ≥ `min_effect_r` and the interval excludes 0 in the claimed direction;
      - a pass makes it `promoted`, and a failure `rejected`.
      - One look means no repeated testing until it happens to pass.
    - **Demotion:** the first `lessons evaluate` of each calendar month re-tests promoted lessons on all their later data. An interval that now includes 0 makes the lesson `demoted`.
    - **The CLI:** `trader lessons evaluate` prints without writing; `--promote` writes. Statuses run `proposed → promoted | rejected`, then `promoted → demoted`.
12. **The playbook** (S§6.4).
    - **`runtime/playbook.md`:** up to `max_playbook_lessons` promoted lessons, ranked by n × |effect|. `lessons evaluate --promote` writes it.
    - **In the brief's and the vet's packs:** each entry is `{id, suggested_use, n, effect_r, ci, conditions, untrusted:{statement}}`. The `conditions` are the validated filters: enums and numbers that code checked, so they sit outside `untrusted`.
    - **`matching_lessons`** on both roles' candidates lists the lessons whose filters, on the fields known when the pack is built, all hold.
      - The model sets the verdict fields (`catalyst_type`, `risk_flags`, `verdict`, `verdict_source`, `confidence`) itself, so filters on them aren't checked by code. The model reads them in `conditions`.
      - A lesson matches only if code checked at least one filter.
    - **Pack version 3** covers every Phase 5 pack change, since no live session runs in between.
13. **Shared report rendering.**
    - `reports/render.py` takes `daily.py`'s escaping, `ModelText` and `LIMITATIONS`. The daily report's output stays byte-identical, and its tests prove it.
    - The weekly report draws its equity curves as inline SVG (A, B and SPY, each indexed to 100), with no new dependency. The Markdown shows a table.
14. **Weekly report** (A§4.10):
    - the ISO week, written to `weekly-YYYY-WW.{md,html}`;
    - the week's and the epoch's trade stats per book;
    - V, with its interval and each side's n, plus the vetoed side's own mean R;
    - live R − shadow R;
    - lessons by status;
    - the factor tally, with the hindsight caveat;
    - the week's `bad_process` reviews;
    - open issues and notes (`followups()`);
    - the week's incidents;
    - the epoch record, and Book B − Book A per epoch;
    - the limitations footer.

    `report weekly [--week 2026-W47]` defaults to the last complete week. Tests always pass the week, so none reads the PC's date (D-072).
15. **Evaluation memo** (S§8), written to `evaluation-epoch-N.{md,html}`.
    - **Each hypothesis is shown as pass, fail or insufficient data, with its numbers:**
      - H1: V, with its interval;
      - H2: B's mean R − A's, with its interval;
      - H3: triggered mean R on `reduced` and `no_trade` days against `normal` days;
      - H4: ranks 1–5 against 16–20, plus a Spearman correlation computed with numpy (no scipy).
    - **The minimum data:** 60 days, 150 triggered signals, 20 vetoes. "Vetoes" here means triggered longs with a standing veto, the population H1 is tested on.
    - **Criteria 1–5:**
      - criterion 4 (20 unattended sessions without a P1) reads "insufficient data" until Phase 6 marks unattended runs;
      - criterion 5 always reads "for the human".
    - **Then the outcome table,** including the new "inconclusive" row.

## The S§8 text to approve (decision 3)
Add a row to "If the criteria are not met":

> | Inconclusive: by 90 trading days in the epoch the minimum data isn't reached, or H1 has fewer than 20 vetoes, or V's interval includes 0 while its width exceeds 0.5R | Not evidence either way. The human chooses, once: extend the epoch by 30 trading days, or stop and treat H1 as not shown. A second inconclusive memo means H1 is not shown. |

## Order
| Step | What | Golden files |
|---|---|---|
| P5.0a | Pack fixes 1–3, pack version 3, the standing-verdict function, `max_turns: 4` (settings.yaml asks you first) | A+B golden: the three `pack_id`s only |
| P5.0b | The foreign-order race | none |
| P5.1 | `stats.py`, the epoch record (migration 0010), the review's stats from `stats.py` | A+B golden: the review's `pack_id` |
| P5.2 | `lessons.py`, `trader lessons evaluate` | none |
| P5.3 | `playbook.py`, the packs' `playbook` and `matching_lessons` | none (no promoted lessons on the golden days) |
| P5.4 | `reports/render.py`, `weekly.py`, `trader report weekly` | none (the daily report stays byte-identical) |
| P5.5 | `evaluation.py`, `trader report evaluation`, S§8's new row | none |

## Tasks (tests first wherever money, time, risk, the ledger, selection or sizing is touched)

### P5.0a: the review pack's labels, and max_turns
- **Code:**
  - `orchestrator/packs.py` `_index_gaps`: returns the gap and the move;
  - `llm/context.py`: `Market` and `_market`; `_trade` and the books; `PACK_VERSION = 3`; `DATA_KEYS`;
  - `memory/reviews.py`: `running_stats` and `epoch_stats` renamed, plus the new veto count;
  - `llm/smoke_packs.py`: the same keys;
  - the standing verdict becomes one function in a new `memory/verdict_view.py`, used by `reviews.py` and `daily.py`.
- **Config:** `config/settings.example.yaml` and `config/settings.yaml`: `max_turns: 4` for each role.
- **Tests:**
  - `test_packs.py`: the gap and the move before and after the open, and after-hours prints left out;
  - `test_llm_context.py`: the new keys, and the version;
  - `test_reviews.py`: the veto counts with BILL's case (a veto that never triggered).
  - The standing-verdict function is tested once, and both callers still pass.
- **Docs:** STRATEGY §2's news line becomes "Alpaca news (one or more feeds, named by `source`)".

### P5.0b: foreign-order race
- **Code:** `execution/service.py`: `_in_flight` gets a condition `_consume` can wait on; the re-check; the existing `foreign order` and resync path unchanged.
- **Tests** (threaded journal, the `FillDuringRefresh` pattern):
  - a leg update that arrives before its submit's answer is ours, with no warning and no resync;
  - a truly foreign order still warns and resyncs;
  - an unknown order with no submit in flight is decided at once.

### P5.1: stats
- **Code:**
  - `memory/stats.py`: `bootstrap_by_day`, `book_metrics`, `veto_value`, `live_vs_shadow`, `spy_buy_and_hold` and `epoch_summary`;
  - migration 0010, with `epochs` and `lessons.status_reason`;
  - `trader epoch show|start`;
  - `reviews.py` takes its stats from `stats.py`.
- **Tests:**
  - **A multi-day fixture,** `tests/fixtures/journal_days.py`, seeds N sessions: signals, verdicts, decisions, trades, shadow outcomes, daily stats, regime and incidents. It plants known effects.
  - **Known answers** worked by hand on small cases.
  - **The bootstrap:**
    - it is deterministic under the seed;
    - it resamples whole days: a test where two days' signals move together gives a wider interval than resampling signals would;
    - a resample with an empty side is skipped and counted.
  - **V's population edges:**
    - a SIP-only trigger;
    - a signal with no verdict;
    - a day Book B was off;
    - `neutral`;
    - a veto that never triggered.
  - **The epoch CLI:** only `start` writes; the row and `meta` change in one transaction.

### P5.2: lessons
- **Code:** `memory/lessons.py` (validation, feature rows, evaluation, the one-look promotion, monthly demotion), `trader lessons evaluate [--promote]` and `trader lessons list`.
- **Tests:**
  - every field and op's type rule;
  - **injection attempts:**
    - SQL fragments in values and field names;
    - nested objects;
    - an oversized `in`;
    - unicode look-alike field names;
    - a `contains` on a field other than `risk_flags`;
    - the model's prompt-injection text in `statement`, which is stored but never evaluated;
  - **forward confirmation:** signals on or before the proposal's day never count;
  - a planted effect promotes;
  - a null effect is rejected at its one look;
  - too few signals stay `proposed`;
  - demotion runs only on a month's first run;
  - `--promote` alone writes, in one transaction;
  - the CI's direction against `claimed_direction`.

### P5.3: playbook
- **Code:**
  - `memory/playbook.py`: the ranking, `playbook.md`, the pack entries, and `matching_lessons` on the fields known at pack time;
  - `context.py` and `packs.py` fill both roles;
  - `PremarketCandidate` gains `lessons`.
- **Tests:**
  - the ranking and the cap of 15;
  - matching with known, unknown and model-set fields;
  - a full playbook keeps the brief and the vet packs under `max_pack_bytes`;
  - the statement stays under `untrusted`, and the sanitizer flags an injected statement;
  - a replayed day still matches (its packs are recorded).

### P5.4: weekly report
- **Code:**
  - `reports/render.py`, extracted from `daily.py`;
  - `reports/weekly.py`, its data model and its two renderers;
  - the SVG chart;
  - `trader report weekly [--week]`, with its `regime_daily` fill.
- **Tests:**
  - **renders** from the multi-day fixture, and from the replayed G4b journal;
  - **escaping:** model text with backticks, pipes, `<script>` and a bare URL stays inert in both formats;
  - **empty cases:** an empty week, and a week with no Book B;
  - **the daily report** is byte-identical before and after the extraction;
  - **the CLI** runs with a fake market for the fill, and offline.

### P5.5: evaluation memo
- **Code:** `reports/evaluation.py`, `trader report evaluation [--epoch N]`, and the S§8 edit.
- **Tests:**
  - each hypothesis and criterion: pass, fail and insufficient data;
  - the inconclusive triggers;
  - the minimum-data counts;
  - criterion 4 before Phase 6;
  - criterion 5 never "pass".

## Records and spec edits
- **DECISIONS:** D-073 onward, one entry per part. Each entry names what changed, including the pack version and `max_turns` for the fingerprint's P3.
- **STRATEGY:**
  - §2: the news feed;
  - §6.2: the field definitions and types;
  - §6.3: forward confirmation, the one look, monthly demotion and the statuses;
  - §6.4: matching, and the `conditions`;
  - §7.3: V's population, resampling by day, the vetoed side's own mean R, live R − shadow R;
  - §8: the new row, and how each hypothesis is computed;
  - §9: the epoch record.
- **ARCHITECTURE:**
  - §4.8 (stats, lessons, playbook, verdict_view);
  - §4.10 (weekly, evaluation, render);
  - §4.11 (`lessons`, `epoch`, `report weekly|evaluation`);
  - §6 (migration 0010);
  - §8 (pack version 3, the market, money and veto keys, the playbook entry);
  - §2's stale `templates/`.
- **PLAN:** add P5.0a and P5.0b; STATUS.
- **CLAUDE.md:** the Commands table gains `lessons evaluate`, `report weekly|evaluation` and `epoch start` (marked as the human's).
- **docs/PHASE5_PLAN.md:** this plan, written into the repo on approval.

## Verification
- **After each task:** `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src`.
- **Goldens:**
  - the A+B golden changes only its `pack_id`s, in P5.0a and P5.1, each a reviewed diff;
  - Book A's golden, the G3 replay and the G4b replay stay byte-identical throughout;
  - the daily report's output stays byte-identical.
- **Determinism:** every interval is the same under two hash seeds, and from a fixed seed.
- **Offline:** no test reads the PC's date or touches the network; the report CLIs get a fake market.
- **An adversarial review** of P5.0b, P5.1 and P5.2's diffs, with mutation checks of the DSL validator and the promotion rule, as in P4.10.

## Human steps
1. Approve this plan and S§8's new row. I then write it to `docs/PHASE5_PLAN.md`.
2. In P5.0a, approve the `config/settings.yaml` edit (`max_turns: 4`).
3. **Optional, any day:** a supervised `run-day --books A,B`, which is still your gate until Phase 6. Each session adds epoch-0 data for the weekly report, and the first one after P5.0a shows the expected P3 `llm_fingerprint_changed`.
4. **To settle before P7.1's freeze, not in Phase 5's code** (D-072 and the held items):
   - leveraged and inverse ETFs in the universe;
   - re-vetting a premarket verdict when the open's gap is much larger;
   - the stop's width and a take-profit;
   - telling the veto prompts about the payoff asymmetry (a wrong veto can cost 10R, a right one saves about 1R);
   - `exclude_corporate_action_days`;
   - pinning model ids;
   - recording retention for a whole epoch.

## Not in this plan
- Changing the shadow's stop-fill rule. P5.1 measures the bias first.
- Scheduling the Saturday job: that is P6.1.
- Marking unattended runs: Phase 6.
- A re-vet harness.
- Any prompt change.

## Promises
- No `run-day`, `smoke` or `claude` without your instruction.
- No edits to `caps.py`, `config/risk*.yaml` or LIVE_APPROVED, and no risk value changes. `settings.yaml` is edited only for `max_turns`, which asks you first.
- No commits unless you ask.
