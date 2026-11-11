-- 0005: background jobs, calibration fits and compiled policies (T2.6, ADR 0004, ADR 0006).

-- On-demand work for the worker, claimed with FOR UPDATE SKIP LOCKED (ADR 0004).
CREATE TABLE jobs (
    job_id      bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id  uuid        NOT NULL REFERENCES projects ON DELETE CASCADE,
    kind        text        NOT NULL CHECK (kind IN ('fit', 'compile')),
    status      text        NOT NULL DEFAULT 'queued'
                            CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    payload     jsonb       NOT NULL,
    result      jsonb,
    error       text,
    attempts    int         NOT NULL DEFAULT 0,
    run_at      timestamptz NOT NULL DEFAULT now(),
    key_id      uuid,  -- who asked
    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,
    finished_at timestamptz
);
CREATE INDEX jobs_due ON jobs (run_at) WHERE status = 'queued';

-- A calibration map for one group (fit/1), with the tune and eval splits it was fitted
-- beside, kept as arrays without ids so compiles and simulations need not reread them.
CREATE TABLE fits (
    fit_id       uuid        PRIMARY KEY,
    project_id   uuid        NOT NULL REFERENCES projects ON DELETE CASCADE,
    question_id  text        NOT NULL,
    model_served text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    body         jsonb       NOT NULL,  -- fit/1
    splits       bytea       NOT NULL   -- numpy .npz: tune and eval probs, labels, audit
);
CREATE INDEX fits_by_group ON fits (project_id, question_id, model_served, created_at DESC);

-- Compiled policies (policy/1). The body never changes; the state does (M3, FR-27).
CREATE TABLE policies (
    policy_id    uuid        PRIMARY KEY,
    project_id   uuid        NOT NULL REFERENCES projects ON DELETE CASCADE,
    question_id  text        NOT NULL,
    model_served text        NOT NULL,
    version      int         NOT NULL CHECK (version >= 1),
    state        text        NOT NULL DEFAULT 'draft'
                             CHECK (state IN ('draft', 'proposed', 'approved', 'active',
                                              'retired', 'rejected')),
    fit_id       uuid        NOT NULL REFERENCES fits,
    body         jsonb       NOT NULL,  -- policy/1
    fingerprint  text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, question_id, model_served, version)
);
