-- 0001_init.sql: the schema of ARCHITECTURE.md §6 as of 2026-10-09, copied verbatim.
-- Never edit an applied migration (the runner refuses changed files); add 0002_*.sql instead.

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);            -- schema_version, eval_epoch
CREATE TABLE trading_days (date TEXT PRIMARY KEY, open_ts TEXT, close_ts TEXT, is_early_close INTEGER,
  run_status TEXT, notes TEXT);
CREATE TABLE universe_daily (date TEXT, symbol TEXT, name TEXT, exchange TEXT, prev_close TEXT,
  atr14 TEXT, adv14 INTEGER, or_vol_avg14 REAL, eligible INTEGER, exclusion_reason TEXT,
  PRIMARY KEY (date, symbol));
CREATE TABLE or_bars (date TEXT, symbol TEXT, feed TEXT, open TEXT, high TEXT, low TEXT, close TEXT,
  volume INTEGER, PRIMARY KEY (date, symbol, feed));
CREATE TABLE signals (id TEXT PRIMARY KEY, date TEXT, symbol TEXT, strategy TEXT, direction TEXT,
  status TEXT, rel_vol REAL, rel_vol_rank INTEGER, or_open TEXT, or_high TEXT, or_low TEXT, or_close TEXT,
  or_volume INTEGER, atr14 TEXT, adv14 INTEGER, prev_close TEXT, gap_pct REAL, trigger_price TEXT,
  stop_price TEXT, entry_limit TEXT, max_chase_price TEXT, armed_at TEXT, triggered_at TEXT,
  features_json TEXT, eval_epoch INTEGER);
CREATE TABLE decisions (id INTEGER PRIMARY KEY, signal_id TEXT REFERENCES signals(id), book TEXT,
  take INTEGER, reason_code TEXT, detail TEXT, llm_call_id TEXT, decided_at TEXT);
CREATE TABLE risk_checks (id INTEGER PRIMARY KEY, signal_id TEXT, book TEXT, approved INTEGER,
  violations_json TEXT, state_json TEXT, checked_at TEXT);
CREATE TABLE orders (client_order_id TEXT PRIMARY KEY, broker_order_id TEXT, book TEXT, signal_id TEXT,
  symbol TEXT, side TEXT, type TEXT, order_class TEXT, qty INTEGER, limit_price TEXT, stop_price TEXT,
  parent_client_order_id TEXT, status TEXT, submitted_at TEXT, updated_at TEXT, raw_json TEXT);
CREATE TABLE fills (id TEXT PRIMARY KEY, broker_order_id TEXT, book TEXT, symbol TEXT, side TEXT,
  qty INTEGER, price TEXT, filled_at TEXT, raw_json TEXT);
CREATE TABLE trades (id TEXT PRIMARY KEY, book TEXT, signal_id TEXT, symbol TEXT, qty INTEGER,
  entry_ts TEXT, entry_price TEXT, stop_price TEXT, exit_ts TEXT, exit_price TEXT, exit_reason TEXT,
  pnl TEXT, r_multiple REAL, fees_est TEXT, entry_slippage TEXT, entry_delay_ms INTEGER, eval_epoch INTEGER);
CREATE TABLE shadow_outcomes (signal_id TEXT PRIMARY KEY REFERENCES signals(id), entry_price TEXT,
  exit_price TEXT, exit_reason TEXT, shadow_r REAL, mfe_r REAL, mae_r REAL, minutes_held INTEGER,
  path_source TEXT, computed_at TEXT);
CREATE TABLE llm_calls (id TEXT PRIMARY KEY, date TEXT, role TEXT, model_alias TEXT, model_resolved TEXT,
  prompt_sha256 TEXT, schema_sha256 TEXT, started_at TEXT, ended_at TEXT, duration_ms INTEGER,
  exit_code INTEGER, is_error INTEGER, num_turns INTEGER, cost_estimate_usd REAL, schema_valid INTEGER,
  repair_attempted INTEGER, input_path TEXT, output_path TEXT, error TEXT);
CREATE TABLE verdicts (id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, source TEXT, verdict TEXT,
  confidence REAL, catalyst_type TEXT, catalyst_fresh INTEGER, catalyst_confirmed INTEGER,
  risk_flags_json TEXT, reason TEXT, llm_call_id TEXT REFERENCES llm_calls(id));
CREATE TABLE day_modes (date TEXT, book TEXT, mode TEXT, reason TEXT, llm_call_id TEXT,
  PRIMARY KEY (date, book));
CREATE TABLE news (id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, headline TEXT, summary TEXT,
  source TEXT, url TEXT, symbols_json TEXT, received_at TEXT);
CREATE TABLE filings (accession TEXT PRIMARY KEY, cik TEXT, symbol TEXT, form TEXT, filed_at TEXT,
  items TEXT, url TEXT, fetched_at TEXT);
CREATE TABLE ledger_entries (id INTEGER PRIMARY KEY, book TEXT, kind TEXT, symbol TEXT, amount TEXT,
  trade_date TEXT, settle_date TEXT, ref TEXT, created_at TEXT);
CREATE TABLE daily_stats (date TEXT, book TEXT, start_equity TEXT, end_equity TEXT, pnl TEXT,
  n_signals INTEGER, n_triggered INTEGER, n_trades INTEGER, max_intraday_dd TEXT, regime_json TEXT,
  eval_epoch INTEGER, PRIMARY KEY (date, book));
CREATE TABLE lessons (id TEXT PRIMARY KEY, created_at TEXT, source_call_id TEXT, statement TEXT,
  rationale TEXT, evidence_query_json TEXT, claimed_direction TEXT, suggested_use TEXT, status TEXT,
  n_subset INTEGER, n_complement INTEGER, effect_r REAL, ci_low REAL, ci_high REAL,
  evaluated_at TEXT, promoted_at TEXT, demoted_at TEXT);
CREATE TABLE daily_reviews (date TEXT PRIMARY KEY, status TEXT, summary TEXT,   -- status: ok | missing | invalid
  llm_call_id TEXT REFERENCES llm_calls(id), eval_epoch INTEGER, created_at TEXT);
CREATE TABLE signal_reviews (signal_id TEXT PRIMARY KEY REFERENCES signals(id), date TEXT, went_well TEXT,
  went_badly TEXT, why TEXT, factors_json TEXT, knowable_at_decision TEXT, b_process TEXT, b_note TEXT,
  llm_call_id TEXT REFERENCES llm_calls(id), eval_epoch INTEGER);
CREATE TABLE process_issues (id INTEGER PRIMARY KEY, first_seen TEXT, last_seen TEXT, occurrences INTEGER,
  severity TEXT, component TEXT, description TEXT, status TEXT, resolution TEXT, resolved_at TEXT,
  last_llm_call_id TEXT);                                          -- status: open | fixed | wontfix
CREATE TABLE review_notes (id INTEGER PRIMARY KEY, first_raised TEXT, last_raised TEXT, times_raised INTEGER,
  note TEXT, status TEXT, human_response TEXT, decided_at TEXT,
  last_llm_call_id TEXT);                                          -- status: open | accepted | rejected | done
CREATE TABLE incidents (id INTEGER PRIMARY KEY, ts TEXT, severity TEXT, component TEXT, code TEXT,
  message TEXT, detail_json TEXT, resolved_at TEXT, acknowledged_by TEXT);
CREATE TABLE book_state (book TEXT PRIMARY KEY, halted INTEGER, halt_reason TEXT, halted_at TEXT,
  peak_equity TEXT, consecutive_losing_days INTEGER, updated_at TEXT);
CREATE INDEX ix_signals_date ON signals(date);
CREATE INDEX ix_decisions_signal ON decisions(signal_id);
CREATE INDEX ix_trades_book_date ON trades(book, entry_ts);
CREATE INDEX ix_signal_reviews_date ON signal_reviews(date);
CREATE INDEX ix_news_created ON news(created_at);
