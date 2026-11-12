# Changelog

All notable changes are listed here, newest first, in the style of
[Keep a Changelog](https://keepachangelog.com). Versions follow
[semantic versioning](https://semver.org); before 1.0, a minor version may change an
interface. How a release is made: [docs/releasing.md](docs/releasing.md).

## [Unreleased]

The first alpha: capture, outcomes, calibration, a compiled policy with a stated
guarantee, and `gate()` in your app, end to end. Run it from a checkout with Docker
Compose; three demos in `examples/` show each stage.

### Added

- **Capture.** A proxy (Rust) in front of your decision model: it forwards TypeSafe's
  `/v1/systemone` and OpenAI-style `/v1/decisions` calls unchanged, logs them off the
  request path, and keeps forwarding when the database is down. `POST /v1/decisions:log`
  takes records from any backend. Every record carries the exact model version that
  answered.
- **Outcomes.** `POST /v1/outcomes`, matched by decision id or your own reference; early
  outcomes wait for their decision; corrections keep their history; default labels by
  rule ("no chargeback in 120 days"); audit sampling, which sends a share of decisions
  to a person whatever the policy says, so the cases it would accept get checked too.
- **Calibration and thresholds.** Temperature, Platt and isotonic fits chosen by
  cross-validation, with log loss, Brier and ECE on held-out data; a simulator; and a
  compiler that certifies a target ("false passes at most 2%, with 95% confidence") with
  Learn-then-Test and exact binomial tests, or says why it cannot.
- **Policies.** Versions with diffs; submit, approve, activate and retire, with a reason
  on the record; one-command rollback; an audit log; scheduled refits that only propose;
  the label mix by source and key in every diff.
- **Python SDK and CLI** (`pip install trimset`, Python 3.11+). `trimset.Client.gate()`
  decides locally from a verified policy cache without a network call, never raises and
  never blocks; `log_exchange`, `log`, `outcome` and `instrument()` report back from a
  background thread with bounded memory; the request's state is hashed in your process
  and never sent. The `trimset` command calibrates, simulates, compiles and moves
  policies, and shows coverage and decisions.
- **Server.** Four key scopes (ingest, proxy, review, admin), bootstrap from the command
  line, JSON logs with key redaction, Postgres migrations that run on start in the
  compose setup.
- **Examples.** `examples/m1-capture`, `m2-refund-fraud` and `m3-ticket-triage`, each run
  by CI on every change.
- **Images** for linux/amd64 and linux/arm64 at `ghcr.io/trimset/trimset`.

### Not in this alpha

- Drift and silent-upgrade alerts, health checks.
- Shadow models and the review queue.
- Retention controls and deleting everything for one external reference.
- The TypeScript SDK and the dashboard served by your install.
