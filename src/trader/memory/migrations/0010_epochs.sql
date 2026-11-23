-- P5.1 (DECISIONS D-075): the epoch record, and why a lesson stands where it does.
-- epochs: one row per evaluation epoch (STRATEGY.md §9): when it began, why, the DECISIONS entry
--   behind it, the review note that led to it, if any, and the hashes of the setup it froze.
--   Only the human starts an epoch (`trader epoch start`). Epoch 0, the build and its shakedown,
--   gets its row here.
-- lessons.status_reason: why a lesson is where it is: an invalid query, a failed test (P5.2).
CREATE TABLE epochs (epoch INTEGER PRIMARY KEY, started_at TEXT, reason TEXT NOT NULL,
  decision TEXT, note_id INTEGER REFERENCES review_notes(id), settings_sha256 TEXT,
  risk_sha256 TEXT, events_sha256 TEXT, llm_fingerprint TEXT);
INSERT INTO epochs (epoch, reason, decision)
  VALUES (0, 'the build and its shakedown, before P7.1''s freeze', 'D-075');
ALTER TABLE lessons ADD COLUMN status_reason TEXT;
