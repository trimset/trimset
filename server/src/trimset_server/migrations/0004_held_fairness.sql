-- 0004: held outcomes the worker tried and still couldn't place go to the back of the
-- line, so rows that never match can't keep the rest waiting (T1.9 security review).
ALTER TABLE unmatched_outcomes ADD COLUMN tried_at timestamptz;
CREATE INDEX unmatched_outcomes_next ON unmatched_outcomes (tried_at NULLS FIRST, id);
