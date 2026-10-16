-- Phase 4, P4.0a (DECISIONS D-055): fees as the broker charges them.
-- Each later Phase 4 part brings its own migration, so no column is fixed before its code exists.
-- daily_stats.cash_residual: broker cash minus ledger cash at the EOD check, which leaves out
--   today's fee estimates (Alpaca charges a day's fees after its close). It shows any drift long
--   before the reconcile tolerance halts the book.
ALTER TABLE daily_stats ADD COLUMN cash_residual TEXT;
