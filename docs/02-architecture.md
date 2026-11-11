# Phase 2: Architecture

**Status:** approved 2026-11-10; Phase 3 corrections noted in [03-api/README.md](03-api/README.md) · Inputs: [01-requirements.md](01-requirements.md), [research.md](research.md), [ADRs](adr/)

## 1. Summary

- **Processes:** two (`server` and `worker`) from one Python image, with Postgres 18 as
  the only stateful service.
- **Hot paths:** the server handles ingestion, the proxy, outcomes, policy serving and the
  admin, review and report APIs.
- **Background work:** the worker runs everything in the background from a job table in
  Postgres: joins, refits, drift checks, alerts, shadow reports and retention.
- **Apps:** gate locally with a pure-Python SDK that polls for policies.

| ADR | Decision |
|---|---|
| [0001](adr/0001-server-language.md) | Python 3.14, FastAPI, asyncpg; the SDK is pure Python for 3.11 and newer |
| [0002](adr/0002-monorepo-layout.md) | uv-workspace monorepo; packages `trimset`, `trimset-server`, `trimset-stats` |
| [0003](adr/0003-database-and-partitioning.md) | Postgres 18; monthly partitions; plain SQL migrations |
| [0004](adr/0004-process-model-and-jobs.md) | One image, two processes, jobs queued in Postgres |
| [0005](adr/0005-stats-library.md) | Our own stats library on numpy and scipy, golden-tested |
| [0006](adr/0006-policy-compiler.md) | Learn-then-Test with exact binomial tests; threshold forms for every question type |
| [0007](adr/0007-drift-and-health.md) | Drift tests on output probabilities; anytime-valid live checks |
| [0008](adr/0008-policy-distribution.md) | Pull with ETag; SHA-256 fingerprints; signatures in v0.2 |
| [0009](adr/0009-privacy-model.md) | Keyed state hash; raw state is opt-in; no secrets in the database |
| [0010](adr/0010-question-id.md) | `question_id` = hash of everything the model sees |
| [0011](adr/0011-proxy-design.md) | Proxy key in the URL path (new `proxy` scope); buffered logging that never blocks |
| [0012](adr/0012-sdk-fail-open.md) | Never raise; defaults per question; library default ESCALATE |
| [0013](adr/0013-license.md) | Apache-2.0 with a DCO sign-off |

**Phase 1 open questions, settled:**

| # | Question | Settled in |
|---|---|---|
| 1 | Knowing the project from the base URL alone | ADR 0011 |
| 2 | Keyed or plain state hash | ADR 0009 |
| 3 | The SDK's default when none is configured | ADR 0012 |
| 4 | Threshold forms for choice and score | ADR 0006 |
| 5 | Which method backs each guarantee | ADR 0006, research.md |
| 6 | Exact wire formats | Still open: Phase 3, from the official docs |

## 2. C4 level 1: system context

```mermaid
flowchart TB
  dev["App developer / ML engineer<br/>[person]"]
  appr["Approver<br/>[person]"]
  rev["Reviewer<br/>[person]"]
  ops["Operator<br/>[person]"]
  app["Customer app<br/>[software system]<br/>calls decision models"]
  ts["trimset<br/>[software system]<br/>captures decisions, joins outcomes,<br/>refits and serves policies, alerts"]
  up["Decision-model upstream<br/>[external]<br/>Jev, Ollaya, Ollama, OpenAI"]
  src["Outcome sources<br/>[external]<br/>help desk, payments, moderation tools"]
  ch["Alert channels<br/>[external]<br/>Slack, email, webhooks"]
  otel["Observability stack<br/>[external, optional]<br/>OTLP collector"]

  dev -- "builds" --> app
  app -- "decision calls through the proxy,<br/>or log() and gate()" --> ts
  ts -- "forwards decision calls" --> up
  app -. "direct calls in SDK-only mode" .-> up
  src -- "outcomes by webhook" --> ts
  ts -- "alerts" --> ch
  ts -- "traces, metrics" --> otel
  dev -- "CLI: calibrate, simulate, compile" --> ts
  appr -- "CLI: approve, roll back" --> ts
  rev -- "CLI: label review items" --> ts
  ops -- "compose, migrate, keys" --> ts
```

## 3. C4 level 2: containers

```mermaid
flowchart TB
  app["Customer app<br/>+ trimset Python SDK"]
  cli["trimset CLI<br/>[Python, on a laptop]"]
  up["Upstream model"]
  cand["Shadow candidate model"]
  src["Outcome sources"]
  ch["Alert channels"]

  subgraph sys["trimset deployment"]
    server["server<br/>[Python, FastAPI]<br/>ingest · proxy · outcomes ·<br/>policies · admin · review · reports"]
    worker["worker<br/>[Python]<br/>scheduler · joiner · refit and compiler ·<br/>drift and health · alerts · shadow reports · retention"]
    db[("Postgres 18<br/>decisions partitioned by month,<br/>outcomes, fits, policies, jobs")]
    dash["dashboard<br/>[v2, static web app]"]
  end

  app -- "proxy: /p/key/v1/systemone, /v1/decisions" --> server
  app -- "log(), outcome(), GET /v1/policies" --> server
  server -- "forward" --> up
  server -. "sampled copy, after the response" .-> cand
  src -- "POST /v1/outcomes" --> server
  cli -- "admin and review API" --> server
  dash -. "same API (v2)" .-> server
  server -- "SQL" --> db
  worker -- "SQL, jobs with SKIP LOCKED" --> db
  worker -- "alerts" --> ch
```

The brief listed more containers. [ADR 0004](adr/0004-process-model-and-jobs.md)
shows which process each one runs in.

## 4. C4 level 3: components

### Server

```mermaid
flowchart LR
  subgraph server["server process"]
    auth["Key auth<br/>scope check, 60 s cache"]
    ingest["Ingest handler<br/>validate · question_id · hash"]
    proxy["Proxy handler<br/>forward · parse · decision ids"]
    buf["Record buffer<br/>bounded, drop-oldest"]
    writer["Batch writer<br/>INSERT … ON CONFLICT DO NOTHING"]
    outc["Outcomes handler<br/>match or hold"]
    pol["Policy API<br/>bundle + ETag"]
    admin["Admin API<br/>projects · keys · settings · lifecycle · audit"]
    sim["Reports and simulator<br/>reads fit artifacts"]
    revq["Review API<br/>claim · label · skip"]
    shadow["Shadow dispatcher<br/>sampled, capped, after the response"]
    mv["Model-version watcher<br/>new served version → job"]
  end
  db[("Postgres")]
  up["Upstream"]
  auth --> ingest & proxy & outc & pol & admin & sim & revq
  ingest --> writer
  proxy --> up
  proxy --> buf --> writer --> db
  proxy --> shadow
  ingest & proxy --> mv
  mv & outc & pol & admin & sim & revq --> db
```

### Worker

```mermaid
flowchart LR
  subgraph worker["worker process"]
    sched["Scheduler<br/>due schedules → jobs"]
    runner["Job runner<br/>SKIP LOCKED, retries"]
    join["Outcome joiner<br/>held → matched · rule labels · shadow labels"]
    refit["Refit<br/>dataset · splits · calibration"]
    comp["Policy compiler<br/>Learn-then-Test · cost · feasibility"]
    drift["Drift and health<br/>PSI · JS · permutation · live checks"]
    shrep["Shadow reports<br/>McNemar"]
    alert["Alert dispatcher<br/>de-duplicate · cool-down · channels"]
    maint["Maintenance<br/>partitions · retention · deletions · leases"]
  end
  stats["trimset-stats<br/>library, no I/O"]
  db[("Postgres")]
  ch["Alert channels"]
  sched --> db
  runner --> join & refit & drift & shrep & alert & maint
  refit --> comp
  refit & comp & drift & shrep --> stats
  join & refit & comp & drift & shrep & maint --> db
  alert --> ch
```

### Where the Phase 1 components live

| Component (traceability matrix) | Process | Package |
|---|---|---|
| api, proxy, auth, review, audit log | server | `trimset-server` |
| joiner | server (write path) and worker (sweeps) | `trimset-server` |
| scheduler, drift, alerts, shadow, retention, policies (refits) | worker | `trimset-server` |
| stats, compiler | worker, and the server's simulator | `trimset-stats` |
| sdk-py, cli | the customer's app, a laptop | `trimset` |
| mock-upstream, datagen | tests and demos | `tools/` |

## 5. Sequence diagrams

### (a) Proxy request with logging

```mermaid
sequenceDiagram
  autonumber
  participant App
  participant P as trimset server (proxy)
  participant U as Upstream (Jev / Ollaya)
  participant B as Record buffer
  participant DB as Postgres
  App->>P: POST /p/{proxy key}/v1/systemone (state, questions)
  P->>P: check key scope, find project and upstream
  P->>U: same request, the app's Authorization passed through
  U-->>P: 200 answers (probabilities, model version)
  P-->>App: 200 unchanged body + X-Trimset-Decision-Id
  P->>B: one record per question (state hashed, never stored)
  loop every 50 ms or 500 records
    B->>DB: INSERT batch ON CONFLICT DO NOTHING
  end
  alt Postgres down
    B->>B: hold up to the limit, then drop oldest and count
  end
  opt first time this served version is seen
    P->>DB: record model version, queue silent-upgrade check
  end
```

### (b) Late outcome join

```mermaid
sequenceDiagram
  autonumber
  participant S as Outcome source (payments webhook)
  participant API as trimset server
  participant DB as Postgres
  participant W as worker
  S->>API: POST /v1/outcomes (external_ref rf-1001, refund_fraud, true, downstream)
  API->>DB: find decisions by project, external_ref, question
  alt decision found
    API->>DB: upsert current outcome, append history
    API-->>S: 200 matched
  else decision not logged yet
    API->>DB: hold as unmatched (expires in 30 days)
    API-->>S: 202 held
  end
  loop every minute
    W->>DB: match held outcomes to decisions that have arrived
  end
  loop daily
    W->>DB: rule labels for matured decisions with no outcome
    W->>DB: copy labels to linked shadow decisions
  end
```

### (c) Scheduled refit → proposal → approval → SDK pickup

```mermaid
sequenceDiagram
  autonumber
  participant W as worker
  participant DB as Postgres
  participant Ch as Slack
  actor A as Approver (CLI)
  participant API as trimset server
  participant SDK as SDK in the app
  W->>DB: claim scheduled refit job
  W->>DB: read matured labeled decisions in the window
  W->>W: split fit / tune / eval, fit calibration, compile (Learn-then-Test)
  W->>DB: insert fit and proposed policy with a diff against the active one
  W->>Ch: proposal ready
  A->>API: policy show (diff, guarantee, metrics)
  A->>API: approve and activate (admin key, reason)
  API->>DB: proposed → approved → active, old active → retired, events and audit
  loop every 30 s ± jitter
    SDK->>API: GET /v1/policies (If-None-Match)
    API-->>SDK: 200 new bundle, or 304
  end
  SDK->>SDK: verify fingerprints, swap the cache
  Note over SDK: gate() now answers with the new version
```

### (d) Drift detection → alert

```mermaid
sequenceDiagram
  autonumber
  participant W as worker
  participant DB as Postgres
  participant Ch as Alert channels
  W->>DB: claim hourly drift job
  W->>DB: probabilities for the current and reference windows
  W->>W: PSI, JS divergence, permutation p-value
  alt effect ≥ threshold and p below α and n ≥ minimum
    W->>DB: drift report (triggered), open or update the alert
    opt not in cool-down
      W->>Ch: drift alert with the evidence
    end
    Note over DB: the active policy's guarantee now reads "suspended"
  else no drift
    W->>DB: drift report (not triggered)
    opt an alert was open
      W->>DB: resolve the alert
      W->>Ch: resolved
    end
  end
```

### (e) Shadow comparison

```mermaid
sequenceDiagram
  autonumber
  participant App
  participant P as trimset server
  participant U as Primary model
  participant C as Candidate model
  participant DB as Postgres
  actor E as ML engineer (CLI)
  App->>P: decision request
  P->>U: forward
  U-->>P: answers
  P-->>App: primary answers only
  opt sampled (10%) and under the daily cap
    P->>C: same request, candidate model
    C-->>P: answers
    P->>DB: shadow records linked to the primary decision
  end
  Note over DB: outcomes for the primary are copied to its shadows
  E->>P: shadow report
  P->>DB: paired decisions and labels
  P-->>E: agreement, flip rate, McNemar p, cost and latency deltas
```

## 6. Data model

```mermaid
erDiagram
  projects ||--o{ api_keys : has
  projects ||--o{ questions : has
  projects ||--o{ model_versions : sees
  questions ||--o{ decisions : answered_in
  decisions ||--o| outcomes : current_label
  decisions ||--o{ outcome_history : label_history
  decisions ||--o| decision_states : raw_state_if_opted_in
  projects ||--o{ unmatched_outcomes : holds
  questions ||--o{ fits : calibrated_by
  fits ||--o{ policies : compiled_into
  policies ||--o{ policy_events : transitions
  questions ||--o{ drift_reports : checked_by
  projects ||--o{ alert_rules : configures
  alert_rules ||--o{ alerts : raises
  decisions ||--o| review_items : queued_as
  projects ||--o{ audit_log : records
  projects ||--o{ schedules : runs
  schedules ||--o{ jobs : creates

  projects {
    uuid project_id PK
    text name
    jsonb settings "upstream, retention, privacy, env-var secret refs"
  }
  api_keys {
    uuid key_id PK
    uuid project_id FK
    text scope "proxy, ingest, review or admin"
    text key_hash
    timestamptz revoked_at
    timestamptz last_used_at
  }
  questions {
    text question_id PK
    uuid project_id FK
    text name "stable alias, e.g. refund_fraud"
    text type "noul, choice or score"
    jsonb definition "text, options or levels"
    jsonb settings "actions, targets, rules, audit share, link template"
  }
  model_versions {
    uuid project_id FK
    text model_requested
    text model_served
    timestamptz first_seen
  }
  decisions {
    uuid decision_id PK
    timestamptz ts PK "partition key, monthly"
    uuid project_id PK
    text question_id FK
    text model_requested
    text model_served
    real_array probs
    int latency_ms
    numeric cost_usd
    int policy_version
    text action_taken
    bool audit
    jsonb tags
    text external_ref
    text state_hash "HMAC-SHA-256"
    text role "primary or shadow"
    uuid shadow_of
  }
  decision_states {
    uuid decision_id PK
    timestamptz ts "partition key"
    jsonb state "only if the project opted in"
  }
  outcomes {
    uuid decision_id PK
    text label
    text source "human_review, downstream or rule"
    timestamptz observed_at
    uuid key_id
  }
  outcome_history {
    bigint id PK
    uuid decision_id FK
    text label
    text source
    timestamptz received_at
    uuid key_id
  }
  unmatched_outcomes {
    bigint id PK
    text decision_ref "decision_id or external_ref"
    text label
    timestamptz expires_at
  }
  fits {
    uuid fit_id PK
    text question_id FK
    text model_served
    text method
    jsonb params
    jsonb metrics "raw and calibrated, eval split"
    jsonb reliability
    bytea eval_artifact "scores and labels, no ids"
    jsonb provenance "window, split sizes, seed"
  }
  policies {
    uuid policy_id PK
    text question_id FK
    text model_served
    int version
    text state
    jsonb body "immutable: rules, calibration map, guarantee"
    text fingerprint "SHA-256 of canonical body"
    uuid fit_id FK
  }
  policy_events {
    bigint id PK
    uuid policy_id FK
    text from_state
    text to_state
    uuid key_id
    text reason
  }
  drift_reports {
    uuid report_id PK
    text question_id FK
    text model_served
    text kind
    jsonb stats "PSI, JS, p, window sizes"
    bool triggered
  }
  alert_rules {
    uuid rule_id PK
    text kind
    jsonb params
    jsonb channels "env-var refs only"
  }
  alerts {
    uuid alert_id PK
    uuid rule_id FK
    text status "open, acknowledged or resolved"
    text dedupe_key
    timestamptz last_sent_at
  }
  review_items {
    uuid item_id PK
    uuid decision_id FK
    text state
    real priority
    uuid claimed_by
    timestamptz lease_until
  }
  audit_log {
    bigint id PK
    uuid key_id
    text action
    jsonb before_value
    jsonb after_value
  }
  schedules {
    uuid schedule_id PK
    text kind
    text cron
    timestamptz next_run_at
  }
  jobs {
    bigint job_id PK
    text kind
    jsonb payload
    timestamptz run_at
    int attempts
    text last_error
  }
```

Shadow decisions point to their primary through `decisions.shadow_of`. Every table
carries `project_id`, and every query filters by it (FR-51).

## 7. Lifecycles

### Policy

```mermaid
stateDiagram-v2
  [*] --> draft: compiled by hand
  [*] --> proposed: scheduled refit
  draft --> proposed: submit
  draft --> [*]: discard
  proposed --> approved: approve (admin, reason)
  proposed --> rejected: reject (admin, reason)
  approved --> active: activate
  approved --> retired: superseded before activation
  active --> retired: another version activated
  retired --> active: rollback (only versions once approved)
  rejected --> [*]
```

### Review item

```mermaid
stateDiagram-v2
  [*] --> queued: escalated or audited decision
  queued --> claimed: claim (15-minute lease)
  claimed --> queued: lease expired, released or skipped
  claimed --> labeled: label (becomes an outcome)
  queued --> closed: outcome arrived elsewhere, or data deleted
  labeled --> [*]
  closed --> [*]
```

## 8. Deployment

```mermaid
flowchart TB
  subgraph vm["One VM (2 vCPU, 4 GB): docker compose"]
    rp["reverse proxy (optional)<br/>Caddy or nginx, TLS"]
    s1["server<br/>uvicorn, one process per core<br/>:8800"]
    w1["worker"]
    pg[("postgres:18<br/>volume pgdata")]
  end
  apps["Apps + SDKs"] --> rp --> s1
  s1 --> pg
  w1 --> pg
  s1 --> up["Upstream: Jev on the internet,<br/>or Ollaya on the host at :11435"]
  w1 --> ch["Slack · SMTP · webhooks"]
  s1 & w1 -. OTLP .-> otel["OTel collector (optional)"]
  bk["Backups: nightly pg_dump<br/>or volume snapshots"] -.-> pg
```

**Scaling, in order of need:**
1. **More servers:** behind a load balancer. They are stateless apart from the record
   buffer.
2. **A second worker:** jobs use `SKIP LOCKED`, so two are safe.
3. **Postgres:** monthly partitions and retention keep it small. A read replica can serve
   reports and the simulator.
4. **ClickHouse:** past about 1 billion decisions, or for heavy analytics, fed by logical
   replication. This needs no API change.

## 9. Data flows and trust boundaries

```mermaid
flowchart LR
  subgraph z1["Zone 1: app network"]
    app["App + SDK"]
  end
  subgraph z2["Zone 2: trimset deployment"]
    srv["server"]
    wrk["worker"]
    db[("Postgres")]
  end
  subgraph z3["Zone 3: model providers"]
    up["Upstream or candidate model"]
  end
  subgraph z4["Zone 4: third-party systems"]
    src["Outcome sources"]
  end
  subgraph z5["Zone 5: alert channels"]
    ch["Slack · SMTP · webhooks"]
  end
  subgraph z6["Zone 6: people"]
    cli["CLI with admin or reviewer keys"]
  end
  app -- "F1 state + questions; proxy key" --> srv
  srv -- "F2 state + questions; the app's upstream key" --> up
  up -- "F3 probabilities" --> srv
  app -- "F4 records: probabilities, hashes, tags; ingest key" --> srv
  srv -- "F5 policy bundle + state-hash key" --> app
  src -- "F6 outcomes; ingest key" --> srv
  cli -- "F7 approvals, labels, settings; admin key" --> srv
  srv --> db
  wrk --> db
  wrk -- "F8 alerts: names and numbers, never state" --> ch
```

| Flow | What crosses | Protection |
|---|---|---|
| F1, F2, F3 | Raw state, in transit only | TLS; state hashed before storage; never logged |
| F4, F6 | Probabilities, hashes, tags, labels | Ingest key; tag limits and allow-list; per-source keys |
| F5 | Policies and the state-hash key | TLS; key scope; fingerprints |
| F7 | Lifecycle changes, labels, settings | Admin or ingest key; audit log |
| F8 | Alert text | Never contains state or tag values; signed webhooks |

## 10. Threat model (STRIDE)

| ID | Kind | Threat | Mitigation in v0.1 | Left over, or later |
|---|---|---|---|---|
| S1 | Spoofing | **API key leakage.** A stolen key is used to log decisions or send outcomes. | Keys stored as hashes; four narrow scopes (proxy, ingest, review, admin; review added in Phase 3); one key per source recommended; revocation within 60 s; last use shown. | A stolen ingest key can poison data until someone notices. |
| S2 | Spoofing | **Proxy key read from a URL** in access logs. | The `proxy` scope can only call the proxy; trimset's logs redact it; docs for nginx and Caddy; header mode recommended. | Logs outside trimset's control. |
| S3 | Spoofing | Outcome sources that can only sign webhooks and can't send headers. | They need a small relay that adds the ingest key. | Native signature checks for common senders in v0.2. |
| T1 | Tampering | **Poisoned outcomes:** an attacker or a buggy integration sends wrong labels. | Every label records its source and key; proposals show the label mix by source and the change from the previous policy; a human must approve; audit sampling cross-checks. | Subtle poisoning could get past an approver. Label anomaly alerts in v0.2. |
| T2 | Tampering | **Policy tampering** in transit or in the database. | TLS; fingerprints checked by the SDK; only admin keys change state; audit log. | Someone who can write to the database can forge a policy and its fingerprint. Signed policies in v0.2 ([ADR 0008](adr/0008-policy-distribution.md)). |
| T3 | Tampering | Fake decisions sent to distort drift checks or calibration. | Same as S1; drift reports show their sample sizes. | Same as S1. |
| R1 | Repudiation | "Who approved this?" | Audit log with key, time, before and after values, and a reason; one admin key per person recommended. | No user accounts in v0.1, so a shared key hides who it was. |
| I1 | Information disclosure | **Personal data in tags** or `external_ref`. | Size limits; an optional allow-list of tag keys; clear docs; tags never appear in alerts. | A misconfigured app can still put personal data in. |
| I2 | Information disclosure | Reversing state hashes by guessing. | Keyed HMAC, with the key derived from a secret outside the database ([ADR 0009](adr/0009-privacy-model.md)). | Anyone holding both the server secret and the database. |
| I3 | Information disclosure | Reading across projects. | Every query is scoped to the key's project; tests on every endpoint (FR-51). | — |
| I4 | Information disclosure | Secrets in the database or logs. | Only environment-variable names are stored; headers and keys are redacted in logs and traces. | — |
| I5 | Information disclosure | Raw state, where a project has opted in. | Separate table; shorter retention; admin-only reads; every read audited. | Accepted by the project when it opts in. |
| D1 | Denial of service | Floods or huge batches. | Size limits on requests and batches; bounded buffers that drop and count; rate limits through the reverse proxy (documented). | Rate limits per key in v0.2. |
| D2 | Denial of service | A slow upstream. | Timeout per project; logging is separate from the response path. | — |
| D3 | Denial of service | Shadow traffic running up costs. | Sampling share and a daily cap per question. | — |
| E1 | Elevation of privilege | An ingest or proxy key used for admin actions. | Scope checked on every route, denied by default; tests. | — |
| E2 | Elevation of privilege | Server-side request forgery through admin-set URLs (upstreams, shadows, alert webhooks). | No URL can be set through the API. Upstreams and alert channels are defined by the operator in the server environment and picked by name; no redirects are followed. | An operator who defines an internal URL. |
| E4 | Elevation of privilege | **Confused deputy:** an admin key makes the server send a secret (`TRIMSET_SECRET`, a database URL, another project's upstream key) somewhere it chooses. | The API accepts no variable names, headers or URLs. Each upstream key is bound to its own URL in the operator's config; a key paired with an http URL stops the server from starting; `_PROJECTS` limits which projects may use an upstream. | — |
| E3 | Elevation of privilege | Injection through the review link template, for example a `javascript:` URL. | Only admins set it; it must be an absolute http(s) URL; `external_ref` is URL-encoded; a filled link that isn't http(s) is dropped; the v0.1 CLI prints plain text. | The v2 dashboard must still escape it. |

## 11. Changes to Phase 1 that come from this phase

- **FR-01 / record:** `state_sha256` becomes `state_hash`, with `state_hash_alg: "hmac-sha256/1"` ([ADR 0009](adr/0009-privacy-model.md)).
- **FR-07:** `X-Trimset-Decision-Id` carries one id per question, comma-separated in question order ([ADR 0011](adr/0011-proxy-design.md)).
- **FR-52:** a third key scope, `proxy` ([ADR 0011](adr/0011-proxy-design.md)).
- **Homepage:** the adoption snippet needs the `/p/<proxy key>` path. This gets fixed with the other homepage claims before Phase 7.
