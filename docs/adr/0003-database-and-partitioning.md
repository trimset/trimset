# 0003. Database, schema and partitioning

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** FR-01, FR-56, FR-57, NFR-02, NFR-10, NFR-15

## Context

Decisions arrive at up to 2,000 per second, at peak. They are kept 400 days by default,
deleted on request, and scanned per group for fits and drift checks. Phase 0 chose
Postgres only.

## Options

1. **Postgres with declarative partitioning,** no extensions.
2. **Postgres with TimescaleDB.** Automatic partitions, but an extension that some
   managed Postgres services don't offer, with its own licence terms.
3. **ClickHouse for decisions plus Postgres for the rest.** Strong analytics, but two
   databases for one maintainer and every self-hoster.

## Decision

**Postgres 18,** with no extensions, so any managed Postgres 18 works.

**Tables:**
- `decisions` and `decision_states` (the opt-in raw state) are range-partitioned by month
  on `ts`.
- The worker creates partitions three months ahead.
- A decision whose `ts` is outside [now − retention, now + 5 minutes] is rejected.
  There is no default partition.

**Keys and indexes:**
- The primary key is `(project_id, ts, decision_id)`. The partition key has to be part
  of it; idempotent retries resend the same `ts`.
- Indexes on `decisions`, one per partition:
  - `(project_id, decision_id)`: outcome joins
  - `(project_id, external_ref, question_id)` where `external_ref` is set: outcome joins
  - `(project_id, question_id, model_served, ts)`: fits and drift

**Ids and types:**
- The proxy and the SDK generate `decision_id` as a UUIDv7, which sorts by time. Callers
  of the log API may send any UUID.
- Probabilities are `real[]`. Tags and settings are `jsonb`.

**Writes:**
- Ingestion is one `INSERT … SELECT FROM unnest(...) ON CONFLICT DO NOTHING` per batch,
  committed before the reply (NFR-02: an acknowledged record is durable).

**Migrations:**
- Plain SQL files in `server/migrations/NNNN_name.sql`. They only move forward.
- A small runner applies them under an advisory lock and records them in
  `schema_migrations`.
- No ORM and no Alembic.

**Retention and deletion:**
- Whole partitions are dropped at the longest retention any project uses.
- Projects with shorter retention, and deletion requests, use batched `DELETE` jobs.

## Consequences

- If NFR-02 misses in the M1 load test, the first fix is to batch several requests into
  one insert inside the server, before trying any other option.
- Without a global unique index, uniqueness of `decision_id` relies on it being paired
  with the same `ts` on retry. Phase 3 documents this.
- ClickHouse stays a later option for over about 1 billion decisions, fed by logical
  replication, without changing the API.
