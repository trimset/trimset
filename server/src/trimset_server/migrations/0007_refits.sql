-- Scheduled refits that propose (FR-30), T3.3.

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('fit', 'compile', 'refit'));

-- The schedule's minute a refit job is for: one per project and minute, so two workers
-- can't both queue it.
ALTER TABLE jobs ADD COLUMN scheduled_for timestamptz;
CREATE UNIQUE INDEX jobs_one_refit_per_tick ON jobs (project_id, scheduled_for)
    WHERE kind = 'refit';

-- The share of a fit's tuning labels from each source and key, shown in diffs so an
-- approver can spot poisoned outcomes (threat T1).
ALTER TABLE fits ADD COLUMN label_sources jsonb;
