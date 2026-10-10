-- Phase 2 execution state.
-- halts: one row per halt, so halts stack and clear one at a time (DECISIONS D-036).
--   scope: until_resume (cleared only by `trader resume`) | session (expires_at = next open)
--          | marker (a short-lived flag such as `flattening`, renewed while in use)
-- ledger_entries: at most one row per (book, ref), so a fill can never be booked twice (D-034).
-- orders.group_id: Alpaca's id shared by an OTO entry and its legs; the stream doesn't nest legs,
--   so this is how a leg finds its entry (D-030).
CREATE TABLE halts (id INTEGER PRIMARY KEY, book TEXT NOT NULL, reason TEXT NOT NULL,
  scope TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL, expires_at TEXT,
  cleared_at TEXT, cleared_by TEXT);
CREATE INDEX ix_halts_book ON halts(book, cleared_at);
CREATE UNIQUE INDEX ux_ledger_entries_ref ON ledger_entries(book, ref) WHERE ref IS NOT NULL;
ALTER TABLE orders ADD COLUMN group_id TEXT;
CREATE INDEX ix_orders_book_symbol ON orders(book, symbol);
