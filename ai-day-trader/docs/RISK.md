# RISK.md

Risk controls, in order of defense:

1. Configuration (`config/risk.yaml`).
2. Hard caps in code (`src/trader/risk/caps.py`).
3. The pre-trade risk gate (`src/trader/risk/gate.py`).
4. Broker-side stops on every position.
5. Automatic circuit breakers.
6. The kill switch.
7. An independent watchdog process.

Every layer must work even if the layers above it fail.

---

## 1. Hard caps (code constants, human-owned)

`src/trader/risk/caps.py` defines ceilings that configuration can never exceed. Config loading **fails fast** when a configured value breaks a cap. Claude Code must ask before editing this file; the `.claude/settings.json` ask rule enforces that.

| Constant | Value | Meaning |
|---|---|---|
| `MAX_RISK_PER_TRADE_PCT` | 2.0 | Configured per-trade risk may not exceed 2% of equity |
| `MAX_DAILY_LOSS_LIMIT_PCT` | 5.0 | Configured daily loss limit may not exceed 5% |
| `MAX_POSITION_PCT` | 100.0 | Notional per position ≤ 100% of equity (no leverage) |
| `MAX_CONCURRENT_POSITIONS` | 3 | |
| `MAX_ENTRIES_PER_DAY` | 6 | |
| `LEVERAGE` | 1.0 | Gross exposure ≤ cash; margin is never used |
| `ALLOW_SHORT` | False | |
| `ALLOW_EXTENDED_HOURS` | False | |
| `ALLOWED_ASSET_CLASSES` | {"us_equity"} | |
| `MIN_PRICE_FLOOR` | 1.00 | Absolute floor (the config default is 5.00) |
| `LIVE_MAX_ACCOUNT_EQUITY` | 2000.00 | Refuse to trade a live account with more equity than this |
| `FLATTEN_LATEST_BEFORE_CLOSE_MIN` | 3 | The flatten time may not be later than close − 3 min |

## 2. Configurable defaults (`config/risk.yaml`)

| Key | Default | Notes |
|---|---|---|
| `max_risk_per_trade_pct` | 1.0 | Of start-of-day equity |
| `daily_loss_limit_pct` | 3.0 | Two uses. Pre-trade headroom (R-015): realized loss + open risk + new risk must fit. Intraday breaker (§5): realized + unrealized loss reaching it → flatten that book and halt it for the day |
| `max_position_pct` | 50.0 | Notional per position, of start-of-day equity |
| `max_concurrent_positions` | 2 | |
| `max_entries_per_day` | 2 | |
| `min_price` | 5.00 | |
| `max_price` | 1000.00 | Affordability is also enforced by `qty ≥ 1` |
| `min_stop_distance` | 0.03 | $ per share |
| `max_stop_distance_pct` | 5.0 | Of price |
| `max_chase_pct` | 0.30 | Over `trigger_price` |
| `entry_timeout_s` | 10 | Cancel an unfilled entry |
| `unprotected_position_max_s` | 5 | Flatten a position with no active stop leg after this many seconds |
| `data_stale_halt_s` | 120 | No market data during regular hours → halt new entries |
| `drawdown_breaker_pct` | 10.0 | Peak-to-trough equity per book → halt until a human resumes |
| `consecutive_losing_days_breaker` | 5 | → halt until a human resumes |
| `event_blackout_before_min` / `after_min` | 5 / 10 | Around high-impact events in `config/events.yaml` |
| `llm_failures_degrade_after` | 3 | Book B: no new entries for the rest of the day |

## 3. Pre-trade risk gate rules

Every `OrderIntent` passes through `RiskGate.check(intent, state)`. The gate returns `approved: bool` and a list of violated rule IDs. Any violation means rejection. The gate never auto-adjusts quantities except where R-009 says so. Every rule needs unit tests for its pass and fail cases.

| ID | Rule |
|---|---|
| R-001 | **Mode.** Paper, or live only with the full live gate satisfied (§7). |
| R-002 | **Long only.** The intent opens a long position (buy). Shorts are rejected. |
| R-003 | **Asset.** `us_equity`, tradable, not OTC. Symbol passes the universe symbol rules. |
| R-004 | **Session.** The current time is inside `[entry_window_start, last_entry]` on a trading day, before `flatten_time`. No extended hours. |
| R-005 | **Book state.** The book is not halted (daily loss, breaker, kill switch, `llm_degraded` for B). |
| R-006 | **Positions.** Open positions + pending entries < `max_concurrent_positions`. |
| R-007 | **Entries.** Today's entries < `max_entries_per_day`. |
| R-008 | **Symbol.** At most one entry per symbol per day per book. |
| R-009 | **Settled cash.** `qty × limit ≤ ledger.available_settled_cash(book)`. If over, reduce qty to fit; reject if that reaches 0. |
| R-010 | **Notional.** `qty × limit ≤ max_position_pct × E × day_size_multiplier`. |
| R-011 | **Risk.** `qty × (limit − stop) ≤ max_risk_per_trade_pct × E × day_size_multiplier`. |
| R-012 | **Stop sanity.** `stop ≤ limit − 0.01`; `limit − stop ≥ min_stop_distance`; `(limit − stop)/limit ≤ max_stop_distance_pct`. |
| R-013 | **Price.** `min_price ≤ limit ≤ max_price`; prices comply with sub-penny rules (2 decimals at or above $1). |
| R-014 | **Quantity.** An integer ≥ 1. No fractional shares: it keeps OTO stop legs simple, and fractional support for advanced orders isn't documented. |
| R-015 | **Daily loss headroom.** `realized_loss_today + open_risk + new_risk ≤ daily_loss_limit_pct × E`. |
| R-016 | **Freshness.** The symbol's latest trade or quote is at most `symbol_quote_max_age_s` (default 15 s) old, and the hub is not stale. Use the stream copy; if it's older, refresh through the REST latest-trade/quote endpoint, because IEX trades can be sparse. |
| R-017 | **Tradability.** The symbol isn't halted (if a status feed is available), and wasn't flagged by a corporate action today. |
| R-018 | **Idempotency.** `client_order_id` is unused, and no open order exists for this signal and book. |
| R-019 | **Account sanity.** Broker equity is within ±20% of ledger equity. In live mode, equity ≤ `LIVE_MAX_ACCOUNT_EQUITY`. |
| R-020 | **No naked positions.** Every existing position has an active stop leg; otherwise reject new entries and trigger the unprotected-position handler. |
| R-021 | **Event blackout.** Now is outside the blackout window around every `impact: high` event in `events.yaml`. |

## 4. Cash-account ledger (`risk/ledger.py`)

The ledger enforces cash-account behavior, even though Alpaca paper accounts are margin-enabled. The broker can't be relied on to enforce it in paper.

### Rules

- Track `ledger_entries` per book: `deposit`, `buy`, `sell`, `fee`, `adjust`. Each has a `trade_date` and a `settle_date`.
- **Settlement:** equity trades settle **T+1** on trading days (use the trading calendar, which skips weekends and holidays). Deposits are settled immediately in paper.
- **Settled cash at the start of a day** = settled entries up to and including that day.
- **Rule:** the total cost of buys today must not exceed settled cash at the start of the day. Today's sale proceeds don't settle until T+1, so they can't fund another buy today without risking a good-faith violation.
- **Available settled cash** = start-of-day settled cash − cost of today's buys (filled, plus pending entries at their limit).
- **EOD:** reconcile ledger cash with broker cash. A mismatch above $1 is an incident, and the book does not trade next session until it's resolved or acknowledged.

### Example

> Start of day: $1,000 settled.
>
> 1. Buy A for $500, stopped out at $490 (the $490 is unsettled until T+1).
> 2. Buy B for $500. Total buys today: $1,000, which equals the settled cash.
> 3. A third buy is rejected (R-009), even though the broker would show $490 of buying power.

Property tests (`hypothesis`) must show:

- available settled cash never goes negative;
- buys never exceed it.

## 5. Intraday controls and circuit breakers

- **Daily loss limit (per book).** When realized loss + unrealized (mark-to-market) loss reaches the limit:
  - cancel the book's pending entries;
  - flatten the book's positions;
  - halt the book for the day;
  - alert.
- **Drawdown breaker.** When peak-to-trough equity drawdown reaches 10%, halt the book until `trader resume --book X --ack-drawdown`. The engine prints the drawdown details and requires typed confirmation.
- **Consecutive losing days.** After 5 losing days in a row, halt until a human resumes.
- **Data staleness.**
  - No market events for 30 s during regular hours → warning.
  - No events for 120 s → halt new entries. Existing positions stay protected by broker stops.
  - Reconnect, then backfill bars over REST.
- **Unprotected position.** A filled position without an active stop leg (leg rejected, cancelled, or missing):
  1. If the last price is already at or below the planned stop, market-sell immediately.
  2. Otherwise submit a standalone stop at the planned stop price.
  3. If the stop isn't accepted within `unprotected_position_max_s`, market-sell.
  4. P1 incident.
- **Reconciliation mismatch.** Positions or orders differ between broker and journal → halt new entries for both books, alert, and write reconciliation details to the incident.
- **Flatten verification.** After `flatten_time`, every book must be flat by close − 2 min:
  - retry `close_position` per symbol;
  - escalate to a P1 alert.
- **Book B LLM degradation.** See STRATEGY.md §5.4. It never affects Book A.

## 6. Kill switch and watchdog

### Kill switch

- **File:** if `runtime/KILL` exists, all books flatten immediately and halt new entries. The engine keeps monitoring until flat, then idles. Removing the file does not resume trading; `trader resume` does.
- **CLI:**
  - `trader halt [--book A|B|all] [--flatten]`
  - `trader flatten --book all` (works without the engine running; talks to the broker directly)
  - `trader resume --book X` (typed confirmation)

### Watchdog

`trader watchdog` is a separate process, run by the OS scheduler every 5 min from 09:25 to 16:05 on trading days.

- **Heartbeat:** the engine writes `runtime/heartbeat.json` every 10 s. If the heartbeat is older than 60 s during regular hours:
  - with open positions at the broker → flatten those accounts and send a P1 alert;
  - with no positions → P2 alert.
- **Stop check:** independently checks that every open position has an active stop order. If not, it flattens.
- **Late check:** after close − 2 min, any open position → flatten and send a P1 alert.

## 7. Live-mode gate (Phase 7, human only)

The engine refuses live trading unless **all** of these hold:

1. `mode: live` in `config/settings.yaml`.
2. Environment variable `TRADER_LIVE_CONFIRM=I-ACCEPT-REAL-MONEY-RISK`.
3. `config/LIVE_APPROVED` exists, created by the human (Claude Code is denied edit access). It contains:
   - an approval date;
   - the approved Book policy (A or B);
   - the SHA-256 of `config/risk.yaml` and of `src/trader/risk/caps.py` at approval.
   The engine verifies both hashes at boot.
4. The live account is a separate Alpaca account, and its equity is ≤ `LIVE_MAX_ACCOUNT_EQUITY`.
5. `live.size_multiplier` ≤ 0.5 for the first 10 live sessions (enforced by session counter).

### Live kill criteria (human pre-commitment)

Stop live trading and return to paper if either happens:

- live drawdown reaches 20% ($200 on $1,000);
- a P1 incident has its root cause in code.

## 8. LLM-specific risks

| Risk | Mitigation |
|---|---|
| Prompt injection via news, filings, or web pages | Text is passed as labeled untrusted data. The system prompt forbids following embedded instructions. Output is schema-constrained. Claude has no tools by default. Its output can only veto or reduce. Suspicious strings ("ignore previous instructions", etc.) set `suspicious_content`. |
| Hallucinated symbols or facts | Verdicts for symbols outside the candidate list are dropped. Reasons are journaled for audit. The post-close review scores process quality. |
| Schema drift or invalid JSON | `--json-schema` plus pydantic validation. One repair retry, then the role's fallback (STRATEGY.md §5.4). |
| Overfitting "lessons" | Statistical promotion gate (STRATEGY.md §6.3). Lessons are soft guidance only and can't touch rules or limits. |
| Silent model changes | Log the model alias and any resolved model ID per call. A prompt or model change means a new epoch. |
| Subscription limits or outage | Book B degrades to `llm_unavailable` skips. Book A is unaffected. Calls are capped at `llm.max_calls_per_day`. |
| Silent API billing | The LLM subprocess env strips `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` unless `llm.auth: api_key`. `doctor` asserts `claude auth status` reports `authMethod: oauth_token`. |

## 9. Operational risks

- **Credentials.**
  - `.env` only; the paper keys and the Claude token are separate secrets.
  - The `claude setup-token` token lasts about one year. `doctor` warns from 30 days before the one-year expiry, computed from `CLAUDE_TOKEN_CREATED=YYYY-MM-DD` in `.env`.
- **Clock.** At boot, system time must be within 2 s of Alpaca's clock; otherwise P2 incident and refuse to arm signals.
- **Calendar.** Holidays and early closes come from the Alpaca calendar (cached). DST is handled by `zoneinfo`. Test the March/November transition days and the day after Thanksgiving.
- **Machine.** It must not sleep from 08:15 to 16:45 ET on weekdays. `doctor` warns if the platform's sleep settings can't be verified.
- **Outages.** If Alpaca, the network, or Anthropic is down:
  - the engine retries idempotent reads only;
  - order submission is never blindly retried; query by `client_order_id` first;
  - broker-side stops protect open positions.

## 10. Terms and compliance (do not violate)

- **Fidelity:** its Terms of Use prohibit third-party automated access tools for trading. Never automate it (DECISIONS.md D-001).
- **Anthropic:** use the subscription only through Anthropic's own `claude` CLI, for this personal project. Never script claude.ai, and never pass the OAuth token to other tools (D-002). Re-check the policy status in D-002 each quarter.
- **Alpaca:** use the official API and SDK; respect rate limits.
- **SEC EDGAR:** send a declared User-Agent (name + email, set by the human) and stay well under 10 requests/second (default 5).
- **Personal, non-commercial use only.** Nothing here is investment advice.

## 11. Incident severities

| Severity | Examples | Response |
|---|---|---|
| P1 | Unprotected position; not flat by close − 2 min; reconciliation mismatch with positions; daily loss limit breached beyond the configured value | Immediate push alert; auto-flatten the affected book; halt until the human acknowledges |
| P2 | Engine heartbeat stale with no positions; data stale > 120 s; LLM degraded; clock skew | Push alert; halt new entries as the rule requires |
| P3 | A single LLM failure; scan slow; a minor data gap | Log; show in the daily report |
