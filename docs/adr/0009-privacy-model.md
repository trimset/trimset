# 0009. Privacy model: hashes by default, keyed

**Status:** proposed (Phase 2, 2026-11-10) · **Requirements:** FR-09, FR-50, FR-57, NFR-11, NFR-12

## Context

A state is often a customer's message, a payment or a prompt. The brief says raw state is
never stored unless a project opts in, and only hashes and tags are kept. Phase 1 open
question 2: a plain SHA-256 of a short or guessable state (an email address, an order
number) can be reversed by hashing guesses.

## Options

1. **Plain SHA-256.** Simple, and the same everywhere. Reversible by guessing short
   states.
2. **HMAC-SHA-256 with a per-project key stored in the database.** Safe against
   outsiders, but anyone with a database dump has both the key and the hashes.
3. **HMAC-SHA-256 with a per-project key derived from a server secret held outside the
   database.**

## Decision

**State hash (option 3):**
- `state_hash = HMAC-SHA-256(k_project, RFC 8785 canonical JSON of the state)`, where
  `k_project = HMAC-SHA-256(TRIMSET_SECRET, project_id)`.
- The record calls the field `state_hash`, with `state_hash_alg: "hmac-sha256/1"`. This
  replaces the brief's `state_sha256`.
- The SDK receives `k_project` in the policy bundle, which only the project's keys can
  read. Rotating `TRIMSET_SECRET` changes every future hash; this is documented.

**Raw state:**
- Read only to forward it and hash it. Never written to logs, traces, errors or alerts.
- If the project opts in, it goes to `decision_states`, with its own retention (30 days
  by default).
- Only admin keys can read it. Every read, and every change to the setting, goes in the
  audit log.

**Tags:**
- Strings only: at most 20 keys, keys up to 64 characters, values up to 256.
- There is an optional allow-list of keys per project.
- The docs say to put no personal data in tags or in `external_ref`.

**Secrets are never in the database.** Upstream credentials and alert-channel secrets are
stored as the *name* of an environment variable and resolved when used. Only names
starting with `TRIMSET_UPSTREAM_` or `TRIMSET_ALERT_` are accepted, so an admin key can
never make the server send `TRIMSET_SECRET` or any other variable (Phase 3 security
review).

**Deletion and telemetry:**
- Deletion (FR-57) reaches decisions, outcomes, review items and raw state.
- Fits keep parameters, bin counts and anonymous (score, label) pairs for the simulator,
  with no ids.
- trimset collects no telemetry.

## Consequences

- A database leak alone doesn't let anyone reverse state hashes. The server secret has to
  leak too.
- Hashes from different projects can't be matched with each other.
- Reviewers get context from the link template (FR-50), not from stored state.
