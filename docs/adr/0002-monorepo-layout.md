# 0002. Monorepo layout and packages

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** FR-55, FR-59, FR-60

## Context

trimset has a server, a worker, a stats library, an SDK (Python now, TypeScript in
v0.2), a CLI, test tools, demos, research experiments, docs and a site. One maintainer
needs one place to change a contract and see every side of it.

## Options

1. **One repo per part.** Clear boundaries, but every contract change is spread across
   repos.
2. **A monorepo as a uv workspace,** with separately published packages.

## Decision

A monorepo with a uv workspace:

```
server/            trimset-server: API, proxy, worker, migrations, server-side admin commands
stats/             trimset-stats: calibration, metrics, bounds, compiler, drift; pure functions, no I/O
sdk/python/        trimset: gate(), log(), outcome(), and the `trimset` CLI (an API client)
sdk/typescript/    v0.2
tools/mock_upstream/   deterministic /v1/systemone and /v1/decisions server (FR-59)
tools/datagen/         synthetic decisions and outcomes, six scenarios (FR-60)
dashboard/         v2
deploy/            Dockerfile, compose.yaml
examples/          one runnable demo per starred use case
research/          experiments with fixed seeds
docs/  site/
```

- **Published packages:** `trimset` (the SDK and CLI, which is what users install),
  `trimset-server` and `trimset-stats`. The tools are not published.
- **Two commands:**
  - `trimset` runs anywhere and talks to the API with a key (calibrate, simulate,
    approve, review).
  - `trimset-server` runs on the server and talks to the database (`migrate`, `serve`,
    `worker`, and creating the first admin key).

## Consequences

- `trimset-stats` has no I/O, so it is golden-tested in isolation and reused by
  `research/`.
- The SDK package stays free of dependencies apart from `rfc8785` (pure Python,
  Apache-2.0), which is needed for fingerprints and question ids
  ([0008](0008-policy-distribution.md), [0010](0010-question-id.md)).
