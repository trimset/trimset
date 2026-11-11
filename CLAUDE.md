# trimset

Open-source, self-hosted decision ops for decision models (Jev, Ollaya, Ollama, OpenAI
decisions). It logs every decision, joins it to the real outcome when that arrives,
refits calibration and thresholds, pushes approved policies to running apps, and alerts
on drift. The full brief is [docs/kickoff.md](docs/kickoff.md); it calls the project
"trueset", which was renamed in Phase 0.

## Current phase

**Phase 5, Implementation: M1 Capture in progress** ([docs/04-plan.md](docs/04-plan.md)).
- Phases 0 to 4 were approved on 2026-11-10.
- Done:
  - T1.1, the scaffold;
  - T1.2, CI and [docs/05-testing.md](docs/05-testing.md);
  - T1.3, the image, compose (Postgres and server), `/health`, `/ready`, JSON logs and
    OpenTelemetry;
  - T1.4, the migration runner, `0001_capture` (projects, questions, model_versions,
    and the monthly-partitioned decisions table), the worker (partition upkeep), and
    compose's worker service.
  - T1.5, keys:
    - `0002_keys`: SHA-256 only, four scopes, revocation refused everywhere within 30 s;
    - `/v1/admin/keys` with RFC 9457 errors;
    - `trimset-server create-project | create-key | revoke-key`.
  - T1.6, the wire module in the SDK package:
    - `trimset.hashing` reproduces every Phase 3 vector;
    - `trimset.wire` turns systemone and OpenAI traffic into record/1 and skips anything
      malformed, with a reason.
- Next: T1.7 (`POST /v1/decisions:log`), then T1.8 (proxy) and the rest of M1.
- Plan: v0.1.0-alpha around 2027-07-05 (M1–M3), v0.1.0 around 2027-10-18. Re-plan after
  every milestone, from actual hours.
- Repository: `github.com/trimset/trimset` (public).

## Decisions so far

- **Phase 0:** a Python (FastAPI) server; Postgres only; v0.1 ships the API and a CLI
  (dashboard in v2); Python SDK in v0.1, TypeScript in v0.2; GitHub org `trimset`;
  10 hours a week.
- **Phase 1:** the proxy supports the `/v1/systemone` format and OpenAI's `/v1/decisions`;
  outcomes can come from default-by-rule labels; audit sampling; a review link template
  instead of storing raw state; a policy is bound to its exact `model_served`; the proxy
  never blocks the app when the database is down; no telemetry. Requirement IDs
  (FR-xx, NFR-xx) are stable: cite them in ADRs, tests and commits.
- **Phase 2** (ADRs 0001–0013 in `docs/adr/`):
  - Runtime: Python 3.14, FastAPI, asyncpg with plain SQL, no ORM. One image runs two
    processes, `server` and `worker`, with jobs queued in Postgres 18. No Redis.
  - Policies: Learn-then-Test with exact binomial tests; pulled by the SDK with an ETag;
    SHA-256 fingerprints over RFC 8785 JSON.
  - Privacy: the state hash is a keyed HMAC.
  - SDK: when nothing is configured it falls back to ESCALATE, and never raises. It
    stays pure Python for 3.11 and newer, with no numpy or scipy.
- **Phase 3** (`docs/03-api/`):
  - Wire formats: two, `systemone` and `openai-decisions`, chosen by the upstream's
    format, not the path.
  - `question_id` hashes one canonical `question/1` form, whatever the format.
  - Keys: four scopes (`tsp_`, `tsi_`, `tsr_`, `tsa_`). In proxy mode, actions are
    reported through `POST /v1/decisions:actions`.
  - Upstreams and alert channels are operator-defined in the server environment
    (`TRIMSET_UPSTREAM_<NAME>_*`, `TRIMSET_ALERT_<NAME>_*`), and projects pick them by
    name. The API never carries URLs, headers or secret names (ADR 0011; two security
    reviews).
  - Probability sums: 1e-3 tolerance, then renormalised.
  - Vendor facts, read from the vendors' own docs: Ollama ≥ 0.35.0; only TypeSafe names
    the version that answered.
  - Every hash and id must reproduce `docs/03-api/vectors/vectors.json`. Change the
    vectors only through `generate.py`.

## Rules

- **Tests run in CI, which is the source of truth.** Locally, run only lint, types and
  one test file at a time: the maintainer's machine is short on memory. Never say a task
  is done before its CI run is green: `gh run list --repo trimset/trimset`.
- **Phase gates:** finish a phase, write its doc, stop for approval.
- **Ask, don't guess:** the user often types by voice. When input is unclear, ask short
  numbered questions.
- Everything is written under `docs/`. Diagrams are Mermaid inside Markdown, checked by
  rendering them with mermaid 11.4.1 in a browser before committing.
- Every major technical choice gets an ADR in `docs/adr/NNNN-title.md`.
- Every algorithm cites verified research (`docs/research.md`). Verify through the arXiv
  or Crossref API, never from memory; flag anything you can't verify.
- Independent implementation: never copy code from other projects, s1ops included.
  Reading their public docs is fine.
- Small Conventional Commits naming their task (T1.x) and requirement ids. No secrets in
  the repo, and nothing about the maintainer's employer.
- **Dependencies:**
  - Releases at least 7 days old only. `exclude-newer` in `pyproject.toml` and
    `UV_EXCLUDE_NEWER` in CI move together.
  - Actions are pinned to commit SHAs.
- Update this file at the end of every task that changes how the repo works.

## Homepage

`site/index.html`. Keep its look: dark theme, Space Grotesk / Inter / JetBrains Mono,
the `--accent` variable. Fill the `[X]%` cells only with numbers from our own demo data.
Before it goes live (GitHub Pages, task TA.3) its claims must match what is built. Today
nothing is built. It also promises a TypeScript SDK (v0.2), and its adoption snippet
lacks the `/p/<proxy key>` path that ADR 0011 requires.

## Layout

- `sdk/python/`: package `trimset` (SDK and CLI). Python 3.11 and newer, no dependencies
  yet.
- `stats/`: package `trimset-stats`. Pure functions, no I/O.
- `server/`: package `trimset-server` (server and worker).
- `docs/`:
  - the brief and one doc per phase;
  - `adr/`, the decision records;
  - `research.md`, the verified sources;
  - `03-api/`, the contracts;
  - `design/`, the prompts for Claude Design.
- `site/`: the homepage.
- `.github/workflows/ci.yml`: lint, types, tests per package with coverage gates, SDK on
  Python 3.11, contract checks, and an image and compose smoke test.
- `Dockerfile`: one image for server and worker.
  - Base images and uv are pinned by digest.
  - It runs as uid 10001.
  - Entrypoint `trimset-server`.
- `compose.yaml`: Postgres 18.6 and the server on :8800. It needs `TRIMSET_DB_PASSWORD` in
  `.env`; there is no default password.
- **Server logging and telemetry:**
  - Logs are JSON on stdout. `logs.redact` strips anything that looks like a trimset key
    from every log line.
  - OpenTelemetry is on only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, and span
    attributes pass through the same redaction.
- **HTTP client:** `httpx2`, not `httpx`, which Starlette deprecated.
- **Auth:** routes depend on `auth.require("<capability>")`, where the capability is
  `proxy`, `ingest`, `review` or `admin`. Scope → capability lives in
  `keys.CAPABILITIES`. Every query is scoped by `principal.project_id` (FR-51).
- **Shared logic:** wire formats and hashing live in the SDK (`trimset.wire`,
  `trimset.hashing`), and the server imports them. Never write a second copy.
  `docs/03-api/vectors/generate.py` stays as the independent reference.
- **Errors:** raise `errors.Problem(status, slug, title)`. Slugs are listed in
  `docs/03-api/README.md`.
- **Migrations:** `server/src/trimset_server/migrations/NNNN_name.sql`.
  - They only move forward. Never edit one that has been applied: the checksum guard
    refuses to run if you do.
  - Each task adds its own migration for the tables it needs.
  - With `TRIMSET_AUTO_MIGRATE=true` (as in compose), the server and the worker migrate
    at start under an advisory lock.
- **Database tests:** `server/tests/test_db_integration.py` needs
  `TRIMSET_TEST_DATABASE_URL`. CI provides Postgres 18.6 and sets it; locally the tests
  skip. Each test gets a fresh database.

## Commands

```bash
uv sync                                         # everything, into .venv
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest stats/tests/test_x.py    # one file at a time locally
gh run list --repo trimset/trimset              # the real test results
TRIMSET_DB_PASSWORD=x docker compose config --quiet   # check compose without starting it
# The image is built and smoke-tested in CI (job "image"), not locally.

# Contract checks (also in CI)
cd docs/03-api
uvx --from openapi-spec-validator==0.7.2 openapi-spec-validator openapi.yaml
uv run --no-project --with rfc8785==0.1.4 python vectors/generate.py --check
```
