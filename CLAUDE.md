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
  - T1.7, `POST /v1/decisions:log` (`ingest.py`, `records.py`):
    - strict per-record checks, with 207 and errors by index;
    - question registration, with names moving to the newest wording;
    - model versions recorded;
    - one unnest insert that ignores duplicates.
  - T1.8, the proxy:
    - `proxy.py`, `recorder.py`, `store.py`, `projects.py` (`GET/PATCH /v1/admin/project`)
      and `upstreams.py`;
    - forwards unchanged, never follows redirects, never sends a trimset key upstream;
    - errors in the upstream's format;
    - keys and project settings stay stale while the database is down (NFR-06).
  - T1.9, outcomes and actions (`0003_outcomes`, `outcomes.py`, `actions.py`,
    `questions.py`):
    - `POST /v1/outcomes`, matched by decision id, or by external_ref and a question name
      that covers its earlier wordings;
    - early outcomes held 30 days and early action reports one hour, joined by the worker
      every minute;
    - the last label received wins (enforced in SQL), and `outcome_history` keeps every one;
    - default labels by rule, hourly; `GET/PATCH /v1/admin/questions/{question}`.
  - T1.10, the mock upstream (`tools/`, `compose.mock.yaml`): both formats,
    deterministic answers, a version switch for silent upgrades, latency and error
    injection. CI sends a request through the image's proxy to it.
  - T1.11, the data generator (`tools/.../datagen.py`, `trimset-datagen`): all six use
    cases with their outcome channels; planted miscalibration, drift, a silent model
    swap, missing labels and slower delays; JSONL (with ground truth) or through the API.
  - T1.12, contract tests (`server/tests/test_contract_integration.py`): every route is in
    `openapi.yaml`, and real responses match their documented status and schema. A new
    endpoint gets a `check(...)` call there.
  - T1.13, the load test (`tools/load/`, workflow `load`, started by hand). The proxy's
    hot path is now Rust (ADR 0015): ~1 ms added at p99 at 500 req/s. Ingest's p99 swings
    16–27 ms on GitHub runners with or without the proxy; a rented 2 vCPU VM decides
    NFR-02 before the alpha (needs the user's OK, a few dollars).
  - T1.15, the demo `examples/m1-capture/run.sh`: refund fraud through the proxy, a
    silent upgrade, generated history, held and default-rule labels. CI runs it.
- Waiting: T1.14 (real successful calls) needs the user's TypeSafe and OpenAI keys; then
  the M1 re-plan from actual hours.
- **M2 Calibrate and compile, in progress** (2026-11-11):
  - T2.1, metrics (`stats/.../metrics.py`): log loss, Brier, ECE with equal-width and
    equal-mass bins, reliability bins with Clopper-Pearson intervals, all on one event's
    (p, y); golden-tested against scikit-learn and scipy. scikit-learn is test-only.
  - T2.2, calibrators (`stats/.../calibration.py`): temperature, Platt and isotonic for
    one event, temperature for distributions, and `choose` by 5-fold log loss with a
    seed. Every map is non-decreasing (a reversed Platt fit is refused), so thresholds
    carry back to raw p. Golden against scikit-learn; Guo 2017 and Niculescu-Mizil 2005
    reproduced.
  - T2.3, datasets: `stats/.../dataset.py` (window, maturity, latest per external_ref,
    40/40/20 split by hash of seed and decision id, minimum and shortfall, coverage per
    action path) and `server/.../datasets.py` (loads one group, skips refusals and
    shadows, renormalises probabilities, labels to class indices). Unlabeled matured
    items stay in the splits with label -1, with their audit flags, for the compiler.
  - T2.4, the compiler (`stats/.../compiler.py`): rules per type (FR-20), Learn-then-Test
    with exact binomial p-values, the walk starting at n_start (where alpha/2 errors would
    still pass), false_pass
    (accept rule certified, block rule within the escalation cap), error_budget (every
    rule, delta split), cost (grid, no guarantee), unlabeled fired items as wrong,
    audited_only paths, Declined with the labels needed or the binding constraint, and
    `simulate` for FR-26. Calibration maps follow sdk.md §3 exactly.
  - T2.5, the coverage experiment (`research/test_coverage.py`, marker `slow`, workflow
    `nightly`): 200 seeds of refund fraud, triage and routing; each certified rule's
    true error on fresh traffic (exact, from the generator's q) must hold in a share of
    seeds not credibly below 95% (exact binomial at 1%; NFR-09).
    Slow tests are left out of every push (`-m "not slow"` in pytest's addopts).
  - T2.6, fits and compiles as jobs (`jobs.py`: a `jobs` table claimed with SKIP LOCKED,
    3 tries with backoff, run by the worker every second; `fitting.py`; `analysis.py`
    for the routes): a fit stores fit/1 and the tune/eval arrays (no ids); a compile
    stores a draft policy/1 with its fingerprint, or reports not_enough_labels or
    infeasible; the simulator and label coverage answer at once. Question settings take
    `actions` and `audit_fraction`. Tests: the `check` and `conforms` fixtures
    (conftest.py) validate any response or body against the contract.
  - T2.7, the CLI (`sdk/python/src/trimset/cli.py`, stdlib only, `trimset` on PATH):
    config (0600 file; TRIMSET_URL/TRIMSET_KEY win), calibrate, simulate, policy
    compile/show/diff, coverage, decisions tail; tables or `--json`. The server gained
    read views: GET /v1/admin/policies, /policies/{id}, /policies/{id}/diff and
    /v1/admin/decisions. Export (FR-58) stays in M5. It refuses redirects (they'd carry
    the key) and escapes control characters in everything it prints.
  - T2.8, the M2 demo (`examples/m2-refund-fraud/run.sh`, run in CI's image job): seven
    months of a miscalibrated refund model, then coverage, calibrate, simulate and a
    certified compile, all through the CLI inside the server container. M2 is done.
  - T3.1, the policy lifecycle (`policies.py`, migration 0006): submit, approve, reject,
    activate (retires the group's active policy), retire, and rollback (re-activates the
    version active before), under a lock per question and model, with a unique index for
    one active policy. Each change is a `policy_events` row and an `audit_log` entry
    (`audit.py`, GET /v1/admin/audit-log), as are keys created and revoked (API and
    server CLI, key_id null) and project and question settings changes. CLI: `trimset
    policy submit|approve|reject|activate|retire|rollback`; `policy show` prints history.
  - T3.2, the bundle (`bundle.py`, GET /v1/policies, ingest keys): named questions,
    active policies with guarantee status, the state-hash key (503 without
    TRIMSET_SECRET); ETag per hashing.md §5, 304 on If-None-Match.
  - T3.3, scheduled refits (`refits.py`, `cron.py`, migration 0007): the worker queues a
    project's `refit` job each minute its `refit_schedule` (5-field cron, UTC) falls due,
    once (unique per project and minute), catching up at most a day. The job refits
    every group with an active policy, with its target, and stores a `proposed` policy
    (key null, reason "scheduled refit") unless no threshold moved by 0.005 or more;
    a new proposal rejects the refits' older one. Never activates. Fits store their
    tuning labels' mix by source and key; `policy diff` shows it (threat T1). The
    "proposal ready" alert waits for T4.1.
  - T3.4, the SDK's gate (`trimset.Client`, `client.py`; `policy.py` for sdk.md §3 in
    pure Python): a poller keeps a verified bundle (fingerprints, ETag, 304, backoff,
    an optional disk cache, restarts after fork); `gate()` never raises or touches
    the network, and every default names its reason. `test_gate_agreement.py`
    checks gate() against the compiler on random policies. The compiler now moves
    thresholds off the tune values (`off_data`), so an ulp can't flip a decision.
  - T3.5, the SDK's transport (`sender.py`): one thread, one bounded queue per kind (the
    oldest dropped and counted), batches by size or time, backoff with full jitter on
    network errors, 429 and 5xx, any other 4xx dropped with one warning. `log_exchange`
    hashes the state on the calling thread with the bundle's key and never sends it;
    `gate()` with a decision id queues an action report; `outcome()` checks shape only;
    `instrument()` wraps an in-process model. Flushes for 2 s at exit.
  - T3.6, SDK tests: `docs/03-api/vectors/gates.json` (24 reference `gate()` calls, made
    by `gates.py` with trimset-stats; `test_gate_examples.py` and the TypeScript SDK reproduce
    them); `test_chaos.py` (Hypothesis on gate(), a hanging server, garbage bundles, fork);
    `test_gate_benchmark.py` (NFR-04: 1,000 policies, p99 9 us against 1 ms, no socket);
    `server/tests/test_sdk_integration.py` (a real server on a port: an activation and a
    rollback reach the SDK within a poll; the server stopped and restarted).
  - T3.7, the M3 demo (`examples/m3-ticket-triage/run.sh` and `app.py`, run in CI's image
    job): a month of tickets, a compiled policy approved and activated, an app using the
    SDK that gates six tickets and follows a second policy's activation (+5 s) and a
    rollback (+2 s), and the audit trail. **M3 is done** (2026-11-12); the alpha is next.
  - TA.1, the release pipeline (`.github/workflows/release.yml`, `tools/release_check.py`,
    `CHANGELOG.md`, `docs/releasing.md`): a tag `vX.Y.Z[aN|bN|rcN]` on a green `main`
    builds amd64 and arm64 images on native runners, joins them under one tag on GHCR,
    then publishes the SDK to PyPI by trusted publishing, then the GitHub release;
    a pull request touching the release machinery runs it as a rehearsal. Needs the
    maintainer once: a PyPI pending publisher, the `pypi` environment, GHCR visibility.
  - TA.2, `docs/quickstart.md` (a stand-in model, no account: start, keys, a call through
    the proxy, a generated month of tickets, calibrate, compile, approve, gate) and
    `docs/self-hosting.md` (what runs, TLS, every setting, backups, upgrades, what is not
    enforced yet). Commands are copied from the demos CI runs; when the first image is
    published, switch the quickstart to `docker compose pull`.
  - The docs site (ADR 0016, started early by the re-plan): `mkdocs.yml` + `docs/`, built by
    `tools/build_docs.py` with Zensical 0.1.0 pinned in its own `uv run --no-project` environment
    (not in the lockfile; the project's cutoff predates it). The API reference is Redoc 2.5.1
    (3.x is Node-only) in its own page, `docs/api-reference/viewer.html`, framed by
    `docs/api-reference.md`: the build bundles `openapi.yaml` and `schemas/*.json` into one
    `openapi.json` and fetches Redoc from npm against a pinned checksum; both are generated,
    git-ignored. A relative URL in a page is relative to its *source file* (`src="api-reference/…"`,
    not `"…"`). No `repo_url` in `mkdocs.yml`: the theme would call api.github.com from readers'
    browsers. The CI job `docs site` builds it when `docs/**`, `mkdocs.yml` or the script change; it is
    not one of the nine required checks.
- Pace (2026-11-11): go from one task to the next without asking, each green in CI
  first. Stop for what needs the user: API keys (T1.14), money, the re-plan after M1.
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
- **Phase 5:** the proxy's hot path is Rust, measured before it was kept (ADR 0015);
  nothing else moves to Rust. Rust is installed here by Homebrew's rustup
  (`/opt/homebrew/opt/rustup/bin`); build with `-j 2`, as RAM is tight.
- **Phase 5:** decision models run in the app's own process (a Hugging Face
  `pipeline("decision")`) are captured by the SDK, not the proxy: `instrument()` in T3.5
  (FR-36), the served model named `<repo>@<revision>` (wire-formats.md §2).
- **Phase 5:** agents and a chat assistant wait until the guarantees exist (ADR 0014):
  an MCP server and agent hook (TV.1, v0.2 or earlier if M3 leaves room) and an
  assistant in the v2 dashboard (TV.2). No model playground.
- **Phase 5:** the docs site (T7.1) will be Zensical on docs.trimset.dev, with the API
  reference rendered from `openapi.yaml`, never written by hand (ADR 0016). Material for
  MkDocs is the fallback through the same `mkdocs.yml`.
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

`site/`: 14 static pages and one `style.css`, no build step and no JavaScript, served at
https://trimset.dev by Cloudflare (`wrangler.jsonc`, an assets-only Worker; deploy below).
Built early, on 2026-11-11, from the site v2 design (T7.5's pages, TA.3's deploy).
- Keep its look: dark theme, Space Grotesk / Inter / JetBrains Mono, the `--accent` variable.
- The header and footer are copied into every page, so a nav change touches all 14. The
  six `use-cases/*.html` share one skeleton: change one, change all six.
- Claims match what is built. Anything not built carries a "Coming soon" badge or names
  its milestone, and a use case says "Demo available" only when its `examples/` demo
  exists (today: refund fraud and ticket triage). A PR that builds a feature updates the site with it.
- **No raw `[X]` or `[N]` in copy a visitor reads.** Until a demo can print the number, say what
  the compiler does ("lo and hi are proposed from your own labels"). Figures come only from a demo
  run: the home table and the refund fraud and ticket triage pages are one CI run of the M2 and M3
  demos (2026-11-12), labelled generated data and "one run" (each fit draws fresh splits, so a rerun
  differs a little). Retake them when the demos' output changes.
- **Badges mean three different things:** `Demo available` (an `examples/` demo exists), `No demo yet`
  (the question type works today, no script), `Coming soon` (something it needs isn't built).
- Never write a milestone as the future once it is done ("gate() from M3" went stale the day M3 did).
- Name no company beyond TypeSafe, OpenAI, Ollama, Ollaya, LangGraph and decision-circuits.
- `404.html` uses root-absolute links: it is served at any path.
- Check at 390 px: no sideways scroll; code blocks and tables scroll in their own box.

## Layout

- `sdk/python/`: package `trimset` (SDK and CLI). Python 3.11 and newer, no dependencies
  yet.
- `stats/`: package `trimset-stats`. Pure functions, no I/O.
- `server/`: package `trimset-server` (server and worker).
- `proxy/`: `trimset-proxy`, the front door in Rust (ADR 0015). It forwards proxy calls,
  gives decision ids and passes everything else to the server. It never parses answers
  or touches the database: it hands exchanges to the server's `/internal/proxy/*`
  (`internal.py`, `capture.py`). One file, `src/main.rs`, with its tests.
- `tools/`: package `trimset-tools`, never published and never in the server image. The
  mock upstream (`trimset-mock-upstream`) and the data generator (`trimset-datagen`). Tests
  use the mock in-process: `create_app(transport=httpx2.ASGITransport(mock_upstream.create_app()))`.
- `docs/`:
  - the brief and one doc per phase;
  - `adr/`, the decision records;
  - `research.md`, the verified sources;
  - `03-api/`, the contracts;
  - `design/`, the prompts for Claude Design.
- `site/`: the website (see Homepage); `wrangler.jsonc` deploys it.
- `.github/workflows/ci.yml`: lint, types, tests per package with coverage gates, SDK on
  Python 3.11, contract checks, and an image and compose smoke test.
- `Dockerfile`: one image for the proxy, the server and the worker.
  - Base images and uv are pinned by digest.
  - It runs as uid 10001.
  - Entrypoint `trimset-server`.
- `compose.yaml`: Postgres 18.6, the proxy on :8800, the server behind it on :8810 (two
  processes), and the worker. It needs `TRIMSET_DB_PASSWORD` in
  `.env`; there is no default password.
- `compose.mock.yaml`: adds the mock upstream on :9000 (Dockerfile target
  `mock-upstream`) and points the server at it. CI's image job uses both files.
- **Server logging and telemetry:**
  - Logs are JSON on stdout. `logs.redact` strips anything that looks like a trimset key
    from every log line.
  - OpenTelemetry is on only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, and span
    attributes pass through the same redaction.
- **HTTP client:** `httpx2`, not `httpx`, which Starlette deprecated.
- **Auth:** routes depend on `auth.require("<capability>")`, where the capability is
  `proxy`, `ingest`, `review` or `admin`. Scope → capability lives in
  `keys.CAPABILITIES`. Every query is scoped by `principal.project_id` (FR-51).
- **Proxy records:** `recorder.Recorder` buffers them (bounded, drop-oldest, counted) and a
  background task writes them via `store.store`, the same path as `/v1/decisions:log`.
- **Operator config:** upstreams are `TRIMSET_UPSTREAM_<NAME>_*` (`upstreams.py`), and
  `TRIMSET_SECRET` (at least 32 characters) derives the state-hash keys. Both live in
  `.env` for compose.
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
gh workflow run load.yml --repo trimset/trimset # load test, ~20 min; results in the log and summary
cd proxy && cargo clippy -j 2 --all-targets -- -D warnings && cargo test -j 2  # the Rust proxy
npx wrangler@4.157.0 deploy                     # the site to trimset.dev (once: npx wrangler@4.157.0 login)
uv run --frozen python tools/build_docs.py      # the docs site, into _docs_build/ (ADR 0016)
npx wrangler@4.157.0 deploy -c wrangler.docs.jsonc   # docs.trimset.dev (live since 2026-11-12; deploy from a clean main build)
python3 tools/crate_ages.py 2026-11-04          # crates against the 7-day hold
TRIMSET_DB_PASSWORD=x docker compose config --quiet   # check compose without starting it
uv run trimset-mock-upstream --port 9000        # the mock upstream, for trying the proxy
uv run trimset-datagen refund-fraud --out data/ # synthetic data; --url to send it
uv run trimset --help                           # the CLI (config set --url URL, then calibrate, ...)
# The image is built and smoke-tested in CI (job "image"), not locally.

# Contract checks (also in CI)
cd docs/03-api
uvx --from openapi-spec-validator==0.7.2 openapi-spec-validator openapi.yaml
uv run --no-project --with rfc8785==0.1.4 python vectors/generate.py --check
```
