# Kickoff brief (original, verbatim)

> Saved in Phase 0 so later phases don't depend on chat history. Written when the
> project was called **trueset**; it was renamed **trimset** in Phase 0
> ([00-discovery.md](00-discovery.md)). Where this brief and a later approved phase
> doc disagree, the phase doc wins.

---

# Claude Code Kickoff Prompt — Decision-Ops Platform

> Paste everything below into Claude Code in an empty repo. Project name: trueset (confirm availability in Phase 0).

---

## 0. Your role and how you work

You are the lead engineer and architect for a new open-source project, `trueset`. You will take it through a full SDLC: discovery → requirements → architecture → API contracts → plan → implementation → testing → release → operations.

**Working rules (follow strictly):**
1. **Phase gates.** Finish one phase, write its document, then STOP and ask me to approve before starting the next. Never jump into code before Phases 1–4 are approved.
2. **Ask, don't guess.** I often type by voice, so input may be garbled. If anything is ambiguous, ask short numbered questions instead of assuming.
3. **Everything is written down** under `docs/`. Diagrams are Mermaid inside Markdown so they render on GitHub.
4. **Every major technical choice gets an ADR** in `docs/adr/NNNN-title.md` (context, options, decision, consequences).
5. **Keep a `CLAUDE.md`** at the repo root with project context, conventions, commands and current phase, and update it at the end of every phase.
6. **Independent implementation.** Do not copy code from other projects (including s1ops). Reading their public docs for ideas is fine.
7. **Small, tested commits.** Conventional Commits. No secrets in the repo.

---

## 1. Context: what decision models are

A new model class appeared in September 2026: **decision models** (also called "System One" models). They don't generate text. A request contains:
- a **state** (a message, a ticket, any JSON),
- a set of typed **questions**: `noul` (yes/no probability), `choice` (pick one of K options), `score` (ordinal level 0..L-1).

The model returns **calibrated probabilities** for each question in one forward pass, in milliseconds.

- The de-facto wire format is TypeSafe's `POST /v1/systemone` (state + questions → answers with probabilities). Their hosted model is **Jev**.
- OpenAI exposes a similar `POST /v1/decisions`.
- Open models are served locally by **Ollaya** (Rust, default port 11435, Jev-compatible API) and by **Ollama** (0.40+ serves some decision models over the same wire format).
- Apps use the probabilities as "smart if-statements": `if p_pii > 0.9: block elif > 0.6: review else: allow`.

**Existing tooling (do not rebuild these):**
- Runtimes: Ollaya, Ollama.
- In-graph routing: decision-circuits, jev-lanes, langgraph-jev-router, langchain-typesafe.
- Offline analysis: s1ops (file-based CLI for calibration, thresholds and drift on JSONL logs; no live service, no public repo).

## 2. The gap we fill

Everyone hand-codes thresholds, guesses them once, and never revisits them. Nothing open-source runs **live in production** to close the loop:

> **`trueset` is the open, self-hosted "Langfuse for decision models":** it captures every decision live, joins it to the real outcome when that arrives later, refits calibration and thresholds on a schedule, pushes approved policies back to running apps, and alerts on drift.

**Target users:** developers and ML/platform teams already using Jev, Ollaya, Ollama or OpenAI decisions for routing, guardrails, triage, fraud flags and agent tool approval.

## 2b. Existing asset: the homepage

A homepage design already exists as a single self-contained file, `index.html`, in my **Downloads folder** (e.g. `~/Downloads/index.html`; on Windows `C:\Users\<me>\Downloads\index.html`). If you can't find it, ask me for the path.

- In Phase 0, copy it into the repo at `site/index.html`. Don't redesign it; keep its look (dark theme, Space Grotesk / Inter / JetBrains Mono, `--accent` CSS variable).
- Replace `[YOUR-ORG]` with the GitHub org once chosen. The trade-off table uses `[X]%` placeholders on purpose; fill them only with real numbers from our own demo data, never invented ones.
- Keep the page's claims in sync with what is actually built. If a feature isn't shipped yet, mark it "coming soon" or remove it.
- In Phase 7, add a GitHub Actions workflow that deploys `site/` to GitHub Pages, with notes for pointing the custom domain `trueset.dev` at it.

## 2c. Research backing (every algorithm must cite its source)

Trueset's algorithms must be grounded in published research, not ad-hoc heuristics. In Phase 2, write `docs/research.md` mapping each component to its papers, and cite the relevant paper(s) in every algorithm ADR. Verify each citation (title, authors, year, arXiv/DOI link) before using it; never invent or guess a reference, and flag any you can't verify.

**Foundations (old but still the standard methods):**
- Calibration: Platt (1999), Zadrozny & Elkan (2002, isotonic), Guo et al. (2017, temperature scaling), Naeini et al. (2015, ECE), Kumar et al. (2019, *Verified Uncertainty Calibration*).
- Thresholds / abstention: Chow (1970, reject option), Geifman & El-Yaniv (2017, selective classification).
- Thresholds with guarantees: Angelopoulos, Bates et al. (2021, *Learn then Test*), Angelopoulos et al. (2022, *Conformal Risk Control*).
- Escalation: Madras et al. (2018) and Mozannar & Sontag (2020, learning to defer), Chen et al. (2023, FrugalGPT cascades), Elkan (2001, cost-sensitive learning).
- Live refits under shift: Gibbs & Candès (2021, *Adaptive Conformal Inference*).
- Drift: Rabanser et al. (2019, *Failing Loudly*), Lipton et al. (2018, label shift), Gama et al. (2014, concept drift survey), Žliobaitė (2010, delayed labels).
- Statistics: Clopper-Pearson (1934) intervals, McNemar (1947) test.

**Recent work to build on (2025–2026, read before designing the policy compiler):**
- *Conformal Selective Prediction with General Risk Control* — arXiv 2603.24704
- *LEC: Linear Expectation Constraints for Selection-Conditioned Risk Control in Selective Prediction and Routing Systems* — arXiv 2512.01556
- *MultiRisk: Multiple Risk Control via Iterative Score Thresholding* — arXiv 2512.24587 (several error budgets at once)
- *Hierarchical Group-Conditional Conformal Risk Control for Selective Prediction in Language Models* — arXiv 2607.24562 (per-slice guarantees)
- *Conformal Selective Acting: Anytime-Valid Risk Control for RLVR-Trained LLMs* — arXiv 2605.20270 (valid under continuous monitoring)
- *When Can Conformal Risk Control Certify LLM Outputs?* — arXiv 2606.29054 (limits and failure cases)
- *Available Guardrails: Certifying Selective Prediction across ML Systems* — arXiv 2609.22048
- *SCOPE: Selective Conformal Optimized Pairwise LLM Judging* — arXiv 2602.13110

**Design rules that follow from this:**
- Every policy states which guarantee it gives (e.g. "false-pass rate ≤ 2% with 95% confidence") and the assumptions behind it (exchangeability, sample size). The dashboard and API show this.
- Where assumptions break (distribution shift, delayed labels), use online/adaptive methods and say so instead of claiming a guarantee.
- Golden tests reproduce at least one published result per method on a public or synthetic dataset.

**Research track:** applying conformal risk control and adaptive conformal inference to typed decision models with delayed outcomes in production appears unpublished. Keep a `research/` folder with reproducible experiments (fixed seeds, configs, result tables) on the synthetic generator and public data, so results can become a workshop paper later.

---

## 3. Phase 0 — Discovery (ask me before writing anything)

Ask me these, then write `docs/00-discovery.md` with the answers and your assumptions:
1. Project name (check that it's free on GitHub, PyPI, npm and crates.io; propose 5 options if I have none).
2. Server language: Rust (single binary, fast ingestion), or Python (faster to build, same language as stats)? Give me a short trade-off first.
3. Storage for v1: Postgres only, or Postgres plus SQLite for local dev?
4. Dashboard: in v1 or v2?
5. Which SDKs in v1 (Python, TypeScript, both)?
6. My weekly time budget, so the plan is realistic.

---

## 4. Phase 1 — Requirements → `docs/01-requirements.md`

Write a full PRD containing: vision, personas, user journeys, user stories with acceptance criteria (Given/When/Then), functional requirements with IDs (FR-xx), non-functional requirements (NFR-xx), explicit non-goals, success metrics, and a requirements traceability matrix (FR → component → test).

Include a Mermaid **use-case/flow diagram** and a **requirements mind map**.

**Functional requirements to cover (refine and number them):**

**A. Capture**
- **Decision logging API:** `POST /v1/decisions:log` (batch supported). A record holds:
  - `decision_id`, `project_id`, `timestamp`;
  - `question_id` (stable hash of exact question wording and option order) and `primitive`;
  - `options`/`levels`, `model_requested`, `model_served`;
  - raw probabilities, `latency_ms`, `cost_usd`;
  - `policy_version` used, `action_taken`;
  - `tags` (slice features), `external_ref`;
  - `state_sha256`.
- **Proxy mode (key adoption feature):** the server also exposes `POST /v1/systemone` and `POST /v1/decisions`, forwards them to a configured upstream (Jev, Ollaya, Ollama, OpenAI) and logs automatically. Adoption = change one base URL, zero code.
- **Privacy by default:** raw `state` is never stored unless a project explicitly opts in. Only hashes and tags are kept.

**B. Outcomes**
- `POST /v1/outcomes`: attach ground truth by `decision_id` or `external_ref`.
- Supports late arrival (hours or days), corrections (last-write-wins with audit history), and label sources (human review, downstream signal, rule).
- Webhook-friendly payloads.

**C. Calibration engine**
- Per group `(question_id, model_served, primitive)`; never pool across wordings or models.
- Methods: temperature scaling, Platt, isotonic.
- Metrics: log loss, Brier, ECE (equal-width and equal-mass), reliability diagram data.
- Strict train/cal/eval splits. Applying a fit to items it was fitted on is refused.

**D. Policy compiler**
- Turns targets into thresholds per group:
  - error budget (max error rate on auto-accepted items),
  - max false-pass rate for blocking decisions,
  - System 1 → System 2 **escalation band** `[lo, hi)`,
  - cost model (cost of escalation vs. cost of an error).
- Outputs a versioned policy with a SHA-256 fingerprint plus metrics on the split it was tuned on.
- Use statistically sound bounds (e.g. Clopper-Pearson) when sample sizes are small.

**E. Policy lifecycle**
- States: draft → proposed → approved → active → retired.
- A human approves before activation. One-click rollback.
- Scheduled refits produce *proposals*, never auto-activate in v1.

**F. Runtime gate (in SDKs)**
- SDK fetches active policies, caches them, evaluates locally in under 1 ms, and returns `ACCEPT` / `BLOCK` / `SELECT:<label>` / `ESCALATE`.
- Logs which policy version decided.
- Fails open with configurable defaults when the server is unreachable.

**G. Drift & health monitoring**
- Label-free drift on probability distributions (PSI and Jensen-Shannon) with a permutation test, so small samples don't raise false alarms.
- Silent model upgrade detection (`model_served` changes behind an alias).
- Escalation-rate shift, failure/error-rate jumps, latency regressions, and calibration degradation once labels arrive.

**H. Shadow comparison**
- Run a candidate model in parallel on live traffic (proxy fans out; only the primary answer is returned).
- Paired comparison with a McNemar test, flip rate and cost/latency deltas.

**I. Review queue**
- Low-confidence or escalated items go into a queue. Reviewer labels become outcomes.
- Raw state is shown only if the project opted in to storing it.

**J. Alerts:** webhook, Slack and email. Thresholds configurable per project.

**K. Multi-tenancy & auth:** projects, API keys (ingest-only vs. admin), audit log of policy changes.

**L. Dashboard** (if Phase 0 says v1):
- Calibration curves, threshold/escalation simulator, drift view, shadow comparison, review queue, policy history.

**Non-functional requirements to quantify:**
- Self-host with one `docker compose up`.
- Ingestion p99 < 20 ms at a target rate (e.g. 2k decisions/s on one small VM); proxy overhead < 5 ms p99.
- SDK gate evaluation < 1 ms.
- Statistical correctness verified against reference implementations (scipy/sklearn) via golden tests.
- Data retention settings; OpenTelemetry traces/metrics; Apache-2.0 license.
- Documented upgrade/migration path for schemas.

**Non-goals for v1:** running models (inference), training/fine-tuning, hosted SaaS, generic LLM text tracing, a LangGraph replacement.

---

## 5. Phase 2 — Architecture → `docs/02-architecture.md` + ADRs

Produce, all in Mermaid:
1. **C4 Level 1 – System context:** apps, SDKs, upstream model providers, reviewers, alert channels.
2. **C4 Level 2 – Containers:** API/ingest service, proxy, outcome joiner, stats/refit worker, scheduler, policy service, alerting, dashboard, Postgres, (optional) object storage.
3. **C4 Level 3 – Components** for the API service and the stats worker.
4. **Sequence diagrams:**
   - (a) proxy request with logging,
   - (b) late outcome join,
   - (c) scheduled refit → policy proposal → approval → SDK pickup,
   - (d) drift detection → alert,
   - (e) shadow comparison.
5. **ER diagram:** projects, api_keys, questions, decisions (partitioned by time), outcomes, outcome_history, fits, policies, policy_events, drift_reports, alerts, review_items.
6. **State diagram:** policy lifecycle and review-item lifecycle.
7. **Deployment diagram:** docker compose for single-node; notes on scaling (partitioning, read replicas, optional ClickHouse later).
8. **Data-flow & trust-boundary diagram** for the threat model.

**ADRs to write (at least):**
- server language,
- database and partitioning,
- stats implementation (own library, with golden tests),
- policy distribution (pull vs. push),
- privacy model (hash-only default),
- `question_id` hashing spec,
- SDK fail-open semantics,
- monorepo layout,
- license.

Also include a **threat model** (STRIDE): API key leakage, poisoned outcomes, policy tampering (hence the fingerprint check in the SDK), and PII in tags.

---

## 6. Phase 3 — Contracts → `docs/03-api/`

- `openapi.yaml` for all endpoints, including the proxy endpoints mirroring the System One wire format.
- JSON Schemas for the decision record, outcome, fit, policy and drift report, each with a version field (e.g. `record/1`).
- SDK interface spec (Python and TypeScript): `log()`, `outcome()`, `gate()`, policy cache, retry/backoff and fail-open behavior.
- An exact `question_id` canonicalization spec with test vectors.

---

## 7. Phase 4 — Plan → `docs/04-plan.md`

- Milestones, aiming to keep v0.1 small:
  - **M1:** ingest + proxy + outcomes + storage.
  - **M2:** calibration + policy compiler + CLI.
  - **M3:** SDK gate + policy lifecycle.
  - **M4:** drift + alerts.
  - **M5:** shadow + review queue.
  - **M6:** dashboard.
  - **M7:** v0.1 public launch.
- A task breakdown per milestone with estimates sized to my weekly time budget.
- A **dependency graph** (Mermaid `graph LR`) and a **Gantt chart** (Mermaid `gantt`).
- A risk register (risk, likelihood, impact, mitigation). Include:
  - "Langfuse/Arize add native decision-model support,"
  - "decision-model category fades,"
  - "solo-maintainer bandwidth."
- Definition of Done for tasks, milestones and releases.

---

## 8. Phase 5 — Implementation (only after approval)

- Scaffold the monorepo according to the ADR, e.g. `server/`, `sdk/python/`, `sdk/typescript/`, `stats/`, `dashboard/`, `deploy/`, `docs/`, `examples/`.
- Build milestone by milestone. TDD where practical. Each milestone ends with a demo script in `examples/` that runs end to end against a local Ollaya or a mock upstream.
- Provide a deterministic **mock decision-model upstream** speaking `/v1/systemone`, so everything is testable offline with no API keys.
- Provide a **synthetic data generator**: decisions plus delayed outcomes with planted miscalibration, drift and a silent model swap. All tests and demos use it.

---

## 9. Phase 6 — Testing strategy → `docs/05-testing.md`

- **Unit and property-based tests** (e.g. probabilities sum to 1; a policy can't be applied to a mismatched model).
- **Statistical golden tests:** fits, ECE, PSI and thresholds match scipy/sklearn references within tolerance on fixed seeds.
- **Integration tests:** spin up Postgres and the mock upstream.
- **Contract tests:** the SDKs against the OpenAPI spec.
- **Load tests** verifying the NFR latency/throughput targets.
- **Chaos tests:** server down → SDK fails open correctly.
- **Coverage target**, with CI failing below it.

---

## 10. Phase 7 — CI/CD, release, docs

- **GitHub Actions:** lint, format, test, build, security scan (dependency audit, secret scan).
- **Release:** container image to GHCR, Python package to PyPI, TS package to npm. Semantic versioning with a changelog.
- **Homepage:** deploy `site/index.html` to GitHub Pages (see section 2b); the docs site links back to it.
- **Docs site:**
  - quickstart ("change one base URL"),
  - concepts (calibration, thresholds, escalation band, drift),
  - API reference,
  - self-hosting guide,
  - integration guides for Jev, Ollaya, Ollama, OpenAI, LangGraph and decision-circuits.
- **Community files:** README with an architecture diagram and a 60-second demo GIF placeholder, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY.md, issue/PR templates, GOVERNANCE.md, good-first-issue labels, ROADMAP.md.

---

## 11. Phase 8 — Operations → `docs/06-operations.md`

- Runbooks: backup/restore, migrations, scaling, key rotation.
- Observability: the service's own metrics, logs and traces through OpenTelemetry.
- SLOs for ingestion and the policy API.
- Data retention and deletion (GDPR-friendly: delete by project or `external_ref`).

---

## 12. Start now

Begin with **Phase 0**: ask me the discovery questions in section 3 as a short numbered list, and nothing else.
