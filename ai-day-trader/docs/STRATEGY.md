# STRATEGY.md

The trading rules, the two books, Claude's role, the memory design, and how success is measured. All numbers here are **defaults**: the live values live in `config/settings.yaml` and `config/risk.yaml`, and hard ceilings live in `src/trader/risk/caps.py` (see RISK.md).

---

## 1. Purpose and principles

This project asks one question: **does a frontier LLM, used as a filter on top of a documented rule-based day-trading strategy, improve results enough to justify itself?**

Five principles follow from that question:

1. **Rules decide; the LLM subtracts.** A deterministic strategy generates every candidate trade. Claude may veto a candidate or reduce the day's size. It can never add a symbol, increase size, change a stop, or touch an order.
2. **Measure the LLM against its own absence.** Book A trades the rules alone. Book B trades the same rules plus Claude's vetoes, in a separate paper account. Every signal also gets a simulated "shadow" outcome, so each veto can be scored.
3. **Small, cash-only, long-only.** $1,000 per book, no margin, no shorts, whole shares, buys only with settled cash.
4. **Forward-test only for the LLM.** Never evaluate Claude on dates inside its training data: it may already know what happened. The rules-only strategy may be backtested on history.
5. **Freeze, then evaluate.** Parameters are frozen during the evaluation period (§9). Changes are logged and start a new evaluation epoch.

---

## 2. Instruments, data, timing

| Item | Choice |
|---|---|
| Instruments | US common stocks and ETFs on NYSE, NASDAQ, AMEX/NYSE American, NYSE Arca, Cboe BZX (Alpaca `us_equity`, tradable). No OTC, options, or crypto. |
| Session | Regular hours only (09:30–16:00 ET; early-close days use the calendar's close). No extended hours. |
| Broker | Alpaca; two paper accounts with $1,000 each (A and B). |
| Real-time data | Alpaca free plan: **IEX feed** (single venue), at most 30 streamed symbols. Configurable to `sip` if upgraded. |
| Historical data | Alpaca SIP bars older than 15 minutes (free) for daily features and shadow outcomes. Rolling opening-range baselines use the **same feed as live** (IEX by default) so relative volume compares like with like. |
| News | Alpaca news (Benzinga) WebSocket and REST; SEC EDGAR filings. |
| Time zone | America/New_York everywhere. |

---

## 3. Baseline strategy: ORB5-SIP-LONG

This is a 5-minute Opening Range Breakout limited to "Stocks in Play," adapted to a long-only cash account. It is based on Zarattini, Barbon & Aziz, *A Profitable Day Trading Strategy For The U.S. Equity Market* (Feb 2024).

### 3.1 Universe (computed before the open from prior sessions only)

A symbol is **eligible** on date *t* if all of the following hold:

- It is an active, tradable `us_equity` on an allowed exchange.
- Its symbol contains no `.`, `/`, `-` or space (this excludes most preferreds, units and warrants).
- **ADV14 ≥ 1,000,000 shares**: mean daily volume over the 14 sessions before *t*, from SIP daily bars.
- **ATR14 > $0.50**: Wilder ATR over the 14 sessions before *t*, from SIP daily bars.
- **≥ 14 prior sessions** of opening-range (OR) volume history exist in the live feed.
- **No split or reverse split** effective on *t* or *t−1* (Alpaca corporate actions).

Also store `prev_close` and `or_vol_avg14` (the mean of the 09:30–09:35 volume over the prior 14 sessions, in the live feed).

### 3.2 Opening-range scan (09:35:03 ET)

For every eligible symbol, fetch today's 09:30–09:35 **5-minute bar** in the live feed through the multi-symbol bars REST endpoint. Then:

1. Compute the OR fields:
   - `or_open`, `or_high`, `or_low`, `or_close`, `or_volume`
   - `rel_vol = or_volume / or_vol_avg14`
2. **Liquidity guards for IEX noise:**
   - keep only `or_volume ≥ 1,000` and `or_vol_avg14 ≥ 200` (feed shares);
   - set both guards to 0 when `feed: sip`.
3. **Price filter:** `or_open > $5.00`.
4. **Relative-volume filter:** `rel_vol ≥ 1.0`.
5. **Rank** the survivors by `rel_vol` descending and **keep the top 20**. This top 20 is the paper's portfolio for the day.
6. **Direction:**
   - `or_close > or_open` → **long signal** (armed).
   - `or_close < or_open` → bearish. No trade, because this is a cash account. Record a `shadow_only` short signal for analysis.
   - `or_close == or_open` (doji) → no signal.
7. **Persist** every top-20 row (armed or not) to `signals`, with features.

If the scan has not completed by **09:36:30**, no signals are armed today. Log an incident, run the day as monitor-only, and still do the EOD tasks.

### 3.3 Entry (long signals only)

| Parameter | Default |
|---|---|
| `trigger_price` | `or_high + $0.01` |
| Entry window | from arming (≈09:35:05) to `last_entry` = min(15:30, close − 30 min) |
| Trigger | first stream trade at or above `trigger_price` during the window, while armed |
| Order | marketable **limit** buy, priced at submission time: `limit = min(max(ceil_cent(max(trigger_price, last_price) × 1.0010), trigger_price + $0.02), max_chase_price)`; `time_in_force: day`; Alpaca `order_class: oto` with a **stop_loss** leg. Sizing uses this actual limit. |
| Chase guard | `max_chase_price = trigger_price × 1.0030`. If the last price exceeds it when an entry is about to be submitted, skip with reason `chased` |
| Unfilled entry | cancel if not filled within 10 s (`entry_timeout`), reason `entry_unfilled` |
| Re-entry | **one entry per symbol per day per book**; no re-entry after a stop-out |
| Already above trigger at arming | treat as triggered immediately if within the chase guard, else `missed_at_arm` |

### 3.4 Exit

- **Stop-loss:** `stop_price = floor_cent(trigger_price − 0.10 × ATR14)`. It is placed as the OTO stop leg, so it lives at the broker from the moment of fill.
  - Alpaca requires the stop to be at least $0.01 below the entry limit. ATR14 > $0.50 guarantees at least a $0.05 distance.
- **No profit target** (`take_profit_r: null`). Winners run to the end of day, as in the paper.
- **End-of-day flatten** at `flatten_time` = close − 5 min (15:55 normally): cancel the stop legs, then market-sell.
- **Watchdog/kill-switch exits** are defined in RISK.md.

### 3.5 Sizing and capacity

The variables:

| Symbol | Meaning |
|---|---|
| `E` | start-of-day equity (ledger, reconciled with the broker at boot) |
| `S` | settled cash available (ledger) |
| `risk_per_share` | `limit − stop_price` |

The size is:

```
risk_budget   = max_risk_per_trade_pct (1.0%) × E × day_size_multiplier
notional_cap  = max_position_pct (50%) × E × day_size_multiplier
qty = floor( min( risk_budget / risk_per_share,
                  notional_cap / limit,
                  S_available / limit ) )
```

- If `qty < 1`, skip with reason `qty_zero`.
- `day_size_multiplier` is 1.0 for Book A, and 1.0 or 0.5 for Book B depending on `day_mode` (§5.2).
- **Capacity:** at most 2 concurrent positions and 2 entries per day per book. Total buys per day are also capped by settled cash, so with 50% sizing the second entry uses the rest of the settled cash.
- **Tie-breaks** when several signals trigger in the same second: higher `rel_vol` rank first.

With a $1,000 book, typical numbers are a risk budget of $10, a notional cap of $500, and an actual risk of about $2–$10 per trade. The notional cap usually binds before the risk budget does.

### 3.6 Deviations from the paper (intentional; record any further ones in DECISIONS.md)

| Paper | Here | Why |
|---|---|---|
| Long and short | Long only (shorts logged as shadow-only) | Cash account; under the $2,000 margin minimum |
| Up to 4× leverage, 1% risk per position, up to 20 positions | No leverage, 2 positions max, 50% notional cap | $1,000 cash account |
| Broker stop entry on the consolidated tape | Client-side trigger on the live feed, then a marketable limit order | Cash allocation at trigger time; Book B veto timing. On the IEX feed, triggers can lag the consolidated tape |
| Stop at 10% ATR from the **executed** price | 10% ATR from the **trigger** price (OTO needs the stop at submission) | Stop exists at the broker immediately; difference ≈ slippage |
| Exit at 16:00 | Flatten at close − 5 min | Reliable exit before the closing auction |
| Entries all day | No new entries after close − 30 min | Little time left; risk control |
| Relative volume on consolidated volume | IEX-to-IEX on the free plan | Data cost. Recheck if upgraded to SIP |
| Commission $0.0035/share | Commission-free; estimated SEC/FINRA fees journaled | Alpaca pricing |

---

## 4. Book A — rules only

Book A executes §3 exactly, subject to the risk gate (RISK.md). It has no LLM dependency. If Claude is unavailable, Book A still trades.

---

## 5. Book B — rules plus Claude veto

### 5.1 What Claude may and may not do

**May:**

- Set `day_mode`:
  - `normal` → multiplier 1.0
  - `reduced` → multiplier 0.5
  - `no_trade` → no entries today
- Give each candidate a verdict: `allow`, `neutral` (treated as allow), or `veto`.
- Classify each catalyst and its risk flags (used for measurement and lessons).
- After the close, write a review and propose lesson hypotheses (§6).

**May not:**

- add symbols or signals;
- change triggers, stops, sizes beyond the multiplier, or exits;
- place, modify, or cancel orders;
- edit configuration;
- read or write files outside its sandbox.

Verdicts for symbols that are not among that call's candidates are ignored and logged.

### 5.2 Pre-market brief (starts 09:05, hard deadline 09:28)

- **Input:** a context pack (ARCHITECTURE.md §7). It contains:
  - index ETF pre-market moves and the prior-day regime;
  - today's scheduled events;
  - up to 40 candidates (top gainers among eligible symbols, plus eligible symbols with news in the last 18 h) with gap %, ATR, ADV, recent news, and EDGAR filing flags;
  - the playbook (promoted lessons) and recent performance.
- **Output:** `PremarketBrief` (`day_mode`, plus assessments with verdicts for the candidates).
- **If the brief is missing or invalid by the deadline:** `day_mode = normal`, and every 09:35 signal goes through the vet (§5.3).

### 5.3 Catalyst vet (09:35:05)

- One batched call covers **every armed long signal that has no pre-market verdict**.
- **Model:** fast (default `sonnet`, effort `low`). **Timeout:** 60 s.
- **Input:** OR features (rel_vol, rank, OR range ÷ ATR, OR return), gap, news (last 24 h), filings, and the promoted lessons that match each candidate's features (code pre-computes the matches).
- **Output:** `VetBatch` (one assessment per candidate).
- A pre-market verdict for a symbol stands. The vet does not re-judge it unless `revet_premarket: true`.

### 5.4 Pending and failure policy (Book B)

| Situation at trigger time | Book B behavior | Reason code |
|---|---|---|
| Verdict is `allow` or `neutral` | Enter (normal path) | `taken` |
| Verdict is `veto` | Skip | `vetoed` |
| Verdict still pending | Hold until the verdict arrives or `max_wait_s` (60 s) after arming, whichever comes first; then apply it, re-checking the chase guard | `entered_after_wait` / `chased_after_wait` / `vetoed` |
| Still pending after that | Skip | `vet_timeout` |
| The LLM call covering it failed or returned invalid output (after one repair retry) | Skip | `llm_unavailable` |
| 3 or more failed LLM calls today (`llm_failures_degrade_after` in risk.yaml) | No new Book B entries for the rest of the day | `llm_degraded` |
| `day_mode = no_trade` | Skip all | `day_mode_no_trade` |

Signals that haven't triggered yet keep waiting for their verdict; a late verdict (for example, from a repair call) still applies to them. Book A is never affected by any of these.

### 5.5 Catalyst taxonomy and risk flags (shared enums in `llm/schemas.py`)

`catalyst_type`:

```
earnings, guidance, fda_regulatory, clinical_trial, m_and_a,
contract_partnership, product_launch, analyst_action, offering_dilution,
reverse_split, legal_regulatory, management_change, macro_sector,
sympathy_move, social_meme, crypto_related, no_identifiable_news, other
```

`risk_flags` (zero or more):

```
dilution_offering, shelf_or_atm, reverse_split_recent, promotional_or_pump,
stale_news, rumor_unconfirmed, binary_event_today, earnings_after_close_today,
halt_or_luld_risk, extreme_gap, sympathy_only, conflicting_reports,
suspicious_content
```

Initial veto guidance lives in the prompts, not in code. Claude is told that its veto quality is measured against the baseline, which discourages blanket vetoes.

---

## 6. Memory: post-close review and lessons

The user's "memory section" is implemented as **in-context reflection with statistical gating**. It is not reinforcement learning: no model weights change. Single trades are mostly noise, so lessons must earn their place with data.

### 6.1 Daily review (16:15)

- **Input:**
  - all of today's signals, with A and B decisions, actual trade results, and shadow outcomes;
  - Claude's own verdicts and reasons;
  - the **review targets** (§6.5), each with evidence for "why": the price path after the trigger and news stamped as published before or after the decision;
  - incidents;
  - rolling stats;
  - current lessons (promoted and proposed) with their stats;
  - **history** (§6.6): recent daily summaries, the epoch's factor tally, and open process issues and notes.
- **Output:** `DailyReview`, containing:
  - `summary`;
  - `signal_reviews`: one per review target, covering what went well, what went badly, why, and (for Book B decisions) process quality given the information available at decision time, not outcomes (§6.5);
  - `process_issues`, each new or linked to an open one;
  - up to 3 `lesson_proposals`, each with an `evidence_query`;
  - `human_notes`: anything Claude thinks a human should consider, such as parameter changes, each new or linked to an open one. These are never applied automatically.
- **Storage:** code writes each part to its own table (ARCHITECTURE.md §6), so reviews can be queried across days. When the review is missing or invalid, `daily_reviews.status` records that.

### 6.2 Evidence-query DSL (evaluated by code, never by the LLM)

```json
{
  "filters": [
    {"field": "catalyst_type", "op": "eq", "value": "offering_dilution"},
    {"field": "gap_pct", "op": "gte", "value": 10}
  ],
  "compare_to": "complement",
  "metric": "shadow_r"
}
```

- **Whitelisted fields:**
  - catalyst and verdict: `catalyst_type`, `risk_flags`, `verdict`, `verdict_source`, `confidence`
  - opening range: `gap_pct`, `rel_vol`, `rel_vol_rank`, `or_range_atr`, `or_return_pct`
  - price and timing: `price`, `atr_pct`, `minutes_to_trigger`
  - regime and calendar: `regime_spy_trend`, `regime_vol_bucket`, `day_of_week`, `event_day`
- **Ops:** `eq, ne, gt, gte, lt, lte, in, contains` (`contains` applies to `risk_flags` only).
- **Limits:** 1–4 filters. The metric is fixed to `shadow_r`.
- **Rejection:** unknown fields, ops, or types are rejected and logged.

### 6.3 Promotion and demotion (weekly job, deterministic, seeded bootstrap)

**Promote** a proposal when all of these hold:

- `n_subset ≥ 20` and `n_complement ≥ 20` triggered signals with shadow outcomes.
- `|mean_shadow_r(subset) − mean_shadow_r(complement)| ≥ 0.10 R`.
- The 90% bootstrap CI of that difference (10,000 resamples, fixed seed) excludes 0, in the claimed direction.

**Demote** a promoted lesson when a monthly re-evaluation on all data shows the CI now includes 0. Rejected proposals stay in the table for audit.

### 6.4 Playbook

Code regenerates `runtime/playbook.md` from promoted lessons only:

- At most 15 lessons, ranked by `n × |effect|`.
- Each lesson carries its statement, n, effect, CI, and promotion date.

The playbook goes into the pre-market and vet prompts as **soft guidance**. Lessons never modify rules, risk limits, or parameters.

### 6.5 Review targets and signal reviews

Code picks the review targets, so coverage doesn't depend on what Claude chooses to look at:

- every signal either book traded, wins included;
- every triggered signal Book B vetoed;
- up to `notable_untraded` (default 5) other triggered signals with the largest |`shadow_r`|: big winners neither book took, and big losers both books avoided.

All targets come from the day's top-20 signals, so there are at most 20. For each target the pack carries the evidence: `mfe_r`, `mae_r`, `minutes_held` and `exit_reason` from the shadow path (§7.2), SPY's move over the same window, and news with `published: before_decision | after_decision`.

Each `signal_review` holds:

| Field | Meaning |
|---|---|
| `went_well` | What worked, short and factual; empty if nothing did |
| `went_badly` | What didn't work; empty if nothing. A winner can have entries here and a loser can have entries in `went_well`. |
| `why` | The most likely cause, citing numbers from the pack. "The data doesn't explain it" is an acceptable answer. |
| `factors` | 0–4 tags from the fixed `OutcomeFactor` enum below, so causes can be counted across days |
| `knowable_at_decision` | `yes`, `partly`, or `no`: could the main cause have been seen with only the information available when the decision was made? |
| `b_process` | For a Book B allow or veto: `good_process`, `bad_process`, or `unclear`, judged on the information available then, not on the outcome. Otherwise `not_applicable`. |
| `b_note` | A short reason for `b_process` |

`OutcomeFactor`:

```
# visible before the decision
catalyst_strong, catalyst_weak_or_none, catalyst_stale, dilution_or_offering,
extreme_gap, thin_liquidity, sympathy_only, event_day,
# execution
late_entry, entry_slippage, partial_fill,
# during the trade
trend_continuation, false_breakout, faded_gap, stopped_by_noise,
news_after_entry, market_tailwind, market_headwind,
other
```

**Factors are hindsight labels.** After a loss it's easy to call a breakout "false", or a catalyst "stale". Factor tallies help Claude and the human spot patterns worth testing, but factors are never DSL fields and never promote a lesson. A pattern becomes a lesson only by passing §6.3 on features recorded before the outcome.

### 6.6 Recall: what the review sees from earlier days

Code builds a `history` section for each review pack:

- the last `history_days` (default 10) daily summaries, plus that period's `bad_process` signal reviews;
- the **factor tally** for the current epoch: for each factor, how many targets carried it, their wins and losses, mean `shadow_r`, and how many were marked `knowable_at_decision: yes`;
- open process issues, with first-seen and last-seen dates and how often each has recurred;
- open human notes, with how often each has been raised.

Claude uses this to notice repeats. When a process issue or note matches an open one, Claude links it by id instead of writing a new one, and code bumps its count and last-seen date. Only the human closes issues and notes (`trader followups set`, ARCHITECTURE.md §4.11), recording a response such as "fixed in commit …" or "rejected: …".

A pattern that keeps showing up in the tally and carries `knowable_at_decision: yes` is what Claude should turn into a lesson proposal, written over the whitelisted pre-decision fields (§6.2).

---

## 7. Measurement

### 7.1 Definitions

| Term | Meaning |
|---|---|
| **Signal** | A top-20 long candidate armed at 09:35. Shadow-only shorts are tracked separately. |
| **Triggered signal** | Price reached `trigger_price` within the entry window, judged on SIP 1-minute bars (§7.2). The live trigger time is recorded separately. |
| **Shadow outcome** | Simulated R for a triggered signal, independent of capacity, cash, and LLM (§7.2). |
| **Trade** | An actual paper fill in a book, with entry, exit, P&L, and R from fills. |
| **R** | `(exit − entry) / (entry − stop)` per share. |

### 7.2 Shadow outcome simulation (16:12, SIP 1-minute bars)

1. **Trigger minute:** the first SIP 1-minute bar from 09:35 up to `last_entry` whose high ≥ `trigger_price`. A signal the live (IEX) feed never triggered can still get a shadow outcome; flag it `sip_only_trigger`.
2. **Entry:** `trigger_price + slippage`, where slippage = $0.01 + 0.02% of price (configurable).
3. **Walk the minute bars from the trigger minute:**
   - If a bar's low ≤ stop, exit at min(bar open, stop) − $0.01. In the trigger minute itself, assume the worst: a low at or below the stop counts as stopped.
   - Otherwise exit at the open of the `flatten_time` minute.
4. **Record** `shadow_r`, `exit_reason`, and `path_source = sip_1min`, plus path stats for the review (§6.5): `mfe_r` (best excursion in R before exit), `mae_r` (worst excursion), and `minutes_held`. Path stats are outcomes, so they are never DSL fields.
5. **Shadow-only shorts** mirror these rules: trigger at `or_low − $0.01` (a bar's low ≤ trigger), stop at trigger + 0.10 × ATR14, the same slippage and worst-case conventions, and R with signs reversed.

### 7.3 Metrics (per book, per epoch, rolling)

- **Per-trade results:** trades, win rate, mean R, expectancy in $ and R, profit factor.
- **Equity results:** max drawdown, daily Sharpe, exposure.
- **Execution quality:** average entry slippage vs trigger, entry delay (Book B).
- **Comparison:** SPY buy-and-hold over the same days.
- **Veto value:** `V = mean(shadow_r | B allowed) − mean(shadow_r | B vetoed)`, with a 90% bootstrap CI and the n of each side.

---

## 8. Pre-registered hypotheses and evaluation gate

### Hypotheses

| ID | Statement | Evidence |
|---|---|---|
| **H1** (primary) | Claude's vetoes have information value: `V > 0`. | Shadow outcomes |
| **H2** | Book B's mean R per trade ≥ Book A's. | Actual trades |
| **H3** | On `reduced` or `no_trade` days, triggered signals have lower mean `shadow_r` than on `normal` days. | Shadow outcomes |
| **H4** (sanity, rules only) | Higher relative-volume ranks earn higher mean `shadow_r`, as in the paper. | Shadow outcomes and backtest |

### Minimum data before deciding

- at least 60 trading days in the epoch;
- at least 150 triggered signals;
- at least 20 Book B vetoes. If Claude rarely vetoes, B ≈ A and H1 is untestable; report that.

### Recommend live trading (Book B policy) only if all hold

1. The lower bound of V's 90% CI is above 0.
2. Book B's mean R per trade, after estimated fees, is above 0.
3. Book B's max drawdown ≤ 15%.
4. There have been at least 20 consecutive unattended sessions without a P1 incident.
5. The human signs off.

### If the criteria are not met

| Outcome | Next step |
|---|---|
| H1 fails, Book A positive | The LLM adds nothing measurable. Consider running the rules alone, or continuing paper. |
| Both books negative | Stop, or redesign the strategy (human decision). |

---

## 9. Change control

- **During an epoch,** only bug fixes are allowed. Any change to a parameter, prompt, model, or data feed increments `eval_epoch` (stored in the DB) and gets a DECISIONS.md entry.
- **Reporting** always shows metrics per epoch. Prompt hashes and model aliases are journaled per call.

---

## 10. Known limitations (say these out loud in every report footer)

- **Data:** IEX-only real-time data makes triggers lag, and makes relative volume noisier.
- **Paper fills:** they ignore market impact, queue position, and latency slippage. Regulatory fees and dividends aren't simulated either.
- **Sample size:** small samples mean wide confidence intervals. Two to three months of signals cannot prove a durable edge.
- **Backtest vs live:** the paper's backtest (2016–2023, with leverage, long and short) is not a forecast. Day-trading edges decay, and most retail day traders lose money.
- **LLM drift:** model updates behind an alias (`opus`, `sonnet`) can change behavior mid-epoch. Log the resolved model ID when available.

---

## 11. Optional Book C (Phase 8, only after Phase 7)

Claude proposes setups from a **fixed, parameterized menu**, for example:

- `orb15_sip_long`
- `vwap_reclaim_long`
- `premarket_high_break_long`

Each setup has deterministic trigger and stop definitions in code. Book C runs in a third paper account with the same risk gate. This tests "LLM as discretionary trader" without ever letting it place orders.
