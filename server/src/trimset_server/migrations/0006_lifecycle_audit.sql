-- The policy lifecycle (FR-27 to FR-29) and the audit log (FR-54), T3.1.

-- Every state change of a policy, its creation as a draft included (from_state NULL).
CREATE TABLE policy_events (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id  uuid        NOT NULL REFERENCES projects ON DELETE CASCADE,
    policy_id   uuid        NOT NULL REFERENCES policies ON DELETE CASCADE,
    from_state  text,
    to_state    text        NOT NULL,
    key_id      uuid,       -- NULL: the server itself, e.g. a scheduled refit
    reason      text,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX policy_events_by_policy ON policy_events (policy_id, id);

-- Policies compiled before this migration get their creation event.
INSERT INTO policy_events (project_id, policy_id, from_state, to_state, at)
SELECT project_id, policy_id, NULL, state, created_at FROM policies;

-- FR-29: at most one active policy per question and served model.
CREATE UNIQUE INDEX policies_one_active ON policies (project_id, question_id, model_served)
    WHERE state = 'active';

-- FR-54: who changed what, with the values before and after. Written in the same
-- transaction as the change.
CREATE TABLE audit_log (
    id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project_id  uuid        NOT NULL REFERENCES projects ON DELETE CASCADE,
    key_id      uuid,       -- NULL: a command run on the server (trimset-server create-key)
    action      text        NOT NULL,
    target      text        NOT NULL,
    before      jsonb,
    after       jsonb,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_by_project ON audit_log (project_id, id DESC);
