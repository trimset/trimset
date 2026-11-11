-- 0002: API keys (FR-52). Only a SHA-256 of each key is stored: keys carry about 190 bits
-- of randomness, so a fast hash can't be reversed by guessing.

CREATE TABLE api_keys (
    key_id       uuid PRIMARY KEY,
    project_id   uuid NOT NULL REFERENCES projects ON DELETE CASCADE,
    scope        text NOT NULL CHECK (scope IN ('proxy', 'ingest', 'review', 'admin')),
    label        text NOT NULL CHECK (length(label) BETWEEN 1 AND 100),
    prefix       text NOT NULL CHECK (length(prefix) = 8),
    key_hash     bytea NOT NULL UNIQUE CHECK (length(key_hash) = 32),
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);
CREATE INDEX api_keys_project ON api_keys (project_id);
