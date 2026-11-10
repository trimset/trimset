# trimset

Open-source, self-hosted decision ops for decision models (Jev, Ollaya, Ollama, OpenAI
decisions). It logs every decision, joins it to the real outcome when that arrives,
refits calibration and thresholds, pushes approved policies to running apps, and alerts
on drift. The full brief is [docs/kickoff.md](docs/kickoff.md); it calls the project
"trueset", which was renamed in Phase 0.

## Current phase

**Phase 2, Architecture: written, awaiting approval** ([docs/02-architecture.md](docs/02-architecture.md),
[docs/adr/](docs/adr/), [docs/research.md](docs/research.md)). Phases 0 and 1 were approved
on 2026-11-10. Next: Phase 3, Contracts (`docs/03-api/`). Phases and their docs are in
the brief.

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

None yet.
