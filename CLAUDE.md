# trimset

Open-source, self-hosted decision ops for decision models (Jev, Ollaya, Ollama, OpenAI
decisions). It logs every decision, joins it to the real outcome when that arrives,
refits calibration and thresholds, pushes approved policies to running apps, and alerts
on drift. The full brief is [docs/kickoff.md](docs/kickoff.md); it calls the project
"trueset", which was renamed in Phase 0.

## Current phase

**Phase 0, Discovery: written, awaiting approval** ([docs/00-discovery.md](docs/00-discovery.md)).
Next: Phase 1, Requirements (`docs/01-requirements.md`). Phases and their docs are in
the brief.

## Decisions so far

Python (FastAPI) server; Postgres only; v0.1 ships the API and a CLI (dashboard in v2);
Python SDK in v0.1, TypeScript in v0.2; GitHub org `trimset`; 10 h/week assumed. The
reasons are in the discovery doc. ADRs come in Phase 2.

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
nothing is built, and it also promises a TypeScript SDK, which is planned for v0.2.

## Layout

- `docs/`: the brief and one doc per phase.
- `docs/design/`: prompts for Claude Design (the full site, and the v2 dashboard). When a
  Phase 1 change adds, drops or renames a screen, update `dashboard-prompt.md` too.
- `site/`: the homepage.

Code arrives in Phase 5.

## Commands

None yet.
