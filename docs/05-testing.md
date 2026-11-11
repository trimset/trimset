# Testing strategy

**Status:** in force from M1 (T1.2) · 2026-11-10

The brief places this document in Phase 6. It is written first instead, so that CI and
the coverage gate exist from the first line of product code.

## 1. Where tests run

- **CI is the source of truth.** [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)
  runs on every push and pull request.
  - A change is done when its run is green, never before.
- **Locally:** lint, types, and one test file at a time, for example
  `uv run --frozen pytest stats/tests/test_metrics.py`. The maintainer's machine is short
  on memory.
  - The full suite, integration tests, load tests and the experiments run only in CI, or
    on the load-test VM.

## 2. Kinds of test

| Kind | Tool | What it proves | Runs |
|---|---|---|---|
| Unit | pytest | One function or module behaves as specified | Every push |
| Property | Hypothesis | Invariants hold for any input (see below) | Every push |
| Golden | pytest, marker `golden`; scikit-learn and scipy as test-only references | Our statistics match reference implementations to a stated tolerance, and reproduce one published result per method ([research.md](research.md) §3) | Every push |
| Contract | `openapi-spec-validator`, `check-jsonschema`, `vectors/generate.py --check`, plus a fixture that validates every request and response in integration tests against `openapi.yaml` | The API, the payloads and the hashes match [03-api](03-api/README.md); the SDK sends what the server accepts | Every push |
| Integration | pytest, marker `integration`; Postgres 18 as a CI service container; the mock upstream (FR-59) | Server, worker and database together: ingest, proxy, joins, jobs | Every push, from T1.4 |
| Chaos | Integration tests that stop Postgres or the server mid-test | NFR-06 (the proxy keeps forwarding) and NFR-07 (the SDK never raises or blocks) | Every push, from T1.8 and T3.6 |
| Experiments | pytest, marker `slow`; the data generator (FR-60) | NFR-09 (guarantees hold in ≥ 95% of 200 seeds), SM-1, SM-3, SM-4 | Nightly, from T2.5 |
| Demos | `examples/*` scripts | Each milestone's end-to-end story still works | Nightly, from T1.15 |
| Load | k6 (`tools/load/`, workflow `load`), on a GitHub runner with Postgres, server and worker pinned to 2 CPUs and 4 GB, k6 and the mock upstream on the other CPUs | NFR-02, NFR-03, NFR-10 | Started by hand at the end of each milestone and before releases; never on a laptop. A rented 2 vCPU VM only when a result is close to its limit |

Property-test invariants, as examples of the kind:
- probabilities that are accepted sum to 1 after renormalising;
- every probability maps to exactly one action;
- the same inputs give the same `question_id` and policy fingerprint;
- a policy is never applied to another model or wording;
- the train/tune/eval split depends only on the `decision_id` and the seed.

## 3. Traceability

- **Marking tests:** every test that verifies a requirement carries
  `@pytest.mark.req("FR-12")`, or the NFR id.
- **Checking coverage of requirements:** from M2, a CI step lists the requirements with
  no test, so the matrix in [01-requirements.md](01-requirements.md) §13 can't silently
  rot.
- **Commits** name the requirement ids they touch.

## 4. Coverage gate

Branch coverage, enforced per package in CI:

| Package | Minimum | Why |
|---|---|---|
| `trimset-stats` | 95% | The numbers behind every guarantee |
| `trimset` (SDK) | 90% | Runs inside customers' apps; the fail-open paths must be tested |
| `trimset-server` | 85% | Lots of glue; integration tests cover the rest |
| `trimset-tools` | 90% | The mock upstream and data generator every other test leans on |

Lowering a minimum needs a sentence in the pull request saying why. Raising it doesn't.

## 5. Determinism

- **Seeds:** every random number comes from an explicit seed (NFR-19).
- **Hypothesis in CI:** runs a fixed profile (`derandomize=True`), so a failure
  reproduces exactly. Local runs may explore randomly.
- **Golden references** are computed by scipy or scikit-learn inside the test. Stored
  numbers are allowed only for published results, with the citation beside them.
- **Tolerances:**
  - 1e-9 for deterministic metrics;
  - 1e-6 on the objective for fitted parameters;
  - each statistical test names its own, in its docstring.

## 6. Flaky tests

A flaky test is a bug. Mark it `xfail(strict=True)` with a link to an issue on the day
it is found, and fix it within the milestone.

## 7. Supply chain for test tools

- **Version hold:** dev tools resolve only releases at least 7 days old
  (`exclude-newer` in `pyproject.toml`, `UV_EXCLUDE_NEWER` in CI). Both dates move
  together.
- **Pinned actions:** GitHub Actions are pinned to commit SHAs.
- **One-off tools:** `uvx` tools are pinned to exact versions.
