# Phase 0: Discovery

**Status:** awaiting approval · 2026-11-10 · Brief: [kickoff.md](kickoff.md)

## Answers

| # | Question | Answer | Decided by |
|---|---|---|---|
| 1 | Project name | **trimset** | Claude, at your request |
| 2 | Server language | **Python** | Claude (you handed over the tech stack) |
| 3 | Storage for v1 | **Postgres only** | Claude's recommendation; you said ok |
| 4 | Dashboard | **v2**; v0.1 ships the API and a CLI | Claude's recommendation; you said ok |
| 5 | SDKs in v1 | **Python** in v0.1, **TypeScript** in v0.2 | Claude's recommendation; you said ok |
| 6 | Weekly time budget | **Assumed 10 h/week** | Assumption: confirm before Phase 4 |
| 7 | GitHub org or personal account | **Org `trimset`** | Claude's recommendation (you asked) |

### 1. Name: trimset

To *trim* a sail or an aircraft is to adjust it so it holds a true course. A trimset is
the set of thresholds you keep trimmed against what really happened. It keeps the
homepage's headline ("Keep your decision thresholds true") and its check-mark logo.

**Why not trueset:** the GitHub org `trueset` exists (8 repos), and PyPI `trueset` is a
data-quality checker (0.2.0, released 2026-07-25), close enough to our space to confuse.

Availability, checked 2026-11-10:

| Where | `trimset` |
|---|---|
| GitHub user or org | free |
| PyPI | free |
| npm | free |
| crates.io | free |
| `trimset.dev`, `.com`, `.io` | no DNS records (likely unregistered) |

No software product uses the name. The nearest match is an unrelated barber app made by
"Smox Trimsetters LLC".

Runners-up, all free on GitHub, PyPI, npm and crates.io: `steadyset`, `tareline`,
`outcomeset`, `keelset`. Rejected: `truewind` (the `.com`, `.io` and GitHub
`truewind-ai` are taken, likely by an AI company of that name), `plumbline`,
`hindsight`, `sextant`, `telltale` (taken everywhere).

### 2. Server language: Python

- The product's core is statistics: calibration fits, Clopper-Pearson bounds,
  permutation tests. numpy and scipy already have these. With Rust we would write them by
  hand or run a second Python service anyway.
- Golden tests compare directly against scipy and scikit-learn.
- One maintainer: Python is much faster to build.
- **Risk:** the speed targets (ingestion p99 < 20 ms at 2k decisions/s; proxy overhead
  < 5 ms p99). Plan: batched async inserts, and a load test in M1 proves it. If it misses,
  only the ingest and proxy path is rewritten in Rust, behind the same API.

Proposed rest of the stack (each gets an ADR in Phase 2):

- FastAPI on uvicorn, asyncpg, httpx for calls to the upstream model.
- numpy and scipy for the stats; scikit-learn only as a reference in tests.
- A worker process for outcome joins, refits and drift checks, with its jobs in a
  Postgres table. No Redis, no message broker.
- uv workspace monorepo, ruff, pytest and hypothesis.
- `docker compose up` starts the server, the worker and Postgres.

### 3. Storage: Postgres only

Compose already starts Postgres. The decisions table is partitioned by time, which
SQLite can't do. One database means one set of tests.

### 4. Dashboard: v2

v0.1 is the API and a CLI. The threshold simulator, calibration curves and drift
reports come out of the CLI and the API as tables and JSON first. Milestone M6
(dashboard) moves to after the v0.1 launch.

### 5. SDKs: Python first

The Python SDK ships in v0.1 (`log`, `outcome`, `gate`, policy cache, fail-open). The
TypeScript SDK follows in v0.2 from the same spec, written in Phase 3 so the two can't
drift apart. TypeScript users can still adopt on day one through proxy mode, which needs
only a base-URL change.

### 6. Time budget

Not answered yet. Until you say, the plan assumes 10 hours a week. Phase 4 sizes the
milestones to the real number.

### 7. GitHub org

Start with an org named `trimset`. It's free, takes a minute, and you stay its only
owner. The owner's name ends up in the container image path (`ghcr.io/trimset/...`), the
Pages URL, the PyPI publishing setup and every install line. Moving the repo later
redirects the repo but none of those.

## Use cases (real-world examples)

Phase 1 turns each of these into a worked example: the question asked, its type, the
policy target, what outcome comes back and when. Each one also becomes a scenario in the
synthetic data generator. The three marked ★ get runnable demos in `examples/` and a
section on the homepage.

| | Use case | Real-world example | Question (type) | Outcome arrives | Policy target |
|---|---|---|---|---|---|
| a | PII guardrail | A company's internal chat assistant checks every prompt before it reaches an LLM | "Does this text contain personal data?" (noul) | Privacy team reviews a sample; days | False passes ≤ 1% |
| b ★ | Support ticket triage | An online shop routes tickets to billing, shipping, returns or tech | "Which team should handle this?" (choice, 4 options) | The team that finally handled the ticket; hours | Misroutes ≤ 5% among auto-routed; the rest go to a human dispatcher |
| c ★ | Refund fraud | The same shop screens refund requests before paying out | "Is this refund request fraudulent?" (noul) | Chargeback or confirmed fraud; 30–90 days | False passes ≤ 2% with 95% confidence; the uncertain middle goes to the risk team |
| d | Agent tool approval | An ops agent wants to run a database write or send an email | "Is this tool call safe to run without a human?" (noul) | Human approve/deny on escalated calls; incident reports | Unsafe passes ≤ 0.5% |
| e ★ | Model routing | A chat product sends easy requests to a small model and hard ones to a large model | "How hard is this request?" (score, 4 levels) | Thumbs-down, a retry, or an LLM judge on a sample; minutes to hours | Cost model: price of a large-model call vs. a bad answer |
| f | Content moderation | A marketplace checks new listings before they go live | "How serious is the problem?" (score: none, mild, serious, remove) | Moderator decision or a user report; days | Review queue under N items a day |

**Why these three demos:** between them they cover all three question types (choice,
noul, score), the escalation band, an outcome that arrives months later, and the cost
model. Refund fraud is also the example the homepage already shows.

## Premise check

Checked 2026-11-10. Most sources are third-party write-ups; the official API docs get
read in Phase 3, before the contracts are written.

- **Jev (TypeSafe):** `POST https://api.typesafe.ai/v1/systemone` with `model`, `state`
  and `questions`; question types `choice`, `score` and `noul`. Versions can be pinned
  (`jev-1.13.0`), or called through a moving `latest` alias, which is exactly the case our
  silent-upgrade detection is for. Official docs: `docs.typesafe.ai/concepts/system-one`.
  Sources: [Prompt Index](https://www.thepromptindex.com/jev-typesafe-system-one-model-guide.html),
  [DataCamp](https://www.datacamp.com/blog/system-one-models-jev),
  [OpenRouter](https://openrouter.ai/typesafe/jev-1.13/providers).
- **Ollaya:** Rust, Apache-2.0, a single binary, listens on `127.0.0.1:11435`
  (`ollaya serve`, `ollaya pull laya:latest`), API compatible with Jev. Repo:
  [ollaya-dev/ollaya](https://github.com/ollaya-dev/ollaya/releases); write-up:
  [RuntimeWire](https://runtimewire.com/article/ollaya-local-decision-model-runtime).
- **OpenAI:** `POST /v1/decisions` on `gpt-6-luna`, up to 400 questions per request.
  Reportedly public beta 2026-10-06, then generally available 2026-10-22; sources
  disagree. Sources: [promptfoo](https://www.promptfoo.dev/docs/providers/openai-decisions/),
  [Parallel](https://parallel.ai/articles/what-is-a-decisions-api.md).
- **Seen only in search-result titles, not checked:** an Azure OpenAI Decisions API, a
  Microsoft model called "decision-1", and a model called Clef. Phase 1 decides which
  upstreams the proxy supports in v0.1.
- **Research:** arXiv [2601.13284](https://arxiv.org/abs/2601.13284) (calibration-aware
  reinforcement learning for decision-making LLMs) is a candidate for `research.md`. The
  2026 arXiv IDs in the brief are newer than Claude's training data and stay unchecked
  until Phase 2 verifies each one.

## Changes to the brief

- Name: `trueset` becomes `trimset`. The homepage is in `site/index.html` with the new
  name and the org `trimset`; the `[X]%` placeholders are kept.
- The dashboard (M6) moves to after v0.1.
- The TypeScript SDK moves to v0.2. The homepage still says "SDKs for Python and
  TypeScript" and `npm i trimset`. Both get fixed before the homepage goes live in
  Phase 7.

## For you to do (outside the repo)

1. Create the GitHub org `trimset` on the free plan:
   <https://github.com/account/organizations/new>. The GitHub CLI can't create orgs.
2. Register `trimset.dev` before Phase 7 (it becomes the homepage domain).
3. The package names are free today, and the first real PyPI release comes at M1. Say
   if you want them locked in sooner.

## Open before Phase 1

- Your weekly hours (10 assumed).
- Anything above you want changed.
