# 0015. The proxy's hot path is Rust; everything else stays Python

**Status:** accepted (Phase 5, T1.13, 2026-11-11) · **Requirements:** NFR-02, NFR-03,
NFR-06 · **Follows:** [ADR 0001](0001-server-language.md), [ADR 0011](0011-proxy-design.md)

## Context

ADR 0001 chose Python and kept a way out: if the M1 load test missed NFR-02 or NFR-03,
the hot path would become a Rust binary using the same database schema and wire contract.

T1.13 ran that test (workflow `load`, `tools/load/`) on a GitHub runner, with trimset
(Postgres, server, worker) pinned to two CPUs and 4 GB, standing in for the 2 vCPU / 4 GB
VM. k6 and the mock upstream ran on the other CPUs, and each requirement got a fresh
stack. With the Python proxy and two server processes:

| Test | Target | Result |
|---|---|---|
| Ingest, 10 min at 2,022 decisions/s | p99 < 20 ms | 15.7 and 17.2 ms |
| Proxy at 300 requests/s (one process) | overhead < 5 ms at p99 | 1.8 ms |
| Proxy at 400 requests/s | " | 67 ms |
| Proxy at 500 requests/s | " | 341 ms; the server used about a whole CPU, often both |

The proxy's code was fast enough below saturation, but each request cost 1.5–1.9 ms of
CPU. Two CPUs shared with Postgres top out between 300 and 400 requests a second.

## Options

1. **Lower NFR-03** to 300 requests a second.
2. **Speed up the Python proxy:** a faster HTTP client, less work per record. Maybe
   500, not certain.
3. **The way out ADR 0001 kept:** the proxy in Rust.

## Decision

**Option 3, kept only because it measured fast.** `trimset-proxy` (`proxy/`, Rust 1.99.1,
hyper, rustls with ring) is the front door on :8800:
- It serves the proxy routes itself, with the key in a header or the path, and passes
  every other request to the Python server (on :8810, inside the compose network)
  unchanged. It refuses `/internal/*` from outside.
- **It carries bytes and interprets nothing.**
  - It asks the server who a key is and caches the answer for 30 s. During an outage, an
    answer up to 10 minutes old stands (NFR-06).
  - It forwards with ADR 0011's header rules and never follows redirects.
  - It gives each question a decision id by position.
  - It hands each exchange to the server in batches, holding them while the server is
    away (bounded, dropping the oldest).
- **Parsing, hashing and storage stay in Python** (`capture.py`, through `internal.py`),
  next to the SDK's wire module: one copy of the wire logic, not two. The internal
  endpoints take a token derived from `TRIMSET_SECRET` (HMAC-SHA-256 of a fixed context).
- **A question trimset can't read still gets an id.** The proxy doesn't parse answers,
  so that id simply never gets a record. Before, its slot in `X-Trimset-Decision-Id` was
  empty. The documented contract ("one id per question") still holds.

Same test, with the Rust proxy:

| Test | Target | Result |
|---|---|---|
| Proxy at 500 requests/s | overhead < 5 ms at p99 | ≈ 0 ms (p99 4.28 ms proxied, 4.40 ms direct; 0.8 ms at p95) |
| CPU at 500 requests/s | | proxy 0.15 of a CPU, server 0.24 (recording in batches) |

NFR-03 stays at 500 requests a second.

## Consequences

- The image holds three processes, `trimset-proxy`, `trimset-server serve` and
  `trimset-server worker`; compose runs each as a service. Ports, URLs and the API don't
  change for users.
- A second language, kept small: one file of about 900 lines, most of it tests. Its CI
  job runs rustfmt, clippy with warnings as errors, and its tests, including end-to-end
  ones against a fake upstream and a fake server.
- **Supply chain:** crates are pinned exactly, and `tools/crate_ages.py` checks the 7-day
  hold across `Cargo.lock`. The Rust build image is pinned by digest.
- **Ingest is at the edge of NFR-02 on GitHub's runners, proxy or not.** Over today's
  runs its p99 ranged from 15.7 to 27 ms (p50 5–7 ms), and one run measured it both ways:
  27.0 ms straight to the server, 25.0 ms through the proxy. The runners vary more than
  trimset does, so a rented 2 vCPU / 4 GB VM decides NFR-02 before the alpha
  (docs/05-testing.md). The load workflow's `ingest-direct` job keeps the comparison.
- **Nothing else moves to Rust.** Ingest passes in Python at a quarter of a CPU, and the
  statistics are numpy and scipy, already compiled code, golden-tested against the
  reference implementations. If a profile ever shows one hot function, it can move on its
  own, through PyO3.
