# Phase 6 plan: unattended runs (the scheduler, the watchdog, backups and retention, the runbook)

> **About this copy.** The human approved this plan on 2026-11-23, with the four decisions below, RISK §6's every-minute watchdog and S§8 criterion 4's computation. Its working file was `C:\Users\Admin\.claude\plans\squishy-popping-honey.md`; this copy in the repository is the one to read. Decisions taken while building it are in `docs/DECISIONS.md` (D-080 onward), which governs where the two differ.

## Context
- **Phase 5 is done and committed** (your `0997717`, "P5.5 Done"), with 1,635 tests passing.
- **Phase 6 makes the bot run without you** (PLAN P6.1–P6.4, then 🛑 G6a and 🛑 G6b):
  - Windows Task Scheduler starts `run-day` each weekday;
  - an independent watchdog flattens the accounts if the engine dies;
  - backups and retention protect the data and the disk;
  - a runbook covers what to do when something breaks.
- **Done when:**
  - 5 consecutive unattended A+B sessions;
  - all their reports;
  - the fire drill passed;
  - you have read the runbook.

### Already there (reused)
- `flatten_account`, the one flatten (D-036), and the CLI's `_halt` loop around it (`cli.py`).
- The heartbeat every 10 s (`orchestrator/tasks.py`).
- `pump_alerts`, which sends at least once.
- The online backup after DONE (`day._backup`, D-055).
- Doctor's disk check, which warns under 2 GiB.
- `RunLock`, `load_calendar` and the `JumpClock`/SimBroker test seams.

### What's missing
- `trader watchdog` and `trader schedule`.
- Any retention.
- Doctor's sleep check (RISK §9).
- A record of unattended runs: S§8 criterion 4 reads "insufficient data".

### Found while planning
These were checked against the code and Microsoft's docs, then reviewed by a planning agent.
- **Notifications.** `notify.provider` is `none`, and the null notifier stamps every alert as sent, so no P1 reaches you.
- **Failures the scheduler would miss.** `_run` turns most failures into a quiet exit 1, which a wrapper that only catches exceptions would never see.
- **The engine's own flattens are invisible.** They set no `flattening` marker: only `trader flatten` does. A naive watchdog would double-flatten, and raise a P1, during the engine's 15:55 flatten or a slow cancel.
- **The heartbeat read can fail.** It can collide with the engine's atomic replace on Windows, a beat can be skipped, and the clock can step. If the watchdog acted on one bad read, it would flatten a healthy engine.
- **`smoke paper` would compete with the engine.** It starts a full ExecutionService on Book A, with its own monitor, reconcile and stream. Run during a session, it would compete with the engine for the same book.
- **Replays and goldens.** Anything new inside `DayOrchestrator` would leak into replays and the golden snapshots, which compare `incidents` and `day_runs`.
- **Orphaned `claude` calls.** `claude` children outlive a killed engine, because uv's launcher lets grandchildren break away from its job object.
- **Backup copies.** They keep the WAL flag, which leaves stray `-wal`/`-shm` files beside them, and pruning ignores those files.
- **Task Scheduler facts:**
  - S4U needs "Logon as batch", which Administrators have;
  - per the docs, S4U has "no access to the network";
  - the shortest repetition is PT1M;
  - the default priority is 7, which is below normal;
  - DisallowStartIfOnBatteries defaults to true.

## Two things for you first
1. **`config/settings.yaml` changed after your commit.**
   - All three roles have `max_turns` back at 3; D-073 set 4.
   - Line 1 gained leading spaces.
   - If that wasn't intended, `git restore config/settings.yaml` puts back what you committed. Otherwise the next run logs a new `llm_fingerprint_changed`.
2. **Notifications (H0.5)** are needed before G6a.
   - ntfy: put a hard-to-guess topic in `.env` as `NTFY_TOPIC`, set `notify.provider: ntfy`, then run `uv run trader notify test`.
   - Discord works the same way, through `DISCORD_WEBHOOK_URL`.

## Your decisions (2026-11-23)
1. **The watchdog runs every minute,** 09:25–16:05 ET. RISK §6 said 5 minutes.
   - A dead engine is noticed within about 2.5 min, not 6.
   - The 15:58 and 15:59 runs let the late check still sell.
2. **S4U**, which runs whether you're logged on or not.
   - `trader schedule check` proves network access at G6a.
   - `--logon interactive` is the fallback.
3. **Windows only.** Install refuses a timezone that doesn't follow US Eastern DST.
4. **Retention:**
   - logs are kept for `retention_days` (60);
   - recordings and LLM artifacts go only when older than that and from before the current epoch began. Epoch 0 has no start, so everything is kept until P7.1.

## Decisions this plan makes (each recorded in DECISIONS as its part lands, D-080 onward)

### P6.1a: the scheduler
1. **A new package, `src/trader/ops/`,** replaces A§2's `scripts/install_scheduler_*.py`. Code under `src/` is type-checked and tested.
2. **The Windows tasks** live in the `\ai-day-trader\` folder. Local time is ET on this PC.

   | Task | Trigger | Command | Settings |
   |---|---|---|---|
   | `run-day` | Mon–Fri 08:25 | `run-day --books A,B --scheduled` | 10 h limit; wake; catch up; priority 4 |
   | `watchdog` | Mon–Fri 09:25, every 1 min for 6 h 40 min | `watchdog` | 4 min limit; wake; catch up; priority 4 |
   | `weekly-job` | Sat 10:00 | `weekly-job` | 1 h; catch up; priority 7 |
   | `universe` (only with `--with-universe`) | Mon–Fri 20:00 | `universe build` | 1 h; catch up |

   Every task:
   - **Instances and power:** IgnoreNew; the battery flags off; RunOnlyIfNetworkAvailable false; LeastPrivilege.
   - **Logon:** S4U, or InteractiveToken with `--logon interactive`.
   - **Action:** `<root>\.venv\Scripts\python.exe -m trader.ops.scheduled --root <root> <command>`, with `pythonw.exe` when interactive. Arguments are quoted with `list2cmdline`, and the working directory is the root.
   - **StartBoundary** is local time with no offset, so DST follows the wall clock.
3. **The wrapper, `trader.ops.scheduled`,** is what every task runs.
   - **Output:** it first redirects file descriptors 1/2 and `sys.stdout`/`stderr` to `runtime/logs/scheduled/<job>-<date>.log`, before importing the CLI. The log gets a header (args, pid) and a footer (exit code).
   - **Under pythonw** it allocates a hidden console, so child processes get one and CTRL_BREAK still works.
   - **Failures:** it runs the CLI with `standalone_mode=False`. A non-zero return or an exception is a failure, and gives one P2 `scheduled_job_failed` per job per day. The text is redacted and pushed at once.
   - **Special cases for run-day:**
     - "Another run-day is running" is a quiet skip.
     - A failure before BOOT is retried every 2 min until 09:20 ET.
4. **`trader schedule show|install|uninstall|check`.**
   - **`show`** prints:
     - the entries in ET and local time;
     - the exact command lines and the logon type;
     - each task's state: identical, differs or missing, read through `schtasks /Query /XML`, which isn't localized;
     - the last 5 sessions' unattended status.
   - **`install [--logon s4u|interactive] [--with-universe]`:**
     - **It refuses** when the Windows zone (`tzutil /g`) isn't a US zone that keeps ET's DST, when DST is switched off (`_dstoff`), or when `notify.provider` is `none`.
     - **Otherwise** it prints everything, asks you to type `INSTALL`, and writes `runtime/schedule/<task>.xml`.
     - **Registering:** `schtasks /Create /TN … /XML … /F` for each task, then `runtime/schedule.json`.
     - **"Access is denied"** tells you to rerun from an elevated PowerShell.
   - **`uninstall`** deletes only our tasks, after a y/N, and removes `schedule.json`.
   - **`check`:**
     - It registers a one-off `\ai-day-trader\probe` with the same principal and action, and starts it with `schtasks /Run`.
     - The probe runs `doctor`, then `notify test`, then a write test of `backup_copy_dir` if one is set.
     - `check` waits up to 3 min for the probe's log footer and prints the log.
     - It always deletes the probe, and exits 1 on any FAIL.
5. **`trader weekly-job`:** `lessons evaluate --promote`, then `report weekly`. Each runs even if the other fails, and the job fails if either did.

### P6.1b: unattended sessions, and keeping the PC awake
6. **The record.**
   - `run-day --scheduled` writes a `day_runs` note named `scheduled`, from `live.run_day`'s paper path only, so replays never see it.
   - These commands each write a `day_runs` row of kind `human`, dated by the session they touch: `halt`, `flatten`, `resume`, `smoke paper`, and `run-day` without `--scheduled`.
7. **When a session counts as unattended** (`memory/unattended.py`). All of these must hold:
   - it was scheduled;
   - it ran to DONE with exactly one BOOT, so no restart;
   - there is no `human` row;
   - no `kill_switch` halt was created that day;
   - run-day logged no `scheduled_job_failed`;
   - both books ran: A and B have `daily_stats` rows, and there is no `book_b_off` note.

   A `human` row or P1 on a weekend or holiday counts against the next session.
8. **S§8 criterion 4, for your approval** (§8 is pre-registered).
   - The memo walks the epoch's calendar sessions, so a day with no run breaks the streak.
   - It **passes** when some run of 20 consecutive sessions is both unattended and P1-free.
   - Otherwise it reads **"insufficient data"** while the epoch has fewer than 60 sessions, and **fail** after that.
   - Its numbers: the longest run, and the unattended sessions out of the total.
   - "The criteria code can check pass" now needs criteria 1–4.
9. **Where you see it.**
   - The weekly report gains an Operations table: each session, unattended yes or no and why, its P1s, and the epoch's longest clean run.
   - `schedule show` lists the last 5 sessions.
10. **Keeping the PC awake.**
    - `ops/power.keep_awake()` holds `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` for run-day's life. The display may still sleep.
    - **Doctor's sleep check (`machine.sleep`)** reads, through powrprof, the AC standby timeout and whether wake timers are allowed:
      - PASS if the PC never sleeps on AC, or if it sleeps but wake timers are on;
      - WARN otherwise.
    - **Doctor's `notify` check** warns while the provider is `none`.

### P6.2a: the watchdog
11. **`orchestrator/watchdog.py`.**
    - **Pure parts:** `read_heartbeat`, `coverage` (the engine's own rule: a working sell stop, a held leg whose buy is final, or a market sell at work), `phase` and `assess`.
    - **`Watchdog.run()`** takes the clock, journal, calendar, brokers, notifier and paths as injected dependencies.
    - **`trader watchdog`:**
      - its own log file, `runtime/logs/watchdog-<date>.jsonl`, never the engine's;
      - its own OS lock, `runtime/watchdog.lock` (`RunLock`'s message becomes a parameter);
      - the calendar from the journal;
      - `runtime/watchdog.json` written each run.
12. **Reading the heartbeat.**
    - It is read three times, 200 ms apart, which tells "absent" from "unreadable".
    - A `ts` up to 5 s in the future counts as fresh.
    - Absent, unreadable or older than 60 s is re-read after 25 s before anything is done.
13. **The rules, each run** (window: open − 5 min to close + 5 min, on trading days; anything else exits):

    | When | Heartbeat stale (after the re-read) | Heartbeat fresh |
    |---|---|---|
    | Before the open | P2 `engine_down`; uncovered shares get GTC stops, with a P1 | As in the next row, but uncovered shares get GTC stops rather than being sold. The 30 s re-read applies to books the engine runs |
    | Open to close − 2 min | Books with positions: flatten them, P1 `engine_down_flattened`. Open orders only: cancel them, P2. Nothing open: P2 `engine_down` | Uncovered shares on a book the engine runs, before the flatten time minus 40 s and not flagged as flattening: wait 30 s, read again, and if still uncovered flatten it, P1. A book the engine doesn't run (missing from the heartbeat): flatten at once, P1 |
    | Close − 2 min to the close (late check) | Flatten every book with a position, P1 | The same, P1 once per book per day; none if the engine already raised `not_flat` |
    | After the close | Shares not under a GTC stop get one, P1 | Nothing: the engine's after-close pass protects them |

    - **`runtime/KILL` with a stale heartbeat:** flatten every book with anything open, under a `kill_switch` halt, P2.
    - **Each flatten:**
      - halts the book (`p1_incident`, or `kill_switch`);
      - renews the `flattening` marker;
      - runs `flatten_account` with purpose `incident`;
      - records an incident with component `watchdog`.
    - **Deduplication:** one alert per episode. The key is (date, book, kind, heartbeat ts), kept in `incidents.detail_json`. A P1 for an episode stops its P2s.
    - **Alerts:** the watchdog pumps them only while the heartbeat is stale, so it never races the engine's pump. A broker it can't read is one P2 `watchdog_blind` per book per day.
14. **The shared flatten, `execution/flatten.py` `flatten_books()`.** Used by `trader flatten`, `trader halt --flatten` and the watchdog:
    - its purpose, halt reason and source are parameters;
    - it flattens books concurrently;
    - it first settles any sell an earlier, killed flatten left `submitting`, looked up by client order id.

### P6.2b: the engine's side
15. **The heartbeat** gains `pid` and, per book, `flattening` (a new `service.flattening` property).
16. **The engine watches the watchdog:** one P2 `watchdog_silent` per episode.
    - It fires when `watchdog.json` is more than 10 min old between open + 10 min and the close, while `schedule.json` exists.
    - It is wired through `DayDeps` from the live paper path, so it is `None` in replays.
17. **`claude` children join a kill-on-close job object** that the engine owns (`llm/claude_cli.py`, best effort), so they die with it.
18. **`smoke paper`.**
    - It refuses while an engine's heartbeat is fresh.
    - The exception is `--marketable --hold`. It sends one OTO through the risk gate without starting the service's loops, polls its own order, confirms the stop leg, and exits still holding the share. That is G6b's step 1.

### P6.3: backups, retention, the disk
19. **`Journal.backup`.**
    - It writes a uniquely named `.partial`, sets `journal_mode=DELETE`, checks `quick_check` is `ok`, closes, then `os.replace`s it.
    - Pruning keeps the newest `db_backups_keep` by date name, and removes `-wal`/`-shm` siblings and stale `.partial` files.
    - A failure is a P2 `backup_failed`. One existing test asserts no incident, and changes.
20. **Off-machine copy and the backup command.**
    - **`storage.backup_copy_dir`** is optional and defaults to null, so no edit to `settings.yaml` is needed. It must be a local folder, such as a OneDrive or USB folder: S4U has no mapped drives or network credentials. Each verified backup is copied there and pruned the same way.
    - **`trader backup [--to DIR]`** takes a backup now.
21. **`ops/retention.py`** is a pure plan over date-named entries.
    - **Logs:** `logs/<date>.jsonl`, `logs/watchdog-<date>.jsonl` and `logs/scheduled/<job>-<date>.log` go after `retention_days`.
    - **Recordings and LLM artifacts:** `recordings/<date>/` and `llm/<date>/` follow decision 4; `llm/smoke` is never touched.
    - **Sim and replay days:** `sim/<date>/` and `replay/<date>/` go after `retention_days`.
    - **Safety:** names must match exactly, symlinks and junctions are never followed, every path stays inside its folder, and errors are collected per entry.
    - **`trader retention [--apply]`** is a dry run by default.
    - **`live.run_day`** applies retention after DONE and the backup, then pumps alerts once more, so a Friday `backup_failed` doesn't wait until Monday.
22. **`disk_low`:** a P2 in `live.run_day`'s paper path, before the engine starts, under 2 GiB free (doctor's threshold, now shared).

### P6.4
23. **`docs/RUNBOOK.md`:**
    - the daily 2-minute check;
    - reading the reports, `watchdog.json` and the scheduled logs;
    - halt, resume and flatten;
    - **common failures:**
      - an Alpaca outage, or WebSocket flapping;
      - a Claude usage limit, or an expired token;
      - a reconciliation mismatch, or clock skew;
      - a reboot, or a missed run;
    - token renewal and key rotation;
    - after a P1: wait for the watchdog's alert before restarting run-day;
    - **operating rules:**
      - no edits under `src/`, `prompts/` or `config/` and no `uv sync` between 08:25 and 16:35;
      - Windows Update active hours set to 08:00–17:00;
      - W32Time set to automatic;
      - sleep and wake timers, and Modern Standby;
    - the fire drill.

## Order
| Step | What | Goldens |
|---|---|---|
| P6.1a | `ops/scheduler` (entries, XML, schtasks), `ops/scheduled`, `trader schedule …`, `trader weekly-job` | none |
| P6.1b | `human` and `scheduled` rows, `memory/unattended.py`, criterion 4, the weekly Operations table, keep-awake, doctor's `machine.sleep` and `notify` | none |
| P6.2a | `orchestrator/watchdog.py`, `execution/flatten.py`, `trader watchdog`; RISK §6 | none |
| P6.2b | Heartbeat fields, `watchdog_silent`, the job object, `smoke paper --hold` and its refusal | none (the heartbeat isn't snapshotted) |
| P6.3 | Backups hardened, `backup_copy_dir`, `trader backup`, retention, `disk_low` | none |
| P6.4 | `docs/RUNBOOK.md` | none |
| 🛑 G6a | You review, install and check the schedule | |
| 🛑 G6b | The fire drill, then the 5 sessions | |

## Tests (offline: schtasks, powrprof, SetThreadExecutionState, job objects and the network are all injected)
- **Scheduler:**
  - golden XML per task, which also parses;
  - quoting with spaces and a trailing backslash;
  - the timezone gate (Eastern, Central, `_dstoff`, Arizona, UTC);
  - the exact `schtasks` argv against a fake runner;
  - `check` always deletes its probe;
  - `install` refuses when no notifier is set.
- **Wrapper:**
  - a returned 1 and an exception are both failures;
  - one P2 per job per day;
  - the text is redacted;
  - the streams are redirected before any `trader` import;
  - run-day's retry before BOOT;
  - a run lock already held means a quiet skip.
- **Unattended:**
  - one test per thing that disqualifies a session;
  - a day with no run breaks the streak;
  - weekend actions count against the next session;
  - criterion 4's pass, fail and insufficient data;
  - `_outcome` with criteria 1–4.
- **Watchdog, pure:**
  - every cell of the rules table;
  - the window edges and an early close;
  - an absent, unreadable, stale or future heartbeat;
  - the re-read;
  - a book missing from the heartbeat;
  - the flattening flag and the marker;
  - deduplication across runs and into the next day;
  - KILL.
- **Watchdog with SimBroker:**
  - books end flat, with one P1, a `p1_incident` halt, the marker cleared, purpose `incident`, and fills booked once;
  - GTC stops after the close;
  - open orders only are cancelled;
  - a stuck `submitting` sell is settled.
- **Concurrency:** a live ExecutionService on the same SimBroker and journal, under `run_on_clock`.
  - The watchdog flattens while the engine runs: no stop is placed after the marker, no "foreign order", no reconcile mismatch.
  - Both flatten at once: at most one rejected sell, and no doubled ledger rows.
- **Engine side:**
  - the heartbeat's fields;
  - `watchdog_silent` (off in replays);
  - the job object's flags;
  - `smoke --hold`, and the refusal while an engine runs.
- **Backup and retention:**
  - a copy has `journal_mode` delete and leaves no `-wal` after a read;
  - a failed `quick_check` keeps the previous copy;
  - the copy folder;
  - the retention plan, epoch protection and error collection;
  - `--apply` alone deletes;
  - `disk_low`.
- **Adversarial review:** of P6.2's diff, with mutation checks of the rules table and the unattended rule, as in P5.

## Records and spec edits
- **RISK:**
  - §6: every minute, the rules table, the re-read, deduplication, books nobody watches, KILL, pumping alerts;
  - §9: the sleep check and keep-awake;
  - §11: the new codes.
- **ARCHITECTURE:**
  - §1 (every minute; the wrapper) and §2 (`ops/`);
  - §3 (16:30: a verified backup, retention, one more alert pump) and §4.9 (the watchdog);
  - §4.11 (`schedule`, `watchdog`, `weekly-job`, `backup`, `retention`);
  - §9 (`backup_copy_dir`) and §11 (the heartbeat's fields, `watchdog.json`, the scheduled logs).
- **STRATEGY §8:** how criterion 4 is computed, and that the outcome checks criteria 1–4.
- **PLAN:** Phase 6's tasks rewritten (Windows only, `ops/`, every minute, the split), and STATUS.
- **CLAUDE.md:**
  - The new commands: `schedule`, `watchdog`, `weekly-job`, `backup` and `retention`.
  - `schedule install` is marked as G6a.
- **DECISIONS** D-080 onward, `docs/PHASE6_PLAN.md` (this plan) and HANDOFF.

## Verification
- **After each task:** `uv run pytest && uv run ruff check . && uv run ruff format --check . && uv run mypy src`.
- **Goldens:** the A+B, Book A, G3 and G4b goldens and the daily report stay byte-identical throughout. Everything new in the engine is off in replays.

## Human steps and gates
1. **Approve this plan,** which includes RISK §6's every minute and S§8 criterion 4's computation. Then settle `settings.yaml` and set up notifications (above).
2. **On the PC:** Windows Update active hours 08:00–17:00, W32Time automatic, and the PC either never sleeps on AC or has wake timers on.
3. **🛑 G6a:** `uv run trader schedule show`, then `install`, then `check`.
   - **Pass:**
     - three tasks: 08:25; 09:25–16:05 every minute; Saturday 10:00;
     - the logon is S4U;
     - the probe's doctor shows PASS for the Alpaca, data, journal, disk and claude lines (uv may WARN);
     - the test push reaches you;
     - exit 0.
   - **Stop and paste the output if:**
     - "Access is denied": rerun elevated;
     - network FAILs: reinstall with `--logon interactive` and check again;
     - a claude FAIL: set `llm.claude_bin` to its full path.
4. **🛑 G6b**, on a scheduled day at about 10:00. This day doesn't count toward the 5 sessions.
   1. `uv run trader smoke paper --marketable --hold`.
   2. `Stop-Process -Id (Get-Content runtime\heartbeat.json | ConvertFrom-Json).pid -Force`.
   3. **Pass:** within 5 min the P1 push arrives, Book A is flat at Alpaca, and Book A is halted `p1_incident`.
   4. Then `uv run trader run-day --books A,B` to finish the day, and `uv run trader resume --book A`.
5. **Then:** 5 consecutive unattended A+B sessions (the weekly report's Operations table shows them), and read the RUNBOOK.

## Not in this plan
- **cron and launchd** (decision 3).
- **Restarting a crashed engine automatically.** Only failures before BOOT are retried.
- **Setting the engine's `_closing` when another process halts the book.** This is a known limitation:
  - an entry whose submit lands after the watchdog's cancel pass can rest;
  - R-020 protects it, and the late check flattens it.
- **Running from a separate worktree.** The RUNBOOK's rules cover it.
- **Any change** to the strategy, risk values or prompts.

## Promises
- No `schedule …`, `run-day`, `smoke` or `claude` without your instruction.
- No edits to `caps.py`, `config/risk*.yaml` or LIVE_APPROVED.
- `settings.yaml` only with your OK.
- No commits unless you ask.
