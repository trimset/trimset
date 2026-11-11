-- 0003: outcomes, their history, what waits for its decision, and action reasons
-- (FR-10 to FR-13, FR-33). Labels are JSON: a boolean (noul), an option's value (choice:
-- string or boolean), or a level index (score).

-- The current label of each decision: the last one received wins (FR-12).
CREATE TABLE outcomes (
    project_id  uuid        NOT NULL REFERENCES projects ON DELETE CASCADE,
    decision_id uuid        NOT NULL,
    question_id text        NOT NULL,
    label       jsonb       NOT NULL,
    source      text        NOT NULL CHECK (source IN ('human_review', 'downstream', 'rule')),
    observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    key_id      uuid,  -- the key that sent it; null for rule labels (threat T1)
    PRIMARY KEY (project_id, decision_id)
);

-- Every label ever received, including corrections (FR-12).
CREATE TABLE outcome_history (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id  uuid        NOT NULL REFERENCES projects ON DELETE CASCADE,
    decision_id uuid        NOT NULL,
    outcome_id  uuid,
    label       jsonb       NOT NULL,
    source      text        NOT NULL,
    observed_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    key_id      uuid
);
CREATE INDEX outcome_history_by_decision ON outcome_history (project_id, decision_id);
CREATE UNIQUE INDEX outcome_history_outcome_id ON outcome_history (project_id, outcome_id, decision_id)
    WHERE outcome_id IS NOT NULL;

-- Outcomes that arrived before their decision, held until it does or until they expire
-- (FR-11).
CREATE TABLE unmatched_outcomes (
    id           bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id   uuid        NOT NULL REFERENCES projects ON DELETE CASCADE,
    outcome_id   uuid,
    decision_id  uuid,
    external_ref text,
    question     text,
    label        jsonb       NOT NULL,
    source       text        NOT NULL,
    observed_at  timestamptz NOT NULL,
    received_at  timestamptz NOT NULL,
    key_id       uuid,
    expires_at   timestamptz NOT NULL,
    CHECK ((decision_id IS NULL) <> (external_ref IS NULL)),
    CHECK (external_ref IS NULL OR question IS NOT NULL)
);
CREATE INDEX unmatched_outcomes_due ON unmatched_outcomes (project_id, id);
CREATE UNIQUE INDEX unmatched_outcomes_outcome_id ON unmatched_outcomes (project_id, outcome_id)
    WHERE outcome_id IS NOT NULL;

-- Action reports that arrived before their decision was written (FR-33).
CREATE TABLE pending_actions (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id  uuid        NOT NULL REFERENCES projects ON DELETE CASCADE,
    decision_id uuid        NOT NULL,
    report      jsonb       NOT NULL,
    received_at timestamptz NOT NULL,
    expires_at  timestamptz NOT NULL
);
CREATE INDEX pending_actions_due ON pending_actions (project_id, id);

ALTER TABLE decisions ADD COLUMN action_reason text;
