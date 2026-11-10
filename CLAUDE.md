# trimset

Open-source, self-hosted decision ops for decision models (Jev, Ollaya, Ollama, OpenAI
decisions). It logs every decision, joins it to the real outcome when that arrives,
refits calibration and thresholds, pushes approved policies to running apps, and alerts
on drift. The full brief is [docs/kickoff.md](docs/kickoff.md); it calls the project
"trueset", which was renamed in Phase 0.

## Current phase

**Phase 4, Plan: written, awaiting approval** ([docs/04-plan.md](docs/04-plan.md)).
Phases 0 to 3 were approved on 2026-11-10. Next: Phase 5, Implementation, milestone M1,
starting with T1.1 (scaffold) and T1.2 (CI plus `docs/05-testing.md`).
- Plan: v0.1.0-alpha around 2027-07-05 (M1–M3), v0.1.0 around 2027-10-18.
- Re-plan after every milestone, from actual hours.
- The GitHub org `trimset` exists, created on 2026-11-10. The repo isn't there yet.

Phases and their docs are in the brief.

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
  - Privacy and access: the state hash is a keyed HMAC; the proxy key sits in the URL
    path, with a `proxy` key scope.
  - SDK: when nothing is configured it falls back to ESCALATE, and never raises.
  - The SDK stays pure Python for 3.11 and newer, with no numpy or scipy.
- An algorithm change needs its source added to `docs/research.md`, verified through the
  arXiv or Crossref API, never from memory.
- Mermaid diagrams are checked by rendering them with mermaid 11.4.1 in a browser before
  committing. All 17 passed in Phase 2.
- **Phase 3** (`docs/03-api/`):
  - Wire formats: two formats, `systemone` and `openai-decisions`, chosen by the
    upstream's settings, not the path.
  - Hashing: `question_id` hashes one canonical `question/1` form, whatever the format.
  - Keys: four scopes (`tsp_`, `tsi_`, `tsr_`, `tsa_`).
  - Upstreams and alert channels are operator-defined in the server environment
    (`TRIMSET_UPSTREAM_<NAME>_*`, `TRIMSET_ALERT_<NAME>_*`), and projects pick them by
    name. The API never carries URLs, headers or secret names (ADR 0011; two security
    reviews). In proxy mode, actions are
    reported through `POST /v1/decisions:actions`.
  - Probability sums: 1e-3 tolerance, then renormalised.
  - Vendor facts, read from the vendors' own docs: Ollama ≥ 0.35.0; only TypeSafe names
    the version that answered.
  - Every hash and id must reproduce `docs/03-api/vectors/vectors.json`. Change the
    vectors only through `generate.py`.

## Rules

- **Phase gates:** finish a phase, write its doc, stop for approval. No code until
  Phases 1 to 4 are approved.
- **Ask, don't guess:** the user often types by voice. When input is unclear, ask short
  numbered questions.
- Everything is written under `docs/`. Diagrams are Mermaid inside Markdown.
- Every major technical choice gets an ADR in `docs/adr/NNNN-title.md` (context,
  options, decision, consequences).
- Every algorithm cites verified research (`docs/research.md`, from Phase 2). Never
  invent a reference, and flag any you can't verify.
- Independent implementation: never copy code from other projects, s1ops included.
  Reading their public docs is fine.
- Small Conventional Commits. No secrets in the repo.
- Update this file at the end of every phase.

## Homepage

`site/index.html`. Keep its look: dark theme, Space Grotesk / Inter / JetBrains Mono,
the `--accent` variable. Fill the `[X]%` cells only with numbers from our own demo data.
Before it goes live (GitHub Pages, Phase 7) its claims must match what is built. Today
nothing is built. It also promises a TypeScript SDK (planned for v0.2), and its adoption
snippet lacks the `/p/<proxy key>` path that ADR 0011 requires.

## Layout

- `docs/`: the brief and one doc per phase; `docs/adr/` holds the decision records;
  `docs/research.md` holds the verified sources.
- `docs/design/`: prompts for Claude Design (the full site, and the v2 dashboard). When a
  Phase 1 change adds, drops or renames a screen, update `dashboard-prompt.md` too.
- `site/`: the homepage.

Code arrives in Phase 5.

## Commands

Contract checks. These are light and run locally; there are no product tests yet.

```bash
cd docs/03-api
uvx --from openapi-spec-validator==0.7.2 openapi-spec-validator openapi.yaml
for s in question record outcome fit policy drift_report bundle; do
  uvx --from check-jsonschema==0.33.0 check-jsonschema --schemafile schemas/$s.schema.json examples/$s.json; done
uv run --no-project --with rfc8785==0.1.4 python vectors/generate.py --check
```
