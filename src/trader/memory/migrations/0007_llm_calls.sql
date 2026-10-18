-- P4.4 (DECISIONS D-064): one llm_calls row per attempt, the first try or the one repair.
-- logical_id: the call the attempt belongs to (YYYYMMDD-role-NN, or YYYYMMDD-smoke-role-NN);
--   attempt: 1 or 2; final: its result is the call's.
-- ok / failure: how the attempt ended (failure is a code: timeout, nonzero_exit, is_error,
--   schema_invalid, wrong_pack_id, abandoned, ...); subtype, session_id: the CLI's envelope.
-- purpose: trading (counts toward max_calls_per_day) or smoke (never counts).
-- deadline: the call's own; pack_*: what went on stdin; candidates_json: the pack's kept
--   candidates or targets, the answer's allowed members; clamped: cuts made before validation.
ALTER TABLE llm_calls ADD COLUMN logical_id TEXT;
ALTER TABLE llm_calls ADD COLUMN attempt INTEGER;
ALTER TABLE llm_calls ADD COLUMN final INTEGER;
ALTER TABLE llm_calls ADD COLUMN ok INTEGER;
ALTER TABLE llm_calls ADD COLUMN failure TEXT;
ALTER TABLE llm_calls ADD COLUMN subtype TEXT;
ALTER TABLE llm_calls ADD COLUMN session_id TEXT;
ALTER TABLE llm_calls ADD COLUMN timed_out INTEGER;
ALTER TABLE llm_calls ADD COLUMN effort TEXT;
ALTER TABLE llm_calls ADD COLUMN purpose TEXT;
ALTER TABLE llm_calls ADD COLUMN deadline TEXT;
ALTER TABLE llm_calls ADD COLUMN pack_id TEXT;
ALTER TABLE llm_calls ADD COLUMN pack_sha256 TEXT;
ALTER TABLE llm_calls ADD COLUMN pack_bytes INTEGER;
ALTER TABLE llm_calls ADD COLUMN candidates_json TEXT;
ALTER TABLE llm_calls ADD COLUMN cli_version TEXT;
ALTER TABLE llm_calls ADD COLUMN schema_mode TEXT;
ALTER TABLE llm_calls ADD COLUMN clamped INTEGER;
ALTER TABLE llm_calls ADD COLUMN eval_epoch INTEGER;
CREATE INDEX ix_llm_calls_day ON llm_calls(date, purpose, role);
CREATE UNIQUE INDEX ux_llm_calls_attempt ON llm_calls(logical_id, attempt);
