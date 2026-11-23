# HANDOFF: Phase 5 done (plan `docs/PHASE5_PLAN.md`; P5.0a–P5.5, D-073 to D-079); next Phase 6's plan (updated 2026-11-22)

**Latest:**
- **Phase 4's plan is approved** (2026-10-14), now at `docs/PHASE4_PLAN.md`. Its file under `~\.claude\plans` was removed by Claude Code's cleanup of old files; the copy in the repository is its unchanged text (D-068). Order: P4.0a, P4.0b, P4.0c, P4.1–P4.6, 🛑 G4a, P4.7–P4.10, 🛑 G4b.
- **P4.0a is done (D-055).**
  - Fees are booked from Alpaca's FEE activities, at boot (before E) and at EOD. The EOD cash check is the same in paper and live, and stores its residual.
  - D-054's fixes:
    - #2: stuck-exit timing runs from our own send;
    - #3: stale marks use the broker's price;
    - #4: entry delay is on the PC clock, plus an hourly skew reading;
    - #7: `boot.json` names the source by hash and lists the engine's capabilities.
  - An adversarial review, and a second pass on its fixes, found 2 major and 12 minor problems; all are fixed, each with a test that fails without its fix. 763 tests pass.
- **P4.0b is done (D-056).**
  - A size skip is named by R-006 or R-007 when one also binds (labels only; G3's STT now reads `capacity_entries`, pinned as `g3_day.CODE_CHANGES`).
  - Order submits, cancels and sells go ahead of polls in the rate limiter, and polls leave it 4 tokens; a working entry is read every 2 s, so two working entries make 120 calls a minute, not 240.
  - Its review found no blocker; the five minor findings are fixed and tested. 784 tests pass.
- **P4.0c is done (D-057). D-059 restates the backtest from the human's tape fetch:**
  - Book A −16.8% (D-042: −20.2%); shadow longs −0.072R, or −0.183R without one +126.7R signal Book A didn't take (MRNA, 2026-08-19).
  - H4: ranks 1–5 do best under every check, but there is no gradient below them, and the evidence is thin.
  - Shadow outcomes enter at max(trigger, the trigger minute's open), so gap-throughs are fixed (CRWG: +3.07R, not +5.08R).
  - A trigger minute that opened below the trigger, touched the stop and closed above it is read from the SIP trade tape: live at 16:12 (one request each, capability `shadow_tape`) and in the backtest (`--tape`, cached in `bt_tape`).
  - Its review found no blocker; every finding is fixed, including a look-ahead in cached backtest reruns that predated it. 799 tests pass.
- **The fee probe is done (D-058).**
  - Paper lists each trading day's CAT and REG charges, dated the trade day and posted that evening (20:05 and 20:15 ET).
  - A dry run on a copy of the journal books 6 charges and 3 reversals, with a $0.00 residual against the broker's $996.08.
- **D-060:** verifying D-058 found a fee edge case: an older day's charge posting late could retire a later day's estimates before that day's own charge posted. Fixed in `fee_reconciliation`, with a test; D-058's dry run is unchanged.
- **P4.1 is done (D-061).**
  - Claude's output models and their schemas (`schemas/*.json`, `full` and `wire_safe`) are strict and closed, and each answer echoes the pack's `pack_id`.
  - Over-long free text and free lists are clamped before validation; numbers, enums, patterns and set-like lists never are.
  - `jsonschema` is a new dev dependency. Reviewed; 853 tests pass.
- **P4.2 is done (D-062), uncommitted.**
  - `llm/sanitize.py` cleans news for the packs until it settles; markup that won't settle is removed and flagged.
  - It flags injections across several views and folds. On the reviewers' corpora: no false positives on 735 benign items, and 94% of 247 attacks caught.
  - A flagged item will set its candidate's `suspicious_content` in code (P4.4). Reviewed by three agents; 1,008 tests pass.
- **P4.3 is done (D-063).**
  - `llm/candidates.py` picks the brief's 40 candidates: 30 gainers, then 10 more with news.
  - `llm/context.py` builds the three packs:
    - third-party and model text only under `untrusted`;
    - a count of what every cap leaves out;
    - trims to 300 KB;
    - a `pack_id` from the pack's hash;
    - bytes that depend only on the inputs.
  - Its review found no blocker. The one confirmed major: filings showed as "none" when never fetched.
  - It also fixed three things in P4.2's sanitizer: a quadratic parser, a cut that wasn't idempotent, and no per-field cap for packs. 1,065 tests pass.
- **P4.4 is done (D-064).**
  - `LlmCalls` (`llm/client.py`) is the one runner: an attempt and at most one repair per call, a `llm_calls` row per attempt, and `run()` never raises for a failure.
    - `LLMResult.call_id` is the attempt whose result stands: every `llm_call_id` column holds one.
    - `persist()` lets P4.7 and P4.10 store an answer in the transaction that completes it.
  - **Refusals:** for time or the cap, no row. A dirty sandbox, a missing prompt or a pack over its cap is journaled as a failed attempt that counts toward `llm_degraded` but not the cap (decision 4).
  - **Artifacts:** ASCII, redacted (a cut tail loses 512 characters at its edge), and fail-closed: no call without its input on disk, no answer without its output.
  - **The CLI** runs with the instruction right after `-p`, `--setting-sources user` (new: nothing from the repository's `CLAUDE.md` or `.claude/` loads) and `--safe-mode`. On Windows only the native `claude.exe` will do.
  - **Stopping a call:** CTRL_BREAK, 5 s, then System32's `taskkill /T /F`. Exit is read from the return code; a cancelled call, even at a loop's teardown, kills its child.
  - Tested with `scripts/fake_claude.py` (16 scenarios) and `ScriptedTransport`, also under simdrive; a guard on `subprocess.Popen` refuses the real `claude` in every test.
  - Its review found no blocker; the 7 confirmed majors are fixed (threads under simdrive, `call_id` against the foreign keys, errors leaving rows open, a lone surrogate, the CLI left running at a teardown). 1,230 tests pass; 91 mutations caught.
- **P4.5 is done (D-065).**
  - The three prompts are v1 and hold no numbers: every setting reaches Claude in the pack (`experiment`, `limits`, `book_b`, `as_of`), so a changed setting can't leave a prompt stating the old one. Each asks for the `pack_id`; the brief's web-tools sentence is gone.
  - A test checks that every name a prompt quotes exists in its role's real pack or in the answers' schemas.
  - `llm/epoch.py` fingerprints what the calls ask (prompts, instructions, schemas, models, efforts, flags). A change is a P3 `llm_fingerprint_changed` at boot, wired in P4.8; bumping the epoch stays the human's call (S§9).
  - Prompt hashes ignore line endings, since `core.autocrlf` can rewrite the prompts with CRLF on checkout. 1,255 tests pass; 20 mutations caught.
- **P4.6 is done (D-066): `trader smoke llm`, the code only.** Running it was G4a (the runbook below).
  - **A call per role** on packs of fictional issuers (six-letter symbols; the vet at full size, 25 candidates with 5 news items each), through `LlmCalls` with purpose `smoke`, so nothing counts toward the day's budget. Each prints the envelope's fields, the answer's counts, and what became of the injection headline. The sandbox must stay empty.
  - **The isolation canary** plants every source of configuration a call could pick up: instructions with a code word; settings whose hooks leave a marker and whose `env` block and `apiKeyHelper` would fail the call; an `.mcp.json` server that leaves a marker. It plants them above an empty sandbox and in the working directory. `--canary-control` reruns it with the isolation flags off, to show each detector fires.
  - **`--kill-test`:** the vet with a 3 s timeout, watched for the processes the CLI starts (`llm/descendants.py`), so G4a shows how the real CLI stops and whether anything outlives it (D-064's residual).
  - It holds `run-day.lock`, so a session and a smoke run never share the sandbox. 1,308 tests pass; 50 mutations caught.
- **🛑 G4a passed (D-067), 2026-11-17, on Claude Code 2.1.296.**
  - **Q4 is answered:** `structured_output` works under every isolation flag, with the `full` schemas, and no fallback was needed. Every call took 2 turns and exited 0, with nothing repaired, clamped or dropped.
  - **Timings:**

    | Call | Took | Limit |
    |---|---|---|
    | the brief | 59.5 s | 600 s |
    | the vet, at full size | 16.5 s | 60 s |
    | the review | 107.6 s | 600 s |

  - **Cost:** about $0.86 a day at list prices, which the subscription covers.
  - **The injection:** no role followed the headline, and Claude flagged it itself.
  - **The canary** was clean in both halves, and its control fired every detector.
  - **The kill test:** CTRL_BREAK stopped the CLI inside the grace, and nothing outlived it.
  - **D-021:** `.claude.json` lives in `runtime/claude-config`. The subscription is a personal plan, so no admin-managed settings reach the calls.
  - **Found the same evening:**
    - The PC clock was 14.49 s off because the Windows Time service was stopped. Fixed at 20:54: the service is running and synced, now 0.005 s off.
    - The smoke's review pack has P&L totals that don't match its trades. That fix goes in P4.10.
  - **Settings:** the human set `llm.max_tested_cli_version: "2.1.296"`.
- **P4.7 is done (D-068): Book B.**
  - **`books/llm_filtered.py`** decides in decision 8's order:
    1. a halt;
    2. degradation, re-read before every decision;
    3. `no_trade`;
    4. a veto, before the chase guard;
    5. `llm_unavailable`;
    6. pending: chased on the first look only, else `Defer` until the deadline, then `vet_timeout`;
    7. the shared entry path with the day mode's multiplier (`entered_after_wait` / `chased_after_wait` after a wait).

    Every decision carries the `llm_call_id` it rests on.
  - **`books/verdicts.py` (`VerdictStore`):**
    - the brief's and the vet's answers are stored by the runner's `persist`, then memory follows;
    - vet jobs (`expect_vet` → `vet_started` → `vet_done` / `vet_abandoned`);
    - deadlines are max(armed_at, the call's start) + 60 s, never past the last entry;
    - `load()` rebuilds after a restart, holding pending again every armed long no call judged.
  - **`books/entry.py`:** the entry path both books share. Book A is unchanged: a differential check over 13,824 cases found no difference, and the goldens pass byte-identical.
  - **Also:**
    - migration 0008 (the verdicts' and day modes' columns);
    - a trigger that waited is priced from the feed, or from one REST snapshot if the feed is silent.
  - **Review:** three adversarial reviewers found no blocker, 2 majors and 13 minors, all fixed. 74 mutations caught; 1,383 tests pass.
- **P4.8 is done (D-069): Book B in the day.**
  - **Its jobs** (`orchestrator/llm_jobs.py`):
    - **boot:** abandoned attempts closed, the fingerprint noted, the store rebuilt. A boot that fails halts Book B for the day with a P2, and Book A goes on. The news feed starts in the background once the stock stream is connected, and retries each minute;
    - **the 09:00 scan and the 09:05 brief** (deadline 09:28; no attempt after 09:27; never asked twice);
    - **the vet at arming** (EDGAR for at most 10 s; its deadline is the last entry);
    - **the review after the shadow job**, which runs once P4.10 provides its content.

    After each call: a P3 per failed attempt, a P2 for a refusal a human must fix, and at three failed attempts the `llm_degraded` halt with one P2. No job raises into the day.
  - **The worker** parks a trigger Book B defers and asks it again when the verdicts change or at its deadline; the last entry settles any still parked. Book A's loop is unchanged.
  - **`run-day --books A,B`**, and **`--no-llm`** (also `llm.enabled: false`, or a missing CLI or token, with a P2): Book A runs alone, and the boot record and the report say why.
    - `--sim` uses a scripted model and no credential.
    - `--dry-run` shows Book B's times and whether its LLM is ready.
  - **Recording and replay:** `llm.jsonl.gz` holds every pack and each attempt's answer (redacted, flushed at once). `trader replay` serves them back at their recorded times, with no news, EDGAR or `claude`.
  - **The report:** Book B's calls, budget, failures and day mode; a B-verdict column; an LLM-calls table; Claude's text, in code spans so it can't make a link.
  - **Found:** the simulated brokers gave both books the same order and fill ids, so an A+B simulated day mixed them up. Fixed: ids now carry the book, and Book A keeps its own, so its goldens are unchanged.
  - **Review:** two adversarial reviewers found 4 majors, 8 minors and 10 nits. All are fixed but two, which D-069 records as known limitations: a refusal made before the transport doesn't replay, and a torn gzip member loses what follows it.
    - **The majors:**
      - a Book B trigger parked at a crash was lost when Book A had entered it;
      - the brief's index gaps were measured from two sessions back;
      - a cut stderr kept most of the credential in the recording;
      - a Book B boot failure stopped Book A's day.
    - **Then 210 mutants:** 204 caught, 5 equivalent, and 1 left for P4.9's restart tests. 1,504 tests pass.
- **P4.9 is done (D-070): end-to-end A+B days.** Tests and fixtures only; no engine code changed.
  - **The synthetic day gained Book B's morning:** pre-market prints, news and filings, and snapshots with daily bars as D-024 describes. The brief now has candidates (GAPPER, LOSE, SHORT), and GAPPER's injection headline gets flagged. Book A sees none of it: its golden day is byte-identical.
  - **Fourteen new tests,** each a whole simulated day:
    - a brief's veto that frees Book B's cash for CAP;
    - a trigger past its chase cap when its verdict arrives;
    - answers still invalid after their repair, which degrade Book B and leave Book A's day as it was;
    - `no_trade` and reduced days;
    - a failed brief and a late start;
    - a restart mid-vet, and one after the vet;
    - the call cap, which fails closed;
    - an A-only day that asks for none of Book B's data.
  - **The A+B golden day** (`tests/golden/standard_day_ab.json`):
    - it is the same under two hash seeds;
    - its replay reproduces every row, each call's `pack_id` included;
    - Book A's rows on it equal Book A's own golden day's.
  - **Found:** a waited trigger is priced from its last print, and R-016 allows 15 s. On a thinly traded name, Book B's wait can cost an entry that Book A keeps. That is for the evaluation to watch. 1,517 tests pass.
- **P4.10 is done (D-071): the review's content and the follow-ups.**
  - **`memory/reviews.py`** fills the 16:15 review job.
    - **Targets:** the day's trades, Book B's vetoes on triggered longs (a long only SIP saw trigger included), and the 5 largest untraded moves.
    - **Each target's evidence:** its decision time (when the pack behind Book B's verdict was sent), SPY's move over its window, and news stamped before or after the decision.
    - **Storing the answer:** the pack carries `history` and the epoch's stats. The answer goes to its tables in the call's own transaction. A linked issue or note is bumped and keeps its first text; an unknown or closed id opens a new one. Lesson proposals are stored as `proposed`.
  - **The shadow job** adds SPY to its SIP request on review days, for `spy_move_pct` (migration 0009). Book A's goldens are byte-identical.
  - **`trader followups list [--all]` and `followups set issue|note ID --status S --response "…"`:** only you close an issue (`fixed`, `wontfix`) or a note (`accepted`, `rejected`, `done`).
  - **The report** lists the issues and notes the review raised, new or recurring, and says why when the review didn't come.
  - **Replay** runs the review only for a day recorded with the new capability `review_v1`. The A+B golden day gained its review.
  - **The smoke's review pack** is now self-consistent (D-067): its books and stats come from its own trades, and the capacity codes are right.
  - **Checks:** 30 mutations of the review's rules, all caught. 1,534 tests pass.
- **🛑 G4b passed (D-072).** The human ran one supervised A+B session on 2026-11-20, on commit `acea04f`.
  - **All five criteria held:**
    1. `DONE`;
    2. no P1 (one P3, a partial fill protected in 0.7 s);
    3. both books flat (`doctor --quick` on 11-21);
    4. the report complete;
    5. `trader replay` matches the recorded day.
  - **Claude's calls:** three, each ok on its first attempt: the brief in 70.9 s (day mode normal), the vet in 8.4 s, and the review in 81.5 s (7 targets, 7 process issues, 2 notes).
  - **The day:** SPY fell 3.51%. Both books took ROST and FNDA, and both stopped out. Book A ended −$6.03 and Book B −$5.22: B's ROST waited 6.3 s for the vet and filled lower.
  - **The day is now a golden replay:**
    - the recording is in `tests/golden/recordings/2026-11-20/`, rebuilt by `tests/fixtures/g4b_day.py`;
    - the test is `test_the_g4b_day_replays_as_it_was_recorded`, about 35 s;
    - the pin is `tests/golden/g4b_2026-11-20.json`.
  - **Findings, all in D-072:**
    - Alpaca paper took 9.5–13 s to accept entries in the 09:35 rush;
    - the shadow fills stops too kindly when the price gaps through them;
    - three labels in the review's pack misled the model (the day's move called a gap, gross trades against net books, the veto count);
    - the `foreign order` warning was our own stop leg, racing the journal;
    - the brief and the review used their spare turn.
- **Commits:** the human's `acea04f` ("Review recorded") holds everything through the G4b runbook. D-072, the G4b fixture and its test, and these doc updates are uncommitted. Commit only if asked.
- **Waiting on the human:**
  - **The Phase 5 plan.** Claude presents it in plan mode. D-072's fixes come first (P5.0), then P5.1–P5.5.
  - **Questions from D-072**, for the plan or for later:
    - `max_turns: 4`, to keep a spare turn?
    - Should leveraged and inverse ETFs stay in the universe?
    - Re-vet a premarket verdict when the open's gap is much larger?
    - The stop's width and a take-profit, decided with Phase 7's data.
  - **The review's follow-ups:** only you close them, with `uv run trader followups set issue ID --status fixed|wontfix --response "…"` (or a note's `accepted`, `rejected` or `done`). D-072 gives Claude's reading of each.
  - **The PC clock is fixed** (2026-11-17 20:54; D-067). It drifts about 35 ms an hour between the Windows Time service's syncs (D-072). The machine that runs the bot (H0.4) must keep that service running. Doctor's `alpaca.clock` and the bot's hourly skew check will catch it if the service stops again.
  - **Checks before G4b, done 2026-11-19 at about 10:00. All passed.**
    - **news:** 2 AAPL articles in 24 h, both from `fiscal.ai`. Alpaca's docs now say articles can come from several feeds, with `source` naming the feed; no code reads `source`. The feed looked thin, since the `*` news stream stored none in 2 minutes. **That was wrong (D-072):** on G4b's day the stream and the backfill stored 2,394 articles, every one from `fiscal.ai`.
    - **filings:** 5 flagged AAPL filings in 30 days.
    - **stream --news:** 4 bars, 315 trades, 1 connection, and `news: stream mode`, so the news socket may run beside the stock socket (part of Q2).
    - **`ALPACA_DATA_*`:** not set, so market data uses Book B's keys and its 200 requests a minute. The runbook allows this.
    - **doctor:** 0 FAIL, 1 WARN. The WARN is `events`: nothing is listed in the 14 days before the 12-04 jobs report. The file runs to 2027-01-08, and the WARN clears at 2026-11-20 08:30.
    - **Recorded in D-072:** Q2's partial answer and the news feed's source.
  - `run-day` isn't needed between gates: it stays a human gate until Phase 6.
- **Phase 5's plan is approved** (2026-11-21): `docs/PHASE5_PLAN.md`, in the order P5.0a, P5.0b, P5.1–P5.5.
  - **Your four decisions:** lessons count only later signals; the bootstrap resamples whole trading days; S§8 gains an "inconclusive" row; `max_turns` goes to 4.
  - **P5.0a is done (D-073):**
    - from the open, the packs' market gives each index its opening gap and its change since the previous close (`{sym}_change_pct`);
    - the review's trades show `gross_pnl_usd` and `fees_est_usd`, its books `net_pnl_usd`;
    - its stats split `b_vetoed_triggers` from `b_veto_verdicts`;
    - one standing-verdict rule;
    - pack version 3, and `max_turns: 4`.
  - **Your next `run-day`** will log a P3 `llm_fingerprint_changed` (pack version and turns). That one is expected.
  - **P5.0b is done (D-074).**
    - Our own OTO stop leg, arriving ahead of its entry's answer, is now logged at info level as `stop leg ahead of its entry's answer`, not as a `foreign order` warning. The resync runs either way.
    - The plan's wait-and-look-again was dropped: it stalled the stream and deadlocked the simulated drivers.
  - **P5.1 is done (D-075):**
    - `memory/stats.py` computes each book's metrics, V with the vetoed side's own mean, live R − shadow R, Book B − Book A, and SPY held;
    - its intervals resample whole days, as you chose;
    - the review's stats gain V;
    - the epoch record is migration 0010.
  - **`trader epoch show|start` is new, and `start` is yours.** P7.1 uses it: `uv run trader epoch start 1 --decision D-xxx --reason "the freeze"`. It refuses while a run-day runs.
  - **P5.2 is done (D-076):**
    - `memory/lessons.py` checks the DSL against a typed whitelist and applies it in Python, never as SQL;
    - promotion counts only signals after the proposal's day, with one look per lesson;
    - demotion runs on the month's first evaluation;
    - the commands are `trader lessons evaluate [--promote]` and `trader lessons list`.
  - **P5.3 is done (D-077):**
    - the promoted lessons now appear in every pack's `playbook`;
    - the vet's candidates get `matching_lessons`, and the brief's don't, since its gap is pre-market;
    - `lessons evaluate --promote` writes `runtime/playbook.md`;
    - `reports/render.py` holds the shared escaping.
  - **P5.4 is done (D-078):**
    - `trader report weekly [--week YYYY-Www]` writes `runtime/reports/weekly-YYYY-WW.{md,html}`: curves, each book, V, lessons, the factor tally, follow-ups, incidents and epochs;
    - it fetches the week's missing SPY closes first, unless `--offline`.
  - **P5.5 is done (D-079), and with it Phase 5:**
    - `trader report evaluation [--epoch N] [--through YYYY-MM-DD]` writes `runtime/reports/evaluation-epoch-N.{md,html}`. Each line of S§8 reads pass, fail or insufficient data, then the outcome, with your inconclusive row. It's offline;
    - on G4b's day it reads "keep running": 1 trading day, 11 triggered longs, 0 vetoes;
    - **one reading is yours to confirm:** before 90 days, a V interval spanning 0 by more than 0.5 R keeps the epoch running instead of failing H1. Your row calls that case inconclusive at 90 days, so an earlier look shouldn't call it a fail;
    - Phase 5's "Done when" holds: both reports render from a multi-day fixture journal and from G4b's replayed one; the DSL and promotion are fully tested; a full playbook fits the packs' size caps.
  - **Next: Phase 6's plan,** in plan mode: the scheduler (P6.1), the watchdog (P6.2), retention and backups (P6.3), the RUNBOOK (P6.4), then gates G6a and G6b.
  - **Nothing from Phase 5 is committed;** commit when you're ready.
- **New since G3, for the G4b runbook** (kept for reference):
  - **Book B's milestones** (D-069):
    - `book B ready` at boot, and `news started`;
    - `pre-market scan` at 09:00;
    - `brief done` soon after 09:05, with the day mode;
    - `vet expected` at arming, then `vet done`;
    - `trigger parked` when a Book B trigger waits for its verdict;
    - the review at 16:15: an `llm_calls` row with role `review`, then `daily_reviews` for the day (`ok`, or `missing` or `invalid` with why). A `no review today` log line means it didn't come (D-071).
  - **After the session:** `uv run trader followups list` shows what the review raised; the daily report lists them too.
  - **Book B's incidents:**
    - P2: `llm_degraded` (Book B halted to the next open), `llm_refused` (a human must fix the cause: the sandbox, a prompt, the pack's size), `llm_job_failed`, `llm_unavailable` (Book B off: no CLI or token), `idle_book_not_flat` (a book kept off holds positions or orders: flatten it or run it);
    - P3: `llm_call_failed` (one per failed attempt), `llm_call_refused`, `brief_missed`, `premarket_scan_failed`, `news_unavailable`, `llm_cli_untested` (the CLI updated itself past `max_tested_cli_version`: rerun the smoke), `edgar_unavailable` (no usable EDGAR user agent).
  - **Log lines:**
    - `fees booked`, at boot and EOD. The first boot after 10-15 should book 10-12 to 10-14's 6 charges and 3 reversals (net $0.00), leaving E at $996.08 if no day trades before it;
    - `clock checked`, every hour;
    - `entry cancel sent`, with why;
    - `position opened`, `position closed` and `flatten pass`.
  - **A P2 `clock_skew ... (hourly check)`** alerts only, and arms or disarms nothing.
  - **New P3s:**
    - `fees_unavailable`: fees weren't read, so a later boot or EOD books them;
    - `cash_drift`: the EOD residual is over half the $1 tolerance;
    - `shadow_tape_unresolved`: an ambiguous trigger minute's tape couldn't be read or settle it, so it was counted as stopped.
  - **At 16:12 the shadow job also reads one SIP trade tape per ambiguous trigger minute** (usually a handful).
  - **`--dry-run` prints the ledger's E before the fees are booked,** so it can differ by a few cents from the E boot uses.
  - **A size skip reads `capacity_positions` or `capacity_entries`** when R-006 or R-007 also binds (D-056), where it used to read `insufficient_settled_cash`.

To resume: read this block, the STATUS block in `docs/PLAN.md`, `CLAUDE.md`, D-072, D-079, and PLAN's Phase 6. The G4b runbook below is done (D-072); keep it for the next supervised session. The G4a runbook after it is done (D-067); keep it for rerunning the smoke test after a Claude Code update. The G3 runbook further down served both of its runs. Everything below the line further down is the older G2 handoff, kept for its environment notes and the Phase 2 code map.

## 🛑 G4b: the supervised A+B paper session (the human)

**Passed 2026-11-20 (D-072).** Its steps still serve any supervised session before Phase 6 automates the day.

**What it proves** (PLAN's Phase 4 "Done when"):
- a whole session with both books;
- every LLM call logged with its artifacts;
- Book B's decisions journaled with their reasons;
- the 16:15 review in its tables, with a signal review for every target;
- a replay of the day reproduces the decisions, with the recorded LLM answers served back rather than asked again.

**Pick a full session.** Thanksgiving, 2026-11-26, is closed, and 2026-11-27 closes at 13:00. That works, but makes a short day.

**Before the session day,** in PowerShell at the repo root, with no `run-day` going:
1. **Phase 1's leftovers.** They answer Q2, so paste all three whatever they show.
   ```powershell
   uv run trader news --symbols AAPL --hours 24
   uv run trader filings --symbol AAPL --days 30
   uv run trader stream --news --minutes 2
   ```
   - **news:** headlines, then `N articles in the last 24 h`. 0 is fine; an error isn't.
   - **filings:** lines with forms, then `N flagged filings for AAPL in the last 30 days`. 0 is fine.
   - **stream, during market hours:** bar lines, then `done: … bars, … trades, 1 connection(s)`, and one of:
     - `news: stream mode, …`: two sockets are allowed;
     - `news: poll mode, …`: only one. Run-day connects the stock stream first, then polls the news over REST.

     A `stream did not connect` that names a connection limit also means one socket.
2. **Market-data keys**, printing names only:
   ```powershell
   Select-String -Path .env -Pattern '^ALPACA_DATA_(KEY_ID|SECRET_KEY)=\S' | ForEach-Object { $_.Line.Split('=')[0] }
   ```
   Two names is right. With none, market data uses Book B's keys and shares its 200 requests a minute. The 09:00 scan adds about 8 requests, so that works, but set them if you can.
3. **`uv run trader doctor`:** every check PASS, and `alpaca.A` and `alpaca.B` show `0 positions, 0 orders`.

   A WARN on `claude.version` means Claude Code updated itself past 2.1.296. Then rerun G4a's smoke (`uv run trader smoke llm --canary-control --kill-test`, the runbook below), and raise `llm.max_tested_cli_version` once it passes.
4. **The evening before:** `uv run trader universe build` (it builds for the next session).

**On the session day, before 08:25:**
1. **Machine:**
   - Set sleep to Never on AC power, and keep Windows Update paused.
   - Use a standalone PowerShell 7 window at the repo root (`cd D:\Projects\StockExperiment`), not VS Code's terminal: a VS Code restart would kill run-day.
   - Put off any Claude Code update until after DONE.
2. **Pre-flight at about 08:10:** `uv run trader run-day --books A,B --dry-run`. Expect:
   - `session <date>` and the timeline, with Book B's rows: `09:00:00 pre-market scan (Book B)`, `09:05:00 Claude's pre-market brief (Book B)`, `09:28:00 the brief's deadline`, `16:15:00 Claude's post-close review (Book B)` and `16:29:00 the review's cutoff`;
   - `Book B's LLM: claude_cli, C:\Users\Admin\.local\bin\claude.exe; credential set`;
   - `clock skew … (ok)` and `universe: <n> eligible`;
   - `Book A: cash 996.08, …, 0 positions, 0 open orders; ledger E <within cents of 996.08>; halts none`;
   - `Book B: cash 1000…, 0 positions, 0 open orders; ledger not seeded (seeds at boot); halts none`. This boot seeds Book B's ledger from its flat account;
   - no `KILL FILE PRESENT`.

   **Don't start, and paste the dry run instead,** if:
   - Book B's LLM reads `UNAVAILABLE`;
   - a book shows positions, orders or a halt. For a halt, `uv run trader resume --book A` (type `RESUME A`), then dry-run again;
   - the universe isn't built.
3. **Start by 08:30:** `uv run trader run-day --books A,B`. It prints `run-day <date>: Book A, B; logs in …`. Within a minute you should see:
   - `book B ready`;
   - `fees booked` for Book A: 10-12 to 10-14's 6 charges and 3 reversals, net $0.00;
   - `stream connected … /v2/iex`, then `news started`.

**During the day** (G3's rules hold: never stop run-day before DONE, and run no other `trader` command except the emergency stop):
- **Milestones:**
  - **09:00:** `pre-market scan eligible=… printed=… candidates=…` (at most 40), then an `edgar` line.
  - **09:05:** the brief. Within about a minute, `brief done ok=True … day_mode=…` (G4a's took 60 s; the deadline is 09:28).
  - **09:29:** `subscribed`.
  - **09:35:** `scan done` and the armed signals, then `vet expected job=1 symbols=[…]` and `vet done job=1 ok=True …` (G4a's vet took 17 s).
  - **Through the day:** `trigger parked` when a Book B trigger waits up to 60 s for its verdict, then its decision.
  - **After the close:** FLATTEN at 15:30, the flatten at 15:55, EOD at 16:05, shadow outcomes at 16:12.
  - **16:15:** the review. Within a few minutes, `review done ok=True … targets=N` (G4a's took 108 s).
  - **16:30:** the report, then `run-day <date>: DONE`.
- **Expected, not a problem:**
  - everything G3's list had;
  - a P3 `llm_call_failed`, then its repair `ok`;
  - Book B decisions `vetoed`, `entered_after_wait`, `chased_after_wait`, `vet_timeout`, and `llm_unavailable` for a symbol an answer left out;
  - any day mode;
  - `news stream refused for lack of a connection slot; polling REST`.
- **Stop and paste, leaving run-day running:**
  - everything on G3's list;
  - `Book B is off today: …` (Book A then trades alone);
  - a P2 `llm_degraded`, `llm_refused`, `llm_job_failed` or `llm_unavailable`;
  - a P3 `brief_missed`, `premarket_scan_failed`, `news_unavailable`, `llm_cli_untested` or `llm_call_refused`;
  - `no review today`, or no `review done` by 16:29.
- **Emergency stop:** from a second PowerShell, `New-Item -ItemType File runtime\KILL`. Run-day halts and flattens both books and still finishes the day; afterwards, `Remove-Item runtime\KILL`. If run-day isn't running, use `uv run trader halt --flatten --book all`.
- **If run-day exits early:** copy its last lines and start it again at once; it resumes. A restart costs the replay criterion.

**Pass:**
1. The last line is `run-day <date>: DONE`.
2. No P1 incidents. The report lists every incident.
3. Both books flat: after DONE, `uv run trader doctor --quick` shows `0 positions, 0 orders` for `alpaca.A` and `alpaca.B`.
4. `runtime\reports\<date>.md` has:
   - `LLM calls: N attempts (…)` and `Book B day mode: …`;
   - the LLM-calls table;
   - `- The review (ok): …` and the review's targets table.
5. With no restart, KILL file or manual halt, `uv run trader replay --date <date>` ends with `the replay matches the recorded day`. It prints nothing until it's done, which takes several minutes.

Then paste:
- the dry run;
- run-day's first lines, its DONE line, and any line flagged above;
- `doctor --quick`;
- the replay's last lines;
- `uv run trader followups list`.

**Afterwards (Claude):**
- **Check, from the journal and `runtime\llm\<date>\`:** every attempt has its row and its artifacts; each of Book B's decisions has its reason and its call; the review has a signal review for every target.
- **Record G4b in DECISIONS:** Q2's answer, each call's duration against its limit, any repairs, the model's day mode and verdicts, and what the review raised.
- **Make the day a golden replay fixture,** as G3's run 2 became.
- Phase 4 is then done, and Phase 5 starts in plan mode.

## 🛑 G4a: `trader smoke llm` (the human)

**Passed 2026-11-17 (D-067).** Rerun it after any Claude Code update: doctor warns once the CLI is newer than `llm.max_tested_cli_version`. When the rerun passes, raise that setting.

**Before, once (H0.2),** in PowerShell at the repo root:
1. Claude Code 2.1.268 or later, as the native `claude.exe`: `where.exe claude` should print `C:\Users\Admin\.local\bin\claude.exe`, and `claude --version` a version.
2. `claude setup-token`. Copy the printed token into `.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`, and add `CLAUDE_TOKEN_CREATED=` with today's date (YYYY-MM-DD).
3. `uv run trader doctor`. Expect:
   - `claude.version` PASS (`llm.max_tested_cli_version` stays unset until G4a, so no WARN yet);
   - `claude.auth` PASS: `authMethod oauth_token, isolated config dir runtime/claude-config`;
   - `claude.token` PASS.

**The run,** with no `run-day` going (the command takes `run-day.lock` and refuses otherwise). It uses the subscription: three role calls, three canary calls and the kill test, for about 5 to 15 minutes.

```powershell
uv run trader smoke llm --canary-control --kill-test
```

**Pass criteria:**
1. **The last line is `SMOKE LLM PASSED`.**
2. **Each role** (`-- premarket`, `-- vet`, `-- review`):
   - its last attempt reads `ok`, with `exit 0, subtype success, is_error no`;
   - `envelope keys:` includes `structured_output`;
   - the answer covers its pack: `40 assessments of 40 candidates`, `25 assessments of 25 candidates`, `8 reviews of 8 targets`;
   - an `injection: QZIVOR flagged in the pack; ..., flags: ... suspicious_content` line for the brief and the vet, and `injection: flagged in the pack: 20261103-QZIVOR-orb5` for the review;
   - `the sandbox is empty after the ... call`.
3. **The canary:** `parent half: clean`, `in-sandbox half: clean`, then `control: every detector fired (instructions, hooks, the MCP server)` and `canary clean: its folder is removed`.
4. **The kill test:** `stopped by ...`, then `nothing it started outlived the stop`.

**Not failures** (paste them anyway: they are what G4a records):
- an attempt `-1` that failed with `schema_invalid`, `wrong_pack_id` or `no_structured_output` and was repaired, with attempt `-2` `ok`;
- any mix of verdicts and any day mode, including on QZIVOR: what matters is its flag;
- `not assessed: ...` or `not reviewed: ...`: a gap, recorded at G4a. Book B will treat a candidate the answer leaves out as `llm_unavailable` (plan decision 8);
- the durations, the cost estimates and the resolved model names;
- `stopped by a kill of its process tree`: the CLI ignored CTRL_BREAK for the 5 s grace;
- `.claude.json is there` or `is not there`: either answers D-021;
- `git isn't on PATH`.

**Stop and paste everything:**
- `SMOKE LLM FAILED`, or a traceback.
- **A role fails with `no_structured_output`, `structured_retries`, `invalid_envelope`, `is_error` or `nonzero_exit`.** Paste it, then try A§7.2's fallbacks one at a time and paste each:
  ```powershell
  uv run trader smoke llm --role vet --no-canary --tools-flag deny_all
  uv run trader smoke llm --role vet --no-canary --schema-mode wire_safe
  ```
- **A `canary` FAIL line** (a code word, a hook, an MCP server, a planted model or key, a file left behind): the isolation is broken, so Book B must not run. The folder stays under `runtime\llm_canary\` for me to read; delete it once I have.
- **A `control:` FAIL line:** the canary can't vouch for the detectors it names.
- **`still running after the stop: <name> (<pid>)`:** paste it, then end each with `Stop-Process -Id <pid>`.
- **`the sandbox ... holds ...`:** paste it, then empty it with `Remove-Item runtime\llm_sandbox\* -Recurse`.

**After G4a (Claude):**
1. Record DECISIONS D-067 from the output and the artifacts in `runtime\llm\smoke\<date>\`:
   - whether `structured_output` works with `--tools ""`, `--setting-sources user` and `--safe-mode` (Q4);
   - the envelope's keys, the subtypes and the exit codes seen;
   - `schema_mode`, and any fallback that was needed;
   - the canary's and the control's results;
   - each role's duration against its timeout;
   - the kill test's stop, and any process left behind;
   - D-021's `.claude.json` answer.
2. The human sets `llm.max_tested_cli_version` (and any fallback) in `config/settings.yaml`.
3. Tick G4a in PLAN, then P4.7 (Book B).

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
- **P3.9 (backtest) is done.** It ran 2026-04-01 to 2026-10-09; the results are at the end of D-042. (Restated 2026-10-15 in D-059, with the trigger minutes read from the SIP tape: Book A −16.8%; longs average −0.07R.)
  - **Book A:** −20.2%; longs average −0.31R.
  - **But:** the shadow R for longs lies between −0.31 and +0.14 depending on how a trigger minute that touched both trigger and stop is read. The approved rule is the pessimistic end.
  - **Waiting on the human:** whether to resolve it from the SIP trade tape. (Decided 2026-10-14: yes, Phase 4's P4.0c, D-057.)
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
