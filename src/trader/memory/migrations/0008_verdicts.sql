-- P4.7 (DECISIONS D-068): Book B's verdicts and its day mode.
-- verdicts.signal_id: the signal a vet verdict judged (the brief's come before the signals, so
--   theirs is NULL); eval_epoch: the epoch the verdict was made in.
-- day_modes.source: premarket (the brief's) or default (normal: the brief failed or never ran);
--   multiplier: the size multiplier the mode sets (S§5.1); eval_epoch.
ALTER TABLE verdicts ADD COLUMN signal_id TEXT REFERENCES signals(id);
ALTER TABLE verdicts ADD COLUMN eval_epoch INTEGER;
ALTER TABLE day_modes ADD COLUMN source TEXT;
ALTER TABLE day_modes ADD COLUMN multiplier TEXT;
ALTER TABLE day_modes ADD COLUMN eval_epoch INTEGER;
CREATE INDEX ix_verdicts_day ON verdicts(date, symbol);
