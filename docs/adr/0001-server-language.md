# 0001. Server language and runtime

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** NFR-02, NFR-03, NFR-08, NFR-16

## Context

The core of trimset is statistics: calibration fits, exact binomial bounds and
permutation tests. The hot paths (ingestion and the proxy) have latency targets: p99
under 20 ms at 2,000 decisions/s, and under 5 ms of added proxy latency. There is one
maintainer, with 10 hours a week.

## Options

1. **Rust** (axum and sqlx). The fastest hot path, and a single binary. But the stats
   would be written by hand or run in a second Python service, and progress would be
   slow for one person.
2. **Go.** A fast hot path and simple deployment. The stats ecosystem is thin, so the
   same problem as Rust.
3. **Python** (FastAPI, asyncpg, numpy, scipy). The stats come ready-made and tested;
   golden tests compare directly with scipy and scikit-learn; fastest to build. The hot
   path is slower, and the GIL means one process per core.

## Decision

Python for the server and the worker:
- **Runtime:** Python 3.14, falling back to 3.13 if a dependency lacks 3.14 wheels.
- **Server:** FastAPI on uvicorn, asyncpg with plain SQL (no ORM), and httpx for upstream
  calls.
- **Packaging:** a uv workspace ([0002](0002-monorepo-layout.md)).

The Python SDK is pure Python, supports CPython 3.11 and newer, and does not depend on
numpy or scipy. Both need Python 3.12 or newer (scipy 1.18, numpy 2.5), and the SDK's
gate is only threshold arithmetic.

## Consequences

- The M1 load test decides whether NFR-02 and NFR-03 hold. If they don't, only the
  ingest and proxy hot path is rewritten in Rust, as a separate binary using the same
  database schema and wire contract. Nothing else changes.
- CPU-heavy statistics run in the worker's process pool, never on the server's event
  loop.
- The server runs one uvicorn process per core.
