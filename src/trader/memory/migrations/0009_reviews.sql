-- P4.10 (DECISIONS D-071): the post-close review's content and its follow-ups.
-- daily_reviews.failure: why a review is missing or invalid; targets_json: the signal ids the
--   review was asked about, in the pack's order.
-- review_mentions: each time a review raised a process issue or a note, new or linked, with that
--   day's own text; the issue or note keeps its first text.
-- lessons.source_date: the review day that proposed the lesson.
-- shadow_outcomes.spy_move_pct: SPY's move over the shadow trade's window (its trigger minute's
--   open to its exit minute's close), on review days only.
ALTER TABLE daily_reviews ADD COLUMN failure TEXT;
ALTER TABLE daily_reviews ADD COLUMN targets_json TEXT;
CREATE TABLE review_mentions (id INTEGER PRIMARY KEY, date TEXT NOT NULL, kind TEXT NOT NULL,
  item_id INTEGER NOT NULL, severity TEXT, component TEXT, text TEXT,
  llm_call_id TEXT REFERENCES llm_calls(id));  -- kind: issue | note
CREATE INDEX ix_review_mentions_item ON review_mentions(kind, item_id);
CREATE INDEX ix_process_issues_status ON process_issues(status);
CREATE INDEX ix_review_notes_status ON review_notes(status);
ALTER TABLE lessons ADD COLUMN source_date TEXT;
ALTER TABLE shadow_outcomes ADD COLUMN spy_move_pct REAL;
