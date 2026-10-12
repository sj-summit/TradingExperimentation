-- Phase 3 state: the strategy, the day orchestrator, shadow outcomes and alerts.
-- All of Phase 3's schema lives here: the runner refuses a migration edited after it was applied,
-- so later needs go into 0005+.
-- regime_daily: SPY regime tags computed from a session's complete daily bars (at the next boot);
--   signals on day t carry the tags of t-1 as pre-decision features.
-- day_runs: the orchestrator's state transitions (kind 'state', a log) and the EOD steps it has
--   finished (kind 'step', at most one row per day and step), so a restart never repeats a step.
-- orders.purpose: why a sell exists (stop_leg, protective_stop, stop_breached, eod_flatten,
--   daily_loss, incident, kill_switch, manual); trades take their exit reason from it.
-- incidents.alerted_at: set once a P1/P2 has been pushed; incidents from before this migration
--   count as alerted, so the first run doesn't push old alerts.
-- shadow_outcomes.trigger_ts / sip_only_trigger: the SIP trigger minute, and whether only the SIP
--   bars (not the live feed) reached the trigger.
-- book_state.streak_through: the last session counted in consecutive_losing_days, so counting a
--   day twice is impossible.
-- final_decisions: the latest decision per (signal, book); decisions are append-only.
CREATE TABLE regime_daily (date TEXT PRIMARY KEY, spy_close TEXT, spy_sma20 TEXT, spy_trend TEXT,
  spy_atr_pct REAL, tercile_low REAL, tercile_high REAL, vol_bucket TEXT, computed_at TEXT);
CREATE TABLE day_runs (id INTEGER PRIMARY KEY, date TEXT NOT NULL, kind TEXT NOT NULL,
  name TEXT NOT NULL, at TEXT NOT NULL, detail TEXT);
CREATE INDEX ix_day_runs_date ON day_runs(date, kind);
CREATE UNIQUE INDEX ux_day_runs_step ON day_runs(date, name) WHERE kind = 'step';
ALTER TABLE orders ADD COLUMN purpose TEXT;
ALTER TABLE incidents ADD COLUMN alerted_at TEXT;
UPDATE incidents SET alerted_at = ts WHERE alerted_at IS NULL;
ALTER TABLE shadow_outcomes ADD COLUMN trigger_ts TEXT;
ALTER TABLE shadow_outcomes ADD COLUMN sip_only_trigger INTEGER;
ALTER TABLE book_state ADD COLUMN streak_through TEXT;
CREATE INDEX ix_decisions_signal_book ON decisions(signal_id, book, id);
CREATE VIEW final_decisions AS
  SELECT d.* FROM decisions d
  WHERE d.id = (SELECT MAX(d2.id) FROM decisions d2
                WHERE d2.signal_id = d.signal_id AND d2.book = d.book);
