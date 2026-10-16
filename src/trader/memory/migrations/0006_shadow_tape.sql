-- P4.0c (DECISIONS D-057): how each shadow outcome's trigger minute was judged.
-- bars: the 1-minute bar settled it; tape: an ambiguous minute read from the SIP trade tape;
-- fallback: an ambiguous minute with no usable tape, counted as stopped (D-041's pessimism).
ALTER TABLE shadow_outcomes ADD COLUMN trigger_resolution TEXT;
