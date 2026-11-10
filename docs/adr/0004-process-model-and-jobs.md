# 0004. Process model and background jobs

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** FR-11, FR-13, FR-30, FR-38 to FR-46, FR-56, NFR-01, NFR-17

## Context

The brief lists these containers:
- API/ingest service and proxy
- outcome joiner
- stats/refit worker and scheduler
- policy service
- alerting
- dashboard
- Postgres

Self-hosting must be one `docker compose up` on a small VM (NFR-01), using under 1 GB of
RAM at idle (NFR-17).

## Options

1. **One container per part.** About seven services and more network hops, for no gain
   at v0.1 volumes.
2. **One image, two processes** (`server` and `worker`), with background jobs queued in
   Postgres.
3. **Two processes plus Redis or a message broker for jobs.** One more service to run,
   back up and secure.

## Decision

One image, two processes. The brief's parts are modules inside them:

| Brief's container | Runs in |
|---|---|
| API/ingest service, proxy, policy service, review API | `server` |
| Outcome joiner (write path) | `server` (it matches at write time) |
| Outcome joiner (sweeps), stats/refit worker, scheduler, alerting, shadow reports, retention | `worker` |
| Dashboard | v2: static files served by `server` under `/ui` |
| Object storage | Not needed in v0.1. Fit artifacts are small and live in Postgres; exports stream. |

**How jobs run:**
- Jobs are rows in a `jobs` table, claimed with `SELECT … FOR UPDATE SKIP LOCKED`.
- Retries use backoff, and the last error is kept on the row.
- Schedules live in `schedules`. The worker turns due schedules into jobs.
- Every job is idempotent, so running two workers is safe.
- A one-shot `migrate` service runs first in compose.

## Consequences

- No Redis, no broker and no Kafka. The only stateful service is Postgres.
- The server is stateless apart from its in-memory record buffer, so it can run behind a
  load balancer.
- Job throughput is bounded by Postgres. It is fine for thousands of jobs an hour, which
  is all v0.1 needs.
