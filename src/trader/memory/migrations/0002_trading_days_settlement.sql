-- 0002: keep Alpaca's settlement_date for each session (DECISIONS D-022).
-- Settlement skips bank holidays on which NYSE trades (Columbus Day, Veterans Day), so it is not
-- always the next trading day.
ALTER TABLE trading_days ADD COLUMN settlement_date TEXT;
