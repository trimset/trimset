-- 0001: the capture schema (FR-01, FR-03, FR-39; ADR 0003). Migrations only move forward.
-- Keys (T1.5), outcomes (T1.9) and raw state (opt-in) arrive in their own migrations.

CREATE TABLE projects (
    project_id  uuid PRIMARY KEY,
    name        text NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 100),
    settings    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- One row per question wording (ADR 0010). A name points to the latest id; the id it
-- replaced keeps its history and is linked through previous_question_id.
CREATE TABLE questions (
    project_id           uuid NOT NULL REFERENCES projects ON DELETE CASCADE,
    question_id          text NOT NULL CHECK (question_id ~ '^q_[0-9a-f]{32}$'),
    name                 text CHECK (length(name) BETWEEN 1 AND 128),
    type                 text NOT NULL CHECK (type IN ('noul', 'choice', 'score')),
    definition           jsonb NOT NULL,
    settings             jsonb NOT NULL DEFAULT '{}'::jsonb,
    previous_question_id text,
    first_seen           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, question_id)
);
CREATE UNIQUE INDEX questions_name_key ON questions (project_id, name) WHERE name IS NOT NULL;

-- Every (requested, served) pair seen. A new served version for a known requested model
-- is a silent upgrade (FR-39).
CREATE TABLE model_versions (
    project_id      uuid NOT NULL REFERENCES projects ON DELETE CASCADE,
    model_requested text NOT NULL,
    model_served    text NOT NULL,
    first_seen      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, model_requested, model_served)
);

-- One row per answered question (record/1). Partitioned by month on ts; partitions.py
-- creates them ahead of time. No foreign key to projects: deleting a project's decisions
-- is a batched job (FR-57), not a cascade over millions of rows.
CREATE TABLE decisions (
    project_id         uuid             NOT NULL,
    ts                 timestamptz      NOT NULL,
    decision_id        uuid             NOT NULL,
    request_id         uuid,
    question_id        text             NOT NULL,
    type               text             NOT NULL CHECK (type IN ('noul', 'choice', 'score')),
    format             text             NOT NULL
                       CHECK (format IN ('systemone', 'openai-decisions', 'direct')),
    model_requested    text             NOT NULL,
    model_served       text             NOT NULL,
    probs              real[],
    refusal            boolean          NOT NULL DEFAULT false,
    latency_ms         real             CHECK (latency_ms >= 0),
    cost_usd           double precision CHECK (cost_usd >= 0),
    policy_version     integer          CHECK (policy_version >= 1),
    policy_fingerprint text,
    action_taken       text,
    action_source      text             CHECK (action_source IN ('policy', 'audit', 'default')),
    audit              boolean          NOT NULL DEFAULT false,
    tags               jsonb            NOT NULL DEFAULT '{}'::jsonb,
    external_ref       text,
    state_hash         text             CHECK (state_hash ~ '^[0-9a-f]{64}$'),
    role               text             NOT NULL DEFAULT 'primary'
                       CHECK (role IN ('primary', 'shadow')),
    shadow_of          uuid,
    received_at        timestamptz      NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, ts, decision_id),
    CHECK (refusal = (probs IS NULL)),
    CHECK (probs IS NULL OR cardinality(probs) BETWEEN 1 AND 255),
    CHECK (probs IS NULL OR (0 <= ALL (probs) AND 1 >= ALL (probs))),
    CHECK ((role = 'shadow') = (shadow_of IS NOT NULL))
) PARTITION BY RANGE (ts);

CREATE INDEX decisions_by_id ON decisions (project_id, decision_id);
CREATE INDEX decisions_by_ref ON decisions (project_id, external_ref, question_id)
    WHERE external_ref IS NOT NULL;
CREATE INDEX decisions_by_group ON decisions (project_id, question_id, model_served, ts);
