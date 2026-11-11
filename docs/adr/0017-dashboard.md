# 0017. The dashboard is a static React app that only reads and writes through the API

**Status:** accepted (Phase 5, M6 started early, 2026-11-11) · **Requirements:** FR-61,
FR-26, FR-51, FR-52, NFR-11 · **Follows:** [ADR 0002](0002-monorepo-layout.md),
[ADR 0004](0004-process-model-and-jobs.md), [ADR 0009](0009-privacy-model.md),
[ADR 0016](0016-docs-site.md)

## Context

FR-61 asks for the v2 web dashboard; its screens are those of the dashboard design (made
from [design/dashboard-prompt.md](../design/dashboard-prompt.md) in Claude Design, kept
out of the repository). ADR 0002 reserves `dashboard/` for it, and ADR 0004 says the
server serves it as static files under `/ui`. The maintainer asked for React, TypeScript
and Vite, talking only to the API in [`openapi.yaml`](../03-api/openapi.yaml) with an
admin key the user pastes in.

On 2026-11-11 the API covers project settings, keys, one question, fits, the simulator,
compile jobs, label coverage, decisions and policies (read only). It has no list of
questions, no outcome feed, no drift reports, shadow reports, review queue or alerts yet.

ADR 0016 keeps a Node toolchain out of the docs site. The dashboard can't avoid one: the
maintainer chose the stack, and the screens are an interactive app, not pages.

## Decision

- **Stack:** React 19, TypeScript and Vite in `dashboard/`, built to static files with
  relative asset paths (`base: "./"`), so they work under `/ui` or anywhere else.
  - Hash routing (`#/questions/refund_fraud/simulator`): no router package, and the server
    needs no fallback route.
  - Charts are hand-drawn SVG: a reliability diagram, a histogram, two line charts and
    sparklines do not justify a chart library.
  - Fonts are bundled from `@fontsource` packages. A self-hosted install must not call
    Google Fonts; trimset has no telemetry (Phase 1).
- **Dependencies:** 73 npm packages in `package-lock.json`, resolved with
  `npm install --before=<date>`, the same 7-day hold as `UV_EXCLUDE_NEWER`. CI installs
  with `npm ci --ignore-scripts`: no dependency needs an install script. The Node
  toolchain stays inside `dashboard/`; no Python package depends on it.
- **The key:** pasted on a sign-in screen, checked with `GET /v1/admin/project`, kept in
  `sessionStorage` (gone when the tab closes) and sent only to the same origin as
  `Authorization: Bearer`. A 401 signs the user out. One admin key is one project
  (FR-51), so "switch project" means signing in with another key.
- **Sample data behind one marked layer:** screens whose endpoints don't exist yet read
  `src/mock.ts`, which holds the design's sample data and names the endpoint each export
  waits for. Those screens show a "Sample data" pill, and their actions change only the
  page. When an endpoint ships, the screen calls `api()` and the export is deleted.
- **The simulator computes nothing (FR-26):** every number on it is a field of
  `POST /v1/admin/questions/{question}/simulate` or of the compile job's result. The rule
  shapes come from the server's own grid (one `simulate` without candidates), each handle
  move sends one debounced request, and "Propose policy" runs the server's compiler. The
  design computed a Wilson interval and a "meets the target" verdict in the browser; the
  build does not.
- **Raw state:** the drawer shows a locked placeholder unless the project stores raw state
  (ADR 0009). Links from data (the review link template) render only if they are http(s)
  (threat E3 in the architecture).
- **A public demo, not a hosted dashboard:** `npm run build:demo` builds the same app with
  no sign-in and no backend, served at demo.trimset.dev. It answers from a recording
  that CI's image job makes after the M2 demo (`demo/record.py`: the dashboard's own
  calls against that real server, the simulator on a 0.05 grid, three compiles and a
  lifecycle walk). Writes are refused. It never sees a key, so it can't leak one.
- **Checks:** Node's own test runner (`node --test`, type stripping, no test framework):
  the formatting and the simulate mapping, and a contract check that every `/v1` path in
  `dashboard/src` is a path in `openapi.yaml`. CI job `dashboard` runs them, `tsc` and the
  build.

## Consequences

- Each new endpoint retires a part of `src/mock.ts`; the Questions list, Outcomes, Drift,
  Shadow, Review queue, Alerts and the Overview tiles are sample data until then.
- Policies use the lifecycle from T3.1 (FR-27 to FR-29): each action has a confirm that
  names the change, and reject, retire and rollback take the reason the server requires.
- The recording goes stale when the API changes; CI records on every run that touches
  the server, so re-recording is downloading the `demo-recording` artifact.
- Serving under `/ui` is still to do: the server mounts the built files, and the image
  gets a Node build stage (or CI builds `dashboard/dist` and the image copies it). Until
  then, `npm run dev` proxies `/v1` to compose's port 8800.
- A working dashboard is never hosted on trimset.dev: each install serves its own, and a
  hosted copy would ask people to paste admin keys into a third-party page. Only the
  demo build is.
